import os
import re
import json
import requests
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI

# -----------------------------
# 1) 기본 설정
# -----------------------------
st.set_page_config(page_title="미샵 쇼핑친구 미야언니", layout="centered")

OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
if not OPENAI_API_KEY:
    st.error("OPENAI_API_KEY가 필요합니다. Streamlit Secrets에 OPENAI_API_KEY를 추가해주세요.")
    st.stop()

client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
# 2) 미야언니 정책 DB (형준님 확정 반영)
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
        "note": "주문량/입고 상황에 따라 출고가 지연될 수 있음"
    },
    "exchange_return": {
        "exchange_possible": "사이즈 교환 가능 / 동일상품 교환 가능 / 타상품 교환 가능",
        "period": "상품 수령 후 7일 이내",
        "exchange_fee": 6000,  # 왕복
        "return_fee_rule": "단순 변심 반품: 반품 후 주문금액이 7만원 이상이면 편도 3,000원 / 7만원 미만이면 왕복 6,000원",
        "defect_or_wrong": "불량/오배송은 무료(또는 별도 안내 기준에 따름)"
    }
}

# -----------------------------
# 3) 유틸: URL에서 product_no 추출
# -----------------------------
def extract_product_no(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"product_no=(\d+)", url)
    return m.group(1) if m else None

# -----------------------------
# 4) 유틸: 상품페이지 텍스트 가져오기
#    (v1: 단순 추출 / v2에서 섹션 분리 강화 가능)
# -----------------------------
def fetch_product_page_text(url: str) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # 불필요 요소 제거
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = soup.get_text("\n")
        # 공백 정리
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        # 너무 길면 잘라서(모델 컨텍스트 보호)
        return text[:12000]
    except Exception as e:
        return f"[상품페이지 내용을 가져오지 못했습니다: {e}]"

# -----------------------------
# 5) 미야언니 톤/역할 프롬프트 (친구+전문가)
# -----------------------------
SYSTEM_PROMPT = """
너는 '미샵 쇼핑친구 미야언니'다.
목표: 4050 여성 고객의 쇼핑 결정을 돕고(추천/비추천), 반품을 줄이며, 정책 문의를 친절하게 해결한다.

톤: 친구처럼 친근하지만, 결론과 근거가 분명한 전문가.
규칙:
1) 항상 공감 1줄 → 결론 먼저(추천/비추천) → 근거 2~3개 → 다음 행동(추가 질문/체크 포인트) 순서.
2) 과장 금지(무조건/완벽 금지). 불확실하면 '가능성/주의'로 말한다.
3) 고객이 특정 상품을 보고 있다면 그 상품 기준으로 답한다.
4) 정책 답변은 POLICY_DB 기준으로 정확히. 없으면 '확인 필요'라고 말하고 확인 경로를 안내한다.
5) 추천할 때 계절/간절기 상식 반영: 지금은 봄 전환기(3월) 기준으로 너무 한겨울/한여름 아이템은 피하고,
   고객이 원하면 '세일/전시즌'임을 안내한 뒤 선택을 돕는다.
"""

# -----------------------------
# 6) B 방식 체형 입력(필요할 때만) 저장 구조
# -----------------------------
def ensure_profile_state():
    if "profile" not in st.session_state:
        st.session_state.profile = None
    if "profile_saved" not in st.session_state:
        st.session_state.profile_saved = False

ensure_profile_state()

def profile_form():
    st.subheader("체형 정보 (30초)")
    col1, col2 = st.columns(2)

    with col1:
        height = st.selectbox("키", ["선택", "150 이하", "150~155", "156~160", "161~165", "166~170", "170 이상"])
        size = st.selectbox("평소 사이즈", ["선택", "55", "66", "66반", "77", "77반", "88"])
    with col2:
        weight = st.selectbox("몸무게(선택)", ["선택", "45 이하", "46~50", "51~55", "56~60", "61~65", "66 이상"])
        style = st.selectbox("원하는 스타일/TPO", ["선택", "출근룩", "모임룩", "데일리룩", "여행룩"])

    concerns = st.multiselect("체형 고민(복수 선택)", ["복부", "팔뚝", "힙", "허벅지", "상체통통", "하체통통", "전체통통"])

    c1, c2 = st.columns(2)
    with c1:
        submitted = st.button("입력 완료")
    with c2:
        save = st.checkbox("다음에도 재사용(저장)", value=False)

    if submitted:
        st.session_state.profile = {
            "height": None if height == "선택" else height,
            "weight": None if weight == "선택" else weight,
            "size": None if size == "선택" else size,
            "style": None if style == "선택" else style,
            "concerns": concerns
        }
        st.session_state.profile_saved = bool(save)
        st.success("체형 정보가 입력되었습니다 🙂")
        st.rerun()

