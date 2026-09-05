"""Turns a pool of `Comp`s into a `PriceBaseline` for one target listing.
Deterministic median-based comps analysis — no LLM, no regression (not
enough volume yet for one; see Project.md stage 4 notes).
"""

from __future__ import annotations

import statistics

from .models import Comp, PriceBaseline

# The year cascade widens twice before giving up — but, unlike the mileage
# cascade below, it never drops the year constraint entirely. A model name
# alone can span wildly different eras/trims of "the same car" (e.g. a 1986
# Ford F-150 base trim vs. a modern F-150 Raptor) — comparing across that
# gap just because too few same-era comps turned up produces a meaningless
# median, so the widest tier is the floor: whatever it finds (even 0 or 1)
# is what gets used, never the full unfiltered pool.
_YEAR_WINDOW_TIGHT = 2
_YEAR_WINDOW_WIDE = 4
_YEAR_WINDOW_MAX = 8
_MILEAGE_FRACTION_TIGHT = 0.4  # +/-40% of target mileage
_MILEAGE_FRACTION_WIDE = 0.7

_MIN_SAMPLE_FOR_TRIM = 5
"""Below this, don't trim outliers — there's nothing to spare."""

_MAX_PRICE_RATIO = 5.0
"""If the matched comp pool's highest price is more than this many times
its lowest, the pool almost certainly still mixes incompatible trims/
generations even after year/mileage filtering (e.g. only 2-3 comps
survived the year filter and one of them is a rare high-trim outlier) —
the median from a pool like that isn't trustworthy. Outliers are trimmed
toward this ratio before computing the median, and confidence is forced
down if trimming still can't bring it under the threshold."""


def _filter_by_year(comps: list[Comp], target_year: int, window: int) -> list[Comp]:
    return [c for c in comps if c.year is not None and abs(c.year - target_year) <= window]


def _filter_by_mileage(comps: list[Comp], target_mileage_km: int, fraction: float) -> list[Comp]:
    lo, hi = target_mileage_km * (1 - fraction), target_mileage_km * (1 + fraction)
    return [c for c in comps if c.mileage_km is not None and lo <= c.mileage_km <= hi]


def _median_price(comps: list[Comp]) -> int:
    prices = sorted(c.price_eur for c in comps)
    if len(prices) >= _MIN_SAMPLE_FOR_TRIM:
        prices = prices[1:-1]  # drop the single lowest and highest as light outlier trim
    return round(statistics.median(prices))


def _price_ratio(comps: list[Comp]) -> float:
    prices = [c.price_eur for c in comps if c.price_eur > 0]
    if len(prices) < 2:
        return 1.0
    return max(prices) / min(prices)


def _trim_to_price_ratio(comps: list[Comp], max_ratio: float) -> list[Comp]:
    """Iteratively drops whichever extreme (lowest or highest price) is
    farther from the median, until the pool's price ratio is within bounds
    or there's nothing left worth trimming (2 comps)."""
    remaining = sorted(comps, key=lambda c: c.price_eur)
    while len(remaining) > 2 and _price_ratio(remaining) > max_ratio:
        med = statistics.median(c.price_eur for c in remaining)
        if (med - remaining[0].price_eur) >= (remaining[-1].price_eur - med):
            remaining = remaining[1:]
        else:
            remaining = remaining[:-1]
    return remaining


def _confidence_for(sample_size: int, price_ratio: float) -> str:
    if price_ratio > _MAX_PRICE_RATIO:
        # Even after trimming, this pool spans too wide a price range to
        # trust — cap confidence regardless of how many comps remain.
        return "low" if sample_size >= 1 else "none"
    if sample_size >= 5:
        return "high"
    if sample_size >= 2:
        return "medium"
    if sample_size == 1:
        return "low"
    return "none"


def compute_baseline(
    make: str,
    model: str,
    comps: list[Comp],
    *,
    target_year: int | None = None,
    target_mileage_km: int | None = None,
) -> PriceBaseline:
    matched = comps
    if target_year is not None:
        for window in (_YEAR_WINDOW_TIGHT, _YEAR_WINDOW_WIDE, _YEAR_WINDOW_MAX):
            candidate = _filter_by_year(comps, target_year, window)
            if len(candidate) >= 2:
                matched = candidate
                break
        else:
            # Not even the widest year window found 2+ comps of a
            # plausible era — use whatever that widest window has (0 or 1
            # comps) rather than silently falling back to the full,
            # era-mismatched pool.
            matched = _filter_by_year(comps, target_year, _YEAR_WINDOW_MAX)

    if target_mileage_km is not None:
        tight = _filter_by_mileage(matched, target_mileage_km, _MILEAGE_FRACTION_TIGHT)
        if len(tight) >= 2:
            matched = tight
        else:
            wide = _filter_by_mileage(matched, target_mileage_km, _MILEAGE_FRACTION_WIDE)
            if len(wide) >= 2:
                matched = wide
            # else: keep the year-filtered pool as-is — mileage data on
            # comps is patchy enough that requiring it would too often
            # throw away an otherwise-good year-based match.

    if not matched:
        return PriceBaseline(
            make=make, model=model, target_year=target_year, target_mileage_km=target_mileage_km
        )

    matched = _trim_to_price_ratio(matched, _MAX_PRICE_RATIO)

    return PriceBaseline(
        make=make,
        model=model,
        target_year=target_year,
        target_mileage_km=target_mileage_km,
        median_price_eur=_median_price(matched),
        sample_size=len(matched),
        confidence=_confidence_for(len(matched), _price_ratio(matched)),
    )
