import os
import re
import json
from urllib.parse import urlparse, parse_qs

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI


# =========================
# 기본 설정
# =========================
st.set_page_config(
    page_title="미야언니",
    layout="centered",
    initial_sidebar_state="collapsed",
)

OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))

if not OPENAI_API_KEY:
    st.error("OPENAI_API_KEY가 설정되지 않았습니다. .streamlit/secrets.toml 또는 Streamlit Secrets에 넣어주세요.")
    st.stop()

client = OpenAI(api_key=OPENAI_API_KEY)

DB_FILE_CANDIDATES = [
    "misharp_miya_db.csv",
    "misharp_miya_db (1).csv",
]

SIZE_OPTIONS_UI = ["", "44", "55", "55반", "66", "66반", "77", "77반", "88"]

POLICY_DB = {
    "shipping": {
        "courier": "CJ 대한통운",
        "shipping_fee": 3000,
        "free_shipping_over": 70000,
        "delivery_time": "결제 완료 후 2~4일 (영업일 기준)",
        "same_day_dispatch_rule": "오후 2시 이전 주문은 당일 출고",
    },
    "exchange_return": {
        "exchange_possible": "사이즈 교환 가능 / 동일상품 교환 가능 / 타상품 교환 가능",
        "period": "상품 수령 후 7일 이내",
        "exchange_fee": 6000,
        "return_fee_rule": "단순 변심 반품: 반품 후 주문금액이 7만원 이상이면 편도 3,000원 / 7만원 미만이면 왕복 6,000원",
        "defect_wrong": "불량/오배송은 미샵 부담입니다.",
    },
}

SYSTEM_PROMPT = """
너는 '미샵 쇼핑친구 미야언니'다.
4050 여성 고객의 쇼핑을 도와주는 친근하고 실용적인 상담 파트너다.

반드시 아래 원칙을 지켜라.
1. 상품 DB 정보가 있으면 DB를 최우선 근거로 사용한다.
2. DB에 없는 내용은 추측하지 않는다.
3. 사용자의 키/체중/상의/하의 정보를 우선 반영한다.
4. 상품 최대 권장 범위를 넘는 경우 절대 '잘 맞는다', '편하게 맞는다', '추천드린다'라고 단정하지 않는다.
5. 답변은 짧고 실용적으로, 미샵 고객 상담 톤으로 답한다.
6. 가능하면 현재 상품 설명 + 체형 판단 + 대체 상품 제안 순서로 답한다.
"""

REQUIRED_DB_COLUMNS = [
    "product_no",
    "product_name",
    "category",
    "sub_category",
    "price",
    "fabric",
    "fit_type",
    "size_range",
    "recommended_body_type",
    "body_cover_features",
    "style_tags",
    "season",
    "length_type",
    "sleeve_type",
    "color_options",
    "recommended_age",
    "coordination_items",
    "product_summary",
]


