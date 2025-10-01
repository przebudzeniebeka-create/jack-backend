# main.py — FastAPI backend for JackQS
# - CORS
# - CORE (R2 URL lub lokalny core/core-v1.md)
# - Default greeting EN, auto-switch to PL on Polish input
# - Turnstile verify endpoint
# - /api/chat -> OpenAI z system_prompt z CORE

from fastapi import FastAPI, Body, HTTPException, Response, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict, Optional, Tuple
from pathlib import Path
import os, re, hashlib, requests

# ---------- CONFIG ----------
CORE_OBJECT_KEY    = os.getenv("CORE_OBJECT_KEY", "core-v1.md").strip()
CORE_FILE          = os.getenv("CORE_FILE", f"core/{CORE_OBJECT_KEY}")
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", "core/system_prompt.md")
ENV_SYSTEM_PROMPT  = os.getenv("SYSTEM_PROMPT", "").strip()
CORE_R2_URL        = os.getenv("CORE_R2_URL", "").strip()
TURNSTILE_SECRET   = os.getenv("TURNSTILE_SECRET", "").strip()
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# ---------- UTILS ----------
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""

def _http_get_text(url: str, timeout: int = 8) -> str:
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=timeout)
        if r.ok and isinstance(r.text, str):
            return r.text
    except Exception:
        pass
    return ""

def _sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:12]

# ---------- CORE PARSING ----------
import re as _re

AI_MARKER_RE  = _re.compile(r"<!--\s*AI_PROMPT_BEGIN\s*-->(?P<ai>.*?)<!--\s*AI_PROMPT_END\s*-->", _re.S | _re.I)
RAW_MARKER_RE = _re.compile(r"<!--\s*RAW_BEGIN\s*-->(?P<raw>.*?)<!--\s*RAW_END\s*-->", _re.S | _re.I)
AI_HEADER_RE   = _re.compile(r"(?mis)^\s{0,3}#{1,6}\s*(AI\s*(VERSION|PROMPT)|SYSTEM\s*PROMPT|WERSJA\s*DLA\s*AI|PROMPT\s*SYSTE-?MOWY)\b.*?$")
HEADER_LINE_RE = _re.compile(r"(?m)^\s{0,3}#{1,6}\s+.+$")

def _extract_ai_and_raw_from_core(text: str) -> Tuple[str, str, str]:
    if not (text or "").strip():
        return "", "", "all_raw"
    m_ai  = AI_MARKER_RE.search(text)
    m_raw = RAW_MARKER_RE.search(text)
    if m_ai or m_raw:
        ai  = (m_ai.group("ai") if m_ai else "").strip()
        raw = (m_raw.group("raw") if m_raw else text).strip()
        return ai, raw, "markers"
    m = AI_HEADER_RE.search(text)
    if m:
        start = m.start()
        rest  = text[start:]
        m_next = HEADER_LINE_RE.search(rest[m.end()-start:])
        ai = (rest[:m_next.start()] if m_next else rest).strip()
        return ai, text.strip(), "headers"
    return "", text.strip(), "all_raw"

def _auto_system_from_raw(raw: str, limit_chars: int = 1500) -> str:
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.splitlines()]
    keep, acc = [], 0
    for ln in lines:
        if ln.startswith(("#", "•", "-", "–", "*")) or (0 < len(ln) <= 120):
            keep.append(ln)
            acc += len(ln)
        if acc > limit_chars:
            break
    summary = "\n".join(keep)[:limit_chars]
    core = (
        "You are JackQS — a warm, steady companion. "
        "Default to EN on first contact, then answer in the user's language (PL/EN). "
        "Honor non-duality; broaden perspective; be kind and succinct (1–3 sentences). "
        "Continuously use the CORE knowledge to build understanding and context."
    )
    return core + "\n" + summary

