# main.py — FastAPI backend for JackQS (full)
from fastapi import FastAPI, Body, HTTPException, Response, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from typing import Any, Dict, Optional, Tuple
from pathlib import Path
from pydantic import BaseModel
import os, re, hashlib, httpx

# ------------- CONFIG ---------------------------------------------------------
CORE_OBJECT_KEY    = os.getenv("CORE_OBJECT_KEY", "core-v1.md").strip()
CORE_FILE          = os.getenv("CORE_FILE", f"core/{CORE_OBJECT_KEY}")
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", "core/system_prompt.md")
ENV_SYSTEM_PROMPT  = os.getenv("SYSTEM_PROMPT", "").strip()
CORE_R2_URL        = os.getenv("CORE_R2_URL", "").strip()

# Turnstile secret: akceptuj obie nazwy
TURNSTILE_SECRET   = (os.getenv("TURNSTILE_SECRET") or os.getenv("TURNSTILE_SECRET_KEY") or "").strip()

# ------------- UTILS ----------------------------------------------------------
def _read(p: Path) -> str:
    try: return p.read_text(encoding="utf-8")
    except Exception: return ""

def _sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:12]

# ------------- PROMPT / LANG --------------------------------------------------
PL_CHARS = set("ąćęłńóśżź")
PL_WORDS = {"że","czy","nie","jak","co","dla","żeby","będzie","mam","muszę","dziękuję","proszę","dobrze","chcę","można","jesteś","jestem","to","tak","cześć","hej"}

def detect_lang(s: str) -> str:
    if not s: return "en"
    s_low = s.lower()
    if any(ch in PL_CHARS for ch in s_low): return "pl"
    tokens = re.findall(r"\b\w+\b", s_low)
    if sum(1 for t in tokens if t in PL_WORDS) >= 2: return "pl"
    return "en"

def friendly_fallback(user_text: str, preferred_lang: Optional[str] = None) -> str:
    lang = preferred_lang if preferred_lang in {"en","pl"} else detect_lang(user_text or "")
    t = (user_text or "").strip().lower()

    if lang == "pl":
        if re.search(r"\b(hi|hello|hey|cześć|czesc|hej|heja)\b", t) or len(t) < 3:
            return "Cześć, nazywam się Jack. Jestem otwarty na to, by Ciebie wysłuchać. Od czego chcesz zacząć naszą rozmowę?"
        if t.endswith("?"):
            return "Dobre pytanie. Zajrzyjmy spokojnie — od czego chcesz zacząć?"
        return "Jestem tu dla Ciebie. Zróbmy mały, życzliwy krok — co teraz najbardziej potrzebne?"

    if re.search(r"\b(hi|hello|hey)\b", t) or len(t) < 3:
        return "Hi, I’m Jack. I’m here to listen. Where would you like to start?"
    if t.endswith("?"):
        return "Good question — let’s take it gently, step by step. Where shall we begin?"
    return "I’m with you. Let’s take a kind, small step — what would help right now?"

# ------------- FASTAPI + CORS -------------------------------------------------
app = FastAPI(title="jack-backend")

ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "https://chat.jackqs.ai",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["*"],
)

# Preflight dla /api/*
api_cors_router = APIRouter()
@api_cors_router.options("/{rest:path}")
def options_cors(rest: str):
    return Response(status_code=204)

app.include_router(api_cors_router, prefix="/api")

# ------------- ROUTES ---------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "service": "jack-backend"}

class TurnstileVerifyIn(BaseModel):
    token: str

@app.post("/api/turnstile/verify")
async def turnstile_verify(body: TurnstileVerifyIn):
    token = (body.token or "").strip()
    if not TURNSTILE_SECRET:
        raise HTTPException(status_code=500, detail="TURNSTILE_SECRET not set")
    if len(token) < 10:
        return {"success": False, "errors": ["token_missing_or_short"]}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": TURNSTILE_SECRET, "response": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        j = r.json()
        return {"success": bool(j.get("success")), "errors": j.get("error-codes", [])}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"turnstile_verify_error:{e}")

def _extract_text(payload: Dict[str, Any]) -> str:
    for k in ("message","text","input"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

@app.post("/api/chat")
async def chat(payload: Dict[str, Any] = Body(...)):
    text = _extract_text(payload)
    if not text:
        raise HTTPException(status_code=400, detail='No message text. Send {"message":"..."}')
    pref = payload.get("lang")
    if pref not in {"pl","en"}: pref = None
    reply = friendly_fallback(text, preferred_lang=pref)
    return {"reply": reply, "lang": pref or detect_lang(reply)}

@app.get("/")
def root():
    return {"ok": True, "service": "jack-backend", "entrypoint": "main:app"}






