# wsgi.py
"""
Gunicorn entrypoint for the Flask app.
"""
import sys
import traceback

# Try direct "app" import from app.py. If not, fall back to factory "create_app()".
try:
    from app import app as app
except Exception:
    try:
        from app import create_app  # type: ignore
        app = create_app()
    except Exception as e:
        print("FATAL during app import/create_app:", e, file=sys.stderr)
        traceback.print_exc()
        raise
