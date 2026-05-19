# ORCHESTRATION_RUNTIME.md

> Sprint 3 deliverable. Describes the orchestration foundation shipped
> in `core/orchestration/`. **Nothing in this package runs autonomously
> yet** — Sprint 4 wires it into FastAPI's lifespan.

---

## 0. Package layout

```
core/orchestration/                 # NEW package
├── __init__.py                     # public re-exports
├── event_envelope.py               # EventEnvelope dataclass + factory
├── retry.py                        # bounded retry primitives
├── circuit_breaker.py              # per-service breakers + registry
├── critic.py                       # BaseCritic + Schema/Chain/AlwaysAccept
├── base_agent.py                   # BaseAgent + TickAgent + StreamAgent
├── event_bus.py                    # EventBus ABC + Redis + InMemory impls
└── orchestrator.py                 # registry, lifecycle, health
```

Naming note: this package was originally drafted as `core/agents/` but
renamed during Sprint 3 to avoid a collision with the pre-existing
`core/agents.py` module (which `loader.py` and `pipeline.py` consume).
The rename also matches the doc filenames (ORCHESTRATION_RUNTIME,
not AGENT_RUNTIME).

---

## 1. Public API at a glance

```python
from orchestration import (
    # Wire format
    EventEnvelope, new_envelope,

    # Retry
    RetryPolicy, RetryExhausted, with_retry,

    # Circuit breaker
    CircuitBreaker, CircuitOpenError, CircuitRegistry,
    CircuitState, default_registry,

    # Critic
    BaseCritic, ChainCritic, SchemaCritic, CritiqueResult,

    # Agents
    BaseAgent, TickAgent, StreamAgent,

    # Event bus
    EventBus, InMemoryEventBus, RedisEventBus,
    stream_name, dlq_stream_name,

    # Orchestrator
    Orchestrator, AgentHealth, AgentStatus,
)
```

---

## 2. Minimum usage example (writing your first agent)

```python
from orchestration import (
    InMemoryEventBus, Orchestrator, TickAgent, RetryPolicy,
)


class HelloAgent(TickAgent):
    name = "hello"
    family = "demo"
    tick_interval = 10.0                                # seconds
    retry_policy = RetryPolicy(max_attempts=2,
                               retryable_categories=frozenset({"external_api"}))
    timeout = 5.0                                       # seconds per tick

    async def run_once(self):
        # Do work. Emit results.
        await self.emit_event(
            event_type="hello.tick",
            payload={"greeting": "hi"},
        )


# Sprint 3 is library-only — wiring happens in Sprint 4 (FastAPI lifespan).
# Here's the test-driven shape:
async def demo():
    bus = InMemoryEventBus()
    orch = Orchestrator()

    agent = HelloAgent()
    agent.event_bus = bus
    orch.register(agent)

    # Single tick (sync trigger — Sprint 4 starts the loop):
    stats = await orch.tick_agent("hello")
    assert stats["success"]

    # Or run the loop until you stop it:
    await orch.start_agent("hello")
    # ... do other work ...
    await orch.stop_agent("hello", timeout=10.0)

    # Health for /api/agents:
    for health in orch.health():
        print(health.to_dict())
```

---

## 3. Lifecycle states

```
register()
    │
    ▼
REGISTERED ──── start_agent() ────► RUNNING
                                       │
                                       │ stop_agent()
                                       ▼
                                   STOPPING ───► STOPPED
                                       │
                                       │ max_consecutive_failures hit
                                       ▼
                                   DISABLED  ◄── reset_disabled()
                                       │
                                       └────► REGISTERED (next start)
```

- **REGISTERED**: in the registry; no loop running. `tick_agent(name)` invokes once manually (test/admin).
- **RUNNING**: loop task active. Ticks at `tick_interval`. Reads `stop_event` between ticks.
- **STOPPING**: stop_event set; awaiting current tick to finish (up to graceful timeout).
- **STOPPED**: clean exit. Can be restarted.
- **DISABLED**: `max_consecutive_failures` hit (default 5). Manual `reset_disabled()` required — prevents self-healing loops from masking persistent bugs.

---

## 4. Trace propagation (ContextVars from Sprint 2)

Every tick of `BaseAgent.tick()`:

1. `agent_name_var.set(self.name)`
2. `request_id_var.set(stats.tick_id)` — fresh 12-char hex per tick
3. `trace_id_var.set(uuid4().hex)` — one trace per tick by default

All logs emitted *during* the tick — by the agent itself, by external libraries, by exceptions — automatically carry these fields in the JSON envelope (see Sprint 2 `LOGGING_STANDARD.md`).

Cross-agent trace propagation: a producer agent's `emit_event()` reads the current `trace_id_var` and stamps it onto the envelope. The consumer can call `trace_id_var.set(envelope.trace_id)` at the top of `handle_event` to continue the trace.

---

## 5. What the orchestrator does NOT do (intentional)

