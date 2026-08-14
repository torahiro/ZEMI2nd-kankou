# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

from dynamic_fetcher import fetch_places_dynamically, analyze_reviews_and_build_vector
from optimizer import extract_phi_t, cosine_similarity, get_peak_weight, estimate_travel_time_and_cost

app = FastAPI(title="Tourism Itinerary Generator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class DiagnoseRequest(BaseModel):
    user_text: str
    preferences_4step: List[float]
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

@app.post("/diagnose_top3")
async def diagnose_top3(req: DiagnoseRequest):
    area = req.target_area.strip() if req.target_area.strip() else "箱根温泉"
    raw_places = await fetch_places_dynamically(area)
    
    phi_t = extract_phi_t(req.user_text)
    sum_b = sum(req.preferences_4step) if sum(req.preferences_4step) > 0 else 1.0
    psi_b = [v / sum_b for v in req.preferences_4step]
    u = [0.6 * phi_t[i] + 0.4 * psi_b[i] for i in range(3)]

    scored_places = []
    for i, raw in enumerate(raw_places):
        c_i = analyze_reviews_and_build_vector(raw, req.custom_reviews_text or "")
        base_score = cosine_similarity(u, c_i) * 100
        
        price_lvl = raw.get("price_level", 2)
        cost = 2500 * price_lvl if c_i[0] > 0.7 else (15000 if c_i[2] > 0.8 else 1200)

        scored_places.append({
            "id": f"poi_{i+1}",
            "name": raw["name"],
            "category_vector": c_i,
            "score": round(base_score, 1),
            "cost": cost,
            "t_base": 120 if (c_i[2] > 0.7 or c_i[1] > 0.8) else 60,
            "reviews": [f"{raw['name']}の特徴"]
        })

    top3 = sorted(scored_places, key=lambda x: x["score"], reverse=True)[:3]
    return {
        "status": "success",
        "target_area": area,
        "top3_places": top3
    }

@app.post("/build_itinerary")
async def build_itinerary(req: GenerateItineraryRequest):
    selected_nodes = [p for p in req.candidate_places if p["id"] in req.selected_place_ids]
    if not selected_nodes:
        selected_nodes = req.candidate_places[:1]

    all_visit_targets = []

    # 経由地を追加
    for wp_name in req.custom_waypoints:
        wp_clean = wp_name.strip()
        if wp_clean:
            all_visit_targets.append({
                "id": f"wp_{wp_clean}",
                "name": wp_clean,
                "cost": 500,
                "t_base": 45,
                "is_custom_waypoint": True
            })

    # メインスポットを追加
    for node in selected_nodes:
        all_visit_targets.append({
            "id": node["id"],
            "name": node["name"],
            "cost": node.get("cost", 1000),
            "t_base": node.get("t_base", 60),
            "is_custom_waypoint": False
        })

    K = len(all_visit_targets)
    main_peak_id = selected_nodes[0]["id"] if selected_nodes else ""
    pace_factor = 1.15 if req.generation_group in ["family", "senior"] else 1.0

    itinerary_nodes = []
    total_cost = 0
    total_time = 0
    total_move_time = 0
    current_loc_name = req.start_location

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
        
        total_move_time += move_time
        total_time += move_time
        total_cost += move_cost

        is_custom_wp = target.get("is_custom_waypoint", False)
        is_peak = (target["id"] == main_peak_id) and not is_custom_wp
        
        stay_d = int((120 if is_peak else (45 if is_custom_wp else 60)) * pace_factor)
        w_k = get_peak_weight(idx + 1, K, req.peak_position)
        
        total_time += stay_d
        total_cost += (target["cost"] * req.member_count)

        itinerary_nodes.append({
            "type": "spot",
            "place": {"name": target["name"]},
            "stay_minutes": stay_d,
            "is_peak": is_peak,
            "is_custom_waypoint": is_custom_wp,
            "weight": round(w_k, 2)
        })

        current_loc_name = target["name"]

    return {
        "status": "success",
        "itinerary": itinerary_nodes,
        "start_time": req.start_time,
        "end_time": req.end_time,
        "trip_type": req.trip_type,
        "member_count": req.member_count,
        "metrics": {
            "total_cost": total_cost,
            "total_time_minutes": total_time,
            "cost_saved_yen": max(500, int(total_cost * 0.2)),
            "time_saved_minutes": max(15, int(total_move_time * 0.25))
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)