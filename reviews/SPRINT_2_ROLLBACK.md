# SPRINT_2_ROLLBACK.md — Phase A Logging

> Working in: `~/ai-system/core/` (real repo: `mahendirank/ai-market-terminal`)
> Branch: `sprint-2/phase-a-logging` (off `sprint-1/safety-net`)
> Sprint 2 goal: structured logging + request correlation. **No production behavior changes.**
> Rule (carried from Sprint 1): prod = staging. Each milestone is reversible.

---

## Branch strategy

Sprint 2 stacks on Sprint 1:

```
main
  └─ sprint-1/safety-net  (6 commits, local-only)
       └─ sprint-2/phase-a-logging  ◄── all Sprint 2 commits
```

To abandon Sprint 2 only:
```bash
cd ~/ai-system/core
git checkout sprint-1/safety-net
git branch -D sprint-2/phase-a-logging
```

To abandon both Sprints 1 and 2:
```bash
cd ~/ai-system/core
git checkout main
git branch -D sprint-1/safety-net sprint-2/phase-a-logging
```

---

## Three-level rollback (fastest to safest)

### Level 1 — Env-var fallback (NO redeploy)

If JSON logs are noisy or middleware logging is too verbose:

```bash
# Edit /opt/zyvora/.env on the VPS:
LOG_FORMAT=console        # back to text — visually like pre-Sprint-2
LOG_HTTP_REQUESTS=false   # silence per-request log line
UVICORN_ACCESS_LOG=on     # restore uvicorn's classic plain-text access log

# Restart only:
docker compose -f docker-compose.prod.yml restart market-terminal
```

Time to recovery: ~30 seconds. **Zero code changes.**

The `X-Request-ID` header still gets injected (it's free); only logging output changes.

### Level 2 — Disable middleware only (one revert)

If the middleware itself causes a problem (unlikely but possible):

```bash
cd ~/ai-system/core
git log --oneline | grep "wire.*middleware"   # find the commit SHA
git revert <middleware-wire-sha>
```

This reverts the `app.add_middleware(RequestContextMiddleware)` line in `dashboard_api.py` only. `logging_config.py` and the middleware module remain — harmless.

Time to recovery: ~5 minutes including image rebuild + redeploy.

### Level 3 — Full Sprint 2 rollback

```bash
cd ~/ai-system/core
# Either:
git checkout sprint-1/safety-net   # if not yet merged to main
# OR if pushed:
git revert <oldest-sprint-2-sha>..HEAD --no-commit
git commit -m "revert: Sprint 2 Phase A logging"
```

Result: tree identical to post-Sprint-1. All 102 Sprint-1 tests still pass.

---

## Milestones + their rollback details

### M1 — Logging core (`logging_config.py`)

- **What lands**: new file `core/logging_config.py` (236 lines). No callers yet.
- **Production impact**: zero. File exists but isn't imported until M3.
- **Rollback**: `git revert <sha>` — file deleted; nothing references it.

### M2 — Middleware (`logging_middleware.py`)

- **What lands**: new file `core/logging_middleware.py` (~100 lines). No callers yet.
- **Production impact**: zero until M4 wires it into FastAPI.
- **Rollback**: `git revert <sha>`.

### M3 — Wire setup_logging into `run.py`

- **What lands**: 3 lines added inside `__main__` block, before `uvicorn.run()`. `setup_logging()` is idempotent.
- **Production impact**: **medium** — when prod restarts, structured logging kicks in. With default `LOG_FORMAT=console`, output looks similar to before. Setting `LOG_FORMAT=json` switches to JSON.
- **Mitigation**: default is `console`. Switching format requires an env-var change, not a code change.
- **Rollback**: `git revert <sha>` — uvicorn launches without our setup; existing log behavior restored.

### M4 — Wire middleware into `dashboard_api.py`

- **What lands**: 2 lines added after RateLimitMiddleware registration. Middleware sees every HTTP request.
- **Production impact**: **medium-low** — `X-Request-ID` header in every response; one log line per request; ~0.05ms latency overhead.
- **Mitigation**: `LOG_HTTP_REQUESTS=false` silences the log line; the header and ContextVar still work.
- **Rollback**: `git revert <sha>`.

### M5 — Tests + .env.production.example

- **What lands**: 4 new test files (17 tests); 4 new env-var entries documented.
- **Production impact**: zero — tests don't run in prod; `.env.production.example` is a template only.
- **Rollback**: `git revert <sha>`.

### M6 — Documentation (LOGGING_STANDARD update + OBSERVABILITY_PLAN + this file)

- **What lands**: 3 docs files in `reviews/`.
- **Production impact**: zero.
- **Rollback**: `git revert <sha>`.

---

## Push policy (same as Sprint 1)

Nothing pushed to `origin/sprint-2/phase-a-logging` until the user explicitly approves. Local-only by default.

Nothing merged to `main` until:
1. M1–M6 all landed locally ✅
2. Sprint 1 itself is merged to `main` first (Sprint 2 stacks on it)
3. Local `pytest -m smoke` is green ✅ (121 tests passing as of close-of-Sprint-2)
4. User explicitly approves the merge

---

## Emergency: prod is broken and Sprint 2 is suspected

1. **Try env vars first** (Level 1 above): set `LOG_FORMAT=console`, `LOG_HTTP_REQUESTS=false`, `UVICORN_ACCESS_LOG=on`; restart. 90% of "Sprint 2 broke logging" symptoms vanish here.

2. **If still broken**: roll back to the merge commit BEFORE Sprint 2 landed:
   ```bash
   ssh root@<vps>
   cd /opt/zyvora
   git log --oneline -20  # find the pre-Sprint-2 SHA
   git checkout <pre-sprint-2-sha>
   docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal
   ```

3. **Verify**:
   ```bash
   curl -s http://localhost:8001/api/health | jq .
   docker logs market-terminal | tail -50    # should look like pre-Sprint-2
   ```

If `git pull` was never run on the VPS after Sprint 2 merge, step 2 is a no-op — the VPS is already at the safe state.

---

## What is NOT rolled back by any of the above

- Sprint 1 changes (tests, CI, deps). Those stay regardless.
- The outer `ai-system/` repo's submodule pointer drift. Same as it was.
- `reviews/` documents (Phase 1, Sprint 1, Sprint 2). They're docs; they stay even after revert.

---

## Verified rollback paths

Each rollback path above has been considered but **not executed during Sprint 2** — they exist for the user's emergency use, not as a test. To actually verify a rollback path, run the steps on a non-prod environment first. Sprint 6+ scale-out planning includes standing up a staging env so these can be tested end-to-end.
