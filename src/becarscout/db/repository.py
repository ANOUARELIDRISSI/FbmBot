"""Persistence + the incremental-processing queries that make hourly cron
runs cheap: each `get_listings_needing_*` function returns only rows that
haven't reached that stage yet, so a listing scraped last hour and already
analyzed doesn't get re-sent to Mistral (or re-scraped, re-scored, ...)
on every subsequent run. Converts between the pipeline's pydantic models
(unchanged from stages 1-6) and the DB rows — the DB is purely a
persistence detail, not a change to the actual pipeline logic.

Multi-user note (added 2026-09-06): scrape/structure/analyze/baseline
stay shared queries (one row per listing, `ListingRow`). Score/gate/notify
are per-subscriber (`UserListingScoreRow`, keyed by chat_id + listing_id) --
see `db/models.py`'s module docstring for the reasoning split.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from becarscout.analyzer.models import DescriptionSignals
from becarscout.pricing.models import PriceBaseline
from becarscout.scoring.models import ScoredListing, ScoringWeights
from becarscout.scraper.models import RawListing
from becarscout.settings import DEFAULT_RADIUS_KM, PipelineSettings
from becarscout.structurer.models import StructuredListing

from .models import (
    GlobalScrapeSettingsRow,
    ListingRow,
    PendingSuggestionRow,
    PipelineSettingsRow,
    ScoringWeightsRow,
    SubscriberRow,
    UserListingScoreRow,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_all_listing_ids(session: Session) -> set[str]:
    """Used to tell the scraper which listings it's already seen, so it
    can skip their (expensive) detail-page visit — see `known_ids` on
    `scrape_belgium_cars`."""
    return {row_id for (row_id,) in session.execute(select(ListingRow.listing_id))}


def get_known_price_texts(session: Session) -> dict[str, str]:
    """listing_id -> its stored price_text, for every known listing that
    has one — lets the scraper detect a seller changing the price on an
    already-known listing (comparing against the grid's cheap price
    field) without revisiting every known listing's detail page. See
    `known_price_texts` on `scrape_belgium_cars`."""
    rows = session.execute(
        select(ListingRow.listing_id, ListingRow.price_text).where(ListingRow.price_text.is_not(None))
    ).all()
    return {listing_id: price_text for listing_id, price_text in rows}


def update_changed_listing(session: Session, listing: RawListing) -> None:
    """A known listing whose price changed — refreshes the raw fields
    (price, description, photos — all re-fetched from the same
    detail-page revisit that confirmed the price change) and resets the
    downstream stage timestamps so it flows back through structure/score/
    notify as if freshly scraped. `scraped_at` (first-seen time) is left
    untouched. Analysis (`analyzed_at`) is only reset if the description
    text itself actually changed too — no point re-spending a Mistral
    call on stage 3 when only the price moved and the seller's wording
    didn't. The market baseline (`baseline_computed_at`) is left alone —
    a price change on this one listing doesn't change what similar cars
    sell for — but every subscriber's own score of it is invalidated
    (deleted from `UserListingScoreRow`) since their price-gap component
    depends on this listing's price specifically."""
    row = session.get(ListingRow, listing.listing_id)
    if row is None:
        return
    description_changed = listing.description is not None and listing.description != row.description

    row.price_text = listing.price_text
    row.description = listing.description
    row.photo_urls_json = json.dumps(listing.photo_urls)
    row.listed_relative_text = listing.listed_relative_text
    row.structured_at = None
    if description_changed:
        row.analyzed_at = None

    for user_score in session.execute(
        select(UserListingScoreRow).where(UserListingScoreRow.listing_id == listing.listing_id)
    ).scalars().all():
        session.delete(user_score)

    session.commit()


def upsert_raw_listings(session: Session, listings: list[RawListing]) -> int:
    """Inserts new listings only — a listing already known (by listing_id)
    is left untouched, including everything the later stages computed for
    it. Returns how many were actually new."""
    existing_ids = {
        row_id
        for (row_id,) in session.execute(
            select(ListingRow.listing_id).where(
                ListingRow.listing_id.in_([listing.listing_id for listing in listings])
            )
        )
    }

    new_count = 0
    for listing in listings:
        if listing.listing_id in existing_ids:
            continue
        session.add(
            ListingRow(
                listing_id=listing.listing_id,
                url=listing.url,
                raw_title=listing.title,
                price_text=listing.price_text,
                location_text=listing.location_text,
                listed_relative_text=listing.listed_relative_text,
                description=listing.description,
                photo_urls_json=json.dumps(listing.photo_urls),
                search_hub=listing.search_hub,
                scraped_at=listing.scraped_at,
            )
        )
        new_count += 1
    session.commit()
    return new_count


