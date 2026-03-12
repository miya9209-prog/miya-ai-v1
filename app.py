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
        "combined_shipping": "합배송 가능(1박스 기준). 단 박스 크기 초과 시 합배송 불가",
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
4050 여성 고객이 쇼핑할 때 옆에서 같이 봐주는, 친근하고 믿음 가는 언니처럼 말한다.

핵심 역할:
- 지금 보시는 상품 기준으로 사이즈 / 코디 / 컬러 / 배송 / 교환 상담을 돕는다.
- 고객이 덜 고민하고, 덜 헷갈리고, 반품 가능성도 줄어들도록 돕는다.
- 답변은 짧지만 성의 있게, 너무 설명서처럼 딱딱하지 않게 말한다.

말투 규칙:
- 친근한 대화체로 말한다.
- '첫째, 둘째', '근거로 말씀드리면', '정리하면' 같은 딱딱한 표현은 쓰지 않는다.
- 매번 문장 구조를 똑같이 반복하지 않는다.
- 상품명이 확실할 때만 쓰고, 애매하면 '지금 보시는 상품'이라고 말한다.
- 고객 체형 정보가 있으면 자연스럽게 반영한다.
- 확신이 낮으면 단정하지 말고 안전하게 제안한다.

답변 스타일:
- 기본 2~5문장.
- 바로 답부터 말하고, 필요한 설명만 자연스럽게 덧붙인다.
- 마지막 질문은 꼭 필요할 때만 짧게 붙인다.
- 너무 길어지면 줄인다.

