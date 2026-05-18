# OBSERVABILITY_PLAN.md

> Companion to `LOGGING_STANDARD.md`. Covers metrics, traces, retention,
> and the multi-sprint roadmap for making the system observable end-to-end.
> Sprint 2 ships only logging (per the Phase A scope). This document is
> mostly forward-looking.

---

## 0. The three pillars

| Pillar | Sprint 2 status | Owner module(s) (planned) |
|---|---|---|
| **Logs** | ✅ shipped (Phase A) | `logging_config.py`, `logging_middleware.py` |
| **Metrics** | ⏳ Sprint 5 | `metrics.py` (new) — `prometheus_client` |
| **Traces** | ⏳ Sprint 4+ (conditional) | `tracing.py` (new) — `opentelemetry-api` + `opentelemetry-sdk` |

The Sprint 2 ContextVars (`request_id_var`, `trace_id_var`, `tenant_id_var`, `agent_name_var`) form the **correlation layer that connects all three pillars** when the later ones land. No retrofit required.

---

## 1. Logs — current state (post Sprint 2)

| Aspect | Implementation |
|---|---|
| Format | One JSON object per line, or human-readable text (controlled by `LOG_FORMAT`) |
| Transport | stdout → Docker `json-file` driver → host filesystem |
| Correlation | `request_id` per HTTP request, propagated via ContextVar |
| Volume estimate | ~50 KB/min at 10 req/s; ~1 GB/week |
| Shipping | None — logs live on the VPS until container/host rotation kicks in |

**Gaps that Sprint 3+ will close**:
- Per-tenant log enrichment (need `tenant_id_var` to be set in route handlers — Sprint 3)
- Background-loop log correlation (`agent_name_var` populated by BaseAgent — Sprint 3)
- Off-host shipping (a log collector — Sprint 5 or sooner if disk fills)

---

## 2. Metrics — planned (Sprint 5)

### Why Prometheus

- Pull-based: scraper hits `/metrics` on our schedule, no agent process
- Stdlib-free, single Python package (`prometheus_client`)
- Wide ecosystem (Grafana, Alertmanager, exporters)
- Sub-megabyte memory footprint per process

### Endpoint design

`GET /metrics` returns Prometheus exposition format. Binds to `127.0.0.1` only (no Caddy proxy) so it's not publicly exposed.

### Initial metric set

| Metric | Type | Labels | Why |
|---|---|---|---|
| `http_requests_total` | counter | `method`, `path_template`, `status` | Track traffic + error rate |
| `http_request_duration_seconds` | histogram | `method`, `path_template` | Latency budget tracking |
| `agent_tick_total` | counter | `agent`, `outcome=success\|error` | Sprint 3 BaseAgent visibility |
| `agent_tick_duration_seconds` | histogram | `agent` | Detect slow agents |
| `agent_last_success_timestamp` | gauge | `agent` | Alert if agent stops ticking |
| `event_published_total` | counter | `stream` | Redis Stream traffic |
| `event_consumed_total` | counter | `stream`, `consumer` | Consumer lag indirectly |
| `event_dlq_depth` | gauge | `stream` | Stuck events |
| `circuit_open` | gauge (0/1) | `service` | External dep health |
| `llm_call_total` | counter | `provider`, `model`, `task` | AI usage |
| `llm_call_cost_usd_total` | counter | `provider`, `model` | $5/day budget tracking |
| `llm_call_duration_seconds` | histogram | `provider`, `model` | LLM latency |
| `signals_emitted_total` | counter | `decision`, `quality_label` | Signal volume by quality |
| `signals_rejected_total` | counter | `reason` | Critic verdicts |
| `errors_total` | counter | `category`, `module` | `ErrorCategory` rollup |

### Recording metrics from the existing codebase

The Sprint 2 ContextVars + ErrorCategory constants are forward-compatible. Sprint 5 work will:
1. Add `metrics.py` module declaring all counters/histograms
2. Wrap `RequestContextMiddleware` to record `http_*` metrics
3. Wrap `ai_router.chat()` to record `llm_*` metrics
4. Wrap `signal_memory.log_signal()` to record `signals_emitted_total`

No refactor of business logic required.

### Alerting

Initial rules (configured in Prometheus alertmanager, not in our codebase):

| Rule | Condition | Severity |
|---|---|---|
| `AgentStuck` | `time() - agent_last_success_timestamp{agent="X"} > 3 * tick_interval` | warning |
| `DLQDepthHigh` | `event_dlq_depth > 50` | warning |
| `CircuitOpenSustained` | `avg_over_time(circuit_open{service="X"}[10m]) > 0.5` | critical |
| `DailyLLMBudgetExceeded` | `increase(llm_call_cost_usd_total[1d]) > 5` | critical (cuts cheap-model fallback) |
| `HTTPErrorRateHigh` | `rate(http_requests_total{status=~"5.."}[5m]) > 0.05` | critical |
| `RequestLatencyP99High` | `histogram_quantile(0.99, http_request_duration_seconds) > 2` | warning |

---

## 3. Log retention and rotation (action item, can be done in Sprint 2 followup)

Docker's default `json-file` driver writes unbounded log files. On a small VPS, this can fill the disk silently.

### Recommended `docker-compose.prod.yml` block

Add to the `market-terminal` service:

```yaml
services:
  market-terminal:
    # ... existing config ...
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
        compress: "true"
```

Effect: keeps at most 5 × 50MB = 250MB of compressed logs per container.

Same block recommended for `redis` and `caddy` services.

### Activation

