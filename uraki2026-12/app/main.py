# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
 
from dynamic_fetcher import fetch_places_dynamically, analyze_reviews_and_build_vector
from optimizer import (
    extract_phi_t,
    cosine_similarity,
    get_peak_weight,
    estimate_travel_time_and_cost,
    build_psi_b,
    combine_preference_vector,
    compute_f1,
    check_open_hours,
    check_constraints,
    compute_worst_case_travel_time,
    TAU_PEAK_MINUTES,
    TAU_TOUR_MINUTES,
    DEFAULT_OPEN_HOUR,
    DEFAULT_CLOSE_HOUR,
    INDIVIDUAL_BOOKING_MARKUP,
    ALPHA_PREFERENCE,
)
 
app = FastAPI(title="Tourism Itinerary Generator API")
 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
 
class DiagnoseRequest(BaseModel):
    user_text: str
    # 旧UI互換用の3段階重み（非推奨）。detailed_vector が渡された場合はそちらを優先する。
    preferences_4step: Optional[List[float]] = None
    detailed_vector: Dict[str, float]
    target_area: str
    season: str
    generation_group: str
    custom_reviews_text: Optional[str] = ""
 
class GenerateItineraryRequest(BaseModel):
    selected_place_ids: List[str]
    candidate_places: List[Dict[str, Any]]
    custom_waypoints: List[str]
    start_location: str
    start_time: str
    end_time: Optional[str] = ""
    trip_type: str
    transport_mode: str
    generation_group: str
    member_count: int
    peak_position: str
    budget_limit: float
    time_limit: float
 
