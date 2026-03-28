import os
import re
import json
import html
from urllib.parse import urlparse, parse_qs

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI, RateLimitError, APIError, APITimeoutError
import time

st.set_page_config(
    page_title="미야언니",
    layout="centered",
    initial_sidebar_state="collapsed",
)

OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))

if not OPENAI_API_KEY:
    st.error("OPENAI_API_KEY가 설정되지 않았습니다. Streamlit Cloud > App settings > Secrets에 넣어주세요.")
    st.stop()

client = OpenAI(api_key=OPENAI_API_KEY, timeout=25.0, max_retries=1)

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
4050 여성 고객이 쇼핑할 때 옆에서 같이 봐주는 친근한 쇼핑 상담 파트너다.

규칙:
- 상품 DB 정보가 있으면 DB를 최우선 근거로 사용한다.
- 지금 보고 있는 상품 기준으로 답한다.
- 사용자가 입력한 키/체중/상의/하의 정보를 우선 사용한다.
- 상품 최대 권장 범위를 넘으면 절대 '잘 맞는다', '편하게 맞는다', '추천드린다'라고 말하지 않는다.
- 배송/교환 답변은 제공된 정책만 사용한다.
- 모르는 정보는 지어내지 않는다.
- 답변은 짧고 실용적으로 한다.
- 현재 상품이 있으면 현재 상품 설명 후, 필요 시 비슷한 대체 상품을 함께 제안한다.
"""

SIZE_OPTIONS_UI = ["", "44", "55", "55반", "66", "66반", "77", "77반", "88"]
GENERIC_NAMES = {"미샵", "misharp", "MISHARP", "미샵여성", "Misharp"}
DB_FILE_CANDIDATES = [
    "misharp_miya_db.csv",
    "misharp_miya_db (1).csv",
]
DB_REQUIRED_COLUMNS = [
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


def qp_value(qp, key, default=""):
    value = qp.get(key, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value or default


qp = st.query_params
current_url = qp_value(qp, "url", "")
product_no_from_qp = qp_value(qp, "pn", "")
product_name_q = qp_value(qp, "pname", "")


def build_context_key(url: str, pn: str, pname: str) -> str:
    return f"{url}|{pn}|{pname}"


def is_product_page(url: str, pn: str) -> bool:
    u = (url or "").lower()
    p = (pn or "").strip()
    return "/product/detail" in u or "product_no=" in u or bool(p)


def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def normalize_product_name(name: str) -> str:
    name = clean_text(name)
    name = re.sub(r"\s*\|\s*.*$", "", name)
    name = re.sub(r"\s*-\s*미샵.*$", "", name, flags=re.I)
    name = re.sub(r"\s*-\s*MISHARP.*$", "", name, flags=re.I)
    return clean_text(name)


def is_generic_name(name: str) -> bool:
    name = clean_text(name)
    return (not name) or (name in GENERIC_NAMES) or len(name) <= 2


def uniq_keep_order(items):
    out = []
    seen = set()
    for item in items:
        item = clean_text(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


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


def split_sections(text: str) -> dict:
    if not text:
        return {"summary": "", "size_tip": ""}

    lines = [clean_text(x) for x in text.split("\n")]
    lines = [x for x in lines if x]
    joined = "\n".join(lines)

    size_lines = []
    for line in lines:
        if any(k in line for k in ["사이즈", "정사이즈", "55", "66", "66반", "77", "77반", "88", "F(", "L(", "S(", "M(", "XL"]):
            size_lines.append(line)

    return {
        "summary": joined[:3000],
        "size_tip": " / ".join(size_lines)[:1200],
    }


def nearby_label_text(select_tag) -> str:
    pieces = []
    prev_label = select_tag.find_previous(["label", "th", "dt", "strong", "span"])
    if prev_label:
        pieces.append(prev_label.get_text(" ", strip=True))
    parent = select_tag.parent
    if parent:
        pieces.append(parent.get_text(" ", strip=True)[:200])
    return clean_text(" ".join(pieces))


def is_bad_option_text(text: str) -> bool:
    bad_keywords = [
        "필수 옵션", "옵션 선택", "선택해주세요", "----", "품절", "SOLD OUT",
        "LANGUAGE", "SHIPPING TO", "통화", "국가", "배송국가", "배송지", "언어",
    ]
    return any(k.lower() in text.lower() for k in bad_keywords)


def looks_like_size_group(label_text: str, option_texts: list[str]) -> bool:
    joined = " ".join(option_texts).upper()
    label_text_l = label_text.lower()
    if "사이즈" in label_text or "size" in label_text_l:
        return True
    patterns = [
        r"\b44\b", r"\b55\b", r"55반", r"\b66\b", r"66반", r"\b77\b", r"77반", r"\b88\b",
        r"\bS\b", r"\bM\b", r"\bL\b", r"\bXL\b", r"\bFREE\b", r"\bF\b",
    ]
    return any(re.search(p, joined) for p in patterns)


def extract_option_groups(soup: BeautifulSoup):
    groups = []
    for sel in soup.select("select"):
        name_attr = clean_text(sel.get("name", ""))
        id_attr = clean_text(sel.get("id", ""))
        cls_attr = " ".join(sel.get("class", []))
        meta = f"{name_attr} {id_attr} {cls_attr}".lower()

        if any(bad in meta for bad in ["quantity", "qty", "language", "shipping", "country", "currency"]):
            continue

        option_texts = []
        for opt in sel.select("option"):
            if opt.has_attr("disabled"):
                continue
            val = clean_text(opt.get("value", ""))
            txt = clean_text(opt.get_text(" ", strip=True))
            if not txt:
                continue
            if not val and is_bad_option_text(txt):
                continue
            if is_bad_option_text(txt):
                continue
            if len(txt) > 80:
                continue
            option_texts.append(txt)

        option_texts = uniq_keep_order(option_texts)
        if not option_texts:
            continue

        label_text = nearby_label_text(sel)
        group_type = "size" if looks_like_size_group(label_text, option_texts) else None

        groups.append({
            "type": group_type,
            "label": label_text,
            "options": option_texts,
        })
    return groups


def extract_product_no_from_url(url: str, pn_fallback: str = "") -> str:
    if pn_fallback:
        return clean_text(pn_fallback)
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "product_no" in qs and qs["product_no"]:
            return clean_text(qs["product_no"][0])
    except Exception:
        pass
    m = re.search(r"product_no=(\d+)", url or "")
    if m:
        return m.group(1)
    return ""


def find_db_file() -> str:
    for path in DB_FILE_CANDIDATES:
        if os.path.exists(path):
            return path
    return ""


@st.cache_data(ttl=300, show_spinner=False)
def load_product_db():
    db_path = find_db_file()
    if not db_path:
        return None, ""
    try:
        df = pd.read_csv(db_path)
    except Exception as e:
        return None, f"상품 DB CSV를 읽지 못했습니다: {e}"

    df.columns = [clean_text(c) for c in df.columns]
    missing = [c for c in DB_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return None, f"상품 DB 컬럼이 부족합니다: {', '.join(missing)}"

    for col in DB_REQUIRED_COLUMNS:
        df[col] = df[col].fillna("").astype(str).map(clean_text)

    df["product_no"] = df["product_no"].astype(str).str.replace(".0", "", regex=False).map(clean_text)
    df = df.drop_duplicates(subset=["product_no"], keep="first").reset_index(drop=True)
    return df, ""


def parse_db_size_options(size_range_text: str) -> list[str]:
    text = clean_text(size_range_text)
    if not text:
        return []

    tokens = ["44", "55", "55반", "66", "66반", "77", "77반", "88", "FREE", "F", "S", "M", "L", "XL"]
    found = []
    upper = text.upper()
    for token in tokens:
        if token in ["FREE", "F", "S", "M", "L", "XL"]:
            if re.search(rf"\b{re.escape(token)}\b", upper):
                found.append(token)
        else:
            if token in text:
                found.append(token)

    if "-" in text:
        parts = [clean_text(x) for x in text.split("-")]
        if len(parts) == 2:
            order = ["44", "55", "55반", "66", "66반", "77", "77반", "88"]
            if parts[0] in order and parts[1] in order:
                s = order.index(parts[0])
                e = order.index(parts[1])
                found.extend(order[min(s, e): max(s, e) + 1])

    return uniq_keep_order(found)


def db_row_to_product_context(row: dict) -> dict:
    size_options = parse_db_size_options(row.get("size_range", ""))
    size_tip_parts = []
    if row.get("size_range"):
        size_tip_parts.append(f"사이즈 범위 {row.get('size_range')}")
    if row.get("recommended_body_type"):
        size_tip_parts.append(f"추천 체형 {row.get('recommended_body_type')}")
    if row.get("body_cover_features"):
        size_tip_parts.append(f"체형 보완 {row.get('body_cover_features')}")

    summary_parts = []
    for key in [
        "product_summary", "fabric", "fit_type", "style_tags", "season",
        "length_type", "sleeve_type", "color_options", "coordination_items",
    ]:
        value = row.get(key, "")
        if value:
            label = {
                "product_summary": "요약",
                "fabric": "소재",
                "fit_type": "핏",
                "style_tags": "스타일",
                "season": "계절",
                "length_type": "기장",
                "sleeve_type": "소매",
                "color_options": "컬러",
                "coordination_items": "코디",
            }[key]
            summary_parts.append(f"{label}: {value}")

    return {
        "product_name": row.get("product_name", "") or "지금 보시는 상품",
        "summary": " / ".join(summary_parts)[:3000],
        "size_tip": " / ".join(size_tip_parts)[:1200],
        "size_options": size_options,
        "raw_excerpt": json.dumps(row, ensure_ascii=False),
        "db_row": row,
        "source": "db",
    }


def get_product_row_from_db(df: pd.DataFrame | None, product_no: str) -> dict | None:
    if df is None or not product_no:
        return None
    rows = df[df["product_no"].astype(str) == str(product_no)]
    if len(rows) == 0:
        return None
    return rows.iloc[0].to_dict()


def get_similar_products_from_db(df: pd.DataFrame | None, row: dict | None, topn: int = 3) -> list[dict]:
    if df is None or not row:
        return []

    work = df.copy()
    work = work[work["product_no"].astype(str) != str(row.get("product_no", ""))]

    if row.get("category"):
        work = work[work["category"].astype(str) == str(row.get("category"))]
    if row.get("sub_category"):
        sub = work[work["sub_category"].astype(str) == str(row.get("sub_category"))]
        if len(sub) > 0:
            work = sub
    if row.get("fit_type"):
        fit = work[work["fit_type"].astype(str).str.contains(str(row.get("fit_type")), na=False)]
        if len(fit) > 0:
            work = fit

    cols = [
        "product_no", "product_name", "category", "sub_category", "fit_type",
        "size_range", "body_cover_features", "style_tags", "product_summary"
    ]
    cols = [c for c in cols if c in work.columns]
    return work.head(topn)[cols].to_dict("records")


def fetch_product_context(url: str, passed_name: str = "") -> dict | None:
    if not url:
        return None

    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    product_name = normalize_product_name(passed_name)
    if is_generic_name(product_name):
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
                    product_name = candidate
                    break

    if is_generic_name(product_name):
        product_name = "지금 보시는 상품"

    option_groups = extract_option_groups(soup)
    size_options = []

    for group in option_groups:
        if group["type"] == "size":
            size_options.extend(group["options"])

    size_options = uniq_keep_order(size_options)

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    raw_text = soup.get_text("\n")
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()
    sections = split_sections(raw_text)

    return {
        "product_name": product_name,
        "summary": sections["summary"],
        "size_tip": sections["size_tip"],
        "size_options": size_options,
        "raw_excerpt": raw_text[:5000],
        "source": "crawl",
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
            "summary": "",
            "size_tip": "",
            "size_options": [],
            "raw_excerpt": f"[상품 정보를 가져오지 못했습니다: {e}]",
            "source": "crawl",
        }


def normalize_size_options(size_options):
    cleaned = []
    for s in size_options or []:
        s = clean_text(s)
        up = s.upper()
        if not s:
            continue
        if any(bad in up for bad in ["LANGUAGE", "SHIPPING TO", "COUNTRY", "배송지", "언어"]):
            continue
        if len(s) > 40:
            continue
        cleaned.append(s)
    return uniq_keep_order(cleaned)


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


def extract_supported_ranks_from_option_text(text: str):
    text = clean_text(text)
    if not text:
        return []

    found = []
    ordered_tokens = ["44", "55반", "55", "66반", "66", "77반", "77", "88"]
    for token in ordered_tokens:
        if token in text:
            rank = size_rank_korean(token)
            if rank is not None:
                found.append(rank)
    return found


def extract_max_supported_rank(product_context: dict | None):
    if not product_context:
        return None

    ranks = []
    for s in product_context.get("size_options", []) or []:
        ranks.extend(extract_supported_ranks_from_option_text(s))

    ranks.extend(extract_supported_ranks_from_option_text(product_context.get("size_tip", "")))

    db_row = product_context.get("db_row") or {}
    ranks.extend(extract_supported_ranks_from_option_text(db_row.get("size_range", "")))

    if not ranks:
        return None
    return max(ranks)


def is_user_size_over_product_limit(user_top_size: str, product_context: dict | None):
    user_rank = size_rank_korean(user_top_size)
    max_rank = extract_max_supported_rank(product_context)

    if user_rank is None or max_rank is None:
        return False, None, None

    return user_rank > max_rank, user_rank, max_rank


def detect_free_size(size_options):
    for s in size_options:
        up = s.upper()
        if "FREE" in up or up == "F" or up.startswith("F("):
            return s
    return None


def contains_alpha_sizes(size_options):
    joined = " ".join(size_options).upper()
    return any(re.search(rf"\b{x}\b", joined) for x in ["S", "M", "L", "XL", "XXL"])


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


def recommend_size(height_cm, weight_kg, top_size, product_context: dict | None):
    options = normalize_size_options((product_context or {}).get("size_options", []))
    if not product_context:
        return {"recommended": None, "reason": "", "status": "unknown"}

    over_limit, _user_rank, max_rank = is_user_size_over_product_limit(top_size, product_context)
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

    free_size = detect_free_size(options)
    if free_size:
        return {
            "recommended": free_size,
            "reason": f"이 상품은 {free_size} 기준으로 보시면 돼요.",
            "status": "ok",
        }

    weight = try_number(weight_kg)
    if weight is None:
        if top_size:
            return {
                "recommended": top_size,
                "reason": "평소 입으시는 상의 사이즈 기준으로 먼저 보는 쪽이 가장 안전해요.",
                "status": "ok",
            }
        return {"recommended": None, "reason": "", "status": "unknown"}

    if contains_alpha_sizes(options):
        return {
            "recommended": pick_from_alpha(weight, options),
            "reason": "현재 체형 기준으로 가장 무난하게 보이는 옵션이에요.",
            "status": "ok",
        }

    if contains_korean_sizes(options):
        return {
            "recommended": pick_from_korean(weight, options),
            "reason": "지금 입력해주신 체형 기준으로 가장 가까운 옵션이에요.",
            "status": "ok",
        }

    if top_size:
        return {
            "recommended": top_size,
            "reason": "DB에 세부 옵션이 적어서 평소 입으시는 상의 사이즈 기준으로 먼저 보는 쪽이 안전해요.",
            "status": "ok",
        }

    return {"recommended": None, "reason": "", "status": "unknown"}


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
            f"보통은 {POLICY_DB['shipping']['delivery_time']} 정도 생각해주시면 돼요."
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


def is_size_question(user_text: str) -> bool:
    t = clean_text(user_text).replace(" ", "")
    keywords = [
        "사이즈", "맞을까", "맞나요", "맞아", "커요", "작아요", "타이트", "여유",
        "추천해", "추천", "몇사이즈", "어떤사이즈", "m이", "l이", "free", "f사이즈",
    ]
    return any(k in t for k in keywords)


def wants_similar_reco(user_text: str) -> bool:
    t = clean_text(user_text).replace(" ", "")
    keywords = ["비슷한상품", "다른상품", "다른거", "유사한상품", "대체상품", "비슷한거", "다른추천"]
    return any(k in t for k in keywords)


def build_hard_size_answer(product_context: dict | None):
    if not product_context:
        return None

    body_ctx = build_body_context()
    top_size = clean_text(body_ctx.get("top_size", ""))

    over_limit, _user_rank, max_rank = is_user_size_over_product_limit(top_size, product_context)
    if not over_limit:
        return None

    rank_to_label = {
        1: "44", 2: "55", 3: "55반", 4: "66",
        5: "66반", 6: "77", 7: "77반", 8: "88",
    }
    max_label = rank_to_label.get(max_rank, "")

    option_text = ", ".join(product_context.get("size_options", []) or [])
    tip_text = clean_text(product_context.get("size_tip", ""))

    basis = option_text or f"최대 {max_label}"
    if tip_text:
        basis = f"{basis} / 사이즈 안내: {tip_text[:120]}"

    return (
        f"입력하신 상의 {top_size} 기준이면 이 상품은 페이지상 {basis}까지라 "
        f"여유 있게 맞는다고 보긴 어려워요.\n"
        f"최대 권장 범위가 {max_label}까지로 보여서 타이트할 수 있고, "
        f"편안함 우선이면 더 여유 있게 커버되는 상의를 보시는 쪽이 더 안전해요."
    )


def build_context_pack(product_context: dict | None, similar_products: list[dict], db_status: dict):
    body_context = build_body_context()
    is_detail = is_product_page(current_url, product_no_from_qp)

    size_reco = None
    if product_context:
        size_reco = recommend_size(
            body_context.get("height_cm", ""),
            body_context.get("weight_kg", ""),
            body_context.get("top_size", ""),
            product_context,
        )

    return {
        "policy_db": POLICY_DB,
        "viewer_context": {
            "url": current_url,
            "product_no": extract_product_no_from_url(current_url, product_no_from_qp),
            "is_product_page": is_detail,
        },
        "body_context": body_context,
        "product_context": product_context,
        "size_recommendation": size_reco,
        "similar_products": similar_products,
        "db_status": db_status,
    }


def build_similar_reply(similar_products: list[dict]) -> str | None:
    if not similar_products:
        return None
    lines = ["비슷하게 보실 만한 상품도 같이 골라드릴게요 :)"]
    for idx, item in enumerate(similar_products[:3], start=1):
        reason_parts = []
        if item.get("fit_type"):
            reason_parts.append(item["fit_type"])
        if item.get("body_cover_features"):
            reason_parts.append(item["body_cover_features"])
        if item.get("style_tags"):
            reason_parts.append(item["style_tags"])
        reason = " / ".join(reason_parts)
        if reason:
            lines.append(f"{idx}. {item.get('product_name','')} · {reason}")
        else:
            lines.append(f"{idx}. {item.get('product_name','')}")
    lines.append("원하시면 이 중에서 어떤 분께 더 잘 맞는지도 바로 골라드릴게요.")
    return "\n".join(lines)


def _trim_text(value, limit=240):
    if value is None:
        return ""
    value = str(value).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _slim_product_context(product_context: dict | None) -> dict:
    if not product_context:
        return {}
    keep = [
        "product_name", "price", "summary", "fabric", "fit", "fit_type",
        "size_tip", "size_options", "color_options", "season", "recommended_age",
        "recommended_body_type", "body_cover_features", "style_tags", "coordination_items",
    ]
    slim = {}
    for k in keep:
        if k in product_context and product_context.get(k):
            v = product_context.get(k)
            if isinstance(v, list):
                slim[k] = [ _trim_text(x, 60) for x in v[:6] ]
            else:
                slim[k] = _trim_text(v, 260)
    return slim


def _slim_similar_products(similar_products: list[dict]) -> list[dict]:
    slim_list = []
    for item in (similar_products or [])[:3]:
        slim_list.append({
            "product_name": _trim_text(item.get("product_name", ""), 80),
            "category": _trim_text(item.get("category", ""), 40),
            "price": _trim_text(item.get("price", ""), 30),
            "style_tags": _trim_text(item.get("style_tags", ""), 80),
            "reason": _trim_text(item.get("reason", ""), 120),
        })
    return slim_list


def _slim_context_pack(context_pack: dict) -> dict:
    body_context = context_pack.get("body_context") or {}
    viewer_context = context_pack.get("viewer_context") or {}
    size_reco = context_pack.get("size_recommendation") or {}
    return {
        "viewer_context": viewer_context,
        "body_context": body_context,
        "db_status": context_pack.get("db_status") or {},
        "size_recommendation": {
            "status": size_reco.get("status"),
            "recommended": size_reco.get("recommended"),
            "reason": _trim_text(size_reco.get("reason", ""), 180),
        },
        "product_context": _slim_product_context(context_pack.get("product_context")),
        "similar_products": _slim_similar_products(context_pack.get("similar_products") or []),
    }


def _llm_fallback_answer(user_text: str, product_context: dict | None, similar_products: list[dict], db_status: dict) -> str:
    pname = (product_context or {}).get("product_name") or "지금 보시는 상품"
    if is_size_question(user_text):
        hard = build_hard_size_answer(product_context)
        if hard:
            return hard
    if wants_similar_reco(user_text):
        sim = build_similar_reply(similar_products)
        if sim:
            return sim
    fast = get_fast_policy_answer(user_text)
    if fast:
        return fast

    parts = [f"지금은 문의가 잠깐 몰려서 답변이 지연되고 있어요. 우선 {pname} 기준으로 짧게 안내드릴게요."]
    if product_context:
        if product_context.get("size_tip"):
            parts.append("사이즈는 " + _trim_text(product_context.get("size_tip"), 120))
        elif product_context.get("size_options"):
            parts.append("확인되는 사이즈 옵션은 " + ", ".join(product_context.get("size_options", [])[:6]) + " 입니다.")
        if product_context.get("fabric"):
            parts.append("소재는 " + _trim_text(product_context.get("fabric"), 90) + " 기준으로 보시면 됩니다.")
    if similar_products:
        first = similar_products[0].get("product_name")
        if first:
            parts.append(f"비슷한 느낌으로는 {first}도 함께 보셔도 좋아요.")
    parts.append("잠시 후 다시 한 번 보내주시면 더 정확하게 이어서 답변드릴게요.")
    return "\n\n".join(parts)


def get_llm_answer(user_text: str, product_context: dict | None, similar_products: list[dict], db_status: dict) -> str:
    context_pack = build_context_pack(product_context, similar_products, db_status)
    slim_context = _slim_context_pack(context_pack)
    is_detail = (slim_context.get("viewer_context") or {}).get("is_product_page")

    extra_rules = []
    if is_detail:
        extra_rules.append("현재는 상품 상세페이지 기준 상담입니다. 현재 페이지 기준으로 바로 답하세요.")
        extra_rules.append("상세페이지에서 다시 눌러달라는 말은 하지 마세요.")
    else:
        extra_rules.append("현재는 일반 유입일 수 있습니다. 상품 정보가 부족하면 현재 보고 계신 상품 기준으로 물어보면 더 정확하다고 아주 짧게만 안내할 수 있습니다.")

    if db_status.get("db_hit"):
        extra_rules.append("현재 상품은 DB와 매칭되었습니다. DB 정보를 가장 우선적으로 반영하세요.")
    elif db_status.get("db_loaded"):
        extra_rules.append("DB는 로드되어 있지만 현재 상품은 DB 미매칭 상태입니다. 크롤링 정보만 사용하세요.")
    else:
        extra_rules.append("현재 DB를 불러오지 못했습니다. 크롤링 정보만 사용하세요.")

    if product_context:
        pname = product_context.get("product_name", "")
        if pname and pname != "지금 보시는 상품":
            extra_rules.append(f"현재 상품명 후보: {pname}")
        if product_context.get("size_options"):
            extra_rules.append("확인된 사이즈 옵션: " + ", ".join(product_context["size_options"][:6]))
        if product_context.get("size_tip"):
            extra_rules.append("본문/DB 사이즈 안내: " + _trim_text(product_context["size_tip"], 220))

    if similar_products:
        extra_rules.append("사용자가 다른 상품 추천을 원하면 similar_products 범위 안에서 먼저 제안하세요.")

    size_reco = slim_context.get("size_recommendation") or {}
    if size_reco.get("recommended"):
        extra_rules.append(f"추천 사이즈 기준값: {size_reco['recommended']}")
    if size_reco.get("status") == "over_limit":
        extra_rules.append("사용자 상의 사이즈가 상품 최대 권장 범위를 넘으면 절대 추천하지 마세요.")
        extra_rules.append(f"사이즈 제한 사유: {size_reco.get('reason', '')}")

    body_ctx = slim_context.get("body_context") or {}
    if any(body_ctx.values()):
        extra_rules.append("사용자가 이미 입력한 키/체중/상의/하의 정보가 있으면 그 정보를 우선 반영해서 답하세요.")
        extra_rules.append("사용자 입력 체형 정보가 있는데도 다시 체형을 물어보지 마세요.")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "추가 규칙:\n- " + "\n- ".join(extra_rules)},
        {"role": "system", "content": "참고 데이터(JSON):\n" + json.dumps(slim_context, ensure_ascii=False)},
    ]

    history = st.session_state.messages[-6:]
    for m in history:
        content = _trim_text(m.get("content", ""), 280)
        if content:
            messages.append({"role": m["role"], "content": content})

    messages.append({"role": "user", "content": _trim_text(user_text, 350)})

    last_err = None
    for wait_s in (1.0, 2.0):
        try:
            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=messages,
                temperature=0.1,
                max_tokens=280,
            )
            content = (resp.choices[0].message.content or "").strip()
            if content:
                return content
            break
        except RateLimitError as e:
            last_err = e
            time.sleep(wait_s)
        except (APIError, APITimeoutError) as e:
            last_err = e
            time.sleep(wait_s)

    print(f"[MIYA_V1_LLM_ERROR] {type(last_err).__name__ if last_err else 'EmptyResponse'}")
    return _llm_fallback_answer(user_text, product_context, similar_products, db_status)


def process_user_message(user_text: str, product_context: dict | None, similar_products: list[dict], db_status: dict):
    st.session_state.messages.append({"role": "user", "content": user_text})

    fast = get_fast_policy_answer(user_text)
    if fast:
        st.session_state.messages.append({"role": "assistant", "content": fast})
        return

    if is_size_question(user_text):
        hard_answer = build_hard_size_answer(product_context)
        if hard_answer:
            st.session_state.messages.append({"role": "assistant", "content": hard_answer})
            return

    if wants_similar_reco(user_text):
        similar_answer = build_similar_reply(similar_products)
        if similar_answer:
            st.session_state.messages.append({"role": "assistant", "content": similar_answer})
            return

    answer = get_llm_answer(user_text, product_context, similar_products, db_status)
    st.session_state.messages.append({"role": "assistant", "content": answer})


product_db, db_error = load_product_db()
resolved_product_no = extract_product_no_from_url(current_url, product_no_from_qp)
db_row = get_product_row_from_db(product_db, resolved_product_no)
similar_products = get_similar_products_from_db(product_db, db_row, topn=3)

db_status = {
    "db_loaded": product_db is not None,
    "db_error": db_error,
    "db_hit": db_row is not None,
}

context_key = build_context_key(current_url, resolved_product_no, product_name_q)
if context_key != st.session_state.last_context_key:
    st.session_state.last_context_key = context_key
    st.session_state.messages = []

product_context = None
if db_row is not None:
    product_context = db_row_to_product_context(db_row)
elif current_url and is_product_page(current_url, resolved_product_no):
    product_context = fetch_product_context_cached(current_url, product_name_q)

body_ctx = build_body_context()
size_result = None
if product_context:
    size_result = recommend_size(
        body_ctx.get("height_cm", ""),
        body_ctx.get("weight_kg", ""),
        body_ctx.get("top_size", ""),
        product_context,
    )

st.markdown(
    """
