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

# 누적 조건(핵심)
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
        }
    }

# 마지막 추천했던 place id들(다음 추천에서 제외)
if "last_picks_ids" not in st.session_state:
    st.session_state.last_picks_ids = []

# 디버그용 raw 저장
if "debug_raw_patch" not in st.session_state:
    st.session_state.debug_raw_patch = ""

if "debug_raw_rerank" not in st.session_state:
    st.session_state.debug_raw_rerank = ""

# -----------------------------
# Helpers: robust JSON parsing
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
    """
    조건 dict 구조를 항상 안정적으로 유지하기 위한 방어.
    """
    if not isinstance(cond, dict):
        return

    if "constraints" not in cond or not isinstance(cond["constraints"], dict):
        cond["constraints"] = {
            "cannot_eat": [],
            "avoid_recent": [],
            "need_parking": None
        }

    c = cond["constraints"]

    if "cannot_eat" not in c or not isinstance(c["cannot_eat"], list):
        c["cannot_eat"] = []
    if "avoid_recent" not in c or not isinstance(c["avoid_recent"], list):
        c["avoid_recent"] = []
    if "need_parking" not in c:
        c["need_parking"] = None

def merge_conditions(base: dict, patch: dict):
    """
    patch는 '변경된 값만' 들어있는 dict.
    None은 덮어쓰기하지 않음(= 언급 안 된 것으로 처리)
    리스트는 덮어쓰기(사용자가 '해산물 빼줘'처럼 바꾼 케이스)
    """
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

    # top-level merge
    for k, v in patch.items():
        if k == "constraints":
            continue
        if v is None:
            continue
        base[k] = v

    normalize_conditions(base)
    return base

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
    """
    대화 전체를 다시 읽게 하지 않고,
    '지금 발화'에서 바뀐 값만 뽑는다.
    """
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
# 2) 부족한 정보 질문 (친구톤)
# -----------------------------
def is_ready_to_recommend(conditions: dict):
    """
    기존 ready_to_recommend 기준을 코드로 안정적으로 구현.
    """
    if not conditions.get("location"):
        return False
    if conditions.get("food_type") or conditions.get("mood"):
        return True
    return False

def generate_followup_question(conditions):
    if client is None:
        return "지역이랑 먹고 싶은 거만 말해줘! 내가 바로 찾아줄게 😎"

    prompt = f"""
너는 '식당 잘 아는 친구' 톤으로 말한다.
사용자 조건이 부족할 때, 자연스럽게 추가 질문을 1~2문장으로 해라.
너무 정중하지 말고, 약간 장난스러운 느낌도 OK.
이모지 1~2개 허용.

현재 조건:
{json.dumps(conditions, ensure_ascii=False, indent=2)}

질문 후보:
- 음식 종류 (한식/양식/중식/일식/술집)
- 분위기 (조용/시끌/데이트/가성비)
- 인원
- 주차 필요 여부
- 못 먹는 음식
"""

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.85
    )
    return (res.choices[0].message.content or "").strip()

# -----------------------------
# 3) Kakao 검색어 만들기
# -----------------------------
def build_query(conditions):
    tokens = []
    if conditions.get("location"):
        tokens.append(conditions["location"])

    # 음식종류 우선, 없으면 분위기
    if conditions.get("food_type"):
        tokens.append(conditions["food_type"])
    elif conditions.get("mood"):
        tokens.append(conditions["mood"])
    else:
        tokens.append("맛집")

    return " ".join(tokens).strip()

# -----------------------------
# 4) 후보 필터링(방금 추천 제외)
# -----------------------------
def filter_places(places, exclude_ids):
    if not exclude_ids:
        return places
    return [p for p in places if p.get("id") not in set(exclude_ids)]

