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
- "근거로", "첫째", "둘째", "정리하면" 같은 표현은 되도록 쓰지 않는다
- 자연스럽게 설명하고, 답변 패턴이 매번 똑같지 않게 조금씩 다르게 말한다
- "지금 보시는 상품"이라는 표현을 자연스럽게 사용한다
- 상품명이 확실할 때만 상품명을 쓴다
- 상품명이 불확실하면 "지금 보시는 상품"이라고 말한다
- 고객 체형 정보가 있으면 꼭 참고해서 말한다
- 정보가 부족하면 짧게 필요한 부분만 다시 물어본다

답변 스타일:
- 3~7문장 내외
- 먼저 질문에 바로 답하고
- 이어서 이유를 자연스럽게 풀어주고
- 마지막에는 필요할 때만 짧게 추가 질문을 붙인다

배송/교환 규칙:
- 정책 관련 답변은 반드시 POLICY_DB 기준으로만 말한다

사이즈 상담 규칙:
- 고객 키/체중/상의/하의 정보가 있으면 꼭 반영한다
- 확신이 부족하면 단정적으로 말하지 말고 안전하게 추천한다

코디 상담 규칙:
- 실제로 입기 쉬운 조합 위주로 말한다
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
    if name in GENERIC_NAMES or len(name) <= 2:
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
        "size_tip": extract_by_keywords(["사이즈", "정사이즈", "추천", "44", "55", "55반", "66", "66반", "77", "77반", "88", "S", "M", "L", "XL", "허리", "총장", "힙", "허벅지"]),
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


def fetch_product_context(url: str, passed_name: str = "") -> dict | None:
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
def fetch_product_context_cached(url: str, passed_name: str = "") -> dict | None:
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
header[data-testid="stHeader"] {display:none;}
div[data-testid="stToolbar"] {display:none;}
#MainMenu {visibility:hidden;}
footer {visibility:hidden;}

.block-container{
  max-width:760px;
  padding-top:0.22rem !important;
  padding-bottom:11.2rem !important;
}

:root{
  --miya-accent:#0f6a63;
  --miya-title:#303443;
  --miya-sub:#5f6471;
  --miya-muted:#7a7f8c;
  --miya-divider:#ccccd2;
  --miya-bot-bg:#071b4e;
  --miya-user-bg:#dff0ec;
  --miya-user-text:#1f3b36;
}

div[data-testid="column"]{
  min-width:0 !important;
}

div[data-testid="stTextInput"] label,
div[data-testid="stSelectbox"] label{
  color:var(--miya-title) !important;
  font-weight:700 !important;
  font-size:11.5px !important;
}

div[data-testid="stTextInput"] input,
div[data-baseweb="select"] > div{
  border-radius:12px !important;
}

div[data-testid="stTextInput"],
div[data-testid="stSelectbox"]{
  margin-bottom:-2px !important;
}

hr{
  margin-top:8px !important;
  margin-bottom:8px !important;
  border-color:var(--miya-divider) !important;
}

div[data-testid="stChatInput"]{
  position:fixed !important;
  left:50% !important;
  transform:translateX(-50%) !important;
  bottom:68px !important;
  width:min(720px, calc(100% - 24px)) !important;
  z-index:9999 !important;
}