<style>
header[data-testid="stHeader"] {display:none;}
div[data-testid="stToolbar"] {display:none;}
#MainMenu {visibility:hidden;}
footer {visibility:hidden;}

:root{
  --miya-page-bg:#ffffff;
  --miya-title:#303443;
  --miya-sub:#5f6471;
  --miya-muted:#7a7f8c;
  --miya-divider:#d8dbe2;
  --miya-bot-bg:#071b4e;
  --miya-bot-text:#ffffff;
  --miya-user-bg:#dff0ec;
  --miya-user-text:#1f3b36;
  --miya-label:#303443;
  --miya-input-bg:#f3f5f8;
  --miya-input-text:#303443;
  --miya-chat-bg:#f3f5f8;
  --miya-chat-text:#303443;
  --miya-chat-placeholder:#7a7f8c;
}

@media (prefers-color-scheme: dark){
  :root{
    --miya-page-bg:#0b1220;
    --miya-title:#f3f4f6;
    --miya-sub:#d1d5db;
    --miya-muted:#c0c7d1;
    --miya-divider:rgba(255,255,255,.14);
    --miya-bot-bg:#0b2a78;
    --miya-bot-text:#ffffff;
    --miya-user-bg:#dff0ec;
    --miya-user-text:#173630;
    --miya-label:#f3f4f6;
    --miya-input-bg:#ffffff;
    --miya-input-text:#0f172a;
    --miya-chat-bg:rgba(255,255,255,0.08);
    --miya-chat-text:#ffffff;
    --miya-chat-placeholder:rgba(255,255,255,0.72);
  }
}