def get_listings_needing_structuring(session: Session) -> list[RawListing]:
    rows = session.execute(select(ListingRow).where(ListingRow.structured_at.is_(None))).scalars().all()
    return [
        RawListing(
            listing_id=row.listing_id,
            url=row.url,
            title=row.raw_title,
            price_text=row.price_text,
            location_text=row.location_text,
            listed_relative_text=row.listed_relative_text,
            description=row.description,
            photo_urls=json.loads(row.photo_urls_json),
            search_hub=row.search_hub,
            scraped_at=row.scraped_at,
        )
        for row in rows
    ]


def save_structured(session: Session, listings: list[StructuredListing]) -> None:
    for listing in listings:
        row = session.get(ListingRow, listing.listing_id)
        if row is None:
            continue
        row.price_eur = listing.price_eur
        row.is_free = listing.is_free
        row.make = listing.make
        row.model_hint = listing.model_hint
        row.year = listing.year
        row.mileage_km = listing.mileage_km
        row.fuel_type = listing.fuel_type
        row.transmission = listing.transmission
        row.location_city = listing.location_city
        row.location_region = listing.location_region
        row.listing_age_days = listing.listing_age_days
        row.photo_count = listing.photo_count
        row.structured_at = _now()
    session.commit()


def _row_to_structured(row: ListingRow) -> StructuredListing:
    return StructuredListing(
        listing_id=row.listing_id,
        url=row.url,
        price_eur=row.price_eur,
        is_free=row.is_free,
        make=row.make,
        model_hint=row.model_hint,
        year=row.year,
        mileage_km=row.mileage_km,
        fuel_type=row.fuel_type,
        transmission=row.transmission,
        location_city=row.location_city,
        location_region=row.location_region,
        listing_age_days=row.listing_age_days,
        photo_count=row.photo_count,
        raw_title=row.raw_title,
        raw_description=row.description,
    )


