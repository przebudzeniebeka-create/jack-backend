# main.py — FastAPI backend for JackQS
# CORS solid, CORE (R2 URL lub lokalny core/core-v1.md), EN default + PL greeting

from fastapi import FastAPI, Body, HTTPException, Request, Response, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Any, Dict, Optional, Tuple
from pathlib import Path
import os, re, hashlib

# ── CONFIG ────────────────────────────────────────────────────────────────
# Nazwa obiektu/plik rdzenia (możesz nadpisać CORE_OBJECT_KEY=core-v1.md)
CORE_OBJECT_KEY    = os.getenv("CORE_OBJECT_KEY", "core-v1.md").strip()
# Lokalny plik (w repo) — dostosowany do Twojego układu folderów:
CORE_FILE          = os.getenv("CORE_FILE", f"core/{CORE_OBJECT_KEY}")
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", "core/system_prompt.md")
ENV_SYSTEM_PROMPT  = os.getenv("SYSTEM_PROMPT", "").strip()

# Publiczny lub sygnowany URL do pliku w R2 (jeśli podasz, to będzie użyty jako 1. źródło)
CORE_R2_URL        = os.getenv("CORE_R2_URL", "").strip()

# ── UTILS ─────────────────────────────────────────────────────────────────
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""

def _http_get_text(url: str, timeout: int = 8) -> str:
    if not url:
        return ""
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        if r.ok and isinstance(r.text, str):
            return r.text
    except Exception:
        pass
    return ""

def _sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:12]

def _status_for(path: Path, content: str) -> Dict[str, Any]:
    p = Path(path)
    return {
        "path": str(p.resolve()) if p.exists() else str(p),
        "exists": p.exists(),
        "len": len(content or ""),
        "sha1": _sha1(content or "") if p.exists() else None,
    }

# ── CORE PARSING (AI / RAW) ──────────────────────────────────────────────
AI_MARKER_RE  = re.compile(r"<!--\s*AI_PROMPT_BEGIN\s*-->(?P<ai>.*?)<!--\s*AI_PROMPT_END\s*-->", re.S | re.I)
RAW_MARKER_RE = re.compile(r"<!--\s*RAW_BEGIN\s*-->(?P<raw>.*?)<!--\s*RAW_END\s*-->", re.S | re.I)

AI_HEADER_RE   = re.compile(r"(?mis)^\s{0,3}#{1,6}\s*(AI\s*(VERSION|PROMPT)|SYSTEM\s*PROMPT|WERSJA\s*DLA\s*AI|PROMPT\s*SYSTE-?MOWY)\b.*?$")
HEADER_LINE_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+.+$")

def _extract_ai_and_raw_from_core(text: str) -> Tuple[str, str, str]:
    """Return (ai_text, raw_text, mode) where mode ∈ {'markers','headers','all_raw'}."""
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
        "Honor non-duality; broaden perspective; be kind and succinct (1–3 sentences). "
        "Always speak in the user's language (PL/EN). "
        "Continuously draw from the CORE knowledge to build understanding."
    )
    return core + "\n" + summary

