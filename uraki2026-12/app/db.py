# db.py
"""
Googleログイン導入にあわせて、ユーザー・お気に入り・口コミを永続化するための
最小限のデータストア。
 
これまで口コミは reviews.jsonl への追記、お気に入りはブラウザの localStorage
のみで管理しており、(1) どちらもユーザーアカウントに紐付いていない、
(2) 端末をまたいで共有されない、という制約があった。ログイン機能を足す以上
「誰の」お気に入り・口コミかを区別する必要が出てくるため、この機会に
Python標準ライブラリのsqlite3ベースの簡易DBへ一本化する。
 
【重要な注意】Renderの無料プランはディスクが揮発性（再起動・再デプロイで
リセットされる）。つまりこのSQLiteファイルもアプリのソースコードと同様に
デプロイのたびに消える。ユーザーが「アカウントを作ったのに次にアクセスしたら
消えていた」となるのを避けるには、Render上でPersistent Disk（有料）を
アタッチするか、外部のマネージドDB（Render無料PostgresなどSQLite以外）へ
移行する必要がある。今の実装はまず「アカウントに紐付ける仕組み」を動かす
ところまでで、本番運用にはディスクの永続化が別途必須であることを明記しておく。
 
【Googleログイン／ユーザー名・パスワード認証の併用について】
当初はGoogleログインのみだったが、Google Cloud ConsoleでのOAuth設定が
デプロイのたびに手間になるため、外部サービス不要のユーザー名・パスワード認証
（ユーザー参考コードのPHP実装に相当する、自前のシンプルな認証）を追加で
選べるようにした。usersテーブルは両方式に対応できるよう、Googleの google_sub
だけでなくユーザー名・パスワードハッシュも保持し、共通の主キーとして
「google:<sub>」または「local:<username>」という形式の id を使う。これにより
favorites/reviewsのuser_idはどちらの認証方式でも同じ扱いで紐付けられる。
 
【usersテーブルのスキーマ変更に関する注意】
このバージョンから users テーブルの主キーが google_sub から id (TEXT) に
変わった。既存の app_data.db（旧スキーマ）がある状態でこのファイルに
差し替えると、CREATE TABLE IF NOT EXISTS は既存テーブルをそのまま残すため
カラム不整合でエラーになる可能性がある。まだ本番データが無い開発段階なので、
差し替え時は古い app_data.db（および -wal/-shm）を削除してから起動すること。
"""
import os
import sqlite3
import json
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
 
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_data_v2.db")
 
# ユーザー名・パスワード認証用のパスワードハッシュ化パラメータ。
# 外部ライブラリ（bcrypt等）を追加せず、Python標準のhashlibのみで実装するため、
# PBKDF2-HMAC-SHA256を使う（OWASP推奨の反復回数の目安に合わせ26万回とした）。
_PBKDF2_ITERATIONS = 260_000
 
 
def _hash_password(password: str) -> str:
    """ソルト付きPBKDF2でパスワードをハッシュ化し、"salt$hash"形式の文字列で返す"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"{salt}${dk.hex()}"
 
 
def _verify_password(password: str, stored: str) -> bool:
    """_hash_password()が生成した文字列に対して、入力パスワードが一致するか検証する"""
    try:
        salt, hash_hex = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    # タイミング攻撃を避けるため、単純な == ではなく定数時間比較を使う
    return hmac.compare_digest(dk.hex(), hash_hex)
 
 
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WALモードにしておくと、読み取りと書き込みが同時に来てもロック待ちで
    # 詰まりにくくなる（無料枠の小規模運用でも安全側に倒す）。
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn
 
 
def init_db() -> None:
    """起動時に一度呼び出し、テーブルが無ければ作成する。"""
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                google_sub TEXT UNIQUE,
                username TEXT UNIQUE,
                password_hash TEXT,
                email TEXT,
                name TEXT,
                picture TEXT,
                created_at TEXT NOT NULL
            );
 
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                title TEXT,
                created_at TEXT NOT NULL,
                data_json TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
 
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                rating INTEGER NOT NULL,
                review_text TEXT,
                final_destination TEXT,
                start_location TEXT,
                transport_mode TEXT,
                trip_type TEXT,
                member_count INTEGER,
                total_cost REAL,
                total_time_minutes REAL,
                submitted_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
        """)
        conn.commit()
    finally:
        conn.close()
 
 
