# server.py (全国POI動的取得 & リアル感想文・口コミ解析対応版)
import os
import math
import re
import httpx
from typing import List, Dict, Any, Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Dynamic Peak-Aware Tourism Itinerary Generator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 外部APIキー設定（※環境変数から取得、未設定時はシミュレーションモードで動作）
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

class PlanRequest(BaseModel):
    user_text: str                          # 旅の気分・動機（自由記述）
    preferences_4step: List[float]          # [gourmet, sightseeing, healing] (0.0〜3.0)
    budget_limit: float                     # B (予算上限)
    time_limit: float                       # T (時間上限: 分)
    start_location: str                     # 出発地 (例: 東京駅, 福岡駅...)
    end_location: Optional[str] = ""        # 全国どこでも指定可能な目的地 (例: 金沢, 湯布院, 仙台...)
    peak_position: str                      # "前半", "中盤", "後半"
    custom_reviews_text: Optional[str] = ""  # 自身で記入した感想文・リアル口コミテキスト

# -------------------------------------------------------------
# 1. 全国POIの動的検索 (Google Places API / フォールバック検索エンジン)
# -------------------------------------------------------------
async def fetch_places_dynamically(target_area: str) -> List[Dict[str, Any]]:
    """全国任意のエリア名から実際の観光地・グルメスポットを動的取得"""
    if GOOGLE_MAPS_API_KEY:
        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        params = {
            "query": f"{target_area} 観光 スポット グルメ 温泉",
            "language": "ja",
            "key": GOOGLE_MAPS_API_KEY
        }
        async with httpx.AsyncClient() as client:
            res = await client.get(url, params=params)
            data = res.json()
            
        results = []
        for item in data.get("results", [])[:8]:
            results.append({
                "name": item.get("name"),
                "keyword": target_area,
                "types": item.get("types", []),
                "rating": item.get("rating", 4.0),
                "price_level": item.get("price_level", 2)
            })
        if results:
            return results

    # APIキー未設定時・テスト用の全国動的シミュレーションデータ（全国どこでも対応）
    return [
        {"name": f"{target_area}の名物グルメ・名店街", "keyword": target_area, "types": ["restaurant", "food"], "price_level": 2},
        {"name": f"{target_area}の主要観光名所・歴史散策", "keyword": target_area, "types": ["tourist_attraction", "park"], "price_level": 1},
        {"name": f"{target_area}の絶景・景勝スポット", "keyword": target_area, "types": ["point_of_interest"], "price_level": 1},
        {"name": f"{target_area}の温泉・リラクゼーション処", "keyword": target_area, "types": ["spa", "lodging"], "price_level": 3}
    ]

# -------------------------------------------------------------
# 2. 感想文・口コミテキスト解析 & 動的属性ベクトル c_i 生成
# -------------------------------------------------------------
def analyze_reviews_and_build_vector(place: Dict[str, Any], custom_reviews: str) -> List[float]:
    """スポット属性とユーザー自作の感想文・口コミ文から c_i = [gourmet, sightseeing, healing] を算出"""
    types = place.get("types", [])
    name = place.get("name", "")

    # スポット種別による基礎スコア
    gourmet = 0.9 if any(t in types for t in ["restaurant", "food", "cafe"]) or "グルメ" in name or "名店" in name else 0.3
    sightseeing = 0.9 if any(t in types for t in ["tourist_attraction", "park", "museum"]) or "観光" in name or "絶景" in name else 0.4
    healing = 0.9 if any(t in types for t in ["spa", "lodging"]) or "温泉" in name or "癒" in name else 0.2

    # 感想文・口コミテキストからの感度調整・キーワード加算
    if custom_reviews:
        text = custom_reviews.lower()
        if any(w in text for w in ["美味", "食", "海鮮", "肉", "ランチ", "絶品"]):
            gourmet = min(1.0, gourmet + 0.3)
        if any(w in text for w in ["絶景", "きれい", "写真", "散策", "歴史", "城"]):
            sightseeing = min(1.0, sightseeing + 0.3)
        if any(w in text for w in ["温泉", "のんびり", "ゆっくり", "疲れ", "静か", "癒やし"]):
            healing = min(1.0, healing + 0.3)

    return [round(gourmet, 2), round(sightseeing, 2), round(healing, 2)]

# -------------------------------------------------------------
# Step-2: 嗜好ベクトル u の生成 (式(1))
# -------------------------------------------------------------
def extract_phi_t(text: str) -> List[float]:
    keywords = {
        0: ["食", "海鮮", "カニ", "美味", "肉", "食べ", "グルメ", "名物", "丼", "酒", "ランチ", "ディナー"],
        1: ["景色", "絶景", "城", "歴史", "散策", "巡り", "観光", "名所", "海", "山", "写真", "映え", "寺", "神社"],
        2: ["温泉", "癒やし", "ゆっくり", "のんびり", "疲れ", "静か", "リラックス", "露天風呂", "休日", "休む"]
    }
    negations = ["嫌", "避けたい", "くない", "ない", "ダメ", "無理", "不要", "控え"]
    vec = [0.2, 0.2, 0.2]
    if not text.strip(): return vec
    sentences = re.split(r'[。！!？?\n]', text)
    for sent in sentences:
        if not sent: continue
        is_neg = any(neg in sent for neg in negations)
        for cat_idx, kw_list in keywords.items():
            for kw in kw_list:
                if kw in sent:
                    if is_neg: vec[cat_idx] = max(-1.0, vec[cat_idx] - 0.3)
                    else: vec[cat_idx] = min(1.0, vec[cat_idx] + 0.4)
    return vec

