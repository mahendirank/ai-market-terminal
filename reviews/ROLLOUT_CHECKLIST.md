# ROLLOUT_CHECKLIST.md

> The exact sequence to land Sprint 1–3 into production. Each item has a
> definition of done and a rollback if something goes wrong.

---

## 0. Pre-flight (one-time, before any merge)

- [ ] **Confirm CI is green on all three branches**
  - `gh run list --branch sprint-1/safety-net --limit 1` → success
  - `gh run list --branch sprint-2/phase-a-logging --limit 1` → success
  - `gh run list --branch sprint-3/orchestration-foundation --limit 1` → success
  - Verified 2026-05-19: 176 / 195 / 262 tests pass remotely.
- [ ] **Confirm working tree is clean on local branches**
  - `git status` on each branch → "nothing to commit"
- [ ] **Confirm rollback files exist for each sprint**
  - `ls reviews/SPRINT_{1,2,3}_ROLLBACK.md` → all 3 present
- [ ] **VPS access verified**
  - SSH login works
  - `cd /opt/zyvora && git status` clean
- [ ] **VPS has a known-good fallback commit on `main`**
  - Tag the current HEAD: `git tag pre-sprint-rollout $(git rev-parse HEAD)` (on local)
  - Push the tag: `git push origin pre-sprint-rollout`

**Rollback if pre-flight fails**: do not proceed. Investigate.

---

## 1. Merge to `main` (in order, with verification gates)

### 1.1 Merge sprint-1/safety-net → main

- [ ] `gh pr create --base main --head sprint-1/safety-net --title "Sprint 1: safety net"`
- [ ] Self-review the PR diff on GitHub — confirm:
  - No code changes outside `tests/`, `scripts/`, `reviews/`, requirements.txt, `.github/workflows/`, `engine.py` (deletion), `run.py`, `claude_bridge.py`
  - 102 smoke tests added; 176 total pass on CI
- [ ] Merge PR (squash NOT recommended — preserve the 6 milestone commits for revertability)
- [ ] Pull on local: `git checkout main && git pull`
- [ ] Run `pytest -m smoke` locally → green

**Rollback if PR merge causes a problem**:
```bash
git revert -m 1 <merge-commit-sha>
git push
```
The 6 commits are also individually revertible via `git revert <sha>`.

### 1.2 Merge sprint-2/phase-a-logging → main

- [ ] Rebase or merge `main` into sprint-2: `git checkout sprint-2/phase-a-logging && git merge main` (should be a no-op if sprint-2 is stacked correctly)
- [ ] `gh pr create --base main --head sprint-2/phase-a-logging --title "Sprint 2: Phase A logging"`
- [ ] Self-review:
  - `dashboard_api.py` modification limited to ONE `app.add_middleware(RequestContextMiddleware)` line
  - `run.py` modification limited to two `setup_logging` lines inside `__main__`
  - `.env.production.example` adds 4 LOG_* env vars
  - Default `LOG_FORMAT=console` preserves visual UX
- [ ] Merge PR
- [ ] Local pull + `pytest -m smoke` → 121 green

**Rollback options** (per SPRINT_2_ROLLBACK §1):
- Level 1 (env-only, no redeploy): set `LOG_FORMAT=console`, `LOG_HTTP_REQUESTS=false`, `UVICORN_ACCESS_LOG=on` in `.env` and restart container.
- Level 2: revert middleware commit only.
- Level 3: revert all 6 Sprint-2 commits.

### 1.3 Merge sprint-3/orchestration-foundation → main

- [ ] Rebase/merge `main` into sprint-3 (no-op if stacked)
- [ ] `gh pr create --base main --head sprint-3/orchestration-foundation --title "Sprint 3: orchestration foundation (library only)"`
- [ ] Self-review:
  - All new code in `orchestration/` package
  - NO modifications to `dashboard_api.py`, `run.py`, `requirements.txt`, Dockerfile
  - 67 new tests added; 262 total pass on CI
- [ ] Merge PR
- [ ] Local pull + `pytest -m smoke` → 188 green

**Rollback** (per SPRINT_3_ROLLBACK §3):
- Level 2 (one delete) is sufficient — nothing in prod imports `orchestration` yet.

---

## 2. Deploy to VPS

### 2.1 Pre-deploy snapshot

