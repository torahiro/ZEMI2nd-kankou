# main.py
import os
import secrets
import asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
 
import db
 
from dynamic_fetcher import fetch_places_dynamically, analyze_reviews_and_build_vector
from optimizer import (
    extract_phi_t,
    cosine_similarity,
    build_psi_b,
    combine_preference_vector,
    check_constraints,
    compute_worst_case_travel_time,
    build_travel_matrix,
    solve_itinerary_order,
    solve_itinerary_with_dropping,
    geocode_location,
    seed_geocode_cache,
    annotate_feasibility,
    TAU_PEAK_MINUTES,
    TAU_TOUR_MINUTES,
    DEFAULT_OPEN_HOUR,
    DEFAULT_CLOSE_HOUR,
    INDIVIDUAL_BOOKING_MARKUP,
    ALPHA_PREFERENCE,
)
 
# 季節(season)×「その時期ならではのことをしたい(seasonal)」入力を嗜好ベクトルuに反映するための
# 簡易対応表。実データに基づく季節性推定ではなく、ドメイン知識による近似（カテゴリはgourmet/
# sightseeing/healingの順）。以前はseasonがAPIに送られてくるのに一切使われていなかった。
SEASON_CATEGORY_BOOST: Dict[str, List[float]] = {
    "spring": [0.0, 0.3, 0.0],   # 花見・行楽シーズン → 観光
    "summer": [0.1, 0.2, 0.0],   # 夏祭り・屋外観光 → 観光やや＋グルメ少々
    "autumn": [0.0, 0.3, 0.1],   # 紅葉狩り → 観光、味覚の秋 → 癒し少々
    "winter": [0.0, 0.0, 0.3],   # 温泉シーズン → 癒し
}
 
# サーバーを再起動せずファイルだけ差し替えても、実際に動いているのは起動時に読み込んだ
# 古いコードのまま……という見落としが繰り返し発生したため、目視で確認できる目印を用意する。
# ファイルを更新するたびにこの文字列を変え、起動ログと /version エンドポイントで
# 「今動いているのは本当に最新版か」をすぐ確認できるようにする。
APP_CODE_VERSION = "2026-09-10-local-auth-1"
print(f"[main.py] loaded. APP_CODE_VERSION = {APP_CODE_VERSION}")
 
app = FastAPI(title="Tourism Itinerary Generator API")
 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
 
# ------------------------------------------------------------------
# Googleログイン（OAuth 2.0 / OpenID Connect）
#
# ログインセッションはCookieに署名して保持する方式（Starlette標準のSessionMiddleware）。
# この署名に使うSECRET_KEYが環境変数SESSION_SECRET_KEYとして固定値で設定されていないと、
# サーバーが再起動するたびに鍵が変わり、ログイン中の利用者が全員ログアウト扱いになってしまう。
# Renderにデプロイする際は必ずSESSION_SECRET_KEYを設定すること（render.yaml参照）。
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "")
if not SESSION_SECRET_KEY:
    SESSION_SECRET_KEY = secrets.token_urlsafe(32)
    print("[main.py] 警告: 環境変数 SESSION_SECRET_KEY が未設定のため、起動のたびに変わる一時的な"
          "鍵でセッションを署名しています。このままではサーバーを再起動するたびに全員が"
          "ログアウトされます。本番運用ではSESSION_SECRET_KEYを固定の値で設定してください。")
 
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax")
 
# Google Cloud ConsoleでOAuthクライアントID/シークレットを発行し、環境変数に設定する。
# 未設定の場合はアプリ自体は通常通り動作し、ログイン関連のエンドポイントだけが
# 503（設定未完了）を返す（他の全機能はログイン無しでも従来通り使える設計を維持する）。
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
 
oauth = OAuth()
if GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
else:
    print("[main.py] 警告: GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET が未設定のため、"
          "Googleログイン機能は無効です（/auth/login は503を返します）。")
 
db.init_db()
 
 
def _current_session_user(request: Request) -> Optional[Dict[str, Any]]:
    return request.session.get("user")
 
 
