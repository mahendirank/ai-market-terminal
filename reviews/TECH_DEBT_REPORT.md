# TECH_DEBT_REPORT.md — `ai-system/core/`

> Phase 1 artifact. Each item has severity, evidence, and a one-line "what would fix it." No fixes applied.
> Severity scale: **P0** ship-blocker, **P1** scaling/maintenance pain, **P2** quality, **P3** cosmetic.

---

## 1. Three names for one product — **P1**

**Evidence**: local dir `ai-system/`, GitHub repo `mahendirank/ai-market-terminal` (per `deploy.sh:12`), brand `Zyvora Terminal` at `zyvoratech.co`, container name `market-terminal`, prod path `/opt/zyvora/`. Two other local dirs (`ai-market-terminal/`, `Bloomberg feed/`) share or echo these names.

**Why it hurts**: every new contributor (or future-you) wastes time mapping the naming. README references / Slack mentions / commit messages will diverge. Increases the cost of every conversation about the system.

**Fix**: pick one canonical name (`zyvora-terminal` is the safest — matches the prod path and brand). Rename the local dir, rename the GitHub repo (`gh repo rename`), update `deploy.sh` URL. Document the legacy aliases in CLAUDE.md so old links still resolve mentally.

---

## 2. `dashboard_api.py` is a 2,990-LOC god file — **P1**

**Evidence**: `dashboard_api.py` at 2,990 lines holds the FastAPI app, lifespan, background tasks, route handlers, auth wiring, WebSocket handlers, and probably more. It's the second-largest file in the project and contains the only `lifespan` block.

**Why it hurts**: every route change is a 3,000-line-file diff. Merge conflicts compound. Cannot meaningfully reason about it as a whole. Onboarding cost is high.

**Fix**: split into a `routers/` package with one module per route group (auth, signals, watchlist, settings, ai, admin, health, websocket). Keep `dashboard_api.py` only as the FastAPI app + middleware + lifespan + router wire-up — should drop to <300 LOC. Use FastAPI `APIRouter`.

---

## 3. Flat module layout — no packages, no `__init__.py` — **P1**

**Evidence**: all 105 Python files sit at one directory level in `core/`. No subdirectories (other than `tests/`, `db/`, `static/`, `templates/`). `dashboard_api.py:6` uses `sys.path.insert(0, os.path.dirname(__file__))` to make this work.

**Why it hurts**: imports are flat and ambiguous (`from news import ...` could mean any of 13 unrelated module groups). Refactoring is unsafe — there's no IDE-friendly way to see which group owns what. Any future packaging (PyPI, internal monorepo) is blocked.

**Fix**: introduce 13 packages matching the groups in `ARCHITECTURE_MAP.md` §"Module groups". Add `__init__.py` files. Move files. Update imports. Do this incrementally — one package per PR, starting with the lowest-coupled group (e.g. `forex/` or `regime/`).

---

## 4. `engine.py` is broken — **P0** *(if anyone imports it)*

**Evidence**: `core/engine.py:3` says `from loader import build_system_prompt, detect_domains`. `core/loader.py` exports `build_prompt` (line 77) — not `build_system_prompt`. Running `engine.py` will `ImportError` at import time.

**Why it hurts**: dead code in a prod-relevant module is a hazard — a future caller will hit the bug, or a refactor will spread the typo. Also confuses anyone reading the code.

**Fix**: two options — (a) if `engine.py` is unused, delete it; (b) if it's intended, rename `loader.build_prompt` to `build_system_prompt` consistently, or change `engine.py` to import `build_prompt`. Check usage first: `grep -rn "from engine\|import engine" ~/ai-system`.

---

## 5. Two unrelated functional surfaces coexist — **P2**

**Evidence**:
- `run.py` → uvicorn → `dashboard_api:app` — the production web app
- `terminal.py` — a CLI that prints macro/news/stocks to stdout, separate code path
- `~/ai-system/app.py` — a Streamlit shell that imports `core.loader` and `run_qwen`

Three entry points, three product surfaces, one codebase. The CLI and Streamlit shell don't appear in `PRODUCTION.md`.

