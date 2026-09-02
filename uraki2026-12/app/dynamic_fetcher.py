# dynamic_fetcher.py
import os
import httpx
from typing import List, Dict, Any
 
from optimizer import geocode_location
 
# overpass-api.de単体は混雑時に403/429を返しやすいため、複数の公開ミラーへ順にリトライする。
# また、User-Agent未指定だと弾かれることがあるため、アプリを識別できるヘッダーを付与する。
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_HEADERS = {"User-Agent": "peak-aware-tourism-itinerary-app/1.0 (educational project)"}
SEARCH_RADIUS_METERS = 5000  # target_area中心からの検索半径
MAX_POI_RESULTS = 10  # スコアリング対象の候補地数（多いほどTOP3の多様性が出る）
 
# 環境変数にGoogle Maps Platform(Places API)のAPIキーが設定されている場合、
# OSM Overpassより先にGoogle Places Text Searchを優先的に使う（未設定なら従来通りOSMのみ）。
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
GOOGLE_PLACES_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
 
# TripAdvisor Content API（要アカウント登録・要クレジットカード登録、無料枠は月5,000コール）。
# 設定されていれば最優先で使う。price_level/hoursの詳細情報は別途Location Detailsを叩く必要が
# あり、無料枠の消費を抑えるため、name/lat/lon等の基本情報のみnearby_searchから取得している。
TRIPADVISOR_API_KEY = os.getenv("TRIPADVISOR_API_KEY", "")
TRIPADVISOR_SEARCH_URL = "https://api.content.tripadvisor.com/api/v1/location/nearby_search"
TRIPADVISOR_CATEGORIES = {"attractions": "tourist_attraction", "restaurants": "restaurant"}
 
# 式(5)の営業時間制約 wr_open / wr_close 用のデフォルト値。
# 実データに営業時間が無いため、種別に応じた簡易な想定値を付与する。
DEFAULT_OPEN_HOUR = 9
 
def _default_hours(types):
    """種別からデフォルトの営業時間帯 (wr_open, wr_close) を推定する"""
    if "restaurant" in types:
        return (DEFAULT_OPEN_HOUR, 21)
    if "spa" in types:
        return (DEFAULT_OPEN_HOUR, 22)
    return (DEFAULT_OPEN_HOUR, 17)
 
def _map_google_types(g_types: List[str]) -> List[str]:
    """Google Places の types 配列を、本アプリの3分類 (tourist_attraction/restaurant/spa) へ正規化する"""
    types = []
    if any(t in g_types for t in ["restaurant", "cafe", "food", "meal_takeaway"]):
        types.append("restaurant")
    if any(t in g_types for t in ["tourist_attraction", "museum", "park", "point_of_interest"]):
        types.append("tourist_attraction")
    if any(t in g_types for t in ["spa", "lodging"]):
        types.append("spa")
    if not types:
        types.append("tourist_attraction")
    return types
 
 