def _require_login(request: Request) -> Dict[str, Any]:
    user = _current_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="ログインが必要です。")
    return user
 
 
@app.get("/auth/login")
async def auth_login(request: Request):
    # ページの入り口自体をログイン必須にしたため、ここで生のJSONエラーを返すと
    # 利用者がアプリに一切たどり着けなくなる。必ずlogin.htmlへ戻し、設定不足である旨を
    # その場で案内する（サーバー管理者がGOOGLE_OAUTH_CLIENT_ID/SECRETを設定すれば解消する）。
    if not (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET):
        return RedirectResponse(url="/login.html?error=config")
    # request.url_for("auth_callback") はリバースプロキシ経由だとhttp/httpsの判定を誤ることがある。
    # RenderではUvicornを --proxy-headers 付きで起動し、X-Forwarded-Protoを信用させる必要がある
    # （render.yamlのstartCommand参照）。ここがずれるとGoogle側で「redirect_uri_mismatch」になる。
    redirect_uri = str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)
 
 
@app.get("/auth/callback")
async def auth_callback(request: Request):
    # ページの入り口自体をログイン必須にしたため、ここで生のJSONエラーを返すと
    # 利用者が完全に行き詰まってしまう。失敗時は必ずlogin.htmlへ戻し、
    # そこでエラーメッセージを表示して再挑戦できるようにする。
    if not (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET):
        return RedirectResponse(url="/login.html?error=config")
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        print(f"[auth_callback] Googleとのトークン交換に失敗しました: {e}")
        return RedirectResponse(url="/login.html?error=oauth")
 
    userinfo = token.get("userinfo") or {}
    google_sub = userinfo.get("sub")
    if not google_sub:
        return RedirectResponse(url="/login.html?error=oauth")
 
    email = userinfo.get("email", "") or ""
    name = userinfo.get("name", "") or ""
    picture = userinfo.get("picture", "") or ""
 
    # upsert_google_userは、セッションにそのまま保存できる共通形式（id/provider/email/name/picture）の
    # 辞書を返す。id は "google:<google_sub>" という形式で、ユーザー名・パスワード認証の
    # "local:<username>" と衝突しないようにしている（favorites/reviewsのuser_idもこのidで統一）。
    user = await asyncio.to_thread(db.upsert_google_user, google_sub, email, name, picture)
 
    request.session["user"] = user
    return RedirectResponse(url="/")
 
 
@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url="/")
 
 
@app.get("/auth/me")
async def auth_me(request: Request):
    user = _current_session_user(request)
    return {"logged_in": bool(user), "user": user}
 
 
# ------------------------------------------------------------------
# ユーザー名・パスワード認証（Googleログインの代替・簡易ルート）
#
# Googleログインは毎回Google Cloud ConsoleでのOAuthクライアント設定が必要になり、
# ローカル開発やデプロイのたびの手間になっていた。外部サービスに依存しない
# シンプルな自前認証（ユーザー参考のPHP実装と同様の考え方）を追加し、
# Googleログインと同じセッション（request.session["user"]）にログインできるようにする。
# パスワードはPython標準のhashlibのみでPBKDF2-HMAC-SHA256ハッシュ化して保存し、
# 平文はDBは元よりログにも一切出力しない。
# ------------------------------------------------------------------
class LocalAuthRequest(BaseModel):
    username: str
    password: str
 
 
@app.post("/auth/local/signup")
async def local_signup(request: Request, body: LocalAuthRequest):
    username = body.username.strip()
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="ユーザー名は3文字以上で入力してください。")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="パスワードは8文字以上で入力してください。")
 
    user = await asyncio.to_thread(db.create_local_user, username, body.password)
    if user is None:
        raise HTTPException(status_code=409, detail="そのユーザー名は既に使われています。別のユーザー名をお試しください。")
 
    request.session["user"] = user
    return {"status": "ok", "user": user}
 
 
