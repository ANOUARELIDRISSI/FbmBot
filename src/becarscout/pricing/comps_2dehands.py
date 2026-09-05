"""Scrapes comparable listings to ground the price baseline used in stage
4's price-gap calculation. Queries two sites and merges the results:

- **2dehands.be** (Dutch-language) — Belgium's major classifieds site, no
  login required. Chosen after AutoScout24.be and Gocar.be both actively
  blocked automated access (403 on every request, real bot-detection at
  the edge — not worth fighting).
- **2ememain.be** (French-language) — added for coverage. Important
  honesty check done before wiring this in: inspecting live ad IDs proved
  these two sites are **the same underlying database** (Adevinta Belgium
  runs both as a bilingual front-end over one shared inventory — the exact
  same numeric ad id, e.g. "m2437911744", turns up on both for the same
  car). So this is genuinely *not* an independent second source for price
  cross-validation — querying both just surfaces a few extra ads that rank
  differently per language's search index, deduped by that shared ad id.
  It's a real, if modest, coverage improvement (more comps per query,
  which directly helps `baseline.py`'s empirical-Bayes shrinkage for
  thin comp pools) — not two-source validation. A genuinely independent
  second source would need AutoScout24.be/Gocar.be to stop blocking, or a
  different site entirely; neither has been re-attempted.

Both sites carry price/year/mileage/fuel/transmission directly in the
search grid — no detail-page visit needed per comp, unlike the Facebook
scraper.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import Browser, async_playwright

from .models import Comp

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data/pricing_cache")
DEFAULT_CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600
"""Comps prices don't shift meaningfully day to day; a week-old cache is
fine and saves hammering both sites on every scoring run."""

_PRICE_RE = re.compile(r"€\s*([\d.,]+)")
_YEAR_LINE_RE = re.compile(r"^(19[5-9]\d|20[0-2]\d)$")
_MILEAGE_LINE_RE = re.compile(r"^([\d.,]+)\s*km$", re.IGNORECASE)
_AD_ID_RE = re.compile(r"/m(\d+)-")
"""Both sites' URLs are "/v/<auto-s|autos>/<make>/m<digits>-<slug>" — this
digit id is the same across both sites for the same underlying ad (see
module docstring), so it's what dedup keys on, not the full URL."""

# Sanity floor: a used-car mileage reading below this is almost certainly a
# data-entry error on the source site (e.g. "152 km" meaning 152.000 km),
# not a genuinely near-new car — excluded rather than skewing the median.
_MIN_PLAUSIBLE_MILEAGE_KM = 500


@dataclass(frozen=True)
class _SiteConfig:
    name: str
    base_url: str
    search_path: str  # {query} placeholder
    href_fragment: str  # substring identifying a result anchor's href
    fuel_lines: dict[str, str]
    transmission_lines: dict[str, str]
    favorite_button_text: str
    """Every result card repeats a "save to favorites" button whose text
    ends up as a line in the anchor's innerText — filtered out before
    parsing so it's never mistaken for a data field."""


_SITES = [
    _SiteConfig(
        name="2dehands.be",
        base_url="https://www.2dehands.be",
        search_path="/l/auto-s/q/{query}/",
        href_fragment="/v/auto-s/",
        fuel_lines={
            "diesel": "diesel",
            "benzine": "petrol",
            "hybride elektrisch/benzine": "hybrid",
            "hybride elektrisch/diesel": "hybrid",
            "elektrisch": "electric",
            "lpg": "lpg",
        },
        transmission_lines={"automaat": "automatic", "handgeschakeld": "manual"},
        favorite_button_text="Bewaren in Mijn Favorieten",
    ),
    _SiteConfig(
        name="2ememain.be",
        base_url="https://www.2ememain.be",
        search_path="/l/autos/q/{query}/",
        href_fragment="/v/autos/",
        fuel_lines={
            "diesel": "diesel",
            "essence": "petrol",
            "hybride électrique/essence": "hybrid",
            "hybride électrique/diesel": "hybrid",
            "électrique": "electric",
            "lpg": "lpg",
        },
        transmission_lines={"automatique": "automatic", "boîte manuelle": "manual"},
        favorite_button_text="Sauvegarder dans Mes Favoris",
    ),
]


