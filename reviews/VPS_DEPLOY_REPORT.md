# VPS_DEPLOY_REPORT.md

> Foundation deployment to production VPS, 2026-05-19. Sprint 1–3 only.
> Orchestration flags remain OFF. **Deployment successful.**

---

## Deployment summary

| Aspect | Value |
|---|---|
| VPS hostname | `srv1664231` (Hostinger, IP 72.61.173.89) |
| Install path | `/opt/zyvora` |
| Pre-deploy SHA | `cb63f899f4956a887bf00e2fe962e0f6d8af20d1` *(VPS-local commit, never pushed to GitHub)* |
| Post-deploy SHA | `fcba9038fd83da8081ebfc923302369fac66997f` *(matches origin/main HEAD)* |
| Commits applied | 33 (Sprint 1 + Sprint 2 + Sprint 3 + stabilization) |
| Old container ID | `d78f1a672d40d860460167a4b32274fbe91ba0a2af1163ee81b711293177fe98` |
| New container ID | `0f5149d511f025e89e2d990f63c324909a886002c9d2b24a58c3bd05d37830f0` |
| Rollback tag | `pre-sprint-rollout-2026-05-19` (local on VPS only — see note below) |
| Rollback DB snapshot | `/opt/backups/db-pre-rollout-2026-05-19_0511.tar.gz` (661K, 24 SQLite files) |
| Downtime | ~30 seconds (between force-recreate and "healthy") |
| Deployment wall-clock | ~6 minutes (excluding observation) |

---

## Step-by-step timeline (UTC)

| Step | Time (UTC) | What happened |
|---|---|---|
| 1. SSH connect | 05:08 | Confirmed SSH key auth works to `root@72.61.173.89` |
| 2. Pre-deploy recon | 05:09 | Baseline: SHA, containers, .env keys, disk, Redis |
| 3. DB snapshot | 05:11 | Tarred named volume `zyvora_terminal_db` from host (`/var/lib/docker/volumes/zyvora_terminal_db/_data` → `/opt/backups/db-pre-rollout-2026-05-19_0511.tar.gz`) |
| 4. Git tag | 05:11 | `git tag -a pre-sprint-rollout-2026-05-19 cb63f899...` |
| 5. Git fetch + pull --ff-only | 05:14 | 33 commits pulled. New HEAD: `fcba9038`. All Sprint 1/2/3 sentinel files now present on disk. |
| 6. Docker rebuild | 05:15 | `docker compose build market-terminal` succeeded. New image `b6bec2ba717b` (1.18GB). Old container still serving. |
| 7. Force-recreate | 05:16:49 | `docker compose up -d --force-recreate market-terminal`. Container restart. |
| 8. Health came up | 05:17:50 | Container reported `healthy` at t≈60s post-restart |
| 9. Validation suite | 05:17–05:18 | 10 validation checks ran; all passed |

---

## What landed in production

| Sprint | What's now active in the container |
|---|---|
| 1 | `pytest`/`ruff`/`pre-commit` configs on disk; `engine.py` removed; `run.py` and `claude_bridge.py` wrapped in `__main__` guards; `requirements.txt` includes `python-pptx`. |
| 2 | **`logging_config.setup_logging()` is called at startup**; **`RequestContextMiddleware` is wired into FastAPI**. Every HTTP response now carries `X-Request-ID`. Every log line emitted via stdlib `logging` carries `request_id` in console format. |
| 3 | `orchestration/` package is on disk but **NOT imported** by any running code. Verified: `'orchestration' in sys.modules == False` in a fresh subprocess. |

---

## What did NOT change

- **`requirements.txt`**: only `python-pptx` added (Sprint 1). No new dependencies in Sprint 2 or 3.
- **No new env vars set in `.env`**: Sprint 2 vars use code defaults (`LOG_FORMAT=console`, `LOG_HTTP_REQUESTS=true`, `UVICORN_ACCESS_LOG=off`). No `AGENT_*` flags exist.
- **No data-store changes**: 18 SQLite DBs untouched; Redis state intact (post-deploy `/api/health` reports `redis.ok=true, latency_ms=3`).
- **No Caddy config changes**: external HTTPS routing unchanged.

---

## Note: pre-existing tag/SHA divergence

The pre-deploy SHA on the VPS was `cb63f899...` — a commit that does **not exist on GitHub origin**. This means the VPS has historically had local-only commits, possibly from direct edits or unpushed work. The fast-forward pull succeeded, so this didn't block the deploy.

**Implication**: any rollback must be done **on the VPS itself** using the local tag `pre-sprint-rollout-2026-05-19` — the tag could not be pushed to GitHub since the SHA isn't there.

**Follow-up for tech debt**: align VPS state with GitHub by either:
- Pushing `cb63f899` to a `backup/vps-pre-rollout` branch on GitHub (so the SHA exists upstream), OR
- Cherry-pick any unique commits from `cb63f899` onto `main` and discard the parallel history.

This is NOT a Sprint-4 blocker; flagging for a future cleanup.

---

## Resource usage post-deploy

| Metric | Pre-deploy | Post-deploy (t+30s) |
|---|---|---|
| market-terminal memory | 781.1 MiB | 288.8 MiB |
| market-terminal CPU | 31.4% | 10.65% |
| Redis memory | unknown | 1.59 MiB (post-deploy reports it) |
| Container restart count | n/a | 0 (single clean start) |
| Host free RAM | 956 MiB + 12 GiB cached | (similar) |
| Host disk used | 44/193 GB (23%) | (same — image rebuild reused most layers) |

Lower memory post-deploy is expected: the previous container had been running 3 days and accumulated state; the new container is fresh.

---

## Authorization context

This deploy was performed by Claude Opus 4.7 (1M context) at the user's explicit direction ("Proceed with controlled VPS deployment for Sprint 1–3 foundation only"). All 10 deployment requirement steps from the user's instructions were executed in sequence. The `AGENT_*` flags remain unset (defaults to OFF in code).

---

## Rollback path (if needed within 24h)

```bash
ssh root@72.61.173.89
cd /opt/zyvora
git checkout pre-sprint-rollout-2026-05-19   # the local tag created at step 4
docker compose -f docker-compose.prod.yml build market-terminal
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal

# If DB needs restore (unlikely — Sprint 1-3 didn't touch DBs):
docker compose -f docker-compose.prod.yml down market-terminal
tar xzf /opt/backups/db-pre-rollout-2026-05-19_0511.tar.gz \
  -C /var/lib/docker/volumes/zyvora_terminal_db/_data
docker compose -f docker-compose.prod.yml up -d
```

Estimated recovery time: 5 minutes.
