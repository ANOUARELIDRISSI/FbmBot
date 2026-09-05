"""Stage [7]'s review agent: a small LangGraph graph that turns accumulated
👍/👎 feedback (via `memory.py`'s mem0 store) into a human-readable report
of patterns plus *suggested* `scoring.py` weight tweaks.

Per the design decision behind this stage (see Project.md): the agent never
changes anything by itself. It writes a markdown report **and** a
structured list of weight suggestions; a human decides whether to act on
them — either by hand, or via the Telegram `/validate` command (see
`apply.py`), which applies exactly what this graph proposed, nothing more.
Same "LLM interprets, a human/deterministic step judges" principle stages
3-5 already follow, just applied one level up (patterns across many
verdicts, instead of facts within one description).

Two nodes gate on whether there's enough signal to say anything useful
yet (`_MIN_FEEDBACK_FOR_REVIEW`), which is the one real branch in the
graph — with only a handful of verdicts recorded, a "pattern" is just
noise dressed up as insight.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph
from mistralai.client import Mistral

from becarscout.db import get_session
from becarscout.db import repository as repo
from becarscout.scoring.models import ScoringWeights

from .memory import get_all_feedback_memories

logger = logging.getLogger(__name__)

load_dotenv()

DEFAULT_REPORT_DIR = Path(os.getenv("BECARSCOUT_FEEDBACK_REPORT_DIR", "data/feedback"))
LATEST_SUGGESTIONS_PATH = DEFAULT_REPORT_DIR / "latest_suggestions.json"
"""Overwritten by every review — `/validate` (see `apply.py`) always acts
on the *most recent* review's suggestions, not some specific past one."""

_REVIEW_MODEL = os.getenv("MISTRAL_MODEL", "ministral-8b-latest")

_MIN_FEEDBACK_FOR_REVIEW = 5
"""Below this many total verdicts, any "pattern" the LLM reports is
almost certainly noise, not signal — the graph short-circuits to a plain
"not enough data yet" result instead of fabricating suggestions."""

_WEIGHT_FIELDS = list(ScoringWeights.model_fields.keys())

_WEIGHT_MEANINGS = """\
- gearbox_issue_likely_major / gearbox_issue_minor / gearbox_issue_unknown: point penalty \
by severity when the description mentions a gearbox issue.
- engine_issue_likely_major / engine_issue_minor / engine_issue_unknown: same, for engine issues.
- accident_damage: flat penalty when accident/body damage is mentioned.
- warning_light_needs_diagnostic / warning_light_only: penalty when a dashboard warning \
light is mentioned, split by whether it needs a diagnostic.
- timing_belt_replaced: flat bonus when a timing belt replacement is mentioned.
- inspection_valid / inspection_invalid: bonus/penalty for BE roadworthiness inspection \
(keuring/contrôle technique) status.
- service_history_complete / service_history_none: bonus/penalty for service history completeness.
- for_export: penalty when the listing is marked for export.
- min_plausible_car_price_eur: a sanity floor below which a price is assumed to be a \
placeholder/parts listing, not a real asking price — only suggest changing this if liked/\
disliked cars show genuine placeholder pricing near the current floor, not as a way to \
express a preferred price range.\
"""

_SYSTEM_PROMPT = f"""You are a review assistant for a personal used-car deal-finder. \
You're given a list of cars the user marked "liked" (👍) and "disliked" (👎) after seeing \
them as scored opportunities, plus the current value of every scoring weight you're allowed \
to suggest changing. Your job is only to describe patterns and suggest possible scoring \
adjustments — you never decide anything; a human reviews your suggestions and chooses \
whether to apply them.

Only suggest changes to weights from this list, using their actual meaning below — never \
invent a new weight name or repurpose one for something it doesn't control:
{_WEIGHT_MEANINGS}

For each suggestion, use the CURRENT VALUE given to you in the user message as
`current_value` — never guess or assume it. Only propose a change you have reasonable
confidence in from the actual liked/disliked data; if nothing supports a specific change,
return an empty suggestions list rather than guessing. List 2-5 patterns you actually
observed in `patterns_observed`; if nothing meaningful stands out, say so plainly there
instead of inventing one.
"""

_SUGGESTION_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "patterns_observed": {"type": "array", "items": {"type": "string"}},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "weight": {"type": "string", "enum": _WEIGHT_FIELDS},
                    "current_value": {"type": "integer"},
                    "new_value": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["weight", "current_value", "new_value", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["patterns_observed", "suggestions"],
    "additionalProperties": False,
}


def _current_weights_block() -> str:
    """Reads the actual current values straight from the DB-backed
    `ScoringWeights` (see `db/repository.get_scoring_weights`) rather than
    letting the LLM guess/hallucinate them — a suggestion like "raise X
    from -10 to -20" is useless (or misleading) if -10 was never the real
    current value. Reads live values, not hardcoded defaults, so a
    previously-applied `/validate` is correctly reflected here too."""
    session = get_session()
    try:
        weights = repo.get_scoring_weights(session)
    finally:
        session.close()
    return "\n".join(f"{name} = {value}" for name, value in weights.model_dump().items())


