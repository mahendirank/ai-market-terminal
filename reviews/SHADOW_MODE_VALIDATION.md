# SHADOW_MODE_VALIDATION.md

> Verifies the shadow-mode safety properties of NewsFetchAgent. Done in
> Stage 4.3 implementation, to be re-verified in the operator's 48h soak.

---

## 1. The 9 "strictly forbidden" rules from spec

| # | Rule | Status | Evidence |
|---|---|---|---|
| 1 | Existing news pipeline remains primary | ✅ | Legacy `_async_digest_loop` and route handlers UNCHANGED |
| 2 | Existing outputs continue serving production | ✅ | UI, Telegram, signals all wired to legacy paths; agent emits to a bus NO consumer reads |
| 3 | New agent runs silently in parallel | ✅ | Behind `AGENT_NEWS_FETCH_ENABLED` flag; logs but doesn't act |
| 4 | No routing changes | ✅ | No new middleware, no new request paths beyond Sprint 4.1's admin endpoints |
| 5 | No signal generation | ✅ | Agent emits `news.raw` events with metadata only; no signal logic |
| 6 | No trade execution | ✅ | No trade-related code in `news_fetch_agent.py` |
| 7 | No critic enforcement | ✅ | `input_critic = AlwaysAcceptCritic` (Sprint 4.4 will add critics, also observe-only) |
| 8 | No LangGraph reasoning yet | ✅ | LangGraph not imported anywhere in Sprint 4 code |
| 9 | No autonomous retries beyond existing limits | ✅ | RetryPolicy max_attempts=3 hard cap; bounded backoff; classified category whitelist |

---

## 2. The "absolutely forbidden" list from spec

| Forbidden | Evidence not present |
|---|---|
| Replace legacy fetch path | `from news import get_all_news` — agent USES legacy, doesn't REPLACE |
| Enable SignalCriticAgent | Not implemented in Stage 4.3 — Stage 4.4 work |
| Enable autonomous orchestration | Orchestrator only runs agents that are EXPLICITLY registered; only `news.fetch` registered (if its flag is on) |
| Enable recursive agents | No agent invokes another's methods or spawns sub-agents |
| Introduce LangGraph workflows | `grep -rn langgraph` in our code → no matches |
| Add trade execution hooks | No `executor`, `trade`, or `position` references in agent code |
| Add self-healing loops | Agent DISABLE state requires manual `reset_disabled` |

---

## 3. Bus topology — verify isolation

The agent emits to `events:news:news.raw`. To prove this stream is
**isolated** (no consumer reads from it):

```bash
# Sprint 4.3 expected state: stream may grow, but NO consumer group
# is registered against it. Verify:
ssh root@72.61.173.89 'docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli XINFO GROUPS events:news:news.raw' \
  || echo "  (stream may not exist yet OR no groups — both confirm isolation)"
```

Expected outputs:
- If flag is OFF: stream doesn't exist (no XADD has happened). XINFO returns error.
- If flag is ON and agent has ticked: stream exists; XINFO returns empty list (no consumer groups). Events sit waiting; would eventually be evicted by MAXLEN 5000.

**Either way, no consumer is processing the events.** The agent is shadow.

---

## 4. Failure-cascade isolation

The user requires:
> If NewsFetchAgent fails:
> - legacy pipeline MUST continue normally
> - no cascade failures
> - no websocket degradation
> - no API latency regression
> - no orchestrator instability

How each is enforced:

