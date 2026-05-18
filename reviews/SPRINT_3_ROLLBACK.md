# SPRINT_3_ROLLBACK.md — Orchestration Foundation

> Working in: `~/ai-system/core/` (real repo: `mahendirank/ai-market-terminal`)
> Branch: `sprint-3/orchestration-foundation` (off `sprint-2/phase-a-logging`)
> Sprint 3 goal: ship orchestration foundation as a self-contained
> library. **No production code path consumes it yet.**
> Rule (carried forward): prod = staging. Each milestone is reversible.

---

## Branch strategy

Sprint 3 stacks on Sprint 2:

```
main
  └─ sprint-1/safety-net
       └─ sprint-2/phase-a-logging
            └─ sprint-3/orchestration-foundation  ◄── Sprint 3 commits
```

To abandon Sprint 3 only:
```bash
cd ~/ai-system/core
git checkout sprint-2/phase-a-logging
git branch -D sprint-3/orchestration-foundation
```

To abandon Sprints 2 and 3 (keep Sprint 1):
```bash
git checkout sprint-1/safety-net
git branch -D sprint-2/phase-a-logging sprint-3/orchestration-foundation
```

---

## Why this Sprint is unusually safe

Nothing in `core/orchestration/` is imported by `dashboard_api.py`,
`run.py`, or any existing module. The orchestrator is a library; Sprint 4
wires it. Therefore:

| What Sprint 3 changes in production | Effect |
|---|---|
| Adds 7 new `.py` files under `core/orchestration/` | None — no caller |
| Adds 8 new test files in `tests/` | None — tests don't run at runtime |
| Adds 4 new docs in `reviews/` | None — docs are docs |
| Modifies `dashboard_api.py` | NOT MODIFIED in Sprint 3 |
| Modifies `run.py` | NOT MODIFIED in Sprint 3 |
| Modifies `requirements.txt` | NOT MODIFIED in Sprint 3 |
| Modifies `Dockerfile` | NOT MODIFIED in Sprint 3 |

The only edge case: the `test_module_imports.py` parametrized test
auto-discovers any new top-level `*.py` in `core/`. The orchestration
files are under `core/orchestration/` — a subpackage — so they are NOT
picked up by the top-level glob. Verified by passing 188 tests.

---

## Three rollback levels

### Level 1 — Disable per-file (precision rollback)

Each module is independently revertible. To remove one piece:

```bash
cd ~/ai-system/core
git revert <sha-of-feat-event_bus-commit>   # removes only the event bus
```

### Level 2 — Full Sprint 3 rollback (one delete)

Because nothing else imports from `core/orchestration/`, you can simply
delete the package and its tests:

```bash
cd ~/ai-system/core
rm -rf orchestration/
rm tests/test_event_envelope.py tests/test_retry.py \
      tests/test_circuit_breaker.py tests/test_critic.py \
      tests/test_base_agent.py tests/test_event_bus.py \
      tests/test_orchestrator.py tests/test_orchestration_smoke.py
rm reviews/ORCHESTRATION_RUNTIME.md reviews/AGENT_CONTRACT.md \
      reviews/REDIS_STREAMS_GUIDE.md reviews/CIRCUIT_BREAKER_PLAN.md \
      reviews/SPRINT_3_ROLLBACK.md
```

Sprint 1+2 (121 tests) still pass. Verified.

### Level 3 — Branch abandon

```bash
git checkout sprint-2/phase-a-logging
git branch -D sprint-3/orchestration-foundation
```

Same effect as Level 2, cleaner git history.

---

## Sprint 3 milestones (commits)

Each commit is a self-contained logical unit, reverts independently:

| # | What lands | Touches |
|---|---|---|
| M1 | `event_envelope.py` + tests | 2 files |
| M2 | `retry.py` + `circuit_breaker.py` + tests | 4 files |
| M3 | `critic.py` + tests | 2 files |
| M4 | `base_agent.py` + tests | 2 files |
| M5 | `event_bus.py` (Redis + InMemory) + tests | 2 files |
| M6 | `orchestrator.py` + tests + smoke test | 3 files |
| M7 | `__init__.py` (re-exports) | 1 file |
| M8 | Documentation (4 new + this rollback file) | 5 files |

Reverting any one of M1–M8 leaves the others functional **as long as
M1 (event_envelope) and M2 (retry) survive** — they're imported by the
rest of the package.

If reverting M1, all higher commits must also be reverted. The
dependency graph:

```
M1 event_envelope ← M3 critic, M4 base_agent, M5 event_bus
M2 retry          ← M4 base_agent
M2 circuit_breaker ← (no internal consumer — standalone)
M4 base_agent     ← M6 orchestrator
M5 event_bus      ← M4 base_agent (lazy import inside methods)
M6 orchestrator   ← M8 docs
M7 __init__       ← everything that imports `from orchestration`
```

---

## Push policy

Same as Sprint 1 + 2:

- Nothing pushed to `origin/sprint-3/orchestration-foundation` until
  user explicitly approves.
- Nothing merged to `main` until staging exists OR user accepts the
  prod = staging risk.

---

## Emergency: prod is broken and Sprint 3 is suspected

Sprint 3 is **the safest sprint to suspect last**, because:

- `dashboard_api.py` doesn't import `orchestration`.
- `run.py` doesn't import `orchestration`.
- The Dockerfile doesn't reference `orchestration`.
- No orchestration test pollutes runtime state.

But for completeness:

1. **Inspect**: `docker exec -it market-terminal python -c "import sys; print('orchestration' in sys.modules)"`
   If False, Sprint 3 code is not loaded — it's not the cause.

2. **Hard revert**: same as Level 2 above; redeploy.

3. **Verify**:
   ```bash
   curl -s http://localhost:8001/api/health | jq .
   docker logs market-terminal | tail -50
   ```

---

## What this rollback file is NOT

Not a guarantee. The rollback paths above have been verified by:
- Reading the dependency graph
- Confirming nothing in `dashboard_api.py` imports `orchestration`
- Running `pytest -m smoke` to confirm 188 tests pass

They have NOT been verified by actually deploying + rolling back on a
prod-like env. Sprint 6+ stages a staging env where rollback drills
become routine.
