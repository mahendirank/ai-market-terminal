# CIRCUIT_BREAKER_PLAN.md

> Sprint 3 deliverable. State diagram, failure classification, when to
> open vs. close, and policy for wiring breakers into external calls.

---

## 1. State diagram

```
                ┌───────────────┐
                │    CLOSED     │ ← normal operation
                │  failures=0   │
                └───────┬───────┘
                        │ failure
                        │ failures += 1
                        ▼
                ┌───────────────┐
                │    CLOSED     │
                │  failures=N-1 │
                └───────┬───────┘
                        │ failure
                        │ (failures = N, threshold hit)
                        ▼
                ┌───────────────┐
                │     OPEN      │ ← reject calls fast
                │  opened_at    │
                └───────┬───────┘
                        │ recovery_timeout elapses
                        ▼
                ┌───────────────┐
                │   HALF_OPEN   │ ← one probe at a time
                └───┬───────┬───┘
            probe   │       │   probe
              fails │       │   succeeds
                    ▼       ▼
                  OPEN    CLOSED
```

- `failure_threshold` (default 5): consecutive failures in CLOSED before tripping.
- `recovery_timeout` (default 30s): time spent OPEN before allowing one probe.
- `half_open_success_threshold` (default 1): probe successes needed to close.

---

## 2. Per-service config recommendations

| Service | failure_threshold | recovery_timeout | Notes |
|---|---|---|---|
| `groq` | 5 | 30s | Free tier — rate limits common. Recovery quick. |
| `anthropic` | 3 | 60s | Paid. Don't burn dollars on a sick service. |
| `perplexity` | 5 | 30s | Same shape as Groq. |
| `telegram` | 10 | 10s | Single-tenant, transient flakes common. |
| `yfinance` | 8 | 60s | Unofficial; 5xx waves happen. |
| `nse` | 5 | 60s | Off-market-hours service is intentionally down. |
| `fred` | 5 | 120s | Slow but reliable. |
| `coingecko` | 10 | 30s | High variability. |
| `tvdatafeed` | 3 | 120s | Unofficial; opens fast, recovers slowly. |

These are starting points. Tune based on actual error rates after Sprint 4
when breakers are wired into real calls.

---

## 3. What counts as a "failure" for the breaker

The breaker treats **any exception** as a failure unless the caller
explicitly catches and decides otherwise.

Recommended pattern:

```python
from orchestration import default_registry, CircuitOpenError

breaker = default_registry.get_or_create("groq", failure_threshold=5)

async def call_groq(prompt):
    try:
        return await breaker.call(lambda: actual_groq_request(prompt))
    except CircuitOpenError:
        # Don't count THIS toward error rate — the call never happened.
        return fallback_response()
```

