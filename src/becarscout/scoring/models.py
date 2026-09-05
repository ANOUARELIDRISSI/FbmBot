"""Stage [4]/[5] output — the final opportunity score plus its full
reasoning trail. Every point added or subtracted here traces back to a
specific field from stages 2-4's price baseline, per Project.md's design
principle that nothing gets flagged without an explanation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScoredListing(BaseModel):
    listing_id: str
    url: str
    raw_title: str

    price_eur: int | None = None
    make: str | None = None
    model_hint: str | None = None
    year: int | None = None
    mileage_km: int | None = None
    fuel_type: str | None = None
    transmission: str | None = None

    condition_highlights: list[str] = Field(default_factory=list)
    """Plain-language, no-numbers versions of stage 3's condition flags
    (e.g. "Gearbox needs attention") — what the default Telegram card
    shows. `reasoning` below (with point deltas) is reserved for the
    on-demand "Why this score?" explanation."""

    baseline_median_price_eur: int | None = None
    baseline_sample_size: int = 0
    baseline_confidence: str = "none"

    price_component: float = 0.0
    condition_component: float = 0.0
    score: int = 0
    above_threshold: bool = False

    reasoning: list[str] = Field(default_factory=list)


class ScoringWeights(BaseModel):
    """The condition-signal point weights `scoring.py` applies — used to
    be hardcoded module constants; moved into a plain, DB-backable model
    so `/validate` (see `feedback_agent/`) can actually change scoring
    behavior at runtime from an approved feedback suggestion, not just
    print one in a report for manual hand-editing. Every default here is
    exactly what the original hardcoded constant was — constructing this
    with no arguments reproduces the original, un-tuned behavior."""

    gearbox_issue_likely_major: int = -40
    gearbox_issue_minor: int = -20
    gearbox_issue_unknown: int = -20
    engine_issue_likely_major: int = -40
    engine_issue_minor: int = -20
    engine_issue_unknown: int = -20
    accident_damage: int = -25
    warning_light_needs_diagnostic: int = -20
    warning_light_only: int = -8
    timing_belt_replaced: int = 12
    inspection_valid: int = 6
    inspection_invalid: int = -18
    service_history_complete: int = 6
    service_history_none: int = -6
    for_export: int = -15
    min_plausible_car_price_eur: int = 300


DEFAULT_WEIGHTS = ScoringWeights()
