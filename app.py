# decision_mate_v5_final.py
# ✅ FINAL v5 — "절대 기능 축소 없음" 통합본
# - 장소 타입(자동/식사/술/카페) + 음식 분류(자동/한/중/일/양) 사이드바
# - 필터 변경 시 질문 흐름만 리셋(대화 유지)
# - OpenAI/Kakao 키 세션 유지 + 새 추천 버튼(키 유지)
# - 자연어 파싱 빡세게(대부분 문장형 처리)
# - "그냥 추천해" fast mode (질문 취소 후 즉시 추천)
# - Kakao 후보 풀 확장(페이지/중복제거/반경/중심좌표)
# - 이동수단/도보제한/주차(추정) 가중치
# - 술 여부/술 중심 시 주종 반영 강화
# - 소개팅/어색 + 민감도 높은 경우 "과한 옵션(오마카세 등)" 금기 필터
# - 장소 타입 강제 필터(카페/술집/식사)
# - 후보 부족 시 단계적 완화(search_relax) + 무조건 3개 보장
# - 방금 추천 제외/다른 데 요청 처리
# - 디버그 모드(원문/후보풀/파싱상태)

import json
import re
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
# Session: API keys (persist)
# -----------------------------
if "openai_key" not in st.session_state:
    st.session_state.openai_key = ""
if "kakao_key" not in st.session_state:
    st.session_state.kakao_key = ""

st.sidebar.header("🔑 API 설정")
openai_key = st.sidebar.text_input(
    "OpenAI API Key",
    type="password",
    value=st.session_state.openai_key,
)
kakao_key = st.sidebar.text_input(
    "Kakao Local REST API Key",
    type="password",
    value=st.session_state.kakao_key,
)

st.session_state.openai_key = openai_key
st.session_state.kakao_key = kakao_key

debug_mode = st.sidebar.checkbox("🛠️ 디버그 모드(원문/후보풀/파싱)", value=False)
client = OpenAI(api_key=openai_key) if openai_key else None


# -----------------------------
# Core state init
# -----------------------------
def init_conditions():
    return {
        "location": None,
        "food_type": None,     # (대화 기반) 예: "파스타", "고기", "초밥" 등 자유
        "purpose": None,
        "people": None,
        "mood": None,
        "constraints": {
            "cannot_eat": [],
            "avoid_recent": [],
            "need_parking": None,
            "avoid_franchise": False,
        },
        "meta": {
            # sidebar
            "context_mode": "선택 안 함",
            "people_count": 2,
            "budget_tier": "상관없음",
            "place_type": "자동",   # 자동/식사/술/카페
            "food_class": "자동",   # 자동/한식/중식/일식/양식

            # flow
            "answers": {},          # mode-specific answers
            "fast_mode": False,     # “그냥 추천해” 즉시 추천
            "common": {
                "cannot_eat_done": False,
                "alcohol_level": None,       # 없음/가볍게/술 중심
                "alcohol_plan": None,        # 한 곳/1차·2차 나눌 수도/모르겠음
                "alcohol_type": None,        # 소주/맥주/와인/상관없음
                "transport": None,           # 차/대중교통/상관없음
                "walk_limit_min": 20,        # 도보 허용(분)
                "sensitivity_level": None,   # 1~4
                "focus_priority": None,      # 대화 중심/음식 중심/균형
                "center_name": None,
                "search_relax": 0,           # 후보 부족 시 완화 단계 0~3
            },
        }
    }

def init_messages():
    return [{
        "role": "assistant",
        "content": "오케이 😎\n오늘 어디서 누구랑 뭐 먹을지 내가 딱 정해줄게.\n일단 **어느 동네/역 근처**에서 찾을까?"
    }]

if "messages" not in st.session_state:
    st.session_state.messages = init_messages()

if "conditions" not in st.session_state:
    st.session_state.conditions = init_conditions()

if "last_picks_ids" not in st.session_state:
    st.session_state.last_picks_ids = []

if "pending_question" not in st.session_state:
    st.session_state.pending_question = None

# debug raw
if "debug_raw_patch" not in st.session_state:
    st.session_state.debug_raw_patch = ""
if "debug_raw_rerank" not in st.session_state:
    st.session_state.debug_raw_rerank = ""
if "debug_candidates" not in st.session_state:
    st.session_state.debug_candidates = []

# location center cache
if "loc_center_cache" not in st.session_state:
    st.session_state.loc_center_cache = {}

# -----------------------------
# Sidebar: scenario filters
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

PLACE_TYPE_OPTIONS = ["자동", "식사", "술", "카페"]
FOOD_CLASS_OPTIONS = ["자동", "한식", "중식", "일식", "양식"]

selected_mode = st.sidebar.selectbox("상황 모드", MODE_OPTIONS, index=0)
place_type = st.sidebar.selectbox("장소 타입", PLACE_TYPE_OPTIONS, index=0)
food_class = st.sidebar.selectbox("음식 분류", FOOD_CLASS_OPTIONS, index=0)

people_count = st.sidebar.number_input("인원", min_value=1, max_value=30, value=2, step=1)
budget_tier = st.sidebar.radio("예산대(1인)", BUDGET_OPTIONS, index=0)

st.sidebar.markdown("---")
avoid_franchise = st.sidebar.checkbox("프랜차이즈(체인) 지양", value=False)

st.sidebar.markdown("---")
if st.sidebar.button("🔄 새 추천 시작(키 유지)"):
    st.session_state.messages = init_messages()
    st.session_state.pending_question = None
    st.session_state.last_picks_ids = []
    st.session_state.conditions = init_conditions()
    # keys stay in session_state.openai_key/kakao_key
    st.rerun()

# -----------------------------
# Filter change → reset only question flow, keep chat
# -----------------------------
profile = f"{selected_mode}|{place_type}|{food_class}|{int(people_count)}|{budget_tier}|{avoid_franchise}"
prev_profile = st.session_state.get("sidebar_profile")
if prev_profile is None:
    st.session_state.sidebar_profile = profile
else:
    if profile != prev_profile:
        st.session_state.sidebar_profile = profile
        # apply immediately
        st.session_state.pending_question = None
        st.session_state.conditions["meta"]["answers"] = {}
        st.session_state.conditions["meta"]["fast_mode"] = False
        cm = st.session_state.conditions["meta"]["common"]
        # 모드/타입이 바뀌면 의미 달라지는 질문은 다시 묻도록
        cm["sensitivity_level"] = None
        cm["focus_priority"] = None
        # 술/카페/식사 타입 변화는 술 질문 흐름에도 영향 → 필요한 경우 다시 묻도록
        # (단, 이미 사용자가 술을 명확히 말했다면 유지해도 되는데, 여기선 안정적으로 reset 안 함)
        # 대신 place_type이 '술'이면 alcohol_level 없으면 빨리 채우도록 질문 뜨게 함