@app.post("/auth/local/login")
async def local_login(request: Request, body: LocalAuthRequest):
    user = await asyncio.to_thread(db.authenticate_local_user, body.username.strip(), body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="ユーザー名またはパスワードが正しくありません。")
 
    request.session["user"] = user
    return {"status": "ok", "user": user}
 
 
# ------------------------------------------------------------------
# お気に入り（ログイン中のアカウントに紐付けて保存する）。
# ログインしていない利用者は従来通りブラウザのlocalStorageのみで運用を続けられるため、
# ここは401を返すだけで、フロントエンド側でlocalStorageにフォールバックする。
# ------------------------------------------------------------------
class FavoriteCreate(BaseModel):
    title: str
    data: Dict[str, Any]
 
 
@app.get("/favorites")
async def list_favorites(request: Request):
    user = _require_login(request)
    favorites = await asyncio.to_thread(db.list_favorites, user["id"])
    return {"favorites": favorites}
 
 
@app.post("/favorites")
async def create_favorite(request: Request, favorite: FavoriteCreate):
    user = _require_login(request)
    favorite_id = await asyncio.to_thread(db.add_favorite, user["id"], favorite.title, favorite.data)
    return {"status": "ok", "id": favorite_id}
 
 
@app.delete("/favorites/{favorite_id}")
async def remove_favorite(request: Request, favorite_id: int):
    user = _require_login(request)
    deleted = await asyncio.to_thread(db.delete_favorite, user["id"], favorite_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="指定されたお気に入りが見つからないか、削除する権限がありません。")
    return {"status": "ok"}
 
 
# ------------------------------------------------------------------
# フロントエンド（login.html / intoro.html / app.js / style.css）を同じサービスから配信する。
# バックエンドAPIとフロントエンドを別々にデプロイすると、フロントエンドが
# 「どのURLのAPIを呼べばいいか」を知る必要が出てCORSやURL管理が煩雑になるため、
# この1つのFastAPIサービスだけで両方まとめて配信する構成にしている。
#
# 「ページを開いた最初にログインページにして、そこからアプリを使えるようにしたい」という
# 要望に対応し、"/" はログイン状態を見て出し分ける。未ログインならlogin.html（Googleログイン
# ボタンのみの単独ページ）、ログイン済みならこれまで通りintoro.html（アプリ本体）を返す。
# 以前は「/favorites等のAPIだけ401」で、アプリ本体（intoro.html）自体は未ログインでも
# 誰でも開けたが、今回のご要望でページの入り口自体をログイン必須にする形へ変更した。
# なお診断・旅程生成などのAPIエンドポイント自体には引き続きログイン必須化はしていない
# （UIからしか事実上たどり着けないため）。API単体にも認証を必須にしたい場合は別途対応する。
# ------------------------------------------------------------------
@app.get("/")
async def root(request: Request):
    if not _current_session_user(request):
        return FileResponse("login.html")
    return FileResponse("intoro.html")
 
 
@app.get("/login.html")
async def serve_login_html():
    return FileResponse("login.html")
 
 
@app.get("/app.js")
async def serve_app_js():
    return FileResponse("app.js", media_type="application/javascript")
 
 
@app.get("/style.css")
async def serve_style_css():
    return FileResponse("style.css", media_type="text/css")
 
 
@app.get("/version")
async def version():
    """サーバーが実際に読み込んでいるコードのバージョンを確認するためのデバッグ用エンドポイント。
    ブラウザで http://localhost:8000/version を開く、または curl で叩くと確認できる。
    ここに表示される値がお送りしたファイルのAPP_CODE_VERSIONと一致していなければ、
    ファイルの差し替えが反映されていない（サーバーの再起動が必要）ことが分かる。"""
    return {"version": APP_CODE_VERSION}
 
 
