# optimizer.py
import asyncio
import itertools
import math
import os
import random
import re
import time
import httpx
from typing import List, Dict, Any
 
# --- 論文の定数定義 (5.4節) ---
TAU_PEAK_MINUTES = 120    # τ^peak: 体験ピーク（メインピーク）ノードの滞在時間 (式8)
TAU_TOUR_MINUTES = 60     # τ^tour: 周遊ノードの滞在時間 (式8)
DEFAULT_OPEN_HOUR = 9     # wr_open のデフォルト値（POI側に営業時間データが無い場合）
DEFAULT_CLOSE_HOUR = 17   # wr_close のデフォルト値
INDIVIDUAL_BOOKING_MARKUP = 1.2  # 式(10) Δg のベースライン: 区間ごとに個別手配した場合の想定割増率
ALPHA_PREFERENCE = 0.6    # 式(1) の α（テキスト解析 vs 明示的重み付けの寄与比率。本文5.2節に基づく）
 
# 全国どこでも同じロジックで動くよう、地名→座標の解決はNominatim/Google Geocodingの
# 実ジオコーディングAPIに一本化している（詳細は geocode_location() を参照）。
# 以前はここに主要駅・観光地の座標を手打ちした辞書(LOCATION_COORDINATES)＋locations.csv
# （都道府県庁所在地など100件強）を最終フォールバックとして持たせていたが、
#   - 「東京都渋谷区」が部分一致で辞書の「京都」に誤ってヒットする
#     （"京都" in "東京都渋谷区" が真になってしまう）
#   - 「東京都八王子市」も同様に「京都」の座標を返してしまう
# など、キーワードの部分一致に起因する誤判定が、全国の地名を相手にすると無視できない
# 頻度で発生することが分かったため撤去した。カバーできる地名がもともと100件強に限られ、
# 全国展開とは相性が悪かった実データ的な理由もある。
# 実APIが両方とも本当に使えない場合の最終安全策としてのみ、東京駅の座標を1点だけ残す。
TOKYO_STATION_FALLBACK = (139.7671, 35.6812)
 
# 式(1)のφ(t)で使うカテゴリ別キーワード（0:gourmet, 1:sightseeing, 2:healing）
_PHI_KEYWORDS = {
    0: ["食", "海鮮", "カニ", "美味", "肉", "食べ", "グルメ", "名物", "丼", "酒", "ランチ", "ディナー"],
    1: ["景色", "絶景", "城", "歴史", "散策", "巡り", "観光", "名所", "海", "山", "写真", "映え", "寺", "神社"],
    2: ["温泉", "癒やし", "ゆっくり", "のんびり", "疲れ", "静か", "リラックス", "露天風呂", "休日", "休む"]
}
_PHI_NEGATIONS = ["嫌", "避けたい", "くない", "ない", "ダメ", "無理", "不要", "控え"]
 
# 論文5.2節は「形態素解析器MeCabを用いてテキストから名詞・形容詞等の重要単語を抽出」と
# 明記しているが、これまでの実装はMeCabを一切使わず文全体に対する部分文字列一致のみで
# φ(t)を計算していた。ここではMeCab(mecab-python3等)が利用可能な環境ではそれを使い、
# 未インストールの環境では従来の部分文字列マッチにフォールバックする
# （pip install mecab-python3 と、unidic-lite等の辞書が別途必要）。
_MECAB_TAGGER = None
_MECAB_AVAILABLE = False
try:
    import MeCab as _MeCab  # type: ignore
    _MECAB_TAGGER = _MeCab.Tagger()
    _MECAB_TAGGER.parse("")  # 辞書未設定などの初期化エラーはparse時に遅延して出ることがあるため、ここで疎通確認する
    _MECAB_AVAILABLE = True
except Exception:
    _MECAB_TAGGER = None
    _MECAB_AVAILABLE = False
 
 
def _mecab_important_words(sentence: str) -> List[str]:
    """MeCabで形態素解析し、名詞・形容詞・動詞（自立語）の表層形のみを抽出する"""
    words = []
    node = _MECAB_TAGGER.parseToNode(sentence)
    while node:
        surface = node.surface
        if surface:
            features = node.feature.split(",") if node.feature else []
            pos = features[0] if features else ""
            if pos in ("名詞", "形容詞", "動詞"):
                words.append(surface)
        node = node.next
    return words
 
 
