"""Stage [3] output — signals extracted from a listing's free-text
description by an LLM. Per Project.md's design principle, the LLM's only
job is translation from unstructured text to structured facts (plus a
rough severity/confidence estimate); it never judges whether the car is a
good deal — that stays with the deterministic Scoring Engine (stage 4).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DescriptionSignalFields(BaseModel):
    """Exactly what the LLM is asked to fill in — kept separate from
    `DescriptionSignals` because `listing_id` is ours to set, never the
    model's. See `schema.py` for the matching JSON schema sent to the API;
    keep the two in sync if fields change here."""

    language: Literal["nl", "fr", "en", "mixed", "other"] | None = None

    warning_light: bool = False
    warning_light_severity: Literal["minor", "major", "unknown"] | None = None
    needs_diagnostic: bool = False

    gearbox_issue: bool = False
    gearbox_issue_severity: Literal["minor", "likely_major", "unknown"] | None = None

    engine_issue: bool = False
    engine_issue_severity: Literal["minor", "likely_major", "unknown"] | None = None

    accident_damage: bool = False
    timing_belt_replaced: bool = False

    is_whole_vehicle: bool = True
    """False when the listing is clearly not a whole vehicle for sale — e.g.
    seats/wheels/an engine/other parts pulled from a car, with no vehicle
    actually included. A whole car explicitly sold "for parts" (non-running,
    but the actual vehicle is what's being sold) still counts as True; this
    is specifically for listings where no vehicle is being sold at all. Real
    case that motivated this field: a listing titled "2006 Subaru outback"
    whose description was "Seats for 2006-2008 Subaru outback... 300 for
    all" — nothing about the title alone flagged it as non-car."""

    inspection_valid: bool | None = None
    """Belgian roadworthiness inspection (keuring / contrôle technique)."""
    service_history: Literal["complete", "partial", "none", "unknown"] = "unknown"
    vat_scheme: Literal["normal", "margin", "unknown"] = "unknown"
    for_export: bool = False

    fuel_type: Literal["diesel", "petrol", "hybrid", "electric", "lpg"] | None = None
    transmission: Literal["manual", "automatic"] | None = None
    mileage_km: int | None = None
    """Only set if explicitly mentioned in the description — cross-checks
    stage 2's regex-based extraction rather than replacing it."""

    confidence: Literal["high", "medium", "low"] = "medium"
    """Confidence in how complete/clear the description was to extract
    from — not a judgment of the car itself."""


class DescriptionSignals(DescriptionSignalFields):
    listing_id: str
    extraction_failed: bool = False
    """True if the LLM call/parse failed and this is an all-defaults
    fallback — lets downstream stages tell "nothing to report" apart from
    "we couldn't ask"."""
