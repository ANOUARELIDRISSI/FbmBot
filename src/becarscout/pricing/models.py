"""Price comps and baseline models for stage [4]'s price-gap input. Comps
come from 2dehands.be search results (see `comps_2dehands.py`) — a second,
much smaller scrape than the Facebook one, used only to establish what a
given make/model/year/mileage combination typically sells for in Belgium
right now.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class Comp(BaseModel):
    """A single comparable listing scraped from 2dehands.be."""

    title: str
    url: str
    price_eur: int
    year: int | None = None
    mileage_km: int | None = None
    fuel_type: str | None = None
    transmission: str | None = None


class PriceBaseline(BaseModel):
    make: str
    model: str
    target_year: int | None = None
    target_mileage_km: int | None = None

    median_price_eur: int | None = None
    sample_size: int = 0
    confidence: Literal["high", "medium", "low", "none"] = "none"
    """Based on how many comps matched after year/mileage filtering — not
    a judgment of the target listing itself."""

    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