# ------------------------------------------------------------------
# 口コミ・評価の収集（★評価＋自由記述の口コミを、今後のデータ活用のために永続化する）。
#
# 以前はJSON Linesファイルへの追記だったが、Googleログイン導入にあわせて
# 「誰の口コミか」を紐付けられるよう db.py（SQLite）へ移行した。ログインしていない
# 利用者の口コミも引き続き受け付ける（user_idがNULLの匿名レコードとして保存）。
# ------------------------------------------------------------------
class ReviewSubmission(BaseModel):
    rating: int                              # 1〜5の★評価（必須）
    review_text: Optional[str] = ""          # 自由記述の口コミ（任意）
    final_destination: Optional[str] = ""
    start_location: Optional[str] = ""
    transport_mode: Optional[str] = ""
    trip_type: Optional[str] = ""
    member_count: Optional[int] = None
    total_cost: Optional[float] = None
    total_time_minutes: Optional[float] = None
 
 
@app.post("/submit_review")
async def submit_review(request: Request, review: ReviewSubmission):
    if review.rating < 1 or review.rating > 5:
        raise HTTPException(status_code=400, detail="評価は1〜5の範囲で指定してください。")
 
    user = _current_session_user(request)
    record = {
        "user_id": user["id"] if user else None,
        "rating": review.rating,
        "review_text": (review.review_text or "").strip(),
        "final_destination": review.final_destination or "",
        "start_location": review.start_location or "",
        "transport_mode": review.transport_mode or "",
        "trip_type": review.trip_type or "",
        "member_count": review.member_count,
        "total_cost": review.total_cost,
        "total_time_minutes": review.total_time_minutes,
    }
 
    try:
        await asyncio.to_thread(db.add_review, record)
    except Exception as e:
        print(f"[submit_review] 保存に失敗しました: {e}")
        raise HTTPException(status_code=500, detail="評価の保存中にエラーが発生しました。")
 
    return {"status": "ok"}
 
 
@app.get("/reviews_summary")
async def reviews_summary_endpoint():
    """蓄積された口コミ・評価の簡易集計。今後のデータ活用の第一歩として、
    件数と平均評価だけをまず確認できるようにしている。"""
    return await asyncio.to_thread(db.reviews_summary)
 
 
class DiagnoseRequest(BaseModel):
    user_text: str
    # 旧UI互換用の3段階重み（非推奨）。detailed_vector が渡された場合はそちらを優先する。
    preferences_4step: Optional[List[float]] = None
    detailed_vector: Dict[str, float]
    target_area: str
    season: str
    generation_group: str
    custom_reviews_text: Optional[str] = ""
    # 出発時間・出発地点・到着地点・費用・移動手段・行程タイプの制約（すべて任意）。
    # 指定された場合、これらの制約内に収まる候補地を優先してTOP3を提案する
    # （未指定の場合は従来通り好みの一致度のみでTOP3を選ぶ＝後方互換）。
    start_location: Optional[str] = ""
    end_location: Optional[str] = ""
    start_time: Optional[str] = ""
    transport_mode: Optional[str] = "transit"
    trip_type: Optional[str] = "round_trip"
    budget_limit: Optional[float] = None
    time_limit: Optional[float] = None
    member_count: Optional[int] = 1
 
