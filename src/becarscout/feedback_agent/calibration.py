"""Stage [7] companion, added alongside Telegram's `/showall` review flow:
turns explicit user-given score corrections into the same kind of
structured weight-change suggestions `graph.py`'s feedback review produces
from liked/disliked patterns -- just from a more direct signal (an actual
target score per listing, given by hand, instead of an inferred pattern
across many up/down verdicts). Reuses `graph.py`'s weight enum/schema/
prompt scaffolding rather than duplicating it, and writes to the exact
same `PendingSuggestionRow` `/validate` already reads from, so applying a
calibration's suggestions works identically to applying a feedback
review's -- same "the agent proposes, a human decides" principle, just a
second, stronger source of evidence for it to propose from.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from becarscout.db import get_session
from becarscout.db import repository as repo

from .graph import (
    DEFAULT_REPORT_DIR,
    _REVIEW_MODEL,
    _SUGGESTION_JSON_SCHEMA,
    _WEIGHT_MEANINGS,
    _current_weights_block,
    _get_mistral_client,
)

logger = logging.getLogger(__name__)

_MIN_CORRECTIONS_FOR_CALIBRATION = 3
"""Lower than feedback review's floor of 5 (`graph._MIN_FEEDBACK_FOR_REVIEW`)
-- a /showall correction is a direct target score, a much stronger signal
per data point than an implicit up/down verdict, so fewer of them are
still meaningful."""

_SYSTEM_PROMPT = f"""You are a review assistant for a personal used-car deal-finder's \
rule-based scoring engine. You're given a list of cars the engine scored, each with the \
score it actually gave, the point-by-point reasoning behind that score, and the score the \
user says it should have gotten instead. Your job is to suggest scoring weight changes that \
would close that gap -- you never decide anything; a human reviews your suggestions and \
chooses whether to apply them.

Only suggest changes to weights from this list, using their actual meaning below -- never \
invent a new weight name or repurpose one for something it doesn't control:
{_WEIGHT_MEANINGS}

For each suggestion, use the CURRENT VALUE given to you in the user message as
`current_value` -- never guess or assume it. Base every suggestion on a weight that actually
appears in that listing's own reasoning trail below -- don't propose changing a weight that
had nothing to do with the gap for any of the listings given. Only propose a change you have
reasonable confidence in from the actual corrections; if nothing supports a specific change,
return an empty suggestions list rather than guessing. List 2-5 patterns you actually
observed in `patterns_observed`; if nothing meaningful stands out, say so plainly there
instead of inventing one.
"""


def _corrections_block(corrections: list[dict]) -> str:
    lines = []
    for c in corrections:
        gap = c["user_score"] - c["model_score"]
        reasoning = "; ".join(c["reasoning"]) or "(none)"
        lines.append(
            f"- {c['title']} -- engine scored {c['model_score']:+d}, user says it should be "
            f"{c['user_score']:+d} (gap {gap:+d})\n  Reasoning: {reasoning}"
        )
    return "\n".join(lines)


def run_score_calibration(chat_id: int, corrections: list[dict]) -> dict:
    """`corrections`: one dict per listing reviewed via `/showall`, each
    with `listing_id`, `title`, `model_score`, `user_score`, and
    `reasoning` (that listing's own reasoning trail -- the same strings
    the "Why?" button shows). Returns a dict shaped like
    `graph.run_feedback_review`'s result (`report_markdown` /
    `suggestions`, or `skipped` / `skip_reason`) so the Telegram side can
    treat both the same way."""
    if len(corrections) < _MIN_CORRECTIONS_FOR_CALIBRATION:
        return {
            "skipped": True,
            "skip_reason": (
                f"Only {len(corrections)} correction(s) given (need "
                f"{_MIN_CORRECTIONS_FOR_CALIBRATION}+) -- not enough to suggest anything "
                "meaningful yet."
            ),
        }

    user_prompt = (
        f"Current scoring weights:\n{_current_weights_block(chat_id)}\n\n"
        f"Corrected listings:\n{_corrections_block(corrections)}"
    )
    client = _get_mistral_client()
    response = client.chat.complete(
        model=_REVIEW_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "score_calibration", "schema": _SUGGESTION_JSON_SCHEMA, "strict": True},
        },
    )
    parsed = json.loads(response.choices[0].message.content)
    suggestions = parsed.get("suggestions", [])

    report_lines = [f"## Reviewed {len(corrections)} listing(s)", "", "## Patterns observed"]
    report_lines.extend(f"- {p}" for p in parsed.get("patterns_observed", []))
    report_lines.append("")
    report_lines.append("## Suggested scoring adjustments")
    if suggestions:
        for s in suggestions:
            report_lines.append(f"- `{s['weight']}`: {s['current_value']} -> {s['new_value']} ({s['reason']})")
        report_lines.append("")
        report_lines.append("Send /validate on Telegram to apply these, or leave them and they'll be replaced next time.")
    else:
        report_lines.append("No confident suggestions yet.")
    report_markdown = "\n".join(report_lines)

    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = DEFAULT_REPORT_DIR / f"calibration_{chat_id}_{timestamp}.md"
    path.write_text(report_markdown, encoding="utf-8")

    session = get_session()
    try:
        repo.save_pending_suggestions(session, chat_id, suggestions)
    finally:
        session.close()

    logger.info("Score calibration written to %s (%d suggestion(s))", path, len(suggestions))
    return {"report_markdown": report_markdown, "suggestions": suggestions, "report_path": str(path)}
