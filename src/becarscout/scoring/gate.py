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


def apply_decision_gate(
    scored: list[ScoredListing],
    threshold: int = DEFAULT_THRESHOLD,
    min_year: int | None = DEFAULT_MIN_YEAR,
) -> list[ScoredListing]:
    """Returns the full list with `above_threshold` set on each — callers
    that only want the opportunities themselves should filter on that.
    `min_year`, when set, excludes cars from that year or older even if
    they'd otherwise clear the score threshold. A listing with no
    detected year still passes: stage 2 misses the year on plenty of
    genuine listings (Project.md's stage 2 limitations), and punishing an
    extraction gap isn't the intent here."""

    def passes(s: ScoredListing) -> bool:
        if s.score < threshold:
            return False
        if min_year is not None and s.year is not None and s.year <= min_year:
            return False
        return True

    return [s.model_copy(update={"above_threshold": passes(s)}) for s in scored]
