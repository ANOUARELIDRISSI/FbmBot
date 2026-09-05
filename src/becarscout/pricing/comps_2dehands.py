"""Scrapes comparable listings from 2dehands.be (Belgium's major classifieds
site — no login required, unlike Facebook) to ground the price baseline
used in stage 4's price-gap calculation.

Chosen after AutoScout24.be actively blocked automated access (403 on every
request, real bot-detection at the edge — not worth fighting) and Gocar.be
did the same. 2dehands.be returned clean 200s and, unlike Facebook's grid
cards, its search results already carry price/year/mileage/fuel/
transmission directly — no detail-page visit needed per comp.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import Browser, async_playwright

from .models import Comp

logger = logging.getLogger(__name__)

BASE_URL = "https://www.2dehands.be"

DEFAULT_CACHE_DIR = Path("data/pricing_cache")
DEFAULT_CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600
"""Comps prices don't shift meaningfully day to day; a week-old cache is
fine and saves hammering the site on every scoring run."""

_PRICE_RE = re.compile(r"€\s*([\d.,]+)")
_YEAR_LINE_RE = re.compile(r"^(19[5-9]\d|20[0-2]\d)$")
_MILEAGE_LINE_RE = re.compile(r"^([\d.,]+)\s*km$", re.IGNORECASE)

_FUEL_LINES = {
    "diesel": "diesel",
    "benzine": "petrol",
    "hybride elektrisch/benzine": "hybrid",
    "hybride elektrisch/diesel": "hybrid",
    "elektrisch": "electric",
    "lpg": "lpg",
}
_TRANSMISSION_LINES = {
    "automaat": "automatic",
    "handgeschakeld": "manual",
}

# Sanity floor: a used-car mileage reading below this is almost certainly a
# data-entry error on the source site (e.g. "152 km" meaning 152.000 km),
# not a genuinely near-new car — excluded rather than skewing the median.
_MIN_PLAUSIBLE_MILEAGE_KM = 500


def _parse_price(line: str) -> int | None:
    match = _PRICE_RE.search(line)
    if not match:
        return None
    # Belgian/Dutch formatting: "22.750,-" = €22,750 flat. Cut at the comma
    # (cents, always ",-" for a round amount here) then strip separators.
    whole_part = match.group(1).split(",")[0]
    digits = re.sub(r"[^\d]", "", whole_part)
    return int(digits) if digits else None


def _parse_comp_card(text: str, href: str) -> Comp | None:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    lines = [line for line in lines if line != "Bewaren in Mijn Favorieten"]
    if not lines:
        return None

    price_eur = None
    year = None
    mileage_km = None
    fuel_type = None
    transmission = None
    title = lines[0]

    for line in lines:
        if price_eur is None:
            price_eur = _parse_price(line)
            if price_eur is not None:
                continue
        if year is None and _YEAR_LINE_RE.match(line):
            year = int(line)
            continue
        if mileage_km is None:
            match = _MILEAGE_LINE_RE.match(line)
            if match:
                km = int(re.sub(r"[^\d]", "", match.group(1)))
                if km >= _MIN_PLAUSIBLE_MILEAGE_KM:
                    mileage_km = km
                continue
        lowered = line.lower()
        if fuel_type is None and lowered in _FUEL_LINES:
            fuel_type = _FUEL_LINES[lowered]
            continue
        if transmission is None and lowered in _TRANSMISSION_LINES:
            transmission = _TRANSMISSION_LINES[lowered]
            continue

    if price_eur is None:
        return None

    return Comp(
        title=title,
        url=f"{BASE_URL}{href}",
        price_eur=price_eur,
        year=year,
        mileage_km=mileage_km,
        fuel_type=fuel_type,
        transmission=transmission,
    )


async def _fetch_with_browser(browser: Browser, make: str, model: str, max_results: int) -> list[Comp]:
    query = quote(f"{make} {model}")
    url = f"{BASE_URL}/l/auto-s/q/{query}/"

    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)

        anchors = await page.query_selector_all('a[href*="/v/auto-s/"]')
        comps: dict[str, Comp] = {}
        for anchor in anchors:
            href = await anchor.get_attribute("href")
            if not href or href in comps:
                continue
            text = await anchor.inner_text()
            comp = _parse_comp_card(text, href)
            if comp:
                comps[href] = comp
            if len(comps) >= max_results:
                break

        logger.info("Fetched %d comps for %s %s", len(comps), make, model)
        return list(comps.values())
    finally:
        await page.close()


async def fetch_comps(
    make: str, model: str, *, max_results: int = 40, browser: Browser | None = None
) -> list[Comp]:
    """One search-results page fetch — no pagination, no detail visits.
    Returns whatever the first page of results carries (typically ~25-35
    genuine matches after removing duplicates). Pass an existing `browser`
    to reuse it across many calls (see `get_comps_cached`); otherwise one
    is launched and closed just for this call."""
    if browser is not None:
        return await _fetch_with_browser(browser, make, model, max_results)

    async with async_playwright() as playwright:
        owned_browser = await playwright.chromium.launch(headless=True)
        try:
            return await _fetch_with_browser(owned_browser, make, model, max_results)
        finally:
            await owned_browser.close()


def _cache_path(make: str, model: str, cache_dir: Path) -> Path:
    safe = re.sub(r"[^a-z0-9]+", "_", f"{make}_{model}".lower()).strip("_")
    return cache_dir / f"{safe}.json"


async def get_comps_cached(
    make: str,
    model: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_age_seconds: float = DEFAULT_CACHE_MAX_AGE_SECONDS,
    max_results: int = 40,
    browser: Browser | None = None,
) -> list[Comp]:
    path = _cache_path(make, model, cache_dir)
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_seconds:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [Comp.model_validate(item) for item in data]

    comps = await fetch_comps(make, model, max_results=max_results, browser=browser)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([c.model_dump(mode="json") for c in comps], ensure_ascii=False), encoding="utf-8"
    )
    return comps