# apply sidebar into conditions
def normalize_conditions(cond: dict):
    if "constraints" not in cond or not isinstance(cond["constraints"], dict):
        cond["constraints"] = {"cannot_eat": [], "avoid_recent": [], "need_parking": None, "avoid_franchise": False}
    c = cond["constraints"]
    c.setdefault("cannot_eat", [])
    c.setdefault("avoid_recent", [])
    c.setdefault("need_parking", None)
    c.setdefault("avoid_franchise", False)
    if not isinstance(c["cannot_eat"], list):
        c["cannot_eat"] = []
    if not isinstance(c["avoid_recent"], list):
        c["avoid_recent"] = []
    if "meta" not in cond or not isinstance(cond["meta"], dict):
        cond["meta"] = {}
    m = cond["meta"]
    m.setdefault("context_mode", "선택 안 함")
    m.setdefault("people_count", 2)
    m.setdefault("budget_tier", "상관없음")
    m.setdefault("place_type", "자동")
    m.setdefault("food_class", "자동")
    m.setdefault("answers", {})
    m.setdefault("fast_mode", False)
    m.setdefault("common", {})
    cm = m["common"]
    cm.setdefault("cannot_eat_done", False)
    cm.setdefault("alcohol_level", None)
    cm.setdefault("alcohol_plan", None)
    cm.setdefault("alcohol_type", None)
    cm.setdefault("transport", None)
    cm.setdefault("walk_limit_min", 20)
    cm.setdefault("sensitivity_level", None)
    cm.setdefault("focus_priority", None)
    cm.setdefault("center_name", None)
    cm.setdefault("search_relax", 0)

normalize_conditions(st.session_state.conditions)
st.session_state.conditions["meta"]["context_mode"] = selected_mode
st.session_state.conditions["meta"]["people_count"] = int(people_count)
st.session_state.conditions["meta"]["budget_tier"] = budget_tier
st.session_state.conditions["meta"]["place_type"] = place_type
st.session_state.conditions["meta"]["food_class"] = food_class
st.session_state.conditions["constraints"]["avoid_franchise"] = bool(avoid_franchise)


