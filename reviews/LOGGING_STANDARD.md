# LOGGING_STANDARD.md

> **Phase A: shipped in Sprint 2 (2026-05-18). Phase B: deferred to Sprint 3+.**
>
> This document supersedes the Sprint 1 proposal version. Sections marked
> "As shipped" describe what currently exists in the codebase. Sections
> marked "Future" describe what's not yet implemented.

---

## 0. TL;DR — how to use this today

```python
# In any module:
import logging
log = logging.getLogger(__name__)

log.info("clean_string_message")                          # plain
log.info("with_fields", extra={"asset": "NQ", "price": 18234.5})  # structured
log.exception("external_api_failed", extra={"service": "groq",
                                            "error_category": ErrorCategory.EXTERNAL_API})
```

```bash
# Production env (.env or container env):
LOG_LEVEL=INFO                   # DEBUG / INFO / WARNING / ERROR
LOG_FORMAT=console               # console (text+ctx prefix) | json
LOG_HTTP_REQUESTS=true           # emit per-request structured line
UVICORN_ACCESS_LOG=off           # silence uvicorn's plain access log
```

Every HTTP response now carries `X-Request-ID`. Every log record carries
`request_id` from the active ContextVar. JSON mode emits one JSON object
per line — pipe `docker logs market-terminal` through `jq` for ad-hoc
queries.

---

## 1. What's in place (as shipped, Sprint 2 Phase A)

### Modules added

| File | Role |
|---|---|
| `core/logging_config.py` | `setup_logging()`, `JsonFormatter`, `ContextFilter`, ContextVars (request_id, tenant_id, trace_id, agent_name), `ErrorCategory`, `new_request_id()` |
| `core/logging_middleware.py` | `RequestContextMiddleware` — ASGI; sets request_id, tracks latency, logs `request_complete` |

### Wiring

| File | Change |
|---|---|
| `core/run.py` | Calls `setup_logging()` inside `__main__` block, before importing `dashboard_api` |
| `core/dashboard_api.py` | `app.add_middleware(RequestContextMiddleware)` after existing CORS / Auth / RateLimit middleware |
| `core/.env.production.example` | Adds `LOG_FORMAT`, `LOG_HTTP_REQUESTS`, `UVICORN_ACCESS_LOG`; clarifies `LOG_LEVEL` |

### JSON envelope (stable contract)

```json
{
  "ts": "2026-05-18T14:23:45.123Z",
  "level": "INFO",
  "logger": "http.request",
  "msg": "request_complete",
  "request_id": "a1b2c3d4e5f6",
  "tenant_id": "-",
  "trace_id": "-",
  "agent": "-",
  "method": "GET",
  "path": "/api/health",
  "status": 200,
  "duration_ms": 12.34
}
```

Field guarantees:
- The 8 envelope fields (`ts` … `agent`) are **always present**, even if the underlying ContextVar is unset (then they emit `"-"`).
- Any `extra={...}` keys are appended at the top level.
- Unserializable extras fall back to `repr(v)` rather than failing the log call.
- Exceptions add `exc_type`, `exc_msg`, `exc_traceback` fields.

### Console envelope (default, human-readable)

```
2026-05-18 14:23:45 INFO    [http.request] [a1b2c3d4e5f6] request_complete
```

Equivalent context, less machine-friendly. The default for Sprint 2 to preserve familiar `docker logs` UX.

### Tests

- `tests/test_logging_config.py` — 10 tests
- `tests/test_logging_middleware.py` — 7 tests

All marked `@pytest.mark.smoke`. Run with `pytest -m smoke`.

---

## 2. Context variables (async-safe)

Four declared in `logging_config.py`:

| ContextVar | Set by | Default | Future consumer |
|---|---|---|---|
| `request_id_var` | `RequestContextMiddleware` per request | `"-"` | OTel span resource attribute |
| `tenant_id_var` | route handlers after auth resolves | `"-"` | per-tenant log shipping |
| `trace_id_var` | (reserved) | `"-"` | OpenTelemetry trace ID |
| `agent_name_var` | (reserved — Sprint 3 `BaseAgent.run_once`) | `"-"` | per-agent metric labels |

ContextVars propagate automatically across `await`. For `asyncio.create_task`, each task receives a copy at task-creation time — manual `request_id_var.set()` is needed inside long-lived background tasks if you want a fresh ID per tick. Sprint 3's `BaseAgent` will do this in `run_once`.

