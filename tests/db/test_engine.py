"""Tests the one-time single-user-to-subscriber migration directly against
an isolated in-memory engine -- this is the piece that protects a real,
already-populated production DB across the multi-user upgrade, so it's
worth covering thoroughly (see `db/engine.py`'s docstring)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from becarscout.db.engine import _migrate_singleuser_to_subscriber
from becarscout.db.models import (
    Base,
    GlobalScrapeSettingsRow,
    ListingRow,
    PipelineSettingsRow,
    ScoringWeightsRow,
    SubscriberRow,
    UserListingScoreRow,
)

CHAT_ID = 918273645


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return engine


def _seed_legacy_settings(engine, **overrides):
    defaults = dict(id=1, radius_km=120, min_price=1000, max_price=20000, min_year=2012, threshold=25)
    defaults.update(overrides)
    with Session(engine) as session:
        session.add(PipelineSettingsRow(**defaults))
        session.commit()


def _seed_legacy_weights(engine, **overrides):
    defaults = dict(id=1, for_export=-99)
    defaults.update(overrides)
    with Session(engine) as session:
        session.add(ScoringWeightsRow(**defaults))
        session.commit()


def _seed_scored_listing(engine, listing_id, **overrides):
    defaults = dict(
        listing_id=listing_id,
        url=f"https://example.test/{listing_id}",
        raw_title=f"Car {listing_id}",
        scraped_at=datetime.now(timezone.utc),
        score=42,
        above_threshold=True,
        scored_at=datetime.now(timezone.utc),
        notified_at=datetime.now(timezone.utc),
        reasoning_json="[]",
        condition_highlights_json="[]",
    )
    defaults.update(overrides)
    with Session(engine) as session:
        session.add(ListingRow(**defaults))
        session.commit()


def test_no_op_when_telegram_chat_id_not_set(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    engine = _engine()
    _seed_legacy_settings(engine)

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        assert session.execute(select(SubscriberRow)).scalars().all() == []


def test_no_op_when_chat_id_env_var_is_not_a_number(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "not-a-number")
    engine = _engine()

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        assert session.execute(select(SubscriberRow)).scalars().all() == []


def test_subscribes_the_chat_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
    engine = _engine()

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        assert session.get(SubscriberRow, CHAT_ID) is not None


def test_copies_legacy_settings_into_a_real_chat_row(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
    engine = _engine()
    _seed_legacy_settings(engine, min_price=3000, max_price=15000, threshold=30)

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        row = session.execute(select(PipelineSettingsRow).where(PipelineSettingsRow.chat_id == CHAT_ID)).scalar_one()
        assert row.min_price == 3000
        assert row.max_price == 15000
        assert row.threshold == 30


def test_copies_legacy_radius_into_global_scrape_settings(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
    engine = _engine()
    _seed_legacy_settings(engine, radius_km=175)

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        assert session.get(GlobalScrapeSettingsRow, 1).radius_km == 175


def test_copies_legacy_weights_into_a_real_chat_row(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
    engine = _engine()
    _seed_legacy_weights(engine, for_export=-77)

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        row = session.execute(select(ScoringWeightsRow).where(ScoringWeightsRow.chat_id == CHAT_ID)).scalar_one()
        assert row.for_export == -77


def test_carries_over_notified_at_so_nothing_gets_resent(monkeypatch):
    # The critical property: an existing single user must not get flooded
    # with re-sent notifications for cars they've already seen once this
    # deploys.
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
    engine = _engine()
    _seed_scored_listing(engine, "already_sent", score=55, above_threshold=True)

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        user_score = session.get(UserListingScoreRow, (CHAT_ID, "already_sent"))
        assert user_score is not None
        assert user_score.score == 55
        assert user_score.above_threshold is True
        assert user_score.notified_at is not None  # carried over -- won't be re-sent


def test_only_migrates_already_scored_listings(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
    engine = _engine()
    _seed_scored_listing(engine, "scored", scored_at=datetime.now(timezone.utc))
    with Session(engine) as session:
        session.add(
            ListingRow(
                listing_id="not_scored",
                url="https://example.test/not_scored",
                raw_title="Not scored yet",
                scraped_at=datetime.now(timezone.utc),
                scored_at=None,
                reasoning_json="[]",
                condition_highlights_json="[]",
            )
        )
        session.commit()

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        assert session.get(UserListingScoreRow, (CHAT_ID, "scored")) is not None
        assert session.get(UserListingScoreRow, (CHAT_ID, "not_scored")) is None


def test_sets_baseline_computed_at_on_migrated_listings_so_it_is_not_redone(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
    engine = _engine()
    # SQLite's DateTime column round-trips as naive -- compare naive on
    # both sides rather than asserting tzinfo survives, which it doesn't.
    scored_time = datetime(2026, 1, 1)
    _seed_scored_listing(engine, "l1", scored_at=scored_time, baseline_computed_at=None)

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        assert session.get(ListingRow, "l1").baseline_computed_at == scored_time


def test_is_idempotent_and_safe_to_call_every_startup(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
    engine = _engine()
    _seed_legacy_settings(engine, threshold=40)
    _seed_scored_listing(engine, "l1")

    _migrate_singleuser_to_subscriber(engine)
    # A second call must not duplicate rows or crash -- this runs on
    # every container startup, not just once.
    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        settings_rows = session.execute(select(PipelineSettingsRow).where(PipelineSettingsRow.chat_id == CHAT_ID)).scalars().all()
        assert len(settings_rows) == 1
        assert settings_rows[0].threshold == 40


def test_does_not_touch_a_chat_that_already_has_its_own_settings(monkeypatch):
    # If the migration already ran (or the person just used /budget
    # normally before ever restarting with this code), it should not
    # clobber their real, already-personalized settings with the stale
    # legacy singleton's values.
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
    engine = _engine()
    with Session(engine) as session:
        session.add(SubscriberRow(chat_id=CHAT_ID, created_at=datetime.now(timezone.utc)))
        session.add(PipelineSettingsRow(id=2, chat_id=CHAT_ID, threshold=99))
        session.commit()
    _seed_legacy_settings(engine, threshold=1)

    _migrate_singleuser_to_subscriber(engine)

    with Session(engine) as session:
        rows = session.execute(select(PipelineSettingsRow).where(PipelineSettingsRow.chat_id == CHAT_ID)).scalars().all()
        assert len(rows) == 1
        assert rows[0].threshold == 99  # untouched
