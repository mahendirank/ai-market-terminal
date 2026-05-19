# FAILURE_MODE_ANALYSIS.md

> For every component shipped in Sprint 1–3: what can fail, how it's
> detected, what recovers automatically, and what residual risk
> remains. Read this before Sprint 4 starts producing real events.

Severity legend:
- **S1**: data loss or silent corruption
- **S2**: outage or feature unavailable
- **S3**: degraded performance or visibility loss
- **S4**: cosmetic or operationally annoying

Each row: `Component | Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity`

---

## Sprint 1: Safety net

### Tests + CI

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| CI runner is down | `gh run list` shows no progress | None | Wait or self-host runner | Deploy blocked until CI is back; not a runtime risk | S3 |
| pytest hangs on an asyncio test | CI 10-min timeout per job | Job times out | Inspect logs; fix or add `@pytest.mark.timeout` | Slow developer iteration | S4 |
| Ruff false-positive on legacy code | CI lint job reports F541 etc. | `continue-on-error: true` masks it | Tune ruff config in Sprint 4–5 | Visual noise in CI; no runtime impact | S4 |
| Dependency added without pin → reproducibility broken | `pip freeze` diff in `pin-deps.sh` | None | Pin the package | Future image rebuilds may regress silently | S3 |

### `engine.py` deletion + `__main__` guards

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| Hidden importer of `engine.py` we missed | `test_module_imports` would have caught it | n/a | Restore from git history if needed | We verified zero imports via grep; very low | S2 if forgotten |
| `__main__` guard removed in a future PR | `test_module_imports` will fail when re-running run.py / claude_bridge.py | Self-disable; loop exits | Re-add guard | Low — test catches | S2 |

---

## Sprint 2: Phase A logging

### `setup_logging()`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| Called before any module that calls `logging.getLogger()` — handler attached to root, fine | Test: `test_setup_logging_is_idempotent` | n/a | n/a | None | — |
| Called AFTER a module has already set its own handlers | `disable_existing_loggers=False` preserves them, but root may double-log | None | Audit module-level handler setup | 8 modules use `getLogger(__name__)` — none attach handlers themselves; safe | S4 |
| `dictConfig` raises (typo, invalid level) | Process startup crash | None | Read traceback, fix env var or code | Caught at startup, not in flight | S2 |
| Two threads call `setup_logging` concurrently before sentinel set | Race window: tens of microseconds | Idempotency sentinel; second call no-ops | n/a | First-call wins; second's args ignored | S4 |

### `JsonFormatter`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| Log record contains an unserializable object in `extra=` | `_safe_json_value` returns `repr(v)` | Automatic | n/a | Slightly noisier log; never blocks | S4 |
| `record.exc_info` contains a non-standard object | `formatException()` falls back | Automatic | Inspect, fix logger call | Very rare | S4 |
| Final `json.dumps(envelope)` raises | Fallback envelope emitted with `failed to serialize` message | Automatic | Investigate the offending logger | Never silently swallowed | S3 |
| Log volume exceeds disk capacity | Docker journal-driver retains; host disk fills | None today | Configure rotation (OBSERVABILITY §3) | **Real risk** until rotation block applied | S2 |

### `RequestContextMiddleware`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| ContextVar leaks between requests | `test_request_id_resets_between_requests` | Token reset in finally | n/a | Reset is in `finally`; even raises don't leak | S2 if regressed |
| Caller injects malformed X-Request-ID | `_extract_or_generate_id` falls through to UUID on UnicodeDecodeError | Automatic | n/a | None | — |
| Middleware itself raises (bug in our code) | Request returns 500; FastAPI exception handler engages | App stays up | Inspect log, fix | Bug-class; tests cover ASGI roundtrip | S2 |
| Per-request log emission saturates stdout | Docker `json-file` buffers | Possible event loop pause if writes block | Disable via `LOG_HTTP_REQUESTS=false` | Latency hiccup at very high QPS only | S3 |

---

## Sprint 3: Orchestration foundation

