# ORCHESTRATION_FLOW.md

> Sequence diagrams for the data paths through the Sprint-3 orchestration
> primitives. Each shows: happy path → critic-reject path → retry path →
> circuit-open path → DLQ path.

---

## 1. Happy path: producer → bus → critic → consumer → ack

```mermaid
sequenceDiagram
    autonumber
    participant P as ProducerAgent.tick()
    participant E as new_envelope()
    participant B as EventBus
    participant C as ConsumerAgent.tick()
    participant CR as input_critic
    participant H as handle_event()

    P->>E: build envelope<br/>(reads trace_id_var, request_id_var)
    P->>B: publish(stream, envelope)<br/>XADD MAXLEN ~5000
    B-->>P: msg_id

    Note over P,B: ── async gap ──

    C->>B: try_consume_one(stream, group, consumer)
    B-->>C: envelope<br/>(envelope._bus_msg_id = msg_id)
    C->>CR: evaluate(envelope)
    CR-->>C: CritiqueResult(accepted=True)
    C->>H: handle_event(envelope)
    H-->>C: None
    C->>B: ack(stream, group, envelope)
    B-->>C: ok
```

**Invariants**:
- Envelope is unchanged from producer to handler (read-only contract).
- Ack runs after handler, in a finally block — even handler exceptions
  result in an ack (no reprocess). Failures are handled via DLQ, not
  re-delivery.
- `_bus_msg_id` is a non-dataclass attribute — clean `payload`.

---

## 2. Critic-reject path: handler is NOT called

```mermaid
sequenceDiagram
    autonumber
    participant C as ConsumerAgent.tick()
    participant B as EventBus
    participant CR as input_critic
    participant L as logger
    participant H as handle_event

    C->>B: try_consume_one
    B-->>C: envelope
    C->>CR: evaluate(envelope)
    CR-->>C: CritiqueResult(accepted=False, reason="schema_invalid")
    C->>L: log "input_rejected_by_critic"<br/>{reason, event_type, trace_id}
    C->>B: ack(stream, group, envelope)
    Note over C,H: handle_event NEVER called
```

**Why ack on reject**: rejection is a durable decision. Reprocessing
would just reject again. The audit trail is in the structured log.

---

## 3. Retry path inside one tick (transient external failure)

```mermaid
sequenceDiagram
    autonumber
    participant T as BaseAgent.tick()
    participant RT as retry_call(RetryPolicy)
    participant RO as run_once()
    participant EXT as external API

    T->>RT: attempt 1
    RT->>RO: run_once()
    RO->>EXT: fetch
    EXT-->>RO: ConnectionError
    RO-->>RT: raise

    Note over RT: policy.delay_for(2) = 1.0s × jitter
    RT-->>RT: await asyncio.sleep(1.0)

    RT->>RO: attempt 2
    RO->>EXT: fetch
    EXT-->>RO: ConnectionError
    RO-->>RT: raise

    Note over RT: policy.delay_for(3) = 2.0s × jitter
    RT-->>RT: await asyncio.sleep(2.0)

    RT->>RO: attempt 3
    RO->>EXT: fetch
    EXT-->>RO: ok
    RO-->>RT: result
    RT-->>T: result

    T->>T: consecutive_failures = 0<br/>(success after retry counts as success)
```

**Notes**:
- Retries are bounded by `RetryPolicy.max_attempts` (hard cap 20).
- If the exception's classification is NOT in `retryable_categories`,
  the policy fails fast on attempt 1 — no retry.
- Time spent retrying counts against `timeout` (if set). Pick `timeout
  > sum(delay_for(1..max_attempts))` or you'll abort mid-retry.

---

## 4. Retry exhausted → DLQ

```mermaid
sequenceDiagram
    autonumber
    participant H as handle_event
    participant RT as retry_call
    participant W as do_work()
    participant B as EventBus

    H->>RT: attempt 1
    RT->>W: do work
    W-->>RT: raise (transient)
    RT->>RT: await delay
    RT->>W: attempt 2
    W-->>RT: raise (still failing)
    RT->>RT: await delay
    RT->>W: attempt 3
    W-->>RT: raise (still failing)
    RT-->>H: RetryExhausted

    H->>B: publish_to_dlq(<br/>  original_stream=self.stream,<br/>  envelope=envelope,<br/>  reason=f"retry_exhausted:{e.__cause__!r}")
    B-->>H: dlq_msg_id
    Note over H,B: Original ack still happens<br/>via the StreamAgent.tick wrapper.
```

**DLQ semantics**:
- Same envelope, with `payload._dlq_reason` and `payload._dlq_original_stream` added.
- Lands in `dlq:<family>:<event_type>` parallel stream.
- No automatic replay — human inspects, fixes, manually re-publishes.

---

## 5. Circuit breaker path: open circuit short-circuits the call

