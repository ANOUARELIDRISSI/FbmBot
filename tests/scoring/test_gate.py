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


def test_max_mileage_excludes_higher_mileage_cars():
    high_mileage = _listing(listing_id="l1", mileage_km=200_000)
    gated = apply_decision_gate([high_mileage], threshold=DEFAULT_THRESHOLD, max_mileage_km=150_000)
    assert not gated[0].above_threshold


def test_max_mileage_allows_lower_mileage_cars():
    low_mileage = _listing(listing_id="l1", mileage_km=100_000)
    gated = apply_decision_gate([low_mileage], threshold=DEFAULT_THRESHOLD, max_mileage_km=150_000)
    assert gated[0].above_threshold


def test_unknown_mileage_is_not_punished_by_the_mileage_cap():
    unknown_mileage = _listing(listing_id="l1", mileage_km=None)
    gated = apply_decision_gate([unknown_mileage], threshold=DEFAULT_THRESHOLD, max_mileage_km=150_000)
    assert gated[0].above_threshold


def test_make_filter_only_allows_matching_brands():
    bmw = _listing(listing_id="bmw", make="BMW")
    toyota = _listing(listing_id="toyota", make="Toyota")
    gated = apply_decision_gate([bmw, toyota], threshold=DEFAULT_THRESHOLD, makes="bmw")
    by_id = {s.listing_id: s for s in gated}
    assert by_id["bmw"].above_threshold
    assert not by_id["toyota"].above_threshold


def test_make_filter_is_case_insensitive_and_supports_multiple_brands():
    bmw = _listing(listing_id="bmw", make="BMW")
    gated = apply_decision_gate([bmw], threshold=DEFAULT_THRESHOLD, makes="TOYOTA, bmw")
    assert gated[0].above_threshold


def test_make_filter_excludes_unknown_make_unlike_the_year_cutoff():
    # Unlike min_year/max_mileage, a make filter is an explicit "only show
    # me X" request -- letting undetected-make listings through would make
    # the filter nearly meaningless.
    unknown_make = _listing(listing_id="l1", make=None)
    gated = apply_decision_gate([unknown_make], threshold=DEFAULT_THRESHOLD, makes="bmw")
    assert not gated[0].above_threshold


def test_fuel_type_filter():
    diesel = _listing(listing_id="diesel", fuel_type="diesel")
    petrol = _listing(listing_id="petrol", fuel_type="petrol")
    gated = apply_decision_gate([diesel, petrol], threshold=DEFAULT_THRESHOLD, fuel_types="diesel,hybrid")
    by_id = {s.listing_id: s for s in gated}
    assert by_id["diesel"].above_threshold
    assert not by_id["petrol"].above_threshold


def test_transmission_filter():
    automatic = _listing(listing_id="auto", transmission="automatic")
    manual = _listing(listing_id="manual", transmission="manual")
    gated = apply_decision_gate([automatic, manual], threshold=DEFAULT_THRESHOLD, transmission="automatic")
    by_id = {s.listing_id: s for s in gated}
    assert by_id["auto"].above_threshold
    assert not by_id["manual"].above_threshold


def test_no_filters_set_behaves_exactly_like_before():
    plain = _listing(listing_id="l1", year=2020, mileage_km=50_000, make=None, fuel_type=None, transmission=None)
    gated = apply_decision_gate([plain], threshold=DEFAULT_THRESHOLD)
    assert gated[0].above_threshold
