# PRODUCTION_READINESS.md

> Gate criteria before Sprint 4 begins wiring real agents. Each item is
> binary (pass/fail). The aggregate decides whether the foundation is
> production-grade enough to carry intelligent agents safely.

---

## Summary

| Pillar | Score | Verdict |
|---|---|---|
| Stability | 7/7 ✅ | Foundation is stable |
| Observability | 4/7 ⚠️ | Logs done; metrics + tracing pending (planned Sprint 5) |
| Debuggability | 6/6 ✅ | Structured logs + correlation IDs sufficient |
| Reversibility | 6/6 ✅ | Three rollback levels documented and verified |
| Maintainability | 5/6 ⚠️ | Flat module layout still owes a refactor (Sprint 6+) |

**Overall**: **GO for Sprint 4**, with the documented Sprint 5 + 6
followups locked in.

---

## Pillar 1: Stability

- [x] **All Sprint 1–3 tests pass locally**: 188 smoke tests, 0 failures, 0 skips.
- [x] **All Sprint 1–3 tests pass on CI**: 262 tests pass on sprint-3 (includes pre-existing stage tests; verified 2026-05-19).
- [x] **No new dependencies introduced**: `requirements.txt` unchanged from Sprint 2 (only `python-pptx` added in Sprint 1; Sprint 3 used only stdlib + existing `redis`).
- [x] **Python version pinned in Dockerfile**: 3.11-slim.
- [x] **Existing `print()` calls preserved**: Phase A logging is additive; no `print` was rewritten.
- [x] **No autonomous loops**: Orchestrator's `_run_loop` is opt-in via `start_agent`. Sprint 4 wires the first start.
- [x] **Hard caps everywhere**: RetryPolicy max_attempts ≤ 20; CircuitBreaker has bounded states; Streams have MAXLEN.

---

## Pillar 2: Observability

- [x] **Structured JSON logs available**: Sprint 2 `JsonFormatter` ships, opt-in via `LOG_FORMAT=json`.
- [x] **Request correlation IDs**: `request_id` ContextVar populated on every HTTP request.
- [x] **Per-agent log context**: `agent_name` + `request_id` + `trace_id` set per tick (Sprint 3).
- [ ] **Prometheus `/metrics` endpoint**: **deferred to Sprint 5**.
- [ ] **OpenTelemetry traces**: **deferred to Sprint 4+ conditional**.
- [ ] **Off-host log shipping**: **deferred until disk pressure or query needs justify** (likely Sprint 5–6).
- [x] **`/api/health` already exists** (pre-existing in `dashboard_api.py`).

**Gap analysis**: today's observability is "logs + duration_ms" only. That covers the common debugging case (find request_id, grep all related lines, see what went wrong). Lacks: trend analysis, alerting, SLO tracking. Sprint 5 closes the gap.

---

## Pillar 3: Debuggability

- [x] **Every log line is greppable by request_id**: tested by Sprint 2 middleware tests.
- [x] **Exceptions carry exc_type + exc_msg + exc_traceback** in JSON envelope.
- [x] **Agent ticks log tick_id + agent_name** on failure.
- [x] **Circuit breaker state changes logged**: `circuit_breaker_transition` log line on every transition.
- [x] **CI logs accessible**: `gh run view --log-failed` returns the failing step output.
- [x] **Test failures show full stack**: `pytest --tb=short` captures and reports.

---

## Pillar 4: Reversibility

- [x] **Rollback docs for each sprint**: `SPRINT_{1,2,3}_ROLLBACK.md` published.
- [x] **Three rollback levels documented**: env-var fallback → revert one commit → revert whole sprint.
- [x] **Pre-rollout VPS snapshot procedure**: in `ROLLOUT_CHECKLIST.md §2.1`.
- [x] **No PR is squashed**: individual milestone commits preserved for per-feature revert.
- [x] **Sprint 3 has zero production callers**: revertible at any time without affecting runtime.
- [x] **`LOG_FORMAT=console` default preserves visual UX**: switching to JSON is opt-in.

---

## Pillar 5: Maintainability

- [x] **Each module has a one-line docstring + section comments**: enforced by review.
- [x] **Public API re-exported from package `__init__`**: `orchestration/__init__.py` exports 22 names.
- [x] **Tests cover failure paths**: every agent test has a "records failure" counterpart.
- [x] **Stable wire format**: EventEnvelope `SCHEMA_VERSION` constant; from_dict tolerates unknown fields.
- [x] **Naming follows project convention**: `<noun>_<verb>` for log events, `events:<family>:<verb>` for streams.
- [ ] **Flat module layout still owes refactor**: 105 modules in `core/` root. Sprint 6+ refactor (per IMPROVEMENT_ROADMAP).

---

## Hard prerequisites for Sprint 4

Before Sprint 4 wires `orchestration` into `dashboard_api.py`:

- [ ] Sprint 1–3 merged to `main`
- [ ] VPS pulled + redeployed with Sprint 1–3
- [ ] At least 24 hours of post-rollout observation showing no new errors
- [ ] `LOG_FORMAT=json` enabled (or at least tested in staging) — Sprint 4's agents emit structured logs that are only useful in JSON form

---

## Soft prerequisites (nice-to-have, not blockers)

- [ ] Docker log rotation configured (per OBSERVABILITY_PLAN §3) — recommended before Sprint 4 since structured logs are higher-volume
- [ ] `scripts/pin-deps.sh` run to produce a `requirements-lock.txt` from the running container
- [ ] Backup cron (`setup-backup.sh`) verified to be running

---

## Things Sprint 4 will explicitly NOT inherit

The following are out of scope for Sprint 4 even though they're related:

- The 328 `print()` → `log.*` migration (Phase B of LOGGING_STANDARD) — Sprint 4 may migrate ONE module if it touches it for agent reasons; rest is later sprints.
- The flat module layout refactor — Sprint 6+.
- The outer `ai-system/` repo unification — TECH_DEBT §14, not blocking.
- Multi-tenant agent isolation — Sprint 5+ if a paying client demands it.

---

## Sign-off

To exit this checklist and begin Sprint 4, the user should:

1. Verify each `[x]` is genuinely true.
2. Acknowledge each unchecked `[ ]` is intentionally deferred (not forgotten).
3. Approve the merge sequence in `ROLLOUT_CHECKLIST.md`.

After approval and successful rollout, `SPRINT_4_PLAN.md` becomes the
working document.