def _parse_price(line: str) -> int | None:
    match = _PRICE_RE.search(line)
    if not match:
        return None
    # Belgian formatting (both languages): "22.750,-" = €22,750 flat. Cut
    # at the comma (cents, always ",-" for a round amount here) then strip
    # separators.
    whole_part = match.group(1).split(",")[0]
    digits = re.sub(r"[^\d]", "", whole_part)
    return int(digits) if digits else None


def _ad_id(href: str) -> str | None:
    match = _AD_ID_RE.search(href)
    return match.group(1) if match else None


def _parse_comp_card(text: str, href: str, site: _SiteConfig) -> Comp | None:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    lines = [line for line in lines if line != site.favorite_button_text]
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
        if fuel_type is None and lowered in site.fuel_lines:
            fuel_type = site.fuel_lines[lowered]
            continue
        if transmission is None and lowered in site.transmission_lines:
            transmission = site.transmission_lines[lowered]
            continue

    if price_eur is None:
        return None

    return Comp(
        title=title,
        url=f"{site.base_url}{href}",
        price_eur=price_eur,
        year=year,
        mileage_km=mileage_km,
        fuel_type=fuel_type,
        transmission=transmission,
    )


async def _fetch_one_site(browser: Browser, site: _SiteConfig, make: str, model: str, max_results: int) -> dict[str, Comp]:
    """Returns {ad_id: Comp} rather than a list, so the caller can merge
    multiple sites' results by ad id without a separate dedup pass."""
    query = quote(f"{make} {model}")
    url = f"{site.base_url}{site.search_path.format(query=query)}"

    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)

        anchors = await page.query_selector_all(f'a[href*="{site.href_fragment}"]')
        comps: dict[str, Comp] = {}
        for anchor in anchors:
            href = await anchor.get_attribute("href")
            ad_id = _ad_id(href) if href else None
            if not href or not ad_id or ad_id in comps:
                continue
            text = await anchor.inner_text()
            comp = _parse_comp_card(text, href, site)
            if comp:
                comps[ad_id] = comp
            if len(comps) >= max_results:
                break

        logger.info("Fetched %d comps for %s %s from %s", len(comps), make, model, site.name)
        return comps
    except Exception:
        logger.exception("Comps fetch failed for %s %s on %s", make, model, site.name)
        return {}
    finally:
        await page.close()


async def fetch_comps(
    make: str, model: str, *, max_results: int = 40, browser: Browser | None = None
) -> list[Comp]:
    """One search-results page fetch per site, merged and deduped by ad id
    (see module docstring — 2dehands.be and 2ememain.be share one
    underlying inventory, so this is a coverage merge, not independent
    cross-validation). A single site failing (blocked, markup changed)
    doesn't sink the other — whatever it returns is used as-is. Pass an
    existing `browser` to reuse it across many calls (see
    `get_comps_cached`); otherwise one is launched and closed just for
    this call."""
    if browser is not None:
        return await _fetch_from_all_sites(browser, make, model, max_results)

    async with async_playwright() as playwright:
        owned_browser = await playwright.chromium.launch(headless=True)
        try:
            return await _fetch_from_all_sites(owned_browser, make, model, max_results)
        finally:
            await owned_browser.close()


async def _fetch_from_all_sites(browser: Browser, make: str, model: str, max_results: int) -> list[Comp]:
    merged: dict[str, Comp] = {}
    for site in _SITES:
        site_comps = await _fetch_one_site(browser, site, make, model, max_results)
        for ad_id, comp in site_comps.items():
            merged.setdefault(ad_id, comp)  # first site wins on a shared ad
        if len(merged) >= max_results:
            break
    return list(merged.values())[:max_results]


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