.stApp{
  background:var(--miya-page-bg) !important;
}

.block-container{
  max-width:820px;
  padding-top:0.6rem !important;
  padding-bottom:10.4rem !important;
  padding-left:14px !important;
  padding-right:14px !important;
}

div[data-testid="stHorizontalBlock"]{
  display:grid !important;
  grid-template-columns:minmax(0,1fr) minmax(0,1fr) !important;
  gap:12px !important;
  align-items:start !important;
  width:100% !important;
}

div[data-testid="stHorizontalBlock"] > div,
div[data-testid="column"]{
  min-width:0 !important;
  width:100% !important;
}

div[data-testid="stTextInput"],
div[data-testid="stSelectbox"]{
  margin-bottom:-2px !important;
  width:100% !important;
}

div[data-testid="stTextInput"] label,
div[data-testid="stSelectbox"] label{
  color:var(--miya-label) !important;
  font-weight:700 !important;
  font-size:12px !important;
  line-height:1.15 !important;
  margin-bottom:4px !important;
}

div[data-testid="stTextInput"] input{
  border-radius:12px !important;
  min-width:0 !important;
  width:100% !important;
  height:46px !important;
  padding-left:14px !important;
  padding-right:14px !important;
  color:var(--miya-input-text) !important;
  background:var(--miya-input-bg) !important;
}

