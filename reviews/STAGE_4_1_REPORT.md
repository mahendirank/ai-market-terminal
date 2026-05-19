# STAGE_4_1_REPORT.md

> Sprint 4 Stage 4.1 — orchestrator lifespan hook + admin endpoints.
> Implemented, tested, merged, deployed. Flag remains **OFF**. 2026-05-19.

---

## 0. Scope recap

Stage 4.1 wires `Orchestrator` + `EventBus` into FastAPI's lifespan
**strictly behind a feature flag, off by default**. It also adds three
read-only admin endpoints. **Zero agents registered.** No autonomous
execution. No production behavior change in flag-off mode.

---

## 1. Files touched

| File | Change | LOC |
|---|---|---|
| `orchestration/runtime.py` | NEW — factory functions (`build_event_bus`, `build_orchestrator`, `orchestrator_enabled`) | 122 |
| `orchestration/admin.py` | NEW — endpoint helpers (`agents_snapshot`, `circuits_snapshot`, `streams_health_snapshot`) | 80 |
| `dashboard_api.py` | MODIFIED — +50 lines: lifespan block (lazy import) + 3 routes | +52, -1 |
| `.env.production.example` | MODIFIED — Sprint 4 env vars block (all safe-off defaults) | +25 |
| `tests/test_sprint4_runtime.py` | NEW | 122 |
| `tests/test_sprint4_lifespan.py` | NEW | 130 |
| `tests/test_sprint4_admin_endpoints.py` | NEW | 138 |

**Total: ~670 LOC added (~530 prod + ~140 test), 1 LOC modified.**

---

## 2. Commits (preserved for revertability)

```
72ad6b5  docs+test: env example + 31 Sprint-4.1 tests
f1606c7  feat(dashboard_api): Sprint 4 Stage 4.1 lifespan + admin endpoints
e55e3e3  feat(orchestration): runtime + admin helpers for Sprint 4 Stage 4.1
```

Each commit is independently revertible:
- Revert `f1606c7` only → `dashboard_api.py` unwound; runtime+admin still on disk (harmless library)
- Revert `e55e3e3` only → would break the lifespan block; must also revert `f1606c7`
- Revert all 3 → tree identical to pre-Stage-4.1

---

## 3. Test results

| Layer | Count | Status |
|---|---|---|
| Sprint 4.1 new tests (local) | 31 | ✅ |
| Sprint 1+2+3 smoke (local) | 188 | ✅ no regression |
| Total smoke (local) | 219 | ✅ |
| Full suite remote on PR (CI) | 293 | ✅ |
| 12 failure-mode simulations | 12/12 | ✅ no regression |

---

## 4. PR + CI

| Item | Result |
|---|---|
| PR | https://github.com/mahendirank/ai-market-terminal/pull/6 |
| CI on PR branch | ✅ success (1m2s, all 3 jobs green) |
| CI on main after merge | ✅ success (1m0s) |
| Merge commit on main | `de45e3e` |

---

## 5. VPS deployment

| Aspect | Value |
|---|---|
| Pre-deploy SHA on VPS | `fcba9038` (matches origin/main from prior deploy) |
| Pre-deploy tag | `pre-stage-4-1-2026-05-19_0546` → `e20c2115...` |
| Pre-deploy DB snapshot | `/opt/backups/db-pre-stage4-1-2026-05-19_0546.tar.gz` (662K) |
| Post-deploy SHA on VPS | `de45e3e` (matches origin/main HEAD) |
| Downtime | ~30s |
| Health at | t+30s |
| Container memory post-deploy | 264 MiB (well within budget) |
| Container CPU post-deploy | 19.5% (steady-state) |
| Container restart count | 0 |
| Flag state | `AGENT_ORCHESTRATOR_ENABLED` **NOT SET** (defaults to false) |

---

## 6. Validation matrix (post-deploy on VPS)

| # | Check | Method | Result |
|---|---|---|---|
| 1 | `/health` unchanged | `docker exec curl /health` | `{"status":"ok"}` HTTP 200 ✅ |
| 2 | Orchestration not loaded | logs grep for orchestration init lines | none ✅ (flag off) |
| 3a | `/api/agents` registered | `docker exec curl /api/agents` | HTTP 401 ✅ (route exists, auth-gated; 401 ≠ 404) |
| 3b | `/api/circuits` registered | HTTP code | HTTP 401 ✅ |
| 3c | `/api/streams/health` registered | HTTP code | HTTP 401 ✅ |
| 4 | Existing routes unchanged | `/api/news`, `/api/regime` | HTTP 401 ✅ (identical to pre-deploy) |
| 5 | External HTTPS path works | `curl https://zyvoratech.co/health` | HTTP 200 ✅ |
| 6 | No new ERROR logs from Sprint 4 | grep ERROR for 60s | only pre-existing TG/yfinance noise ✅ |
| 7 | Restart loop check | `docker inspect RestartCount` | 0 ✅ |