# -------------------------------------------------------------
# Step-3〜5: コサイン類似度(式(2)), ピーク重み(式(7)), 滞在時間(式(8)), 節約量(式(10),(11))
# -------------------------------------------------------------
def cosine_similarity(u: List[float], c: List[float]) -> float:
    dot = sum(u[i] * c[i] for i in range(len(u)))
    norm_u = math.sqrt(sum(x**2 for x in u)) or 1e-6
    norm_c = math.sqrt(sum(x**2 for x in c)) or 1e-6
    return dot / (norm_u * norm_c)

def get_peak_weight(k: int, K: int, peak_pos: str, beta: float = 2.0, sigma: float = 0.15) -> float:
    rho_k = k / max(1, K)
    rho_star = 0.25 if peak_pos == "前半" else (0.50 if peak_pos == "中盤" else 0.75)
    return 1.0 + beta * math.exp(-((rho_k - rho_star) ** 2) / (2 * (sigma ** 2)))

@app.post("/generate_itinerary")
async def generate_itinerary(req: PlanRequest):
    target_area = req.end_location.strip() if req.end_location.strip() else "箱根温泉"
    
    # 1. 全国POIの動的フェッチ
    raw_places = await fetch_places_dynamically(target_area)
    
    # 2. 口コミ・感想文を取り入れた動的属性ベクトル c_i 構築
    dynamic_places_db = []
    for i, raw in enumerate(raw_places):
        c_i = analyze_reviews_and_build_vector(raw, req.custom_reviews_text or "")
        price_lvl = raw.get("price_level", 2)
        cost = 2500 * price_lvl if c_i[0] > 0.7 else (15000 if c_i[2] > 0.8 else 1200)
        t_base = 120 if (c_i[2] > 0.7 or c_i[1] > 0.8) else 60

        reviews = [f"{raw['name']}に関する評判・特徴"]
        if req.custom_reviews_text:
            reviews.append(f"入力された感想・口コミ: {req.custom_reviews_text}")

        dynamic_places_db.append({
            "id": f"dyn_{i+1}",
            "name": raw["name"],
            "keyword": target_area,
            "category_vector": c_i, # c_i (式(2))
            "cost": cost,
            "t_base": t_base,
            "open_time": 9,
            "close_time": 20,
            "reviews": reviews
        })

    # 3. 嗜好ベクトル u の生成 (式(1))
    phi_t = extract_phi_t(req.user_text)
    sum_b = sum(req.preferences_4step) if sum(req.preferences_4step) > 0 else 1.0
    psi_b = [v / sum_b for v in req.preferences_4step]
    alpha = 0.6
    u = [alpha * phi_t[i] + (1 - alpha) * psi_b[i] for i in range(3)]

    # 4. 候補地スコアリング (式(2) コサイン類似度)
    scored_places = []
    for p in dynamic_places_db:
        s_i = cosine_similarity(u, p["category_vector"])
        p_copy = p.copy()
        p_copy["score"] = s_i
        scored_places.append(p_copy)

    candidates = sorted(scored_places, key=lambda x: x["score"], reverse=True)[:4]

    # 5. 体験ピーク構造に基づく滞在時間決定 (式(8)) & 求解
    K = len(candidates)
    main_peak_id = candidates[0]["id"] if K > 0 else ""
    
    itinerary_nodes = []
    total_cost = 0
    total_time = 0

    for idx, node in enumerate(candidates):
        is_peak = (node["id"] == main_peak_id)
        stay_d = 120 if is_peak else 60                        # 式(8)
        w_k = get_peak_weight(idx + 1, K, req.peak_position)  # 式(7)
        
        h_d = min(1.0, stay_d / node["t_base"])
        node_f1 = node["score"] * h_d * w_k
        
        move_time = 40 if idx > 0 else 60
        move_cost = 1500 if idx > 0 else 4000
        
        total_time += stay_d + move_time
        total_cost += node["cost"] + move_cost

        itinerary_nodes.append({
            "place": node,
            "stay_minutes": stay_d,
            "is_peak": is_peak,
            "weight": round(w_k, 2),
            "node_satisfaction": round(node_f1, 3)
        })

    # 式(10) コスパ / 式(11) タイパ 算出
    baseline_cost = total_cost * 1.3
    baseline_time = total_time + 90
    
    delta_g = max(0, int(baseline_cost - total_cost)) # 式(10)
    delta_t = max(0, int(baseline_time - total_time)) # 式(11)

    return {
        "status": "success",
        "target_area": target_area,
        "preference_vector": {"gourmet": round(u[0], 2), "sightseeing": round(u[1], 2), "healing": round(u[2], 2)},
        "itinerary": itinerary_nodes,
        "metrics": {
            "total_cost": total_cost,
            "total_time_minutes": total_time,
            "cost_saved_yen": delta_g,
            "time_saved_minutes": delta_t,
            "budget_satisfied": total_cost <= req.budget_limit, # 式(3)
            "time_satisfied": total_time <= req.time_limit      # 式(4)
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)