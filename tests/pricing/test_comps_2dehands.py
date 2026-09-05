"""Covers the pure parsing logic plus the merge/dedup across sites. Real
network calls (fetch_comps against the live sites) were used to validate
this by hand during development, not in the automated suite — these tests
mock at the `_fetch_one_site` boundary."""

from __future__ import annotations

import pytest

from becarscout.pricing import comps_2dehands
from becarscout.pricing.comps_2dehands import (
    _SITES,
    _ad_id,
    _fetch_from_all_sites,
    _parse_comp_card,
    _parse_price,
)
from becarscout.pricing.models import Comp

_DUTCH_SITE = _SITES[0]
_FRENCH_SITE = _SITES[1]


def test_parse_price_belgian_format():
    assert _parse_price("€22.750,-") == 22750
    assert _parse_price("€1.699,-") == 1699


def test_parse_price_missing_returns_none():
    assert _parse_price("Automatique") is None


def test_ad_id_extracted_from_either_site_url_shape():
    assert _ad_id("/v/auto-s/volkswagen/m2437911744-volkswagen-golf-8") == "2437911744"
    assert _ad_id("/v/autos/volkswagen/m2437911744-volkswagen-golf-8") == "2437911744"


def test_ad_id_missing_returns_none():
    assert _ad_id("/some/other/path") is None


def test_parse_comp_card_dutch_site():
    text = (
        "Bewaren in Mijn Favorieten\n"
        "Volkswagen Golf 8 1.4 hybride\n"
        "€22.750,-\n2021\n74.000 km\nHybride elektrisch/Benzine\nAutomaat\n"
    )
    comp = _parse_comp_card(text, "/v/auto-s/volkswagen/m1-golf", _DUTCH_SITE)
    assert comp == Comp(
        title="Volkswagen Golf 8 1.4 hybride",
        url="https://www.2dehands.be/v/auto-s/volkswagen/m1-golf",
        price_eur=22750,
        year=2021,
        mileage_km=74000,
        fuel_type="hybrid",
        transmission="automatic",
    )


def test_parse_comp_card_french_site():
    text = (
        "Sauvegarder dans Mes Favoris\n"
        "Volkswagen Golf 8 1.4 hybride\n"
        "€22.750,-\n2021\n74.000 km\nHybride électrique/Essence\nAutomatique\n"
    )
    comp = _parse_comp_card(text, "/v/autos/volkswagen/m1-golf", _FRENCH_SITE)
    assert comp is not None
    assert comp.fuel_type == "hybrid"
    assert comp.transmission == "automatic"
    assert comp.price_eur == 22750


def test_parse_comp_card_returns_none_without_a_price():
    text = "Some title\nNo price here\n"
    assert _parse_comp_card(text, "/v/auto-s/x/m1-y", _DUTCH_SITE) is None


def test_parse_comp_card_rejects_implausibly_low_mileage():
    text = "Title\n€5.000,-\n152 km\n"
    comp = _parse_comp_card(text, "/v/auto-s/x/m1-y", _DUTCH_SITE)
    assert comp is not None
    assert comp.mileage_km is None  # 152km rejected as a data-entry error, not near-new


@pytest.mark.asyncio
async def test_merge_dedupes_the_same_ad_across_both_sites(monkeypatch):
    shared = Comp(title="Shared ad", url="https://www.2dehands.be/v/auto-s/x/m1-shared", price_eur=9000)
    only_on_french_site = Comp(title="French-only ad", url="https://www.2ememain.be/v/autos/x/m2-french", price_eur=8000)

    async def fake_fetch_one_site(browser, site, make, model, max_results):
        if site.name == "2dehands.be":
            return {"1": shared}
        return {"1": shared, "2": only_on_french_site}  # site 2 sees the shared ad too, plus one more

    monkeypatch.setattr(comps_2dehands, "_fetch_one_site", fake_fetch_one_site)
    merged = await _fetch_from_all_sites(browser=None, make="Volkswagen", model="Golf", max_results=40)

    ad_ids = {c.url for c in merged}
    assert len(merged) == 2  # not 3 -- the shared ad counted once
    assert shared.url in ad_ids
    assert only_on_french_site.url in ad_ids


@pytest.mark.asyncio
async def test_merge_survives_one_site_failing(monkeypatch):
    ok_comp = Comp(title="ok", url="https://www.2dehands.be/v/auto-s/x/m1-ok", price_eur=9000)

    async def fake_fetch_one_site(browser, site, make, model, max_results):
        if site.name == "2dehands.be":
            return {"1": ok_comp}
        return {}  # e.g. the second site was down or its markup broke

    monkeypatch.setattr(comps_2dehands, "_fetch_one_site", fake_fetch_one_site)
    merged = await _fetch_from_all_sites(browser=None, make="Volkswagen", model="Golf", max_results=40)

    assert len(merged) == 1
    assert merged[0].url == ok_comp.url
