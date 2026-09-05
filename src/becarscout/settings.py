"""User-adjustable pipeline parameters — search radius, price range, the
year/score gates. Its own module (rather than living inside `cli.py` or
`scoring/`) since it's shared by the CLI (`becarscout run` reads this as
its defaults) and the Telegram bot (`/budget`, `/minyear`, `/threshold`,
`/radius`, `/settings` change it live, without a redeploy). Persisted in
`db/models.py`'s `PipelineSettingsRow` singleton — this pydantic model is
what callers actually pass around; see `db/repository.py`'s
`get_pipeline_settings`/`update_pipeline_settings`.
"""

from __future__ import annotations

from pydantic import BaseModel


class PipelineSettings(BaseModel):
    radius_km: int = 100
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