# =========================
# 유틸
# =========================
def clean_text(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def ensure_state():
    defaults = {
        "messages": [],
        "last_context_key": "",
        "body_height": "",
        "body_weight": "",
        "body_top": "",
        "body_bottom": "",
        "manual_url": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def qp_value(qp, key, default=""):
    value = qp.get(key, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value or default


def find_db_file():
    for path in DB_FILE_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


@st.cache_data
def load_product_db():
    db_path = find_db_file()
    if not db_path:
        return None, "CSV 파일을 찾지 못했습니다. app.py와 같은 위치에 misharp_miya_db.csv를 넣어주세요."

    try:
        df = pd.read_csv(db_path)
    except Exception as e:
        return None, f"CSV 파일을 읽는 중 오류가 발생했습니다: {e}"

    df.columns = [clean_text(c) for c in df.columns]

    missing = [c for c in REQUIRED_DB_COLUMNS if c not in df.columns]
    if missing:
        return None, f"CSV 필수 컬럼이 없습니다: {', '.join(missing)}"

    for col in REQUIRED_DB_COLUMNS:
        df[col] = df[col].fillna("").astype(str).map(clean_text)

    # product_no 문자열화
    df["product_no"] = df["product_no"].astype(str).str.replace(".0", "", regex=False).map(clean_text)

    # 중복 제거
    df = df.drop_duplicates(subset=["product_no"], keep="first").reset_index(drop=True)

    return df, None


def extract_product_no(url: str):
    if not url:
        return None
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "product_no" in qs and qs["product_no"]:
            return clean_text(qs["product_no"][0])
    except Exception:
        pass

    m = re.search(r"product_no=(\d+)", url)
    if m:
        return m.group(1)

    return None


def get_product_by_no(df: pd.DataFrame, product_no: str):
    if df is None or not product_no:
        return None
    rows = df[df["product_no"].astype(str) == str(product_no)]
    if len(rows) == 0:
        return None
    return rows.iloc[0].to_dict()


def contains_any(text: str, keywords):
    t = clean_text(text)
    return any(k in t for k in keywords)


def search_products(df: pd.DataFrame, keyword="", category="", body_type="", fit_type="", season="", limit=5):
    if df is None:
        return pd.DataFrame()

    result = df.copy()

    if category:
        result = result[
            result["category"].str.contains(category, case=False, na=False) |
            result["sub_category"].str.contains(category, case=False, na=False)
        ]

    if fit_type:
        result = result[result["fit_type"].str.contains(fit_type, case=False, na=False)]

    if body_type:
        result = result[
            result["recommended_body_type"].str.contains(body_type, case=False, na=False) |
            result["body_cover_features"].str.contains(body_type, case=False, na=False)
        ]

    if season:
        result = result[result["season"].str.contains(season, case=False, na=False)]

    if keyword:
        result = result[
            result["product_name"].str.contains(keyword, case=False, na=False) |
            result["style_tags"].str.contains(keyword, case=False, na=False) |
            result["product_summary"].str.contains(keyword, case=False, na=False) |
            result["coordination_items"].str.contains(keyword, case=False, na=False)
        ]

    return result.head(limit)


def recommend_similar_products(df: pd.DataFrame, product: dict, topn=3):
    if df is None or not product:
        return []

    result = df.copy()
    result = result[result["product_no"] != str(product.get("product_no", ""))]

    # 같은 카테고리 우선
    if product.get("category"):
        result = result[result["category"] == product["category"]]

    # 핏이 있으면 우선 반영
    if product.get("fit_type"):
        same_fit = result[result["fit_type"].str.contains(product["fit_type"], case=False, na=False)]
        if len(same_fit) > 0:
            result = same_fit

    return result.head(topn).to_dict("records")


def try_number(value):
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


def size_rank_korean(size_text: str):
    order = {
        "44": 1,
        "55": 2,
        "55반": 3,
        "66": 4,
        "66반": 5,
        "77": 6,
        "77반": 7,
        "88": 8,
    }
    return order.get(clean_text(size_text))


def extract_supported_ranks_from_db(size_range_text: str):
    text = clean_text(size_range_text)
    if not text:
        return []

    found = []
    ordered_tokens = ["44", "55반", "55", "66반", "66", "77반", "77", "88"]
    for token in ordered_tokens:
        if token in text:
            rank = size_rank_korean(token)
            if rank is not None:
                found.append(rank)

    # 55-77 형태 처리
    if "-" in text:
        parts = [clean_text(x) for x in text.split("-")]
        if len(parts) == 2:
            start_rank = size_rank_korean(parts[0])
            end_rank = size_rank_korean(parts[1])
            if start_rank and end_rank and start_rank <= end_rank:
                found.extend(list(range(start_rank, end_rank + 1)))

    return sorted(list(set(found)))


def extract_max_supported_rank_from_db(product: dict):
    if not product:
        return None
    ranks = extract_supported_ranks_from_db(product.get("size_range", ""))
    if not ranks:
        return None
    return max(ranks)


def is_user_size_over_product_limit(user_top_size: str, product: dict):
    user_rank = size_rank_korean(user_top_size)
    max_rank = extract_max_supported_rank_from_db(product)

    if user_rank is None or max_rank is None:
        return False, None, None

    return user_rank > max_rank, user_rank, max_rank


def recommend_size_from_db(weight_kg, top_size, product: dict):
    if not product:
        return {"recommended": None, "reason": "", "status": "unknown"}

    over_limit, _user_rank, max_rank = is_user_size_over_product_limit(top_size, product)
    if over_limit:
        rank_to_label = {
            1: "44", 2: "55", 3: "55반", 4: "66",
            5: "66반", 6: "77", 7: "77반", 8: "88",
        }
        max_label = rank_to_label.get(max_rank, "")
        return {
            "recommended": None,
            "reason": f"입력하신 상의 사이즈 기준으로는 이 상품이 최대 {max_label}까지만 커버하는 것으로 보여 권장 범위를 넘어요.",
            "status": "over_limit",
        }

    if top_size:
        return {
            "recommended": top_size,
            "reason": "DB 기준으로 보면 평소 입으시는 상의 사이즈를 먼저 기준 삼는 쪽이 가장 안전해요.",
            "status": "ok",
        }

    weight = try_number(weight_kg)
    if weight is not None:
        if weight <= 53:
            reco = "55"
        elif weight <= 61:
            reco = "66"
        elif weight <= 66:
            reco = "66반"
        elif weight <= 72:
            reco = "77"
        else:
            reco = "77반"
        return {
            "recommended": reco,
            "reason": "입력하신 체형 정보 기준으로 가장 무난한 방향으로 잡아본 추천이에요.",
            "status": "ok",
        }

    return {"recommended": None, "reason": "", "status": "unknown"}


def fetch_product_context_fallback(url: str):
    if not url:
        return None

    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return {
            "summary": "",
            "size_tip": "",
            "raw_excerpt": f"[상품 페이지 조회 실패: {e}]"
        }

    soup = BeautifulSoup(r.text, "html.parser")

    title = ""
    for selector in [
        "#span_product_name",
        "#span_product_name_mobile",
        ".infoArea #span_product_name",
        ".infoArea .headingArea h2",
        ".headingArea h2",
        "title",
    ]:
        el = soup.select_one(selector)
        if el:
            title = clean_text(el.get_text(" ", strip=True))
            if title:
                break

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    raw_text = clean_text(soup.get_text("\n"))
    raw_excerpt = raw_text[:2500]

    size_tip_lines = []
    for line in raw_text.split("."):
        line = clean_text(line)
        if contains_any(line, ["사이즈", "55", "66", "77", "88", "FREE", "L(", "F("]):
            size_tip_lines.append(line)

    return {
        "title": title,
        "summary": raw_excerpt[:1200],
        "size_tip": " / ".join(size_tip_lines)[:600],
        "raw_excerpt": raw_excerpt,
    }


def build_body_context():
    return {
        "height_cm": clean_text(st.session_state.body_height),
        "weight_kg": clean_text(st.session_state.body_weight),
        "top_size": clean_text(st.session_state.body_top),
        "bottom_size": clean_text(st.session_state.body_bottom),
    }


def build_body_context_text(body_ctx: dict):
    if not any(body_ctx.values()):
        return "입력된 체형 정보 없음"
    return (
        f"키 {body_ctx.get('height_cm') or '-'}cm / "
        f"체중 {body_ctx.get('weight_kg') or '-'}kg / "
        f"상의 {body_ctx.get('top_size') or '-'} / "
        f"하의 {body_ctx.get('bottom_size') or '-'}"
    )


def get_fast_policy_answer(user_text: str):
    q = user_text.replace(" ", "").lower()

    if any(k in q for k in ["배송비", "무료배송"]):
        return (
            f"배송은 {POLICY_DB['shipping']['courier']}를 이용하고 있어요.\n"
            f"배송비는 {POLICY_DB['shipping']['shipping_fee']:,}원이고, "
            f"{POLICY_DB['shipping']['free_shipping_over']:,}원 이상이면 무료배송이에요."
        )

    if any(k in q for k in ["언제출고", "출고", "당일출고"]):
        return (
            f"{POLICY_DB['shipping']['same_day_dispatch_rule']}예요.\n"
            f"보통은 {POLICY_DB['shipping']['delivery_time']} 정도 생각해주시면 돼요."
        )

    if any(k in q for k in ["교환", "사이즈교환"]):
        return (
            "교환은 가능해요.\n"
            f"{POLICY_DB['exchange_return']['exchange_possible']}이고, "
            f"{POLICY_DB['exchange_return']['period']} 안에 접수해주시면 돼요.\n"
            f"단순 변심 교환은 왕복 {POLICY_DB['exchange_return']['exchange_fee']:,}원으로 안내드리고 있어요."
        )

    if any(k in q for k in ["반품", "환불"]):
        return (
            "반품도 가능해요.\n"
            f"{POLICY_DB['exchange_return']['period']} 안에 접수해주시면 되고, "
            f"{POLICY_DB['exchange_return']['return_fee_rule']} 기준으로 진행돼요.\n"
            f"불량이나 오배송이면 배송비는 미샵에서 부담해드려요."
        )

    return None


def is_size_question(user_text: str):
    t = clean_text(user_text).replace(" ", "")
    keywords = [
        "사이즈", "맞을까", "맞나요", "맞아", "커요", "작아요", "타이트", "여유",
        "추천해", "추천", "몇사이즈", "어떤사이즈", "m이", "l이", "free", "f사이즈",
    ]
    return any(k in t for k in keywords)


def detect_general_recommendation_query(user_text: str):
    t = clean_text(user_text)
    categories = {
        "자켓": ["자켓", "재킷", "아우터"],
        "니트": ["니트", "가디건"],
        "슬랙스": ["슬랙스", "팬츠", "바지"],
        "원피스": ["원피스"],
        "블라우스": ["블라우스", "셔츠"],
        "맨투맨": ["맨투맨", "티셔츠"],
    }
    body_keywords = ["복부", "뱃살", "팔뚝", "하체", "키작녀", "키큰", "허리", "힙"]
    season_keywords = ["봄", "여름", "가을", "겨울", "간절기"]

    detected_category = ""
    for k, words in categories.items():
        if any(w in t for w in words):
            detected_category = k
            break

    detected_body = ""
    for b in body_keywords:
        if b in t:
            detected_body = b
            break

    detected_season = ""
    for s in season_keywords:
        if s in t:
            detected_season = s
            break

    is_query = any(x in t for x in ["추천", "어울", "찾아", "골라", "뭐 입", "코디"])
    return {
        "is_general_query": is_query and (detected_category or detected_body or detected_season),
        "category": detected_category,
        "body_type": detected_body,
        "season": detected_season,
    }


def format_product_for_prompt(product: dict):
    if not product:
        return "없음"

    lines = [
        f"상품번호: {product.get('product_no', '')}",
        f"상품명: {product.get('product_name', '')}",
        f"카테고리: {product.get('category', '')}",
        f"서브카테고리: {product.get('sub_category', '')}",
        f"가격: {product.get('price', '')}",
        f"소재: {product.get('fabric', '')}",
        f"핏: {product.get('fit_type', '')}",
        f"사이즈범위: {product.get('size_range', '')}",
        f"추천체형: {product.get('recommended_body_type', '')}",
        f"체형보완: {product.get('body_cover_features', '')}",
        f"스타일태그: {product.get('style_tags', '')}",
        f"계절: {product.get('season', '')}",
        f"기장: {product.get('length_type', '')}",
        f"소매: {product.get('sleeve_type', '')}",
        f"컬러: {product.get('color_options', '')}",
        f"코디아이템: {product.get('coordination_items', '')}",
        f"요약: {product.get('product_summary', '')}",
    ]
    return "\n".join(lines)


def build_context_key(url: str):
    return clean_text(url)


def get_llm_answer(user_text: str, current_product: dict, similar_products: list, fallback_context: dict):
    body_ctx = build_body_context()
    size_reco = recommend_size_from_db(body_ctx.get("weight_kg", ""), body_ctx.get("top_size", ""), current_product)

    prompt_payload = {
        "body_context": body_ctx,
        "current_product_db": current_product or {},
        "size_recommendation": size_reco,
        "similar_products": similar_products or [],
        "fallback_context": fallback_context or {},
        "policy_db": POLICY_DB,
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "참고 데이터(JSON):\n" + json.dumps(prompt_payload, ensure_ascii=False)},
    ]

    history = st.session_state.messages[-8:]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})

    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.15,
        max_tokens=500,
    )
    return resp.choices[0].message.content.strip()


