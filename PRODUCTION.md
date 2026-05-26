# Production Architecture — Status & Roadmap

This document tracks what's production-ready in the terminal. **Last updated 2026-05-26** — bonds/yields coverage, breaking-news event bus, data-quality hardening (section below). Step 8 baseline: 2026-05-12.

---

## 🛠 Pending commit 2026-05-26 — bonds, breaking-news bus, data-truth (in `market-terminal` container, not yet pushed)

A live incident exposed three classes of bug at once: (1) the BOND
YIELDS column showed only US sovereigns, so a Japan-10Y-rising-overnight
signal was structurally invisible; (2) the Morning Note kept serving a
05:00 IST brief that missed a 06:15 IST US-attacks-Iran headline because
the TTL was 90–150 min; (3) the Explainer wrote "crude fell 3.1%, risk-on"
based on yfinance's broken futures-rollover `previous_close` (it returned
$96.60 vs Stooq's actual $91.02), then `bias_consensus_engine` cascaded
that into systematic SELL across all 8 markets in the pre-market report.

Six waves, all running on the bind-mounted container — each restart was a
single ~5s blip. **No commits yet — review the diff before pushing.**

### 1. Non-US 10Y yields live in BOND YIELDS  (`live_prices.py`, `Dockerfile`)

The BOND YIELDS column only had `US_3M/2Y/5Y/10Y/30Y`. JGB, Bund, Gilt,
India 10Y were absent — and `tvdata.py` (the TradingView-fed module
already in the repo) was silently returning `None` because the
`tvdatafeed` PyPI package had been withdrawn and the Dockerfile's
optional `pip install tvdatafeed || true` swallowed the failure. NSE
indices had been falling through to yfinance's 15-min-delayed quotes
the whole time, undetected.

- `Dockerfile`: install `git` in apt-get + `pip install
  'tvdatafeed @ git+https://github.com/rongardF/tvdatafeed.git'` so the
  maintained fork lands at build time.
- `live_prices.py`: added `TV_BOND_MAP = {JP_10Y, DE_10Y, UK_10Y, IN_10Y → TVC}`,
  `_TV_BOND_KEYS`, `_tv_bond_one` (with 3× retry/0.4s backoff because
  tvdata cold-starts SSL-timeout on first call), `_tv_bond_batch`, and
  `_VALID` ranges. Wired into the existing `_fetch_all` parallel
  executor.
- Frontend auto-renders the new keys via the existing
  `_renderDbr('db-yld', lp.bonds||{}, 'BONDS')` — no HTML edit needed.

Side benefit: NSE indices now use live TradingView instead of the
15-min-delayed yfinance fallback they'd been on for months.

### 2. Morning Note: global shocks attach to every market  (`morning_report.py`)

`_overnight_catalysts` filtered news clusters by per-market keyword sets
(USA: `"us ", "fed", "fomc", ...`). "Iran", "Israel", "missile",
"oil shock" appeared in *no* market's list — so a "US strikes Iran"
headline sat in the GEOPOLITICS bucket attached to *no* market brief
even though every oil-importer index reprices on it instantly.

- Added `GLOBAL_SHOCK_KEYWORDS` (Iran, Israel, Russia, Ukraine, Taiwan,
  missile, strike, oil shock, sanction, bond rout, BOJ intervention, ...).
- `_overnight_catalysts` now returns up to 2 local-keyword hits + 2 global-shock
  hits per brief; global hits carry `scope: "GLOBAL"` so the UI can mark them.
- Global shocks only count when severity ≥ 5 so routine flashpoint-country
  mentions don't dilute the signal.

### 3. Adaptive TTL on breaking news  (`morning_report.py`)

`_GLOBAL_TTL = 90 min` and `_BASE_TTL = 150 min` per market meant a brief
generated at 05:00 IST stayed cached through the entire 05:00–07:30
window even after a major catalyst. New helper `_breaking_news_active(snap)`
scans `snap.news.clusters` for any cluster with severity ≥ 7
(`event_classifier` scores geopolitical military events at 7–9). When
True, `_BREAKING_TTL = 5 min` overrides both `_GLOBAL_TTL` and
`_market_ttl` at cache-write time. Default behaviour unchanged when no
HIGH-severity event is in-snapshot.