def _extract_phi_t_mecab(text: str) -> List[float]:
    """MeCabで抽出した重要単語（名詞・形容詞・動詞）を対象にカテゴリキーワードと照合する。
    文全体に対する部分文字列一致より精度が高く、無関係な語に含まれる偶然の部分一致
    （例:「岩滝寺」の「寺」で観光と誤判定される等）も起きにくい。"""
    vec = [0.2, 0.2, 0.2]
    sentences = re.split(r'[。！!？?\n]', text)
    for sent in sentences:
        if not sent:
            continue
        is_neg = any(neg in sent for neg in _PHI_NEGATIONS)
        words = _mecab_important_words(sent)
        for cat_idx, kw_list in _PHI_KEYWORDS.items():
            hit = any(kw in w or w in kw for w in words for kw in kw_list)
            if hit:
                if is_neg:
                    vec[cat_idx] = max(-1.0, vec[cat_idx] - 0.3)
                else:
                    vec[cat_idx] = min(1.0, vec[cat_idx] + 0.4)
    return vec
 
 
def _extract_phi_t_keyword_fallback(text: str) -> List[float]:
    """MeCab未インストール環境向けのフォールバック（従来の部分文字列マッチ実装）"""
    vec = [0.2, 0.2, 0.2]
    sentences = re.split(r'[。！!？?\n]', text)
    for sent in sentences:
        if not sent:
            continue
        is_neg = any(neg in sent for neg in _PHI_NEGATIONS)
        for cat_idx, kw_list in _PHI_KEYWORDS.items():
            for kw in kw_list:
                if kw in sent:
                    if is_neg:
                        vec[cat_idx] = max(-1.0, vec[cat_idx] - 0.3)
                    else:
                        vec[cat_idx] = min(1.0, vec[cat_idx] + 0.4)
    return vec
 
 
def extract_phi_t(text: str) -> List[float]:
    """式(1)のφ(t)。論文通りMeCabが使える環境ではそれを使い、使えない環境では
    キーワード部分一致にフォールバックする。"""
    if not text.strip():
        return [0.2, 0.2, 0.2]
    if _MECAB_AVAILABLE:
        try:
            return _extract_phi_t_mecab(text)
        except Exception as e:
            print(f"[MeCab Warn] falling back to keyword heuristic: {e}")
    return _extract_phi_t_keyword_fallback(text)
 
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
 
def estimate_transit_minutes(dist_km: float) -> int:
    """
    「電車・バス」モードの所要時間を距離帯ごとの平均速度で見積もる。
    従来は距離÷30km/hの一律計算だったため、東京駅⇔箱根温泉(約90km)のような
    都市間移動でも近距離の路線バス並みの低速が適用され、192分という非現実的な
    所要時間になっていた。近距離は乗換・待ち時間を考慮して低速、都市間は
    優等列車の利用を想定して高速の平均速度を割り当てる。
    """
    if dist_km <= 15:
        speed_kmh, overhead = 18.0, 15    # 近距離: 路線バス・各停中心
    elif dist_km <= 50:
        speed_kmh, overhead = 40.0, 20    # 中距離: 快速・急行中心
    else:
        speed_kmh, overhead = 60.0, 25    # 長距離: 特急・新幹線等の優等列車を想定
    return int((dist_km / speed_kmh) * 60) + overhead
 
 
_GEOCODE_CACHE: Dict[str, tuple] = {}
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
 
 
async def _geocode_via_google(name: str, area_hint: str = ""):
    address = f"{area_hint} {name}".strip() if area_hint else name
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(GOOGLE_GEOCODE_URL, params={
                "address": address, "language": "ja", "region": "jp", "key": GOOGLE_MAPS_API_KEY
            })
            if res.status_code == 200:
                results = res.json().get("results", [])
                if results:
                    loc = results[0]["geometry"]["location"]
                    return (loc["lng"], loc["lat"])
    except Exception as e:
        print(f"[Google Geocoding Warn] {e}")
    return None
 
 
