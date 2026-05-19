# AGENT_CONTRACT.md

> Sprint 3 deliverable. The invariants every agent must honor, and the
> recipe for writing one. Companion to `ORCHESTRATION_RUNTIME.md`.

---

## 1. The contract in one paragraph

An agent is an async, ContextVar-aware unit of work that does **one
bounded task per tick**, emits at most a handful of events, and either
succeeds or fails without leaking state. It does not own a thread, does
not call `asyncio.sleep` for routine pacing (the orchestrator paces it),
does not retry forever, does not import the FastAPI app, and does not
modify other agents' state. It is replaceable by a stub for testing.

---

## 2. Required surface

Every agent must:

| What | Where | Why |
|---|---|---|
| Implement `run_once()` (TickAgent) or `handle_event()` (StreamAgent) | subclass method | The only thing the orchestrator drives |
| Set `name`, `family`, `version` | class attrs | Unique identity; appears in logs, metrics, /api/agents |
| Be safe to call `run_once()` concurrently in distinct ticks | discipline | The orchestrator only drives one tick at a time per agent, but the *same agent class* may be instantiated multiple times in tests |
| Not raise from `__init__` | discipline | Registry would leave a half-built agent. Validate config in `on_start` (Sprint 4) instead |
| Not call `asyncio.sleep` in `run_once` (except very brief yields) | discipline | The orchestrator handles pacing. Sleeping inside a tick blocks the timeout/retry machinery |
| Use `self.emit_event()` for ALL outbound events | discipline | Trace fields auto-injected, retry_count auto-set, target stream auto-resolved |

---

## 3. Recommended surface

Override these only if the default isn't enough:

| Method | Default | Override when |
|---|---|---|
| `validate_input(envelope)` | delegates to `self.input_critic` (which defaults to `AlwaysAcceptCritic`) | You want custom validation logic in the agent itself, not as a separate critic |
| `handle_failure(exc, *, stats)` | logs + records | You want to flush a cache, send an admin alert, etc. |
| `on_tick_start(stats)` | no-op | Sprint 5: Prometheus counter increment |
| `on_tick_end(stats)` | no-op | Sprint 5: Prometheus duration histogram |

---

## 4. Configuration knobs (class attrs you can set)

| Attr | Default | Effect |
|---|---|---|
| `tick_interval` | 60.0 (TickAgent) | Seconds between ticks. Stream agents back off to 1.0 when bus is empty. |
| `timeout` | None | Per-tick wall-clock cap. `asyncio.TimeoutError` if exceeded. |
| `retry_policy` | None | If set, each tick wraps `run_once` in `retry_call`. The agent-level counter (`_consecutive_failures`) increments only if all retries are exhausted. |
| `input_critic` | `AlwaysAcceptCritic` | Used by StreamAgent before dispatching to `handle_event`. Reject → ack + log + skip handler. |
| `stream` (StreamAgent only) | `""` | Required. Source stream for inbound events. |
| `consumer_group` (StreamAgent only) | `"default"` | Redis consumer group. Use per-agent name in prod to allow multiple consumers per stream. |

---

## 5. Invariants (broken contract = orchestrator complaint)

1. **`name` is unique per Orchestrator instance.** Re-registering raises.
2. **`run_once` returns None.** Use `self.emit_event()` for outputs.
3. **No `print()`.** Use `self.log.info(...)` — the agent logger is set up in `__init__`.
4. **No mutation of `envelope` in StreamAgent.** Critics are read-only by contract; agents inherit the same discipline. If you need to "transform", emit a new envelope.
5. **No re-emit of the same envelope.** Use `with_retry_incremented` if you need to send it to DLQ or re-stream.
6. **`handle_failure` MUST NOT raise.** It's the last-resort handler. If you must, log and return.
7. **No imports of `dashboard_api` from inside agents.** Keeps the agent layer decoupled from HTTP. Communicate via the bus.
8. **Idempotency keys are caller-set.** The runtime does not dedup automatically.

---

## 6. Failure classification

When `run_once` raises, the orchestrator records:

- `consecutive_failures` += 1
- `total_failures` += 1
- `last_tick_success` = False
- `last_error_type` = exception class name

If `consecutive_failures` hits the orchestrator's threshold (default 5),
the agent transitions to **DISABLED** and the loop exits. Operator
intervention required.

### Categorizing errors

Use `from logging_config import ErrorCategory` in your `run_once`:

```python
try:
    response = await httpx.get(...)
except httpx.TimeoutException:
    self.log.exception("fetch_timeout",
                       extra={"error_category": ErrorCategory.TIMEOUT})
    raise
except httpx.HTTPStatusError as e:
    if e.response.status_code == 429:
        self.log.warning("rate_limited",
                         extra={"error_category": ErrorCategory.RATE_LIMIT})
        raise
    raise
```

Then a RetryPolicy with `retryable_categories={"timeout", "external_api"}`
will retry the first two but not, say, a `ValueError` (unclassified).

---

## 7. The emit/consume protocol

### Producing
```python
envelope = await self.emit_event(
    event_type="news.fetched",
    payload={"headlines": [...], "source": "rss"},
    # stream defaults to "events:<family>:<event_type>"
    target_agent=None,    # optional routing hint
)
```

The returned envelope is fully populated (trace_id, request_id, timestamp,
schema_version, etc.). Tests use it for assertions.

### Consuming
```python
class NewsDedupAgent(StreamAgent):
    name = "news.dedup"
    family = "news"
    stream = "events:news:fetched"
    consumer_group = "news.dedup"

    async def handle_event(self, envelope: EventEnvelope) -> None:
        # envelope.payload has the original publisher's payload (no
        # bus-internal bookkeeping like msg_id — that's stashed as a
        # non-dataclass attribute).
        deduped = [h for h in envelope.payload["headlines"] if not seen(h)]
        await self.emit_event(
            event_type="news.deduped",
            payload={"headlines": deduped, "source": envelope.payload["source"]},
        )
```

---

## 8. Critic placement

Two valid placements:

### A. Input critic on a StreamAgent
```python
agent.input_critic = SchemaCritic(name="news.schema", predicate=...)
```
Runs BEFORE `handle_event`. Rejection → ack + log + skip.

### B. Output critic between two agents (Sprint 4+ pattern)
```python
# Producer emits to a "candidate" stream:
await self.emit_event(event_type="signal.candidate", ...)

# Critic-as-StreamAgent consumes "candidate", emits to "approved":
class SignalCriticAgent(StreamAgent):
    stream = "events:signal:candidate"
    async def handle_event(self, env):
        result = await self.critic.evaluate(env)
        if result.accepted:
            await self.emit_event(event_type="signal.approved",
                                  payload=env.payload)
        else:
            await self.event_bus.publish_to_dlq(
                original_stream=self.stream,
                envelope=env,
                reason=result.reason,
            )
```

Pattern A is sufficient for input validation. Pattern B is for between-stage
gates where the critic deserves its own observability surface.

---

## 9. Anti-patterns

| Don't | Instead |
|---|---|
| `while True: await fetch()` inside `run_once` | Return after one fetch; the orchestrator's tick loop calls you again |
| Catch and ignore exceptions silently | Let them propagate; `tick()` records them. Override `handle_failure` if you have a SPECIFIC recovery |
| Set `tick_interval = 0.01` | The default 60s exists for a reason — be intentional about high-frequency agents |
| Share state across agent instances via class variables | Each tick should be self-contained; use Redis if state must persist |
| Call `asyncio.create_task` from inside `run_once` | The orchestrator should be the only task-spawner. If you need fan-out, emit multiple events |
| Import `dashboard_api` | The agent layer is decoupled from HTTP. Communicate via events |
| Modify `envelope.payload` after consumption | Treat envelopes as immutable. Build new ones for downstream emission |
| `print(...)` for debug | `self.log.info(...)` — appears in the structured JSON stream |

---

## 10. Testing recipe

```python
import pytest
from orchestration import InMemoryEventBus

@pytest.mark.smoke
@pytest.mark.asyncio
async def test_my_agent_emits_expected_event():
    bus = InMemoryEventBus()
    agent = MyAgent()
    agent.event_bus = bus

    # Single tick — sync trigger for tests:
    stats = await agent.tick()
    assert stats.success
    assert stats.events_emitted == 1

    # Inspect emitted event:
    consumed = await bus.try_consume_one(
        stream="events:my_family:my.event",
        group="test_group", consumer="t",
    )
    assert consumed.payload["key"] == "expected_value"
    assert consumed.trace_id  # auto-populated
```

For failure paths:

```python
async def test_my_agent_records_failure_without_raising():
    agent = BrokenAgent()
    stats = await agent.tick()    # should NOT raise
    assert not stats.success
    assert agent._consecutive_failures == 1
```
