# DEPLOYMENT_CHECKLIST.md

> Production deploy of Sprint 1–3 to the VPS (`/opt/zyvora` on Hostinger
> Ubuntu). Complements `ROLLOUT_CHECKLIST.md` (which covers merge to
> main). This file is the **VPS** half.
>
> **State at time of writing**: main is at `0a1bcf8` with Sprint 1–3
> merged. VPS is still on `3eaf7b1` (pre-rollout).

---

## Pre-deploy snapshot (mandatory)

```bash
ssh root@<vps>

cd /opt/zyvora
git log --oneline -1                    # capture this SHA — fallback target
git status --short                      # MUST be clean

# Tag the current prod state for easy revert
git tag pre-sprint-rollout-2026-05-19
git push origin pre-sprint-rollout-2026-05-19

# Snapshot the SQLite volume
docker compose -f docker-compose.prod.yml exec market-terminal \
  tar czf /tmp/db-pre-rollout-$(date +%Y%m%d-%H%M).tar.gz /app/db
docker cp market-terminal:/tmp/db-pre-rollout-*.tar.gz /opt/backups/
ls -lh /opt/backups/db-pre-rollout-*.tar.gz   # confirm non-empty
```

**Don't proceed without this snapshot.**

---

## Deploy

```bash
cd /opt/zyvora

# 1. Pull
git fetch origin
git log origin/main..HEAD                # should be empty (no local commits)
git pull --ff-only origin main           # fast-forward only
git log --oneline -5                     # confirm 3 new merge commits

# 2. Build
docker compose -f docker-compose.prod.yml build market-terminal
# expect: image rebuilds, layers mostly cached (just app code + COPY)

# 3. Restart
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal

# 4. Wait for lifespan + 8 background loops
sleep 60
```

---

## Smoke checks (each must pass)

```bash
# A. Health endpoints
curl -fsS http://localhost:8001/health
# expect: 200 OK with body

curl -fsS http://localhost:8001/api/health | jq '.status'
# expect: NOT "unhealthy"

# B. Sprint 2 — X-Request-ID header present
curl -sI http://localhost:8001/api/health | grep -i x-request-id
# expect: x-request-id: <12-hex-chars>

# C. Sprint 2 — request log line in JSON or console form
docker logs market-terminal --tail 20 | grep request_complete
# expect: at least one line; format depends on LOG_FORMAT

# D. Sprint 3 — orchestration NOT loaded (it's library only)
docker exec market-terminal python -c \
  "import sys; print('orchestration loaded' if 'orchestration' in sys.modules else 'orchestration NOT loaded')"
# expect: "orchestration NOT loaded"

# E. UI smoke
# Open https://zyvoratech.co in a browser
# - Login page renders
# - Login as admin succeeds
# - Dashboard loads with live data
# - Pick one frequently-used flow (e.g. signals, watchlist) — works
```

**If any check fails**: rollback (next section).

---

## Post-deploy observation (60 minutes minimum)

```bash
# Tail logs for new errors
docker logs -f market-terminal | jq 'select(.level == "ERROR" or .level == "WARNING")'

# OR if LOG_FORMAT=console:
docker logs -f market-terminal | grep -E "ERROR|WARNING"
```

Watch for:
- [ ] **No new ERROR lines** from `http.request`, `logging_*`, or any new logger
- [ ] **Latency**: spot-check `duration_ms` field on `request_complete` — should be in expected range (compare with pre-deploy if you have data)
- [ ] **Memory**: `docker stats market-terminal --no-stream` — same order of magnitude as pre-deploy
- [ ] **CPU**: same — middleware adds ~0.05ms/request, negligible at 10 req/s
- [ ] **Background loops**: existing `[REGIME]`, `[ALERTS]`, etc. `print()` lines still appearing (Sprint 1+2+3 don't touch them)

---

## Rollback procedures (3 levels)

### Level 1 — Env-var fallback (no redeploy, ~30s)

If logs are noisy or middleware behavior is unexpected:

```bash
# Edit /opt/zyvora/.env on the VPS:
LOG_FORMAT=console        # restore familiar plain text
LOG_HTTP_REQUESTS=false   # silence per-request line
UVICORN_ACCESS_LOG=on     # restore classic uvicorn access log

docker compose -f docker-compose.prod.yml restart market-terminal
```

Visual UX returns to pre-Sprint-2. `X-Request-ID` header still injected (cheap; useful).

### Level 2 — Revert one PR (~5 min including rebuild)

If a specific Sprint introduced a problem:

```bash
cd /opt/zyvora
git log --oneline -10                              # find the offending merge SHA
git revert -m 1 <merge-sha>                        # safe revert; preserves history
git push origin main                               # push the revert
docker compose -f docker-compose.prod.yml build market-terminal
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal

# verify
curl -fsS http://localhost:8001/api/health | jq '.status'
```

### Level 3 — Full rollback to pre-deploy tag (~10 min)

If everything's broken:

```bash
cd /opt/zyvora
git checkout pre-sprint-rollout-2026-05-19      # the tag from snapshot step
docker compose -f docker-compose.prod.yml build market-terminal
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal

# verify
curl -fsS http://localhost:8001/api/health | jq '.status'

# If DB volume needs restore:
docker cp /opt/backups/db-pre-rollout-*.tar.gz market-terminal:/tmp/
docker exec market-terminal tar xzf /tmp/db-pre-rollout-*.tar.gz -C /
docker compose -f docker-compose.prod.yml restart market-terminal
```

---

## Settings to confirm on the VPS `.env`

After deploy, ensure these are set correctly:

| Var | Recommended value | Why |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Default. Matches Sprint 2 expectations. |
| `LOG_FORMAT` | `console` | Sprint 2 default. Easier to read in `docker logs`. Flip to `json` only after a log shipper is in place. |
| `LOG_HTTP_REQUESTS` | `true` | Get per-request structured logs. Set to `false` only if log volume is a problem. |
| `UVICORN_ACCESS_LOG` | `off` | Avoid duplicate per-request lines. |
| `AGENT_ORCHESTRATOR_ENABLED` | (unset / `false`) | **Critical**: Sprint 4 won't start any agents anyway, but explicit is safer. |
| All other Sprint-1/2/3 settings | (unset → defaults) | Defaults are safe. |

---

## Log rotation (RECOMMENDED, can be done in this window)

While the VPS is in maintenance mode, apply the log rotation block from
`OBSERVABILITY_PLAN.md §3`:

```yaml
# docker-compose.prod.yml
services:
  market-terminal:
    # ... existing ...
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
        compress: "true"
```

```bash
# After editing:
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal
```

This caps Docker's log buffer at 250MB total. Strongly recommended given
Sprint 2 increases per-request log volume.

---

## Summary checklist (paper-thin version)

- [ ] Pre-deploy snapshot taken (DB + git tag)
- [ ] `git pull --ff-only`
- [ ] `docker compose build && up -d --force-recreate`
- [ ] Smoke A-E (health, X-Request-ID, log line, orchestration not loaded, UI)
- [ ] 60-minute observation passes
- [ ] Log rotation block applied (recommended)
- [ ] User notified

If any step fails: pick rollback Level 1 → 2 → 3 in that order.

---

## What this checklist deliberately does NOT cover

- Sprint 4 deploy (different concerns — feature-flag flips per stage)
- Database schema migrations (none in Sprint 1–3)
- Caddy config changes (none)
- TLS cert rotation (separate cadence)
- Backup cron setup (already exists per `setup-backup.sh`)
