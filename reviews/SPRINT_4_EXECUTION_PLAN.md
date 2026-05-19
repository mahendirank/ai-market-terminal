# SPRINT_4_EXECUTION_PLAN.md — Implementation-Ready

> Sprint 4 builds: NewsFetchAgent, SignalCriticAgent (observe-only),
> RedisEventBus integration into FastAPI lifespan. **Implementation
> details below — file names, signatures, test recipes, acceptance
> criteria, rollback per stage.** No code yet. This is the blueprint
> the next sprint executes.

---

## 0. Premise

Sprint 1–3 are merged into `main` (verified 2026-05-19). Foundation
simulations pass 12/12 (FAILURE_MODE_SIMULATION_REPORT). Rollback paths
empirically validated (ROLLBACK_VALIDATION). Sprint 4 introduces the
first runtime consumer of `core/orchestration/`.

**Hard rule**: Sprint 4 ends with **exactly one** wrapped agent (news
fetch) and **one** observe-only critic. No reasoning chains. No
LangGraph. No multi-agent fan-out beyond the news→critic edge.

---

## 1. Branches + commits

```
main (post Sprint 1–3 merge: 0a1bcf8)
  └─ sprint-4/lifespan-hook              (Stage 4.1)
       └─ sprint-4/admin-endpoints       (Stage 4.2)
            └─ sprint-4/news-fetch-agent (Stage 4.3)
                 └─ sprint-4/signal-critic-observe (Stage 4.4)
                      └─ sprint-4/circuit-wraps   (Stage 4.5)
                           └─ sprint-4/xautoclaim (Stage 4.6)
```

Each stage = one branch, one PR, one merge. Allows independent revert.
Per `gh pr merge --merge` (preserve milestones).

---

## 2. Stage 4.1 — Lifespan hook (off by default)

### Files

**Modify**: `dashboard_api.py` — single insertion inside the existing
`lifespan` async context manager.

**Add**: `orchestration/runtime.py` — small factory + DI helper.

### Spec

```python
# orchestration/runtime.py  (NEW, ~80 LOC)

"""Runtime factory: builds Orchestrator + EventBus per env config.
Separate from orchestrator.py so dashboard_api.py doesn't have to know
about Redis client construction."""

from __future__ import annotations

import logging
import os
from typing import Any

from orchestration.event_bus import EventBus, InMemoryEventBus, RedisEventBus
from orchestration.orchestrator import Orchestrator


_log = logging.getLogger("orchestration.runtime")


async def build_event_bus() -> EventBus:
    """Build the bus per env config. In-memory if REDIS_URL unset
    or AGENT_BUS=memory; Redis otherwise."""
    bus_mode = os.environ.get("AGENT_BUS", "auto").lower()
    if bus_mode == "memory":
        _log.info("event_bus_init", extra={"mode": "memory"})
        return InMemoryEventBus()

    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        _log.warning("event_bus_init_no_redis_url",
                     extra={"mode": "memory_fallback"})
        return InMemoryEventBus()

    import redis.asyncio as aioredis  # lazy import
    client = aioredis.from_url(redis_url, decode_responses=False)
    _log.info("event_bus_init", extra={"mode": "redis", "url": redis_url})
    return RedisEventBus(client)


async def build_orchestrator() -> Orchestrator:
    max_failures = int(os.environ.get("AGENT_MAX_FAILURES", "5"))
    return Orchestrator(max_consecutive_failures=max_failures)


def orchestrator_enabled() -> bool:
    return os.environ.get("AGENT_ORCHESTRATOR_ENABLED", "false").lower() == "true"
```

```python
# dashboard_api.py  (MODIFICATION, ~25 lines added inside lifespan)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start all background tasks when server boots."""
    # ── EXISTING STARTUP CODE — unchanged ──
    saved_hni = _disk_load("hni_v3__market_", HNI_CACHE_TTL)
    # ... [existing code as-is] ...

    # ── SPRINT 4 STAGE 4.1: optional orchestrator ──
    from orchestration.runtime import (
        build_event_bus, build_orchestrator, orchestrator_enabled,
    )

    if orchestrator_enabled():
        try:
            app.state.event_bus = await build_event_bus()
            app.state.orchestrator = await build_orchestrator()
            # NO agents registered in 4.1 — see Stage 4.3
            logging.getLogger("agents.orchestrator").info(
                "orchestrator_lifespan_started",
                extra={"registered_agents": 0},
            )
        except Exception:
            logging.getLogger("agents.orchestrator").exception(
                "orchestrator_lifespan_init_failed"
            )
            # CRITICAL: orchestrator failure must not prevent FastAPI boot
            app.state.orchestrator = None
            app.state.event_bus = None
    else:
        app.state.orchestrator = None
        app.state.event_bus = None

    yield  # app runs here

    # ── SPRINT 4 SHUTDOWN ──
    orch = getattr(app.state, "orchestrator", None)
    if orch is not None:
        try:
            await orch.stop_all(timeout=30.0)
            logging.getLogger("agents.orchestrator").info("orchestrator_lifespan_stopped")
        except Exception:
            logging.getLogger("agents.orchestrator").exception(
                "orchestrator_lifespan_stop_failed"
            )

    # ── EXISTING SHUTDOWN — if any — preserved below ──
```

