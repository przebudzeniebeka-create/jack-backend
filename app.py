# app.py
from __future__ import annotations

import os, time, requests, traceback
from typing import Optional, Dict
from datetime import datetime

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text as sa_text
from dateutil import parser as dtparser
from dotenv import load_dotenv

load_dotenv()

# ───────────────────────── Flask ─────────────────────────
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
try:
    app.json.ensure_ascii = False
except Exception:
    pass

BUILD = {
    "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("COMMIT_SHA") or "dev",
    "boot_ts": int(time.time()),
}

# ──────────────────────── Database ───────────────────────
db_url = os.getenv("DATABASE_URL", "sqlite:///app.db")  # Railway: postgresql+psycopg2://...?...sslmode=require
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

def _db_kind() -> str:
    s = db_url.split("://", 1)[0].lower()
    if "postgres" in s: return "postgres"
    if "sqlite"   in s: return "sqlite"
    return s

def _db_scheme_info(uri: str) -> Dict[str, str]:
    scheme = (uri.split("://", 1)[0] if "://" in uri else uri).lower()
    kind = "postgres" if "postgres" in scheme else ("sqlite" if "sqlite" in scheme else scheme)
    return {"kind": kind, "scheme": scheme}

# ORM korzysta z małoliterowej tabeli `message`
class Message(db.Model):
    __tablename__ = "message"
    id         = db.Column(db.Integer, primary_key=True)
    role       = db.Column(db.String(10))      # 'user' | 'jack'
    content    = db.Column(db.Text)
    timestamp  = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id    = db.Column(db.String(64), index=True)
    session_id = db.Column(db.String(64), index=True)

def ensure_schema() -> None:
    """
    Tworzy public.message, dodaje kolumny user_id/session_id i indeksy.
    Jeśli istnieje stara public."Message" a nie ma public.message → tworzy message i kopiuje dane.
    """
    try:
        if _db_kind() == "postgres":
            exists_message = db.session.execute(sa_text("SELECT to_regclass('public.message');")).scalar()
            exists_Message = db.session.execute(sa_text("SELECT to_regclass('public.\"Message\"');")).scalar()

            if not exists_message:
                db.session.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS public.message (
                        id BIGSERIAL PRIMARY KEY,
                        role VARCHAR(10),
                        content TEXT,
                        timestamp TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
                    );
                """))
                if exists_Message:
                    db.session.execute(sa_text("""
                        INSERT INTO public.message (role, content, timestamp)
                        SELECT role, content, timestamp FROM public."Message";
                    """))
                db.session.commit()

            db.session.execute(sa_text('ALTER TABLE public.message ADD COLUMN IF NOT EXISTS user_id VARCHAR(64);'))
            db.session.execute(sa_text('ALTER TABLE public.message ADD COLUMN IF NOT EXISTS session_id VARCHAR(64);'))

            db.session.execute(sa_text('CREATE INDEX IF NOT EXISTS idx_message_ts ON public.message(timestamp);'))
            db.session.execute(sa_text('CREATE INDEX IF NOT EXISTS idx_message_user ON public.message(user_id);'))
            db.session.execute(sa_text('CREATE INDEX IF NOT EXISTS idx_message_session ON public.message(session_id);'))
            db.session.commit()

        else:  # SQLite
            db.session.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS message (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT,
                    content TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """))
            db.session.execute(sa_text('ALTER TABLE message ADD COLUMN IF NOT EXISTS user_id TEXT;'))
            db.session.execute(sa_text('ALTER TABLE message ADD COLUMN IF NOT EXISTS session_id TEXT;'))
            db.session.execute(sa_text('CREATE INDEX IF NOT EXISTS idx_message_ts ON message(timestamp);'))
            db.session.execute(sa_text('CREATE INDEX IF NOT EXISTS idx_message_user ON message(user_id);'))
            db.session.execute(sa_text('CREATE INDEX IF NOT EXISTS idx_message_session ON message(session_id);'))
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[schema][ERROR] {e}\n{traceback.format_exc()}")

with app.app_context():
    ensure_schema()

def serialize_message(m: "Message") -> Dict[str, object]:
    return {
        "id": m.id, "role": m.role, "content": m.content,
        "timestamp": (m.timestamp.isoformat() if m.timestamp else None),
        "user_id": m.user_id, "session_id": m.session_id,
    }

# ───────────────────────── CORS ─────────────────────────
CORS(app, resources={r"/api/*": {"origins": "*"}})  # zawęzimy później

# ─────────────────────── Turnstile ───────────────────────
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET_KEY", "")
BYPASS_TURNSTILE_DEV = os.getenv("BYPASS_TURNSTILE_DEV", "0")

