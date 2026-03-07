import os
import re
import json
import time
import html
import requests
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI

st.set_page_config(
    page_title="미야언니",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# -----------------------------
# OpenAI
# -----------------------------
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
if not OPENAI_API_KEY:
    st.error("OPENAI_API_KEY가 필요합니다. Streamlit Secrets에 OPENAI_API_KEY를 추가해주세요.")
    st.stop()

client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
# 정책 DB
# -----------------------------
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
    },
    "payment": {
        "bank_transfer": "주문 후 3일 이내 입금해야 하며, 미입금 시 자동취소됩니다.",
        "card": "카드 결제 후 부분취소 가능하며, 불가 카드인 경우 고객센터에서 별도 안내드립니다."
    },
    "point": {
        "use_min": "적립금은 3,000원 이상부터 사용 가능합니다.",
        "expiry": "유효기간은 적립일로부터 1년입니다.",
        "purchase": "구매금액의 1% 적립",
        "text_review": "일반후기 500원",
        "photo_review": "포토후기 2,000원"
    }
}

# -----------------------------
# 시스템 프롬프트
# -----------------------------
SYSTEM_PROMPT = """
너는 '미샵 쇼핑친구 미야언니'다.
너의 역할은 4050 여성 고객이 쇼핑할 때 친구처럼 같이 봐주되, 전문가처럼 결론을 정리해주는 것이다.

핵심 목표:
1. 고객이 상품 선택에서 실패하지 않도록 돕기
2. 반품 가능성을 줄이기
3. 체형/사이즈/코디/정책 질문에 정확하게 답하기

말투 규칙:
- 항상 친근하지만 가볍지 않게
- 결론 먼저 말하고, 근거를 2~3개 설명
- 과장 금지
- 모르면 솔직하게 말하기
- 상품 정보가 부족하면 '정확한 판단은 어렵다'고 말하기
- 상품명이나 핵심 정보가 불명확하면 함부로 추천하지 않기

답변 구조:
1. 공감 또는 상황 이해
2. 결론 (추천 / 주의 / 비추천)
3. 이유 2~3개
4. 필요한 경우 추가 질문

중요 규칙:
- 당일출고 기준은 반드시 '오후 2시 이전 주문'으로만 답한다.
- 정책 답변은 POLICY_DB 기준으로 답한다.
- 상품 상담에서는 제공된 상품 구조 데이터(product_context)를 최우선으로 참고한다.
- 상품명 모를 때는 [상품명] 같은 표현 쓰지 말고, '지금 보시는 상품'이라고 표현한다.
- 상품페이지에서 실제 읽힌 근거 없이 '출근룩으로 좋다', '체형 커버된다' 같은 일반론을 남발하지 말 것.
- 상품 구조 데이터에 없는 내용은 확정적으로 말하지 말 것.
- 답변은 너무 길지 않게, 4~8문장 정도로 읽기 쉽게 정리할 것.
"""

# -----------------------------
# 상태
# -----------------------------
def ensure_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "profile" not in st.session_state:
        st.session_state.profile = None
    if "profile_saved" not in st.session_state:
        st.session_state.profile_saved = False
    if "page_url" not in st.session_state:
        st.session_state.page_url = ""
    if "product_context" not in st.session_state:
        st.session_state.product_context = None
    if "last_action_at" not in st.session_state:
        st.session_state.last_action_at = 0.0
    if "last_action_key" not in st.session_state:
        st.session_state.last_action_key = ""
    if "show_profile_form" not in st.session_state:
        st.session_state.show_profile_form = False

ensure_state()

def reset_all():
    st.session_state.messages = []
    st.session_state.profile = None
    st.session_state.profile_saved = False
    st.session_state.show_profile_form = False

def debounce(action_key: str, min_sec: float = 0.8) -> bool:
    now = time.time()
    if st.session_state.last_action_key == action_key and (now - st.session_state.last_action_at) < min_sec:
        return False
    st.session_state.last_action_key = action_key
    st.session_state.last_action_at = now
    return True

# -----------------------------
# URL / 상품 추출
# -----------------------------
def extract_product_no(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"product_no=(\d+)", url)
    return m.group(1) if m else None

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text