_nominatim_lock = asyncio.Lock()
_last_nominatim_call_time = 0.0
NOMINATIM_MIN_INTERVAL_SEC = 1.1  # Nominatim利用ポリシー: 最大1req/秒。IP規制を避けるための自主制限。
 
 
async def _geocode_via_nominatim(name: str, area_hint: str = ""):
    """
    OpenStreetMapの無料ジオコーダー。APIキー不要・料金無料だが、利用ポリシー上
    ①User-Agentの指定が必須、②最大1req/秒までという制約がある。これを超えるとIP単位で
    アクセス拒否されうるため、グローバルなロック+待機で強制的にリクエスト間隔を空けている。
 
    countrycodes=jp で検索対象を日本国内に限定し（本アプリは日本国内の旅程しか扱わない）、
    area_hint が渡された場合は地名をクエリに含めて曖昧な店名・スポット名の解決精度を上げる
    （例:「ミラノ亭」単体ではなく「箱根温泉 ミラノ亭」で検索し、全国の同名店との混同を減らす）。
    """
    global _last_nominatim_call_time
    query = f"{area_hint} {name}".strip() if area_hint else name
    try:
        async with _nominatim_lock:
            now = time.monotonic()
            wait = NOMINATIM_MIN_INTERVAL_SEC - (now - _last_nominatim_call_time)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_nominatim_call_time = time.monotonic()
 
            headers = {"User-Agent": "peak-aware-tourism-itinerary-app/1.0"}
            async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
                res = await client.get(NOMINATIM_URL, params={
                    "q": query, "format": "json", "limit": 1, "accept-language": "ja",
                    "countrycodes": "jp"
                })
                if res.status_code == 200:
                    results = res.json()
                    if results:
                        return (float(results[0]["lon"]), float(results[0]["lat"]))
                else:
                    print(f"[Nominatim Geocoding Warn] HTTP {res.status_code}")
    except Exception as e:
        print(f"[Nominatim Geocoding Warn] {e}")
    return None
 
 
async def geocode_location(name: str, area_hint: str = ""):
    """
    地名を (lon, lat) 座標に変換する。
 
    「無料枠をなるべく制限なく使いたい」という方針に合わせ、APIキー登録が不要で完全無料の
    OpenStreetMap Nominatimを第一候補にしている（1req/秒の自主制限つき）。Google Geocoding APIは
    キーが設定されている場合のみ、Nominatimで解決できなかった時の補完として使う（有償/要クレジット
    カードのため）。以前あった固定辞書/CSVによるオフラインフォールバックは、地名の部分一致に
    起因する誤判定（例:「東京都渋谷区」が「京都」に誤ヒットする等）が全国運用では無視できない
    頻度で起きるため撤去し、実ジオコーディングAPIのみに一本化した。
    優先順位: ①キャッシュ ②OpenStreetMap Nominatim（無料・APIキー不要） ③Google Geocoding API
    （GOOGLE_MAPS_API_KEY設定時のみ） ④両方とも不通の場合のみ、東京駅の座標を返す。
 
    area_hint: 「ミラノ亭」のような単体では全国どこにでも同名店が存在しうる曖昧な地名を、
    対象エリア名（例:「箱根温泉」）を検索クエリに含めることで曖昧さを減らすための補助情報。
    候補地（診断で得たOSM/TripAdvisor由来のスポット）は seed_geocode_cache() で実座標を
    事前投入しキャッシュヒットさせるため、area_hint は主にユーザーが自由入力した経由地・
    出発地・到着地の解決時にのみ使う想定。
    """
    if name in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[name]
 
    coords = None
    if area_hint:
        coords = await _geocode_via_nominatim(name, area_hint)
    if not coords:
        # area_hint付きクエリで過剰に絞り込まれ0件になった場合や、area_hint未指定の場合の通常検索
        coords = await _geocode_via_nominatim(name)
    if not coords and GOOGLE_MAPS_API_KEY:
        if area_hint:
            coords = await _geocode_via_google(name, area_hint)
        if not coords:
            coords = await _geocode_via_google(name)
    if not coords:
        coords = TOKYO_STATION_FALLBACK
 
    _GEOCODE_CACHE[name] = coords
    return coords
 
 
def seed_geocode_cache(name: str, lon: Any, lat: Any) -> None:
    """
    診断段階（OSM Overpass / TripAdvisor / Google Places）で既に取得済みの実座標を、
    ジオコーディングキャッシュへ直接投入する。
 
    以前は候補地の実座標（raw_places由来のlat/lon）を診断結果からそのまま捨ててしまい、
    旅程生成（build_itinerary）側で候補地名だけを使って再度ジオコーディングし直していた。
    「洋食 ミラノ亭」のような店名は全国に同名・類似名の店が存在しうるため、店名単体の
    再ジオコーディングでは診断時に見つけた実店舗とは全く違う（数百km離れた）場所に
    解決されてしまうことがあり、それが移動時間の異常値（例: 900分）や、それに起因する
    制約違反表示の主因になっていた。取得済みの実座標を信頼してそのまま使うことで、
    この種の誤ジオコーディングを構造的に防ぐ。
    """
    if not name or lon is None or lat is None:
        return
    try:
        _GEOCODE_CACHE[name] = (float(lon), float(lat))
    except (TypeError, ValueError):
        pass
 
 
