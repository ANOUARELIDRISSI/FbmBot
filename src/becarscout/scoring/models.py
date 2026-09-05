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
