# SPRINT_1_ROLLBACK.md — Safety Net + Rollback Log

> Working in: `~/ai-system/core/` (real repo: `mahendirank/ai-market-terminal`)
> Branch: `sprint-1/safety-net` (off `main`)
> Sprint 1 goal: build a safety net — tests, CI, pinned deps. **No production behavior changes.**
> Rule: prod = staging, so every commit is potentially live. Each milestone below is reversible.

---

## Branch strategy

All Sprint 1 work is on `sprint-1/safety-net`. `main` is untouched.

```
main (prod tracks this)
  └─ sprint-1/safety-net  ◄── all Sprint 1 commits
```

To abandon Sprint 1 entirely at any point:

```bash
cd ~/ai-system/core
git checkout main
git branch -D sprint-1/safety-net  # destroys the branch
```

To selectively keep some Sprint 1 work after abandoning the rest, cherry-pick the desired commits onto `main`.

---

## Milestones + rollback instructions

Each milestone is one commit. Reverting one milestone never breaks another.

### M1 — Safety baseline (this file + requirements snapshot)
- **What lands**: `reviews/SPRINT_1_ROLLBACK.md`, `reviews/` (relocated from outer dir), `requirements.baseline.txt` (frozen snapshot of current `requirements.txt`).
- **Production impact**: zero. Pure additive docs + a frozen reference file.
- **Rollback**: `git revert <M1-sha>` — the docs vanish; nothing else changes.

### M2 — Dependency pinning
- **What lands**: `requirements.txt` gains missing packages (`anthropic`, `groq`, possibly `openai`). New `requirements-lock.txt` with pinned versions. `Dockerfile` updated to `pip install -r requirements-lock.txt` (verify the change). `requirements.txt` retained as the unpinned spec.
- **Production impact**: **medium**. Next image build uses pinned deps. If a pin is wrong, the build fails or runtime behavior shifts.
- **Mitigation**: build the image locally before any deploy. Compare `pip list` inside the new image vs. `requirements.baseline.txt`.
- **Rollback**:
  1. `git revert <M2-sha>` — restores old `requirements.txt` and removes lockfile + Dockerfile change.
  2. Rebuild image: `docker compose -f docker-compose.prod.yml build --no-cache market-terminal`.
  3. On VPS: `cd /opt/zyvora && git pull && docker compose -f docker-compose.prod.yml up -d`.

### M3 — CI/CD baseline (advisory)
- **What lands**: `.github/workflows/ci.yml` (runs ruff + pytest + docker build on PRs), `.pre-commit-config.yaml`, `ruff.toml`.
- **Production impact**: zero — CI runs in GitHub, not on the VPS. Configured as **advisory** (no required status checks) so a CI failure cannot block an emergency `git push` to main.
- **Rollback**: `git revert <M3-sha>` — workflows vanish. No prod state to undo.

### M4 — Test framework + 5 baseline tests
- **What lands**: `pyproject.toml` with `[tool.pytest.ini_options]`, `tests/test_health_smoke.py`, `tests/test_auth_login.py`, `tests/test_alert_engine_cooldown.py`, `tests/test_signal_memory_roundtrip.py`, `tests/test_tenants_isolation.py`. Possibly `tests/conftest.py` with shared fixtures.
- **Production impact**: zero — test files aren't loaded by `dashboard_api`.
- **Rollback**: `git revert <M4-sha>` — tests vanish. No prod state to undo.

### M5 — engine.py decision
- **What lands**: either `git rm engine.py` (if unused) or a one-line import fix.
- **Production impact**: zero if unused (most likely). One-line if used — verify import resolves.
- **Rollback**: `git revert <M5-sha>` — restores file or original import.

### M6 — Logging audit doc (no code changes)
- **What lands**: `reviews/LOGGING_STANDARD.md` proposing a logging standard. Inventory of current `print()` / `logging` / `sys.stderr` usage.
- **Production impact**: zero — docs only. No code touched.
- **Rollback**: `git revert <M6-sha>`.

### M7 — Sprint 1 wrap-up
- **What lands**: `reviews/SPRINT_1_OUTCOMES.md`. Updates to `reviews/IMPROVEMENT_ROADMAP.md` Sprint 2 section.
- **Production impact**: zero — docs only.
- **Rollback**: `git revert <M7-sha>`.

---

## Push policy

**Nothing is pushed to `origin/sprint-1/safety-net` until the user explicitly approves.**
After local commits accumulate, propose the push as a single action so the user can review the full diff range.

**Nothing is merged to `main` until**:
1. M1–M7 all landed locally
2. Local `pytest` is green
3. Local `docker compose build` succeeds
4. Outer `ai-system/` repo's gitlink pointer is updated (or the embedded-repo P1 in TECH_DEBT §14 is resolved)
5. User explicitly approves the merge

---

## Emergency: prod is broken and Sprint 1 is suspected

```bash
# 1. SSH to VPS
ssh root@<vps>

# 2. Revert to main on the prod deploy
cd /opt/zyvora
git log --oneline -10  # find the last known-good commit on main
git checkout <known-good-sha>

# 3. Restart
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal

# 4. Verify
curl -s http://localhost:8001/api/health | jq .
```

The VPS's local repo state diverges from origin only if `git pull` was run on it after Sprint 1 work merged. As long as `main` on origin stays clean, `git pull` on the VPS is a no-op.
