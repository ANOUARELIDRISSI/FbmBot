from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .analyzer import DEFAULT_MODEL, analyze_descriptions
from .db import get_session
from .db import repository as repo
from .feedback_agent import run_feedback_review
from .notifier import find_chat_ids, run_feedback_listener, send_new_opportunities
from .scoring import apply_decision_gate, resolve_baselines, score_listing
from .scraper import launch_login_browser, scrape_belgium_cars
from .structurer import structure_listings

logger = logging.getLogger(__name__)


# ---- core stage functions (reusable by both individual commands and `run`) ----
#
# Multi-user note (2026-09-06): scrape/structure/analyze/resolve-baselines
# stay shared, one pass regardless of how many people use the bot. score
# and notify loop over every subscriber internally, using each one's own
# settings/weights -- see db/models.py's module docstring for why the
# split lands where it does.


async def do_scrape(
    *,
    radius_km: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    max_scrolls: int = 8,
    fetch_details: bool = True,
    headless: bool = True,
) -> tuple[int, int]:
    """Returns (new_count, price_changed_count)."""
    session = get_session()
    try:
        known_ids = frozenset(repo.get_all_listing_ids(session))
        known_price_texts = repo.get_known_price_texts(session)
        resolved_radius = radius_km if radius_km is not None else repo.get_global_scrape_radius_km(session)
    finally:
        session.close()

    new_listings, price_changed_listings = await scrape_belgium_cars(
        radius_km=resolved_radius,
        min_price=min_price,
        max_price=max_price,
        max_scrolls=max_scrolls,
        fetch_details=fetch_details,
        headless=headless,
        known_ids=known_ids,
        known_price_texts=known_price_texts,
    )

    session = get_session()
    try:
        new_count = repo.upsert_raw_listings(session, new_listings)
        for listing in price_changed_listings:
            repo.update_changed_listing(session, listing)
        if price_changed_listings:
            logger.info(
                "%d listing(s) had a price change — will re-run structure/score/notify for them",
                len(price_changed_listings),
            )
        return new_count, len(price_changed_listings)
    finally:
        session.close()


def do_structure() -> int:
    session = get_session()
    try:
        raw_listings = repo.get_listings_needing_structuring(session)
        if not raw_listings:
            return 0
        structured = structure_listings(raw_listings)
        repo.save_structured(session, structured)
        return len(structured)
    finally:
        session.close()


def do_analyze(*, model: str = DEFAULT_MODEL, delay: float = 1.0) -> tuple[int, int]:
    session = get_session()
    try:
        to_analyze = repo.get_listings_needing_analysis(session)
        if not to_analyze:
            return 0, 0
        signals = analyze_descriptions(to_analyze, model=model, delay_s=delay)
        repo.save_signals(session, signals)
        failed = sum(1 for s in signals if s.extraction_failed)
        return len(signals), failed
    finally:
        session.close()


async def do_resolve_baselines() -> int:
    """Shared step: resolves the market-price baseline for every analyzed
    listing that doesn't have one yet — once per listing, regardless of
    subscriber count (see module docstring)."""
    session = get_session()
    try:
        pending = repo.get_listings_needing_baseline(session)
        if not pending:
            return 0
    finally:
        session.close()

    baselines = await resolve_baselines(pending)

    session = get_session()
    try:
        repo.save_baselines(session, baselines)
        return len(baselines)
    finally:
        session.close()


async def do_score() -> tuple[int, int]:
    """Per-subscriber: scores every listing with a resolved baseline that
    hasn't been scored for that subscriber yet, using their own weights
    and gate settings. Pure computation, no network — cheap even with
    several subscribers, unlike `do_resolve_baselines`."""
    session = get_session()
    try:
        subscribers = repo.get_subscribers(session)
        total_scored = 0
        total_opportunities = 0
        for chat_id in subscribers:
            pending = repo.get_listings_needing_scoring(session, chat_id)
            if not pending:
                continue
            weights = repo.get_scoring_weights(session, chat_id)
            settings = repo.get_pipeline_settings(session, chat_id)
            scored = [score_listing(structured, signals, baseline, weights) for structured, signals, baseline in pending]
            scored = apply_decision_gate(
                scored,
                threshold=settings.threshold,
                min_year=settings.min_year,
                max_mileage_km=settings.max_mileage_km,
                makes=settings.makes,
                fuel_types=settings.fuel_types,
                transmission=settings.transmission,
            )
            repo.save_user_scores(session, chat_id, scored)
            total_scored += len(scored)
            total_opportunities += sum(1 for s in scored if s.above_threshold)
        return total_scored, total_opportunities
    finally:
        session.close()


