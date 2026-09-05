"""Belgium-specific search configuration for the Marketplace scraper.

Facebook Marketplace search is radius-based from a single point, not
country-based, and Belgium's Marketplace inventory isn't fully covered by
any single hub + radius combo. We fan out over a handful of hub cities
(with Facebook's own location slugs) and dedupe results by listing id.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Hub:
    slug: str
    """Facebook's location slug, as used in the marketplace URL path."""
    label: str


# Chosen to spread coverage across Belgium's three regions without needing
# an unreasonable number of hubs. Radius is set generously per-hub below
# since Belgium is small enough that overlap (and dedup) is cheap.
BELGIUM_HUBS: list[Hub] = [
    Hub("brussels", "Brussels"),
    Hub("antwerp", "Antwerp"),
    Hub("ghent", "Ghent"),
    Hub("liege", "Liège"),
    Hub("charleroi", "Charleroi"),
]

# Facebook Marketplace radius options are discrete; this is the largest
# commonly available step that still lets adjacent hubs overlap for
# coverage rather than leaving gaps.
DEFAULT_RADIUS_KM = 100

CATEGORY = "vehicles"
