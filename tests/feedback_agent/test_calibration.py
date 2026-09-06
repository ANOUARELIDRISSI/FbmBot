from __future__ import annotations

from becarscout.feedback_agent.calibration import _corrections_block, run_score_calibration


def _make_correction(**overrides) -> dict:
    defaults = dict(
        listing_id="l1",
        title="VW Golf 7 TDI",
        model_score=10,
        user_score=30,
        reasoning=["Priced €9,500 vs €11,000 median -> +15.0"],
    )
    defaults.update(overrides)
    return defaults


def test_run_score_calibration_skips_below_the_minimum_correction_count():
    result = run_score_calibration(chat_id=1, corrections=[_make_correction(), _make_correction(listing_id="l2")])

    assert result["skipped"] is True
    assert "2 correction" in result["skip_reason"]


def test_corrections_block_includes_gap_and_reasoning():
    block = _corrections_block([_make_correction(model_score=10, user_score=30)])

    assert "engine scored +10" in block
    assert "should be +30" in block
    assert "gap +20" in block
    assert "Priced €9,500" in block


def test_corrections_block_handles_missing_reasoning():
    block = _corrections_block([_make_correction(reasoning=[])])

    assert "(none)" in block