async def estimate_travel_time_and_cost(p1_name: str, p2_name: str, transport_mode: str = "transit") -> Dict[str, Any]:
    coord1 = await geocode_location(p1_name)
    coord2 = await geocode_location(p2_name)
 
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
                        travel_min = estimate_transit_minutes(dist_km)
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
 
    # OSRMが使えない場合も移動手段ごとに現実的な所要時間を見積もる（式(3)(4)の制約判定に直結するため）
    if transport_mode == "transit":
        travel_min = max(15, estimate_transit_minutes(dist_km))
    elif transport_mode == "driving":
        travel_min = max(10, int((dist_km / 35.0) * 60) + 10)
    elif transport_mode == "walking":
        travel_min = max(5, int((dist_km / 4.5) * 60))
    else:
        travel_min = max(15, int((dist_km / 30.0) * 60) + 15)
 
    return {
        "travel_time": travel_min,
        "travel_cost": int(dist_km * 25),
        "distance_km": dist_km,
        "is_osrm": False
    }
 
 
async def compute_worst_case_travel_time(start_location: str, stop_names: List[str], transport_mode: str,
                                          trip_type: str, pace_factor: float = 1.0,
                                          end_location: str = "") -> int:
    """
    式(11) Δt = 最悪の巡回順による総移動時間 − f2(R) のベースラインを算出する。
 
    全訪問地点間の移動時間行列を一度だけ並列計算した上で、順列探索により総移動時間が
    最大となる巡回順（＝最悪ケース）を求める。
    end_location（出発地と異なる到着地点の明示指定）が与えられた場合、実際の旅程側
    （solve_itinerary_order）と同様にそこを固定の最終目的地として扱う。これを揃えて
    おかないと、実際のf2(R)は到着地点までの区間を含むのに最悪ケース側は含まない、
    といった非対称な比較になりΔtが不当に大きく出てしまう。
    地点数が多い場合は全探索が高コストになるため乱択サンプリングで近似する。
    """
    nodes = [start_location] + list(stop_names)
    end_node_idx = None
    if end_location and end_location != start_location:
        nodes = nodes + [end_location]
        end_node_idx = len(nodes) - 1
 
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
 
    # 巡回順として並べ替える対象は「実際に訪問するスポット」のみ。end_locationは
    # 常に最後に固定（=実際の旅程側と同じ扱い）で、並べ替え対象には含めない。
    target_idxs = list(range(1, len(stop_names) + 1))
 
    def path_time(order) -> int:
        total = matrix[(0, order[0])]
        for a, b in zip(order, order[1:]):
            total += matrix[(a, b)]
        if end_node_idx is not None:
            total += matrix[(order[-1], end_node_idx)]
        elif trip_type == "round_trip":
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
 
 
async def annotate_feasibility(places: List[Dict[str, Any]], start_location: str, end_location: str,
                                transport_mode: str, trip_type: str, start_time: str,
                                budget_limit: Any, time_limit: Any, member_count: int = 1) -> None:
    """
    診断（TOP3提案）の段階で、各候補地を「その候補地1件だけを主体験ピークとして訪問する」
    最小構成の旅程として見積もり、ユーザーが指定した出発時間・出発地点・到着地点・費用・
    移動手段・行程タイプの制約に実際に収まるかどうかを判定する。
 
    以前はこれらの制約を一切考慮せず好みの一致度（式2のコサイン類似度）だけでTOP3を選んでいた
    ため、選んでから旅程を作って初めて「予算オーバー」「営業時間外」等が判明する問題があった。
    ここでの判定は経由地を含まない下限側の楽観的な見積もり（経由地を追加すればさらに費用・時間は
    増えうる）だが、少なくとも明らかに成立しない候補を提案段階で除外・可視化できるようにする。
 
    places の各要素に副作用として以下を書き込む:
        fits_constraints          : bool
        estimated_total_cost      : int
        estimated_total_time_minutes : int
        violation_reasons         : List[str]
        violation_score           : float（0=制約内。値が小さいほど「惜しい」候補）
    """
    if not places:
        return
    try:
        start_h, start_m = map(int, (start_time or "09:00").split(":"))
        start_time_min = start_h * 60 + start_m
    except (ValueError, AttributeError):
        start_time_min = 9 * 60
 
    end_loc = (end_location or "").strip()
    has_distinct_end = bool(end_loc) and end_loc != start_location
    member_count = member_count or 1
    sem = asyncio.Semaphore(8)
 
    async def evaluate(place: Dict[str, Any]) -> None:
        name = place["name"]
        # 診断で既に実座標が分かっている候補地は、店名の再ジオコーディングによる
        # 取り違え（同名の別店舗に解決される等）を避けるためキャッシュに投入しておく。
        if place.get("lat") is not None and place.get("lon") is not None:
            seed_geocode_cache(name, place["lon"], place["lat"])
 
        async with sem:
            go = await estimate_travel_time_and_cost(start_location, name, transport_mode)
 
        return_time = 0
        return_cost = 0
        if has_distinct_end:
            async with sem:
                back = await estimate_travel_time_and_cost(name, end_loc, transport_mode)
            return_time, return_cost = back["travel_time"], back["travel_cost"]
        elif trip_type == "round_trip":
            async with sem:
                back = await estimate_travel_time_and_cost(name, start_location, transport_mode)
            return_time, return_cost = back["travel_time"], back["travel_cost"]
        # trip_type == "one_way" かつ到着地点未指定の場合、この候補地自体が終着点なので復路は無い
 
        stay_min = place.get("t_base", TAU_TOUR_MINUTES)
        arrival_min = start_time_min + go["travel_time"]
        hours_ok = check_open_hours(arrival_min, stay_min,
                                     place.get("open_hour", DEFAULT_OPEN_HOUR),
                                     place.get("close_hour", DEFAULT_CLOSE_HOUR))
 
        total_time = go["travel_time"] + stay_min + return_time
        total_cost = (go["travel_cost"] + return_cost) * member_count + place.get("cost", 0) * member_count
 
        reasons: List[str] = []
        violation_score = 0.0
        if budget_limit is not None and total_cost > budget_limit:
            reasons.append("予算超過")
            violation_score += (total_cost - budget_limit) / max(float(budget_limit), 1.0)
        if time_limit is not None and total_time > time_limit:
            reasons.append("時間上限を超過")
            violation_score += (total_time - time_limit) / max(float(time_limit), 1.0)
        if not hours_ok:
            reasons.append("営業時間外の可能性")
            violation_score += 1.0
 
        place["fits_constraints"] = (len(reasons) == 0)
        place["estimated_total_cost"] = total_cost
        place["estimated_total_time_minutes"] = total_time
        place["violation_reasons"] = reasons
        place["violation_score"] = round(violation_score, 3)
 
    await asyncio.gather(*[evaluate(p) for p in places])
 
 
