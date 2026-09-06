from __future__ import annotations

from datetime import datetime, timezone

from becarscout.db.models import ListingRow
from becarscout.notifier.bot import (
    _PROMPTABLE_COMMANDS,
    _QUICK_PICK_LABELS,
    _QUICK_PICKS,
    _SEARCH_RANGE_DAYS,
    _TEXT_COMMANDS,
    _WEIGHT_LABELS,
    _WIZARD_STEPS,
    _budget_text,
    _cmd_budget,
    _cmd_settings,
    _cmd_weights,
    _format_search_hit,
    _parse_csv_arg,
    _parse_int_arg,
    _parse_search_args,
    _quick_pick_keyboard,
    _search_range_keyboard,
    _summarize_find_output,
    _weight_value_text,
    _weights_keyboard,
    _welcome_text,
)
from becarscout.scoring.models import ScoredListing, ScoringWeights
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
    row = _make_row(price_eur=9500, year=2016, mileage_km=120000)
    scored = ScoredListing(listing_id="l1", url=row.url, raw_title=row.raw_title, score=34, reasoning=[])
    text = _format_search_hit(row, scored)
    assert "€9,500" in text
    assert "2016" in text
    assert "120,000 km" in text
    assert "Score: +34" in text
    assert row.url in text


def test_format_search_hit_marks_unscored_listings():
    row = _make_row()
    text = _format_search_hit(row, None)
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


def test_quick_pick_keyboard_returns_none_for_settings_without_one():
    assert _quick_pick_keyboard("minyear") is None
    assert _quick_pick_keyboard("radius") is None
    assert _quick_pick_keyboard("nonexistent") is None


def test_quick_pick_keyboard_threshold_has_one_button_per_option():
    keyboard = _quick_pick_keyboard("threshold")
    assert keyboard is not None
    buttons = keyboard.inline_keyboard[0]
    assert len(buttons) == len(_QUICK_PICK_LABELS["threshold"])
    assert all(btn.callback_data.startswith("set:qp:threshold:") for btn in buttons)


def test_quick_pick_keyboard_budget_has_one_button_per_option():
    keyboard = _quick_pick_keyboard("budget")
    assert keyboard is not None
    buttons = keyboard.inline_keyboard[0]
    assert len(buttons) == len(_QUICK_PICK_LABELS["budget"])
    assert all(btn.callback_data.startswith("set:qp:budget:") for btn in buttons)


def test_every_quick_pick_key_has_a_label():
    for setting, options in _QUICK_PICKS.items():
        labelled_keys = {key for key, _ in _QUICK_PICK_LABELS[setting]}
        assert set(options.keys()) == labelled_keys


def test_quick_pick_changes_are_valid_pipeline_settings_fields():
    valid_fields = set(PipelineSettings.model_fields.keys())
    for options in _QUICK_PICKS.values():
        for changes in options.values():
            assert set(changes.keys()) <= valid_fields


def test_wizard_steps_are_all_promptable():
    assert set(_WIZARD_STEPS) <= set(_PROMPTABLE_COMMANDS.keys())


def test_wizard_steps_have_no_duplicates():
    assert len(_WIZARD_STEPS) == len(set(_WIZARD_STEPS))


def test_parse_search_args_splits_off_a_trailing_range_token():
    assert _parse_search_args(["golf", "week"]) == ("golf", 7)
    assert _parse_search_args(["golf"]) == ("golf", None)


def test_parse_search_args_range_alone_means_no_keyword():
    assert _parse_search_args(["today"]) == ("", 1)


def test_parse_search_args_multi_word_keyword_before_range():
    assert _parse_search_args(["land", "rover", "3days"]) == ("land rover", 3)


def test_parse_search_args_empty_input():
    assert _parse_search_args([]) == ("", None)


def test_search_range_keyboard_has_one_button_per_range():
    keyboard = _search_range_keyboard()
    buttons = keyboard.inline_keyboard[0]
    assert len(buttons) == len(_SEARCH_RANGE_DAYS)
    assert all(btn.callback_data.startswith("set:sr:") for btn in buttons)


def test_welcome_text_greets_by_name_when_known():
    text = _welcome_text("Badr")
    assert "Hi Badr!" in text


def test_welcome_text_falls_back_to_generic_greeting():
    text = _welcome_text(None)
    assert "Hi!" in text
    assert "Hi None" not in text


def test_name_is_promptable():
    assert "name" in _PROMPTABLE_COMMANDS


def test_weight_labels_match_scoring_weights_fields_exactly():
    assert set(_WEIGHT_LABELS.keys()) == set(ScoringWeights.model_fields.keys())


def test_weight_value_text_formats_price_field_as_currency():
    assert _weight_value_text("min_plausible_car_price_eur", 300) == "€300"


def test_weight_value_text_formats_point_fields_with_sign():
    assert _weight_value_text("accident_damage", -25) == "-25 pts"
    assert _weight_value_text("timing_belt_replaced", 12) == "+12 pts"


def test_weights_keyboard_has_one_button_per_weight_field():
    keyboard = _weights_keyboard()
    assert len(keyboard.inline_keyboard) == len(_WEIGHT_LABELS)
    all_callback_data = [row[0].callback_data for row in keyboard.inline_keyboard]
    assert all(cd.startswith("set:wt:") for cd in all_callback_data)
    fields = {cd.split(":", 2)[2] for cd in all_callback_data}
    assert fields == set(_WEIGHT_LABELS.keys())


def test_text_commands_covers_every_promptable_command():
    assert set(_PROMPTABLE_COMMANDS.keys()) <= set(_TEXT_COMMANDS.keys())


def test_text_commands_includes_weights_and_its_showall_alias():
    assert _TEXT_COMMANDS["weights"] is _cmd_weights
    assert _TEXT_COMMANDS["showall"] is _cmd_weights


def test_text_commands_maps_plain_words_to_the_same_handlers_as_their_slash_command():
    assert _TEXT_COMMANDS["settings"] is _cmd_settings
    assert _TEXT_COMMANDS["budget"] is _cmd_budget
