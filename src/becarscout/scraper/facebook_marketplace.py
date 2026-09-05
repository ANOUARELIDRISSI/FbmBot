"""Stage [1] scraper: pulls raw car listings off Facebook Marketplace for
Belgium. Outputs `RawListing`s only — title, price, location, description,
photos, url. No normalization or interpretation; that's stages 2 and 3.

The search grid only exposes price + location per card (Facebook dropped
the title from grid cards at some point) so it's used purely to discover
listing links. Every listing is then visited individually for the real
title, price, location, description and photos — trafilatura handles
stripping Facebook's nav/sidebar/"today's picks" boilerplate out of the
rendered page, which is far more robust than hand-picking selectors for a
DOM that's mostly hashed, auto-generated class names.

Facebook's markup is unstable and has no public API for this, so what
selectors remain lean on structural/semantic hooks (link hrefs, image alt
text, the fact that the real title is the last <h1> on the page) rather
than CSS class names. Expect this file to need upkeep as Facebook ships
changes.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

import trafilatura
from playwright.async_api import BrowserContext, Page, async_playwright

from .belgium import BELGIUM_HUBS, CATEGORY, DEFAULT_RADIUS_KM, Hub
from .models import RawListing
from .session import DEFAULT_PROFILE_DIR, has_saved_session

logger = logging.getLogger(__name__)

BASE_URL = "https://www.facebook.com"
ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")

# Restricts the vehicles category to cars/trucks, excluding motorcycles,
# boats, powersports, etc. Facebook may change this param's accepted
# values over time.
CAR_VEHICLE_TYPE = "car_truck"

# Text fragments trafilatura leaves behind from Facebook's own UI chrome
# (buttons, section headings, translate prompts) that aren't part of what
# the seller actually wrote.
_DESCRIPTION_HEADING = "Seller's description"
_DESCRIPTION_END_MARKERS = ("Seller information", "Seller details")
_DESCRIPTION_NOISE_LINES = {"See less", "See more", "See translation"}

# Facebook's `vehicleType=car_truck` query param doesn't reliably exclude
# other vehicle categories (observed boats and motorcycles leaking into
# results), and most listings don't fill in structured fields (mileage,
# transmission, ...) that could otherwise be used to distinguish a real
# car listing. This keyword check on the title is a blunt but auditable
# backstop — covers BE-NL/FR terms for the other vehicle categories.
_NON_CAR_TITLE_KEYWORDS = (
    "bateau", "boat", "yacht", "jet ski", "jetski",
    "moto", "motorcycle", "scooter", "quad", "atv",
    "camping-car", "camping car", "camper", "caravane", "caravan",
    "remorque", "trailer", "aanhangwagen",
    "vélo", "velo", "fiets", "bike", "bicycle",
    "tracteur", "tractor",
    # Brand names that are unambiguously motorcycles/boats in a vehicles-
    # category listing (unlike e.g. "Suzuki" or "Honda", which also make
    # cars, so aren't safe to blanket-exclude on brand alone).
    "kawasaki", "ducati", "harley-davidson", "harley davidson", "triumph",
    "ktm", "aprilia", "husqvarna", "moto guzzi", "royal enfield", "benelli",
    "cfmoto", "glastron", "quicksilver", "zodiac", "beneteau", "bénéteau",
)


def _is_non_car_vehicle(title: str) -> bool:
    haystack = title.lower()
    return any(keyword in haystack for keyword in _NON_CAR_TITLE_KEYWORDS)


def _search_url(hub: Hub, *, radius_km: int, min_price: int | None, max_price: int | None) -> str:
    params: dict[str, str] = {
        "exact": "false",
        "radius_km": str(radius_km),
        "vehicleType": CAR_VEHICLE_TYPE,
    }
    if min_price is not None:
        params["minPrice"] = str(min_price)
    if max_price is not None:
        params["maxPrice"] = str(max_price)
    return f"{BASE_URL}/marketplace/{hub.slug}/{CATEGORY}?{urlencode(params)}"


async def _scroll_to_load(page: Page, *, max_scrolls: int, wait_ms: int = 1200) -> None:
    previous_height = 0
    for _ in range(max_scrolls):
        height = await page.evaluate("document.body.scrollHeight")
        if height == previous_height:
            break
        previous_height = height
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(wait_ms)


async def _extract_cards(page: Page, hub: Hub) -> list[RawListing]:
    """Grid pass: mainly link discovery, plus a best-effort price/location
    fallback in case a listing's detail fetch fails later."""
    anchors = await page.query_selector_all('a[href*="/marketplace/item/"]')
    listings: dict[str, RawListing] = {}

    for anchor in anchors:
        href = await anchor.get_attribute("href")
        if not href:
            continue
        match = ITEM_ID_RE.search(href)
        if not match:
            continue
        listing_id = match.group(1)
        if listing_id in listings:
            continue

        text = await anchor.inner_text()
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        # Card text is a badge line or two (e.g. "Just listed") followed by
        # price, then location — never a title.
        price_text = lines[-2] if len(lines) >= 2 else (lines[0] if lines else None)
        location_text = lines[-1] if lines else None

        thumb = await anchor.query_selector("img")
        photo_urls = []
        if thumb:
            src = await thumb.get_attribute("src")
            if src:
                photo_urls.append(src)

        listings[listing_id] = RawListing(
            listing_id=listing_id,
            url=f"{BASE_URL}/marketplace/item/{listing_id}/",
            title="",
            price_text=price_text,
            location_text=location_text,
            photo_urls=photo_urls,
            search_hub=hub.slug,
        )

    return list(listings.values())


