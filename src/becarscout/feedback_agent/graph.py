"""Stage [7]'s review agent: a small LangGraph graph that turns accumulated
👍/👎 feedback (via `memory.py`'s mem0 store) into a human-readable report
of patterns plus *suggested* `scoring.py` weight tweaks — advisory only.

Per the design decision behind this stage (see Project.md): the agent never
edits `scoring.py` itself and never changes what gets sent to Telegram.
It only writes a markdown report for you to read and decide on manually —
the same "LLM interprets, a human/deterministic step judges" principle
stages 3-5 already follow, just applied one level up (patterns across many
verdicts, instead of facts within one description).

Two nodes gate on whether there's enough signal to say anything useful
yet (`_MIN_FEEDBACK_FOR_REVIEW`), which is the one real branch in the
graph — with only a handful of verdicts recorded, a "pattern" is just
noise dressed up as insight.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph
from mistralai.client import Mistral

from becarscout.scoring import scoring as scoring_constants

from .memory import get_all_feedback_memories

logger = logging.getLogger(__name__)

load_dotenv()

DEFAULT_REPORT_DIR = Path(os.getenv("BECARSCOUT_FEEDBACK_REPORT_DIR", "data/feedback"))
_REVIEW_MODEL = os.getenv("MISTRAL_MODEL", "ministral-8b-latest")

_MIN_FEEDBACK_FOR_REVIEW = 5
"""Below this many total verdicts, any "pattern" the LLM reports is
almost certainly noise, not signal — the graph short-circuits to a plain
"not enough data yet" result instead of fabricating suggestions."""

_SYSTEM_PROMPT = """You are a review assistant for a personal used-car deal-finder. \
You're given a list of cars the user marked "liked" (👍) and "disliked" (👎) after seeing \
them as scored opportunities. Your job is only to describe patterns and suggest \
possible scoring adjustments — you never decide anything, the user reviews and applies \
changes manually.

Write a short markdown report with exactly two sections:

## Patterns observed
2-5 bullet points on what the liked cars have in common and/or what the disliked cars \
have in common (price range, mileage, make, mentioned condition issues, etc). If nothing \
meaningful stands out, say so plainly instead of inventing a pattern.

## Suggested scoring.py adjustments
Only suggest changes to constants from this list, using their actual meaning below — do \
not invent new constants or repurpose one for something it doesn't control:
- _GEARBOX_ISSUE, _ENGINE_ISSUE: point penalty dict by severity (likely_major/minor/unknown) \
when the description mentions that kind of issue.
- _ACCIDENT_DAMAGE: flat penalty when accident/body damage is mentioned.
- _WARNING_LIGHT_NEEDS_DIAGNOSTIC, _WARNING_LIGHT_ONLY: penalty when a dashboard warning \
light is mentioned, split by whether it needs a diagnostic.
- _TIMING_BELT_REPLACED: flat bonus when a timing belt replacement is mentioned.
- _INSPECTION_VALID, _INSPECTION_INVALID: bonus/penalty for BE roadworthiness inspection \
(keuring/contrôle technique) status.
- _SERVICE_HISTORY_COMPLETE, _SERVICE_HISTORY_NONE: bonus/penalty for service history \
completeness.
- _FOR_EXPORT: penalty when the listing is marked for export.
- _MIN_PLAUSIBLE_CAR_PRICE_EUR: a sanity floor (currently €300) below which a price is \
assumed to be a placeholder/parts listing, not a real asking price — only suggest \
changing this if liked/disliked cars show genuine placeholder pricing near the current \
floor, not as a way to express a preferred price range.

For each suggestion, give the constant name and a concrete before/after value, using the \
CURRENT VALUES given to you in the user message as the "before" — never guess or assume a \
current value. If the data doesn't support a specific, well-reasoned change to one of these \
constants, say "No confident suggestions yet" instead of guessing.
"""

_TUNABLE_CONSTANTS = (
    "_GEARBOX_ISSUE",
    "_ENGINE_ISSUE",
    "_ACCIDENT_DAMAGE",
    "_WARNING_LIGHT_NEEDS_DIAGNOSTIC",
    "_WARNING_LIGHT_ONLY",
    "_TIMING_BELT_REPLACED",
    "_INSPECTION_VALID",
    "_INSPECTION_INVALID",
    "_SERVICE_HISTORY_COMPLETE",
    "_SERVICE_HISTORY_NONE",
    "_FOR_EXPORT",
    "_MIN_PLAUSIBLE_CAR_PRICE_EUR",
)


def _current_constants_block() -> str:
    """Reads the actual current values straight from `scoring.py` rather
    than letting the LLM guess/hallucinate them — a suggestion like
    "raise X from -10 to -20" is useless (or misleading) if -10 was never
    the real current value."""
    lines = [f"{name} = {getattr(scoring_constants, name)!r}" for name in _TUNABLE_CONSTANTS]
    return "\n".join(lines)


class ReviewState(TypedDict, total=False):
    memories: list[dict]
    liked: list[dict]
    disliked: list[dict]
    report_markdown: str
    report_path: str
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
        f"Current scoring.py values:\n{_current_constants_block()}\n\n"
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
    )
    return {"report_markdown": response.choices[0].message.content}


def _write_report(state: ReviewState) -> ReviewState:
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = DEFAULT_REPORT_DIR / f"review_{timestamp}.md"
    path.write_text(state["report_markdown"], encoding="utf-8")
    logger.info("Feedback review written to %s", path)
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
    """Entry point for `becarscout feedback-review`. Advisory only — never
    touches `scoring.py`; just produces a markdown report for you to read
    and decide whether to act on."""
    app = build_feedback_review_graph()
    return app.invoke({})
