from __future__ import annotations

from becarscout.scraper.facebook_marketplace import _prices_differ


def test_real_price_change_is_detected():
    assert _prices_differ("€9,500", "€8,000") is True


def test_same_price_different_formatting_is_not_a_change():
    # This is the whole reason to compare parsed numbers, not raw text --
    # the grid and detail pages don't always format the same price
    # identically.
    assert _prices_differ("8.500", "8 500") is False
    assert _prices_differ("€8,500", "8500") is False


def test_unparseable_price_never_produces_a_false_positive():
    # Missing/unparseable data shouldn't trigger an unnecessary detail-page
    # revisit -- a false negative here is far cheaper than a false positive.
    assert _prices_differ(None, "€8,000") is False
    assert _prices_differ("€8,000", None) is False
    assert _prices_differ(None, None) is False


def test_free_listing_price_text_does_not_false_positive_against_itself():
    assert _prices_differ("Free", "Free") is False
