"""Uses an isolated in-memory SQLite DB (not the module-level singleton in
db/engine.py) so these tests never touch a real data/becarscout.db."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from becarscout.db import repository as repo
from becarscout.db.models import Base, ListingRow, UserListingScoreRow
from becarscout.pricing.models import PriceBaseline
from becarscout.scraper.models import RawListing

CHAT_A = 111
CHAT_B = 222


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
        baseline_computed_at=datetime.now(timezone.utc),
        photo_urls_json="[]",
        reasoning_json="[]",
        condition_highlights_json="[]",
    )
    defaults.update(overrides)
    row = ListingRow(**defaults)
    session.add(row)
    session.commit()
    return row


def _make_user_score(session, chat_id, listing_id, **overrides):
    defaults = dict(chat_id=chat_id, listing_id=listing_id, reasoning_json="[]", condition_highlights_json="[]")
    defaults.update(overrides)
    row = UserListingScoreRow(**defaults)
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


def test_update_changed_listing_resets_structuring(session):
    _make_row(session, "l1", description="old text")
    updated = RawListing(listing_id="l1", url="https://example.test/l1", title="Car l1", price_text="€7.000,-", description="old text")

    repo.update_changed_listing(session, updated)

    assert session.get(ListingRow, "l1").structured_at is None


def test_update_changed_listing_invalidates_every_subscribers_score(session):
    # A price change affects everyone's price-gap component, regardless
    # of whose weights/gate computed it -- see repository.py's docstring.
    _make_row(session, "l1", description="old text")
    _make_user_score(session, CHAT_A, "l1", scored_at=datetime.now(timezone.utc), notified_at=datetime.now(timezone.utc))
    _make_user_score(session, CHAT_B, "l1", scored_at=datetime.now(timezone.utc))
    updated = RawListing(listing_id="l1", url="https://example.test/l1", title="Car l1", price_text="€7.000,-", description="old text")

    repo.update_changed_listing(session, updated)

    assert session.get(UserListingScoreRow, (CHAT_A, "l1")) is None
    assert session.get(UserListingScoreRow, (CHAT_B, "l1")) is None


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


# --- baseline resolution (shared step) ---


def test_get_listings_needing_baseline_only_returns_analyzed_unbaselined(session):
    _make_row(session, "ready", baseline_computed_at=None)
    _make_row(session, "done", baseline_computed_at=datetime.now(timezone.utc))
    _make_row(session, "not_analyzed", analyzed_at=None, baseline_computed_at=None)

    pending = repo.get_listings_needing_baseline(session)

    assert [s.listing_id for s in pending] == ["ready"]


def test_save_baselines_sets_shared_fields_and_marker(session):
    _make_row(session, "l1", baseline_computed_at=None)
    baseline = PriceBaseline(make="bmw", model="320d", median_price_eur=12000, sample_size=8, confidence="high")

    repo.save_baselines(session, {"l1": baseline})

    row = session.get(ListingRow, "l1")
    assert row.baseline_median_price_eur == 12000
    assert row.baseline_sample_size == 8
    assert row.baseline_confidence == "high"
    assert row.baseline_computed_at is not None


# --- per-subscriber scoring ---


def test_get_listings_needing_scoring_is_per_chat(session):
    _make_row(session, "l1")
    _make_user_score(session, CHAT_A, "l1", scored_at=datetime.now(timezone.utc))

    assert repo.get_listings_needing_scoring(session, CHAT_A) == []
    pending_b = repo.get_listings_needing_scoring(session, CHAT_B)
    assert [s.listing_id for s, _, _ in pending_b] == ["l1"]


def test_save_user_scores_then_get_scored_listing_round_trips(session):
    from becarscout.scoring.models import ScoredListing

    _make_row(session, "l1", raw_title="BMW 320d", make="BMW")
    scored = ScoredListing(listing_id="l1", url="https://example.test/l1", raw_title="BMW 320d", score=42, above_threshold=True, reasoning=["good deal"])

    repo.save_user_scores(session, CHAT_A, [scored])

    result = repo.get_scored_listing(session, CHAT_A, "l1")
    assert result is not None
    assert result.score == 42
    assert result.above_threshold is True
    assert repo.get_scored_listing(session, CHAT_B, "l1") is None  # different subscriber, no score yet


def test_get_unnotified_opportunities_only_returns_this_chats_above_threshold(session):
    _make_row(session, "l1")
    _make_user_score(session, CHAT_A, "l1", above_threshold=True, notified_at=None, scored_at=datetime.now(timezone.utc))
    _make_user_score(session, CHAT_B, "l1", above_threshold=False, notified_at=None, scored_at=datetime.now(timezone.utc))

    assert [s.listing_id for s in repo.get_unnotified_opportunities(session, CHAT_A)] == ["l1"]
    assert repo.get_unnotified_opportunities(session, CHAT_B) == []


def test_mark_notified_only_affects_that_chat(session):
    _make_row(session, "l1")
    _make_user_score(session, CHAT_A, "l1", above_threshold=True, scored_at=datetime.now(timezone.utc))
    _make_user_score(session, CHAT_B, "l1", above_threshold=True, scored_at=datetime.now(timezone.utc))

    repo.mark_notified(session, CHAT_A, ["l1"])

    assert session.get(UserListingScoreRow, (CHAT_A, "l1")).notified_at is not None
    assert session.get(UserListingScoreRow, (CHAT_B, "l1")).notified_at is None


def test_reset_scoring_for_rescore_resets_scored_at(session):
    _make_row(session, "l1")
    _make_user_score(session, CHAT_A, "l1", scored_at=datetime.now(timezone.utc))

    count = repo.reset_scoring_for_rescore(session)

    assert count == 1
    assert session.get(UserListingScoreRow, (CHAT_A, "l1")).scored_at is None


def test_reset_scoring_for_rescore_leaves_notified_at_alone(session):
    _make_row(session, "l1")
    _make_user_score(session, CHAT_A, "l1", scored_at=datetime.now(timezone.utc), notified_at=datetime.now(timezone.utc))

    repo.reset_scoring_for_rescore(session)

    assert session.get(UserListingScoreRow, (CHAT_A, "l1")).notified_at is not None


def test_reset_scoring_for_rescore_can_target_one_chat(session):
    _make_row(session, "l1")
    _make_user_score(session, CHAT_A, "l1", scored_at=datetime.now(timezone.utc))
    _make_user_score(session, CHAT_B, "l1", scored_at=datetime.now(timezone.utc))

    count = repo.reset_scoring_for_rescore(session, chat_id=CHAT_A)

    assert count == 1
    assert session.get(UserListingScoreRow, (CHAT_A, "l1")).scored_at is None
    assert session.get(UserListingScoreRow, (CHAT_B, "l1")).scored_at is not None


# --- subscribers ---


def test_ensure_subscriber_is_idempotent(session):
    repo.ensure_subscriber(session, CHAT_A)
    repo.ensure_subscriber(session, CHAT_A)

    assert repo.get_subscribers(session) == [CHAT_A]


def test_get_subscribers_returns_everyone(session):
    repo.ensure_subscriber(session, CHAT_A)
    repo.ensure_subscriber(session, CHAT_B)

    assert set(repo.get_subscribers(session)) == {CHAT_A, CHAT_B}


# --- global scrape radius (shared) ---


def test_get_global_scrape_radius_km_defaults_when_never_set(session):
    assert repo.get_global_scrape_radius_km(session) == 100


def test_update_global_scrape_radius_km_persists(session):
    repo.update_global_scrape_radius_km(session, 150)
    assert repo.get_global_scrape_radius_km(session) == 150


# --- pipeline settings (/budget, /minyear, /threshold, /mileage, /make, /fuel, /transmission) ---


def test_get_pipeline_settings_defaults_when_never_set(session):
    settings = repo.get_pipeline_settings(session, CHAT_A)
    assert settings.min_year == 2010
    assert settings.threshold == 20
    assert settings.min_price is None
    assert settings.max_price is None


def test_update_pipeline_settings_partial_update_preserves_other_fields(session):
    repo.update_pipeline_settings(session, CHAT_A, max_price=15000)
    repo.update_pipeline_settings(session, CHAT_A, threshold=30)

    settings = repo.get_pipeline_settings(session, CHAT_A)
    assert settings.max_price == 15000
    assert settings.threshold == 30
    assert settings.min_price is None  # untouched, still the default


def test_update_pipeline_settings_can_explicitly_disable_min_year(session):
    repo.update_pipeline_settings(session, CHAT_A, min_year=None)
    assert repo.get_pipeline_settings(session, CHAT_A).min_year is None


def test_update_pipeline_settings_budget_range(session):
    repo.update_pipeline_settings(session, CHAT_A, min_price=3000, max_price=12000)
    settings = repo.get_pipeline_settings(session, CHAT_A)
    assert (settings.min_price, settings.max_price) == (3000, 12000)


def test_update_pipeline_settings_new_filters_partial_update(session):
    repo.update_pipeline_settings(session, CHAT_A, max_mileage_km=150_000)
    repo.update_pipeline_settings(session, CHAT_A, makes="bmw,toyota")
    repo.update_pipeline_settings(session, CHAT_A, fuel_types="diesel")
    repo.update_pipeline_settings(session, CHAT_A, transmission="automatic")

    settings = repo.get_pipeline_settings(session, CHAT_A)
    assert settings.max_mileage_km == 150_000
    assert settings.makes == "bmw,toyota"
    assert settings.fuel_types == "diesel"
    assert settings.transmission == "automatic"


def test_update_pipeline_settings_can_clear_new_filters(session):
    repo.update_pipeline_settings(session, CHAT_A, makes="bmw", max_mileage_km=100_000)
    repo.update_pipeline_settings(session, CHAT_A, makes=None, max_mileage_km=None)

    settings = repo.get_pipeline_settings(session, CHAT_A)
    assert settings.makes is None
    assert settings.max_mileage_km is None


def test_pipeline_settings_are_isolated_per_chat(session):
    repo.update_pipeline_settings(session, CHAT_A, threshold=50, makes="bmw")
    repo.update_pipeline_settings(session, CHAT_B, threshold=10)

    settings_a = repo.get_pipeline_settings(session, CHAT_A)
    settings_b = repo.get_pipeline_settings(session, CHAT_B)
    assert settings_a.threshold == 50
    assert settings_a.makes == "bmw"
    assert settings_b.threshold == 10
    assert settings_b.makes is None


# --- scoring weights (/validate) ---


def test_get_scoring_weights_defaults_when_never_set(session):
    weights = repo.get_scoring_weights(session, CHAT_A)
    assert weights.for_export == -15
    assert weights.gearbox_issue_likely_major == -40


def test_update_scoring_weights_partial_update(session):
    repo.update_scoring_weights(session, CHAT_A, warning_light_needs_diagnostic=-30)
    weights = repo.get_scoring_weights(session, CHAT_A)
    assert weights.warning_light_needs_diagnostic == -30
    assert weights.for_export == -15  # untouched


def test_update_scoring_weights_twice_accumulates(session):
    repo.update_scoring_weights(session, CHAT_A, accident_damage=-35)
    repo.update_scoring_weights(session, CHAT_A, for_export=-20)
    weights = repo.get_scoring_weights(session, CHAT_A)
    assert weights.accident_damage == -35
    assert weights.for_export == -20


def test_scoring_weights_are_isolated_per_chat(session):
    repo.update_scoring_weights(session, CHAT_A, for_export=-99)

    assert repo.get_scoring_weights(session, CHAT_A).for_export == -99
    assert repo.get_scoring_weights(session, CHAT_B).for_export == -15  # untouched default


# --- pending review suggestions (/reviewfeedback, /validate) ---


def test_get_pending_suggestions_defaults_to_none(session):
    assert repo.get_pending_suggestions(session, CHAT_A) is None


def test_save_and_get_pending_suggestions(session):
    suggestions = [{"weight": "for_export", "current_value": -15, "new_value": -25, "reason": "test"}]
    repo.save_pending_suggestions(session, CHAT_A, suggestions)

    assert repo.get_pending_suggestions(session, CHAT_A) == suggestions
    assert repo.get_pending_suggestions(session, CHAT_B) is None  # isolated per chat


def test_clear_pending_suggestions(session):
    repo.save_pending_suggestions(session, CHAT_A, [{"weight": "for_export", "current_value": -15, "new_value": -25, "reason": "test"}])
    repo.clear_pending_suggestions(session, CHAT_A)

    assert repo.get_pending_suggestions(session, CHAT_A) is None


# --- search ---


def test_search_listings_matches_make_model_or_title(session):
    _make_row(session, "golf1", raw_title="VW Golf 7 TDI", make="Volkswagen", model_hint="Golf")
    _make_row(session, "bmw1", raw_title="BMW 320d", make="BMW", model_hint="320d")
    _make_row(session, "mystery", raw_title="Nice little golf cart", make=None, model_hint=None)

    hits = repo.search_listings(session, "golf")

    assert {row.listing_id for row in hits} == {"golf1", "mystery"}


def test_search_listings_empty_keyword_matches_everything_structured(session):
    _make_row(session, "a", raw_title="Any car")
    _make_row(session, "unstructured", raw_title="Not ready yet", structured_at=None)

    hits = repo.search_listings(session, "")

    assert [row.listing_id for row in hits] == ["a"]


def test_search_listings_ignores_unstructured_listings(session):
    _make_row(session, "structured", raw_title="Audi A3", make="Audi", model_hint="A3", structured_at=datetime.now(timezone.utc))
    _make_row(session, "unstructured", raw_title="Audi A4", make="Audi", model_hint="A4", structured_at=None)

    hits = repo.search_listings(session, "audi")

    assert [row.listing_id for row in hits] == ["structured"]


def test_search_listings_orders_by_recency(session):
    _make_row(session, "older", raw_title="Opel Corsa older", make="Opel", model_hint="Corsa", scraped_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _make_row(session, "newer", raw_title="Opel Corsa newer", make="Opel", model_hint="Corsa", scraped_at=datetime(2026, 2, 1, tzinfo=timezone.utc))

    hits = repo.search_listings(session, "corsa")

    assert [row.listing_id for row in hits] == ["newer", "older"]


def test_search_listings_since_filters_by_recency(session):
    now = datetime.now(timezone.utc)
    _make_row(session, "old", raw_title="Toyota Yaris old", make="Toyota", scraped_at=now - timedelta(days=10))
    _make_row(session, "recent", raw_title="Toyota Yaris recent", make="Toyota", scraped_at=now - timedelta(hours=1))

    hits = repo.search_listings(session, "yaris", since=now - timedelta(days=1))

    assert [row.listing_id for row in hits] == ["recent"]


def test_search_listings_respects_limit(session):
    for i in range(15):
        _make_row(session, f"seat{i}", raw_title=f"Seat Leon {i}", make="Seat", model_hint="Leon")

    hits = repo.search_listings(session, "leon", limit=5)

    assert len(hits) == 5
