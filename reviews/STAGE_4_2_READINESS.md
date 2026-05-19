# STAGE_4_2_READINESS.md

> Forward-looking: is Stage 4.2 ready? (It already shipped — see §0.)
> Then: what's the gate to Stage 4.3 (NewsFetchAgent)?

---

## 0. Stage 4.2 status — BUNDLED with 4.1

I bundled Stage 4.2 (admin endpoints) into the same PR as Stage 4.1
(lifespan hook) because the endpoints DEPEND on `app.state.orchestrator`
and `app.state.event_bus` being set up by the lifespan. Implementing
them as separate PRs would require:
- PR #1: lifespan hook, no endpoints (can't see whether it works)
- PR #2: endpoints

Bundling kept it as one mergeable, testable unit. The bundled work is:

| Stage | What | Status in PR #6 |
|---|---|---|
| 4.1 | Lifespan hook + orchestrator state | ✅ shipped |
| 4.2 | `/api/agents`, `/api/circuits`, `/api/streams/health` endpoints | ✅ shipped |

Both verified by 31 tests (CI) and live VPS validation (HTTP 401 confirms registration).

**No separate Stage 4.2 work remaining.**

---

## 1. Gate criteria for Stage 4.3 (NewsFetchAgent)

Stage 4.3 wraps `news.get_all_news()` in a `TickAgent` and runs it in
parallel with the legacy loop. Before starting:

