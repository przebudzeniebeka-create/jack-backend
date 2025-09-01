# app.py
from __future__ import annotations

import os, time, requests
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

# Info o buildzie (szybkie sprawdzenie świeżości deployu)
BUILD = {
    "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("COMMIT_SHA") or "dev",
    "boot_ts": int(time.time()),
}

# ──────────────────────── Database ───────────────────────
# W Railway: DATABASE_URL=postgresql+psycopg2://...?...sslmode=require
db_url = os.getenv("DATABASE_URL", "sqlite:///app.db")
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

class Message(db.Model):
    __tablename__ = "Message"
    id         = db.Column(db.Integer, primary_key=True)
    role       = db.Column(db.String(10))      # 'user' | 'jack'
    content    = db.Column(db.Text)
    timestamp  = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id    = db.Column(db.String(64), index=True)
    session_id = db.Column(db.String(64), index=True)

with app.app_context():
    db.create_all()

def serialize_message(m: "Message") -> Dict[str, object]:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "timestamp": (m.timestamp.isoformat() if m.timestamp else None),
        "user_id": m.user_id,
        "session_id": m.session_id,
    }

# ───────────────────────── CORS ─────────────────────────
# Testowo szeroko; później zawęzimy do *.jackqs.ai i lokalnych dev originów
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ─────────────────────── Turnstile ───────────────────────
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET_KEY", "")
BYPASS_TURNSTILE_DEV = os.getenv("BYPASS_TURNSTILE_DEV", "0")  # "1" = pomijaj weryfikację

def verify_turnstile(token: str, remote_ip: Optional[str] = None) -> tuple[bool, dict]:
    # DEV bypass lub brak secretu -> nie blokuj
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
    openai_client = None  # brak klucza nie powinien blokować startu

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

def _db_scheme_info(uri: str) -> Dict[str, str]:
    # zwraca "kind" i skrócony "scheme" do diagnostyki
    scheme = (uri.split("://", 1)[0] if "://" in uri else uri).lower()
    kind = "postgres" if "postgres" in scheme else ("sqlite" if "sqlite" in scheme else scheme)
    return {"kind": kind, "scheme": scheme}

# ─────────────────────── Routes ──────────────────────────
@app.route("/")
def root():
    return "Jack backend – OK. Użyj /api/health"

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "time": int(time.time()),
        "db": _db_status(),
        "db_url": _db_scheme_info(db_url),
        "build": BUILD
    })

@app.route("/api/db-ping")
def db_ping():
    try:
        db.session.execute(sa_text("SELECT 1"))
        return jsonify({"db": "ok"})
    except Exception as e:
        return jsonify({"db": "error", "error": str(e)}), 500

@app.route("/api/routes")
def routes_list():
    rules = sorted([str(r.rule) for r in app.url_map.iter_rules()])
    return jsonify(rules)

# ─────────── /api/history — wersja DB z filtrami ─────────
@app.route("/api/history", methods=["GET"])
def history_db():
    """
    Query params:
      user_id, session_id, role(user|jack), search, since_ts, until_ts,
      order(asc|desc=default), limit(1..200=50), offset(>=0=0)
    """
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

    items = [serialize_message(m) for m in q.offset(offset).limit(limit + 1).all()]
    has_more = len(items) > limit
    if has_more:
        items = items[:limit]

    return jsonify({"ok": True, "items": items, "limit": limit, "offset": offset, "has_more": has_more})

# ─────── /api/history/recent — ostatnie N bez filtrów ────
@app.route("/api/history/recent", methods=["GET"])
def history_recent():
    limit_raw = request.args.get("limit", "")
    try:
        limit = int(limit_raw)
    except Exception:
        limit = 10
    limit = max(1, min(50, limit))
    q = Message.query.order_by(Message.timestamp.desc(), Message.id.desc()).limit(limit)
    items = [serialize_message(m) for m in q.all()]
    return jsonify({"ok": True, "items": items, "limit": limit})

# ─────────────────────── /api/chat ────────────────────────
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}

    # Turnstile (dev: BYPASS_TURNSTILE_DEV=1 w Railway Variables)
    ok_ts, details = verify_turnstile(data.get("cf_turnstile_token"), request.remote_addr)
    if not ok_ts:
        return jsonify({"ok": False, "error": "turnstile_failed", "details": details}), 403

    # Treść wiadomości
    user_message = _extract_message(data)
    if not user_message:
        return jsonify({"ok": False, "error": "message is required"}), 400

    # Identyfikacja rozmowy (do historii)
    user_id    = _clip(data.get("user_id"), 64)
    session_id = _clip(data.get("session_id"), 64)

    # Domyślna odpowiedź (fallback)
    reply = f"Echo: {user_message}"

    # OpenAI (SDK 1.x)
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

    # Zapis do DB (obie wiadomości, best-effort) + informacja czy się udało
    saved = False
    try:
        db.session.add(Message(role="user", content=user_message, user_id=user_id, session_id=session_id))
        db.session.add(Message(role="jack", content=reply,       user_id=user_id, session_id=session_id))
        db.session.commit()
        saved = True
    except Exception as e:
        # widoczne w logach Railway (Deploy/HTTP logs)
        print(f"[chat][DB-ERROR] {e}")
        db.session.rollback()

    return jsonify({"ok": True, "reply": reply, "saved": saved, "build": BUILD})

# ────────────────────── Local run (dev) ───────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

















