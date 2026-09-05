"""Structured listing data model — stage [2] output in the pipeline (see
Project.md). Only deterministic, auditable normalization happens here: no
LLM, no judgment calls about whether the car is a good deal. Every field
traces back to a specific regex/keyword match against the raw title,
description, or location text — that's stage 3's and 4's job to build on.
"""

from __future__ import annotations

from pydantic import BaseModel


class StructuredListing(BaseModel):
    listing_id: str
    url: str

    price_eur: int | None = None
    is_free: bool = False

    make: str | None = None
    model_hint: str | None = None
    """Best-effort text following the detected make in the title — not a
    validated model name, just a hint for later stages."""
    year: int | None = None
    mileage_km: int | None = None
    fuel_type: str | None = None
    """One of: diesel, petrol, hybrid, electric, lpg."""
    transmission: str | None = None
    """One of: manual, automatic."""

    location_city: str | None = None
    location_region: str | None = None
    """Belgian region code as shown by Facebook: VLG, WAL, or BRU."""

    listing_age_days: int | None = None
    """Approximate — parsed from Facebook's relative time phrase, not exact."""
    photo_count: int = 0

    raw_title: str
    raw_description: str | None = None
