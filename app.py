# decision_mate_app_final_v3.py
# Streamlit prototype for "결정 메이트" (Decision Mate)
# v3 changes:
# - 자연어 답변 허용(정확한 선택지 강제 X) 강화: apply_answer() 통짜 교체
# - 공통 질문에 "대화 vs 음식 중심" (focus_priority) 추가
# - build_query()에 focus_priority를 약하게 반영(후보 풀 말라죽지 않게)
# - 기존: 후보 풀 확장(page+radius+center), ensure 3 picks, 필터+LLM 하이브리드 유지

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
st.caption("맛집 추천이 아니라, 약속 장소 ‘결정 피로’를 줄이는 대화형 AI")

# -----------------------------
# Sidebar
# -----------------------------
st.sidebar.header("🔑 API 설정")
openai_key = st.sidebar.text_input("OpenAI API Key", type="password")
kakao_key = st.sidebar.text_input("Kakao Local REST API Key", type="password")

st.sidebar.markdown("---")
debug_mode = st.sidebar.checkbox("🛠️ 디버그 모드(LLM 원문/후보풀 출력)", value=False)

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
        "location": None,
        "food_type": None,
        "purpose": None,
        "people": None,
        "mood": None,
        "constraints": {
            "cannot_eat": [],
            "avoid_recent": [],
            "need_parking": None
        },
        "meta": {
            "context_mode": None,       # 회사 회식 / 친구 / 단체 모임 / 연인·소개팅 / 혼밥 / 가족 / None
            "people_count": None,       # int
            "budget_tier": "상관없음",  # 가성비 / 보통 / 조금 특별 / 상관없음
            "answers": {},
            "common": {
                "cannot_eat_done": False,
                "alcohol_level": None,        # 없음 / 가볍게 / 술 중심
                "transport": None,            # 차 / 대중교통 / 상관없음
                "sensitivity_level": None,    # 1~4
                "focus_priority": None,       # 대화 중심 / 음식 중심 / 균형
                "alcohol_plan": None,         # (술 중심) 한 곳 / 1차·2차 나눌 수도 / 모르겠음
                "alcohol_type": None,         # (술 중심) 소주/맥주/와인/상관없음
                "search_relax": 0,            # 0~3
                "center_name": None,
            },
            "fast_mode": False
        }
    }

if "last_picks_ids" not in st.session_state:
    st.session_state.last_picks_ids = []

if "pending_question" not in st.session_state:
    st.session_state.pending_question = None

if "debug_raw_patch" not in st.session_state:
    st.session_state.debug_raw_patch = ""

if "debug_raw_rerank" not in st.session_state:
    st.session_state.debug_raw_rerank = ""

if "loc_center_cache" not in st.session_state:
    st.session_state.loc_center_cache = {}

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

    cond.setdefault("constraints", {})
    c = cond["constraints"]
    c.setdefault("cannot_eat", [])
    c.setdefault("avoid_recent", [])
    c.setdefault("need_parking", None)
    if not isinstance(c["cannot_eat"], list):
        c["cannot_eat"] = []
    if not isinstance(c["avoid_recent"], list):
        c["avoid_recent"] = []

    cond.setdefault("meta", {})
    m = cond["meta"]
    m.setdefault("context_mode", None)
    m.setdefault("people_count", None)
    m.setdefault("budget_tier", "상관없음")
    m.setdefault("answers", {})
    m.setdefault("fast_mode", False)

    m.setdefault("common", {})
    cm = m["common"]
    cm.setdefault("cannot_eat_done", False)
    cm.setdefault("alcohol_level", None)
    cm.setdefault("transport", None)
    cm.setdefault("sensitivity_level", None)
    cm.setdefault("focus_priority", None)
    cm.setdefault("alcohol_plan", None)
    cm.setdefault("alcohol_type", None)
    cm.setdefault("search_relax", 0)
    cm.setdefault("center_name", None)

def merge_conditions(base: dict, patch: dict):
    if not isinstance(patch, dict):
        return base
    if "constraints" in patch and isinstance(patch["constraints"], dict):
        for k, v in patch["constraints"].items():
            if v is None:
                continue
            base["constraints"][k] = v
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
    keywords = ["넓혀", "넓혀봐", "범위", "조금만 넓혀", "근처로", "주변으로", "더 멀어도"]
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
    if x and y:
        params["x"] = x
        params["y"] = y
    if radius is not None:
        params["radius"] = radius
    if sort:
        params["sort"] = sort
    res = requests.get(url, headers=headers, params=params, timeout=10)
    res.raise_for_status()
    return res.json()

