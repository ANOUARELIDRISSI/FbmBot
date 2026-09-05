"""Stage [4]: Scoring Engine. Rule-based, not an LLM — combines the price
gap (listing price vs. `PriceBaseline` comps median) with condition
adjustments from stage 3's `DescriptionSignals`, each weighted by how much
that part of the score should be trusted (baseline sample size, signals
extraction confidence). Every point is named in the reasoning trail.
"""

from __future__ import annotations

from becarscout.analyzer.models import DescriptionSignals
from becarscout.pricing.models import PriceBaseline
from becarscout.structurer.models import StructuredListing

from .models import ScoredListing

# Applied to the price-gap component, based on how many comps the baseline
# median was computed from (see pricing/baseline.py).
_BASELINE_CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3, "none": 0.0}

# Applied to the sum of condition adjustments, based on stage 3's own
# confidence in how clear/complete the description was to extract from.
_SIGNALS_CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}

# Point deltas per condition flag — the only place "what matters and how
# much" is decided, so tuning the model's behavior means editing numbers
# here, not hunting through scoring logic.
_GEARBOX_ISSUE = {"likely_major": -40, "minor": -20, "unknown": -20}
_ENGINE_ISSUE = {"likely_major": -40, "minor": -20, "unknown": -20}
_ACCIDENT_DAMAGE = -25
_WARNING_LIGHT_NEEDS_DIAGNOSTIC = -20
_WARNING_LIGHT_ONLY = -8
_TIMING_BELT_REPLACED = 12
_INSPECTION_VALID = 6
_INSPECTION_INVALID = -18
_SERVICE_HISTORY_COMPLETE = 6
_SERVICE_HISTORY_NONE = -6
_FOR_EXPORT = -15

# A genuine whole running car essentially never sells for less than this in
# Belgium, even rough condition — prices below it are "make an offer" bait
# pricing (seen as literal €0/€1 listings during MVP validation) or a
# parts-only listing, neither of which is comparable to whole-car baseline
# comps. Scored as "no reliable price" rather than as a 100%-below-market
# steal.
_MIN_PLAUSIBLE_CAR_PRICE_EUR = 300


def _price_component(price_eur: int | None, baseline: PriceBaseline) -> tuple[float, list[str]]:
    if price_eur is None or baseline.median_price_eur is None:
        return 0.0, [
            f"No price baseline available for {baseline.make} {baseline.model} "
            "(no comparable listings found) — score based on condition signals only."
        ]

    if price_eur <= _MIN_PLAUSIBLE_CAR_PRICE_EUR:
        return 0.0, [
            f"Price €{price_eur} is below a plausible whole-car threshold "
            f"(€{_MIN_PLAUSIBLE_CAR_PRICE_EUR}) — likely a parts listing or "
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


def _condition_component(signals: DescriptionSignals | None) -> tuple[float, list[str]]:
    if signals is None:
        return 0.0, ["No description signals available — condition unscored."]
    if signals.extraction_failed:
        return 0.0, ["Description analysis failed — condition unscored."]

    raw = 0
    reasoning: list[str] = []

    if signals.gearbox_issue:
        severity = signals.gearbox_issue_severity or "unknown"
        delta = _GEARBOX_ISSUE.get(severity, _GEARBOX_ISSUE["unknown"])
        raw += delta
        reasoning.append(f"Gearbox issue ({severity}): {delta:+d}")

    if signals.engine_issue:
        severity = signals.engine_issue_severity or "unknown"
        delta = _ENGINE_ISSUE.get(severity, _ENGINE_ISSUE["unknown"])
        raw += delta
        reasoning.append(f"Engine issue ({severity}): {delta:+d}")

    if signals.accident_damage:
        raw += _ACCIDENT_DAMAGE
        reasoning.append(f"Accident/body damage mentioned: {_ACCIDENT_DAMAGE:+d}")

    if signals.warning_light:
        if signals.needs_diagnostic:
            raw += _WARNING_LIGHT_NEEDS_DIAGNOSTIC
            reasoning.append(f"Warning light, needs diagnostic: {_WARNING_LIGHT_NEEDS_DIAGNOSTIC:+d}")
        else:
            raw += _WARNING_LIGHT_ONLY
            reasoning.append(f"Warning light mentioned: {_WARNING_LIGHT_ONLY:+d}")

    if signals.timing_belt_replaced:
        raw += _TIMING_BELT_REPLACED
        reasoning.append(f"Timing belt replaced: {_TIMING_BELT_REPLACED:+d}")

    if signals.inspection_valid is True:
        raw += _INSPECTION_VALID
        reasoning.append(f"Inspection (keuring) valid: {_INSPECTION_VALID:+d}")
    elif signals.inspection_valid is False:
        raw += _INSPECTION_INVALID
        reasoning.append(f"Inspection (keuring) not valid: {_INSPECTION_INVALID:+d}")

    if signals.service_history == "complete":
        raw += _SERVICE_HISTORY_COMPLETE
        reasoning.append(f"Complete service history: {_SERVICE_HISTORY_COMPLETE:+d}")
    elif signals.service_history == "none":
        raw += _SERVICE_HISTORY_NONE
        reasoning.append(f"No service history: {_SERVICE_HISTORY_NONE:+d}")

    if signals.for_export:
        raw += _FOR_EXPORT
        reasoning.append(f"Listed for export: {_FOR_EXPORT:+d}")

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
    the default card reads like a person describing the car, not a ledger."""
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
) -> ScoredListing:
    # A real bug found via live Telegram output: a listing titled "2006
    # Subaru outback" was actually someone selling seats pulled from one,
    # priced at exactly the €300 floor (see _MIN_PLAUSIBLE_CAR_PRICE_EUR,
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

    price_component, price_reasoning = _price_component(structured.price_eur, baseline)
    condition_component, condition_reasoning = _condition_component(signals)

    return ScoredListing(
        **_base_listing_fields(structured, baseline),
        condition_highlights=_condition_highlights(signals),
        price_component=price_component,
        condition_component=condition_component,
        score=round(price_component + condition_component),
        reasoning=price_reasoning + condition_reasoning,
    )
