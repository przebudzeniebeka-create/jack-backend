# bust-cache: 2025-08-31-0959
FROM python:3.11-slim
ARG CACHEBUST=2025-08-31-1008

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . /app

# Prosty healthcheck - używa PORT z env
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python - <<'PY' || exit 1
import os, urllib.request, sys
port = os.environ.get("PORT", "8000")
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3)
except Exception:
    sys.exit(1)
PY

# Uruchom gunicorn, nasłuchuj na $PORT (domyślnie 8000)
CMD ["sh","-lc","gunicorn -w 4 -k gthread -t 120 -b 0.0.0.0:${PORT:-8000} app:app --access-logfile -"]

