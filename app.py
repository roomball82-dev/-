# decision_mate_v7_final.py
# ✅ 심사용 안정 데모 우선 + 논의한 완성도(핵심 기능) 전부 반영한 통짜 최종본
# - Sidebar: 상황 모드 / 장소 타입 / 음식 분류 / 인원 / 예산 / 프차 지양 / 디버그
# - Chat: 질문 트리 + 자연어 입력 대부분 처리 + fast mode(그냥 추천해) + 다른 데(exclude last)
# - Candidate: 카카오 로컬 API 페이지 확장 + 완화 단계 + 거리/도보 추정 + 타입 강제 필터 + 민감도 금기(소개팅 과한 옵션 지양)
# - Alcohol: 술 여부 + 술 중심이면 주종/1차2차 반영(가중치/프롬프트)
# - Output: 무조건 3개 보장 + 추천 이유/장면/해시태그 + 카카오맵 링크

import json
import re
import math
import requests
import streamlit as st
from openai import OpenAI
from math import radians, sin, cos, sqrt, atan2


# -----------------------------
# Streamlit page
# -----------------------------
st.set_page_config(page_title="결정 메이트", page_icon="🍽️", layout="wide")
st.title("🍽️ 결정 메이트 (Decision Mate)")
st.caption("맛집 추천이 아니라, 약속 장소 '결정 피로'를 줄이는 대화형 추천")


# -----------------------------
# Session init
# -----------------------------
def init_messages():
    return [{
        "role": "assistant",
        "content": "오케이 😎\n오늘 어디서 누구랑 뭐 먹을지 내가 딱 정해줄게.\n일단 **어느 동네/역 근처**에서 찾을까?"
    }]


def init_conditions():
    return {
        "location": None,
        "constraints": {
            "cannot_eat": [],
            "avoid_recent": [],
            "avoid_franchise": False,
            "need_parking": None,  # 확정 정보는 아니고 가중치 힌트로만 사용
        },
        "meta": {
            # sidebar
            "mode": "선택 안 함",
            "place_type": "자동",   # 자동/식사/술/카페
            "food_class": "자동",   # 자동/한식/중식/일식/양식
            "people_count": 2,
            "budget_tier": "상관없음",

            # chat flow
            "fast_mode": False,     # "그냥 추천해"면 질문 중단
            "pending_question": None,
            "answers": {},          # mode-specific answers

            # common extracted
            "common": {
                "cannot_eat_done": False,
                "alcohol_level": None,   # 없음/가볍게/술 중심
                "alcohol_plan": None,    # 한 곳/1차·2차 나눌 수도/모르겠음
                "alcohol_type": None,    # 소주/맥주/와인/상관없음
                "transport": None,       # 차/대중교통/상관없음
                "walk_limit_min": 20,    # 기본 20
                "sensitivity": None,     # 1~4
                "focus": None,           # 대화 중심/음식 중심/균형
                "search_relax": 0,       # 후보 부족시 완화 단계
                "center": None,          # {x,y,name}
            }
        }
    }


if "messages" not in st.session_state:
    st.session_state.messages = init_messages()

if "conditions" not in st.session_state:
    st.session_state.conditions = init_conditions()

if "last_picks_ids" not in st.session_state:
    st.session_state.last_picks_ids = []

if "openai_key" not in st.session_state:
    st.session_state.openai_key = ""
if "kakao_key" not in st.session_state:
    st.session_state.kakao_key = ""

if "debug_raw_rerank" not in st.session_state:
    st.session_state.debug_raw_rerank = ""

if "loc_center_cache" not in st.session_state:
    st.session_state.loc_center_cache = {}


# -----------------------------
# Sidebar
# -----------------------------
st.sidebar.header("🔑 API 설정")
openai_key = st.sidebar.text_input("OpenAI API Key", type="password", value=st.session_state.openai_key)
kakao_key = st.sidebar.text_input("Kakao Local REST API Key", type="password", value=st.session_state.kakao_key)
st.session_state.openai_key = openai_key
st.session_state.kakao_key = kakao_key

debug_mode = st.sidebar.checkbox("🛠️ 디버그 모드", value=False)

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
PLACE_TYPE_OPTIONS = ["자동", "식사", "술", "카페"]
FOOD_CLASS_OPTIONS = ["자동", "한식", "중식", "일식", "양식"]
BUDGET_OPTIONS = ["상관없음", "가성비", "보통", "조금 특별"]

mode = st.sidebar.selectbox("상황 모드", MODE_OPTIONS, index=0)
place_type = st.sidebar.selectbox("장소 타입", PLACE_TYPE_OPTIONS, index=0)
food_class = st.sidebar.selectbox("음식 분류", FOOD_CLASS_OPTIONS, index=0)
people_count = st.sidebar.number_input("인원", min_value=1, max_value=30, value=2, step=1)
budget_tier = st.sidebar.radio("예산대(1인)", BUDGET_OPTIONS, index=0)
avoid_franchise = st.sidebar.checkbox("프랜차이즈(체인) 지양", value=False)