def find_product_name(soup: BeautifulSoup) -> str:
    candidates = []

    # title
    if soup.title and soup.title.string:
        candidates.append(clean_text(soup.title.string))

    # og:title
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        candidates.append(clean_text(og["content"]))

    # h1/h2/h3
    for tag in soup.find_all(["h1", "h2", "h3"]):
        txt = clean_text(tag.get_text(" ", strip=True))
        if txt and len(txt) <= 80:
            candidates.append(txt)

    # strong/span/div 중 상품명처럼 보이는 짧은 텍스트
    for tag in soup.find_all(["strong", "span", "div"]):
        txt = clean_text(tag.get_text(" ", strip=True))
        if 3 <= len(txt) <= 60:
            if any(word in txt for word in ["자켓", "슬랙스", "니트", "티셔츠", "블라우스", "셔츠", "원피스", "팬츠", "데님", "가디건", "코트"]):
                candidates.append(txt)

    # 중복 제거
    seen = set()
    uniq = []
    for c in candidates:
        if c not in seen:
            uniq.append(c)
            seen.add(c)

    # 너무 흔한 문자열 제거
    blacklist = ["미샵", "MISHARP", "상품결제정보", "배송정보", "교환 및 반품정보", "상품명", "전체상품"]
    filtered = []
    for c in uniq:
        if any(b == c or b in c and len(c) < 8 for b in blacklist):
            continue
        filtered.append(c)

    if filtered:
        # 가장 상품명스러운 첫 번째 것 사용
        return filtered[0]
    return ""

def split_sections(text: str) -> dict:
    if not text:
        return {
            "summary": "",
            "material": "",
            "fit": "",
            "size_tip": "",
            "shipping": ""
        }

    lines = [clean_text(x) for x in text.split("\n")]
    lines = [x for x in lines if x]

    joined = "\n".join(lines)

    def extract_by_keywords(keywords, max_len=1200):
        matched = []
        for line in lines:
            if any(k in line for k in keywords):
                matched.append(line)
        result = " / ".join(matched)
        return result[:max_len]

    return {
        "summary": joined[:2500],
        "material": extract_by_keywords(["소재", "원단", "혼용", "%", "면", "폴리", "레이온", "아크릴", "울", "스판", "비스코스", "나일론"]),
        "fit": extract_by_keywords(["핏", "여유", "라인", "체형", "복부", "팔뚝", "허벅지", "힙", "루즈", "와이드", "슬림", "정핏", "세미", "커버"]),
        "size_tip": extract_by_keywords(["사이즈", "정사이즈", "추천", "55", "66", "77", "S", "M", "L", "XL"]),
        "shipping": extract_by_keywords(["배송", "출고", "교환", "반품", "배송비"])
    }

def guess_category(name: str, text: str) -> str:
    corpus = f"{name} {text}"
    mapping = {
        "슬랙스": ["슬랙스", "팬츠", "바지"],
        "블라우스": ["블라우스"],
        "셔츠": ["셔츠"],
        "티셔츠": ["티셔츠", "티", "탑"],
        "니트": ["니트", "가디건"],
        "자켓": ["자켓", "재킷"],
        "원피스": ["원피스"],
        "데님": ["데님", "청바지"],
        "코트": ["코트"],
    }
    for cat, keywords in mapping.items():
        if any(k in corpus for k in keywords):
            return cat
    return "기타"

def fetch_product_context(url: str) -> dict:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    raw_text = soup.get_text("\n")
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()

    product_name = find_product_name(soup)
    sections = split_sections(raw_text)
    category = guess_category(product_name, raw_text)

    return {
        "product_name": product_name if product_name else "지금 보시는 상품",
        "category": category,
        "summary": sections["summary"],
        "material": sections["material"],
        "fit": sections["fit"],
        "size_tip": sections["size_tip"],
        "shipping": sections["shipping"],
        "raw_excerpt": raw_text[:4000]
    }

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_product_context_cached(url: str) -> dict:
    try:
        return fetch_product_context(url)
    except Exception as e:
        return {
            "product_name": "지금 보시는 상품",
            "category": "기타",
            "summary": "",
            "material": "",
            "fit": "",
            "size_tip": "",
            "shipping": "",
            "raw_excerpt": f"[상품 정보를 가져오지 못했습니다: {e}]"
        }

