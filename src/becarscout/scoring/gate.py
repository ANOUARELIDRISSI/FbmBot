"""Stage [5]: Decision Gate. Deliberately trivial — a configurable score
threshold, nothing more. This is what keeps downstream delivery (stage 6)
from turning into spam; the actual judgment already happened in stage 4.
"""

from __future__ import annotations

from .models import ScoredListing

DEFAULT_THRESHOLD = 20


def apply_decision_gate(scored: list[ScoredListing], threshold: int = DEFAULT_THRESHOLD) -> list[ScoredListing]:
    """Returns the full list with `above_threshold` set on each — callers
    that only want the opportunities themselves should filter on that."""
    return [s.model_copy(update={"above_threshold": s.score >= threshold}) for s in scored]
