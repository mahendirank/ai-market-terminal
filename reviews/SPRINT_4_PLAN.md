# SPRINT_4_PLAN.md — Implementation Plan (Review Only)

> Sprint 4 scope: take the orchestration foundation built in Sprint 3
> from "library" to "running in production with ONE wrapped agent and
> ONE observe-mode critic". **NO new reasoning agents.** **NO LangGraph.**
>
> This document is for **review and approval**. No code has been written.

---

## 0. Sprint 4 framed in one paragraph

Wire the Sprint-3 `Orchestrator` into FastAPI's lifespan **off by default**
behind a feature flag. Expose two read-only admin endpoints
(`/api/agents`, `/api/circuits`) so we can SEE the orchestrator state
even with no agents registered. Then, in three feature-flagged steps,
wrap ONE existing background loop (recommend `news.py`'s fetch) as a
`TickAgent`, run it in parallel with the legacy loop, verify
equivalence via correlated logs, and cut over by flipping the flag.
Add ONE `SchemaCritic` in **observe mode only** (logs verdict, does
not block). Wrap external calls with circuit breakers. Sprint 5 is
where we start consuming critic verdicts and where LangGraph enters,
not Sprint 4.

---

## 1. What Sprint 4 will deliver

| # | Deliverable | LOC est. (impl) | LOC (tests) | Touches |
|---|---|---|---|---|
| 1 | FastAPI lifespan hook — register orchestrator, start/stop | ~50 | ~80 | `dashboard_api.py`, new `orchestration/runtime.py` |
| 2 | `/api/agents` + `/api/circuits` + `/api/streams/health` endpoints | ~60 | ~80 | `dashboard_api.py` |
| 3 | `NewsFetchAgent` (TickAgent wrapping `news.py:get_all_news`) | ~80 | ~120 | new `orchestration/agents/news_fetch_agent.py` |
| 4 | `SignalCriticAgent` in observe mode (StreamAgent) | ~70 | ~100 | new `orchestration/agents/signal_critic_agent.py` |
| 5 | `circuit_wrap.py` — helper to wrap external calls with per-service breakers | ~50 | ~60 | new |
| 6 | Wrap `ai_router.chat()` + `notify.py:send_telegram()` with circuits | ~30 | ~40 | edits to two existing files |
| 7 | XAUTOCLAIM sweep in RedisEventBus for stuck pending events | ~60 | ~80 | edit `event_bus.py` |
| 8 | Docs: SPRINT_4_OUTCOMES.md, SPRINT_4_ROLLBACK.md, AGENT_OBSERVE_MODE.md | ~400 | n/a | new |
| | **Total** | **~400** | **~560** | |

Substantially smaller than Sprint 3 (1,532 LOC). Most of the work is
configuration and wiring; the heavy primitives already exist.

---

## 2. Stages — exact sequence, each independently revertible

### Stage 4.1 — Lifespan hook (off by default)

**Goal**: orchestrator is constructed and disposed correctly during
app startup/shutdown. ZERO agents registered yet.

```python
# dashboard_api.py (added inside lifespan)

@asynccontextmanager
async def lifespan(app):
    # ... existing startup ...

    # Sprint 4.1 — orchestrator wiring, OFF by default
    if os.environ.get("AGENT_ORCHESTRATOR_ENABLED", "false").lower() == "true":
        from orchestration import Orchestrator, RedisEventBus
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        app.state.event_bus = RedisEventBus(redis_client)
        app.state.orchestrator = Orchestrator()
        log.info("orchestrator_lifespan_started", extra={"agents": 0})

    yield

    # Sprint 4.1 — shutdown
    orch = getattr(app.state, "orchestrator", None)
    if orch is not None:
        await orch.stop_all(timeout=30)
        log.info("orchestrator_lifespan_stopped")
```

**Verification**:
- With `AGENT_ORCHESTRATOR_ENABLED=false` (default): app boots identically to today. No new logs.
- With `AGENT_ORCHESTRATOR_ENABLED=true`: app boots, logs `orchestrator_lifespan_started agents=0`, no agents running.

**Rollback**: set flag false; restart container. ~30s.

### Stage 4.2 — Read-only admin endpoints

**Goal**: SEE the orchestrator from outside the process.

```python
@app.get("/api/agents")
def get_agents():
    orch = getattr(app.state, "orchestrator", None)
    if orch is None:
        return {"enabled": False, "agents": []}
    return {"enabled": True, "agents": [h.to_dict() for h in orch.health()]}

@app.get("/api/circuits")
def get_circuits():
    from orchestration import default_registry
    return {"circuits": default_registry.snapshot()}

@app.get("/api/streams/health")
async def streams_health():
    bus = getattr(app.state, "event_bus", None)
    if bus is None:
        return {"enabled": False, "streams": []}
    # Bus implementations expose stream_length but not "list all streams";
    # this endpoint enumerates from a hard-coded known-streams list (Sprint 4 starting set).
    KNOWN_STREAMS = ["events:news:raw", "dlq:news:raw"]
    out = []
    for s in KNOWN_STREAMS:
        out.append({"stream": s, "length": await bus.stream_length(s)})
    return {"streams": out}
```

**Auth**: same as existing `/api/*` — session cookie required.

**Verification**: With flag false, returns empty/disabled. With flag
true and no agents, returns empty arrays. After Stage 4.3 wraps an
agent, the endpoint shows it.

### Stage 4.3 — `NewsFetchAgent` (off by default, parallel to legacy)

**Goal**: ONE wrapped background loop. Sprint 1's tests already cover
that `news.py:get_all_news()` exists; here we wrap it.

```python
# orchestration/agents/news_fetch_agent.py
from orchestration import TickAgent, RetryPolicy
from news import get_all_news  # existing module
from logging_config import ErrorCategory

class NewsFetchAgent(TickAgent):
    name = "news.fetch"
    family = "news"
    version = "1"
    tick_interval = 60.0  # match the legacy loop's cadence
    timeout = 30.0
    retry_policy = RetryPolicy(
        max_attempts=3,
        base_delay=2.0,
        retryable_categories=frozenset({ErrorCategory.EXTERNAL_API, ErrorCategory.TIMEOUT}),
    )

    async def run_once(self):
        # Wrap the existing sync function — Sprint 4 doesn't refactor
        # news.py; that's Phase B of LOGGING_STANDARD.
        raw_news = await asyncio.to_thread(get_all_news)
        await self.emit_event(
            event_type="news.raw",
            payload={"headlines": [n._asdict() for n in raw_news], "source": "rss"},
        )
```

Registered behind a SECOND flag in `dashboard_api.py`:
```python
if os.environ.get("AGENT_NEWS_FETCH_ENABLED", "false").lower() == "true":
    from orchestration.agents.news_fetch_agent import NewsFetchAgent
    agent = NewsFetchAgent()
    agent.event_bus = app.state.event_bus
    app.state.orchestrator.register(agent)
    await app.state.orchestrator.start_agent(agent.name)
```

**Verification protocol (must run for ≥48h before flipping the legacy
loop off)**:
1. Both the legacy `news.py` loop AND `NewsFetchAgent` run.
2. Correlate via timestamps + log envelope.
3. Compare emitted events vs legacy DB writes for ≥48h.
4. If equivalent, flip a third flag `LEGACY_NEWS_LOOP_DISABLED=true`
   to silence the old loop.
5. Old loop deletion happens in a FOLLOW-UP PR after another 48h.

**Rollback**: `AGENT_NEWS_FETCH_ENABLED=false` → restart. Legacy loop
unaffected. ~30s recovery.

### Stage 4.4 — `SignalCriticAgent` (observe mode only)

**Goal**: a StreamAgent that consumes `events:signal:candidate` and
emits a critique log line but does NOT block, reject, or DLQ.

```python
class SignalCriticAgent(StreamAgent):
    name = "signal.critic"
    family = "signal"
    stream = "events:signal:candidate"
    consumer_group = "signal.critic.observe"

    async def handle_event(self, envelope):
        verdict = await self._evaluate(envelope)  # deterministic checks
        self.log.info(
            "signal_critic_observed",
            extra={
                "trace_id": envelope.trace_id,
                "verdict": "accept" if verdict.accepted else "reject",
                "reason": verdict.reason,
                "confidence": verdict.confidence,
            },
        )
        # OBSERVE MODE: do nothing else. Sprint 5 flips this to enforce
        # mode where rejected → DLQ.
```

After 1 week of observe-mode operation:
- Compute reject rate per `reason` from the logs.
- Verify no obviously-good signals get reject verdicts.
- If clean, Sprint 5 flips `SIGNAL_CRITIC_ENFORCE=true` and the agent
  starts DLQ-routing rejections.

**Note**: There's NO `events:signal:candidate` producer in Sprint 4
yet. This critic is a no-op until a real producer exists (Sprint 5+).
Wiring it now establishes the topology + log surface; bringing up the
producer is a separate decision.

### Stage 4.5 — Circuit-wrap external calls

**Goal**: every outbound HTTP call to Groq, Anthropic, Telegram, yfinance
goes through a per-service circuit breaker.

```python
# orchestration/circuit_wrap.py
from logging_config import ErrorCategory
from orchestration import CircuitBreaker, CircuitOpenError, default_registry

async def with_circuit(
    service: str,
    fn,
    *,
    failure_categories=None,
    classify=None,
):
    breaker = default_registry.get_or_create(service)
    if not breaker.can_attempt():
        raise CircuitOpenError(service, breaker._opened_at)
    try:
        result = await fn()
    except BaseException as e:
        category = classify(e) if classify else None
        if (failure_categories is None
                or category in failure_categories):
            await breaker.record_failure()
        raise
    else:
        await breaker.record_success()
        return result
```

Wired into `ai_router.chat()` and `notify.py:send_telegram()`:
```python
# ai_router.py — sketch
async def chat(...):
    return await with_circuit("groq", lambda: _groq_complete(...))
```

**Verification**: `/api/circuits` shows `groq` and `telegram` after first
call. Force-open the `groq` breaker → next call returns `CircuitOpenError`
without dialing.

### Stage 4.6 — XAUTOCLAIM sweep in RedisEventBus

**Goal**: when a consumer crashes between consume and ack, the pending
event gets reclaimed by another consumer after a grace period.

```python
# event_bus.py — added method on RedisEventBus
async def reclaim_stale_pending(
    self,
    *,
    stream: str,
    group: str,
    consumer: str,
    min_idle_ms: int = 60_000,
    max_count: int = 50,
) -> int:
    """Claim events that have been pending longer than min_idle_ms.
    Called periodically by Orchestrator (Sprint 4+) — NOT by individual agents.
    Returns count of reclaimed events.
    """
    result = await self._r.xautoclaim(
        name=stream, groupname=group, consumername=consumer,
        min_idle_time=min_idle_ms, count=max_count,
    )
    return len(result[1]) if result else 0
```

Orchestrator gets a new method `reclaim_loop()` that runs every 60s
and calls `reclaim_stale_pending` for each registered StreamAgent's
(stream, group, consumer) tuple.

**InMemoryEventBus**: implements the same method but as a no-op
returning 0 — there's no crash recovery semantics in tests.

---

## 3. What Sprint 4 will NOT do

| Excluded | Why |
|---|---|
| `MacroReasoningAgent` / LangGraph | Reasoning chains haven't shown a latency complaint yet; defer to Sprint 5 |
| `NewsDedupAgent`, `NewsClassifyAgent`, etc. | Each is its own observation cycle. Sprint 5 adds them one at a time |
| Migrate ANY of the 328 `print()` calls (Phase B of LOGGING_STANDARD) | Out of scope — touch the agent layer, not legacy modules, unless wrapping forces a change |
| `dashboard_api.py` route split | TECH_DEBT §2 — Sprint 6+ |
| Postgres migration | Sprint 4+ workload won't pressure SQLite |
| Prometheus / Grafana | Sprint 5 |
| Multi-tenant agent isolation | No paying client demands it today |
| Per-agent processes (scale-out) | Single-process suffices |
| `LEGACY_NEWS_LOOP_DISABLED=true` (the cutover) | Only flip after observation period — happens at the END of Sprint 4 |

---

## 4. Risk register for Sprint 4

| # | Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|---|
| R1 | NewsFetchAgent emits to bus but legacy code path doesn't, so two sources of truth | high | S3 | Observation period — both paths must produce equivalent state for 48h before legacy is disabled |
| R2 | `asyncio.to_thread(get_all_news)` blocks event loop if the sync call is long | medium | S2 | Wrap with `agent.timeout=30`; CI smoke test ensures we honor it |
| R3 | Circuit breaker false-positive: opens on a transient that shouldn't have | medium | S3 | Per-service thresholds tuned conservatively (CIRCUIT_BREAKER_PLAN §2); operator can `force_close` via admin endpoint |
| R4 | `/api/agents` endpoint exposes internals to an unauthorized request | low | S2 | Bound by existing AuthMiddleware; ensure endpoint is under the auth-gated set |
| R5 | XAUTOCLAIM sweeps reclaim a legitimately-running consumer's events (false stale) | low | S3 | Conservative `min_idle_ms=60s`; per-consumer `consumer` name (uuid would be better — Sprint 5) |
| R6 | Observation period reveals subtle drift, but rollout is already half-done | medium | S2 | Each stage is feature-flagged off; rolling back doesn't touch other stages |
| R7 | Orchestrator startup blocks the FastAPI lifespan if Redis is slow | low | S2 | Lifespan opens connection lazily; first publish failure is bounded by breaker |
| R8 | Two parallel news loops double the API rate to external sources | medium | S3 | The legacy loop can be temporarily slowed; or run NewsFetchAgent with `tick_interval=120` for the observation window |

---

## 5. Exit criteria for Sprint 4

To consider Sprint 4 "done" and graduate to Sprint 5:

- [ ] All 5 stages merged (4.1 → 4.6) with feature flags appropriately set
- [ ] `AGENT_ORCHESTRATOR_ENABLED=true` running on prod for ≥72 hours
- [ ] `AGENT_NEWS_FETCH_ENABLED=true` running for ≥48 hours
- [ ] Equivalence verification: NewsFetchAgent emissions count matches legacy DB writes ±5%
- [ ] `LEGACY_NEWS_LOOP_DISABLED=true` flipped; system runs for ≥24h with only the agent producing
- [ ] `/api/agents` shows news.fetch as RUNNING, ≥0 failures in 24h window
- [ ] `/api/circuits` shows groq + telegram as CLOSED (or briefly OPEN with auto-recovery)
- [ ] Test count grows from 188 to ≥210 (mostly agent + endpoint tests)
- [ ] Zero new ERROR-level logs from `agents.*` loggers in 24h

If any of these miss, the unblockers go into Sprint 4.5 (a mini-followup
sprint) before Sprint 5 starts.

---

## 6. Sprint 5 preview (so you can see where this is going)

Sprint 5 will:
1. Add 2–3 more news family agents (`NewsDedup`, `NewsClassify`, `NewsSummarize`).
2. Build `events:signal:candidate` producer (`DecisionAgent` migration).
3. Flip `SignalCriticAgent` from observe → enforce.
4. Add Prometheus `/metrics` endpoint.
5. First LangGraph: `MacroReasoningAgent` if needed (only if `ai_router.chat()` shows complex chains hitting latency).

Sprint 6+ is intel cluster, UI broadcast agents, risk family.

---

## 7. Decisions needed before Sprint 4 starts

Same 5 open questions from `MULTI_AGENT_PLAN.md §10`:

1. **Orchestrator location** — proposal: new `orchestration/runtime.py` rather than baking into `dashboard_api.py`. Confirm?
2. **Per-tenant agent isolation** — assume **no** until a paying client asks. Confirm?
3. **Critics-as-LLMs** — Sprint 4 ships only deterministic critics. Sprint 5 considers LLM critics IF false-positive rate is high. Confirm?
4. **`/metrics` endpoint binding** — `127.0.0.1` only? Confirm?
5. **Redis HA** — accept single-instance. Confirm?

Plus 3 new Sprint-4-specific decisions:
6. **News loop cadence during dual-run** — keep agent at 60s like legacy, OR slow to 120s to halve external API load during the observation window?
7. **News fetch agent name** — `news.fetch` (canonical) or include version (`news.fetch.v1`) for explicit migration tracking?
8. **Order of stages** — proposed 4.1 → 4.6 above. Any reason to reorder?

---

## 8. Reading order for review

Read in this order to evaluate the plan:

1. This file (§0–§6) — what + why
2. `MULTI_AGENT_PLAN.md` (Stage 2 in §7) — context for why Sprint 4 is structured this way
3. `FAILURE_MODE_ANALYSIS.md` — what can go wrong in the primitives we're now using
4. `PRODUCTION_READINESS.md` — gate criteria
5. `ROLLOUT_CHECKLIST.md` — how to land Sprint 1–3 first (prereq)
6. `CIRCUIT_BREAKER_PLAN.md` §2, §4 — the threshold tuning that Sprint 4 will use

After reading: answer the 8 questions in §7, approve the stage order,
then Sprint 4 starts.