class GenerateItineraryRequest(BaseModel):
    selected_place_ids: List[str]
    candidate_places: List[Dict[str, Any]]
    custom_waypoints: List[str]
    start_location: str
    # 出発地と異なる到着地点（任意）。空文字/未指定なら従来通りtrip_typeで終着点を決める。
    end_location: Optional[str] = ""
    start_time: str
    end_time: Optional[str] = ""
    trip_type: str
    transport_mode: str
    generation_group: str
    member_count: int
    peak_position: str
    budget_limit: float
    time_limit: float
    # 「10項目こだわり」入力（診断時と同じもの）。packed_schedule/relax_scheduleを
    # 滞在時間ペースに反映するために旅程生成側でも受け取る。
    detailed_vector: Optional[Dict[str, float]] = None
    # 診断時に指定したエリア名（任意）。custom_waypoints等、座標が未知のユーザー自由入力
    # 地名のジオコーディング精度を上げるための補助情報として使う（geocode_locationのarea_hint）。
    target_area: Optional[str] = ""
    # candidate_placesの中で「唯一の主体験ピーク」となる候補地のid（任意）。
    # 以前はTOP3から選んだ1件だけがcandidate_placesに入り、それがそのままメインピークだったが、
    # TOP3のうち選ばなかった残りをAIが自動提案する経由地としてcandidate_placesに含められるように
    # なったため、その中のどれが「本当のメインピーク」（式7・8のτ^peak/ピーク重みを適用する対象）
    # かを明示するために追加。未指定時はcandidate_places[0]をメインピークとして扱う（後方互換）。
    main_peak_place_id: Optional[str] = ""
 
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
    # 以前は対象エリア未入力時に「箱根温泉」を無言で補完していたが、ユーザーが
    # 意図せずデフォルト値のまま提案を実行してしまい、後段の旅程生成で
    # （実際には指定していないはずの）箱根エリアの結果が出てくる混乱の原因になっていた。
    # フロント側でも必須入力チェックを行うが、API単体で叩かれた場合の保険として
    # ここでも明示的にエラーを返す。
    area = req.target_area.strip()
    if not area:
        raise HTTPException(status_code=400, detail="対象エリア（旅行先）を入力してください。")
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
 
    # 「その時期ならではのことをしたい(seasonal)」× season(季節) をuに反映する。
    # 以前はseasonがリクエストに含まれているのに一切使われていなかった。
    seasonal_weight = max(0.0, (req.detailed_vector or {}).get("seasonal", 0.0))
    if seasonal_weight > 0:
        season_boost = SEASON_CATEGORY_BOOST.get(req.season, [0.0, 0.0, 0.0])
        u = [max(-1.5, min(1.5, u[i] + seasonal_weight * season_boost[i])) for i in range(3)]
 
    # 「アニメ・ドラマ・歴史上の聖地へ行きたい(pilgrimage)」は本アプリのカテゴリ空間
    # (gourmet/sightseeing/healing)に対応する軸が無く、聖地データベースも持たないため、
    # uには混ぜ込まず、観光地(tourist_attraction)タイプの候補への小幅なスコア加点として
    # 個別に反映する（粗い近似であることに留意）。
    pilgrimage_weight = max(0.0, (req.detailed_vector or {}).get("pilgrimage", 0.0))
 
    # discount(割引重視)・avoid_crowd(混雑回避)・event(イベント目的)・safety(治安重視)は、
    # クーポン/混雑度/イベントカレンダー/治安指標のいずれも取得できるデータソースを
    # 持っていないため、現状は意図的に未使用としている（黙って無視するのではなく、
    # ここに明示しておく）。将来これらのAPIを組み込む際の差し込み点はここになる。
 
    scored_places = []
    for i, raw in enumerate(raw_places):
        c_i = analyze_reviews_and_build_vector(raw, req.custom_reviews_text or "")
        # 式(2): s_i = cos(u, c_i)
        s_i = cosine_similarity(u, c_i)
        if pilgrimage_weight > 0 and "tourist_attraction" in raw.get("types", []):
            s_i = min(1.0, s_i + 0.1 * pilgrimage_weight)
 
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
            # "osm"=OSMの実データ(opening_hoursタグ)に基づく／"default"=種別からの推定値。
            # 営業時間が推定値かどうかをUI側で示せるようにするためのフラグ。
            "hours_source": raw.get("hours_source", "default"),
            # OSM/TripAdvisor/Google Places側で既に分かっている実座標。旅程生成(build_itinerary)側で
            # 同名スポットの取り違えによる誤ジオコーディング（別の場所への移動時間になってしまう問題）
            # を避けるため、ここで得た実座標をそのまま最後まで引き継ぐ（seed_geocode_cache参照）。
            "lat": raw.get("lat"),
            "lon": raw.get("lon"),
            "reviews": [f"{raw['name']}の特徴"]
        })
 
    # 出発時間・出発地点・到着地点・費用・移動手段・行程タイプの制約が指定されている場合、
    # 好みの一致度（式2）だけでなく、その候補地を実際に訪問できるか（式3〜5相当）も
    # 提案段階で見積もり、制約内に収まる候補を優先してTOP3を選ぶ。以前はこれらの制約を
    # 一切考慮せずTOP3を提案していたため、選んでから旅程を作って初めて予算超過や
    # 営業時間外が判明する問題があった。start_location未指定時は従来通り（後方互換）。
    if req.start_location:
        await annotate_feasibility(
            scored_places,
            start_location=req.start_location,
            end_location=req.end_location or "",
            transport_mode=req.transport_mode or "transit",
            trip_type=req.trip_type or "round_trip",
            start_time=req.start_time or "09:00",
            budget_limit=req.budget_limit,
            time_limit=req.time_limit,
            member_count=req.member_count or 1,
        )
        feasible = [p for p in scored_places if p.get("fits_constraints")]
        # violation_scoreはあくまで「制約内に収まらない候補の中で、どれを残り枠に選ぶか」を
        # 決めるための基準（違反が小さい＝惜しい候補を優先的に選ぶ）。選ばれた後の画面表示順
        # （🥇🥈🥉）にそのまま使うと、violation_scoreの小さい順＝スコアの高い順とは限らないため、
        # 「スコアが低い方が🥇として表示される」という不具合になっていた。選定基準と表示順は
        # 別物として扱い、表示直前に必ずスコア降順へ揃える。
        infeasible_by_violation = sorted(
            [p for p in scored_places if not p.get("fits_constraints")],
            key=lambda p: p.get("violation_score", 0)
        )
        top3 = _select_diverse_top3(feasible) if feasible else []
        if len(top3) < 3:
            # 制約内に収まる候補が3件に満たない場合のみ、最も惜しい（違反が小さい）候補で
            # 残り枠を埋める。0件を返すより、違反理由を明示した上で見せる方が親切なため。
            used_names = {p["name"] for p in top3}
            needed = 3 - len(top3)
            backfill = []
            for p in infeasible_by_violation:
                if len(backfill) >= needed:
                    break
                if p["name"] not in used_names:
                    backfill.append(p)
                    used_names.add(p["name"])
            # 選定基準（違反の小ささ）と表示順（スコアの高さ）は別物なので、
            # 実際に画面へ足す直前にスコア降順へ並べ替える。
            backfill.sort(key=lambda p: p["score"], reverse=True)
            top3.extend(backfill)
    else:
        top3 = _select_diverse_top3(scored_places)
 
    return {
        "status": "success",
        "target_area": area,
        "preference_vector": {"gourmet": round(u[0], 3), "sightseeing": round(u[1], 3), "healing": round(u[2], 3)},
        "constraints_applied": bool(req.start_location),
        "top3_places": top3
    }
 
