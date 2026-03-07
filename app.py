import os
import re
import json
import time
import html
import urllib.parse
import requests
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI

st.set_page_config(
    page_title="미야언니",
    layout="centered",
    initial_sidebar_state="collapsed"
)

OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
if not OPENAI_API_KEY:
    st.error("OPENAI_API_KEY가 필요합니다. Streamlit Secrets에 OPENAI_API_KEY를 추가해주세요.")
    st.stop()

client = OpenAI(api_key=OPENAI_API_KEY)

POLICY_DB = {
    "shipping": {
        "courier": "CJ 대한통운",
        "shipping_fee": 3000,
        "free_shipping_over": 70000,
        "delivery_time": "결제 완료 후 2~4일 (영업일 기준)",
        "same_day_dispatch_rule": "오후 2시 이전 주문은 당일 출고",
        "reservation_product": "예약상품 개념 없음",
        "combined_shipping": "합배송 가능(1박스 기준). 단 박스크기 초과 시 합배송 불가",
        "dispatch_order": "결제 순서대로 순차 출고",
        "jeju": "제주 및 도서산간 지역은 추가배송비가 자동 부과됩니다."
    },
    "exchange_return": {
        "exchange_possible": "사이즈 교환 가능 / 동일상품 교환 가능 / 타상품 교환 가능",
        "period": "상품 수령 후 7일 이내",
        "exchange_fee": 6000,
        "return_fee_rule": "단순 변심 반품: 반품 후 주문금액이 7만원 이상이면 편도 3,000원 / 7만원 미만이면 왕복 6,000원",
        "defect_wrong": "불량/오배송은 미샵 부담입니다."
    }
}

SYSTEM_PROMPT = """
너는 '미샵 쇼핑친구 미야언니'다.
4050 여성 고객의 쇼핑 결정을 돕고, 반품을 줄이며, 정책/사이즈/코디 질문에 정확히 답한다.

규칙:
- 친근하지만 가볍지 않게
- 결론 먼저, 근거 2~3개
- 상품명은 product_context.product_name이 있으면 그것만 사용
- 상품명 없으면 '지금 보시는 상품'이라고 표현
- SEO용 긴 제목 반복 금지
- 정책은 POLICY_DB 기준
- 확실하지 않은 내용은 단정하지 말 것
- 답변은 4~8문장 정도로 정리
"""

GENERIC_NAMES = {"미샵", "misharp", "MISHARP", "미샵여성", "Misharp"}

def ensure_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "profile" not in st.session_state:
        st.session_state.profile = None
    if "last_context_key" not in st.session_state:
        st.session_state.last_context_key = ""
    if "last_action_nonce" not in st.session_state:
        st.session_state.last_action_nonce = ""

ensure_state()

def reset_all():
    st.session_state.messages = []
    st.session_state.profile = None

qp = st.query_params
current_url = qp.get("url", "") or ""
product_no = qp.get("pn", "") or ""
product_name_q = qp.get("pname", "") or ""
action = qp.get("action", "") or ""
nonce = qp.get("nonce", "") or ""

context_key = f"{current_url}|{product_no}|{product_name_q}"
if context_key != st.session_state.last_context_key:
    st.session_state.last_context_key = context_key
    st.session_state.messages = []

def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()

def is_generic_name(name: str) -> bool:
    if not name:
        return True
    name = clean_text(name)
    if name in GENERIC_NAMES:
        return True
    lower = name.lower()
    if lower in {x.lower() for x in GENERIC_NAMES}:
        return True
    if len(name) <= 2:
        return True
    return False

def split_meta_title(title: str) -> str:
    if not title:
        return ""
    title = clean_text(title)
    for sep in ["|", "-", "–", "—"]:
        parts = [x.strip() for x in title.split(sep) if x.strip()]
        if parts and 3 <= len(parts[0]) <= 50:
            return parts[0]
    return title