async def search_hub(
    context: BrowserContext,
    hub: Hub,
    *,
    radius_km: int = DEFAULT_RADIUS_KM,
    min_price: int | None = None,
    max_price: int | None = None,
    max_scrolls: int = 8,
) -> list[RawListing]:
    page = await context.new_page()
    try:
        url = _search_url(hub, radius_km=radius_km, min_price=min_price, max_price=max_price)
        logger.info("Searching %s: %s", hub.label, url)
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await _scroll_to_load(page, max_scrolls=max_scrolls)
        return await _extract_cards(page, hub)
    finally:
        await page.close()


def _clean_description(raw: str | None) -> str | None:
    """Trims the Facebook-UI fragments (buttons, headings, seller bio)
    that survive trafilatura's boilerplate removal, keeping only the text
    between the "Seller's description" heading and the seller-info block."""
    if not raw:
        return None

    lines = [line.strip() for line in raw.split("\n") if line.strip()]

    start = 0
    for i, line in enumerate(lines):
        if line == _DESCRIPTION_HEADING:
            start = i + 1
            break

    end = len(lines)
    for marker in _DESCRIPTION_END_MARKERS:
        try:
            end = min(end, lines.index(marker, start))
        except ValueError:
            continue

    body_lines = [
        line
        for line in lines[start:end]
        if line not in _DESCRIPTION_NOISE_LINES and "Location is approximate" not in line
    ]
    body = "\n".join(body_lines).strip()
    return body or None


async def _extract_title_price_location(
    page: Page,
) -> tuple[str | None, str | None, str | None, str | None]:
    """The real title is the last <h1> on the page (the first is generic
    chrome, e.g. "Notifications"). Price and location are the two spans
    immediately following the title's text in document order; the location
    span is typically "Listed <relative time> in <place>"."""
    h1s = await page.query_selector_all("h1")
    if not h1s:
        return None, None, None, None
    title = (await h1s[-1].inner_text()).strip()

    spans = await page.query_selector_all('span[dir="auto"]')
    texts = []
    for sp in spans:
        t = (await sp.inner_text()).strip()
        if t:
            texts.append(t)
    price, location, listed_relative = None, None, None
    if title in texts:
        idx = texts.index(title)
        if idx + 1 < len(texts):
            price = texts[idx + 1]
        if idx + 2 < len(texts):
            loc_line = texts[idx + 2]
            match = re.match(r"^(.*?)\s+in\s+([^·]+)$", loc_line)
            if match:
                listed_relative = match.group(1).strip()
                location = match.group(2).strip()
            else:
                location = loc_line

    return title, price, location, listed_relative