# -----------------------------
# 빠른 정책 응답
# -----------------------------
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

    if any(k in q for k in ["배송언제", "언제와", "언제도착", "배송기간"]):
        return (
            f"보통 배송은 {POLICY_DB['shipping']['delivery_time']} 정도예요 🙂\n"
            f"오후 2시 이전 주문은 당일 출고되고,\n"
            f"이후 주문은 다음 영업일 출고로 보시면 됩니다."
        )

    if any(k in q for k in ["교환", "사이즈교환"]):
        return (
            "교환은 가능해요 🙂\n"
            f"{POLICY_DB['exchange_return']['exchange_possible']}이고,\n"
            f"{POLICY_DB['exchange_return']['period']} 이내 접수해주시면 됩니다.\n"
            f"단순 변심 교환은 왕복 {POLICY_DB['exchange_return']['exchange_fee']:,}원이에요."
        )

    if any(k in q for k in ["반품", "환불"]):
        return (
            "반품도 가능해요 🙂\n"
            f"{POLICY_DB['exchange_return']['period']} 이내 접수해주시면 되고,\n"
            f"{POLICY_DB['exchange_return']['return_fee_rule']} 기준으로 진행됩니다."
        )

    if any(k in q for k in ["합배송"]):
        return f"{POLICY_DB['shipping']['combined_shipping']} 🙂"

    if any(k in q for k in ["적립금"]):
        return (
            f"{POLICY_DB['point']['use_min']} {POLICY_DB['point']['expiry']}\n"
            f"기본적으로 {POLICY_DB['point']['purchase']}되고,\n"
            f"후기는 일반 {POLICY_DB['point']['text_review']}, 포토 {POLICY_DB['point']['photo_review']}이에요 🙂"
        )

    if any(k in q for k in ["무통장", "입금", "카드", "부분취소"]):
        return (
            f"무통장 입금은 {POLICY_DB['payment']['bank_transfer']}\n"
            f"카드 결제는 {POLICY_DB['payment']['card']}"
        )

    return None

# -----------------------------
# GPT 응답
# -----------------------------
def get_llm_answer(user_text: str, current_url: str, product_no: str | None, product_context: dict | None) -> str:
    context_pack = {
        "policy_db": POLICY_DB,
        "viewer_context": {
            "url": current_url,
            "is_product_page": bool(product_no),
            "product_no": product_no
        },
        "product_context": product_context,
        "customer_profile": st.session_state.profile
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
        max_tokens=450
    )
    return resp.choices[0].message.content

# -----------------------------
# 메시지 처리
# -----------------------------
def process_user_message(user_text: str, current_url: str, product_no: str | None, product_context: dict | None):
    st.session_state.messages.append({"role": "user", "content": user_text})

    fast = get_fast_policy_answer(user_text)
    if fast:
        st.session_state.messages.append({"role": "assistant", "content": fast})
        return

    answer = get_llm_answer(user_text, current_url, product_no, product_context)
    st.session_state.messages.append({"role": "assistant", "content": answer})

# -----------------------------
# 체형 입력 폼
# -----------------------------
def profile_form():
    st.markdown("### 정확한 추천 받기 (체형 입력 30초)")
    c1, c2 = st.columns(2)
    with c1:
        height = st.selectbox("키", ["선택", "150 이하", "150~155", "156~160", "161~165", "166~170", "170 이상"])
        size = st.selectbox("평소 사이즈", ["선택", "55", "66", "66반", "77", "77반", "88"])
    with c2:
        weight = st.selectbox("몸무게(선택)", ["선택", "45 이하", "46~50", "51~55", "56~60", "61~65", "66 이상"])
        tpo = st.selectbox("스타일/TPO", ["선택", "출근룩", "모임룩", "데일리룩", "여행룩"])
    concerns = st.multiselect("체형 고민(복수)", ["복부", "팔뚝", "힙", "허벅지", "상체통통", "하체통통", "전체통통"])

    colA, colB = st.columns([1, 1])
    with colA:
        ok = st.button("입력 완료", use_container_width=True)
    with colB:
        save = st.checkbox("다음에도 재사용(저장)", value=False)

    if ok:
        st.session_state.profile = {
            "height": None if height == "선택" else height,
            "weight": None if weight == "선택" else weight,
            "size": None if size == "선택" else size,
            "tpo": None if tpo == "선택" else tpo,
            "concerns": concerns
        }
        st.session_state.profile_saved = bool(save)
        st.success("체형 정보를 저장했어요 🙂")
        st.session_state.show_profile_form = False
        st.rerun()

# -----------------------------
# 현재 페이지 로드
# -----------------------------
query = st.query_params
current_url = query.get("url", "")
product_no = extract_product_no(current_url)
is_product_page = bool(product_no)

if current_url and st.session_state.page_url != current_url:
    st.session_state.page_url = current_url
    st.session_state.product_context = fetch_product_context_cached(current_url)

product_context = st.session_state.product_context

# -----------------------------
# CSS
# -----------------------------
st.markdown("""
<style>
.block-container {
  padding-top: 1.8rem;
  padding-bottom: 6rem;
  max-width: 760px;
}
header[data-testid="stHeader"] { height: 0px; }
div[data-testid="stToolbar"] { visibility: hidden; height: 0px; }

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

.input-hint {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: 10px;
  width: min(720px, calc(100% - 24px));
  background: rgba(0,0,0,.65);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 14px;
  padding: 9px 12px;
  color: rgba(255,255,255,.82);
  font-size: 13px;
  z-index: 9998;
  backdrop-filter: blur(10px);
}

div[data-testid="stChatInput"] {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: 48px;
  width: min(720px, calc(100% - 24px));
  z-index: 9999;
}
</style>
""", unsafe_allow_html=True)