# -----------------------------
# 5) 후보 -> BEST3 재랭킹 + 근거 생성 (안정화 버전)
# -----------------------------
def rerank_and_format(conditions, places):
    if client is None:
        return []

    compact = []
    for p in places[:15]:
        compact.append({
            "id": p.get("id"),
            "name": p.get("place_name"),
            "category": p.get("category_name"),
            "address": p.get("road_address_name") or p.get("address_name"),
            "url": p.get("place_url"),
        })

    prompt = f"""
너는 '결정 메이트'다.
사용자 조건에 맞춰 아래 후보 중 BEST 3곳만 골라라.

반드시 아래 JSON 형식으로만 출력해라:
{{
  "picks": [
    {{
      "id": "...",
      "one_line": "짧은 한줄 소개 (친구톤)",
      "hashtags": ["#...","#..."],
      "matched_conditions": ["사용자 조건 중 실제로 반영한 것"],
      "reason": "왜 추천인지 2~3문장"
    }}
  ]
}}

중요 규칙:
- matched_conditions는 '사용자가 말한 조건'에서만 뽑아라.
- hashtags는 사용자 조건 기반으로 먼저 만들고, 부족하면 category로 보충.
- 해시태그는 4~6개
- 과장 금지 ('무조건', '최고', '완벽' 금지)
- 후보 데이터 기반으로만 말하기 (없는 정보 상상 금지)
- picks는 반드시 3개만

[사용자 조건]
{json.dumps(conditions, ensure_ascii=False, indent=2)}

[후보 목록]
{json.dumps(compact, ensure_ascii=False, indent=2)}
"""

    def call_llm(extra_msg=None, temp=0.3):
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
        res2 = call_llm(
            extra_msg="방금 출력이 스키마를 안 지켰어. JSON만 다시 출력해.",
            temp=0.1
        )
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
# 6) 추천 시작 멘트 생성
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

        # -----------------------------
        # (핵심) 최신 발화에서 PATCH 추출 → 조건 merge
        # -----------------------------
        patch = extract_conditions_patch(user_input, st.session_state.conditions)

        diversify = bool(patch.pop("diversify", False))
        exclude_last = bool(patch.pop("exclude_last", False))

        st.session_state.conditions = merge_conditions(st.session_state.conditions, patch)
        conditions = st.session_state.conditions

        # -----------------------------
        # 디버그 출력
        # -----------------------------
        with st.expander("🧾 현재 누적 조건(JSON)"):
            st.json(conditions)
            if debug_mode:
                st.markdown("**(디버그) patch 원문**")
                st.code(st.session_state.debug_raw_patch)

        # -----------------------------
        # 조건 부족하면 follow-up
        # -----------------------------
        if not is_ready_to_recommend(conditions):
            q = generate_followup_question(conditions)
            st.markdown(q)
            st.session_state.messages.append({"role": "assistant", "content": q})
            st.stop()

        # -----------------------------
        # Kakao 검색
        # -----------------------------
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

        # -----------------------------
        # (핵심) '다른 데 추천해줘' 요청 처리
        # -----------------------------
        if diversify or exclude_last:
            places = filter_places(places, st.session_state.last_picks_ids)

        # 후보가 너무 줄어들면 안전장치: 제외 풀기
        if len(places) < 6:
            # 너무 적으면 다시 전체 후보로
            places = kakao_keyword_search(query, kakao_key, size=15)

        # -----------------------------
        # rerank
        # -----------------------------
        picks = rerank_and_format(conditions, places)

        if debug_mode:
            with st.expander("🤖 (디버그) rerank LLM 원문"):
                st.code(st.session_state.debug_raw_rerank)

        if not picks:
            msg = "후보는 찾았는데… 정리하다가 살짝 꼬였어 😅\n한 번만 더 말해줄래?"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        # -----------------------------
        # 렌더링
        # -----------------------------
        kakao_map = {p.get("id"): p for p in places}

        st.markdown("---")
        st.subheader("🍽️ 딱 3곳만 골랐어")

        cols = st.columns(3)

        # 이번 추천 id 저장(다음에 제외하기 위해)
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

                st.markdown(f"### {i+1}. {name}")
                st.caption(category or "")
                st.write(f"📍 {addr}")

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

        # 다음 추천에서 제외할 수 있도록 저장
        st.session_state.last_picks_ids = current_pick_ids

        final = "끝! 😎\n셋 중에 하나 고르거나, '더 조용한 데', '주차 되는 데', '완전 다른 스타일' 이런 식으로 다시 시켜도 돼."
        st.session_state.messages.append({"role": "assistant", "content": final})
        st.markdown(final)
