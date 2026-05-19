# STAGE_4_3_ENABLEMENT_RECOMMENDATION.md

> After Stage A's flag flip + 15-min observation, recommend whether
> Stage 4.3 (`NewsFetchAgent` dual-run with legacy `news.py`) is safe
> to begin. 2026-05-19.

---

## 1. TL;DR

**Conditional GO.** All 15-minute signals are green. The hard
prerequisite is **the operator completing 24h of passive observation**
per `ORCHESTRATOR_SOAK_REPORT.md §5`. If that 24h elapses without
tripping any stop condition, Stage 4.3 implementation is safe to begin.

If the 24h soak trips any stop condition, **DO NOT proceed**. Rollback
Level 1 (env-var flip), investigate the root cause, fix, re-attempt
Stage A. Then evaluate Stage 4.3 again.

---

## 2. Decision matrix (current state, t+15 min)

| Criterion | Threshold | Observed | Verdict |
|---|---|---|---|
| Orchestrator boot succeeds | logs `orchestrator_lifespan_started` | yes | ✅ |
| 0 agents registered | `list_agents() == []` | yes (no Stage 4.3+ code shipped) | ✅ |
| No new ERROR from orchestration | grep filter ≠ pre-existing | 0 | ✅ |
| Health endpoint responsive | < 100ms p99 | 1.8–4.8ms p100 | ✅ |
| Restart count | 0 | 0 | ✅ |
| Memory growth controlled | < 50 MiB / hour | +4 MiB age-matched (likely <10/hour projected) | ✅ |
| PID cycling (no thread leak) | non-monotonic | cycled 13→21→14 | ✅ |
| Redis stable | no reconnect storm | 1 stable connection | ✅ |
| Async warnings | 0 | 0 | ✅ |
| External HTTPS works | 200 OK | 200 OK consistently | ✅ |
| WebSocket regression | (not directly tested, but health proves loop is live) | indirect ✅ | ⚠️ direct verification needed in 24h |

**11/11 green** at the 15-min mark; one is indirect (WS).

---

## 3. Gate to Stage 4.3 — required before implementation begins

| # | Criterion | Status |
|---|---|---|
| 1 | Stage A flag flipped + container restarted | ✅ done |
| 2 | Boot logs confirm orchestrator running with 0 agents | ✅ done |
| 3 | 15-min observation clean | ✅ done |
| 4 | **24h observation clean** (per playbook §5) | ⏳ **pending operator** |
| 5 | WS pipe verified by browser/wscat session ≥ 1h | ⏳ **pending operator** |
| 6 | Stop conditions checked at hourly intervals | ⏳ **pending operator** |
| 7 | Memory at 24h < 600 MiB | ⏳ pending |
| 8 | PID count at 24h < 30 | ⏳ pending |

**Items 4–8 must be ✅ before I write any Stage 4.3 code.**

---

## 4. What Stage 4.3 will add (preview, not yet implemented)

Per `SPRINT_4_EXECUTION_PLAN.md §4`:

| File | New code |
|---|---|
| `orchestration/agents/__init__.py` | empty package marker |
| `orchestration/agents/news_fetch_agent.py` | `NewsFetchAgent(TickAgent)` wrapping `news.get_all_news()` via `asyncio.to_thread` |
| `dashboard_api.py` lifespan | register + start agent if `AGENT_NEWS_FETCH_ENABLED=true` |
| `.env.production.example` | `AGENT_NEWS_FETCH_ENABLED=false` |
| `tests/test_sprint4_news_fetch_agent.py` | ~7 tests |

**Critical**: the new flag `AGENT_NEWS_FETCH_ENABLED` will default to
`false`. Deploying the Stage 4.3 code does NOT start the agent. The
operator must explicitly flip it. Same safety model as Stage A.

---

## 5. Risk-adjusted recommendation

| Path | Risk | Cost | When |
|---|---|---|---|
| **Wait full 24h, then proceed to 4.3 implementation** | Lowest | +23h calendar time, $0 | After tomorrow's reading |
| Proceed to 4.3 implementation NOW; deploy after 24h | Medium-low (code lands while soak runs) | -23h calendar | Trades soak-with-stable-code for stability |
| Wait 6h, then 4.3 | Medium | +6h | Compromise |
| Skip soak entirely, go to 4.3 | Medium-high (no validation under sustained load) | 0 | NOT RECOMMENDED |

**My recommendation: option 1 — wait full 24h.**

Rationale:
- The cost is only 23h of calendar time during which Stage A keeps running with zero risk-of-harm (no agents).
- Stage A is the FIRST time orchestration runtime sees production conditions. A full overnight + business-hours cycle catches issues a 15-min window can't.
- Stage 4.3's first agent (NewsFetchAgent) duplicates the legacy news loop — defense in depth via dual-run is already built in, so urgency is low.

If the user prefers option 2 (implement 4.3 in parallel), I can do that — the code work is bounded (~150 LOC implementation + ~150 LOC tests + 1 PR). The implementation itself adds zero risk while the flag remains off.

---

## 6. Stage 4.3 readiness checklist (run when ready)

When you're ready to begin Stage 4.3, confirm:

- [ ] 24h has elapsed since flag flip (currently ~15 min in)
- [ ] `RestartCount` is still 0
- [ ] Memory at 24h is < 600 MiB
- [ ] No new orchestration ERROR lines accumulated
- [ ] PIDs at 24h are < 30
- [ ] External HTTPS still 200 OK
- [ ] WebSocket session tested for ≥ 1h with stable price ticks
- [ ] Redis used_memory_human still < 5 MiB
- [ ] Stream keys (`events:*`, `dlq:*`) still absent

If all 9 ✅: tell me "Proceed with Stage 4.3" and I'll start writing.

---

## 7. Rollback plan (if 24h soak fails)

```bash
# Level 1 — env-var only (30s)
ssh root@72.61.173.89 'bash -s' <<'EOF'
sed -i 's/^AGENT_ORCHESTRATOR_ENABLED=.*/AGENT_ORCHESTRATOR_ENABLED=false/' /opt/zyvora/.env
docker compose -f /opt/zyvora/docker-compose.prod.yml up -d --force-recreate market-terminal
EOF

# Then investigate the specific signal that tripped (memory leak? restart loop?)
# Optionally: git checkout pre-stageA-2026-05-19_0559 if the failure points
# at the orchestration code itself (not just env behavior).
```

---

## 8. Final word

15-minute Stage A is healthy and producing the expected
"infrastructure-on-but-idle" state. The architecture is doing what the
unit tests + 12 simulations predicted: empty orchestrator costs near
zero. The remaining 23h are the real validation — sustained behavior
matters more than initial behavior.

When the 24h soak completes cleanly, Stage 4.3 is safe to begin under
the same gradual-rollout model: implement → CI green → deploy
code-only → flag flip → 48h dual-run → cutover.