def build_hard_size_answer(current_product: dict):
    if not current_product:
        return None

    body_ctx = build_body_context()
    top_size = clean_text(body_ctx.get("top_size", ""))

    over_limit, _user_rank, max_rank = is_user_size_over_product_limit(top_size, current_product)
    if not over_limit:
        return None

    rank_to_label = {
        1: "44", 2: "55", 3: "55반", 4: "66",
        5: "66반", 6: "77", 7: "77반", 8: "88",
    }
    max_label = rank_to_label.get(max_rank, "")

    return (
        f"입력하신 상의 {top_size} 기준이면 이 상품은 DB상 최대 {max_label}까지로 보여서 "
        f"여유 있게 맞는다고 보긴 어려워요.\n"
        f"편안함 우선이면 {top_size}를 안정적으로 커버하는 다른 상품을 같이 보시는 쪽이 더 안전해요."
    )


def build_general_recommendation_answer(user_text: str, db: pd.DataFrame):
    q = detect_general_recommendation_query(user_text)
    if not q["is_general_query"]:
        return None

    candidates = search_products(
        db,
        keyword=user_text,
        category=q["category"],
        body_type=q["body_type"],
        season=q["season"],
        limit=5,
    )

    if len(candidates) == 0:
        return None

    body_ctx = build_body_context()
    candidate_records = candidates.to_dict("records")

    payload = {
        "body_context": body_ctx,
        "query": user_text,
        "candidate_products": candidate_records,
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "system",
            "content": (
                "아래 candidate_products 안에서만 추천하라. "
                "각 상품명과 추천 이유를 3개 정도 제안하고, "
                "가능하면 체형/활용상황 기준으로 설명하라."
            ),
        },
        {"role": "system", "content": "참고 데이터(JSON):\n" + json.dumps(payload, ensure_ascii=False)},
        {"role": "user", "content": user_text},
    ]

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.15,
        max_tokens=500,
    )
    return resp.choices[0].message.content.strip()


