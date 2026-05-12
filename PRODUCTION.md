# Production Architecture — Status & Roadmap

This document tracks what's production-ready in the terminal as of **2026-05-12**.

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
- All 8 background loops with `last_run_secs_ago` heartbeats

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
