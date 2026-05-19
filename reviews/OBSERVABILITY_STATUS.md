# OBSERVABILITY_STATUS.md

> Three-pillar status after Sprint 1–3 deploy, 2026-05-19.

---

## Pillar 1: Logs — ✅ ACTIVE

| Capability | Status | Where |
|---|---|---|
| Structured per-request log | ✅ Live (Sprint 2) | `2026-05-19 10:47:55 INFO [http.request] [da8196752855] request_complete` |
| Correlation ID propagation | ✅ Live | `X-Request-ID` header on every response; `request_id` field on every middleware log |
| ContextVar-based async isolation | ✅ Live | Verified empirically with 2 sequential requests → 2 distinct IDs |
| Trace ID reservation | ✅ Code-ready | `trace_id_var` exists; pre-populated with `-` until OTel arrives |
| Agent name reservation | ✅ Code-ready | `agent_name_var` exists; pre-populated with `-` until Sprint 4 |
| JSON log format option | ✅ Code-ready, NOT enabled | Set `LOG_FORMAT=json` in `.env` + restart to flip. Default is `console`. |
| Log rotation | ✅ Active | Docker `json-file` driver: 5 × 20 MB = 100 MB cap |
| Off-host log shipping | ❌ Not configured | Pending real need (Sprint 5–6) |

### Stable JSON envelope shape (when `LOG_FORMAT=json`)

```json
{
  "ts": "2026-05-19T10:47:55.123Z",
  "level": "INFO",
  "logger": "http.request",
  "msg": "request_complete",
  "request_id": "521b2b1bd43e",
  "tenant_id": "-",
  "trace_id": "-",
  "agent": "-",
  "method": "GET",
  "path": "/api/health",
  "status": 200,
  "duration_ms": 12.34
}
```

`request_id`, `tenant_id`, `trace_id`, `agent` always present (default `-`).

---

## Pillar 2: Metrics — ❌ NOT YET (planned Sprint 5)

No Prometheus endpoint exposed yet. Today's observability is:
- Docker stats for memory/CPU snapshot
- `/api/health` for subsystem rollup
- `docker logs` + jq for ad-hoc queries

Sprint 5 plan (per `OBSERVABILITY_PLAN.md §2`):
- `/metrics` endpoint bound to `127.0.0.1` only
- Counter set: `http_requests_total`, `agent_tick_total`, `event_published_total`, `circuit_open`, `llm_call_total`, `signals_emitted_total`, etc.
- Latency histograms via Prometheus client

---

## Pillar 3: Traces — ❌ NOT YET (conditional Sprint 4+)

Decision per `MULTI_AGENT_PLAN.md §6` and `OBSERVABILITY_PLAN.md §4`:
adopt OpenTelemetry **only when** a reasoning chain shows a latency
complaint that logs can't diagnose. Sprint 2's `trace_id_var` is the
correlation hook OTel will plug into without retrofit.

---

## How to use what's there today

### Trace a single request through the system

```bash
# Find the request_id from the response header:
curl -sI https://zyvoratech.co/api/some-endpoint | grep -i x-request-id
# x-request-id: 521b2b1bd43e

# Grep logs by it:
ssh root@72.61.173.89 'docker logs market-terminal' \
  | grep "521b2b1bd43e"
```

### Find slow requests

```bash
# Requires LOG_FORMAT=json to be set. Currently in console mode, so use awk:
ssh root@72.61.173.89 'docker logs market-terminal --tail 1000' \
  | grep request_complete \
  | awk -F'duration_ms=' '{print $2}' \
  | sort -n | tail -10
```

(For systemic latency tracking, wait for Sprint 5's histograms.)

### Find errors for one tenant

```bash
# When tenant_id_var is populated by Sprint 4+ route handlers:
ssh root@72.61.173.89 "docker logs market-terminal" \
  | grep '"tenant_id":"acme-corp".*"level":"ERROR"'
```

Today `tenant_id` is always `-` in logs, since no handler sets it yet.
Sprint 4 will add per-tenant context.

---

## Health endpoints summary

| Endpoint | Status | Auth | Purpose |
|---|---|---|---|
| `/health` | ✅ Live | none | Docker healthcheck. Returns `{"status":"ok"}` if app started. |
| `/api/health` | ✅ Live | none | Full system status: Redis, 23 SQLite DBs, key subsystems. ~50 KB JSON. |
| `/api/agents` | ❌ Sprint 4 | auth | Per-agent health snapshot |
| `/api/circuits` | ❌ Sprint 4 | auth | Per-service breaker state |
| `/api/streams/health` | ❌ Sprint 4 | auth | Redis Streams length + lag |
| `/metrics` | ❌ Sprint 5 | none (bind 127.0.0.1) | Prometheus exposition |

---

## Gaps NOT yet closed

| Gap | Why it's open | When |
|---|---|---|
| Per-tenant log enrichment | Need route handlers to set `tenant_id_var` | Sprint 4 |
| Per-agent log enrichment | Need `BaseAgent.tick()` to set `agent_name_var` | Sprint 4 (already in the agent contract; activates when agents run) |
| Trend analysis (rates over time) | Need metrics; logs are point-in-time | Sprint 5 |
| Alerting on degradation | Need metrics + alertmanager | Sprint 5 |
| Distributed tracing across reasoning chains | Need OTel SDK + collector | Sprint 4+ conditional |
| Long-term log retention | Off-host shipper not yet wired | Sprint 6 if disk pressure shows up |

---

## Observability baseline metrics (now)

Captured 2026-05-19 ~30s after restart:

| Metric | Value |
|---|---|
| Container memory | 288.8 MiB / 15.62 GiB |
| Container CPU | 10.65% |
| Log line rate (idle) | ~0.13 lines/sec (4 lines / 30s window) |
| Existing `print()` calls being emitted | many — legacy `[REGIME]`, `[NEWS]`, `[TG]` lines |
| New `request_complete` log lines | one per HTTP request (~3-5 per minute idle, peaks higher) |
| Disk used by `/opt/zyvora` repo | not measured separately; total host: 44/193 GB |
| Docker log retention | 100 MB rolling per container |

These form the **baseline** against which Sprint 4–5 changes are measured.
