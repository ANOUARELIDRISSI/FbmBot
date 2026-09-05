"""User-adjustable pipeline parameters. Split in two, as of multi-user
support (2026-09-06):

- `DEFAULT_RADIUS_KM` / `db/models.py`'s `GlobalScrapeSettingsRow` — the
  *one* shared search radius, since there's no per-listing distance figure
  to filter by after the fact; every subscriber's search uses the same
  radius. Changed via `/radius`.
- `PipelineSettings` below — everything else (budget, year, threshold,
  mileage, brand, fuel, transmission), one full set *per subscriber*,
  persisted in `db/models.py`'s `PipelineSettingsRow` (keyed by chat_id).
  Changed via `/budget`, `/minyear`, `/threshold`, `/mileage`, `/make`,
  `/fuel`, `/transmission`, `/settings`.

Its own module (rather than living inside `cli.py` or `scoring/`) since
it's shared by the CLI (`becarscout run` reads this as its defaults) and
the Telegram bot.
"""

from __future__ import annotations

from pydantic import BaseModel

DEFAULT_RADIUS_KM = 100


class PipelineSettings(BaseModel):
    min_price: int | None = None
    max_price: int | None = None
    min_year: int | None = 2010
    threshold: int = 20
    max_mileage_km: int | None = None
    makes: str | None = None
    """Comma-separated, lowercase (e.g. "bmw,toyota") -- stored as a raw
    string rather than a list since SQLite has no native array column;
    parsed back into a list only where it's actually matched against, in
    `scoring/gate.py`."""
    fuel_types: str | None = None
    """Comma-separated subset of `structurer.parse.FUEL_TYPES` (e.g.
    "diesel,hybrid"). Same storage reasoning as `makes`."""
    transmission: str | None = None
    """One of `structurer.parse.TRANSMISSIONS` ("automatic" or "manual"),
    or None for no preference."""


DEFAULT_SETTINGS = PipelineSettings()
