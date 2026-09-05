from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_DB_PATH = Path(os.getenv("BECARSCOUT_DB_PATH", "data/becarscout.db"))

_engine = None
_SessionLocal = None


def _ensure_initialized() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        return
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}", echo=False)
    Base.metadata.create_all(bind=_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def get_session() -> Session:
    _ensure_initialized()
    assert _SessionLocal is not None
    return _SessionLocal()
