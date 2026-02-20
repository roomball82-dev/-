# decision_mate_app_final.py
# Streamlit prototype for "결정 메이트" (Decision Mate)
# - Multi-step chat questions (common + mode)
# - Kakao Local Keyword Search (paged, center/radius/sort=distance) => bigger candidate pool
# - Rule-based kind filter BEFORE LLM (meal/cafe/drink) to prevent cafe when "식사"
# - Distance + walk minutes badge (center station preferred)
# - Transport weighting (car => parking-signal light bonus)
# - Relax search tokens (0~3) + "근처/주변" variants
# - Always returns 3 picks (fallback if LLM fails)

import json
import re
import time
import math
import requests
import streamlit as st
from openai import OpenAI
from math import radians, sin, cos, sqrt, atan2

# -----------------------------
# App config
# -----------------------------
st.set_page_config(page_title="결정 메이트", page_icon="🍽️", layout="wide")
st.title("🍽️ 결정 메이트 (Decision Mate)")
st.caption("식당/카페/술집… ‘장소 픽스’가 필요할 때, 조건 정리 + 3곳만 딱 추천")

# -----------------------------
# Sidebar
# -----------------------------
st.sidebar.header("🔑 API 설정")
openai_key = st.sidebar.text_input("OpenAI API Key", type="password")
kakao_key = st.sidebar.text_input("Kakao Local REST API Key", type="password")

st.sidebar.markdown("---")
debug_mode = st.sidebar.checkbox("🛠️ 디버그 모드(LLM 원문 출력)", value=False)

client = OpenAI(api_key=openai_key) if openai_key else None

# -----------------------------
# Session State
# -----------------------------
if "messages" not in st.session_state:
    st.session_state.messages = [{
        "role": "assistant",
        "content": "오케이 😎\n오늘 어디서 누구랑 뭐 먹을지 내가 딱 정해줄게.\n일단 **어느 동네/역 근처**에서 찾을까?"
    }]

if "conditions" not in st.session_state:
    st.session_state.conditions = {
        "location": None,     # ex) 신촌 / 신촌역
        "food_type": None,    # ex) 양식 / 일식 / 한식 ...
        "purpose": None,
        "people": None,
        "mood": None,
        "constraints": {
            "cannot_eat": [],
            "avoid_recent": [],
            "need_parking": None
        },
        "meta": {
            "context_mode": None,       # 회사 회식 / 친구 / 단체 모임 / 연인 · 썸 · 소개팅 / 혼밥 / 가족 / None
            "people_count": None,       # int
            "budget_tier": "상관없음",  # 가성비 / 보통 / 조금 특별 / 상관없음
            "answers": {},              # mode 질문 답
            "common": {
                "cannot_eat_done": False,   # 못 먹는 것 질문 완료 여부
                "alcohol_level": None,      # 없음 / 가볍게 / 술 중심
                "stay_duration": None,      # 빠르게 / 적당히 / 오래
                "transport": None,          # 차 / 대중교통 / 상관없음
                "alcohol_plan": None,       # (술 중심) 한 곳 / 나눌 수도 / 모르겠음
                "alcohol_type": None,       # (술 중심) 소주/맥주/와인/상관없음
                "search_relax": 0,          # 0~3: 검색 토큰 완화
                "center_name": None,        # 예: "신촌역"
            },
            "fast_mode": False           # "그냥 추천해" 스킵 의도
        }
    }

if "last_picks_ids" not in st.session_state:
    st.session_state.last_picks_ids = []

if "pending_question" not in st.session_state:
    st.session_state.pending_question = None  # {"scope": "...", "key": "...", "text": "...", "type": "..."}

if "debug_raw_patch" not in st.session_state:
    st.session_state.debug_raw_patch = ""

if "debug_raw_rerank" not in st.session_state:
    st.session_state.debug_raw_rerank = ""

if "loc_center_cache" not in st.session_state:
    st.session_state.loc_center_cache = {}  # {"신촌": {"x":..,"y":..,"name":..}}

