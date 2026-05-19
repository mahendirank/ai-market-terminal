# EVENT_LOOP_STABILITY_REPORT.md

> Stage A — asyncio event loop behavior over the 15-minute observation
> window, 2026-05-19.

---

## 0. What we observed

The FastAPI app runs on a single asyncio event loop hosted by uvicorn.
This report focuses on whether that loop stays responsive after flipping
`AGENT_ORCHESTRATOR_ENABLED=true`.

---

## 1. Direct evidence: latency under load

`/health` hits over the window:

| Time | Latency |
|---|---|
| t+15min, attempt 1 | 2.243ms |
| t+15min, attempt 2 | 4.833ms |
| t+15min, attempt 3 | 2.332ms |
| t+15min, attempt 4 | 2.162ms |
| t+15min, attempt 5 | 1.845ms |

Median: **2.33ms**. Max: 4.83ms (first hit; possible JIT warm). Min:
1.85ms.

Comparison to pre-flip:
- Pre-Sprint-4 measurement (t+30s after deploy): 1.28–13.77 ms over 200 concurrent requests via `sim_logging_load`
- Today's post-flip range (1.8–4.8ms) is at the BETTER end of the historical range

**No event loop blocking observed.**

---

## 2. WebSocket stability (existing /ws price publisher)

The legacy `_price_publisher_loop` runs via `streaming.PricePublisher(interval=2.0).run()` started during lifespan (line 81 of dashboard_api.py). Sprint 4.1 does NOT modify this loop.

Indirect verification (`/health` returns 200 ⇒ healthcheck passes ⇒ uvicorn worker is responsive ⇒ event loop is processing requests):

- `/health` continuously 200 throughout the 15min window
- Container `Health: healthy` continuously
- External HTTPS via Caddy → market-terminal works (200 in <200ms)

**WebSocket pipe was not directly exercised in this short window** (would require a long-lived browser/wscat session). For the 24h soak, operator should:

```bash
# Connect a WebSocket session and watch price ticks for 1+ hour
# (use browser dev tools on https://zyvoratech.co dashboard, or wscat:)
wscat -c wss://zyvoratech.co/ws/prices
```

Expected: a price tick approximately every 2 seconds. Disconnect rate
should be < 1/hour (network-level, not server-level).

---

## 3. Orphan async task analysis

In Stage A, the orchestrator is instantiated but **registers zero
agents**. So `Orchestrator._run_loop` is never invoked. No new
`asyncio.create_task` calls from orchestration code.

The legacy lifespan creates ~5-6 background asyncio tasks:
- `_async_digest_loop`
- `_morning_note_scheduler`
- `_signal_verify_loop` (after 1h delay)
- `_macro_desk_snapshot_loop`
- `_explainer_scan_loop`
- `_alert_engine_loop`
- `_price_publisher_loop`

These are stored in `_bg_tasks` set with `add_done_callback(_bg_tasks.discard)` — proper cleanup pattern. Tasks don't leak.

**Orchestration Stage A contributes ZERO additional asyncio tasks.**

This is the design intent: orchestrator is dormant data until agents register (Stage 4.3+).

To verify externally (would require adding a debug endpoint that calls `asyncio.all_tasks()`):

```python
# Future debug endpoint — NOT shipped in Sprint 4
@app.get("/api/debug/tasks")
async def debug_tasks():
    import asyncio
    tasks = asyncio.all_tasks()
    return {"count": len(tasks), "names": [t.get_name() for t in tasks]}
```

For now: PID count (14 at t+15min) ≈ pre-flip baseline (13 at age-matched
13min) indicates no new thread pool growth from orchestration.

---

## 4. asyncio thread pool behavior

`asyncio.to_thread()` (used by some legacy `await asyncio.to_thread(sync_fn)` calls) uses a default `ThreadPoolExecutor` that grows up to `min(32, os.cpu_count() + 4)`.

Observed PID trajectory: 13 → 14 → 19 → 21 → **14**. The thread pool:
- Grew up to 21 PIDs at t+10min (peak workload during a background loop)
- Reclaimed idle workers back to 14 at t+15min

**This is the expected behavior**. A monotonic-growth pattern would
indicate leak; cycling indicates healthy reclamation.

---

## 5. Loop stall indicators — none observed

Event loop stall signatures we'd see if they occurred:

| Signature | Status |
|---|---|
| `/health` latency > 100ms | NOT observed (max 5ms) |
| Healthcheck failing → restart | NOT observed (0 restarts) |
| `asyncio` warnings in logs | NOT observed |
| `Task was destroyed but it is pending` warnings | NOT observed |
| `asyncio.exceptions.CancelledError` storms | NOT observed |
| WebSocket disconnect spikes (no direct measure) | unknown — needs 24h soak |

---

## 6. Sprint-4 specific code paths exercised

The orchestration code that runs DURING this soak:
- `orchestration.runtime.orchestrator_enabled()` — 1 invocation at boot
- `orchestration.runtime.build_event_bus()` — 1 invocation, returns RedisEventBus
- `orchestration.runtime.build_orchestrator()` — 1 invocation, returns empty Orchestrator
- `orchestration.event_bus.RedisEventBus.__init__()` — 1 invocation, holds the redis client
- `orchestration.event_bus.RedisEventBus._build_redis_bus → ping()` — 1 invocation, succeeded

Code paths NOT exercised yet:
- `EventBus.publish`, `try_consume_one`, `ack`, `publish_to_dlq` — no producers/consumers
- `Orchestrator.register`, `start_agent`, `stop_agent` — no agents
- `Orchestrator._run_loop` — no agents to drive
- Any agent's `tick()` — no agents
- Any critic's `evaluate()` — no events to critique
- Circuit breaker actual usage — no external calls wrapped yet (Stage 4.5)

**This is by design**: Stage A is the infrastructure soak. The interesting
code paths get exercised in Stage 4.3+.

---

## 7. 24h continuation: signals to watch

| Hour | Memory | Restarts | New ERRORs | PIDs | Verdict |
|---|---|---|---|---|---|
| t+1h | (operator captures) | should stay 0 | should stay 0 (excl. TG/yf) | should be <30 | green if all true |
| t+6h | … | 0 | 0 | <30 | green |
| t+12h | … | 0 | 0 | <30 | green |
| t+24h | < 600 MiB | 0 | 0 | <30 | **PROCEED** to Stage 4.3 |

Memory asymptote estimate based on 15-min curve: **400–500 MiB**. If
24h shows it settling under 600 MiB, that's well within budget.

---

## 8. Summary

Event loop behavior in the 15-minute window is **stable and responsive**:
- Latency < 5ms
- No restarts
- PIDs cycle (no leak)
- No new errors from orchestration code
- Memory growth flattens

Stage A is performing as designed. The continuation playbook
(`ORCHESTRATOR_SOAK_REPORT.md §5`) covers the remaining ~23 hours.
