import streamlit as st

st.title("旅行タイプ診断")

# アンケート入力（1〜5）
event = st.slider("イベントへの興味", 1, 5, 3)
sightseeing = st.slider("観光への興味", 1, 5, 3)
budget = st.slider("予算の余裕", 1, 5, 3)
move = st.slider("移動の許容度", 1, 5, 3)
crowd = st.slider("混雑耐性", 1, 5, 3)

user = {
    "event_interest": event,
    "sightseeing_interest": sightseeing,
    "budget_room": budget,
    "travel_tolerance": move,
    "crowd_tolerance": crowd
}

def classify(user):
    event = user["event_interest"]
    sightseeing = user["sightseeing_interest"]
    budget = user["budget_room"]
    move = user["travel_tolerance"]
    crowd = user["crowd_tolerance"]

    oshitabi = event*2 + sightseeing*1.5 + budget*1.0 + move*1.0 + crowd*0.5
    price = event*1.0 + sightseeing*1.0 + budget*1.5 + move*1.5 + crowd*1.0
    born = event*0.5 + sightseeing*1.0 + budget*1.0 + move*2.0 + crowd*1.5 
    balance = event*1.0 + sightseeing*1.0 + budget*1.0 + move*1.0 + crowd*2.0

    scores = {
        "推し旅型": oshitabi,
        "コスパ重視型": price,
        "過密型": born,
        "バランス型": balance
    }

    # 最大スコア取得
    max_score = max(scores.values())

    # 同点タイプ抽出
    top_types = [k for k, v in scores.items() if v == max_score]

    # 判定
    if len(top_types) == 1:
        result = top_types[0]
    else:
        result = "・".join(top_types) + "（複合型）"

    return result, scores, top_types

if st.button("診断する"):
    result, scores, top_types = classify(user)

    st.write("あなたのタイプ:", result)
    st.write("スコア:", scores)

    # 補足説明
    if len(top_types) > 1:
        st.info("※複数の特徴を併せ持つタイプです")
    st.write("スコア:", scores)

def is_balance(user):
    values = list(user.values())
    return all(v >= 3 for v in values) and max(values) - min(values) <= 1
if is_balance(user):
    result = "バランス型"
else:
    result, scores, top_types = classify(user)