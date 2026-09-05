from __future__ import annotations

from becarscout.notifier.bot import _budget_text, _parse_int_arg
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