async def do_notify() -> int:
    """Per-subscriber: sends each subscriber only their own unnotified
    above-threshold opportunities."""
    session = get_session()
    try:
        subscribers = repo.get_subscribers(session)
        total_sent = 0
        for chat_id in subscribers:
            opportunities = repo.get_unnotified_opportunities(session, chat_id)
            if not opportunities:
                continue
            opportunities.sort(key=lambda s: s.score, reverse=True)
            sent_ids = await send_new_opportunities(chat_id, opportunities)
            repo.mark_notified(session, chat_id, sent_ids)
            total_sent += len(sent_ids)
        return total_sent
    finally:
        session.close()


async def run_full_pipeline(
    *,
    radius_km: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    max_scrolls: int = 8,
    model: str = DEFAULT_MODEL,
    delay: float = 1.0,
    headless: bool = True,
) -> None:
    """Runs every stage in sequence — used by `becarscout run` (and cron).
    Each stage is independently guarded: a failure in one (e.g. Facebook
    changed their markup) still lets already-processed backlog move
    through the remaining stages instead of the whole hourly run being a
    no-op. `score`/`notify` loop over every subscriber internally."""
    try:
        new_count, price_changed_count = await do_scrape(
            radius_km=radius_km, min_price=min_price, max_price=max_price,
            max_scrolls=max_scrolls, headless=headless,
        )
        logger.info("scrape: %d new listings, %d price changes", new_count, price_changed_count)
    except Exception:
        logger.exception("scrape stage failed")

    try:
        structured_count = do_structure()
        logger.info("structure: %d listings", structured_count)
    except Exception:
        logger.exception("structure stage failed")

    try:
        analyzed_count, failed_count = do_analyze(model=model, delay=delay)
        logger.info("analyze: %d listings (%d failed)", analyzed_count, failed_count)
    except Exception:
        logger.exception("analyze stage failed")

    try:
        baseline_count = await do_resolve_baselines()
        logger.info("baseline: %d listings", baseline_count)
    except Exception:
        logger.exception("baseline stage failed")

    try:
        scored_count, opportunity_count = await do_score()
        logger.info("score: %d listings (%d opportunities)", scored_count, opportunity_count)
    except Exception:
        logger.exception("score stage failed")

    try:
        sent_count = await do_notify()
        logger.info("notify: %d sent", sent_count)
    except Exception:
        logger.exception("notify stage failed")


# ---- CLI glue ----


def _cmd_login(_args: argparse.Namespace) -> None:
    asyncio.run(launch_login_browser())


def _cmd_scrape(args: argparse.Namespace) -> None:
    new_count, price_changed_count = asyncio.run(
        do_scrape(
            radius_km=args.radius_km,
            min_price=args.min_price,
            max_price=args.max_price,
            max_scrolls=args.max_scrolls,
            fetch_details=not args.no_details,
            headless=not args.headed,
        )
    )
    print(f"{new_count} new listings saved to the database ({price_changed_count} price changes detected)")


def _cmd_structure(_args: argparse.Namespace) -> None:
    count = do_structure()
    print(f"Structured {count} listings")


def _cmd_analyze(args: argparse.Namespace) -> None:
    count, failed = do_analyze(model=args.model, delay=args.delay)
    print(f"Analyzed {count} listings ({failed} failed)")


def _cmd_baseline(_args: argparse.Namespace) -> None:
    count = asyncio.run(do_resolve_baselines())
    print(f"Resolved a market-price baseline for {count} listings")


def _cmd_score(_args: argparse.Namespace) -> None:
    count, opportunities = asyncio.run(do_score())
    print(f"Scored {count} listing(s) across all subscribers ({opportunities} cleared someone's gate)")


def _cmd_notify(_args: argparse.Namespace) -> None:
    sent = asyncio.run(do_notify())
    print(f"Sent {sent} new opportunit{'y' if sent == 1 else 'ies'}")


def _cmd_rescore(args: argparse.Namespace) -> None:
    session = get_session()
    try:
        count = repo.reset_scoring_for_rescore(session, chat_id=args.chat_id)
    finally:
        session.close()
    who = f"subscriber {args.chat_id}" if args.chat_id is not None else "every subscriber"
    print(f"{count} listing-score(s) queued for re-scoring ({who}) — run `becarscout score` (then `notify`) to apply it")


def _cmd_listen(_args: argparse.Namespace) -> None:
    run_feedback_listener()


