# --- base ---
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (na wszelki wypadek pod psycopg / libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Najpierw tylko requirements – to wymusza świeżą instalację
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# Potem reszta aplikacji
COPY . /app

# Gunicorn
ENV PORT=8000
EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-k", "gthread", "-t", "120", "-b", "0.0.0.0:8000", "app:app"]