```mermaid
sequenceDiagram
    autonumber
    participant A as agent.run_once
    participant CB as CircuitBreaker("groq")
    participant EXT as Groq API

    Note over CB: state = OPEN<br/>(opened 5s ago, recovery_timeout=30s)

    A->>CB: call(lambda: groq_complete(...))
    CB->>CB: _maybe_half_open()<br/>(too soon; still OPEN)
    CB-->>A: raise CircuitOpenError

    Note over A: Caller decides:<br/>A. serve stale<br/>B. fall back<br/>C. queue for later

    A->>A: cache.get("intel:current")<br/>(graceful degradation)
```

---

## 6. Circuit breaker recovery path

```mermaid
stateDiagram-v2
    [*] --> CLOSED
    CLOSED --> CLOSED: success<br/>consecutive_failures = 0
    CLOSED --> CLOSED: failure (count < threshold)<br/>consecutive_failures++

    CLOSED --> OPEN: failure (count = threshold)<br/>opened_at = now

    OPEN --> OPEN: any call → CircuitOpenError
    OPEN --> HALF_OPEN: now - opened_at >= recovery_timeout<br/>(lazy, on next can_attempt())

    HALF_OPEN --> CLOSED: probe success<br/>(half_open_success_threshold met)
    HALF_OPEN --> OPEN: probe failure<br/>opened_at = now

    note right of OPEN
        Calls rejected immediately
        Cheap; no socket dialed
    end note

    note right of HALF_OPEN
        ONE probe call allowed
        through. Used to verify
        service is healthy again.
    end note
```

**Per-service defaults** (per `CIRCUIT_BREAKER_PLAN.md`):

| Service | threshold | recovery_timeout |
|---|---|---|
| groq | 5 | 30s |
| anthropic | 3 | 60s |
| yfinance | 8 | 60s |
| telegram | 10 | 10s |
| nse | 5 | 60s |

---

## 7. Backpressure on a full stream (MAXLEN cap)

```mermaid
sequenceDiagram
    autonumber
    participant P as ProducerAgent
    participant B as EventBus
    participant Q as Stream (max_len=5000)

    Note over Q: count = 4999

    P->>B: publish (event #5000)
    B->>Q: XADD MAXLEN ~5000
    Q-->>B: ok

    Note over Q: count = 5000

    P->>B: publish (event #5001)
    B->>Q: XADD MAXLEN ~5000
    Q-->>Q: drop oldest (#1)
    Q-->>B: ok

    Note over Q: count = 5000 (oldest gone)
```

**Implication**: a slow consumer can lose events if the producer's rate
× backlog time > 5000. Mitigation: raise `max_len`, add a dedicated
consumer, or split the stream by partition. Sprint 5 adds Prometheus
metrics for backlog so you can detect this *before* it's an outage.

---

## 8. Orchestrator's bounded loop (per agent)

```mermaid
flowchart TD
    START([orchestrator.start_agent]) --> CHECK1{stop_event.is_set?}
    CHECK1 -- yes --> EXIT([status = STOPPED])
    CHECK1 -- no --> TICK[agent.tick]
    TICK --> CHECK2{consecutive_failures<br/>>= max?}
    CHECK2 -- yes --> DISABLED([status = DISABLED])
    CHECK2 -- no --> WAIT{StreamAgent +<br/>empty bus?}
    WAIT -- yes --> SLEEP1[wait 1.0s<br/>or stop_event]
    WAIT -- no --> SLEEP2[wait tick_interval<br/>or stop_event]
    SLEEP1 --> WOKE1{woken by stop_event?}
    SLEEP2 --> WOKE2{woken by stop_event?}
    WOKE1 -- yes --> EXIT
    WOKE1 -- no --> TICK
    WOKE2 -- yes --> EXIT
    WOKE2 -- no --> TICK

    style DISABLED fill:#ffd6d6
    style EXIT fill:#d6f0d6
```

**Two ways to exit cleanly**:
1. **Caller-requested** (`stop_agent` sets stop_event) — graceful, current
   tick finishes first.
2. **Self-disabled** (consecutive failures hit threshold) — fail-safe,
   needs operator `reset_disabled` to restart.

Never an infinite loop: every iteration awaits either a fixed interval
or `stop_event.wait()`. Both are interruptible by cancellation.

---

## 9. End-to-end smoke (the test that validates the whole chain)

```mermaid
sequenceDiagram
    autonumber
    participant T as test_end_to_end_flow
    participant P as ProducerAgent (TickAgent)
    participant B as InMemoryEventBus
    participant C as ConsumerAgent (StreamAgent)
    participant CR as SchemaCritic
    participant H as handle_event

    T->>P: orch.tick_agent("producer") × 4
    P->>P: emit 4 events alternating valid/invalid

    Note over B: 4 events queued

    loop 4 times
        T->>C: orch.tick_agent("consumer")
        C->>B: try_consume_one
        B-->>C: envelope
        C->>CR: evaluate
        alt valid envelope (has "asset")
            CR-->>C: accepted
            C->>H: handle_event
            H->>H: append to received
            C->>B: ack
        else invalid (no "asset")
            CR-->>C: rejected
            C->>B: ack
        end
    end

    Note over T: assert received == 2<br/>assert stream_length == 0<br/>assert pending == []
```

This is `tests/test_orchestration_smoke.py::test_end_to_end_flow` —
the canonical "did Sprint 3 build the right thing" verification.
