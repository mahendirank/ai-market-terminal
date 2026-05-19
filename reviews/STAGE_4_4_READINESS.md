# STAGE_4_4_READINESS.md

> Is Stage 4.4 (SignalCriticAgent in observe-only mode) ready to begin?
> 2026-05-19.

---

## 0. Stage 4.4 in one sentence

**Add a `SignalCriticAgent` (StreamAgent) that consumes a hypothetical
`events:signal:candidate` stream, runs a Chain of deterministic
critics, and EMITS A LOG LINE ONLY — does NOT reject, route, or DLQ.**

This is the observation half of the critic infrastructure. Sprint 5
will add a producer (`events:signal:candidate` emitter) AND flip
critic enforcement.

---

## 1. Gate criteria for starting Stage 4.4

| # | Criterion | Status |
|---|---|---|
| 1 | Stage 4.3 code merged to main | ✅ PR #9 (`dd6fddbf`) |
| 2 | Stage 4.3 code deployed to VPS (flag off) | ✅ 2026-05-19 16:19 UTC |
| 3 | Stage 4.3 tests + simulations green | ✅ 229 + 12/12 |
| 4 | `AGENT_NEWS_FETCH_ENABLED=true` flipped by operator | ❌ pending |
| 5 | 48h dual-run with NewsFetchAgent active | ❌ pending |
| 6 | No regression observed during dual-run | ❌ pending |
| 7 | Resource budget held (memory < 600 MiB at 48h) | ❌ pending |
| 8 | Zero non-transient agent ERROR lines | ❌ pending |
| 9 | At least 50 successful agent ticks recorded | ❌ pending |

Items 4–9 require operator action + 48h elapsed time. **Stage 4.4
implementation should NOT begin until all 9 are ✅.**

---

## 2. Why we wait 48h before Stage 4.4

Stage 4.4 adds a SECOND agent. Each additional agent multiplies
debugging complexity if something goes wrong:

- One-agent failure: clearly traceable to the news fetch path
- Two-agent failure: requires bisection to identify which one caused it

A clean 48h dual-run for the news agent establishes the baseline; then
Stage 4.4's signal critic is the controlled change against that baseline.

Additionally:
- 48h covers a full business-day cycle in both US (UTC overnight) and Asia (UTC mid-day) traffic patterns
- Cache cooperation and external API rate-limit windows reset across multiple cycles
- Memory leak signal (if any) becomes detectable

---

## 3. Stage 4.4 work breakdown (preview, not yet started)

Per `SPRINT_4_EXECUTION_PLAN.md §5`:

| File | Action | LOC est. |
|---|---|---|
| `orchestration/agents/signal_critic_agent.py` | NEW (StreamAgent + ChainCritic) | ~150 |
| `dashboard_api.py` lifespan | register if `AGENT_ORCHESTRATOR_ENABLED=true` | +15 |
| `.env.production.example` | (optional) `AGENT_SIGNAL_CRITIC_ENABLED` if separate flag preferred | +5 |
| `tests/test_sprint4_signal_critic_agent.py` | NEW | ~150 |

Key implementation decisions Stage 4.4 will face:

