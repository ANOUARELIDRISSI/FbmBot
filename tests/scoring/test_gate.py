from __future__ import annotations

from becarscout.scoring.gate import DEFAULT_MIN_YEAR, DEFAULT_THRESHOLD, apply_decision_gate
from becarscout.scoring.models import ScoredListing


def _listing(**overrides) -> ScoredListing:
    defaults = dict(listing_id="l1", url="https://example.test/1", raw_title="a car", score=50, reasoning=[])
    defaults.update(overrides)
    return ScoredListing(**defaults)


def test_default_min_year_excludes_cars_at_or_before_the_cutoff():
    old = _listing(listing_id="old", year=DEFAULT_MIN_YEAR)
    older = _listing(listing_id="older", year=DEFAULT_MIN_YEAR - 5)
    gated = apply_decision_gate([old, older], threshold=DEFAULT_THRESHOLD)
    assert all(not s.above_threshold for s in gated)


def test_default_min_year_allows_cars_after_the_cutoff():
    newer = _listing(listing_id="newer", year=DEFAULT_MIN_YEAR + 1)
    gated = apply_decision_gate([newer], threshold=DEFAULT_THRESHOLD)
    assert gated[0].above_threshold


def test_unknown_year_is_not_punished_by_the_year_cutoff():
    # Stage 2 misses the year on plenty of genuine listings -- a gap in
    # extraction shouldn't be treated as "this car is too old."
    unknown_year = _listing(listing_id="l1", year=None)
    gated = apply_decision_gate([unknown_year], threshold=DEFAULT_THRESHOLD)
    assert gated[0].above_threshold


def test_min_year_none_disables_the_cutoff():
    old = _listing(listing_id="old", year=1990)
    gated = apply_decision_gate([old], threshold=DEFAULT_THRESHOLD, min_year=None)
    assert gated[0].above_threshold


def test_score_below_threshold_still_fails_regardless_of_year():
    low_score_new_car = _listing(listing_id="l1", year=2024, score=0)
    gated = apply_decision_gate([low_score_new_car], threshold=DEFAULT_THRESHOLD)
    assert not gated[0].above_threshold


def test_custom_min_year_is_respected():
    listing_2015 = _listing(listing_id="l1", year=2015)
    gated = apply_decision_gate([listing_2015], threshold=DEFAULT_THRESHOLD, min_year=2018)
    assert not gated[0].above_threshold
