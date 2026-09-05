from __future__ import annotations

from becarscout.notifier.formatting import format_opportunity_message
from becarscout.scoring.models import ScoredListing


def _listing(**overrides) -> ScoredListing:
    defaults = dict(
        listing_id="l1",
        url="https://example.test/1",
        raw_title="2015 Volkswagen Golf",
        price_eur=8500,
        year=2015,
        mileage_km=110_000,
        score=42,
        reasoning=["Priced €8,500 vs. €11,000 median (23% below market) -> +23.0"],
    )
    defaults.update(overrides)
    return ScoredListing(**defaults)


def test_message_has_no_memory_note_without_similar_feedback():
    message = format_opportunity_message(_listing())
    assert "similar" not in message.lower()


def test_message_notes_liked_only():
    similar = [{"metadata": {"verdict": "up"}}, {"metadata": {"verdict": "up"}}]
    message = format_opportunity_message(_listing(), similar)
    assert "2 similar cars you liked before" in message
    assert "disliked" not in message


def test_message_notes_disliked_only():
    similar = [{"metadata": {"verdict": "down"}}]
    message = format_opportunity_message(_listing(), similar)
    assert "1 you disliked before" in message
    assert "you liked before" not in message


def test_message_notes_mixed_liked_and_disliked():
    similar = [{"metadata": {"verdict": "up"}}, {"metadata": {"verdict": "down"}}]
    message = format_opportunity_message(_listing(), similar)
    assert "1 similar car you liked before" in message
    assert "1 you disliked before" in message


def test_similar_feedback_note_never_changes_the_score_line():
    similar = [{"metadata": {"verdict": "up"}}]
    message = format_opportunity_message(_listing(), similar)
    assert "Score: +42" in message