### What NOT to count as a failure
- 4xx user errors (bad input — fix the caller, don't open the circuit)
- 429 rate-limited (handle with backoff, not breaker)
- HTTP 200 with an error in the body (depends — usually NOT)

### What SHOULD count
- Connection refused / timeout
- 5xx responses (server-side fault)
- Invalid JSON / unexpected schema
- Provider-specific "service unavailable" responses

Sprint 3 implements the breaker primitive. Sprint 4+ wraps each external
call site with a per-service classifier that decides whether to call
`record_failure()`.

---

## 4. Failure classification (cross-reference)

Use `from logging_config import ErrorCategory`:

| Category | Counts toward breaker? | Example |
|---|---|---|
| `EXTERNAL_API` | yes | 5xx from Groq, timeout from yfinance |
| `RATE_LIMIT` | **no** (backoff instead) | 429 from any provider |
| `TIMEOUT` | yes | deadline exceeded |
| `VALIDATION` | no | bad input from our caller |
| `INTERNAL` | no | bug in our code |
| `DATABASE` | usually no | SQLite locked — retry, not breaker |
| `AUTH` | **case-by-case** | "credentials revoked" yes; "session expired" no |
| `CIRCUIT_OPEN` | n/a | the call never reached the service |

A wrapper helper for Sprint 4 (sketch):

```python
async def call_with_breaker(service: str, fn, *,
                            failure_categories=None):
    breaker = default_registry.get_or_create(service)
    failure_categories = failure_categories or {
        ErrorCategory.EXTERNAL_API, ErrorCategory.TIMEOUT,
    }
    if not breaker.can_attempt():
        raise CircuitOpenError(service, breaker._opened_at)
    try:
        result = await fn()
    except BaseException as e:
        if classify(e) in failure_categories:
            await breaker.record_failure()
        raise
    else:
        await breaker.record_success()
        return result
```

This belongs in a Sprint 4 commit, not Sprint 3.

---

## 5. Graceful degradation policy

When a breaker is OPEN, the caller has three choices:

### A. Serve stale (preferred when freshness has a tolerance window)
```python
try:
    fresh = await call_with_breaker("groq", reason_about, ...)
    cache.put("intel:current", fresh, ttl=60)
    return fresh
except CircuitOpenError:
    return cache.get("intel:current")  # last known good
```

### B. Fall back to a simpler model / source
```python
try:
    return await call_with_breaker("groq", complex_reasoning, ...)
except CircuitOpenError:
    return simple_rule_based(input_data)
```

### C. Queue for later (only when freshness doesn't matter)
```python
try:
    return await call_with_breaker("telegram", send_alert, ...)
except CircuitOpenError:
    # Stash for replay when circuit closes
    await event_bus.publish("dlq:telegram:alerts", envelope)
    return None
```

Pattern A is the default for read-heavy paths (intel, regime, narrative).
Pattern B is for the signal path. Pattern C is for non-critical notifications.

---

## 6. Observability

For each breaker, the `snapshot()` returns:

```json
{
  "service": "groq",
  "state": "closed",
  "consecutive_failures": 0,
  "opened_at": 0.0,
  "failure_threshold": 5,
  "recovery_timeout": 30.0
}
```

Sprint 5 wires Prometheus:
- `circuit_open{service="groq"}`: 0 or 1
- `circuit_failures_total{service="groq"}`: counter
- `circuit_state_changes_total{service="groq", from_state, to_state}`: counter

Sprint 4 adds `GET /api/circuits` that returns `default_registry.snapshot()`
for admin inspection.

---

## 7. Anti-patterns

| Don't | Why |
|---|---|
| Use one breaker for "all external calls" | Defeats the purpose. One slow service shouldn't open the circuit for all others. |
| Set `recovery_timeout` to 0 | The breaker never gets a chance to protect downstream — it immediately re-tries. |
| Manually call `record_success` / `record_failure` in business logic | Use `breaker.call(fn)`. The wrapper makes the success/failure path symmetric. |
| Catch `CircuitOpenError` and retry the call | The breaker IS the retry policy. Backoff and serve stale, or fall back. |
| Open the circuit on the FIRST failure (`failure_threshold=1`) | Too sensitive. One slow response will trip you. Default 5 is the sweet spot. |
| Persist circuit state across restarts | We don't. After every restart, every circuit is fresh CLOSED. This is acceptable because state is at most `recovery_timeout` stale. |
| Share one CircuitRegistry across test cases | Each test should use `CircuitRegistry()` directly (not `default_registry`) to avoid cross-test bleed. |

---

## 8. Sprint roadmap for circuit work

| Sprint | Item |
|---|---|
| 3 (current) | Primitive shipped. Not wired into any actual external call. |
| 4 | Wrap `ai_router.chat()`, `news_fetch.fetch_url`, `yfinance` calls, Telegram dispatcher. Per-service thresholds from §2. |
| 4 | Add `GET /api/circuits` health endpoint reading `default_registry.snapshot()`. |
| 5 | Prometheus metrics; alert on `circuit_open == 1` sustained for >10min. |
| 6+ | Persist breaker state in Redis (so all worker processes agree). Needed only when worker count > 1. |

---

## 9. Test coverage

Sprint 3 ships 11 tests for `CircuitBreaker`:

- Starts CLOSED
- Opens after `failure_threshold` consecutive failures
- Success in CLOSED resets the counter
- Transitions to HALF_OPEN after `recovery_timeout`
- HALF_OPEN failure → OPEN
- HALF_OPEN success → CLOSED
- `call()` raises `CircuitOpenError` when OPEN
- `force_open` / `force_close` work
- Registry `get_or_create` returns existing on repeat
- Registry snapshot shape
- Breaker snapshot shape

These don't test integration with real services — that's Sprint 4 work
once breakers are wired into external calls.