def verify_turnstile(token: str, remote_ip: Optional[str] = None) -> tuple[bool, dict]:
    if BYPASS_TURNSTILE_DEV == "1":
        return True, {"dev": "bypass"}
    if not TURNSTILE_SECRET:
        return True, {"dev": "no_secret"}
    if not token:
        return False, {"error": "missing_token"}
    try:
        r = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": remote_ip or ""},
            timeout=4,
        )
        data = r.json()
        return bool(data.get("success")), data
    except Exception as e:
        return False, {"error": str(e)}

# ───────────────────────── OpenAI ────────────────────────
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("openai_api_key")
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    openai_client = None

# ─────────────────────── Helpers ─────────────────────────
def _extract_message(data: dict) -> Optional[str]:
    for k in ("message", "text", "prompt", "query", "q"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def _clip(s: Optional[str], n: int) -> Optional[str]:
    if not s: return s
    s = str(s).strip()
    return s[:n]

def _db_status() -> str:
    try:
        db.session.execute(sa_text("SELECT 1"))
        return "ok"
    except Exception:
        return "error"

# ─────────────────────── Routes ──────────────────────────
@app.route("/")
def root():
    return "Jack backend – OK. Użyj /api/health"

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True, "time": int(time.time()),
        "db": _db_status(), "db_url": _db_scheme_info(db_url),
        "build": BUILD
    })

