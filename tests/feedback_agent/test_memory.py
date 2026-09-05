from __future__ import annotations

from becarscout.feedback_agent.memory import _describe
from becarscout.scoring.models import ScoredListing


def _listing(**overrides) -> ScoredListing:
    defaults = dict(
        listing_id="l1",
        url="https://example.test/1",
        raw_title="2015 Volkswagen Golf",
        make="Volkswagen",
        model_hint="Golf",
        price_eur=8500,
        year=2015,
        mileage_km=110_000,
        baseline_median_price_eur=11_000,
        score=42,
        reasoning=[],
    )
    defaults.update(overrides)
    return ScoredListing(**defaults)


def test_describe_includes_the_core_comparable_facts():
    text = _describe(_listing())
    assert "2015 Volkswagen Golf" in text
    assert "8,500" in text
    assert "110,000km" in text
    assert "11,000" in text
    assert "+42" in text


def test_describe_includes_condition_reasoning_so_patterns_are_detectable():
    # Without the reasoning trail in the stored memory, the review agent
    # (graph.py) could never notice a pattern like "you keep disliking
    # cars with unresolved warning lights" -- see memory.py's docstring.
    text = _describe(_listing(reasoning=["Warning light, needs diagnostic: -20"]))
    assert "Warning light, needs diagnostic" in text


def test_describe_handles_missing_optional_fields():
    text = _describe(
        ScoredListing(listing_id="l2", url="u2", raw_title="Unknown car", score=0, reasoning=[])
    )
    assert "unknown make" in text
    assert "+0" in text