def extract_product_name_from_soup(soup: BeautifulSoup) -> str:
    candidates = []

    priority_selectors = [
        "#span_product_name",
        "#span_product_name_mobile",
        ".infoArea #span_product_name",
        ".infoArea .headingArea h2",
        ".infoArea .headingArea h3",
        ".headingArea h2",
        ".headingArea h3",
        ".prdName",
        ".name",
        "h2.name",
        "h3.name",
        "h1"
    ]

    for selector in priority_selectors:
        try:
            for tag in soup.select(selector):
                txt = clean_text(tag.get_text(" ", strip=True))
                if 3 <= len(txt) <= 50:
                    candidates.append(txt)
        except Exception:
            pass

    # JSON-LD Product
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = script.string or script.get_text()
            if not raw:
                continue
            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "Product" and item.get("name"):
                    txt = clean_text(str(item["name"]))
                    if 3 <= len(txt) <= 50:
                        candidates.append(txt)
        except Exception:
            pass

    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        txt = split_meta_title(og.get("content", ""))
        if 3 <= len(txt) <= 50:
            candidates.append(txt)

    if soup.title and soup.title.string:
        txt = split_meta_title(soup.title.string)
        if 3 <= len(txt) <= 50:
            candidates.append(txt)

    seen = set()
    uniq = []
    for c in candidates:
        c = clean_text(c)
        if c and c not in seen:
            uniq.append(c)
            seen.add(c)

    keywords = ["자켓", "슬랙스", "니트", "티셔츠", "블라우스", "셔츠", "원피스", "팬츠", "데님", "가디건", "코트", "점퍼", "맨투맨"]
    blacklist = ["상품결제정보", "배송정보", "교환 및 반품정보", "전체상품", "로그인", "회원가입", "장바구니", "마이페이지"]

    def score_name(name: str):
        score = 0
        if 4 <= len(name) <= 30:
            score += 6
        if any(k in name for k in keywords):
            score += 6
        if any(b == name for b in blacklist):
            score -= 20
        if is_generic_name(name):
            score -= 20
        if len(name) > 38:
            score -= 2
        if "/" in name or "|" in name:
            score -= 2
        return score

    if not uniq:
        return ""

    uniq.sort(key=score_name, reverse=True)
    best = uniq[0]
    return "" if is_generic_name(best) else best

def split_sections(text: str) -> dict:
    if not text:
        return {"summary": "", "material": "", "fit": "", "size_tip": "", "shipping": ""}

    lines = [clean_text(x) for x in text.split("\n")]
    lines = [x for x in lines if x]
    joined = "\n".join(lines)

    def extract_by_keywords(keywords, max_len=1200):
        matched = []
        for line in lines:
            if any(k in line for k in keywords):
                matched.append(line)
        return " / ".join(matched)[:max_len]

    return {
        "summary": joined[:2500],
        "material": extract_by_keywords(["소재", "원단", "혼용", "%", "면", "폴리", "레이온", "아크릴", "울", "스판", "비스코스", "나일론"]),
        "fit": extract_by_keywords(["핏", "여유", "라인", "체형", "복부", "팔뚝", "허벅지", "힙", "루즈", "와이드", "슬림", "정핏", "세미", "커버"]),
        "size_tip": extract_by_keywords(["사이즈", "정사이즈", "추천", "55", "66", "77", "S", "M", "L", "XL", "허리", "총장", "힙", "허벅지"]),
        "shipping": extract_by_keywords(["배송", "출고", "교환", "반품", "배송비"])
    }

def guess_category(name: str, text: str) -> str:
    corpus = f"{name} {text}"
    mapping = {
        "슬랙스": ["슬랙스", "팬츠", "바지"],
        "블라우스": ["블라우스"],
        "셔츠": ["셔츠"],
        "티셔츠": ["티셔츠", "탑"],
        "니트": ["니트", "가디건"],
        "자켓": ["자켓", "재킷"],
        "원피스": ["원피스"],
        "데님": ["데님", "청바지"],
        "코트": ["코트"],
        "맨투맨": ["맨투맨"],
    }
    for cat, keywords in mapping.items():
        if any(k in corpus for k in keywords):
            return cat
    return "기타"

def fetch_product_context(url: str, passed_name: str = "") -> dict:
    if not url:
        return None

    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    raw_text = soup.get_text("\n")
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()

    passed_name = clean_text(passed_name)
    if is_generic_name(passed_name):
        passed_name = ""

    html_name = extract_product_name_from_soup(soup)
    product_name = passed_name if passed_name else html_name
    if is_generic_name(product_name):
        product_name = "지금 보시는 상품"

    sections = split_sections(raw_text)
    category = guess_category(product_name, raw_text)

    return {
        "product_name": product_name,
        "category": category,
        "summary": sections["summary"],
        "material": sections["material"],
        "fit": sections["fit"],
        "size_tip": sections["size_tip"],
        "shipping": sections["shipping"],
        "raw_excerpt": raw_text[:4000]
    }