**Why it hurts**: ambiguous product story — what does this app do? It's a web SaaS by infrastructure but a CLI tool by `terminal.py` and a dev playground by `app.py`. Each surface drifts independently.

**Fix**: decide. If only the FastAPI app is live, archive `terminal.py` and the parent `app.py` to a `legacy/` folder with a NOTE.md. If `terminal.py` is a useful dev tool, document it explicitly in PROJECT_ANALYSIS.md and keep it tested.

---

## 6. Test coverage is effectively zero on hot paths — **P1**

**Evidence**: `core/tests/` has 6 files — 4 of them are stage-gated regression tests (`test_stage2.py` … `test_stage5.py`), 1 tests prompt-builder reasoning, 1 tests HNI macro integration. No tests touch `dashboard_api.py`, `alert_engine.py`, `signal_memory.py`, `auth.py`, `tenants.py`, `notify.py`, or any of the ingestion modules. No `pytest.ini` / `pyproject.toml` config at `core/` level.

**Why it hurts**: zero safety net for refactors. The Phase 2 module-split refactor is unsafe without tests. Production regressions slip through.

**Fix**: write tests in priority order:
1. `auth.py` — login, cookie issuance, password reset (correctness + security)
2. `alert_engine.py` — cooldown logic + threshold evaluation
3. `signal_memory.py` — dedup + persistence round-trip
4. `tenants.py` — tenant isolation (regression for multi-user data leak)
5. One smoke test that hits `GET /api/health` end-to-end via TestClient

Add `pyproject.toml` with `[tool.pytest.ini_options]`. Target ≥40% line coverage on these 5 modules before any structural refactor.

---

## 7. No version pins on dependencies — **P2**

**Evidence**: `core/requirements.txt` has 18 packages; only `redis>=5.0` and `ta>=0.11.0` have version specifiers. The rest float to latest.

**Why it hurts**: reproducibility is broken. A `docker build` today may produce a different image than tomorrow. A breaking yfinance/pandas release will land silently in prod.

**Fix**: `pip freeze` from a working image and pin every package. Switch to `requirements.in` + `pip-compile` for lockfile workflow, or migrate to `pyproject.toml` + `uv`/`poetry`.

---

## 8. Anthropic / OpenAI / Groq clients used in code but absent from `requirements.txt` — **P2**

**Evidence**: `engine.py:2` imports `anthropic`. `groq_research.py` (498 LOC) implies Groq client. Neither package is listed in `requirements.txt`.

**Why it hurts**: builds may succeed locally (cached) but fail on fresh installs. The `requirements.txt` is not the source of truth for the runtime environment.

**Fix**: audit imports vs. `requirements.txt`. Add the missing packages with pins. `grep -h "^import \|^from " *.py | awk '{print $2}' | sort -u` is a starting point.

---

## 9. 18 SQLite databases as durable state — **P2** (architectural)

**Evidence**: `PRODUCTION.md` confirms 18 SQLite DBs in the `terminal_db` volume. `dashboard_api.py` does `_disk_load("hni_v3__market_", HNI_CACHE_TTL)` — disk-backed cache pattern.

**Why it hurts**: SQLite is fine for single-writer local apps, but with 18 DBs in a multi-tenant SaaS:
- Cross-DB queries are impossible
- Concurrent writes can serialize the WAL
- Backup is "tar the volume" rather than `pg_dump`
- Scaling to multiple containers requires sticky sessions or shared NFS

Not urgent — it works today. Becomes a P1 when paid users exceed ~20–50 concurrent.

**Fix**: scope a migration to Postgres for the high-write tables first (`auth.db`, `signal_*.db`, per-user settings). Keep low-write caches in SQLite or Redis. Phase 3+.

---

## 10. No CI / pre-commit / linter config — **P2**

**Evidence**: no `.github/workflows/`, no `.pre-commit-config.yaml`, no `ruff.toml` / `.flake8` / `mypy.ini` found in `core/` or its parent. `git status --short` shows 2 dirty files on `main` with no branch.

**Why it hurts**: deploys go straight from `main` to prod. No automated check on PRs (if PRs exist at all). Style and type errors land in `main`.

