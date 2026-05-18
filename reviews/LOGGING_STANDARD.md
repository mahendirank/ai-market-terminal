# LOGGING_STANDARD.md — Proposal

> Sprint 1 artifact — task #7. **Audit only.** No code rewritten.
> Migration work is scheduled for Sprint 2.

---

## What's there today

| Pattern | Count | Where | Notes |
|---|---|---|---|
| `print(...)` | **328 calls** | 40+ modules | Dominant pattern. `dashboard_api.py` alone has 66. |
| `print(f"[MODULE] msg", flush=True)` | ≥30 sites | dashboard_api, alert_engine, signal_memory, etc. | Pseudo-structured: a `[TAG]` prefix and `flush=True`. Migration-friendly. |
| `logging.getLogger(__name__)` + `log.debug(...)` | 8 modules | ai_router, ai_persona, correlation_engine, market_memory, indicators, sentiment_weighting, market_intel, regime_engine | Calls .debug() only. **Effectively silent** in prod — no handler attached, root logger sits at WARNING. |
| `sys.stderr.write` | 0 | — | Not used. |
| `structlog` / `loguru` | 0 | — | Not used. |
| `logging.dictConfig` / `basicConfig` | 0 | — | **No centralized config exists.** |
| Uvicorn logger | 1 site | `run.py:31` | `log_level="info", access_log=True` |
| Domain logger (`logger.py`) | 1 file, orphan consumer | imported only by orphan `mt5_bot.py` | Not a general logging module — it's a trade-log writer. |

**Symptoms of the current state:**
- The 8 modules already using stdlib `logging` produce **zero output in production** — their `.debug` calls are below the active log level.
- The 328 `print()` calls all hit stdout. They show up in `docker logs market-terminal` and Caddy access logs in the same stream — no severity filtering.
- No structured fields (timestamp, level, request_id, tenant_id) — debugging multi-tenant issues from logs alone is hard.
- `flush=True` on every print → guaranteed delivery but extra syscalls.

---

## Proposed standard

A two-phase migration. Phase A is the **config-only** change; Phase B is the gradual `print` → `log.*` migration that lets the system improve incrementally.

### Phase A — Centralized config (Sprint 2, ~1 day)

Add `core/logging_config.py`:

```python
import logging
import logging.config
import os
import sys

def setup(level: str | None = None) -> None:
    """Idempotent. Call once at startup before any FastAPI / uvicorn import."""
    level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "logging_config.JsonFormatter",
            },
            "console": {
                "format": "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "json" if os.environ.get("LOG_FORMAT") == "json" else "console",
            },
        },
        "root": {"level": level, "handlers": ["stdout"]},
        "loggers": {
            "uvicorn":        {"level": level, "propagate": True},
            "uvicorn.access": {"level": level, "propagate": True},
            "uvicorn.error":  {"level": level, "propagate": True},
        },
    })

class JsonFormatter(logging.Formatter):
    """One-line JSON per record. Minimal — no extras until we need them."""
    def format(self, record):
        import json, time
        return json.dumps({
            "ts":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
        })
```

Call from `run.py` **before** importing dashboard_api:

```python
# run.py — top of __main__
from logging_config import setup
setup()
```

**Effect of Phase A alone:**
- Existing `log.debug(...)` calls start producing output (controllable via `LOG_LEVEL` env var)
- All logs share one timestamp/level format
- Set `LOG_FORMAT=json` in prod for structured logs; leave unset for dev
- Existing `print()` calls are unaffected — no breakage

**Rollback**: `git revert` the commit; remove `logging_config.py` + the `setup()` call. Behavior reverts to today's.

### Phase B — Incremental `print` → `log.*` migration (Sprint 3+, multiple PRs)

Convert `print(f"[TAG] msg", flush=True)` to `log.info("msg")` one module at a time. Use a script:

```bash
# tools/migrate_prints.py  (write in Sprint 2)
# For each *.py:
#   1. If "import logging" absent, add: import logging; log = logging.getLogger(__name__)
#   2. Replace print(f"[TAG] msg", flush=True) → log.info("msg")
#   3. Replace print(f"[TAG] err: {e}", flush=True) inside `except` → log.exception("err: %s", e)
#   4. Leave bare print() in CLI tools (terminal.py, claude_bridge.py) alone.
```

Order of modules (lowest risk first, prod-critical last):
1. `regime.py`, `forex.py`, `econ.py` (data fetchers, low coupling) — 1 PR
2. `signal_memory.py`, `alert_engine.py` (already use SQLite) — 1 PR each
3. `dashboard_api.py` (66 prints — biggest payoff, biggest risk) — last

**Rule per PR**:
- Each PR converts ONE module.
- Each PR adds a regression test that checks specific log lines appear (using `caplog`).
- No PR mixes a `print → log` migration with a logic change.

---

## What this proposal does NOT cover

- **Tracing / OpenTelemetry**: separate Sprint 4 concern. Logs alone won't give per-request tracing.
- **Log shipping**: where logs go after stdout (Loki? CloudWatch? File rotation?) is a deploy concern, not a code concern. Today `docker logs` is the truth.
- **Sentry / error tracking**: also Sprint 4. Logs are the prerequisite.
- **Per-tenant log enrichment**: needs request-scoped context (FastAPI middleware) — proposal to come after the `dashboard_api.py` split (Sprint 3).

---

## Recommendation

**Do Phase A in Sprint 2** (~1 day). It's a config-only change that immediately turns on the 8 modules' silent `log.debug` calls and unifies output format. Zero risk to production behavior (text output goes to the same stdout, just with a different formatter).

**Defer Phase B to Sprint 3** — and only start it after the test coverage from Sprint 1 has stabilized, since `print → log` migrations can subtly change behavior (e.g. uncaught exceptions in format strings).
