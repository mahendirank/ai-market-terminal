# PROJECT_ANALYSIS.md — `ai-system/core/` (Zyvora Terminal)

> Phase 1 artifact. Read-only analysis. No code modified.
> Generated: 2026-05-18.

## Identity

| Aspect | Value |
|---|---|
| Local dir | `~/ai-system/core/` |
| Git remote (per ops scripts) | `github.com/mahendirank/ai-market-terminal` |
| Product brand | **Zyvora Terminal** (zyvoratech.co) |
| Container name | `market-terminal` |
| Prod install path | `/opt/zyvora/` (Hostinger Ubuntu VPS) |
| Git branch (local) | `main`, 2 dirty files |
| Last activity | 2026-05-18 (today) |

**Naming inconsistency**: three names for one product (`ai-system` / `ai-market-terminal` / `Zyvora`). Documented in TECH_DEBT_REPORT.md §1.

## What it is

A multi-user, multi-asset market intelligence terminal. FastAPI on port 8001 serving a single-page dashboard, backed by Redis + 18 SQLite databases, with 8 background loops for live data ingestion. Caddy reverse proxy in production. Multi-tenant with per-user data isolation (watchlists, alerts, settings, chat history).

Data sources include yfinance, NSE, FRED, CoinGecko, Capitol Trades, Telegram, plus AI providers Groq, Perplexity, and Anthropic.

## Code scale

| Metric | Value |
|---|---|
| Python files | 105 (all flat in `core/`) |
| Total Python LOC | **27,376** |
| Shell scripts | 7 |
| HTML templates | 3 |
| Test files | 6 (in `tests/`) |
| Test-to-module ratio | ~6% |

### Top 10 files by size

| LOC | File | Role |
|---|---|---|
| 2,990 | `dashboard_api.py` | **God file**. FastAPI app, routes, lifespan, background loops, auth wiring |
| 1,041 | `macro_reasoning_engine.py` | Macro analysis layer |
| 875 | `market_intel.py` | Market intel aggregator |
| 872 | `signal_memory.py` | Signal persistence + dedup |
| 748 | `notify.py` | Telegram + email notification dispatch |
| 733 | `earnings_telegram.py` | Earnings calendar Telegram poster |
| 650 | `alert_engine.py` | Alert evaluation + cooldown |
| 579 | `indicators.py` | TA indicators |
| 571 | `news.py` | News fetch + parse |
| 554 | `earnings.py` | Earnings data |

## Entry points

| Entry | What it does | Status |
|---|---|---|
| `run.py` | Loads `.env`, starts `uvicorn dashboard_api:app` on `$PORT` (default 8001) | **Canonical prod entry** |
| `dashboard_api.py` | The FastAPI app object | Production ASGI app |
| `terminal.py` | CLI variant: imports macro/news/stocks/econ/smc/sniper/mtf/etc., prints to stdout | Working but parallel surface |
| `engine.py` | Anthropic-backed `run(user_input)` helper | **Broken** — `from loader import build_system_prompt` but `loader.py` only exports `build_prompt` |
| `~/ai-system/app.py` (parent) | Streamlit shell that calls `core.loader.build_prompt` + `run_qwen` | Imports resolve; product role unclear |

Two unrelated functional surfaces (FastAPI web app + CLI terminal + Streamlit shell) coexist. The FastAPI surface is the only one operationally live.

## Infrastructure

`docker-compose.prod.yml` defines three services:

| Service | Image | Purpose |
|---|---|---|
| `market-terminal` | built from local `Dockerfile` | The FastAPI app |
| `redis` | image | Cache + cooldowns + chat history |
| `caddy` | image | Reverse proxy + auto-TLS |

Volumes: `terminal_db` (mounted at `/app/db` — holds the 18 SQLite files), `redis_data`, `caddy_data`, `caddy_config`.

Local dev compose (`docker-compose.yml`) has only `market-terminal` with healthcheck.

## Production posture (per `PRODUCTION.md`, dated 2026-05-12)

- `/api/health` endpoint returning 200/503 with full subsystem status
- 18 SQLite databases probed for size + table count
- 8 background loops with `last_run_secs_ago` heartbeats
- Redis service with 256MB cap + `allkeys-lru` eviction
- Per-user data isolation: `user_settings.py` + `/api/me/...` endpoints
- Hot fallback: every Redis-using module falls back to SQLite if `REDIS_URL` unset

This is a real shipped product, not a prototype. The architecture decisions are mostly sound. The debt is structural, not functional.

## Operational scripts

| Script | Purpose | Safety |
|---|---|---|
| `deploy.sh` | Fresh-VPS install (`curl ... \| bash` on Ubuntu) | Requires root; idempotent enough |
| `fill-env.sh` | Interactive .env filler post-deploy | Well-bounded |
| `setup-backup.sh` | Installs daily 22:00 UTC cron for `/opt/zyvora/backup.sh` | Idempotent |
| `backup.sh` | (referenced, not read in this pass) | Phase 2 audit |
| `nuclear-reset-admin.sh` | Wipes `auth.db`, regenerates admin password from `.env` | **Well-documented and intentional** — admin-recovery escape hatch. Not dangerous by default; requires deliberate execution. |
| `Caddyfile` | Caddy reverse-proxy config | Phase 2 audit |
| `Dockerfile` | Build recipe | Phase 2 audit |

## Tests

`core/tests/` contains:

```
__init__.py
macro_integration_examples.py
test_hni_macro_integration.py
test_prompt_builder_reasoning.py
test_stage2.py
test_stage3.py
test_stage4.py
test_stage5.py
```

Stage-gated (test_stage2 through 5), not per-module unit tests. Coverage on `dashboard_api.py` (2,990 LOC) and most ingestion modules is effectively zero. No pytest config (`pyproject.toml`, `setup.cfg`, or `pytest.ini`) found at the `core/` level.

## Dependencies (`requirements.txt`)

```
fastapi, uvicorn[standard], yfinance, pandas, numpy, requests,
beautifulsoup4, feedparser, lxml, python-multipart, httpx,
aiohttp, aiofiles, nltk, websocket-client, redis>=5.0,
python-dotenv, ta>=0.11.0
```

No version pins for most packages. `anthropic`, `openai`, `groq` clients are imported in code but absent from `requirements.txt` (covered separately or via transitive deps?). Phase 2 audit.

## Surface area summary

- **1 web app** (FastAPI :8001, multi-tenant, Caddy-fronted)
- **3 infra services** (Redis, app, Caddy)
- **8 background loops** (per PRODUCTION.md)
- **18 SQLite DBs** (per-user + system caches)
- **3 AI providers** (Groq, Perplexity, Anthropic) + local fallbacks
- **~15 external data sources** (yfinance, NSE, FRED, CoinGecko, Capitol Trades, Telegram, etc.)
- **2 notification channels** (Telegram, email per `notify.py`)

## What's not in this analysis

- Code-coverage measurement (no pytest run was performed)
- Static type checking (mypy/pyright)
- Profiling / latency
- Actual port + ufw audit on the live VPS
- Security review of auth flow, session handling, rate limiting
- `backup.sh`, `Caddyfile`, `Dockerfile` content (deferred to Phase 2)

These are deferred to Phase 2 — they need either runtime access or focused review and don't belong in a Phase 1 read-only pass.
