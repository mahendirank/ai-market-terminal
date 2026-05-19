# IMPROVEMENT_ROADMAP.md — `ai-system/core/`

> Phase 1 artifact. Prioritized actions derived from `TECH_DEBT_REPORT.md`. Each item lists effort, blast radius, and rollback strategy.
> **Nothing here has been executed.** This is the proposal for Phase 2+.

---

## Sequencing principle

Do **read-only / additive** work before **destructive** work. Build a safety net (tests, CI, branch hygiene) before any large refactor. Pay down structural debt only after the safety net catches regressions.

```
Sprint 1 (safety net)    →    Sprint 2 (small wins)    →
Sprint 3 (refactor)      →    Sprint 4 (scale)
```

---

## Sprint 1 — Safety net (1–2 days, additive only)

Goal: every change after this sprint is verifiable.

### 1.1 Add pytest config + 5 high-value tests
- **Action**: create `core/pyproject.toml` with `[tool.pytest.ini_options]`, add tests for `auth.py`, `alert_engine.py`, `signal_memory.py`, `tenants.py`, and one `/api/health` smoke test using `fastapi.testclient.TestClient`.
- **Effort**: 4–6 hours
- **Blast radius**: zero — tests are additive
- **Rollback**: `git rm core/pyproject.toml core/tests/test_*_new.py`
- **Resolves**: TECH_DEBT §6

### 1.2 Pin dependencies
- **Action**: `pip freeze` inside the running prod container, write to `requirements-lock.txt`, keep `requirements.txt` as the unpinned spec. Add `anthropic`, `groq`, `openai` (whichever are imported) to `requirements.txt`.
- **Effort**: 1 hour
- **Blast radius**: rebuilt image uses pinned deps — must verify a test build before promoting
- **Rollback**: revert the file change
- **Resolves**: TECH_DEBT §7, §8

### 1.3 Add CI pipeline
- **Action**: `.github/workflows/ci.yml` running `ruff check`, `pytest`, and `docker build` on every PR. Protect `main` (require PR).
- **Effort**: 2 hours
- **Blast radius**: future merges require green CI — surfaces existing latent failures
- **Rollback**: delete the workflow file; unprotect `main`
- **Resolves**: TECH_DEBT §10

### 1.4 Verify or delete `engine.py`
- **Action**: `grep -rn "from engine\|import engine" ~/ai-system` to find callers. If none, `git rm engine.py`. If callers exist, fix the import (`build_system_prompt` → `build_prompt`).
- **Effort**: 15 min
- **Blast radius**: zero if no callers; otherwise a one-line fix
- **Rollback**: `git revert`
- **Resolves**: TECH_DEBT §4

**Sprint 1 deliverable**: green CI on `main`, 5 tests passing, locked dependencies, no broken imports.

---

## Sprint 2 — Small wins (1 day, low risk)

### 2.1 Resolve product naming
- **Action**: pick one — recommendation `zyvora-terminal`. Rename local dir `ai-system/` → `zyvora-terminal/`. Rename GitHub repo via `gh repo rename`. Update `deploy.sh` URL constant. Update `CLAUDE.md` with the legacy aliases.
- **Effort**: 1 hour
- **Blast radius**: any tool / bookmark / Slack message referencing the old paths breaks. Symlink `~/ai-system` → `~/zyvora-terminal` for a transition period.
- **Rollback**: rename back; the symlink absorbs the change.
- **Resolves**: TECH_DEBT §1
- **NOTE**: requires user approval before execution — it's a cross-cutting rename.

### 2.2 Move runtime DBs to `core/db/`
- **Action**: configure modules that write `earn_tg_cache.db` (and any others) to write to `core/db/earn_tg_cache.db`. Move existing file. Update Docker volume mount if needed.
- **Effort**: 1 hour
- **Blast radius**: medium — wrong path = lost cache. Test in dev first.
- **Rollback**: move the file back; revert path constant.
- **Resolves**: TECH_DEBT §12