@app.post("/build_itinerary")
async def build_itinerary(req: GenerateItineraryRequest):
    # start_locationはPydantic上は必須(str)だが、空文字("")は型検証を通ってしまうため、
    # ここで明示的に弾く（デフォルト値を無言で使っていた過去の挙動を廃止）。
    if not req.start_location or not req.start_location.strip():
        raise HTTPException(status_code=400, detail="出発地点を入力してください。")
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
                "hours_source": "default",  # 自由入力の経由地は営業時間の実データを持たない
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
            "hours_source": node.get("hours_source", "default"),
            "score": node.get("score"),
            "is_custom_waypoint": False
        })
        # 診断段階（OSM/TripAdvisor/Google Places）で既に分かっている実座標があれば、
        # ジオコーディングキャッシュに投入して同名スポットの取り違えを防ぐ（店名だけでの
        # 再ジオコーディングは、全国に同名・類似名の店がある場合に全く別の場所に解決され、
        # 移動時間が異常な値になる不具合の原因になっていた）。
        if node.get("lat") is not None and node.get("lon") is not None:
            seed_geocode_cache(node["name"], node["lon"], node["lat"])
 
    # 満足度スコア上位の候補地からなる「メインピーク集合M」（式8）。
    # TOP3のうち選ばなかった残りがAI自動提案の経由地としてselected_nodesに含まれるように
    # なったため、その中で「本当にメインピークとして扱う（τ^peak・ピーク重みを適用する）」のは
    # main_peak_place_idで明示された1件のみとする（未指定時は先頭の1件＝後方互換）。
    if req.main_peak_place_id and any(n["id"] == req.main_peak_place_id for n in selected_nodes):
        main_peak_ids = {req.main_peak_place_id}
    elif selected_nodes:
        main_peak_ids = {selected_nodes[0]["id"]}
    else:
        main_peak_ids = set()
 
    # メインピーク以外（AI自動提案の残り経由地＋手動追加の経由地）は、制約に収まらない場合に
    # 自動的に間引ける「任意」ノードとして扱う。メインピーク自身は必須（required）。
    required_ids = set(main_peak_ids)
 
    # 世代グループ由来のペース調整に加え、「10項目こだわり」のpacked_schedule(過密日程)／
    # relax_schedule(ゆっくり観光)も滞在時間ペースに反映する（以前はこの2項目もdiagnose_top3の
    # ψ(b)構築にしか使われておらず、旅程生成側では無視されていた）。
    detailed = req.detailed_vector or {}
    packed = max(0.0, detailed.get("packed_schedule", 0.0))
    relaxed = max(0.0, detailed.get("relax_schedule", 0.0))
    schedule_scale = max(0.7, min(1.3, 1.0 - 0.15 * packed + 0.15 * relaxed))
    generation_scale = 1.15 if req.generation_group in ["family", "senior"] else 1.0
    pace_factor = generation_scale * schedule_scale
 
    start_h, start_m = map(int, req.start_time.split(":"))
    start_time_min = start_h * 60 + start_m
 
    # 到着地点（出発地と異なる場合のみ・任意）の解決。
    end_loc = (req.end_location or "").strip()
    has_distinct_end = bool(end_loc) and end_loc != req.start_location
 
    node_names = [req.start_location] + [t["name"] for t in all_visit_targets]
    if has_distinct_end:
        node_names = node_names + [end_loc]
        tail_idx = len(node_names) - 1
    elif req.trip_type == "round_trip":
        tail_idx = 0  # 出発地に戻る
    else:
        tail_idx = None  # 片道：最後に訪問したスポットが終着点
 
    # 経由地（ユーザーの自由入力テキストで、診断済みの実座標を持たないもの）は、
    # target_area（診断時に指定したエリア名）をヒントに事前にジオコーディングしておく。
    # 「ミラノ亭」のような店名単体だと全国の同名店と混同され得るため、「箱根温泉 ミラノ亭」の
    # ように地名を補って検索精度を上げる（該当地名で0件なら地名なしの通常検索に自動フォールバック）。
    area_hint = (req.target_area or "").strip()
    if area_hint:
        for wp_name in req.custom_waypoints:
            wp_clean = wp_name.strip()
            if wp_clean:
                await geocode_location(wp_clean, area_hint=area_hint)
 
    # 式(9): 全ペアの移動時間・費用を一度だけ計算し、GAの適応度評価をメモリ上で完結させる
    matrix = await build_travel_matrix(node_names, req.transport_mode)
 
    if all_visit_targets:
        # 式(9)の並び順探索に加え、予算・時間・営業時間（式3〜5）に実際に収まるよう、
        # 必要であれば価値の低い経由地から自動的に間引く（solve_itinerary_with_dropping）。
        # メインピーク（required_ids）だけは常に残される。
        sim = solve_itinerary_with_dropping(
            matrix=matrix,
            targets=all_visit_targets,
            required_ids=required_ids,
            tail_idx=tail_idx,
            start_time_min=start_time_min,
            pace_factor=pace_factor,
            member_count=req.member_count,
            peak_position=req.peak_position,
            main_peak_ids=main_peak_ids,
            budget_limit=req.budget_limit,
            time_limit=req.time_limit,
        )
    else:
        sim = {"order": [], "itinerary_nodes": [], "total_cost": 0, "total_move_cost": 0,
               "total_time": 0, "total_move_time": 0, "hours_checks": [], "f1_total": 0.0,
               "dropped_names": []}
 
    # GA(_simulate_order)はインデックスベースで結果を返すため、フロントエンドが期待する
    # 地名ベースのitinerary構造に変換する。
    def idx_to_name(idx: int) -> str:
        if idx == 0:
            return req.start_location
        if has_distinct_end and idx == len(node_names) - 1:
            return end_loc
        return all_visit_targets[idx - 1]["name"]
 
    itinerary_nodes = []
    for node in sim["itinerary_nodes"]:
        if node["type"] == "transit":
            itinerary_nodes.append({
                "type": "transit",
                "from_name": idx_to_name(node["from_idx"]),
                "to_name": idx_to_name(node["to_idx"]),
                "duration_minutes": node["duration_minutes"],
                "cost": node["cost"]
            })
        else:
            target = all_visit_targets[node["target_idx"] - 1]
            itinerary_nodes.append({
                "type": "spot",
                "place": {"name": target["name"]},
                "stay_minutes": node["stay_minutes"],
                "is_peak": node["is_peak"],
                "is_custom_waypoint": node["is_custom_waypoint"],
                "weight": node["weight"],
                "arrival_minutes": node["arrival_minutes"],
                "hours_satisfied": node["hours_satisfied"],
                # 営業時間が実データ(OSM)由来か、種別からの推定値かをUI側で区別できるようにする。
                "hours_source": target.get("hours_source", "default")
            })
 
    total_cost = sim["total_cost"]
    total_move_cost = sim["total_move_cost"]
    total_time = sim["total_time"]
    total_move_time = sim["total_move_time"]
    hours_checks = sim["hours_checks"]
    f1_total = sim["f1_total"]
 
    # 終着点の決定（Googleマップ連携・表示用）。到着地点指定＞往復＞片道(最終訪問地)の順。
    if has_distinct_end:
        final_destination = end_loc
    elif req.trip_type == "round_trip":
        final_destination = req.start_location
    elif sim["order"]:
        final_destination = all_visit_targets[sim["order"][-1] - 1]["name"]
    else:
        final_destination = req.start_location
 
    # 式(3)〜(5): 多目的制約充足判定
    constraints = check_constraints(total_cost, req.budget_limit, total_time, req.time_limit, hours_checks)
 
    # 式(10): Δg = Σ(区間ごとの個別手配運賃想定) − f3(R)（移動費用のみに個別手配割増を適用）
    baseline_move_cost = int(total_move_cost * INDIVIDUAL_BOOKING_MARKUP)
    cost_saved_yen = max(0, baseline_move_cost - total_move_cost)
 
    # 式(11): Δt = 最悪の巡回順による総移動時間 − f2(R)（到着地点指定時はそこまで含めて比較する）
    # 制約に収めるため間引かれた経由地は実際には訪問しないため、最悪ケース側も
    # 「実際に訪問した地点集合」だけで比較する（間引いた地点まで含めると、訪問していない
    # 分だけΔtが不当に大きく出てしまうため）。
    stop_names = [all_visit_targets[i - 1]["name"] for i in sim.get("order", [])]
    worst_case_time = await compute_worst_case_travel_time(
        req.start_location, stop_names, req.transport_mode, req.trip_type, pace_factor,
        end_location=end_loc if has_distinct_end else ""
    )
    time_saved_minutes = max(0, worst_case_time - total_move_time)
 
    return {
        "status": "success",
        "itinerary": itinerary_nodes,
        "start_time": req.start_time,
        "end_time": req.end_time,
        "trip_type": req.trip_type,
        "member_count": req.member_count,
        "final_destination": final_destination,
        # 予算・時間・営業時間の制約に収めるため、自動的に旅程から除外された経由地の名前一覧。
        # 空リストなら、指定した経由地はすべてそのまま含められたことを意味する。
        "dropped_names": sim.get("dropped_names", []),
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
    # Renderなどのホスティング環境はリッスンすべきポート番号を環境変数PORTで渡してくる。
    # ローカル実行時はPORT未設定なので、従来通り8000番を使う。
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)