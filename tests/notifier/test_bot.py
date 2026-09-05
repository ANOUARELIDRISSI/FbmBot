from __future__ import annotations

from datetime import datetime, timezone

from becarscout.db.models import ListingRow
from becarscout.notifier.bot import _SUMMARY_LINE_RE, _budget_text, _format_search_hit, _parse_int_arg
from becarscout.settings import PipelineSettings


def test_parse_int_arg_valid():
    assert _parse_int_arg("15000") == 15000
    assert _parse_int_arg("-5") == -5


def test_parse_int_arg_invalid_returns_none():
    assert _parse_int_arg("not-a-number") is None
    assert _parse_int_arg("") is None


def test_budget_text_no_limits():
    text = _budget_text(PipelineSettings(min_price=None, max_price=None))
    assert "€0" in text
    assert "no max" in text


def test_budget_text_max_only():
    text = _budget_text(PipelineSettings(min_price=None, max_price=15000))
    assert "€15,000" in text
    assert "€0" in text


def test_budget_text_full_range():
    text = _budget_text(PipelineSettings(min_price=3000, max_price=12000))
    assert "€3,000" in text
    assert "€12,000" in text


def _make_row(**overrides) -> ListingRow:
    defaults = dict(
        listing_id="l1",
        url="https://example.test/l1",
        raw_title="VW Golf 7 TDI",
        scraped_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return ListingRow(**defaults)


def test_format_search_hit_includes_stats_and_score():
    row = _make_row(price_eur=9500, year=2016, mileage_km=120000, score=34, scored_at=datetime.now(timezone.utc))
    text = _format_search_hit(row)
    assert "€9,500" in text
    assert "2016" in text
    assert "120,000 km" in text
    assert "Score: +34" in text
    assert row.url in text


def test_format_search_hit_marks_unscored_listings():
    row = _make_row(scored_at=None)
    text = _format_search_hit(row)
    assert "Not yet scored" in text


def test_summary_line_regex_matches_pipeline_stage_lines():
    assert _SUMMARY_LINE_RE.search("score: 31 listings (0 opportunities)")
    assert _SUMMARY_LINE_RE.search("notify: 0 sent")
    assert not _SUMMARY_LINE_RE.search("some unrelated log line")


def test_summary_line_regex_strips_timestamp_and_level_prefix():
    match = _SUMMARY_LINE_RE.search("2026-09-05 19:04:44,550 INFO score: 31 listings (0 opportunities)")
    assert match is not None
    assert match.group(0) == "score: 31 listings (0 opportunities)"
