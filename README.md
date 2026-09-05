# BE-CarScout

Belgian used-car deal finder. See [Project.md](Project.md) for the full pipeline design
and build notes for every stage.

A 7-stage pipeline: scrape Facebook Marketplace → normalize fields → LLM-extract
condition signals → score against real market comps → gate on a threshold → deliver to
Telegram → remember your feedback for next time. Runs hourly, unattended, in Docker.

- **Stage 1** (`src/becarscout/scraper/`) — Playwright scraper across 5 Belgian hub cities.
- **Stage 2** (`src/becarscout/structurer/`) — deterministic regex/keyword extraction
  (price, year, mileage, make, fuel, transmission, location).
- **Stage 3** (`src/becarscout/analyzer/`) — Mistral, strict JSON schema, pulls out
  mechanical/administrative signals a regex can't (warning lights, timing belt
  replacement, BTW/margin scheme, export intent, ...).
- **Stage 4/5** (`src/becarscout/pricing/` + `src/becarscout/scoring/`) — price comps from
  2dehands.be and 2ememain.be (same underlying marketplace, bilingual front-ends — not
  independent sources, just broader coverage), combined with stage 3's signals into one
  auditable score + reasoning trail, filtered by a score threshold and a minimum year.
- **Stage 6** (`src/becarscout/notifier/`) — sends opportunity cards to Telegram with
  👍/👎 buttons; a separate standing process records button presses.
- **Stage 7** (`src/becarscout/feedback_agent/`) — a local mem0 memory store remembers
  every verdict (so a new card can say "2 similar cars you liked before"), and a small
  LangGraph agent (`becarscout feedback-review`) turns accumulated feedback into a report
  of patterns + suggested `scoring.py` tweaks. Advisory only, by design — it never edits
  `scoring.py` or the Telegram score itself; a human reads the report and decides.

**Persistence**: SQLite (`src/becarscout/db/`, one row per listing gaining columns as it
moves through stages) — this is what makes hourly runs cheap: only genuinely new listings
get scraped in full detail, sent to Mistral, or scored; everything already processed is
skipped by a plain `WHERE ... IS NULL` query per stage.

`src/becarscout/bot/` holds an unrelated, unused Telegram bot skeleton salvaged from an
earlier prototype (conversational brand/model picker, seller negotiation) — not wired
into anything; stage 6 was built fresh instead. See Project.md for why.

## Setup

```bash
uv sync
uv run playwright install chromium
```

`.env` needs:
```
MISTRAL_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```
Get a Mistral key at console.mistral.ai. For Telegram: message **@BotFather** →
`/newbot` for the token, then message your new bot once and run `uv run becarscout
whoami` to find your `TELEGRAM_CHAT_ID`.

**Facebook login is never automated** — a real, visible browser window opens for you to
log in normally (handles 2FA/checkpoints), and the session is reused after that:

```bash
uv run becarscout login
```

This is the one step that **cannot run in Docker** (no display in a container) — always
run it on a host machine first. It writes `data/.session/profile/`, which the container
picks up via the mounted `data/` volume.

## Running manually (one stage at a time)

```bash
uv run becarscout scrape      # -> new listings into the DB
uv run becarscout structure   # -> normalizes newly-scraped listings
uv run becarscout analyze     # -> LLM signals for newly-structured listings
uv run becarscout score       # -> price/condition score for newly-analyzed listings
uv run becarscout notify      # -> sends newly-scored opportunities to Telegram
```

