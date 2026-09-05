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
from .scoring import DEFAULT_MIN_YEAR, DEFAULT_THRESHOLD, apply_decision_gate, score_listings
from .scraper import launch_login_browser, scrape_belgium_cars
from .settings import PipelineSettings
from .structurer import structure_listings

logger = logging.getLogger(__name__)


# ---- core stage functions (reusable by both individual commands and `run`) ----


async def do_scrape(
    *,
    radius_km: int = 100,
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
    finally:
        session.close()

    new_listings, price_changed_listings = await scrape_belgium_cars(
        radius_km=radius_km,
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


async def do_score(*, threshold: int = DEFAULT_THRESHOLD, min_year: int | None = DEFAULT_MIN_YEAR) -> tuple[int, int]:
    session = get_session()
    try:
        pending = repo.get_listings_needing_scoring(session)
        if not pending:
            return 0, 0
        structured_listings = [s for s, _ in pending]
        signals_by_id = {s.listing_id: sig for s, sig in pending if sig is not None}
        weights = repo.get_scoring_weights(session)
        scored = await score_listings(structured_listings, signals_by_id, weights)
        scored = apply_decision_gate(scored, threshold=threshold, min_year=min_year)
        repo.save_scores(session, scored)
        opportunities = sum(1 for s in scored if s.above_threshold)
        return len(scored), opportunities
    finally:
        session.close()


async def do_notify() -> int:
    session = get_session()
    try:
        opportunities = repo.get_unnotified_opportunities(session)
        if not opportunities:
            return 0
        opportunities.sort(key=lambda s: s.score, reverse=True)
        sent_ids = await send_new_opportunities(opportunities)
        repo.mark_notified(session, sent_ids)
        return len(sent_ids)
    finally:
        session.close()


async def run_full_pipeline(
    *,
    radius_km: int = 100,
    min_price: int | None = None,
    max_price: int | None = None,
    max_scrolls: int = 8,
    model: str = DEFAULT_MODEL,
    delay: float = 1.0,
    threshold: int = DEFAULT_THRESHOLD,
    min_year: int | None = DEFAULT_MIN_YEAR,
    headless: bool = True,
) -> None:
    """Runs every stage in sequence — used by `becarscout run` (and cron).
    Each stage is independently guarded: a failure in one (e.g. Facebook
    changed their markup) still lets already-processed backlog move
    through the remaining stages instead of the whole hourly run being a
    no-op."""
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
        scored_count, opportunity_count = await do_score(threshold=threshold, min_year=min_year)
        logger.info("score: %d listings (%d opportunities)", scored_count, opportunity_count)
    except Exception:
        logger.exception("score stage failed")

    try:
        sent_count = await do_notify()
        logger.info("notify: %d sent", sent_count)
    except Exception:
        logger.exception("notify stage failed")


# ---- CLI glue ----


def _current_settings() -> PipelineSettings:
    """What `becarscout run`/`scrape`/`score` fall back to for any
    parameter not explicitly given on the command line — this is what
    lets `/budget`, `/minyear`, `/threshold`, `/radius` (Telegram) change
    the *next* hourly cron run's behavior without a redeploy, since cron
    always invokes `becarscout run` with zero flags."""
    session = get_session()
    try:
        return repo.get_pipeline_settings(session)
    finally:
        session.close()


def _cmd_login(_args: argparse.Namespace) -> None:
    asyncio.run(launch_login_browser())


def _cmd_scrape(args: argparse.Namespace) -> None:
    settings = _current_settings()
    new_count, price_changed_count = asyncio.run(
        do_scrape(
            radius_km=args.radius_km if args.radius_km is not None else settings.radius_km,
            min_price=args.min_price if args.min_price is not None else settings.min_price,
            max_price=args.max_price if args.max_price is not None else settings.max_price,
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


def _cmd_score(args: argparse.Namespace) -> None:
    settings = _current_settings()
    min_year = None if args.no_min_year else (args.min_year if args.min_year is not None else settings.min_year)
    threshold = args.threshold if args.threshold is not None else settings.threshold
    count, opportunities = asyncio.run(do_score(threshold=threshold, min_year=min_year))
    year_note = f", newer than {min_year}" if min_year is not None else ""
    print(f"Scored {count} listings ({opportunities} above threshold {threshold}{year_note})")


def _cmd_notify(_args: argparse.Namespace) -> None:
    sent = asyncio.run(do_notify())
    print(f"Sent {sent} new opportunit{'y' if sent == 1 else 'ies'}")


def _cmd_rescore(_args: argparse.Namespace) -> None:
    session = get_session()
    try:
        count = repo.reset_scoring_for_rescore(session)
    finally:
        session.close()
    print(f"{count} listings queued for re-scoring — run `becarscout score` (then `notify`) to apply it")


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


def _cmd_feedback_review(_args: argparse.Namespace) -> None:
    result = run_feedback_review()
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
    print("Chat IDs found (use the right one as TELEGRAM_CHAT_ID in .env):")
    for chat_id, label in chats:
        print(f"  {chat_id}  ({label})")


def _cmd_run(args: argparse.Namespace) -> None:
    # cron invokes this with zero flags every time, so this is the one
    # place where the Telegram-set settings (/budget, /minyear,
    # /threshold, /radius) actually take effect hour to hour — an
    # explicit CLI flag still wins if one is given.
    settings = _current_settings()
    min_year = None if args.no_min_year else (args.min_year if args.min_year is not None else settings.min_year)
    asyncio.run(
        run_full_pipeline(
            radius_km=args.radius_km if args.radius_km is not None else settings.radius_km,
            min_price=args.min_price if args.min_price is not None else settings.min_price,
            max_price=args.max_price if args.max_price is not None else settings.max_price,
            max_scrolls=args.max_scrolls,
            model=args.model,
            delay=args.delay,
            threshold=args.threshold if args.threshold is not None else settings.threshold,
            min_year=min_year,
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
    scrape_parser.add_argument("--radius-km", type=int, default=None, help="Default: current /radius setting (100 if never changed)")
    scrape_parser.add_argument("--min-price", type=int, default=None, help="Default: current /budget setting")
    scrape_parser.add_argument("--max-price", type=int, default=None, help="Default: current /budget setting")
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

    score_parser = subparsers.add_parser(
        "score", help="Score newly-analyzed listings (stages 4-5)"
    )
    score_parser.add_argument("--threshold", type=int, default=None, help=f"Default: current /threshold setting ({DEFAULT_THRESHOLD} if never changed)")
    score_parser.add_argument("--min-year", type=int, default=None, help=f"Default: current /minyear setting ({DEFAULT_MIN_YEAR} if never changed)")
    score_parser.add_argument("--no-min-year", action="store_true", help="Disable the year cutoff entirely")
    score_parser.set_defaults(func=_cmd_score)

    notify_parser = subparsers.add_parser(
        "notify", help="Send newly-scored above-threshold opportunities to Telegram (stage 6)"
    )
    notify_parser.set_defaults(func=_cmd_notify)

    rescore_parser = subparsers.add_parser(
        "rescore",
        help="Queue every already-scored listing for re-scoring with current code (run after a scoring/baseline fix ships)",
    )
    rescore_parser.set_defaults(func=_cmd_rescore)

    listen_parser = subparsers.add_parser(
        "listen", help="Run the standing bot process that listens for thumbs up/down feedback button presses"
    )
    listen_parser.set_defaults(func=_cmd_listen)

    feedback_review_parser = subparsers.add_parser(
        "feedback-review",
        help="Stage 7: review accumulated thumbs up/down feedback for patterns and suggested scoring.py tweaks (advisory only, run manually)",
    )
    feedback_review_parser.set_defaults(func=_cmd_feedback_review)

    whoami_parser = subparsers.add_parser(
        "whoami", help="Find your Telegram chat_id (after sending the bot a message) to put in .env"
    )
    whoami_parser.set_defaults(func=_cmd_whoami)

    run_parser = subparsers.add_parser(
        "run", help="Run the full pipeline once: scrape, structure, analyze, score, notify — meant for cron"
    )
    run_parser.add_argument("--radius-km", type=int, default=None, help="Default: current /radius setting")
    run_parser.add_argument("--min-price", type=int, default=None, help="Default: current /budget setting")
    run_parser.add_argument("--max-price", type=int, default=None, help="Default: current /budget setting")
    run_parser.add_argument("--max-scrolls", type=int, default=8)
    run_parser.add_argument("--model", default=DEFAULT_MODEL)
    run_parser.add_argument("--delay", type=float, default=1.0)
    run_parser.add_argument("--threshold", type=int, default=None, help="Default: current /threshold setting")
    run_parser.add_argument("--min-year", type=int, default=None, help="Default: current /minyear setting")
    run_parser.add_argument("--no-min-year", action="store_true", help="Disable the year cutoff entirely")
    run_parser.add_argument("--headed", action="store_true")
    run_parser.set_defaults(func=_cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