class ReviewState(TypedDict, total=False):
    memories: list[dict]
    liked: list[dict]
    disliked: list[dict]
    report_markdown: str
    report_path: str
    suggestions: list[dict]
    skipped: bool
    skip_reason: str


def _get_mistral_client() -> Mistral:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY not set — add it to your .env file.")
    return Mistral(api_key=api_key)


def _load_memories(_state: ReviewState) -> ReviewState:
    memories = get_all_feedback_memories()
    liked = [m for m in memories if m.get("metadata", {}).get("verdict") == "up"]
    disliked = [m for m in memories if m.get("metadata", {}).get("verdict") == "down"]
    return {"memories": memories, "liked": liked, "disliked": disliked}


def _route_on_feedback_volume(state: ReviewState) -> str:
    return "synthesize" if len(state.get("memories", [])) >= _MIN_FEEDBACK_FOR_REVIEW else "not_enough_data"


def _not_enough_data(state: ReviewState) -> ReviewState:
    n = len(state.get("memories", []))
    # No emoji here (even though this is a thumbs-up/down feedback report) —
    # this string reaches a plain `print()` in cli.py, and Windows consoles
    # using the cp1252 codepage crash on emoji the same way argparse's
    # --help text did earlier in this project (see Project.md).
    reason = (
        f"Only {n} feedback verdict(s) recorded so far (need {_MIN_FEEDBACK_FOR_REVIEW}+) "
        "— not enough signal yet for a meaningful review. Keep using the thumbs up/down "
        "buttons on Telegram cards and run this again later."
    )
    logger.info(reason)
    return {"skipped": True, "skip_reason": reason}


def _synthesize(state: ReviewState) -> ReviewState:
    liked_lines = "\n".join(f"- {m['memory']}" for m in state["liked"]) or "(none)"
    disliked_lines = "\n".join(f"- {m['memory']}" for m in state["disliked"]) or "(none)"
    user_prompt = (
        f"Current scoring weights:\n{_current_weights_block()}\n\n"
        f"Liked cars:\n{liked_lines}\n\nDisliked cars:\n{disliked_lines}"
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
            "json_schema": {"name": "feedback_review", "schema": _SUGGESTION_JSON_SCHEMA, "strict": True},
        },
    )
    parsed = json.loads(response.choices[0].message.content)
    suggestions = parsed.get("suggestions", [])

    report_lines = ["## Patterns observed"]
    report_lines.extend(f"- {p}" for p in parsed.get("patterns_observed", []))
    report_lines.append("")
    report_lines.append("## Suggested scoring adjustments")
    if suggestions:
        for s in suggestions:
            report_lines.append(f"- `{s['weight']}`: {s['current_value']} -> {s['new_value']} ({s['reason']})")
        report_lines.append("")
        report_lines.append("Send /validate on Telegram to apply these, or leave them and they'll be replaced by the next review.")
    else:
        report_lines.append("No confident suggestions yet.")

    return {"report_markdown": "\n".join(report_lines), "suggestions": suggestions}


def _write_report(state: ReviewState) -> ReviewState:
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = DEFAULT_REPORT_DIR / f"review_{timestamp}.md"
    path.write_text(state["report_markdown"], encoding="utf-8")

    # Always overwritten -- /validate acts on the latest review only.
    LATEST_SUGGESTIONS_PATH.write_text(
        json.dumps(state.get("suggestions", []), ensure_ascii=False), encoding="utf-8"
    )

    logger.info("Feedback review written to %s (%d suggestion(s))", path, len(state.get("suggestions", [])))
    return {"report_path": str(path)}


def build_feedback_review_graph():
    graph = StateGraph(ReviewState)
    graph.add_node("load_memories", _load_memories)
    graph.add_node("synthesize", _synthesize)
    graph.add_node("write_report", _write_report)
    graph.add_node("not_enough_data", _not_enough_data)

    graph.set_entry_point("load_memories")
    graph.add_conditional_edges(
        "load_memories",
        _route_on_feedback_volume,
        {"synthesize": "synthesize", "not_enough_data": "not_enough_data"},
    )
    graph.add_edge("synthesize", "write_report")
    graph.add_edge("write_report", END)
    graph.add_edge("not_enough_data", END)
    return graph.compile()


def run_feedback_review() -> ReviewState:
    """Entry point for `becarscout feedback-review` (and the Telegram
    `/reviewfeedback` command). Produces a markdown report plus a
    structured suggestion list saved to `LATEST_SUGGESTIONS_PATH` — see
    `apply.py`'s `apply_latest_suggestions` for the "/validate" step that
    actually applies them. Never touches scoring itself on its own."""
    app = build_feedback_review_graph()
    return app.invoke({})
