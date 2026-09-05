from __future__ import annotations

import pytest

from becarscout.pricing import comps_2dehands
from becarscout.pricing.comps_2dehands import check_comps_source_health
from becarscout.pricing.models import Comp


def _comp(price_eur: int = 9000) -> Comp:
    return Comp(title="VW Golf", url="https://example.test/1", price_eur=price_eur)


@pytest.mark.asyncio
async def test_healthy_when_enough_canary_results(monkeypatch):
    async def fake_fetch_comps(make, model, *, max_results=10, browser=None):
        return [_comp() for _ in range(5)]

    monkeypatch.setattr(comps_2dehands, "fetch_comps", fake_fetch_comps)
    assert await check_comps_source_health() is True


@pytest.mark.asyncio
async def test_unhealthy_when_too_few_canary_results(monkeypatch):
    async def fake_fetch_comps(make, model, *, max_results=10, browser=None):
        return [_comp()]

    monkeypatch.setattr(comps_2dehands, "fetch_comps", fake_fetch_comps)
    assert await check_comps_source_health() is False


@pytest.mark.asyncio
async def test_unhealthy_when_fetch_raises(monkeypatch):
    async def fake_fetch_comps(make, model, *, max_results=10, browser=None):
        raise TimeoutError("boom")

    monkeypatch.setattr(comps_2dehands, "fetch_comps", fake_fetch_comps)
    assert await check_comps_source_health() is False