def kakao_keyword_search_paged(query: str, kakao_rest_key: str,
                              x: str | None = None, y: str | None = None,
                              radius: int | None = None, sort: str | None = None,
                              size: int = 15, max_pages: int = 3):
    all_docs = []
    for page in range(1, max_pages + 1):
        data = kakao_keyword_search(query, kakao_rest_key, size=size, page=page, x=x, y=y, radius=radius, sort=sort)
        docs = data.get("documents", [])
        meta = data.get("meta", {}) or {}
        all_docs.extend(docs)
        if meta.get("is_end") is True:
            break
    seen, uniq = set(), []
    for d in all_docs:
        pid = d.get("id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        uniq.append(d)
    return uniq

# -----------------------------
# Geo / Walk
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

def get_location_center(location: str, kakao_rest_key: str):
    loc = (location or "").strip()
    if not loc:
        return None
    cache = st.session_state.loc_center_cache
    if loc in cache:
        return cache[loc]
    candidates = [loc] if "역" in loc else [f"{loc}역", loc]
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
    text = f"{place.get('place_name','')} {place.get('category_name','')}".lower()
    score = 0
    if "주차" in text or "parking" in text or "발렛" in text:
        score += 3
    big_like = ["백화점", "몰", "아울렛", "호텔", "컨벤션", "대형"]
    if any(k in text for k in big_like):
        score += 1
    return score

def sort_places_for_transport(places: list, center: dict, transport: str):
    if not center or not center.get("x") or not center.get("y"):
        return places
    cx, cy = center["x"], center["y"]
    scored = []
    for p in places:
        px, py = p.get("x"), p.get("y")
        dist = haversine_m(cx, cy, px, py) if (px and py) else 10**12
        park = parking_signal_score(p) if transport == "차" else 0
        score = dist - (park * 120)
        scored.append((score, dist, p))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [p for _, __, p in scored]

# -----------------------------
# Place kind inference (meal/cafe/drink)
# -----------------------------
def infer_place_kind(conditions: dict) -> str:
    normalize_conditions(conditions)
    cm = conditions["meta"]["common"]
    alcohol = cm.get("alcohol_level")

    ft = (conditions.get("food_type") or "")
    mood = (conditions.get("mood") or "")
    purpose = (conditions.get("purpose") or "")
    text = f"{ft} {mood} {purpose}"
    if any(k in text for k in ["카페", "커피", "디저트", "베이커리"]):
        return "cafe"

    if alcohol in ("가볍게", "술 중심"):
        return "drink"
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
# Mild "too heavy" filter
# -----------------------------
def mild_context_filter(places: list, conditions: dict):
    normalize_conditions(conditions)
    mode = conditions["meta"].get("context_mode")
    s = conditions["meta"]["common"].get("sensitivity_level")
    if mode != "연인 · 썸 · 소개팅":
        return places
    if not isinstance(s, int) or s < 3:
        return places

    banned = ["오마카세", "파인다이닝", "코스요리", "테이스팅", "셰프", "한우오마카세"]
    out = []
    for p in places:
        name = (p.get("place_name") or "")
        if any(b in name for b in banned):
            continue
        out.append(p)
    return out if len(out) >= 10 else places

# -----------------------------
# Ensure 3 picks
# -----------------------------
def ensure_3_picks(picks: list, candidates: list):
    if not isinstance(picks, list):
        picks = []
    cand_ids = [p.get("id") for p in candidates if p.get("id")]
    cand_set = set(cand_ids)

    fixed, used = [], set()
    for pk in picks:
        if not isinstance(pk, dict):
            continue
        pid = pk.get("id")
        if not pid or pid not in cand_set or pid in used:
            continue
        used.add(pid)
        fixed.append(pk)

    for p in candidates:
        pid = p.get("id")
        if not pid or pid in used:
            continue
        used.add(pid)
        fixed.append({
            "id": pid,
            "scene_feel": "후보 풀 상위에서 무난하게 맞는 곳도 같이 챙겨뒀어. 링크 눌러서 분위기만 빠르게 확인하면 돼.",
            "one_line": "근처에서 안정적으로 가기 좋은 선택지!",
            "hashtags": ["#근처", "#무난", "#바로가기", "#후보추가"],
            "matched_conditions": ["근처 우선"],
            "reason": "추천 결과에 누락이 생겨서, 후보 풀 상위에서 대신 채웠어 😎"
        })
        if len(fixed) >= 3:
            break
    return fixed[:3]

# -----------------------------
# LLM Patch extraction
# -----------------------------
def extract_conditions_patch(latest_user_text: str, current_conditions: dict):
    if client is None:
        return {}
    system = """
너는 '결정 메이트'의 조건 업데이트 엔진이다.

[목표]
사용자의 '최신 발화'를 보고, 기존 조건에서 변경/추가된 값만 JSON PATCH로 출력한다.

[중요]
- 반드시 JSON 오브젝트만 출력.
- 언급하지 않은 필드는 출력하지 말 것.
- null로 초기화 금지.
- constraints 리스트는 사용자가 새로 언급한 경우에만 업데이트.
- "아까 말고 다른 데" => diversify=true
- "방금 추천 제외" => exclude_last=true
- "프차 빼줘" => avoid_franchise=true

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
    return patch if isinstance(patch, dict) else {}

# -----------------------------
# Question flow
# -----------------------------
MODE_REQUIRED_QUESTIONS = {
    "친구": [
        {"key": "friend_style", "text": "친구랑이면 오늘 느낌이 뭐야? **수다 중심 / 먹는 재미 중심** 😆", "type": "enum"},
    ],
    "회사 회식": [
        {"key": "work_vibe", "text": "회식 분위기: **가볍게 / 정돈된 자리** 중 뭐에 가까워?", "type": "enum"},
    ],
    "연인 · 썸 · 소개팅": [
        {"key": "dating_stage", "text": "지금 단계는? **첫/어색 / 익숙**", "type": "enum"},
    ],
    "가족": [
        {"key": "family_member", "text": "가족 구성에 **아이/어른(연세)** 있어? **아이 / 어른 / 둘 다 / 없음**", "type": "enum"},
    ],
}

SENSI_TEXT = "이 자리는 얼마나 신경 써야 하는 자리야?\n**1) 아무 생각 없이 / 2) 편하지만 너무 막은 아닌 / 3) 좀 신경 써야 하는 / 4) 중요한 자리**"
FOCUS_TEXT = "오늘은 **대화가 더 중요해? 음식이 더 중요해?** 😌\n**대화 중심 / 음식 중심 / 둘 다 비슷(균형)**"

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
        return {"scope": "common", "key": "cannot_eat", "text": "못 먹는 거 있어? (알레르기/극혐 포함) 없으면 **없음** 🙅", "type": "list_or_none"}

    if conditions["meta"].get("fast_mode"):
        return None

    if cm.get("alcohol_level") is None:
        return {"scope": "common", "key": "alcohol_level", "text": "오늘 술은 어때? **없음 / 가볍게 / 술 중심** 🍻", "type": "enum_alcohol"}

    if cm.get("transport") is None:
        return {"scope": "common", "key": "transport", "text": "이동수단은? **대중교통 / 차 / 상관없음** 🧭", "type": "enum_transport"}

    if cm.get("sensitivity_level") is None:
        return {"scope": "common", "key": "sensitivity_level", "text": SENSI_TEXT, "type": "enum_sensitivity"}

    if cm.get("focus_priority") is None:
        return {"scope": "common", "key": "focus_priority", "text": FOCUS_TEXT, "type": "enum_focus"}

    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") is None:
        return {"scope": "common", "key": "alcohol_plan",
                "text": "술 중심이면 흐름은? **한 곳 / 1차·2차 나눌 수도 / 모르겠음**", "type": "enum_alcohol_plan"}

    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") in ("한 곳", "1차·2차 나눌 수도") and cm.get("alcohol_type") is None:
        return {"scope": "common", "key": "alcohol_type",
                "text": "주로 뭐 마실 생각이야? **소주 / 맥주 / 와인 / 상관없음** 🍶", "type": "enum_alcohol_type"}

    return None

def get_next_question(conditions: dict):
    q = get_next_common_question(conditions)
    if q:
        return q
    return get_next_mode_question(conditions)

# -----------------------------
# ✅ apply_answer() : 자연어 대응 통짜 교체 + focus_priority 포함
# -----------------------------
def apply_answer(conditions: dict, pending_q: dict, user_text: str) -> bool:
    normalize_conditions(conditions)

    t = (user_text or "").strip()
    if not t:
        return False

    t_low = t.lower()

    cm = conditions["meta"]["common"]
    answers = conditions["meta"]["answers"]

    key = pending_q.get("key")
    qtype = pending_q.get("type")

    # -----------------------------
    # LOCATION
    # -----------------------------
    if key == "location":
        conditions["location"] = t
        return True

    # -----------------------------
    # CANNOT EAT (알레르기/못먹는거)
    # -----------------------------
    if qtype == "list_or_none" and key == "cannot_eat":
        if any(k in t for k in ["없", "상관없", "다 먹", "아무거나"]):
            conditions["constraints"]["cannot_eat"] = []
        else:
            parts = re.split(r"[,\n/]+", t)
            cleaned = []
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                p = re.sub(r"(은|는|이|가|을|를|만|빼고|빼줘)$", "", p).strip()
                if p and p not in cleaned:
                    cleaned.append(p)
            conditions["constraints"]["cannot_eat"] = cleaned[:6]
        cm["cannot_eat_done"] = True
        return True

    # -----------------------------
    # ALCOHOL LEVEL
    # -----------------------------
    if qtype == "enum_alcohol" and key == "alcohol_level":
        if any(k in t_low for k in ["안 마", "술 안", "금주", "노알콜", "노 알콜", "못 마", "안먹", "안 먹"]):
            cm["alcohol_level"] = "없음"
            cm["alcohol_plan"] = None
            cm["alcohol_type"] = None
            return True

        if any(k in t_low for k in ["가볍", "한두잔", "한두 잔", "적당히", "조금", "살짝", "1~2잔", "1-2잔"]):
            cm["alcohol_level"] = "가볍게"
            cm["alcohol_plan"] = None
            cm["alcohol_type"] = None
            return True

        if any(k in t_low for k in ["술 중심", "제대로", "많이", "달릴", "끝까지", "취할", "폭", "쭉"]):
            cm["alcohol_level"] = "술 중심"
            return True

        return False

    # -----------------------------
    # TRANSPORT (차/대중교통/도보/택시 등 자연어)
    # -----------------------------
    if qtype == "enum_transport" and key == "transport":
        # car-ish
        if any(k in t_low for k in [
            "차", "자가용", "운전", "몰고", "끌고", "주차", "발렛", "카풀", "렌트", "대리", "타고갈", "타고 갈"
        ]):
            cm["transport"] = "차"
            return True

        # public/walk-ish (사용자 표현을 "대중교통"으로 묶음)
        if any(k in t_low for k in [
            "지하철", "버스", "대중", "걸어", "도보", "뚜벅", "뚜벅이", "택시", "전철", "환승", "역", "근처 걸을"
        ]):
            cm["transport"] = "대중교통"
            return True

        # doesn't matter
        if any(k in t_low for k in ["상관", "아무", "몰라", "그냥", "무관"]):
            cm["transport"] = "상관없음"
            return True

        return False

    # -----------------------------
    # SENSITIVITY LEVEL (신경 쓰는 정도)
    # -----------------------------
    if qtype == "enum_sensitivity" and key == "sensitivity_level":
        # numeric
        if re.search(r"\b1\b", t):
            cm["sensitivity_level"] = 1; return True
        if re.search(r"\b2\b", t):
            cm["sensitivity_level"] = 2; return True
        if re.search(r"\b3\b", t):
            cm["sensitivity_level"] = 3; return True
        if re.search(r"\b4\b", t):
            cm["sensitivity_level"] = 4; return True

        # keywords
        if any(k in t for k in ["아무 생각", "막", "편하게", "완전 편", "캐주얼", "대충", "가볍게 가자"]):
            cm["sensitivity_level"] = 1; return True

        if any(k in t for k in ["적당히", "무난", "너무 막은 아닌", "깔끔하면", "평범하게"]):
            cm["sensitivity_level"] = 2; return True

        if any(k in t for k in ["좀 신경", "분위기", "괜찮은 데", "데이트 느낌", "나쁘지 않게", "괜찮게"]):
            cm["sensitivity_level"] = 3; return True

        if any(k in t for k in ["중요", "격식", "기념일", "특별한 날", "상견례", "부모님", "접대"]):
            cm["sensitivity_level"] = 4; return True

        return False

    # -----------------------------
    # FOCUS PRIORITY (대화/음식/균형) ✅ 추가
    # -----------------------------
    if qtype == "enum_focus" and key == "focus_priority":
        # 대화
        if any(k in t for k in ["대화", "수다", "얘기", "말", "토크", "이야기", "조용", "편하게 얘기"]):
            cm["focus_priority"] = "대화 중심"
            return True

        # 음식
        if any(k in t for k in ["음식", "먹는", "맛", "메뉴", "맛있는", "맛집", "배고파", "든든"]):
            cm["focus_priority"] = "음식 중심"
            return True

        # 균형
        if any(k in t for k in ["둘", "비슷", "반반", "균형", "상관", "아무"]):
            cm["focus_priority"] = "균형"
            return True

        return False

    # -----------------------------
    # ALCOHOL PLAN
    # -----------------------------
    if qtype == "enum_alcohol_plan" and key == "alcohol_plan":
        if any(k in t for k in ["한 곳", "한군데", "한 군데", "올인원", "한방에", "한 번에"]):
            cm["alcohol_plan"] = "한 곳"; return True
        if any(k in t for k in ["나눠", "2차", "1차", "옮겨", "이동", "코스", "바꿔"]):
            cm["alcohol_plan"] = "1차·2차 나눌 수도"; return True
        if any(k in t for k in ["모르", "아직", "그때 가서", "상황봐서"]):
            cm["alcohol_plan"] = "모르겠음"; return True
        return False

    # -----------------------------
    # ALCOHOL TYPE
    # -----------------------------
    if qtype == "enum_alcohol_type" and key == "alcohol_type":
        if "소주" in t or "참이슬" in t or "처음처럼" in t:
            cm["alcohol_type"] = "소주"; return True
        if any(k in t for k in ["맥주", "비어", "크래프트", "IPA", "라거", "에일"]):
            cm["alcohol_type"] = "맥주"; return True
        if "와인" in t or "내추럴" in t:
            cm["alcohol_type"] = "와인"; return True
        if any(k in t for k in ["상관", "아무", "무관"]):
            cm["alcohol_type"] = "상관없음"; return True
        return False

    # -----------------------------
    # MODE questions (optional)
    # -----------------------------
    if pending_q.get("scope") == "mode":
        k = key
        maps = {
            "friend_style": {"수다": "수다 중심", "대화": "수다 중심", "먹": "먹는 재미 중심"},
            "work_vibe": {"가볍": "가볍게", "캐주얼": "가볍게", "정돈": "정돈된 자리", "격식": "정돈된 자리"},
            "dating_stage": {"첫": "첫/어색", "어색": "첫/어색", "익숙": "익숙", "편": "익숙"},
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
# Query build (relax 0~3) + focus_priority 반영(약하게)
# -----------------------------
def build_query(conditions):
    normalize_conditions(conditions)
    cm = conditions["meta"]["common"]
    mode = conditions["meta"].get("context_mode")
    budget = conditions["meta"].get("budget_tier")
    alcohol = cm.get("alcohol_level")
    alcohol_type = cm.get("alcohol_type")
    s = cm.get("sensitivity_level")
    focus = cm.get("focus_priority")
    relax = int(cm.get("search_relax", 0) or 0)

    tokens = []
    loc = conditions.get("location")
    if loc:
        tokens.append(loc)

    # user food_type (optional)
    if conditions.get("food_type"):
        tokens.append(conditions["food_type"])

    kind = infer_place_kind(conditions)
    if kind == "cafe":
        place_token = "카페"
    elif kind == "drink":
        if alcohol_type == "와인":
            place_token = "와인바"
        elif alcohol_type == "맥주":
            place_token = "펍"
        else:
            place_token = "술집"
    else:
        place_token = "맛집"

    # relax==0에서만 약한 컨텍스트 토큰 추가 (후보 풀 말라죽는 거 방지)
    if relax == 0:
        tokens.append(place_token)

        if mode == "연인 · 썸 · 소개팅" and kind != "drink":
            tokens.append("데이트")

        if mode == "회사 회식":
            tokens.append("회식")

        if budget == "가성비":
            tokens.append("가성비")

        # 신경 레벨이 높으면 '분위기' 정도만
        if isinstance(s, int) and s >= 3 and kind == "meal":
            tokens.append("분위기")

        # ✅ focus_priority: 대화 중심이면 조용/대화 토큰 약하게(검색어에만)
        if focus == "대화 중심":
            # 너무 강하면 후보가 줄어서 '조용' 하나만
            tokens.append("조용")
        elif focus == "음식 중심":
            # 맛집 토큰은 이미 있어서 추가 X (과도제약 방지)
            pass

    elif relax == 1:
        tokens.append(place_token)
    elif relax == 2:
        tokens.append("술집" if kind == "drink" else ("카페" if kind == "cafe" else "맛집"))
    else:
        tokens.append("술집" if kind == "drink" else "맛집")

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
# Rerank + formatting (LLM)
# -----------------------------
def rerank_and_format(conditions, places):
    if client is None:
        return []
    normalize_conditions(conditions)
    cm = conditions["meta"]["common"]
    split_12 = (cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") == "1차·2차 나눌 수도")

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
      "reason": "왜 추천인지 2~3문장(후보 데이터 기반, 없는 정보 상상 금지)"%s
    }
  ]
}
""" % (',\n      "phase": "1차"  // split 모드일 때만. "1차" 또는 "2차"' if split_12 else "")

    extra_rules = ""
    if split_12:
        extra_rules = """
추가 규칙:
- 지금은 '1차·2차'를 나눠서 추천.
- picks 총 3개 유지.
- phase 포함:
  - 1차 2개
  - 2차 1개
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
- picks는 반드시 3개
- scene_feel은 "실내 좌석 간격/조명/사진 분석"처럼 단정 금지. '체감'만.
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
    return picks if isinstance(picks, list) else []

# -----------------------------
# Pre recommend text
# -----------------------------
def generate_pre_recommend_text(conditions, query):
    if client is None:
        return f"오케이ㅋㅋ **{query}**로 바로 3곳 뽑아볼게 🔍"
    prompt = f"""
너는 식당 잘 아는 친구다.
추천을 시작하기 직전에 하는 멘트를 1~2문장으로 만들어라.
조건을 반영해서 말해라. 이모지 1개 정도.

조건:
{json.dumps(conditions, ensure_ascii=False)}

검색 키워드:
{query}
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

        # intents
        if detect_skip_intent(user_input):
            conditions["meta"]["fast_mode"] = True
        if detect_expand_intent(user_input):
            cm["search_relax"] = min(3, int(cm.get("search_relax", 0)) + 1)

        # 1) Apply pending answer first
        if st.session_state.pending_question is not None:
            ok = apply_answer(conditions, st.session_state.pending_question, user_input)
            if ok:
                st.session_state.pending_question = None

        # 2) Patch merge
        patch = extract_conditions_patch(user_input, conditions)
        diversify = bool(patch.pop("diversify", False))
        exclude_last = bool(patch.pop("exclude_last", False))
        avoid_franchise = bool(patch.pop("avoid_franchise", False))
        conditions = merge_conditions(conditions, patch)
        st.session_state.conditions = conditions
        cm = conditions["meta"]["common"]

        # Debug
        with st.expander("🧾 현재 누적 조건(JSON)"):
            st.json(conditions)
            if debug_mode:
                st.markdown("**(디버그) patch 원문**")
                st.code(st.session_state.debug_raw_patch)

        # 3) Next question
        next_q = get_next_question(conditions)

        if next_q and not (conditions["meta"].get("fast_mode") and next_q.get("key") not in ("location", "cannot_eat")):
            st.markdown(next_q["text"])
            st.session_state.messages.append({"role": "assistant", "content": next_q["text"]})
            st.session_state.pending_question = next_q
            st.stop()

        if not conditions.get("location"):
            msg = "좋아! 근데 **동네/역**부터 알려줘야 내가 뽑아주지 😎\n예: `합정`, `연남동`, `강남역`"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.session_state.pending_question = {"scope": "common", "key": "location", "text": msg, "type": "free"}
            st.stop()

        # -----------------------------
        # Kakao search: bigger pool + center/radius/distance
        # -----------------------------
        transport = cm.get("transport") or "상관없음"
        location = conditions.get("location")

        center = get_location_center(location, kakao_key)
        cm["center_name"] = center.get("name") if center else None

        pool_radius_steps = [1600, 2500, 4000] if transport == "차" else [1200, 1800, 2500]

        def run_kakao_pooled(query_str: str):
            if not center:
                return kakao_keyword_search_paged(query_str, kakao_key, size=15, max_pages=3)
            final_docs = []
            for r in pool_radius_steps:
                docs = kakao_keyword_search_paged(
                    query_str, kakao_key,
                    x=center["x"], y=center["y"],
                    radius=r, sort="distance",
                    size=15, max_pages=3
                )
                final_docs = docs
                if len(docs) >= 25:
                    break
            return final_docs

        places = []
        used_query = None

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

        pre_text = generate_pre_recommend_text(conditions, used_query or build_query(conditions))
        st.markdown(pre_text)

        if debug_mode:
            st.caption(f"🔎 사용된 검색어: {used_query} (relax={cm.get('search_relax', 0)})")
            if center:
                st.caption(f"📌 중심좌표: {cm.get('center_name')}")

        # -----------------------------
        # Sort + exclude last + radius focus + attach meta
        # -----------------------------
        if center:
            places = sort_places_for_transport(places, center, transport)

        if diversify or exclude_last:
            places = filter_places(places, st.session_state.last_picks_ids)

        focused = []
        if center:
            for r in ([1200, 1800, 2500] if transport != "차" else [1600, 2500, 4000]):
                within = filter_places_by_radius(places, center, r)
                if len(within) >= 12:
                    focused = within
                    break
            if not focused:
                focused = places
        else:
            focused = places

        focused = attach_distance_meta(focused, center)

        # -----------------------------
        # Filters BEFORE LLM
        # -----------------------------
        kind = infer_place_kind(conditions)
        filtered = filter_by_kind(focused, kind)
        filtered = mild_context_filter(filtered, conditions)
        filtered = filter_franchise(filtered, avoid_franchise)

        candidates = filtered[:25]

        if debug_mode:
            with st.expander("🧪 (디버그) 후보 풀"):
                sample = [{
                    "name": p.get("place_name"),
                    "cat": p.get("category_name"),
                    "walk_min": p.get("_walk_min"),
                    "dist_m": p.get("_distance_m")
                } for p in candidates[:15]]
                st.json({
                    "kind": kind,
                    "raw_places": len(places),
                    "focused": len(focused),
                    "after_filters": len(filtered),
                    "candidates": len(candidates),
                    "sample": sample
                })

        # -----------------------------
        # Rerank
        # -----------------------------
        picks = rerank_and_format(conditions, candidates)

        if debug_mode:
            with st.expander("🤖 (디버그) rerank LLM 원문"):
                st.code(st.session_state.debug_raw_rerank)

        picks = ensure_3_picks(picks, candidates)

        # -----------------------------
        # Render
        # -----------------------------
        kakao_map = {p.get("id"): p for p in candidates if p.get("id")}

        st.markdown("---")
        st.subheader("🍽️ 딱 3곳만 골랐어")
        st.caption("※ 정답 추천이 아니라, 고민 범위를 3개로 줄여주는 후보 압축이야.")

        cols = st.columns(3)
        current_pick_ids = []
        center_name = cm.get("center_name") or "기준점"

        for i, pick in enumerate(picks[:3]):
            pid = pick.get("id")
            place = kakao_map.get(pid)
            if not place:
                continue
            current_pick_ids.append(pid)

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

        # optional log
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
                        "after_filters": len(filtered),
                        "candidates": len(candidates),
                    }
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

        final = "끝! 😎\n셋 중에 하나 고르거나, '대화 더 되는 쪽', '음식 더 확실한 쪽', '프차 빼줘', '방금 추천 제외하고 다시' 이런 식으로 다시 시켜도 돼."
        st.session_state.messages.append({"role": "assistant", "content": final})
        st.markdown(final)
