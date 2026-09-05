"""Stage [6]: Telegram delivery. Deliberately not built on the salvaged
`src/becarscout/bot/` skeleton — that was a conversational preference-
picker UI for a different, unrelated product idea (brand/model selection
via chat, seller negotiation, KBB pricing). What Project.md actually specs
for this stage is much simpler: push a card per opportunity with inline
👍/👎, nothing more.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from becarscout.db import get_session
from becarscout.db import repository as repo
from becarscout.feedback_agent.memory import find_similar_feedback, store_feedback_memory
from becarscout.scoring.models import ScoredListing

from .feedback import record_feedback
from .formatting import format_history_summary, format_opportunity_message, format_score_explanation

logger = logging.getLogger(__name__)

load_dotenv()

CALLBACK_PREFIX = "fb"

# python-telegram-bot's defaults (5s connect/read, 1s pool timeout) are too
# tight for a container's network path — a single slow DNS lookup or a
# momentary blip is enough to raise TimedOut before a single message goes
# out. Seen for real: the container's other outbound calls (Mistral,
# 2dehands.be) succeeded in the same run while this timed out.
_REQUEST_TIMEOUTS = dict(connect_timeout=20.0, read_timeout=20.0, write_timeout=20.0, pool_timeout=10.0)
_CONNECT_RETRY_DELAYS_S = (2, 8, 20)


def _build_bot(token: str) -> Bot:
    return Bot(token=token, request=HTTPXRequest(**_REQUEST_TIMEOUTS))


def _get_credentials() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set in .env")
    return token, chat_id


async def find_chat_ids() -> list[tuple[int, str]]:
    """Looks at recent messages the bot has received (getUpdates) and
    returns (chat_id, sender_description) pairs — used to find your own
    chat_id after you've sent the bot a message, without needing to call
    the Telegram API by hand."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be set in .env")

    bot = _build_bot(token)
    seen: dict[int, str] = {}
    async with bot:
        updates = await bot.get_updates()
        for update in updates:
            chat = update.effective_chat
            if chat:
                label = chat.full_name or chat.username or chat.type
                seen[chat.id] = label
    return list(seen.items())


def _keyboard(listing_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍", callback_data=f"{CALLBACK_PREFIX}:up:{listing_id}"),
                InlineKeyboardButton("👎", callback_data=f"{CALLBACK_PREFIX}:down:{listing_id}"),
                InlineKeyboardButton("ℹ️ Why?", callback_data=f"{CALLBACK_PREFIX}:why:{listing_id}"),
            ]
        ]
    )


async def send_new_opportunities(opportunities: list[ScoredListing]) -> list[str]:
    """Sends every opportunity given (the caller — the DB-backed `notify`
    stage — is what decides which ones haven't been sent yet). Returns the
    listing_ids that actually went out, so the caller can mark only those
    as notified; one failed send shouldn't be recorded as delivered.

    Retries the whole connection (not just individual sends) on failure,
    since a `TimedOut` during `Bot.initialize()` means nothing has sent
    yet — but tracks what *did* go out across attempts so a retry after a
    partial batch never double-sends."""
    token, chat_id = _get_credentials()
    bot = _build_bot(token)
    sent_ids: list[str] = []

    for attempt, delay in enumerate((0, *_CONNECT_RETRY_DELAYS_S)):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with bot:
                for listing in opportunities:
                    if listing.listing_id in sent_ids:
                        continue
                    try:
                        similar_feedback = find_similar_feedback(listing)
                        await bot.send_message(
                            chat_id=chat_id,
                            text=format_opportunity_message(listing, similar_feedback),
                            parse_mode="Markdown",
                            reply_markup=_keyboard(listing.listing_id),
                        )
                        sent_ids.append(listing.listing_id)
                    except Exception:
                        logger.exception("Failed to send notification for %s", listing.url)
            return sent_ids
        except Exception as exc:
            logger.warning("Telegram connection attempt %d failed: %s", attempt + 1, exc)

    logger.error("Could not reach Telegram after %d attempts — sent %d/%d before giving up", len(_CONNECT_RETRY_DELAYS_S) + 1, len(sent_ids), len(opportunities))
    return sent_ids


