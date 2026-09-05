"""Applies a completed feedback review's structured weight suggestions to
the live `ScoringWeights` DB row — the actual "/validate" action. Kept
separate from `graph.py` (which only ever *produces* suggestions) so the
LangGraph agent module doesn't need direct DB-repository knowledge; this
is the one place a human's approval turns into a real behavior change.
"""

from __future__ import annotations

import json
import logging

from becarscout.db import get_session
from becarscout.db import repository as repo

from .graph import LATEST_SUGGESTIONS_PATH

logger = logging.getLogger(__name__)


def apply_latest_suggestions() -> list[dict] | None:
    """Applies whatever `run_feedback_review` most recently suggested,
    then clears the file so a repeat `/validate` doesn't reapply the same
    thing. Returns the list of changes actually applied (empty if the
    last review had none), or `None` if there's nothing pending — no
    review has run yet, or its suggestions were already applied/replaced."""
    if not LATEST_SUGGESTIONS_PATH.exists():
        return None

    suggestions = json.loads(LATEST_SUGGESTIONS_PATH.read_text(encoding="utf-8"))
    LATEST_SUGGESTIONS_PATH.unlink()
    if not suggestions:
        return []

    changes = {s["weight"]: s["new_value"] for s in suggestions}
    session = get_session()
    try:
        repo.update_scoring_weights(session, **changes)
    finally:
        session.close()

    logger.info("Applied %d scoring weight change(s) from feedback review: %s", len(changes), changes)
    return suggestions