중요 규칙:
- 배송/교환 관련 답변은 POLICY_DB 기준으로만 말한다.
- 실제로 확인되지 않은 컬러, 사이즈, 소재는 지어내지 않는다.
- 현재가 상품 상세페이지라면 절대 '상세페이지에서 다시 문의하세요'라고 말하지 않는다.
- 상품 정보가 일부 부족해도 현재 페이지 기준으로 최대한 도움 되는 답을 한다.
"""

GENERIC_NAMES = {"미샵", "misharp", "MISHARP", "미샵여성", "Misharp"}
COLOR_HINTS = [
    "블랙", "아이보리", "크림", "화이트", "베이지", "오트밀", "그레이", "차콜",
    "네이비", "블루", "소라", "카키", "브라운", "핑크", "레드", "와인",
    "버건디", "퍼플", "민트", "옐로우"
]
SIZE_OPTIONS = ["", "44", "55", "55반", "66", "66반", "77", "77반", "88"]


def ensure_state():
    defaults = {
        "messages": [],
        "last_context_key": "",
        "body_height": "",
        "body_weight": "",
        "body_top": "",
        "body_bottom": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


ensure_state()

qp = st.query_params
current_url = qp.get("url", "") or ""
product_no = qp.get("pn", "") or ""
product_name_q = qp.get("pname", "") or ""


def build_context_key(url: str, pn: str, pname: str) -> str:
    return f"{url}|{pn}|{pname}"


def is_product_page(url: str, pn: str) -> bool:
    url = (url or "").lower()
    return ("/product/detail" in url) or ("product_no=" in url) or bool((pn or "").strip())


context_key = build_context_key(current_url, product_no, product_name_q)
if context_key != st.session_state.last_context_key:
    st.session_state.last_context_key = context_key
    st.session_state.messages = []


def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def is_generic_name(name: str) -> bool:
    name = clean_text(name)
    return (not name) or (name in GENERIC_NAMES) or len(name) <= 2


def try_number(value: str):
    value = clean_text(value)
    if not value:
        return None
    m = re.search(r"\d+(?:\.\d+)?", value)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def uniq_keep_order(items):
    seen = set()
    out = []
    for item in items:
        item = clean_text(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


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
        "맨투맨": ["맨투맨", "스웻"],
    }
    for cat, keywords in mapping.items():
        if any(k in corpus for k in keywords):
            return cat
    return "기타"


def normalize_product_name(name: str) -> str:
    name = clean_text(name)
    name = re.sub(r"\s*\|\s*.*$", "", name)
    name = re.sub(r"\s*-\s*미샵.*$", "", name, flags=re.I)
    name = re.sub(r"\s*-\s*MISHARP.*$", "", name, flags=re.I)
    return clean_text(name)


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
        "summary": joined[:2600],
        "material": extract_by_keywords(["소재", "원단", "혼용", "%", "면", "폴리", "레이온", "아크릴", "울", "스판", "비스코스", "나일론"]),
        "fit": extract_by_keywords(["핏", "여유", "라인", "체형", "복부", "팔뚝", "허벅지", "힙", "루즈", "와이드", "슬림", "정핏", "세미", "커버"]),
        "size_tip": extract_by_keywords(["사이즈", "정사이즈", "추천", "44", "55", "55반", "66", "66반", "77", "77반", "88", "S", "M", "L", "XL", "허리", "총장"]),
        "shipping": extract_by_keywords(["배송", "출고", "교환", "반품", "배송비"])
    }


def extract_color_candidates(text: str):
    found = []
    corpus = clean_text(text)
    for color in COLOR_HINTS:
        if color in corpus:
            found.append(color)
    return uniq_keep_order(found)


def extract_select_options(soup: BeautifulSoup):
    selects = soup.select("select")
    all_options = []
    for sel in selects:
        opts = []
        for opt in sel.select("option"):
            text = clean_text(opt.get_text(" ", strip=True))
            if not text:
                continue
            if any(bad in text for bad in ["필수 옵션", "옵션 선택", "선택해주세요", "품절", "----"]):
                continue
            opts.append(text)
        if opts:
            all_options.append(uniq_keep_order(opts))
    return all_options


def fetch_product_context(url: str, passed_name: str = "") -> dict | None:
    if not url:
        return None

    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    name = normalize_product_name(passed_name)
    if is_generic_name(name):
        for selector in [
            "#span_product_name",
            "#span_product_name_mobile",
            ".infoArea #span_product_name",
            ".infoArea .headingArea h2",
            ".infoArea .headingArea h3",
            ".headingArea h2",
            ".headingArea h3",
            "title",
        ]:
            el = soup.select_one(selector)
            if el:
                candidate = normalize_product_name(el.get_text(" ", strip=True))
                if not is_generic_name(candidate):
                    name = candidate
                    break

    if is_generic_name(name):
        name = "지금 보시는 상품"

    option_groups = extract_select_options(soup)
    color_options = option_groups[0] if len(option_groups) >= 1 else []
    size_options = option_groups[1] if len(option_groups) >= 2 else []

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    raw_text = soup.get_text("\n")
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()
    sections = split_sections(raw_text)
    category = guess_category(name, raw_text)

    if not color_options:
        color_options = extract_color_candidates(raw_text)

    return {
        "product_name": name,
        "category": category,
        "summary": sections["summary"],
        "material": sections["material"],
        "fit": sections["fit"],
        "size_tip": sections["size_tip"],
        "shipping": sections["shipping"],
        "color_options": color_options,
        "size_options": size_options,
        "raw_excerpt": raw_text[:4500],
    }


@st.cache_data(ttl=300, show_spinner=False)
def fetch_product_context_cached(url: str, passed_name: str = "") -> dict | None:
    try:
        return fetch_product_context(url, passed_name)
    except Exception as e:
        safe_name = normalize_product_name(passed_name)
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
            "color_options": [],
            "size_options": [],
            "raw_excerpt": f"[상품 정보를 가져오지 못했습니다: {e}]",
        }


def normalize_size_options(size_options):
    cleaned = []
    for s in size_options or []:
        s = clean_text(s)
        if not s:
            continue
        if any(bad in s for bad in ["필수", "선택", "품절"]):
            continue
        cleaned.append(s)
    return uniq_keep_order(cleaned)


def detect_free_size(size_options):
    for s in size_options:
        up = s.upper()
        if "FREE" in up or up == "F" or up.startswith("F("):
            return s
    return None


def contains_alpha_sizes(size_options):
    joined = " ".join(size_options).upper()
    return any(token in joined for token in [" S", " M", " L", " XL", "XXL"]) or any(x in size_options for x in ["S", "M", "L", "XL", "XXL"])


def contains_korean_sizes(size_options):
    joined = " ".join(size_options)
    return any(x in joined for x in ["44", "55", "55반", "66", "66반", "77", "77반", "88"])


def pick_from_alpha(weight, options):
    upper_map = {o.upper(): o for o in options}
    if weight <= 50 and "S" in upper_map:
        return upper_map["S"]
    if weight <= 58 and "M" in upper_map:
        return upper_map["M"]
    if weight <= 66 and "L" in upper_map:
        return upper_map["L"]
    if "XL" in upper_map:
        return upper_map["XL"]
    return options[-1]


def pick_from_korean(weight, options):
    order = ["44", "55", "55반", "66", "66반", "77", "77반", "88"]
    available = [x for x in order if any(x == o or x in o for o in options)]
    if not available:
        return options[0]

    if weight <= 47:
        target = "44"
    elif weight <= 53:
        target = "55"
    elif weight <= 56:
        target = "55반"
    elif weight <= 61:
        target = "66"
    elif weight <= 65:
        target = "66반"
    elif weight <= 70:
        target = "77"
    elif weight <= 74:
        target = "77반"
    else:
        target = "88"

    return target if target in available else available[-1]


def recommend_size(height_cm, weight_kg, top_size, size_options):
    options = normalize_size_options(size_options)
    if not options:
        return {"recommended": None, "reason": "옵션 정보가 없어 사이즈를 딱 잘라 말하긴 어려워요."}

    free_size = detect_free_size(options)
    if free_size:
        return {"recommended": free_size, "reason": f"이 상품은 {free_size} 기준으로 보시면 돼요."}

    weight = try_number(weight_kg)
    if weight is None:
        if top_size:
            return {"recommended": top_size, "reason": "평소 입으시는 상의 사이즈 기준으로 먼저 보시는 쪽이 가장 안전해요."}
        return {"recommended": options[0], "reason": "체형 정보가 적어서 가장 기본 옵션부터 보는 게 무난해요."}

    if contains_alpha_sizes(options):
        return {"recommended": pick_from_alpha(weight, options), "reason": "현재 체형 기준으로 가장 무난하게 보이는 옵션이에요."}

    if contains_korean_sizes(options):
        return {"recommended": pick_from_korean(weight, options), "reason": "지금 입력해주신 체형 기준으로 가장 가까운 옵션이에요."}

    return {"recommended": options[0], "reason": "옵션명이 일반형이 아니라 첫 번째 유효 옵션 기준으로 봐주시는 게 좋아요."}


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


def get_fast_policy_answer(user_text: str) -> str | None:
    q = user_text.replace(" ", "").lower()

    if any(k in q for k in ["배송비", "무료배송"]):
        return (
            f"배송은 {POLICY_DB['shipping']['courier']}를 이용하고 있어요 :)\n"
            f"배송비는 {POLICY_DB['shipping']['shipping_fee']:,}원이고, "
            f"{POLICY_DB['shipping']['free_shipping_over']:,}원 이상이면 무료배송으로 적용돼요."
        )

    if any(k in q for k in ["언제출고", "출고", "당일출고"]):
        return (
            f"{POLICY_DB['shipping']['same_day_dispatch_rule']}예요 :)\n"
            f"보통은 {POLICY_DB['shipping']['delivery_time']} 정도 생각해주시면 되고, "
            f"{POLICY_DB['shipping']['dispatch_order']}로 진행되고 있어요."
        )

    if any(k in q for k in ["교환", "사이즈교환"]):
        return (
            "교환은 가능해요 :)\n"
            f"{POLICY_DB['exchange_return']['exchange_possible']}이고, "
            f"{POLICY_DB['exchange_return']['period']} 안에 접수해주시면 돼요.\n"
            f"단순 변심 교환은 왕복 {POLICY_DB['exchange_return']['exchange_fee']:,}원으로 안내드리고 있어요."
        )

    if any(k in q for k in ["반품", "환불"]):
        return (
            "반품도 가능해요 :)\n"
            f"{POLICY_DB['exchange_return']['period']} 안에 접수해주시면 되고, "
            f"{POLICY_DB['exchange_return']['return_fee_rule']} 기준으로 진행돼요.\n"
            f"불량이나 오배송이면 배송비는 미샵에서 부담해드려요."
        )

    return None


def build_context_pack(product_context: dict | None):
    body_context = build_body_context()
    is_detail = is_product_page(current_url, product_no)
    size_reco = recommend_size(
        body_context.get("height_cm", ""),
        body_context.get("weight_kg", ""),
        body_context.get("top_size", ""),
        (product_context or {}).get("size_options", []),
    ) if product_context else None

    return {
        "policy_db": POLICY_DB,
        "viewer_context": {
            "url": current_url,
            "product_no": product_no,
            "is_product_page": is_detail,
        },
        "body_context": body_context,
        "product_context": product_context,
        "size_recommendation": size_reco,
    }


def get_llm_answer(user_text: str, product_context: dict | None) -> str:
    context_pack = build_context_pack(product_context)
    is_detail = context_pack["viewer_context"]["is_product_page"]

    extra_rules = []
    if is_detail:
        extra_rules.append("현재는 상품 상세페이지 기준 상담입니다. 현재 페이지 기준으로 바로 답하세요.")
        extra_rules.append("상세페이지에서 다시 눌러달라는 말은 하지 마세요.")
    else:
        extra_rules.append("현재는 일반 유입일 수 있습니다. 상품 정보가 부족하면 자연스럽게 현재 보고 계신 상품 기준으로 다시 물어봐 달라고 짧게 안내할 수 있습니다.")

    if product_context:
        if product_context.get("product_name") and product_context.get("product_name") != "지금 보시는 상품":
            extra_rules.append(f"현재 상품명 후보: {product_context['product_name']}")
        if product_context.get("color_options"):
            extra_rules.append("확인된 컬러 후보: " + ", ".join(product_context["color_options"]))
        if product_context.get("size_options"):
            extra_rules.append("확인된 사이즈 옵션: " + ", ".join(product_context["size_options"]))

    if context_pack.get("size_recommendation", {}).get("recommended"):
        extra_rules.append(f"추천 사이즈 기준값: {context_pack['size_recommendation']['recommended']}")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "추가 규칙:\n- " + "\n- ".join(extra_rules)},
        {"role": "system", "content": "참고 데이터(JSON):\n" + json.dumps(context_pack, ensure_ascii=False)},
    ]

    history = st.session_state.messages[-8:]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})

    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.68,
        max_tokens=300,
    )
    return resp.choices[0].message.content.strip()


def process_user_message(user_text: str, product_context: dict | None):
    st.session_state.messages.append({"role": "user", "content": user_text})

    fast = get_fast_policy_answer(user_text)
    if fast:
        st.session_state.messages.append({"role": "assistant", "content": fast})
        return

    answer = get_llm_answer(user_text, product_context)
    st.session_state.messages.append({"role": "assistant", "content": answer})


product_context = fetch_product_context_cached(current_url, product_name_q) if current_url else None
body_ctx = build_body_context()
size_result = recommend_size(
    body_ctx.get("height_cm", ""),
    body_ctx.get("weight_kg", ""),
    body_ctx.get("top_size", ""),
    (product_context or {}).get("size_options", []),
) if product_context else None

st.markdown(
    """
