"""Formats a `ScoredListing` into the Telegram card described in
Project.md stage 6: photo/price/key stats, the reasoning trail as "why
this scored well / what to watch out for," and a link. No new judgment
happens here — it's a direct rendering of stage 4's already-computed
score and reasoning.
"""

from __future__ import annotations

from becarscout.scoring.models import ScoredListing


def format_opportunity_message(listing: ScoredListing, similar_feedback: list[dict] | None = None) -> str:
    lines = [f"*{_escape(listing.raw_title)}*", f"Score: {listing.score:+d}"]

    stats = []
    if listing.price_eur is not None:
        stats.append(f"€{listing.price_eur:,}")
    if listing.year is not None:
        stats.append(str(listing.year))
    if listing.mileage_km is not None:
        stats.append(f"{listing.mileage_km:,} km")
    if stats:
        lines.append(_escape(" · ".join(stats)))

    if listing.reasoning:
        lines.append("")
        lines.append("_Why:_")
        for reason in listing.reasoning:
            lines.append(f"• {_escape(reason)}")

    if similar_feedback:
        lines.append("")
        lines.append(_escape(_similar_feedback_note(similar_feedback)))

    lines.append("")
    lines.append(listing.url)

    return "\n".join(lines)


def _similar_feedback_note(similar_feedback: list[dict]) -> str:
    """Stage 7's memory context, surfaced as a plain note — never changes
    the score above, just gives you more to go on before you tap
    👍/👎 yourself (see Project.md's stage 7 design decision: memory
    informs, it doesn't decide)."""
    liked = sum(1 for m in similar_feedback if m.get("metadata", {}).get("verdict") == "up")
    disliked = len(similar_feedback) - liked
    parts = []
    if liked:
        parts.append(f"{liked} similar car{'s' if liked != 1 else ''} you liked before")
    if disliked:
        parts.append(f"{disliked} you disliked before")
    return "🧠 " + " and ".join(parts)


def _escape(text: str) -> str:
    """Escapes Telegram MarkdownV1 special characters (`_`, `*`, `[`, `` ` ``)
    — using the older Markdown mode, not MarkdownV2, since it needs far
    fewer characters escaped for what these messages contain."""
    for char in ("_", "*", "[", "`"):
        text = text.replace(char, f"\\{char}")
    return text