def _print_safe(text: str) -> None:
    """`print()` alone crashes on a Windows console whose codepage (e.g.
    cp1252) can't encode a character — a real risk here since the report
    text comes from an LLM and isn't fully under our control. Falls back
    to replacing unencodable characters instead of raising."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write(text.encode(encoding, errors="replace"))
        sys.stdout.buffer.write(b"\n")


def _cmd_feedback_review(args: argparse.Namespace) -> None:
    result = run_feedback_review(args.chat_id)
    if result.get("skipped"):
        _print_safe(result["skip_reason"])
        return
    _print_safe(f"Review written to {result['report_path']}")
    _print_safe("")
    _print_safe(result["report_markdown"])


def _cmd_whoami(_args: argparse.Namespace) -> None:
    chats = asyncio.run(find_chat_ids())
    if not chats:
        print("No messages found yet — send your bot a message on Telegram first, then run this again.")
        return
    print("Chat IDs found (use one as --chat-id, or just message the bot to subscribe):")
    for chat_id, label in chats:
        print(f"  {chat_id}  ({label})")


def _cmd_run(args: argparse.Namespace) -> None:
    asyncio.run(
        run_full_pipeline(
            radius_km=args.radius_km,
            min_price=args.min_price,
            max_price=args.max_price,
            max_scrolls=args.max_scrolls,
            model=args.model,
            delay=args.delay,
            headless=not args.headed,
        )
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx (used by both the Mistral and python-telegram-bot SDKs) logs
    # full request URLs at INFO level — for Telegram that URL contains the
    # bot token in plaintext. Quieting it here keeps API keys/tokens out
    # of logs, including the persisted docker/data/pipeline.log.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(prog="becarscout")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="Save a Facebook login session for scraping")
    login_parser.set_defaults(func=_cmd_login)

    scrape_parser = subparsers.add_parser("scrape", help="Scrape car listings across Belgium into the database")
    scrape_parser.add_argument("--radius-km", type=int, default=None, help="Default: current /radius setting (shared by every subscriber)")
    scrape_parser.add_argument("--min-price", type=int, default=None, help="Narrows the live scrape itself (admin-only knob; subscriber /budget is a separate post-scrape filter)")
    scrape_parser.add_argument("--max-price", type=int, default=None)
    scrape_parser.add_argument("--max-scrolls", type=int, default=8)
    scrape_parser.add_argument(
        "--no-details", action="store_true",
        help="Skip visiting each listing page for full description/photos (faster, less data)",
    )
    scrape_parser.add_argument(
        "--headed", action="store_true",
        help="Run with a visible browser window (useful for debugging selectors)",
    )
    scrape_parser.set_defaults(func=_cmd_scrape)

    structure_parser = subparsers.add_parser(
        "structure", help="Normalize newly-scraped listings into structured fields"
    )
    structure_parser.set_defaults(func=_cmd_structure)

    analyze_parser = subparsers.add_parser(
        "analyze", help="Run each newly-structured listing's description through the LLM analyzer (stage 3)"
    )
    analyze_parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Mistral model to use (default: {DEFAULT_MODEL})")
    analyze_parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between API calls (default 1.0)")
    analyze_parser.set_defaults(func=_cmd_analyze)

    baseline_parser = subparsers.add_parser(
        "baseline", help="Resolve the shared market-price baseline for newly-analyzed listings (stage 4, shared half)"
    )
    baseline_parser.set_defaults(func=_cmd_baseline)

    score_parser = subparsers.add_parser(
        "score", help="Score newly-baselined listings for every subscriber, using each one's own weights and gate settings"
    )
    score_parser.set_defaults(func=_cmd_score)

    notify_parser = subparsers.add_parser(
        "notify", help="Send each subscriber their own newly-scored above-threshold opportunities (stage 6)"
    )
    notify_parser.set_defaults(func=_cmd_notify)

    rescore_parser = subparsers.add_parser(
        "rescore",
        help="Queue scored listings for re-scoring with current code (run after a scoring/baseline fix ships)",
    )
    rescore_parser.add_argument("--chat-id", type=int, default=None, help="Only this subscriber; default: every subscriber")
    rescore_parser.set_defaults(func=_cmd_rescore)

    listen_parser = subparsers.add_parser(
        "listen", help="Run the standing bot process that listens for thumbs up/down feedback button presses"
    )
    listen_parser.set_defaults(func=_cmd_listen)

    feedback_review_parser = subparsers.add_parser(
        "feedback-review",
        help="Stage 7: review one subscriber's accumulated thumbs up/down feedback for patterns and suggested weight changes (advisory only)",
    )
    feedback_review_parser.add_argument("--chat-id", type=int, required=True, help="Whose feedback to review (see `whoami`)")
    feedback_review_parser.set_defaults(func=_cmd_feedback_review)

    whoami_parser = subparsers.add_parser(
        "whoami", help="Find a Telegram chat_id (after that chat has messaged the bot)"
    )
    whoami_parser.set_defaults(func=_cmd_whoami)

    run_parser = subparsers.add_parser(
        "run", help="Run the full pipeline once: scrape, structure, analyze, baseline, score, notify — meant for cron"
    )
    run_parser.add_argument("--radius-km", type=int, default=None, help="Default: current /radius setting (shared by every subscriber)")
    run_parser.add_argument("--min-price", type=int, default=None, help="Narrows the live scrape itself (admin-only knob)")
    run_parser.add_argument("--max-price", type=int, default=None)
    run_parser.add_argument("--max-scrolls", type=int, default=8)
    run_parser.add_argument("--model", default=DEFAULT_MODEL)
    run_parser.add_argument("--delay", type=float, default=1.0)
    run_parser.add_argument("--headed", action="store_true")
    run_parser.set_defaults(func=_cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