def build_system_prompt() -> Tuple[str, Dict[str, Any]]:
    default_sp = (
        "You are JackQS — a warm, steady companion. "
        "Default to EN for greeting. After detecting Polish, answer in PL. "
        "Be kind, succinct (1–3 sentences). Use CORE if available."
    )

    if ENV_SYSTEM_PROMPT:
        return ENV_SYSTEM_PROMPT, {"source": "ENV.SYSTEM_PROMPT"}

    if CORE_R2_URL:
        r2_text = _http_get_text(CORE_R2_URL)
        if r2_text.strip():
            ai, raw, mode = _extract_ai_and_raw_from_core(r2_text)
            if ai:
                return ai, {"source": f"R2({mode})", "r2_url": CORE_R2_URL, "sha1": _sha1(r2_text)}
            if raw:
                auto = _auto_system_from_raw(raw)
                return auto, {"source": f"R2(auto_from_raw:{mode})", "r2_url": CORE_R2_URL, "sha1": _sha1(r2_text)}

    core_path = Path(__file__).parent / CORE_FILE
    sys_path  = Path(__file__).parent / SYSTEM_PROMPT_FILE
    core_text = _read(core_path)
    ai, raw, mode = _extract_ai_and_raw_from_core(core_text)
    if ai:
        return ai, {"source": f"CORE_FILE({mode})"}

    sys_text = _read(sys_path)
    if sys_text.strip():
        return sys_text.strip(), {"source": "SYSTEM_PROMPT_FILE"}

    if raw:
        auto = _auto_system_from_raw(raw)
        return auto, {"source": f"CORE_FILE(auto_from_raw:{mode})"}

    return default_sp, {"source": "DEFAULT"}

# ---------- LANG ----------
PL_CHARS = set("ąćęłńóśżź")
PL_WORDS = {"że","czy","nie","jak","co","dla","żeby","będzie","mam","muszę","dziękuję","proszę","dobrze","chcę","można","jesteś","jestem","to","tak","cześć","hej"}

def detect_lang(s: str) -> str:
    if not s:
        return "en"
    s_low = s.lower()
    if any(ch in PL_CHARS for ch in s_low):
        return "pl"
    tokens = re.findall(r"\b\w+\b", s_low)
    if sum(1 for t in tokens if t in PL_WORDS) >= 2:
        return "pl"
    return "en"

# ---------- APP + CORS ----------
app = FastAPI(title="jack-backend")

ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "https://chat.jackqs.ai",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# OPTIONS for /api/*
api_cors_router = APIRouter()
@api_cors_router.options("/{rest_of_path:path}")
def options_cors(rest_of_path: str):
    return Response(status_code=204)
app.include_router(api_cors_router, prefix="/api")

# ---------- Turnstile ----------
class TurnstileVerifyIn(BaseModel):
    token: str

import httpx

@app.post("/api/turnstile/verify")
async def turnstile_verify(body: TurnstileVerifyIn):
    if not TURNSTILE_SECRET:
        raise HTTPException(status_code=500, detail="TURNSTILE_SECRET not set")
    token = (body.token or "").strip()
    if len(token) < 10:
        return {"success": False, "errors": ["token_missing_or_short"]}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": TURNSTILE_SECRET, "response": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        j = resp.json()
        return {"success": bool(j.get("success")), "errors": j.get("error-codes", [])}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"turnstile_verify_error:{e}")

# ---------- Chat (OpenAI + CORE) ----------
def _extract_text(payload: Dict[str, Any]) -> str:
    for key in ("message", "text", "input"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs:
        last = msgs[-1]
        if isinstance(last, dict):
            c = last.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
            if isinstance(c, list):
                for part in c:
                    if isinstance(part, dict):
                        t = part.get("text")
                        if isinstance(t, str) and t.strip():
                            return t.strip()
    return ""

from openai import OpenAI
_client: Optional[OpenAI] = None

def openai_client() -> OpenAI:
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set")
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client

@app.post("/api/chat")
def chat(payload: Dict[str, Any] = Body(...)):
    user_text = _extract_text(payload)
    if not user_text:
        raise HTTPException(status_code=400, detail='No message text found. Send {"message":"..."}')

    # system prompt from CORE
    system_prompt, meta = build_system_prompt()

    # language: prefer EN on first contact; switch to PL only if we detect Polish in user_text
    prefer = payload.get("lang")
    detected = detect_lang(user_text)
    reply_lang = "pl" if detected == "pl" else (prefer if prefer in {"pl","en"} else "en")

    try:
        client = openai_client()
        msgs = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=msgs,
            temperature=0.6,
            max_tokens=300,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        # twardy fallback jeśli API padnie
        if reply_lang == "pl":
            text = "Cześć, jestem Jack. Jestem tu, by Cię życzliwie wesprzeć. Od czego zaczynamy?"
        else:
            text = "Hi, I’m Jack. I’m here to listen and help. Where would you like to start?"

    return {
        "reply": text,
        "lang": reply_lang,
        "core_source": meta.get("source"),
    }

@app.get("/")
def root():
    return {"ok": True, "service": "jack-backend", "entrypoint": "main:app"}








