# SAFE_ENABLEMENT_PLAN.md

> Forward-looking: how to safely turn on each Sprint 4 component, one at
> a time, with explicit gates and rollback per flag. **Not yet executed.**

---

## 0. Where we are now

After the 2026-05-19 deploy:
- Sprint 1+2+3 code is on the VPS
- Sprint 1+2 features are **ACTIVE** (tests/CI invisible at runtime; logging middleware live)
- Sprint 3 features are **DORMANT** (orchestration code on disk, not imported)
- No `AGENT_*` env vars set; defaults are OFF

The system is in a stable production state. Nothing else needs to flip
right away.

---

## 1. The 4 flags Sprint 4 introduces

Sprint 4 doesn't change runtime behavior on merge alone — it adds code
behind feature flags. Each flag is independent; default off; flipping
requires a container restart.

| Flag | What it enables | Default | When to flip |
|---|---|---|---|
| `AGENT_ORCHESTRATOR_ENABLED` | FastAPI lifespan builds + holds `Orchestrator` + `EventBus`. Zero agents registered. | `false` | After Sprint 4.1 + 4.2 merge + observation |
| `AGENT_NEWS_FETCH_ENABLED` | Registers `NewsFetchAgent` in the orchestrator; starts the tick loop. Runs PARALLEL to legacy news loop. | `false` | After Sprint 4.3 merge + 24h orchestrator-only soak |
| `LEGACY_NEWS_LOOP_DISABLED` | Silences the legacy `news.py` periodic call. Only the agent emits. | `false` | After ≥48h dual-run with equivalent emission counts (±5%) |
| `SIGNAL_CRITIC_ENFORCE` | Flips the SignalCriticAgent from observe-only to enforce (rejected events → DLQ). | `false` | Sprint 5 only — after observe-mode reveals false-positive rate |

---

## 2. Pre-flip checks (apply to every flag flip)

Before flipping ANY flag in `.env` and restarting:

```bash
# 1. Confirm rollback artifacts present
ssh root@72.61.173.89 '
  cd /opt/zyvora
  git tag --list pre-sprint-rollout-* | tail -3
  ls -lh /opt/backups/db-pre-rollout-*.tar.gz | tail -3
'

# 2. Confirm tests still green on the deployed SHA (CI as proxy)
gh run list --branch main --limit 1

# 3. Take a fresh snapshot before flipping (cheap insurance)
ssh root@72.61.173.89 '
  cd /opt/zyvora
  STAMP="2026-XX-XX_$(date -u +%H%M)"
  VOL=$(docker volume inspect zyvora_terminal_db --format "{{.Mountpoint}}")
  tar czf /opt/backups/db-pre-flag-${STAMP}.tar.gz -C "$VOL" .
'

# 4. Capture baseline metrics
ssh root@72.61.173.89 '
  docker stats market-terminal --no-stream
  docker exec market-terminal curl -s http://localhost:8001/api/health | jq ".checks.redis.ok"
'
```

If any of these fail, **do not flip**.

---

## 3. Flag flip recipe (one at a time, NEVER batched)

```bash
# 1. SSH and edit .env on VPS
ssh root@72.61.173.89
cd /opt/zyvora
# Add ONE line (e.g.):
echo "AGENT_ORCHESTRATOR_ENABLED=true" >> .env

# 2. Restart container
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal

# 3. Wait for health
for i in 1 2 3 4 5 6; do
  sleep 10
  if [ "$(docker inspect --format='{{.State.Health.Status}}' market-terminal)" = "healthy" ]; then
    echo "  → healthy at ${i}0s"
    break
  fi
done

# 4. Smoke check: did the new flag take effect?
docker exec market-terminal curl -s http://localhost:8001/api/agents | jq .

# 5. Watch logs for first 5 minutes
docker logs -f --since 1m market-terminal | grep -E "ERROR|WARNING|orchestrator"
# Ctrl-C when satisfied

# 6. Observation window — leave it for the documented duration (24h or 48h)
```

If anything goes wrong: **rollback by removing the line**:
```bash
sed -i '/^AGENT_ORCHESTRATOR_ENABLED=/d' .env
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal
```

---

## 4. Stage-by-stage enablement sequence

### Stage A — Empty orchestrator (after Sprint 4.1 + 4.2)

```bash
# Flag:
AGENT_ORCHESTRATOR_ENABLED=true

# Acceptance:
# - /api/agents returns {enabled: true, agents: []}
# - /api/circuits returns {circuits: []}
# - No new ERROR logs from agents.* loggers
# - Memory grows by <50 MB (orchestrator state only)
# - 24h: no instability, no leak

# Rollback:
AGENT_ORCHESTRATOR_ENABLED=false
# Or remove the line entirely.
```

### Stage B — Add NewsFetchAgent in parallel with legacy (after Sprint 4.3)

```bash
# Flag (in addition to A):
AGENT_NEWS_FETCH_ENABLED=true

# Acceptance during 48h soak:
# - /api/agents shows news.fetch as RUNNING with total_ticks > 0
# - news.raw events appear in /api/streams/health (length > 0)
# - Legacy [NEWS] log lines continue (unchanged)
# - Both paths emitting at ~equivalent rate (±5%)
# - No consecutive failures > 1 in any 1h window
# - Memory growth <100 MB over 24h (no leak)

# Rollback:
AGENT_NEWS_FETCH_ENABLED=false
# Legacy loop unaffected — keeps producing news.
```

