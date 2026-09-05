from __future__ import annotations

from datetime import datetime, timezone

from becarscout.db.models import ListingRow
from becarscout.notifier.bot import _budget_text, _format_search_hit, _parse_csv_arg, _parse_int_arg, _summarize_find_output
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


def test_parse_csv_arg_handles_comma_with_no_spaces():
    assert _parse_csv_arg(["bmw,toyota"]) == ["bmw", "toyota"]


def test_parse_csv_arg_handles_comma_with_spaces_split_across_args():
    assert _parse_csv_arg(["bmw,", "toyota"]) == ["bmw", "toyota"]


def test_parse_csv_arg_lowercases_and_strips():
    assert _parse_csv_arg(["BMW", " , ", "Land Rover"]) == ["bmw", "land rover"]


def test_parse_csv_arg_empty_input():
    assert _parse_csv_arg([]) == []


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


def test_summarize_find_output_reports_new_listings_and_sent_opportunities():
    output = (
        "2026-09-05 19:04:44 INFO scrape: 20 new listings, 3 price changes\n"
        "2026-09-05 19:05:00 INFO score: 20 listings (2 opportunities)\n"
        "2026-09-05 19:05:01 INFO notify: 2 sent\n"
    )
    text = _summarize_find_output(output)
    assert "20 new listing" in text
    assert "2" in text and "sent" in text.lower()


def test_summarize_find_output_when_nothing_cleared_the_bar():
    output = (
        "2026-09-05 19:04:44 INFO scrape: 5 new listings, 0 price changes\n"
        "2026-09-05 19:05:00 INFO score: 5 listings (0 opportunities)\n"
        "2026-09-05 19:05:01 INFO notify: 0 sent\n"
    )
    text = _summarize_find_output(output)
    assert "5 new listing" in text
    assert "Nothing good enough" in text


def test_summarize_find_output_handles_singular_listing():
    output = "2026-09-05 19:04:44 INFO scrape: 1 new listings, 0 price changes\n"
    text = _summarize_find_output(output)
    assert "1 new listing." in text


def test_summarize_find_output_falls_back_when_nothing_recognizable():
    text = _summarize_find_output("some unrelated crash traceback")
    assert "went wrong" in text