<style>
header[data-testid="stHeader"] {display:none;}
div[data-testid="stToolbar"] {display:none;}
#MainMenu {visibility:hidden;}
footer {visibility:hidden;}

.block-container{
  max-width:760px;
  padding-top:0.38rem !important;
  padding-bottom:10.5rem !important;
}

:root{
  --miya-accent:#0f6a63;
  --miya-title:#303443;
  --miya-sub:#5f6471;
  --miya-muted:#7a7f8c;
  --miya-divider:#d8dbe2;
  --miya-bot-bg:#071b4e;
  --miya-user-bg:#dff0ec;
  --miya-user-text:#1f3b36;
}

div[data-testid="column"]{min-width:0 !important;}

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
  margin-top:6px !important;
  margin-bottom:6px !important;
  border-color:var(--miya-divider) !important;
}

div[data-testid="stChatInput"]{
  position:fixed !important;
  left:50% !important;
  transform:translateX(-50%) !important;
  bottom:52px !important;
  width:min(720px, calc(100% - 24px)) !important;
  z-index:9999 !important;
}

@media (max-width: 768px){
  .block-container{
    max-width:100%;
    padding-top:0.65rem !important;
    padding-bottom:9.8rem !important;
  }

  div[data-testid="stHorizontalBlock"]{
    gap:6px !important;
  }

  div[data-testid="stTextInput"] label,
  div[data-testid="stSelectbox"] label{
    font-size:11px !important;
  }

  div[data-testid="stChatInput"]{
    bottom:56px !important;
    width:calc(100% - 16px) !important;
  }
}
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style="text-align:center; margin:0 0 10px 0;">
      <div style="font-size:31px; font-weight:800; line-height:1.08; letter-spacing:-0.02em; color:#303443;">
        미샵 쇼핑친구 <span style="color:#0f6a63;">미야언니</span>
      </div>
      <div style="margin-top:4px; font-size:13px; line-height:1.3; color:#5f6471;">
        24시간 언제나 미샵님들의 쇼핑 판단에 도움을 드리는 똑똑한 쇼핑친구
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style="margin-top:0; margin-bottom:2px;">
      <div style="font-size:13px; font-weight:700; line-height:1.2; color:#303443; margin-bottom:4px;">
        사이즈 입력 <span style="font-size:11px; font-weight:500; color:#7a7f8c;">(더 구체적인 상담 가능)</span>
      </div>
      <div style="padding:6px 8px 0 8px; border:1px solid rgba(0,0,0,.05); border-radius:14px; background:transparent;">
    """,
    unsafe_allow_html=True,
)

row1 = st.columns(2, gap="small")
with row1[0]:
    st.session_state.body_height = st.text_input(
        "키",
        value=st.session_state.body_height,
        placeholder="cm",
        key="body_height_input",
    )
with row1[1]:
    st.session_state.body_weight = st.text_input(
        "체중",
        value=st.session_state.body_weight,
        placeholder="kg",
        key="body_weight_input",
    )

row2 = st.columns(2, gap="small")
with row2[0]:
    current_top = st.session_state.body_top if st.session_state.body_top in SIZE_OPTIONS else ""
    st.session_state.body_top = st.selectbox(
        "상의",
        options=SIZE_OPTIONS,
        index=SIZE_OPTIONS.index(current_top),
        key="body_top_input",
    )
with row2[1]:
    current_bottom = st.session_state.body_bottom if st.session_state.body_bottom in SIZE_OPTIONS else ""
    st.session_state.body_bottom = st.selectbox(
        "하의",
        options=SIZE_OPTIONS,
        index=SIZE_OPTIONS.index(current_bottom),
        key="body_bottom_input",
    )

st.markdown("</div></div>", unsafe_allow_html=True)

body_summary = build_body_context_text(build_body_context())
if any(build_body_context().values()):
    st.markdown(
        f'<div style="margin-top:2px; margin-bottom:2px; font-size:10.8px; color:#7a7f8c;">현재 입력 정보: {html.escape(body_summary)}</div>',
        unsafe_allow_html=True,
    )

if size_result and size_result.get("recommended"):
    st.markdown(
        f'<div style="margin-top:2px; margin-bottom:2px; font-size:10.8px; color:#7a7f8c;">참고 추천 사이즈: {html.escape(size_result["recommended"])} · {html.escape(size_result["reason"])}</div>',
        unsafe_allow_html=True,
    )

if not st.session_state.messages:
    if is_product_page(current_url, product_no):
        welcome = (
            "안녕하세요? 옷 같이 봐드리는 미야언니예요:)\n"
            "'지금 보시는 상품' 기준으로 제가 같이 봐드릴게요!\n"
            "사이즈, 코디, 배송,교환 중 뭐부터 얘기해볼까요?"
        )
    else:
        welcome = (
            "안녕하세요? 옷 같이 봐드리는 미야언니예요:)\n"
            "원하시는 상품 기준으로 같이 봐드릴게요!\n"
            "사이즈, 코디, 배송,교환 중 뭐부터 얘기해볼까요?"
        )
    st.session_state.messages.append({"role": "assistant", "content": welcome})

st.divider()

for msg in st.session_state.messages:
    safe_text = html.escape(msg["content"]).replace("\n", "<br>")

    if msg["role"] == "user":
        st.markdown(
            (
                '<div style="display:flex; justify-content:flex-end; width:100%; margin:2px 0 4px 0;">'
                '<div style="max-width:92%;">'
                '<div style="display:block; font-size:12px; font-weight:700; line-height:1.15; color:#0f6a63; text-align:right; margin:0 6px 1px 0;">고객님</div>'
                f'<div style="padding:10px 14px 10px 10px; border-radius:18px; border-bottom-right-radius:6px; font-size:15px; line-height:1.52; white-space:pre-wrap; word-break:keep-all; background:#dff0ec; color:#1f3b36; border:1px solid rgba(15,106,99,.14);">{safe_text}</div>'
                '</div>'
                '</div>'
            ),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            (
                '<div style="display:flex; justify-content:flex-start; width:100%; margin:2px 0 4px 0;">'
                '<div style="max-width:92%;">'
                '<div style="display:block; font-size:12px; font-weight:700; line-height:1.15; color:#5f6471; margin:0 0 1px 6px;">미야언니</div>'
                f'<div style="padding:10px 14px 10px 10px; border-radius:18px; border-bottom-left-radius:6px; font-size:15px; line-height:1.52; white-space:pre-wrap; word-break:keep-all; background:#071b4e; color:#ffffff; border:1px solid rgba(255,255,255,.08);">{safe_text}</div>'
                '</div>'
                '</div>'
            ),
            unsafe_allow_html=True,
        )

user_input = st.chat_input("메시지를 입력하세요…")
if user_input:
    process_user_message(user_input, product_context)
    st.rerun()