def process_user_message(user_text: str, db: pd.DataFrame, current_product: dict, similar_products: list, fallback_context: dict):
    st.session_state.messages.append({"role": "user", "content": user_text})

    fast = get_fast_policy_answer(user_text)
    if fast:
        st.session_state.messages.append({"role": "assistant", "content": fast})
        return

    general_answer = build_general_recommendation_answer(user_text, db)
    if general_answer:
        st.session_state.messages.append({"role": "assistant", "content": general_answer})
        return

    if is_size_question(user_text):
        hard_answer = build_hard_size_answer(current_product)
        if hard_answer:
            st.session_state.messages.append({"role": "assistant", "content": hard_answer})
            return

    answer = get_llm_answer(user_text, current_product, similar_products, fallback_context)
    st.session_state.messages.append({"role": "assistant", "content": answer})


# =========================
# 시작
# =========================
ensure_state()

db, db_error = load_product_db()

qp = st.query_params
url_from_qp = qp_value(qp, "url", "")
product_name_q = qp_value(qp, "pname", "")

current_url = url_from_qp or st.session_state.manual_url
current_product_no = extract_product_no(current_url)

if build_context_key(current_url) != st.session_state.last_context_key:
    st.session_state.last_context_key = build_context_key(current_url)
    st.session_state.messages = []

