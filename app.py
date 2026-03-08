import os
import re
import json
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
4050 여성 고객이 쇼핑할 때, 옆에서 같이 봐주는 믿음 가는 언니처럼 대화한다.

핵심 역할:
- 지금 보시는 상품 기준으로 사이즈 / 코디 / 컬러 / 배송 / 교환 상담을 도와준다.
- 고객이 덜 고민하고 덜 헷갈리게 도와준다.
- 너무 딱딱하지 않게, 그러나 가볍지도 않게 답한다.
- 반품을 줄이는 방향으로 솔직하고 안전하게 말한다.

말투 규칙:
- 친근한 대화체
- "근거로", "첫째", "둘째", "정리하면" 같은 로봇 같은 표현은 되도록 쓰지 않는다
- 자연스럽게 설명하고, 말마다 패턴이 똑같지 않게 조금씩 다르게 말한다
- "지금 보시는 상품"이라는 표현을 자연스럽게 사용한다
- 상품명이 확실할 때만 상품명을 쓴다
- 상품명이 불확실하거나 일반명으로 보이면 상품명 대신 "지금 보시는 상품"이라고 말한다
- 고객 체형 정보가 있으면 꼭 참고해서 말한다
- 정보가 부족하면 단정하지 말고, 필요한 부분만 짧게 다시 물어본다

답변 스타일:
- 3~7문장 내외
- 먼저 바로 질문에 답하고
- 이어서 이유를 자연스럽게 풀어주고
- 마지막에는 필요할 때만 짧은 추가 질문을 붙인다

배송/교환 규칙:
- 정책 관련 답변은 반드시 POLICY_DB 기준으로만 말한다

사이즈 상담 규칙:
- 고객 키/체중/상의/하의 정보가 있으면 꼭 반영한다
- 확신이 부족하면 "딱 맞다" 식으로 단정하지 말고
  "이 체형이면 이렇게 입는 느낌일 것 같다"는 식으로 안전하게 말한다

코디 상담 규칙:
- 과장하지 말고 실제로 입기 쉬운 조합 위주로 말한다
- 학모룩, 출근룩, 모임룩 같은 상황 질문에는 분위기와 활용도를 자연스럽게 설명한다
"""

GENERIC_NAMES = {"미샵", "misharp", "MISHARP", "미샵여성", "Misharp"}

def ensure_state():
    defaults = {
        "messages": [],
        "last_context_key": "",
        "body_height": "",
        "body_weight": "",
        "body_top": "",
        "body_bottom": ""
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

ensure_state()

def reset_all():
    st.session_state.messages = []

qp = st.query_params
current_url = qp.get("url", "") or ""
product_no = qp.get("pn", "") or ""
product_name_q = qp.get("pname", "") or ""

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
    if len(name) <= 2:
        return True
    return False

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
        "size_tip": extract_by_keywords(["사이즈", "정사이즈", "추천", "44", "55", "66", "77", "88", "S", "M", "L", "XL", "허리", "총장", "힙", "허벅지"]),
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

    product_name = clean_text(passed_name)
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
            f"배송은 {POLICY_DB['shipping']['courier']}를 이용하고 있고요 :) \n"
            f"배송비는 {POLICY_DB['shipping']['shipping_fee']:,}원이에요. "
            f"{POLICY_DB['shipping']['free_shipping_over']:,}원 이상이면 무료배송으로 적용돼요."
        )

    if any(k in q for k in ["언제출고", "출고", "당일출고"]):
        return (
            f"{POLICY_DB['shipping']['same_day_dispatch_rule']}예요 :) \n"
            f"보통은 {POLICY_DB['shipping']['delivery_time']} 정도 생각해주시면 되고, "
            f"결제 순서대로 순차 출고되고 있어요."
        )

    if any(k in q for k in ["교환", "사이즈교환"]):
        return (
            "교환은 가능해요 :) \n"
            f"{POLICY_DB['exchange_return']['exchange_possible']}이고, "
            f"{POLICY_DB['exchange_return']['period']} 안에 접수해주시면 돼요. \n"
            f"단순 변심 교환은 왕복 {POLICY_DB['exchange_return']['exchange_fee']:,}원으로 안내드리고 있어요."
        )

    if any(k in q for k in ["반품", "환불"]):
        return (
            "반품도 가능해요 :) \n"
            f"{POLICY_DB['exchange_return']['period']} 안에 접수해주시면 되고, "
            f"{POLICY_DB['exchange_return']['return_fee_rule']} 기준으로 진행돼요. \n"
            f"불량이나 오배송이면 배송비는 미샵에서 부담해드려요."
        )

    return None

def build_body_context() -> dict:
    return {
        "height_cm": clean_text(st.session_state.body_height),
        "weight_kg": clean_text(st.session_state.body_weight),
        "top_size": clean_text(st.session_state.body_top),
        "bottom_size": clean_text(st.session_state.body_bottom),
    }

def build_body_context_text(body_ctx: dict) -> str:
    if not any(body_ctx.values()):
        return "입력된 체형 정보 없음"
    return (
        f"키: {body_ctx.get('height_cm') or '-'}cm, "
        f"체중: {body_ctx.get('weight_kg') or '-'}kg, "
        f"상의: {body_ctx.get('top_size') or '-'}, "
        f"하의: {body_ctx.get('bottom_size') or '-'}"
    )

def get_llm_answer(user_text: str, current_url: str, product_no: str, product_context: dict | None) -> str:
    body_context = build_body_context()

    context_pack = {
        "policy_db": POLICY_DB,
        "viewer_context": {
            "url": current_url,
            "is_product_page": bool(product_no),
            "product_no": product_no
        },
        "body_context": body_context,
        "product_context": product_context
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "참고 데이터(JSON):\n" + json.dumps(context_pack, ensure_ascii=False)},
    ]

    history = st.session_state.messages[-8:]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})

    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.72,
        max_tokens=420
    )
    return resp.choices[0].message.content.strip()

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
  padding-top: calc(1.05rem + env(safe-area-inset-top));
  padding-bottom: 8.2rem;
  max-width: 760px;
}

header[data-testid="stHeader"] {
  height: 0px;
}
div[data-testid="stToolbar"] {
  visibility: hidden;
  height: 0px;
}

.main-title-wrap {
  margin-top: -4px;
  margin-bottom: 6px;
  text-align: center;
}
.main-title {
  font-size: 47px;
  font-weight: 800;
  line-height: 1.08;
  margin: 0;
  letter-spacing: -0.02em;
  white-space: nowrap;
}
.main-subtitle {
  margin-top: 8px;
  margin-bottom: 8px;
  color: rgba(255,255,255,0.74);
  font-size: 13px;
  line-height: 1.45;
}

.profile-wrap {
  margin-top: 6px;
  margin-bottom: 8px;
}
.profile-label {
  font-size: 13px;
  font-weight: 700;
  margin: 0 0 8px 0;
  color: rgba(255,255,255,0.92);
}
.profile-help {
  font-size: 11px;
  color: rgba(255,255,255,0.58);
  margin-left: 4px;
  font-weight: 500;
}

.profile-box {
  padding: 12px 12px 4px 12px;
  border: 1px solid rgba(255,255,255,0.09);
  border-radius: 14px;
  background: rgba(255,255,255,0.02);
}

.profile-caption {
  font-size: 11px;
  color: rgba(255,255,255,0.56);
  margin-top: 6px;
  margin-bottom: 0;
}

div[data-testid="stTextInput"] label,
div[data-testid="stSelectbox"] label {
  font-size: 12px !important;
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
  max-width: 80%;
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
  line-height: 1.68;
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

div[data-testid="stChatInput"] {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: 42px;
  width: min(720px, calc(100% - 20px));
  z-index: 9999;
}

@media (max-width: 768px) {
  .block-container {
    padding-top: calc(0.35rem + env(safe-area-inset-top));
    padding-bottom: 8.6rem;
  }

  .main-title-wrap {
    margin-top: -8px;
    margin-bottom: 4px;
  }

  .main-title {
    font-size: 29px;
    line-height: 1.06;
    white-space: nowrap;
  }

  .main-subtitle {
    font-size: 11.5px;
    line-height: 1.38;
    margin-top: 6px;
    margin-bottom: 8px;
  }

  .profile-wrap {
    margin-top: 4px;
    margin-bottom: 4px;
  }

  .profile-box {
    padding: 10px 10px 2px 10px;
    border-radius: 12px;
  }

  .msg-col {
    max-width: 84%;
  }

  .msg-bubble {
    font-size: 14px;
    line-height: 1.62;
  }

  div[data-testid="stChatInput"] {
    bottom: 26px;
    width: calc(100% - 16px);
  }
}
</style>
""", unsafe_allow_html=True)