### `EventEnvelope`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| Producer with new schema_version, consumer doesn't recognize | Consumer's `from_dict` filters unknown fields → silently downgrades | Automatic forward-compat | Bump consumer to new schema_version | Lossy if producer added a field consumer needs; conscious tradeoff | S3 |
| Payload contains unserializable object | `to_json` raises `TypeError` | None — caller's bug | Pre-serialize before constructing envelope | Caller's responsibility to keep payload JSON-safe | S2 |
| `new_envelope` called with no ContextVar set | Defaults to random UUID + "-" tenant | Automatic | n/a | Traces won't link to a request | S4 |

### `retry.py`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| Operation always fails (no transient cause) | `RetryExhausted` raised after `max_attempts` | Bounded; never infinite | Surface upstream or DLQ | Caller may not handle `RetryExhausted` → propagates | S2 |
| `on_attempt` callback raises | `_log.warning` records it; retry continues | Automatic | Fix callback | None — callback is decoupled | S4 |
| Caller passes `max_attempts=21` | `ValueError` at construction | n/a | Fix the constant | Caught at module load | S3 |
| Backoff jitter generates inverted timing (very unlikely with uniform(1-0.1, 1+0.1)) | n/a | Bounded by `max_delay` | n/a | None | — |
| Total retry time exceeds caller's deadline | Caller's own timeout fires; retry abandoned | Depends on caller | Use `agent.timeout` to bound | Caller must size timeout > sum(delays) | S3 |

### `circuit_breaker.py`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| Service truly down — breaker opens correctly | `CircuitOpenError` raised by `call()` | After `recovery_timeout`, HALF_OPEN allows probe | Inspect upstream, adjust if expected | Graceful — degraded mode in caller | S2 (intended) |
| Flaky service oscillates open/closed | Logs `circuit_breaker_transition` repeatedly | None | Tune `failure_threshold` or `recovery_timeout` higher | Monitor `circuit_state_changes_total` (Sprint 5) | S3 |
| All probes during HALF_OPEN succeed but real load fails | Edge case — probe rate too low | n/a | Increase `half_open_success_threshold` so closing is harder | Hard to detect without traffic | S3 |
| Two callers race to open simultaneously | `_lock` serializes; one trips, other observes already-open | Automatic | n/a | None | — |
| Process restarts → all breakers fresh CLOSED | At most `recovery_timeout` of state lost | Acceptable today | Sprint 6+: persist state in Redis | Short window of un-gated calls after restart | S3 |

### `critic.py`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| Critic's predicate raises | `SchemaCritic.evaluate` catches, returns `critic_internal_error` | Automatic; reject with confidence=0 | Fix predicate; check `_log.exception` | None — critic never crashes the pipeline | S3 |
| `ChainCritic` with empty list | `ValueError` at construction | n/a | Add at least one critic | Caught at module load | S3 |
| Critic is stateful (against the contract) | Concurrent reads see racy state | None — discipline only | Refactor critic to be stateless | Real risk if violated; not runtime-enforced | S2 if violated |
| LLM-backed critic (Sprint 4+) times out | Not yet implemented; timeout will be agent-level | n/a (deferred) | Sprint 4 wires LLM critics with per-call timeout | Future risk | S3 |

### `base_agent.py`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| `run_once` raises | `tick()` records, logs via `handle_failure`, swallows | Automatic | Inspect log; consecutive_failures may hit threshold | None — swallowed; counted | S3 |
| `run_once` exceeds `timeout` | `asyncio.TimeoutError` → handled same as raise | Automatic | Investigate why slow | tick() never blocks longer than timeout | S3 |
| `handle_failure` raises (against contract) | Propagates to `tick()` finally block; ContextVars still reset | Token reset uses `finally` | Fix handle_failure | Will mark tick as failed; not catastrophic | S3 |
| Many ticks of empty `run_once` (no work) | StreamAgent backs off to 1.0s; TickAgent honors interval | Automatic | Tune `tick_interval` if needed | None | S4 |
| Subclass forgets to set `name` | Class-level default `"agent"` collides with another instance | `Orchestrator.register` raises on duplicate | Set unique name | Caught at register time | S3 |
| `emit_event` called with no `event_bus` | Logs warning, returns envelope without publishing | Automatic; safe | Wire event_bus or remove emit call | Caller may think event landed | S3 |