Requires a container restart:
```bash
ssh root@<vps>
cd /opt/zyvora
docker compose -f docker-compose.prod.yml up -d --force-recreate
```

This is out-of-band of Sprint 2 because it needs user-controlled deploy. Documented here so the user can apply it whenever convenient.

### Long-term (Sprint 5 or sooner if disk fills)

Move to a log shipper. Options ranked by complexity:

1. **Vector + local file sink** — vector.dev binary tail's docker logs, writes compressed files to S3/B2 with date-based naming. Lowest complexity.
2. **Promtail + Loki** — if you want Grafana querying of historical logs.
3. **CloudWatch / Datadog / Sentry** — managed services; cost scales with volume.

Recommend option 1 first — preserves the structured JSON format without locking into a vendor.

---

## 4. Tracing — planned (Sprint 4+, conditional)

### When to add OTel

Only adopt tracing when:
- A reasoning chain (LangGraph) shows up in latency complaints, AND
- Logs alone can't tell you which step is slow

Until then, logs + `duration_ms` fields are enough.

### Design (when it lands)

```python
# core/tracing.py — added in Sprint 4 only if needed
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,  # dev
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # prod

def setup_tracing():
    provider = TracerProvider()
    if os.environ.get("OTEL_EXPORTER") == "otlp":
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
```

Then in `RequestContextMiddleware`:
```python
async def __call__(self, scope, receive, send):
    # ... existing setup ...
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("http.request") as span:
        span.set_attribute("http.method", scope.get("method"))
        span.set_attribute("http.path", scope.get("path"))
        # Sprint 2 trace_id_var becomes the OTel trace_id automatically
        trace_id_var.set(format(span.get_span_context().trace_id, "032x"))
        await self.app(scope, receive, send_wrapper)
```

### Where traces should NOT propagate (yet)

- WebSocket pipes (high-volume, low value)
- Bulk-ingest agents (per-fetch tracing dwarfs the work)
- Background loops that tick every <1s

Default: only trace request paths and reasoning chains. Re-evaluate when there's a real performance question.

---

## 5. Health endpoints (extend, don't replace)

The existing `/api/health` (per `PRODUCTION.md`) probes 18 SQLite DBs + Redis + 8 background loops. Sprint 3 will extend this with per-agent heartbeats from `BaseAgent.health()`:

```
GET /api/agents
  → [
      {"name": "news.fetch", "family": "news", "status": "ok",
       "last_run_ago_s": 42, "last_error": null, "tick_interval": 60},
      {"name": "signal.critic", "family": "signal", "status": "ok",
       "last_run_ago_s": 3, "last_error": null, "tick_interval": null},
      ...
    ]
```

Until Sprint 3, the existing `/api/health` covers the safety case (caddy probes it, would alert via UptimeRobot etc.).

---

## 6. Observability vs. cost trade-offs

| Item | Cost | When to add |
|---|---|---|
| Stdlib logging (Sprint 2) | ~negligible | ✅ shipped |
| Prometheus `/metrics` endpoint | <5MB memory; sub-ms scrape | Sprint 5 — when there's >1 metric question per day |
| Local Grafana | ~200MB memory; separate container | Sprint 5 |
| OpenTelemetry SDK | ~10MB; ~1ms per span | Sprint 4+, conditional on a real perf question |
| Cloud log shipper (Loki/CW) | $5–50/mo depending on volume | When disk pressure or log search slowness becomes a daily pain |
| Distributed tracing backend (Tempo/Jaeger) | ~500MB; separate container | Only after agents split across containers (Stage 7 of MULTI_AGENT_PLAN) |
| Sentry (error tracking) | Free tier sufficient at current scale | Add early if uncaught exceptions become a debugging bottleneck |

Operating principle: **add observability when you have an unanswered question, not in anticipation of one**.

---

## 7. Roadmap summary

| Sprint | Pillar | Deliverable |
|---|---|---|
| 2 (current) | Logs | Phase A — logging_config + middleware. **Done.** |
| 3 | Logs (continuation) | Migrate ~50% of `print()` calls (data ingest + signal layer); tenant_id_var populated in route handlers; `agent_name_var` populated by `BaseAgent.run_once` |
| 4 | Traces (conditional) | OTel SDK + `tracing.py` if and only if a reasoning chain hits a latency complaint |
| 4 (also) | Metrics (BudgetAgent only) | LLM call counter + cost gauge; `BudgetAgent` reads these |
| 5 | Metrics (full) | `/metrics` endpoint with the full table in §2; recording wrappers added incrementally |
| 5 | Log retention | docker-compose `logging` block applied during a scheduled restart |
| 6 (gated) | Off-host shipping | Vector → S3 or Loki, when local volume exceeds 1GB/week or when a paid-user incident needs historical search |

---

## 8. Decision log

| Decision | Rationale | Date |
|---|---|---|
| Use stdlib `logging` not `structlog` / `loguru` | One less dep. Existing 8 modules already use stdlib. JSON formatter is 50 lines of code. | 2026-05-18 |
| ContextVar for correlation, not threading.local | Async-safe across `await`. Required for FastAPI. | 2026-05-18 |
| Default `LOG_FORMAT=console` | Preserve current `docker logs` UX. Flip to JSON when shipper exists. | 2026-05-18 |
| Reserve `trace_id_var` for OTel | Avoid migration when OTel arrives. | 2026-05-18 |
| Defer Prometheus to Sprint 5 | No metric question is unanswered today. Bigger ROI on agent infra first. | 2026-05-18 |
| Defer OTel to Sprint 4+ conditionally | Tracing only earns its cost when reasoning chains are slow. | 2026-05-18 |
