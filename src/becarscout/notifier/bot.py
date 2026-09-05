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

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from becarscout.db import get_session
from becarscout.db import repository as repo
from becarscout.feedback_agent import apply_latest_suggestions, run_feedback_review
from becarscout.feedback_agent.memory import find_similar_feedback, store_feedback_memory
from becarscout.scoring.models import ScoredListing

from .feedback import record_feedback
from .formatting import format_opportunity_message, format_score_explanation

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
                await query.message.reply_text(format_score_explanation(scored))
            except Exception:
                logger.exception("Failed to send score explanation for %s", listing_id)
        return

    record_feedback(listing_id, verdict)
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


def _parse_int_arg(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _budget_text(settings) -> str:
    lo = f"€{settings.min_price:,}" if settings.min_price is not None else "€0"
    hi = f"€{settings.max_price:,}" if settings.max_price is not None else "no max"
    return f"{lo} - {hi}"


async def _reply(update: Update, text: str) -> None:
    if update.effective_chat:
        await update.get_bot().send_message(chat_id=update.effective_chat.id, text=text)


async def _cmd_settings(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/settings` — shows every parameter the other commands below can
    change, in one place, since each of them only shows/changes its own."""
    session = get_session()
    try:
        settings = repo.get_pipeline_settings(session)
    finally:
        session.close()
    min_year_text = str(settings.min_year) if settings.min_year is not None else "off"
    await _reply(
        update,
        "Current settings:\n"
        f"Radius: {settings.radius_km} km\n"
        f"Budget: {_budget_text(settings)}\n"
        f"Min year: {min_year_text}\n"
        f"Score threshold: {settings.threshold}\n\n"
        "Change with /budget, /minyear, /threshold, /radius. "
        "Takes effect from the next scrape/score onward.",
    )


async def _cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/budget` — sets the price range future scrapes search within.
    `/budget 15000` sets a max only; `/budget 3000 15000` sets both;
    `/budget off` clears it entirely."""
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session)
            await _reply(update, f"Current budget: {_budget_text(settings)}\nUsage: /budget <max>, /budget <min> <max>, or /budget off")
            return

        if args[0].lower() == "off":
            settings = repo.update_pipeline_settings(session, min_price=None, max_price=None)
        elif len(args) == 1:
            max_price = _parse_int_arg(args[0])
            if max_price is None:
                await _reply(update, "Couldn't parse that as a number. Usage: /budget <max> or /budget <min> <max>")
                return
            settings = repo.update_pipeline_settings(session, max_price=max_price)
        else:
            min_price, max_price = _parse_int_arg(args[0]), _parse_int_arg(args[1])
            if min_price is None or max_price is None:
                await _reply(update, "Couldn't parse those as numbers. Usage: /budget <min> <max>")
                return
            settings = repo.update_pipeline_settings(session, min_price=min_price, max_price=max_price)
    finally:
        session.close()
    await _reply(update, f"Budget set: {_budget_text(settings)} (applies from the next scrape onward)")


async def _cmd_minyear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/minyear 2015` excludes cars from that year or older regardless
    of score; `/minyear off` disables the cutoff entirely."""
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session)
            current = str(settings.min_year) if settings.min_year is not None else "off"
            await _reply(update, f"Current min year: {current}\nUsage: /minyear <year> or /minyear off")
            return

        if args[0].lower() == "off":
            settings = repo.update_pipeline_settings(session, min_year=None)
        else:
            year = _parse_int_arg(args[0])
            if year is None:
                await _reply(update, "Couldn't parse that as a year.")
                return
            settings = repo.update_pipeline_settings(session, min_year=year)
    finally:
        session.close()
    current = str(settings.min_year) if settings.min_year is not None else "off"
    await _reply(update, f"Min year set: {current} (applies from the next scoring run onward)")


async def _cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/threshold 30` — a listing needs at least this score to reach
    Telegram; higher means fewer, more confident opportunities."""
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session)
            await _reply(update, f"Current score threshold: {settings.threshold}\nUsage: /threshold <n>")
            return
        value = _parse_int_arg(args[0])
        if value is None:
            await _reply(update, "Couldn't parse that as a number.")
            return
        settings = repo.update_pipeline_settings(session, threshold=value)
    finally:
        session.close()
    await _reply(update, f"Score threshold set: {settings.threshold} (applies from the next scoring run onward)")


async def _cmd_radius(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/radius 150` — search radius in km around each Belgian hub city."""
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session)
            await _reply(update, f"Current search radius: {settings.radius_km} km\nUsage: /radius <km>")
            return
        km = _parse_int_arg(args[0])
        if km is None or km <= 0:
            await _reply(update, "Couldn't parse that as a positive number of km.")
            return
        settings = repo.update_pipeline_settings(session, radius_km=km)
    finally:
        session.close()
    await _reply(update, f"Search radius set: {settings.radius_km} km (applies from the next scrape onward)")


async def _cmd_reviewfeedback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/reviewfeedback` — runs stage 7's LangGraph review agent over
    accumulated 👍/👎 feedback and posts the patterns + suggested
    scoring weight changes it found. Advisory only: nothing changes until
    you send /validate."""
    await _reply(update, "Reviewing feedback so far, one moment...")
    result = run_feedback_review()
    if result.get("skipped"):
        await _reply(update, result["skip_reason"])
        return
    await _reply(update, result["report_markdown"])


async def _cmd_validate(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/validate` — applies the *last* `/reviewfeedback` run's suggested
    scoring weight changes for real. This is the one command that
    actually changes future scoring behavior; everything else in stage 7
    is advisory until you send this."""
    applied = apply_latest_suggestions()
    if applied is None:
        await _reply(update, "Nothing to validate yet — run /reviewfeedback first.")
        return
    if not applied:
        await _reply(update, "The last review had no suggestions to apply.")
        return
    lines = ["Applied:"]
    for s in applied:
        lines.append(f"- {s['weight']}: {s['current_value']} -> {s['new_value']}")
    lines.append("")
    lines.append("The agent will score with these from now on.")
    await _reply(update, "\n".join(lines))


def run_feedback_listener() -> None:
    """Blocks, polling for 👍/👎 button presses and commands until
    interrupted (Ctrl+C). Run this as a standing background process —
    `becarscout notify` only sends messages, it doesn't listen for
    replies or commands."""
    token, _ = _get_credentials()
    application = ApplicationBuilder().token(token).build()
    application.add_handler(
        CallbackQueryHandler(_handle_feedback_callback, pattern=rf"^{CALLBACK_PREFIX}:")
    )
    application.add_handler(CommandHandler("settings", _cmd_settings))
    application.add_handler(CommandHandler("budget", _cmd_budget))
    application.add_handler(CommandHandler("minyear", _cmd_minyear))
    application.add_handler(CommandHandler("threshold", _cmd_threshold))
    application.add_handler(CommandHandler("radius", _cmd_radius))
    application.add_handler(CommandHandler("reviewfeedback", _cmd_reviewfeedback))
    application.add_handler(CommandHandler("validate", _cmd_validate))
    logger.info("Listening for feedback button presses and commands... (Ctrl+C to stop)")
    application.run_polling()
