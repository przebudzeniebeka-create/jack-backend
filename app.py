# app.py
from __future__ import annotations

import os, json, re, time, requests
from typing import Optional, Tuple, Dict
from datetime import datetime

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text as sa_text
from dotenv import load_dotenv
from dateutil import parser as dtparser

# -----------------------------------------------------------------------------
# Init
# -----------------------------------------------------------------------------
load_dotenv()
app = Flask(__name__)

# JSON UTF-8
app.config["JSON_AS_ASCII"] = False
try:
    app.json.ensure_ascii = False
except Exception:
    pass

# Database
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# OpenAI (używasz w /api/chat)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
from openai import OpenAI
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Pliki lokalne
HISTORY_FILE = "history.json"
BELIEFS_FILE = "core_beliefs.json"

# Turnstile
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET_KEY", "")
print("Turnstile secret configured:", bool(TURNSTILE_SECRET))

# Dev: pozwól obejść Turnstile lokalnie (domyślnie włączone)
BYPASS_TURNSTILE_DEV = os.getenv("BYPASS_TURNSTILE_DEV", "1") == "1"

# -----------------------------------------------------------------------------
# CORS
# -----------------------------------------------------------------------------
CORS(
    app,
    resources={r"/api/*": {"origins": [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://*.pages.dev",
        "https://jackqs.ai",
        "https://*.jackqs.ai",
    ]}},
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=86400,
)

ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://jackqs.ai",
    "https://www.jackqs.ai",
    "https://app.jackqs.ai",
}
PAGES_RE = re.compile(r"^https://[a-z0-9-]+\.jackqs-frontend\.pages\.dev$", re.IGNORECASE)

def origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return False
    return origin in ALLOWED_ORIGINS or bool(PAGES_RE.match(origin))

@app.after_request
def add_cors(resp):
    origin = request.headers.get("Origin")
    if origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        req_method = request.headers.get("Access-Control-Request-Method")
        req_headers = request.headers.get("Access-Control-Request-Headers")
        resp.headers["Access-Control-Allow-Methods"] = req_method or "GET, POST, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = req_headers or "Content-Type, Authorization"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"
    ct = (resp.headers.get("Content-Type") or "").lower()
    if resp.mimetype == "application/json" and "charset=" not in ct:
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp

@app.route("/api/<path:_any>", methods=["OPTIONS"])
def any_api_options(_any):
    return ("", 204)

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def load_core_beliefs_en() -> str:
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
    try:
        db.session.add(Message(role="user", content=user_message, user_id=user_id, session_id=session_id))
        db.session.add(Message(role="jack", content=jack_reply, user_id=user_id, session_id=session_id))
        db.session.commit()
    except Exception:
        db.session.rollback()

# Turnstile verify
def _dev_bypass_turnstile() -> bool:
    if not BYPASS_TURNSTILE_DEV:
        return False
    host = (request.host or "").split(":")[0]
    return host in ("127.0.0.1", "localhost")

def verify_turnstile(token: str, remote_ip: Optional[str] = None) -> tuple[bool, dict]:
    if _dev_bypass_turnstile():
        return True, {"dev": "bypass"}
    if not token or not TURNSTILE_SECRET:
        return False, {"error": "missing_token_or_secret"}
    try:
        resp = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": remote_ip or ""},
            timeout=4,
        )
        data = resp.json()
        return bool(data.get("success")), data
    except Exception as e:
        return False, {"error": str(e)}

# -----------------------------------------------------------------------------
# Lightweight capacity control
# -----------------------------------------------------------------------------
ACTIVE_USERS: Dict[str, float] = {}
ACTIVE_TTL_SEC = 600
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

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/")
def home():
    return "Jack backend is running (see /api/health)"

@app.route("/api/health", methods=["GET", "OPTIONS"])
def health():
    try:
        db.session.execute(sa_text("SELECT 1"))
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "db_error", "error": str(e)}), 500

def _get_param(data: dict, key: str) -> Optional[str]:
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
    for k in ("text", "prompt", "query", "q"):
        v = _get_param(data, k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if data:
        for k, v in data.items():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None

def _fallback_user_id() -> str:
    ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or "anon"
    return f"ip-{ip}"

# Chat
@app.route("/api/chat", methods=["POST", "GET", "OPTIONS"])
@app.route("/api/jack", methods=["POST", "GET", "OPTIONS"])
def chat_handler():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}

    # Turnstile (wymagane poza trybem dev-bypass)
    ts_token = data.get("cf_turnstile_token") or _get_param(data, "cf_turnstile_token")
    ok_ts, details = verify_turnstile(ts_token, request.remote_addr)
    print("Turnstile verify (chat):", "OK" if ok_ts else "FAIL", details.get("error"), details.get("messages"))
    if not ok_ts:
        return jsonify({"ok": False, "error": "turnstile_failed", "details": details}), 403

    user_message = _extract_message(data) or ""
    user_id      = (_get_param(data, "user_id") or request.headers.get("X-User-Id") or _fallback_user_id())
    session_id   = (_get_param(data, "session_id") or None)

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

# ------------------------------- TTS (ElevenLabs) -------------------------------
def _voice_id_for_lang(lang: str) -> str:
    lang = (lang or "en").lower()
    if lang.startswith("pl"):
        return os.getenv("ELEVENLABS_VOICE_ID_PL") or os.getenv("ELEVENLABS_VOICE_ID") or os.getenv("ELEVENLABS_VOICE_ID_EN") or ""
    else:
        return os.getenv("ELEVENLABS_VOICE_ID_EN") or os.getenv("ELEVENLABS_VOICE_ID") or ""

@app.route("/api/tts", methods=["POST", "OPTIONS"])
def tts():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    lang = (data.get("lang") or "en").strip()

    # Turnstile
    ts_token = data.get("cf_turnstile_token") or request.headers.get("X-CF-Turnstile")
    ok_ts, details = verify_turnstile(ts_token, request.remote_addr)
    print("Turnstile verify (tts):", "OK" if ok_ts else "FAIL", details.get("error"), details.get("messages"))
    if not ok_ts:
        return jsonify({"ok": False, "error": "turnstile_failed", "details": details}), 403

    if not text:
        return jsonify({"error": "text is required"}), 400

    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
    voice_id = _voice_id_for_lang(lang)
    if not api_key or not voice_id:
        return jsonify({"error": "missing_elevenlabs_key_or_voice_id"}), 500

    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?optimize_streaming_latency=2"
        payload = {
            "text": text,
            "model_id": model_id,
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.9},
        }
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        if r.status_code != 200:
            return jsonify({"error": "elevenlabs_error", "status": r.status_code, "body": r.text}), 502

        from flask import Response
        resp = Response(r.content, mimetype="audio/mpeg")
        resp.headers["Content-Disposition"] = "inline; filename=tts.mp3"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------------- History -------------------------------
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

# -----------------------------------------------------------------------------
# Local run
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))



















