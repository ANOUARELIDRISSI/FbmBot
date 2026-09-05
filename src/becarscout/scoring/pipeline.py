"""Ties stages 2-4 together: for each structured listing, resolves a price
baseline (fetching/caching 2dehands.be comps only when a make was
detected) and joins it with that listing's stage-3 signals, if any.
"""

from __future__ import annotations

import logging

from playwright.async_api import Browser, async_playwright

from becarscout.analyzer.models import DescriptionSignals
from becarscout.pricing.baseline import compute_baseline
from becarscout.pricing.comps_2dehands import check_comps_source_health, get_comps_cached
from becarscout.pricing.models import PriceBaseline
from becarscout.structurer.makes import MAKES
from becarscout.structurer.models import StructuredListing

from .models import ScoredListing
from .scoring import score_listing

logger = logging.getLogger(__name__)

# A handful of very common abbreviations sellers actually use that won't
# exact-match the curated MAKES list (see `_canonicalize_make`) — kept
# short and deliberately conservative rather than a general fuzzy-match,
# since a wrong canonicalization would corrupt the 2dehands cache key.
_MAKE_ALIASES = {"vw": "Volkswagen", "mb": "Mercedes"}

# Model names that are genuinely multi-word — tried as a whole phrase
# before falling back to the first-word heuristic below, so a comps search
# for e.g. "Jeep Grand Cherokee" doesn't end up querying 2dehands for just
# "Jeep Grand" (a real accuracy gap flagged in Project.md/next_version.md).
# Deliberately a short, curated list (not exhaustive) rather than a full
# make->model catalogue, which stage 2 explicitly avoids maintaining.
_MULTI_WORD_MODELS = sorted(
    [
        "Grand Cherokee", "Range Rover", "Land Cruiser", "Grand Vitara",
        "Model 3", "Model S", "Model X", "Model Y",
        "C-Class", "E-Class", "A-Class", "S-Class", "M-Class", "G-Class",
        "Golf GTI", "Golf GTD", "Polo GTI", "Civic Type R",
    ],
    key=len,
    reverse=True,
)


def _canonicalize_make(make_text: str | None) -> str | None:
    """Matches free-text make (from the LLM, stage 3) against the curated
    `MAKES` catalogue stage 2 already uses, so a fallback lookup lands on
    the same 2dehands cache key stage 2 would have used (e.g. "vw" and
    "Volkswagen" must not become two different cache entries). Exact
    match only, plus a tiny alias table — not fuzzy matching, since a
    wrong canonicalization silently corrupts comps for that make."""
    if not make_text:
        return None
    lowered = make_text.strip().lower()
    if lowered in _MAKE_ALIASES:
        return _MAKE_ALIASES[lowered]
    for make in MAKES:
        if make.lower() == lowered:
            return make
    return None


def _model_query_from_hint(model_hint: str | None) -> str | None:
    """The structurer's model_hint is "everything after the make in the
    title" (e.g. "Grand Cherokee 3.0 CRD 2013 automatic"), too noisy to
    search on directly. Tries the curated multi-word names first (longest
    first), then falls back to just the first token."""
    if not model_hint:
        return None
    normalized = model_hint.strip()
    lowered = normalized.lower()
    for candidate in _MULTI_WORD_MODELS:
        if lowered.startswith(candidate.lower()):
            return candidate
    tokens = normalized.split()
    return tokens[0] if tokens else None


def resolve_make_and_model_query(
    listing: StructuredListing, signals: DescriptionSignals | None
) -> tuple[str | None, str | None]:
    """Make/model to actually search 2dehands.be with. Stage 2's
    regex-based detection is preferred (it's what `listing.make`/
    `model_hint` already are); when it found nothing, falls back to
    stage 3's LLM extraction — which sees the same title/description but
    handles abbreviations, non-English spellings, and multi-word model
    names stage 2's fixed keyword list and first-token heuristic can't.
    This is the fix for ~64% of listings having no baseline at all
    (Project.md's stage 4 known limitation)."""
    make = listing.make or _canonicalize_make(signals.make if signals else None)

    model_query = _model_query_from_hint(listing.model_hint)
    if not model_query and signals and signals.model:
        model_query = signals.model.strip() or None

    return make, model_query


async def _resolve_baseline(
    listing: StructuredListing, signals: DescriptionSignals | None, browser: Browser
) -> PriceBaseline:
    make, model_query = resolve_make_and_model_query(listing, signals)
    if not make or not model_query:
        return PriceBaseline(make=make or "unknown", model=model_query or "unknown")

    try:
        comps = await get_comps_cached(make, model_query, browser=browser)
    except Exception:
        logger.exception("Comps fetch failed for %s %s (%s) — scoring without a price baseline", make, model_query, listing.url)
        return PriceBaseline(make=make, model=model_query)

    return compute_baseline(
        make,
        model_query,
        comps,
        target_year=listing.year,
        target_mileage_km=listing.mileage_km,
    )


async def score_listings(
    structured_listings: list[StructuredListing],
    signals_by_id: dict[str, DescriptionSignals],
) -> list[ScoredListing]:
    scored = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            if structured_listings:
                # One cheap extra request per batch against a make/model
                # that's always heavily listed in Belgium — if 2dehands.be
                # ever returns almost nothing for it, their markup broke
                # (our selectors stopped matching), not that Golfs stopped
                # being sold. Bypasses the cache deliberately, so a real
                # break is caught the run it happens instead of being
                # masked for up to 7 days by a stale cache hit.
                healthy = await check_comps_source_health(browser=browser)
                if not healthy:
                    logger.error(
                        "2dehands.be comps source looks unhealthy this run — price "
                        "baselines below may be missing or wrong for every listing."
                    )

            for listing in structured_listings:
                signals = signals_by_id.get(listing.listing_id)
                baseline = await _resolve_baseline(listing, signals, browser)
                scored.append(score_listing(listing, signals, baseline))
        finally:
            await browser.close()
    return scored