# -----------------------------
# Text helpers / intents
# -----------------------------
def normalize_text(t: str) -> str:
    if not t:
        return ""
    t = t.strip().lower()
    t = re.sub(r"[`~!@#$%^&*_=+\[\]{};:\"\\|<>]", " ", t)
    t = t.replace("…", " ").replace("·", " ").replace("・", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t

def normalize_compact(t: str) -> str:
    return re.sub(r"\s+", "", normalize_text(t))

def contains_any(t_compact: str, keys: list[str]) -> bool:
    return any(k in t_compact for k in keys)

def detect_fast_intent(text: str) -> bool:
    tc = normalize_compact(text)
    keys = ["그냥추천", "걍추천", "빨리추천", "바로추천", "됐고추천", "묻지말고추천", "스킵", "skip", "아무거나추천", "대충추천"]
    return contains_any(tc, keys)

def detect_exclude_last_intent(text: str) -> bool:
    tc = normalize_compact(text)
    keys = ["다른데", "다른곳", "방금제외", "아까제외", "그거빼고", "새로운데", "딴데", "중복말고"]
    return contains_any(tc, keys)

def parse_minutes(text: str) -> int | None:
    t = normalize_text(text)
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

# -----------------------------
# Natural language parsers (빡세게)
# -----------------------------
def parse_transport(text: str) -> str | None:
    tc = normalize_compact(text)
    if not tc:
        return None
    car_keys = ["차", "자가용", "운전", "몰고", "끌고", "주차", "발렛", "parking", "대리", "렌트", "카풀"]
    transit_keys = ["지하철", "버스", "대중", "전철", "역", "도보", "걸어", "뚜벅", "택시", "킥보드"]
    any_keys = ["상관없", "아무", "무관", "그냥"]
    if contains_any(tc, car_keys):
        return "차"
    if contains_any(tc, transit_keys):
        return "대중교통"
    if contains_any(tc, any_keys):
        return "상관없음"
    return None

def parse_alcohol_level(text: str) -> str | None:
    tc = normalize_compact(text)
    if not tc:
        return None
    none_keys = ["없음", "안마셔", "안마실", "술안", "금주", "노알콜", "노알코올", "패스", "skip", "x", "ㄴㄴ", "안함", "안먹", "안마"]
    light_keys = ["가볍", "한잔", "한잔만", "한두잔", "적당히", "살짝", "조금", "분위기만", "1잔", "2잔", "한두"]
    heavy_keys = ["술중심", "달리", "취하", "끝까지", "제대로", "진하게", "폭음", "2차", "3차", "차수", "술먹자", "한바탕"]
    if contains_any(tc, none_keys):
        return "없음"
    if contains_any(tc, heavy_keys):
        return "술 중심"
    if contains_any(tc, light_keys):
        return "가볍게"
    # 술종류만 언급해도 최소 가볍게로
    if contains_any(tc, ["소주", "맥주", "와인", "하이볼", "막걸리", "칵테일"]):
        return "가볍게"
    return None

def parse_alcohol_plan(text: str) -> str | None:
    tc = normalize_compact(text)
    if not tc:
        return None
    one_place = ["한곳", "한군데", "한군데서", "올인원", "한방에", "그자리에서", "옮기기싫", "이동없"]
    split = ["1차", "2차", "3차", "나눠", "옮겨", "이동", "코스", "바꿔", "돌아다", "2차갈", "2차가자"]
    unsure = ["모르", "미정", "상황봐", "그때가서", "일단가서"]
    if contains_any(tc, unsure):
        return "모르겠음"
    if contains_any(tc, split):
        return "1차·2차 나눌 수도"
    if contains_any(tc, one_place):
        return "한 곳"
    return None

def parse_alcohol_type(text: str) -> str | None:
    tc = normalize_compact(text)
    if not tc:
        return None
    soju = ["소주", "참이슬", "처음처럼", "진로", "새로", "소맥", "막걸리", "전통주"]
    beer = ["맥주", "비어", "beer", "호프", "크래프트", "ipa", "라거", "에일", "하이볼"]
    wine = ["와인", "wine", "내추럴", "샴페인", "비스트로"]
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

def parse_sensitivity_level(text: str) -> int | None:
    t = normalize_text(text)
    tc = normalize_compact(text)
    m = re.search(r"\b([1-4])\b", t)
    if m:
        return int(m.group(1))
    lvl4 = ["중요", "격식", "기념일", "상견례", "부모님", "접대", "모시는자리", "프러포즈"]
    lvl3 = ["좀신경", "분위기", "실패하면안", "소개팅", "썸", "데이트", "조용한데", "예쁜데"]
    lvl2 = ["무난", "적당히", "깔끔하면", "보통", "평범"]
    lvl1 = ["아무생각", "대충", "막", "캐주얼", "편하게", "걍", "아무데나"]
    if contains_any(tc, lvl4):
        return 4
    if contains_any(tc, lvl3):
        return 3
    if contains_any(tc, lvl2):
        return 2
    if contains_any(tc, lvl1):
        return 1
    return None

def parse_focus_priority(text: str) -> str | None:
    tc = normalize_compact(text)
    if not tc:
        return None
    talk = ["대화", "수다", "얘기", "토크", "이야기", "조용", "말하기", "썰", "분위기대화"]
    food = ["음식", "맛", "맛집", "메뉴", "먹는", "식도락", "배고파", "든든", "푸짐"]
    balance = ["균형", "반반", "둘다", "비슷", "상관없", "아무", "무관"]
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

def parse_dating_stage(text: str) -> str | None:
    t = normalize_text(text)
    tc = normalize_compact(text)
    # 수치 기반: "2번째", "3번 만남"
    if re.search(r"(\d+)\s*(번|번째|회|차|번만남|번째만남)", t):
        try:
            n = int(re.search(r"(\d+)", t).group(1))
            if n >= 2:
                return "익숙"
        except Exception:
            pass

    first_keys = ["처음", "첫", "첫만남", "첫만", "첫데이트", "소개팅", "썸초", "썸초기", "초반", "초기",
                  "아직어색", "어색", "낯가림", "낯설", "연락만", "톡만", "dm만", "초면", "잘몰라"]
    familiar_keys = ["익숙", "편", "편해", "편한", "친해", "가까워", "여러번", "자주", "오래", "연인", "커플", "기념일",
                     "두번째", "세번째", "n번째"]
    if "어색" in tc and ("안" in tc or "아니" in tc):
        return "익숙"
    if contains_any(tc, familiar_keys):
        return "익숙"
    if contains_any(tc, first_keys):
        return "첫/어색"
    return None

def parse_friend_style(text: str) -> str | None:
    tc = normalize_compact(text)
    talk = ["수다", "대화", "얘기", "토크", "이야기", "조용", "말많"]
    food = ["먹", "맛", "메뉴", "맛집", "식도락", "푸짐", "배고파"]
    has_talk = contains_any(tc, talk)
    has_food = contains_any(tc, food)
    if has_talk and not has_food:
        return "수다 중심"
    if has_food and not has_talk:
        return "먹는 재미 중심"
    if has_talk and has_food:
        return "수다 중심"
    return None

def parse_work_vibe(text: str) -> str | None:
    tc = normalize_compact(text)
    casual = ["가볍", "캐주얼", "편하게", "술자리", "친목", "가볍게한잔"]
    formal = ["정돈", "격식", "접대", "조용", "깔끔", "윗사람", "임원", "대표", "상사", "팀장"]
    if contains_any(tc, formal):
        return "정돈된 자리"
    if contains_any(tc, casual):
        return "가볍게"
    return None

def parse_family_member(text: str) -> str | None:
    tc = normalize_compact(text)
    both = ["둘다", "아이도", "어른도", "부모님도", "조카도", "할머니도"]
    kids = ["아이", "아기", "유아", "초등", "조카", "키즈"]
    adults = ["어른", "부모", "부모님", "할머니", "할아버지", "연세", "고령"]
    none = ["없", "없음", "해당없", "no"]
    if contains_any(tc, both):
        return "둘 다"
    has_k = contains_any(tc, kids)
    has_a = contains_any(tc, adults)
    if has_k and has_a:
        return "둘 다"
    if has_k:
        return "아이"
    if has_a:
        return "어른"
    if contains_any(tc, none):
        return "없음"
    return None


# -----------------------------
# Kakao API (paged + uniq)
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

def kakao_keyword_search_paged(query: str, kakao_rest_key: str, size: int = 15, max_pages: int = 3,
                              x: str | None = None, y: str | None = None,
                              radius: int | None = None, sort: str | None = None):
    all_docs = []
    for page in range(1, max_pages + 1):
        data = kakao_keyword_search(query, kakao_rest_key, size=size, page=page, x=x, y=y, radius=radius, sort=sort)
        docs = data.get("documents", [])
        meta = data.get("meta", {}) or {}
        all_docs.extend(docs)
        if meta.get("is_end") is True:
            break
        # 안전: 응답이 짧아지면 중단
        if len(docs) < size:
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
# Geo helpers (distance / walk)
# -----------------------------
def haversine_m(x1, y1, x2, y2):
    lon1, lat1, lon2, lat2 = map(radians, [float(x1), float(y1), float(x2), float(y2)])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return 6371000 * c

def estimate_walk_minutes(distance_m: float, speed_m_per_min: float = 80.0) -> int:
    if distance_m is None or distance_m >= 10**11:
        return 999
    return max(1, int(math.ceil(distance_m / speed_m_per_min)))

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
    # -----------------------------
# Mode-specific questions
# -----------------------------
MODE_REQUIRED_QUESTIONS = {
    "친구": [
        {"key": "friend_style", "text": "친구면 오늘은 **수다/대화** 쪽이야, 아니면 **메뉴/맛** 쪽이야? (자유롭게 말해도 됨)", "type": "enum"},
    ],
    "회사 회식": [
        {"key": "work_vibe", "text": "회식 분위기 어떤 쪽? (예: 가볍게 / 정돈된 자리·접대 느낌)", "type": "enum"},
    ],
    "연인 · 썸 · 소개팅": [
        {"key": "dating_stage", "text": "관계 단계가 어때? (예: 처음·아직 어색 / 몇 번 만남·편한 편)", "type": "enum"},
    ],
    "가족": [
        {"key": "family_member", "text": "가족 구성에 **아이/어른(연세)** 있어? (예: 아이 있음/어른 있음/둘 다/없음)", "type": "enum"},
    ],
}

SENSI_TEXT = "이 자리는 얼마나 신경 써야 해? (1 대충~ 4 중요한 자리)"
FOCUS_TEXT = "오늘은 **대화**가 더 중요해? **음식**이 더 중요해? (대화/음식/균형)"

def get_next_mode_question(conditions: dict):
    normalize_conditions(conditions)
    mode = conditions["meta"]["context_mode"]
    if not mode or mode == "선택 안 함" or mode not in MODE_REQUIRED_QUESTIONS:
        return None
    answers = conditions["meta"]["answers"]
    for q in MODE_REQUIRED_QUESTIONS[mode]:
        if answers.get(q["key"]) is None:
            return {"scope": "mode", **q}
    return None

def get_next_common_question(conditions: dict):
    normalize_conditions(conditions)
    cm = conditions["meta"]["common"]

    # 0) location
    if not conditions.get("location"):
        return {"scope": "common", "key": "location", "text": "오케이! **어느 동네/역 근처**에서 찾을까? 📍", "type": "free"}

    # 1) cannot_eat (allergy)
    if not cm.get("cannot_eat_done", False):
        return {"scope": "common", "key": "cannot_eat", "text": "못 먹는 거 있어? (알레르기/극혐 포함) 없으면 **없음** 🙅", "type": "list_or_none"}

    # fast mode: stop asking
    if conditions["meta"].get("fast_mode"):
        return None

    # 술 타입 필터가 술이면: alcohol_level 우선 질문 뜨게
    if cm.get("alcohol_level") is None:
        # place_type이 술이면 술 질문이 먼저 뜨는 게 자연스러움
        return {"scope": "common", "key": "alcohol_level", "text": "오늘 술은 어때? (예: 안 마셔/한잔/술 중심)", "type": "enum_alcohol"}

    if cm.get("transport") is None:
        return {"scope": "common", "key": "transport", "text": "이동수단은? (예: 뚜벅/지하철/택시 vs 차/주차)", "type": "enum_transport"}

    # 도보 제한: 자연어로 받되, 상관없으면 넓게
    if cm.get("walk_limit_min") is None:
        return {"scope": "common", "key": "walk_limit_min", "text": "도보는 최대 몇 분까지 괜찮아? (예: 10분/15분/상관없음)", "type": "enum_walk"}

    if cm.get("sensitivity_level") is None:
        return {"scope": "common", "key": "sensitivity_level", "text": SENSI_TEXT, "type": "enum_sensitivity"}

    if cm.get("focus_priority") is None:
        # 여기서 “대화 vs 음식” 질문으로 기획 의도 어필 포인트 살림
        return {"scope": "common", "key": "focus_priority", "text": FOCUS_TEXT, "type": "enum_focus"}

    # 술 중심이면 플랜/주종 질문
    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") is None:
        return {"scope": "common", "key": "alcohol_plan",
                "text": "술 중심이면 흐름은? (예: 한 곳에서 쭉 / 1차2차 나눔 / 모르겠음)", "type": "enum_alcohol_plan"}

    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") in ("한 곳", "1차·2차 나눌 수도") and cm.get("alcohol_type") is None:
        return {"scope": "common", "key": "alcohol_type",
                "text": "주로 뭐 마실 생각이야? (예: 소주/맥주/와인/상관없음)", "type": "enum_alcohol_type"}

    return None

def get_next_question(conditions: dict):
    q = get_next_common_question(conditions)
    if q:
        return q
    return get_next_mode_question(conditions)


# -----------------------------
# Apply answer (핵심)
# - pending 질문에 답하면서도, 한 문장에 섞인 다른 정보들도 '빈 필드만' 추가 채움
# -----------------------------
def apply_answer(conditions: dict, pending_q: dict, user_text: str) -> bool:
    normalize_conditions(conditions)
    t = user_text or ""
    tc = normalize_compact(t)
    cm = conditions["meta"]["common"]
    answers = conditions["meta"]["answers"]

    # fast intent anywhere
    if detect_fast_intent(t):
        conditions["meta"]["fast_mode"] = True
        return True

    # helper: fill extras if empty
    def fill_extras_if_empty():
        if cm.get("alcohol_level") is None:
            v = parse_alcohol_level(t)
            if v:
                cm["alcohol_level"] = v
                if v == "없음":
                    cm["alcohol_plan"] = None
                    cm["alcohol_type"] = None
        if cm.get("transport") is None:
            v = parse_transport(t)
            if v:
                cm["transport"] = v
        if cm.get("sensitivity_level") is None:
            v = parse_sensitivity_level(t)
            if v:
                cm["sensitivity_level"] = v
        if cm.get("focus_priority") is None:
            v = parse_focus_priority(t)
            if v:
                cm["focus_priority"] = v
        if cm.get("walk_limit_min") is None:
            v = parse_minutes(t)
            if v:
                cm["walk_limit_min"] = v
        if cm.get("alcohol_level") == "술 중심":
            if cm.get("alcohol_plan") is None:
                v = parse_alcohol_plan(t)
                if v:
                    cm["alcohol_plan"] = v
            if cm.get("alcohol_type") is None:
                v = parse_alcohol_type(t)
                if v:
                    cm["alcohol_type"] = v

    key = pending_q.get("key")
    qtype = pending_q.get("type")
    scope = pending_q.get("scope")

    # ----- common: location
    if scope == "common" and key == "location":
        conditions["location"] = t.strip()
        fill_extras_if_empty()
        return True

    # ----- common: cannot_eat
    if scope == "common" and key == "cannot_eat":
        if contains_any(tc, ["없", "상관없", "다먹", "아무거나", "no", "노"]):
            conditions["constraints"]["cannot_eat"] = []
        else:
            parts = re.split(r"[,\n/]+", t)
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
        fill_extras_if_empty()
        return True

    # ----- common: alcohol level
    if scope == "common" and key == "alcohol_level":
        v = parse_alcohol_level(t)
        if not v:        if not v:
            # 술종류만 말해도 "가볍게"로 최소 채움
            if contains_any(tc, ["소주", "맥주", "와인", "하이볼", "막걸리"]):
                v = "가볍게"
        if not v:
            return False
        cm["alcohol_level"] = v
        if v == "없음":
            cm["alcohol_plan"] = None
            cm["alcohol_type"] = None
        fill_extras_if_empty()
        return True

    # ----- common: transport
    if scope == "common" and key == "transport":
        v = parse_transport(t)
        if not v and contains_any(tc, ["걸어", "도보", "뚜벅"]):
            v = "대중교통"
        if not v:
            return False
        cm["transport"] = v
        # 차면 주차 필요 여부는 “강제 true”는 하지 않고, 가중치만 강화(후보 정렬에서 처리)
        fill_extras_if_empty()
        return True

    # ----- common: walk limit
    if scope == "common" and key == "walk_limit_min":
        if contains_any(tc, ["상관없", "아무", "무관"]):
            cm["walk_limit_min"] = 30  # 상관없음이면 넉넉히(데모 안정)
            fill_extras_if_empty()
            return True
        v = parse_minutes(t)
        if not v:
            return False
        cm["walk_limit_min"] = max(5, min(60, v))
        fill_extras_if_empty()
        return True

    # ----- common: sensitivity
    if scope == "common" and key == "sensitivity_level":
        v = parse_sensitivity_level(t)
        if not v:
            return False
        cm["sensitivity_level"] = v
        fill_extras_if_empty()
        return True

    # ----- common: focus priority (대화/음식/균형)
    if scope == "common" and key == "focus_priority":
        v = parse_focus_priority(t)
        if not v:
            return False
        cm["focus_priority"] = v
        fill_extras_if_empty()
        return True

    # ----- common: alcohol plan
    if scope == "common" and key == "alcohol_plan":
        v = parse_alcohol_plan(t)
        if not v:
            return False
        cm["alcohol_plan"] = v
        fill_extras_if_empty()
        return True

    # ----- common: alcohol type
    if scope == "common" and key == "alcohol_type":
        v = parse_alcohol_type(t)
        if not v:
            if contains_any(tc, ["상관없", "아무", "무관"]):
                v = "상관없음"
        if not v:
            return False
        cm["alcohol_type"] = v
        fill_extras_if_empty()
        return True

    # ----- mode scope
    if scope == "mode":
        k = key
        picked = None

        if k == "friend_style":
            picked = parse_friend_style(t)
        elif k == "work_vibe":
            picked = parse_work_vibe(t)
        elif k == "dating_stage":
            picked = parse_dating_stage(t)
        elif k == "family_member":
            picked = parse_family_member(t)

        if not picked:
            return False
        answers[k] = picked
        fill_extras_if_empty()
        return True

    # fallback: try fill extras anyway
    fill_extras_if_empty()
    return False


# -----------------------------
# Build query tokens (검색어)
# - 장소 타입/음식 분류/주종/예산/모드/대화vs음식 반영
# -----------------------------
def build_query(conditions: dict) -> str:
    normalize_conditions(conditions)
    m = conditions["meta"]
    cm = m["common"]
    tokens = []

    # location must
    if conditions.get("location"):
        tokens.append(conditions["location"])

    # place type (강제)
    pt = m.get("place_type", "자동")
    if pt == "술":
        tokens.append("술집")
    elif pt == "카페":
        tokens.append("카페")
    elif pt == "식사":
        tokens.append("맛집")

    # food class (강제)
    fc = m.get("food_class", "자동")
    if fc != "자동":
        tokens.append(fc)

    # alcohol signals: 술 중심/가볍게면 술 키워드 섞기(단, pt가 카페면 제외)
    if pt != "카페":
        if cm.get("alcohol_level") == "술 중심":
            tokens.append("술")
        elif cm.get("alcohol_level") == "가볍게" and pt == "자동":
            # 자동인데 술 가볍게면 후보에 술집도 섞이도록
            tokens.append("술집")

    # alcohol type
    at = cm.get("alcohol_type")
    if at and at != "상관없음":
        tokens.append(at)

    # focus: 대화 중심이면 조용/분위기, 음식 중심이면 맛집/메뉴
    focus = cm.get("focus_priority")
    if focus == "대화 중심":
        tokens.append("조용한")
    elif focus == "음식 중심":
        tokens.append("맛집")

    # mode hints
    mode = m.get("context_mode")
    if mode == "회사 회식":
        tokens.append("회식")
    elif mode == "단체 모임":
        tokens.append("단체")
    elif mode == "혼밥":
        tokens.append("혼밥")
    elif mode == "연인 · 썸 · 소개팅":
        tokens.append("데이트")

    return " ".join([t for t in tokens if t]).strip()


# -----------------------------
# Candidate pool building (확장 + 완화)
# -----------------------------
def get_candidate_pool(conditions: dict, kakao_key: str):
    """
    단계적 완화 로직:
    relax 0: (반경 1200m, max_pages 3)
    relax 1: (반경 2000m, max_pages 3)
    relax 2: (반경 None, max_pages 4)
    relax 3: (query 약화: location + (place_type/food_class 최소만), max_pages 4)
    """
    normalize_conditions(conditions)
    cm = conditions["meta"]["common"]
    relax = int(cm.get("search_relax", 0))

    location = conditions.get("location")
    center = get_location_center(location, kakao_key)
    if center:
        cm["center_name"] = center.get("name")

    query = build_query(conditions)
    base_pages = 3 if relax <= 1 else 4
    radius = 1200 if relax == 0 else (2000 if relax == 1 else None)

    x = center["x"] if center else None
    y = center["y"] if center else None

    # sort: distance if center exists
    sort = "distance" if center else None

    places = kakao_keyword_search_paged(
        query=query,
        kakao_rest_key=kakao_key,
        size=15,
        max_pages=base_pages,
        x=x,
        y=y,
        radius=radius,
        sort=sort,
    )

    # relax 3: weaken query if still low
    if relax >= 3 and len(places) < 10:
        weak_tokens = [location]
        pt = conditions["meta"].get("place_type", "자동")
        fc = conditions["meta"].get("food_class", "자동")
        if pt == "술":
            weak_tokens.append("술집")
        elif pt == "카페":
            weak_tokens.append("카페")
        elif pt == "식사":
            weak_tokens.append("맛집")
        if fc != "자동":
            weak_tokens.append(fc)
        weak_query = " ".join([t for t in weak_tokens if t]).strip()
        places2 = kakao_keyword_search_paged(
            query=weak_query,
            kakao_rest_key=kakao_key,
            size=15,
            max_pages=4,
            x=x,
            y=y,
            radius=None,
            sort=sort
        )
        # merge uniq
        byid = {p.get("id"): p for p in places if p.get("id")}
        for p in places2:
            pid = p.get("id")
            if pid and pid not in byid:
                byid[pid] = p
        places = list(byid.values())

    return places, center, query


# -----------------------------
# Place type filtering / mild constraints
# -----------------------------
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
    # 매우 보수적으로(오탐 줄이기) 유명 프차 일부만
    franchise_keywords = ["스타벅스", "투썸", "이디야", "메가커피", "빽다방", "홍콩반점", "교촌", "bhc", "bbq", "버거킹", "맥도날드", "kfc"]
    out = []
    for p in places:
        name = (p.get("place_name") or "")
        if any(k.lower() in name.lower() for k in franchise_keywords):
            continue
        out.append(p)
    return out if len(out) >= 8 else places


def dating_high_sensitivity_filter(places: list, conditions: dict):
    normalize_conditions(conditions)
    mode = conditions["meta"].get("context_mode")
    cm = conditions["meta"]["common"]
    s = cm.get("sensitivity_level")

    # 소개팅/썸 + 민감도 3 이상일 때만 "과한 옵션" 제거
    if mode != "연인 · 썸 · 소개팅":
        return places
    if not isinstance(s, int) or s < 3:
        return places

    banned_words = ["오마카세", "파인다이닝", "코스", "테이스팅", "한우오마카세", "프리미엄코스"]
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
        hits = ["포차", "주점", "한식주점", "소주", "막걸리", "전통주", "전", "고기", "곱창", "삼겹"]
        misses = ["펍", "브루", "브루어리", "크래프트", "와인", "와인바", "칵테일", "bar", "beer", "pub"]
    elif alcohol_type == "맥주":
        hits = ["호프", "펍", "비어", "브루", "브루어리", "크래프트", "beer", "pub", "치킨"]
        misses = ["와인", "와인바", "전통주", "막걸리", "소주", "포차", "한식주점"]
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


def prioritize_by_transport_and_alcohol(places: list, center: dict | None, conditions: dict):
    """
    - 차면 parking 시그널(이름/카테고리) 있으면 약간 가점
    - 대중교통이면 distance 우선
    - 술 중심 + 주종 있으면 match score로 정렬 보정
    """
    normalize_conditions(conditions)
    cm = conditions["meta"]["common"]
    transport = cm.get("transport")
    alcohol_type = cm.get("alcohol_type")
    walk_limit = cm.get("walk_limit_min") or 20

    def parking_signal(place: dict) -> int:
        text = f"{place.get('place_name','')} {place.get('category_name','')}".lower()
        score = 0
        if "주차" in text or "parking" in text or "발렛" in text:
            score += 3
        big_like = ["백화점", "몰", "아울렛", "호텔", "컨벤션", "대형"]
        if any(k in text for k in big_like):
            score += 1
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

        # 기본: dist
        score = dist

        # transport: 차면 parking 가점 / 대중교통이면 walk limit 반영
        if transport == "차":
            score -= parking_signal(p) * 140
        elif transport == "대중교통":
            if walk is not None and walk > walk_limit:
                score += (walk - walk_limit) * 120  # 페널티
        # alcohol type
        score -= alcohol_type_match_score(p, alcohol_type) * 180

        scored.append((score, dist, p))

    scored.sort(key=lambda x: (x[0], x[1]))
    return [p for _, __, p in scored]


def filter_exclude_last(places: list, exclude_ids: list):
    if not exclude_ids:
        return places
    ex = set(exclude_ids)
    out = [p for p in places if p.get("id") not in ex]
    return out if len(out) >= 6 else places# -----------------------------
# LLM rerank prompt (강제 반영)
# -----------------------------
def rerank_and_format(conditions: dict, places: list):
    if client is None:
        return []

    normalize_conditions(conditions)
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
        "place_type": m.get("place_type"),
        "food_class": m.get("food_class"),
        "budget_tier": m.get("budget_tier"),
        "people_count": m.get("people_count"),
        "mode": m.get("context_mode"),
        "focus": cm.get("focus_priority"),
        "alcohol_level": cm.get("alcohol_level"),
        "alcohol_type": cm.get("alcohol_type"),
        "transport": cm.get("transport"),
        "walk_limit_min": cm.get("walk_limit_min"),
        "sensitivity_level": cm.get("sensitivity_level"),
    }

    prompt = f"""
너는 '결정 메이트'다.
아래 후보 중 BEST 3곳만 고르고, 왜 이 3곳인지 "사용자 조건 기반"으로만 설명해라.

반드시 JSON으로만 출력:
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

중요 규칙:
- picks는 반드시 3개.
- place_type/food_class는 최대한 지켜라(후보가 이미 필터된 상태지만, 최종 선택도 맞춰라).
- 술 중심 + 주종 있으면 주종에 맞는 곳 우선.
- 소개팅/어색 + 민감도 높음(3~4)이면 '과한 옵션(오마카세/파인다이닝 느낌)' 지양.
- 후보 데이터에 없는 정보(주차 가능 확정/실내 좌석 간격/가격) 상상 금지.
- hashtags는 사용자 조건 중심으로, 부족하면 category로 보충(4~6개).
- "무조건/최고/완벽" 같은 표현 금지.

[사용자 조건/룰]
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
        if not pid or pid not in cand_map or pid in used:
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
            "one_line": "후보 상위에서 무난하게 맞는 곳도 같이 챙겨뒀어 😎",
            "scene_feel": "링크 눌러서 사진/리뷰만 빠르게 확인하면 감이 올 거야.",
            "hashtags": ["#근처", "#무난", "#후보추가", "#바로확인"],
            "matched_conditions": ["근처 우선"],
            "reason": "추천 결과가 부족해서 후보 풀 상위에서 안전하게 채웠어."
        })
        if len(fixed) >= 3:
            break

    return fixed[:3]


def generate_pre_text(conditions: dict, query: str):
    if client is None:
        return f"오케이ㅋㅋ **{query}**로 바로 3곳 뽑아볼게 🔍"
    prompt = f"""