# ---------------------------------------------------------------------------
# 式(9) 多目的制約充足求解: maximize f1(R,d), minimize f2(R), minimize f3(R) s.t. 式(3)〜(5)
#
# 従来の実装は、訪問順序Rを最適化せず「経由地→選択スポット」という入力順のまま
# 逐次シミュレーションしていた（=Rが常に固定で、Step-5の求解が実質存在しなかった）。
# これだと体験ピーク位置（前半/中盤/後半）を選んでも、メインピークが常に最後の訪問地に
# なってしまい、式(7)のω(k;π)がρ*と噛み合わず式(6)のf1が意図通りに機能しない、という
# 副作用も生んでいた。ここでは論文5.5節が明記する「遺伝的アルゴリズム（個体数100、
# 世代数200）」に沿って、訪問順序Rを実際に探索する簡易GAを実装する。
# 真のNSGA-II（非優越ソート＋混雑度）までは実装していないが、f1最大化・f2/f3最小化・
# 制約(3)〜(5)違反へのペナルティを1つのスカラー適応度に重み付き統合する近似版として、
# 「訪問順序が一切最適化されない」状態からは大きく前進する。
# ---------------------------------------------------------------------------
 
GA_POPULATION_SIZE = 100   # 論文5.5節の個体数に合わせる
GA_GENERATIONS = 200       # 論文5.5節の世代数に合わせる
GA_MUTATION_RATE = 0.15
GA_ELITE_SIZE = 5
GA_TOURNAMENT_K = 3
 
 
async def build_travel_matrix(node_names: List[str], transport_mode: str) -> Dict[tuple, Dict[str, Any]]:
    """
    node_names の全ペア間の移動時間・移動費用を一度だけ並列取得し、行列として返す。
    GA は世代を重ねるごとに大量の順序候補を評価する必要があるが、そのたびに
    estimate_travel_time_and_cost（ジオコーディング+OSRM）を呼んでいては現実的な
    時間で終わらない。事前に1回だけ全ペアを取得し、以降はメモリ上の行列参照だけで
    評価できるようにする（compute_worst_case_travel_timeと同じ発想）。
    """
    n = len(node_names)
    matrix: Dict[tuple, Dict[str, Any]] = {}
    if n <= 1:
        return matrix
 
    pairs = [(i, j) for i in range(n) for j in range(n) if i != j]
    sem = asyncio.Semaphore(8)
 
    async def fetch(i: int, j: int):
        async with sem:
            info = await estimate_travel_time_and_cost(node_names[i], node_names[j], transport_mode)
            return i, j, info["travel_time"], info["travel_cost"]
 
    results = await asyncio.gather(*[fetch(i, j) for i, j in pairs])
    for i, j, t, c in results:
        matrix[(i, j)] = {"time": t, "cost": c}
    return matrix
 
 