| # | Criterion | Status |
|---|---|---|
| 1 | Stage 4.1 + 4.2 merged to main | ✅ (PR #6, commit `de45e3e`) |
| 2 | CI green on the Stage 4.1 PR | ✅ (293 tests pass) |
| 3 | VPS deployed with Stage 4.1 code (flag off) | ✅ (2026-05-19) |
| 4 | Container stable for ≥1h post-deploy | ⏳ Started 05:46 UTC; 1h mark is ~06:46 UTC |
| 5 | Operator flipped `AGENT_ORCHESTRATOR_ENABLED=true` and observed empty orchestrator for ≥24h | ❌ NOT YET — recommended Stage A from `SAFE_ENABLEMENT_PLAN.md` |
| 6 | `/api/agents` returns `{enabled:true, agents:[]}` against authenticated session | ❌ Requires Step 5 first |
| 7 | No new ERROR logs from `orchestration.*` loggers in the 24h window | ❌ Requires Step 5 first |
| 8 | 12 simulations still pass | ✅ (re-run after Stage 4.1) |

**Items 5–7 are the BLOCKERS.** Stage 4.3 should not start until the
operator has flipped the flag and observed for 24h.

---

## 2. What "Stage A" looks like (the bridge between 4.1 and 4.3)

Per `SAFE_ENABLEMENT_PLAN.md §4`, the first operator action is to flip
`AGENT_ORCHESTRATOR_ENABLED=true` and observe the empty orchestrator
for 24h. Recipe:

```bash
ssh root@72.61.173.89
cd /opt/zyvora

# 1. Take a fresh snapshot
STAMP="2026-XX-XX_$(date -u +%H%M)"
VOL=$(docker volume inspect zyvora_terminal_db --format '{{.Mountpoint}}')
tar czf /opt/backups/db-pre-stageA-${STAMP}.tar.gz -C "$VOL" .

# 2. Flip the flag
echo "AGENT_ORCHESTRATOR_ENABLED=true" >> .env

# 3. Restart
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal

# 4. Wait for healthy
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 6
  s=$(docker inspect --format='{{.State.Health.Status}}' market-terminal)
  echo "  $i: $s"
  [ "$s" = "healthy" ] && break
done

# 5. Verify new boot log
docker logs --tail 100 market-terminal | grep -E "orchestrator_lifespan|event_bus_init"
# Expected: "orchestrator_lifespan_started registered_agents=0"

# 6. Verify endpoint via authenticated session (requires admin login)
ADMIN_PASS='<paste admin password — never put in shell history>'
curl -c /tmp/c -s -X POST -d "username=admin&password=${ADMIN_PASS}" https://zyvoratech.co/login -L -o /dev/null
curl -b /tmp/c https://zyvoratech.co/api/agents | jq .
# Expected: {"enabled": true, "agents": []}
rm /tmp/c
unset ADMIN_PASS
```

**Observation window: 24 hours.**

What to watch:
- `docker stats market-terminal --no-stream` — memory growth < 50 MiB
- `docker logs market-terminal | grep -E "ERROR|orchestration"` — no new errors
- `curl -b /tmp/c /api/agents` — `enabled` stays true; `agents` stays `[]`
- `curl -b /tmp/c /api/streams/health` — all stream lengths stay 0
- `docker inspect market-terminal --format '{{.RestartCount}}'` — stays at 0

If all good → ready for Stage 4.3.

---

## 3. Stage 4.3 work breakdown (preview)

Once Stage A passes, Stage 4.3 implements:

| File | Action | LOC est. |
|---|---|---|
| `orchestration/agents/__init__.py` | NEW (empty package marker) | 1 |
| `orchestration/agents/news_fetch_agent.py` | NEW (TickAgent subclass wrapping `news.get_all_news`) | ~120 |
| `dashboard_api.py` | MODIFY lifespan — register agent if both flags true | +15 |
| `tests/test_sprint4_news_fetch_agent.py` | NEW | ~150 |
| `.env.production.example` | ADD `AGENT_NEWS_FETCH_ENABLED=false` | +1 |

Behind a SECOND flag (`AGENT_NEWS_FETCH_ENABLED=false` default).
Production change is purely opt-in.

**Soak protocol** (per `SPRINT_4_EXECUTION_PLAN.md §4`):
- 48h dual-run: legacy `news.py` loop AND `NewsFetchAgent` both active
- Compare emission counts: legacy [NEWS] logs vs `news.raw` events on the bus
- Cutover (`LEGACY_NEWS_LOOP_DISABLED=true`) only after equivalence ±5%

---

## 4. Items that would block Stage 4.3 even after Stage A passes

| Risk | Trigger | Action |
|---|---|---|
| `[TG] send failed` errors not addressed | Pre-existing; not Sprint 4 issue | Set real `TELEGRAM_CHAT_ID` in `.env`, or `ALERT_DISABLED=true`. Optional but cleaner. |
| Redis memory growth >100MB in 24h | Indicates an existing cache leak | Investigate; Stage 4.3 adds more Redis usage on top |
| Disk free <10GB on VPS | 23% used now → 77% free → headroom | Monitor in case any agent stream grows quickly |
| User wants to skip Stage A observation | Higher risk; revisit `SAFE_ENABLEMENT_PLAN` | Make the trade-off explicit |

---

## 5. My recommendation

**Stage 4.2 = DONE (bundled with 4.1).**

**Next step**: operator flips `AGENT_ORCHESTRATOR_ENABLED=true` on the
VPS and observes for 24h. The flip itself is a 30-second operation; the
24h soak is passive.

If the operator wants to proceed faster (e.g. 4-hour soak instead of
24h) that's a risk decision they can make. Sprint 4 doesn't write
production data yet — failures are limited to "orchestrator boots but
doesn't tick anything", which is intrinsically low-blast-radius.

If the operator wants me to **stage that flag flip now and verify
empirically**, say so. I have SSH access and can:
1. Take a fresh snapshot
2. Flip the flag in `.env`
3. Restart the container
4. Verify boot log + endpoint shape
5. Report back

That's a single one-off action — the 24h soak still needs to elapse,
but the flip + initial verification can happen in 5 minutes.

---

## 6. Summary

| Layer | Status |
|---|---|
| Stage 4.1 (lifespan hook) | ✅ shipped, deployed, validated flag-off |
| Stage 4.2 (admin endpoints) | ✅ bundled with 4.1, registered, auth-gated |
| Stage A (flag-on observe with 0 agents) | ⏳ Awaiting operator decision |
| Stage 4.3 (NewsFetchAgent) | 🔒 Blocked on Stage A |
| Stages 4.4, 4.5, 4.6 | 🔒 Sequenced per `SPRINT_4_EXECUTION_PLAN.md` |