# -----------------------------
# 7) 챗 호출
# -----------------------------
def chat_reply(user_message: str, page_context: str, product_no: str | None):
    profile = st.session_state.profile

    context_pack = {
        "policy_db": POLICY_DB,
        "viewer_context": {
            "is_product_page": bool(product_no),
            "product_no": product_no,
            "page_text_excerpt": page_context[:4000] if page_context else ""
        },
        "customer_profile": profile
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "다음은 참고 데이터(JSON)다. 정책과 상품/프로필을 이 데이터 기준으로 답해라:\n" + json.dumps(context_pack, ensure_ascii=False)}
    ]

    # 대화 히스토리
    for m in st.session_state.get("chat", []):
        messages.append(m)

    messages.append({"role": "user", "content": user_message})

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.4
    )
    return resp.choices[0].message.content

# -----------------------------
# 8) UI
# -----------------------------
st.title("미샵 쇼핑친구 미야언니 🙂")

query = st.query_params
current_url = query.get("url", "")
product_no = extract_product_no(current_url)
is_product_page = bool(product_no)

with st.expander("현재 페이지 정보", expanded=False):
    st.write("URL:", current_url if current_url else "(전달된 URL 없음)")
    st.write("product_no:", product_no if product_no else "(상품페이지 아님)")

# 상품페이지면 컨텍스트 로드(1회)
page_text = ""
if current_url:
    if "page_text" not in st.session_state or st.session_state.get("page_text_url") != current_url:
        st.session_state.page_text_url = current_url
        st.session_state.page_text = fetch_product_page_text(current_url)
    page_text = st.session_state.page_text

# 초기 채팅 상태
if "chat" not in st.session_state:
    st.session_state.chat = []
if "ui_mode" not in st.session_state:
    st.session_state.ui_mode = "product" if is_product_page else "main"
if "need_profile" not in st.session_state:
    st.session_state.need_profile = False

# 상단 인사
st.markdown("**안녕하세요 🙂 미샵 쇼핑친구 미야언니예요.**  쇼핑하다 고민될 때 같이 봐드릴게요.")

# 빠른 메뉴(상품페이지 자동 모드)
if is_product_page:
    st.info("지금 보고 계신 상품 기준으로 같이 볼까요?")
    quick = st.columns(3)
    if quick[0].button("이 상품 사이즈"):
        st.session_state.need_profile = True
        st.session_state.prefill = "이 상품 사이즈가 저에게 맞을까요? 키/평소사이즈/체형 고민 기준으로 추천 또는 비추천까지 결론 먼저 말해줘."
    if quick[1].button("추천/비추천 포인트"):
        st.session_state.prefill = "이 상품 추천/비추천 포인트를 결론 먼저 말해줘. 내 체형에 따라 주의할 점도 같이."
    if quick[2].button("코디 추천"):
        st.session_state.need_profile = True
        st.session_state.prefill = "이 상품으로 코디 추천해줘. 출근/모임/데일리 중 잘 맞는 조합으로 2~3세트."

else:
    st.caption("쇼핑몰 전체 상담 모드입니다.")
    quick = st.columns(3)
    if quick[0].button("쇼핑 상담"):
        st.session_state.prefill = "쇼핑 상담 시작. 지금 계절(봄 전환기)에 맞게 추천해줘."
    if quick[1].button("정책/배송"):
        st.session_state.prefill = "배송/교환/반품/쿠폰/적립금 관련 문의를 도와줘."
    if quick[2].button("체형/사이즈"):
        st.session_state.need_profile = True
        st.session_state.prefill = "체형/사이즈 상담을 시작해줘."

# B 방식: 필요할 때만 체형 입력 요청
if st.session_state.need_profile and st.session_state.profile is None:
    st.warning("더 정확한 추천을 위해 체형 정보를 입력해주시면 좋아요 🙂")
    profile_form()

# 저장 안내(선택)
if st.session_state.profile and st.session_state.profile_saved:
    st.success("체형 정보가 저장되어 다음 상담에서 더 빨라집니다 🙂")

st.divider()

# 대화 표시
for m in st.session_state.chat:
    if m["role"] == "user":
        st.chat_message("user").write(m["content"])
    elif m["role"] == "assistant":
        st.chat_message("assistant").write(m["content"])

# 입력창
prefill = st.session_state.get("prefill", "")
user_input = st.chat_input("예) 이 상품 언제 출고돼요? / 66인데 배 부각될까요? / 출근룩 추천", key="chat_input")

# 버튼에서 prefill이 생겼는데 아직 입력 안 했다면, 바로 전송 버튼 제공
if prefill and not user_input:
    if st.button("이 내용으로 상담 시작"):
        user_input = prefill

if user_input:
    # prefill 초기화
    st.session_state.prefill = ""

    st.session_state.chat.append({"role": "user", "content": user_input})
    with st.chat_message("assistant"):
        with st.spinner("미야언니가 같이 보고 있어요 🙂"):
            answer = chat_reply(user_input, page_text, product_no)
            st.write(answer)
    st.session_state.chat.append({"role": "assistant", "content": answer})
