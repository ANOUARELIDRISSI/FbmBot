"""Stage [4]: Scoring Engine. Rule-based, not an LLM — combines the price
gap (listing price vs. `PriceBaseline` comps median) with condition
adjustments from stage 3's `DescriptionSignals`, each weighted by how much
that part of the score should be trusted (baseline sample size, signals
extraction confidence). Every point is named in the reasoning trail.

Condition-signal weights are a `ScoringWeights` parameter (see
`models.py`), not hardcoded constants — this is what lets `/validate`
(see `feedback_agent/`) actually change scoring behavior at runtime from
an approved feedback suggestion. Every function defaults to
`DEFAULT_WEIGHTS`, which reproduces the original hardcoded values exactly,
so nothing here changes behavior unless a caller explicitly passes a
different `ScoringWeights`.
"""

from __future__ import annotations

from becarscout.analyzer.models import DescriptionSignals
from becarscout.pricing.models import PriceBaseline
from becarscout.structurer.models import StructuredListing

from .models import DEFAULT_WEIGHTS, ScoredListing, ScoringWeights

# Applied to the price-gap component, based on how many comps the baseline
# median was computed from (see pricing/baseline.py).
_BASELINE_CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3, "none": 0.0}

# Applied to the sum of condition adjustments, based on stage 3's own
# confidence in how clear/complete the description was to extract from.
_SIGNALS_CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}


def _price_component(
    price_eur: int | None, baseline: PriceBaseline, weights: ScoringWeights = DEFAULT_WEIGHTS
) -> tuple[float, list[str]]:
    if price_eur is None or baseline.median_price_eur is None:
        return 0.0, [
            f"No price baseline available for {baseline.make} {baseline.model} "
            "(no comparable listings found) — score based on condition signals only."
        ]

    if price_eur <= weights.min_plausible_car_price_eur:
        return 0.0, [
            f"Price €{price_eur} is below a plausible whole-car threshold "
            f"(€{weights.min_plausible_car_price_eur}) — likely a parts listing or "
            "\"make an offer\" placeholder, not comparable to whole-car "
            "baseline comps. Price-gap not scored."
        ]

    gap_eur = baseline.median_price_eur - price_eur
    gap_pct = gap_eur / baseline.median_price_eur
    raw_score = gap_pct * 100
    weight = _BASELINE_CONFIDENCE_WEIGHT[baseline.confidence]
    # A handful of listings during MVP validation had implausible asking
    # prices (e.g. a 2006 Peugeot 407 at €31,000) — almost certainly a
    # seller data-entry error, not a real market signal. The gap direction
    # is still correct information ("steer away from this"), so it isn't
    # dropped, but it's clamped so one typo doesn't dominate the output.
    weighted = max(-150.0, min(150.0, raw_score * weight))

    direction = "below" if gap_eur >= 0 else "above"
    reasoning = [
        f"Priced €{price_eur:,} vs. €{baseline.median_price_eur:,} median "
        f"({abs(gap_pct) * 100:.0f}% {direction} market, n={baseline.sample_size} comps, "
        f"{baseline.confidence} confidence) -> {weighted:+.1f}"
    ]
    return weighted, reasoning


def _condition_component(
    signals: DescriptionSignals | None, weights: ScoringWeights = DEFAULT_WEIGHTS
) -> tuple[float, list[str]]:
    if signals is None:
        return 0.0, ["No description signals available — condition unscored."]
    if signals.extraction_failed:
        return 0.0, ["Description analysis failed — condition unscored."]

    gearbox_weights = {
        "likely_major": weights.gearbox_issue_likely_major,
        "minor": weights.gearbox_issue_minor,
        "unknown": weights.gearbox_issue_unknown,
    }
    engine_weights = {
        "likely_major": weights.engine_issue_likely_major,
        "minor": weights.engine_issue_minor,
        "unknown": weights.engine_issue_unknown,
    }

    raw = 0
    reasoning: list[str] = []

    if signals.gearbox_issue:
        severity = signals.gearbox_issue_severity or "unknown"
        delta = gearbox_weights.get(severity, gearbox_weights["unknown"])
        raw += delta
        reasoning.append(f"Gearbox issue ({severity}): {delta:+d}")

    if signals.engine_issue:
        severity = signals.engine_issue_severity or "unknown"
        delta = engine_weights.get(severity, engine_weights["unknown"])
        raw += delta
        reasoning.append(f"Engine issue ({severity}): {delta:+d}")

    if signals.accident_damage:
        raw += weights.accident_damage
        reasoning.append(f"Accident/body damage mentioned: {weights.accident_damage:+d}")

    if signals.warning_light:
        if signals.needs_diagnostic:
            raw += weights.warning_light_needs_diagnostic
            reasoning.append(f"Warning light, needs diagnostic: {weights.warning_light_needs_diagnostic:+d}")
        else:
            raw += weights.warning_light_only
            reasoning.append(f"Warning light mentioned: {weights.warning_light_only:+d}")

    if signals.timing_belt_replaced:
        raw += weights.timing_belt_replaced
        reasoning.append(f"Timing belt replaced: {weights.timing_belt_replaced:+d}")

    if signals.inspection_valid is True:
        raw += weights.inspection_valid
        reasoning.append(f"Inspection (keuring) valid: {weights.inspection_valid:+d}")
    elif signals.inspection_valid is False:
        raw += weights.inspection_invalid
        reasoning.append(f"Inspection (keuring) not valid: {weights.inspection_invalid:+d}")

    if signals.service_history == "complete":
        raw += weights.service_history_complete
        reasoning.append(f"Complete service history: {weights.service_history_complete:+d}")
    elif signals.service_history == "none":
        raw += weights.service_history_none
        reasoning.append(f"No service history: {weights.service_history_none:+d}")

    if signals.for_export:
        raw += weights.for_export
        reasoning.append(f"Listed for export: {weights.for_export:+d}")

    if not reasoning:
        return 0.0, ["No notable condition signals found in description."]

    weight = _SIGNALS_CONFIDENCE_WEIGHT.get(signals.confidence, 0.4)
    weighted = raw * weight
    reasoning.append(f"Condition subtotal {raw:+d} x {signals.confidence} confidence ({weight}) = {weighted:+.1f}")
    return weighted, reasoning


