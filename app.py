# decision_mate_v6_final.py
# --- 완전 통합 안정본 ---
import json
import re
import math
import requests
import streamlit as st
from openai import OpenAI
from math import radians, sin, cos, sqrt, atan2

# ---------------------------
# 기본 설정
# ---------------------------
st.set_page_config(page_title="결정 메이트", page_icon="🍽️", layout="wide")
st.title("🍽️ 결정 메이트")
st.caption("약속 장소 정하는 인지 피로를 줄여주는 AI")

# ---------------------------
# 세션 초기화
# ---------------------------
if "messages" not in st.session_state:
    st.session_state.messages = [{
        "role": "assistant",
        "content": "오케이 😎 어디 동네에서 찾을까?"
    }]

if "last_picks_ids" not in st.session_state:
    st.session_state.last_picks_ids = []

if "openai_key" not in st.session_state:
    st.session_state.openai_key = ""

if "kakao_key" not in st.session_state:
    st.session_state.kakao_key = ""

# ---------------------------
# 사이드바
# ---------------------------
st.sidebar.header("🔑 API 설정")
openai_key = st.sidebar.text_input("OpenAI API Key", type="password", value=st.session_state.openai_key)
kakao_key = st.sidebar.text_input("Kakao REST API Key", type="password", value=st.session_state.kakao_key)

st.session_state.openai_key = openai_key
st.session_state.kakao_key = kakao_key

st.sidebar.markdown("---")

mode = st.sidebar.selectbox("상황", [
    "선택 안 함",
    "회사 회식",
    "친구",
    "단체 모임",
    "연인 · 썸 · 소개팅",
    "혼밥",
    "가족"
])

place_type = st.sidebar.selectbox("장소 타입", ["자동", "식사", "술", "카페"])
food_class = st.sidebar.selectbox("음식 분류", ["자동", "한식", "중식", "일식", "양식"])

people_count = st.sidebar.number_input("인원", 1, 20, 2)
budget = st.sidebar.selectbox("예산", ["상관없음", "가성비", "보통", "조금 특별"])

avoid_franchise = st.sidebar.checkbox("프랜차이즈 지양")

st.sidebar.markdown("---")
if st.sidebar.button("🔄 새 추천 시작"):
    st.session_state.messages = [{
        "role": "assistant",
        "content": "오케이 😎 어디 동네에서 찾을까?"
    }]
    st.session_state.last_picks_ids = []
    st.rerun()

# ---------------------------
# 유틸
# ---------------------------
def normalize(text):
    return re.sub(r"\s+", "", text.lower()) if text else ""

def haversine(x1, y1, x2, y2):
    lon1, lat1, lon2, lat2 = map(radians, [float(x1), float(y1), float(x2), float(y2)])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    c = 2*atan2(sqrt(a), sqrt(1-a))
    return 6371000 * c

def walk_minutes(m):
    return int(math.ceil(m/80))

# ---------------------------
# 카카오 API
# ---------------------------
def kakao_search(query, key, page=1):
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {key}"}
    params = {"query": query, "size": 15, "page": page}
    r = requests.get(url, headers=headers, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def search_all(query, key):
    results = []
    for p in range(1,4):
        data = kakao_search(query, key, p)
        results.extend(data.get("documents", []))
        if data.get("meta", {}).get("is_end"):
            break
    uniq = {}
    for r in results:
        uniq[r["id"]] = r
    return list(uniq.values())

# ---------------------------
# 후보 필터
# ---------------------------
def filter_places(places):
    filtered = places

    if place_type == "카페":
        filtered = [p for p in filtered if "카페" in (p.get("category_name") or "")]
    elif place_type == "술":
        filtered = [p for p in filtered if any(x in (p.get("category_name") or "") for x in ["술", "주점", "포차", "호프", "펍"])]
    elif place_type == "식사":
        filtered = [p for p in filtered if "카페" not in (p.get("category_name") or "")]

    if avoid_franchise:
        franchise = ["스타벅스", "이디야", "투썸", "메가커피", "맥도날드", "버거킹", "홍콩반점"]
        filtered = [p for p in filtered if not any(f.lower() in p["place_name"].lower() for f in franchise)]

    if len(filtered) < 6:
        return places
    return filtered

# ---------------------------
# LLM 추천
# ---------------------------
def rerank(conditions, candidates):
    client = OpenAI(api_key=openai_key)
    compact = [{
        "id": c["id"],
        "name": c["place_name"],
        "category": c["category_name"],
        "address": c.get("road_address_name") or c.get("address_name"),
        "url": c["place_url"]
    } for c in candidates[:20]]

    prompt = f"""
사용자 조건:
{json.dumps(conditions, ensure_ascii=False)}

후보:
{json.dumps(compact, ensure_ascii=False)}

반드시 3개만 JSON으로:
{{
 "picks":[
  {{
   "id":"...",
   "one_line":"한줄",
   "reason":"2~3문장",
   "hashtags":["#1","#2","#3","#4"]
  }}
 ]
}}
"""
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"user","content":prompt}],
        temperature=0.3,
        response_format={"type":"json_object"}
    )
    data = json.loads(res.choices[0].message.content)
    return data.get("picks", [])

# ---------------------------
# 채팅 출력
# ---------------------------
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

user_input = st.chat_input("예: 홍대역 근처")

if user_input:
    st.session_state.messages.append({"role":"user","content":user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    if not openai_key or not kakao_key:
        st.warning("API 키 필요")
        st.stop()

    query = user_input
    if place_type == "술":
        query += " 술집"
    if place_type == "카페":
        query += " 카페"
    if food_class != "자동":
        query += f" {food_class}"

    places = search_all(query, kakao_key)
    places = filter_places(places)

    if not places:
        st.write("조건에 맞는 결과 없음")
        st.stop()

    conditions = {
        "mode": mode,
        "place_type": place_type,
        "food_class": food_class,
        "people": people_count,
        "budget": budget
    }

    picks = rerank(conditions, places)

    # 3개 보장
    if len(picks) < 3:
        for p in places:
            if len(picks) >= 3:
                break
            if p["id"] not in [x["id"] for x in picks]:
                picks.append({
                    "id":p["id"],
                    "one_line":"후보 중 상위 노출",
                    "reason":"조건 기반 후보 상위",
                    "hashtags":["#후보","#근처","#무난","#추천"]
                })

    kakao_map = {p["id"]:p for p in places}

    st.markdown("---")
    cols = st.columns(3)
    for i,p in enumerate(picks[:3]):
        place = kakao_map.get(p["id"])
        if not place:
            continue
        with cols[i]:
            st.markdown(f"### {i+1}. {place['place_name']}")
            st.caption(place["category_name"])
            st.write(place.get("road_address_name") or place.get("address_name"))
            st.markdown(p["one_line"])
            st.write(p["reason"])
            st.markdown(" ".join(p["hashtags"]))
            st.link_button("카카오맵 보기", place["place_url"])

    st.session_state.last_picks_ids = [p["id"] for p in picks[:3]]

    final = "끝 😎 다른 데 보려면 '다른 데'라고 말해."
    st.session_state.messages.append({"role":"assistant","content":final})
    st.markdown(final)
