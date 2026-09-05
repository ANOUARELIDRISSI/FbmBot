from __future__ import annotations

from becarscout.notifier.formatting import (
    format_history_summary,
    format_opportunity_message,
    format_score_explanation,
)
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


def test_default_card_shows_fuel_and_transmission():
    message = format_opportunity_message(_listing(fuel_type="diesel", transmission="manual"))
    assert "Diesel" in message
    assert "Manual" in message


def test_default_card_shows_condition_highlights_plainly():
    message = format_opportunity_message(_listing(condition_highlights=["⚠️ Warning light mentioned"]))
    assert "Warning light mentioned" in message


def test_default_card_does_not_include_the_full_numeric_reasoning():
    # The point-by-point breakdown is reserved for the "Why?" button (see
    # format_score_explanation) so the default card stays scannable.
    message = format_opportunity_message(_listing())
    assert "€8,500 vs" not in message
    assert "Why?" in message  # points the user at the explanation instead


def test_score_explanation_contains_the_full_reasoning_trail():
    explanation = format_score_explanation(_listing())
    assert "€8,500 vs" in explanation
    assert "scored +42" in explanation


def test_score_explanation_handles_no_reasoning():
    explanation = format_score_explanation(_listing(reasoning=[]))
    assert "No detailed reasoning available." in explanation


def test_history_summary_handles_empty_range():
    assert "No opportunities sent" in format_history_summary([], days=7)


def test_history_summary_marks_reviewed_and_unreviewed():
    entries = [
        (_listing(listing_id="a"), "up"),
        (_listing(listing_id="b"), "down"),
        (_listing(listing_id="c"), None),
    ]
    summary = format_history_summary(entries, days=7)
    assert "1 not yet reviewed" in summary
    assert "/missed" in summary


def test_history_summary_omits_the_unreviewed_line_when_all_reviewed():
    entries = [(_listing(listing_id="a"), "up"), (_listing(listing_id="b"), "down")]
    summary = format_history_summary(entries, days=7)
    assert "not yet reviewed" not in summary


def test_history_summary_caps_long_lists():
    entries = [(_listing(listing_id=str(i)), "up") for i in range(50)]
    summary = format_history_summary(entries, days=30)
    assert "...and 20 more." in summary
