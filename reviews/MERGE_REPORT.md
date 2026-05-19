# MERGE_REPORT.md — Sprint 1–3 → main

> Foundation Consolidation Phase, 2026-05-19.

---

## Merges executed

| Order | PR | Source branch | Merge commit | CI | Tests |
|---|---|---|---|---|---|
| 1 | [#1](https://github.com/mahendirank/ai-market-terminal/pull/1) | sprint-1/safety-net | `7f927a7` | ✅ success (1m20s) | 176 pass |
| 2 | [#2](https://github.com/mahendirank/ai-market-terminal/pull/2) | sprint-2/phase-a-logging | `34524bf` | ⚠️ cancelled* | 195 pass (pre-merge) |
| 3 | [#3](https://github.com/mahendirank/ai-market-terminal/pull/3) | sprint-3/orchestration-foundation | `0a1bcf8` | ✅ success (1m7s) | 262 pass |

\* PR #2's CI run was cancelled by the `concurrency: cancel-in-progress` rule when PR #3's push landed seconds later. Sprint 3's CI run includes all Sprint 2 code, so the cumulative state IS validated. Pre-merge sprint-2 branch CI also passed independently (195 tests verified 2026-05-19 03:28).

**Net result**: `main` advanced from `3eaf7b1` → `0a1bcf8` via 3 merge commits, no rebases, all milestone commits preserved for revertability.

---

## Local main verification

```
$ git checkout main && git pull --ff-only origin main
$ pytest -m smoke --no-header -q
188 passed, 74 deselected in 36.71s

$ pytest --no-header -q     # full suite, matches CI
262 passed in 35.11s
```

---

## What's in main now

```
main HEAD: 0a1bcf8

Recent history (first-parent):
  0a1bcf8  Merge pull request #3 — Sprint 3: orchestration foundation
  34524bf  Merge pull request #2 — Sprint 2: Phase A logging
  7f927a7  Merge pull request #1 — Sprint 1: safety net
  3eaf7b1  Patch stale Yahoo tickers in earnings panel  ← pre-rollout main
  ...
```

Files added since pre-rollout:
- `logging_config.py` (Sprint 2)
- `logging_middleware.py` (Sprint 2)
- `orchestration/` (Sprint 3 — 7 modules + __init__)
- `tests/test_*.py` (Sprint 1: 4 files; Sprint 2: 2 files; Sprint 3: 8 files)
- `.github/workflows/ci.yml` (Sprint 1)
- `pyproject.toml`, `ruff.toml`, `.pre-commit-config.yaml` (Sprint 1)
- `requirements.baseline.txt` (Sprint 1)
- `scripts/pin-deps.sh` (Sprint 1)
- `scripts/sim/sim_*.py` (Sprint 3 — 4 files)
- `reviews/*.md` (16 files spanning Phase 1, Sprint 1, Sprint 2, Sprint 3)
- `reviews/diagrams/*.md` (4 diagrams)

Files modified since pre-rollout:
- `requirements.txt` (+ python-pptx)
- `run.py` (+ setup_logging + __main__ guard)
- `claude_bridge.py` (+ __main__ guard)
- `dashboard_api.py` (+ 1 line: RequestContextMiddleware registration)
- `.env.production.example` (+ 4 LOG_* env vars)

Files deleted:
- `engine.py` (orphan, broken import — Sprint 1)

---

## What did NOT change

- **No imports of `orchestration` from any production module.** Verified:
  ```
  $ grep -rn "from orchestration\|import orchestration" *.py | grep -v test_
  (empty)
  ```
- **`dashboard_api.py` lifespan unchanged** — the orchestrator is not running.
- **No new external dependencies in `requirements.txt`** except `python-pptx`.
- **No agent autostart.** Sprint 4 will be the first sprint where orchestration code executes in the lifespan.

---

## Rollback availability

| Level | Action | Recovery time |
|---|---|---|
| L1 — env var | `LOG_FORMAT=console`, `LOG_HTTP_REQUESTS=false`, restart container | ~30s |
| L2 — revert one PR | `git revert -m 1 <merge-sha>` for PR #3, #2, or #1 | ~5min (rebuild + deploy) |
| L3 — revert all | `git revert -m 1 0a1bcf8 34524bf 7f927a7` (back to `3eaf7b1`) | ~10min |
| L4 — checkout pre-rollout | `git reset --hard 3eaf7b1` (DESTRUCTIVE — only if origin/main rewound) | n/a |

---

## Open items before VPS deploy

The merge to `main` is **complete**. The VPS at `/opt/zyvora` has NOT been updated. Steps remaining (per `ROLLOUT_CHECKLIST.md §2`):

- [ ] SSH to VPS, snapshot DB volume
- [ ] `git pull` on `/opt/zyvora`
- [ ] `docker compose build` + `up -d --force-recreate market-terminal`
- [ ] Smoke checks: `/health`, `/api/health`, login flow
- [ ] 60-minute observation window
- [ ] (Optional) flip `LOG_FORMAT=json` once log shipper exists

These can be done at the user's convenience — there's no auto-deploy.