def _select_diverse_top3(scored_places: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    式(2)のスコア降順だけでTOP3を選ぶと、gourmet/sightseeing/healingのいずれか1系統が
    たまたま高得点になった時、3枠すべてが似た系統（極端な場合は同じスポット）に偏りやすい。
    3系統それぞれの最高得点候補を1件ずつ優先的に選び、TOP3が自然と別々の目的地・別々の
    体験タイプ（グルメ／観光／癒し）になるようにする。系統に候補が無い場合のみ、
    残り枠をスコア降順で埋める。
    """
    by_category: Dict[int, List[Dict[str, Any]]] = {0: [], 1: [], 2: []}
    for p in scored_places:
        dominant = max(range(3), key=lambda i: p["category_vector"][i])
        by_category[dominant].append(p)
    for cat in by_category:
        by_category[cat].sort(key=lambda x: x["score"], reverse=True)
 
    top3: List[Dict[str, Any]] = []
    used_names = set()
    for cat in (0, 1, 2):
        if by_category[cat]:
            pick = by_category[cat][0]
            top3.append(pick)
            used_names.add(pick["name"])
 
    if len(top3) < 3:
        remaining = sorted(
            [p for p in scored_places if p["name"] not in used_names],
            key=lambda x: x["score"], reverse=True
        )
        for p in remaining:
            if len(top3) >= 3:
                break
            top3.append(p)
            used_names.add(p["name"])
 
    return sorted(top3, key=lambda x: x["score"], reverse=True)[:3]
 
 
@app.post("/diagnose_top3")
async def diagnose_top3(req: DiagnoseRequest):
    area = req.target_area.strip() if req.target_area.strip() else "箱根温泉"
    raw_places = await fetch_places_dynamically(area)
 
    # 式(1): u = α・φ(t) + (1-α)・ψ(b)
    phi_t = extract_phi_t(req.user_text)
    if req.detailed_vector:
        # 「10項目こだわり」入力(4段階評価相当)からψ(b)を構築（本来の想定経路）
        psi_b = build_psi_b(req.detailed_vector)
    elif req.preferences_4step:
        # 旧UI互換フォールバック（非推奨）
        sum_b = sum(req.preferences_4step) if sum(req.preferences_4step) > 0 else 1.0
        psi_b = [v / sum_b for v in req.preferences_4step]
    else:
        psi_b = [1 / 3, 1 / 3, 1 / 3]
    u = combine_preference_vector(phi_t, psi_b, alpha=ALPHA_PREFERENCE)
 
    scored_places = []
    for i, raw in enumerate(raw_places):
        c_i = analyze_reviews_and_build_vector(raw, req.custom_reviews_text or "")
        # 式(2): s_i = cos(u, c_i)
        s_i = cosine_similarity(u, c_i)
 
        price_lvl = raw.get("price_level", 2)
        cost = 2500 * price_lvl if c_i[0] > 0.7 else (15000 if c_i[2] > 0.8 else 1200)
        # τ^base の推定（癒し/観光の強い候補地はメインピーク候補として長めの基準時間を割当）
        t_base = TAU_PEAK_MINUTES if (c_i[2] > 0.7 or c_i[1] > 0.8) else TAU_TOUR_MINUTES
 
        scored_places.append({
            "id": f"poi_{i+1}",
            "name": raw["name"],
            "category_vector": c_i,
            "score": round(s_i * 100, 1),
            "cost": cost,
            "t_base": t_base,
            "open_hour": raw.get("open_hour", DEFAULT_OPEN_HOUR),
            "close_hour": raw.get("close_hour", DEFAULT_CLOSE_HOUR),
            "reviews": [f"{raw['name']}の特徴"]
        })
 
    top3 = _select_diverse_top3(scored_places)
    return {
        "status": "success",
        "target_area": area,
        "preference_vector": {"gourmet": round(u[0], 3), "sightseeing": round(u[1], 3), "healing": round(u[2], 3)},
        "top3_places": top3
    }
 
@app.post("/build_itinerary")
async def build_itinerary(req: GenerateItineraryRequest):
    selected_nodes = [p for p in req.candidate_places if p["id"] in req.selected_place_ids]
    if not selected_nodes:
        selected_nodes = req.candidate_places[:1]
 
    all_visit_targets = []
 
    # 経由地を追加（論文には無いアプリ独自拡張。τ^peak/τ^tourとは別枠の滞在時間45分を割当）
    for wp_name in req.custom_waypoints:
        wp_clean = wp_name.strip()
        if wp_clean:
            all_visit_targets.append({
                "id": f"wp_{wp_clean}",
                "name": wp_clean,
                "cost": 500,
                "t_base": 45,
                "open_hour": DEFAULT_OPEN_HOUR,
                "close_hour": 20,
                "is_custom_waypoint": True
            })
 
    # メインスポット（診断で得たスコア付き候補地 = 式(6)のf1算出対象）を追加
    for node in selected_nodes:
        all_visit_targets.append({
            "id": node["id"],
            "name": node["name"],
            "cost": node.get("cost", 1000),
            "t_base": node.get("t_base", TAU_TOUR_MINUTES),
            "open_hour": node.get("open_hour", DEFAULT_OPEN_HOUR),
            "close_hour": node.get("close_hour", DEFAULT_CLOSE_HOUR),
            "score": node.get("score"),
            "is_custom_waypoint": False
        })
 
    K = len(all_visit_targets)
    main_peak_id = selected_nodes[0]["id"] if selected_nodes else ""
    pace_factor = 1.15 if req.generation_group in ["family", "senior"] else 1.0
 
    start_h, start_m = map(int, req.start_time.split(":"))
    current_min = start_h * 60 + start_m
 
    itinerary_nodes = []
    total_cost = 0
    total_move_cost = 0
    total_time = 0
    total_move_time = 0
    current_loc_name = req.start_location
    hours_checks = []
    f1_nodes = []
 
    for idx, target in enumerate(all_visit_targets):
        travel_info = await estimate_travel_time_and_cost(current_loc_name, target["name"], req.transport_mode)
 
        move_time = int(travel_info["travel_time"] * pace_factor)
        move_cost = travel_info["travel_cost"] * req.member_count
 
        itinerary_nodes.append({
            "type": "transit",
            "from_name": current_loc_name,
            "to_name": target["name"],
            "duration_minutes": move_time,
            "cost": move_cost
        })
 
        current_min += move_time
        arrival_min = current_min
        total_move_time += move_time
        total_time += move_time
        total_cost += move_cost
        total_move_cost += move_cost
 
        is_custom_wp = target.get("is_custom_waypoint", False)
        is_peak = (target["id"] == main_peak_id) and not is_custom_wp
 
        # 式(8): dk = τ^peak (メインピーク) / τ^tour (周遊ノード)。経由地はアプリ拡張枠(45分)。
        base_stay = TAU_PEAK_MINUTES if is_peak else (45 if is_custom_wp else TAU_TOUR_MINUTES)
        stay_d = int(base_stay * pace_factor)
        # 式(7): ω(k;π)
        w_k = get_peak_weight(idx + 1, K, req.peak_position)
 
        # 式(5): 営業時間制約 wr_open ≤ ak, ak + dk ≤ wr_close
        hours_ok = check_open_hours(arrival_min, stay_d, target.get("open_hour", DEFAULT_OPEN_HOUR),
                                     target.get("close_hour", DEFAULT_CLOSE_HOUR))
        hours_checks.append(hours_ok)
 
        total_time += stay_d
        total_cost += (target["cost"] * req.member_count)
 
        # 式(6)のf1集計対象は診断スコアを持つメインスポットのみ（経由地は対象外）
        if not is_custom_wp and target.get("score") is not None:
            f1_nodes.append({
                "score": target["score"] / 100.0,  # diagnose_top3では表示用に100倍しているため式(2)の値域[-1,1]へ戻す
                "stay_minutes": stay_d,
                "t_base": target.get("t_base", stay_d),
                "peak_weight": w_k
            })
 
        itinerary_nodes.append({
            "type": "spot",
            "place": {"name": target["name"]},
            "stay_minutes": stay_d,
            "is_peak": is_peak,
            "is_custom_waypoint": is_custom_wp,
            "weight": round(w_k, 2),
            "arrival_minutes": arrival_min,
            "hours_satisfied": hours_ok
        })
 
        current_min += stay_d
        current_loc_name = target["name"]
 
    # 式(3)〜(5): 多目的制約充足判定
    constraints = check_constraints(total_cost, req.budget_limit, total_time, req.time_limit, hours_checks)
 
    # 式(6): f1(R,d) = Σ s_rk・h(dk)・ω(k;π)
    f1_total = compute_f1(f1_nodes)
 
    # 式(10): Δg = Σ(区間ごとの個別手配運賃想定) − f3(R)（移動費用のみに個別手配割増を適用）
    baseline_move_cost = int(total_move_cost * INDIVIDUAL_BOOKING_MARKUP)
    cost_saved_yen = max(0, baseline_move_cost - total_move_cost)
 
    # 式(11): Δt = 最悪の巡回順による総移動時間 − f2(R)
    stop_names = [t["name"] for t in all_visit_targets]
    worst_case_time = await compute_worst_case_travel_time(
        req.start_location, stop_names, req.transport_mode, req.trip_type, pace_factor
    )
    time_saved_minutes = max(0, worst_case_time - total_move_time)
 
    return {
        "status": "success",
        "itinerary": itinerary_nodes,
        "start_time": req.start_time,
        "end_time": req.end_time,
        "trip_type": req.trip_type,
        "member_count": req.member_count,
        "metrics": {
            "total_cost": total_cost,               # f3(R)
            "total_move_time_minutes": total_move_time,  # f2(R)
            "total_time_minutes": total_time,
            "f1_satisfaction": f1_total,             # 式(6)
            "budget_satisfied": constraints["budget_satisfied"],   # 式(3)
            "time_satisfied": constraints["time_satisfied"],       # 式(4)
            "hours_satisfied": constraints["hours_satisfied"],     # 式(5)
            "hours_violation_indices": constraints["hours_violation_indices"],
            "cost_saved_yen": cost_saved_yen,        # 式(10)
            "time_saved_minutes": time_saved_minutes  # 式(11)
        }
    }
 
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)