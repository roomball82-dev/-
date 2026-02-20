import json
import re
import requests
import streamlit as st
from openai import OpenAI

st.set_page_config(page_title="결정 메이트", page_icon="🍽️", layout="wide")
st.title("🍽️ 결정 메이트 (Decision Mate)")
st.caption("식당 잘 아는 친구처럼, 대화로 조건을 정리하고 3곳만 딱 추천해주는 챗봇")

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
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": "오케이 😎\n오늘 어디서 누구랑 뭐 먹을지 내가 딱 정해줄게.\n일단 **어느 동네 근처**에서 찾을까?"
        }
    ]

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
            "context_mode": None,       # 회사 회식 / 친구 / 단체 모임 / 연인 · 썸 · 소개팅 / 혼밥 / 가족 / None
            "people_count": None,       # int
            "budget_tier": "상관없음",  # 가성비 / 보통 / 조금 특별 / 상관없음
            "answers": {},              # 모드/추가 질문 답 저장
            "common": {                 # 공통 질문 답 저장
                "cannot_eat_done": False,   # True/False (없음이라도 질문 1회 완료)
                "alcohol_level": None,      # 없음 / 가볍게 / 술 중심
                "stay_duration": None,      # 빠르게 / 적당히 / 오래
                "transport": None,          # 차 / 대중교통 / 상관없음
                "alcohol_plan": None,       # (술 중심일 때만) 한 곳 / 나눌 수도 / 모르겠음
                "alcohol_type": None,       # (필요 시) 소주/맥주/와인/상관없음
            },
            "fast_mode": False          # "그냥 추천해" 등 스킵 의도
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
        cond["meta"] = {
            "context_mode": None,
            "people_count": None,
            "budget_tier": "상관없음",
            "answers": {},
            "common": {
                "cannot_eat_done": False,
                "alcohol_level": None,
                "stay_duration": None,
                "transport": None,
                "alcohol_plan": None,
                "alcohol_type": None,
            },
            "fast_mode": False
        }

    m = cond["meta"]
    if "context_mode" not in m:
        m["context_mode"] = None
    if "people_count" not in m:
        m["people_count"] = None
    if "budget_tier" not in m:
        m["budget_tier"] = "상관없음"
    if "answers" not in m or not isinstance(m["answers"], dict):
        m["answers"] = {}
    if "common" not in m or not isinstance(m["common"], dict):
        m["common"] = {
            "cannot_eat_done": False,
            "alcohol_level": None,
            "stay_duration": None,
            "transport": None,
            "alcohol_plan": None,
            "alcohol_type": None,
        }
    if "fast_mode" not in m:
        m["fast_mode"] = False

    cm = m["common"]
    for k in ["cannot_eat_done", "alcohol_level", "stay_duration", "transport", "alcohol_plan", "alcohol_type"]:
        if k not in cm:
            cm[k] = False if k == "cannot_eat_done" else None

def merge_conditions(base: dict, patch: dict):
    if not isinstance(patch, dict):
        return base

    # constraints merge
    if "constraints" in patch and isinstance(patch["constraints"], dict):
        base_constraints = base.get("constraints", {}) or {}
        for k, v in patch["constraints"].items():
            if v is None:
                continue
            base_constraints[k] = v
        base["constraints"] = base_constraints

    # meta merge (부분 업데이트만 허용)
    if "meta" in patch and isinstance(patch["meta"], dict):
        base_meta = base.get("meta", {}) or {}
        for k, v in patch["meta"].items():
            if v is None:
                continue
            base_meta[k] = v
        base["meta"] = base_meta

    # top-level merge
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
# Kakao API
# -----------------------------
def kakao_keyword_search(query: str, kakao_rest_key: str, size: int = 15):
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {kakao_rest_key}"}
    params = {"query": query, "size": size}
    res = requests.get(url, headers=headers, params=params, timeout=10)
    res.raise_for_status()
    return res.json().get("documents", [])

