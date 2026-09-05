# 🇧🇪 BE-CarScout — Belgian Used-Car Deal Finder

A personal pipeline that watches Facebook Marketplace for used cars in Belgium, understands each listing (not just its numbers, but what the seller *actually wrote*), scores it against the real market, and only pings you on Telegram when something is genuinely worth looking at. Over time it learns from your own 👍/👎 verdicts what "good" means to you specifically.

## Why this exists

Marketplace is full of noise: bad prices, rebuilt-title exports, cars "for parts" mislabeled as running, and vague descriptions that hide real mechanical issues. Scrolling manually doesn't scale, and a plain price/km filter is too dumb to catch the good stuff — you also can't just have an LLM "read and decide," because it hallucinates and doesn't have a consistent standard. The goal here is to split the work the way a smart human would: extract facts → understand the free text → score it against the market → only surface real opportunities.

## Scope (v1)

Belgium only, to start. Rather than build something generic for all of Europe, the point of v1 is to build a system that actually understands the Belgian used-car market — regional wording (NL/FR), local paperwork terms (*keuring / contrôle technique*, *carnet d'entretien*, CO₂ malus, BTW/margin vehicles), typical price bands per make/model/year in this market, and common phrasing patterns Belgian sellers use.

## Pipeline

```
Facebook Marketplace
        │
        ▼
   [1] Scraper           → raw listing (title, price, km, year, location, description, photos, url)
        │
        ▼
   [2] Structurer         → normalized fields (make, model, variant, fuel, transmission, options)
        │
        ▼
   [3] LLM Description Analyzer
        │   - parses free text (BE-NL / FR / mixed / abbreviations)
        │   - flags mechanical & administrative signals
        │   - outputs structured JSON, not prose
        ▼
   [4] Scoring Engine (rule-based, not LLM)
        │   - price vs. market baseline for that make/model/year/km
        │   - penalties/bonuses from step 3's flags
        │   - confidence score based on data completeness
        ▼
   [5] Decision Gate       → only listings above threshold continue
        │
        ▼
   [6] Telegram Bot        → sends opportunity card + 👍/👎 buttons
        │
        ▼
   [7] Feedback Store      → your verdicts saved and fed back into scoring weights
```

### 1. Scraper — ✅ Done (2026-09-04)
Pulls listings from Facebook Marketplace for configured search terms/locations in Belgium (Playwright-based; this part is largely a solved problem — several open-source scrapers already do this well). Outputs raw listing data on a schedule.

**Code:** `src/becarscout/scraper/` — `models.py` (the `RawListing` schema), `belgium.py` (hub cities/search config), `session.py` (login), `facebook_marketplace.py` (the scraper itself). CLI: `uv run becarscout login` / `uv run becarscout scrape`. Output: `data/raw/listings_<timestamp>.jsonl`.

**How it was built, in short:**
- **Login**: Facebook's login form is never automated (fastest way to get an account flagged). `becarscout login` launches a real, visible Chromium window backed by a persistent profile directory (`data/.session/profile/`) and just idles — a human logs in normally, cookies land on disk continuously, no explicit export step. Every later scrape reuses that same profile directory.
- **Coverage**: Facebook Marketplace search is radius-based from one point, not country-based, so Belgium is covered by fanning out over 5 hub cities (Brussels, Antwerp, Ghent, Liège, Charleroi) with overlapping radius and deduping by listing id (`belgium.py`).
- **The grid vs. the detail page**: Facebook's search-results cards turned out to expose only price + location, no title at all (confirmed by inspecting live rendered HTML, not assumed) — so the grid pass is used purely for link discovery. Every listing is then visited individually for the real title (the last `<h1>` on the page — the first is generic chrome like "Notifications"), price, location, description, and photos. This is slower (~76 listings ≈ 11 minutes) but far more reliable.
- **Description extraction**: raw Facebook HTML is packed with nav/sidebar/"today's picks" boilerplate. Rather than hand-picking brittle selectors for it, the detail page's full HTML is run through `trafilatura`, which strips the boilerplate on its own; a small anchor-based trim (cut between the "Seller's description" heading and the "Seller information" block) removes the last few Facebook-UI fragments trafilatura leaves behind.
- **Belgium ↔ elsewhere sanity check**: confirmed Facebook honors the location slug in the URL (not the account's real/IP-detected location) by pointing the same code at Rabat, Morocco and getting back genuine Moroccan listings in MAD.
- **Cars-only filtering**: Facebook's `vehicleType=car_truck` query param does **not** reliably exclude other vehicle types — boats and motorcycles leaked through in testing. Most listings also don't fill in structured fields (mileage/transmission/etc.) that could otherwise distinguish a car from a boat. Current filter is a blunt but auditable keyword check against the title (`_NON_CAR_TITLE_KEYWORDS` in `facebook_marketplace.py`) — generic terms (moto, bateau, remorque, camping-car, ...) plus a short list of unambiguous motorcycle/boat *brand* names added after two slipped through in testing (a "Jeanneau" boat, a "Kawasaki" motorcycle — "Suzuki"/"Honda" deliberately excluded from that brand list since they also make cars). Still not exhaustive — real classification belongs in stage 3 (LLM analyzer); this is just a coarse backstop.
- **Known limitations / things to revisit**: (1) brand-name non-car leakage, per above — still possible for less-common brands; (2) Facebook's markup uses hashed/auto-generated class names that change often — selectors lean on structural hooks (link hrefs, last-`<h1>`, span ordering) but will likely need upkeep; (3) detail-fetching is sequential, not parallelized; (4) no de-duplication yet against listings already scraped in a previous run — every run currently re-fetches everything from scratch.

**Salvaged for later (not yet wired in):** `src/becarscout/bot/` holds a working Telegram bot skeleton (conversation-based preference collection, SQLAlchemy models, Alembic migrations) recovered from an earlier prototype repo before it was discarded — real infrastructure for stage 6, but not touched yet. It won't run as-is: needs `uv add python-telegram-bot sqlalchemy alembic openai nest-asyncio` and a `.env` with `TELEGRAM_BOT_TOKEN` first.

### 2. Structurer — ✅ Done (2026-09-04)
Cleans and normalizes what can be extracted deterministically: price, mileage, year, make/model, fuel type, transmission, seller location, listing age, photo count.

**Code:** `src/becarscout/structurer/` — `models.py` (the `StructuredListing` schema), `makes.py` (curated Belgian-market make list), `parse.py` (standalone, individually-testable regex/keyword parsers — one function per field), `structurer.py` (combines them into `structure_listing`/`structure_listings`). CLI: `uv run becarscout structure [--input path]` (defaults to the most recent `data/raw/listings_*.jsonl`). Output: `data/structured/structured_<timestamp>.jsonl`.

**How it was built, in short:**
- **Price**: strip everything but digits from `price_text`; "Free" → `price_eur=0, is_free=True`.
- **Year**: regex for a standalone 4-digit token in the plausible car-year range, checked against title then description (avoids matching inside a longer number like a mileage figure).
- **Mileage**: regex anchored on the literal "km" token, both orders ("112000km" and "km 219000"), with a sanity bound (10–1,000,000) to reject false positives.
- **Fuel type / transmission**: keyword lists covering BE-NL/FR/EN terms (e.g. "essence"/"benzine"/"petrol", "automaat"/"automatique"/"dsg"), checked in a fixed priority order (electric → hybrid → lpg → diesel → petrol) so rarer/more-specific terms win over generic ones.
- **Make/model**: a curated flat list of ~50 makes realistic for the Belgian market (`makes.py`) — deliberately **not** the salvaged `bot/models/marksmakes.py` list, which is US-market-focused and missing Citroën/Renault/Peugeot/etc. Matched by literal word-boundary search in the title first, then description; `model_hint` is just the text following the make in the title, not a validated model name.
- **Location**: split `"City, REGION"` on the comma (REGION is VLG/WAL/BRU as Facebook shows it).
- **Listing age**: parses Facebook's own relative-time phrase ("Listed a week ago", "Just listed") into approximate days — this required a small stage-1 addition (`RawListing.listed_relative_text`, captured alongside price/location on the detail page) since the scraper wasn't retaining that text before.

**Validated against the real 76-listing Belgium dataset from stage 1** (`data/raw/listings_20260904T045935Z.jsonl`) — extraction rates: price 100%, location 100%, year 56%, make 36%, fuel type 32%, mileage 21%, transmission 18%. `listing_age_days` was 0% on that file since it predates the `listed_relative_text` capture; will populate on the next scrape.

**Known limitations:**
- Make/model detection only works when the make is spelled out somewhere in the text — "Golf 6" alone won't resolve to Volkswagen without a maintained model→make catalogue (out of scope for now; a plain regex pass shouldn't try to be a car database).
- No fuzzy matching — a seller typo like "Metcedes" won't match "Mercedes".
- All of the low-yield fields (mileage, fuel, transmission) are exactly the kind of information often buried in free-text descriptions in ways a regex can't reliably parse ("5 versnelling bak" implies manual but was never explicitly said) — that gap is what stage 3 (LLM analyzer) exists to close.

**Real bug found via live Telegram output, fixed 2026-09-05**: several listings showed implausible 300-500km mileages on cars from the 1980s-2000s. Root cause, found by pulling the raw stored text for the offending listings straight out of the DB: `_MILEAGE_BEFORE_RE`'s `[:\s]+` gap allowed matching *across newlines*, bridging Facebook's own "within a 64 km radius" UI boilerplate to an unrelated sidebar listing's price several lines below it (`"...64 km\n500 $US..."` → extracted as 500km). A third listing's regex correctly matched digits+"km" but grabbed the wrong occurrence — a mid-description repair anecdote ("après 300km la distribution a cassé") — instead of the actual odometer figure stated earlier as "209mille km" (French "thousand", which the old regex didn't parse as a number at all since "mille" isn't a digit). Fixed in `parse.py`: the gap in `_MILEAGE_BEFORE_RE` is now restricted to spaces/tabs only (no newlines), a new `_MILEAGE_MILLE_RE` handles "X mille km" phrasing and is tried first, and a new `sanity_check_mileage()` (wired into `structure_listing`) discards any extracted mileage under 1,000km on a car that isn't genuinely this model year and whose text doesn't say "0 km"/"neuf(ve)"/"nieuw(e)"/"new" — a safety net against the next mis-extraction of this shape, not just these three. Regression tests in `tests/structurer/test_parse.py` use the actual raw text from the three affected listings as fixtures.

### 3. LLM Description Analyzer — ✅ Done (2026-09-04)
This is the part a keyword filter can't do. The description is fed to an LLM with a strict extraction prompt so it returns **structured fields, not opinions** — for example:

| Raw phrase (BE listing) | Extracted signal |
|---|---|
| "motor lampje soms aan" | `warning_light: true, severity: unknown, needs_diagnostic: true` |
| "versnellingsbak heeft aandacht nodig" | `gearbox_issue: true, severity: likely_major` |
| "voor export" | `for_export: true` → usually correlates with hidden issues or non-BE paperwork |
| "nieuwe distributieriem" | `timing_belt_replaced: true` → positive signal, reduces near-term risk |
| "keuringsbewijs aanwezig" | `inspection_valid: true` |
| "carnet d'entretien complet" | `service_history: complete` |
| "geen BTW / marge voertuig" | `vat_scheme: margin` (affects resale/export math) |

The LLM's *only* job here is translation from unstructured text to structured facts, plus a rough severity/confidence estimate — it does **not** decide whether the car is a good deal. That judgment call is reserved for step 4, which is deterministic and auditable.

**Code:** `src/becarscout/analyzer/` — `models.py` (`DescriptionSignalFields`/`DescriptionSignals`), `schema.py` (the hand-written strict JSON schema + system prompt), `analyzer.py` (`analyze_description`/`analyze_descriptions`). CLI: `uv run becarscout analyze [--input path] [--model name] [--delay seconds] [--limit N]` (defaults to the most recent `data/structured/structured_*.jsonl`). Output: `data/analyzed/analyzed_<timestamp>.jsonl`, joinable back to structured listings by `listing_id`.

**How it was built, in short:**
- **Provider**: Mistral (user's explicit choice over Claude/GPT), via the official `mistralai` Python SDK. Note for future sessions: the installed SDK version (2.9.4+) has no top-level package re-export — the correct import is `from mistralai.client import Mistral`, not `from mistralai import Mistral` as older docs/training data would suggest. Verified this against the actual installed package structure rather than assuming, since the API surface has grown substantially (agents, conversations, connectors, realtime, etc.) since general knowledge of this SDK was last current.
- **Model gating discovered live**: `mistral-small-latest` returned a hard 429 on every attempt (including after 90s backoffs) on this account, while `ministral-8b-latest` worked immediately — looks like a paid-tier gate rather than per-request throttling. Default model is `ministral-8b-latest`, overridable via `MISTRAL_MODEL` env var or `--model` if the account is upgraded later.
- **Structured output**: uses `response_format={"type": "json_schema", "json_schema": {..., "strict": True}}` with a hand-written schema dict in `schema.py` (kept in sync with `models.py` manually) rather than `DescriptionSignalFields.model_json_schema()`, because Mistral's strict mode requires every property listed in `required` (nullable ones included) and `additionalProperties: false` at every level — Pydantic's default schema output isn't shaped that way out of the box.
- **Resilience**: retries only on an actual 429 (checked via `SDKError.raw_response.status_code`, not string-matching), with backoff; any other failure (bad JSON, schema mismatch, auth) fails fast and falls back to an all-defaults `DescriptionSignals(extraction_failed=True, confidence="low")` rather than crashing the whole batch. A 1-second delay between sequential calls is the default (`--delay`) to stay clear of per-minute limits — batches run unattended, so correctness matters more than throughput.
- **Validated against 5 real listings** from the stage 1/2 test data — extraction quality was genuinely good: correctly recognized that a parts listing's "gearbox already removed" isn't a `gearbox_issue` (per an explicit instruction in the prompt to prevent that specific false positive), pulled `gearbox_issue_severity: likely_major` out of French free text ("Problème de boîte de vitesses"), and cross-checked fuel/transmission/mileage against stage 2's regex results. One observed miss: a misspelled "essance" (should be "essence"/petrol) wasn't picked up as `fuel_type` — LLM extraction isn't immune to unusual typos either.

### 4. Scoring Engine — ✅ Done (2026-09-04)
A rules-based model — not an LLM — combines:
- **Price gap**: listed price vs. expected price for that make/model/year/mileage (regression or lookup table built from historical BE listings)
- **Condition adjustments**: bonuses/penalties from the structured flags in step 3 (e.g. confirmed timing belt replacement = +, unresolved warning light = −, "for export" = − unless the user is specifically looking to export)
- **Data confidence**: listings with vague/missing info are down-weighted rather than excluded, since a great deal with a bad description is still worth a look at a lower confidence tier
- **Personal weights**: adjustable per-signal weights that get tuned from your feedback (step 7)

Output: a single opportunity score (and the reasoning trail behind it), not a black-box "yes/no."

**Code:** `src/becarscout/pricing/` (the price-baseline half) — `models.py` (`Comp`/`PriceBaseline`), `comps_2dehands.py` (the comps scraper + disk cache), `baseline.py` (year/mileage-cascaded median). `src/becarscout/scoring/` (the scoring half) — `scoring.py` (`score_listing`, all point-weight constants live at the top of this file), `pipeline.py` (joins structured + signals + baseline per listing), `models.py` (`ScoredListing`). CLI: `uv run becarscout score [--structured path] [--signals path] [--threshold N]`. Output: `data/scored/scored_<timestamp>.jsonl`, every listing included with an `above_threshold` flag (not just the ones that passed).

**How it was built, in short:**
- **Where the "expected price" comes from** — this was the open question going in. Considered three options: (a) accumulate our own historical Marketplace scrapes over time and bucket by make/model/year, (b) scrape a second site with structured, filterable listings for live comps, (c) live web search per listing. Went with (b): **AutoScout24.be and Gocar.be both returned a hard 403** (real bot-detection, not worth fighting), but **2dehands.be** (Belgium's major classifieds site) returned clean 200s — and its search results already carry price/year/mileage/fuel/transmission directly in the grid, no detail-page visit needed per comp, unlike the Facebook scraper. (a) is still worth doing later as a free-over-time supplement per the original v4 roadmap item below; (c) was set aside — turning search snippets into a number needs either another LLM call (reintroducing judgment into a step meant to stay deterministic) or fragile parsing, for a noisier result than (b).
- **Baseline matching**: comps are filtered by year (±2, widened to ±4 if that's under 2 matches) *and* mileage (±40%, widened to ±70%) with graceful fallback to the unfiltered pool at each step — added after the first version (year-only) pulled in a 2021 Golf 8 alongside a 219,000km 2013 Golf 6 and skewed the median up. Median is computed with light outlier trimming (drop the single highest/lowest) once there are ≥5 comps.
- **Two real data-quality bugs found by actually running it on the 76 real listings**, not by inspection: (1) a network timeout fetching comps for one listing crashed the *entire batch* — fixed with per-listing try/except that falls back to "no baseline" and logs, matching the resilience pattern already used in the scraper and analyzer; (2) several listings priced at literal €0/€1 (Marketplace's "make an offer" placeholder pricing) were scoring as extreme "steals" (+100, +118) since the price-gap formula read that as "100% below market" — fixed with a €300 floor below which price-gap is skipped entirely (parts listings and placeholder prices aren't comparable to whole-car comps). A few genuine seller data-entry errors (a 2006 Peugeot 407 listed at €31,000) also produced enormous scores (-1165); these are directionally correct ("steer away") but were clamped to ±150 so one typo doesn't dominate the output.
- **Performance**: originally launched a fresh Chromium instance per comps fetch; refactored to share one browser across an entire scoring run once the crash above surfaced how many fetches actually happen per run.
- **Result on the real dataset**: of 76 listings, 5 scored above the default threshold of 20 after all fixes — each with a full reasoning trail (e.g. `"Priced €850 vs. €4,970 median (83% below market, n=8 comps, high confidence) -> +82.9"` followed by any condition adjustments). Confirms the end-to-end pipeline (scrape → structure → analyze → score) produces genuinely differentiated, explainable output on real data, not just on synthetic test cases.

**Known limitations:**
- Comps search only tries the first word of the structurer's `model_hint` as the model name (e.g. "Mondeo" out of "Mondeo 2.0 diesel 2013 automatic") — works for most European car names but not perfectly.
- No baseline at all for the ~64% of listings where stage 2 couldn't detect a make (see stage 2's limitations) — these score on condition signals alone.
- 2dehands.be comps are cached for 7 days per make/model but otherwise this is a live scrape on every `becarscout score` run for new make/models — same "Facebook could change their markup" fragility as stage 1, just against a second site now.
- Point weights in `scoring.py` (e.g. gearbox issue = -40, timing belt replaced = +12) are reasoned first-pass numbers, not tuned against real outcomes — that's explicitly what stage 7's feedback loop exists to eventually correct.
- A `likely_major` condition signal (medium+ confidence) is still fully offsettable by a large enough price gap — e.g. a car with a seller-stated "needs a clutch module" can still clear the gate on price alone. Flagged, not fixed: whether a serious mechanical issue should *cap* the achievable score outright (rather than just being one more weighted input) is a product decision about how risk-averse this system should be, not a pure bug — needs a decision on what the cap should be and whether it applies pipeline-wide or only above some severity/confidence combination before it's worth implementing.

**Real bug found via live Telegram output, fixed 2026-09-05**: a 1986 Ford F-150 (base trim, €8,000 asking) scored +72 as "72% below market" against a median of €29,000 from 19 comps — but the Belgian/US F-150 market for that model name today is dominated by modern trucks (mostly Raptor trim, €30k-€100k+), so the comp pool was comparing a 40-year-old classic against an entirely different vehicle generation. Root cause: `compute_baseline`'s year cascade (`baseline.py`) already filtered by year at ±2 and ±4, but when *neither* found 2+ matches it fell back to the **entire unfiltered comp pool** — silently reintroducing every era-mismatched comp it had just tried to filter out. Fixed by adding a third, final ±8-year tier and making it the floor: if even that finds nothing, the baseline is now reported as "no data" (`median_price_eur=None`, `confidence="none"`) rather than fabricated from a different vehicle generation. Also added a `_MAX_PRICE_RATIO` (5x) sanity guard — if the matched comp pool's highest price is still more than 5x its lowest (a handful of comps that survived year filtering but span incompatible trims), extreme values are trimmed toward that ratio before computing the median, and confidence is forced to "low" if trimming still can't bring it under the threshold, regardless of sample size. Regression tests in `tests/pricing/test_baseline.py` cover the mixed-era case, a tight/consistent pool (unaffected), and the variance guard.

### 5. Decision Gate — ✅ Done (2026-09-04)
Only listings above a configurable score threshold get pushed further — this is what keeps Telegram from turning into spam.

**Code:** `src/becarscout/scoring/gate.py` — deliberately trivial, exactly per the design principle above: `apply_decision_gate(scored, threshold)`. Default threshold is 20, overridable via `--threshold`.

**Next step:** stage 6, the Telegram Bot — wire the salvaged bot skeleton (`src/becarscout/bot/`) to send a card per `ScoredListing` where `above_threshold` is true, using the `reasoning` list as the "why this scored well" text. Needs `uv add python-telegram-bot sqlalchemy alembic openai nest-asyncio` (the bot skeleton's dependencies were never added, since it wasn't wired in until now) and a `.env` `TELEGRAM_BOT_TOKEN`.

### 6. Telegram Bot — ✅ Done (2026-09-04)
Sends a compact card per opportunity: photo, price, key stats, the 2–3 sentence "why this scored well/what to watch out for," and a link — with inline 👍/👎 buttons.

**Code:** `src/becarscout/notifier/` — `formatting.py` (renders a `ScoredListing`'s existing reasoning trail into the card, no new interpretation), `bot.py` (`send_new_opportunities`, `run_feedback_listener`, `find_chat_ids`), `feedback.py` (verdict capture). CLI: `uv run becarscout notify` (send) and `uv run becarscout listen` (a separate, standing process that catches button presses) and `uv run becarscout whoami` (finds your `TELEGRAM_CHAT_ID` after you message the bot once).

**How it was built, in short:** deliberately *not* built on the salvaged `src/becarscout/bot/` skeleton — that was a conversational preference-picker UI (brand/model selection via chat menus, seller negotiation, KBB pricing) for an entirely different, unrelated product idea, and retrofitting it would mean fighting its structure for no benefit. This is a small, purpose-built module instead, matching exactly what this stage actually needs. `.env` needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

**Known limitation:** no photo in the card yet (Project.md's spec mentions one) — text-only for now; `listing.photo_urls[0]` is available whenever this gets added.

### 7. Feedback Loop — ✅ Done (2026-09-05), advisory-only by design
Your 👍/👎 on each card is remembered, and a small LangGraph agent periodically turns accumulated feedback into a plain-English report of patterns plus concrete *suggested* `scoring.py` weight changes. It never edits `scoring.py` or Telegram's output itself — a deliberate product decision (see below), not a technical limitation.

**Code:** `src/becarscout/feedback_agent/` — `memory.py` (mem0-backed storage/retrieval of feedback, fully local), `graph.py` (the LangGraph review agent). CLI: `uv run becarscout feedback-review`.

**How it was built, in short:**
- **The raw log stays**: `notifier/feedback.py`'s flat `data/feedback/feedback.jsonl` (listing_id, verdict, timestamp) is unchanged — still the source-of-truth audit trail. mem0 sits alongside it, turning the same verdicts into something *query-able by similarity* rather than just an append-only file.
- **mem0, fully local**: a Chroma vector store on disk (`data/mem0/`) plus FastEmbed (a small ONNX embedding model, CPU-only, downloaded once — baked into the Docker image at build time, same reasoning as installing Chromium ahead of time rather than on first use) for embeddings. No OpenAI key, no data leaving the machine for storage/retrieval. The one network call is to Mistral (via `litellm`, which mem0 uses as a generic LLM backend) for mem0's own internal fact-extraction step when a memory is added — reuses `MISTRAL_API_KEY`, no second LLM provider.
- **Two integration points, both additive**:
  1. **At notify time** (`notifier/bot.py`): before a new opportunity is sent, `find_similar_feedback` checks mem0 for similar-sounding cars you've rated before, and the card gets one extra line ("2 similar cars you liked before") if there's a match. This never changes the score — see `formatting.py`'s `_similar_feedback_note`.
  2. **On every 👍/👎** (`notifier/bot.py`'s callback handler): the verdict is looked up against its full DB row (`db/repository.get_scored_listing`) and stored as a memory describing that car (make/model/year/price/mileage/market median/score **and its condition-signal reasoning trail** — without the reasoning text, the review agent could never notice a pattern like "you keep disliking cars with unresolved warning lights").
- **The review agent itself** (`graph.py`) is a 4-node LangGraph graph: `load_memories` → (conditional: enough volume?) → `synthesize` → `write_report`, or straight to `not_enough_data` if fewer than `_MIN_FEEDBACK_FOR_REVIEW` (5) verdicts exist yet — below that, any "pattern" is noise dressed up as insight. `synthesize` calls Mistral with the actual current `scoring.py` constant values injected into the prompt (found this the hard way in testing: without them, the model guessed plausible-looking but wrong "current" values, e.g. reporting `_WARNING_LIGHT_NEEDS_DIAGNOSTIC` as "-10" when it's actually -20) and a description of what each tunable constant actually controls, so it can't repurpose one for something it doesn't mean (e.g. it initially tried to use `_MIN_PLAUSIBLE_CAR_PRICE_EUR`, a placeholder-pricing sanity floor, to express "the user prefers pricier cars" — fixed by spelling out each constant's real meaning in the prompt).
- **Product decision, not a bug**: whether the agent should be allowed to *auto-apply* its suggestions was considered explicitly and rejected for now — a human stays in the loop reading `data/feedback/review_<timestamp>.md` and editing `scoring.py` by hand. This keeps the same "LLM interprets, doesn't judge" principle stages 3-5 already follow, just applied one level up (patterns across verdicts, not facts within one description).
- **A real Windows bug found while testing this**: printing the review report (LLM-generated markdown, not fully within our control character-wise) via a plain `print()` crashed with `UnicodeEncodeError` on a `→` character the model used — the same class of bug as the emoji-in-argparse-help crash from stage 1's CLI work. Fixed with a `_print_safe` helper in `cli.py` that falls back to character replacement instead of raising.

**Real bug found 2026-09-05, fixed**: `becarscout listen` (the standing process that captures Telegram thumbs up/down presses) was never actually started inside the Docker container — `docker/entrypoint.sh` only ran the hourly `becarscout run` pipeline. This meant every 👍/👎 pressed on a live opportunity card went nowhere: nothing was polling Telegram for the button press, so it wasn't recorded to `feedback.jsonl`, DB, or mem0, even though all of that code worked correctly when run manually. Fixed by starting `becarscout listen` in the background from `entrypoint.sh` alongside cron (its own log, `data/feedback_listener.log`, also tailed into `docker compose logs`). This also means the container now genuinely runs two long-lived processes concurrently for the first time (the hourly pipeline and the standing listener) — not multi-threaded within either one (see below), but real concurrent access to the same SQLite DB from two OS processes, which prompted also switching SQLite to WAL journal mode + a 5s busy-timeout in `db/engine.py` (readers and a writer no longer block each other; a genuine write/write collision retries briefly instead of raising "database is locked" immediately).

**Is any of this actually multi-threaded/concurrent? Answered honestly:**
- **Within one pipeline run**: no. Scraping visits listing detail pages sequentially (see stage 1's known limitations), stage 3's Mistral calls are sequential with a deliberate delay, and `score_listings` resolves each listing's price baseline one at a time (`scoring/pipeline.py`) — correctness and staying clear of rate limits mattered more than throughput for an hourly personal job, so none of this was parallelized.
- **Across processes**: yes, for real, as of this fix — the hourly `run` and the standing `listen` process both run continuously inside the same container and can touch the DB/mem0 at the same moment. The DB is now WAL-mode to handle that. **mem0's local Chroma store is not given the same treatment** — chromadb's persistent (file-based) client is designed around single-process use, and `listen` (writing a feedback memory) and `run`'s `notify` step (reading via `find_similar_feedback`) are now two separate processes pointed at the same on-disk `data/mem0/chroma/`. At this project's actual volume (one button press at a time, at most a few notify-time lookups per hour) a genuine collision is unlikely, but it's a real, undocumented-until-now risk rather than something proven safe — worth watching `data/feedback_listener.log` for Chroma errors, and revisiting (e.g. running Chroma as a small local server instead of embedded-file mode) if one ever shows up.

**Known limitations / how to enhance further:**
- `_MIN_FEEDBACK_FOR_REVIEW = 5` is an arbitrary floor, not a statistically-derived one — with real usage volume, this is worth revisiting (a higher floor for a more reliable trend, or a lower one just to see the report take shape sooner).
- The review agent looks at *all* accumulated feedback every time, with no decay — a preference from months ago carries the same weight as one from yesterday. A real enhancement here would timestamp-weight or window the query (e.g. "feedback from the last 90 days") once there's enough history for that to matter.
- Pattern detection is single-pass (`get_all_feedback_memories` + one Mistral call) rather than iterative — the agent doesn't re-query mem0 mid-analysis to dig into a pattern it half-notices (e.g. "let me check if high-mileage diesels specifically are disliked more than high-mileage petrols"). A genuinely iterative LangGraph agent (a loop node that decides whether it has enough evidence yet, re-searching mem0 with a more specific query if not) would produce sharper, better-evidenced suggestions — reasonable v2 scope, held back for now since the volume of real feedback doesn't yet justify the extra complexity.
- No feedback on *rejected* listings (below the decision-gate threshold, never sent to Telegram) — the agent only ever sees what you explicitly rated, not silent near-misses. Widening this would mean sending borderline listings anyway with a lower-confidence label, which is a real product trade-off against "low noise over completeness" (Project.md's own design principle), not just an engineering task.
- Suggestions are prose in a markdown file, not a structured diff — applying one still means manually finding and editing the right line in `scoring.py`. A future version could emit a structured `{constant: new_value}` mapping and a small script to apply it (still requiring manual confirmation, keeping the human-in-the-loop decision above), removing the copy-paste step without removing the review step.
- `mem0`'s own optional NLP extras (spaCy-based entity/lemma matching) aren't installed (`pip install mem0ai[nlp]` — logs a harmless warning on every call) — fine at this data volume; worth reconsidering only if search quality becomes a problem with hundreds+ of stored memories.

## Design principles
- **LLM does interpretation, not judgment.** Free text → structured facts is the one thing worth using an LLM for; the actual buy/no-buy scoring stays deterministic and inspectable.
- **Everything the system flags should be traceable** back to a specific phrase or field — no unexplained scores.
- **Belgium-first.** Get the market model, language handling, and paperwork terms right for one country before generalizing.
- **Low noise over completeness.** Better to miss a borderline listing than to erode trust in the Telegram alerts.

## Similar / prior art
A quick survey before building this — nothing found combines all four pieces (Belgian focus, structured problem-extraction from free text, separate deterministic scoring, and a learned feedback loop), but these overlap partially and are worth a look for scraping code and inspiration:

- **[passivebot/facebook-marketplace-scraper](https://github.com/passivebot/facebook-marketplace-scraper)** — the most mature open-source FB Marketplace scraper (Playwright + BeautifulSoup + Streamlit). Good base for step 1.
- **[kevmaindev/Facebook-Marketplace_Scraper](https://github.com/kevmaindev/Facebook-Marketplace_Scraper)** — similar scraping approach with configurable search criteria.
- **[martin3252/Facebook_Marketplace_Scraper](https://github.com/martin3252/Facebook_Marketplace_Scraper)** — adds a GPT step that shortlists the top 3 cars from scraped results; closest in spirit but no structured extraction or scoring separation.
- **[etonealbert/AI-faceboo-marketplace-deal-finder-bot](https://github.com/etonealbert/AI-faceboo-marketplace-deal-finder-bot)** — uses KBB data for profitability scoring and GPT for seller negotiation; no Telegram delivery or learned personalization.
- **Apify "Israel Used-Car Deal Finder"** — scores Yad2 listings against an official market-value table (Levi Yitzhak index); good precedent for the price-vs-market baseline idea, but no free-text understanding.
- **"How to find a car under market value" (Medium, 2020)** — a German used-car-portal scraper piped into a Telegram bot; closest precedent for the delivery mechanism, but pure price/km filtering with no LLM or learning component.

## Roadmap
- [x] v1: Scraper + structurer for Belgium, manual score-threshold tuning, Telegram delivery
  - [x] Scraper (stage 1) — see notes under "1. Scraper" above
  - [x] Structurer (stage 2) — see notes under "2. Structurer" above
  - [x] Scoring Engine + Decision Gate (stages 4–5) — see notes above; threshold is manually set via `--threshold`, not yet tuned from real feedback
  - [x] Telegram delivery (stage 6) — see notes under "6. Telegram Bot" above; the salvaged `src/becarscout/bot/` skeleton was *not* used, built fresh instead
- [x] v2: LLM description analyzer with a fixed extraction schema — see notes under "3. LLM Description Analyzer" above (no formal eval set of real BE listings yet, just spot-checked against real scraped listings)
- [x] v3: Feedback loop wired to Telegram button callbacks — see "7. Feedback Loop" above. Advisory-only by design: a LangGraph + mem0 agent reports patterns and suggests `scoring.py` weight changes for manual review; it doesn't auto-retrain
- [ ] v4: Expand price-baseline model — currently live 2dehands.be comps per scoring run (see stage 4 notes); still worth building a regression/lookup table from our *own* accumulated Marketplace scrapes over time as a free-over-time supplement or replacement
- [x] Infra: SQLite persistence + Docker + hourly cron — see "Infrastructure" section below
- [ ] Later: consider other countries once the BE model is solid

## Infrastructure — ✅ Done (2026-09-04)
The pipeline runs unattended, hourly, in a Docker container.

**Database.** Replaced the per-stage JSONL files with SQLite (`src/becarscout/db/`) — one denormalized `listings` table, one row per listing, gaining columns as it moves through stages 1→6. This is what makes hourly cron cheap: `get_listings_needing_structuring`/`_analysis`/`_scoring` each query for rows that haven't reached that stage yet (a plain `WHERE ... IS NULL`), so a listing scraped and processed last hour is never re-sent to Mistral, re-scraped for comps, or re-scored on the next run — only genuinely new listings do any work. `notified_at` on the same table replaced the separate `sent_ids.json` file the notifier used to track. The pydantic models from stages 1–6 didn't change at all; the DB is purely a persistence detail behind `db/repository.py`, converting to/from those same models.

**`becarscout run`** (`cli.py`) chains scrape → structure → analyze → score → notify in one call, with each stage independently wrapped in try/except — if Facebook's markup breaks the scraper mid-run, already-processed backlog still moves through structure/analyze/score/notify instead of the whole hourly run being a no-op.

**Docker.** `Dockerfile` bases on plain `python:3.12-slim` + `uv run --no-project playwright install --with-deps chromium` (resolves the right OS packages for whatever Chromium version the pinned `playwright` package needs, rather than betting on a matching pre-built `mcr.microsoft.com/playwright/python` tag existing). Cron (the real `cron` package, not a Python sleep loop — installed in the image) fires `becarscout run` hourly via `docker/crontab`; `docker/entrypoint.sh` also runs it once immediately at container startup, then hands off to `cron -f` as PID 1 so `docker stop`/logs behave correctly. One non-obvious fix along the way: cron doesn't inherit the container's environment, so `entrypoint.sh` snapshots `printenv` to a file that `docker/run_pipeline.sh` sources on every tick — otherwise `MISTRAL_API_KEY`/`TELEGRAM_BOT_TOKEN` would be invisible to the hourly job despite being set on the container.

**What can't run in Docker: `becarscout login`.** It opens a real, visible browser window for a human to log into Facebook — that's structurally impossible in a headless container. Login must happen once on a machine with a display (the host), which creates `data/.session/profile/`; `docker-compose.yml` mounts the whole `data/` directory as a volume, so the container picks up that already-authenticated session (plus the SQLite DB, the pricing cache, and pipeline logs) without ever needing a display itself.

**Real bugs found by actually running the container against live data** (not by inspection — every one of these only showed up once the whole thing ran for real end-to-end):
- `python-telegram-bot`'s HTTP client logs full request URLs at INFO level, which for Telegram means the bot token appears in plaintext — including what would have been the persisted `data/pipeline.log`. Fixed by dropping `httpx`'s logger to WARNING in `cli.py`'s `main()`.
- **The scraper had no idea what the DB already knew about.** The first live container run was about to spend ~10 minutes re-visiting the detail page of every listing found in that hour's grid search — including ones already fully processed in a previous run — before the DB-level dedup in `upsert_raw_listings` would finally discard them as duplicates. That defeated a good chunk of the whole point of moving to a database. Fixed by having `do_scrape` fetch known listing_ids from the DB first and passing them to `scrape_belgium_cars(known_ids=...)`, which now skips the detail-page visit entirely for anything already known.
- **Two Docker build-layer bugs**, both from the deps-before-source layering meant to keep the slow Chromium install cached separately from code changes: `uv run playwright install` failed because a bare `uv run` tries to sync/build the local project too, and the project's source wasn't copied in yet at that layer (fixed with `uv run --no-project`); `uv sync --frozen` after copying source failed because `pyproject.toml` references `README.md` as the package readme, which had been excluded via `.dockerignore` for no real reason (fixed by not excluding it).
- **A live Telegram `TimedOut`** on the very first real notify call, even though the same run's Mistral and 2dehands.be calls succeeded fine. Diagnosed layer-by-layer from inside the running container (DNS resolved fine, raw TCP connected in 80ms, a plain `httpx` GET succeeded) before finding it was specific to `python-telegram-bot`'s default timeouts (5s connect/read, a notably tight 1s pool timeout) — a manual retry of the exact failing call succeeded instantly. Fixed with longer timeouts (20s) and a 3-attempt retry with backoff around the connection itself, tracking already-sent listing_ids across attempts so a retry never double-sends. Even so: the real safety net is architectural, not the retry — since delivery status lives in the DB (`notified_at`), a `notify` failure just means those opportunities get picked up and sent on the *next* hourly run instead of being lost, which is exactly what was observed happening correctly during validation.
- **Not a code bug, but a real incident**: `docker compose config` was run once to sanity-check the compose file, and it echoes fully-resolved environment values — printing `MISTRAL_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` in plaintext into this session's own output. Combined with the earlier httpx logging leak (caught and fixed, but only after it had already printed the token once), that's two real secrets that ended up in this conversation's transcript. Both were rotated afterward. Lesson for future sessions: never run `docker compose config` (or anything else that echoes resolved env values) once real secrets are in `.env`.

**Status as of 2026-09-05: the container is live** (`docker compose up -d`, `restart: unless-stopped`), running the full pipeline hourly. First real run surfaced 8 genuine opportunities, delivered to Telegram successfully.

## Stack
- **Scraping**: Playwright (Python)
- **Storage**: SQLite (`data/becarscout.db`, via SQLAlchemy) for listings through every stage; a small JSON comps cache (`data/pricing_cache/`) and a feedback JSONL (`data/feedback/feedback.jsonl`) alongside it. Postgres was the original fallback plan but SQLite is genuinely sufficient at personal-project scale and keeps the Docker setup to one container.
- **LLM**: Mistral API (`ministral-8b-latest`, via the `mistralai` SDK) for the extraction step only, strict JSON schema output
- **Scoring**: plain Python, no ML — see stage 4 notes
- **Delivery**: python-telegram-bot
- **Scheduling**: real cron inside the Docker container, hourly
- **Feedback agent (stage 7)**: LangGraph (small graph orchestration) + mem0 (local, Chroma + FastEmbed) for feedback memory, `litellm` as the glue letting mem0 use Mistral instead of OpenAI internally — see stage 7 notes above.
- **Tests**: pytest (`uv run pytest`), added 2026-09-05 alongside the first regression tests (`tests/structurer/`, `tests/pricing/`) — no test setup existed before those two bugs, so there's not yet coverage for every stage, just the ones that have had a real, reported bug (later extended to `tests/notifier/`, `tests/feedback_agent/` alongside stage 7's build).

---
*This README describes the intended architecture and design decisions before implementation. Note: scraping Facebook Marketplace may be subject to Facebook's Terms of Service — worth reviewing before running this against your own account.*