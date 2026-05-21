# Production Architecture — Status & Roadmap

This document tracks what's production-ready in the terminal. **Last updated 2026-05-21** — reliability hardening (section below). Step 8 baseline: 2026-05-12.

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
