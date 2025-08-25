# app.py
from __future__ import annotations

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from openai import OpenAI
import os, json, re, time
from dotenv import load_dotenv
from datetime import datetime
from dateutil import parser as dtparser  # pip install python-dateutil
from typing import Optional, Tuple, Dict
from sqlalchemy import text as sa_text

# ─────────────────────────────────────────────────────────────────────────────
# Init
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()
app = Flask(__name__)

# JSON in UTF-8 (no \uXXXX escapes)
app.config["JSON_AS_ASCII"] = False
try:
    app.json.ensure_ascii = False  # Flask >= 2.3
except Exception:
    pass

# Database
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# OpenAI
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Local files (simple JSON backups)
HISTORY_FILE = "history.json"
BELIEFS_FILE = "core_beliefs.json"

# ─────────────────────────────────────────────────────────────────────────────
# CORS (Cloudflare Pages, własne domeny i localhost)
# ─────────────────────────────────────────────────────────────────────────────
CORS(
    app,
    resources={r"/api/*": {"origins": [
        "https://jackqs.ai",
        "https://*.jackqs.ai",
        "https://jackqs-frontend.pages.dev",
        "https://*.pages.dev",
        "http://localhost:5173",
    ]}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=86400,
)

ALLOWED_ORIGINS = {
    "https://app.jackqs.ai",
    "https://jackqs.ai",
    "https://www.jackqs.ai",
    "http://localhost:5173",
}
PAGES_RE = re.compile(r"^https://[a-z0-9-]+\.jackqs-frontend\.pages\.dev$", re.IGNORECASE)

_env = os.getenv("CORS_ORIGIN", "")
if _env.strip():
    ALLOWED_ORIGINS |= {o.strip() for o in _env.split(",") if o.strip()}

def origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return False
    return origin in ALLOWED_ORIGINS or bool(PAGES_RE.match(origin))

@app.after_request
def add_cors(resp):
    # Doprecyzowanie nagłówków i charset dla JSON
    origin = request.headers.get("Origin")
    if origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"

        req_method = request.headers.get("Access-Control-Request-Method")
        req_headers = request.headers.get("Access-Control-Request-Headers")
        resp.headers["Access-Control-Allow-Methods"] = req_method or "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = req_headers or "Content-Type, Authorization"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"

    ct = (resp.headers.get("Content-Type") or "").lower()
    if resp.mimetype == "application/json" and "charset=" not in ct:
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp

# Global preflight dla dowolnego /api/*
@app.route("/api/<path:_any>", methods=["OPTIONS"])
def any_api_options(_any):
    return ("", 204)

# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────
class Message(db.Model):
    __tablename__ = "Message"
    id         = db.Column(db.Integer, primary_key=True)
    role       = db.Column(db.String(10))      # 'user' | 'jack'
    content    = db.Column(db.Text)
    timestamp  = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id    = db.Column(db.String(64), index=True)
    session_id = db.Column(db.String(64), index=True)

def serialize_message(m: "Message") -> Dict[str, object]:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "timestamp": (m.timestamp.isoformat() if m.timestamp else None),
        "user_id": m.user_id,
        "session_id": m.session_id,
    }

with app.app_context():
    db.create_all()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def load_core_beliefs_en() -> str:
    """English-only system prompt."""
    try:
        with open(BELIEFS_FILE, "r", encoding="utf-8") as f:
            beliefs_data = json.load(f)
        beliefs_text = "\n\n".join(
            f"{b.get('title','')}: {b.get('content','')}"
            for b in beliefs_data.get("core_beliefs", [])
        )
        return (
            "You are Jack – a supportive, humble, and empathetic companion who sees the world "
            "through a non-dual perspective. Here are your core beliefs:\n\n"
            f"{beliefs_text}"
        )
    except Exception:
        return "You are Jack – a helpful and empathetic assistant."

def save_to_history(user_message: str, jack_reply: str, user_id: Optional[str], session_id: Optional[str]) -> None:
    entry = {"user": user_message, "jack": jack_reply}
    # JSON backup
    try:
        if not os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump([entry], f, ensure_ascii=False, indent=2)
        else:
            with open(HISTORY_FILE, "r+", encoding="utf-8") as f:
                try:
                    history = json.load(f)
                except json.JSONDecodeError:
                    history = []
                history.append(entry)
                f.seek(0)
                json.dump(history, f, ensure_ascii=False, indent=2)
                f.truncate()
    except Exception:
        pass
    # DB
    try:
        db.session.add(Message(role="user", content=user_message, user_id=user_id, session_id=session_id))
        db.session.add(Message(role="jack", content=jack_reply, user_id=user_id, session_id=session_id))
        db.session.commit()
    except Exception:
        db.session.rollback()