### `event_bus.py`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| Redis down — publish fails | `xadd` raises `ConnectionError` | None — propagates to caller | Caller's circuit breaker handles | Producer agent's tick logs failure | S2 |
| Stream MAXLEN reached, slow consumer | Old events evicted silently | Inevitable | Raise `max_len` or add consumers | **Real data loss risk** if rate >> consumer | S2 |
| Consumer crashes between read and ack | Event stays pending until XAUTOCLAIM (Sprint 4+) | None today | Manual XPENDING / XCLAIM | Today: event stuck in pending until manual intervention | S2 |
| `try_consume_one` returns malformed JSON | `EventEnvelope.from_json` raises | None — propagates | Inspect; if recurrent, schema bug | Rare; tests cover roundtrip | S3 |
| `_bus_msg_id` attribute lost in transit | Ack is no-op (returns silently) | n/a — but event reprocessed on next read | Audit consumer code that loses attribute | Possible double-process if envelope is cloned | S3 |
| Group created at HEAD (not 0) → misses backlog | XGROUP CREATE id="0" in `ensure_group` | Automatic | n/a | Tests cover | S3 |

### `orchestrator.py`

| Failure | Detection | Auto-recovery | Operator action | Residual risk | Severity |
|---|---|---|---|---|---|
| Agent loops grows hot (tick_interval=0) | CPU spike visible in `docker stats` | None — discipline | Tune interval | Don't set 0 except in tests | S3 |
| Orchestrator's `stop_all` doesn't return | `asyncio.wait_for(timeout=10)` cancels tasks | Cancellation suppressed | Inspect why hung | Bounded shutdown | S3 |
| `register` called twice with same name | `ValueError` | n/a | Use unique name | Caught at register time | S3 |
| Agent's `tick` never returns (deadlock in run_once) | `agent.timeout` cancels it; `handle_failure` records | Tick-level recovery | Investigate run_once | If `timeout=None` AND deadlocked, the agent task hangs | S2 if timeout unset |
| Process crashes mid-loop | Loop dies with process | None — Docker restart policy resurrects container | Verify container restart policy | All breaker state, in-memory bus state lost | S2 |

---

## Cross-cutting risks

| Risk | Severity | Mitigation today | Improvement |
|---|---|---|---|
| Disk fills from JSON logs | S2 | None | Sprint 4 rollout: apply docker `logging` block from OBSERVABILITY §3 |
| Redis OOM → stream eviction | S2 | 256MB cap + `allkeys-lru` + bounded streams (~30MB total at planned rates) | Monitor eviction in Sprint 5; raise if needed |
| Single process death loses all in-flight state | S2 | Docker `restart: unless-stopped` | Sprint 7+ multi-process makes this irrelevant |
| `prod = staging` — no safety net for misconfigs | S1–S2 | Sprint 5+ stand up staging | Until then: env-var rollback is the safety net |
| ContextVar leak across asyncio.create_task boundary | S3 | `copy_context()` copies values; spawned tasks inherit | Spawned tasks should explicitly `set()` if they want fresh IDs |
| Agent that holds an OS-level lock and crashes | S2 | We don't use OS locks; SQLite WAL handles per-connection state | Don't introduce file locks in agents |
| Schema_version increments without consumer update | S2 | `from_dict` ignores unknown fields | Producer schema bumps require coordinated consumer update |

---

## Untested failure modes (residual risk)

These are NOT exercised by automated tests in Sprint 3:

- Redis cluster split-brain (we use single-instance; not applicable)
- Actual prod-scale concurrent load (Sprint 5 will load-test)
- Long-lived agent leaks (no memory profile yet)
- Cross-tenant data leak via ContextVar bug (need multi-tenant test scenarios)

These move from "untested" to "tested" as Sprint 4–5 progress.