@media (max-width: 768px){
  .block-container{
    max-width:100%;
    padding-top:0.14rem !important;
    padding-bottom:11.9rem !important;
  }

  div[data-testid="stHorizontalBlock"]{
    gap:6px !important;
  }

  div[data-testid="stHorizontalBlock"] > div{
    flex:1 1 0 !important;
    min-width:0 !important;
  }

  div[data-testid="stTextInput"] label,
  div[data-testid="stSelectbox"] label{
    font-size:11px !important;
  }

  div[data-testid="stTextInput"],
  div[data-testid="stSelectbox"]{
    margin-bottom:-4px !important;
  }

  hr{
    margin-top:5px !important;
    margin-bottom:5px !important;
  }

  div[data-testid="stChatInput"]{
    bottom:64px !important;
    width:calc(100% - 16px) !important;
  }
}
</style>
""", unsafe_allow_html=True)

# 타이틀 하단 카피 글자크기 + 줄간격 확대 반영
st.markdown(
    """
    <div style="text-align:center; margin:0 0 16px 0;">
      <div style="font-size:31px; font-weight:800; line-height:1.1; letter-spacing:-0.02em; color:#303443;">
        미샵 쇼핑친구 <span style="color:#0f6a63;">미야언니</span>
      </div>
      <div style="margin-top:6px; font-size:13.5px; line-height:1.35; color:#5f6471;">
        24시간 언제나 쇼핑 판단에 도움드리는 스마트한 쇼핑친구
      </div>
    </div>
    """,
    unsafe_allow_html=True
)

st.markdown(
    """
    <div style="margin-top:2px; margin-bottom:4px;">
      <div style="font-size:13px; font-weight:700; line-height:1.2; color:#303443; margin-bottom:4px;">
        사이즈 입력<span style="font-size:11px; font-weight:500; color:#7a7f8c;">(더 구체적인 상담 가능)</span>
      </div>
      <div style="padding:6px 8px 0 8px; border:1px solid rgba(0,0,0,.04); border-radius:14px; background:transparent;">
    """,
    unsafe_allow_html=True
)

row1 = st.columns(2, gap="small")
with row1[0]:
    st.session_state.body_height = st.text_input(
        "키",
        value=st.session_state.body_height,
        placeholder="cm",
        key="body_height_input"
    )
with row1[1]:
    st.session_state.body_weight = st.text_input(
        "체중",
        value=st.session_state.body_weight,
        placeholder="kg",
        key="body_weight_input"
    )

size_options = ["", "44", "55", "55반", "66", "66반", "77", "77반", "88"]

row2 = st.columns(2, gap="small")
with row2[0]:
    current_top = st.session_state.body_top if st.session_state.body_top in size_options else ""
    st.session_state.body_top = st.selectbox(
        "상의",
        options=size_options,
        index=size_options.index(current_top),
        key="body_top_input"
    )
with row2[1]:
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
    st.markdown(
        f'<div style="margin-top:3px; margin-bottom:2px; font-size:10.8px; color:#7a7f8c;">현재 입력 정보: {html.escape(body_summary)}</div>',
        unsafe_allow_html=True
    )

if not st.session_state.messages:
    st.session_state.messages.append({
        "role": "assistant",
        "content": (
"안녕하세요? 옷 같이 봐드리는 미야언니예요 :) \n"
"'지금 보시는 상품' 기준으로 제가 같이 봐드릴게요. \n"
"사이즈, 코디, 배송, 교환 중 뭐부터 이야기해볼까요?"
        )
    })

st.divider()

for msg in st.session_state.messages:
    safe_text = html.escape(msg["content"]).replace("\n", "<br>")

    if msg["role"] == "user":
        st.markdown(
            f"""
            <div style="display:flex; justify-content:flex-end; width:100%; margin:8px 0;">
              <div style="max-width:92%;">
                <div style="display:block; font-size:12px; font-weight:700; line-height:1.2; color:#0f6a63; text-align:right; margin:0 6px 5px 0;">
                  고객님
                </div>
                <div style="padding:12px 14px; border-radius:18px; border-bottom-right-radius:6px; font-size:15px; line-height:1.62; white-space:pre-wrap; word-break:keep-all; background:#dff0ec; color:#1f3b36; border:1px solid rgba(15,106,99,.14);">
                  {safe_text}
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            f"""
            <div style="display:flex; justify-content:flex-start; width:100%; margin:8px 0;">
              <div style="max-width:92%;">
                <div style="display:block; font-size:12px; font-weight:700; line-height:1.2; color:#5f6471; margin:0 0 5px 6px;">
                  미야언니
                </div>
                <div style="padding:12px 14px; border-radius:18px; border-bottom-left-radius:6px; font-size:15px; line-height:1.55; white-space:pre-wrap; word-break:keep-all; background:#071b4e; color:#ffffff; border:1px solid rgba(255,255,255,.08);">
                  {safe_text}
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True
        )

user_input = st.chat_input("메시지를 입력하세요…")
if user_input:
    process_user_message(user_input, current_url, product_no, product_context)
    st.rerun()
