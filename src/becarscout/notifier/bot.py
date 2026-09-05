"""Stage [6]: Telegram delivery. Deliberately not built on the salvaged
`src/becarscout/bot/` skeleton — that was a conversational preference-
picker UI for a different, unrelated product idea (brand/model selection
via chat, seller negotiation, KBB pricing). What Project.md actually specs
for this stage is much simpler: push a card per opportunity with inline
👍/👎, nothing more.

Multi-user note (added 2026-09-06): every chat that has ever messaged the
bot is a subscriber (`_ensure_subscribed_middleware`, a `group=-1` handler
that runs before everything else) with their own budget/year/threshold/
mileage/brand/fuel/transmission, their own scores, and their own feedback
memory — see `db/models.py`'s module docstring. Search radius is the one
setting still shared by everyone (`/radius`), since there's no per-listing
distance figure to filter by afterward.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from becarscout.db import get_session
from becarscout.db import repository as repo
from becarscout.db.models import ListingRow
from becarscout.feedback_agent import apply_latest_suggestions, run_feedback_review
from becarscout.feedback_agent.memory import find_similar_feedback, store_feedback_memory
from becarscout.scoring.models import ScoredListing
from becarscout.structurer.makes import MAKES
from becarscout.structurer.parse import FUEL_TYPES, TRANSMISSIONS

from .feedback import record_feedback
from .formatting import format_opportunity_message, format_score_explanation

logger = logging.getLogger(__name__)

load_dotenv()

CALLBACK_PREFIX = "fb"
SETTINGS_CALLBACK_PREFIX = "set"

# Chat id -> which command is waiting for its value on the *next* plain
# message from that chat. Lets /budget, /minyear, /threshold, /radius (and
# the /settings buttons) work as a two-step prompt ("send me the value")
# instead of requiring the full command-with-args up front. In-memory and
# per-process only — acceptable here since a listener restart losing a
# half-finished prompt just means re-tapping the button, no data at risk.
_pending_prompts: dict[int, str] = {}

# Chat id -> remaining steps in a guided /setup run, seeded by _start_wizard
# and consumed by _advance_wizard after each step is successfully applied.
# A chat not in this dict is simply not mid-setup -- single /budget-style
# edits never touch it.
_wizard_queue: dict[int, list[str]] = {}
_WIZARD_STEPS = ["budget", "minyear", "radius", "threshold"]

# Chat ids with a /find pipeline run currently in flight -- stops a second
# tap from launching an overlapping `becarscout run` subprocess on top of
# one already going.
_find_running: set[int] = set()

# Chat id -> the last keyword it searched for -- lets the /search time-range
# quick-pick buttons ("Today"/"2 days"/...) re-run the same search with a
# range applied without re-encoding the keyword into callback_data (which
# has a 64-byte limit and awkward separator rules).
_last_search_keyword: dict[int, str] = {}

_QUICK_PICKS: dict[str, dict[str, dict[str, object]]] = {
    "threshold": {
        "loose": {"threshold": 10},
        "balanced": {"threshold": 20},
        "strict": {"threshold": 35},
    },
    "budget": {
        "under5k": {"min_price": None, "max_price": 5000},
        "5to15k": {"min_price": 5000, "max_price": 15000},
        "15kplus": {"min_price": 15000, "max_price": None},
    },
}

_QUICK_PICK_LABELS: dict[str, list[tuple[str, str]]] = {
    "threshold": [("loose", "😌 Loose"), ("balanced", "⚖️ Balanced"), ("strict", "🔥 Strict")],
    "budget": [("under5k", "Under €5,000"), ("5to15k", "€5,000-15,000"), ("15kplus", "€15,000+")],
}

_SEARCH_RANGE_DAYS: dict[str, int] = {"today": 1, "2days": 2, "3days": 3, "week": 7}
_SEARCH_RANGE_LABELS: list[tuple[str, str]] = [
    ("today", "📅 Today"),
    ("2days", "📅 2 days"),
    ("3days", "📅 3 days"),
    ("week", "📅 Last week"),
]

_SETTINGS_PROMPTS: dict[str, str] = {
    "budget": (
        "💶 What's your budget?\n"
        "Just type a top price, like 15000\n"
        "Or a range, like 3000 15000\n"
        "Or type off for no limit."
    ),
    "minyear": (
        "📅 What's the oldest car you'll consider?\n"
        "Type a year, like 2015\n"
        "Or type off to allow any year."
    ),
    "threshold": (
        "🎯 How picky should I be?\n"
        "Type a number — the higher it is, the fewer (but better) deals I'll send you. "
        "20 is a good number to start with."
    ),
    "radius": (
        "📍 How far should I search around each city?\n"
        "Type a distance in km, like 100.\n"
        "Heads up: this changes the search area for everyone using this bot, not just you."
    ),
    "mileage": (
        "🛣️ What's the highest mileage you'll accept?\n"
        "Type a distance in km, like 150000\n"
        "Or type off for no limit."
    ),
    "make": (
        "🚘 Any specific brands? Type them separated by commas, like: bmw, toyota\n"
        f"(some brands I recognize: {', '.join(sorted(MAKES)[:8])}, ...)\n"
        "Or type off to see any brand."
    ),
    "fuel": (
        f"⛽ Any fuel preference? Type one or more, separated by commas: {', '.join(FUEL_TYPES)}\n"
        "Or type off for no preference."
    ),
    "transmission": (
        f"🔧 Automatic or manual? Type one: {', '.join(TRANSMISSIONS)}\n"
        "Or type off for no preference."
    ),
    "name": "🙋 What should I call you?",
}


def _welcome_text(name: str | None) -> str:
    greeting = f"👋 Hi {name}!" if name else "👋 Hi!"
    return (
        f"{greeting} I'm BE-CarScout — I search Facebook Marketplace for used cars in Belgium "
        "and message you here whenever I spot a good deal, just for you.\n\n"
        "Tap 🚀 Quick setup below and I'll ask a few quick questions to get you started -- "
        "or use /settings anytime to see and change:\n"
        "💶 your budget\n"
        "📅 the oldest year you'll consider\n"
        "📍 how far I should search\n"
        "🎯 how picky I should be\n"
        "🛣️ 🚘 ⛽ 🔧 and filters for mileage, brand, fuel type, transmission\n\n"
        "Other things I can do:\n"
        "🔎 /find — search right now instead of waiting for the next hour\n"
        "🔍 /search — look through cars I've already found, e.g. /search golf or /search today\n"
        "👍 / 👎 — tap the buttons under a car to tell me if you like it, "
        "so I get better at picking cars for you over time\n"
        "🚫 /cancel — stop whatever it's currently asking you\n\n"
        "Type /settings anytime to see your current setup."
    )

# python-telegram-bot's defaults (5s connect/read, 1s pool timeout) are too
# tight for a container's network path — a single slow DNS lookup or a
# momentary blip is enough to raise TimedOut before a single message goes
# out. Seen for real: the container's other outbound calls (Mistral,
# 2dehands.be) succeeded in the same run while this timed out.
_REQUEST_TIMEOUTS = dict(connect_timeout=20.0, read_timeout=20.0, write_timeout=20.0, pool_timeout=10.0)
_CONNECT_RETRY_DELAYS_S = (2, 8, 20)


def _build_bot(token: str) -> Bot:
    return Bot(token=token, request=HTTPXRequest(**_REQUEST_TIMEOUTS))


def _get_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be set in .env")
    return token


async def find_chat_ids() -> list[tuple[int, str]]:
    """Looks at recent messages the bot has received (getUpdates) and
    returns (chat_id, sender_description) pairs — a quick way to find a
    chat_id for CLI/admin use (e.g. `becarscout feedback-review
    --chat-id`) without needing to call the Telegram API by hand.
    Subscribing itself no longer needs this — see
    `_ensure_subscribed_middleware`."""
    bot = _build_bot(_get_token())
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


async def send_new_opportunities(chat_id: int, opportunities: list[ScoredListing]) -> list[str]:
    """Sends every opportunity given to one subscriber (the caller — the
    DB-backed `notify` stage — is what decides which ones haven't been
    sent to *this* chat_id yet). Returns the listing_ids that actually
    went out, so the caller can mark only those as notified; one failed
    send shouldn't be recorded as delivered.

    Retries the whole connection (not just individual sends) on failure,
    since a `TimedOut` during `Bot.initialize()` means nothing has sent
    yet — but tracks what *did* go out across attempts so a retry after a
    partial batch never double-sends."""
    bot = _build_bot(_get_token())
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
                        similar_feedback = find_similar_feedback(listing, chat_id)
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


def _fetch_scored_listing(chat_id: int, listing_id: str) -> ScoredListing | None:
    session = get_session()
    try:
        return repo.get_scored_listing(session, chat_id, listing_id)
    finally:
        session.close()


async def _ensure_subscribed_middleware(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler (registered with `group=-1`), so
    any chat that has ever sent the bot anything becomes a subscriber —
    notifications and settings don't depend on remembering to send
    `/start` specifically. A no-op for an already-known chat."""
    chat = update.effective_chat
    if not chat:
        return
    session = get_session()
    try:
        repo.ensure_subscriber(session, chat.id)
    finally:
        session.close()


