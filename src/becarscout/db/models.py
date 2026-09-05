"""One denormalized row per listing, gaining columns as it moves through
the pipeline stages. A single wide table (rather than one table per
stage) keeps "what still needs processing" a plain `WHERE ... IS NULL`
query — reasonable at personal-project scale (hundreds to low thousands
of rows), and avoids join complexity for no real benefit here.

Multi-user note (added 2026-09-06): scraping/structuring/analyzing stay
shared across everyone — one Facebook scrape, one LLM pass, reused by
every subscriber. So does the market-price *baseline* (comps are the same
regardless of who's asking) — hence `baseline_*` and `baseline_computed_at`
staying here. What genuinely differs per person is the *weights* applied
to condition signals and the *gate* (threshold/year/mileage/brand/fuel/
transmission) — that's why `price_component`/`condition_component`/
`score`/`above_threshold`/`reasoning_json`/`condition_highlights_json`/
`scored_at`/`notified_at` moved to `UserListingScoreRow` below, keyed by
(chat_id, listing_id). The old columns of those names are left in place
on this table (SQLite makes dropping columns painful, and it's harmless)
but nothing reads or writes them anymore — same tolerance this project
already has for the stray leftover `feedback_verdict` column from an
earlier branch (see Project.md's SQLite-corruption incident notes).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text
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

    # Stage 4 (shared half): the market-price baseline is the same for
    # every subscriber, so it's resolved once here rather than per user.
    baseline_median_price_eur: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baseline_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    baseline_confidence: Mapped[str] = mapped_column(String, default="none")
    baseline_computed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    """Gates the shared `resolve_baselines` step the same way `analyzed_at`
    gates stage 3 -- added post-deployment, see `_ensure_column`."""

    # Legacy (pre-multiuser, 2026-09-04 to 2026-09-05): scoring/gate/notify
    # used to be single-user singleton values stored directly on the
    # listing. Superseded by `UserListingScoreRow` -- kept only because
    # SQLite can't cheaply drop columns; no code reads or writes these
    # anymore.
    price_component: Mapped[float] = mapped_column(Float, default=0.0)
    condition_component: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[int] = mapped_column(Integer, default=0)
    above_threshold: Mapped[bool] = mapped_column(Boolean, default=False)
    reasoning_json: Mapped[str] = mapped_column(Text, default="[]")
    condition_highlights_json: Mapped[str] = mapped_column(Text, default="[]")
    scored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SubscriberRow(Base):
    """Anyone who has ever messaged the bot -- the set of chat_ids that
    `do_score`/`do_notify` loop over. Populated by a `group=-1` "middleware"
    handler in `notifier/bot.py` that runs before every other handler, so
    subscribing doesn't require remembering to send `/start` specifically."""

    __tablename__ = "subscribers"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    """What they've told the bot to call them (`/name`) -- added after
    this table's first deployment, see `_ensure_column`. None until they
    answer the name prompt `/start` leads with for a brand-new chat."""


class UserListingScoreRow(Base):
    """The per-subscriber half of scoring a listing -- everything that
    depends on that person's own `ScoringWeightsRow`/`PipelineSettingsRow`
    (condition weights, gate thresholds). Deliberately a separate table
    from `ListingRow` rather than more columns on it: a listing has one
    market baseline but N scores, one per subscriber."""

    __tablename__ = "user_listing_scores"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    listing_id: Mapped[str] = mapped_column(String, primary_key=True)
    price_component: Mapped[float] = mapped_column(Float, default=0.0)
    condition_component: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[int] = mapped_column(Integer, default=0)
    above_threshold: Mapped[bool] = mapped_column(Boolean, default=False)
    reasoning_json: Mapped[str] = mapped_column(Text, default="[]")
    condition_highlights_json: Mapped[str] = mapped_column(Text, default="[]")
    scored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GlobalScrapeSettingsRow(Base):
    """Singleton row (always id=1) holding the *one* parameter that
    genuinely can't be personalized without multiplying scrape cost per
    distinct value: search radius. There's no per-listing distance-from-hub
    figure to filter by afterward, so unlike budget/year/mileage/brand/
    fuel/transmission (all per-subscriber, see `PipelineSettingsRow`),
    radius has to stay one shared value everyone's search uses. `/radius`
    still works from Telegram -- it just changes the shared search area,
    not something personal."""

    __tablename__ = "global_scrape_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    radius_km: Mapped[int] = mapped_column(Integer, default=100)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PipelineSettingsRow(Base):
    """One row per subscriber (`chat_id`), holding the gate parameters
    `/budget`, `/minyear`, `/threshold`, `/mileage`, `/make`, `/fuel`,
    `/transmission` change live. Was a singleton (`id=1`, one set of
    values for everyone) before multi-user support -- `chat_id` was added
    to an already-deployed table via `_ensure_column`, so the pre-existing
    `id=1` row (with `chat_id` left NULL) is simply orphaned going forward;
    `db/engine.py`'s one-time migration copies its values into a real
    subscriber's row so an existing single user doesn't lose their
    settings across the upgrade."""

    __tablename__ = "pipeline_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    radius_km: Mapped[int] = mapped_column(Integer, default=100)
    """Legacy -- radius moved to `GlobalScrapeSettingsRow`. Column kept
    (unused by new code) for the same can't-easily-drop-it-in-SQLite
    reason as `ListingRow`'s legacy scoring columns."""
    min_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_mileage_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    makes: Mapped[str | None] = mapped_column(String, nullable=True)
    fuel_types: Mapped[str | None] = mapped_column(String, nullable=True)
    transmission: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ScoringWeightsRow(Base):
    """One row per subscriber (`chat_id`), holding the condition-signal
    point weights `/validate` can change for that person specifically.
    Was a singleton (`id=1`) before multi-user support -- see
    `PipelineSettingsRow`'s docstring for the same migration story."""

    __tablename__ = "scoring_weights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gearbox_issue_likely_major: Mapped[int] = mapped_column(Integer, default=-40)
    gearbox_issue_minor: Mapped[int] = mapped_column(Integer, default=-20)
    gearbox_issue_unknown: Mapped[int] = mapped_column(Integer, default=-20)
    engine_issue_likely_major: Mapped[int] = mapped_column(Integer, default=-40)
    engine_issue_minor: Mapped[int] = mapped_column(Integer, default=-20)
    engine_issue_unknown: Mapped[int] = mapped_column(Integer, default=-20)
    accident_damage: Mapped[int] = mapped_column(Integer, default=-25)
    warning_light_needs_diagnostic: Mapped[int] = mapped_column(Integer, default=-20)
    warning_light_only: Mapped[int] = mapped_column(Integer, default=-8)
    timing_belt_replaced: Mapped[int] = mapped_column(Integer, default=12)
    inspection_valid: Mapped[int] = mapped_column(Integer, default=6)
    inspection_invalid: Mapped[int] = mapped_column(Integer, default=-18)
    service_history_complete: Mapped[int] = mapped_column(Integer, default=6)
    service_history_none: Mapped[int] = mapped_column(Integer, default=-6)
    for_export: Mapped[int] = mapped_column(Integer, default=-15)
    min_plausible_car_price_eur: Mapped[int] = mapped_column(Integer, default=300)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PendingSuggestionRow(Base):
    """One row per subscriber -- the structured suggestions from their
    *last* `/reviewfeedback` run, applied (and cleared) by their next
    `/validate`. DB-backed rather than the single global JSON file the
    pre-multiuser version used (`data/feedback/latest_suggestions.json`),
    since per-chat state belongs in the same place as everything else
    per-chat now."""

    __tablename__ = "pending_suggestions"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    suggestions_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