너는 식당 잘 아는 친구다.
추천 시작 멘트를 1~2문장으로, 조건 반영해서 만들어라. 이모지 1개.

조건:
{json.dumps(conditions, ensure_ascii=False)}

검색어:
{query}
"""
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8
    )
    return (res.choices[0].message.content or "").strip()


# -----------------------------
# Render chat history
# -----------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("예: 홍대역 근처, 소개팅이라 조용했으면 / 예: 그냥 추천해")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})

    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        if not openai_key or not kakao_key:
            st.warning("사이드바에 OpenAI 키랑 Kakao 키부터 넣어줘!")
            st.stop()

        # pending 질문이 있으면 우선 처리
        normalize_conditions(st.session_state.conditions)

        # “방금 추천 제외 / 다른 데” 의도 처리
        exclude_last = detect_exclude_last_intent(user_input)

        pending = st.session_state.pending_question
        if pending:
            ok = apply_answer(st.session_state.conditions, pending, user_input)
            if not ok:
                # 다시 같은 질문(유도 문구만 살짝)
                msg = f"오케이 근데 내가 제대로 잡게 한 번만 더! 😅\n\n**{pending['text']}**"
                st.markdown(msg)
                st.session_state.messages.append({"role": "assistant", "content": msg})
                st.stop()
            st.session_state.pending_question = None
        else:
            # pending이 없는데 사용자가 많은 정보를 주면 '빈 필드'를 최대한 채운다
            # location이 없으면 location으로 받기
            if not st.session_state.conditions.get("location"):
                st.session_state.conditions["location"] = user_input.strip()
            else:
                # fast mode 포함해서 apply_answer가 비슷한 보조 채움을 해주지만,
                # 여기서는 pending이 없으니 common scope dummy로 처리
                dummy_q = {"scope": "common", "key": "noop", "type": "free"}
                apply_answer(st.session_state.conditions, dummy_q, user_input)

        conditions = st.session_state.conditions
        cm = conditions["meta"]["common"]

        # 디버그: 현재 조건
        if debug_mode:
            with st.expander("🧾 현재 누적 조건(JSON)"):
                st.json(conditions)

        # 다음 질문이 있으면 질문
        next_q = get_next_question(conditions)
        if next_q:
            st.session_state.pending_question = next_q
            st.markdown(next_q["text"])
            st.session_state.messages.append({"role": "assistant", "content": next_q["text"]})
            st.stop()

        # -----------------------------
        # 추천 단계
        # -----------------------------
        query = build_query(conditions)
        pre = generate_pre_text(conditions, query)
        st.markdown(pre)

        # 후보 풀 확보 + 단계 완화
        all_places, center, used_query = get_candidate_pool(conditions, kakao_key)

        # 필터 체인
        all_places = franchise_filter(all_places, conditions["constraints"].get("avoid_franchise", False))
        all_places = filter_by_place_type(all_places, conditions["meta"].get("place_type", "자동"))
        all_places = dating_high_sensitivity_filter(all_places, conditions)
        all_places = prioritize_by_transport_and_alcohol(all_places, center, conditions)

        # 방금 추천 제외
        if exclude_last:
            all_places = filter_exclude_last(all_places, st.session_state.last_picks_ids)

        # 후보 너무 적으면 완화 단계 올리고 재조회
        relax_guard = 0
        while len(all_places) < 8 and relax_guard < 3:
            cm["search_relax"] = min(3, int(cm.get("search_relax", 0)) + 1)
            all_places, center, used_query = get_candidate_pool(conditions, kakao_key)
            all_places = franchise_filter(all_places, conditions["constraints"].get("avoid_franchise", False))
            all_places = filter_by_place_type(all_places, conditions["meta"].get("place_type", "자동"))
            all_places = dating_high_sensitivity_filter(all_places, conditions)
            all_places = prioritize_by_transport_and_alcohol(all_places, center, conditions)
            if exclude_last:
                all_places = filter_exclude_last(all_places, st.session_state.last_picks_ids)
            relax_guard += 1

        if debug_mode:
            with st.expander("🧪 후보 풀(상위 25)"):
                st.write(f"query: {used_query}")
                st.write(f"candidates: {len(all_places)} / relax: {cm.get('search_relax')}")
                for p in all_places[:25]:
                    st.write(f"- {p.get('place_name')} | {p.get('category_name')} | {p.get('road_address_name') or p.get('address_name')}")

        if not all_places:
            msg = "헉… 이 조건으로는 딱 맞는 데가 잘 안 잡히네 🥲\n지역을 조금만 넓혀볼까?"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        # rerank
        picks = rerank_and_format(conditions, all_places)

        if debug_mode:
            with st.expander("🤖 (디버그) rerank LLM 원문"):
                st.code(st.session_state.debug_raw_rerank)

        # 3개 보장
        picks = ensure_3_picks(picks, all_places)

        # 렌더링
        kakao_map = {p.get("id"): p for p in all_places if p.get("id")}

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

                st.markdown(f"**{pick.get('# -----------------------------
# LLM rerank prompt (강제 반영)
# -----------------------------
def rerank_and_format(conditions: dict, places: list):
    if client is None:
        return []

    normalize_conditions(conditions)
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
        "place_type": m.get("place_type"),
        "food_class": m.get("food_class"),
        "budget_tier": m.get("budget_tier"),
        "people_count": m.get("people_count"),
        "mode": m.get("context_mode"),
        "focus": cm.get("focus_priority"),
        "alcohol_level": cm.get("alcohol_level"),
        "alcohol_type": cm.get("alcohol_type"),
        "transport": cm.get("transport"),
        "walk_limit_min": cm.get("walk_limit_min"),
        "sensitivity_level": cm.get("sensitivity_level"),
    }

    prompt = f"""
