"""One denormalized row per listing, gaining columns as it moves through
the pipeline stages. A single wide table (rather than one table per
stage) keeps "what still needs processing" a plain `WHERE ... IS NULL`
query — reasonable at personal-project scale (hundreds to low thousands
of rows), and avoids join complexity for no real benefit here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ListingRow(Base):
    __tablename__ = "listings"

    listing_id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String, nullable=False)

    # Stage 1: scraper
    raw_title: Mapped[str] = mapped_column(String, nullable=False)
    price_text: Mapped[str | None] = mapped_column(String, nullable=True)
    location_text: Mapped[str | None] = mapped_column(String, nullable=True)
    listed_relative_text: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    search_hub: Mapped[str | None] = mapped_column(String, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Stage 2: structurer
    price_eur: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_free: Mapped[bool] = mapped_column(Boolean, default=False)
    make: Mapped[str | None] = mapped_column(String, nullable=True)
    model_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mileage_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fuel_type: Mapped[str | None] = mapped_column(String, nullable=True)
    transmission: Mapped[str | None] = mapped_column(String, nullable=True)
    location_city: Mapped[str | None] = mapped_column(String, nullable=True)
    location_region: Mapped[str | None] = mapped_column(String, nullable=True)
    listing_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    photo_count: Mapped[int] = mapped_column(Integer, default=0)
    structured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Stage 3: LLM analyzer — stored as a JSON blob (many small fields,
    # simplest to round-trip through DescriptionSignals as-is)
    signals_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Stage 4/5: scoring + decision gate
    baseline_median_price_eur: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baseline_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    baseline_confidence: Mapped[str] = mapped_column(String, default="none")
    price_component: Mapped[float] = mapped_column(Float, default=0.0)
    condition_component: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[int] = mapped_column(Integer, default=0)
    above_threshold: Mapped[bool] = mapped_column(Boolean, default=False)
    reasoning_json: Mapped[str] = mapped_column(Text, default="[]")
    condition_highlights_json: Mapped[str] = mapped_column(Text, default="[]")
    """Plain-language versions of `reasoning`'s condition flags, no point
    deltas — added after `reasoning_json` (see `db/engine.py`'s
    `_ensure_column` for how this column gets added to an already-deployed
    DB, since a fresh `create_all` alone wouldn't touch an existing table)."""
    scored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Stage 6: Telegram delivery
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    feedback_verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    """'up'/'down' once you tap a button on the card, else None. Kept
    alongside the flat feedback.jsonl audit trail and mem0's memory store
    — this is what lets /history and /missed tell reviewed from
    unreviewed opportunities without cross-referencing a separate file."""
