# database.py
import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, func
)
from sqlalchemy.orm import sessionmaker, declarative_base

# Weź URL z env (Railway/Supabase) albo użyj SQLite lokalnie
RAW_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

def _normalize_url(url: str) -> str:
    """
    Upewnij się, że używamy sterownika psycopg2.
    Railway/Supabase często podają 'postgresql://...'
    W SQLAlchemy 2.x domyślny kierowca to 'psycopg' (v3), więc jawnie
    przełączamy na 'postgresql+psycopg2://'.
    """
    if not url:
        return "sqlite:///jack_memory.db"

    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)

    if url.startswith("postgresql://") and "+psycopg2" not in url:
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)

    return url

DATABASE_URL = _normalize_url(RAW_DATABASE_URL)

engine_kwargs = dict(pool_pre_ping=True)

# Specjalne opcje dla SQLite lokalnie
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

Base = declarative_base()

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(255), nullable=True)
    role = Column(String(32), nullable=False)
    message = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

def init_db() -> None:
    """Utwórz tabele, jeśli nie istnieją."""
    Base.metadata.create_all(bind=engine)

def save_message(user_id: str, role: str, message: str) -> None:
    """Zapisz wiadomość w bazie."""
    with SessionLocal() as db:
        db.add(Conversation(user_id=user_id, role=role, message=message))
        db.commit()