# -----------------------------
# 헤더
# -----------------------------
top = st.columns([1, 0.25])
with top[0]:
    st.markdown("## 미샵 쇼핑친구 미야언니 🙂")
    st.caption("쇼핑 고민될 때, 친구처럼 같이 보고 전문가처럼 딱 정리해드릴게요.")
with top[1]:
    if st.button("초기화", use_container_width=True):
        reset_all()
        st.rerun()

# -----------------------------
# 초기 메시지
# -----------------------------
if not st.session_state.messages:
    if is_product_page:
        name = product_context["product_name"] if product_context else "지금 보시는 상품"
        st.session_state.messages.append({
            "role": "assistant",
            "content": f"안녕하세요 🙂 미야언니예요.\n지금 보고 계신 '{name}' 기준으로 같이 볼까요?\n사이즈 / 코디 / 배송·교환 중 뭐부터 볼까요?"
        })
    else:
        st.session_state.messages.append({
            "role": "assistant",
            "content": "안녕하세요 🙂 미야언니예요.\n무엇을 도와드릴까요?\n예) 출근룩 추천 / 배송비 / 교환비 / 66인데 이 옷 괜찮을까요?"
        })

# -----------------------------
# 빠른 버튼
# -----------------------------
cols = st.columns(3)
if is_product_page:
    if cols[0].button("사이즈", use_container_width=True):
        if debounce("size_btn"):
            process_user_message(
                "지금 보이는 상품 기준으로 사이즈 상담해줘. 결론 먼저 말하고, 근거를 상품 정보 기준으로 설명해줘. 정보가 부족하면 추가 질문해줘.",
                current_url, product_no, product_context
            )
            st.rerun()
    if cols[1].button("코디", use_container_width=True):
        if debounce("codi_btn"):
            process_user_message(
                "지금 보이는 상품 기준으로 코디 추천해줘. 상품 정보에 근거해서만 말해주고, 부족하면 안전하게 표현해줘.",
                current_url, product_no, product_context
            )
            st.rerun()
    if cols[2].button("배송/교환", use_container_width=True):
        if debounce("policy_btn"):
            process_user_message(
                "이 상품 배송이나 교환 반품 핵심만 알려줘.",
                current_url, product_no, product_context
            )
            st.rerun()
else:
    if cols[0].button("쇼핑추천", use_container_width=True):
        if debounce("shop_btn"):
            process_user_message(
                "봄 전환기 기준으로 쇼핑 추천을 시작해줘. 먼저 출근/모임/데일리 중 무엇인지 물어봐줘.",
                current_url, product_no, product_context
            )
            st.rerun()
    if cols[1].button("정책/배송", use_container_width=True):
        if debounce("policy2_btn"):
            process_user_message(
                "배송/교환/반품/적립금 정책을 빠르게 알려줘.",
                current_url, product_no, product_context
            )
            st.rerun()
    if cols[2].button("체형/사이즈", use_container_width=True):
        st.session_state.show_profile_form = True
        st.rerun()

if st.session_state.show_profile_form:
    with st.expander("정확한 추천 받기 (체형 입력 30초)", expanded=True):
        profile_form()

st.divider()

# -----------------------------
# 대화창
# -----------------------------
for msg in st.session_state.messages:
    safe_text = html.escape(msg["content"]).replace("\n", "<br>")
    if msg["role"] == "user":
        st.markdown(
            f"""
            <div class="msg-row user">
              <div class="msg-col">
                <div class="msg-bubble user">{safe_text}</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            f"""
            <div class="msg-row bot">
              <div class="msg-col">
                <div class="msg-name">미야언니</div>
                <div class="msg-bubble bot">{safe_text}</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True
        )

st.markdown('<div id="chat-bottom"></div>', unsafe_allow_html=True)

st.markdown("""
<script>
const el = window.parent.document.getElementById("chat-bottom");
if (el) { el.scrollIntoView({behavior: "smooth"}); }
</script>
""", unsafe_allow_html=True)

# -----------------------------
# 입력창
# -----------------------------
user_input = st.chat_input("메시지를 입력하세요…")
st.markdown(
    '<div class="input-hint">여기서 바로 질문하면, 고객님 말은 오른쪽 / 미야언니 답변은 왼쪽에 보여드릴게요 🙂</div>',
    unsafe_allow_html=True
)

if user_input:
    if debounce("chat_send"):
        process_user_message(user_input, current_url, product_no, product_context)
        st.rerun()
