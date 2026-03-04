import os
import re
import json
import requests
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI

# -----------------------------
# Page config
# -----------------------------
st.set_page_config(page_title="미야언니", layout="centered", initial_sidebar_state="collapsed")

# -----------------------------
# OpenAI
# -----------------------------
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
if not OPENAI_API_KEY:
    st.error("OPENAI_API_KEY가 필요합니다. Streamlit Secrets에 OPENAI_API_KEY를 추가해주세요.")
    st.stop()
client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
# 정책 DB (형준님 확정 반영)
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
    },
    "exchange_return": {
        "exchange_possible": "사이즈 교환 가능 / 동일상품 교환 가능 / 타상품 교환 가능",
        "period": "상품 수령 후 7일 이내",
        "exchange_fee": 6000,
        "return_fee_rule": "단순 변심 반품: 반품 후 주문금액이 7만원 이상이면 편도 3,000원 / 7만원 미만이면 왕복 6,000원",
    }
}

# -----------------------------
# Helpers
# -----------------------------
def extract_product_no(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"product_no=(\d+)", url)
    return m.group(1) if m else None

def fetch_page_text(url: str) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        text = soup.get_text("\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:12000]
    except Exception as e:
        return f"[페이지 내용을 가져오지 못했습니다: {e}]"

SYSTEM_PROMPT = """
너는 '미샵 쇼핑친구 미야언니'다.
목표: 4050 여성 고객의 쇼핑 결정을 돕고(추천/비추천), 반품을 줄이며, 정책 문의를 친절하게 해결한다.

톤: 친구처럼 친근하지만, 결론과 근거가 분명한 전문가.
규칙:
1) 공감 1줄 → 결론 먼저(추천/비추천) → 근거 2~3개 → 다음 행동(추가 질문/체크 포인트).
2) 과장 금지(무조건/완벽 금지). 불확실하면 '가능성/주의'로 말한다.
3) 상품페이지면 '지금 보고 있는 상품' 기준으로 답한다.
4) 정책 답변은 POLICY_DB 기준으로 정확히. 없으면 확인 필요/확인 경로 안내.
5) 지금은 봄 전환기(3월). 추천 시 너무 한겨울/한여름 아이템은 피하고,
   고객이 세일/전시즌 의사가 있으면 그 점을 알려 선택을 돕는다.
6) '언제 도착'처럼 주문단위 질문은 주문DB 연동 전이므로
   정책/평균 + 2시 이전 당일출고 기준으로 안내하고,
   마지막에 '주문시간/마이페이지 주문조회' 안내로 마무리한다.
"""

def ensure_state():
    if "chat" not in st.session_state:
        st.session_state.chat = []
    if "profile" not in st.session_state:
        st.session_state.profile = None
    if "profile_saved" not in st.session_state:
        st.session_state.profile_saved = False
    if "need_profile" not in st.session_state:
        st.session_state.need_profile = False
    if "page_text" not in st.session_state:
        st.session_state.page_text = ""
    if "page_url" not in st.session_state:
        st.session_state.page_url = ""
ensure_state()

def reset_all():
    st.session_state.chat = []
    st.session_state.profile = None
    st.session_state.profile_saved = False
    st.session_state.need_profile = False

def profile_form_inline():
    st.markdown("**더 정확한 추천을 위해 체형 정보를 알려주실까요? (30초)**")
    c1, c2 = st.columns(2)
    with c1:
        height = st.selectbox("키", ["선택", "150 이하", "150~155", "156~160", "161~165", "166~170", "170 이상"])
        size = st.selectbox("평소 사이즈", ["선택", "55", "66", "66반", "77", "77반", "88"])
    with c2:
        weight = st.selectbox("몸무게(선택)", ["선택", "45 이하", "46~50", "51~55", "56~60", "61~65", "66 이상"])
        tpo = st.selectbox("스타일/TPO", ["선택", "출근룩", "모임룩", "데일리룩", "여행룩"])

    concerns = st.multiselect("체형 고민(복수 선택)", ["복부", "팔뚝", "힙", "허벅지", "상체통통", "하체통통", "전체통통"])

    colA, colB = st.columns([1,1])
    with colA:
        submit = st.button("입력 완료", use_container_width=True)
    with colB:
        save = st.checkbox("다음에도 재사용(저장)", value=False)

    if submit:
        st.session_state.profile = {
            "height": None if height == "선택" else height,
            "weight": None if weight == "선택" else weight,
            "size": None if size == "선택" else size,
            "tpo": None if tpo == "선택" else tpo,
            "concerns": concerns
        }
        st.session_state.profile_saved = bool(save)
        st.session_state.need_profile = False
        st.success("체형 정보가 입력되었어요 🙂")
        st.rerun()

def call_llm(user_text: str, url: str, product_no: str | None, page_text: str):
    context_pack = {
        "policy_db": POLICY_DB,
        "viewer_context": {
            "url": url,
            "is_product_page": bool(product_no),
            "product_no": product_no,
            "page_text_excerpt": (page_text or "")[:4000]
        },
        "customer_profile": st.session_state.profile
    }

    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "참고 데이터(JSON):\n" + json.dumps(context_pack, ensure_ascii=False)}
    ]
    msgs.extend(st.session_state.chat)
    msgs.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=msgs,
        temperature=0.4
    )
    return resp.choices[0].message.content

