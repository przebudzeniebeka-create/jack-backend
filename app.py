# app.py
from __future__ import annotations

import os, json, time, requests
from typing import Optional, Dict
from datetime import datetime

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text as sa_text

from dotenv import load_dotenv
load_dotenv()

# --------------------------- Flask ---------------------------
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
try:
    app.json.ensure_ascii = False
except Exception:
    pass

# Build info (do szybkiego sprawdzenia świeżości deployu)
BUILD = {
    "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("COMMIT_SHA") or "dev",
    "boot_ts": int(time.time()),
}

# ------------------------ Database --------------------------
# MUSI być ustawione w Railway: DATABASE_URL=postgresql+psycopg2://...?...sslmode=require
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

# -------------------------- CORS ----------------------------
# Na czas testów szeroko; później zawężamy do *.jackqs.ai i lokalnych devów.
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ------------------------ Turnstile -------------------------
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET_KEY", "")
BYPASS_TURNSTILE_DEV = os.getenv("BYPASS_TURNSTILE_DEV", "0")  # "1" = pomiń weryfikację

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

# -------------------------- OpenAI --------------------------
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("openai_api_key")
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    openai_client = None  # brak klucza nie powinien blokować startu

# ------------------------- Helpers --------------------------
def _extract_message(data: dict) -> Optional[str]:
    for k in ("message", "text", "prompt", "query", "q"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def _db_status() -> str:
    try:
        db.session.execute(sa_text("SELECT 1"))
        return "ok"
    except Exception:
        return "error"

# -------------------------- Routes --------------------------
@app.route("/")
def root():
    return "Jack backend – OK. Użyj /api/health"

@app.route("/api/health")
def health():
    # lekki healthcheck + minimalny status DB + build info
    return jsonify({
        "ok": True,
        "time": int(time.time()),
        "db": _db_status(),
        "build": BUILD,
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
    # Podgląd tras głównej appki (bez rozróżniania legacy)
    rules = sorted([str(r.rule) for r in app.url_map.iter_rules()])
    return jsonify(rules)

@app.route("/api/history", methods=["GET"])
def history_min():
    """
    Sanity endpoint — na teraz zwraca pustą listę.
    W następnym kroku podmienimy na wersję z SQLAlchemy i filtrami.
    """
    # opcjonalnie: przyjmij parametry, żeby frontend mógł już wołać /api/history?user_id=...
    _user_id = request.args.get("user_id", "")
    _session = request.args.get("session_id", "")
    _limit = request.args.get("limit", "")
    _offset = request.args.get("offset", "")
    return jsonify([])

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    ok_ts, details = verify_turnstile(data.get("cf_turnstile_token"), request.remote_addr)
    if not ok_ts:
        return jsonify({"ok": False, "error": "turnstile_failed", "details": details}), 403

    user_message = _extract_message(data)
    if not user_message:
        return jsonify({"error": "message is required"}), 400

    # prosta odpowiedź, a jeśli jest OpenAI – użyj
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

    # best-effort zapis do DB (bez twardego faila, żeby nie blokować odpowiedzi)
    try:
        db.session.add(Message(role="user", content=user_message))
        db.session.add(Message(role="jack", content=reply))
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify({"reply": reply})

# ------------------------- Run local ------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


















