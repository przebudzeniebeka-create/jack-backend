from flask import Flask, request, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from openai import OpenAI
import os
from dotenv import load_dotenv
import json
from datetime import datetime
from dateutil import parser as dtparser  # pip install python-dateutil
import requests
import base64

load_dotenv()

app = Flask(__name__)

# ── CORS ───────────────────────────────────────────────────────────────────────
# Produkcyjnie możesz zawęzić do konkretnych domen podając env CORS_ORIGIN="https://jackqs.ai,https://www.jackqs.ai"
cors_origins = os.getenv("CORS_ORIGIN")
if cors_origins:
    origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
    CORS(app, resources={r"/api/*": {"origins": origins}})
else:
    CORS(app)

# ── Klucze, DB ────────────────────────────────────────────────────────────────
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ── Konfiguracja ──────────────────────────────────────────────────────────────
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_TTS_CHARS = int(os.getenv("MAX_TTS_CHARS", "4000"))  # limit bezpieczeństwa

# ── Pliki (lokalny backup historii i beliefs) ─────────────────────────────────
HISTORY_FILE = "history.json"
BELIEFS_FILE = "core_beliefs.json"

# ── MODELE DB ────────────────────────────────────────────────────────────────
class Message(db.Model):
    __tablename__ = "Message"
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(10))          # 'user' | 'jack'
    content = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id = db.Column(db.String(64), index=True)
    session_id = db.Column(db.String(64), index=True)

def serialize_message(m: "Message"):
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "timestamp": (m.timestamp.isoformat() if m.timestamp else None),
        "user_id": m.user_id,
        "session_id": m.session_id,
    }

# ✅ Upewnij się, że tabele powstaną także w produkcji (Railway/gunicorn)
try:
    with app.app_context():
        db.create_all()
        print("✅ DB tables ensured")
except Exception as e:
    print("⚠️ DB init error:", e)