| Non-feature | Why |
|---|---|
| Auto-start agents on `register()` | Explicit lifecycle prevents surprise behavior. Sprint 4 starts agents from the FastAPI lifespan. |
| Resurrect DISABLED agents automatically | Operator intervention required. Avoids tight-loop spam if a bug regresses. |
| Cross-process coordination | Single-process only. Sprint 7+ may revisit. |
| LangGraph orchestration | LangGraph is for *reasoning inside* an agent, not for orchestrating between agents. |
| Schedule agents at specific clock times (cron-style) | Tick intervals only. If you need cron, write a TickAgent that checks `time.time()` and no-ops when out of window. |
| Resource limits (memory, CPU) | The asyncio event loop gives cooperative scheduling. Heavy agents should be moved to a separate process (Sprint 7+) if they starve others. |

---

## 6. Error surfaces

| Exception | Raised when | What you should do |
|---|---|---|
| `RetryExhausted` | `with_retry` ran `max_attempts` and the last attempt failed | Catch and decide: DLQ, alert, or accept. `__cause__` has the root exception. |
| `CircuitOpenError` | A call was rejected because the circuit was OPEN | Serve a cached/stale value, queue for later, or fail gracefully. |
| `ValueError` from `register(...)` | Duplicate agent name | Fix the agent's `name` to be unique. |
| `RuntimeError` from `start_agent` | Agent is DISABLED | Call `reset_disabled(name)` first; investigate root cause. |
| Exceptions inside `run_once()` | Subclass bug or external failure | Caught by `tick()` → `handle_failure()` → logged. Counts toward consecutive failure tally. |

---

## 7. Where to extend (recipes)

### A new agent family
1. Subclass `TickAgent` or `StreamAgent`.
2. Set `name`, `family`, `version`.
3. Implement `run_once()` (TickAgent) or `handle_event()` (StreamAgent).
4. Optionally set `retry_policy`, `timeout`, `input_critic`.
5. Register via `orch.register(MyAgent())`.

### A new critic
1. Subclass `BaseCritic`.
2. Implement `async def evaluate(envelope) -> CritiqueResult`.
3. Wire onto an agent: `agent.input_critic = MyCritic()`.
4. Or compose: `ChainCritic(name="signal_chain", critics=[Schema, Confidence, Cooldown])`.

### A new event type
1. Pick `event_type` namespace: `<family>.<verb>` (e.g. `news.fetched`).
2. Use `new_envelope(event_type=..., payload=..., agent_name=...)`.
3. Publish via `await self.emit_event(...)` (uses default stream `events:<family>:<event_type>`).
4. Consumer subscribes by setting `stream` on its `StreamAgent`.

### Wrapping an external service with a circuit breaker
```python
from orchestration import default_registry, CircuitOpenError

breaker = default_registry.get_or_create("groq", failure_threshold=5)

async def call_groq(...):
    return await breaker.call(lambda: actual_groq_call(...))
```

---

## 8. What's tested

The Sprint 3 commit adds **70 tests** across 8 files:

| File | Tests | Covers |
|---|---|---|
| `tests/test_event_envelope.py` | 6 | JSON roundtrip, retry increment, factory, unknown-field tolerance |
| `tests/test_retry.py` | 11 | Policy validation, backoff math, category filtering, decorator |
| `tests/test_circuit_breaker.py` | 11 | All state transitions, registry, snapshot |
| `tests/test_critic.py` | 9 | CritiqueResult, SchemaCritic, ChainCritic halt-on-fail, exception safety |
| `tests/test_event_bus.py` | 9 | InMemory pub/sub/ack, DLQ, backpressure, msg-id isolation |
| `tests/test_base_agent.py` | 9 | TickAgent + StreamAgent lifecycle, retry, timeout, critic dispatch |
| `tests/test_orchestrator.py` | 10 | Register/unregister, start/stop, DISABLED state, health snapshot |
| `tests/test_orchestration_smoke.py` | 2 | End-to-end producer → bus → critic → consumer |

Sprint 1+2 tests (121) still pass. Total: **188 green**.

---

## 9. Status table

| Capability | Sprint 3 status |
|---|---|
| BaseAgent abstraction | ✅ |
| Agent execution contract | ✅ |
| Shared event envelope | ✅ |
| Redis Streams integration | ✅ (Redis + InMemory) |
| Async event bus | ✅ |
| Shared trace propagation | ✅ (via Sprint 2 ContextVars) |
| Critic pattern foundation | ✅ |
| Retry/recovery layer | ✅ |
| Circuit breaker foundation | ✅ |
| Minimal orchestrator runtime | ✅ |
| Autonomous loops | ❌ (deliberately deferred) |
| LangGraph integration | ❌ (deliberately deferred) |
| `/api/agents` health endpoint | ❌ Sprint 4 |
| `/metrics` Prometheus | ❌ Sprint 5 |
| Per-tenant routing | ❌ Sprint 4+ |