# -----------------------------
# 1) 최신 발화 -> 조건 PATCH 추출(JSON)
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

PATCH 스키마 예시:
{
  "location": "합정",
  "mood": "조용한",
  "constraints": {
    "need_parking": true,
    "cannot_eat": ["해산물"]
  },
  "diversify": true,
  "exclude_last": true
}

가능한 필드:
- location, food_type, purpose, people, mood
- constraints.cannot_eat (list[str])
- constraints.avoid_recent (list[str])
- constraints.need_parking (true/false)
- diversify (true/false)
- exclude_last (true/false)
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
# 질문 트리: 공통 + 모드
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

    # 0) 위치 없으면 항상 먼저
    if not conditions.get("location"):
        return {"scope": "common", "key": "location", "text": "오케이! **어느 동네 근처**에서 찾을까? 📍", "type": "free"}

    # 1) 못 먹는 것 (1회 필수)
    if not cm.get("cannot_eat_done", False):
        return {"scope": "common", "key": "cannot_eat", "text": "못 먹는 거 있어? (알레르기/극혐 포함) 없으면 **없음**이라고 해줘 🙅", "type": "list_or_none"}

    # 스킵이면 여기서 공통 질문 중단
    if conditions["meta"].get("fast_mode"):
        return None

    # 2) 술 여부
    if cm.get("alcohol_level") is None:
        return {"scope": "common", "key": "alcohol_level", "text": "오늘 술은 어때? **없음 / 가볍게 / 술 중심** 🍻", "type": "enum_alcohol"}

    # 3) 체류 시간
    if cm.get("stay_duration") is None:
        return {"scope": "common", "key": "stay_duration", "text": "얼마나 있을 거야? **빠르게 / 적당히 / 오래** ⏱️", "type": "enum_stay"}

    # 4) 이동수단
    if cm.get("transport") is None:
        return {"scope": "common", "key": "transport", "text": "이동수단은 뭐야? **차 / 대중교통 / 상관없음** 🧭", "type": "enum_transport"}

    # 5) 술 중심이면 (조건부) 1차/2차 의향
    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") is None:
        return {
            "scope": "common",
            "key": "alcohol_plan",
            "text": "오케이 술 중심 👍 한 곳에서 쭉 갈 거야, 아니면 **1차·2차 나눌 수도** 있어? (**한 곳 / 나눌 수도 / 모르겠음**)",
            "type": "enum_alcohol_plan"
        }

    # 6) 술 중심 + 나눌 수도(or 한 곳)일 때만 주종
    if cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") in ("한 곳", "나눌 수도") and cm.get("alcohol_type") is None:
        return {
            "scope": "common",
            "key": "alcohol_type",
            "text": "주로 뭐 마실 생각이야? **소주 / 맥주 / 와인 / 상관없음** 🍶",
            "type": "enum_alcohol_type"
        }

    return None

def get_next_question(conditions: dict):
    # 공통 먼저, 그 다음 모드
    q = get_next_common_question(conditions)
    if q:
        return q
    return get_next_mode_question(conditions)

# -----------------------------
# 답변 파싱 & 저장
# -----------------------------
def parse_list_or_none(text: str):
    t = (text or "").strip()
    if not t:
        return None
    if "없" in t:
        return []
    # 쉼표/슬래시/공백 기반 분리
    parts = re.split(r"[,\n/]+", t)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 조사/불필요 단어 조금 제거
        p = re.sub(r"(은|는|이|가|을|를|만|빼고|빼줘)$", "", p).strip()
        if p and p not in out:
            out.append(p)
    return out[:6]

