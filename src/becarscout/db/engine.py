from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    Base,
    GlobalScrapeSettingsRow,
    ListingRow,
    PipelineSettingsRow,
    ScoringWeightsRow,
    SubscriberRow,
    UserListingScoreRow,
)

logger = logging.getLogger(__name__)

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


def _ensure_column(engine, table: str, column: str, ddl_type: str) -> None:
    """`Base.metadata.create_all` only creates missing *tables*, never adds
    columns to one that already exists on disk — this project has no
    Alembic wired to the real schema yet (see next_version.md), so a new
    column on an already-deployed DB needs this instead. Safe to call on
    every startup: a no-op once the column exists."""
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
            conn.commit()


def _migrate_singleuser_to_subscriber(engine) -> None:
    """One-time, idempotent migration for a deployment that predates
    multi-user support: `pipeline_settings`/`scoring_weights` used to be a
    single singleton row (id=1) shared by everyone; scoring/notification
    state lived directly on `ListingRow`. If `TELEGRAM_CHAT_ID` is set and
    that chat isn't a subscriber yet, this copies the legacy singleton
    values into a real per-chat row for them and — critically — carries
    over every already-scored listing's `notified_at` into
    `UserListingScoreRow`, so upgrading doesn't re-send everything they've
    already been notified about. Safe to call on every startup: it's a
    no-op the moment that chat_id has its own subscriber row, whether from
    this migration or from just using the bot normally."""
    chat_id_raw = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id_raw:
        return
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        return

    with Session(engine) as session:
        if session.execute(select(SubscriberRow).where(SubscriberRow.chat_id == chat_id)).scalar_one_or_none():
            return

        now = datetime.now(timezone.utc)
        session.add(SubscriberRow(chat_id=chat_id, created_at=now))

        legacy_settings = session.get(PipelineSettingsRow, 1)
        if legacy_settings is not None and legacy_settings.chat_id is None:
            session.add(
                PipelineSettingsRow(
                    chat_id=chat_id,
                    min_price=legacy_settings.min_price,
                    max_price=legacy_settings.max_price,
                    min_year=legacy_settings.min_year,
                    threshold=legacy_settings.threshold,
                    max_mileage_km=legacy_settings.max_mileage_km,
                    makes=legacy_settings.makes,
                    fuel_types=legacy_settings.fuel_types,
                    transmission=legacy_settings.transmission,
                    updated_at=now,
                )
            )
            if session.get(GlobalScrapeSettingsRow, 1) is None:
                session.add(GlobalScrapeSettingsRow(id=1, radius_km=legacy_settings.radius_km, updated_at=now))

        legacy_weights = session.get(ScoringWeightsRow, 1)
        if legacy_weights is not None and legacy_weights.chat_id is None:
            session.add(
                ScoringWeightsRow(
                    chat_id=chat_id,
                    gearbox_issue_likely_major=legacy_weights.gearbox_issue_likely_major,
                    gearbox_issue_minor=legacy_weights.gearbox_issue_minor,
                    gearbox_issue_unknown=legacy_weights.gearbox_issue_unknown,
                    engine_issue_likely_major=legacy_weights.engine_issue_likely_major,
                    engine_issue_minor=legacy_weights.engine_issue_minor,
                    engine_issue_unknown=legacy_weights.engine_issue_unknown,
                    accident_damage=legacy_weights.accident_damage,
                    warning_light_needs_diagnostic=legacy_weights.warning_light_needs_diagnostic,
                    warning_light_only=legacy_weights.warning_light_only,
                    timing_belt_replaced=legacy_weights.timing_belt_replaced,
                    inspection_valid=legacy_weights.inspection_valid,
                    inspection_invalid=legacy_weights.inspection_invalid,
                    service_history_complete=legacy_weights.service_history_complete,
                    service_history_none=legacy_weights.service_history_none,
                    for_export=legacy_weights.for_export,
                    min_plausible_car_price_eur=legacy_weights.min_plausible_car_price_eur,
                    updated_at=now,
                )
            )

        already_scored = session.execute(select(ListingRow).where(ListingRow.scored_at.is_not(None))).scalars().all()
        for row in already_scored:
            session.add(
                UserListingScoreRow(
                    chat_id=chat_id,
                    listing_id=row.listing_id,
                    price_component=row.price_component,
                    condition_component=row.condition_component,
                    score=row.score,
                    above_threshold=row.above_threshold,
                    reasoning_json=row.reasoning_json,
                    condition_highlights_json=row.condition_highlights_json,
                    scored_at=row.scored_at,
                    notified_at=row.notified_at,
                )
            )
            if row.baseline_computed_at is None:
                row.baseline_computed_at = row.scored_at

        session.commit()
        logger.info(
            "Migrated legacy single-user settings/weights/scores to subscriber %s (%d already-scored listing(s) carried over)",
            chat_id,
            len(already_scored),
        )


def _ensure_initialized() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        return
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}", echo=False)
    event.listens_for(_engine, "connect")(_set_sqlite_pragma)
    Base.metadata.create_all(bind=_engine)
    _ensure_column(_engine, "listings", "condition_highlights_json", "TEXT DEFAULT '[]'")
    _ensure_column(_engine, "listings", "baseline_computed_at", "DATETIME")
    _ensure_column(_engine, "pipeline_settings", "max_mileage_km", "INTEGER")
    _ensure_column(_engine, "pipeline_settings", "makes", "TEXT")
    _ensure_column(_engine, "pipeline_settings", "fuel_types", "TEXT")
    _ensure_column(_engine, "pipeline_settings", "transmission", "TEXT")
    _ensure_column(_engine, "pipeline_settings", "chat_id", "BIGINT")
    _ensure_column(_engine, "scoring_weights", "chat_id", "BIGINT")
    _ensure_column(_engine, "subscribers", "name", "TEXT")
    _migrate_singleuser_to_subscriber(_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def get_session() -> Session:
    _ensure_initialized()
    assert _SessionLocal is not None
    return _SessionLocal()
