# app.py
from __future__ import annotations

import os, re, time, requests, traceback, uuid
from typing import Optional, Dict, Tuple
from datetime import datetime

from flask import Flask, request, jsonify, g
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

# access-log & request id
@app.before_request
def _assign_req_id_and_timer():
    g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    g.t0 = time.perf_counter()

@app.after_request
def _add_std_headers(resp):
    # request id + expose dla frontu
    rid = g.get("request_id", "")
    if rid:
        resp.headers["X-Request-ID"] = rid
    existing = resp.headers.get("Access-Control-Expose-Headers", "")
    expose = [h.strip() for h in existing.split(",") if h.strip()]
    for h in ("X-Request-ID", "Retry-After"):
        if h not in expose:
            expose.append(h)
    resp.headers["Access-Control-Expose-Headers"] = ", ".join(expose)

    # prosty access log
    try:
        dt_ms = int((time.perf_counter() - (g.t0 or time.perf_counter())) * 1000)
    except Exception:
        dt_ms = -1
    ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr
    uid = getattr(g, "_user_id_for_log", "")
    sid = getattr(g, "_session_id_for_log", "")
    print(f'[access] {request.method} {request.path} -> {resp.status_code} {dt_ms}ms ip={ip} user={uid} sess={sid} req={rid}')
    return resp

# helper: zwracaj JSON z nagłówkiem X-Request-ID (i ewentualnie Retry-After)
def _json(payload: Dict, status: int = 200, retry_after: Optional[int] = None):
    payload = dict(payload or {})
    payload.setdefault("request_id", g.get("request_id", ""))
    resp = jsonify(payload)
    resp.status_code = status
    rid = g.get("request_id", "")
    if rid:
        resp.headers["X-Request-ID"] = rid
    if retry_after is not None:
        resp.headers["Retry-After"] = str(int(retry_after))
    return resp

# ──────────────────────── Database ───────────────────────
db_url = os.getenv("DATABASE_URL", "sqlite:///app.db")
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

class Message(db.Model):
    __tablename__ = "message"
    id         = db.Column(db.Integer, primary_key=True)
    role       = db.Column(db.String(10))
    content    = db.Column(db.Text)
    timestamp  = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id    = db.Column(db.String(64), index=True)
    session_id = db.Column(db.String(64), index=True)

# ─────────────────────── Rate limit ──────────────────────
RATE_TABLE = "public.rate_limit_usage" if _db_kind() == "postgres" else "rate_limit_usage"

def parse_rate(s: Optional[str], default_limit: int, default_window: int) -> Tuple[int, int]:
    try:
        if not s: return default_limit, default_window
        a, b = s.strip().split("/")
        return int(a), int(b)
    except Exception:
        return default_limit, default_window

RATE_LIMIT_IP    = parse_rate(os.getenv("RATE_LIMIT_IP"),    30, 60)
RATE_LIMIT_USER  = parse_rate(os.getenv("RATE_LIMIT_USER"),  60, 60)

def _bucket_start(window_sec: int) -> datetime:
    ts = int(time.time())
    return datetime.utcfromtimestamp(ts - (ts % window_sec))

def _client_ip() -> str:
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.headers.get("X-Real-IP")
            or (request.remote_addr or "0.0.0.0"))

def ensure_schema() -> None:
    try:
        # message table + kolumny/indeksy
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
        else:
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
        # rate_limit_usage
        if _db_kind() == "postgres":
            db.session.execute(sa_text(f"""
                CREATE TABLE IF NOT EXISTS {RATE_TABLE} (
                    key VARCHAR(128) PRIMARY KEY,
                    window_start TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                    count INTEGER NOT NULL
                );
            """))
            db.session.execute(sa_text(f'CREATE INDEX IF NOT EXISTS idx_rl_window ON {RATE_TABLE}(window_start);'))
        else:
            db.session.execute(sa_text(f"""
                CREATE TABLE IF NOT EXISTS {RATE_TABLE} (
                    key TEXT PRIMARY KEY,
                    window_start DATETIME NOT NULL,
                    count INTEGER NOT NULL
                );
            """))
            db.session.execute(sa_text(f'CREATE INDEX IF NOT EXISTS idx_rl_window ON {RATE_TABLE}(window_start);'))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[schema][ERROR] {e}\n{traceback.format_exc()}")

with app.app_context():
    ensure_schema()

