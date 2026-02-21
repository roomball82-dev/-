import json
import re
import math
import requests
import streamlit as st
from openai import OpenAI

# -------------------------
# 기본 설정
# -------------------------
st.set_page_config(page_title="결정 메이트", page_icon="🍽️", layout="wide")
st.title("🍽️ 결정 메이트")

# -------------------------
# API 키 세션 유지
# -------------------------
if "openai_key" not in st.session_state:
    st.session_state.openai_key = ""
if "kakao_key" not in st.session_state:
    st.session_state.kakao_key = ""

st.sidebar.header("🔑 API 설정")
openai_key = st.sidebar.text_input("OpenAI API Key", type="password", value=st.session_state.openai_key)
kakao_key = st.sidebar.text_input("Kakao REST API Key", type="password", value=st.session_state.kakao_key)

st.session_state.openai_key = openai_key
st.session_state.kakao_key = kakao_key

client = OpenAI(api_key=openai_key) if openai_key else None

# -------------------------
# 사이드바 필터
# -------------------------
st.sidebar.markdown("---")
st.sidebar.header("🧭 상황 설정")

PLACE_TYPE = ["자동", "식사", "술", "카페"]
FOOD_TYPE = ["자동", "한식", "중식", "일식", "양식"]

place_type = st.sidebar.selectbox("장소 타입", PLACE_TYPE)
food_type = st.sidebar.selectbox("음식 분류", FOOD_TYPE)
people_count = st.sidebar.number_input("인원", 1, 20, 2)
budget = st.sidebar.radio("예산대", ["상관없음", "가성비", "보통", "조금 특별"])

# -------------------------
# 세션 상태
# -------------------------
def init_state():
    return {
        "location": None,
        "alcohol": None,
        "alcohol_type": None,
        "transport": None,
        "focus": None,
        "sensitivity": None
    }

if "state" not in st.session_state:
    st.session_state.state = init_state()

if "messages" not in st.session_state:
    st.session_state.messages = [{
        "role": "assistant",
        "content": "어디 근처에서 찾을까? 😊"
    }]

if st.sidebar.button("🔄 새 추천 시작"):
    st.session_state.state = init_state()
    st.session_state.messages = [{
        "role": "assistant",
        "content": "새로 시작하자 😎 어느 동네?"
    }]
    st.rerun()

# -------------------------
# 유틸
# -------------------------
def normalize(text):
    return re.sub(r"\s+", "", text.lower())

def parse_alcohol(text):
    t = normalize(text)
    if any(x in t for x in ["안마", "금주", "없음", "노"]):
        return "없음"
    if any(x in t for x in ["가볍", "한잔", "적당"]):
        return "가볍게"
    if any(x in t for x in ["술중심", "달리", "끝까지"]):
        return "술 중심"
    return None

def parse_alcohol_type(text):
    t = normalize(text)
    if "소주" in t: return "소주"
    if "맥주" in t: return "맥주"
    if "와인" in t: return "와인"
    return None

def parse_transport(text):
    t = normalize(text)
    if any(x in t for x in ["차", "주차"]):
        return "차"
    if any(x in t for x in ["지하철", "버스", "뚜벅", "걸어"]):
        return "대중교통"
    return None

# -------------------------
# 카카오 검색
# -------------------------
def kakao_search(query):
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {kakao_key}"}
    params = {"query": query, "size": 15}
    res = requests.get(url, headers=headers, params=params)
    return res.json().get("documents", [])

# -------------------------
# 추천 LLM
# -------------------------
def rerank(conditions, places):
    if not client:
        return []

    compact = [{
        "id": p["id"],
        "name": p["place_name"],
        "category": p["category_name"],
        "address": p.get("road_address_name") or p.get("address_name"),
        "url": p["place_url"]
    } for p in places]

    prompt = f"""
사용자 조건:
{json.dumps(conditions, ensure_ascii=False)}

후보:
{json.dumps(compact, ensure_ascii=False)}

조건에 맞는 BEST 3개만 JSON으로 출력.
"""

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        response_format={"type": "json_object"},
    )

    data = json.loads(res.choices[0].message.content)
    return data.get("picks", [])[:3]

# -------------------------
# UI
# -------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("예: 강남역 근처, 술은 가볍게")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})

    # 자연어 파싱
    if not st.session_state.state["location"]:
        st.session_state.state["location"] = user_input.strip()
    else:
        st.session_state.state["alcohol"] = parse_alcohol(user_input) or st.session_state.state["alcohol"]
        st.session_state.state["alcohol_type"] = parse_alcohol_type(user_input) or st.session_state.state["alcohol_type"]
        st.session_state.state["transport"] = parse_transport(user_input) or st.session_state.state["transport"]

    # 검색 쿼리 구성
    location = st.session_state.state["location"]
    query_parts = [location]

    # 장소 타입 반영
    if place_type == "식사":
        query_parts.append("맛집")
    elif place_type == "술":
        query_parts.append("술집")
    elif place_type == "카페":
        query_parts.append("카페")

    # 음식 분류 반영
    if food_type != "자동":
        query_parts.append(food_type)

    # 주종 반영
    if st.session_state.state["alcohol_type"]:
        query_parts.append(st.session_state.state["alcohol_type"])

    query = " ".join(query_parts)

    places = kakao_search(query)

    if not places:
        st.chat_message("assistant").markdown("조건을 조금 넓혀볼까?")
    else:
        picks = rerank(st.session_state.state, places)
        if not picks:
            picks = places[:3]

        st.chat_message("assistant").markdown("### 🍽️ 여기 어때?")
        for i, p in enumerate(picks[:3]):
            st.markdown(f"**{i+1}. {p.get('name', '')}**")
            st.markdown(p.get("address", ""))
            if p.get("url"):
                st.link_button("카카오맵 보기", p["url"])
