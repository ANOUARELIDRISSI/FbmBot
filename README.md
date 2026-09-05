# BE-CarScout

Belgian used-car deal finder. See [Project.md](Project.md) for the full pipeline design
and build notes for every stage.

A 7-stage pipeline: scrape Facebook Marketplace → normalize fields → LLM-extract
condition signals → score against real market comps → gate on a threshold → deliver to
Telegram → remember your feedback for next time. Runs hourly, unattended, in Docker.

**Multi-user**: anyone who messages the bot gets their own budget, year cutoff,
pickiness, mileage/brand/fuel/transmission filters, scoring weights, notifications, and
feedback memory — fully isolated per Telegram chat. Scraping (and its one shared
parameter, search radius) is the only thing everyone has in common: one shared pool of
listings, each person's own filter on top. See Project.md's stage 6/7 notes for how the
split actually works under the hood.

- **Stage 1** (`src/becarscout/scraper/`) — Playwright scraper across 5 Belgian hub cities.
- **Stage 2** (`src/becarscout/structurer/`) — deterministic regex/keyword extraction
  (price, year, mileage, make, fuel, transmission, location).
- **Stage 3** (`src/becarscout/analyzer/`) — Mistral, strict JSON schema, pulls out
  mechanical/administrative signals a regex can't (warning lights, timing belt
  replacement, BTW/margin scheme, export intent, ...).
- **Stage 4/5** (`src/becarscout/pricing/` + `src/becarscout/scoring/`) — price comps from
  2dehands.be and 2ememain.be (same underlying marketplace, bilingual front-ends — not
  independent sources, just broader coverage) resolve one shared market-price baseline per
  listing; each subscriber's own condition weights and gate (score threshold, minimum
  year, mileage, brand, fuel, transmission) then combine with it into their own auditable
  score + reasoning trail.
- **Stage 6** (`src/becarscout/notifier/`) — sends each subscriber their own opportunity
  cards with 👍/👎 buttons; a separate standing process records button presses and
  subscribes any chat that messages the bot.
- **Stage 7** (`src/becarscout/feedback_agent/`) — a local mem0 memory store remembers
  every verdict per subscriber (so a new card can say "2 similar cars you liked before"),
  and a small LangGraph agent (`becarscout feedback-review --chat-id`) turns one
  subscriber's accumulated feedback into a report of patterns + suggested weight tweaks
  for them specifically. Advisory only, by design — it never edits scoring on its own;
  that person reads the report and decides via `/validate`.