# ─────────────────────────────────────────────────────────────────────────────
# Lightweight capacity control
# ─────────────────────────────────────────────────────────────────────────────
ACTIVE_USERS: Dict[str, float] = {}
ACTIVE_TTL_SEC = 600  # 10 minutes
ACTIVE_LIMIT   = 100

def _cleanup_active(now: float) -> None:
    stale_before = now - ACTIVE_TTL_SEC
    for uid in [u for u, ts in ACTIVE_USERS.items() if ts < stale_before]:
        ACTIVE_USERS.pop(uid, None)

def _check_capacity(user_id: str) -> Tuple[bool, Optional[str]]:
    now = time.time()
    _cleanup_active(now)
    if user_id not in ACTIVE_USERS and len(ACTIVE_USERS) >= ACTIVE_LIMIT:
        return False, "capacity_reached"
    ACTIVE_USERS[user_id] = now
    return True, None

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "Jack backend is running 🚀  (see /api/health)"

@app.route("/api/health", methods=["GET", "OPTIONS"])
def health():
    try:
        db.session.execute(sa_text("SELECT 1"))
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "db_error", "error": str(e)}), 500

def _get_param(data: dict, key: str) -> Optional[str]:
    """Prefer JSON, then query, then form; zwróć przycięty string."""
    v = data.get(key)
    if v is None:
        v = request.args.get(key)
    if v is None:
        v = request.form.get(key)
    if isinstance(v, str):
        return v.strip()
    return v

def _extract_message(data: dict) -> Optional[str]:
    msg = _get_param(data, "message")
    if msg:
        return msg
    # delikatne fallbacki na inne nazwy
    for k in ("text", "prompt", "query", "q"):
        v = _get_param(data, k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # jeśli JSON zawiera 1 sensowne pole tekstowe – użyj go
    if data:
        for k, v in data.items():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None

def _chat_handler():
    data = request.get_json(silent=True) or {}
    user_message = _extract_message(data) or ""
    user_id      = (_get_param(data, "user_id") or "")  # JSON albo ?user_id=...
    session_id   = (_get_param(data, "session_id") or None)

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    if not user_message:
        return jsonify({"error": "message is required"}), 400

    ok, _ = _check_capacity(user_id)
    if not ok:
        return jsonify({"error": "capacity reached, please try again in a moment"}), 429

    try:
        system_prompt = load_core_beliefs_en()
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
        )
        jack_reply = resp.choices[0].message.content
        save_to_history(user_message, jack_reply, user_id=user_id, session_id=session_id)
        return jsonify({"reply": jack_reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/jack", methods=["POST", "GET", "OPTIONS"])
def chat_with_jack():
    return _chat_handler()

@app.route("/api/chat", methods=["POST", "GET", "OPTIONS"])
def chat_alias():
    return _chat_handler()

# History API
@app.route("/api/history", methods=["GET", "DELETE", "OPTIONS"])
def history():
    if request.method == "OPTIONS":
        return ("", 204)

    if request.method == "DELETE":
        try:
            user_id = request.args.get("user_id")
            session_id = request.args.get("session_id")
            if not user_id and not session_id:
                return jsonify({"error": "Provide user_id or session_id"}), 400

            q = db.session.query(Message)
            if user_id:
                q = q.filter(Message.user_id == user_id)
            if session_id:
                q = q.filter(Message.session_id == session_id)

            deleted = q.delete(synchronize_session=False)
            db.session.commit()
            return jsonify({"status": "ok", "deleted": int(deleted)})
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": str(e)}), 500

    try:
        user_id = request.args.get("user_id")
        session_id = request.args.get("session_id")
        if not user_id and not session_id:
            return jsonify({"error": "Provide user_id or session_id"}), 400

        limit    = min(int(request.args.get("limit", 100)), 500)
        order    = request.args.get("order", "asc").lower()
        since    = request.args.get("since")
        until    = request.args.get("until")
        after_id = request.args.get("after_id", type=int)

        q = Message.query
        if user_id:
            q = q.filter(Message.user_id == user_id)
        if session_id:
            q = q.filter(Message.session_id == session_id)
        if since:
            try:
                q = q.filter(Message.timestamp >= dtparser.isoparse(since))
            except Exception:
                return jsonify({"error": "Invalid 'since' datetime. Use ISO 8601."}), 400
        if until:
            try:
                q = q.filter(Message.timestamp <= dtparser.isoparse(until))
            except Exception:
                return jsonify({"error": "Invalid 'until' datetime. Use ISO 8601."}), 400
        if after_id:
            q = q.filter(Message.id > after_id)

        order_by = Message.timestamp.desc() if order == "desc" else Message.timestamp.asc()
        items = q.order_by(order_by).limit(limit).all()
        return jsonify([serialize_message(m) for m in items])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# Local run
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))



















