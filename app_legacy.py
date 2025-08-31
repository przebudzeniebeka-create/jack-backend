# app.py
from __future__ import annotations

import os
from typing import Optional
from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy

# ── DB init (global handle, ale nie łączymy się przy imporcie)
db = SQLAlchemy()

def _normalize_db_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    # Railway/Supabase czasem dają postgres:// - SQLAlchemy  wymaga postgresql+psycopg2://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    # Supabase "pooler" i "direct" obie formy zadziałają, ważny jest driver.
    return url

def create_app() -> Flask:
    app = Flask(__name__)

    # ── ENV & Config (nie wywalaj appa przy braku ENV; loguj i dawaj bezpieczne fallbacki)
    db_url = (
        os.getenv("DATABASE_URL")
        or os.getenv("SUPABASE_DB_URL")
        or os.getenv("SQLALCHEMY_DATABASE_URI")
    )
    db_url = _normalize_db_url(db_url)

    if not db_url:
        # Fallback na SQLite, żeby app nie umierał na starcie; log + health pokaże WARN
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///local.db"
        app.config["DB_WARN"] = "DATABASE_URL missing; using sqlite:///local.db"
    else:
        app.config["SQLALCHEMY_DATABASE_URI"] = db_url

    # Opcjonalnie: wyłącz ostrzeżenia trackowania
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # ── Zainicjalizuj db (nie tworzymy tabel przy imporcie, ale można to zrobić na żądanie)
    db.init_app(app)

    @app.get("/api/health")
    def health():
        return jsonify({
            "ok": True,
            "db": "configured" if db_url else "sqlite-fallback",
            "warn": app.config.get("DB_WARN")
        })

    # Dodaj swój główny endpoint, kiedy już ruszy health
    @app.get("/")
    def root():
        return jsonify({"ok": True, "msg": "Jack backend is running 🚀 (see /api/health)"})

    return app

# Jeśli chcesz też mieć zmienną globalną "app", aby działało `from app import app`
app = create_app()


