**Persistence**: SQLite (`src/becarscout/db/`). Scrape/structure/analyze/baseline stay one
row per listing gaining columns as it moves through stages — this is what makes hourly
runs cheap: only genuinely new listings get scraped in full detail, sent to Mistral, or
have their market-price baseline resolved; everything already processed is skipped by a
plain `WHERE ... IS NULL` query per stage. Scoring, the decision gate, and notifications
are per-subscriber instead (a separate table keyed by chat_id + listing_id), since two
people can have different budgets, weights, and thresholds against the exact same shared
listing.

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
`/newbot` for the token. `TELEGRAM_CHAT_ID` is optional now that the bot supports
multiple people — anyone can just message the bot and send `/start` to subscribe. Set it
only if you're upgrading a deployment that predates multi-user support: on first startup
it's used once to carry your existing settings, weights, and already-sent notifications
over to a real per-chat profile instead of resetting them (see `db/engine.py`'s
migration). `uv run becarscout whoami` finds a chat_id from recent messages, for this or
for `--chat-id` on the admin CLI commands below.

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
uv run becarscout scrape      # -> new listings into the DB (shared)
uv run becarscout structure   # -> normalizes newly-scraped listings (shared)
uv run becarscout analyze     # -> LLM signals for newly-structured listings (shared)
uv run becarscout baseline    # -> resolves the shared market-price baseline (stage 4, shared half)
uv run becarscout score       # -> scores newly-baselined listings for every subscriber
uv run becarscout notify      # -> sends each subscriber their own newly-scored opportunities
```

Each command only processes what the previous stage left behind since the last run —
re-running `analyze` right after itself does nothing, for example. `scrape` also detects
when a known listing's price has changed (comparing parsed numbers, not raw text, so
formatting differences don't cause false positives) and invalidates every subscriber's
score for it so it flows back through `score`/`notify` — a pure description edit with no
price change isn't detectable this way (would need revisiting every known listing's page
every run). Useful `scrape` flags: `--radius-km` (default: current `/radius` setting,
shared by everyone), `--min-price`/`--max-price` (an admin-only knob that narrows the live
scrape itself — a subscriber's own `/budget` is a separate filter applied afterward, not
this), `--max-scrolls`, `--no-details` (skip detail pages, faster/less data), `--headed`
(visible browser, for debugging selectors). `analyze` takes `--model` (default
`ministral-8b-latest`) and `--delay` (seconds between API calls, default 1.0). `score` and
`notify` take no flags at all — they loop over every subscriber internally, using each
one's own settings/weights (see `/budget`, `/threshold`, etc. below); there's no single
"the" gate to override from the CLI anymore.

```bash
uv run becarscout rescore [--chat-id ID]    # queue scored listings for re-evaluation with current code
```

Incremental processing means a `scoring.py`/`baseline.py` fix only affects listings scored
*after* it ships — anything already scored keeps its old value until something resets it.
Run `rescore` then `score` (then `notify`) after a scoring-affecting fix to apply it
retroactively; `notified_at` is left alone, so this won't cause already-sent opportunities
to resend on their own. Omit `--chat-id` to re-queue every subscriber.

```bash
uv run becarscout listen                              # standing process: records 👍/👎, runs the bot
uv run becarscout feedback-review --chat-id ID         # stage 7: one subscriber's patterns + suggested weight changes
```

`feedback-review` needs at least 5 recorded verdicts *from that chat_id* to say anything
(otherwise it tells you so and exits) — it reads what `listen` has stored in mem0 for
them specifically, writes a report to `data/feedback/review_<chat_id>_<timestamp>.md`,
and prints it. On its own it never changes scoring; see the Telegram commands below for
the step that does (the day-to-day equivalent of these last two commands is
`/reviewfeedback` and `/validate` on Telegram, scoped to whoever sends them).

## Controlling it from Telegram

Once `becarscout listen` is running (it's always running in Docker), these commands work
straight from the chat — no SSH, no redeploy. Typing `/` in the chat shows all of them as
autocomplete suggestions with a short description (registered via `setMyCommands` on
startup — see `_post_init` in `notifier/bot.py`), so you don't need to remember the exact
names. All bot replies use plain, non-technical language on purpose (no "scrape"/"score"
pipeline jargon) — anyone can use this without knowing how it works internally.

Every setting below is personal to whoever's chatting — your budget, filters, weights,
notifications, and feedback are yours alone — **except `/radius`**, which is shared by
everyone using this bot (there's no per-listing distance figure to filter by afterward, so
one radius has to define what gets scraped for everyone; see Project.md).

- `/start` or `/help` — a plain-language welcome message explaining what the bot does and
  how to set it up, with a "🚀 Quick setup" button. Sending this (or really any message)
  also subscribes you — no separate registration step.
- `/setup` — a guided flow through the four core settings (budget, min year, radius,
  threshold) one question at a time, instead of tapping four separate `/settings` buttons.
- `/cancel` — stops whatever it's currently asking you (a pending prompt or an in-progress
  `/setup`) without changing anything.
- `/find` — runs the full pipeline right now (scrape → structure → analyze → score →
  notify) instead of waiting for the next hourly cron tick. Since scraping is shared, this
  refreshes results for *everyone* using the bot, not just whoever sent it. Shows a
  "typing…" indicator the whole time so a multi-minute wait doesn't look like the bot
  froze, and a second `/find` while one's already running just says so instead of starting
  an overlapping run. Replies immediately, then messages again with a summary once the run
  finishes.
- `/search <keyword>` — looks through listings already scraped for a make/model/title
  match, e.g. `/search golf`, showing your own score for each hit. Add a time range to
  filter by how recently a listing was found: `/search golf week`, or just `/search today`
  / `/search 3days` with no keyword to see everything new. Doesn't scrape anything new —
  see `/find` for that.
- `/settings` — shows every filter below at once, with buttons to change any of them.
- `/budget <max>`, `/budget <min> <max>`, or `/budget off` — your personal price range,
  applied after the shared scrape (not a live search-query narrower) — see `/radius` above
  for the one thing that actually is shared.
- `/minyear <year>` or `/minyear off` — cars from that year or older never reach you,
  regardless of score (default 2010).
- `/threshold <n>` — the minimum score a listing needs to reach you.
- `/radius <km>` — search radius around each Belgian hub city, shared by every subscriber.
- `/mileage <km>` or `/mileage off` — cars with more mileage than that never reach you,
  regardless of score.
- `/make <brand, brand, ...>` or `/make off` — only show specific brands, e.g. `/make bmw,
  toyota`. Unlike the filters above, a listing whose brand wasn't detected does *not* pass
  once this is set (see `scoring/gate.py`'s docstring for why that's a deliberate
  difference).
- `/fuel <type, type, ...>` or `/fuel off` — only show specific fuel types (`electric`,
  `hybrid`, `lpg`, `diesel`, `petrol`).
- `/transmission <automatic|manual>` or `/transmission off` — only show one gearbox type.
- `/reviewfeedback` — runs stage 7's review agent over your accumulated 👍/👎 and posts the
  patterns + suggested scoring weight changes it found.
- `/validate` — applies the *last* `/reviewfeedback`'s suggestions for real. This is the
  only command that changes scoring behavior; everything else in stage 7 is advisory until
  you send this. Nothing is applied automatically, ever.

`/budget`, `/minyear`, `/threshold`, `/radius`, `/mileage`, `/make`, `/fuel`, and
`/transmission` all also work as a two-step prompt: send the command with no arguments (or
tap its button under `/settings`) and the bot asks for the value, then applies whatever you
type next — no need to remember the exact argument syntax. `/budget` and `/threshold` also
show quick-pick buttons (e.g. "😌 Loose / ⚖️ Balanced / 🔥 Strict") for the common cases, so
typing a number is optional. Invalid input (a typo, an out-of-range value) leaves the same
prompt armed rather than silently giving up — just send another try, or `/cancel` to back
out entirely. Any other unhandled error is caught by a bot-wide error handler that tells you
something went wrong instead of just going silent.

Every setting above takes effect from the *next* search onward — cron runs the shared
scrape/structure/analyze/baseline stages once per hour, then scores and notifies every
subscriber using whatever they currently have set (`radius_km` resolves as
explicit CLI flag → shared `/radius` value → hardcoded default; every personal filter
just reads straight from that subscriber's own settings, no CLI override involved).

Run the whole chain in one call (what cron actually invokes):

```bash
uv run becarscout run
```

Each of its 6 stages is independently try/excepted — if Facebook changes their markup
mid-run, already-processed backlog still reaches every subscriber instead of the whole
run being a no-op.

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