너는 '결정 메이트'다.
아래 후보 중 BEST 3곳만 고르고, 왜 이 3곳인지 "사용자 조건 기반"으로만 설명해라.

반드시 JSON으로만 출력:
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

중요 규칙:
- picks는 반드시 3개.
- place_type/food_class는 최대한 지켜라(후보가 이미 필터된 상태지만, 최종 선택도 맞춰라).
- 술 중심 + 주종 있으면 주종에 맞는 곳 우선.
- 소개팅/어색 + 민감도 높음(3~4)이면 '과한 옵션(오마카세/파인다이닝 느낌)' 지양.
- 후보 데이터에 없는 정보(주차 가능 확정/실내 좌석 간격/가격) 상상 금지.
- hashtags는 사용자 조건 중심으로, 부족하면 category로 보충(4~6개).
- "무조건/최고/완벽" 같은 표현 금지.

[사용자 조건/룰]
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
        if not pid or pid not in cand_map or pid in used:
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
            "one_line": "후보 상위에서 무난하게 맞는 곳도 같이 챙겨뒀어 😎",
            "scene_feel": "링크 눌러서 사진/리뷰만 빠르게 확인하면 감이 올 거야.",
            "hashtags": ["#근처", "#무난", "#후보추가", "#바로확인"],
            "matched_conditions": ["근처 우선"],
            "reason": "추천 결과가 부족해서 후보 풀 상위에서 안전하게 채웠어."
        })
        if len(fixed) >= 3:
            break

    return fixed[:3]