Each command only processes what the previous stage left behind since the last run —
re-running `analyze` right after itself does nothing, for example. `scrape` also detects
when a known listing's price has changed (comparing parsed numbers, not raw text, so
formatting differences don't cause false positives) and automatically re-scores/
re-notifies it — a pure description edit with no price change isn't detectable this way
(would need revisiting every known listing's page every run). Useful `scrape` flags:
`--radius-km`, `--min-price`/`--max-price`, `--max-scrolls`, `--no-details` (skip detail
pages, faster/less data), `--headed` (visible browser, for debugging selectors). `score`
takes `--threshold` (default 20) and `--min-year` (default 2010 — cars from that year or
older never clear the gate regardless of score; `--no-min-year` disables it). `analyze`
takes `--model` (default `ministral-8b-latest`) and `--delay` (seconds between API calls,
default 1.0).

```bash
uv run becarscout rescore     # queue every scored listing for re-evaluation with current code
```

Incremental processing means a `scoring.py`/`baseline.py` fix only affects listings scored
*after* it ships — anything already scored keeps its old value until something resets it.
Run `rescore` then `score` (then `notify`) after a scoring-affecting fix to apply it
retroactively; `notified_at` is left alone, so this won't cause already-sent opportunities
to resend on their own.

```bash
uv run becarscout listen      # standing process: records 👍/👎 button presses
uv run becarscout feedback-review   # stage 7: patterns + suggested scoring.py tweaks
```

`feedback-review` needs at least 5 recorded verdicts to say anything (otherwise it tells
you so and exits) — it reads what `listen` has stored in mem0, writes a report to
`data/feedback/review_<timestamp>.md`, and prints it. On its own it never changes scoring;
see the Telegram commands below for the step that does.

## Controlling it from Telegram

Once `becarscout listen` is running (it's always running in Docker), these commands work
straight from the chat — no SSH, no redeploy. Typing `/` in the chat shows all of them as
autocomplete suggestions with a short description (registered via `setMyCommands` on
startup — see `_post_init` in `notifier/bot.py`), so you don't need to remember the exact
names. All bot replies use plain, non-technical language on purpose (no "scrape"/"score"
pipeline jargon) — anyone can use this without knowing how it works internally.

- `/start` or `/help` — a plain-language welcome message explaining what the bot does and
  how to set it up. Send this first if you're new.
- `/find` — runs the full pipeline right now (scrape → structure → analyze → score →
  notify) instead of waiting for the next hourly cron tick. Replies immediately, then
  messages again with a summary once the run finishes (can take a few minutes).
- `/search <keyword>` — looks through listings already scraped for a make/model/title
  match, e.g. `/search golf`. Doesn't scrape anything new — see `/find` for that.
- `/settings` — shows the current radius, budget, min year, and score threshold at once,
  with buttons to change any of them.
- `/budget <max>`, `/budget <min> <max>`, or `/budget off` — the price range future scrapes
  search within.
- `/minyear <year>` or `/minyear off` — cars from that year or older never reach Telegram,
  regardless of score (default 2010).
- `/threshold <n>` — the minimum score a listing needs to reach Telegram.
- `/radius <km>` — search radius around each Belgian hub city.
- `/reviewfeedback` — runs stage 7's review agent over your accumulated 👍/👎 and posts the
  patterns + suggested scoring weight changes it found.
- `/validate` — applies the *last* `/reviewfeedback`'s suggestions for real. This is the
  only command that changes scoring behavior; everything else in stage 7 is advisory until
  you send this. Nothing is applied automatically, ever.

`/budget`, `/minyear`, `/threshold`, and `/radius` also work as a two-step prompt: send the
command with no arguments (or tap its button under `/settings`) and the bot asks for the
value, then applies whatever you type next — no need to remember the exact argument syntax.

Every setting above takes effect from the *next* scrape/score run onward — cron always
invokes `becarscout run` with no flags, and it resolves each parameter as
explicit CLI flag → Telegram-set value → hardcoded default, in that order.

Run the whole chain in one call (what cron actually invokes):

```bash
uv run becarscout run
```

Each of its 5 stages is independently try/excepted — if Facebook changes their markup
mid-run, already-processed backlog still reaches Telegram instead of the whole run being
a no-op.

## Running in Docker (the hourly, unattended setup)

```bash
uv run becarscout login        # once, on the host — see above
docker compose up -d --build
```

That's it — the container runs `becarscout run` once immediately, then every hour on the
hour via cron (see `docker/crontab`), for as long as it's up (`restart: unless-stopped`).
`docker-compose.yml` mounts `./data` into the container, so the login session, the SQLite
DB, the 2dehands.be pricing cache, and `data/pipeline.log` all persist across restarts.

```bash
docker compose logs -f         # follow pipeline output live
docker compose down             # stop
```

Notes:
- Never run `becarscout login` inside the container — see above.
- `data/pipeline.log` grows unbounded (no rotation set up yet) — worth an eye over time.
- A `TimedOut` from Telegram in the logs is handled gracefully (retried with backoff,
  and even if every retry fails, unset opportunities just get picked up and sent on the
  next hourly run — nothing is lost since delivery status lives in the DB, not a
  one-shot in-memory list).

## Note on Terms of Service

Scraping Facebook Marketplace, and the price-comps lookup against 2dehands.be, may be
subject to those sites' Terms of Service — this is for personal use; review the ToS
before running it. (AutoScout24.be and Gocar.be were tried first for comps and actively
blocked automated access — not used here.)