### Decision A: enable flag — single or separate?
- **Option 1**: separate `AGENT_SIGNAL_CRITIC_ENABLED=false` flag
- **Option 2**: auto-register when `AGENT_ORCHESTRATOR_ENABLED=true` (since it's observe-only and harmless)

Recommendation: Option 1 (separate flag). Matches the pattern.

### Decision B: producer for `events:signal:candidate`?
Stage 4.4 by itself has NO producer. The critic will register against
an empty stream and tick uselessly until Stage 5.x adds a producer.

That's intentional: get the critic topology right BEFORE adding the
producer.

### Decision C: critic chain composition
Three deterministic critics from `SPRINT_4_EXECUTION_PLAN §5`:
- `SchemaCritic` — payload must have `asset`, `confidence`
- `_ConfidenceFloorCritic` — confidence ≥ 50
- `_RecentBarCritic` — envelope timestamp recent enough

All composed via `ChainCritic(halt_on_reject=True)`.

---

## 4. Decision tree if Stage 4.3 soak FAILS

If during 48h the news agent trips a stop condition:

| Failure pattern | Action |
|---|---|
| Memory leak (sustained growth > 50 MiB/h) | Rollback flag. Profile. Likely cause: cache state in legacy module. Stage 4.4 BLOCKED. |
| Frequent retries (consecutive_failures = 3-4 hourly) | Investigate external API health. Adjust retry policy. Re-attempt soak. Stage 4.4 BLOCKED. |
| Container restart | Catastrophic. Rollback. Audit dashboard_api.py wiring. Likely a Stage 4.3 bug. |
| WebSocket disconnect rate increase | The agent IS blocking the event loop somewhere. Profile `asyncio.to_thread` behavior. |
| API latency p99 climbs | Same — agent is contending with the event loop. |
| New ERROR class | Investigate. Could be a logging path issue or a real fault. |

Recovery from each: Level 1 env rollback (30s), then debug.

---

## 5. Conservative timeline

| Day | Event |
|---|---|
| 2026-05-19 | Stage 4.3 code deployed (flag off) — ✅ done |
| 2026-05-20 (T0) | Operator flips `AGENT_NEWS_FETCH_ENABLED=true` |
| 2026-05-20 +1h | First-hour smoke check |
| 2026-05-21 (T0 +24h) | Halfway daily review |
| 2026-05-22 (T0 +48h) | Final review; if green → start Stage 4.4 |
| 2026-05-22 → 2026-05-24 | Stage 4.4 implementation |
| 2026-05-25 onwards | Stage 4.4 soak |

If you want to compress this, implementation of Stage 4.4 can happen IN
PARALLEL with the 48h soak — the PR sits unmerged until soak passes.
This is what I'd recommend if calendar time is precious.

---

## 6. My recommendation

**Wait for the 48h soak to complete before starting Stage 4.4 implementation.**

Rationale:
- Stage 4.3 IS the first agent in production. If something is wrong with the agent runtime, it'll show during this 48h. Better to find it now with one agent than two.
- The work isn't urgent — the system is healthy and functioning.
- Implementation of 4.4 is bounded and can be done at the end of Stage 4.3 soak.

Alternative: implement 4.4 NOW (in parallel with soak), merge to main
when 4.3 soak passes. Adds ~1 day calendar savings; minimal risk because
the merge is gated on soak success.

---

## 7. Stage 4.4 SAFE / FORBIDDEN list (preview, will be refined when starting)

When Stage 4.4 implementation begins, the prompt will likely look very
similar to Stage 4.3:

```
ALLOWED:
- SignalCriticAgent(StreamAgent)
- Deterministic critic chain (schema + confidence floor + recency)
- Observe-only mode (log verdict; never reject/DLQ)
- Per-tick latency tracking
- Same flag pattern: AGENT_SIGNAL_CRITIC_ENABLED=false default

FORBIDDEN:
- Critic enforcement
- DLQ routing of rejects
- LLM-backed critics
- Producer for events:signal:candidate (Stage 5.x)
- Multi-agent fan-out
- Trade execution
```

The cycle (implement → tests → CI → deploy code → flip flag → 48h soak)
will repeat.

---

## 8. Summary

| Layer | Status |
|---|---|
| Stage 4.3 code | ✅ shipped, deployed flag-off |
| Stage 4.3 48h dual-run | ⏳ pending operator flag flip |
| Stage 4.4 implementation | 🔒 gated on Stage 4.3 soak success |
| Sprint 5 (next sprint, critic enforce + producers + Prometheus) | 🔒 farther |

**Next operator action**: flip `AGENT_NEWS_FETCH_ENABLED=true` when
ready; observe per `NEWS_AGENT_DUAL_RUN_REPORT.md §4`; report back after
48h. I'll write Stage 4.4 implementation prompt at that point.

Or, if you prefer parallel progress: tell me "implement Stage 4.4 now"
and I'll start writing the code while the soak runs. Merge will be
gated on soak passing.