# -----------------------------
# Helpers
# -----------------------------
def safe_json_load(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None

def extract_first_json_object(text: str):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    return safe_json_load(m.group(0))

def normalize_conditions(cond: dict):
    if not isinstance(cond, dict):
        return

    if "constraints" not in cond or not isinstance(cond["constraints"], dict):
        cond["constraints"] = {"cannot_eat": [], "avoid_recent": [], "need_parking": None}

    c = cond["constraints"]
    if "cannot_eat" not in c or not isinstance(c["cannot_eat"], list):
        c["cannot_eat"] = []
    if "avoid_recent" not in c or not isinstance(c["avoid_recent"], list):
        c["avoid_recent"] = []
    if "need_parking" not in c:
        c["need_parking"] = None

    if "meta" not in cond or not isinstance(cond["meta"], dict):
        cond["meta"] = {}

    m = cond["meta"]
    m.setdefault("context_mode", None)
    m.setdefault("people_count", None)
    m.setdefault("budget_tier", "상관없음")
    m.setdefault("answers", {})
    m.setdefault("fast_mode", False)
    if "common" not in m or not isinstance(m["common"], dict):
        m["common"] = {}

    cm = m["common"]
    cm.setdefault("cannot_eat_done", False)
    cm.setdefault("alcohol_level", None)
    cm.setdefault("stay_duration", None)
    cm.setdefault("transport", None)
    cm.setdefault("alcohol_plan", None)
    cm.setdefault("alcohol_type", None)
    cm.setdefault("search_relax", 0)
    cm.setdefault("center_name", None)

def merge_conditions(base: dict, patch: dict):
    if not isinstance(patch, dict):
        return base

    if "constraints" in patch and isinstance(patch["constraints"], dict):
        base_constraints = base.get("constraints", {}) or {}
        for k, v in patch["constraints"].items():
            if v is None:
                continue
            base_constraints[k] = v
        base["constraints"] = base_constraints

    if "meta" in patch and isinstance(patch["meta"], dict):
        base_meta = base.get("meta", {}) or {}
        for k, v in patch["meta"].items():
            if v is None:
                continue
            base_meta[k] = v
        base["meta"] = base_meta

    for k, v in patch.items():
        if k in ("constraints", "meta"):
            continue
        if v is None:
            continue
        base[k] = v

    normalize_conditions(base)
    return base

def detect_skip_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    keywords = ["그냥 추천", "걍 추천", "빨리 추천", "스킵", "아무거나", "됐고 추천", "바로 추천", "추천해줘"]
    return any(k in t for k in keywords)

def detect_expand_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    keywords = ["넓혀", "넓혀봐", "범위", "조금만 넓혀", "근처로", "주변으로"]
    return any(k in t for k in keywords)

# -----------------------------
# Sidebar: Mode / People / Budget
# -----------------------------
st.sidebar.markdown("---")
st.sidebar.header("🧭 상황 설정")

MODE_OPTIONS = [
    "선택 안 함",
    "회사 회식",
    "친구",
    "단체 모임",
    "연인 · 썸 · 소개팅",
    "혼밥",
    "가족",
]
BUDGET_OPTIONS = ["상관없음", "가성비", "보통", "조금 특별"]

selected_mode = st.sidebar.selectbox("상황 모드", MODE_OPTIONS, index=0)
people_count = st.sidebar.number_input("인원", min_value=1, max_value=30, value=2, step=1)
budget_tier = st.sidebar.radio("예산대(1인)", BUDGET_OPTIONS, index=0)

normalize_conditions(st.session_state.conditions)
meta = st.session_state.conditions["meta"]
meta["context_mode"] = None if selected_mode == "선택 안 함" else selected_mode
meta["people_count"] = int(people_count) if people_count else None
meta["budget_tier"] = budget_tier

# -----------------------------
# Kakao Local API
# -----------------------------
def kakao_keyword_search(query: str, kakao_rest_key: str, size: int = 15, page: int = 1,
                        x: str | None = None, y: str | None = None,
                        radius: int | None = None, sort: str | None = None):
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {kakao_rest_key}"}
    params = {"query": query, "size": size, "page": page}
    # Kakao: x=longitude, y=latitude
    if x and y:
        params["x"] = x
        params["y"] = y
    if radius is not None:
        params["radius"] = radius
    if sort:
        params["sort"] = sort  # "distance" or "accuracy"

    res = requests.get(url, headers=headers, params=params, timeout=10)
    res.raise_for_status()
    return res.json()

def kakao_keyword_search_paged(query: str, kakao_rest_key: str,
                              x: str | None = None, y: str | None = None,
                              radius: int | None = None,
                              sort: str | None = None,
                              size: int = 15, max_pages: int = 3):
    """
    정책/스펙상 size=15가 일반적으로 최대. page를 돌려 최대 45개까지 풀을 확보.
    """
    all_docs = []
    for page in range(1, max_pages + 1):
        data = kakao_keyword_search(query, kakao_rest_key, size=size, page=page, x=x, y=y, radius=radius, sort=sort)
        docs = data.get("documents", [])
        meta = data.get("meta", {}) or {}
        all_docs.extend(docs)
        if meta.get("is_end") is True:
            break

    # Dedup by id
    seen = set()
    uniq = []
    for d in all_docs:
        pid = d.get("id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        uniq.append(d)
    return uniq

# -----------------------------
# Geo / Walk / Transport scoring
# -----------------------------
def haversine_m(x1, y1, x2, y2):
    lon1, lat1, lon2, lat2 = map(radians, [float(x1), float(y1), float(x2), float(y2)])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return 6371000 * c

def estimate_walk_minutes(distance_m: float, speed_m_per_min: float = 80.0) -> int:
    if distance_m is None:
        return 999
    if distance_m >= 10**11:
        return 999
    return max(1, int(math.ceil(distance_m / speed_m_per_min)))

def place_distance_m(place: dict, center: dict):
    if not center or not center.get("x") or not center.get("y"):
        return None
    px, py = place.get("x"), place.get("y")
    if not px or not py:
        return None
    return haversine_m(center["x"], center["y"], px, py)

def get_location_center(location: str, kakao_rest_key: str):
    """
    location이 '동네'면 '동네역'을 우선 시도해 중심좌표 확보.
    (키워드 검색 1개 결과의 좌표를 center로 사용)
    """
    loc = (location or "").strip()
    if not loc:
        return None

    cache = st.session_state.loc_center_cache
    if loc in cache:
        return cache[loc]

    candidates = []
    if "역" in loc:
        candidates.append(loc)
    else:
        candidates.append(f"{loc}역")
        candidates.append(loc)

    for cand in candidates:
        try:
            docs = kakao_keyword_search_paged(cand, kakao_rest_key, size=15, max_pages=1)
            if not docs:
                continue
            d = docs[0]
            center = {"x": d.get("x"), "y": d.get("y"), "name": cand}
            if center["x"] and center["y"]:
                cache[loc] = center
                return center
        except Exception:
            continue
    return None

def filter_places_by_radius(places: list, center: dict, radius_m: int):
    if not center or not center.get("x") or not center.get("y"):
        return places
    out = []
    cx, cy = center["x"], center["y"]
    for p in places:
        px, py = p.get("x"), p.get("y")
        if not px or not py:
            continue
        if haversine_m(cx, cy, px, py) <= radius_m:
            out.append(p)
    return out

def parking_signal_score(place: dict) -> int:
    # v1 heuristic only
    text = f"{place.get('place_name','')} {place.get('category_name','')}".lower()
    score = 0
    if "주차" in text or "parking" in text or "발렛" in text:
        score += 3
    big_like = ["백화점", "몰", "아울렛", "호텔", "리조트", "웨딩", "컨벤션", "대형"]
    if any(k in text for k in big_like):
        score += 1
    alley_like = ["포차", "호프", "이자카야", "바", "주점"]
    if any(k in text for k in alley_like):
        score -= 1
    return score

def sort_places_for_transport(places: list, center: dict, transport: str):
    """
    - 대중교통: 거리 우선
    - 차: 거리 기반이지만, 주차 신호 약가점(가까운 거리대에서만 살짝 유리)
    """
    if not center or not center.get("x") or not center.get("y"):
        return places
    cx, cy = center["x"], center["y"]
    scored = []
    for p in places:
        px, py = p.get("x"), p.get("y")
        if px and py:
            dist = haversine_m(cx, cy, px, py)
        else:
            dist = 10**12
        park = parking_signal_score(p) if transport == "차" else 0
        score = dist - (park * 120)  # 1점당 120m 가점
        scored.append((score, dist, p))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [p for _, __, p in scored]

def attach_distance_meta(places: list, center: dict):
    if center:
        for p in places:
            d = place_distance_m(p, center)
            p["_distance_m"] = d if d is not None else 10**12
            p["_walk_min"] = estimate_walk_minutes(p["_distance_m"])
    else:
        for p in places:
            p["_distance_m"] = 10**12
            p["_walk_min"] = None
    return places

# -----------------------------
# Kind filter BEFORE LLM (prevents cafe when meal)
# -----------------------------
def infer_place_kind_from_conditions(conditions: dict) -> str:
    cm = conditions["meta"]["common"]
    alcohol = cm.get("alcohol_level")
    stay = cm.get("stay_duration")

    # 술이 있으면 drink 우선
    if alcohol in ("가볍게", "술 중심"):
        return "drink"

    # 오래 머무르면 카페 성향
    if stay == "오래":
        return "cafe"

    # 기본은 식사
    return "meal"

def filter_by_kind(places: list, kind: str):
    def cat(p): return (p.get("category_name") or "")

    if kind == "meal":
        banned = ["카페", "디저트", "베이커리", "아이스크림"]
        out = [p for p in places if not any(b in cat(p) for b in banned)]
        return out if len(out) >= 10 else places

    if kind == "cafe":
        allow = ["카페", "디저트", "베이커리", "아이스크림"]
        out = [p for p in places if any(a in cat(p) for a in allow)]
        return out if len(out) >= 10 else places

    if kind == "drink":
        allow = ["술", "주점", "호프", "이자카야", "바", "포차", "펍"]
        out = [p for p in places if any(a in cat(p) for a in allow)]
        return out if len(out) >= 10 else places

    return places

# -----------------------------
# Simple franchise filter (optional)
# -----------------------------
DEFAULT_FRANCHISE = [
    "쉐이크쉑", "스타벅스", "투썸", "이디야", "빽다방", "메가커피", "컴포즈",
    "파리바게뜨", "뚜레쥬르", "버거킹", "맥도날드", "롯데리아", "kfc", "서브웨이"
]

def filter_franchise(places: list, enabled: bool):
    if not enabled:
        return places
    out = []
    for p in places:
        name = (p.get("place_name") or "").lower()
        if any(f.lower() in name for f in DEFAULT_FRANCHISE):
            continue
        out.append(p)
    return out if len(out) >= 10 else places

# -----------------------------
# 1) Latest utterance -> condition PATCH (LLM)
# -----------------------------
def extract_conditions_patch(latest_user_text: str, current_conditions: dict):
    if client is None:
        return {}

    system = """
너는 '결정 메이트'의 조건 업데이트 엔진이다.

[목표]
사용자의 '최신 발화'를 보고,
기존 조건에서 변경/추가된 값만 JSON PATCH 형태로 출력해라.

[중요]
- 반드시 JSON 오브젝트만 출력해라.
- 사용자가 언급하지 않은 필드는 출력하지 마라.
- "null로 초기화" 같은 행동 금지.
- constraints 안의 리스트는 사용자가 새로 언급한 경우에만 업데이트해라.
- 사용자가 "아까 추천 말고 다른 데"라고 하면 diversify=true 를 넣어라.
- 사용자가 "방금 추천한 데 제외" 같은 의미면 exclude_last=true 를 넣어라.

가능한 필드:
- location, food_type, purpose, people, mood
- constraints.cannot_eat (list[str])
- constraints.avoid_recent (list[str])
- constraints.need_parking (true/false)
- diversify (true/false)
- exclude_last (true/false)
- avoid_franchise (true/false)
"""

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"[기존 조건]\n{json.dumps(current_conditions, ensure_ascii=False)}"},
            {"role": "user", "content": f"[최신 발화]\n{latest_user_text}"},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )

    raw = (res.choices[0].message.content or "").strip()
    st.session_state.debug_raw_patch = raw

    patch = safe_json_load(raw) or extract_first_json_object(raw)
    if not isinstance(patch, dict):
        return {}
    return patch

