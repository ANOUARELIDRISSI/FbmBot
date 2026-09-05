"""Turns a pool of `Comp`s into a `PriceBaseline` for one target listing.
Deterministic median-based comps analysis — no LLM, no black-box ML model
(see Project.md's design principle: every score traces back to an
explainable number, never an opaque model's output). Two statistically-
grounded techniques were added after researching what a proper
pricing/anomaly-detection pipeline would actually use (see Project.md's
comps-matching hardening notes): a robust outlier detector (median
absolute deviation, not a fixed ratio) and empirical-Bayes-style shrinkage
of a thin local comps pool toward a broader market prior. Both are simple,
well-established formulas — not opaque models — so the reasoning trail
stays fully auditable; a full ML anomaly ensemble (e.g. Isolation Forest)
was considered and rejected for this reason, not for lack of accuracy.
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

_MAD_OUTLIER_THRESHOLD = 3.5
"""A modified z-score (median-absolute-deviation based, not mean/stdev
based) beyond this is the conventional threshold for "probable outlier"
(Iglewicz & Hoaglin, 1993) — far less sensitive to the very outliers it's
detecting than a plain z-score would be, since MAD itself isn't dragged
around by extreme values the way standard deviation is. Needs at least 3
points to compute a stable MAD; below that, `_MAX_PRICE_RATIO` below is
the fallback guard (see `_trim_mad_outliers`)."""

_MAX_PRICE_RATIO = 5.0
"""Fallback guard for pools too small (<3) to compute a stable MAD from:
if the pool's highest price is still more than this many times its
lowest, confidence is capped regardless of n (see `_confidence_for`)."""

_SHRINKAGE_PRIOR_WEIGHT = 3
"""Empirical-Bayes-style shrinkage constant: a thin local comps pool (few
matches surviving year/mileage filtering) gets its median pulled toward a
broader prior — this make/model's full comp pool, before that filtering —
proportional to how little local evidence there is (n / (n + k) linear
blending). At n=k local comps the estimate is a 50/50 blend; well above k,
the prior's influence fades toward zero. Motivated by a real case: a 2006
Subaru Outback matched only 2 comps after filtering, and their raw median
was taken as gospel with nothing to check it against — shrinking a 2-comp
estimate toward the wider Subaru Outback market is more honest than
trusting 2 data points outright. See `_shrink_toward_prior`."""


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


def _modified_z_scores(prices: list[int]) -> list[float]:
    """Iglewicz & Hoaglin's modified z-score: 0.6745 * (x - median) / MAD.
    The 0.6745 constant scales MAD to be comparable to a standard
    deviation under a normal distribution, so the same ~3.5 threshold
    convention used for a plain z-score applies here too."""
    med = statistics.median(prices)
    mad = statistics.median([abs(p - med) for p in prices])
    if mad == 0:
        return [0.0] * len(prices)
    return [0.6745 * (p - med) / mad for p in prices]


def _trim_mad_outliers(comps: list[Comp], threshold: float = _MAD_OUTLIER_THRESHOLD) -> list[Comp]:
    """Needs at least 3 points for a MAD estimate to mean anything — below
    that, `compute_baseline` relies on `_confidence_for`'s simpler ratio
    guard instead of trying to identify an "outlier" among 1-2 points."""
    if len(comps) < 3:
        return comps
    prices = [c.price_eur for c in comps]
    z_scores = _modified_z_scores(prices)
    kept = [c for c, z in zip(comps, z_scores) if abs(z) <= threshold]
    return kept if len(kept) >= 2 else comps  # never trim down to nothing


def _shrink_toward_prior(local_comps: list[Comp], all_comps: list[Comp]) -> int:
    """Blends the local (year/mileage-filtered, outlier-trimmed) median
    toward the broader make/model prior when there's little local
    evidence — see `_SHRINKAGE_PRIOR_WEIGHT`. A no-op once there's enough
    local data (`_MIN_SAMPLE_FOR_TRIM` or more) to stand on its own."""
    local_median = _median_price(local_comps)
    n = len(local_comps)
    if n >= _MIN_SAMPLE_FOR_TRIM or len(all_comps) < 2:
        return local_median
    prior_median = _median_price(all_comps)
    weight = n / (n + _SHRINKAGE_PRIOR_WEIGHT)
    return round(weight * local_median + (1 - weight) * prior_median)


def _confidence_for(sample_size: int, price_ratio: float) -> str:
    if price_ratio > _MAX_PRICE_RATIO:
        # Even after MAD trimming (or too few points to trim at all), this
        # pool spans too wide a price range to trust — cap confidence
        # regardless of how many comps remain.
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

    trimmed = _trim_mad_outliers(matched)

    return PriceBaseline(
        make=make,
        model=model,
        target_year=target_year,
        target_mileage_km=target_mileage_km,
        median_price_eur=_shrink_toward_prior(trimmed, comps),
        sample_size=len(trimmed),
        confidence=_confidence_for(len(trimmed), _price_ratio(trimmed)),
    )