def _simulate_order(order: List[int], matrix: Dict[tuple, Dict[str, Any]], targets: List[Dict[str, Any]],
                     tail_idx: Any, start_time_min: int, pace_factor: float, member_count: int,
                     peak_position: str, main_peak_ids: set) -> Dict[str, Any]:
    """
    与えられた訪問順序(order: targetsのインデックス1..Nの並び)を実際にシミュレーションし、
    式(3)〜(6)に基づく各種指標とitinerary_nodesを算出する。GAの適応度計算と、
    最終的に採用する旅程の両方で共通して使う。
    """
    total_cost = 0
    total_move_cost = 0
    total_time = 0
    total_move_time = 0
    current_min = start_time_min
    current_idx = 0  # 0 = 出発地
    hours_checks: List[bool] = []
    f1_nodes: List[Dict[str, Any]] = []
    itinerary_nodes: List[Dict[str, Any]] = []
    K = len(order)
 
    for pos, node_idx in enumerate(order):
        target = targets[node_idx - 1]
        leg = matrix[(current_idx, node_idx)]
        move_time = int(leg["time"] * pace_factor)
        move_cost = leg["cost"] * member_count
 
        itinerary_nodes.append({
            "type": "transit",
            "from_idx": current_idx,
            "to_idx": node_idx,
            "duration_minutes": move_time,
            "cost": move_cost
        })
 
        current_min += move_time
        arrival_min = current_min
        total_move_time += move_time
        total_time += move_time
        total_move_cost += move_cost
        total_cost += move_cost
 
        is_custom_wp = target.get("is_custom_waypoint", False)
        is_peak = (target["id"] in main_peak_ids) and not is_custom_wp
 
        base_stay = TAU_PEAK_MINUTES if is_peak else (45 if is_custom_wp else TAU_TOUR_MINUTES)
        stay_d = int(base_stay * pace_factor)
        # 式(7): ρk = k/K （kは1始まりの訪問順位置）をこの候補順序で実際に評価する
        w_k = get_peak_weight(pos + 1, K, peak_position)
 
        hours_ok = check_open_hours(arrival_min, stay_d, target.get("open_hour", DEFAULT_OPEN_HOUR),
                                     target.get("close_hour", DEFAULT_CLOSE_HOUR))
        hours_checks.append(hours_ok)
 
        total_time += stay_d
        total_cost += target.get("cost", 1000) * member_count
 
        if not is_custom_wp and target.get("score") is not None:
            f1_nodes.append({
                "score": target["score"] / 100.0,
                "stay_minutes": stay_d,
                "t_base": target.get("t_base", stay_d),
                "peak_weight": w_k
            })
 
        itinerary_nodes.append({
            "type": "spot",
            "target_idx": node_idx,
            "stay_minutes": stay_d,
            "is_peak": is_peak,
            "is_custom_waypoint": is_custom_wp,
            "weight": round(w_k, 2),
            "arrival_minutes": arrival_min,
            "hours_satisfied": hours_ok
        })
 
        current_min += stay_d
        current_idx = node_idx
 
    if tail_idx is not None:
        leg = matrix[(current_idx, tail_idx)]
        move_time = int(leg["time"] * pace_factor)
        move_cost = leg["cost"] * member_count
        itinerary_nodes.append({
            "type": "transit",
            "from_idx": current_idx,
            "to_idx": tail_idx,
            "duration_minutes": move_time,
            "cost": move_cost
        })
        total_move_time += move_time
        total_time += move_time
        total_move_cost += move_cost
        total_cost += move_cost
 
    f1_total = compute_f1(f1_nodes)
 
    return {
        "order": order,
        "itinerary_nodes": itinerary_nodes,
        "total_cost": total_cost,
        "total_move_cost": total_move_cost,
        "total_time": total_time,
        "total_move_time": total_move_time,
        "hours_checks": hours_checks,
        "f1_total": f1_total,
    }
 
 