def apply_answer(conditions: dict, pending_q: dict, user_text: str) -> bool:
    normalize_conditions(conditions)
    t = (user_text or "").strip()
    cm = conditions["meta"]["common"]
    answers = conditions["meta"]["answers"]

    key = pending_q.get("key")
    qtype = pending_q.get("type")

    # location
    if key == "location":
        # 사용자가 동네를 말했으면 그대로 저장 (LLM patch도 같이 돌지만, 최소 방어)
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
        if "가볍" in t or "한두" in t or "1" in t:
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
        if "나눌" in t or "1" in t or "2" in t:
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

    # mode enum (룰 기반 간단 저장)
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
# Kakao 검색어 만들기 (카페/술집 포함)
# -----------------------------
def build_query(conditions):
    normalize_conditions(conditions)
    tokens = []
    loc = conditions.get("location")
    if loc:
        tokens.append(loc)

    mode = conditions["meta"].get("context_mode")
    budget = conditions["meta"].get("budget_tier")
    cm = conditions["meta"]["common"]

    alcohol = cm.get("alcohol_level")
    stay = cm.get("stay_duration")
    transport = cm.get("transport")
    alcohol_type = cm.get("alcohol_type")

    # 장소 타입 토큰(가장 중요)
    if alcohol in ("가볍게", "술 중심"):
        # 술 중심이면 주종에 따라 조금 더 구체화
        if alcohol_type == "와인":
            tokens.append("와인바")
        elif alcohol_type == "맥주":
            tokens.append("펍")
        elif alcohol_type == "소주":
            tokens.append("술집")
        else:
            tokens.append("술집")
    else:
        # 술 없음
        if stay == "오래":
            tokens.append("카페")
        elif stay == "빠르게":
            tokens.append("식사")
        else:
            tokens.append("맛집")

    # 모드에 따른 보조 토큰 (과하지 않게)
    if mode == "회사 회식":
        tokens.append("회식")
    elif mode == "가족":
        tokens.append("가족식사")
    elif mode == "연인 · 썸 · 소개팅":
        tokens.append("데이트")
    elif mode == "단체 모임":
        tokens.append("단체")

    # 예산대는 검색어에 과하게 넣으면 잡음이 늘어서 v1은 최소만
    if budget == "가성비":
        tokens.append("가성비")

    # 교통은 키워드로 넣으면 잡음이 커서 v1은 프롬프트에서 처리(거리 데이터 없어서)
    return " ".join([t for t in tokens if t]).strip()

# -----------------------------
# 후보 필터링(방금 추천 제외)
# -----------------------------
def filter_places(places, exclude_ids):
    if not exclude_ids:
        return places
    return [p for p in places if p.get("id") not in set(exclude_ids)]