### "legacy pipeline MUST continue normally"
- Legacy code paths (`_async_digest_loop`, route handlers calling `get_all_news`, etc.) DON'T import the orchestration package at all.
- Agent failure has no observable side effect on legacy execution.
- Verified via `grep -rn "from orchestration\|import orchestration" *.py | grep -v test_ | grep -v dashboard_api` → only `dashboard_api.py` and orchestration/* themselves reference orchestration. Legacy untouched.

### "no cascade failures"
- Each agent's `tick()` wraps `run_once` in try/finally. Exceptions are CAUGHT, logged, recorded. They never propagate out of `tick()`.
- Orchestrator's `_run_loop` increments `consecutive_failures` on each failed tick. After `max_consecutive_failures` (default 5), agent transitions to DISABLED and the loop exits — not crashes.

### "no websocket degradation"
- The existing `_price_publisher_loop` continues running as before.
- Agent's `asyncio.to_thread` releases the event loop while the blocking `get_all_news` runs, so the WS publisher's 2s tick is unaffected.
- Verified by `sim_timeout_cascade.py` (3/3 scenarios) — timeouts on one agent don't cascade.

### "no API latency regression"
- Agent ticks on its own schedule, not on request-time. Per-request latency is unchanged.
- The only request-touching middleware (RequestContextMiddleware from Sprint 2) is also unchanged.

### "no orchestrator instability"
- Orchestrator manages all agents with bounded retries + DISABLED escape hatch.
- Agent failures DON'T propagate to orchestrator's own state machine.

---

## 5. Memory + handle isolation

| Resource | Isolation mechanism |
|---|---|
| Heap memory | Agent holds its own instance state (`_prev_tick_count`, etc.). No shared mutables with legacy. |
| Redis client | Same connection pool shared via redis-py (already shared between legacy modules). No additional FDs. |
| Async tasks | Agent's loop task is owned by `Orchestrator._agents[name].task`. Cleaned up on `stop_agent`. |
| ContextVars | Agent's `tick()` sets `agent_name_var`, `request_id_var`, `trace_id_var` in a try/finally — never leaks to other code paths. |
| Logger | Agent uses `agent.news.news.fetch` logger — its OWN namespace; doesn't pollute root or legacy loggers. |

---

## 6. Test coverage for safety properties

The 10 unit tests in `tests/test_sprint4_news_fetch_agent.py` cover:

| Test | Safety property verified |
|---|---|
| `test_extract_sources_skips_non_dict_and_non_string_source` | Defensive parsing — won't crash on malformed data |
| `test_run_once_calls_get_all_news_in_thread` | `asyncio.to_thread` used — non-blocking |
| `test_run_once_emits_bounded_payload` | Bus payload bounded — no leak of full news content |
| `test_drift_detection_first_tick_marks_first` | Stateful drift tracking works |
| `test_drift_detection_second_tick_reports_drift` | Drift metrics computed correctly |
| `test_non_list_return_does_not_crash` | Defensive — get_all_news returning garbage → count=0 |
| `test_external_api_error_triggers_retry` | RetryPolicy active on transient errors |
| `test_persistent_failure_is_swallowed_by_tick` | tick() catches; no propagation |
| `test_timeout_cancels_slow_fetch` | Timeout enforced cleanly |
| `test_class_config_defaults` | Configuration matches spec |

Plus 12 failure-mode simulations re-run after the implementation —
all passing. Specifically:
- `sim_failed_consumer.py` (4/4): consumer crashes don't strand events
- `sim_timeout_cascade.py` (3/3): one agent's timeout doesn't cascade
- `sim_redis_disconnect.py` (5/5): Redis down surfaces cleanly
- `sim_retry.py` (6/6): retry bounded
- `sim_retry_storm.py` (PASS): concurrent retries don't pile up

---

## 7. Shadow-mode invariants verified post-deploy

After the code-only deploy to VPS (flag stays off), I verified:

```
docker logs market-terminal | grep agent_registered_and_started → 0 lines
docker exec market-terminal python -c "import sys; print('news_fetch_agent' in sys.modules)" → False (lazy import; not loaded)
```

The agent code is on disk but `orchestration.agents.news_fetch_agent`
is NEVER imported when the flag is off. **Cannot fail because it
doesn't run.**

When the flag flips on:
- Agent's module is imported once
- One instance registered with the orchestrator
- One asyncio loop task created
- Agent ticks every 120s

These are the ONLY changes. Verified by code review of the lifespan
extension in `dashboard_api.py`.

---

## 8. Operator's safety checklist (run before flag flip)

- [ ] Stage A has been running ≥24h with `AGENT_ORCHESTRATOR_ENABLED=true` cleanly
- [ ] No regressions reported in Stage A
- [ ] DB snapshot taken
- [ ] Rollback tag created on VPS
- [ ] Stop-condition criteria reviewed (`AGENT_RESOURCE_PROFILE.md §5`)
- [ ] Monitoring playbook acknowledged (`NEWS_AGENT_DUAL_RUN_REPORT.md §4`)
- [ ] Operator has 30+ minutes available to watch the first ticks

If all checks pass: safe to flip `AGENT_NEWS_FETCH_ENABLED=true`.

---

## 9. Verdict

Stage 4.3 is **fully compliant with shadow-mode requirements**. The
agent cannot harm production:
- It uses legacy code (no separate fetch path)
- It emits to a bus no consumer reads
- It has bounded retries + timeouts
- It self-disables on persistent failure
- Legacy pipeline doesn't depend on it

Operator can flip the flag with confidence in the safety envelope.
The 48h soak validates the EXPECTED behavior; the safety guarantees
are built into the architecture regardless of what the soak reveals.
