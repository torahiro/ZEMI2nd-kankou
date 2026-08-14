# optimizer.py
import math
import re
import httpx
from typing import List, Dict, Any

LOCATION_COORDINATES = {
    "東京駅": (139.7671, 35.6812),
    "新宿駅": (139.7006, 35.6896),
    "池袋駅": (139.7101, 35.7289),
    "横浜駅": (139.6223, 35.4658),
    "湯河原駅": (139.1039, 35.1462),
    "湯河原": (139.1039, 35.1462),
    "箱根温泉": (139.1036, 35.2333),
    "箱根湯本": (139.1036, 35.2333),
    "熱海温泉": (139.0716, 35.0966),
    "西武球場前": (139.4206, 35.7703),
    "金沢駅": (136.6478, 36.5780),
}

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

def cosine_similarity(u: List[float], c: List[float]) -> float:
    dot = sum(u[i] * c[i] for i in range(len(u)))
    norm_u = math.sqrt(sum(x**2 for x in u)) or 1e-6
    norm_c = math.sqrt(sum(x**2 for x in c)) or 1e-6
    return dot / (norm_u * norm_c)

def get_peak_weight(k: int, K: int, peak_pos: str, beta: float = 2.0, sigma: float = 0.15) -> float:
    rho_k = k / max(1, K)
    rho_star = 0.25 if peak_pos == "前半" else (0.50 if peak_pos == "中盤" else 0.75)
    return 1.0 + beta * math.exp(-((rho_k - rho_star) ** 2) / (2 * (sigma ** 2)))

def get_coordinates(name: str):
    for k, v in LOCATION_COORDINATES.items():
        if k in name or name in k:
            return v
    if "湯河原" in name:
        return (139.1039, 35.1462)
    elif "球場" in name or "所沢" in name or "埼玉" in name or "西武" in name:
        return (139.4206, 35.7703)
    elif "箱根" in name or "小田原" in name:
        return (139.1036, 35.2333)
    elif "熱海" in name:
        return (139.0716, 35.0966)
    return (139.7671, 35.6812)

async def estimate_travel_time_and_cost(p1_name: str, p2_name: str, transport_mode: str = "transit") -> Dict[str, Any]:
    coord1 = get_coordinates(p1_name)
    coord2 = get_coordinates(p2_name)

    osrm_profile = "foot" if transport_mode == "walking" else "car"
    osrm_url = f"http://router.project-osrm.org/route/v1/{osrm_profile}/{coord1[0]},{coord1[1]};{coord2[0]},{coord2[1]}?overview=false"

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            res = await client.get(osrm_url)
            if res.status_code == 200:
                data = res.json()
                if data.get("code") == "Ok" and len(data.get("routes", [])) > 0:
                    route = data["routes"][0]
                    duration_sec = route["duration"]
                    distance_meters = route["distance"]

                    dist_km = round(distance_meters / 1000.0, 1)
                    
                    if transport_mode == "transit":
                        travel_min = int((dist_km / 30.0) * 60) + 20
                    elif transport_mode == "driving":
                        travel_min = int((duration_sec / 60.0) * 1.25) + 10
                    else:
                        travel_min = int(duration_sec / 60.0)

                    if dist_km > 30.0:
                        travel_min = max(65, travel_min)
                    elif dist_km > 10.0:
                        travel_min = max(35, travel_min)

                    cost = int(dist_km * 22) if transport_mode == "transit" else int(dist_km * 25) + (1000 if dist_km > 30 else 0)

                    return {
                        "travel_time": travel_min,
                        "travel_cost": cost,
                        "distance_km": dist_km,
                        "is_osrm": True
                    }
    except Exception as e:
        print(f"OSRM API Fallback: {e}")

    # フォールバック
    dlon = math.radians(coord2[0] - coord1[0])
    dlat = math.radians(coord2[1] - coord1[1])
    a = math.sin(dlat/2)**2 + math.cos(math.radians(coord1[1])) * math.cos(math.radians(coord2[1])) * math.sin(dlon/2)**2
    dist_km = round(6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)) * 1.3, 1)
    if dist_km == 0: dist_km = 15.0
    travel_min = max(30, int((dist_km / 30.0) * 60) + 15)

    return {
        "travel_time": travel_min,
        "travel_cost": int(dist_km * 25),
        "distance_km": dist_km,
        "is_osrm": False
    }