def _rl_upsert_and_get_count(key: str, window_start: datetime) -> Optional[int]:
    try:
        if _db_kind() == "postgres":
            row = db.session.execute(
                sa_text(f"""
                    INSERT INTO {RATE_TABLE} (key, window_start, count)
                    VALUES (:key, :ws, 1)
                    ON CONFLICT (key)
                    DO UPDATE SET count = {RATE_TABLE}.count + 1
                    RETURNING count;
                """),
                {"key": key, "ws": window_start},
            ).fetchone()
            db.session.commit()
            return int(row[0]) if row else 1
        else:
            upd = db.session.execute(sa_text(f"UPDATE {RATE_TABLE} SET count = count + 1 WHERE key = :key"), {"key": key})
            if upd.rowcount == 0:
                try:
                    db.session.execute(sa_text(f"INSERT INTO {RATE_TABLE} (key, window_start, count) VALUES (:key, :ws, 1)"),
                                       {"key": key, "ws": window_start})
                except Exception:
                    db.session.execute(sa_text(f"UPDATE {RATE_TABLE} SET count = count + 1 WHERE key = :key"), {"key": key})
            db.session.commit()
            row = db.session.execute(sa_text(f"SELECT count FROM {RATE_TABLE} WHERE key = :key"), {"key": key}).fetchone()
            return int(row[0]) if row else 1
    except Exception as e:
        db.session.rollback()
        print(f"[ratelimit][ERROR] {e}\n{traceback.format_exc()}")
        return None

def _ratelimit(scope: str, ident: Optional[str], limit: int, window_sec: int):
    if not ident:
        return True, None
    try:
        ws = _bucket_start(window_sec)
        key = f"{scope}:{ident}:{int(ws.timestamp())}"
        count = _rl_upsert_and_get_count(key, ws)
        if count is None:
            return True, None
        if count > limit:
            retry_in = window_sec - (int(time.time()) % window_sec) + 1
            return False, {"ok": False, "error": "rate_limited", "scope": scope, "limit": limit,
                           "window_sec": window_sec, "retry_in": retry_in}
        return True, None
    except Exception as e:
        print(f"[ratelimit][FALLBACK-ALLOW] {e}")
        return True, None

def _rate_limited_response(pay: Dict):
    ra = pay.get("retry_in")
    return _json(pay, status=429, retry_after=ra)

def serialize_message(m: "Message") -> Dict[str, object]:
    return {
        "id": m.id, "role": m.role, "content": m.content,
        "timestamp": (m.timestamp.isoformat() if m.timestamp else None),
        "user_id": m.user_id, "session_id": m.session_id,
    }

# ───────────────────────── CORS ─────────────────────────
CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    re.compile(r"^https:\/\/([a-z0-9-]+\.)?jackqs\.ai$"),
    re.compile(r"^https:\/\/([a-z0-9-]+\.)?jackqs-frontend\.pages\.dev$"),
]
CORS(app, resources={
    r"/api/*": {
        "origins": CORS_ALLOWED_ORIGINS,
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
        "supports_credentials": False,
        "max_age": 600,
        "expose_headers": ["X-Request-ID", "Retry-After"],
    }
})

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
    return _json({"ok": True, "time": int(time.time()), "db": _db_status(),
                  "db_url": _db_scheme_info(db_url), "build": BUILD})

@app.route("/api/db-debug")
def db_debug():
    out = {"kind": _db_scheme_info(db_url)["kind"], "tables": [], "message_columns": []}
    try:
        if _db_kind() == "postgres":
            rows = db.session.execute(sa_text("SELECT schemaname, tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;")).fetchall()
            out["tables"] = [{"schema":r[0], "table":r[1]} for r in rows]
            cols = db.session.execute(sa_text("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='message' ORDER BY ordinal_position;")).fetchall()
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
        return _json({"ok": True, "db": out})
    except Exception as e:
        return _json({"ok": False, "error": str(e), "db": out}, status=500)

@app.route("/api/routes")
def routes_list():
    rules = sorted([str(r.rule) for r in app.url_map.iter_rules()])
    return _json(rules)

# /api/history (ORM + RAW fallback)
@app.route("/api/history", methods=["GET"])
def history_db():
    ip = _client_ip()
    ok_ip, pay = _ratelimit("ip", ip, *RATE_LIMIT_IP)
    if not ok_ip: return _rate_limited_response(pay)

    user_id_for_rl = (request.args.get("user_id") or "").strip()
    if user_id_for_rl:
        ok_user, pay2 = _ratelimit("user", user_id_for_rl, *RATE_LIMIT_USER)
        if not ok_user: return _rate_limited_response(pay2)

    args = request.args
    user_id    = _clip(args.get("user_id", ""), 64)
    session_id = _clip(args.get("session_id", ""), 64)
    role       = (args.get("role") or "").strip().lower()
    search     = (args.get("search") or "").strip()
    g._user_id_for_log = user_id or ""
    g._session_id_for_log = session_id or ""

    def _to_int(v: str, d: int, lo: int, hi: int) -> int:
        try: x = int(v)
        except Exception: x = d
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

    # ORM path
    try:
        q = Message.query
        if user_id:    q = q.filter(Message.user_id == user_id)
        if session_id: q = q.filter(Message.session_id == session_id)
        if role in ("user", "jack"): q = q.filter(Message.role == role)
        if since_ts is not None: q = q.filter(Message.timestamp >= since_ts)
        if until_ts is not None: q = q.filter(Message.timestamp <= until_ts)
        if search: q = q.filter(Message.content.ilike(f"%{search}%"))
        if order == "asc": q = q.order_by(Message.timestamp.asc(), Message.id.asc())
        else:              q = q.order_by(Message.timestamp.desc(), Message.id.desc())
        rows = q.offset(offset).limit(limit + 1).all()
        items = [serialize_message(m) for m in rows]
        has_more = len(items) > limit
        if has_more: items = items[:limit]
        return _json({"ok": True, "items": items, "limit": limit, "offset": offset, "has_more": has_more})
    except Exception as e:
        print(f"[history][ORM-ERROR] {e}\n{traceback.format_exc()}")

    # RAW SQL fallback
    try:
        table = "public.message" if _db_kind() == "postgres" else "message"
        where = []
        params: Dict[str, object] = {}
        if user_id:    where.append("user_id = :user_id");    params["user_id"] = user_id
        if session_id: where.append("session_id = :session_id"); params["session_id"] = session_id
        if role in ("user", "jack"): where.append("role = :role"); params["role"] = role
        if since_ts is not None: where.append("timestamp >= :since_ts"); params["since_ts"] = since_ts
        if until_ts is not None: where.append("timestamp <= :until_ts"); params["until_ts"] = until_ts
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
        return _json({"ok": True, "items": items, "limit": limit, "offset": offset, "has_more": has_more})
    except Exception as e:
        print(f"[history][RAW-ERROR] {e}\n{traceback.format_exc()}")
        return _json({"ok": False, "error": "history_failed"}, status=500)

