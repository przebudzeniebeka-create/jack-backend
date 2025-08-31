# app.py
from __future__ import annotations

import os
import sys
import importlib
from typing import Optional

from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# ─────────────────────────────────────────────────────────────────────────────
# DB handle (initialized later inside create_app)
# ─────────────────────────────────────────────────────────────────────────────
db = SQLAlchemy()


def _normalize_db_url(url: Optional[str]) -> Optional[str]:
    """Ensure SQLAlchemy-friendly URL (e.g., postgres -> postgresql+psycopg2)."""
    if not url:
        return None
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    return url


def create_app() -> Flask:
    app = Flask(__name__)

    # ── Config: Database
    db_url = (
        os.getenv("DATABASE_URL")
        or os.getenv("SUPABASE_DB_URL")
        or os.getenv("SQLALCHEMY_DATABASE_URI")
    )
    db_url = _normalize_db_url(db_url)

    if not db_url:
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///local.db"
        app.config["DB_WARN"] = "DATABASE_URL missing; using sqlite:///local.db"
    else:
        app.config["SQLALCHEMY_DATABASE_URI"] = db_url

    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # ── CORS (open; adjust origins later if you want)
    CORS(app, resources={r"/*": {"origins": "*"}})

    # ── Init DB
    db.init_app(app)

    # ── Basic routes
    @app.get("/")
    def root():
        return jsonify({"ok": True, "msg": "Jack backend is running 🚀 (see /api/health)"}), 200

    @app.get("/api/health")
    def health():
        return jsonify({
            "ok": True,
            "db": "configured" if db_url else "sqlite-fallback",
            "warn": app.config.get("DB_WARN"),
            "legacy": app.config.get("LEGACY_STATUS", "not-mounted"),
            "legacy_error": app.config.get("LEGACY_ERROR"),
        }), 200

    @app.get("/api/version")
    def version():
        return jsonify({
            "version": os.getenv("APP_VERSION", "0.1.0"),
            "python": sys.version.split()[0]
        }), 200

    # ── Try to mount legacy app (from app_legacy.py) under /legacy
    try:
        legacy_mod = importlib.import_module("app_legacy")  # file added earlier
        legacy_app = getattr(legacy_mod, "app", None)
        if legacy_app is None and hasattr(legacy_mod, "create_app"):
            legacy_app = legacy_mod.create_app()

        if legacy_app is not None:
            # Mount legacy as a sub-application
            from werkzeug.middleware.dispatcher import DispatcherMiddleware
            app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
                "/legacy": legacy_app
            })
            app.config["LEGACY_STATUS"] = "mounted"
        else:
            app.config["LEGACY_STATUS"] = "not-found"
    except Exception as e:
        print("LEGACY attach failed:", e, file=sys.stderr)
        app.config["LEGACY_STATUS"] = "failed"
        app.config["LEGACY_ERROR"] = str(e)

    return app


# Export global 'app' for gunicorn "wsgi:app"
app = create_app()


















