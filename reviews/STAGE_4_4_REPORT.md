# STAGE_4_4_REPORT.md

> Sprint 4 Stage 4.4 — SignalCriticAgent (observe-only). Implemented,
> tested, merged, deployed flag-OFF. 2026-05-20.
>
> **Stage 4.4 itself succeeded.** A separate incident occurred during
> the deploy restart — see §6 and `THREAD_LEAK_INCIDENT.md`.

---

## 1. What Stage 4.4 delivered

| File | Change | LOC |
|---|---|---|
| `orchestration/agents/signal_critic_agent.py` | NEW — `SignalCriticAgent` + 3 critics | ~250 |
| `orchestration/agents/__init__.py` | exports `SignalCriticAgent` | +3 |
| `dashboard_api.py` | lifespan registers it behind `AGENT_SIGNAL_CRITIC_ENABLED` | +27 |
| `.env.production.example` | `AGENT_SIGNAL_CRITIC_ENABLED=false` + docs | +13 |
| `tests/test_sprint4_signal_critic_agent.py` | NEW — 27 tests | ~330 |
| `scripts/sim/sim_signal_critic.py` | NEW — 6 failure scenarios | ~210 |

Commits (PR #12, merged `f338bd0`):
```
52d98fb  test: 27 SignalCriticAgent tests + sim_signal_critic
72d6b20  feat(dashboard_api): register SignalCriticAgent behind flag
883d7ef  feat(orchestration): SignalCriticAgent — observe-only critic
```

---

## 2. The agent

`SignalCriticAgent(StreamAgent)`:
- Consumes `events:signal:candidate` (no producer yet → idle in Sprint 4.4)
- Runs a 3-critic `ChainCritic`:
  - `SchemaCritic` — payload has `asset`, `confidence`, `decision`
  - `_ConfidenceFloorCritic` — confidence ≥ 50
  - `_RecentBarCritic` — envelope timestamp within 300s
- Per event: logs `signal_critic_observed` + emits `signal.critique`
  event (metadata only — verdict, reason, original trace_id; no
  original signal payload replicated)

### Observe-only — verified
- The original `signal.candidate` event is ACKed regardless of verdict
- NO DLQ routing, NO halt, NO enforcement
- A `reject` verdict is logged + emitted but does NOT act
- Fail-open: critic chain exception or bus-emit failure → log + return,
  event still acked, tick still succeeds

---

## 3. Test results

| Layer | Count | Status |
|---|---|---|
| New SignalCriticAgent tests | 27 | ✅ |
| Sprint 1-4.3 smoke (regression) | 229 | ✅ |
| Total smoke local | 256 | ✅ |
| Full suite remote CI (PR #12) | **330** | ✅ |
| Simulations (12 prior + sim_signal_critic) | 13/13 | ✅ |

`sim_signal_critic.py` — 6/6 scenarios:
1. healthy event → accept + ack + critique emitted
2. rejectable event → reject logged, still acked, no DLQ
3. critic chain exception → fail-open
4. bus emit failure → fail-open
5. malformed payload → schema reject, no propagation
6. concurrent events → 5 verdicts in order

---

## 4. CI

| Item | Result |
|---|---|
| PR | https://github.com/mahendirank/ai-market-terminal/pull/12 |
| CI on PR | ✅ all 3 jobs green (330 tests) |
| Merge commit | `f338bd0` |

---

## 5. VPS deployment

| Aspect | Value |
|---|---|
| Pre-deploy SHA | `dd6fddb` |
| Post-deploy SHA | `f338bd0` |
| Pre-deploy snapshot | `/opt/backups/db-pre-stage4-4-2026-05-20_0751.tar.gz` (812K) |
| Tag | `pre-stage-4-4-2026-05-20_0751` (VPS-local) |
| `AGENT_SIGNAL_CRITIC_ENABLED` | NOT SET → false (correct) |
| `agent_registered_and_started` count | 1 (only `news.fetch`; `signal.critic` correctly NOT registered) |

The SignalCriticAgent code is on the VPS, flag off, dormant. **It works
and is safe.**

---

## 6. Incident during the deploy restart

The Stage 4.4 deploy required a container restart. That restart —
happening on 2026-05-20 when an upstream RSS feed is hung — triggered a
**thread-churn in the legacy `news.py` pipeline**:

- Thread count spiked from 14 to a peak of 284
- Root cause: cold-cache boot + a hung RSS feed + `news.py`'s
  unbounded `ThreadPoolExecutor` fan-out
- The Stage 4.3 `NewsFetchAgent` amplified it (`asyncio.to_thread` +
  timeout orphans threads), so it was **disabled** per the
  instability protocol
- After disabling the agent, the churn **persisted** — confirming
  legacy `news.py`, not the agent, is the root
- The condition **self-stabilized** at ~40 threads once the cache
  warmed; container stayed healthy, 0 restarts, served traffic
  throughout

**Stage 4.4's SignalCriticAgent was never enabled and is NOT
implicated.** Full analysis: `THREAD_LEAK_INCIDENT.md`.

---

## 7. Current production state

```
AGENT_ORCHESTRATOR_ENABLED=true
AGENT_NEWS_FETCH_ENABLED=false      ← disabled in the incident
AGENT_SIGNAL_CRITIC_ENABLED         ← unset (never enabled)

Container: healthy, 0 restarts
Agents:    0 registered (orchestrator idle — Stage A baseline)
Legacy:    authoritative, serving normally
Threads:   ~40 stable (29 residual from the hung feed; clears on restart)
Memory:    ~432 MiB
```

---

## 8. Stage 4.4 verdict

| Aspect | Status |
|---|---|
| SignalCriticAgent implemented | ✅ |
| Observe-only semantics enforced | ✅ verified by 27 tests + 6 sims |
| Fail-open on all failure layers | ✅ |
| Feature-flagged, default off | ✅ |
| Full regression + simulations | ✅ 330 CI tests, 13 sims |
| Deployed to VPS (flag off) | ✅ |
| Legacy pipeline authority maintained | ✅ |

**Stage 4.4 is COMPLETE and SOUND.**

---

## 9. What must happen before any further agent enablement

| Agent | Can it be enabled? | Blocker |
|---|---|---|
| `SignalCriticAgent` | YES — safe (doesn't touch news.py). But idle without a producer. | None — though enabling it now has near-zero value (no `events:signal:candidate` producer until Sprint 5) |
| `NewsFetchAgent` | **NO — keep OFF** | Needs Fix A (no thread orphaning on timeout) + benefits from Fix B (news.py feed hardening) — see `THREAD_LEAK_INCIDENT.md §8` |

---

## 10. Recommended next steps (priority order)

1. **Stage 4.3.1 — fix `NewsFetchAgent` thread orphaning.** Change
   `run_once` to a cache-read-only observer that never triggers a
   fanning fetch (recommended option in `THREAD_LEAK_INCIDENT §8`).
2. **Legacy hardening task — `news.py` feed-fetch bounding.** Make
   `get_rss_news` guarantee its worker threads return. Scope as an
   explicit change-controlled task (touches legacy).
3. **Identify the hung feed.** Audit `sources_config.py` / news.py
   feed list against live feed health; quarantine the bad one.
4. **Then** re-attempt the NewsFetchAgent dual-run.
5. Stage 4.4 SignalCriticAgent stays deployed flag-off until Sprint 5
   adds an `events:signal:candidate` producer — only then does enabling
   it produce observable verdicts.