@app.route("/api/history/recent", methods=["GET"])
def history_recent():
    ip = _client_ip()
    ok_ip, pay = _ratelimit("ip", ip, *RATE_LIMIT_IP)
    if not ok_ip: return _rate_limited_response(pay)

    try:
        limit = int(request.args.get("limit", "10"))
    except Exception:
        limit = 10
    limit = max(1, min(50, limit))
    try:
        q = Message.query.order_by(Message.timestamp.desc(), Message.id.desc()).limit(limit)
        items = [serialize_message(m) for m in q.all()]
        return _json({"ok": True, "items": items, "limit": limit})
    except Exception as e:
        print(f"[recent][ERROR] {e}\n{traceback.format_exc()}")
        return _json({"ok": False, "error": "recent_failed"}, status=500)

# ─────────────────────── /api/chat ────────────────────────
@app.route("/api/chat", methods=["POST"])
def chat():
    ip = _client_ip()
    ok_ip, pay = _ratelimit("ip", ip, *RATE_LIMIT_IP)
    if not ok_ip: return _rate_limited_response(pay)

    data = request.get_json(silent=True) or {}
    user_id    = _clip((data.get("user_id") or ""), 64)
    session_id = _clip((data.get("session_id") or ""), 64)
    g._user_id_for_log = user_id or ""
    g._session_id_for_log = session_id or ""

    if user_id:
        ok_user, pay2 = _ratelimit("user", user_id, *RATE_LIMIT_USER)
        if not ok_user: return _rate_limited_response(pay2)

    ok_ts, details = verify_turnstile(data.get("cf_turnstile_token"), ip)
    if not ok_ts:
        return _json({"ok": False, "error": "turnstile_failed", "details": details}, status=403)

    user_message = _extract_message(data)
    if not user_message:
        return _json({"ok": False, "error": "message is required"}, status=400)

    reply = f"Echo: {user_message}"
    if openai_client and OPENAI_API_KEY:
        try:
            resp = openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "system", "content": "You are Jack."},
                          {"role": "user", "content": user_message}],
                temperature=0.7,
            )
            reply = resp.choices[0].message.content
        except Exception as e:
            reply = f"(fallback) {reply} – openai_error: {e}"

    ensure_schema()
    saved = False
    db_error = None
    try:
        db.session.add(Message(role="user", content=user_message, user_id=user_id, session_id=session_id))
        db.session.add(Message(role="jack", content=reply,       user_id=user_id, session_id=session_id))
        db.session.commit()
        saved = True
    except Exception as e1:
        db.session.rollback()
        db_error = f"orm_insert_failed: {e1}"
        print(f"[chat][DB-ERROR][ORM] {e1}\n{traceback.format_exc()}")
        try:
            table = "public.message" if _db_kind() == "postgres" else "message"
            db.session.execute(sa_text(f"""
                INSERT INTO {table} (role, content, user_id, session_id)
                VALUES (:r1, :c1, :u, :s), (:r2, :c2, :u, :s)
            """), {"r1":"user","c1":user_message,"u":user_id,"s":session_id,"r2":"jack","c2":reply})
            db.session.commit()
            saved = True
        except Exception as e2:
            db.session.rollback()
            db_error = f"{db_error}; raw_insert_failed: {e2}"
            print(f"[chat][DB-ERROR][RAW] {e2}\n{traceback.format_exc()}")

    return _json({"ok": True, "reply": reply, "saved": saved, "db_error": db_error, "build": BUILD})

# ────────────────────── Local run (dev) ───────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))