def upsert_google_user(google_sub: str, email: str, name: str, picture: str) -> Dict[str, Any]:
    """Googleログイン成功時に呼ぶ。usersテーブルへupsertし、セッションに保存する
    共通形式のユーザー情報辞書（id/provider/email/name/picture）を返す。"""
    user_id = f"google:{google_sub}"
    conn = get_connection()
    try:
        existing = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET email = ?, name = ?, picture = ? WHERE id = ?",
                (email, name, picture, user_id),
            )
        else:
            conn.execute(
                """INSERT INTO users (id, provider, google_sub, email, name, picture, created_at)
                   VALUES (?, 'google', ?, ?, ?, ?, ?)""",
                (user_id, google_sub, email, name, picture, datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
    finally:
        conn.close()
    return {"id": user_id, "provider": "google", "email": email, "name": name, "picture": picture}
 
 
def create_local_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """ユーザー名・パスワードで新規アカウントを作る。ユーザー名が既に使われていれば
    Noneを返す（呼び出し側で409エラーにする）。"""
    user_id = f"local:{username}"
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE id = ? OR username = ?", (user_id, username)
        ).fetchone()
        if existing:
            return None
        conn.execute(
            """INSERT INTO users (id, provider, username, password_hash, name, created_at)
               VALUES (?, 'local', ?, ?, ?, ?)""",
            (user_id, username, _hash_password(password), username, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": user_id, "provider": "local", "email": "", "name": username, "picture": ""}
 
 
def authenticate_local_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """ユーザー名・パスワードを検証する。一致すればユーザー情報辞書を、
    ユーザーが存在しない／パスワード不一致ならNoneを返す。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, name FROM users WHERE username = ? AND provider = 'local'",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["password_hash"]:
        return None
    if not _verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "provider": "local", "email": "", "name": row["name"] or row["username"], "picture": ""}
 
 
def add_favorite(user_id: str, title: str, data: Dict[str, Any]) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO favorites (user_id, title, created_at, data_json) VALUES (?, ?, ?, ?)",
            (user_id, title, datetime.now(timezone.utc).isoformat(), json.dumps(data, ensure_ascii=False)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()
 
 
def list_favorites(user_id: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, created_at, data_json FROM favorites WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": r["created_at"],
                "data": json.loads(r["data_json"]),
            }
            for r in rows
        ]
    finally:
        conn.close()
 
 
def delete_favorite(user_id: str, favorite_id: int) -> bool:
    """本人のお気に入りだけを削除できるようにする（他人のIDを指定しても削除できない）。"""
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM favorites WHERE id = ? AND user_id = ?", (favorite_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
 
 
def add_review(record: Dict[str, Any]) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO reviews
               (user_id, rating, review_text, final_destination, start_location,
                transport_mode, trip_type, member_count, total_cost, total_time_minutes, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.get("user_id"),
                record["rating"],
                record.get("review_text", ""),
                record.get("final_destination", ""),
                record.get("start_location", ""),
                record.get("transport_mode", ""),
                record.get("trip_type", ""),
                record.get("member_count"),
                record.get("total_cost"),
                record.get("total_time_minutes"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()
 
 
def reviews_summary() -> Dict[str, Optional[float]]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS c, AVG(rating) AS avg_rating FROM reviews").fetchone()
        count = row["c"] or 0
        avg = round(row["avg_rating"], 2) if row["avg_rating"] is not None else None
        return {"count": count, "average_rating": avg}
    finally:
        conn.close()
 