### 4. AI yield narrative + new endpoint  (`yield_watch.py` (new), `dashboard_api.py`, `templates/dashboard.html`)

New module reads US/JP/DE/UK/IN 10Y from `live_prices`, computes
basis-point deltas (1d), and calls `ai_router.chat(task="fast_summary")`
for a one-paragraph cross-asset read whenever any |Δ| ≥ 5 bp. Cached 5
min normally, 1 min when any |Δ| ≥ 10 bp.

- New endpoint `/api/yield-watch` (`dashboard_api.py:1977-1991`).
- New UI element `#db-yld-insight` under the BOND YIELDS column; the
  `loadLivePrices()` call now also calls `loadYieldInsight()`.
- `morning_report._macro_drivers` includes `yield_watch.{yields,
  narrative, big_movers, any_breaking}` in every brief.
- Gated by `ENABLE_YIELD_NARRATION` env (default on); narrative stays
  hidden until a yield actually moves ≥ 5 bp so the UI doesn't show an
  empty paragraph.

### 5. Redis pub/sub event bus  (`event_bus.py` (new), `news_deduper.py`, subscribers)

Until this landed, every cache decided freshness on its own TTL clock.
A 25-minute morning brief stayed warm through the first 25 minutes of a
geopolitical shock while competing free tools (ChatGPT-with-web-search)
were already naming the new driver.

- New channel `events:breaking`. Payload is JSON with `topic`, `severity`,
  `ts`, optional `category`, `direction`, `sources`.
- `event_bus.publish_breaking()`: dedups per-topic within a 90s window so
  a sticky cluster doesn't fire every poll.
- `event_bus.start_listener()` (idempotent): spawns a daemon thread that
  pumps the pub/sub and reconnects on Redis blips.
- `news_deduper.dedupe_news()` publishes every cluster with severity ≥ 7
  to the channel.
- `morning_report.py` and `yield_watch.py` subscribe at module import:
  on event they drop `morning:global_signals`, every `morning:brief:*`
  (via Redis SCAN), and `yield_watch:v1`. Verified: smoke test publishes
  a fake severity-8 event → all three caches gone within 2s.
- Falls back silently when Redis is unavailable — caches just expire by
  TTL as before.

### 6. BONDS/RATES tab fills with the actual move drivers  (`news.py`, `dashboard_api.py`, `templates/dashboard.html`)

Source-based `SOURCE_CATEGORY` only tagged 3 sources as BONDS (FT
Markets, WSJ Mkt, BondBuyer). A Reuters story about "Japan 10Y rises,
Nikkei falls" came from a `MARKETS`-tagged source and never landed in
the BONDS/RATES tab.

- `news.py`: added `_BOND_NEWS_KEYWORDS` (~30 terms: yield curve,
  10-year, treasury, JGB, Bund, Gilt, rate hike, dot plot, FOMC, BOJ,
  basis points, ...) and `_tag_content_categories()` — runs after dedup
  and appends `BONDS` to each item's `tags` list when content matches.
  Primary `category` stays as source-derived.
- `dashboard_api.api_news` now serialises the `tags` field in the JSON
  response (the bug that hid the fix in v1).
- Frontend `renderFeed` + `updateCounts` filter on
  `n.category === activeCat || (Array.isArray(n.tags) && n.tags.includes(activeCat))`.

