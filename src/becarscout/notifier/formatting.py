"""Formats a `ScoredListing` into the Telegram card described in
Project.md stage 6. The default card (`format_opportunity_message`) is
deliberately scannable: title, key stats, plain-language condition
highlights — no point deltas. The full point-by-point breakdown
(`format_score_explanation`) is a separate message, sent only when the
"Why?" button is tapped (see `notifier/bot.py`), so a quick glance at
Telegram doesn't mean reading a wall of numbers first.

Plain text, deliberately — no `parse_mode`. A real bug found live: a
listing titled `...carpass *130.000 km*` (a literal asterisk pair in a
seller's own title) broke Telegram's legacy Markdown parser
(`BadRequest: Can't parse entities`) even after escaping, because an
escaped closing backslash-asterisk landing immediately next to the wrapping bold
marker's own `*` is a known fragile edge case in that parser. Scraped
titles can contain *any* character a seller typed — trying to safely
escape arbitrary text for a parser with fragile, under-specified edge
cases is the wrong fight. Plain text has no delimiter syntax to break in
the first place, so this class of bug can't recur; the small loss is
just bold styling on the title."""

from __future__ import annotations

from becarscout.scoring.models import ScoredListing


def format_opportunity_message(listing: ScoredListing, similar_feedback: list[dict] | None = None) -> str:
    lines = [listing.raw_title, f"Score: {listing.score:+d}"]

    stats = []
    if listing.price_eur is not None:
        stats.append(f"€{listing.price_eur:,}")
    if listing.year is not None:
        stats.append(str(listing.year))
    if listing.mileage_km is not None:
        stats.append(f"{listing.mileage_km:,} km")
    if listing.fuel_type is not None:
        stats.append(listing.fuel_type.capitalize())
    if listing.transmission is not None:
        stats.append(listing.transmission.capitalize())
    if stats:
        lines.append(" · ".join(stats))

    if listing.condition_highlights:
        lines.append("")
        lines.extend(listing.condition_highlights)

    if similar_feedback:
        lines.append("")
        lines.append(_similar_feedback_note(similar_feedback))

    lines.append("")
    lines.append("Tap ℹ️ Why? below for the full price/condition breakdown.")
    lines.append(listing.url)

    return "\n".join(lines)


def format_score_explanation(listing: ScoredListing) -> str:
    """The full point-by-point reasoning trail behind `listing.score` —
    sent as a follow-up reply when the "Why?" inline button is tapped
    (see `notifier/bot.py`'s callback handler). Kept out of the default
    card so a quick glance at Telegram isn't a wall of numbers."""
    lines = [f"Why {listing.raw_title} scored {listing.score:+d}:"]
    if listing.reasoning:
        lines.append("")
        for reason in listing.reasoning:
            lines.append(f"- {reason}")
    else:
        lines.append("No detailed reasoning available.")
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
    return "\U0001f9e0 " + " and ".join(parts)