### Stage C — Cutover from legacy to agent (after 48h equivalence)

```bash
# Flag (in addition to A + B):
LEGACY_NEWS_LOOP_DISABLED=true

# This requires a code change in news.py to honor the flag — Sprint 4
# work, not a pure env-var flip.

# Acceptance:
# - Legacy [NEWS] log lines STOP
# - news.raw events continue from the agent
# - Downstream consumers unaffected
# - 24h: no missed headlines (compare against pre-cutover sample)

# Rollback:
LEGACY_NEWS_LOOP_DISABLED=false
# Legacy loop resumes; both paths active again.
```

### Stage D — SignalCriticAgent in OBSERVE mode (after Sprint 4.4)

Note: in Sprint 4 there's no producer feeding `events:signal:candidate`,
so this agent ticks but processes 0 events. The wiring is in place for
Sprint 5 to add a producer. **NO flag** is needed for observe mode — it
runs automatically when `AGENT_ORCHESTRATOR_ENABLED=true` AND it's
registered in the lifespan.

To prevent it from registering (if needed):
- Remove its registration block from `dashboard_api.py`, OR
- Add a feature flag `AGENT_SIGNAL_CRITIC_ENABLED` and gate registration

The latter is recommended; document in the Sprint 4 PR.

### Stage E — SignalCritic enforce mode (Sprint 5 only)

```bash
# Flag (Sprint 5):
SIGNAL_CRITIC_ENFORCE=true

# Pre-flip check:
# - 1 week of observe-mode data showing low false-positive rate
# - Specific reason breakdown: percentage of rejects per `reason`
# - Operator review of "borderline" rejects

# Acceptance:
# - DLQ for events:signal:candidate starts growing
# - Approved signals continue to flow
# - Reject rate matches observe-mode prediction within ±10%

# Rollback:
SIGNAL_CRITIC_ENFORCE=false
# Restored: critic logs but doesn't act.
```

---

## 5. Forbidden combinations

Do not flip these simultaneously:

| Combination | Why forbidden |
|---|---|
| `AGENT_NEWS_FETCH_ENABLED=true` AND `LEGACY_NEWS_LOOP_DISABLED=true` BEFORE 48h dual-run | Skips equivalence verification; data loss risk if agent has a regression |
| `SIGNAL_CRITIC_ENFORCE=true` without observe data | Could DLQ signals that should have shipped |
| Any flag flip without DB snapshot in the last 24h | If something goes wrong, no recent recovery point |

---

## 6. Time budget per stage

| Stage | Soak time minimum | Calendar |
|---|---|---|
| A — Empty orchestrator | 24h | Day 1–2 after Sprint 4.1+4.2 merge |
| B — Agent parallel with legacy | 48h | Day 3–5 |
| C — Cutover | 24h | Day 6–7 |
| D — SignalCritic observe | 7 days | Spans into Sprint 5 |
| E — SignalCritic enforce | Sprint 5 work | n/a in Sprint 4 |

Total wall-clock Sprint 4 lifecycle: ~1 week of soak time after code lands.

---

## 7. Stop conditions (revert IMMEDIATELY if seen)

- Any new ERROR-level log line from `agents.*` or `orchestration.*` loggers
- Memory growth >2× baseline within an hour
- CPU sustained >70% for >10 minutes
- Health endpoint non-200 for >30s
- DLQ depth > 50 events
- Any agent's `consecutive_failures` reaches the disable threshold (default 5)
- WebSocket disconnect rate doubling

Recovery procedure: flip the relevant flag false and restart. Sprint
4.x rollback procedures in `SPRINT_4_EXECUTION_PLAN.md` cover code-level
reverts.

---

## 8. Recommendation: Sprint 4 Stage 4.1 readiness

**Stage 4.1 (lifespan hook, empty orchestrator)** is safe to begin
**immediately**. Justification:

| Criterion | Status |
|---|---|
| Foundation deployed to production | ✅ 2026-05-19 |
| Foundation observably stable | ✅ /api/health all-OK, 0 restarts |
| Rollback tested empirically | ✅ Per `ROLLBACK_VALIDATION.md` |
| All 12 simulations pass | ✅ Per `FAILURE_MODE_SIMULATION_REPORT.md` |
| Sprint 4 plan implementation-ready | ✅ `SPRINT_4_EXECUTION_PLAN.md` |
| User explicitly authorized post-deploy work | ✅ This PR is that authorization |

**Recommended next action**: implement Sprint 4 Stage 4.1 (lifespan
hook + admin endpoints) as one PR. Merge to main. Deploy to VPS. Flip
`AGENT_ORCHESTRATOR_ENABLED=true`. Soak 24h. Then proceed to 4.3
(NewsFetchAgent).

Total elapsed time from "now" to "first agent running in prod":
~3 working days + ~3 days of soak time = ~1 calendar week.