st.sidebar.markdown("---")
if st.sidebar.button("🔄 새 추천 시작(키 유지)"):
    # 키는 session에 남기고 대화/조건만 리셋
    st.session_state.messages = init_messages()
    st.session_state.conditions = init_conditions()
    st.session_state.last_picks_ids = []
    st.rerun()


# apply sidebar to conditions (필수)
cond = st.session_state.conditions
cond["meta"]["mode"] = mode
cond["meta"]["place_type"] = place_type
cond["meta"]["food_class"] = food_class
cond["meta"]["people_count"] = int(people_count)
cond["meta"]["budget_tier"] = budget_tier
cond["constraints"]["avoid_franchise"] = bool(avoid_franchise)


# -----------------------------
# OpenAI client
# -----------------------------
client = OpenAI(api_key=openai_key) if openai_key else None


# -----------------------------
# Helpers: text normalize + intent detect
# -----------------------------
def nt(text: str) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    t = re.sub(r"[`~!@#$%^&*_=+\[\]{};:\"\\|<>]", " ", t)
    t = t.replace("…", " ").replace("·", " ").replace("・", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def nc(text: str) -> str:
    return re.sub(r"\s+", "", nt(text))


def contains_any(tc: str, keys: list[str]) -> bool:
    return any(k in tc for k in keys)


def detect_fast(text: str) -> bool:
    tc = nc(text)
    keys = ["그냥추천", "걍추천", "바로추천", "됐고추천", "묻지말고", "스킵", "skip", "대충추천", "아무거나추천"]
    return contains_any(tc, keys)


def detect_exclude_last(text: str) -> bool:
    tc = nc(text)
    keys = ["다른데", "다른곳", "딴데", "방금제외", "아까제외", "그거빼고", "중복말고", "새로운데"]
    return contains_any(tc, keys)


# -----------------------------
# Natural language parsers (핵심)
# -----------------------------
def parse_minutes(text: str) -> int | None:
    t = nt(text)
    m = re.search(r"(\d+)\s*(분|min|mins|minutes)?", t)
    if not m:
        return None
    try:
        v = int(m.group(1))
        if 1 <= v <= 120:
            return v
    except Exception:
        return None
    return None


def parse_transport(text: str) -> str | None:
    tc = nc(text)
    if not tc:
        return None
    car = ["차", "자가용", "운전", "몰고", "끌고", "주차", "발렛", "parking", "대리", "렌트"]
    transit = ["지하철", "버스", "대중", "전철", "역", "뚜벅", "도보", "걸어", "택시", "킥보드"]
    anyv = ["상관없", "아무", "무관"]
    if contains_any(tc, car):
        return "차"
    if contains_any(tc, transit):
        return "대중교통"
    if contains_any(tc, anyv):
        return "상관없음"
    return None


def parse_alcohol_level(text: str) -> str | None:
    tc = nc(text)
    if not tc:
        return None
    none_keys = ["없음", "안마셔", "안마실", "술안", "금주", "노알", "패스", "안함", "안먹", "안마", "no"]
    light_keys = ["가볍", "한잔", "한두잔", "적당", "살짝", "조금", "분위기만", "1잔", "2잔"]
    heavy_keys = ["술중심", "달리", "끝까지", "제대로", "진하게", "폭음", "2차", "3차", "차수"]
    if contains_any(tc, none_keys):
        return "없음"
    if contains_any(tc, heavy_keys):
        return "술 중심"
    if contains_any(tc, light_keys):
        return "가볍게"
    if contains_any(tc, ["소주", "맥주", "와인", "하이볼", "막걸리", "칵테일"]):
        return "가볍게"
    return None


def parse_alcohol_plan(text: str) -> str | None:
    tc = nc(text)
    one_place = ["한곳", "한군데", "한자리", "옮기기싫", "이동없", "그자리에서", "한방에"]
    split = ["1차", "2차", "3차", "나눠", "옮겨", "이동", "코스", "돌아다", "2차가자"]
    unsure = ["모르", "미정", "상황봐", "그때가서"]
    if contains_any(tc, unsure):
        return "모르겠음"
    if contains_any(tc, split):
        return "1차·2차 나눌 수도"
    if contains_any(tc, one_place):
        return "한 곳"
    return None


def parse_alcohol_type(text: str) -> str | None:
    tc = nc(text)
    soju = ["소주", "참이슬", "처음처럼", "진로", "새로", "소맥", "막걸리", "전통주"]
    beer = ["맥주", "비어", "beer", "호프", "크래프트", "ipa", "라거", "에일", "하이볼", "펍"]
    wine = ["와인", "wine", "내추럴", "샴페인", "비스트로", "와인바"]
    anyv = ["상관없", "아무", "무관", "다좋", "다괜찮"]
    if contains_any(tc, soju):
        return "소주"
    if contains_any(tc, beer):
        return "맥주"
    if contains_any(tc, wine):
        return "와인"
    if contains_any(tc, anyv):
        return "상관없음"
    return None


def parse_sensitivity(text: str) -> int | None:
    t = nt(text)
    tc = nc(text)
    m = re.search(r"\b([1-4])\b", t)
    if m:
        return int(m.group(1))
    lvl4 = ["중요", "격식", "기념일", "상견례", "접대", "부모님", "프러포즈"]
    lvl3 = ["소개팅", "썸", "데이트", "신경", "분위기", "조용한데", "실패하면안"]
    lvl2 = ["무난", "적당", "보통", "깔끔하면"]
    lvl1 = ["대충", "아무", "막", "캐주얼", "편하게"]
    if contains_any(tc, lvl4):
        return 4
    if contains_any(tc, lvl3):
        return 3
    if contains_any(tc, lvl2):
        return 2
    if contains_any(tc, lvl1):
        return 1
    return None


def parse_focus(text: str) -> str | None:
    tc = nc(text)
    talk = ["대화", "수다", "얘기", "이야기", "토크", "조용", "말하기"]
    food = ["음식", "맛", "맛집", "메뉴", "식도락", "든든", "푸짐", "배고파"]
    balance = ["균형", "반반", "둘다", "상관없", "무관", "아무"]
    has_talk = contains_any(tc, talk)
    has_food = contains_any(tc, food)
    if has_talk and has_food:
        return "균형"
    if has_talk:
        return "대화 중심"
    if has_food:
        return "음식 중심"
    if contains_any(tc, balance):
        return "균형"
    return None


def parse_mode_answer(mode_key: str, text: str) -> str | None:
    tc = nc(text)

    if mode_key == "friend_style":
        if contains_any(tc, ["수다", "대화", "얘기", "조용", "토크"]):
            return "수다 중심"
        if contains_any(tc, ["맛", "메뉴", "먹", "식도락", "푸짐"]):
            return "먹는 재미 중심"
        return None

    if mode_key == "work_vibe":
        if contains_any(tc, ["접대", "격식", "정돈", "조용", "윗사람", "임원", "대표"]):
            return "정돈된 자리"
        if contains_any(tc, ["가볍", "캐주얼", "편하게", "친목", "술자리"]):
            return "가볍게"
        return None

    if mode_key == "dating_stage":
        if contains_any(tc, ["처음", "첫", "소개팅", "어색", "초반", "초기", "초면"]):
            return "첫/어색"
        if contains_any(tc, ["익숙", "편", "커플", "연인", "여러번", "자주", "기념일"]):
            return "익숙"
        # "2번째" 같은 표현
        if re.search(r"(\d+)\s*(번|번째|회|차)", nt(text)):
            try:
                n = int(re.search(r"(\d+)", nt(text)).group(1))
                if n >= 2:
                    return "익숙"
            except Exception:
                pass
        return None

    if mode_key == "family_member":
        if contains_any(tc, ["둘다", "아이도", "어른도", "부모님도"]):
            return "둘 다"
        has_kid = contains_any(tc, ["아이", "아기", "유아", "조카", "키즈"])
        has_adult = contains_any(tc, ["부모", "부모님", "할머니", "할아버지", "연세", "어른"])
        if has_kid and has_adult:
            return "둘 다"
        if has_kid:
            return "아이"
        if has_adult:
            return "어른"
        if contains_any(tc, ["없", "없음", "해당없"]):
            return "없음"
        return None

    return None


# -----------------------------
# Kakao API (paged + uniq)
# -----------------------------
def kakao_keyword_search(query: str, rest_key: str, size: int = 15, page: int = 1,
                         x: str | None = None, y: str | None = None,
                         radius: int | None = None, sort: str | None = None):
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {rest_key}"}
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


def kakao_search_paged(query: str, rest_key: str, max_pages: int = 3, size: int = 15,
                      x: str | None = None, y: str | None = None,
                      radius: int | None = None, sort: str | None = None):
    all_docs = []
    for page in range(1, max_pages + 1):
        data = kakao_keyword_search(query, rest_key, size=size, page=page, x=x, y=y, radius=radius, sort=sort)
        docs = data.get("documents", []) or []
        meta = data.get("meta", {}) or {}
        all_docs.extend(docs)
        if meta.get("is_end") is True:
            break
        if len(docs) < size:
            break

    uniq = {}
    for d in all_docs:
        pid = d.get("id")
        if pid:
            uniq[pid] = d
    return list(uniq.values())


# -----------------------------
# Geo helpers
# -----------------------------
def haversine_m(x1, y1, x2, y2):
    lon1, lat1, lon2, lat2 = map(radians, [float(x1), float(y1), float(x2), float(y2)])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return 6371000 * c


def estimate_walk_minutes(distance_m: float, speed_m_per_min: float = 80.0) -> int:
    return max(1, int(math.ceil(distance_m / speed_m_per_min)))


def get_location_center(location: str, rest_key: str):
    loc = (location or "").strip()
    if not loc:
        return None

    cache = st.session_state.loc_center_cache
    if loc in cache:
        return cache[loc]

    candidates = [loc] if "역" in loc else [f"{loc}역", loc]
    for cand in candidates:
        try:
            docs = kakao_search_paged(cand, rest_key, max_pages=1, size=15)
            if not docs:
                continue
            d = docs[0]
            x, y = d.get("x"), d.get("y")
            if x and y:
                center = {"x": x, "y": y, "name": cand}
                cache[loc] = center
                return center
        except Exception:
            continue
    return None


# -----------------------------
# Query + Candidate pipeline
# -----------------------------
def build_query(conditions: dict) -> str:
    m = conditions["meta"]
    cm = m["common"]
    tokens = []

    if conditions.get("location"):
        tokens.append(conditions["location"])

    # place type
    pt = m.get("place_type", "자동")
    if pt == "술":
        tokens.append("술집")
    elif pt == "카페":
        tokens.append("카페")
    elif pt == "식사":
        tokens.append("맛집")

    # food class
    fc = m.get("food_class", "자동")
    if fc != "자동":
        tokens.append(fc)

    # alcohol hint
    if pt != "카페":
        if cm.get("alcohol_level") == "술 중심":
            tokens.append("술")
        elif cm.get("alcohol_level") == "가볍게" and pt == "자동":
            tokens.append("술집")

    # alcohol type
    at = cm.get("alcohol_type")
    if at and at != "상관없음":
        tokens.append(at)

    # focus hint
    focus = cm.get("focus")
    if focus == "대화 중심":
        tokens.append("조용한")
    elif focus == "음식 중심":
        tokens.append("맛집")

    # mode hints (가볍게만)
    mode = m.get("mode")
    if mode == "회사 회식":
        tokens.append("회식")
    elif mode == "단체 모임":
        tokens.append("단체")
    elif mode == "혼밥":
        tokens.append("혼밥")
    elif mode == "연인 · 썸 · 소개팅":
        tokens.append("데이트")

    return " ".join([t for t in tokens if t]).strip()


def get_candidate_pool(conditions: dict, rest_key: str):
    """
    완화 단계:
    relax 0: radius=1200, pages=2
    relax 1: radius=2000, pages=3
    relax 2: radius=None, pages=4
    relax 3: query 약화(location + place_type + food_class), radius=None, pages=4
    """
    m = conditions["meta"]
    cm = m["common"]
    relax = int(cm.get("search_relax", 0))

    center = get_location_center(conditions.get("location"), rest_key)
    cm["center"] = center

    pages = 2 if relax == 0 else (3 if relax == 1 else 4)
    radius = 1200 if relax == 0 else (2000 if relax == 1 else None)

    x = center["x"] if center else None
    y = center["y"] if center else None
    sort = "distance" if center else None

    query = build_query(conditions)
    places = kakao_search_paged(query, rest_key, max_pages=pages, size=15, x=x, y=y, radius=radius, sort=sort)

    if relax >= 3 and len(places) < 10:
        weak = [conditions.get("location", "")]
        pt = m.get("place_type", "자동")
        fc = m.get("food_class", "자동")
        if pt == "술":
            weak.append("술집")
        elif pt == "카페":
            weak.append("카페")
        elif pt == "식사":
            weak.append("맛집")
        if fc != "자동":
            weak.append(fc)
        weak_query = " ".join([t for t in weak if t]).strip()
        places2 = kakao_search_paged(weak_query, rest_key, max_pages=4, size=15, x=x, y=y, radius=None, sort=sort)
        byid = {p.get("id"): p for p in places if p.get("id")}
        for p in places2:
            pid = p.get("id")
            if pid and pid not in byid:
                byid[pid] = p
        places = list(byid.values())

    return places, center, query


def filter_by_place_type(places: list, place_type: str):
    if place_type == "카페":
        allow = ["카페", "디저트", "베이커리", "아이스크림"]
        out = [p for p in places if any(a in (p.get("category_name") or "") for a in allow)]
        return out if len(out) >= 8 else places
    if place_type == "술":
        allow = ["술", "주점", "호프", "이자카야", "바", "포차", "펍", "와인", "막걸리", "전통주"]
        out = [p for p in places if any(a in (p.get("category_name") or "") for a in allow)]
        return out if len(out) >= 8 else places
    if place_type == "식사":
        banned = ["카페", "디저트", "베이커리", "아이스크림"]
        out = [p for p in places if not any(b in (p.get("category_name") or "") for b in banned)]
        return out if len(out) >= 8 else places
    return places


def franchise_filter(places: list, avoid: bool):
    if not avoid:
        return places
    franchise_keywords = ["스타벅스", "투썸", "이디야", "메가커피", "빽다방", "홍콩반점", "교촌", "bhc", "bbq", "버거킹", "맥도날드", "kfc"]
    out = []
    for p in places:
        name = (p.get("place_name") or "")
        if any(k.lower() in name.lower() for k in franchise_keywords):
            continue
        out.append(p)
    return out if len(out) >= 8 else places


def dating_high_sensitivity_filter(places: list, conditions: dict):
    m = conditions["meta"]
    cm = m["common"]
    if m.get("mode") != "연인 · 썸 · 소개팅":
        return places
    s = cm.get("sensitivity")
    if not isinstance(s, int) or s < 3:
        return places
    banned_words = ["오마카세", "파인다이닝", "테이스팅", "코스"]
    out = []
    for p in places:
        name = (p.get("place_name") or "")
        if any(b in name for b in banned_words):
            continue
        out.append(p)
    return out if len(out) >= 8 else places


def alcohol_type_match_score(place: dict, alcohol_type: str | None) -> int:
    if not alcohol_type or alcohol_type == "상관없음":
        return 0
    name = (place.get("place_name") or "").lower()
    cat = (place.get("category_name") or "").lower()
    text = f"{name} {cat}"

    if alcohol_type == "소주":
        hits = ["포차", "주점", "한식주점", "소주", "막걸리", "전통주", "곱창", "삼겹", "고기"]
        misses = ["펍", "브루", "브루어리", "크래프트", "와인", "와인바", "칵테일", "beer", "pub"]
    elif alcohol_type == "맥주":
        hits = ["호프", "펍", "비어", "브루", "브루어리", "크래프트", "beer", "pub", "치킨", "하이볼"]
        misses = ["와인", "와인바", "전통주", "막걸리", "포차", "한식주점", "소주"]
    elif alcohol_type == "와인":
        hits = ["와인", "와인바", "비스트로", "내추럴", "wine", "bar", "브런치"]
        misses = ["호프", "펍", "포차", "소주", "막걸리"]
    else:
        return 0

    score = 0
    for h in hits:
        if h in text:
            score += 2
    for m in misses:
        if m in text:
            score -= 2
    return score


def prioritize_places(places: list, center: dict | None, conditions: dict):
    m = conditions["meta"]
    cm = m["common"]
    transport = cm.get("transport")
    walk_limit = cm.get("walk_limit_min") or 20
    alcohol_type = cm.get("alcohol_type")

    def parking_signal(p: dict) -> int:
        text = f"{p.get('place_name','')} {p.get('category_name','')}".lower()
        score = 0
        if "주차" in text or "parking" in text or "발렛" in text:
            score += 3
        return score

    scored = []
    for p in places:
        dist = 10**12
        walk = None
        if center and center.get("x") and center.get("y") and p.get("x") and p.get("y"):
            try:
                dist = haversine_m(center["x"], center["y"], p["x"], p["y"])
                walk = estimate_walk_minutes(dist)
            except Exception:
                dist = 10**12
                walk = None

        score = dist

        if transport == "차":
            score -= parking_signal(p) * 140
        elif transport == "대중교통":
            if walk is not None and walk > walk_limit:
                score += (walk - walk_limit) * 120

        score -= alcohol_type_match_score(p, alcohol_type) * 180

        scored.append((score, dist, p))

    scored.sort(key=lambda x: (x[0], x[1]))
    return [p for _, __, p in scored]


def filter_exclude_last(places: list, exclude_ids: list):
    if not exclude_ids:
        return places
    ex = set(exclude_ids)
    out = [p for p in places if p.get("id") not in ex]
    return out if len(out) >= 6 else places


# -----------------------------
# Questions (공통 + 모드별)
# -----------------------------
MODE_QUESTIONS = {
    "친구": [{"key": "friend_style", "text": "친구면 오늘은 **수다/대화** 쪽이야, 아니면 **메뉴/맛** 쪽이야? (자유롭게 말해도 됨)"}],
    "회사 회식": [{"key": "work_vibe", "text": "회식 분위기 어떤 쪽? (예: 가볍게 / 정돈된 자리·접대 느낌)"}],
    "연인 · 썸 · 소개팅": [{"key": "dating_stage", "text": "관계 단계가 어때? (예: 처음·아직 어색 / 몇 번 만남·편한 편)"}],
    "가족": [{"key": "family_member", "text": "가족 구성에 **아이/어른(연세)** 있어? (아이 있음/어른 있음/둘 다/없음)"}],
}

def next_common_question(conditions: dict):
    cm = conditions["meta"]["common"]
    m = conditions["meta"]

    if not conditions.get("location"):
        return {"scope": "common", "key": "location", "text": "오케이! **어느 동네/역 근처**에서 찾을까? 📍"}

    if not cm.get("cannot_eat_done"):
        return {"scope": "common", "key": "cannot_eat", "text": "못 먹는 거 있어? (알레르기/극혐 포함) 없으면 **없음** 🙅"}

    # fast mode면 더 안 묻고 바로 추천
    if m.get("fast_mode"):
        return None

    if cm.get("alcohol_level") is None:
        # place_type이 카페면 술 질문을 뒤로 미루되, 사용자가 술을 말하면 자동 반영됨
        if m.get("place_type") == "카페":
            cm["alcohol_level"] = "없음"
        else:
            return {"scope": "common", "key": "alcohol_level", "text": "오늘 술은 어때? (안 마셔/한잔/술 중심)"}

    if cm.get("transport") is None:
        return {"scope": "common", "key": "transport", "text": "이동수단은? (뚜벅/지하철/택시 vs 차/주차)"}

    if cm.get("walk_limit_min") is None:
        return {"scope": "common", "key": "walk_limit_min", "text": "도보는 최대 몇 분까지 괜찮아? (10분/15분/상관없음)"}

    if cm.get("sensitivity") is None:
        return {"scope": "common", "key": "sensitivity", "text": "이 자리는 얼마나 신경 써야 해? (1 대충~ 4 중요한 자리)"}

    if cm.get("focus") is None:
        return {"scope": "common", "key": "focus", "text": "오늘은 **대화**가 더 중요해? **음식**이 더 중요해? (대화/음식/균형)"}

    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") is None:
        return {"scope": "common", "key": "alcohol_plan", "text": "술 중심이면 흐름은? (한 곳/1차2차 나눔/모르겠음)"}

    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_type") is None:
        return {"scope": "common", "key": "alcohol_type", "text": "주로 뭐 마실 생각이야? (소주/맥주/와인/상관없음)"}

    return None


def next_mode_question(conditions: dict):
    m = conditions["meta"]
    mode = m.get("mode", "선택 안 함")
    if mode not in MODE_QUESTIONS:
        return None
    answers = m.get("answers", {})
    for q in MODE_QUESTIONS[mode]:
        if answers.get(q["key"]) is None:
            return {"scope": "mode", **q}
    return None


def get_next_question(conditions: dict):
    q = next_common_question(conditions)
    if q:
        return q
    return next_mode_question(conditions)


# -----------------------------
# Apply answer (pending 질문 + 자동 채움)
# -----------------------------
def apply_answer(conditions: dict, pending: dict | None, user_text: str) -> bool:
    m = conditions["meta"]
    cm = m["common"]
    answers = m.get("answers", {})

    if detect_fast(user_text):
        m["fast_mode"] = True
        return True

    # 자동 채움(비어있을 때만)
    def fill_extras():
        if cm.get("alcohol_level") is None:
            v = parse_alcohol_level(user_text)
            if v:
                cm["alcohol_level"] = v
                if v == "없음":
                    cm["alcohol_plan"] = None
                    cm["alcohol_type"] = None
        if cm.get("transport") is None:
            v = parse_transport(user_text)
            if v:
                cm["transport"] = v
        if cm.get("walk_limit_min") is None:
            v = parse_minutes(user_text)
            if v:
                cm["walk_limit_min"] = max(5, min(60, v))
        if cm.get("sensitivity") is None:
            v = parse_sensitivity(user_text)
            if v:
                cm["sensitivity"] = v
        if cm.get("focus") is None:
            v = parse_focus(user_text)
            if v:
                cm["focus"] = v
        if cm.get("alcohol_level") == "술 중심":
            if cm.get("alcohol_plan") is None:
                v = parse_alcohol_plan(user_text)
                if v:
                    cm["alcohol_plan"] = v
            if cm.get("alcohol_type") is None:
                v = parse_alcohol_type(user_text)
                if v:
                    cm["alcohol_type"] = v

    # pending 없으면 extras만 채워도 성공 처리
    if not pending:
        fill_extras()
        return True

    key = pending.get("key")
    scope = pending.get("scope")
    tc = nc(user_text)

    if scope == "common" and key == "location":
        conditions["location"] = user_text.strip()
        fill_extras()
        return True

    if scope == "common" and key == "cannot_eat":
        if contains_any(tc, ["없", "상관없", "다먹", "아무거나", "no", "노"]):
            conditions["constraints"]["cannot_eat"] = []
        else:
            parts = re.split(r"[,\n/]+", user_text)
            cleaned = []
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                p = re.sub(r"(은|는|이|가|을|를|만|빼고|빼줘|싫어|못먹|알레르기)$", "", p).strip()
                if p and p not in cleaned:
                    cleaned.append(p)
            conditions["constraints"]["cannot_eat"] = cleaned[:10]
        cm["cannot_eat_done"] = True
        fill_extras()
        return True

    if scope == "common" and key == "alcohol_level":
        v = parse_alcohol_level(user_text)
        if not v:
            # "없음"의 다양한 표현
            if contains_any(tc, ["안마", "술안", "패스", "x", "노"]):
                v = "없음"
        if not v:
            return False
        cm["alcohol_level"] = v
        if v == "없음":
            cm["alcohol_plan"] = None
            cm["alcohol_type"] = None
        fill_extras()
        return True

    if scope == "common" and key == "transport":
        v = parse_transport(user_text)
        if not v and contains_any(tc, ["걸어", "도보", "뚜벅"]):
            v = "대중교통"
        if not v:
            return False
        cm["transport"] = v
        fill_extras()
        return True

    if scope == "common" and key == "walk_limit_min":
        if contains_any(tc, ["상관없", "아무", "무관"]):
            cm["walk_limit_min"] = 30
            fill_extras()
            return True
        v = parse_minutes(user_text)
        if not v:
            return False
        cm["walk_limit_min"] = max(5, min(60, v))
        fill_extras()
        return True

    if scope == "common" and key == "sensitivity":
        v = parse_sensitivity(user_text)
        if not v:
            return False
        cm["sensitivity"] = v
        fill_extras()
        return True

    if scope == "common" and key == "focus":
        v = parse_focus(user_text)
        if not v:
            return False
        cm["focus"] = v
        fill_extras()
        return True

    if scope == "common" and key == "alcohol_plan":
        v = parse_alcohol_plan(user_text)
        if not v:
            return False
        cm["alcohol_plan"] = v
        fill_extras()
        return True

    if scope == "common" and key == "alcohol_type":
        v = parse_alcohol_type(user_text)
        if not v:
            return False
        cm["alcohol_type"] = v
        fill_extras()
        return True

    if scope == "mode":
        v = parse_mode_answer(key, user_text)
        if not v:
            return False
        answers[key] = v
        m["answers"] = answers
        fill_extras()
        return True

    fill_extras()
    return True


# -----------------------------
# LLM rerank (안정 JSON)
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


def rerank_and_format(conditions: dict, places: list):
    if client is None:
        return []

    m = conditions["meta"]
    cm = m["common"]

    compact = []
    for p in places[:20]:
        compact.append({
            "id": p.get("id"),
            "name": p.get("place_name"),
            "category": p.get("category_name"),
            "address": p.get("road_address_name") or p.get("address_name"),
            "url": p.get("place_url"),
        })

    rules = {
        "mode": m.get("mode"),
        "place_type": m.get("place_type"),
        "food_class": m.get("food_class"),
        "people_count": m.get("people_count"),
        "budget_tier": m.get("budget_tier"),
        "focus": cm.get("focus"),
        "alcohol_level": cm.get("alcohol_level"),
        "alcohol_plan": cm.get("alcohol_plan"),
        "alcohol_type": cm.get("alcohol_type"),
        "transport": cm.get("transport"),
        "walk_limit_min": cm.get("walk_limit_min"),
        "sensitivity": cm.get("sensitivity"),
        "cannot_eat": conditions["constraints"].get("cannot_eat", []),
        "avoid_franchise": conditions["constraints"].get("avoid_franchise", False),
    }

    prompt = f"""
너는 '결정 메이트'다. 후보 중 BEST 3곳만 고르고, 왜 이 3곳인지 '사용자 조건 기반'으로만 설명해라.

반드시 아래 JSON 형식만 출력:
{{
  "picks":[
    {{
      "id":"...",
      "one_line":"친구톤 한줄",
      "scene_feel":"이 곳에서 약속하면 어떤 느낌인지 1~2문장",
      "hashtags":["#...","#...","#...","#..."],
      "matched_conditions":["실제로 반영한 조건만"],
      "reason":"2~3문장. 과장 금지. 후보 데이터 기반만."
    }}
  ]
}}

중요:
- picks는 반드시 3개.
- place_type/food_class를 최대한 지켜라.
- 술 중심 + 주종 있으면 주종에 맞는 곳 우선.
- 소개팅/첫/어색 + 민감도(3~4)이면 '과한 옵션(오마카세/파인다이닝 느낌)' 지양.
- 후보 데이터에 없는 정보(주차 확정/실내간격/가격/예약가능 등) 상상 금지.
- hashtags 4~6개.
- "무조건/최고/완벽" 금지.

[사용자 조건]
{json.dumps(rules, ensure_ascii=False, indent=2)}

[후보 목록]
{json.dumps(compact, ensure_ascii=False, indent=2)}
"""

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.25,
        response_format={"type": "json_object"},
    )
    raw = (res.choices[0].message.content or "").strip()
    st.session_state.debug_raw_rerank = raw

    data = safe_json_load(raw) or extract_first_json_object(raw)
    if not isinstance(data, dict):
        return []
    picks = data.get("picks", [])
    if not isinstance(picks, list):
        return []
    return picks[:3]


