# optimizer.py
import asyncio
import itertools
import math
import random
import re
import httpx
from typing import List, Dict, Any

# --- 論文の定数定義 (5.4節) ---
TAU_PEAK_MINUTES = 120    # τ^peak: 体験ピーク（メインピーク）ノードの滞在時間 (式8)
TAU_TOUR_MINUTES = 60     # τ^tour: 周遊ノードの滞在時間 (式8)
DEFAULT_OPEN_HOUR = 9     # wr_open のデフォルト値（POI側に営業時間データが無い場合）
DEFAULT_CLOSE_HOUR = 17   # wr_close のデフォルト値
INDIVIDUAL_BOOKING_MARKUP = 1.2  # 式(10) Δg のベースライン: 区間ごとに個別手配した場合の想定割増率
ALPHA_PREFERENCE = 0.6    # 式(1) の α（テキスト解析 vs 明示的重み付けの寄与比率。本文5.2節に基づく）

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


def build_psi_b(detailed_vector: Dict[str, float]) -> List[float]:
    """
    式(1) の ψ(b) = b / |b|_1 を算出する。

    論文の b は D 次元のカテゴリ別「4段階評価」(0〜3の非負値) だが、本アプリのUIは
    「10項目こだわり」テーブル(-1.0〜1.0の重み)として実装されているため、実装スコアリング
    (c_i = [gourmet, sightseeing, healing]) と同じ3カテゴリに対応する項目を抽出して b とする。
        - gourmet     <- detailed_vector["gourmet"]        （グルメを堪能したい）
        - sightseeing <- detailed_vector["sightseeing"]    （観光地を巡りたい）
        - healing     <- detailed_vector["relax_schedule"] （ゆっくり観光したい＝癒し志向の代理指標）
    「避けたい(-1.0)」は論文の b が非負であることに合わせ 0 にクリップしてから正規化する。
    """
    b = [
        max(0.0, detailed_vector.get("gourmet", 0.0)),
        max(0.0, detailed_vector.get("sightseeing", 0.0)),
        max(0.0, detailed_vector.get("relax_schedule", 0.0)),
    ]
    norm = sum(b) or 1e-6
    return [round(v / norm, 4) for v in b]


def combine_preference_vector(phi_t: List[float], psi_b: List[float], alpha: float = ALPHA_PREFERENCE) -> List[float]:
    """式(1): u = α・φ(t) + (1 - α)・ψ(b)"""
    return [round(alpha * phi_t[i] + (1 - alpha) * psi_b[i], 4) for i in range(len(phi_t))]


def saturation_h(stay_minutes: float, t_base: float) -> float:
    """h(d) = min(1, d / τ^base): 滞在時間に対する満足度の飽和関数"""
    if not t_base or t_base <= 0:
        return 1.0
    return min(1.0, stay_minutes / t_base)


def compute_f1(scored_nodes: List[Dict[str, Any]]) -> float:
    """
    式(6): f1(R,d) = Σ_k s_rk・h(dk)・ω(k;π)

    scored_nodes の各要素は以下を持つ辞書:
        score        : s_rk (式2 のコサイン類似度スコア)
        stay_minutes : dk
        t_base       : τ^base
        peak_weight  : ω(k;π) (式7)
    経由地など診断スコアを持たないノードは呼び出し側で除外して渡すこと。
    """
    total = 0.0
    for node in scored_nodes:
        h = saturation_h(node["stay_minutes"], node.get("t_base"))
        total += node["score"] * h * node["peak_weight"]
    return round(total, 3)


def check_open_hours(arrival_min: int, stay_min: int, open_hour: float, close_hour: float) -> bool:
    """式(5): wr_open ≤ ak かつ ak + dk ≤ wr_close"""
    open_min = open_hour * 60
    close_min = close_hour * 60
    return open_min <= arrival_min and (arrival_min + stay_min) <= close_min


def check_constraints(total_cost: float, budget_limit: float, total_time: float, time_limit: float,
                       hours_checks: List[bool]) -> Dict[str, Any]:
    """式(3)〜(5) の多目的制約充足判定をまとめて返す"""
    return {
        "budget_satisfied": total_cost <= budget_limit,                    # 式(3)
        "time_satisfied": total_time <= time_limit,                        # 式(4)
        "hours_satisfied": all(hours_checks) if hours_checks else True,    # 式(5)
        "hours_violation_indices": [i for i, ok in enumerate(hours_checks) if not ok],
    }

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


async def compute_worst_case_travel_time(start_location: str, stop_names: List[str], transport_mode: str,
                                          trip_type: str, pace_factor: float = 1.0) -> int:
    """
    式(11) Δt = 最悪の巡回順による総移動時間 − f2(R) のベースラインを算出する。

    全訪問地点間の移動時間行列を一度だけ並列計算した上で、順列探索により総移動時間が
    最大となる巡回順（＝最悪ケース）を求める。訪問順そのものの最適化（式9の多目的GA、
    NSGA-II導入）は別途対応予定で、ここではΔtの分母となる「最悪値」の算出のみを行う。
    地点数が多い場合は全探索が高コストになるため乱択サンプリングで近似する。
    """
    nodes = [start_location] + list(stop_names)
    n = len(nodes)
    if n <= 1:
        return 0

    pairs = [(i, j) for i in range(n) for j in range(n) if i != j]
    sem = asyncio.Semaphore(8)

    async def fetch(i: int, j: int):
        async with sem:
            info = await estimate_travel_time_and_cost(nodes[i], nodes[j], transport_mode)
            return i, j, info["travel_time"]

    results = await asyncio.gather(*[fetch(i, j) for i, j in pairs])
    matrix: Dict[tuple, int] = {}
    for i, j, t in results:
        matrix[(i, j)] = t

    target_idxs = list(range(1, n))

    def path_time(order) -> int:
        total = matrix[(0, order[0])]
        for a, b in zip(order, order[1:]):
            total += matrix[(a, b)]
        if trip_type == "round_trip":
            total += matrix[(order[-1], 0)]
        return total

    if len(target_idxs) <= 8:
        candidate_orders = list(itertools.permutations(target_idxs))
    else:
        rnd = random.Random(42)
        candidate_orders = []
        for _ in range(3000):
            order = target_idxs[:]
            rnd.shuffle(order)
            candidate_orders.append(tuple(order))

    worst = max(path_time(order) for order in candidate_orders)
    return int(worst * pace_factor)