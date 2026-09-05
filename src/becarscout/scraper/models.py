"""Raw listing data model — stage [1] output in the pipeline (see Project.md).

No normalization or interpretation happens here. Whatever the scraper can
read off the page goes in as-is; the Structurer (stage 2) is responsible for
turning this into normalized fields.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class RawListing(BaseModel):
    listing_id: str
    url: str
    title: str
    price_text: str | None = None
    location_text: str | None = None
    listed_relative_text: str | None = None
    """Facebook's own relative time phrase, e.g. "Listed a week ago"."""
    description: str | None = None
    photo_urls: list[str] = Field(default_factory=list)
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    search_hub: str | None = None
    """Which Belgian hub city's search this listing was found under."""