def ensure_3_picks(picks: list, candidates: list):
    if not isinstance(picks, list):
        picks = []

    cand_map = {p.get("id"): p for p in candidates if p.get("id")}
    used = set()
    fixed = []

    for pk in picks:
        if not isinstance(pk, dict):
            continue
        pid = pk.get("id")
        if not pid or pid in used or pid not in cand_map:
            continue
        used.add(pid)
        # 안전 필드 보강
        pk.setdefault("one_line", "여기 무난하게 괜찮아 보여 😎")
        pk.setdefault("scene_feel", "카카오맵 사진/리뷰로 분위기 빠르게 확인 가능!")
        pk.setdefault("hashtags", ["#근처", "#무난", "#후보", "#바로확인"])
        pk.setdefault("matched_conditions", ["근처 우선"])
        pk.setdefault("reason", "후보 데이터 기준으로 조건에 무난하게 맞는 편이라 포함했어.")
        fixed.append(pk)

    for p in candidates:
        if len(fixed) >= 3:
            break
        pid = p.get("id")
        if not pid or pid in used:
            continue
        used.add(pid)
        fixed.append({
            "id": pid,
            "one_line": "후보 상위에서 안전하게 하나 더 챙김 😎",
            "scene_feel": "링크 눌러서 리뷰/사진 확인하면 감 바로 올 거야.",
            "hashtags": ["#근처", "#무난", "#후보추가", "#바로확인"],
            "matched_conditions": ["근처 우선"],
            "reason": "추천 결과가 부족해서 후보 풀 상위에서 안정적으로 채웠어."
        })

    return fixed[:3]