def generate_pre_text(conditions: dict, query: str):
    if client is None:
        return f"오케이ㅋㅋ **{query}**로 바로 3곳 뽑아볼게 🔍"
    prompt = f"""
너는 식당 잘 아는 친구다.
추천 시작 멘트를 1~2문장으로, 조건 반영해서 만들어라. 이모지 1개.

조건:
{json.dumps(conditions, ensure_ascii=False)}

검색어:
{query}
"""
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8
    )
    return (res.choices[0].message.content or "").strip()


# -----------------------------
# Render chat history
# -----------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("예: 홍대역 근처, 소개팅이라 조용했으면 / 예: 그냥 추천해")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})

    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        if not openai_key or not kakao_key:
            st.warning("사이드바에 OpenAI 키랑 Kakao 키부터 넣어줘!")
            st.stop()

        # pending 질문이 있으면 우선 처리
        normalize_conditions(st.session_state.conditions)

        # “방금 추천 제외 / 다른 데” 의도 처리
        exclude_last = detect_exclude_last_intent(user_input)

        pending = st.session_state.pending_question
        if pending:
            ok = apply_answer(st.session_state.conditions, pending, user_input)
            if not ok:
                # 다시 같은 질문(유도 문구만 살짝)
                msg = f"오케이 근데 내가 제대로 잡게 한 번만 더! 😅\n\n**{pending['text']}**"
                st.markdown(msg)
                st.session_state.messages.append({"role": "assistant", "content": msg})
                st.stop()
            st.session_state.pending_question = None
        else:
            # pending이 없는데 사용자가 많은 정보를 주면 '빈 필드'를 최대한 채운다
            # location이 없으면 location으로 받기
            if not st.session_state.conditions.get("location"):
                st.session_state.conditions["location"] = user_input.strip()
            else:
                # fast mode 포함해서 apply_answer가 비슷한 보조 채움을 해주지만,
                # 여기서는 pending이 없으니 common scope dummy로 처리
                dummy_q = {"scope": "common", "key": "noop", "type": "free"}
                apply_answer(st.session_state.conditions, dummy_q, user_input)

        conditions = st.session_state.conditions
        cm = conditions["meta"]["common"]

        # 디버그: 현재 조건
        if debug_mode:
            with st.expander("🧾 현재 누적 조건(JSON)"):
                st.json(conditions)

        # 다음 질문이 있으면 질문
        next_q = get_next_question(conditions)
        if next_q:
            st.session_state.pending_question = next_q
            st.markdown(next_q["text"])
            st.session_state.messages.append({"role": "assistant", "content": next_q["text"]})
            st.stop()

        # -----------------------------
        # 추천 단계
        # -----------------------------
        query = build_query(conditions)
        pre = generate_pre_text(conditions, query)
        st.markdown(pre)

        # 후보 풀 확보 + 단계 완화
        all_places, center, used_query = get_candidate_pool(conditions, kakao_key)

        # 필터 체인
        all_places = franchise_filter(all_places, conditions["constraints"].get("avoid_franchise", False))
        all_places = filter_by_place_type(all_places, conditions["meta"].get("place_type", "자동"))
        all_places = dating_high_sensitivity_filter(all_places, conditions)
        all_places = prioritize_by_transport_and_alcohol(all_places, center, conditions)

        # 방금 추천 제외
        if exclude_last:
            all_places = filter_exclude_last(all_places, st.session_state.last_picks_ids)

        # 후보 너무 적으면 완화 단계 올리고 재조회
        relax_guard = 0
        while len(all_places) < 8 and relax_guard < 3:
            cm["search_relax"] = min(3, int(cm.get("search_relax", 0)) + 1)
            all_places, center, used_query = get_candidate_pool(conditions, kakao_key)
            all_places = franchise_filter(all_places, conditions["constraints"].get("avoid_franchise", False))
            all_places = filter_by_place_type(all_places, conditions["meta"].get("place_type", "자동"))
            all_places = dating_high_sensitivity_filter(all_places, conditions)
            all_places = prioritize_by_transport_and_alcohol(all_places, center, conditions)
            if exclude_last:
                all_places = filter_exclude_last(all_places, st.session_state.last_picks_ids)
            relax_guard += 1

        if debug_mode:
            with st.expander("🧪 후보 풀(상위 25)"):
                st.write(f"query: {used_query}")
                st.write(f"candidates: {len(all_places)} / relax: {cm.get('search_relax')}")
                for p in all_places[:25]:
                    st.write(f"- {p.get('place_name')} | {p.get('category_name')} | {p.get('road_address_name') or p.get('address_name')}")

        if not all_places:
            msg = "헉… 이 조건으로는 딱 맞는 데가 잘 안 잡히네 🥲\n지역을 조금만 넓혀볼까?"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        # rerank
        picks = rerank_and_format(conditions, all_places)

        if debug_mode:
            with st.expander("🤖 (디버그) rerank LLM 원문"):
                st.code(st.session_state.debug_raw_rerank)

        # 3개 보장
        picks = ensure_3_picks(picks, all_places)

        # 렌더링
        kakao_map = {p.get("id"): p for p in all_places if p.get("id")}

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

                st.markdown(f"**{pick.get('                st.markdown(f"**{pick.get('one_line','')}**")

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

                # 도보 시간 표시 (있으면)
                if center and center.get("x") and center.get("y") and place.get("x") and place.get("y"):
                    try:
                        dist = haversine_m(center["x"], center["y"], place["x"], place["y"])
                        walk_min = estimate_walk_minutes(dist)
                        st.caption(f"🚶 예상 도보 약 {walk_min}분")
                    except Exception:
                        pass

                if url:
                    st.link_button("카카오맵에서 보기", url)

        # 다음 추천에서 제외 저장
        st.session_state.last_picks_ids = current_pick_ids

        final = "끝! 😎\n셋 중에 하나 고르거나, '다른 데', '더 조용한 데', '완전 다른 분위기' 이런 식으로 다시 시켜도 돼."
        st.session_state.messages.append({"role": "assistant", "content": final})
        st.markdown(final)