async def fetch_listing_detail(context: BrowserContext, listing: RawListing) -> RawListing:
    """Visits the listing's own page for the authoritative title, price,
    location, description, and photos — the grid card can't be trusted
    for any of these beyond a rough price/location fallback."""
    page = await context.new_page()
    try:
        await page.goto(listing.url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        see_more = await page.query_selector('div[role="button"]:has-text("See more")')
        if see_more:
            await see_more.click()
            await page.wait_for_timeout(300)

        title, price_text, location_text, listed_relative_text = await _extract_title_price_location(page)

        html = await page.content()
        extracted = trafilatura.extract(
            html, include_comments=False, include_tables=False, favor_recall=True
        )
        description = _clean_description(extracted)

        photo_els = await page.query_selector_all('img[alt]:not([alt=""])')
        photo_urls: list[str] = []
        for el in photo_els:
            src = await el.get_attribute("src")
            if src and src.startswith("http") and src not in photo_urls:
                photo_urls.append(src)

        return listing.model_copy(
            update={
                "title": title or listing.title,
                "price_text": price_text or listing.price_text,
                "location_text": location_text or listing.location_text,
                "listed_relative_text": listed_relative_text,
                "description": description,
                "photo_urls": photo_urls or listing.photo_urls,
            }
        )
    finally:
        await page.close()


async def scrape_belgium_cars(
    *,
    hubs: list[Hub] = BELGIUM_HUBS,
    radius_km: int = DEFAULT_RADIUS_KM,
    min_price: int | None = None,
    max_price: int | None = None,
    max_scrolls: int = 8,
    fetch_details: bool = True,
    headless: bool = True,
    known_ids: frozenset[str] = frozenset(),
) -> list[RawListing]:
    """`known_ids` lets a caller (the DB-backed CLI) skip the expensive
    per-listing detail-page visit for listings it has already seen in a
    previous run — critical for hourly cron, since without this every run
    would re-visit every still-live listing's page even though only the
    handful of genuinely new ones matter."""
    if not has_saved_session():
        raise RuntimeError(
            "No saved Facebook session found. Run `becarscout login` first."
        )

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(DEFAULT_PROFILE_DIR), headless=headless
        )
        try:
            all_listings: dict[str, RawListing] = {}
            for hub in hubs:
                found = await search_hub(
                    context,
                    hub,
                    radius_km=radius_km,
                    min_price=min_price,
                    max_price=max_price,
                    max_scrolls=max_scrolls,
                )
                for listing in found:
                    all_listings.setdefault(listing.listing_id, listing)

            listings = list(all_listings.values())
            logger.info("Found %d unique listings across %d hubs", len(listings), len(hubs))

            if known_ids:
                new_listings = [listing for listing in listings if listing.listing_id not in known_ids]
                logger.info(
                    "%d already known from a previous run, %d new", len(listings) - len(new_listings), len(new_listings)
                )
                listings = new_listings

            if fetch_details:
                detailed = []
                skipped = 0
                for listing in listings:
                    try:
                        detail = await fetch_listing_detail(context, listing)
                    except Exception:
                        logger.exception("Failed to fetch detail for %s", listing.url)
                        detail = listing

                    if _is_non_car_vehicle(detail.title):
                        skipped += 1
                        logger.info("Skipping non-car listing: %r (%s)", detail.title, detail.url)
                        continue
                    detailed.append(detail)

                logger.info("Kept %d cars, skipped %d non-car vehicles", len(detailed), skipped)
                listings = detailed

            return listings
        finally:
            await context.close()