def generate_pre_text(conditions: dict, query: str):
    if client is None:
        return f"오케이ㅋㅋ **{query}**로 바로 3곳 뽑아볼게 🔍"
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"친구처럼 1~2문장으로 추천 시작 멘트. 조건 반영. 이모지 1개.\n검색어: {query}"}],
        temperature=0.8
    )
    return (res.choices[0].message.content or "").strip()


# -----------------------------
# Render chat history
# -----------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])


# -----------------------------
# Main chat input
# -----------------------------
user_input = st.chat_input("예: 홍대역 근처, 소개팅이라 조용했으면 / 예: 그냥 추천해 / 예: 다른 데")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        if not openai_key or not kakao_key:
            st.warning("사이드바에 OpenAI 키랑 Kakao 키부터 넣어줘!")
            st.stop()

        # exclude last intent
        exclude_last = detect_exclude_last(user_input)

        # apply answer
        pending = st.session_state.conditions["meta"].get("pending_question")
        ok = apply_answer(st.session_state.conditions, pending, user_input)
        if pending and not ok:
            msg = f"오케이 근데 내가 제대로 잡게 한 번만 더! 😅\n\n**{pending['text']}**"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        # clear pending
        st.session_state.conditions["meta"]["pending_question"] = None

        # next question?
        next_q = get_next_question(st.session_state.conditions)
        if next_q:
            st.session_state.conditions["meta"]["pending_question"] = next_q
            st.markdown(next_q["text"])
            st.session_state.messages.append({"role": "assistant", "content": next_q["text"]})
            st.stop()

        # -----------------------------
        # Recommend phase
        # -----------------------------
        conditions = st.session_state.conditions
        cm = conditions["meta"]["common"]

        query = build_query(conditions)
        pre = generate_pre_text(conditions, query)
        st.markdown(pre)

        # candidate pipeline with relax escalation
        relax_guard = 0
        places = []
        center = None
        used_query = query

        while relax_guard < 4:
            places, center, used_query = get_candidate_pool(conditions, kakao_key)
            places = franchise_filter(places, conditions["constraints"].get("avoid_franchise", False))
            places = filter_by_place_type(places, conditions["meta"].get("place_type", "자동"))
            places = dating_high_sensitivity_filter(places, conditions)
            places = prioritize_places(places, center, conditions)
            if exclude_last:
                places = filter_exclude_last(places, st.session_state.last_picks_ids)

            if len(places) >= 8:
                break

            # not enough -> relax up
            cm["search_relax"] = min(3, int(cm.get("search_relax", 0)) + 1)
            relax_guard += 1

        if debug_mode:
            with st.expander("🧾 현재 누적 조건(JSON)"):
                st.json(conditions)
            with st.expander("🧪 후보 풀(상위 25)"):
                st.write(f"query: {used_query}")
                st.write(f"candidates: {len(places)} / relax: {cm.get('search_relax')}")
                for p in places[:25]:
                    st.write(f"- {p.get('place_name')} | {p.get('category_name')} | {p.get('road_address_name') or p.get('address_name')}")

        if not places:
            msg = "헉… 이 조건으로는 딱 맞는 데가 잘 안 잡히네 🥲\n지역을 조금만 넓혀볼까?"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        # rerank
        picks = rerank_and_format(conditions, places)
        if debug_mode:
            with st.expander("🤖 (디버그) rerank LLM 원문"):
                st.code(st.session_state.debug_raw_rerank)

        # ensure 3
        picks = ensure_3_picks(picks, places)

        kakao_map = {p.get("id"): p for p in places if p.get("id")}

        st.markdown("---")
        st.subheader("🍽️ 딱 3곳만 골랐어")
        cols = st.columns(3)

        current_pick_ids = []

        for i, pick in enumerate(picks[:3]):
            pid = pick.get("id")
            place = kakao_map.get(pid)
            if not pid or not place:
                continue
            current_pick_ids.append(pid)

            with cols[i]:
                name = place.get("place_name")
                addr = place.get("road_address_name") or place.get("address_name")
                url = place.get("place_url")
                category = place.get("category_name")

                st.markdown(f"### {i+1}. {name}")
                st.caption(category or "")
                st.write(f"📍 {addr}")

                st.markdown(f"**{pick.get('one_line','')}**")
                scene = pick.get("scene_feel")
                if scene:
                    st.markdown(f"_이 자리 느낌_: {scene}")

                matched = pick.get("matched_conditions", [])
                if matched:
                    st.markdown("**반영한 조건**")
                    st.markdown(" · ".join([f"`{m}`" for m in matched]))

                tags = pick.get("hashtags", [])
                if tags:
                    st.markdown(" ".join(tags))

                st.markdown("**왜 여기냐면…**")
                st.write(pick.get("reason", ""))

                # walk estimate
                if center and center.get("x") and center.get("y") and place.get("x") and place.get("y"):
                    try:
                        dist = haversine_m(center["x"], center["y"], place["x"], place["y"])
                        walk_min = estimate_walk_minutes(dist)
                        st.caption(f"🚶 예상 도보 약 {walk_min}분")
                    except Exception:
                        pass

                if url:
                    st.link_button("카카오맵에서 보기", url)

        st.session_state.last_picks_ids = current_pick_ids

        final = "끝! 😎\n셋 중에 하나 고르거나, **'다른 데'**, **'더 조용한 데'**, **'완전 다른 분위기'** 이렇게 다시 시켜도 돼."
        st.session_state.messages.append({"role": "assistant", "content": final})
        st.markdown(final)