### 2.3 Archive the parent Streamlit shell
- **Action**: User confirmed (2026-05-18) that `~/ai-system/app.py` is **still used temporarily**. **Do NOT archive in Sprint 2.** Revisit when the user retires it.
- **Status**: deferred indefinitely
- **Resolves**: TECH_DEBT §13 (parking)

### 2.4 Resolve embedded-repo / outer-wrapper state — **NEW from Sprint 1**
- **Action**: rename `~/ai-system/core/` → `~/zyvora-terminal/`. Delete (or archive on GitHub) the outer `mahendirank/ai-system` repo. The inner repo `mahendirank/ai-market-terminal` becomes the only repo. Optionally rename the GitHub repo to `mahendirank/zyvora-terminal` for naming consistency (TECH_DEBT §1).
- **Effort**: 1 hour for local moves; 5 min for GitHub repo rename via `gh repo rename`.
- **Blast radius**: paths in `start-ai-terminal.sh`, `Bloomberg feed/docker-compose.market-terminal.yml`, `master-dashboard/projects.json` may reference `~/ai-system/core/` — grep first.
- **Rollback**: rename back; restore outer repo from GitHub archive.
- **Resolves**: TECH_DEBT §14, partial §1
- **User approval**: already granted (2026-05-18 — answer to Phase 1 Q3)

### 2.5 Phase A logging config — **NEW from Sprint 1**
- **Action**: add `core/logging_config.py` per `reviews/LOGGING_STANDARD.md` §"Phase A". Call `setup()` from `run.py` before importing `dashboard_api`. Add `LOG_LEVEL` and `LOG_FORMAT` to `.env.production.example`.
- **Effort**: 1 day
- **Blast radius**: changes output formatter on stdout. Same log volume, different format. Verify `docker logs` is still parseable.
- **Rollback**: `git revert` — formatter reverts to default.
- **Resolves**: TECH_DEBT (new) — silent `log.debug` calls in 8 modules
- **Prereq**: none

### 2.6 Generate `requirements-lock.txt` — **NEW from Sprint 1**
- **Action**: ensure prod container is up locally. Run `bash scripts/pin-deps.sh`. Commit the resulting `requirements-lock.txt`. Update `Dockerfile` to `pip install -r requirements-lock.txt` (with `requirements.txt` kept as the unpinned spec for development).
- **Effort**: 15 min (after container is running)
- **Blast radius**: future docker builds use pinned versions — exact parity with current prod image. Big win for reproducibility.
- **Rollback**: revert Dockerfile change; pip resolves freely again.
- **Resolves**: TECH_DEBT §7
- **Prereq**: market-terminal container running locally.

---

## Sprint 3 — Structural refactor (1–2 weeks, requires safety net from Sprint 1)

### 3.1 Introduce package boundaries
- **Action**: convert `core/` flat layout into 13 packages per the groups in `ARCHITECTURE_MAP.md`. One package per PR. Order (lowest risk first): `forex/` → `regime/` → `news/` → `earnings/` → `signals/` → `macro/` → `ai/` → `api/` (last, biggest).
- **Effort**: 2–3 days per package; 2 weeks total
- **Blast radius**: every PR touches imports across many files. CI from Sprint 1.3 catches regressions.
- **Rollback**: each PR is independently revertible. No PR larger than one package.
- **Resolves**: TECH_DEBT §3
- **Prereq**: Sprint 1 (tests + CI) must be green.

### 3.2 Split `dashboard_api.py` into routers
- **Action**: create `core/api/routers/` with one file per route group (`auth.py`, `signals.py`, `me.py`, `admin.py`, `health.py`, `ws.py`). Use `APIRouter`. `dashboard_api.py` becomes app + middleware + lifespan + router includes only.
- **Effort**: 2 days
- **Blast radius**: every route URL must continue to resolve. Add a route-inventory test that asserts the OpenAPI schema is unchanged before/after.
- **Rollback**: each router extraction is one PR.
- **Resolves**: TECH_DEBT §2
- **Prereq**: 3.1 complete (so `api/` package exists).