@st.cache_data(ttl=300, show_spinner=False)
def fetch_product_context_cached(url: str, passed_name: str = "") -> dict:
    try:
        return fetch_product_context(url, passed_name)
    except Exception as e:
        safe_name = clean_text(passed_name)
        if is_generic_name(safe_name):
            safe_name = "지금 보시는 상품"
        return {
            "product_name": safe_name,
            "category": "기타",
            "summary": "",
            "material": "",
            "fit": "",
            "size_tip": "",
            "shipping": "",
            "raw_excerpt": f"[상품 정보를 가져오지 못했습니다: {e}]"
        }

def get_fast_policy_answer(user_text: str) -> str | None:
    q = user_text.replace(" ", "").lower()

    if any(k in q for k in ["배송비", "무료배송"]):
        return (
            f"배송은 {POLICY_DB['shipping']['courier']}를 이용하고요 🙂\n"
            f"배송비는 {POLICY_DB['shipping']['shipping_fee']:,}원이고, "
            f"{POLICY_DB['shipping']['free_shipping_over']:,}원 이상 구매 시 무료배송이에요."
        )

    if any(k in q for k in ["언제출고", "출고", "당일출고"]):
        return (
            f"{POLICY_DB['shipping']['same_day_dispatch_rule']}예요 🙂\n"
            f"보통은 {POLICY_DB['shipping']['delivery_time']} 정도 보시면 되고,\n"
            f"결제 순서대로 순차 출고됩니다."
        )

    if any(k in q for k in ["교환", "사이즈교환"]):
        return (
            "교환은 가능해요 🙂\n"
            f"{POLICY_DB['exchange_return']['exchange_possible']}이고,\n"
            f"{POLICY_DB['exchange_return']['period']} 이내 접수해주시면 됩니다.\n"
            f"단순 변심 교환은 왕복 {POLICY_DB['exchange_return']['exchange_fee']:,}원이에요."
        )

    return None

def get_llm_answer(user_text: str, current_url: str, product_no: str, product_context: dict | None) -> str:
    context_pack = {
        "policy_db": POLICY_DB,
        "viewer_context": {
            "url": current_url,
            "is_product_page": bool(product_no),
            "product_no": product_no
        },
        "product_context": product_context
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "참고 데이터(JSON):\n" + json.dumps(context_pack, ensure_ascii=False)}
    ]

    history = st.session_state.messages[-8:]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})

    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.35,
        max_tokens=420
    )
    return resp.choices[0].message.content

def process_user_message(user_text: str, current_url: str, product_no: str, product_context: dict | None):
    st.session_state.messages.append({"role": "user", "content": user_text})
    fast = get_fast_policy_answer(user_text)
    if fast:
        st.session_state.messages.append({"role": "assistant", "content": fast})
        return
    answer = get_llm_answer(user_text, current_url, product_no, product_context)
    st.session_state.messages.append({"role": "assistant", "content": answer})

product_context = fetch_product_context_cached(current_url, product_name_q) if current_url else None