Verified live today: 14 items in BONDS tab vs the previous 4 (e.g.
"UK gilt yields retreat from multi-decade highs", "Treasury yields
slide as traders weigh Iran peace prospects", "Bessent's Options Seen
Limited to Halt Climb in Treasury Yields").

### 7. Data-truth hardening: prev_close cache  (`prev_close_cache.py` (new), `live_prices.py`)

`yfinance.Ticker(sym).fast_info.previous_close` is unreliable for
commodity futures — `CL=F` returned $96.60 while reality (per Stooq)
was $91.02 today, flipping crude's daily change from +3% to −3% and
cascading into the SELL-everything pre-market report.

- New module `prev_close_cache.py`: per-symbol Redis-cached
  yesterday's-close keyed by `(SYMBOL, IST date)`, 24h TTL,
  in-process dict fallback. `put()` is first-write-wins;
  `reconcile_with_quality()` lets a low-trust source's prev be
  overridden by a high-trust one's, returning `(value, "OK" | "DEGRADED")`.
- `_stooq_one` writes the truth value (Stooq's intraday "open", which
  for commodity futures ≈ yesterday's close because they trade ~24h).
- `_yf_one` reads via `reconcile_with_quality(..., max_drift_pct=5.0)`.
  When the cached prev disagrees with yfinance's by > 5%, the cached
  value wins and the entry is tagged `quality: "DEGRADED"`.
- Verified: `_yf_one("CRUDE","CL=F")` returns `+2.977%` (was `-2.795%`).
  Drift-detect logged: "candidate 96.60 drifts 6.13% from cached 91.02 — using cached".

Also purged 6 wrong "OIL DOWN" entries (id 134, 135, 136, 137, 141, 144)
from `db/explainer.db` and triggered an event-bus flush so the rest of
the caches recomputed against corrected data.

### 8. Stooq cooldown reset  (`live_prices.py`)

`_stooq_blocked` used to be a module-global that, once True, stayed True
for the process lifetime. A 11am "limit exceeded" reply meant the
terminal ran on yfinance fallback for the rest of the day. New
`_stooq_check_unblock()` resets after `_STOOQ_COOLDOWN_SECS = 1800`
(30 min). Called at the top of `_stooq_one` and `_stooq_batch`.

### 9. Cross-source quality flag propagation  (`live_prices.py`, `morning_report.py`)

`_entry()` now carries `quality: "OK" | "DEGRADED"`.
`_build_global_signals` reads quality on the watched inputs (`US_10Y`,
`CRUDE`, `GOLD`, `DXY`, `SPX`, `NASDAQ`) and emits
`out["data_quality"] = {"status", "degraded_assets", "checked_at"}`.
`build_market_brief` propagates this into every brief AND appends a
`DATA QUALITY DEGRADED — sources disagree on …` risk warning when
status is `DEGRADED`. `narrate_brief` returns `None` (no LLM call) when
`data_quality.status == "DEGRADED"` — better to render the deterministic
bias alone than to fabricate confident prose grounded in conflicting
source data.

### 10. Explainer LLM grounding validator  (`explainer.py`)

The WHY-MOVE prompt instructs "cite ONLY data points present in the LIVE
SNAPSHOT" but Llama-3 still wrote "crude fell 3.10%" when the snapshot
said `+3.03%`. New `_validate_grounding(parsed, context_block, move)`
runs after every LLM call and **rejects** the explanation when:
- The actual move is UP > 0.5% but the prose contains down-words
  (`"fell"`, `"dropped"`, `"declined"`, `"plunged"`, `"sold off"`, ...),
  or the actual move is DOWN < -0.5% but the prose contains up-words.
- More than 40% of numbers cited in `what_moved + why_it_moved + evidence`
  don't appear in the context block (within 1% tolerance).

Verified: a confabulated "crude fell 3.10% on risk-on rotation" against
actual +3.03% returns `(False, "actual move +3.03% but prose says 'fell'")`.
A correctly-grounded "crude rose 3.03% on Iran tensions" returns
`(True, "")`. Rejected explanations never reach `db/explainer.db`.

### Operational notes for the next session

- **Not yet committed** in `/Users/mahendiran/ai-system/core`. 12 files
  modified/added (3 new: `event_bus.py`, `yield_watch.py`,
  `prev_close_cache.py`). Recommend reviewing the diff per-wave before
  pushing — each wave is independently revertable.
- The container has `tvdatafeed` installed at runtime but the Dockerfile
  change won't take effect until the next image rebuild. To verify the
  rebuild works: `docker compose -f docker-compose.market-terminal.yml
  build --no-cache market-terminal && docker compose ... up -d
  --force-recreate market-terminal`.
- The honest ceiling on free-data sources is now visible: the
  `DATA QUALITY DEGRADED` warning fires when yfinance + Stooq disagree.
  The product is "good for personal use with a credibility floor"; for
  paid users, a Polygon.io Starter ($79/mo) + Trading Economics
  ($50/mo) tier replaces yfinance/Stooq/manual-bonds entirely.
- The Stooq cooldown is set to 30 min. If you see the
  `[live_prices] Stooq cooldown elapsed` log line followed shortly by
  `Stooq daily limit hit` again, lengthen `_STOOQ_COOLDOWN_SECS` to
  3600 (1h) — they may have lowered the daily threshold.

---

## ✅ Shipped 2026-05-22 — AI sidebar cold-start fixes (live now)

The right-hand AI column (TRADE SIGNAL, FIBONACCI + STRUCTURE, AI TRADE
DECISIONS) rendered the literal string `undefined` — or sat on
"Loading AI analysis..." — for the first 1–5 minutes after every boot.
Five commits, each committed, browser-verified (Playwright) on the live
`market-terminal` container, and pushed to `main`.

### 1. Signal panel "undefined" on cold start  (commit `54c5317`)

`/api/signal` returns an empty `{}` while its `_bg_refresh` cache is
cold (~30 s after boot). The frontend painted the missing fields
straight in, so the panel read `Score: undefined | undefined | Vol:
undefined` and `High/Low/Pivot/R1/S1 = undefined`, and only re-polled
every 120 s — so the broken display stuck for up to 2 minutes.

- `_warm()` now warms the `signal` cache at startup.
- `loadSignal()` bails on an empty/error response — keeps the `—`
  placeholders and retries in 8 s instead of 120 s; `smcr()` and the
  score line coerce `undefined`/`null` to `—`.

### 2. `_build_signal()` can no longer hang  (commit `9db581a`)

Every blocking call in `_build_signal()` is now time-bounded:

- The task pool used `with ThreadPoolExecutor()`, whose `__exit__` does
  `shutdown(wait=True)` — one stuck task would hang the whole build
  forever. Now a manually-managed pool: `shutdown(wait=False,
  cancel_futures=True)` + a 15 s `as_completed` budget.
- The four context pre-fetches (`macro`/`news`/`stocks`/`econ`) run
  concurrently under a shared 25 s `futures.wait` budget; a hung source
  degrades to its fallback (`""` / `[]`).
- `detect_market_regime()` moved into the task pool to share the budget.

Verified: with a permanently-hung news provider, `_build_signal()`
returns in ~41 s with partial results instead of hanging forever.

### 3. Decisions panel cold-start lag  (commit `fd4b384`)

The "AI TRADE DECISIONS" panel sat on "Loading AI analysis..." for up
to 5 minutes — the `decisions` cache wasn't warmed and `loadDecisions()`
re-polled only every 300 s.

- `_warm()` warms the `ai_news` + `decisions` caches at startup.
- `loadDecisions()` retries in 8 s on a cold response.

### 4. Warm-up reorder  (commit `e24335b`)

The `signal`/`ai_news`/`decisions` warm-ups ran last in `_warm()`,
behind `stocks`/`earnings`/`nse`. Moved to the front of the non-Railway
sequence (right after the `news` cache they depend on) so the AI column
warms first.

### 5. Build-storm dedup  (commit `bfc584e`)

`_bg_refresh()` spawned a fresh build thread on **every** cold-cache
call. Combined with the new 8 s retry, a single page load piled up ~8
concurrent `_build_signal()` runs that thrashed the shared upstream
data sources and *lengthened* the cold window to ~64 s. Added an
in-flight guard (`_refresh_inflight`) — if a build for a key is already
running, further `_bg_refresh()` calls skip spawning and return the
placeholder until it lands.

**Verified in-browser (Playwright, post-restart):** the AI panels warm
at **~33 s** (was ~64 s during the storm) — **2** `_build_signal` runs
instead of 8, **0** prefetch timeouts. No `undefined` and no stuck
"Loading..." at any sample across the cold window — the panels show
clean `—` / "Loading AI analysis..." placeholders, then populate.

---

## ✅ Shipped 2026-05-22 — Deterministic causal intelligence (live now)

A causal-intelligence layer on top of `event_graph`, deterministic-first —
the LLM narrates its conclusions, never computes them.

### New modules (`1d545df`)

- **`pressure_vector.py`** — layers on `event_graph`; folds **central-bank
  actions** in as a 9th force (via the yields + DXY transmission channels,
  no graph edit) and produces a directional **pressure vector** (9 forces),
  the **dominant driver**, a **net-risk vector**, and a **market-contagion**
  map (affected markets, breadth, 0-1 severity, transmission paths).
- **`contradiction_engine.py`** — aggregates `event_graph`'s macro-internal
  contradictions and adds cross-layer checks (regime-vs-pressure,
  central-bank-vs-market, pressure-vs-observed), rolled into a
  `contradiction_score` + `consistency`.

Both are pure, deterministic, cached, fail-soft and async-safe — no agents,
no recursion, no heavy frameworks. Standalone runners: 29 + 22 checks.

### Wiring (`8953a69`)

- `macro_reasoning_engine.causal_overlay()` — consolidates both engines into
  a narration-ready macro causal summary; the 5-stage pipeline is untouched.
- `morning_report` — computes the overlay, feeds the pressure-vector
  **net-risk** into the `event_graph` consensus vote, tightens confidence
  **stability** with the contradiction-engine `consistency`, and surfaces
  the overlay in every market brief.
- `confidence_engine` / `bias_consensus_engine` need no change — they
  already expose the `stability` hook and the causal signal slot the wiring
  feeds; `SOURCE_WEIGHTS` stays at 7 sources.

Verified live on `/api/morning-report?force=1`: every brief carries the
causal overlay; the consensus vote and confidence stability route through
it. Full suite **438 passed**.

**Central-bank feed — done (`a5c520c`):** `cb_calendar.get_action_tilt()`
aggregates the news-inferred hawk/dove stance of all six central banks
(Fed-weighted) into one `cb_action` tilt; `morning_report` feeds it into
`causal_overlay`, so the 9th force is now live rather than a neutral 0.0.
Verified: a hawkish-Fed news read surfaces as a `central_bank` pressure
of −0.35 in the causal overlay.

---

## ✅ Shipped 2026-05-21 — Reliability hardening (live now)

Three fixes — each committed, tested, and deployed to the `market-terminal`
container (fast-forward merge to `main`, container restarted; code is
bind-mounted so no image rebuild was needed).

### 1. Event-loop blocking fix — `/health` 7–9 s → ~3 ms  (commit `2dad4ba`)

`streaming.PricePublisher.run()` (every 2 s) and the `/api/stream` SSE generator
(every 15 s) called **synchronous `yfinance`/forex HTTP directly on the asyncio
event loop**. A slow upstream froze the loop, so every request — including the
trivial `/health` — queued 7–9 s behind it and the 10 s Docker healthcheck
flapped between healthy/unhealthy.

- Both fetches offloaded to `asyncio.to_thread`; the loop can no longer be
  blocked by data I/O.
- The "5 of 8 background loops `no-heartbeat`" symptom was a **separate
  instrumentation bug** — those 5 loops never called `heartbeat()`. Added the
  missing calls (`continuous_refresh`, `digest`, `morning_note`,
  `signal_verify`, `morning_report`) and reconciled
  `production._get_bg_loop_status()` with the real loop set: dropped the
  one-shot `warm`, added `price_publisher` and `morning_report` → **9 recurring
  loops, all heartbeating**.

### 2. `event_graph.py` hardening — caching, fail-soft + async  (commit `6db39b0`)

The deterministic causal engine feeding `morning_report`, `confidence_engine`
and `bias_consensus_engine` is now production-grade:

- **Cached outputs** — TTL + size-bounded memo keyed on input content; a
  repeated macro snapshot returns an instant deep copy. TTL via
  `EVENT_GRAPH_CACHE_TTL` (default 300 s).
- **Fail-soft** — `analyze()` never raises; a malformed macro feed logs once and
  returns a neutral result flagged `degraded=True`.
- **Async-safe** — new `analyze_async()` offloads to a worker thread.
- Backward-compatible — no consumer changes required.

### 3. Cache-only health probes — `/api/health` always fast  (commit `4d39c2f`)

The `live_data` / `news` / `regime` / `fx` probes used to call the synchronous
network getters, so `/api/health` took **~13 s on a cold cache** (right after a
restart — exceeding the 10 s healthcheck timeout).

They now **peek the in-memory caches** the background loops keep warm and report
a cold/stale cache instead of fetching. `/api/health` now responds in
**6–40 ms even immediately after a restart** — cold → `degraded` (HTTP 200) for
a few seconds, then `healthy` once the loops warm the caches. The `redis` and
`sqlite` probes are unchanged (genuine liveness checks, already fast).

**Test coverage:** full suite **411 passing** (was 395 before this work) — adds
`event_graph` cache/fail-soft/async tests and `tests/test_health_probes.py`.

**Follow-up — done (`417a7dd`):** audited every `yfinance`/`requests` call
site. yfinance 1.2.0 already bounds every HTTP call internally
(`download`/`history` 10 s, metadata 30 s); 100/102 `requests` calls already
passed `timeout=`. Added explicit timeouts to the two that didn't —
`loader.py` (ollama) and `econ.py` (Forex Factory scrape).

**Follow-up — done (`0adbd55`):** audited `_build_morning_note_data`. It was
an `async def` with no `await` whose synchronous Groq `requests.post` ran
directly on the event loop (freezing it for the LLM round-trip once a day at
09:15 IST and on-demand via `/api/morning-note`). Converted to a plain `def`,
now invoked via `asyncio.to_thread` at both call sites.

**Verified end-to-end (2026-05-21):** drove `GET /api/morning-note` on the
live container through a forced cache-miss generation. The note generated
correctly (HTTP 200, 1.85 s, well-formed structured note) and — the key
check — `/health` served **49 requests during that 1.85 s window**, proving
the event loop stayed responsive throughout (a loop blocked by the old sync
path would have served ~1). A re-hit served the cached note in 3.8 ms.

**`?force=` param + JSON-mode fix (`167ffad`, `bd26923`):** added `?force=1`
to `/api/morning-note` for on-demand regeneration (the verification above
had to clear the disk cache and restart for lack of it). `force=1` bypasses
both cache checks and regenerates via the same `asyncio.to_thread` path;
`force=0`/absent is unchanged. Verifying `?force=1` then surfaced a separate
pre-existing bug — the morning-note Groq call omitted
`response_format: {"type": "json_object"}`, so the model intermittently
returned non-JSON and `json.loads` failed → HTTP 503. Added JSON mode
(matching `explainer.py`; the prompt already says "Return JSON").
**Re-verified live:** `?force=1` → HTTP 200 with a valid note, regenerated
off the loop — `/health` served **107 requests during the 1.7 s regen**
(max latency 22 ms), and a no-force re-hit served the cached result in
4.3 ms.

No reliability follow-ups remain open — every known sync-I/O-on-the-event-loop
path (price publisher, SSE stream, health probes, morning-note builder) is
fixed and, for the morning-note builder, verified live.

**`top_3_ideas` single-idea quirk — resolved (`8f54da1`):**
`/api/morning-note` was returning `top_3_ideas` with one idea instead of
three — `SCHEMA_MORNING_NOTE` showed the field as a one-element array
template, so the model mirrored it. The schema now shows three ranked idea
objects, and `max_tokens` was raised 1000 → 1500 so three full ideas can't
truncate the JSON. The 3-idea schema is confirmed in the morning-note prompt;
live output confirms on the next successful generation.

**Groq error handling — typed failures (`d9bae80`):**
`_build_morning_note_data` collapsed every Groq non-200 into an opaque
`groq_error` → HTTP 503 with no logging, so a rate-limit, a bad key, and an
outage were indistinguishable. It now captures Groq's own error message,
logs the status (`[MORNING] groq <code> ...`), and returns a typed error.
`/api/morning-note` maps a rate-limit to **HTTP 429 + `Retry-After`** (not
503) and surfaces the status/detail for every other failure. Verified live
against a real Groq 429: `?force=1` returned 429 with `Retry-After: 1468`
and Groq's "tokens per day" message in the body.

> Note: the morning note depends on the Groq free tier's **100k tokens/day**
> (per-org) budget. When it's exhausted, generation 429s until the daily
> reset — `/api/morning-note` now reports this clearly rather than as a 503.

---

## ✅ Shipped in Step 8 (live now, no breaking changes)

### 1. Comprehensive `/api/health` endpoint

```bash
curl http://localhost:8001/api/health
```

Returns 200 (healthy/degraded) or 503 (unhealthy). Probes:
- Redis connection + memory used
- 18 SQLite databases (size + table count each)
- Groq API key configured
- Telegram bot token + chat configured
- Live data freshness (DXY, GOLD, NASDAQ, US 10Y)
- News feed (headline count)
- Regime engine status (and whether it's in fallback mode)
- Forex pair count
- All 9 recurring background loops with `last_run_secs_ago` heartbeats

Use with any uptime monitor (UptimeRobot, BetterUptime, Pingdom, Datadog).

### 2. Redis service in docker-compose

`market-terminal-redis` runs alongside the API. 256MB cap, allkeys-lru eviction, 60-second snapshot.

All modules with Redis-fallback now use Redis for fast paths:
- `signal_store.py` — signal storage hot reads
- `macro_analyst.py` — chat history with 30-day TTL
- `alert_engine.py` — cooldown dedup keys

When `REDIS_URL` is unset, all modules auto-fall back to SQLite.

### 3. Per-user data isolation (multi-user SaaS)

New module `user_settings.py` + endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/me/settings` | Get current user's settings (defaults + overrides) |
| `POST /api/me/settings {…}` | Update any setting |
| `POST /api/me/watchlist/add {asset}` | Add asset to user's watchlist |
| `POST /api/me/watchlist/remove {asset}` | Remove asset |

Each user has isolated:
- **Watchlist** — 7 assets default, customisable
- **Alert thresholds** — override the global VIX/Gold/Yield/DXY thresholds
- **Telegram chat ID** — personal alerts (overrides global `TELEGRAM_CHAT_ID`)
- **Preferences** — default tab, theme, `show_india` flag for forex tenants

Storage: SQLite `user_settings.db`, indexed by lowercase username.

### 4. Production stability layer (`production.py`)

```python
from production import log, retry, healthy_or, rate_limit_check, register_health, heartbeat
```

Provides:
- **Structured logging** — `log("INFO", "scope", "msg", k=v, k=v)` — single-line, parseable
- **Retry decorator** — `@retry(max_attempts=3, base_delay=0.5)` — exponential backoff, logs each attempt
- **Graceful fallback** — `@healthy_or(default_value)` — never propagates exceptions
- **Rate limiter** — sliding window per IP key
- **Health probe registry** — `register_health("name", probe_fn)` from any module
- **Heartbeat tracking** — `heartbeat("loop_name")` so `/api/health` can detect stale loops

### 5. Rate limiting (per-IP, in-memory)

Middleware applied automatically:
- **Default:** 120 req / 60s per IP (general endpoints)
- **Heavy AI endpoints:** 10 req / 60s per IP (`/api/analyst/chat`, `/api/explainer/generate`, `/api/alerts/run-now`)
- **Whitelisted (no limit):** `/static/*`, `/health`, `/api/health`, `/login`, `/logout`

When triggered:
```json
HTTP 429
{"error": "rate_limited", "retry_after_secs": 42, "limit": 10, "window_secs": 60}
```

### 6. Worker entry point (`worker.py`)

Runs ONLY the background loops, no HTTP server. For production split:

```bash
# Run all loops in a separate container
python worker.py

# Run only one (debug)
python worker.py --only=alerts
```

Available loops: `refresh`, `macro_snap`, `explainer`, `alerts`, `signal_verify`

The `docker-compose.market-terminal.yml` includes a commented `market-terminal-worker` service. Uncomment to deploy as a separate container.

---

## 🟡 Deferred (genuine production concerns, planned next)

These need careful planning + downtime, intentionally NOT done in this session:

### Postgres migration (replace 18 SQLite files)

**Why deferred:**
- Live system has real users + active SQLite data
- Migration requires schema mapping for each of 18 DBs
- Zero-downtime migration needs dual-write phase

**When to do:**
- When you cross ~1000 active users
- When SQLite write contention becomes visible (sqlite3.OperationalError: database is locked)

**Effort:** ~2 weeks: schema design + dual-write + cutover + rollback plan

### Container split (frontend / api / worker / redis / db)

**Why deferred:**
- Single container is simpler to operate at current scale
- Splitting requires reverse proxy (Caddy/Traefik), TLS termination, internal networking
- Worker container needs same Python deps + db volume mounts

**When to do:**
- When you need to scale workers independently of API (e.g., 5 worker replicas, 1 API)
- When you have an ops team to maintain the orchestration

**Effort:** ~1 week: nginx/Caddy front-end, k8s or docker-swarm orchestration

### Authentication hardening (already mostly done)

What you have:
- ✅ PBKDF2 password hashing (260K iterations, salt per user)
- ✅ Random session tokens, 30-day TTL
- ✅ HTTP-only cookies, samesite=lax
- ✅ Admin role-based gating
- ✅ Random admin password on fresh install (Step 8 earlier session)

What's deferred:
- 2FA (TOTP) — adds friction, optional
- OAuth (Google/Apple) — for retail tier
- API key auth (for programmatic clients)
- Session revocation UI (admin can already do via DB)

---

## Common ops tasks

### Restart cleanly

```bash
docker compose -f "/Users/mahendiran/Bloomberg feed/docker-compose.market-terminal.yml" restart
```

### Check health

```bash
curl -s http://localhost:8001/api/health | jq
```

### Tail structured logs

```bash
docker logs -f market-terminal | grep -E "INFO|WARN|ERROR"
```

### Disable alerts globally (kill switch)

```bash
echo "ALERT_DISABLED=true" >> /Users/mahendiran/ai-system/core/.env
docker compose -f "/Users/mahendiran/Bloomberg feed/docker-compose.market-terminal.yml" restart market-terminal
```

### Switch background loops to dedicated worker container

1. In `docker-compose.market-terminal.yml`, uncomment the `market-terminal-worker` service
2. In API env, add `WORKER_DISABLE_INLINE=true` (then add a check for this in `dashboard_api.py` lifespan to skip starting the inline loops)
3. `docker compose up -d`

---

## Environment variables (full list)

| Var | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | (set) | LLM for analyst/explainer |
| `TELEGRAM_BOT_TOKEN` | (set) | Bot for alerts |
| `TELEGRAM_CHAT_ID` | (set) | Default chat (per-user override available) |
| `REDIS_URL` | redis://redis:6379/0 | Redis connection (auto-set in compose) |
| `LOG_LEVEL` | INFO | DEBUG / INFO / WARN / ERROR |
| `ADMIN_PASSWORD` | (random) | Default admin pw on fresh install |
| `ALERT_DISABLED` | false | Global alerts kill-switch |
| `ALERT_VIX_SPIKE_PCT` | 15 | VIX trigger |
| `ALERT_GOLD_PCT` | 0.8 | Gold breakout trigger |
| `ALERT_YIELD_SHOCK_PCT` | 0.5 | Yield shock trigger |
| `ALERT_DXY_PCT` | 0.4 | DXY reversal trigger |
| `ALERT_COOLDOWN_SECS` | 3600 | Per-key cooldown |
| `ALERT_MIN_CONF` | 80 | High-conf explainer trigger |
| `WORKER_DISABLE_INLINE` | false | Skip inline bg loops (when using worker container) |
