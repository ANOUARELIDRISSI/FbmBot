from __future__ import annotations

from becarscout.analyzer.models import DescriptionSignals
from becarscout.pricing.models import PriceBaseline
from becarscout.scoring.models import ScoringWeights
from becarscout.scoring.scoring import _condition_component, _condition_highlights, _price_component, score_listing
from becarscout.structurer.models import StructuredListing


def _signals(**overrides) -> DescriptionSignals:
    defaults = dict(listing_id="l1")
    defaults.update(overrides)
    return DescriptionSignals(**defaults)


def _structured(**overrides) -> StructuredListing:
    defaults = dict(
        listing_id="l1", url="https://example.test/1", raw_title="2006 Subaru outback",
        price_eur=300, make="Subaru", model_hint="outback", year=2006,
    )
    defaults.update(overrides)
    return StructuredListing(**defaults)


def test_no_signals_means_no_highlights():
    assert _condition_highlights(None) == []


def test_extraction_failed_means_no_highlights():
    assert _condition_highlights(_signals(extraction_failed=True)) == []


def test_warning_light_needing_diagnostic_is_worded_differently_than_a_plain_mention():
    with_diagnostic = _condition_highlights(_signals(warning_light=True, needs_diagnostic=True))
    plain = _condition_highlights(_signals(warning_light=True, needs_diagnostic=False))
    assert with_diagnostic != plain
    assert any("needs diagnostic" in h.lower() for h in with_diagnostic)


def test_major_gearbox_issue_is_flagged_as_major():
    highlights = _condition_highlights(_signals(gearbox_issue=True, gearbox_issue_severity="likely_major"))
    assert any("gearbox" in h.lower() and "major" in h.lower() for h in highlights)


def test_highlights_have_no_point_deltas_in_them():
    # These are meant for the default Telegram card -- see
    # notifier/formatting.py -- the numeric breakdown is a separate,
    # on-demand explanation.
    highlights = _condition_highlights(
        _signals(timing_belt_replaced=True, for_export=True, inspection_valid=True)
    )
    joined = " ".join(highlights)
    assert "+" not in joined
    assert "-" not in joined


def test_positive_and_negative_signals_together():
    highlights = _condition_highlights(
        _signals(timing_belt_replaced=True, inspection_valid=False, service_history="none")
    )
    assert any("timing belt" in h.lower() for h in highlights)
    assert any("inspection" in h.lower() for h in highlights)
    assert any("service history" in h.lower() for h in highlights)


def test_price_at_exactly_the_floor_is_not_scored_as_a_steal():
    # Real bug: price_eur < _MIN_PLAUSIBLE_CAR_PRICE_EUR (300) let a listing
    # priced at exactly 300 slip through the "implausible price" guard.
    baseline = PriceBaseline(make="Subaru", model="outback", median_price_eur=9500, sample_size=2, confidence="medium")
    component, reasoning = _price_component(300, baseline)
    assert component == 0.0
    assert "not comparable" in reasoning[0].lower() or "not scored" in reasoning[0].lower()


def test_price_just_above_the_floor_still_scores_normally():
    baseline = PriceBaseline(make="Volkswagen", model="golf", median_price_eur=9500, sample_size=5, confidence="high")
    component, _ = _price_component(301, baseline)
    assert component != 0.0


def test_parts_listing_is_not_scored_as_a_car_deal():
    # The real listing that motivated this: "2006 Subaru outback" title,
    # but the description was "Seats for 2006-2008 Subaru outback... 300
    # for all" -- a real, live false positive (scored +58, "97% below
    # market") caught by manually auditing Telegram output.
    baseline = PriceBaseline(make="Subaru", model="outback", median_price_eur=9500, sample_size=2, confidence="medium")
    signals = _signals(is_whole_vehicle=False, confidence="low")
    scored = score_listing(_structured(), signals, baseline)
    assert scored.score == 0
    assert scored.price_component == 0.0
    assert scored.condition_component == 0.0
    assert any("not a whole vehicle" in r.lower() or "parts" in r.lower() for r in scored.reasoning)


def test_whole_vehicle_default_still_scores_normally():
    # is_whole_vehicle defaults to True -- a real car's signals shouldn't
    # accidentally trip the new gate.
    baseline = PriceBaseline(make="Subaru", model="outback", median_price_eur=9500, sample_size=2, confidence="medium")
    signals = _signals(confidence="low")
    scored = score_listing(_structured(price_eur=4000), signals, baseline)
    assert scored.score != 0


def test_extraction_failure_does_not_trip_the_whole_vehicle_gate():
    # extraction_failed listings default is_whole_vehicle=True too --
    # they should fall through to normal (condition-unscored) handling,
    # not get zeroed out as if they were known to be parts.
    baseline = PriceBaseline(make="Subaru", model="outback", median_price_eur=9500, sample_size=2, confidence="medium")
    signals = _signals(extraction_failed=True)
    scored = score_listing(_structured(price_eur=4000), signals, baseline)
    assert scored.score != 0


# --- ScoringWeights actually changes behavior (this is the whole point of
# moving off hardcoded constants -- /validate needs this to be real) ---


def test_custom_weights_change_the_condition_component():
    signals = _signals(warning_light=True, needs_diagnostic=True, confidence="high")
    default_component, _ = _condition_component(signals)

    harsher = ScoringWeights(warning_light_needs_diagnostic=-50)
    harsher_component, harsher_reasoning = _condition_component(signals, harsher)

    assert harsher_component != default_component
    assert harsher_component < default_component  # more negative
    assert any("-50" in r for r in harsher_reasoning)


def test_default_weights_reproduce_original_hardcoded_values():
    # ScoringWeights() with no arguments must be behaviorally identical to
    # the pre-refactor hardcoded constants -- this is the safety property
    # the whole refactor depends on.
    signals = _signals(gearbox_issue=True, gearbox_issue_severity="likely_major", confidence="high")
    component, reasoning = _condition_component(signals, ScoringWeights())
    assert any("-40" in r for r in reasoning)


def test_score_listing_accepts_custom_weights_end_to_end():
    baseline = PriceBaseline(make="Volkswagen", model="Golf", median_price_eur=9000, sample_size=5, confidence="high")
    signals = _signals(for_export=True, confidence="high")

    default_scored = score_listing(_structured(price_eur=8000), signals, baseline)
    custom_scored = score_listing(_structured(price_eur=8000), signals, baseline, ScoringWeights(for_export=-60))

    assert custom_scored.score < default_scored.score