# -----------------------------
# Question tree: common + mode
# -----------------------------
MODE_REQUIRED_QUESTIONS = {
    "회사 회식": [
        {"key": "work_tone", "text": "분위기는 **가벼운 회식**이야, 아니면 **정돈된 자리** 쪽?", "type": "enum"},
    ],
    "친구": [
        {"key": "friend_focus", "text": "오늘은 **수다/대화 중심**이야, 아니면 **먹는 재미 중심**이야? 😆", "type": "enum"},
    ],
    "단체 모임": [
        {"key": "group_purpose", "text": "모임 목적이 뭐야? (**밥+수다 / 스터디+얘기 / 축하/행사**)", "type": "enum"},
    ],
    "연인 · 썸 · 소개팅": [
        {"key": "dating_stage", "text": "첫 만남(어색한 단계)이야, 아니면 좀 익숙한 사이야? (**첫/어색 / 익숙**)", "type": "enum"},
    ],
    "혼밥": [
        {"key": "solo_weight", "text": "오늘은 든든하게 먹을래, 가볍게 먹을래? (**든든 / 가볍게**)", "type": "enum"},
    ],
    "가족": [
        {"key": "family_member", "text": "구성원에 **아이/어른(연세)** 있어? (**아이 / 어른 / 둘 다 / 없음**)", "type": "enum"},
    ],
}

