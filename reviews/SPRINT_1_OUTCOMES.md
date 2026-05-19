# SPRINT_1_OUTCOMES.md — Safety Net

> Sprint 1 closed: 2026-05-18.
> Branch: `sprint-1/safety-net` (in `~/ai-system/core/` — the real repo `mahendirank/ai-market-terminal`).
> Status: **complete, awaiting user review before push/merge**.

---

## What landed

### Files added (new)

| Path | Purpose |
|---|---|
| `pyproject.toml` | Pytest config |
| `ruff.toml` | Lint config (permissive Sprint 1 ruleset) |
| `.pre-commit-config.yaml` | Pre-commit hooks (ruff + detect-secrets) |
| `.github/workflows/ci.yml` | Advisory CI (lint + test + docker build) |
| `requirements.baseline.txt` | Frozen snapshot of pre-Sprint-1 requirements |
| `scripts/pin-deps.sh` | Generates `requirements-lock.txt` from the running prod container |
| `tests/test_module_imports.py` | Subprocess-based import test — every `*.py` must import cleanly |
| `tests/test_auth_crypto.py` | Password hash roundtrip + garbage-input safety |
| `tests/test_alert_format.py` | `_fmt_alert` structure |
| `tests/test_health_route_grep.py` | Dockerfile healthcheck path must exist in `dashboard_api.py` |
| `reviews/SPRINT_1_ROLLBACK.md` | Per-milestone rollback playbook |
| `reviews/LOGGING_STANDARD.md` | Phase A/B logging migration proposal |
| `reviews/SPRINT_1_OUTCOMES.md` | This file |

### Files modified

| Path | Change |
|---|---|
| `requirements.txt` | Added `python-pptx` (was missing; consumed by `executor.py` → `ppt_generator.py`) |
| `run.py` | Wrapped `uvicorn.run(...)` + prints in `if __name__ == "__main__":` — makes the module safely importable. Production CMD `python run.py` continues to work unchanged. |
| `claude_bridge.py` | Wrapped module-level `input()` block in `if __name__ == "__main__":` — same rationale |
| `reviews/TECH_DEBT_REPORT.md` | Added §14 (embedded-repo wrapper, P1) — discovered in pre-flight |

### Files deleted

| Path | Reason |
|---|---|
| `engine.py` | Orphan (no callers) + broken import (`build_system_prompt` not in `loader.py`) + missing transitive dep (`anthropic` not in requirements). Dead code in three ways. |

### Files relocated

| From | To |
|---|---|
| `~/ai-system/reviews/` (outer repo, untracked) | `~/ai-system/core/reviews/` (real repo) |

---

## Test results

```
pytest -m smoke
102 passed in 32.26s
```

- 102 smoke tests (one per `*.py` import + 6 unit tests)
- 0 failures, 0 skips
- 74 deselected (pre-existing stage tests, not @smoke — rehab is Sprint 2)

Test files write to `tmp_path` so they don't pollute `core/db/`.

---

## New findings discovered during Sprint 1

| # | Finding | Severity | Where logged |
|---|---|---|---|
| A | `core/` is an embedded git repo with no `.gitmodules` registration; outer `ai-system/` repo has stale pointer (5 commits behind) | P1 | TECH_DEBT_REPORT §14 |
| B | `run.py` ran `uvicorn.run()` at module level → unsafe to import (test process bound port 8001) | (fixed) | this doc |
| C | `claude_bridge.py` ran `input()` at module level → blocked on EOF | (fixed) | this doc |
| D | `groq` was a false positive in the missing-deps audit — it's the local module `groq_research`, not the `groq` SDK | (no-op) | this doc |
| E | `newspaper3k` and `tvdatafeed` are intentional best-effort installs in `Dockerfile` (`|| true`) — promoting them to `requirements.txt` would change failure mode | (deferred) | LOGGING_STANDARD considerations |
| F | 8 modules use `logging.getLogger().debug(...)` but produce **zero output in prod** because no central handler is configured | P2 | LOGGING_STANDARD §"What's there today" |
| G | Dockerfile uses **Python 3.11**, local dev uses **Python 3.13.6** — a locally-generated lockfile may be incompatible with prod | P2 | `scripts/pin-deps.sh` works around it |
| H | `mt5_bot.py` is orphan; consumed `MetaTrader5` dep that's never installed | P3 | this doc |
| I | `pipeline.py` is orphan | P3 | this doc |
| J | Two health endpoints exist (`/health` and `/api/health`) — Dockerfile uses `/health`, PRODUCTION.md describes `/api/health` | P3 | covered by `test_health_route_grep.py` |

