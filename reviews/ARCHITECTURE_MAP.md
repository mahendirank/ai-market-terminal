# ARCHITECTURE_MAP.md — `ai-system/core/`

> Phase 1 artifact. Derived from file naming, imports, and `PRODUCTION.md`. Cross-reference with the live system before acting on it.

## Runtime topology (production)

```
                 Internet
                    │
              :443 / :80
                    ▼
            ┌──────────────┐
            │    Caddy     │  auto-TLS, reverse proxy
            └──────┬───────┘
                   │ proxy_pass
                   ▼
        ┌──────────────────────┐         ┌───────────────┐
        │  market-terminal     │◄────────┤    Redis      │
        │  uvicorn :8001       │         │  256MB cap    │
        │  (run.py →           │         │  allkeys-lru  │
        │   dashboard_api:app) │         └───────────────┘
        └──────┬───────────────┘
               │
               ▼
        ┌──────────────────────┐
        │  /app/db  (volume)   │
        │  18× SQLite files    │
        │  • auth.db           │
        │  • signal_*.db       │
        │  • market_memory.db  │
        │  • earn_tg_cache.db  │
        │  • per-user *.db     │
        │  • …                 │
        └──────────────────────┘

External: GROQ, PERPLEXITY, ANTHROPIC, TELEGRAM, yfinance, NSE,
          FRED, CoinGecko, Capitol Trades, TwelveData (opt),
          Polygon (opt)
```

## Module groups (105 Python files → 13 functional clusters)

The codebase has no package boundaries — every module sits flat in `core/`. The groupings below are conceptual, derived from file names. **A Phase 2 refactor proposal should formalize these as Python packages with `__init__.py`.**

### 1. HTTP / API layer (3 files)
- `dashboard_api.py` (2990) — FastAPI app, routes, lifespan, background loops
- `production.py` (288) — production-specific helpers
- `webhook.py` — inbound webhook handlers

### 2. Auth + multi-tenant (4 files)
- `auth.py` (324) — session cookies, password hashing
- `tenants.py` (319) — multi-tenant resolution
- `user_settings.py` — per-user settings + watchlists
- `production.py` — overlaps

### 3. AI orchestration layer (9 files)
- `ai_layer.py` (281), `ai_router.py` (431), `ai_models.py` (169), `ai_schemas.py` (164), `ai_persona.py` (417)
- `claude_bridge.py` — Anthropic bridge
- `perplexity.py` (303), `groq_research.py` (498)
- `prompt_builder.py` (349)

### 4. Macro / economic (10 files)
- `macro.py` (192), `macro_brain.py`, `macro_analyst.py` (462), `macro_desk.py` (448)
- `macro_reasoning_engine.py` (1041), `macro_scenarios.py` (495)
- `econ.py` (206), `econ_calendar.py` (235), `cb_calendar.py` (232)
- `fred_data.py` (186)

### 5. News + intel (6 files)
- `news.py` (571), `news_deduper.py` (299), `telegram_news.py`
- `market_intel.py` (875), `intel_cluster.py` (191)
- `event_classifier.py` (347)

### 6. Earnings (4 files)
- `earnings.py` (554), `earnings_social.py` (361), `earnings_telegram.py` (733)
- `nse_earnings.py` (287)

### 7. Price + symbol data (7 files)
- `live_prices.py` (441), `stocks.py`, `screener_data.py` (267)
- `tvdata.py` (286), `nse_data.py` (271)
- `symbol_resolver.py` (449), `sector_pulse.py` (159)

### 8. Signal engine (5 files)
- `signal_store.py` (243), `signal_memory.py` (872)
- `trade_signal.py` (280), `decision_engine.py` (184)
- `alert_engine.py` (650)

### 9. Regime + structure (8 files)
- `regime.py` (514), `regime_engine.py` (438)
- `vix_term.py` (157), `indicators.py` (579)
- `smc.py`, `smc_entry.py` (207), `structure.py`, `reversal.py`

### 10. Forex + correlations (4 files)
- `forex.py` (315), `correlations.py` (167), `correlation_engine.py` (304)
- `cot_data.py` (190)

### 11. Insider / political flow (2 files)
- `insider_tracker.py` (206), `capitol_trades.py`

### 12. Sentiment + explanation (3 files)
- `sentiment_weighting.py` (233), `explainer.py` (531)
- `ai_persona.py` — also fits here

### 13. Plumbing / utility (rest)
- `notify.py` (748) — Telegram + email dispatch
- `loader.py` (179) — prompt building + domain detection
- `worker.py` (161), `streaming.py` (239)
- `chart_context.py` (294), `executor.py`, `agents.py`
- `backtest.py`, `data.py`, `loader.py`, `engine.py` (broken)
- `terminal.py` — CLI variant
- `run.py` — uvicorn launcher

## Data stores

| Store | Where | Purpose | Notes |
|---|---|---|---|
| **Redis** | `redis://redis:6379` container | Hot path: signal cache, chat history (30-day TTL), alert cooldowns | 256MB cap, `allkeys-lru` |
| **SQLite × 18** | `/app/db/*.db` (named volume `terminal_db`) | Durable: auth, per-user settings, signals, news cache, market memory, earnings cache, etc. | Backed up daily 22:00 UTC by `setup-backup.sh` cron |
| **`.env`** | `/opt/zyvora/.env` on VPS | Secrets (GROQ, ANTHROPIC, TELEGRAM, ADMIN_PASSWORD, alert thresholds) | Gitignored. Filled by `fill-env.sh` |

## Background loops (8, per PRODUCTION.md)

Names/cadences not enumerated in PRODUCTION.md. To map them, grep `dashboard_api.py` for `asyncio.create_task` and the `lifespan` startup block. Deferred to Phase 2.

## Request flow (inferred — verify in Phase 2)

```
HTTPS request
    → Caddy (TLS termination, proxy_pass)
    → uvicorn :8001
    → FastAPI middleware: CORS, auth (auth.py cookie check via COOKIE_NAME)
    → tenants.py resolves tenant context
    → dashboard_api route handler
    → reads from Redis (hot path) or SQLite (cold path)
    → may invoke ai_router → groq / anthropic / perplexity
    → JSON / HTML / WebSocket response
```

## Module-import dependencies (raw, top 10 internal imports)

```
5 × from smc
5 × from news
4 × from trade_signal, stocks, macro, econ
3 × from structure, sniper, mtf, interpreter, executor
2 × from surprise, smc_entry, priority, ppt_generator, notify, loader, agents
```

The hot internal modules (`smc`, `news`, `macro`, `econ`, `trade_signal`) are good candidates for the first package extraction in Phase 2.

## What's missing for a complete map

- **Background-loop names + cadences** — read `dashboard_api.py` lifespan block
- **Route inventory** — `grep -E '@(app|router)\.(get|post|put|delete|websocket)' dashboard_api.py`
- **External API call sites** — `grep -E '(groq|anthropic|perplexity|telegram)\.com'`
- **SQLite schema** — read each `.db` table list via `sqlite3 *.db .schema`

These are Phase 2 tasks. The package-boundary proposal (groups 1–13 above) is the structural foundation.