def get_next_mode_question(conditions: dict):
    normalize_conditions(conditions)
    mode = conditions["meta"]["context_mode"]
    if not mode or mode not in MODE_REQUIRED_QUESTIONS:
        return None
    answers = conditions["meta"]["answers"]
    for q in MODE_REQUIRED_QUESTIONS[mode]:
        if answers.get(q["key"]) is None:
            return {"scope": "mode", **q}
    return None

def get_next_common_question(conditions: dict):
    normalize_conditions(conditions)
    cm = conditions["meta"]["common"]

    if not conditions.get("location"):
        return {"scope": "common", "key": "location", "text": "오케이! **어느 동네/역 근처**에서 찾을까? 📍", "type": "free"}

    if not cm.get("cannot_eat_done", False):
        return {"scope": "common", "key": "cannot_eat", "text": "못 먹는 거 있어? (알레르기/극혐 포함) 없으면 **없음**이라고 해줘 🙅", "type": "list_or_none"}

    if conditions["meta"].get("fast_mode"):
        return None

    if cm.get("alcohol_level") is None:
        return {"scope": "common", "key": "alcohol_level", "text": "오늘 술은 어때? **없음 / 가볍게 / 술 중심** 🍻", "type": "enum_alcohol"}

    if cm.get("stay_duration") is None:
        return {"scope": "common", "key": "stay_duration", "text": "얼마나 있을 거야? **빠르게 / 적당히 / 오래** ⏱️", "type": "enum_stay"}

    if cm.get("transport") is None:
        return {"scope": "common", "key": "transport", "text": "이동수단은 뭐야? **차 / 대중교통 / 상관없음** 🧭", "type": "enum_transport"}

    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") is None:
        return {"scope": "common", "key": "alcohol_plan",
                "text": "오케이 술 중심 👍 한 곳에서 쭉 갈 거야, 아니면 **1차·2차 나눌 수도** 있어? (**한 곳 / 나눌 수도 / 모르겠음**)",
                "type": "enum_alcohol_plan"}

    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") in ("한 곳", "나눌 수도") and cm.get("alcohol_type") is None:
        return {"scope": "common", "key": "alcohol_type",
                "text": "주로 뭐 마실 생각이야? **소주 / 맥주 / 와인 / 상관없음** 🍶",
                "type": "enum_alcohol_type"}

    return None

def get_next_question(conditions: dict):
    q = get_next_common_question(conditions)
    if q:
        return q
    return get_next_mode_question(conditions)

# -----------------------------
# Answer parsing & apply (prevents loop)
# -----------------------------
def parse_list_or_none(text: str):
    t = (text or "").strip()
    if not t:
        return None
    if "없" in t:
        return []
    parts = re.split(r"[,\n/]+", t)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        p = re.sub(r"(은|는|이|가|을|를|만|빼고|빼줘)$", "", p).strip()
        if p and p not in out:
            out.append(p)
    return out[:6]

