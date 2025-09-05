# main.py
from __future__ import annotations
import os, typing as t
import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI

# ─────────────────────────────────────────────────────────────────────────────
# Config / ENV
# ─────────────────────────────────────────────────────────────────────────────
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# (zostawiamy pod przyszłe włączenie Turnstile)
def _get_turnstile_secret() -> str:
    return (
        os.getenv("TURNSTILE_SECRET")
        or os.getenv("TURNSTILE_SECRET_KEY")
        or os.getenv("CF_TURNSTILE_SECRET")
        or ""
    )
TURNSTILE_SECRET  = _get_turnstile_secret()

ELEVEN_API_KEY    = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID   = os.getenv("ELEVEN_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVEN_VOICE_ID_EN= os.getenv("ELEVEN_VOICE_ID_EN", "")
ELEVEN_VOICE_ID_PL= os.getenv("ELEVEN_VOICE_ID_PL", "")

# CORS (jackqs.ai, *.pages.dev, localhost)
FRONTEND_ORIGINS      = os.getenv("FRONTEND_ORIGINS", "http://localhost:3000")
FRONTEND_ORIGIN_REGEX = os.getenv(
    "FRONTEND_ORIGIN_REGEX",
    r"^https://([a-z0-9-]+\.)?jackqs\.ai$|^https://([a-z0-9-]+\.)?pages\.dev$|^http://localhost:3000$"
)
allowed_origins = [o.strip() for o in FRONTEND_ORIGINS.split(",") if o.strip()]

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI(title="JackQS API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=FRONTEND_ORIGIN_REGEX or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: t.Literal["system", "user", "assistant"]
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., description="Historia rozmowy")
    turnstile_token: t.Optional[str] = Field(None, description="cf-turnstile-response z frontu")

class ChatResponse(BaseModel):
    text: str
    usage: dict | None = None
    source: str | None = None

class TTSRequest(BaseModel):
    text: str
    voice_id: t.Optional[str] = None
    lang: t.Optional[str] = None  # 'en' lub 'pl'
    turnstile_token: t.Optional[str] = None

# ─────────────────────────────────────────────────────────────────────────────
# Turnstile (BY PASS – na czas debugowania)
# ─────────────────────────────────────────────────────────────────────────────
def verify_turnstile(token: str | None, remoteip: str | None) -> tuple[bool, dict]:
    # HARD BYPASS: zawsze przepuszcza
    return True, {"bypass": True}

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"ok": True, "service": "jackqs-api", "routes": [r.path for r in app.router.routes]}

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "backend": "fastapi",
        "model": OPENAI_MODEL,
        "commit": os.getenv("RAILWAY_GIT_COMMIT_SHA"),
        "ts_mode": "bypass",   # znacznik dla sanity-checku
        "warn": None,
    }

@app.get("/api/routes")
def routes():
    return {"routes": [r.path for r in app.router.routes]}

@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: Request, body: ChatRequest):
    # (bypass – verify zwraca True)
    ip = req.client.host if req.client else None
    ok, details = verify_turnstile(body.turnstile_token, ip)
    if not ok:
        raise HTTPException(status_code=403, detail={"turnstile": details})

    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

    # System prompt, jeśli brak
    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    if not any(m["role"] == "system" for m in messages):
        messages.insert(0, {
            "role": "system",
            "content": "You are Jack, a concise, kind, fast assistant. Keep answers short and helpful."
        })

    try:
        res = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.6,
            max_tokens=700,
        )
        text = res.choices[0].message.content or ""
        usage = res.usage.to_dict() if getattr(res, "usage", None) else None
        return ChatResponse(text=text, usage=usage)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenAI error: {e}")

@app.post("/api/tts")
def tts(req: Request, body: TTSRequest):
    """Zwraca audio/mpeg (ElevenLabs). Jeśli brak klucza – 204 jako fallback."""
    # (bypass – verify zwraca True)
    ip = req.client.host if req.client else None
    ok, details = verify_turnstile(body.turnstile_token, ip)
    if not ok:
        raise HTTPException(status_code=403, detail={"turnstile": details})

    if not ELEVEN_API_KEY:
        return Response(status_code=204)

    # priorytet: jawny voice_id > język (pl/en) > domyślny
    voice_id = (
        body.voice_id
        or (ELEVEN_VOICE_ID_PL if (body.lang == "pl" and ELEVEN_VOICE_ID_PL) else None)
        or (ELEVEN_VOICE_ID_EN if (body.lang == "en" and ELEVEN_VOICE_ID_EN) else None)
        or ELEVEN_VOICE_ID
    )

    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": ELEVEN_API_KEY,
            "accept": "audio/mpeg",
            "content-type": "application/json"
        }
        payload = {
            "text": body.text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}
        }
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 200 and r.content:
            return Response(content=r.content, media_type="audio/mpeg")
        return Response(status_code=204)
    except Exception:
        return Response(status_code=204)