# ── ElevenLabs TTS ───────────────────────────────────────────────────────────
def synthesize_speech(text: str, voice_id: str = None, model_id: str = None) -> bytes:
    """
    Zwraca bajty MP3 z ElevenLabs.
    Wymaga .env: ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID (domyślnie), ELEVENLABS_MODEL_ID.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set in environment")

    # Parametry z żądania mają pierwszeństwo, potem .env
    voice_id = voice_id or os.getenv("ELEVENLABS_VOICE_ID", "")
    model_id = model_id or os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

    if not voice_id:
        raise RuntimeError("VOICE_ID missing: pass `voice_id` in request or set ELEVENLABS_VOICE_ID in .env")

    # Przytnij nadmiernie długi tekst
    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS]

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    r = requests.post(url, headers=headers, json=payload, stream=True, timeout=60)
    if r.status_code != 200:
        try:
            err = r.json()
        except Exception:
            err = r.text
        raise RuntimeError(f"ElevenLabs TTS error ({r.status_code}): {err}")

    return r.content  # MP3 bytes

# ── Core beliefs ──────────────────────────────────────────────────────────────
def load_core_beliefs(language="en"):
    try:
        with open(BELIEFS_FILE, "r", encoding="utf-8") as f:
            beliefs_data = json.load(f)
            beliefs_text = "\n\n".join(
                f"{belief.get('title','')}: {belief.get('content','')}"
                for belief in beliefs_data.get("core_beliefs", [])
            )
            if language == "pl":
                return (
                    "Jesteś Jackiem – wspierającym, pokornym i empatycznym towarzyszem, "
                    "który widzi świat z perspektywy niedualnej. Oto twoje podstawowe przekonania:\n\n"
                    f"{beliefs_text}"
                )
            else:
                return (
                    "You are Jack – a supportive, humble and empathetic companion who "
                    "sees the world through a non-dual perspective. Here are your core beliefs:\n\n"
                    f"{beliefs_text}"
                )
    except Exception as e:
        print("❌ Error loading beliefs:", str(e))
        return "You are Jack – a helpful and empathetic assistant."

# ── Zapis historii (plik + DB) ───────────────────────────────────────────────
def save_to_history(user_message, jack_reply, user_id=None, session_id=None):
    history_entry = {"user": user_message, "jack": jack_reply}
    # JSON (lokalny backup)
    try:
        if not os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump([history_entry], f, ensure_ascii=False, indent=2)
        else:
            with open(HISTORY_FILE, "r+", encoding="utf-8") as f:
                try:
                    history = json.load(f)
                except json.JSONDecodeError:
                    history = []
                history.append(history_entry)
                f.seek(0)
                json.dump(history, f, ensure_ascii=False, indent=2)
                f.truncate()
    except Exception as e:
        print("⚠️ File save failed:", str(e))

    # DB
    try:
        db.session.add(Message(role="user", content=user_message, user_id=user_id, session_id=session_id))
        db.session.add(Message(role="jack", content=jack_reply, user_id=user_id, session_id=session_id))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print("⚠️ DB save failed:", str(e))

# ── ROUTES ───────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "Backend Jack działa 🚀  (sprawdź też /api/health)"

# CHAT
@app.route("/api/jack", methods=["POST"])
def chat_with_jack():
    data = request.get_json() or {}
    user_message = data.get("message", "")
    language = data.get("language", "en")
    user_id = data.get("user_id")        # wymagane w testach 100 osób
    session_id = data.get("session_id")  # opcjonalnie do rozdzielania sesji

    # opcjonalne audio
    return_audio = bool(data.get("return_audio", False))
    tts_voice_id = data.get("voice_id")      # override domyślnego głosu
    tts_model_id = data.get("model_id")      # np. "eleven_multilingual_v2"

    if not user_message:
        return jsonify({"error": "message is required"}), 400
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        system_prompt = load_core_beliefs(language)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
        )
        jack_reply = response.choices[0].message.content
        save_to_history(user_message, jack_reply, user_id=user_id, session_id=session_id)

        result = {"reply": jack_reply}

        if return_audio:
            try:
                audio_bytes = synthesize_speech(
                    text=jack_reply,
                    voice_id=tts_voice_id,
                    model_id=tts_model_id,
                )
                audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
                result.update(
                    {
                        "audio_b64": audio_b64,
                        "audio_mime": "audio/mpeg",
                        "audio_bytes": len(audio_bytes),
                    }
                )
            except Exception as tts_err:
                # Nie blokuj odpowiedzi tekstowej:
                result["tts_error"] = str(tts_err)

        return jsonify(result)
    except Exception as e:
        print("❌ Backend error:", str(e))
        return jsonify({"error": str(e)}), 500

# TTS (JSON → base64 lub binarnie)
@app.route("/api/tts", methods=["POST"])
def tts():
    """
    body: { text: string, voice_id?: string, model_id?: string, as_base64?: bool }
    domyślnie zwraca base64 MP3 (JSON).
    """
    try:
        data = request.get_json() or {}
        text = data.get("text", "")
        voice_id = data.get("voice_id")
        model_id = data.get("model_id")
        as_base64 = data.get("as_base64", True)

        if not text.strip():
            return jsonify({"error": "text is required"}), 400

        audio_bytes = synthesize_speech(text, voice_id=voice_id, model_id=model_id)

        if as_base64:
            b64 = base64.b64encode(audio_bytes).decode("utf-8")
            return jsonify({"audio_b64": b64, "mime_type": "audio/mpeg", "length_bytes": len(audio_bytes)})
        else:
            return Response(audio_bytes, mimetype="audio/mpeg")
    except Exception as e:
        print("❌ /api/tts error:", str(e))
        return jsonify({"error": str(e)}), 500

# TTS (binarny stream)
@app.route("/api/tts.stream", methods=["POST"])
def tts_stream():
    """
    body: { text: string, voice_id?: string, model_id?: string }
    Zwraca binarne audio/mpeg.
    """
    try:
        data = request.get_json() or {}
        text = data.get("text", "")
        voice_id = data.get("voice_id")
        model_id = data.get("model_id")

        if not text.strip():
            return jsonify({"error": "text is required"}), 400

        audio_bytes = synthesize_speech(text, voice_id=voice_id, model_id=model_id)
        return Response(audio_bytes, mimetype="audio/mpeg")
    except Exception as e:
        print("❌ /api/tts.stream error:", str(e))
        return jsonify({"error": str(e)}), 500

# Ręczne dopisanie wiadomości do historii – przydatne do testów
@app.route("/api/message", methods=["POST"])
def add_message():
    """
    body: { role: 'user'|'jack', content: '...', user_id: 'u1', session_id?: 's1' }
    """
    data = request.get_json() or {}
    role = data.get("role")
    content = data.get("content")
    user_id = data.get("user_id")
    session_id = data.get("session_id")

    if role not in ("user", "jack") or not content or not user_id:
        return jsonify({"error": "role, content, user_id are required"}), 400

    try:
        m = Message(role=role, content=content, user_id=user_id, session_id=session_id)
        db.session.add(m)
        db.session.commit()
        return jsonify(serialize_message(m)), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

# HISTORIA (per user / per session) + paginacja
@app.route("/api/history", methods=["GET"])
def get_history():
    """
    Query params:
      - user_id: wymagane (albo session_id)
      - session_id: alternatywnie do user_id
      - limit: int (domyślnie 100, max 500)
      - order: 'asc' | 'desc' (domyślnie asc)
      - since, until: ISO datetime
      - after_id: zwróć rekordy o id > after_id (prosta paginacja strumieniowa)
    """
    try:
        user_id = request.args.get("user_id")
        session_id = request.args.get("session_id")

        if not user_id and not session_id:
            return jsonify({"error": "Provide user_id or session_id"}), 400

        limit = min(int(request.args.get("limit", 100)), 500)
        order = request.args.get("order", "asc").lower()
        since = request.args.get("since")
        until = request.args.get("until")
        after_id = request.args.get("after_id", type=int)

        q = Message.query
        if user_id:
            q = q.filter(Message.user_id == user_id)
        if session_id:
            q = q.filter(Message.session_id == session_id)

        if since:
            try:
                dt = dtparser.isoparse(since)
                q = q.filter(Message.timestamp >= dt)
            except Exception:
                return jsonify({"error": "Invalid 'since' datetime format. Use ISO 8601."}), 400

        if until:
            try:
                dt = dtparser.isoparse(until)
                q = q.filter(Message.timestamp <= dt)
            except Exception:
                return jsonify({"error": "Invalid 'until' datetime format. Use ISO 8601."}), 400

        if after_id:
            q = q.filter(Message.id > after_id)

        order_by = Message.timestamp.desc() if order == "desc" else Message.timestamp.asc()
        items = q.order_by(order_by).limit(limit).all()
        return jsonify([serialize_message(m) for m in items]), 200

    except Exception as e:
        print("❌ /api/history error:", str(e))
        return jsonify({"error": str(e)}), 500

# Wyczyść historię danego użytkownika/sesji (do testów)
@app.route("/api/history", methods=["DELETE"])
def clear_history():
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

# Healthcheck
@app.route("/api/health", methods=["GET"])
def health():
    try:
        db.session.execute(db.select(Message).limit(1))
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "db_error", "error": str(e)}), 500

# ── Lokalny start ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(port=5000)