- [ ] On VPS: `cd /opt/zyvora && git log --oneline -1` — note the SHA as `LAST_GOOD_SHA`
- [ ] On VPS: backup the running database volume
  ```bash
  docker compose -f docker-compose.prod.yml exec market-terminal tar czf /tmp/db-pre-rollout.tar.gz /app/db
  docker cp market-terminal:/tmp/db-pre-rollout.tar.gz /opt/backups/
  ```
- [ ] Verify backup file exists and is non-empty.

### 2.2 Pull + rebuild

- [ ] On VPS: `git pull` (now includes Sprint 1–3 merges)
- [ ] On VPS: `docker compose -f docker-compose.prod.yml build market-terminal`
- [ ] Verify build succeeds (image tag updated)

### 2.3 Restart + verify

- [ ] On VPS: `docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal`
- [ ] Wait 60s for `lifespan` startup + 8 background loops to come up
- [ ] **Smoke checks (each must pass before continuing):**
  - [ ] `curl -s http://localhost:8001/health` → 200
  - [ ] `curl -sf http://localhost:8001/api/health | jq '.status'` → not "unhealthy"
  - [ ] Open `https://zyvoratech.co` in a browser → login page renders
  - [ ] Log in as admin → dashboard loads
  - [ ] `docker logs market-terminal --tail 100` → no `ERROR` lines from the new code paths (Sprint 1+2; Sprint 3 isn't loaded)
  - [ ] `curl -s -I http://localhost:8001/api/health | grep -i x-request-id` → header present (Sprint 2 middleware live)

**If any smoke check fails**:
```bash
cd /opt/zyvora
git checkout $LAST_GOOD_SHA
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal
# Verify with: curl http://localhost:8001/api/health
```

### 2.4 First-hour observation

- [ ] Tail logs for 60 minutes: `docker logs -f market-terminal | jq 'select(.level=="ERROR" or .level=="WARNING")'`
- [ ] **Error rate**: zero or near-zero new ERROR lines from `http.request`, `agents.*`, `logging_*` loggers
- [ ] **Latency**: `jq 'select(.msg=="request_complete") | .duration_ms' | sort -n | tail -10` → no order-of-magnitude regressions
- [ ] **Memory**: `docker stats market-terminal --no-stream` → similar to pre-rollout snapshot
- [ ] User-visible: spot-check 2–3 frequently-used UI flows

If anomalies appear → Level 1 env rollback first; if persistent, Level 3.

---

## 3. Post-merge cleanup

- [ ] Delete remote sprint branches (optional, history is preserved in main):
  ```bash
  gh api -X DELETE /repos/mahendirank/ai-market-terminal/git/refs/heads/sprint-1/safety-net
  gh api -X DELETE /repos/mahendirank/ai-market-terminal/git/refs/heads/sprint-2/phase-a-logging
  gh api -X DELETE /repos/mahendirank/ai-market-terminal/git/refs/heads/sprint-3/orchestration-foundation
  ```
- [ ] Update local: `git branch -D sprint-1/safety-net sprint-2/phase-a-logging sprint-3/orchestration-foundation`
- [ ] Verify the outer `~/ai-system/` repo's pointer to `core` is correct
  - This is TECH_DEBT §14; cleanup is Sprint 4+ work.

---

## 4. Stop conditions (do NOT proceed past these)

If any of the following are true, halt the rollout and investigate before resuming:

- [ ] CI red on any sprint branch
- [ ] Local `pytest -m smoke` fails after a merge
- [ ] Healthcheck fails for >30s after restart
- [ ] Memory in the new image exceeds 1.5× the pre-rollout baseline
- [ ] Any new ERROR-level log line not previously seen
- [ ] User reports a regression in any feature

---

## 5. Communication

- [ ] Before rollout: mention in CLAUDE.md or a one-liner to the user — "starting rollout, ETA 30 min"
- [ ] After rollout: short summary — what merged, what changed externally (X-Request-ID header, possibly different log format if `LOG_FORMAT=json` set)

---

## 6. Expected runtime

| Step | Estimated time |
|---|---|
| 1.1 Sprint-1 PR + merge | 5 min |
| 1.2 Sprint-2 PR + merge | 5 min |
| 1.3 Sprint-3 PR + merge | 5 min |
| 2.1 Snapshot | 2 min |
| 2.2 Pull + rebuild | 5–10 min |
| 2.3 Restart + smoke | 5 min |
| 2.4 Observation | 60 min (passive) |
| **Total active time** | **~30 min** |
| **Total wall-clock** | **~90 min** (incl. observation) |