async def _fetch_from_google_places(target_area: str) -> List[Dict[str, Any]]:
    """Google Places Text Search APIから実在スポットを取得（APIキー未設定/失敗時は空リストを返す）"""
    params = {
        "query": f"{target_area} 観光 スポット グルメ 温泉",
        "language": "ja",
        "key": GOOGLE_MAPS_API_KEY
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(GOOGLE_PLACES_URL, params=params)
            if res.status_code != 200:
                return []
            data = res.json()
    except Exception as e:
        print(f"[Google Places API Warn] Fallback activated: {e}")
        return []
 
    results = []
    for item in data.get("results", [])[:8]:
        name = item.get("name")
        if not name:
            continue
        types = _map_google_types(item.get("types", []))
        open_hour, close_hour = _default_hours(types)
        loc = item.get("geometry", {}).get("location", {})
        results.append({
            "name": name,
            "keyword": target_area,
            "types": types,
            "lat": loc.get("lat"),
            "lon": loc.get("lng"),
            "price_level": min(3, max(1, item.get("price_level", 2))),
            "open_hour": open_hour,
            "close_hour": close_hour
        })
    return results
 
 
async def _fetch_from_tripadvisor(target_area: str) -> List[Dict[str, Any]]:
    """
    TripAdvisor Content APIのnearby_searchから実在スポットを取得する。
    「attractions」「restaurants」の2カテゴリでそれぞれ検索し、結果を統合する。
    APIキー未設定/HTTPエラー/想定外のレスポンス形式の場合は空リストを返し、
    呼び出し側（fetch_places_dynamically）が次の取得手段にフォールバックする。
 
    無料枠（月5,000コール）を節約するため、Location Details（営業時間・価格帯など）は
    ここでは呼ばず、nearby_searchで得られる基本情報（name/緯度経度）のみを使い、
    価格帯・営業時間はカテゴリからのデフォルト推定値で補う。
    """
    if not TRIPADVISOR_API_KEY:
        return []
 
    lon, lat = await geocode_location(target_area)
 
    results = []
    seen_names = set()
 
    async with httpx.AsyncClient(timeout=8.0) as client:
        for category_key, internal_type in TRIPADVISOR_CATEGORIES.items():
            params = {
                "key": TRIPADVISOR_API_KEY,
                "latLong": f"{lat},{lon}",
                "category": category_key,
                "radius": SEARCH_RADIUS_METERS // 1000,
                "radiusUnit": "km",
                "language": "ja",
            }
            try:
                res = await client.get(
                    TRIPADVISOR_SEARCH_URL,
                    params=params,
                    headers={"accept": "application/json"},
                )
                if res.status_code != 200:
                    print(f"[TripAdvisor Warn] {category_key} -> HTTP {res.status_code}: {res.text[:200]}")
                    continue
                data = res.json()
            except Exception as e:
                print(f"[TripAdvisor Warn] {category_key} request failed: {e}")
                continue
 
            for item in data.get("data", [])[:MAX_POI_RESULTS]:
                name = item.get("name")
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
 
                # nearby_searchのレスポンスに緯度経度が含まれない場合があるため、
                # 取得できなければ検索中心地点の座標にフォールバックする。
                item_lat = item.get("latitude") or (item.get("address_obj") or {}).get("latitude")
                item_lon = item.get("longitude") or (item.get("address_obj") or {}).get("longitude")
                try:
                    item_lat = float(item_lat) if item_lat is not None else lat
                    item_lon = float(item_lon) if item_lon is not None else lon
                except (TypeError, ValueError):
                    item_lat, item_lon = lat, lon
 
                types = [internal_type]
                if any(w in name for w in ["温泉", "スパ", "Spa", "湯"]):
                    types.append("spa")
 
                open_hour, close_hour = _default_hours(types)
                results.append({
                    "name": name,
                    "keyword": target_area,
                    "types": types,
                    "lat": item_lat,
                    "lon": item_lon,
                    "price_level": 2,
                    "open_hour": open_hour,
                    "close_hour": close_hour,
                })
 
    return results[:MAX_POI_RESULTS]
 
 
async def _fetch_from_overpass(query: str) -> List[Dict[str, Any]]:
    """複数のOverpass公開ミラーへ順にリトライし、最初に成功したレスポンスのelementsを返す"""
    for url in OVERPASS_ENDPOINTS:
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=OVERPASS_HEADERS) as client:
                res = await client.post(url, data={"data": query})
                if res.status_code == 200:
                    elements = res.json().get("elements", [])
                    if elements:
                        return elements
                else:
                    print(f"[OSM Overpass Warn] {url} -> HTTP {res.status_code}")
        except Exception as e:
            print(f"[OSM Overpass Warn] {url} failed: {e}")
    return []
 
 