def _fetch_scored_listing(listing_id: str) -> ScoredListing | None:
    session = get_session()
    try:
        return repo.get_scored_listing(session, listing_id)
    finally:
        session.close()


async def _handle_feedback_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    try:
        _, verdict, listing_id = query.data.split(":", 2)
    except ValueError:
        logger.warning("Malformed callback data: %r", query.data)
        return

    if verdict == "why":
        # Not feedback — just reveals the full point-by-point breakdown
        # kept out of the default card (see formatting.py). Leaves the
        # buttons in place so 👍/👎 is still available afterwards.
        scored = _fetch_scored_listing(listing_id)
        if scored is not None and query.message:
            try:
                await query.message.reply_text(format_score_explanation(scored), parse_mode="Markdown")
            except Exception:
                logger.exception("Failed to send score explanation for %s", listing_id)
        return

    record_feedback(listing_id, verdict)
    session = get_session()
    try:
        repo.mark_feedback(session, listing_id, verdict)
    finally:
        session.close()
    scored = _fetch_scored_listing(listing_id)
    if scored is not None:
        store_feedback_memory(scored, verdict)
    else:
        logger.warning("No DB row for %s — feedback.jsonl has it, but mem0 couldn't store context for it", listing_id)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
        if query.message:
            await query.message.reply_text("👍 Noted" if verdict == "up" else "👎 Noted")
    except Exception:
        logger.exception("Failed to acknowledge feedback for %s", listing_id)


def _parse_days_arg(args: list[str] | None, default: int | None) -> int | None:
    if not args:
        return default
    try:
        return max(1, int(args[0]))
    except ValueError:
        return default


async def _cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/history [days]` — browse past opportunities by date range instead
    of only ever seeing what's still visible in the chat (default: last 7
    days). Read-only: just a summary line per listing, no buttons."""
    if not update.effective_chat:
        return
    days = _parse_days_arg(context.args, default=7)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    session = get_session()
    try:
        entries = repo.get_opportunities_in_range(session, start, end)
    finally:
        session.close()

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=format_history_summary(entries, days),
        parse_mode="Markdown",
    )


_MAX_MISSED_RESENDS = 20
"""Each unreviewed opportunity is a full card -- resending more than this
in one go floods the chat; narrow with /missed <days> instead."""


async def _cmd_missed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/missed [days]` — resends opportunities that were never reviewed
    (no 👍/👎 yet), *with* their buttons, so a busy day that buried a card
    under newer ones doesn't mean it's unreachable. No `days` argument
    means "everything still unreviewed," regardless of age."""
    if not update.effective_chat:
        return
    days = _parse_days_arg(context.args, default=None)
    since = datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None

    session = get_session()
    try:
        unreviewed = repo.get_unreviewed_opportunities(session, since=since)
    finally:
        session.close()

    if not unreviewed:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Nothing waiting on your review.")
        return

    to_send = unreviewed[:_MAX_MISSED_RESENDS]
    for listing in to_send:
        similar_feedback = find_similar_feedback(listing)
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=format_opportunity_message(listing, similar_feedback),
                parse_mode="Markdown",
                reply_markup=_keyboard(listing.listing_id),
            )
        except Exception:
            logger.exception("Failed to resend missed opportunity %s", listing.listing_id)

    if len(unreviewed) > _MAX_MISSED_RESENDS:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Showing {_MAX_MISSED_RESENDS} of {len(unreviewed)} unreviewed — "
            "narrow with /missed <days> to see fewer at a time.",
        )


def run_feedback_listener() -> None:
    """Blocks, polling for 👍/👎 button presses (and /history, /missed
    commands) until interrupted (Ctrl+C). Run this as a standing
    background process — `becarscout notify` only sends messages, it
    doesn't listen for replies or commands."""
    token, _ = _get_credentials()
    application = ApplicationBuilder().token(token).build()
    application.add_handler(
        CallbackQueryHandler(_handle_feedback_callback, pattern=rf"^{CALLBACK_PREFIX}:")
    )
    application.add_handler(CommandHandler("history", _cmd_history))
    application.add_handler(CommandHandler("missed", _cmd_missed))
    logger.info("Listening for feedback button presses and commands... (Ctrl+C to stop)")
    application.run_polling()
