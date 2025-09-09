import os
import json
import time
import typing as t
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import jwt

# ---- Prosty „tryb testu” 10 minut ----
TEST_SECRET = os.getenv("TEST_SECRET", "dev-test-secret-change-me")
TEST_DURATION_MIN = int(os.getenv("TEST_DURATION_MIN", "10"))

# ---- OpenAI (opcjonalnie) ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
try:
    from openai import OpenAI
    _client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    _client = None

# ---- App ----
app = FastAPI()

# PROSTE CORS NA TESTY – dowolny front (na produkcji zawęź!)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # na test OK, na prod ustaw konkretną domenę
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pamięć na oceny (trzymamy w RAM; GET /api/test/export zwróci zebrane)
RATINGS: list[dict] = []

# ---- Modele ----
class StartPayload(BaseModel):
    email: t.Optional[str] = None

class ChatPayload(BaseModel):
    message: str = Field(..., min_length=1)
    token: str

class RatePayload(BaseModel):
    rating: int = Field(..., ge=0, le=10)
    token: str
    comment: t.Optional[str] = None

# ---- Pomocnicze JWT ----
def issue_token(email: str | None) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=TEST_DURATION_MIN)
    payload = {
        "sub": email or "anon",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "scope": "test10"
    }
    token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")
    return token, int(exp.timestamp())

def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, TEST_SECRET, algorithms=["HS256"])
        if payload.get("scope") != "test10":
            raise HTTPException(status_code=403, detail="Invalid scope")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="SessionExpired")
    except Exception:
        raise HTTPException(status_code=401, detail="InvalidToken")

# ---- Endpoints ----
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "backend": "fastapi",
        "model": OPENAI_MODEL,
        "commit": os.getenv("COMMIT", "dev"),
        # celowo nie bawimy się już „bypass/verify”; dla testu: off
        "ts_mode": "off",
        "warn": None,
    }

@app.post("/api/test/start")
def test_start(p: StartPayload):
    token, exp = issue_token(p.email)
    return {"token": token, "expires_at": exp, "minutes": TEST_DURATION_MIN}

@app.post("/api/chat")
def chat(p: ChatPayload, request: Request):
    payload = verify_token(p.token)  # 401 gdy upłynęło 10 min
    user = payload.get("sub", "anon")
    text = p.message.strip()

    # Jeśli jest klucz OpenAI – zrób prostą odpowiedź.
    if _client:
        try:
            rsp = _client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are JackQS. Be concise."},
                    {"role": "user", "content": text},
                ],
                temperature=0.6,
            )
            out = rsp.choices[0].message.content
        except Exception as e:
            out = f"(fallback) I got an error talking to the model, echo: {text}\n{e}"
    else:
        out = f"(demo) You said: {text}"

    return {
        "ok": True,
        "user": user,
        "answer": out,
    }

@app.post("/api/test/rate")
def test_rate(p: RatePayload, request: Request):
    payload = verify_token(p.token)  # 401 gdy sesja wygasła (też OK)
    ip = request.headers.get("cf-connecting-ip") or request.client.host
    now = int(time.time())

    entry = {
        "ts": now,
        "user": payload.get("sub", "anon"),
        "rating": p.rating,
        "comment": (p.comment or "").strip(),
        "ip": ip,
    }
    RATINGS.append(entry)
    # dodatkowo log – łatwo zeskrobać z Railway Logs
    print("RATING:", json.dumps(entry, ensure_ascii=False))
    return {"ok": True}

@app.get("/api/test/export")
def test_export():
    return {"ok": True, "count": len(RATINGS), "items": RATINGS}
