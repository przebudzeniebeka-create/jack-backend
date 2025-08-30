FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . /app

# Prosty healthcheck (Railway też pingnie /api/health)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python - <<'PY' || exit 1
import urllib.request,sys
try:
    urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3)
except Exception:
    sys.exit(1)
PY

CMD ["gunicorn","-w","4","-k","gthread","-t","120","-b","0.0.0.0:8000","app:app","--access-logfile","-"]