@app.route("/api/db-debug")
def db_debug():
    out = {"kind": _db_scheme_info(db_url)["kind"], "tables": [], "message_columns": []}
    try:
        if _db_kind() == "postgres":
            rows = db.session.execute(sa_text("""
                SELECT schemaname, tablename FROM pg_tables
                WHERE schemaname='public' ORDER BY tablename;
            """)).fetchall()
            out["tables"] = [{"schema":r[0], "table":r[1]} for r in rows]
            cols = db.session.execute(sa_text("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema='public' AND table_name='message'
                ORDER BY ordinal_position;
            """)).fetchall()
            out["message_columns"] = [{"name": c[0], "type": c[1]} for c in cols]
            cnt = db.session.execute(sa_text("SELECT COUNT(*) FROM public.message;")).scalar()
            out["message_count"] = int(cnt)
        else:
            rows = db.session.execute(sa_text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")).fetchall()
            out["tables"] = [{"table": r[0]} for r in rows]
            cols = db.session.execute(sa_text("PRAGMA table_info(message);")).fetchall()
            out["message_columns"] = [{"name": c[1], "type": c[2]} for c in cols]
            cnt = db.session.execute(sa_text("SELECT COUNT(*) FROM message;")).scalar()
            out["message_count"] = int(cnt)
        return jsonify({"ok": True, "db": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "db": out}), 500

@app.route("/api/routes")
def routes_list():
    rules = sorted([str(r.rule) for r in app.url_map.iter_rules()])
    return jsonify(rules)

# ─────────── /api/history — ORM + RAW SQL fallback ───────
@app.route("/api/history", methods=["GET"])
def history_db():
    args = request.args

    user_id    = _clip(args.get("user_id", ""), 64)
    session_id = _clip(args.get("session_id", ""), 64)
    role       = (args.get("role") or "").strip().lower()
    search     = (args.get("search") or "").strip()

    def _to_int(v: str, default: int, lo: int, hi: int) -> int:
        try: x = int(v)
        except Exception: x = default
        return max(lo, min(hi, x))

    limit  = _to_int(args.get("limit", ""), 50, 1, 200)
    offset = _to_int(args.get("offset", ""), 0, 0, 10_000_000)
    order  = (args.get("order") or "desc").lower()

    def _parse_ts(s: str):
        if not s: return None
        try: return dtparser.parse(s)
        except Exception: return None

    since_ts = _parse_ts(args.get("since_ts", ""))
    until_ts = _parse_ts(args.get("until_ts", ""))

    # 1) ORM
    try:
        q = Message.query
        if user_id:    q = q.filter(Message.user_id == user_id)
        if session_id: q = q.filter(Message.session_id == session_id)
        if role in ("user", "jack"): q = q.filter(Message.role == role)
        if since_ts is not None: q = q.filter(Message.timestamp >= since_ts)
        if until_ts is not None: q = q.filter(Message.timestamp <= until_ts)
        if search: q = q.filter(Message.content.ilike(f"%{search}%"))

        if order == "asc":
            q = q.order_by(Message.timestamp.asc(), Message.id.asc())
        else:
            q = q.order_by(Message.timestamp.desc(), Message.id.desc())

        rows = q.offset(offset).limit(limit + 1).all()
        items = [serialize_message(m) for m in rows]
        has_more = len(items) > limit
        if has_more: items = items[:limit]
        return jsonify({"ok": True, "items": items, "limit": limit, "offset": offset, "has_more": has_more})
    except Exception as e:
        print(f"[history][ORM-ERROR] {e}\n{traceback.format_exc()}")

    # 2) RAW SQL fallback (parametryzowany)
    try:
        table = "public.message" if _db_kind() == "postgres" else "message"
        where = []
        params: Dict[str, object] = {}
        if user_id:
            where.append("user_id = :user_id"); params["user_id"] = user_id
        if session_id:
            where.append("session_id = :session_id"); params["session_id"] = session_id
        if role in ("user", "jack"):
            where.append("role = :role"); params["role"] = role
        if since_ts is not None:
            where.append("timestamp >= :since_ts"); params["since_ts"] = since_ts
        if until_ts is not None:
            where.append("timestamp <= :until_ts"); params["until_ts"] = until_ts
        if search:
            like = "ILIKE" if _db_kind() == "postgres" else "LIKE"
            where.append(f"content {like} :search"); params["search"] = f"%{search}%"

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        order_sql = "ORDER BY timestamp DESC, id DESC" if order != "asc" else "ORDER BY timestamp ASC, id ASC"

        sql = f"""
            SELECT id, role, content, timestamp, user_id, session_id
            FROM {table}
            {where_sql}
            {order_sql}
            LIMIT :limit_plus OFFSET :offset
        """
        params["limit_plus"] = int(limit + 1)
        params["offset"] = int(offset)

        res = db.session.execute(sa_text(sql), params).fetchall()
        items = []
        for row in res:
            m = row._mapping if hasattr(row, "_mapping") else row
            items.append({
                "id": m["id"], "role": m["role"], "content": m["content"],
                "timestamp": (m["timestamp"].isoformat() if m["timestamp"] else None),
                "user_id": m.get("user_id"), "session_id": m.get("session_id"),
            })
        has_more = len(items) > limit
        if has_more: items = items[:limit]
        return jsonify({"ok": True, "items": items, "limit": limit, "offset": offset, "has_more": has_more})
    except Exception as e:
        print(f"[history][RAW-ERROR] {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "error": "history_failed"}), 500

# ─────── /api/history/recent — ostatnie N bez filtrów ────
@app.route("/api/history/recent", methods=["GET"])
def history_recent():
    try:
        limit = int(request.args.get("limit", "10"))
    except Exception:
        limit = 10
    limit = max(1, min(50, limit))
    try:
        q = Message.query.order_by(Message.timestamp.desc(), Message.id.desc()).limit(limit)
        items = [serialize_message(m) for m in q.all()]
        return jsonify({"ok": True, "items": items, "limit": limit})
    except Exception as e:
        print(f"[recent][ERROR] {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "error": "recent_failed"}), 500

# ─────────────────────── /api/chat ────────────────────────
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}

    ok_ts, details = verify_turnstile(data.get("cf_turnstile_token"), request.remote_addr)
    if not ok_ts:
        return jsonify({"ok": False, "error": "turnstile_failed", "details": details}), 403

    user_message = _extract_message(data)
    if not user_message:
        return jsonify({"ok": False, "error": "message is required"}), 400

    user_id    = _clip(data.get("user_id"), 64)
    session_id = _clip(data.get("session_id"), 64)

    reply = f"Echo: {user_message}"

    if openai_client and OPENAI_API_KEY:
        try:
            resp = openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are Jack."},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.7,
            )
            reply = resp.choices[0].message.content
        except Exception as e:
            reply = f"(fallback) {reply} – openai_error: {e}"

    ensure_schema()

    saved = False
    db_error = None

    # 1) ORM insert
    try:
        db.session.add(Message(role="user", content=user_message, user_id=user_id, session_id=session_id))
        db.session.add(Message(role="jack", content=reply,       user_id=user_id, session_id=session_id))
        db.session.commit()
        saved = True
    except Exception as e1:
        db.session.rollback()
        db_error = f"orm_insert_failed: {e1}"
        print(f"[chat][DB-ERROR][ORM] {e1}\n{traceback.format_exc()}")

        # 2) RAW SQL fallback insert
        try:
            if _db_kind() == "postgres":
                db.session.execute(
                    sa_text("""
                        INSERT INTO public.message (role, content, user_id, session_id)
                        VALUES (:r1, :c1, :u, :s), (:r2, :c2, :u, :s)
                    """),
                    {"r1": "user", "c1": user_message, "r2": "jack", "c2": reply, "u": user_id, "s": session_id},
                )
            else:
                db.session.execute(
                    sa_text("""
                        INSERT INTO message (role, content, user_id, session_id)
                        VALUES (:r1, :c1, :u, :s), (:r2, :c2, :u, :s)
                    """),
                    {"r1": "user", "c1": user_message, "r2": "jack", "c2": reply, "u": user_id, "s": session_id},
                )
            db.session.commit()
            saved = True
        except Exception as e2:
            db.session.rollback()
            db_error = f"{db_error}; raw_insert_failed: {e2}"
            print(f"[chat][DB-ERROR][RAW] {e2}\n{traceback.format_exc()}")

    return jsonify({"ok": True, "reply": reply, "saved": saved, "db_error": db_error, "build": BUILD})

# ────────────────────── Local run (dev) ───────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

