div[data-testid="stTextInput"] input::placeholder{
  color:#8a90a0 !important;
  opacity:1 !important;
}

div[data-baseweb="select"]{
  min-width:0 !important;
  width:100% !important;
}

div[data-baseweb="select"] > div{
  border-radius:12px !important;
  min-width:0 !important;
  width:100% !important;
  min-height:46px !important;
  padding-right:38px !important;
  color:var(--miya-input-text) !important;
  background:var(--miya-input-bg) !important;
}

div[data-baseweb="select"] svg{
  display:block !important;
  visibility:visible !important;
  opacity:1 !important;
  color:#111827 !important;
  fill:#111827 !important;
  width:18px !important;
  height:18px !important;
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
  bottom:58px !important;
  width:min(760px, calc(100% - 18px)) !important;
  z-index:9999 !important;
}

div[data-testid="stChatInput"] > div{
  background:var(--miya-chat-bg) !important;
  border:1px solid rgba(255,255,255,.10) !important;
}

div[data-testid="stChatInput"] textarea,
div[data-testid="stChatInput"] input{
  color:var(--miya-chat-text) !important;
  -webkit-text-fill-color:var(--miya-chat-text) !important;
}

div[data-testid="stChatInput"] textarea::placeholder,
div[data-testid="stChatInput"] input::placeholder{
  color:var(--miya-chat-placeholder) !important;
  -webkit-text-fill-color:var(--miya-chat-placeholder) !important;
  opacity:1 !important;
}