### Tests

**Add**: `tests/test_sprint4_lifespan.py` — 4 tests:

1. With `AGENT_ORCHESTRATOR_ENABLED=false`, lifespan completes without
   touching `app.state.orchestrator`.
2. With `=true`, `app.state.orchestrator` is a real `Orchestrator`
   instance.
3. With `=true` and Redis-unreachable, lifespan falls back to InMemory
   (logs warning, doesn't crash).
4. Shutdown calls `stop_all(timeout=30)` if orchestrator exists.

### Acceptance criteria

- [ ] `pytest -m smoke` still green (188 → ≥192).
- [ ] Local boot with `AGENT_ORCHESTRATOR_ENABLED=false`: identical to today's behavior.
- [ ] Local boot with `=true`: logs `orchestrator_lifespan_started agents=0`; `/api/health` still 200.
- [ ] Local boot with `=true` and Redis offline: logs `event_bus_init_no_redis_url`; falls back to InMemory; app still boots.

### Rollback

`AGENT_ORCHESTRATOR_ENABLED=false` in `.env`; restart container. ~30s.

---

## 3. Stage 4.2 — Admin endpoints

### Files

**Modify**: `dashboard_api.py` — add 3 routes.

**Add**: `orchestration/admin.py` — endpoint helpers (keeps route bodies thin).

### Spec

```python
# orchestration/admin.py (NEW, ~60 LOC)

async def agents_snapshot(app) -> dict:
    orch = getattr(app.state, "orchestrator", None)
    if orch is None:
        return {"enabled": False, "agents": []}
    return {
        "enabled": True,
        "agents": [h.to_dict() for h in orch.health()],
    }


async def circuits_snapshot() -> dict:
    from orchestration.circuit_breaker import default_registry
    return {"circuits": default_registry.snapshot()}


async def streams_health_snapshot(app) -> dict:
    bus = getattr(app.state, "event_bus", None)
    if bus is None:
        return {"enabled": False, "streams": []}
    # Sprint 4 known set; Sprint 5+ enumerates dynamically.
    KNOWN = [
        "events:news:raw",
        "events:signal:candidate",
        "dlq:news:raw",
        "dlq:signal:candidate",
    ]
    out = []
    for s in KNOWN:
        try:
            length = await bus.stream_length(s)
        except Exception:
            length = -1
        out.append({"stream": s, "length": length})
    return {"streams": out}
```

```python
# dashboard_api.py — add after the existing /api/health route:

@app.get("/api/agents")
async def get_agents():
    from orchestration.admin import agents_snapshot
    return await agents_snapshot(app)

@app.get("/api/circuits")
async def get_circuits():
    from orchestration.admin import circuits_snapshot
    return await circuits_snapshot()

@app.get("/api/streams/health")
async def get_streams_health():
    from orchestration.admin import streams_health_snapshot
    return await streams_health_snapshot(app)
```

### Tests

**Add**: `tests/test_sprint4_admin_endpoints.py` — 4 tests using
`TestClient`:

1. `GET /api/agents` with orchestrator disabled returns `{enabled: false, agents: []}`.
2. `GET /api/agents` with orchestrator + zero agents returns `{enabled: true, agents: []}`.
3. `GET /api/circuits` returns `{circuits: [...]}` (may be empty).
4. `GET /api/streams/health` with InMemory bus returns `{streams: [{length: 0}, ...]}`.

### Acceptance criteria

- [ ] Endpoints respond ≤100ms each.
- [ ] Schema stable (don't break Sprint 5+ consumers).
- [ ] Auth required (inherits the existing AuthMiddleware gate).

### Rollback

Revert this stage's commit. Endpoints disappear; no other effect.

---

## 4. Stage 4.3 — NewsFetchAgent (off by default)

### Files

**Add**: `orchestration/agents/__init__.py` (empty package marker).

**Add**: `orchestration/agents/news_fetch_agent.py` (~120 LOC).

**Modify**: `dashboard_api.py` lifespan — register agent if both flags true.

**Add**: `tests/test_sprint4_news_fetch_agent.py` (~150 LOC).

### Spec

```python
# orchestration/agents/news_fetch_agent.py (NEW, ~120 LOC)

"""Sprint 4 — first wrapped agent. Wraps the existing `news.get_all_news()`
function as a TickAgent. Runs in parallel with the legacy news loop
during the 48h observation window; replaces it after equivalence is
verified (flag `LEGACY_NEWS_LOOP_DISABLED=true`)."""

from __future__ import annotations

import asyncio
import logging
import os

from orchestration import TickAgent, RetryPolicy
from logging_config import ErrorCategory


class NewsFetchAgent(TickAgent):
    name = "news.fetch"
    family = "news"
    version = "v1"

    # 60s matches the legacy loop's cadence. During observation,
    # consider raising to 120s to halve external API rate while we
    # run BOTH paths.
    tick_interval = float(os.environ.get("NEWS_FETCH_TICK_INTERVAL", "60"))
    timeout = float(os.environ.get("NEWS_FETCH_TIMEOUT", "30"))

    retry_policy = RetryPolicy(
        max_attempts=3,
        base_delay=2.0,
        max_delay=10.0,
        retryable_categories=frozenset({
            ErrorCategory.EXTERNAL_API,
            ErrorCategory.TIMEOUT,
        }),
    )

    async def run_once(self) -> None:
        # Lazy import: don't pull news.py into module-import time so
        # this agent module stays cheap to test.
        from news import get_all_news

        # The legacy function is SYNC. Run in a thread to avoid blocking
        # the event loop. asyncio.to_thread is the canonical bridge.
        raw_news = await asyncio.to_thread(get_all_news)

        # Convert NamedTuple-shaped legacy items to plain dicts for
        # JSON serialization.
        headlines = [
            {"title": n.title, "url": n.url, "source": n.source,
             "ts": getattr(n, "ts", None)}
            for n in raw_news
        ]

        await self.emit_event(
            event_type="news.raw",
            payload={
                "headlines": headlines,
                "count": len(headlines),
                "source": "legacy_get_all_news",
            },
        )
```

```python
# dashboard_api.py — extend Stage 4.1's `if orchestrator_enabled():` block

if orchestrator_enabled():
    # ... event_bus + orchestrator built as in Stage 4.1 ...

    # Stage 4.3 — register NewsFetchAgent if enabled
    if os.environ.get("AGENT_NEWS_FETCH_ENABLED", "false").lower() == "true":
        from orchestration.agents.news_fetch_agent import NewsFetchAgent
        agent = NewsFetchAgent()
        agent.event_bus = app.state.event_bus
        app.state.orchestrator.register(agent)
        await app.state.orchestrator.start_agent(agent.name)
        logging.getLogger("agents.orchestrator").info(
            "agent_registered_and_started", extra={"agent": agent.name}
        )
```

### Tests

`tests/test_sprint4_news_fetch_agent.py`:

1. Agent's `run_once` calls `get_all_news` exactly once.
2. Emits exactly one `news.raw` event with the expected payload shape.
3. `asyncio.to_thread` is used (not blocking the event loop) — verified
   by monkeypatching `get_all_news` to sleep, asserting concurrent work
   makes progress.
4. Retry policy fires on `ExternalAPIError`; fails fast on `ValueError`.
5. Timeout cancels a slow `run_once`; agent records failure.
6. With `AGENT_NEWS_FETCH_ENABLED=false`, the agent is NOT registered.
7. With `=true`, agent is registered AND started.

### Observation protocol (mandatory before Stage 4.7 cutover)

Run for ≥48h with BOTH:
- Legacy news.py loop active (existing code, unchanged)
- NewsFetchAgent active (Sprint 4)

Collect from logs:
- Count of legacy "[NEWS]" emissions per hour
- Count of `news.raw` events emitted per hour

Acceptance: rates match ±5%, no agent failures, no breaker openings.

### Acceptance criteria

- [ ] Agent emits at expected cadence (60s default).
- [ ] `/api/agents` shows `news.fetch` as RUNNING with `total_ticks > 0`.
- [ ] No new ERROR log lines.
- [ ] Bus has events accumulating (visible in `/api/streams/health`).
- [ ] After 48h: legacy + agent emission rates match ±5%.

### Rollback

`AGENT_NEWS_FETCH_ENABLED=false`; restart container. Legacy loop
unaffected. Agent stops cleanly via `stop_all(timeout=30)`.

---

## 5. Stage 4.4 — SignalCriticAgent (OBSERVE-ONLY mode)

### Files

**Add**: `orchestration/agents/signal_critic_agent.py` (~150 LOC).

**Modify**: `dashboard_api.py` lifespan — register if flag.

**Add**: `tests/test_sprint4_signal_critic.py`.

### Spec

```python
# orchestration/agents/signal_critic_agent.py (NEW, ~150 LOC)

"""Sprint 4 — observe-only critic for signal candidates.

Consumes events:signal:candidate, evaluates them with deterministic
critics, and EMITS A LOG LINE — does NOT reject or DLQ. Sprint 5+ flips
to enforce mode after observe period passes.

Note: in Sprint 4, there is NO producer of events:signal:candidate yet
(decision agents come in Sprint 5+). This agent is wired but idle. The
purpose of wiring it now is to:
  - Establish the topology (consumer group exists in Redis)
  - Validate the critic chain on synthetic events during testing
  - Have the metrics surface ready before Sprint 5's producer
"""

from __future__ import annotations

import logging

from orchestration import StreamAgent, SchemaCritic, ChainCritic
from orchestration.critic import CritiqueResult, BaseCritic
from orchestration.event_envelope import EventEnvelope


def _schema_predicate(payload: dict) -> tuple[bool, str | None]:
    if not isinstance(payload, dict):
        return False, "payload_not_dict"
    if "asset" not in payload:
        return False, "missing_asset"
    if "confidence" not in payload:
        return False, "missing_confidence"
    if not isinstance(payload["confidence"], (int, float)):
        return False, "confidence_wrong_type"
    return True, None


class _ConfidenceFloorCritic(BaseCritic):
    """Reject signals where confidence < 50 — likely noise."""
    name = "signal.confidence_floor"
    FLOOR = 50

    async def evaluate(self, envelope: EventEnvelope) -> CritiqueResult:
        c = envelope.payload.get("confidence", 0)
        if c >= self.FLOOR:
            return CritiqueResult.accept(reason="above_floor", confidence=1.0)
        return CritiqueResult.reject(
            reason="below_confidence_floor",
            confidence=1.0,
            detail=f"got {c}, need >= {self.FLOOR}",
        )


class _RecentBarCritic(BaseCritic):
    """Reject signals based on stale data (>5 min old)."""
    name = "signal.recent_bar"
    STALE_THRESHOLD_S = 300

    async def evaluate(self, envelope: EventEnvelope) -> CritiqueResult:
        # Use envelope.timestamp; richer recency checks belong in Sprint 5.
        return CritiqueResult.accept(reason="recency_check_passthrough")


class SignalCriticAgent(StreamAgent):
    """OBSERVE-ONLY in Sprint 4. Sprint 5 sets SIGNAL_CRITIC_ENFORCE=true."""

    name = "signal.critic"
    family = "signal"
    version = "v1"
    stream = "events:signal:candidate"
    consumer_group = "signal.critic.observe"

    # No input_critic — we don't reject based on inputs. We
    # explicitly evaluate inside handle_event so we can log the verdict
    # without acting on it.

    def __init__(self):
        super().__init__()
        self._chain = ChainCritic(name="signal.chain", critics=[
            SchemaCritic(name="signal.schema", predicate=_schema_predicate),
            _ConfidenceFloorCritic(),
            _RecentBarCritic(),
        ])

    async def handle_event(self, envelope: EventEnvelope) -> None:
        verdict = await self._chain.evaluate(envelope)
        self.log.info(
            "signal_critic_observed",
            extra={
                "trace_id": envelope.trace_id,
                "verdict": "accept" if verdict.accepted else "reject",
                "reason": verdict.reason,
                "confidence": verdict.confidence,
                "asset": envelope.payload.get("asset"),
                "envelope_confidence": envelope.payload.get("confidence"),
            },
        )
        # OBSERVE MODE: do nothing else. No emit. No DLQ.
```

### Tests

`tests/test_sprint4_signal_critic.py`:

1. Schema critic rejects payload missing `asset`.
2. Confidence floor critic rejects payload with confidence=10.
3. Chain accepts well-formed payload with confidence=85.
4. Observe mode: handle_event logs but does NOT emit.
5. Critic doesn't raise on garbage input (predicate exception → reject cleanly).

### Acceptance criteria

- [ ] Agent registered, RUNNING in `/api/agents`.
- [ ] In Sprint 4, no producer feeds it → 0 ticks consumed (idle).
- [ ] When fed synthetic events (test), emits log line with verdict.

### Rollback

Same as 4.3 — env-var flag off.

---

## 6. Stage 4.5 — Circuit-wrap external calls

### Files

**Add**: `orchestration/circuit_wrap.py` (~80 LOC).

**Modify**: `ai_router.py` (~5 lines, wrap chat()).

**Modify**: `notify.py` (~5 lines, wrap send_telegram()).

### Spec

```python
# orchestration/circuit_wrap.py (NEW)

from __future__ import annotations
import logging
from typing import Awaitable, Callable, TypeVar

from orchestration.circuit_breaker import CircuitBreaker, CircuitOpenError, default_registry
from logging_config import ErrorCategory

T = TypeVar("T")
_log = logging.getLogger("orchestration.circuit_wrap")


# Default category set that counts toward breaker — does NOT include
# RATE_LIMIT (use backoff) or VALIDATION (caller bug, not service health).
DEFAULT_FAILURE_CATEGORIES = frozenset({
    ErrorCategory.EXTERNAL_API,
    ErrorCategory.TIMEOUT,
})


def classify_http_exception(exc: BaseException) -> str:
    """Heuristic. Sprint 4 starts with HTTPX exceptions; extend as needed."""
    name = type(exc).__name__
    if "Timeout" in name:
        return ErrorCategory.TIMEOUT
    if "RateLimit" in name or "429" in str(exc):
        return ErrorCategory.RATE_LIMIT
    if "Connection" in name or "HTTPStatusError" in name:
        return ErrorCategory.EXTERNAL_API
    return ErrorCategory.INTERNAL


async def with_circuit(
    service: str,
    fn: Callable[[], Awaitable[T]],
    *,
    failure_categories: frozenset[str] = DEFAULT_FAILURE_CATEGORIES,
    classify: Callable[[BaseException], str] = classify_http_exception,
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0,
) -> T:
    breaker = default_registry.get_or_create(
        service,
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
    )
    if not breaker.can_attempt():
        raise CircuitOpenError(service, breaker._opened_at)
    try:
        result = await fn()
    except BaseException as e:
        cat = classify(e)
        if cat in failure_categories:
            await breaker.record_failure()
        raise
    else:
        await breaker.record_success()
        return result
```

```python
# ai_router.py — sketch of where to wrap

from orchestration.circuit_wrap import with_circuit

async def chat(*, task, messages, **kwargs):
    """Existing function. Wrap each provider call with a circuit."""
    # ... existing routing logic to decide provider ...
    if provider == "groq":
        return await with_circuit("groq", lambda: _call_groq(messages, **kwargs))
    elif provider == "anthropic":
        return await with_circuit(
            "anthropic",
            lambda: _call_anthropic(messages, **kwargs),
            failure_threshold=3, recovery_timeout=60.0,  # tighter for paid
        )
    # ... existing fallback ...
```

### Tests

`tests/test_sprint4_circuit_wraps.py`:

1. with_circuit successful call doesn't record failure
2. with_circuit raises CircuitOpenError when circuit is open
3. Failure category filter: ValidationError doesn't trip the breaker
4. classify_http_exception correctly buckets timeout/429/connection errors

### Acceptance criteria

- [ ] After Sprint 4 deploy, `/api/circuits` shows `groq` and `telegram` after first calls.
- [ ] Force-open `groq` via admin → next chat() call gets CircuitOpenError → graceful degradation kicks in (cached intel returned).
- [ ] No new failures in `ai_router.chat()` from the wrapping itself.

### Rollback

Revert the two modified files (ai_router.py, notify.py). `circuit_wrap.py`
remains as dead code; harmless.

---

## 7. Stage 4.6 — XAUTOCLAIM sweep

### Files

**Modify**: `orchestration/event_bus.py` — add `reclaim_stale_pending` to RedisEventBus and (no-op) InMemoryEventBus.

**Modify**: `orchestration/orchestrator.py` — add `reclaim_loop()` method called from `_run_loop`.

### Spec

```python
# orchestration/event_bus.py — append to RedisEventBus

async def reclaim_stale_pending(
    self,
    *,
    stream: str,
    group: str,
    consumer: str,
    min_idle_ms: int = 60_000,
    max_count: int = 50,
) -> int:
    """Reclaim events that have been pending for > min_idle_ms.

    Returns count reclaimed. Suitable for periodic invocation by the
    Orchestrator (every 60s, say). Caller's `consumer` name should be
    the *current* consumer name — events get re-assigned to it.
    """
    try:
        # redis-py's xautoclaim returns (next_cursor, claimed_messages)
        result = await self._r.xautoclaim(
            name=stream,
            groupname=group,
            consumername=consumer,
            min_idle_time=min_idle_ms,
            count=max_count,
        )
        claimed = result[1] if result else []
        return len(claimed)
    except Exception as e:
        _log.warning("xautoclaim_failed", extra={"stream": stream, "err": repr(e)})
        return 0

# orchestration/event_bus.py — append to InMemoryEventBus

async def reclaim_stale_pending(self, **kwargs) -> int:
    """No-op for InMemoryEventBus — no crash-recovery semantics in-memory."""
    return 0
```

### Tests

`tests/test_sprint4_xautoclaim.py`:

1. InMemoryEventBus.reclaim_stale_pending returns 0 always.
2. RedisEventBus.reclaim_stale_pending (with mock) returns count of reclaimed events.
3. reclaim_stale_pending swallows exceptions and logs warning.

### Acceptance criteria

- [ ] No regression in existing event bus tests.
- [ ] When tested against real Redis with a stuck pending event, reclaim count is >0.

### Rollback

Revert the change. Stuck events would need manual `XCLAIM` to recover —
acceptable for Sprint 4 because we have ONE consumer (no risk of stuck
events from crash recovery).

---

## 8. Sprint 4 timing + sequencing

| Stage | Effort | Calendar (estimated) |
|---|---|---|
| 4.1 lifespan hook | 1 day | Day 1 |
| 4.2 admin endpoints | 0.5 day | Day 1 |
| 4.3 NewsFetchAgent | 1 day code + 48h observation | Days 2–4 |
| 4.4 SignalCriticAgent (observe) | 1 day | Day 5 |
| 4.5 circuit wraps | 1 day | Day 5–6 |
| 4.6 XAUTOCLAIM | 0.5 day | Day 6 |
| Cutover (legacy news loop disable) | 0.5 day | Day 7 |
| **Total** | ~6 working days + observation | ~1 week |

---

## 9. Sprint 4 exit criteria

To declare Sprint 4 done and start Sprint 5:

- [ ] All 6 stages merged to `main` via separate PRs.
- [ ] `AGENT_ORCHESTRATOR_ENABLED=true` on prod for ≥72h.
- [ ] `AGENT_NEWS_FETCH_ENABLED=true` on prod for ≥48h.
- [ ] Equivalence: NewsFetchAgent emission count matches legacy ±5%.
- [ ] `LEGACY_NEWS_LOOP_DISABLED=true` for ≥24h with no regression.
- [ ] `/api/agents` shows `news.fetch` RUNNING, no failures in 24h.
- [ ] `/api/circuits` shows `groq` + `telegram` as CLOSED (or briefly OPEN with recovery).
- [ ] Test count ≥210 (currently 188 + ~25 expected new tests).
- [ ] Zero ERROR-level logs from `agents.*` over 24h.

---

## 10. Sprint 4 hard "don'ts"

| Don't | Why |
|---|---|
| Don't add a second agent type until news.fetch has 48h of clean observation | Reduces blast radius and confounds equivalence comparison |
| Don't implement LangGraph | Reasoning hasn't shown a need; defer per `MULTI_AGENT_PLAN §4.2` |
| Don't migrate any `print()` calls — even in news.py | Phase B is Sprint 5+ work; wrapping the call ≠ rewriting the module |
| Don't bind `/metrics` endpoint publicly | Sprint 5; localhost-only binding |
| Don't flip critic to enforce mode | Sprint 4 is observe-only; Sprint 5 takes the call after analysis |
| Don't refactor `dashboard_api.py` route split | TECH_DEBT §2 — Sprint 6+ |
| Don't auto-restart DISABLED agents | Manual `reset_disabled` is the contract |

---

## 11. Sprint 4 → Sprint 5 handoff

After Sprint 4 ends:
- Foundation is stable and exercised
- ONE real agent runs in production
- Pattern is proven — replicate for ingest agents (news.dedup, news.classify, etc.)
- Sprint 5 adds 2–3 more news agents + flip critic to enforce + first Prometheus metrics

Sprint 5's plan document gets written at the end of Sprint 4 (with
observed metrics + lessons-learned).
