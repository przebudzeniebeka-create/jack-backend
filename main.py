# main.py — FastAPI backend for JackQS
# CORS allowlist from ENV, CORE_FILE parsing (AI/RAW), language detection, minimal chat fallback

from fastapi import FastAPI, Body, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Any, Dict, Optional, Tuple, List
from pathlib import Path
import os, re, hashlib
from datetime import datetime

# ── CONFIG ───────────────────────────────────────────────────────────────────
CORE_FILE          = os.getenv("CORE_FILE", "core/jack_master.md")
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", "core/system_prompt.md")
ENV_SYSTEM_PROMPT  = os.getenv("SYSTEM_PROMPT")
CORS_ORIGIN_ENV    = os.getenv("CORS_ORIGIN", "*")  # np. "https://chat.jackqs.ai, https://jackqs.ai"

# ── UTILS ────────────────────────────────────────────────────────────────────
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""

def _sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:12]

def _status_for(path: Path, content: str) -> Dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "exists": path.exists(),
        "len": len(content),
        "sha1": _sha1(content) if path.exists() else None,
    }

def _parse_allowlist(raw: str) -> List[str]:
    items = [x.strip() for x in (raw or "").split(",") if x.strip()]
    return items or ["*"]

# ── CORE_FILE PARSING → (AI, RAW) ───────────────────────────────────────────
AI_MARKER_RE  = re.compile(r"<!--\s*AI_PROMPT_BEGIN\s*-->(?P<ai>.*?)<!--\s*AI_PROMPT_END\s*-->", re.S | re.I)
RAW_MARKER_RE = re.compile(r"<!--\s*RAW_BEGIN\s*-->(?P<raw>.*?)<!--\s*RAW_END\s*-->", re.S | re.I)

# Header-based split
AI_HEADER_RE   = re.compile(r"(?mi)^\s{0,3}#{1,6}\s*(AI\s*(VERSION|PROMPT)|SYSTEM\s*PROMPT|WERSJA\s*DLA\s*AI|PROMPT\s*SYSTE-?MOWY)\b.*$")
HEADER_LINE_RE = re.compile(r"(?mi)^\s{0,3}#{1,6}\s+.+$")

def _extract_ai_and_raw_from_core(text: str) -> Tuple[str, str, str]:
    """
    Returns (ai_text, raw_text, mode)
    mode ∈ {"markers", "headers", "all_raw"}
    """
    if not text or not text.strip():
        return "", "", "all_raw"

    # 1) explicit markers win
    m_ai  = AI_MARKER_RE.search(text)
    m_raw = RAW_MARKER_RE.search(text)
    if m_ai or m_raw:
        ai  = (m_ai.group("ai") if m_ai else "").strip()
        raw = (m_raw.group("raw") if m_raw else text).strip()
        return ai, raw, "markers"

    # 2) header-based — AI section = from AI header to the next header (or EOF)
    m = AI_HEADER_RE.search(text)
    if m:
        start_ai = m.end()
        m_next = HEADER_LINE_RE.search(text, pos=start_ai)
        ai = text[start_ai:(m_next.start() if m_next else None)].strip()
        return ai, text.strip(), "headers"

    # 3) fallback: everything is RAW
    return "", text.strip(), "all_raw"

def _auto_system_from_raw(raw: str, limit_chars: int = 1500) -> str:
    """Create a compact system prompt out of the RAW part (first bullets/short lines)."""
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
        "Accompany gently (non-directive), honor non-duality, speak in user's language (PL/EN), "
        "reply in 1–3 concise sentences, offer one doable next step."
    )
    return core + "\n" + summary

def build_system_prompt() -> Tuple[str, Dict[str, Any]]:
    """Resolve the active system prompt and return (text, meta)."""
    default_sp = (
        "You are JackQS — a warm, steady companion. "
        "Accompany gently, honor non-duality, reply briefly (1–3 sentences), in PL/EN."
    )
    if ENV_SYSTEM_PROMPT and ENV_SYSTEM_PROMPT.strip():
        return ENV_SYSTEM_PROMPT.strip(), {"source": "ENV.SYSTEM_PROMPT"}

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