---

## Production behavior — unchanged

| Aspect | Verified? | How |
|---|---|---|
| `python run.py` still launches uvicorn on :8001 | by inspection | `if __name__ == "__main__":` guard wraps the same code |
| `requirements.txt` install order | by inspection | Added one line at the end; existing entries untouched |
| Docker build process | not run in Sprint 1 | CI's docker-build job will verify on next push |
| Live API routes | not tested live | Out of Sprint 1 scope (no staging env) |

No code path in `dashboard_api.py`, `auth.py`, `alert_engine.py`, `signal_memory.py`, or any data-source module was modified.

---

## What's still deferred (intentionally)

| Item | Why deferred | When |
|---|---|---|
| Generate `requirements-lock.txt` | Needs running prod container | User runs `bash scripts/pin-deps.sh` once container is up |
| Resolve outer-repo / embedded-repo state | Cross-cutting rename — Sprint 2 | After user OKs rename to `zyvora-terminal` |
| Convert 328 `print()` calls to `logging` | Phase B of LOGGING_STANDARD | Sprint 3, module by module |
| Per-tenant log enrichment | Needs FastAPI middleware | After `dashboard_api.py` split (Sprint 3) |
| Rehab existing `tests/test_stage{2,3,4,5}.py` | They need fixtures + DB setup | Sprint 2 |
| `dashboard_api.py` route-group split | Destructive refactor | Sprint 3 (after Sprint 2 package layout) |
| Postgres migration for hot SQLite tables | Scale concern, not today | Sprint 4+ when concurrent users justify it |

---

## Next steps for the user

In order:

1. **Review the diff** — `cd ~/ai-system/core && git status --short && git diff --stat`
2. **Commit incrementally** (proposed structure — 5 commits):
   - `chore: relocate phase-1 reviews into core/` (M1, includes `reviews/` + `SPRINT_1_ROLLBACK.md` + `requirements.baseline.txt`)
   - `chore(deps): add python-pptx; pin-deps script for prod image` (M2)
   - `ci: advisory CI workflow + ruff + pre-commit + pytest config` (M3 + part of M4)
   - `test: smoke tests for imports, auth crypto, alert format, health route` (M4)
   - `fix: remove orphan engine.py; guard run.py + claude_bridge.py with __main__` (M5)
   - `docs: LOGGING_STANDARD + SPRINT_1_OUTCOMES` (M6 + M7)
3. **Optional**: `git push -u origin sprint-1/safety-net` so CI runs.
4. **DO NOT merge to `main`** until staging exists OR user explicitly accepts the production = staging risk.

I'll wait for your explicit go-ahead before committing or pushing.

---

## Sprint 2 — proposed scope (informed by Sprint 1 findings)

In priority order, based on what Sprint 1 surfaced:

1. **Resolve embedded-repo state** (TECH_DEBT §14, Sprint 1 finding A) — rename + restructure. **User approval already given for `zyvora-terminal` rename.**
2. **Phase A logging config** (LOGGING_STANDARD §"Phase A") — turn on the 8 modules' silent `log.debug` and unify format. ~1 day.
3. **Rehab existing stage tests** — `tests/test_stage{2,3,4,5}.py`. Add the missing fixtures so CI runs them.
4. **Generate `requirements-lock.txt`** — run `scripts/pin-deps.sh` against the prod container. ~5 min.
5. **Tighten ruff** — re-enable F401 (unused imports), F841 (unused vars), and add `I` (isort). ~1 hour.
6. **Identify next 5 high-value tests** — pick from `dashboard_api.py` route handlers, prioritizing auth + tenant isolation paths.

Sprint 2 should land within 1 week of Sprint 1 merge. Sprint 3 (the dashboard_api split + package layout) is bigger and depends on Sprint 2.
