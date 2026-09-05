"""Stage [5]: Decision Gate. Deliberately trivial — a configurable score
threshold, nothing more. This is what keeps downstream delivery (stage 6)
from turning into spam; the actual judgment already happened in stage 4.
"""

from __future__ import annotations

from .models import ScoredListing

DEFAULT_THRESHOLD = 20
DEFAULT_MIN_YEAR = 2010
"""Cars from this year or older never clear the gate, even with a great
score -- an explicit preference, not a scoring judgment (an old car can
still be a great price-vs-market deal; this just isn't what should land
in Telegram). Overridable via `--min-year`; pass None to turn it off."""


def _parse_csv_filter(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def apply_decision_gate(
    scored: list[ScoredListing],
    threshold: int = DEFAULT_THRESHOLD,
    min_year: int | None = DEFAULT_MIN_YEAR,
    max_mileage_km: int | None = None,
    makes: str | None = None,
    fuel_types: str | None = None,
    transmission: str | None = None,
) -> list[ScoredListing]:
    """Returns the full list with `above_threshold` set on each — callers
    that only want the opportunities themselves should filter on that.

    `min_year` and `max_mileage_km` are soft ceilings: a listing missing
    that field still passes, since stage 2 misses year/mileage on plenty
    of genuine listings (Project.md's stage 2 limitations) and punishing
    an extraction gap isn't the intent — the gate is meant to express "not
    older/higher-mileage than this", not "must have this field detected".

    `makes` and `fuel_types` (comma-separated, e.g. "bmw,toyota") and
    `transmission` are different in kind: an explicit "only show me X"
    request. Here a listing with the field undetected does *not* pass —
    letting every make-unknown listing through (roughly 64% of them, see
    Project.md) would make the filter nearly meaningless, unlike the soft
    ceilings above where leniency is the whole point."""

    make_set = _parse_csv_filter(makes)
    fuel_set = _parse_csv_filter(fuel_types)
    transmission_norm = transmission.strip().lower() if transmission else None

    def passes(s: ScoredListing) -> bool:
        if s.score < threshold:
            return False
        if min_year is not None and s.year is not None and s.year <= min_year:
            return False
        if max_mileage_km is not None and s.mileage_km is not None and s.mileage_km > max_mileage_km:
            return False
        if make_set is not None and (s.make is None or s.make.lower() not in make_set):
            return False
        if fuel_set is not None and (s.fuel_type is None or s.fuel_type.lower() not in fuel_set):
            return False
        if transmission_norm is not None and (s.transmission is None or s.transmission.lower() != transmission_norm):
            return False
        return True

    return [s.model_copy(update={"above_threshold": passes(s)}) for s in scored]
