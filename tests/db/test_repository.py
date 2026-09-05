"""Uses an isolated in-memory SQLite DB (not the module-level singleton in
db/engine.py) so these tests never touch a real data/becarscout.db."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from becarscout.db import repository as repo
from becarscout.db.models import Base, ListingRow
from becarscout.scraper.models import RawListing


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine)
    s = session_local()
    yield s
    s.close()


def _make_row(session, listing_id, **overrides):
    defaults = dict(
        listing_id=listing_id,
        url=f"https://example.test/{listing_id}",
        raw_title=f"Car {listing_id}",
        scraped_at=datetime.now(timezone.utc),
        price_text="€8.000,-",
        description="original description",
        structured_at=datetime.now(timezone.utc),
        analyzed_at=datetime.now(timezone.utc),
        scored_at=datetime.now(timezone.utc),
        notified_at=datetime.now(timezone.utc),
        photo_urls_json="[]",
        reasoning_json="[]",
        condition_highlights_json="[]",
    )
    defaults.update(overrides)
    row = ListingRow(**defaults)
    session.add(row)
    session.commit()
    return row


def test_get_known_price_texts_returns_only_listings_with_a_price(session):
    _make_row(session, "with_price", price_text="€8.000,-")
    _make_row(session, "without_price", price_text=None)

    prices = repo.get_known_price_texts(session)

    assert prices == {"with_price": "€8.000,-"}


def test_update_changed_listing_refreshes_raw_fields(session):
    _make_row(session, "l1", price_text="€8.000,-", description="old text")
    updated = RawListing(
        listing_id="l1", url="https://example.test/l1", title="Car l1",
        price_text="€7.000,-", description="new text", photo_urls=["https://example.test/photo.jpg"],
    )

    repo.update_changed_listing(session, updated)

    row = session.get(ListingRow, "l1")
    assert row.price_text == "€7.000,-"
    assert row.description == "new text"
    assert row.photo_urls_json == '["https://example.test/photo.jpg"]'


def test_update_changed_listing_resets_downstream_stages_for_rescoring(session):
    _make_row(session, "l1", description="old text")
    updated = RawListing(listing_id="l1", url="https://example.test/l1", title="Car l1", price_text="€7.000,-", description="old text")

    repo.update_changed_listing(session, updated)

    row = session.get(ListingRow, "l1")
    assert row.structured_at is None
    assert row.scored_at is None
    assert row.notified_at is None


def test_update_changed_listing_only_resets_analysis_if_description_actually_changed(session):
    _make_row(session, "same_description", description="unchanged text")
    same_text = RawListing(listing_id="same_description", url="u", title="t", price_text="€7.000,-", description="unchanged text")
    repo.update_changed_listing(session, same_text)
    assert session.get(ListingRow, "same_description").analyzed_at is not None  # not reset -- saves a Mistral call

    _make_row(session, "changed_description", description="old text")
    new_text = RawListing(listing_id="changed_description", url="u", title="t", price_text="€7.000,-", description="edited text")
    repo.update_changed_listing(session, new_text)
    assert session.get(ListingRow, "changed_description").analyzed_at is None


def test_update_changed_listing_on_unknown_id_does_not_raise(session):
    listing = RawListing(listing_id="nope", url="u", title="t", price_text="€1", description=None)
    repo.update_changed_listing(session, listing)  # should just no-op


def test_reset_scoring_for_rescore_only_touches_analyzed_listings(session):
    _make_row(session, "analyzed", analyzed_at=datetime.now(timezone.utc), scored_at=datetime.now(timezone.utc))
    _make_row(session, "not_yet_analyzed", analyzed_at=None, scored_at=None)

    count = repo.reset_scoring_for_rescore(session)

    assert count == 1
    assert session.get(ListingRow, "analyzed").scored_at is None
    assert session.get(ListingRow, "not_yet_analyzed").scored_at is None  # untouched, already None


def test_reset_scoring_for_rescore_leaves_notified_at_alone(session):
    _make_row(session, "l1", analyzed_at=datetime.now(timezone.utc), scored_at=datetime.now(timezone.utc), notified_at=datetime.now(timezone.utc))

    repo.reset_scoring_for_rescore(session)

    assert session.get(ListingRow, "l1").notified_at is not None