def apply_answer(conditions: dict, pending_q: dict, user_text: str) -> bool:
    normalize_conditions(conditions)
    t = (user_text or "").strip()
    cm = conditions["meta"]["common"]
    answers = conditions["meta"]["answers"]

    # Any time user says no alcohol -> override (avoid asking repeatedly)
    if any(x in t for x in ["술 안", "술안", "안 마셔", "금주", "노알콜", "노 알콜"]):
        cm["alcohol_level"] = "없음"
        cm["alcohol_plan"] = None
        cm["alcohol_type"] = None
        return True

    key = pending_q.get("key")
    qtype = pending_q.get("type")

    if key == "location":
        if len(t) >= 1:
            conditions["location"] = t
            return True
        return False

    if qtype == "list_or_none" and key == "cannot_eat":
        parsed = parse_list_or_none(t)
        if parsed is None:
            return False
        conditions["constraints"]["cannot_eat"] = parsed
        cm["cannot_eat_done"] = True
        return True

    if qtype == "enum_alcohol" and key == "alcohol_level":
        if "없" in t:
            cm["alcohol_level"] = "없음"
            return True
        if "가볍" in t or "한두" in t:
            cm["alcohol_level"] = "가볍게"
            return True
        if "술" in t or "제대로" in t or "중심" in t:
            cm["alcohol_level"] = "술 중심"
            return True
        return False

    if qtype == "enum_stay" and key == "stay_duration":
        if "빠" in t or "후딱" in t or "간단" in t:
            cm["stay_duration"] = "빠르게"
            return True
        if "오래" in t or "길게" in t:
            cm["stay_duration"] = "오래"
            return True
        if "적당" in t or "보통" in t:
            cm["stay_duration"] = "적당히"
            return True
        return False

    if qtype == "enum_transport" and key == "transport":
        if "차" in t or "운전" in t:
            cm["transport"] = "차"
            return True
        if "대중" in t or "지하철" in t or "버스" in t:
            cm["transport"] = "대중교통"
            return True
        if "상관" in t or "아무" in t:
            cm["transport"] = "상관없음"
            return True
        return False

    if qtype == "enum_alcohol_plan" and key == "alcohol_plan":
        if "한" in t and "곳" in t:
            cm["alcohol_plan"] = "한 곳"
            return True
        if "나눌" in t or ("1" in t and "2" in t):
            cm["alcohol_plan"] = "나눌 수도"
            return True
        if "모르" in t or "아직" in t:
            cm["alcohol_plan"] = "모르겠음"
            return True
        return False

    if qtype == "enum_alcohol_type" and key == "alcohol_type":
        if "소주" in t:
            cm["alcohol_type"] = "소주"
            return True
        if "맥주" in t or "비어" in t:
            cm["alcohol_type"] = "맥주"
            return True
        if "와인" in t:
            cm["alcohol_type"] = "와인"
            return True
        if "상관" in t or "아무" in t:
            cm["alcohol_type"] = "상관없음"
            return True
        return False

    if pending_q.get("scope") == "mode":
        k = key
        maps = {
            "work_tone": {"가볍": "가벼운 회식", "캐주얼": "가벼운 회식", "정돈": "정돈된 자리", "격식": "정돈된 자리"},
            "friend_focus": {"대화": "대화", "수다": "대화", "먹": "먹는 재미"},
            "group_purpose": {"스터디": "스터디+얘기", "공부": "스터디+얘기", "축하": "축하/행사", "행사": "축하/행사", "밥": "밥+수다", "수다": "밥+수다"},
            "dating_stage": {"첫": "첫/어색", "어색": "첫/어색", "익숙": "익숙", "편": "익숙"},
            "solo_weight": {"든든": "든든", "가볍": "가볍게"},
            "family_member": {"둘": "둘 다", "아이": "아이", "어른": "어른", "부모": "어른", "없": "없음"},
        }
        picked = None
        for kw, val in maps.get(k, {}).items():
            if kw in t:
                picked = val
                break
        if picked is None:
            return False
        answers[k] = picked
        return True

    return False

# -----------------------------
# Query build (relax 0~3)
# -----------------------------
def build_query(conditions):
    normalize_conditions(conditions)
    tokens = []

    mode = conditions["meta"].get("context_mode")
    budget = conditions["meta"].get("budget_tier")
    cm = conditions["meta"]["common"]

    alcohol = cm.get("alcohol_level")
    stay = cm.get("stay_duration")
    alcohol_type = cm.get("alcohol_type")
    relax = int(cm.get("search_relax", 0) or 0)

    loc = conditions.get("location")
    if loc:
        tokens.append(loc)

    # If user explicitly says food_type (양식/일식/중식/한식 등), push early
    if conditions.get("food_type"):
        tokens.append(conditions["food_type"])

    # Place type token
    if alcohol in ("가볍게", "술 중심"):
        if alcohol_type == "와인":
            place_token = "와인바"
        elif alcohol_type == "맥주":
            place_token = "펍"
        elif alcohol_type == "소주":
            place_token = "술집"
        else:
            place_token = "술집"
    else:
        if stay == "오래":
            place_token = "카페"
        elif stay == "빠르게":
            place_token = "식사"
        else:
            place_token = "맛집"

    if relax == 0:
        tokens.append(place_token)
        if mode == "회사 회식":
            tokens.append("회식")
        elif mode == "가족":
            tokens.append("가족식사")
        elif mode == "연인 · 썸 · 소개팅":
            tokens.append("데이트")
        elif mode == "단체 모임":
            tokens.append("단체")
        if budget == "가성비":
            tokens.append("가성비")

    elif relax == 1:
        tokens.append(place_token)

    elif relax == 2:
        if place_token in ("와인바", "펍"):
            tokens.append("술집")
        else:
            tokens.append(place_token)

    else:  # relax >= 3
        if alcohol in ("가볍게", "술 중심"):
            tokens.append("술집")
        else:
            tokens.append("맛집")

    return " ".join([t for t in tokens if t]).strip()