def _fitness(sim: Dict[str, Any], budget_limit: float, time_limit: float) -> float:
    """
    式(9)の3目的（f1最大化・f2最小化・f3最小化）と式(3)〜(5)の制約充足を、
    GAが扱える1つのスカラー適応度に重み付き統合する。制約違反は大きめのペナルティで
    強く抑制し、実行可能解を優先しつつ、実行可能解の中ではf1を主目的として
    移動時間・移動費用が小さいほど加点する。
    """
    over_budget = max(0.0, sim["total_cost"] - budget_limit)
    over_time = max(0.0, sim["total_time"] - time_limit)
    hours_violations = sim["hours_checks"].count(False)
 
    penalty = 0.0
    penalty += 3.0 * (over_budget / max(budget_limit, 1.0))
    penalty += 3.0 * (over_time / max(time_limit, 1.0))
    penalty += 1.0 * hours_violations
 
    norm_time = sim["total_move_time"] / max(time_limit, 1.0)
    norm_cost = sim["total_move_cost"] / max(budget_limit, 1.0)
 
    return sim["f1_total"] - 0.5 * norm_time - 0.5 * norm_cost - penalty
 
 
def _order_crossover(parent_a: List[int], parent_b: List[int], rnd: random.Random) -> List[int]:
    """順列同士の交叉（Order Crossover, OX）。訪問順序という並び替え問題向けの標準的な手法。"""
    n = len(parent_a)
    if n < 2:
        return list(parent_a)
    i, j = sorted(rnd.sample(range(n), 2))
    child: List[Any] = [None] * n
    child[i:j + 1] = parent_a[i:j + 1]
    fill_values = [g for g in parent_b if g not in child[i:j + 1]]
    fill_iter = iter(fill_values)
    for k in range(n):
        if child[k] is None:
            child[k] = next(fill_iter)
    return child
 
 
def _mutate(order: List[int], rnd: random.Random) -> List[int]:
    order = list(order)
    if len(order) >= 2 and rnd.random() < GA_MUTATION_RATE:
        i, j = rnd.sample(range(len(order)), 2)
        order[i], order[j] = order[j], order[i]
    return order
 
 
def solve_itinerary_order(matrix: Dict[tuple, Dict[str, Any]], targets: List[Dict[str, Any]],
                           tail_idx: Any, start_time_min: int, pace_factor: float, member_count: int,
                           peak_position: str, main_peak_ids: set, budget_limit: float, time_limit: float,
                           target_indices: List[int] = None,
                           population_size: int = GA_POPULATION_SIZE, generations: int = GA_GENERATIONS,
                           seed: int = 7) -> Dict[str, Any]:
    """
    式(9)の多目的制約充足問題を、訪問順序Rを個体とする遺伝的アルゴリズムで解く。
    targetsのインデックス(1..N)の並び替え(=訪問順序R)を探索し、_fitnessが最大となる
    個体を返す。Nが小さい（実運用ではメインスポット1件＋経由地数件程度）ため、
    100個体×200世代でも計算はミリ秒〜数十ミリ秒オーダーで終わる。
 
    target_indices: 実際に訪問する対象を、targetsの中の一部（部分集合）に限定したい場合の
    インデックス(1始まり)のリスト。省略時は従来通りtargets全件を訪問する前提で並び替える。
    solve_itinerary_with_dropping() が、経由地の一部を間引いた部分集合を探索する際に使う。
    """
    if target_indices is None:
        target_indices = list(range(1, len(targets) + 1))
    n = len(target_indices)
    base_order = list(target_indices)
 
    if n <= 1:
        # 並べ替える余地が無いので、そのままシミュレーションして返す
        sim = _simulate_order(base_order, matrix, targets, tail_idx, start_time_min,
                               pace_factor, member_count, peak_position, main_peak_ids)
        return sim
 
    rnd = random.Random(seed)
 
    def make_individual() -> List[int]:
        ind = base_order[:]
        rnd.shuffle(ind)
        return ind
 
    population: List[List[int]] = [base_order[:]]  # 入力順を1個体として必ず含める（従来動作より悪化しない保証）
    while len(population) < population_size:
        population.append(make_individual())
 
    def evaluate(order: List[int]) -> float:
        sim = _simulate_order(order, matrix, targets, tail_idx, start_time_min,
                               pace_factor, member_count, peak_position, main_peak_ids)
        return _fitness(sim, budget_limit, time_limit)
 
    scored = [(evaluate(ind), ind) for ind in population]
    best_fitness = max(s for s, _ in scored)
    stall = 0
 
    for _ in range(generations):
        scored.sort(key=lambda x: x[0], reverse=True)
        elites = [ind for _, ind in scored[:GA_ELITE_SIZE]]
 
        def tournament_pick() -> List[int]:
            contenders = rnd.sample(scored, min(GA_TOURNAMENT_K, len(scored)))
            return max(contenders, key=lambda x: x[0])[1]
 
        next_population = list(elites)
        while len(next_population) < population_size:
            parent_a = tournament_pick()
            parent_b = tournament_pick()
            child = _order_crossover(parent_a, parent_b, rnd)
            child = _mutate(child, rnd)
            next_population.append(child)
 
        scored = [(evaluate(ind), ind) for ind in next_population]
        gen_best = max(s for s, _ in scored)
        if gen_best > best_fitness + 1e-9:
            best_fitness = gen_best
            stall = 0
        else:
            stall += 1
        if stall >= 40:  # 40世代改善が無ければ収束とみなして打ち切る（不要な計算を避ける）
            break
 
    scored.sort(key=lambda x: x[0], reverse=True)
    best_order = scored[0][1]
    return _simulate_order(best_order, matrix, targets, tail_idx, start_time_min,
                            pace_factor, member_count, peak_position, main_peak_ids)
 
 
