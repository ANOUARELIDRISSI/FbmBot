"""Applies a completed feedback review's structured weight suggestions to
the live `ScoringWeights` DB row — the actual "/validate" action. Kept
separate from `graph.py` (which only ever *produces* suggestions) so the
LangGraph agent module doesn't need direct DB-repository knowledge; this
is the one place a human's approval turns into a real behavior change.
"""

from __future__ import annotations

import logging

from becarscout.db import get_session
from becarscout.db import repository as repo

logger = logging.getLogger(__name__)


def apply_latest_suggestions(chat_id: int) -> list[dict] | None:
    """Applies whatever `run_feedback_review` most recently suggested for
    this subscriber, then clears it so a repeat `/validate` doesn't
    reapply the same thing. Returns the list of changes actually applied
    (empty if the last review had none), or `None` if there's nothing
    pending — no review has run yet for this chat_id, or its suggestions
    were already applied/replaced."""
    session = get_session()
    try:
        suggestions = repo.get_pending_suggestions(session, chat_id)
        if suggestions is None:
            return None
        repo.clear_pending_suggestions(session, chat_id)
        if not suggestions:
            return []

        changes = {s["weight"]: s["new_value"] for s in suggestions}
        repo.update_scoring_weights(session, chat_id, **changes)
    finally:
        session.close()

    logger.info("Applied %d scoring weight change(s) from feedback review for chat %s: %s", len(changes), chat_id, changes)
    return suggestions