async def _handle_feedback_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if not query or not query.data or not chat:
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
        scored = _fetch_scored_listing(chat.id, listing_id)
        if scored is not None and query.message:
            try:
                await query.message.reply_text(format_score_explanation(scored))
            except Exception:
                logger.exception("Failed to send score explanation for %s", listing_id)
        return

    record_feedback(chat.id, listing_id, verdict)
    scored = _fetch_scored_listing(chat.id, listing_id)
    if scored is not None:
        store_feedback_memory(scored, verdict, chat.id)
    else:
        logger.warning("No score for %s/%s — feedback.jsonl has it, but mem0 couldn't store context for it", chat.id, listing_id)

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


def _parse_csv_arg(args: list[str]) -> list[str]:
    """Joins whitespace-split command args back into one string and splits
    on commas — lets `/make bmw, toyota` and `/make bmw,toyota` both work,
    since Telegram already splits args on whitespace before the handler
    ever sees them."""
    joined = " ".join(args)
    return [item.strip().lower() for item in joined.split(",") if item.strip()]


def _budget_text(settings) -> str:
    lo = f"€{settings.min_price:,}" if settings.min_price is not None else "€0"
    hi = f"€{settings.max_price:,}" if settings.max_price is not None else "no max"
    return f"{lo} - {hi}"


async def _reply(update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if update.effective_chat:
        await update.get_bot().send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)


def _quick_pick_keyboard(setting: str) -> InlineKeyboardMarkup | None:
    """A row of one-tap common values for settings where typing a number
    is friction a lot of users don't need -- e.g. "Loose/Balanced/Strict"
    instead of remembering that higher thresholds mean fewer, better
    matches. Only defined for a couple of settings (see _QUICK_PICK_LABELS);
    returns None for everything else, so callers can skip attaching a
    keyboard at all."""
    options = _QUICK_PICK_LABELS.get(setting)
    if not options:
        return None
    buttons = [
        InlineKeyboardButton(label, callback_data=f"{SETTINGS_CALLBACK_PREFIX}:qp:{setting}:{key}")
        for key, label in options
    ]
    return InlineKeyboardMarkup([buttons])