def send_and_respond(text: str, current_url: str, product_no: str | None, page_text: str):
    st.session_state.chat.append({"role": "user", "content": text})
    ans = call_llm(text, current_url, product_no, page_text)
    st.session_state.chat.append({"role": "assistant", "content": ans})

# -----------------------------
# URL / context
# -----------------------------
q = st.query_params
current_url = q.get("url", "")
product_no = extract_product_no(current_url)
is_product_page = bool(product_no)

# 페이지 텍스트는 URL이 바뀔 때만 갱신
if current_url and st.session_state.page_url != current_url:
    st.session_state.page_url = current_url
    st.session_state.page_text = fetch_page_text(current_url)
page_text = st.session_state.page_text

# -----------------------------
# CSS: 상단 잘림/여백 문제 해결
# -----------------------------
st.markdown("""
<style>
/* 상단/하단 여백 확보: 제목 잘림 방지 */
.block-container { padding-top: 2.4rem; padding-bottom: 1.4rem; }
/* Streamlit 기본 헤더가 겹칠 때 대비 */
header[data-testid="stHeader"] { height: 0px; }
div[data-testid="stToolbar"] { visibility: hidden; height: 0px; }
</style>
""", unsafe_allow_html=True)

# -----------------------------
# Header (최소)
# -----------------------------
top = st.columns([1, 0.25])
with top[0]:
    st.markdown("## 미샵 쇼핑친구 미야언니 🙂")
    st.caption("쇼핑 고민될 때, 친구처럼 같이 보고 전문가처럼 딱 정리해드릴게요.")
with top[1]:
    if st.button("초기화", use_container_width=True):
        reset_all()
        st.rerun()

# 상품 페이지에서 열렸고 대화가 비어있으면, 첫 인사 자동
if is_product_page and not st.session_state.chat:
    st.session_state.chat.append({
        "role": "assistant",
        "content": "안녕하세요 🙂 미야언니예요.\n지금 보고 계신 상품 기준으로 같이 볼까요?\n\n‘사이즈/코디/배송·교환’ 중 뭐부터 볼까요?"
    })

# -----------------------------
# Quick actions: 버튼 누르면 "즉시 대화 시작" (핵심 개선)
# -----------------------------
cols = st.columns(3)

if is_product_page:
    if cols[0].button("사이즈", use_container_width=True):
        st.session_state.need_profile = True
        if st.session_state.profile is not None:
            send_and_respond("이 상품 사이즈가 저에게 맞을까요? 추천/비추천 결론 먼저 말해주고 근거 2~3개와 주의점도 알려줘.", current_url, product_no, page_text)
        st.rerun()

    if cols[1].button("코디", use_container_width=True):
        st.session_state.need_profile = True
        if st.session_state.profile is not None:
            send_and_respond("이 상품으로 코디 2~3세트 추천해줘. 출근/모임/데일리 중 잘 맞는 조합으로.", current_url, product_no, page_text)
        st.rerun()

    if cols[2].button("배송/교환", use_container_width=True):
        send_and_respond("이 상품 배송/교환/반품 관련해서 핵심만 정리해줘.", current_url, product_no, page_text)
        st.rerun()

else:
    if cols[0].button("쇼핑추천", use_container_width=True):
        st.session_state.need_profile = True
        if st.session_state.profile is not None:
            send_and_respond("봄 전환기 기준으로 지금 입기 좋은 추천을 시작해줘. 먼저 출근/모임/데일리 중 무엇인지 질문해줘.", current_url, product_no, page_text)
        st.rerun()

    if cols[1].button("정책/배송", use_container_width=True):
        send_and_respond("배송/교환/반품/쿠폰/적립금 정책 문의를 빠르게 안내해줘.", current_url, product_no, page_text)
        st.rerun()

    if cols[2].button("체형/사이즈", use_container_width=True):
        st.session_state.need_profile = True
        if st.session_state.profile is not None:
            send_and_respond("체형/사이즈 상담 시작해줘. 먼저 어떤 고민인지 질문해줘.", current_url, product_no, page_text)
        st.rerun()

# B 방식: 필요할 때만 체형 입력 폼 노출
if st.session_state.need_profile and st.session_state.profile is None:
    st.info("정확도를 높이려면 체형 입력이 좋아요 🙂 (원치 않으면 ‘초기화’ 후 그냥 질문해도 돼요)")
    profile_form_inline()

if st.session_state.profile and st.session_state.profile_saved:
    st.success("체형 정보를 저장해뒀어요 🙂 다음부터 더 빠르게 추천할게요.")

st.divider()

# -----------------------------
# Chat display
# -----------------------------
for m in st.session_state.chat:
    if m["role"] == "user":
        st.chat_message("user").write(m["content"])
    else:
        st.chat_message("assistant").write(m["content"])

# -----------------------------
# Chat input
# -----------------------------
user_input = st.chat_input("예) 이 상품 66 가능할까요? / 배송 언제 출고돼요? / 출근룩 추천")

if user_input:
    with st.chat_message("assistant"):
        with st.spinner("미야언니가 같이 보고 있어요 🙂"):
            send_and_respond(user_input, current_url, product_no, page_text)
            st.write(st.session_state.chat[-1]["content"])
    st.rerun()