**Anti-pattern (don't do this in async code):**
```python
# This works in single-threaded sync code only.
threading.local().request_id = "..."     # ❌ not async-safe
```

**Correct pattern:**
```python
token = request_id_var.set("...")
try:
    await some_async_work()
finally:
    request_id_var.reset(token)
```

The middleware uses exactly this pattern.

---

## 3. Error classification (constants only — no enforcement yet)

`ErrorCategory` in `logging_config.py` provides string constants:

```
EXTERNAL_API   "external_api"     yfinance, NSE, Groq, Anthropic, Telegram
DATABASE       "database"         SQLite locked / Redis OOM
VALIDATION     "validation"       bad caller input
INTERNAL       "internal"         unexpected exceptions / our bugs
TIMEOUT        "timeout"          deadline hit
RATE_LIMIT     "rate_limit"       external rate limit
AUTH           "auth"             session / token errors
CIRCUIT_OPEN   "circuit_open"     skipped due to open circuit (Sprint 3+)
```

Usage convention:

```python
try:
    response = httpx.get("https://api.groq.com/...")
except httpx.TimeoutException as e:
    log.exception("groq_call_timeout",
                  extra={"error_category": ErrorCategory.TIMEOUT,
                         "service": "groq"})
    raise
except httpx.HTTPError as e:
    log.exception("groq_call_failed",
                  extra={"error_category": ErrorCategory.EXTERNAL_API,
                         "service": "groq",
                         "status": getattr(e.response, "status_code", None)})
    raise
```

Sprint 3+ ties these to Prometheus counters: `errors_total{category="external_api", service="groq"}`.

---

## 4. Phase B — migration path for the 328 `print()` calls

**Not done in Sprint 2.** Phase A only adds the infrastructure; existing prints continue working unchanged.

Migration plan (Sprint 3+):

### Module ordering (lowest risk first)
1. `regime.py`, `forex.py`, `econ.py` — data fetchers, low coupling
2. `news.py`, `news_deduper.py`, `news_fetch.py` — ingest family
3. `signal_memory.py`, `alert_engine.py` — durable consumers
4. `dashboard_api.py` — last (66 prints, biggest payoff + risk)

### Per-module mechanical translation

| Before | After |
|---|---|
| `print(f"[REGIME] entered defensive")` | `log.info("regime_entered_defensive")` |
| `print(f"[REGIME] score={s}", flush=True)` | `log.info("regime_score", extra={"score": s})` |
| `print(f"[REGIME] error: {e}")` *(inside except)* | `log.exception("regime_error", extra={"error_category": ErrorCategory.INTERNAL})` |
| `print(f"[X] {datetime.now()}: ...")` | drop the timestamp — `ts` is in the envelope already |

### Rule per PR

- One module per PR.
- Each PR adds a regression test that the expected log lines appear (`caplog` fixture).
- No PR mixes a `print → log` migration with a logic change.
- Old `flush=True` is dropped — `StreamHandler` flushes automatically.

### CLI tools are exempt

`terminal.py` and `claude_bridge.py` are CLI scripts whose stdout IS the UX. Leave their `print()` calls alone.

---

## 5. Operational notes

### Toggling formats

```bash
# Read structured logs locally during dev:
LOG_FORMAT=json docker compose -f docker-compose.prod.yml up market-terminal
docker logs market-terminal | jq 'select(.level=="ERROR")'

# Back to human-readable:
unset LOG_FORMAT  # or set LOG_FORMAT=console
```

### Common queries (jq)

```bash
# Failed requests in last container run:
docker logs market-terminal | jq 'select(.msg=="request_complete" and .status >= 500)'

# All errors for one request:
docker logs market-terminal | jq 'select(.request_id=="a1b2c3d4e5f6")'

# Slowest 20 requests:
docker logs market-terminal | jq -s 'map(select(.msg=="request_complete")) | sort_by(.duration_ms) | reverse | .[:20]'

# All log lines for a tenant (Sprint 3+ when tenant_id_var is populated):
docker logs market-terminal | jq 'select(.tenant_id=="acme-corp")'
```

### When to switch to JSON

Switch `LOG_FORMAT=json` in production when **any of**:
1. You set up a log shipper (Loki, CloudWatch, Datadog).
2. You start running `jq` queries more than once a week.
3. You exceed ~10 req/s and `grep` over `docker logs` becomes too slow.

Until then, `console` is fine.

---

## 6. Log retention

Currently relies on Docker's default `json-file` driver — see `OBSERVABILITY_PLAN.md` §3 for the recommended `logging.options` block to add to `docker-compose.prod.yml`. Not part of Phase A because it requires a container restart that the user controls.

---

## 7. Backward compatibility

| Pattern | Status |
|---|---|
| Existing `print()` calls | Continue to work; bypass logging entirely. Migrated incrementally in Phase B. |
| `logging.getLogger().debug()` in 8 modules | Now produce output when `LOG_LEVEL=DEBUG`. Silent at default INFO — preserves current behavior. |
| Uvicorn's plain access log | Silenced by default; re-enable with `UVICORN_ACCESS_LOG=on`. |
| WebSocket connections | Not yet correlated. Sprint 3 work — WebSocket has different lifecycle. |

---

## 8. Things this standard intentionally does NOT do

- **Force migration**: 328 prints stay until each is migrated in a code PR.
- **Block on unparseable input**: if a log record can't be serialized, we fall back to repr or a minimal error envelope — never raise.
- **Add structlog or loguru**: stdlib `logging` is enough; one less dep to audit.
- **Configure log shipping**: that's a deploy concern, covered in OBSERVABILITY_PLAN.md.
- **Auto-redact secrets**: the existing `.env`-secret discipline is sufficient at current scale. A redaction filter can be added later as a `ContextFilter` subclass if needed.