def _value_density(target: Dict[str, Any]) -> float:
    """
    間引き候補としての「優先度の低さ」を表す指標。値が小さいほど先に間引かれる。
    手動追加の経由地（is_custom_waypoint）は式(6)のf1に一切寄与しない（呼び出し側で
    スコア無しとして扱われる）ため、常に最優先で間引き対象にする。スコア付き候補地
    （メインピーク以外にAIが自動提案したもの）は「スコア ÷ (入場料+滞在時間)」、
    つまり消費するコスト・時間の割に満足度への貢献が薄いものから間引く。
    """
    if target.get("is_custom_waypoint"):
        return -1.0
    score = target.get("score") or 0.0
    denom = max(1.0, target.get("cost", 0) + target.get("t_base", 0))
    return score / denom
 
 
def solve_itinerary_with_dropping(matrix: Dict[tuple, Dict[str, Any]], targets: List[Dict[str, Any]],
                                   required_ids: set, tail_idx: Any, start_time_min: int, pace_factor: float,
                                   member_count: int, peak_position: str, main_peak_ids: set,
                                   budget_limit: float, time_limit: float) -> Dict[str, Any]:
    """
    式(3)〜(5)の制約（予算・時間・営業時間）に実際に収まる旅程を組み立てる。
 
    以前は全ての訪問対象（メインピーク＋経由地）を固定した上で、その並び順だけを
    最適化していたため、対象そのものが多すぎて予算・時間に収まらない場合、警告を
    出すだけでその旅程をそのまま返していた。ここでは main_peak（required_ids）だけは
    必ず残しつつ、経由地（AIが自動提案したもの／ユーザーが手動追加したもの）を
    「価値が低いものから」1件ずつ自動的に間引きながら再探索し、実際に制約内に
    収まる旅程を優先して返す。全ての経由地を間引いてもメインピーク単体で制約を
    満たせない場合（＝目的地そのものが遠すぎる等）は、それ以上削れないため、
    その時点の（制約違反込みの）結果をそのまま返す。
 
    戻り値は _simulate_order と同じ辞書に "dropped_names"（間引かれた経由地名のリスト）
    を加えたもの。
    """
    active_indices = list(range(1, len(targets) + 1))
    dropped_names: List[str] = []
 
    def is_required(idx: int) -> bool:
        return targets[idx - 1]["id"] in required_ids
 
    while True:
        sim = solve_itinerary_order(
            matrix, targets, tail_idx, start_time_min, pace_factor, member_count,
            peak_position, main_peak_ids, budget_limit, time_limit,
            target_indices=active_indices,
        )
        over_budget = sim["total_cost"] > budget_limit
        over_time = sim["total_time"] > time_limit
        hours_ok = all(sim["hours_checks"]) if sim["hours_checks"] else True
        feasible = (not over_budget) and (not over_time) and hours_ok
 
        droppable = [i for i in active_indices if not is_required(i)]
        if feasible or not droppable:
            sim["dropped_names"] = dropped_names
            return sim
 
        worst_idx = min(droppable, key=lambda i: _value_density(targets[i - 1]))
        dropped_names.append(targets[worst_idx - 1]["name"])
        active_indices = [i for i in active_indices if i != worst_idx]

