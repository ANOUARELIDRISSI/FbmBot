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


DEFAULT_SETTINGS = PipelineSettings()
