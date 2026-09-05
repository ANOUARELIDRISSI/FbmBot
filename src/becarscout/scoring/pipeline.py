"""Ties stages 2-4 together: for each structured listing, resolves a price
baseline (fetching/caching 2dehands.be comps only when a make was
detected) and joins it with that listing's stage-3 signals, if any.
"""

from __future__ import annotations

import logging

from playwright.async_api import Browser, async_playwright

from becarscout.analyzer.models import DescriptionSignals
from becarscout.pricing.baseline import compute_baseline
from becarscout.pricing.comps_2dehands import get_comps_cached
from becarscout.pricing.models import PriceBaseline
from becarscout.structurer.models import StructuredListing

from .models import DEFAULT_WEIGHTS, ScoredListing, ScoringWeights
from .scoring import score_listing

logger = logging.getLogger(__name__)


def _model_query_token(model_hint: str | None) -> str | None:
    """The structurer's model_hint is "everything after the make in the
    title" (e.g. "Mondeo 2.0 diesel 2013 automatic"), too noisy to search
    on directly — the first token is reliably the actual model name."""
    if not model_hint:
        return None
    tokens = model_hint.split()
    return tokens[0] if tokens else None


async def _resolve_baseline(listing: StructuredListing, browser: Browser) -> PriceBaseline:
    model_query = _model_query_token(listing.model_hint)
    if not listing.make or not model_query:
        return PriceBaseline(make=listing.make or "unknown", model=model_query or "unknown")

    try:
        comps = await get_comps_cached(listing.make, model_query, browser=browser)
    except Exception:
        logger.exception("Comps fetch failed for %s %s (%s) — scoring without a price baseline", listing.make, model_query, listing.url)
        return PriceBaseline(make=listing.make, model=model_query)

    return compute_baseline(
        listing.make,
        model_query,
        comps,
        target_year=listing.year,
        target_mileage_km=listing.mileage_km,
    )


async def score_listings(
    structured_listings: list[StructuredListing],
    signals_by_id: dict[str, DescriptionSignals],
    weights: ScoringWeights = DEFAULT_WEIGHTS,
) -> list[ScoredListing]:
    scored = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for listing in structured_listings:
                baseline = await _resolve_baseline(listing, browser)
                signals = signals_by_id.get(listing.listing_id)
                scored.append(score_listing(listing, signals, baseline, weights))
        finally:
            await browser.close()
    return scored
