# FAILURE_MODE_SIMULATION_REPORT.md

> Empirical results from running 12 simulation scripts that exercise the
> failure modes documented in `FAILURE_MODE_ANALYSIS.md`. 50 scenarios
> total. All pass. Captured 2026-05-19.

---

## Summary

| Simulation | Scenarios | Verdict | Key result |
|---|---|---|---|
| `sim_logging_load` | 1 (compound) | ✅ PASS | 200 concurrent requests, **648 req/s**, 200/200 unique IDs, no ContextVar leak |
| `sim_streams_recovery` | 6 | ✅ PASS | Pub/sub/ack roundtrip, pending-on-no-ack, backpressure, DLQ, non-blocking empty, consumer exclusivity |
| `sim_retry` | 6 | ✅ PASS | Transient recovery, exhaustion, category fast-fail, monotonic backoff, callback hook, decorator |
| `sim_circuit_breaker` | 7 | ✅ PASS | All state transitions, no-dial rejection, registry singleton |
| `sim_redis_disconnect` | 5 | ✅ PASS | publish/consume/ack surface ConnectionError; BUSYGROUP swallowed; xlen degrades to -1 |
| `sim_retry_storm` | 1 (compound) | ✅ PASS | 50 concurrent retry chains × 3 attempts = 150 ops, bounded wall-clock |
| `sim_failed_consumer` | 4 | ✅ PASS | Handler raise → still acks; no reprocess; orchestrator DISABLES after threshold; groups isolated |
| `sim_timeout_cascade` | 3 | ✅ PASS | Timeout honored; no cascade to other agents; event loop stays responsive |
| `sim_malformed_events` | 5 | ✅ PASS | JSON garbage raises; missing required field raises; unknown fields ignored; critic rejects cleanly |
| `sim_duplicate_events` | 4 | ✅ PASS | Double publish → 2 deliveries (no auto-dedup); idempotency_key roundtrips; consumer-side dedup pattern works |
| `sim_trace_propagation` | 4 | ✅ PASS | trace_id stamps onto envelope; consumer can propagate via ContextVar; logs carry trace; fresh trace minted when unset |
| `sim_graceful_degradation` | 4 | ✅ PASS | Serve-stale, fall-back-to-rules, queue-for-later, recovery-after-timeout |

**Totals**: 12 simulations, ~50 scenarios, 0 failures.

Raw output for each sim: `reviews/sim-results/<sim_name>.txt`.

---

## Detailed findings

### 1. Logging under load (`sim_logging_load`)

| Metric | Value |
|---|---|
| Concurrent requests | 200 |
| Total wall-clock | 308.4 ms |
| Throughput | **648 req/s** |
| Unique request_id in handler | 200/200 |
| Unique X-Request-ID in response | 200/200 |
| Caller-supplied IDs preserved | 100/100 |
| `request_complete` log lines | 200/200 |
| Latency min..max (per-request) | 1.28 ms .. 13.77 ms |

**Headline**: ContextVar isolation is correct at 200-way concurrency. Sprint 2 middleware is **not** a throughput bottleneck — production load is ~10 req/s; this gives 65× headroom.

**Residual risk**: this used TestClient (sync, in-process). Real uvicorn behind Caddy may differ. Sprint 4's VPS deploy is the real test.

### 2. Redis Streams recovery (`sim_streams_recovery`)

6 scenarios cover the `EventBus` contract:
- Publish → consume → ack roundtrip, payload not polluted by `_bus_msg_id`
- Consume without ack leaves event in pending
- Backpressure (max_len=5): publish 10 → 5 survivors are events 5..9 (oldest dropped)
- DLQ stream: `events:news:fetched` → `dlq:news:fetched` with `_dlq_reason` tag
- Empty stream `try_consume_one` returns None in <1ms (no blocking)
- Within one group, each event delivered to exactly ONE consumer

**Residual risk**: tests use `InMemoryEventBus`, not real Redis. Sprint 4 must repeat with `RedisEventBus` against a live Redis container.

### 3. Retry primitives (`sim_retry`)

- Transient recovery: 2 fails → 3rd succeeds, 32.3ms total
- Persistent failure: `RetryExhausted(3 tries)` with `__cause__` set to original
- Category fast-fail: non-retryable category aborts on attempt 1 (no delay)
- Backoff math: delays `[0.000, 0.050, 0.100, 0.200, 0.400]s` — monotonic, jitter off
- on_attempt callback fires 3× for 3 failed attempts
- Decorator preserves `__wrapped__`

**Residual risk**: none for retry primitives themselves. Real-world retry storms also pass `sim_retry_storm` (50 concurrent chains).

### 4. Circuit breaker (`sim_circuit_breaker`)

7 scenarios covering the full state machine:
- CLOSED → OPEN after N=3 failures
- OPEN → HALF_OPEN after recovery_timeout
- HALF_OPEN success → CLOSED
- HALF_OPEN failure → OPEN (with refreshed opened_at)
- `call()` raises `CircuitOpenError` when OPEN — no socket dialed
- Registry returns same breaker on repeat lookup
- Force open / close

**Residual risk**: in-process breaker state is lost on restart. After Sprint 7+, when agents may run in separate containers, breaker state needs Redis persistence — already documented in `CIRCUIT_BREAKER_PLAN.md §7`.

### 5. Redis disconnect (`sim_redis_disconnect`)

| Operation | When Redis is dead | Recommended caller response |
|---|---|---|
| `publish` | raises `ConnectionError` | breaker.record_failure + DLQ + retry |
| `try_consume_one` | raises `ConnectionError` | breaker.record_failure + back off |
| `ensure_group` | raises `ConnectionError` (unless `BUSYGROUP` — swallowed) | retry at startup |
| `stream_length` | returns `-1` (sentinel) | health endpoint reports unknown |