div[data-testid="stChatInput"] svg{
  color:var(--miya-chat-placeholder) !important;
}

@media (max-width: 768px){
  .block-container{
    max-width:100%;
    padding-top:0.9rem !important;
    padding-bottom:8.2rem !important;
    padding-left:12px !important;
    padding-right:12px !important;
  }

  div[data-testid="stHorizontalBlock"]{
    grid-template-columns:minmax(0,1fr) minmax(0,1fr) !important;
    gap:8px !important;
  }

  div[data-testid="stTextInput"] label,
  div[data-testid="stSelectbox"] label{
    font-size:11px !important;
  }

  div[data-testid="stTextInput"] input{
    height:44px !important;
    padding-left:12px !important;
    padding-right:12px !important;
  }

  div[data-baseweb="select"] > div{
    min-height:44px !important;
    padding-right:34px !important;
  }

  div[data-baseweb="select"] svg{
    width:18px !important;
    height:18px !important;
  }

  div[data-testid="stChatInput"]{
    position:sticky !important;
    left:auto !important;
    transform:none !important;
    bottom:auto !important;
    width:100% !important;
    z-index:5 !important;
    margin-top:10px !important;
  }
}
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style="text-align:center; margin:0 0 8px 0;">
      <div style="font-size:31px; font-weight:800; line-height:1.08; letter-spacing:-0.02em; color:var(--miya-title);">
        미샵 쇼핑친구 <span style="color:#0f8a7a;">미야언니</span>
      </div>
      <div style="margin-top:4px; font-size:13px; line-height:1.3; color:var(--miya-sub);">
        24시간 쇼핑 판단에 도움을 드리는 똑똑한 쇼핑친구
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style="margin-top:0; margin-bottom:2px;">
      <div style="font-size:13px; font-weight:700; line-height:1.2; color:var(--miya-title); margin-bottom:4px;">
        사이즈 입력 <span style="font-size:11px; font-weight:500; color:var(--miya-muted);">(더 구체적인 상담 가능)</span>
      </div>
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
    current_top = st.session_state.body_top if st.session_state.body_top in SIZE_OPTIONS_UI else ""
    st.session_state.body_top = st.selectbox(
        "상의",
        options=SIZE_OPTIONS_UI,
        index=SIZE_OPTIONS_UI.index(current_top),
        key="body_top_input",
    )