MIN_ACCEPTABLE_RESULTS = 3  # OSMの結果がこの件数未満の場合のみ、有償/要キーAPIで補う
 
 
async def fetch_places_dynamically(target_area: str) -> List[Dict[str, Any]]:
    """
    実在スポットを動的取得する。「無料枠をなるべく制限なく使いたい」という方針に合わせ、
    APIキー登録が不要で完全無料のOpenStreetMap Overpass APIを第一候補にしている。
    優先順位:
      1. OpenStreetMap Overpass API（キー不要・無料。MIN_ACCEPTABLE_RESULTS件以上取れれば採用）
      2. TripAdvisor Content API（TRIPADVISOR_API_KEY設定時。OSMの結果が少ない時だけ補完で使用）
      3. Google Places Text Search（GOOGLE_MAPS_API_KEY設定時。同上）
      4. 上記すべて失敗/件数不足の場合、OSMの結果があればそれを返す。1件も無ければフォールバックデータ
 
    OSM検索は以前 area["name"~target_area] で行政区画名との文字列一致に頼っていたが、
    「箱根温泉」のようにOSM上の正式名称と一致しない入力だと検索が常に空振りし、
    実質フォールバックデータしか返らない状態になっていた。target_areaを座標に変換し、
    半径検索(around)に切り替えることで、地名表記のゆれに影響されず実データを取得する。
    """
    lon, lat = await geocode_location(target_area)
    query = f"""
    [out:json][timeout:15];
    (
      node["tourism"~"attraction|museum|viewpoint"](around:{SEARCH_RADIUS_METERS},{lat},{lon});
      node["amenity"~"restaurant|cafe|public_bath"](around:{SEARCH_RADIUS_METERS},{lat},{lon});
      node["leisure"~"spa"](around:{SEARCH_RADIUS_METERS},{lat},{lon});
    );
    out body {MAX_POI_RESULTS * 3};
    """
 
    elements = await _fetch_from_overpass(query)
    osm_results = []
    if elements:
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
 
            open_hour, close_hour = _default_hours(types)
            osm_results.append({
                "name": name,
                "keyword": target_area,
                "types": types,
                "lat": elem.get("lat"),
                "lon": elem.get("lon"),
                "price_level": 2,
                "open_hour": open_hour,
                "close_hour": close_hour
            })
 
    if len(osm_results) >= MIN_ACCEPTABLE_RESULTS:
        return osm_results[:MAX_POI_RESULTS]
 
    # OSM単独では件数が心もとない場合のみ、要キー/有償APIで補う（未設定なら何もせずスキップ）
    if TRIPADVISOR_API_KEY:
        tripadvisor_results = await _fetch_from_tripadvisor(target_area)
        if tripadvisor_results:
            return tripadvisor_results
 
    if GOOGLE_MAPS_API_KEY:
        google_results = await _fetch_from_google_places(target_area)
        if google_results:
            return google_results
 
    if osm_results:
        # 補完APIが無い/失敗しても、OSMで多少なりとも取れていればそれを優先して返す
        return osm_results[:MAX_POI_RESULTS]
 
    # フォールバック用データセット
    fallback = [
        {"name": f"{target_area}の名物グルメ通り", "keyword": target_area, "types": ["restaurant"], "lat": 35.2333, "lon": 139.1036, "price_level": 2},
        {"name": f"{target_area}の歴史・景勝スポット", "keyword": target_area, "types": ["tourist_attraction"], "lat": 35.2400, "lon": 139.1100, "price_level": 1},
        {"name": f"{target_area}の絶景展望処", "keyword": target_area, "types": ["tourist_attraction"], "lat": 35.2450, "lon": 139.1150, "price_level": 1},
        {"name": f"{target_area}の温泉・リラクゼーション", "keyword": target_area, "types": ["spa"], "lat": 35.2300, "lon": 139.0900, "price_level": 3}
    ]
    for place in fallback:
        place["open_hour"], place["close_hour"] = _default_hours(place["types"])
    return fallback
 
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