def _search_range_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"{SETTINGS_CALLBACK_PREFIX}:sr:{key}")
        for key, label in _SEARCH_RANGE_LABELS
    ]
    return InlineKeyboardMarkup([buttons])


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🙋 Name", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:name"),
                InlineKeyboardButton("💰 Budget", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:budget"),
            ],
            [
                InlineKeyboardButton("📅 Min year", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:minyear"),
                InlineKeyboardButton("🎯 Threshold", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:threshold"),
            ],
            [
                InlineKeyboardButton("📍 Radius", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:radius"),
            ],
            [
                InlineKeyboardButton("🛣️ Mileage", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:mileage"),
                InlineKeyboardButton("🚘 Brand", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:make"),
            ],
            [
                InlineKeyboardButton("⛽ Fuel", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:fuel"),
                InlineKeyboardButton("🔧 Transmission", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:transmission"),
            ],
        ]
    )


async def _cmd_settings(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/settings` — shows every parameter the other commands below can
    change, in one place, since each of them only shows/changes its own.
    Tapping a button below prompts for the new value, same as sending the
    matching command with no arguments. Everything here is personal to
    this chat except the search radius, which is shared by every
    subscriber (see module docstring)."""
    chat = update.effective_chat
    if not chat:
        return
    session = get_session()
    try:
        settings = repo.get_pipeline_settings(session, chat.id)
        radius_km = repo.get_global_scrape_radius_km(session)
        name = repo.get_subscriber_name(session, chat.id)
    finally:
        session.close()
    min_year_text = str(settings.min_year) if settings.min_year is not None else "any year"
    mileage_text = f"{settings.max_mileage_km:,} km" if settings.max_mileage_km is not None else "no limit"
    makes_text = settings.makes.replace(",", ", ") if settings.makes else "any brand"
    fuel_text = settings.fuel_types.replace(",", ", ") if settings.fuel_types else "any fuel type"
    transmission_text = settings.transmission or "no preference"
    await _reply(
        update,
        "⚙️ Your current setup:\n\n"
        f"🙋 Name: {name or 'not set'}\n"
        f"💶 Budget: {_budget_text(settings)}\n"
        f"📅 Oldest year I'll consider: {min_year_text}\n"
        f"📍 Search area (shared): {radius_km} km around each city\n"
        f"🎯 How picky I am: {settings.threshold} (higher = fewer, better matches)\n"
        f"🛣️ Highest mileage: {mileage_text}\n"
        f"🚘 Brands: {makes_text}\n"
        f"⛽ Fuel type: {fuel_text}\n"
        f"🔧 Transmission: {transmission_text}\n\n"
        "Tap a button below to change one — I'll ask what you want it set to.\n\n"
        "Changes apply from the next search onward (I check hourly, or you can say /find "
        "to make me search right now).",
        reply_markup=_settings_keyboard(),
    )


async def _start_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/setup` and the "🚀 Quick setup" button — chains the four core
    settings (budget, min year, radius, threshold) into one guided flow
    instead of making a new user tap four separate `/settings` buttons.
    Reuses each setting's own no-args prompt (`_advance_wizard` just calls
    the matching /command with empty args), so there's exactly one place
    that owns each prompt's wording."""
    chat = update.effective_chat
    if not chat:
        return
    _wizard_queue[chat.id] = list(_WIZARD_STEPS)
    await _reply(update, "🚀 Let's get you set up — I'll ask a few quick questions. Send /cancel anytime to stop.")
    await _advance_wizard(chat.id, update, context)


async def _advance_wizard(chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called after a setting is successfully applied -- moves to the next
    step of a guided /setup run, or finishes it. A no-op for a chat that
    isn't mid-setup (the common case: a single /budget-style edit)."""
    queue = _wizard_queue.get(chat_id)
    if queue is None:
        return
    if not queue:
        del _wizard_queue[chat_id]
        await _reply(update, "🎉 All set! I'll use these from your next search onward. Type /settings anytime to change something, or /find to search right now.")
        return
    next_setting = queue.pop(0)
    context.args = []
    await _PROMPTABLE_COMMANDS[next_setting](update, context)


async def _handle_quick_pick(update: Update, context: ContextTypes.DEFAULT_TYPE, setting: str, key: str) -> None:
    """Handles a one-tap quick-pick button (see `_QUICK_PICKS`) -- applies
    the change directly rather than routing through a /command's text
    parsing, since these buttons carry their own fixed values."""
    chat = update.effective_chat
    if not chat:
        return
    changes = _QUICK_PICKS.get(setting, {}).get(key)
    if changes is None:
        return
    session = get_session()
    try:
        settings = repo.update_pipeline_settings(session, chat.id, **changes)
    finally:
        session.close()

    _pending_prompts.pop(chat.id, None)

    if setting == "threshold":
        text = f"✅ Got it — I'll only message you about cars that score {settings.threshold} or higher."
    else:
        text = f"✅ Budget set: {_budget_text(settings)}"
    await _reply(update, f"{text}\nThis applies from your next search onward.")

    await _advance_wizard(chat.id, update, context)


async def _handle_search_range(update: Update, context: ContextTypes.DEFAULT_TYPE, range_key: str) -> None:
    """Handles a tap on one of `/search`'s time-range quick-pick buttons
    -- re-runs the chat's last search (or, if it never searched a
    keyword, just lists what's new) restricted to that window."""
    chat = update.effective_chat
    if not chat or range_key not in _SEARCH_RANGE_DAYS:
        return
    keyword = _last_search_keyword.get(chat.id, "")
    context.args = (keyword.split() if keyword else []) + [range_key]
    await _cmd_search(update, context)


async def _handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on the `/settings` buttons — routes to the same
    handler its matching /command uses, so there's one place that owns
    each setting's prompt wording and quick-pick keyboard."""
    query = update.callback_query
    if not query or not query.data or not update.effective_chat:
        return
    await query.answer()
    parts = query.data.split(":")

    if len(parts) == 4 and parts[1] == "qp":
        await _handle_quick_pick(update, context, parts[2], parts[3])
        return

    if len(parts) == 3 and parts[1] == "sr":
        await _handle_search_range(update, context, parts[2])
        return

    if len(parts) != 2:
        return
    setting = parts[1]
    if setting == "wizard":
        await _start_wizard(update, context)
        return

    handler = _PROMPTABLE_COMMANDS.get(setting)
    if handler is None:
        return
    context.args = []
    await handler(update, context)


async def _cmd_cancel(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/cancel` — clears a pending `/budget`-style prompt or an in-progress
    `/setup` run. Without this, a stray message sent while a prompt is
    pending would otherwise get silently swallowed as that prompt's
    answer, with no way to back out."""
    chat = update.effective_chat
    if not chat:
        return
    had_prompt = _pending_prompts.pop(chat.id, None) is not None
    had_wizard = _wizard_queue.pop(chat.id, None) is not None
    if had_prompt or had_wizard:
        await _reply(update, "Okay, cancelled — nothing changed.")
    else:
        await _reply(update, "Nothing to cancel.")


async def _handle_plain_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Completes a pending `/budget`-style prompt — whatever plain text the
    user sends next is treated as the arguments that command would have
    taken directly (`/budget 15000` and replying `15000` to the prompt do
    the same thing). The pending prompt is only cleared once the handler
    actually applies a value (`applied` is True) -- on invalid input the
    handler shows an error and the same prompt stays armed, so the user
    can just try again without retyping the full command."""
    chat = update.effective_chat
    if not chat or chat.id not in _pending_prompts or not update.message or not update.message.text:
        return
    setting = _pending_prompts[chat.id]
    handler = _PROMPTABLE_COMMANDS.get(setting)
    if handler is None:
        _pending_prompts.pop(chat.id, None)
        return
    context.args = update.message.text.split()
    applied = await handler(update, context)
    if not applied:
        return
    _pending_prompts.pop(chat.id, None)
    await _advance_wizard(chat.id, update, context)


async def _cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """`/budget` — this subscriber's personal price range. `/budget 15000`
    sets a max only; `/budget 3000 15000` sets both; `/budget off` clears
    it entirely. A shared pool of listings is scraped for everyone (see
    `/radius`); this filters what you personally get notified about.
    Returns whether a value was actually applied (used by
    `_handle_plain_reply`/`_advance_wizard` to know whether to move on or
    keep the prompt armed for a retry)."""
    chat = update.effective_chat
    if not chat:
        return False
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session, chat.id)
            _pending_prompts[chat.id] = "budget"
            await _reply(
                update,
                f"Your budget is currently: {_budget_text(settings)}\n\n{_SETTINGS_PROMPTS['budget']}",
                reply_markup=_quick_pick_keyboard("budget"),
            )
            return False

        if args[0].lower() == "off":
            settings = repo.update_pipeline_settings(session, chat.id, min_price=None, max_price=None)
        elif len(args) == 1:
            max_price = _parse_int_arg(args[0])
            if max_price is None:
                await _reply(update, "That doesn't look like a number. Try just a number, like 15000.")
                return False
            settings = repo.update_pipeline_settings(session, chat.id, max_price=max_price)
        else:
            min_price, max_price = _parse_int_arg(args[0]), _parse_int_arg(args[1])
            if min_price is None or max_price is None:
                await _reply(update, "Those don't look like numbers. Try two numbers, like 3000 15000.")
                return False
            settings = repo.update_pipeline_settings(session, chat.id, min_price=min_price, max_price=max_price)
    finally:
        session.close()
    await _reply(update, f"✅ Budget set: {_budget_text(settings)}\nThis applies from your next search onward.")
    return True


async def _cmd_minyear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """`/minyear 2015` excludes cars from that year or older regardless
    of score, for this subscriber; `/minyear off` disables the cutoff."""
    chat = update.effective_chat
    if not chat:
        return False
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session, chat.id)
            current = str(settings.min_year) if settings.min_year is not None else "any year"
            _pending_prompts[chat.id] = "minyear"
            await _reply(update, f"Right now I'll consider cars from: {current}\n\n{_SETTINGS_PROMPTS['minyear']}")
            return False

        if args[0].lower() == "off":
            settings = repo.update_pipeline_settings(session, chat.id, min_year=None)
        else:
            year = _parse_int_arg(args[0])
            if year is None:
                await _reply(update, "That doesn't look like a year. Try a number like 2015.")
                return False
            settings = repo.update_pipeline_settings(session, chat.id, min_year=year)
    finally:
        session.close()
    confirmation = (
        f"✅ I'll now only consider cars from {settings.min_year} onward."
        if settings.min_year is not None
        else "✅ I'll now consider cars from any year."
    )
    await _reply(update, f"{confirmation}\nThis applies from your next search onward.")
    return True


async def _cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """`/threshold 30` — a listing needs at least this score to reach this
    subscriber; higher means fewer, more confident opportunities."""
    chat = update.effective_chat
    if not chat:
        return False
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session, chat.id)
            _pending_prompts[chat.id] = "threshold"
            await _reply(
                update,
                f"Right now, how picky I am: {settings.threshold}\n\n{_SETTINGS_PROMPTS['threshold']}",
                reply_markup=_quick_pick_keyboard("threshold"),
            )
            return False
        value = _parse_int_arg(args[0])
        if value is None:
            await _reply(update, "That doesn't look like a number. Try a number like 20.")
            return False
        settings = repo.update_pipeline_settings(session, chat.id, threshold=value)
    finally:
        session.close()
    await _reply(update, f"✅ Got it — I'll only message you about cars that score {settings.threshold} or higher.\nThis applies from your next search onward.")
    return True


async def _cmd_radius(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """`/radius 150` — search radius in km around each Belgian hub city.
    Unlike every other filter, this is shared by every subscriber: there's
    no per-listing distance figure to filter by after the fact, so one
    radius has to define what's scraped for everyone."""
    chat = update.effective_chat
    if not chat:
        return False
    args = context.args or []
    session = get_session()
    try:
        if not args:
            radius_km = repo.get_global_scrape_radius_km(session)
            _pending_prompts[chat.id] = "radius"
            await _reply(update, f"Right now everyone's search covers {radius_km} km around each city.\n\n{_SETTINGS_PROMPTS['radius']}")
            return False
        km = _parse_int_arg(args[0])
        if km is None or km <= 0:
            await _reply(update, "That doesn't look like a distance. Try a positive number like 100.")
            return False
        radius_km = repo.update_global_scrape_radius_km(session, km)
    finally:
        session.close()
    await _reply(update, f"✅ The shared search now covers {radius_km} km around each city.\nThis applies from the next search onward, for everyone.")
    return True


async def _cmd_mileage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """`/mileage 150000` excludes cars with more km than that regardless
    of score, for this subscriber; `/mileage off` disables the cutoff. A
    listing with no detected mileage still passes -- see
    `scoring/gate.py`'s docstring."""
    chat = update.effective_chat
    if not chat:
        return False
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session, chat.id)
            current = f"{settings.max_mileage_km:,} km" if settings.max_mileage_km is not None else "no limit"
            _pending_prompts[chat.id] = "mileage"
            await _reply(update, f"Right now, highest mileage I'll accept: {current}\n\n{_SETTINGS_PROMPTS['mileage']}")
            return False

        if args[0].lower() == "off":
            settings = repo.update_pipeline_settings(session, chat.id, max_mileage_km=None)
        else:
            km = _parse_int_arg(args[0])
            if km is None or km <= 0:
                await _reply(update, "That doesn't look like a distance. Try a positive number like 150000.")
                return False
            settings = repo.update_pipeline_settings(session, chat.id, max_mileage_km=km)
    finally:
        session.close()
    confirmation = (
        f"✅ I'll now skip cars with more than {settings.max_mileage_km:,} km."
        if settings.max_mileage_km is not None
        else "✅ No mileage limit anymore."
    )
    await _reply(update, f"{confirmation}\nThis applies from your next search onward.")
    return True


async def _cmd_make(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """`/make bmw, toyota` only shows this subscriber those brands;
    `/make off` clears it. Unlike /mileage or /minyear, a listing whose
    brand wasn't detected does *not* pass once this filter is set -- see
    `scoring/gate.py`'s docstring for why."""
    chat = update.effective_chat
    if not chat:
        return False
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session, chat.id)
            current = settings.makes.replace(",", ", ") if settings.makes else "any brand"
            _pending_prompts[chat.id] = "make"
            await _reply(update, f"Right now, brands I'll show you: {current}\n\n{_SETTINGS_PROMPTS['make']}")
            return False

        if args[0].lower() == "off":
            settings = repo.update_pipeline_settings(session, chat.id, makes=None)
        else:
            wanted = _parse_csv_arg(args)
            if not wanted:
                await _reply(update, "I didn't catch a brand there. Try something like: bmw, toyota")
                return False
            known = {m.lower() for m in MAKES}
            unknown = [m for m in wanted if m not in known]
            settings = repo.update_pipeline_settings(session, chat.id, makes=",".join(wanted))
            if unknown:
                await _reply(update, f"⚠️ Heads up, I don't recognize \"{', '.join(unknown)}\" as a brand -- double-check the spelling, or it just won't match anything.")
    finally:
        session.close()
    confirmation = f"✅ I'll only show you: {settings.makes.replace(',', ', ')}" if settings.makes else "✅ I'll show you any brand again."
    await _reply(update, f"{confirmation}\nThis applies from your next search onward.")
    return True


async def _cmd_fuel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """`/fuel diesel, hybrid` only shows this subscriber those fuel types;
    `/fuel off` clears it. Strict about valid values (unlike /make) since
    the set of real fuel types is small and fixed -- see
    `structurer.parse.FUEL_TYPES`."""
    chat = update.effective_chat
    if not chat:
        return False
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session, chat.id)
            current = settings.fuel_types.replace(",", ", ") if settings.fuel_types else "any fuel type"
            _pending_prompts[chat.id] = "fuel"
            await _reply(update, f"Right now, fuel types I'll show you: {current}\n\n{_SETTINGS_PROMPTS['fuel']}")
            return False

        if args[0].lower() == "off":
            settings = repo.update_pipeline_settings(session, chat.id, fuel_types=None)
        else:
            wanted = _parse_csv_arg(args)
            invalid = [f for f in wanted if f not in FUEL_TYPES]
            if invalid:
                await _reply(update, f"I don't recognize \"{', '.join(invalid)}\". Valid options: {', '.join(FUEL_TYPES)}")
                return False
            settings = repo.update_pipeline_settings(session, chat.id, fuel_types=",".join(wanted))
    finally:
        session.close()
    confirmation = f"✅ I'll only show you: {settings.fuel_types.replace(',', ', ')}" if settings.fuel_types else "✅ I'll show you any fuel type again."
    await _reply(update, f"{confirmation}\nThis applies from your next search onward.")
    return True


async def _cmd_transmission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """`/transmission automatic` or `/transmission manual`, for this
    subscriber; `/transmission off` clears it. Same strict-values
    reasoning as /fuel."""
    chat = update.effective_chat
    if not chat:
        return False
    args = context.args or []
    session = get_session()
    try:
        if not args:
            settings = repo.get_pipeline_settings(session, chat.id)
            current = settings.transmission or "no preference"
            _pending_prompts[chat.id] = "transmission"
            await _reply(update, f"Right now: {current}\n\n{_SETTINGS_PROMPTS['transmission']}")
            return False

        if args[0].lower() == "off":
            settings = repo.update_pipeline_settings(session, chat.id, transmission=None)
        else:
            wanted = args[0].strip().lower()
            if wanted not in TRANSMISSIONS:
                await _reply(update, f"I don't recognize \"{wanted}\". Valid options: {', '.join(TRANSMISSIONS)}")
                return False
            settings = repo.update_pipeline_settings(session, chat.id, transmission=wanted)
    finally:
        session.close()
    confirmation = f"✅ I'll only show you: {settings.transmission}" if settings.transmission else "✅ No preference anymore."
    await _reply(update, f"{confirmation}\nThis applies from your next search onward.")
    return True


async def _cmd_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """`/name <text>` sets what this subscriber wants to be called. A
    brand-new chat is asked this before anything else (see `_cmd_start`)
    -- answering it here is what unlocks the full welcome message and
    Quick setup button, same as if they'd typed `/start` after already
    having a name on file."""
    chat = update.effective_chat
    if not chat:
        return False
    args = context.args or []
    session = get_session()
    try:
        if not args:
            current = repo.get_subscriber_name(session, chat.id)
            _pending_prompts[chat.id] = "name"
            if current:
                await _reply(update, f"Right now I call you {current}.\n\n{_SETTINGS_PROMPTS['name']}")
            else:
                await _reply(update, _SETTINGS_PROMPTS["name"])
            return False

        name = " ".join(args).strip()
        if not name:
            await _reply(update, "That doesn't look like a name -- try typing it again.")
            return False
        first_time = repo.get_subscriber_name(session, chat.id) is None
        repo.set_subscriber_name(session, chat.id, name)
    finally:
        session.close()

    await _reply(update, f"✅ Got it, I'll call you {name}.")
    if first_time:
        await _reply(update, _welcome_text(name), reply_markup=_QUICK_SETUP_KEYBOARD)
    return True


# Maps a pending-prompt key (see _SETTINGS_PROMPTS) to the same handler its
# matching /command uses — defined after all four exist so _handle_plain_reply
# and the /settings buttons can dispatch to them without duplicating logic.
_PROMPTABLE_COMMANDS = {
    "name": _cmd_name,
    "budget": _cmd_budget,
    "minyear": _cmd_minyear,
    "threshold": _cmd_threshold,
    "radius": _cmd_radius,
    "mileage": _cmd_mileage,
    "make": _cmd_make,
    "fuel": _cmd_fuel,
    "transmission": _cmd_transmission,
}

# `becarscout run`'s log lines carry a timestamp + level prefix (see
# cli.py's main()) followed by one of these fixed messages per stage --
# used to translate the raw log into a plain-English summary instead of
# showing internal stage names to a non-technical user. score/notify's
# numbers are totals across every subscriber (the run refreshes results
# for everyone, not just whoever typed /find).
_SCRAPE_RE = re.compile(r"scrape: (\d+) new listings, (\d+) price changes")
_SCORE_RE = re.compile(r"score: (\d+) listings \((\d+) opportunities\)")
_NOTIFY_RE = re.compile(r"notify: (\d+) sent")


def _summarize_find_output(output: str) -> str:
    scrape_match = _SCRAPE_RE.search(output)
    score_match = _SCORE_RE.search(output)
    notify_match = _NOTIFY_RE.search(output)

    if not (scrape_match or score_match or notify_match):
        return "🔍 Done searching, but something went wrong along the way -- check with someone technical if this keeps happening."

    new_listings = int(scrape_match.group(1)) if scrape_match else None
    opportunities = int(score_match.group(2)) if score_match else 0
    sent = int(notify_match.group(1)) if notify_match else 0

    parts = ["🔍 Done searching!"]
    if new_listings is not None:
        parts.append(f"Found {new_listings} new listing{'s' if new_listings != 1 else ''}.")
    if sent > 0:
        parts.append(f"🚗 {sent} of them looked like a good deal -- sent {'them' if sent != 1 else 'it'} to you above!")
    elif opportunities > 0:
        parts.append("A good deal was found and already sent to you earlier.")
    else:
        parts.append("Nothing good enough to show you this time -- I'll keep checking every hour, or just say /find again anytime.")
    return " ".join(parts)


async def _cmd_find(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/find` — runs the full pipeline right now instead of waiting for
    the next hourly cron tick. Shells out to `uv run becarscout run` (the
    exact same command cron invokes) as a background subprocess rather
    than calling `run_full_pipeline()` in-process: `do_analyze` blocks
    synchronously per listing, and awaiting it directly here would freeze
    this bot process — no button presses or other commands would work
    until the whole run finished. A separate process is also exactly what
    already happens every hour (cron's `run` and this `listen` process are
    already two concurrent processes sharing the DB via WAL mode), so this
    adds no new concurrency risk. Since scrape/structure/analyze are
    shared and score/notify loop over every subscriber, one person's
    `/find` refreshes results for everyone using this bot, not just them."""
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    if chat_id in _find_running:
        await _reply(update, "🔎 Still searching from a moment ago -- I'll message you as soon as that's done.")
        return
    _find_running.add(chat_id)
    await _reply(update, "🔎 Searching Facebook Marketplace now -- this can take a few minutes, I'll message you again when I'm done.")
    asyncio.create_task(_run_find_pipeline(chat_id, update.get_bot()))


async def _send_typing_until_cancelled(bot: Bot, chat_id: int) -> None:
    """A Telegram "typing…" indicator only lasts ~5s, so it has to be
    resent periodically for the whole duration of a multi-minute /find
    run -- otherwise the chat looks frozen with no sign anything is
    happening. Cancelled from `_run_find_pipeline`'s `finally` once the
    subprocess finishes."""
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            logger.exception("Failed to send typing indicator for /find")
        await asyncio.sleep(4)


async def _run_find_pipeline(chat_id: int, bot: Bot) -> None:
    typing_task = asyncio.create_task(_send_typing_until_cancelled(bot, chat_id))
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                "uv", "run", "becarscout", "run",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
        except Exception:
            logger.exception("Failed to launch pipeline run for /find")
            await bot.send_message(chat_id=chat_id, text="😕 Couldn't start the search -- please tell whoever manages this bot.")
            return

        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        await bot.send_message(chat_id=chat_id, text=_summarize_find_output(output))
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task
        _find_running.discard(chat_id)


def _format_search_hit(row: ListingRow, scored: ScoredListing | None) -> str:
    stats = []
    if row.price_eur is not None:
        stats.append(f"€{row.price_eur:,}")
    if row.year is not None:
        stats.append(str(row.year))
    if row.mileage_km is not None:
        stats.append(f"{row.mileage_km:,} km")
    header = row.raw_title
    if stats:
        header += " -- " + " · ".join(stats)
    score_line = f"Score: {scored.score:+d}" if scored is not None else "Not yet scored for you"
    return f"{header}\n{score_line}\n{row.url}"


def _parse_search_args(args: list[str]) -> tuple[str, int | None]:
    """Splits off a trailing time-range token (see `_SEARCH_RANGE_DAYS`)
    from the rest of the keyword, e.g. ["golf", "week"] -> ("golf", 7).
    A range with no keyword (e.g. just `/search today`) is valid too --
    it means "show me everything new," not "search for the word today"."""
    if args and args[-1].lower() in _SEARCH_RANGE_DAYS:
        range_key = args[-1].lower()
        keyword = " ".join(args[:-1]).strip()
        return keyword, _SEARCH_RANGE_DAYS[range_key]
    return " ".join(args).strip(), None


async def _cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/search <keyword>` looks through listings already scraped for a
    make/model/title match, e.g. `/search golf`. Add a time range to also
    filter by how recently a listing was found, e.g. `/search golf week`
    -- or search a range alone, e.g. `/search today`, to see everything
    new without a keyword. Doesn't scrape anything new -- see /find for
    that."""
    chat = update.effective_chat
    if not chat:
        return
    keyword, range_days = _parse_search_args(context.args or [])
    if not keyword and range_days is None:
        await _reply(
            update,
            "🔍 What are you looking for? Type it after the command, e.g. /search golf -- "
            "or tap a range below to see everything found recently.",
            reply_markup=_search_range_keyboard(),
        )
        return

    _last_search_keyword[chat.id] = keyword
    since = datetime.now(timezone.utc) - timedelta(days=range_days) if range_days else None

    session = get_session()
    try:
        hits = repo.search_listings(session, keyword, since=since)
        scores_by_id = repo.get_user_scores_by_listing_ids(session, chat.id, [h.listing_id for h in hits])
    finally:
        session.close()

    range_note = f" from the last {range_days} day{'s' if range_days != 1 else ''}" if range_days else ""
    show_range_buttons = range_days is None

    if not hits:
        text = f'🔍 No "{keyword}" listings found{range_note}.' if keyword else f"🔍 Nothing found{range_note}."
        if show_range_buttons:
            text += " Try /find to search right now instead of waiting, or tap a range below."
        await _reply(update, text, reply_markup=_search_range_keyboard() if show_range_buttons else None)
        return

    if keyword:
        header = f'🔍 Found {len(hits)} match{"es" if len(hits) != 1 else ""} for "{keyword}"{range_note}:'
    else:
        header = f'🔍 Found {len(hits)} listing{"s" if len(hits) != 1 else ""}{range_note}:'
    lines = [header, ""]
    lines.append("\n\n".join(_format_search_hit(row, scores_by_id.get(row.listing_id)) for row in hits))
    await _reply(update, "\n".join(lines), reply_markup=_search_range_keyboard() if show_range_buttons else None)


async def _cmd_reviewfeedback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/reviewfeedback` — runs stage 7's LangGraph review agent over this
    subscriber's own accumulated 👍/👎 feedback and posts the patterns +
    suggested scoring weight changes it found. Advisory only: nothing
    changes until you send /validate."""
    chat = update.effective_chat
    if not chat:
        return
    await _reply(update, "Reviewing feedback so far, one moment...")
    result = run_feedback_review(chat.id)
    if result.get("skipped"):
        await _reply(update, result["skip_reason"])
        return
    await _reply(update, result["report_markdown"])


async def _cmd_validate(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/validate` — applies this subscriber's *last* `/reviewfeedback`
    run's suggested scoring weight changes for real. This is the one
    command that actually changes future scoring behavior for them;
    everything else in stage 7 is advisory until you send this."""
    chat = update.effective_chat
    if not chat:
        return
    applied = apply_latest_suggestions(chat.id)
    if applied is None:
        await _reply(update, "Nothing to apply yet — run /reviewfeedback first, then /validate if you agree with what it suggests.")
        return
    if not applied:
        await _reply(update, "The last review didn't suggest any changes.")
        return
    lines = ["✅ Applied — here's what changed:"]
    for s in applied:
        lines.append(f"- {s['weight']}: {s['current_value']} -> {s['new_value']}")
    lines.append("")
    lines.append("I'll score cars using these new settings from now on.")
    await _reply(update, "\n".join(lines))


_BOT_COMMANDS = [
    BotCommand("start", "What I do and how to set me up"),
    BotCommand("setup", "Quick guided setup (budget, year, area, pickiness)"),
    BotCommand("name", "Tell me what to call you"),
    BotCommand("find", "Search for cars right now"),
    BotCommand("search", "Look through cars I've already found, e.g. /search golf"),
    BotCommand("settings", "See and change your budget, year, area, pickiness"),
    BotCommand("budget", "Set your price range"),
    BotCommand("minyear", "Set the oldest year you'll consider"),
    BotCommand("threshold", "Set how picky I should be"),
    BotCommand("radius", "Set how far I should search (shared by everyone)"),
    BotCommand("mileage", "Set the highest mileage you'll accept"),
    BotCommand("make", "Only show certain brands, e.g. bmw, toyota"),
    BotCommand("fuel", "Only show certain fuel types, e.g. diesel"),
    BotCommand("transmission", "Only show automatic or manual"),
    BotCommand("reviewfeedback", "See patterns in the cars you liked/disliked"),
    BotCommand("validate", "Apply what the last review suggested"),
    BotCommand("cancel", "Stop whatever it's currently asking you"),
    BotCommand("help", "Show this list again"),
]

_QUICK_SETUP_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🚀 Quick setup", callback_data=f"{SETTINGS_CALLBACK_PREFIX}:wizard")]]
)


async def _cmd_start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/start` (Telegram's standard first-contact command) and `/help` —
    a plain-language welcome explaining what the bot does and how to set
    it up. A chat with no name on file yet is asked for one *first* --
    `_cmd_name` sends the full welcome (this same text) once they answer,
    so `/help` from a chat that already has a name just shows it again."""
    chat = update.effective_chat
    if not chat:
        return
    session = get_session()
    try:
        name = repo.get_subscriber_name(session, chat.id)
    finally:
        session.close()

    if name is None:
        _pending_prompts[chat.id] = "name"
        await _reply(update, "👋 Hi! I'm BE-CarScout. Before anything else -- what should I call you?")
        return

    await _reply(update, _welcome_text(name), reply_markup=_QUICK_SETUP_KEYBOARD)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registered as the Application's catch-all error handler -- without
    this, an unhandled exception inside any command/callback just vanishes
    into the container logs and the user sees nothing at all, which reads
    as "the bot is broken" with no way to tell. Best-effort: if even the
    error notification fails (e.g. the chat is unreachable), it's logged
    and swallowed rather than raised again."""
    logger.error("Unhandled error in a Telegram handler", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="😕 Something went wrong on my end -- try again in a moment.",
            )
        except Exception:
            logger.exception("Failed to notify the chat about the error")


async def _post_init(application) -> None:
    """Registers the command list Telegram shows as autocomplete
    suggestions when you type "/" in the chat — purely a discoverability
    aid, has no effect on which commands actually work."""
    await application.bot.set_my_commands(_BOT_COMMANDS)


def run_feedback_listener() -> None:
    """Blocks, polling for 👍/👎 button presses and commands until
    interrupted (Ctrl+C). Run this as a standing background process —
    `becarscout notify` only sends messages, it doesn't listen for
    replies or commands."""
    application = ApplicationBuilder().token(_get_token()).post_init(_post_init).build()
    # group=-1 runs before every other handler below, for every update --
    # this is what actually makes someone a subscriber (see its docstring).
    application.add_handler(TypeHandler(Update, _ensure_subscribed_middleware), group=-1)
    application.add_handler(
        CallbackQueryHandler(_handle_feedback_callback, pattern=rf"^{CALLBACK_PREFIX}:")
    )
    application.add_handler(
        CallbackQueryHandler(_handle_settings_callback, pattern=rf"^{SETTINGS_CALLBACK_PREFIX}:")
    )
    application.add_handler(CommandHandler("start", _cmd_start))
    application.add_handler(CommandHandler("help", _cmd_start))
    application.add_handler(CommandHandler("setup", _start_wizard))
    application.add_handler(CommandHandler("cancel", _cmd_cancel))
    application.add_handler(CommandHandler("find", _cmd_find))
    application.add_handler(CommandHandler("search", _cmd_search))
    application.add_handler(CommandHandler("settings", _cmd_settings))
    application.add_handler(CommandHandler("name", _cmd_name))
    application.add_handler(CommandHandler("budget", _cmd_budget))
    application.add_handler(CommandHandler("minyear", _cmd_minyear))
    application.add_handler(CommandHandler("threshold", _cmd_threshold))
    application.add_handler(CommandHandler("radius", _cmd_radius))
    application.add_handler(CommandHandler("mileage", _cmd_mileage))
    application.add_handler(CommandHandler("make", _cmd_make))
    application.add_handler(CommandHandler("fuel", _cmd_fuel))
    application.add_handler(CommandHandler("transmission", _cmd_transmission))
    application.add_handler(CommandHandler("reviewfeedback", _cmd_reviewfeedback))
    application.add_handler(CommandHandler("validate", _cmd_validate))
    # Must be added after every CommandHandler above -- filters.COMMAND
    # excludes slash commands, but handler order still matters for any
    # non-command text the other handlers don't claim.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_plain_reply))
    application.add_error_handler(_error_handler)
    logger.info("Listening for feedback button presses and commands... (Ctrl+C to stop)")
    application.run_polling()