# -----------------------------
# BEST3 재랭킹 + 근거 생성 (scene_feel 포함)
# + 술 중심 & 1/2차 분리 지원(조건부)
# -----------------------------
def rerank_and_format(conditions, places):
    if client is None:
        return []

    normalize_conditions(conditions)

    compact = []
    for p in places[:15]:
        compact.append({
            "id": p.get("id"),
            "name": p.get("place_name"),
            "category": p.get("category_name"),
            "address": p.get("road_address_name") or p.get("address_name"),
            "url": p.get("place_url"),
        })

    cm = conditions["meta"]["common"]
    split_12 = (cm.get("alcohol_level") == "술 중심" and cm.get("alcohol_plan") == "나눌 수도")

    schema_hint = """
반드시 아래 JSON 형식으로만 출력해라:
{
  "picks": [
    {
      "id": "...",
      "scene_feel": "여기서 약속하면 어떤 느낌인지 2~3문장(분석 디테일 금지, 체감 중심)",
      "one_line": "짧은 한줄 소개 (친구톤)",
      "hashtags": ["#...","#..."],
      "matched_conditions": ["사용자 조건 중 실제로 반영한 것"],
      "reason": "왜 추천인지 2~3문장(후보 데이터 기반, 과장 금지)"
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
- scene_feel은 "자리 배치/조명/동선" 같은 디테일 설명하지 말고, "체감"만 2~3문장으로.

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
# 추천 시작 멘트 생성
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
# Chat UI
# -----------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("예: 홍대 근처에서 3명이 가볍게 술 마실 곳")

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

        # 0) 스킵 의도 처리 ("그냥 추천해" 등)
        if detect_skip_intent(user_input):
            conditions["meta"]["fast_mode"] = True

        # 1) pending question이 있으면 먼저 답변 적용 시도
        if st.session_state.pending_question is not None:
            ok = apply_answer(conditions, st.session_state.pending_question, user_input)
            if ok:
                st.session_state.pending_question = None  # 질문 해결
            # 답변이었어도, 사용자가 동시에 location/조건을 말했을 수 있으니 patch도 같이 돌림

        # 2) PATCH 추출 → merge (조건 업데이트)
        patch = extract_conditions_patch(user_input, conditions)
        diversify = bool(patch.pop("diversify", False))
        exclude_last = bool(patch.pop("exclude_last", False))
        st.session_state.conditions = merge_conditions(conditions, patch)
        conditions = st.session_state.conditions

        # 디버그 출력
        with st.expander("🧾 현재 누적 조건(JSON)"):
            st.json(conditions)
            if debug_mode:
                st.markdown("**(디버그) patch 원문**")
                st.code(st.session_state.debug_raw_patch)

        # 3) 다음 질문이 있으면(대화형) 먼저 질문
        next_q = get_next_question(conditions)

        # 스킵 모드라도 location 없으면 location은 물어야 함
        if next_q and not (conditions["meta"].get("fast_mode") and next_q.get("key") != "location" and next_q.get("key") != "cannot_eat"):
            # 다음 질문 출력
            st.markdown(next_q["text"])
            st.session_state.messages.append({"role": "assistant", "content": next_q["text"]})
            st.session_state.pending_question = next_q
            st.stop()

        # 4) 추천 진행 준비: location 없으면 안전하게 재질문
        if not conditions.get("location"):
            msg = "좋아! 근데 **동네**부터 알려줘야 내가 뽑아주지 😎\n예: `합정`, `연남동`, `강남역`"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.session_state.pending_question = {"scope": "common", "key": "location", "text": msg, "type": "free"}
            st.stop()

        # 5) Kakao 검색
        query = build_query(conditions)
        pre_text = generate_pre_recommend_text(conditions, query)
        st.markdown(pre_text)

        try:
            places = kakao_keyword_search(query, kakao_key, size=15)
        except Exception as e:
            st.error(f"Kakao 검색 중 오류: {e}")
            st.stop()

        if not places:
            msg = "헉… 이 조건으로는 딱 맞는 데가 잘 안 잡히네 🥲\n지역을 조금만 넓혀볼까?"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        # 6) '다른 데 추천해줘' 처리
        if diversify or exclude_last:
            places = filter_places(places, st.session_state.last_picks_ids)

        if len(places) < 6:
            places = kakao_keyword_search(query, kakao_key, size=15)

        # 7) rerank
        picks = rerank_and_format(conditions, places)

        if debug_mode:
            with st.expander("🤖 (디버그) rerank LLM 원문"):
                st.code(st.session_state.debug_raw_rerank)

        if not picks:
            msg = "후보는 찾았는데… 정리하다가 살짝 꼬였어 😅\n한 번만 더 말해줄래?"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        # 8) 렌더링
        kakao_map = {p.get("id"): p for p in places}

        st.markdown("---")
        st.subheader("🍽️ 딱 3곳만 골랐어")

        cols = st.columns(3)
        current_pick_ids = []

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

                # ✅ 고정 노출: 자리 느낌
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

        final = "끝! 😎\n셋 중에 하나 고르거나, '더 조용한 데', '주차 되는 데', '완전 다른 스타일' 이런 식으로 다시 시켜도 돼."
        st.session_state.messages.append({"role": "assistant", "content": final})
        st.markdown(final)
