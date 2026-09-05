"""Uses an isolated in-memory SQLite DB (not the module-level singleton in
db/engine.py) so these tests never touch a real data/becarscout.db."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from becarscout.db import repository as repo
from becarscout.db.models import Base, ListingRow


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine)
    s = session_local()
    yield s
    s.close()


def _make_row(
    session,
    listing_id,
    *,
    notified_at=None,
    above_threshold=True,
    feedback_verdict=None,
    score=42,
):
    row = ListingRow(
        listing_id=listing_id,
        url=f"https://example.test/{listing_id}",
        raw_title=f"Car {listing_id}",
        scraped_at=datetime.now(timezone.utc),
        score=score,
        above_threshold=above_threshold,
        notified_at=notified_at,
        feedback_verdict=feedback_verdict,
        reasoning_json="[]",
        condition_highlights_json="[]",
    )
    session.add(row)
    session.commit()
    return row


def test_mark_feedback_sets_verdict(session):
    _make_row(session, "l1")
    repo.mark_feedback(session, "l1", "up")
    assert session.get(ListingRow, "l1").feedback_verdict == "up"


def test_mark_feedback_on_unknown_listing_does_not_raise(session):
    repo.mark_feedback(session, "nope", "up")


def test_get_opportunities_in_range_filters_by_notified_at(session):
    now = datetime.now(timezone.utc)
    _make_row(session, "recent", notified_at=now - timedelta(days=1))
    _make_row(session, "old", notified_at=now - timedelta(days=30))
    _make_row(session, "not_notified", notified_at=None)

    entries = repo.get_opportunities_in_range(session, now - timedelta(days=7), now)
    ids = [listing.listing_id for listing, _ in entries]
    assert ids == ["recent"]


def test_get_opportunities_in_range_returns_most_recent_first(session):
    now = datetime.now(timezone.utc)
    _make_row(session, "a", notified_at=now - timedelta(days=3))
    _make_row(session, "b", notified_at=now - timedelta(days=1))

    entries = repo.get_opportunities_in_range(session, now - timedelta(days=7), now)
    ids = [listing.listing_id for listing, _ in entries]
    assert ids == ["b", "a"]


def test_get_opportunities_in_range_includes_feedback_verdict(session):
    now = datetime.now(timezone.utc)
    _make_row(session, "reviewed", notified_at=now, feedback_verdict="down")
    entries = repo.get_opportunities_in_range(session, now - timedelta(days=1), now + timedelta(seconds=1))
    assert entries[0][1] == "down"


def test_get_unreviewed_opportunities_excludes_reviewed_and_unnotified(session):
    now = datetime.now(timezone.utc)
    _make_row(session, "unreviewed", notified_at=now, feedback_verdict=None)
    _make_row(session, "reviewed", notified_at=now, feedback_verdict="up")
    _make_row(session, "not_notified_yet", notified_at=None, feedback_verdict=None)

    unreviewed = repo.get_unreviewed_opportunities(session)
    ids = [listing.listing_id for listing in unreviewed]
    assert ids == ["unreviewed"]


def test_get_unreviewed_opportunities_respects_since(session):
    now = datetime.now(timezone.utc)
    _make_row(session, "recent", notified_at=now - timedelta(days=1))
    _make_row(session, "old", notified_at=now - timedelta(days=30))

    unreviewed = repo.get_unreviewed_opportunities(session, since=now - timedelta(days=7))
    ids = [listing.listing_id for listing in unreviewed]
    assert ids == ["recent"]
