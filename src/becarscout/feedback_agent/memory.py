"""Stage [7]'s memory layer: mem0 turns your 👍/👎 verdicts into something
query-able by similarity, not just an append-only log. `notifier/feedback.py`
still writes the flat `data/feedback/feedback.jsonl` audit trail on every
verdict — that file stays the raw source of truth; mem0 is what makes that
data actually *usable* against a brand new listing (e.g. "similar to 2 cars
you disliked before").

Runs fully locally: a Chroma vector store on disk (`data/mem0/`) and
FastEmbed (a small ONNX embedding model, CPU-only, no API calls, downloaded
once from Hugging Face on first use) for embeddings — no new API key, no
data leaving the machine for the storage/retrieval side. The one network
call this makes at all is to Mistral (via `litellm`, which mem0 uses as a
generic LLM backend), reused for mem0's own internal fact-extraction step
when a memory is added — no OpenAI key, no second LLM provider, consistent
with this project's Mistral-only choice (`MISTRAL_API_KEY` is read by
litellm directly; nothing extra to configure).
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("MEM0_TELEMETRY", "False")

from mem0 import Memory  # noqa: E402  (must follow the telemetry env var above)

from becarscout.scoring.models import ScoredListing

logger = logging.getLogger(__name__)

DEFAULT_MEM0_DIR = os.getenv("BECARSCOUT_MEM0_DIR", "data/mem0/chroma")
DEFAULT_COLLECTION = "becarscout_feedback"
OWNER_USER_ID = "owner"
"""Single-user personal project — one mem0 namespace is enough; not tied
to a real Telegram user id since there's exactly one person's feedback."""

_MEMORY_LLM_MODEL = "mistral/" + os.getenv("MISTRAL_MODEL", "ministral-8b-latest")

_memory: Memory | None = None


def get_memory() -> Memory:
    """Lazily builds the mem0 client once per process — building it (and
    the FastEmbed model load behind it) isn't free, so this is called once
    from each entry point (notify, the feedback listener, the review
    agent) rather than per listing."""
    global _memory
    if _memory is None:
        _memory = Memory.from_config(
            {
                "vector_store": {
                    "provider": "chroma",
                    "config": {"collection_name": DEFAULT_COLLECTION, "path": DEFAULT_MEM0_DIR},
                },
                "embedder": {"provider": "fastembed", "config": {"model": "BAAI/bge-small-en-v1.5"}},
                "llm": {"provider": "litellm", "config": {"model": _MEMORY_LLM_MODEL}},
            }
        )
    return _memory


def _describe(listing: ScoredListing) -> str:
    """The natural-language text mem0 embeds and stores. Leads with the
    facts that anchor similarity search (make/model/year/price/mileage/
    score), then appends the condition-signal reasoning trail (gearbox/
    engine/warning-light/inspection/etc.) — without it, the review agent
    (`graph.py`) could never notice a pattern like "you keep disliking
    cars with unresolved warning lights", since that's exactly the kind
    of thing it needs to see to suggest a scoring.py weight change."""
    parts = [f"{listing.year or '?'} {listing.make or 'unknown make'} {listing.model_hint or ''}".strip()]
    if listing.price_eur is not None:
        parts.append(f"priced €{listing.price_eur:,}")
    if listing.mileage_km is not None:
        parts.append(f"{listing.mileage_km:,}km")
    if listing.baseline_median_price_eur is not None:
        parts.append(f"market median €{listing.baseline_median_price_eur:,}")
    parts.append(f"score {listing.score:+d}")
    description = ", ".join(parts)
    if listing.reasoning:
        description += ". " + "; ".join(listing.reasoning)
    return description


def store_feedback_memory(listing: ScoredListing, verdict: str) -> None:
    """Called whenever a \U0001f44d/\U0001f44e comes in (see
    `notifier/bot.py`'s callback handler). Failures are logged, not
    raised — the raw verdict is already safely recorded in
    `feedback.jsonl` regardless of whether mem0 could also store it."""
    verdict_word = "Liked" if verdict == "up" else "Disliked"
    try:
        get_memory().add(
            f"{verdict_word} this car: {_describe(listing)}",
            user_id=OWNER_USER_ID,
            metadata={"listing_id": listing.listing_id, "verdict": verdict},
        )
    except Exception:
        logger.exception("Failed to store feedback memory for %s", listing.listing_id)


def find_similar_feedback(listing: ScoredListing, limit: int = 3) -> list[dict]:
    """Past verdicts on similar-sounding cars, most relevant first — used
    to add a "similar to N you liked/disliked before" line to a *new*
    opportunity card before you decide on it. Never raises: a memory-layer
    hiccup shouldn't block a notification from going out."""
    try:
        result = get_memory().search(_describe(listing), filters={"user_id": OWNER_USER_ID}, top_k=limit)
    except Exception:
        logger.exception("mem0 search failed for %s — continuing without similarity context", listing.listing_id)
        return []
    return result.get("results", []) if isinstance(result, dict) else list(result)


def get_all_feedback_memories() -> list[dict]:
    """Every stored feedback memory — used by the periodic review agent
    (`feedback_agent/graph.py`) to look for patterns across everything
    liked/disliked so far, not just what's similar to one listing."""
    try:
        result = get_memory().get_all(filters={"user_id": OWNER_USER_ID}, top_k=1000)
    except Exception:
        logger.exception("mem0 get_all failed")
        return []
    return result.get("results", []) if isinstance(result, dict) else list(result)
