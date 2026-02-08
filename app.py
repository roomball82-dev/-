import json
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

if "last_conditions" not in st.session_state:
    st.session_state.last_conditions = {}

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
# 1) 대화 -> 조건 추출(JSON)
# -----------------------------
def extract_conditions(messages):
    if client is None:
        return {}

    system = """
너는 '결정 메이트'의 분석 엔진이다.
대화 전체를 보고 식당 추천에 필요한 조건을 JSON으로 추출해라.
반드시 JSON만 출력해라.

스키마:
{
  "location": "지역명 또는 null",
  "food_type": "음식 종류 또는 null",
  "purpose": "목적 또는 null",
  "people": "인원(숫자) 또는 null",
  "mood": "분위기 또는 null",
  "constraints": {
    "cannot_eat": ["못 먹는 음식"],
    "avoid_recent": ["최근 먹어서 피하고 싶은 음식"],
    "need_parking": true/false/null
  },
  "ready_to_recommend": true/false
}

ready_to_recommend 기준:
- location이 있고,
- food_type 또는 mood 중 하나라도 있으면 true
"""

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(messages, ensure_ascii=False)}
        ],
        temperature=0.2
    )

    try:
        return json.loads(res.choices[0].message.content.strip())
    except:
        return {}

# -----------------------------
# 2) 부족한 정보 질문 (친구톤)
# -----------------------------
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

질문은 아래 중에서 상황에 맞게 골라서 섞어라:
- 목적(데이트/회식/친구모임)
- 인원
- 주차 필요 여부
- 못 먹는 음식
- 조용한지/시끌벅적한지
"""

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.85
    )
    return res.choices[0].message.content.strip()

# -----------------------------
# 3) Kakao 검색어 만들기
# -----------------------------
def build_query(conditions):
    tokens = []
    if conditions.get("location"):
        tokens.append(conditions["location"])

    if conditions.get("food_type"):
        tokens.append(conditions["food_type"])
    elif conditions.get("mood"):
        tokens.append(conditions["mood"])
    else:
        tokens.append("맛집")

    return " ".join(tokens).strip()

# -----------------------------
# 4) 후보 -> BEST3 재랭킹 + 키워드/근거 생성
# -----------------------------
def rerank_and_format(conditions, places):
    if client is None:
        return []

    # LLM에 넘길 후보를 간단히 줄임
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
[
  {{
    "id": "...",
    "one_line": "짧은 한줄 소개 (친구톤)",
    "hashtags": ["#...","#..."],
    "matched_conditions": ["사용자 조건 중 실제로 반영한 것"],
    "reason": "왜 추천인지 2~3문장"
  }}
]

중요 규칙:
- matched_conditions는 '사용자가 말한 조건'에서만 뽑아라.
  (예: '홍대', '3명', '가볍게 술', '해산물 제외', '데이트')
- hashtags도 사용자 조건 기반으로 먼저 만들고, 부족하면 category로 보충해라.
- 해시태그는 4~6개
- 과장 금지 ('무조건', '최고', '완벽' 금지)
- 후보 데이터 기반으로만 말하기 (없는 정보 상상 금지)

[사용자 조건]
{json.dumps(conditions, ensure_ascii=False, indent=2)}

[후보 목록]
{json.dumps(compact, ensure_ascii=False, indent=2)}
"""

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.45
    )

    try:
        return json.loads(res.choices[0].message.content.strip())
    except:
        return []

# -----------------------------
# 5) 추천 시작 멘트 생성
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
    return res.choices[0].message.content.strip()

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

        # 1) 조건 추출
        conditions = extract_conditions(st.session_state.messages)
        st.session_state.last_conditions = conditions

        # 디버그용(원하면 주석 처리)
        with st.expander("🧾 추출된 조건(JSON)"):
            st.json(conditions)

        # 2) 아직 추천 못하면 친구톤으로 추가 질문
        if not conditions.get("ready_to_recommend", False):
            q = generate_followup_question(conditions)
            st.markdown(q)
            st.session_state.messages.append({"role": "assistant", "content": q})
            st.stop()

        # 3) Kakao 검색
        query = build_query(conditions)
        pre_text = generate_pre_recommend_text(conditions, query)
        st.markdown(pre_text)

        places = kakao_keyword_search(query, kakao_key, size=15)

        if not places:
            msg = "헉… 이 조건으로는 딱 맞는 데가 잘 안 잡히네 🥲\n지역을 조금만 넓혀볼까?"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        # 4) 후보 -> BEST3 + 설명/키워드 생성
        picks = rerank_and_format(conditions, places)

        if not picks:
            msg = "후보는 찾았는데… 정리하다가 살짝 꼬였어 😅\n한 번만 더 말해줄래?"
            st.markdown(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})
            st.stop()

        kakao_map = {p.get("id"): p for p in places}

        st.markdown("---")
        st.subheader("🍽️ 딱 3곳만 골랐어")

        cols = st.columns(3)

        for i, pick in enumerate(picks[:3]):
            place = kakao_map.get(pick["id"])
            if not place:
                continue

            with cols[i]:
                name = place.get("place_name")
                addr = place.get("road_address_name") or place.get("address_name")
                url = place.get("place_url")
                category = place.get("category_name")

                st.markdown(f"### {i+1}. {name}")
                st.caption(category or "")
                st.write(f"📍 {addr}")

                # 한줄 소개
                st.markdown(f"**{pick.get('one_line','')}**")

                # 🔥 반영된 조건(키워드) 표시
                matched = pick.get("matched_conditions", [])
                if matched:
                    st.markdown("**반영한 조건**")
                    st.markdown(" · ".join([f"`{m}`" for m in matched]))

                # 해시태그
                tags = pick.get("hashtags", [])
                if tags:
                    st.markdown(" ".join(tags))

                # 추천 이유
                st.markdown("**왜 여기냐면…**")
                st.write(pick.get("reason", ""))

                if url:
                    st.link_button("카카오맵에서 보기", url)

        final = "끝! 😎\n셋 중에 지금 제일 끌리는 데 하나만 골라봐. 아니면 내가 더 좁혀줄까?"
        st.session_state.messages.append({"role": "assistant", "content": final})
        st.markdown(final)
