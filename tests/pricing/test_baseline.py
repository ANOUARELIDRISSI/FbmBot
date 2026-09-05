"""Regression tests for stage [4]'s comp-matching — see Project.md's
Scoring Engine notes. Reproduces a live bug: a 1986 Ford F-150 (base trim)
was priced against a comp pool dominated by modern F-150s (mostly Raptor
trim, €30k-€100k+), producing a meaningless median and a false "72% below
market" deal signal.
"""

from __future__ import annotations

from becarscout.pricing.baseline import _modified_z_scores, _shrink_toward_prior, compute_baseline
from becarscout.pricing.models import Comp


def _comp(price_eur: int, year: int | None = None, mileage_km: int | None = None) -> Comp:
    return Comp(title="test comp", url="https://example.test/1", price_eur=price_eur, year=year, mileage_km=mileage_km)


def test_mixed_era_comp_pool_is_not_used_for_a_classic_car():
    # 19 modern F-150 comps (mostly Raptor-trim) alongside a target 1986
    # base-trim truck — none of these are actually comparable.
    modern_comps = [_comp(price_eur=p, year=y) for p, y in [
        (32000, 2018), (35000, 2019), (41000, 2020), (45000, 2020), (48000, 2021),
        (52000, 2021), (55000, 2022), (58000, 2022), (61000, 2022), (63000, 2023),
        (65000, 2023), (68000, 2023), (70000, 2023), (72000, 2024), (75000, 2024),
        (78000, 2024), (82000, 2024), (90000, 2024), (99000, 2024),
    ]]

    baseline = compute_baseline("Ford", "F-150", modern_comps, target_year=1986, target_mileage_km=150_000)

    # No plausible-era comps exist at all -> no median should be fabricated
    # from an entirely different vehicle generation.
    assert baseline.median_price_eur is None
    assert baseline.confidence == "none"


def test_tight_consistent_comp_pool_still_scores_normally():
    comps = [_comp(price_eur=p, year=y, mileage_km=m) for p, y, m in [
        (9200, 2014, 110_000),
        (9800, 2015, 105_000),
        (10100, 2015, 98_000),
        (10400, 2016, 90_000),
        (10600, 2015, 102_000),
        (11000, 2016, 95_000),
    ]]

    baseline = compute_baseline("Volkswagen", "Golf", comps, target_year=2015, target_mileage_km=100_000)

    assert baseline.median_price_eur is not None
    assert baseline.confidence == "high"
    assert baseline.sample_size >= 4


def test_high_variance_pool_forces_confidence_down_even_with_only_two_comps():
    # Only two comps survive year filtering, and they're wildly different
    # prices (a beater vs. a rare high-trim example) -- too few to trim
    # away the outlier, so confidence must reflect the unreliability
    # instead of reporting "medium" off just n=2.
    comps = [
        _comp(price_eur=4800, year=2007),
        _comp(price_eur=32000, year=2008),
    ]

    baseline = compute_baseline("Dodge", "Nitro", comps, target_year=2007)

    assert baseline.confidence == "low"


def test_price_ratio_outlier_is_trimmed_before_computing_median():
    comps = [
        _comp(price_eur=9000, year=2015),
        _comp(price_eur=9500, year=2015),
        _comp(price_eur=10000, year=2016),
        _comp(price_eur=60000, year=2016),  # one implausible outlier
    ]

    baseline = compute_baseline("Volkswagen", "Golf", comps, target_year=2015)

    assert baseline.median_price_eur is not None
    assert baseline.median_price_eur < 15000
    assert baseline.confidence != "none"


# --- MAD-based outlier detection (Iglewicz & Hoaglin modified z-score) ---


def test_modified_z_score_flags_the_extreme_value():
    z_scores = _modified_z_scores([9000, 9500, 10000, 60000])
    assert abs(z_scores[-1]) > 3.5
    assert all(abs(z) < 3.5 for z in z_scores[:-1])


def test_modified_z_score_flags_nothing_in_a_tight_cluster():
    z_scores = _modified_z_scores([9200, 9800, 10100, 10400, 10600, 11000])
    assert all(abs(z) < 3.5 for z in z_scores)


def test_mad_trimming_needs_at_least_three_points():
    # Can't compute a stable MAD from 1-2 points -- compute_baseline falls
    # back to the simpler ratio guard for those instead (see
    # test_high_variance_pool_forces_confidence_down_even_with_only_two_comps).
    comps = [_comp(price_eur=4800, year=2007), _comp(price_eur=32000, year=2008)]
    baseline = compute_baseline("Dodge", "Nitro", comps, target_year=2007)
    assert baseline.sample_size == 2  # nothing was (or could be) trimmed


# --- Empirical-Bayes-style shrinkage toward a broader prior ---


def test_shrinkage_pulls_a_thin_local_pool_toward_the_wider_market():
    # The real case that motivated this: a 2006 Subaru Outback matched
    # only 2 local comps (both far from the wider Outback market), and
    # their raw median was trusted outright. Shrinkage should land
    # somewhere between the two, not exactly on the thin local median.
    local = [_comp(price_eur=15000), _comp(price_eur=16000)]  # local median 15500
    all_comps = local + [_comp(price_eur=p) for p in (9000, 9200, 9500, 9800, 10000, 10200)]  # wider market ~9500

    blended = _shrink_toward_prior(local, all_comps)

    assert 9500 < blended < 15500


def test_shrinkage_is_a_no_op_with_enough_local_evidence():
    local = [_comp(price_eur=p) for p in (9000, 9500, 10000, 10200, 10500)]  # 5, meets the trim threshold
    all_comps = local + [_comp(price_eur=20000)]

    blended = _shrink_toward_prior(local, all_comps)

    assert blended == 10000  # unaffected by the prior


def test_shrinkage_falls_back_to_local_median_with_no_wider_pool():
    local = [_comp(price_eur=15000)]
    assert _shrink_toward_prior(local, all_comps=local) == 15000


def test_thin_pool_baseline_lands_between_local_and_wider_market():
    # End-to-end version of the shrinkage case above, through
    # compute_baseline's public interface.
    comps = [_comp(price_eur=p, year=2006) for p in (15000, 16000)] + [
        _comp(price_eur=p, year=2010) for p in (9000, 9200, 9500, 9800, 10000, 10200)
    ]

    baseline = compute_baseline("Subaru", "Outback", comps, target_year=2006)

    assert baseline.sample_size == 2
    assert baseline.median_price_eur is not None
    assert baseline.median_price_eur < 15500  # pulled down from the thin local median
