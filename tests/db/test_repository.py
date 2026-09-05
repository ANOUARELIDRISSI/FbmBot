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


# --- pipeline settings (/budget, /minyear, /threshold, /radius) ---


def test_get_pipeline_settings_defaults_when_never_set(session):
    settings = repo.get_pipeline_settings(session)
    assert settings.radius_km == 100
    assert settings.min_year == 2010
    assert settings.threshold == 20
    assert settings.min_price is None
    assert settings.max_price is None


def test_update_pipeline_settings_partial_update_preserves_other_fields(session):
    repo.update_pipeline_settings(session, max_price=15000)
    repo.update_pipeline_settings(session, threshold=30)

    settings = repo.get_pipeline_settings(session)
    assert settings.max_price == 15000
    assert settings.threshold == 30
    assert settings.radius_km == 100  # untouched, still the default


def test_update_pipeline_settings_can_explicitly_disable_min_year(session):
    repo.update_pipeline_settings(session, min_year=None)
    assert repo.get_pipeline_settings(session).min_year is None


def test_update_pipeline_settings_budget_range(session):
    repo.update_pipeline_settings(session, min_price=3000, max_price=12000)
    settings = repo.get_pipeline_settings(session)
    assert (settings.min_price, settings.max_price) == (3000, 12000)


def test_get_pipeline_settings_defaults_for_new_filters(session):
    settings = repo.get_pipeline_settings(session)
    assert settings.max_mileage_km is None
    assert settings.makes is None
    assert settings.fuel_types is None
    assert settings.transmission is None


def test_update_pipeline_settings_new_filters_partial_update(session):
    repo.update_pipeline_settings(session, max_mileage_km=150_000)
    repo.update_pipeline_settings(session, makes="bmw,toyota")
    repo.update_pipeline_settings(session, fuel_types="diesel")
    repo.update_pipeline_settings(session, transmission="automatic")

    settings = repo.get_pipeline_settings(session)
    assert settings.max_mileage_km == 150_000
    assert settings.makes == "bmw,toyota"
    assert settings.fuel_types == "diesel"
    assert settings.transmission == "automatic"
    assert settings.radius_km == 100  # untouched, still the default


def test_update_pipeline_settings_can_clear_new_filters(session):
    repo.update_pipeline_settings(session, makes="bmw", max_mileage_km=100_000)
    repo.update_pipeline_settings(session, makes=None, max_mileage_km=None)

    settings = repo.get_pipeline_settings(session)
    assert settings.makes is None
    assert settings.max_mileage_km is None


# --- scoring weights (/validate) ---


def test_get_scoring_weights_defaults_when_never_set(session):
    weights = repo.get_scoring_weights(session)
    assert weights.for_export == -15
    assert weights.gearbox_issue_likely_major == -40


def test_update_scoring_weights_partial_update(session):
    repo.update_scoring_weights(session, warning_light_needs_diagnostic=-30)
    weights = repo.get_scoring_weights(session)
    assert weights.warning_light_needs_diagnostic == -30
    assert weights.for_export == -15  # untouched


def test_update_scoring_weights_twice_accumulates(session):
    repo.update_scoring_weights(session, accident_damage=-35)
    repo.update_scoring_weights(session, for_export=-20)
    weights = repo.get_scoring_weights(session)
    assert weights.accident_damage == -35
    assert weights.for_export == -20


def test_search_listings_matches_make_model_or_title(session):
    _make_row(session, "golf1", raw_title="VW Golf 7 TDI", make="Volkswagen", model_hint="Golf")
    _make_row(session, "bmw1", raw_title="BMW 320d", make="BMW", model_hint="320d")
    _make_row(session, "mystery", raw_title="Nice little golf cart", make=None, model_hint=None)

    hits = repo.search_listings(session, "golf")

    assert {row.listing_id for row in hits} == {"golf1", "mystery"}


def test_search_listings_ignores_unstructured_listings(session):
    _make_row(session, "structured", raw_title="Audi A3", make="Audi", model_hint="A3", structured_at=datetime.now(timezone.utc))
    _make_row(session, "unstructured", raw_title="Audi A4", make="Audi", model_hint="A4", structured_at=None)

    hits = repo.search_listings(session, "audi")

    assert [row.listing_id for row in hits] == ["structured"]


def test_search_listings_orders_by_score_then_recency(session):
    _make_row(session, "low", raw_title="Opel Corsa low score", make="Opel", model_hint="Corsa", score=5)
    _make_row(session, "high", raw_title="Opel Corsa high score", make="Opel", model_hint="Corsa", score=40)

    hits = repo.search_listings(session, "corsa")

    assert [row.listing_id for row in hits] == ["high", "low"]


def test_search_listings_respects_limit(session):
    for i in range(15):
        _make_row(session, f"seat{i}", raw_title=f"Seat Leon {i}", make="Seat", model_hint="Leon")

    hits = repo.search_listings(session, "leon", limit=5)

    assert len(hits) == 5