with row2[1]:
    current_bottom = st.session_state.body_bottom if st.session_state.body_bottom in SIZE_OPTIONS_UI else ""
    st.session_state.body_bottom = st.selectbox(
        "하의",
        options=SIZE_OPTIONS_UI,
        index=SIZE_OPTIONS_UI.index(current_bottom),
        key="body_bottom_input",
    )

st.markdown(
    '<div style="margin-top:4px; font-size:10px; line-height:1.2; color:var(--miya-muted);">입력 후 바로 상담에 반영돼요.</div></div>',
    unsafe_allow_html=True,
)

body_summary = build_body_context_text(build_body_context())
if any(build_body_context().values()):
    st.markdown(
        f'<div style="margin-top:2px; margin-bottom:2px; font-size:10.5px; color:var(--miya-muted);">현재 입력 정보: {html.escape(body_summary)}</div>',
        unsafe_allow_html=True,
    )

if size_result and size_result.get("recommended"):
    st.markdown(
        f'<div style="margin-top:0; margin-bottom:2px; font-size:10.5px; color:var(--miya-muted);">참고 추천 사이즈: {html.escape(size_result["recommended"])} · {html.escape(size_result["reason"])}</div>',
        unsafe_allow_html=True,
    )
elif size_result and size_result.get("status") == "over_limit":
    st.markdown(
        f'<div style="margin-top:0; margin-bottom:2px; font-size:10.5px; color:#dc2626;">사이즈 주의: {html.escape(size_result["reason"])}</div>',
        unsafe_allow_html=True,
    )