st.markdown("""
<style>
.block-container {
  padding-top: calc(4.4rem + env(safe-area-inset-top));
  padding-bottom: 8rem;
  max-width: 760px;
}
header[data-testid="stHeader"] { height: 0px; }
div[data-testid="stToolbar"] { visibility: hidden; height: 0px; }

.main-title {
  font-size: 44px;
  font-weight: 800;
  line-height: 1.15;
  margin: 0;
  white-space: nowrap;
}
.main-subtitle {
  margin-top: 8px;
  color: rgba(255,255,255,0.72);
  font-size: 14px;
}
@media (max-width: 768px) {
  .block-container {
    padding-top: calc(5.8rem + env(safe-area-inset-top));
    padding-bottom: 9rem;
  }
  .main-title {
    font-size: 26px;
    white-space: nowrap;
  }
  .main-subtitle {
    font-size: 13px;
  }
}

.quickbar {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  margin-top: 16px;
  margin-bottom: 8px;
}
.quickbtn {
  display: block;
  text-align: center;
  text-decoration: none;
  padding: 10px 6px;
  border-radius: 12px;
  border: 1px solid rgba(255,255,255,0.12);
  color: #ffffff;
  background: rgba(255,255,255,0.02);
  font-size: 13px;
  font-weight: 600;
}
@media (max-width: 768px) {
  .quickbar {
    grid-template-columns: repeat(4, 1fr);
    gap: 4px;
  }
  .quickbtn {
    font-size: 11px;
    padding: 10px 2px;
  }
}

.msg-row {
  display: flex;
  width: 100%;
  margin: 12px 0;
}
.msg-row.user {
  justify-content: flex-end;
}
.msg-row.bot {
  justify-content: flex-start;
}
.msg-col {
  max-width: 78%;
}
.msg-name {
  font-size: 12px;
  opacity: .72;
  margin: 0 0 4px 4px;
}
.msg-bubble {
  padding: 12px 14px;
  border-radius: 18px;
  font-size: 15px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: keep-all;
}
.msg-bubble.user {
  background: #2f3640;
  color: #fff;
  border-bottom-right-radius: 6px;
}
.msg-bubble.bot {
  background: #111827;
  color: #fff;
  border: 1px solid rgba(255,255,255,0.08);
  border-bottom-left-radius: 6px;
}

/* 입력창을 더 위로 올림 */
div[data-testid="stChatInput"] {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: 78px;
  width: min(720px, calc(100% - 24px));
  z-index: 9999;
}
@media (max-width: 768px) {
  div[data-testid="stChatInput"] {
    bottom: 108px;
    width: calc(100% - 24px);
  }
}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">미샵 쇼핑친구 미야언니</div>', unsafe_allow_html=True)
st.markdown('<div class="main-subtitle">쇼핑 고민될 때, 친구처럼 같이 보고 전문가처럼 딱 정리해드릴게요.</div>', unsafe_allow_html=True)

encoded_url = urllib.parse.quote(current_url, safe="")
encoded_pn = urllib.parse.quote(product_no, safe="")
encoded_pname = urllib.parse.quote(product_name_q, safe="")

def quick_link(label, action_name):
    return (
        f'<a class="quickbtn" href="?url={encoded_url}&pn={encoded_pn}&pname={encoded_pname}'
        f'&action={action_name}&nonce={int(time.time() * 1000)}">{label}</a>'
    )

quick_html = f"""
<div class="quickbar">
  {quick_link("초기화", "reset")}
  {quick_link("사이즈", "size")}
  {quick_link("코디", "codi")}
  {quick_link("배송/교환", "policy")}
</div>
"""
st.markdown(quick_html, unsafe_allow_html=True)

if action and nonce and st.session_state.last_action_nonce != nonce:
    st.session_state.last_action_nonce = nonce

    if action == "reset":
        reset_all()
        st.rerun()
    elif action == "size":
        process_user_message(
            "지금 보이는 상품 기준으로 사이즈 상담해줘. 결론 먼저 말하고, 근거를 상품 정보 기준으로 설명해줘. 정보가 부족하면 추가 질문해줘.",
            current_url, product_no, product_context
        )
        st.rerun()
    elif action == "codi":
        process_user_message(
            "지금 보이는 상품 기준으로 코디 추천해줘. 상품 정보에 근거해서만 말해주고, 부족하면 안전하게 표현해줘.",
            current_url, product_no, product_context
        )
        st.rerun()
    elif action == "policy":
        process_user_message(
            "이 상품 배송이나 교환 반품 핵심만 알려줘.",
            current_url, product_no, product_context
        )
        st.rerun()

if not st.session_state.messages:
    if product_context:
        name = product_context.get("product_name", "지금 보시는 상품")
        st.session_state.messages.append({
            "role": "assistant",
            "content": f"안녕하세요 🙂 미야언니예요.\n지금 보고 계신 '{name}' 기준으로 같이 볼까요?\n사이즈 / 코디 / 배송·교환 중 뭐부터 볼까요?"
        })
    else:
        st.session_state.messages.append({
            "role": "assistant",
            "content": "안녕하세요 🙂 미야언니예요.\n무엇을 도와드릴까요?\n예) 출근룩 추천 / 배송비 / 교환비 / 66인데 이 옷 괜찮을까요?"
        })

st.divider()

for msg in st.session_state.messages:
    safe_text = html.escape(msg["content"]).replace("\n", "<br>")
    if msg["role"] == "user":
        st.markdown(
            f'<div class="msg-row user"><div class="msg-col"><div class="msg-bubble user">{safe_text}</div></div></div>',
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            f'<div class="msg-row bot"><div class="msg-col"><div class="msg-name">미야언니</div><div class="msg-bubble bot">{safe_text}</div></div></div>',
            unsafe_allow_html=True
        )

st.markdown('<div id="chat-bottom"></div>', unsafe_allow_html=True)
st.markdown("""
<script>
const el = window.parent.document.getElementById("chat-bottom");
if (el) { el.scrollIntoView({behavior: "smooth"}); }
</script>
""", unsafe_allow_html=True)

user_input = st.chat_input("메시지를 입력하세요…")
if user_input:
    process_user_message(user_input, current_url, product_no, product_context)
    st.rerun()