### 3.3 Consolidate functional surfaces
- **Action**: decide product role of `terminal.py` (CLI) and parent `app.py` (Streamlit). Archive or document.
- **Effort**: 30 min decision + 1 hour cleanup
- **Resolves**: TECH_DEBT §5

---

## Sprint 4 — Scale (when paid users > ~20 concurrent, Phase 3+)

### 4.1 Migrate hot SQLite tables to Postgres
- **Action**: identify the 3–5 highest-write SQLite tables (`auth`, `user_settings`, `signals`). Schema-migrate to Postgres. Switch reads/writes behind a feature flag. Keep low-write caches in SQLite.
- **Effort**: 1–2 weeks
- **Blast radius**: data migration — must script + dry-run + verify before cutover
- **Rollback**: feature flag back to SQLite path
- **Resolves**: TECH_DEBT §9
- **NOTE**: defer until concurrency justifies it.

### 4.2 Observability
- **Action**: structured JSON logging via `structlog`. Sentry for error tracking. OpenTelemetry traces for FastAPI routes. Grafana dashboard for the 8 background-loop heartbeats.
- **Effort**: 3–5 days

### 4.3 Rate limiting + abuse protection
- **Action**: `slowapi` or Caddy-side rate limits on `/api/auth/login`, `/api/me/*`. Captcha on signup if applicable.
- **Effort**: 1 day

---

## Phase-2-of-the-original-workflow scaffolding

The original Senior AI Systems Engineer workflow also asks for these directories. Below is what's worth doing **in the `zyvora-terminal` repo only** (not in `~/`):

| Directory | Verdict | Reason |
|---|---|---|
| `/specs/` | Yes — keep product specs / API contracts here | Useful as the system grows |
| `/tasks/` | No — `TaskCreate` skill + GitHub issues already cover this | Avoid YAGNI |
| `/architecture/` | Yes — move `PRODUCTION.md` + the reviews artifacts here | Centralizes design docs |
| `/rules/` | Maybe — `.rules/project-rules.md` can be a single file in `core/` | Don't create a folder for one file |
| `/tests/` | Already exists at `core/tests/` | Don't duplicate |
| `/agents/` | **No, not now** | The original workflow assumes you want planner/review/testing/etc. subagents. Claude Code already provides general-purpose + Explore subagents. Custom agent definitions only make sense when you have a repeating workflow that needs one. Add when a real use case appears. |
| `/docs/` | Yes — for user-facing docs, currently missing | |
| `/scripts/` | Already exists as `core/*.sh` | Could move to `scripts/` |
| `/reviews/` | Just created — this is where Phase 1 artifacts live | Keep |
| `/workflows/` | No — GitHub Actions live in `.github/workflows/` | Don't duplicate |

**Recommendation**: do the directory scaffolding only after Sprint 3.1 (when the package structure makes obvious places for things to live). Premature scaffolding before the refactor will need re-shuffling.

---

## Open questions for the user before Phase 2 begins

1. **Is `terminal.py` (CLI) still used?** If not, can it be archived?
2. **Is `~/ai-system/app.py` (Streamlit shell) still used?** If not, can it be archived?
3. **Approval to rename** the local dir + GitHub repo to `zyvora-terminal`?
4. **Are there other operators / users besides you?** Multi-user SaaS pricing-wise, how many concurrent users today vs. target? (Drives Sprint 4 timing.)
5. **Is there a staging environment**, or does the prod VPS double as staging? (Drives risk-tolerance on Sprint 2/3.)

Answers to these block items 2.1, 2.3, 3.3, and 4.1. Sprint 1 is safe to start regardless.