def make_query_variants(base_query: str, location: str, relax_level: int):
    qs = []
    if relax_level >= 1 and location:
        stripped = base_query.replace(location, "").strip()
        qs.append(f"{location} 근처 {stripped}".strip())
        qs.append(f"{location} 주변 {stripped}".strip())
    qs.append(base_query)

    out, seen = [], set()
    for q in qs:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out

# -----------------------------
# Candidate filtering
# -----------------------------
def filter_places(places, exclude_ids):
    if not exclude_ids:
        return places
    s = set(exclude_ids)
    return [p for p in places if p.get("id") not in s]

# -----------------------------
# rerank + formatting
# -----------------------------
def rerank_and_format(conditions, places):
    if client is None:
        return []

    normalize_conditions(conditions)
    cm = conditions["meta"]["common"]
    split_12 = (cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") == "나눌 수도")

    compact = []
    for p in places[:25]:
        compact.append({
            "id": p.get("id"),
            "name": p.get("place_name"),
            "category": p.get("category_name"),
            "address": p.get("road_address_name") or p.get("address_name"),
            "url": p.get("place_url"),
            "walk_min": p.get("_walk_min"),
            "distance_m": p.get("_distance_m"),
        })

    schema_hint = """
반드시 아래 JSON 형식으로만 출력해라:
{
  "picks": [
    {
      "id": "...",
      "scene_feel": "여기서 약속하면 어떤 느낌인지 2~3문장(체감 중심, 과장 금지)",
      "one_line": "짧은 한줄 소개 (친구톤)",
      "hashtags": ["#...","#..."],
      "matched_conditions": ["사용자 조건 중 실제로 반영한 것"],
      "reason": "왜 추천인지 2~3문장(후보 데이터 기반, 없는 정보 상상 금지)"
      %s
    }
  ]
}
""" % (',\n      "phase": "1차"  // split 모드일 때만. "1차" 또는 "2차"' if split_12 else "")

    extra_rules = ""
    if split_12:
        extra_rules = """
추가 규칙 (중요):
- 지금은 '1차·2차'를 나눠서 추천해야 한다.
- picks는 총 3개 유지.
- phase를 반드시 포함하고,
  - 1차 2개
  - 2차 1개
  구성으로 출력해라.
"""

    prompt = f"""
너는 '결정 메이트'다.
사용자 조건에 맞춰 아래 후보 중 BEST 3곳만 골라라.

{schema_hint}

중요 규칙:
- matched_conditions는 '사용자가 말한 조건/필터/질문 답변'에서만 뽑아라.
- hashtags는 사용자 조건 기반으로 먼저 만들고, 부족하면 category로 보충.
- 해시태그는 4~6개
- 과장 금지 ('무조건', '최고', '완벽' 금지)
- 후보 데이터 기반으로만 말하기 (없는 정보 상상 금지)
- picks는 반드시 3개만
- scene_feel은 "자리 배치/조명/동선" 같은 디테일 묘사 금지. 체감만.
- 가능하면(특히 대중교통일 때) walk_min이 큰 후보는 피하되, 조건 적합성이 더 중요하면 예외 가능.

{extra_rules}

[사용자 조건]
{json.dumps(conditions, ensure_ascii=False, indent=2)}

[후보 목록]
{json.dumps(compact, ensure_ascii=False, indent=2)}
"""

    def call_llm(extra_msg=None, temp=0.35):
        msgs = [{"role": "user", "content": prompt}]
        if extra_msg:
            msgs.append({"role": "user", "content": extra_msg})
        return client.chat.completions.create(
            model="gpt-4o-mini",
            messages=msgs,
            temperature=temp,
            response_format={"type": "json_object"},
        )

    res = call_llm(temp=0.35)
    raw = (res.choices[0].message.content or "").strip()
    st.session_state.debug_raw_rerank = raw

    data = safe_json_load(raw) or extract_first_json_object(raw)
    if data is None or "picks" not in data:
        res2 = call_llm(extra_msg="방금 출력이 스키마를 안 지켰어. JSON만 다시 출력해.", temp=0.1)
        raw2 = (res2.choices[0].message.content or "").strip()
        st.session_state.debug_raw_rerank = raw2
        data = safe_json_load(raw2) or extract_first_json_object(raw2)

    if not isinstance(data, dict):
        return []
    picks = data.get("picks", [])
    if not isinstance(picks, list):
        return []
    return picks[:3]

# -----------------------------
# Pre recommend text
# -----------------------------
def generate_pre_recommend_text(conditions, query):
    if client is None:
        return f"오케이ㅋㅋ **{query}**로 바로 3곳 뽑아볼게 🔍"
    prompt = f"""
너는 식당 잘 아는 친구다.
추천을 시작하기 직전에 하는 멘트를 1~2문장으로 만들어라.
조건을 반영해서 말해라.

조건:
{json.dumps(conditions, ensure_ascii=False)}

검색 키워드:
{query}

톤:
- 편하게
- 리액션 포함
- 이모지 1개 정도
"""
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.85
    )
    return (res.choices[0].message.content or "").strip()

