"""Formats a `ScoredListing` into the Telegram card described in
Project.md stage 6. The default card (`format_opportunity_message`) is
deliberately scannable: title, key stats, plain-language condition
highlights — no point deltas. The full point-by-point breakdown
(`format_score_explanation`) is a separate message, sent only when the
"Why?" button is tapped (see `notifier/bot.py`), so a quick glance at
Telegram doesn't mean reading a wall of numbers first.
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
    if listing.fuel_type is not None:
        stats.append(listing.fuel_type.capitalize())
    if listing.transmission is not None:
        stats.append(listing.transmission.capitalize())
    if stats:
        lines.append(_escape(" · ".join(stats)))

    if listing.condition_highlights:
        lines.append("")
        for highlight in listing.condition_highlights:
            lines.append(_escape(highlight))

    if similar_feedback:
        lines.append("")
        lines.append(_escape(_similar_feedback_note(similar_feedback)))

    lines.append("")
    lines.append(_escape("Tap ℹ️ Why? below for the full price/condition breakdown."))
    lines.append(listing.url)

    return "\n".join(lines)


def format_score_explanation(listing: ScoredListing) -> str:
    """The full point-by-point reasoning trail behind `listing.score` —
    sent as a follow-up reply when the "Why?" inline button is tapped
    (see `notifier/bot.py`'s callback handler). Kept out of the default
    card so a quick glance at Telegram isn't a wall of numbers."""
    lines = [f"*Why {_escape(listing.raw_title)} scored {listing.score:+d}:*"]
    if listing.reasoning:
        lines.append("")
        for reason in listing.reasoning:
            lines.append(f"• {_escape(reason)}")
    else:
        lines.append("No detailed reasoning available.")
    return "\n".join(lines)


_MAX_HISTORY_ROWS = 30
"""Telegram caps a message at 4096 characters -- past this many rows the
summary gets capped and says how many more there were, rather than risk a
send failure on a long history."""


def format_history_summary(entries: list[tuple[ScoredListing, str | None]], days: int) -> str:
    """Backs `/history [days]` — a compact, scannable list (title, score,
    price, reviewed/not) of everything sent in the requested window, not
    just whatever's still visible in the chat. `entries` is
    (listing, feedback_verdict) pairs, most recent first — see
    `db/repository.get_opportunities_in_range`."""
    period = f"{days} day{'s' if days != 1 else ''}"
    if not entries:
        return f"No opportunities sent in the last {period}."

    lines = [f"*Opportunities from the last {period}* ({len(entries)}):", ""]
    for listing, verdict in entries[:_MAX_HISTORY_ROWS]:
        mark = "\U0001f44d" if verdict == "up" else "\U0001f44e" if verdict == "down" else "—"
        price = f"€{listing.price_eur:,}" if listing.price_eur is not None else "?"
        lines.append(f"{mark} {listing.score:+d} · {_escape(listing.raw_title)} · {price}")

    if len(entries) > _MAX_HISTORY_ROWS:
        lines.append(f"...and {len(entries) - _MAX_HISTORY_ROWS} more.")

    unreviewed = sum(1 for _, verdict in entries if verdict is None)
    if unreviewed:
        lines.append("")
        lines.append(f"{unreviewed} not yet reviewed — send /missed to go through them.")

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
