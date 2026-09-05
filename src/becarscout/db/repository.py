"""Persistence + the incremental-processing queries that make hourly cron
runs cheap: each `get_listings_needing_*` function returns only rows that
haven't reached that stage yet, so a listing scraped last hour and already
analyzed doesn't get re-sent to Mistral (or re-scraped, re-scored, ...)
on every subsequent run. Converts between the pipeline's pydantic models
(unchanged from stages 1-6) and `ListingRow` — the DB is purely a
persistence detail, not a change to the actual pipeline logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from becarscout.analyzer.models import DescriptionSignals
from becarscout.scoring.models import ScoredListing
from becarscout.scraper.models import RawListing
from becarscout.structurer.models import StructuredListing

from .models import ListingRow


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_all_listing_ids(session: Session) -> set[str]:
    """Used to tell the scraper which listings it's already seen, so it
    can skip their (expensive) detail-page visit — see `known_ids` on
    `scrape_belgium_cars`."""
    return {row_id for (row_id,) in session.execute(select(ListingRow.listing_id))}


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


def get_listings_needing_scoring(
    session: Session,
) -> list[tuple[StructuredListing, DescriptionSignals | None]]:
    rows = (
        session.execute(
            select(ListingRow).where(
                ListingRow.analyzed_at.is_not(None), ListingRow.scored_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    result = []
    for row in rows:
        structured = _row_to_structured(row)
        signals = DescriptionSignals.model_validate_json(row.signals_json) if row.signals_json else None
        result.append((structured, signals))
    return result


def save_scores(session: Session, scored_list: list[ScoredListing]) -> None:
    for scored in scored_list:
        row = session.get(ListingRow, scored.listing_id)
        if row is None:
            continue
        row.baseline_median_price_eur = scored.baseline_median_price_eur
        row.baseline_sample_size = scored.baseline_sample_size
        row.baseline_confidence = scored.baseline_confidence
        row.price_component = scored.price_component
        row.condition_component = scored.condition_component
        row.score = scored.score
        row.above_threshold = scored.above_threshold
        row.reasoning_json = json.dumps(scored.reasoning)
        row.scored_at = _now()
    session.commit()


def _row_to_scored(row: ListingRow) -> ScoredListing:
    return ScoredListing(
        listing_id=row.listing_id,
        url=row.url,
        raw_title=row.raw_title,
        price_eur=row.price_eur,
        make=row.make,
        model_hint=row.model_hint,
        year=row.year,
        mileage_km=row.mileage_km,
        baseline_median_price_eur=row.baseline_median_price_eur,
        baseline_sample_size=row.baseline_sample_size,
        baseline_confidence=row.baseline_confidence,
        price_component=row.price_component,
        condition_component=row.condition_component,
        score=row.score,
        above_threshold=row.above_threshold,
        reasoning=json.loads(row.reasoning_json),
    )


def get_scored_listing(session: Session, listing_id: str) -> ScoredListing | None:
    """Looks up one listing's full scored context by id — used by the
    feedback agent when a 👍/👎 comes in, since Telegram's callback_data
    only carries the bare listing_id, not the make/year/price/score that
    the memory description needs."""
    row = session.get(ListingRow, listing_id)
    if row is None:
        return None
    return _row_to_scored(row)


def get_unnotified_opportunities(session: Session) -> list[ScoredListing]:
    rows = (
        session.execute(
            select(ListingRow).where(
                ListingRow.above_threshold.is_(True), ListingRow.notified_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    return [_row_to_scored(row) for row in rows]


def mark_notified(session: Session, listing_ids: list[str]) -> None:
    for listing_id in listing_ids:
        row = session.get(ListingRow, listing_id)
        if row is not None:
            row.notified_at = _now()
    session.commit()