st.markdown(
    """
    <div class="main-title-wrap">
      <div class="main-title">미샵 쇼핑친구 미야언니</div>
      <div class="main-subtitle">24시간 언제나 미샵님들의 쇼핑 판단에 도움을 드리는 똑똑한 쇼핑친구</div>
    </div>
    """,
    unsafe_allow_html=True
)

st.markdown(
    """
    <div class="profile-wrap">
      <div class="profile-label">
        사이즈 입력 <span class="profile-help">(더 구체적인 상담 가능)</span>
      </div>
      <div class="profile-box">
    """,
    unsafe_allow_html=True
)

c1, c2, c3, c4 = st.columns(4)

with c1:
    st.session_state.body_height = st.text_input(
        "키",
        value=st.session_state.body_height,
        placeholder="cm",
        key="body_height_input"
    )

with c2:
    st.session_state.body_weight = st.text_input(
        "체중",
        value=st.session_state.body_weight,
        placeholder="kg",
        key="body_weight_input"
    )

size_options = ["", "44", "55", "66", "77", "88"]

with c3:
    current_top = st.session_state.body_top if st.session_state.body_top in size_options else ""
    st.session_state.body_top = st.selectbox(
        "상의",
        options=size_options,
        index=size_options.index(current_top),
        key="body_top_input"
    )

with c4:
    current_bottom = st.session_state.body_bottom if st.session_state.body_bottom in size_options else ""
    st.session_state.body_bottom = st.selectbox(
        "하의",
        options=size_options,
        index=size_options.index(current_bottom),
        key="body_bottom_input"
    )

st.markdown("</div></div>", unsafe_allow_html=True)

body_summary = build_body_context_text(build_body_context())
if any(build_body_context().values()):
    st.caption(f"현재 입력 정보: {body_summary}")

if not st.session_state.messages:
    st.session_state.messages.append({
        "role": "assistant",
        "content": (
            "안녕하세요? 옷 같이 봐드리는 미야언니예요:) \n"
            "'지금 보시는 상품' 기준으로 제가 같이 봐드릴게요! \n"
            "사이즈, 코디, 배송,교환 중 뭐부터 얘기해볼까요?"
        )
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

user_input = st.chat_input("메시지를 입력하세요…")
if user_input:
    process_user_message(user_input, current_url, product_no, product_context)
    st.rerun()