current_product = None
similar_products = []
fallback_context = None

if db is not None and current_product_no:
    current_product = get_product_by_no(db, current_product_no)
    if current_product:
        similar_products = recommend_similar_products(db, current_product, topn=3)

if current_url and not current_product:
    fallback_context = fetch_product_context_fallback(current_url)

# =========================
# UI
# =========================
st.markdown(
    """
    <style>
    header[data-testid="stHeader"] {display:none;}
    #MainMenu {visibility:hidden;}
    footer {visibility:hidden;}
    .block-container {max-width: 860px; padding-top: 1.1rem; padding-bottom: 8rem;}
    .small-muted {font-size: 12px; color: #6b7280;}
    .box {
        border: 1px solid #e5e7eb;
        border-radius: 14px;
        padding: 14px 16px;
        margin-bottom: 12px;
        background: #ffffff;
    }
    .title-main {
        font-size: 30px;
        font-weight: 800;
        line-height: 1.1;
        letter-spacing: -0.02em;
        margin-bottom: 6px;
    }
    .subtitle {
        color: #6b7280;
        margin-bottom: 18px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown("<div class='title-main'>미샵 쇼핑친구 미야언니</div>", unsafe_allow_html=True)
st.markdown("<div class='subtitle'>DB 기반으로 더 안정적으로 상담하는 미샵 전용 AI 쇼핑 도우미</div>", unsafe_allow_html=True)

if db_error:
    st.error(db_error)
else:
    st.success(f"미야언니 DB 로드 완료: {len(db):,}개 상품")

with st.expander("상품 DB 연결 상태 확인", expanded=False):
    if db is not None:
        st.write("CSV 파일:", find_db_file())
        st.write("필수 컬럼 확인 완료")
        st.dataframe(db.head(5), use_container_width=True)

st.markdown("### 상품 URL 입력")
url_input = st.text_input(
    "미샵 상품 상세 URL",
    value=current_url,
    placeholder="https://www.misharp.co.kr/product/detail.html?product_no=28522&cate_no=24&display_group=1",
)
st.session_state.manual_url = url_input

body_col1, body_col2 = st.columns(2)
with body_col1:
    st.session_state.body_height = st.text_input("키", value=st.session_state.body_height, placeholder="cm")
with body_col2:
    st.session_state.body_weight = st.text_input("체중", value=st.session_state.body_weight, placeholder="kg")

body_col3, body_col4 = st.columns(2)
with body_col3:
    current_top = st.session_state.body_top if st.session_state.body_top in SIZE_OPTIONS_UI else ""
    st.session_state.body_top = st.selectbox("상의", SIZE_OPTIONS_UI, index=SIZE_OPTIONS_UI.index(current_top))
with body_col4:
    current_bottom = st.session_state.body_bottom if st.session_state.body_bottom in SIZE_OPTIONS_UI else ""
    st.session_state.body_bottom = st.selectbox("하의", SIZE_OPTIONS_UI, index=SIZE_OPTIONS_UI.index(current_bottom))

st.markdown(f"<div class='small-muted'>현재 체형정보: {build_body_context_text(build_body_context())}</div>", unsafe_allow_html=True)

if current_product:
    st.markdown("### 현재 상품 (DB 기준)")
    st.markdown(
        f"""
        <div class="box">
            <b>{current_product.get("product_name", "")}</b><br>
            카테고리: {current_product.get("category", "")} / {current_product.get("sub_category", "")}<br>
            소재: {current_product.get("fabric", "")}<br>
            핏: {current_product.get("fit_type", "")}<br>
            사이즈범위: {current_product.get("size_range", "")}<br>
            추천체형: {current_product.get("recommended_body_type", "")}<br>
            체형보완: {current_product.get("body_cover_features", "")}<br>
            스타일: {current_product.get("style_tags", "")}<br>
            요약: {current_product.get("product_summary", "")}
        </div>
        """,
        unsafe_allow_html=True,
    )

    size_result = recommend_size_from_db(
        st.session_state.body_weight,
        st.session_state.body_top,
        current_product,
    )

    if size_result.get("recommended"):
        st.info(f"추천 사이즈 기준: {size_result['recommended']} / {size_result['reason']}")
    elif size_result.get("status") == "over_limit":
        st.warning(size_result["reason"])

    if similar_products:
        with st.expander("비슷한 상품 추천 보기", expanded=False):
            for p in similar_products:
                st.markdown(
                    f"- **{p.get('product_name','')}** | {p.get('category','')} | {p.get('fit_type','')} | {p.get('body_cover_features','')}"
                )

elif fallback_context:
    st.markdown("### 현재 상품 (DB 미매칭, 보조 분석)")
    st.markdown(
        f"""
        <div class="box">
            <b>{fallback_context.get("title", product_name_q or "상품명 확인 중")}</b><br>
            요약: {fallback_context.get("summary", "")[:500]}
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.warning("이 상품은 현재 DB에서 찾지 못해 보조 크롤링 정보로만 상담합니다. 가능하면 DB를 최신화해주세요.")
else:
    st.info("상품 URL을 넣으면 해당 상품을 DB 기준으로 먼저 읽고 상담합니다.")

st.markdown("### 빠른 질문 예시")
ex1, ex2, ex3 = st.columns(3)
if ex1.button("이 상품 66반도 괜찮아?"):
    st.session_state.messages.append({"role": "user", "content": "이 상품 66반도 괜찮아?"})
if ex2.button("비슷한 다른 상품도 추천해줘"):
    st.session_state.messages.append({"role": "user", "content": "비슷한 다른 상품도 추천해줘"})
if ex3.button("학교 상담룩 자켓 추천해줘"):
    st.session_state.messages.append({"role": "user", "content": "학교 상담룩 자켓 추천해줘"})

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_text = st.chat_input("예: 163cm 62kg인데 이 상품 66 괜찮을까? / 학교 상담룩 자켓 추천해줘")
if user_text:
    if db is None:
        st.error("먼저 CSV 파일을 정상 연결해주세요.")
    else:
        process_user_message(user_text, db, current_product, similar_products, fallback_context)
        st.rerun()
