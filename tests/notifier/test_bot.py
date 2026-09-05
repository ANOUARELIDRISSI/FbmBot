from __future__ import annotations

from becarscout.notifier.bot import _parse_days_arg


def test_parse_days_uses_default_when_no_args():
    assert _parse_days_arg(None, default=7) == 7
    assert _parse_days_arg([], default=7) == 7


def test_parse_days_parses_a_valid_int():
    assert _parse_days_arg(["14"], default=7) == 14


def test_parse_days_falls_back_on_garbage_input():
    assert _parse_days_arg(["not-a-number"], default=7) == 7


def test_parse_days_clamps_to_at_least_one():
    assert _parse_days_arg(["0"], default=7) == 1
    assert _parse_days_arg(["-5"], default=7) == 1


def test_parse_days_default_can_be_none():
    assert _parse_days_arg(None, default=None) is None
