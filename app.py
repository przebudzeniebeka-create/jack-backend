# app.py
from __future__ import annotations

import os
import sys
import importlib
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text as sa_text
from werkzeug.middleware.dispatcher import DispatcherMiddleware

# ─────────────────────────────────────────────────────────────────────────────
# DB handle (initialized later inside create_app)
# ─────────────────────────────────────────────────────────────────────────────
db = SQLAlchemy()


def _normalize_db_url(url: Optional[str]) -> Optional[str]:
    """
    Make the URL SQLAlchemy/psycopg2 friendly and force SSL.
    - postgres:// -> postgresql+psycopg2://
    - ensure ?sslmode=require (or PGSSLMODE if set)
    """
    if not url:
        return None
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)

    try:
        parsed = urlparse(url)
        q = dict(parse_qsl(parsed.query))
        if "sslmode" not in q:
            q["sslmode"] = os.getenv("PGSSLMODE", "require")
        new_query = urlencode(q)
        url = urlunparse(parsed._replace(query=new_query))
    except Exception:
        pass

    return url


def _collect_routes(flask_app: Flask) -> List[Dict[str, Any]]:
    routes: List[Dict[str, Any]] = []
    for rule in flask_app.url_map.iter_rules():
        routes.append({
            "rule": str(rule),
            "endpoint": rule.endpoint,
            "methods": sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"}),
        })
    routes.sort(key=lambda r: r["rule"])
    return routes


def create_app() -> Flask:
    app = Flask(__name__)

    # ── Config: Database URL
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

    # ── Engine options: healthy connection pool
    engine_opts = {
        "pool_pre_ping": True,
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE", "300")),
        "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "2")),
        "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT", "30")),
    }
    connect_args = {}
    if (db_url or "").startswith("postgresql+psycopg2://"):
        connect_args["sslmode"] = os.getenv("PGSSLMODE", "require")
    if connect_args:
        engine_opts["connect_args"] = connect_args
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_opts

    # ── CORS (na razie otwarte; zawęzimy po testach)
    CORS(app, resources={r"/*": {"origins": "*"}})

    # ── Init DB
    db.init_app(app)

    # ── Basic routes
    @app.get("/")
    def root():
        return jsonify({"ok": True, "msg": "Jack backend is running 🚀 (see /api/health)"}), 200

    @app.get("/api/health")
    def health():
        info = {
            "ok": True,
            "warn": app.config.get("DB_WARN"),
            "legacy": app.config.get("LEGACY_STATUS", "not-mounted"),
        }
        if not db_url:
            info["db"] = "sqlite-fallback"
            return jsonify(info), 200

        try:
            with db.engine.connect() as conn:
                conn.execute(sa_text("SELECT 1"))
            info["db"] = "ok"
        except Exception as e:
            info["db"] = "error"
            info["error"] = f"{e.__class__.__name__}: {e}"
            return jsonify(info), 200

        return jsonify(info), 200

    # ── NEW: list all routes (main + legacy)
    @app.get("/api/routes")
    def routes():
        data = {"main": _collect_routes(app)}
        legacy_app = app.config.get("LEGACY_APP_REF")
        if legacy_app is not None:
            try:
                data["legacy"] = _collect_routes(legacy_app)  # type: ignore[arg-type]
            except Exception as e:
                data["legacy_error"] = f"{e.__class__.__name__}: {e}"
        else:
            data["legacy"] = []
        return jsonify(data), 200

    # ── Mount legacy (from app_legacy.py) under /legacy
    try:
        legacy_mod = importlib.import_module("app_legacy")  # file added earlier
        legacy_app = getattr(legacy_mod, "app", None)
        if legacy_app is None and hasattr(legacy_mod, "create_app"):
            legacy_app = legacy_mod.create_app()

        if legacy_app is not None:
            app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/legacy": legacy_app})
            app.config["LEGACY_STATUS"] = "mounted"
            app.config["LEGACY_APP_REF"] = legacy_app
        else:
            app.config["LEGACY_STATUS"] = "not-found"
    except Exception as e:
        print("LEGACY attach failed:", e, file=sys.stderr)
        app.config["LEGACY_STATUS"] = "failed"
        app.config["LEGACY_ERROR"] = str(e)

    return app


# Export global 'app' for gunicorn "wsgi:app"
app = create_app()


