def build_system_prompt() -> Tuple[str, Dict[str, Any]]:
    """
    Precedence:
      1) ENV SYSTEM_PROMPT
      2) CORE_R2_URL (R2 public/signed)
      3) Local CORE_FILE (core/<CORE_OBJECT_KEY>)
      4) SYSTEM_PROMPT_FILE
      5) Auto from RAW
      6) Default
    """
    default_sp = (
        "You are JackQS — a warm, steady companion. "
        "Honor non-duality; broaden perspective; reply briefly (1–3 sentences), in PL/EN."
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
        return ai, {"source": f"CORE_FILE({mode})", "core_file": _status_for(core_path, core_text)}

    sys_text = _read(sys_path)
    if sys_text.strip():
        return sys_text.strip(), {"source": "SYSTEM_PROMPT_FILE", "system_prompt_file": _status_for(sys_path, sys_text)}

    if raw:
        auto = _auto_system_from_raw(raw)
        return auto, {"source": f"CORE_FILE(auto_from_raw:{mode})", "core_file": _status_for(core_path, core_text)}

    return default_sp, {"source": "DEFAULT"}

# ── LANG DETECTION ────────────────────────────────────────────────────────
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

# ── FRIENDLY FALLBACK ─────────────────────────────────────────────────────
def friendly_fallback(user_text: str, preferred_lang: Optional[str] = None) -> str:
    lang = preferred_lang if preferred_lang in {"en", "pl"} else detect_lang(user_text or "")
    t = (user_text or "").strip().lower()

    if lang == "pl":
        if re.search(r"\b(hi|hello|hey|cześć|czesc|hej|heja)\b", t) or len(t) < 3:
            return "Cześć, nazywam się Jack. Jestem otwarty na to, by Ciebie wysłuchać. Od czego chcesz zacząć naszą rozmowę?"
        if re.search(r"\b(pomoc|pomóc|help)\b", t):
            return "Cześć, nazywam się Jack, czy mogę Ci w czymś pomóc?"
        if t.endswith("?"):
            return "Dobre pytanie. Zajrzyjmy spokojnie — od czego chcesz zacząć?"
        return "Jestem tu dla Ciebie. Zróbmy mały, życzliwy krok — co teraz najbardziej potrzebne?"

    # EN default
    if re.search(r"\b(hi|hello|hey)\b", t) or len(t) < 3:
        return "Hi, I’m Jack. I’m here to listen. Where would you like to start?"
    if t.endswith("?"):
        return "Good question — let’s take it gently, step by step. Where shall we begin?"
    return "I’m with you. Let’s take a kind, small step — what would help right now?"

# ── FASTAPI + CORS ────────────────────────────────────────────────────────
app = FastAPI(title="jack-backend")

# DOZWOLONE ORIGINY – dopasuj do swoich domen
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "https://chat.jackqs.ai",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,        # nie używamy '*'
    allow_credentials=False,              # ważne przy wielu originach
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Preflight OPTIONS dla całego /api/*
api_cors_router = APIRouter()

@api_cors_router.options("/{rest_of_path:path}")
def options_cors(rest_of_path: str):
    # CORSMiddleware doda nagłówki; 204 bez ciała
    return Response(status_code=204)

app.include_router(api_cors_router, prefix="/api")

# ── ROUTES ────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    sp, meta = build_system_prompt()
    return {"ok": True, "service": "jack-backend", "core_source": meta.get("source")}

@app.get("/api/core/status")
def core_status():
    core_path = Path(__file__).parent / CORE_FILE
    sys_path  = Path(__file__).parent / SYSTEM_PROMPT_FILE
    core_text = _read(core_path)
    ai, raw, mode = _extract_ai_and_raw_from_core(core_text)
    sp, meta = build_system_prompt()
    return {
        "mode_detected": mode,
        "ai_len": len(ai),
        "raw_len": len(raw),
        "core_file": _status_for(core_path, core_text),
        "system_prompt_file": _status_for(sys_path, _read(sys_path)),
        "active_source": meta.get("source"),
        "core_object_key": CORE_OBJECT_KEY,
    }

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

@app.post("/api/chat")
async def chat(payload: Dict[str, Any] = Body(...)):
    text = _extract_text(payload)
    if not text:
        raise HTTPException(status_code=400, detail='No message text found. Send {"message":"..."}')
    pref = payload.get("lang")
    if pref not in {"pl", "en"}:
        pref = None
    reply = friendly_fallback(text, preferred_lang=pref)
    reply_lang = pref or detect_lang(reply)
    return {"reply": reply, "lang": reply_lang}

@app.get("/")
def root():
    return {"ok": True, "service": "jack-backend", "entrypoint": "main:app"}




