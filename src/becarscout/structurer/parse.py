"""Pure, deterministic parsing functions for stage [2]. Every function here
takes raw text and returns a normalized value or None — no LLM, no
guessing beyond what a regex/keyword match can justify. Kept as standalone
functions (rather than methods) so each is easy to unit-test and reason
about in isolation.
"""

from __future__ import annotations

import re
from datetime import date

from .makes import MAKES

_CURRENT_YEAR_UPPER_BOUND = 2027  # generous cap; tightened over time as needed

_YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-2]\d)(?!\d)")

# "209mille km" / "209 mille km" (French "thousand") — tried first since it's
# an unambiguous odometer phrasing, unlike the plain digit+km patterns below
# which will happily match an unrelated number elsewhere in the text (a
# repair anecdote, Facebook's own boilerplate) before ever reaching this.
_MILEAGE_MILLE_RE = re.compile(r"(\d{1,3})\s*mille\s*km\b", re.IGNORECASE)
_MILEAGE_AFTER_RE = re.compile(r"(\d[\d.,]{2,7})\s*km\b", re.IGNORECASE)
# Gap between "km" and the number is deliberately restricted to spaces/tabs
# (no newlines) — an unrestricted `\s+` here previously bridged Facebook's
# own "within a 64 km radius" UI text to an unrelated sidebar listing's
# price several lines below it, extracting that price as the mileage.
_MILEAGE_BEFORE_RE = re.compile(r"\bkm[ \t:]{0,3}(\d[\d.,]{2,7})\b", re.IGNORECASE)

_MIN_PLAUSIBLE_USED_CAR_MILEAGE_KM = 1_000
"""Below this, a mileage reading on a car that isn't brand-new this model
year is almost certainly a mis-extraction (a nearby unrelated number in
free text or page boilerplate) rather than a genuinely near-zero odometer
-- see `sanity_check_mileage`."""

_NEW_CAR_RE = re.compile(r"\b(?:0\s*km|neufs?|neuves?|nieuwe?|new)\b", re.IGNORECASE)

_FUEL_KEYWORDS: list[tuple[str, list[str]]] = [
    ("electric", ["elektrisch", "électrique", "electrique", "electric"]),
    ("hybrid", ["hybride", "hybrid", "phev"]),
    ("lpg", ["lpg", "gpl", "autogas"]),
    ("diesel", ["diesel", "gasoil", "gazole", "mazout", "tdi", "hdi", "dci", "cdti"]),
    ("petrol", ["essence", "benzine", "petrol", "gasoline", "benzin"]),
]

_TRANSMISSION_KEYWORDS: list[tuple[str, list[str]]] = [
    ("automatic", ["automatique", "automaat", "automatic", "dsg", "tiptronic", "cvt"]),
    ("manual", ["manuelle", "manuele", "manueel", "manual", "handgeschakeld"]),
]

_AGE_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}
_AGE_RELATIVE_RE = re.compile(r"\b(a|an|\d+)\s+(minute|hour|day|week|month|year)s?\s+ago\b")

_MAKE_PATTERNS = [(make, re.compile(rf"\b{re.escape(make)}\b", re.IGNORECASE)) for make in MAKES]


def parse_price(price_text: str | None) -> tuple[int | None, bool]:
    """Returns (price_eur, is_free)."""
    if not price_text:
        return None, False
    text = price_text.strip().lower()
    if text == "free":
        return 0, True
    digits = re.sub(r"[^\d]", "", price_text)
    return (int(digits), False) if digits else (None, False)


def parse_year(*texts: str | None) -> int | None:
    for text in texts:
        if not text:
            continue
        for match in _YEAR_RE.finditer(text):
            year = int(match.group(1))
            if year <= _CURRENT_YEAR_UPPER_BOUND:
                return year
    return None


def parse_mileage_km(*texts: str | None) -> int | None:
    for text in texts:
        if not text:
            continue
        mille_match = _MILEAGE_MILLE_RE.search(text)
        if mille_match:
            km = int(mille_match.group(1)) * 1000
            if 10 <= km <= 1_000_000:
                return km
        match = _MILEAGE_AFTER_RE.search(text) or _MILEAGE_BEFORE_RE.search(text)
        if not match:
            continue
        digits = re.sub(r"[^\d]", "", match.group(1))
        if not digits:
            continue
        km = int(digits)
        if 10 <= km <= 1_000_000:
            return km
    return None


def sanity_check_mileage(mileage_km: int | None, year: int | None, *texts: str | None) -> int | None:
    """Discards an implausibly-low mileage reading on a car that isn't
    genuinely brand-new this model year, rather than passing a near-certain
    mis-extraction (see the module docstring above `_MILEAGE_BEFORE_RE`)
    downstream into pricing as if it were a real near-zero odometer."""
    if mileage_km is None or mileage_km >= _MIN_PLAUSIBLE_USED_CAR_MILEAGE_KM:
        return mileage_km
    if year is not None and year >= date.today().year:
        return mileage_km
    haystack = " ".join(t for t in texts if t)
    if _NEW_CAR_RE.search(haystack):
        return mileage_km
    return None


def parse_fuel_type(*texts: str | None) -> str | None:
    haystack = " ".join(t.lower() for t in texts if t)
    for label, keywords in _FUEL_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return label
    return None


def parse_transmission(*texts: str | None) -> str | None:
    haystack = " ".join(t.lower() for t in texts if t)
    for label, keywords in _TRANSMISSION_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return label
    return None


def parse_location(location_text: str | None) -> tuple[str | None, str | None]:
    """Returns (city, region). Belgian listings are formatted "City, REGION"
    where REGION is one of VLG/WAL/BRU; falls back to (text, None) if
    there's no comma to split on."""
    if not location_text:
        return None, None
    parts = [p.strip() for p in location_text.split(",")]
    if len(parts) >= 2:
        return parts[0] or None, parts[-1] or None
    return parts[0] or None, None


def parse_listing_age_days(listed_relative_text: str | None) -> int | None:
    if not listed_relative_text:
        return None
    text = listed_relative_text.lower()
    if "just listed" in text or "today" in text:
        return 0
    if "yesterday" in text:
        return 1
    match = _AGE_RELATIVE_RE.search(text)
    if not match:
        return None
    quantity_raw, unit = match.group(1), match.group(2)
    quantity = 1 if quantity_raw in ("a", "an") else int(quantity_raw)
    return quantity * _AGE_UNIT_DAYS[unit]


def parse_make_and_model_hint(title: str, description: str | None) -> tuple[str | None, str | None]:
    """Detects a make by literal name match (title first, then
    description); model_hint is the text immediately following the make in
    the title, if the make was found there. Doesn't infer make from a bare
    model name (e.g. "Golf 6" alone won't resolve to Volkswagen) — that
    needs a maintained model catalogue, which is out of scope here."""
    for make, pattern in _MAKE_PATTERNS:
        match = pattern.search(title)
        if match:
            remainder = title[match.end():].strip(" -,:/|").strip()
            return make, (remainder or None)

    for make, pattern in _MAKE_PATTERNS:
        if description and pattern.search(description):
            return make, None

    return None, None
