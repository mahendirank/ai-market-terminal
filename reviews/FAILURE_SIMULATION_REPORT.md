# FAILURE_SIMULATION_REPORT.md

> Sprint 4 Stage 4.4 — failure-mode simulations for SignalCriticAgent,
> PLUS the live incident that served as an unplanned real-world failure
> test. 2026-05-20.

---

## Part 1 — Planned failure simulations (sim_signal_critic.py)

`scripts/sim/sim_signal_critic.py` — 6 scenarios, all PASS:

| # | Scenario | What it proves | Result |
|---|---|---|---|
| 1 | Healthy signal | accept verdict, candidate acked, critique emitted | ✅ |
| 2 | Rejectable signal (low confidence) | reject LOGGED but candidate still acked, NO DLQ | ✅ |
| 3 | Critic chain raises | fail-open: log + ack + tick succeeds (no crash) | ✅ |
| 4 | Bus emit_event fails | fail-open: log + ack + tick succeeds | ✅ |
| 5 | Malformed payload (missing fields) | schema critic rejects; no propagation, no DLQ | ✅ |
| 6 | 5 concurrent events | 5 verdicts emitted in order | ✅ |

The critical invariant — **observe-only never blocks the signal** —
holds across all 6 scenarios. A reject is metadata; the candidate event
is always acked; the DLQ stays empty.

### Full simulation suite regression

All 13 simulations re-run after the Stage 4.4 implementation:

```
sim_circuit_breaker      PASS (7/7)
sim_duplicate_events     PASS (4/4)
sim_failed_consumer      PASS (4/4)
sim_graceful_degradation PASS (4/4)
sim_logging_load         PASS
sim_malformed_events     PASS (5/5)
sim_redis_disconnect     PASS (5/5)
sim_retry                PASS (6/6)
sim_retry_storm          PASS
sim_signal_critic        PASS (6/6)   ← new
sim_streams_recovery     PASS (6/6)
sim_timeout_cascade      PASS (3/3)
sim_trace_propagation    PASS (4/4)
```

13/13 simulations pass. Zero regression.

---

## Part 2 — The UNPLANNED failure test (live incident)

The Stage 4.4 deploy restart triggered a real production failure mode
that NO simulation had anticipated. It is documented in full in
`THREAD_LEAK_INCIDENT.md`; this section evaluates it as a failure test.

### What failed

`news.py`'s `ThreadPoolExecutor` fan-out across ~66 RSS feeds, combined
with a hung feed (today) and a cold-cache container restart, orphaned
OS threads — peaking at 284 PIDs.

### What our failure-handling did RIGHT

| Designed safeguard | Did it work? |
|---|---|
| Container healthcheck | ✅ stayed green throughout — the app kept serving |
| Docker restart policy | ✅ not needed — container never crashed (0 restarts) |
| Feature flag isolation | ✅ disabling `AGENT_NEWS_FETCH_ENABLED` was a 30s env-only action |
| Instability protocol ("disable agent immediately") | ✅ executed correctly, even though it turned out the agent wasn't the root cause |
| Legacy pipeline independence | ✅ legacy `news.py` kept serving; `/health`=200, `zyvoratech.co`=200 throughout |
| Self-stabilization | ✅ thread churn settled to ~40 once the cache warmed; never unbounded |
| Orchestrator isolation | ✅ disabling one agent left the orchestrator + (absent) other agents unaffected |

### What our failure-handling MISSED

| Gap | Why it matters |
|---|---|
| No simulation modeled `asyncio.to_thread` + timeout thread-orphaning | This is THE bug. `sim_timeout_cascade.py` tested timeout *cancellation* but used pure-async `asyncio.sleep`, not a blocking `to_thread` call — so it never exposed that the OS thread survives a cancelled `to_thread`. |
| No simulation modeled a hung external feed | `sim_redis_disconnect` covers Redis; nothing covered "a third-party RSS feed that accepts the connection then never finishes sending." |
| No thread-count alerting | The churn was found by manual inspection. A `/metrics` thread-count gauge (Sprint 5) would have flagged it automatically. |
| Cold-cache restart behavior was never load-tested | All prior soaks observed WARM, long-running containers. The cold-cache fan-out storm was unobserved until this restart. |

### New simulations to build (Sprint 4.3.1 / Sprint 5)

1. **`sim_to_thread_timeout_orphan.py`** — wrap a genuinely-blocking
   function (one that `time.sleep`s past the timeout) in
   `asyncio.to_thread` + `asyncio.wait_for`; assert that after the
   timeout, the OS thread is STILL ALIVE. This codifies the bug so a
   fix can be verified against it.

2. **`sim_hung_feed.py`** — simulate a feed fetch that hangs; assert
   the fetch layer bounds it (after Fix B lands).

3. **`sim_cold_cache_boot.py`** — simulate the warm-up fetch storm;
   assert thread count stays bounded.

These belong in the Stage 4.3.1 fix work, not Stage 4.4.

---

## Part 3 — Failure-mode coverage matrix (updated)

| Failure mode | Simulated? | Covered by | Live-verified? |
|---|---|---|---|
| Critic chain raises | ✅ | sim_signal_critic #3 | — |
| Bus emit fails | ✅ | sim_signal_critic #4 | — |
| Malformed signal payload | ✅ | sim_signal_critic #5 | — |
| Redis down | ✅ | sim_redis_disconnect | — |
| Retry storm | ✅ | sim_retry_storm | — |
| Timeout (pure async) | ✅ | sim_timeout_cascade | — |
| **Timeout (blocking to_thread) — thread orphan** | ❌ **GAP** | none | ✅ **live incident** |
| **Hung external feed** | ❌ **GAP** | none | ✅ **live incident** |
| **Cold-cache fetch storm** | ❌ **GAP** | none | ✅ **live incident** |
| Failed consumer | ✅ | sim_failed_consumer | — |
| Duplicate events | ✅ | sim_duplicate_events | — |
| Trace propagation | ✅ | sim_trace_propagation | — |

The three GAP rows are the lessons of this incident. They become
Sprint 4.3.1 simulation work.

---

## Part 4 — Verdict

**Stage 4.4's planned failure simulations: 6/6 pass. SignalCriticAgent
is robust** — fail-open at every layer, observe-only invariant holds.

**The unplanned incident exposed a real gap**: our simulations modeled
async failures well but never modeled the interaction of `asyncio`
timeouts with genuinely-blocking thread work. The incident was
contained (legacy preserved, self-stabilized, no outage) but it
revealed that the Stage 4.3 NewsFetchAgent design and the legacy
`news.py` fan-out both need hardening before the news agent runs again.

The SignalCriticAgent has none of this exposure — it does no blocking
I/O — so Stage 4.4 stands as complete and sound.