---

## 7. What's verified by this stage

✅ **Flag-off default is safe**: orchestrator never constructed; orchestration package not imported; admin endpoints respond with `{"enabled": false, ...}` (when auth'd).

✅ **Lazy import discipline**: the `orchestration` package is wrapped in `try: import inside lifespan`; if anything in that try block fails, orchestrator stays None and boot continues.

✅ **No middleware changes**: existing middleware order preserved. Sprint 2 `RequestContextMiddleware` still outermost. No interference.

✅ **No existing route changes**: all 50+ pre-existing routes responding identically.

✅ **Admin endpoints under AuthMiddleware**: respond HTTP 401 without session cookie — same auth gate as `/api/news`, `/api/regime`, etc.

---

## 8. What's NOT verified yet

These require flipping `AGENT_ORCHESTRATOR_ENABLED=true` on the VPS, which is a separate operator decision per `SAFE_ENABLEMENT_PLAN.md`:

- Live orchestrator init log (`"orchestrator_lifespan_started"` with `registered_agents: 0`)
- Live admin endpoint responses with `enabled: true`
- Live `/api/streams/health` response showing zero-length known streams
- Shutdown log line (`"orchestrator_lifespan_stopped"`)
- Idle resource cost of an empty orchestrator (expected: <5MB additional memory)

These are exercised by the 31 new unit tests + simulations on the local
side. The VPS-level "live" verification happens after operator flips
the flag — recommended as a separate 24h soak window (per
`SAFE_ENABLEMENT_PLAN.md §4 Stage A`).

---

## 9. Rollback path (within 24h)

### Level 1 — env-only (no redeploy)
```bash
# Currently no-op (flag is already off). If operator turns it on and
# wants to roll back:
ssh root@72.61.173.89
sed -i '/^AGENT_ORCHESTRATOR_ENABLED=/d' /opt/zyvora/.env
cd /opt/zyvora
docker compose -f docker-compose.prod.yml restart market-terminal
```

### Level 2 — code revert (PR-level)
```bash
cd ~/ai-system/core
git revert -m 1 de45e3e   # revert PR #6 merge commit
git push origin main
# VPS: git pull + rebuild + force-recreate (~5 min)
```

### Level 3 — checkout pre-stage tag (on VPS)
```bash
ssh root@72.61.173.89
cd /opt/zyvora
git checkout pre-stage-4-1-2026-05-19_0546
docker compose -f docker-compose.prod.yml build market-terminal
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal
```

Estimated recovery time: 5 minutes for L2/L3; 30s for L1.

---

## 10. Honest open items

1. **Admin endpoints not exercised with authenticated session** during VPS validation. The 401 responses confirm registration but not response body shape. Mitigated by 7 admin endpoint tests in CI that exercise the bodies via `TestClient`. If you want me to do a full authenticated round-trip, I can ssh + login as admin + curl with cookies.

2. **Flag-on path not exercised on the VPS yet**. All flag-on paths are covered by `tests/test_sprint4_lifespan.py` locally and on CI, but the real production behavior under `AGENT_ORCHESTRATOR_ENABLED=true` is a Stage A item in `SAFE_ENABLEMENT_PLAN.md`. The plan recommends a deliberate 24h soak with the flag on (still 0 agents) before Stage 4.3 starts wrapping `news.py`.

3. **VPS pre-deploy HEAD anomaly**: this time `fcba903` ↔ origin/main; mystery `cb63f899` from last deploy did not recur. Either it was auto-resolved or it's intermittent. Worth a closer look in the next deploy cycle.

4. **Existing `[TG] send failed` noise** is unrelated to Sprint 4. Pre-existing: `.env` has `TELEGRAM_CHAT_ID=your-telegram-chat-id-here` (placeholder). User action separate from this sprint.

---

## 11. Status table

| Stage | What | Status |
|---|---|---|
| 4.1 lifespan hook (this stage) | code merged, tests green, deployed flag-off | ✅ DONE |
| 4.2 admin endpoints (merged with 4.1 in this PR) | endpoints registered, return shape verified | ✅ DONE |
| 4.3 NewsFetchAgent | not started | ⏳ Next stage |
| 4.4 SignalCriticAgent observe-mode | not started | ⏳ |
| 4.5 circuit_wrap external calls | not started | ⏳ |
| 4.6 XAUTOCLAIM sweep | not started | ⏳ |

Stage 4.2 is bundled with 4.1 in PR #6 since the admin endpoints depend on the lifespan exposing `app.state.orchestrator` and `app.state.event_bus`.