def get_listings_needing_analysis(session: Session) -> list[StructuredListing]:
    rows = (
        session.execute(
            select(ListingRow).where(
                ListingRow.structured_at.is_not(None), ListingRow.analyzed_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    return [_row_to_structured(row) for row in rows]


def save_signals(session: Session, signals_list: list[DescriptionSignals]) -> None:
    for signals in signals_list:
        row = session.get(ListingRow, signals.listing_id)
        if row is None:
            continue
        row.signals_json = signals.model_dump_json()
        row.analyzed_at = _now()
    session.commit()


def get_listings_needing_baseline(session: Session) -> list[StructuredListing]:
    """Shared step: every analyzed listing whose market-price baseline
    hasn't been resolved yet — independent of any subscriber, so the
    expensive part (Playwright + comps fetch) happens once per listing no
    matter how many people use the bot."""
    rows = (
        session.execute(
            select(ListingRow).where(
                ListingRow.analyzed_at.is_not(None), ListingRow.baseline_computed_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    return [_row_to_structured(row) for row in rows]


def save_baselines(session: Session, baselines: dict[str, PriceBaseline]) -> None:
    for listing_id, baseline in baselines.items():
        row = session.get(ListingRow, listing_id)
        if row is None:
            continue
        row.baseline_median_price_eur = baseline.median_price_eur
        row.baseline_sample_size = baseline.sample_size
        row.baseline_confidence = baseline.confidence
        row.baseline_computed_at = _now()
    session.commit()


def get_listings_needing_scoring(
    session: Session, chat_id: int
) -> list[tuple[StructuredListing, DescriptionSignals | None, PriceBaseline]]:
    """Per-subscriber: every listing with a resolved baseline that this
    chat_id hasn't scored yet (never scored, or reset by `/rescore`)."""
    already_scored_ids = {
        listing_id
        for (listing_id,) in session.execute(
            select(UserListingScoreRow.listing_id).where(
                UserListingScoreRow.chat_id == chat_id, UserListingScoreRow.scored_at.is_not(None)
            )
        )
    }
    rows = (
        session.execute(
            select(ListingRow).where(
                ListingRow.analyzed_at.is_not(None), ListingRow.baseline_computed_at.is_not(None)
            )
        )
        .scalars()
        .all()
    )
    result = []
    for row in rows:
        if row.listing_id in already_scored_ids:
            continue
        structured = _row_to_structured(row)
        signals = DescriptionSignals.model_validate_json(row.signals_json) if row.signals_json else None
        baseline = PriceBaseline(
            make=row.make or "unknown",
            model=row.model_hint or "unknown",
            target_year=row.year,
            target_mileage_km=row.mileage_km,
            median_price_eur=row.baseline_median_price_eur,
            sample_size=row.baseline_sample_size,
            confidence=row.baseline_confidence,
        )
        result.append((structured, signals, baseline))
    return result


def save_user_scores(session: Session, chat_id: int, scored_list: list[ScoredListing]) -> None:
    for scored in scored_list:
        row = session.get(UserListingScoreRow, (chat_id, scored.listing_id))
        if row is None:
            row = UserListingScoreRow(chat_id=chat_id, listing_id=scored.listing_id)
            session.add(row)
        row.price_component = scored.price_component
        row.condition_component = scored.condition_component
        row.score = scored.score
        row.above_threshold = scored.above_threshold
        row.reasoning_json = json.dumps(scored.reasoning)
        row.condition_highlights_json = json.dumps(scored.condition_highlights)
        row.scored_at = _now()
    session.commit()


def _row_to_scored(row: ListingRow, user_row: UserListingScoreRow) -> ScoredListing:
    return ScoredListing(
        listing_id=row.listing_id,
        url=row.url,
        raw_title=row.raw_title,
        price_eur=row.price_eur,
        make=row.make,
        model_hint=row.model_hint,
        year=row.year,
        mileage_km=row.mileage_km,
        fuel_type=row.fuel_type,
        transmission=row.transmission,
        condition_highlights=json.loads(user_row.condition_highlights_json) if user_row.condition_highlights_json else [],
        baseline_median_price_eur=row.baseline_median_price_eur,
        baseline_sample_size=row.baseline_sample_size,
        baseline_confidence=row.baseline_confidence,
        price_component=user_row.price_component,
        condition_component=user_row.condition_component,
        score=user_row.score,
        above_threshold=user_row.above_threshold,
        reasoning=json.loads(user_row.reasoning_json) if user_row.reasoning_json else [],
    )


def get_scored_listing(session: Session, chat_id: int, listing_id: str) -> ScoredListing | None:
    """Looks up one listing's full scored context for one subscriber —
    used by the feedback agent when a 👍/👎 comes in (Telegram's
    callback_data only carries the bare listing_id) and by the "Why?"
    button. Returns None if either the listing or that chat's score for
    it doesn't exist."""
    row = session.get(ListingRow, listing_id)
    if row is None:
        return None
    user_row = session.get(UserListingScoreRow, (chat_id, listing_id))
    if user_row is None:
        return None
    return _row_to_scored(row, user_row)


def get_user_scores_by_listing_ids(session: Session, chat_id: int, listing_ids: list[str]) -> dict[str, ScoredListing]:
    """Batch version of `get_scored_listing` for a known set of ids — used
    by `/search` to show each hit's score for the requesting chat without
    a query per row."""
    if not listing_ids:
        return {}
    listings_by_id = {
        row.listing_id: row
        for row in session.execute(select(ListingRow).where(ListingRow.listing_id.in_(listing_ids))).scalars().all()
    }
    user_rows = session.execute(
        select(UserListingScoreRow).where(
            UserListingScoreRow.chat_id == chat_id, UserListingScoreRow.listing_id.in_(listing_ids)
        )
    ).scalars().all()
    return {
        user_row.listing_id: _row_to_scored(listings_by_id[user_row.listing_id], user_row)
        for user_row in user_rows
        if user_row.listing_id in listings_by_id
    }


def get_recent_scored_listings(session: Session, chat_id: int, limit: int = 20) -> list[ScoredListing]:
    """This subscriber's most recently scored listings, newest first --
    backs Telegram's `/showall` review flow (see `notifier/bot.py`),
    where a person walks through their own recent scores and corrects
    ones the engine got wrong."""
    user_rows = (
        session.execute(
            select(UserListingScoreRow)
            .where(UserListingScoreRow.chat_id == chat_id, UserListingScoreRow.scored_at.is_not(None))
            .order_by(UserListingScoreRow.scored_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    result = []
    for user_row in user_rows:
        listing_row = session.get(ListingRow, user_row.listing_id)
        if listing_row is not None:
            result.append(_row_to_scored(listing_row, user_row))
    return result


def get_unnotified_opportunities(session: Session, chat_id: int) -> list[ScoredListing]:
    user_rows = (
        session.execute(
            select(UserListingScoreRow).where(
                UserListingScoreRow.chat_id == chat_id,
                UserListingScoreRow.above_threshold.is_(True),
                UserListingScoreRow.notified_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = []
    for user_row in user_rows:
        listing_row = session.get(ListingRow, user_row.listing_id)
        if listing_row is not None:
            result.append(_row_to_scored(listing_row, user_row))
    return result


def mark_notified(session: Session, chat_id: int, listing_ids: list[str]) -> None:
    for listing_id in listing_ids:
        row = session.get(UserListingScoreRow, (chat_id, listing_id))
        if row is not None:
            row.notified_at = _now()
    session.commit()


def reset_scoring_for_rescore(session: Session, chat_id: int | None = None) -> int:
    """Nulls `scored_at` on `UserListingScoreRow` so `becarscout score`
    picks those listings up again with whatever the *current* code
    computes — incremental processing otherwise means a fix to
    scoring.py/baseline.py/analyzer's schema only ever affects listings
    scored *after* the fix (see Project.md's Infrastructure notes — found
    live via the 1986 Ford F-150 case). `notified_at` is left untouched on
    purpose, so this won't cause a re-notification for something already
    sent. `chat_id=None` (the CLI default) resets every subscriber."""
    query = select(UserListingScoreRow)
    if chat_id is not None:
        query = query.where(UserListingScoreRow.chat_id == chat_id)
    rows = session.execute(query).scalars().all()
    for row in rows:
        row.scored_at = None
    session.commit()
    return len(rows)


def search_listings(
    session: Session, keyword: str, limit: int = 10, since: datetime | None = None
) -> list[ListingRow]:
    """Backs Telegram's `/search` — a plain keyword match against make,
    model, and the raw scraped title, over listings already structured
    (so make/model exist to search at all), optionally restricted to
    listings first seen since a given time (the "today"/"3 days"/"week"
    quick-pick ranges). An empty keyword matches everything, so `/search`
    with only a time range (no keyword) works as "show me what's new"."""
    conditions = [ListingRow.structured_at.is_not(None)]
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                ListingRow.make.ilike(pattern),
                ListingRow.model_hint.ilike(pattern),
                ListingRow.raw_title.ilike(pattern),
            )
        )
    if since is not None:
        conditions.append(ListingRow.scraped_at >= since)
    return (
        session.execute(
            select(ListingRow).where(*conditions).order_by(ListingRow.scraped_at.desc()).limit(limit)
        )
        .scalars()
        .all()
    )


def get_subscribers(session: Session) -> list[int]:
    return [chat_id for (chat_id,) in session.execute(select(SubscriberRow.chat_id))]


def ensure_subscriber(session: Session, chat_id: int) -> None:
    """Registers a chat as a subscriber the first time it's seen — called
    from a `group=-1` "middleware" handler in `notifier/bot.py` on every
    update, so subscribing doesn't depend on remembering to send /start
    specifically. A no-op for an already-known chat_id."""
    if session.get(SubscriberRow, chat_id) is None:
        session.add(SubscriberRow(chat_id=chat_id, created_at=_now()))
        session.commit()


def get_subscriber_name(session: Session, chat_id: int) -> str | None:
    """What this subscriber has told the bot to call them (`/name`) —
    None if they've never set one (a brand-new chat is asked for it
    before anything else, see `/start`)."""
    row = session.get(SubscriberRow, chat_id)
    return row.name if row is not None else None


def set_subscriber_name(session: Session, chat_id: int, name: str) -> None:
    row = session.get(SubscriberRow, chat_id)
    if row is None:
        row = SubscriberRow(chat_id=chat_id, created_at=_now())
        session.add(row)
    row.name = name
    session.commit()


def get_global_scrape_radius_km(session: Session) -> int:
    row = session.get(GlobalScrapeSettingsRow, 1)
    return row.radius_km if row is not None else DEFAULT_RADIUS_KM


def update_global_scrape_radius_km(session: Session, radius_km: int) -> int:
    row = session.get(GlobalScrapeSettingsRow, 1)
    if row is None:
        row = GlobalScrapeSettingsRow(id=1, radius_km=radius_km)
        session.add(row)
    else:
        row.radius_km = radius_km
    row.updated_at = _now()
    session.commit()
    return row.radius_km


def get_pipeline_settings(session: Session, chat_id: int) -> PipelineSettings:
    """This subscriber's current gate parameters — `becarscout run`
    reads this per subscriber at score time and uses it as the default
    for any parameter not explicitly overridden by a CLI flag. No row yet
    (nothing has ever been changed via Telegram) just means the hardcoded
    defaults in `settings.py`."""
    row = session.execute(select(PipelineSettingsRow).where(PipelineSettingsRow.chat_id == chat_id)).scalar_one_or_none()
    if row is None:
        return PipelineSettings()
    return PipelineSettings(
        min_price=row.min_price,
        max_price=row.max_price,
        min_year=row.min_year,
        threshold=row.threshold if row.threshold is not None else PipelineSettings().threshold,
        max_mileage_km=row.max_mileage_km,
        makes=row.makes,
        fuel_types=row.fuel_types,
        transmission=row.transmission,
    )


def update_pipeline_settings(session: Session, chat_id: int, **changes: object) -> PipelineSettings:
    """Partial update for one subscriber — only the given fields change;
    anything not passed keeps its current stored value (or the hardcoded
    default, on the very first change ever made for this chat_id).
    `min_year=None` is a valid, meaningful change (disables the year
    cutoff entirely — see `scoring/gate.py`), not "leave unset"; only keys
    actually present in `changes` are touched. `radius_km` is not a valid
    key here anymore — see `update_global_scrape_radius_km`."""
    row = session.execute(select(PipelineSettingsRow).where(PipelineSettingsRow.chat_id == chat_id)).scalar_one_or_none()
    if row is None:
        current = PipelineSettings()
        row = PipelineSettingsRow(
            chat_id=chat_id,
            min_price=current.min_price,
            max_price=current.max_price,
            min_year=current.min_year,
            threshold=current.threshold,
            max_mileage_km=current.max_mileage_km,
            makes=current.makes,
            fuel_types=current.fuel_types,
            transmission=current.transmission,
        )
        session.add(row)
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_at = _now()
    session.commit()
    return get_pipeline_settings(session, chat_id)


def get_scoring_weights(session: Session, chat_id: int) -> ScoringWeights:
    """This subscriber's current condition-signal point weights. No row
    yet means nothing has ever been changed via `/validate` for this
    chat_id — the hardcoded original values from `scoring/models.py`'s
    `ScoringWeights` defaults."""
    row = session.execute(select(ScoringWeightsRow).where(ScoringWeightsRow.chat_id == chat_id)).scalar_one_or_none()
    if row is None:
        return ScoringWeights()
    return ScoringWeights(**{name: getattr(row, name) for name in ScoringWeights.model_fields})


def update_scoring_weights(session: Session, chat_id: int, **changes: int) -> ScoringWeights:
    """Partial update, same pattern as `update_pipeline_settings` —
    backs this subscriber's `/validate` applying their own approved
    feedback suggestion. Only the named weights change; everything else
    keeps its current value."""
    row = session.execute(select(ScoringWeightsRow).where(ScoringWeightsRow.chat_id == chat_id)).scalar_one_or_none()
    if row is None:
        current = ScoringWeights()
        row = ScoringWeightsRow(chat_id=chat_id, **current.model_dump())
        session.add(row)
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_at = _now()
    session.commit()
    return get_scoring_weights(session, chat_id)


def get_pending_suggestions(session: Session, chat_id: int) -> list[dict] | None:
    """This subscriber's last `/reviewfeedback` suggestions, waiting on a
    `/validate` — None if there's nothing pending (never reviewed, or
    already applied/cleared)."""
    row = session.get(PendingSuggestionRow, chat_id)
    if row is None:
        return None
    return json.loads(row.suggestions_json)


def save_pending_suggestions(session: Session, chat_id: int, suggestions: list[dict]) -> None:
    row = session.get(PendingSuggestionRow, chat_id)
    if row is None:
        row = PendingSuggestionRow(chat_id=chat_id, suggestions_json="[]", created_at=_now())
        session.add(row)
    row.suggestions_json = json.dumps(suggestions)
    row.created_at = _now()
    session.commit()


def clear_pending_suggestions(session: Session, chat_id: int) -> None:
    row = session.get(PendingSuggestionRow, chat_id)
    if row is not None:
        session.delete(row)
        session.commit()