if db_error:
    st.markdown(
        f'<div style="margin-top:0; margin-bottom:2px; font-size:10.5px; color:#dc2626;">DB 로드 주의: {html.escape(db_error)}</div>',
        unsafe_allow_html=True,
    )

if not st.session_state.messages:
    if is_product_page(current_url, resolved_product_no):
        welcome = (
            "안녕하세요? 옷 같이 봐드리는 미야언니예요:)\n"
            "지금 보시는 상품 기준으로 같이 봐드릴게요.\n"
            "사이즈, 코디, 배송, 교환 중 뭐부터 이야기해볼까요?"
        )
    else:
        welcome = (
            "안녕하세요? 옷 같이 봐드리는 미야언니예요:)\n"
            "지금은 일반 상담 모드예요.\n"
            "상품 상세페이지에서 채팅창을 열면\n"
            "그 상품 기준으로 더 정확하게 상담해드릴 수 있어요.\n\n"
            "궁금한 상품이 있으면 이 채팅창을 끄고\n"
            "상품 페이지에서 다시 채팅창을 열어주세요:)"
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
                '<div style="display:block; font-size:12px; font-weight:700; line-height:1.15; color:#0f8a7a; text-align:right; margin:0 6px 1px 0;">고객님</div>'
                f'<div style="padding:10px 14px 10px 10px; border-radius:18px; border-bottom-right-radius:6px; font-size:15px; line-height:1.52; white-space:pre-wrap; word-break:keep-all; background:var(--miya-user-bg); color:var(--miya-user-text); border:1px solid rgba(15,106,99,.14);">{safe_text}</div>'
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
                '<div style="display:block; font-size:12px; font-weight:700; line-height:1.15; color:var(--miya-sub); margin:0 0 1px 6px;">미야언니</div>'
                f'<div style="padding:10px 14px 10px 10px; border-radius:18px; border-bottom-left-radius:6px; font-size:15px; line-height:1.52; white-space:pre-wrap; word-break:keep-all; background:var(--miya-bot-bg); color:var(--miya-bot-text); border:1px solid rgba(255,255,255,.08);">{safe_text}</div>'
                '</div>'
                '</div>'
            ),
            unsafe_allow_html=True,
        )

user_input = st.chat_input("메시지를 입력하세요…")
if user_input:
    process_user_message(user_input, product_context, similar_products, db_status)
    st.rerun()