def _condition_highlights(signals: DescriptionSignals | None) -> list[str]:
    """Plain-language, no-numbers versions of the same flags
    `_condition_component` scores — this is what the Telegram card shows
    by default (see `notifier/formatting.py`); the point-by-point
    breakdown above is reserved for the on-demand "Why?" explanation, so
    the default card reads like a person describing the car, not a ledger.
    Wording doesn't depend on the point weight, so this doesn't take a
    `weights` parameter."""
    if signals is None or signals.extraction_failed:
        return []

    highlights: list[str] = []

    if signals.gearbox_issue:
        note = " (major)" if signals.gearbox_issue_severity == "likely_major" else ""
        highlights.append(f"\U0001f527 Gearbox needs attention{note}")

    if signals.engine_issue:
        note = " (major)" if signals.engine_issue_severity == "likely_major" else ""
        highlights.append(f"\U0001f527 Engine issue mentioned{note}")

    if signals.accident_damage:
        highlights.append("\U0001f4a5 Accident/body damage mentioned")

    if signals.warning_light:
        if signals.needs_diagnostic:
            highlights.append("⚠️ Warning light on — needs diagnostic")
        else:
            highlights.append("⚠️ Warning light mentioned")

    if signals.timing_belt_replaced:
        highlights.append("✅ New timing belt")

    if signals.inspection_valid is True:
        highlights.append("✅ Valid inspection (keuringsbewijs aanwezig)")
    elif signals.inspection_valid is False:
        highlights.append("❌ Inspection not valid")

    if signals.service_history == "complete":
        highlights.append("✅ Full service history")
    elif signals.service_history == "none":
        highlights.append("❌ No service history")

    if signals.for_export:
        highlights.append("\U0001f4e6 Listed for export")

    return highlights


def _base_listing_fields(structured: StructuredListing, baseline: PriceBaseline) -> dict:
    """Fields every `ScoredListing` needs regardless of which path below
    computed the score — kept in one place so the not-a-whole-vehicle
    short-circuit can't drift out of sync with the normal path."""
    return dict(
        listing_id=structured.listing_id,
        url=structured.url,
        raw_title=structured.raw_title,
        price_eur=structured.price_eur,
        make=structured.make,
        model_hint=structured.model_hint,
        year=structured.year,
        mileage_km=structured.mileage_km,
        fuel_type=structured.fuel_type,
        transmission=structured.transmission,
        baseline_median_price_eur=baseline.median_price_eur,
        baseline_sample_size=baseline.sample_size,
        baseline_confidence=baseline.confidence,
    )


def score_listing(
    structured: StructuredListing,
    signals: DescriptionSignals | None,
    baseline: PriceBaseline,
    weights: ScoringWeights = DEFAULT_WEIGHTS,
) -> ScoredListing:
    # A real bug found via live Telegram output: a listing titled "2006
    # Subaru outback" was actually someone selling seats pulled from one,
    # priced at exactly the €300 floor (see min_plausible_car_price_eur,
    # which only guards price — this guards content). Nothing about the
    # title alone flagged it; only stage 3 reading the description can.
    if signals is not None and not signals.extraction_failed and not signals.is_whole_vehicle:
        return ScoredListing(
            **_base_listing_fields(structured, baseline),
            condition_highlights=[],
            price_component=0.0,
            condition_component=0.0,
            score=0,
            reasoning=[
                "Listing doesn't appear to be a whole vehicle (parts/accessories only, "
                "per the description) — not scored as a car deal."
            ],
        )

    price_component, price_reasoning = _price_component(structured.price_eur, baseline, weights)
    condition_component, condition_reasoning = _condition_component(signals, weights)

    return ScoredListing(
        **_base_listing_fields(structured, baseline),
        condition_highlights=_condition_highlights(signals),
        price_component=price_component,
        condition_component=condition_component,
        score=round(price_component + condition_component),
        reasoning=price_reasoning + condition_reasoning,
    )