# ── FASTAPI APP + CORS ──────────────────────────────────────────────────────
app = FastAPI(title="jack-backend")

_allowlist = _parse_allowlist(CORS_ORIGIN_ENV)
# Jeśli w allowlist jest "*", otwieramy CORS dla wszystkich; inaczej tylko podane domeny.
if "*" in _allowlist:
    allow_origins = ["*"]
else:
    allow_origins = _allowlist

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,          # przy "*" musi być False
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ── HEALTH & CORE STATUS ────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    _sp, meta = build_system_prompt()
    return {
        "ok": True,
        "service": "jack-backend",
        "time": datetime.utcnow().isoformat() + "Z",
        "core_source": meta.get("source"),
        "cors": {
            "mode": "open" if "*" in allow_origins else "allowlist",
            "allow_origins": allow_origins,
        },
    }

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
        "using": {"source": meta.get("source")},
    }

# ── INPUT EXTRACTION ────────────────────────────────────────────────────────
def _extract_text(payload: Dict[str, Any]) -> str:
    # Simple keys first
    for key in ("message", "text", "input"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # OpenAI-style messages array
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

# ── SIMPLE LANGUAGE DETECTION (PL/EN) ───────────────────────────────────────
PL_CHARS = set("ąćęłńóśżź")
PL_WORDS = {"że","czy","nie","jak","co","dla","żeby","będzie","mam","muszę","dziękuję","proszę","dobrze","chcę","można","jesteś","jestem","to","tak"}

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

# ── FRIENDLY FALLBACK (EN/PL) ───────────────────────────────────────────────
def friendly_fallback(user_text: str, preferred_lang: Optional[str] = None) -> str:
    """
    Minimal, kind reply in the desired language (used until model integration).
    preferred_lang: "en" | "pl" | None
    """
    lang = preferred_lang if preferred_lang in {"en", "pl"} else detect_lang(user_text or "")
    t = (user_text or "").strip().lower()

    if lang == "pl":
        if re.search(r"\b(hi|hello|hey|cześć|czesc|hej|heja)\b", t):
            return "Cześć ✨ Jestem obok. Jak mogę Ci dziś towarzyszyć?"
        if len(t) < 5:
            return "Jestem tutaj. Opowiedz proszę trochę więcej — co teraz najbardziej potrzebne?"
        if t.endswith("?"):
            return "Dobre pytanie. Zajrzyjmy krok po kroku — od czego zaczniemy?"
        return "Rozumiem. Zróbmy mały, życzliwy krok: co byłoby pomocne w tej chwili?"
    else:
        if re.search(r"\b(hi|hello|hey|cześć|czesc|hej|heja)\b", t):
            return "Hi ✨ I’m here with you. How can I be with you today?"
        if len(t) < 5:
            return "I’m here. Tell me a little more—what feels most needed right now?"
        if t.endswith("?"):
            return "Good question. Let’s take it gently, step by step—where shall we begin?"
        return "I hear you. Let’s take a kind, small step—what would help in this moment?"

# ── /api/chat ───────────────────────────────────────────────────────────────
@app.post("/api/chat")
async def chat(payload: Dict[str, Any] = Body(...)):
    """
    Request body (frontend):
    { "message": "hello", "lang": "en" }   # lang is optional; "en"|"pl"
    """
    text = _extract_text(payload)
    if not text:
        raise HTTPException(status_code=400, detail='No message text found. Send {"message":"..."}')

    pref = payload.get("lang")
    if pref not in {"pl", "en"}:
        pref = None

    reply = friendly_fallback(text, preferred_lang=pref)
    user_lang  = detect_lang(text)
    reply_lang = pref or detect_lang(reply)
    return {"reply": reply, "lang": reply_lang}

# Optional root ping
@app.get("/")
def root():
    return {"ok": True, "service": "jack-backend", "entrypoint": "main:app"}