**Fix**: add `.github/workflows/ci.yml` running `ruff check`, `pytest`, and `docker build`. Add `.pre-commit-config.yaml` with `ruff`, `ruff-format`, `detect-secrets`. Adopt a branch policy: `main` is protected, PRs required.

---

## 11. Operational scripts hardcode paths to `/opt/zyvora` — **P3**

**Evidence**: `nuclear-reset-admin.sh:25`, `fill-env.sh:13`, `setup-backup.sh:14` all hardcode `/opt/zyvora` and `docker-compose.prod.yml`.

**Why it hurts**: moving the install path or running multiple environments (staging) requires editing every script. Not a problem today; will be when staging is set up.

**Fix**: introduce a top-of-script `INSTALL_DIR="${ZYVORA_HOME:-/opt/zyvora}"` so env overrides are possible.

---

## 12. Stray cache file `earn_tg_cache.db` committed alongside source — **P3**

**Evidence**: `core/earn_tg_cache.db` sits next to `*.py` files. `.gitignore` does include `*.db` so it's local-only — but it shouldn't live in source tree at all.

**Why it hurts**: confuses readers — is this a fixture or runtime cache? Could be `rm`'d by mistake. Belongs in the `terminal_db` volume.

**Fix**: move runtime DBs to `core/db/` (which already exists). Configure the modules that write `earn_tg_cache.db` to write into `db/`.

---

## 13. Parent `~/ai-system/` directory has stray subdirs — **P3**

**Evidence**: `~/ai-system/` contains `core/` (the real project) plus `app.py` (Streamlit), `modules/`, `skills/`, `data/`, `outputs/`, `Issues/` — none of which are referenced by anything in `core/`. `app.py` is the only file that imports `core.*`.

**Why it hurts**: the parent dir is a different project (a Streamlit AI command center) bolted onto the same parent. Confuses anyone navigating from the repo root.

**Fix**: decide whether the parent project is alive. If yes, give it its own dir (`ai-command-center/`) and move `core/` to its own top-level dir. If no, archive the parent files to `backup_projects/`.

---

## 14. Outer `ai-system/` repo is a misconfigured wrapper around the embedded `core/` repo — **P1**

**Evidence** (discovered in Sprint 1 pre-flight, not Phase 1):
- `~/ai-system/.git` exists with **1 commit total** ("Initial commit — AI Market Terminal core")
- `~/ai-system/core/.git` exists separately, pointing to `github.com/mahendirank/ai-market-terminal.git`, on `main`, 5 commits ahead of the outer pointer
- `~/ai-system/.gitmodules` does **not exist** — so the outer repo records `core` as a gitlink (mode 160000) but with no submodule registration
- The outer repo has a stale pointer: outer says `core` is at `f6f3e63`, but `core` is actually at `3eaf7b1` (5 commits newer)

**Why it hurts**: worst-of-three-worlds — neither a subdirectory, nor a proper submodule, nor a sibling repo. `git submodule status` fails (`no submodule mapping found in .gitmodules`). Anyone cloning `ai-system` gets an empty `core/`. CI in either repo will be confused about what to build.

**Fix** (do **not** execute in Sprint 1 — flagged for Sprint 2 decision):

Three options ranked by simplicity:

- **Option A (recommended)**: drop the outer repo. Move `~/ai-system/core/` to `~/zyvora-terminal/`. The `mahendirank/ai-market-terminal` repo becomes the only repo. Archive the outer `mahendirank/ai-system` repo on GitHub.
- **Option B**: properly register `core` as a submodule with `git submodule add`. Every `git pull` needs `--recurse-submodules` thereafter — friction for no real benefit.
- **Option C**: keep the embedded-repo state. Update the outer pointer manually when the inner repo advances. Ongoing maintenance burden.

Option A is consistent with the rename in §1 (Zyvora Terminal). It eliminates a name and a repo at the same time.

---

## Severity totals

| Severity | Count |
|---|---|
| P0 (ship-blocker) | 1 — engine.py import bug (low impact if unused) |
| P1 (scaling pain) | 5 — naming, god file, flat layout, test coverage, embedded-repo wrapper |
| P2 (quality) | 5 — surfaces, pins, missing deps, DBs, no CI |
| P3 (cosmetic) | 3 — script paths, stray cache, stray parent dirs |
