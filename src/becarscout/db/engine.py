from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_DB_PATH = Path(os.getenv("BECARSCOUT_DB_PATH", "data/becarscout.db"))

_engine = None
_SessionLocal = None


def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
    """SQLite's default journal mode serializes writers against readers —
    fine when this DB only ever had one process touching it at a time, but
    the standing feedback listener (`becarscout listen`) and the hourly
    pipeline (`becarscout run`) now both run continuously in the same
    container, genuinely reading/writing concurrently. WAL lets readers
    and a writer proceed together instead of blocking; busy_timeout makes
    a real write/write collision retry briefly instead of raising
    "database is locked" immediately."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _ensure_initialized() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        return
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}", echo=False)
    event.listens_for(_engine, "connect")(_set_sqlite_pragma)
    Base.metadata.create_all(bind=_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def get_session() -> Session:
    _ensure_initialized()
    assert _SessionLocal is not None
    return _SessionLocal()