# -----------------------------
# Chat UI render history
# -----------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("예: 홍대 근처에서 3명이 가볍게 술 마실 곳")

# -----------------------------
# Main interaction
# -----------------------------
if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):

        if not openai_key or not kakao_key:
            st.warning("사이드바에 OpenAI 키랑 Kakao 키부터 넣어줘!")
            st.stop()

        normalize_conditions(st.session_state.conditions)
        conditions = st.session_state.conditions
        cm = conditions["meta"]["common"]

        # Skip intent
        if detect_skip_intent(user_input):
            conditions["meta"]["fast_mode"] = True

        # Expand intent => relax up
        if detect_expand_intent(user_input):
            cm["search_relax"] = min(3, int(cm.get("search_relax", 0)) + 1)

        # 1) Apply pending question answer first
        if st.session_state.pending_question is not None:
            ok = apply_answer(conditions, st.session_state.pending_question, user_input)
            if ok:
                st.session_state.pending_question = None

        # 2) Extract patch and merge (diversify/exclude_last/franchise)
        patch = extract_conditions_patch(user_input, conditions)
        diversify = bool(patch.pop("diversify", False))
        exclude_last = bool(patch.pop("exclude_last", False))
        avoid_franchise = bool(patch.pop("avoid_franchise", False))
        conditions = merge_conditions(conditions, patch)
        st.session_state.conditions = conditions
        cm = conditions["meta"]["common"]

        # Debug: current conditions
        with st.expander("🧾 현재 누적 조건(JSON)"):
            st.json(conditions)
            if debug_mode:
                st.markdown("**(디버그) patch 원문**")
                st.code(st.session_state.debug_raw_patch)

        # 3) Ask next question if needed
        next_q = get_next_question(conditions)

        # In fast_mode, still ask location/cannot_eat, but skip others
        if next_q and not (conditions["meta"].get("fast_mode") and next_q.get("key") not in ("location", "cannot_eat")):
            st.markdown(next_q["text"])
            st.session_state.messages.append({"role": "assistant", "content": next_q["text"]})
            st.session_state.pending_question = next_q
            st.stop()

        # 4) If location missing, force ask
        if not conditions.get("location"):
            msg = "좋아! 근데 **동네/역**부터 알려줘야 내가 뽑아주지 😎\n예: `합정`, `연남동`, `강남역`"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.session_state.pending_question = {"scope": "common", "key": "location", "text": msg, "type": "free"}
            st.stop()

        # -----------------------------
        # 5) Kakao search: bigger pool + center/radius/distance
        # -----------------------------
        transport = cm.get("transport")  # 차 / 대중교통 / 상관없음
        location = conditions.get("location")

        # center (station preferred)
        center = get_location_center(location, kakao_key)
        cm["center_name"] = center.get("name") if center else None

        # radius steps for pooling
        if transport == "차":
            pool_radius_steps = [1600, 2500, 4000]
        else:
            pool_radius_steps = [1200, 1800, 2500]

        def run_kakao_pooled(query_str: str):
            # if no center, just page through (still up to 45)
            if not center:
                return kakao_keyword_search_paged(query_str, kakao_key, size=15, max_pages=3)

            final_docs = []
            for r in pool_radius_steps:
                docs = kakao_keyword_search_paged(
                    query_str, kakao_key,
                    x=center["x"], y=center["y"],
                    radius=r,
                    sort="distance",
                    size=15, max_pages=3
                )
                final_docs = docs
                if len(docs) >= 25:  # enough pool for rerank
                    break
            return final_docs

        places = []
        used_query = None

        # Try relax 0~3, with variants
        for _ in range(4):
            base_query = build_query(conditions)
            variants = make_query_variants(base_query, location, int(cm.get("search_relax", 0)))

            for q in variants:
                try:
                    docs = run_kakao_pooled(q)
                except Exception as e:
                    st.error(f"Kakao 검색 중 오류: {e}")
                    st.stop()

                if docs:
                    places = docs
                    used_query = q
                    break

            if places:
                break

            if int(cm.get("search_relax", 0)) < 3:
                cm["search_relax"] = int(cm.get("search_relax", 0)) + 1
            else:
                break

        if not places:
            msg = "헉… 이 조건으로는 딱 맞는 데가 잘 안 잡히네 🥲\n조건을 조금 느슨하게 해서 근처 위주로 다시 뽑아볼까?"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        # Pre-text
        pre_text = generate_pre_recommend_text(conditions, used_query or build_query(conditions))
        st.markdown(pre_text)
        if debug_mode and used_query:
            st.caption(f"🔎 사용된 검색어: {used_query} (relax={cm.get('search_relax', 0)})")
            if center:
                st.caption(f"📌 중심좌표: {cm.get('center_name')}")

        # -----------------------------
        # 6) Sort + exclude last + radius focus + attach meta
        # -----------------------------
        if center:
            places = sort_places_for_transport(places, center, transport)

        if diversify or exclude_last:
            places = filter_places(places, st.session_state.last_picks_ids)

        # radius focus (prefer within steps but don't kill pool)
        focus_steps = [1200, 1800, 2500] if transport != "차" else [1600, 2500, 4000]
        focused = []
        if center:
            for r in focus_steps:
                within = filter_places_by_radius(places, center, r)
                if len(within) >= 12:  # keep a stronger pool than 6
                    focused = within
                    break
            if not focused:
                focused = places
        else:
            focused = places

        # Attach distance meta for UI + LLM
        focused = attach_distance_meta(focused, center)

        # -----------------------------
        # 7) Rule-based kind filter BEFORE LLM (meal/cafe/drink)
        # -----------------------------
        kind = infer_place_kind_from_conditions(conditions)
        filtered_kind = filter_by_kind(focused, kind)

        # Optional: franchise exclusion (only if user asked)
        filtered_kind = filter_franchise(filtered_kind, avoid_franchise)

        # Final candidate pool for rerank
        candidates = filtered_kind[:25]  # give LLM more room than 15

        if debug_mode:
            with st.expander("🧪 (디버그) 후보 풀 샘플"):
                sample = [{
                    "name": p.get("place_name"),
                    "cat": p.get("category_name"),
                    "walk_min": p.get("_walk_min"),
                    "dist_m": p.get("_distance_m")
                } for p in candidates[:12]]
                st.json({
                    "kind": kind,
                    "pool_total": len(places),
                    "focused_total": len(focused),
                    "after_kind_filter": len(filtered_kind),
                    "after_franchise_filter": len(candidates),
                    "sample": sample
                })

        # -----------------------------
        # 8) Rerank
        # -----------------------------
        picks = rerank_and_format(conditions, candidates)

        if debug_mode:
            with st.expander("🤖 (디버그) rerank LLM 원문"):
                st.code(st.session_state.debug_raw_rerank)

        # Fallback if LLM fails: choose closest 3 from candidates
        if not picks:
            fallback = []
            for p in candidates[:3]:
                fallback.append({
                    "id": p.get("id"),
                    "scene_feel": "조건을 기준으로 근처 위주로 정리했어. 링크 눌러서 분위기만 빠르게 확인하면 딱이야.",
                    "one_line": "근처에서 무난하게 가기 좋은 선택지!",
                    "hashtags": ["#근처", "#무난", "#바로가기", "#추천"],
                    "matched_conditions": ["근처 우선", "도보/거리 기준"],
                    "reason": "정리 과정이 꼬여서, 우선 가까운 곳부터 추렸어. 메뉴/분위기 확인하고 골라줘 😎"
                })
            picks = fallback

        # -----------------------------
        # 9) Render cards
        # -----------------------------
        kakao_map = {p.get("id"): p for p in candidates}

        st.markdown("---")
        st.subheader("🍽️ 딱 3곳만 골랐어")

        cols = st.columns(3)
        current_pick_ids = []
        center_name = cm.get("center_name") or "기준점"

        for i, pick in enumerate(picks[:3]):
            if not isinstance(pick, dict) or "id" not in pick:
                continue

            place = kakao_map.get(pick["id"])
            if not place:
                continue

            current_pick_ids.append(pick["id"])

            with cols[i]:
                name = place.get("place_name")
                addr = place.get("road_address_name") or place.get("address_name")
                url = place.get("place_url")
                category = place.get("category_name")

                phase = pick.get("phase")
                if phase:
                    st.markdown(f"**[{phase}]**")

                st.markdown(f"### {i+1}. {name}")
                st.caption(category or "")
                st.write(f"📍 {addr}")

                walk_min = place.get("_walk_min")
                if isinstance(walk_min, int) and walk_min < 180:
                    st.caption(f"🚶 {center_name} 기준 도보 약 {walk_min}분")

                scene = (pick.get("scene_feel") or "").strip()
                if scene:
                    st.markdown("🧠 **이런 자리 느낌**")
                    st.write(scene)

                st.markdown(f"**{pick.get('one_line','')}**")

                matched = pick.get("matched_conditions", [])
                if matched:
                    st.markdown("**반영한 조건**")
                    st.markdown(" · ".join([f"`{m}`" for m in matched]))

                tags = pick.get("hashtags", [])
                if tags:
                    st.markdown(" ".join(tags))

                st.markdown("**왜 여기냐면…**")
                st.write(pick.get("reason", ""))

                if url:
                    st.link_button("카카오맵에서 보기", url)

        st.session_state.last_picks_ids = current_pick_ids

        # Prototype log
        try:
            with open("decision_mate_logs.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": int(time.time()),
                    "query_used": used_query,
                    "kind": kind,
                    "avoid_franchise": avoid_franchise,
                    "center": cm.get("center_name"),
                    "conditions": conditions,
                    "picks": picks,
                    "place_ids": current_pick_ids,
                    "pool_counts": {
                        "raw_places": len(places),
                        "focused": len(focused),
                        "after_kind": len(filtered_kind),
                        "candidates": len(candidates),
                    }
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

        final = "끝! 😎\n셋 중에 하나 고르거나, '더 조용한 데', '주차 되는 데', '완전 다른 스타일', '프차 빼줘' 이런 식으로 다시 시켜도 돼."
        st.session_state.messages.append({"role": "assistant", "content": final})
        st.markdown(final)