**Headline**: the bus is intentionally "dumb" — it surfaces errors and lets higher layers (circuit breaker, retry policy) decide.

**Residual risk**: a caller that doesn't wrap `publish`/`consume` in a breaker is exposed. Sprint 4's first agent (NewsFetchAgent) MUST use a circuit breaker around its `emit_event` if Redis stability is questionable.

### 6. Retry storm (`sim_retry_storm`)

50 agents × 3 max_attempts = 150 operations executed concurrently. Wall-clock dominated by jitter, not pile-up. Each chain reports `RetryExhausted(3)` independently. No deadlock.

**Residual risk**: a real retry storm hitting a downstream service that's already failing creates a thundering herd. Mitigation: per-service circuit breaker (which Sprint 4 wires) opens FAST and prevents further calls during the herd window.

### 7. Failed consumer (`sim_failed_consumer`)

- Handler raises → event still acked (finally-block guarantee). No reprocess.
- After 3 consecutive failures, orchestrator marks agent DISABLED. Loop exits cleanly.
- Other agents in different groups are unaffected by one agent's failure.

**Residual risk**: with default behavior, events that hit handler bugs are LOST (acked + logged, but the work didn't happen). Agents that need durability MUST opt into `publish_to_dlq` on handler failure.

### 8. Timeout cascade (`sim_timeout_cascade`)

- Agent with `timeout=0.05s` is cancelled within ~60ms (50ms timeout + 10ms slack).
- Concurrent fast agent finishes in normal time — no cascade.
- During the slow agent's timeout window, a background asyncio task fires 5 times — event loop stays responsive.

**Residual risk**: a `timeout=None` agent that genuinely hangs will block its own loop indefinitely. Convention: every agent should set `timeout`. Sprint 4's NewsFetchAgent will set `timeout=30s`.

### 9. Malformed events (`sim_malformed_events`)

- `EventEnvelope.from_json("garbage")` raises `ValueError` clearly
- `from_dict({})` raises `TypeError` (missing required field)
- `from_dict(...with_unknown_field...)` silently ignores future fields → forward-compat
- `SchemaCritic` rejects invalid payloads with structured reason
- Predicate that raises returns `CritiqueResult(accepted=False, reason="critic_internal_error", confidence=0.0)`

**Residual risk**: a consumer that decodes JSON then doesn't validate before processing is vulnerable. Convention: ALL StreamAgents must have an `input_critic` (default `AlwaysAcceptCritic` is explicit opt-out).

### 10. Duplicate events (`sim_duplicate_events`)

- Publishing same envelope 2× → 2 deliveries. No automatic dedup.
- `idempotency_key` field roundtrips through JSON correctly.
- Consumer-side dedup using a `set()` (or Redis SET in prod) correctly deduplicates.
- When `idempotency_key` is None, dedup is opted out (treats each as unique).

**Headline**: Sprint 3 deliberately does not implement automatic dedup. Each consumer decides. Sprint 4+: consumers where double-processing is expensive (Telegram dispatch, signal emission) opt in.

**Residual risk**: a consumer that needs idempotency but doesn't implement it WILL double-process on retry. This is documented in `AGENT_CONTRACT.md §5`.

### 11. Trace propagation (`sim_trace_propagation`)

- Producer's `tick()` sets `trace_id_var` → `emit_event()` stamps it onto envelope
- Consumer reads `envelope.trace_id` and sets ContextVar → logs during handler carry the trace
- Log line emitted inside handler shows the propagated trace_id
- When ContextVar is unset, `new_envelope()` mints a fresh UUID

**Headline**: end-to-end trace propagation works **today**, before OpenTelemetry. OTel (Sprint 4+ optional) becomes a drop-in via `trace_id_var`.

### 12. Graceful degradation (`sim_graceful_degradation`)

Three canonical degradation patterns + recovery:
- **Pattern A (serve stale)**: circuit OPEN → caller returns cached intel
- **Pattern B (fall back to rules)**: circuit OPEN → caller uses simple rule logic
- **Pattern C (queue for later)**: circuit OPEN → caller adds to retry queue
- **Recovery**: after recovery_timeout + successful probe, circuit returns to CLOSED automatically

These patterns are documented in `CIRCUIT_BREAKER_PLAN.md §5` and now verified runnable.

---

## Gaps NOT covered by these simulations

Honest accounting of what the simulations don't exercise:

| Gap | Why not covered | When |
|---|---|---|
| Real Redis (not InMemory) | Requires `docker-compose up redis` — environmental | Sprint 4 first agent rollout |
| Real external API rate limits | Mocks can't replicate | Sprint 4 with real Groq/Telegram |
| Long-running agent memory leaks | Simulations run <1s each | Sprint 5 with `memray` or `tracemalloc` |
| Cross-process state (e.g. breaker shared across containers) | Single-process simulation | Sprint 7+ |
| WebSocket-specific concurrency | Middleware doesn't touch WS | Sprint 4+ when WS agents land |
| Multi-tenant ContextVar isolation | All sims are single-tenant | Sprint 4 with `tenant_id_var` populated |

These gaps move from "not tested" to "tested" incrementally. Each will get its own sim in the relevant sprint.

---

## How to re-run

```bash
cd ~/ai-system/core
for sim in scripts/sim/sim_*.py; do
  python3 "$sim"
done
```

Each script returns exit code 0 on PASS, 1 on FAIL. Suitable for CI inclusion in a future sprint (gate on simulation outcomes for production-deployment-blocking failures).
