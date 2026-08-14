# dynamic_fetcher.py
import httpx
from typing import List, Dict, Any

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

async def fetch_places_dynamically(target_area: str) -> List[Dict[str, Any]]:
    """
    OpenStreetMap (Overpass API) を非同期呼出しし、対象エリアのPOI情報を取得。
    エラー・タイムアウト時はフォールバックデータを返す。
    """
    query = f"""
    [out:json][timeout:10];
    area["name"~"{target_area}"]->.searchArea;
    (
      node["tourism"~"attraction|museum|viewpoint"](area.searchArea);
      node["amenity"~"restaurant|cafe|public_bath"](area.searchArea);
      node["leisure"~"spa"](area.searchArea);
    );
    out body 10;
    """

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(OVERPASS_URL, data={"data": query})
            if res.status_code == 200:
                data = res.json()
                elements = data.get("elements", [])
                
                results = []
                for elem in elements:
                    tags = elem.get("tags", {})
                    name = tags.get("name")
                    if not name:
                        continue

                    types = []
                    if "tourism" in tags: types.append("tourist_attraction")
                    if "amenity" in tags and tags["amenity"] in ["restaurant", "cafe"]: types.append("restaurant")
                    if ("amenity" in tags and tags["amenity"] == "public_bath") or ("leisure" in tags and tags["leisure"] == "spa"):
                        types.append("spa")

                    results.append({
                        "name": name,
                        "keyword": target_area,
                        "types": types,
                        "lat": elem.get("lat"),
                        "lon": elem.get("lon"),
                        "price_level": 2
                    })

                if results:
                    return results[:6]

    except Exception as e:
        print(f"[OSM Overpass API Warn] Fallback activated: {e}")

    # フォールバック用データセット
    return [
        {"name": f"{target_area}の名物グルメ通り", "keyword": target_area, "types": ["restaurant"], "lat": 35.2333, "lon": 139.1036, "price_level": 2},
        {"name": f"{target_area}の歴史・景勝スポット", "keyword": target_area, "types": ["tourist_attraction"], "lat": 35.2400, "lon": 139.1100, "price_level": 1},
        {"name": f"{target_area}の絶景展望処", "keyword": target_area, "types": ["tourist_attraction"], "lat": 35.2450, "lon": 139.1150, "price_level": 1},
        {"name": f"{target_area}の温泉・リラクゼーション", "keyword": target_area, "types": ["spa"], "lat": 35.2300, "lon": 139.0900, "price_level": 3}
    ]

def analyze_reviews_and_build_vector(place: Dict[str, Any], custom_reviews: str) -> List[float]:
    """
    OSMの属性とテキストから特徴ベクトル c_i = [gourmet, sightseeing, healing] を計算。
    """
    types = place.get("types", [])
    name = place.get("name", "")

    gourmet = 0.9 if "restaurant" in types or any(w in name for w in ["グルメ", "名店", "飯", "食堂"]) else 0.3
    sightseeing = 0.9 if "tourist_attraction" in types or any(w in name for w in ["観光", "絶景", "寺", "神社", "城", "公園"]) else 0.4
    healing = 0.9 if "spa" in types or any(w in name for w in ["温泉", "湯", "癒", "スパ"]) else 0.2

    if custom_reviews:
        text = custom_reviews.lower()
        if any(w in text for w in ["美味", "食", "海鮮", "肉", "絶品", "丼"]): 
            gourmet = min(1.0, gourmet + 0.3)
        if any(w in text for w in ["絶景", "きれい", "写真", "散策", "歴史", "風情"]): 
            sightseeing = min(1.0, sightseeing + 0.3)
        if any(w in text for w in ["温泉", "のんびり", "ゆっくり", "疲れ", "静か", "露天風呂"]): 
            healing = min(1.0, healing + 0.3)

    return [round(gourmet, 2), round(sightseeing, 2), round(healing, 2)]