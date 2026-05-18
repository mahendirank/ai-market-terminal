# MULTI_AGENT_PLAN.md — Orchestration Architecture for Zyvora Terminal

> Phase 7 artifact. **Architecture plan only — no code written.**
> Generated 2026-05-18. Targets: `~/ai-system/core/` (the real codebase repo).
> Builds on Phase 1 (`PROJECT_ANALYSIS.md`, `ARCHITECTURE_MAP.md`) and Sprint 1 (`LOGGING_STANDARD.md`).

---

## 0. Executive summary

This document proposes an evolutionary multi-agent architecture: keep the existing FastAPI + Redis + SQLite spine, formalize the 8 ad-hoc background loops as **named agents** with explicit contracts, and adopt **LangGraph** *only* for the LLM-reasoning chains (where stateful step-by-step reasoning + branching pays for its complexity). All other agents stay as plain asyncio tasks — single-process inside the `market-terminal` container — until concurrency demands change.

**The five agent families** the user named (news, market intelligence, signal validation, UI update, risk management) map cleanly onto the existing module groups identified in `ARCHITECTURE_MAP.md`. This is reorganization with explicit contracts, not a rewrite.

**Non-goals**: Kubernetes, Celery, microservice split, separate VPSes, distributed tracing across nodes, anything that adds an external system the user doesn't already operate.

---

## 1. Design principles

| # | Principle | Why |
|---|---|---|
| 1 | **One process, many agents** | 16GB M4 + single Hostinger VPS. Process boundaries cost more than they buy us today. Revisit at Sprint 4+ if concurrency demands it. |
| 2 | **Agents communicate via Redis, never via direct function calls** | Lets us swap any agent for a stub, run it in a separate process later, or rate-limit it independently. Direct calls couple lifecycles. |
| 3 | **Every agent is idempotent or carries an idempotency key** | Retry safety. A `news.ingest` that re-runs on the same headline must not double-publish. |
| 4 | **LangGraph only for reasoning, asyncio for everything else** | LangGraph shines on "LLM does step A, branches based on result, runs B or C, validates" workflows. It's overkill for "fetch a URL, parse JSON, write to Redis." |
| 5 | **Critic agents are first-class, not optional** | Every primary agent has a peer Critic that validates output *before* it propagates. Halves the blast radius of an LLM hallucination or rule misfire. |
| 6 | **Structured JSON logs + OTel traces from day one** | Multi-agent systems are unobservable by default. Sprint 1's `LOGGING_STANDARD.md` Phase A is a prerequisite. |
| 7 | **Bounded retries with dead-letter queue** | Infinite retry loops are how multi-agent systems silently melt. DLQ + alert on DLQ depth is non-negotiable. |
| 8 | **Per-tenant fan-out at the edge, not the core** | Agents reason about market state once; UI agents fan that state out to N tenants. Multi-tenant lives in one layer, not every layer. |

---

## 2. Current state → target state

### What exists today (per Phase 1 + PRODUCTION.md)

```
                        FastAPI (dashboard_api.py)
                                  │
            ┌─────────────────────┼─────────────────────┐
            │                     │                     │
   ┌────────▼───┐        ┌────────▼─────┐      ┌────────▼─────┐
   │ Background │        │ Route        │      │ WebSocket    │
   │ loops × 8  │        │ handlers     │      │ (streaming)  │
   │ (asyncio)  │        │              │      │              │
   └────────────┘        └──────────────┘      └──────────────┘
            │                     │                     │
            └─────────────────────┼─────────────────────┘
                                  │
                       Redis  ◄───┴───►  18× SQLite DBs
```

**Implicit "agents" already running** (the 8 background loops, names inferred — Sprint 2 will map these precisely):
- News ingest loop
- Price/quote loop
- Macro/regime refresh loop
- Earnings calendar loop
- Alert evaluation loop
- Signal verification loop
- Telegram dispatcher loop
- HNI / macro reasoning loop

They share state through global `dict` caches and SQLite. There is no contract — adding a new loop requires editing `dashboard_api.py`.

### Target state

```
                        FastAPI (dashboard_api.py)
                                  │
                     ┌────────────┼────────────┐
                     │            │            │
                  routes     orchestrator   websocket
                                 │
                  ┌──────────────┼──────────────┐
                  │              │              │
            agent_registry  event_bus     critic_registry
                  │              │              │
   ┌──────────────┴────┐      Redis     ┌──────┴────────────┐
   │                   │   pub/sub +    │                   │
   ▼                   ▼   streams      ▼                   ▼
 Ingest             Reasoning         UI Fanout         Risk
 agents             agents            agents            agents
 (asyncio)          (LangGraph)       (asyncio)         (asyncio)
   │                   │                  │                  │
   └─────────── shared state ─────────────┴──────────────────┘
                  Redis (hot)
                  Postgres (durable — Sprint 4+; SQLite today)
```

Same process, same containers, same Caddy/Redis topology. What changes:
- A thin `orchestrator` module mediates agent start/stop, registry lookup, and circuit-breaker state.
- Every loop becomes a `BaseAgent` subclass with: `name`, `tick_interval`, `run_once()`, `health()`, `on_error()`.
- Reasoning chains (macro_reasoning, decision_engine, explainer) get LangGraph state machines.
- Critic agents wrap the high-risk primaries.

---

## 3. The five agent families

Each family below lists: **purpose, primary agents, critic agents, inputs, outputs, retry policy, where the code already lives**.

### 3.1 News analysis family

**Purpose**: ingest headlines from many sources, dedupe, classify by event type, score by importance, summarize, push to downstream consumers.

| Agent | Role | Existing module |
|---|---|---|
| `NewsFetchAgent` | Pull from RSS / Telegram / NSE / Bloomberg / FRED announcement feeds. | `news.py`, `news_fetch.py`, `telegram_news.py` |
| `NewsDedupAgent` | Suppress near-duplicates within a 6h window. | `news_deduper.py` |
| `NewsClassifyAgent` | Map to event taxonomy (`fed_speak`, `geopolitical`, `earnings_beat`, etc.) via `event_classifier.py`. | `event_classifier.py` |
| `NewsScoreAgent` | Score importance 0–100 (regime impact, market cap touched, recency, source reliability). | scattered logic in `dashboard_api.py` |
| `NewsSummarizeAgent` *(LLM)* | Produce a 2-sentence digest. **LangGraph node.** | new |
| **`NewsCriticAgent`** | Reject items where score is high but source is low-reliability, or where dedup confidence is below threshold. | new |

**Inputs**: source URLs/tokens from `sources_config.py`.
**Outputs**: events to Redis stream `events:news`, structured rows in `news.db`.
**Retry policy**: per-source circuit breaker (fail open after 5 consecutive errors, retry every 5 min). Idempotency key = `sha256(source_url + headline)`.
**Throughput target**: ≤ 200 headlines/hour steady state; spike absorbed by a bounded Redis stream (cap 5000).

### 3.2 Market intelligence family

**Purpose**: synthesize news + prices + macro + regime into structured intel briefs that downstream signal/risk/UI agents consume. This is the layer where most LLM cost lives.

| Agent | Role | Existing module |
|---|---|---|
| `RegimeAgent` | Maintain the 6-dimensional regime vector (RISK/INFLATION/FED/VOL/CREDIT/BREADTH). | `regime.py`, `regime_engine.py` |
| `CorrelationAgent` | Recompute cross-asset correlations; flag breaks. | `correlation_engine.py`, `correlations.py` |
| `MacroReasoningAgent` *(LangGraph)* | Multi-step reasoning: gather signals → form thesis → check contradictions → output structured macro narrative. | `macro_reasoning_engine.py`, `macro_analyst.py` |
| `IntelClusterAgent` | Cluster related news+signals into "intel pods" (a Fed-speak event + bond reaction + USD reaction = one pod). | `intel_cluster.py` |
| `ExplainerAgent` *(LangGraph)* | "Why did X move?" — multi-step: pull context → form hypotheses → score each → pick best. | `explainer.py` |
| **`MacroCriticAgent`** | Reject macro narratives that contradict the regime vector or omit a current top-3 event. | new |

**Inputs**: `events:news`, `events:price_update`, current regime state from Redis.
**Outputs**: intel briefs to Redis hash `intel:current`, narrative to `intel:narrative:{tenant_id}` for per-tenant variation.
**Retry policy**: LLM providers have built-in retries (via `ai_router.py`). Outer retry: 3× with exponential backoff. On DLQ, alert + fall back to last-known-good intel.

### 3.3 Signal validation family

**Purpose**: take candidate trade signals from the decision engine, validate against history/regime/correlations/risk limits, and either emit them or kill them with a logged reason.

| Agent | Role | Existing module |
|---|---|---|
| `DecisionAgent` | Generate candidate signals from regime+correlations+price action. | `decision_engine.py`, `trade_signal.py` |
| `HistoryAgent` | Look up historical performance of this regime + signal type. | `signal_memory.py`, `signal_store.py` |
| `RegimeCheckAgent` | Reject signals contradicted by the current regime vector. | new — uses `regime_engine.py` |
| `CorrelationCheckAgent` | Reject signals whose asset is currently breaking from its peer correlation. | new — uses `correlation_engine.py` |
| **`SignalCriticAgent`** | Composite review: regime + history + correlation + cooldown. Approve/reject + structured reason. | new |
| `SignalEmitAgent` | Persist approved signal, fire downstream (alerts, UI, Telegram). | `alert_engine.py` partial |

**Inputs**: candidate signals on `events:signal_candidate`.
**Outputs**: approved signals to `events:signal_emit`; rejected to `events:signal_rejected` (with reason — feeds back into ML later).
**Retry policy**: validation is deterministic — no retry. Errors in lookup (e.g. SQLite locked) get 3× retry then fail-safe to "reject with reason: validator_unavailable".

**Why this family deserves a Critic**: a false signal that fires to subscribers is the most expensive failure mode in the system. Doubling the validation layer ≠ double cost — most rejections are deterministic and cheap.

### 3.4 UI update family

**Purpose**: turn agent events into WebSocket pushes to N tenant sessions, with debouncing, deduping, and per-tenant filtering.

| Agent | Role | Existing module |
|---|---|---|
| `PriceBroadcastAgent` | Subscribe to `events:price_update`, debounce by 1s, fan out to subscribed WS connections. | `streaming.py` |
| `AlertBroadcastAgent` | Subscribe to `events:signal_emit`, render per-tenant payload, push. | `streaming.py` |
| `IntelBroadcastAgent` | Subscribe to `intel:current` changes, push to subscribed tenants. | new |
| `WSConnectionManager` | Track active connections, channel subscriptions, heartbeat. | `streaming.py` |
| **`UICriticAgent`** | Validate every outbound WS message against a JSON schema before send. Drop + log on schema fail (do not crash the WS pipe). | new |

**Inputs**: every emit stream.
**Outputs**: bytes on WebSocket.
**Retry policy**: WS sends do NOT retry — if a connection is dead, drop and let the client reconnect. The `WSConnectionManager` handles dead-connection cleanup.

**Key constraint**: UI agents must NEVER do an LLM call inline. All LLM output is pre-computed by Family 3.2 and read from Redis. UI latency budget = 50ms p99.

### 3.5 Risk management family

**Purpose**: prevent the system from doing harm — spamming alerts, exceeding rate limits, ignoring cooldowns, missing drawdown thresholds.

| Agent | Role | Existing module |
|---|---|---|
| `CooldownAgent` | Enforce per-event-class cooldowns. | `alert_engine.py:_set_cooldown / _cooldown_active` |
| `RateLimitAgent` | Cap Telegram dispatches per hour, LLM API calls per minute, WS messages per tenant per minute. | scattered |
| `CircuitBreakerAgent` | Track external API health (yfinance, NSE, Groq, Anthropic, Telegram). Open the circuit after N failures, half-open after timeout. | new |
| `DrawdownAgent` | Watch live signal performance; if rolling 7-day win rate drops below floor, raise confidence threshold and alert admin. | `signal_memory.py:get_analytics` |
| `BudgetAgent` | Track daily $ spend on AI providers; throttle or fall back to cheaper model when 80% of daily budget consumed. | new — `ai_router.py` already logs per-call cost to `ai_calls.db` |
| **`RiskCriticAgent`** | Meta-critic: aggregate signals from the other risk agents. If ≥2 raise concerns, escalate to admin Telegram + lower confidence threshold globally. | new |

**Inputs**: `events:signal_emit`, `events:llm_call`, `events:external_api`.
**Outputs**: gates on every other family (pub `system:circuit_open:{service}` keys in Redis that other agents check before calling).
**Retry policy**: risk agents do not retry — they are the *retry policy* for everyone else.

**The Drawdown and Budget agents are why this family exists.** Without them, a bad signal regime can spam clients while bleeding $50/day in LLM cost.

---

## 4. Orchestration layer

### 4.1 `BaseAgent` contract

```python
class BaseAgent:
    name: str                          # e.g. "news.fetch", "signals.critic"
    tick_interval: float | None        # seconds; None = event-driven
    subscribes_to: list[str]           # Redis streams / pub-sub channels
    publishes_to: list[str]            # for static analysis + observability
    requires_circuit: list[str] = []   # external services this agent depends on

    async def run_once(self) -> None: ...
    async def on_start(self) -> None: ...
    async def on_stop(self) -> None: ...
    async def health(self) -> dict: ... # heartbeat, last_run_ts, error count
```

Agents are registered via decorators:

```python
@register(family="news", critical=True)
class NewsFetchAgent(BaseAgent):
    name = "news.fetch"
    tick_interval = 60
    publishes_to = ["events:news"]
    requires_circuit = ["yfinance", "nse"]
    ...
```

The orchestrator's `run()` discovers agents from the registry, starts asyncio tasks, and exposes a `/api/agents` endpoint listing them with health.

### 4.2 LangGraph integration

LangGraph is used **inside** specific agents (`MacroReasoningAgent`, `ExplainerAgent`, `NewsSummarizeAgent`, optionally `SignalCriticAgent`) — not as the top-level coordinator.

Example — `ExplainerAgent`'s LangGraph state machine:

```
            [start]
               │
       ┌───────▼────────┐
       │ gather_context │  ← pull regime, last 3 news items, price data
       └───────┬────────┘
               │
       ┌───────▼────────┐
       │ form_hypotheses│  ← LLM call: list 3 candidate explanations
       └───────┬────────┘
               │
       ┌───────▼────────┐
       │ score_each     │  ← LLM call per hypothesis (parallel)
       └───────┬────────┘
               │
        ┌──────▼───────┐
        │ pick_winner  │  ← deterministic: max score
        └──────┬───────┘
               │
               ▼
         emit → critic → publish
```

Why LangGraph (vs. plain function calls):
- State persistence between steps (resume mid-flow if the worker restarts)
- Branching ("if confidence < 60, run additional verification step")
- Built-in checkpointing → debuggable per-step traces
- Native async + retry hooks

Why **not** LangGraph for everything:
- It adds latency overhead (state read/write per step)
- Most ingest agents are 1-step (`fetch → publish`) — graph has 0 value
- Library churn risk; keep dependency footprint where it earns its keep

### 4.3 Shared state

| Store | Purpose | Why this choice |
|---|---|---|
| **Redis (existing)** | Hot state: current prices, regime vector, intel briefs, agent heartbeats, circuit-breaker state, rate-limit counters | Already in production. 256MB cap. Sub-ms reads. |
| **Redis Streams** | Durable event bus: `events:news`, `events:signal_candidate`, etc. Bounded length (XADD MAXLEN ~5000). Consumer groups give at-least-once semantics. | Better than pub/sub when subscribers can fall behind. Already supported by `redis>=5.0`. |
| **SQLite (existing, 18 DBs)** | Durable agent-specific state: signal history, alert dedup keys, news cache, per-tenant settings. | Today. Migrate hot tables to Postgres in Sprint 4+ (per Phase 1 §9). |
| **LangGraph checkpoints** | Mid-reasoning state for reasoning agents | New: separate SQLite file `core/db/langgraph_checkpoints.db`. Cheap, isolated. |

**No global Python dicts as shared state.** All cross-agent state goes through Redis (with TTL or explicit eviction) or SQLite.

### 4.4 Communication patterns

| Pattern | When to use | Channel example |
|---|---|---|
| **Redis Streams** | At-least-once event delivery between producers and consumer groups | `events:news`, `events:signal_candidate` |
| **Redis Pub/Sub** | Fire-and-forget broadcast (UI updates, circuit-state changes) | `system:regime_changed`, `system:circuit_open:groq` |
| **Redis Hash (TTL)** | Latest-value reads (current price tile, current regime) | `state:price:NQ`, `state:regime:current` |
| **SQLite** | Audit log, historical queries, durable per-tenant data | `signal_memory.db`, `auth.db` |
| **LangGraph checkpoint** | Multi-step reasoning resumable across restarts | one row per reasoning task |

### 4.5 Critic pattern

Every "high-stakes" agent has a paired Critic. Critic invariants:

1. **Read-only on the primary's output** — never mutates.
2. **Idempotent** — re-running on the same input gives the same verdict.
3. **Bounded latency** — ≤ 200ms (no LLM call) for deterministic critics; ≤ 5s for LLM-backed ones.
4. **Halt-on-fail** — if critic rejects, the event does NOT propagate, period. Log + emit to `events:rejected`.

Critics are themselves agents — registered, observable, replaceable. A second-pass critic can sit on top of a first-pass critic if needed (e.g. `SignalCritic` runs first; if it passes, a `RegimeCritic` cross-checks).

---

## 5. Retry and error recovery

### Four layers

1. **Function-level**: `tenacity` decorator for transient external errors. `@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))`. For HTTP calls only.
2. **Agent-level**: `BaseAgent.on_error(exc)` increments a Redis counter. If failure rate exceeds threshold, agent self-disables for cool-down period and emits `system:agent_disabled:{name}`.
3. **Event-level (DLQ)**: a Redis stream `events:dlq` receives any event that failed N times. A `DLQReplayAgent` (manual or automated) can re-emit after fixes.
4. **Circuit breaker**: per external service, `CircuitBreakerAgent` opens the circuit on consecutive failures. Other agents read `system:circuit_open:*` before calling the service. Closed → half-open → closed lifecycle.

### Failure modes and responses

| Failure | Detected by | Response |
|---|---|---|
| External API timeout | `tenacity` retry → fail | Circuit opens; agent emits "degraded" status; intel falls back to cached |
| LLM hallucination produces bad signal | `SignalCriticAgent` | Reject + log + (Sprint 3+) feed back into prompt example set |
| SQLite WAL lock contention | `BaseAgent.on_error` | Agent retries with jitter; if persists, alert admin (likely needs Postgres migration) |
| Redis OOM (256MB cap, eviction kicks in) | Memory pressure on hot keys | `allkeys-lru` policy handles automatically; alert on eviction rate spike |
| Agent process crash | Orchestrator detects task exit | Auto-restart up to 5 times in 1h; then page admin |
| Telegram dispatch failure | `RateLimitAgent` + retry | After 3 failures, drop and log; never block downstream |

### Idempotency

Every event carries an `idempotency_key` (sha256 of natural identifiers). Consumer agents check Redis SET `seen:{key}` (TTL 24h) before acting. Re-emit on retry is safe.

---

## 6. Observability and logging

### Three pillars

#### Logs (Sprint 2 — Phase A from `LOGGING_STANDARD.md`)

Every agent log line is JSON with: `ts`, `level`, `logger` (agent name), `msg`, plus contextvars:
- `agent_name`
- `event_id` (idempotency key of event being processed, if any)
- `tenant_id` (if applicable)
- `trace_id` (OTel — see below)

Shipped to stdout → `docker logs market-terminal` → (later) Loki / CloudWatch.

#### Metrics (Sprint 3+)

Prometheus-format `/metrics` endpoint exposes:

| Metric | Type | Labels |
|---|---|---|
| `agent_tick_total` | counter | `agent_name`, `status=success\|error` |
| `agent_tick_duration_seconds` | histogram | `agent_name` |
| `agent_last_success_timestamp` | gauge | `agent_name` |
| `event_published_total` | counter | `stream` |
| `event_consumed_total` | counter | `stream`, `agent_name` |
| `event_dlq_depth` | gauge | `stream` |
| `circuit_open` | gauge (0/1) | `service` |
| `llm_call_total` | counter | `provider`, `model`, `task` |
| `llm_call_cost_usd_total` | counter | `provider`, `model` |
| `llm_call_duration_seconds` | histogram | `provider`, `model` |
| `signals_emitted_total` | counter | `decision`, `quality_label` |
| `signals_rejected_total` | counter | `reason` |

Alerts on these (via Prometheus alertmanager or simpler — a `RiskCriticAgent` that polls `/metrics`):
- `agent_last_success_timestamp` older than 3× `tick_interval` → agent stuck
- `event_dlq_depth > 50` → events piling up
- `circuit_open == 1` for >10min → external dep is down
- `llm_call_cost_usd_total` daily rate exceeding budget → BudgetAgent intervenes

#### Traces (Sprint 4+)

OpenTelemetry SDK in every agent. Spans for:
- Each `run_once()`
- Each LangGraph node
- Each external API call
- Each Critic evaluation

Export to local Tempo / Jaeger / OTel Collector. Out-of-scope for Sprints 2-3 unless reasoning chains start hitting performance issues that need step-level introspection.

### Per-agent health endpoint

`GET /api/agents` returns a list of `{name, family, last_run_ago_s, last_error, status, tick_interval}` — extends the existing `/api/health` endpoint with per-agent granularity.

---

## 7. Migration strategy (no big-bang)

This is sequenced to avoid "production = staging" risk. Each step is independently reversible.

### Stage 1 — Foundation (Sprint 2)

- Phase A logging from `LOGGING_STANDARD.md` (prereq for everything)
- Introduce `BaseAgent` ABC + `orchestrator.py` (does not yet replace anything)
- Wrap **ONE** existing background loop (recommend `news.py`'s fetch loop) in a `BaseAgent` subclass — feature-flagged, parallel to the existing loop. Verify identical behavior, then cut over.
- Add `/api/agents` endpoint with the one wrapped agent.

**Exit criteria**: one agent runs through `BaseAgent`, metrics show its tick rate, no production behavior change.

### Stage 2 — Migrate ingest family (Sprint 3, first half)

- Wrap the remaining ingest loops (news dedup, classify, score; price/quote; earnings; macro fetch).
- Introduce Redis Streams for `events:news`, `events:price_update`.
- Existing consumer code reads from streams instead of in-memory dicts. Maintain the in-memory cache as a Redis-stream consumer.

**Risk**: stream consumer lag could starve consumers. **Mitigation**: keep the legacy in-memory path live until stream consumers have ≥48h of correct behavior; feature-flag to swap.

### Stage 3 — Introduce critics (Sprint 3, second half)

- Add `SignalCriticAgent`, `MacroCriticAgent`, `UICriticAgent` — start with deterministic checks only (no LLM).
- Critics observe only at first (log decisions but don't block). Tune thresholds for 1 week.
- Flip critics to enforcing mode behind a global feature flag `CRITICS_ENFORCE=true`.

### Stage 4 — LangGraph for reasoning chains (Sprint 4)

- `MacroReasoningAgent`, `ExplainerAgent`, `NewsSummarizeAgent` migrate to LangGraph state machines.
- Checkpoints stored in `core/db/langgraph_checkpoints.db`.
- Latency budget: reasoning agents have a 30s SLO; UI never blocks on them.

### Stage 5 — Risk family (Sprint 4 cont.)

- `BudgetAgent` (immediate ROI — caps LLM spend)
- `CircuitBreakerAgent` (wraps external API calls)
- `RateLimitAgent`, `DrawdownAgent` — last, after the rest is observable.

### Stage 6 — Observability tightening (Sprint 5)

- Prometheus `/metrics` endpoint with the metric set in §6.
- Out-of-process Grafana dashboard (separate VPS container or local-only).
- Optional: OTel tracing for reasoning agents only.

### Stage 7 — Scale-out preparation (Sprint 6+, optional)

Only when single-process limits show up:
- Move ingest family to a separate `ingest-worker` container (same compose, same Redis).
- Reasoning agents to a `reasoner-worker` container.
- UI/auth stay in `market-terminal` for latency.

This is the **only** stage that touches the deployment topology. Skip indefinitely if metrics show no need.

---

## 8. Per-family resource budgets (single-process M4 / Hostinger VPS)

| Family | Target memory | Target CPU | Target net LLM $/day |
|---|---|---|---|
| News | 100 MB | 5% steady | $0 |
| Market intel (incl. LLM) | 200 MB | 10% steady, 30% peak | $5 (cap by `BudgetAgent`) |
| Signal validation | 50 MB | 2% steady | $0 (deterministic only) |
| UI update | 100 MB (incl. WS bufs) | 2% steady, 20% peak | $0 |
| Risk | 30 MB | 1% steady | $0 |
| **Total system budget** | ~480 MB | ~25% steady | **$5/day cap** |
| **Hostinger ceiling** | depends on plan; assume ≥2GB | 100% (1 vCPU min) | n/a |
| **Local M4 ceiling** | 16 GB | n/a | n/a |

Significant headroom on both. When peak memory approaches 1GB or sustained CPU exceeds 70%, that's the trigger for Stage 7 (scale-out).

---

## 9. What this plan deliberately does NOT include

| Excluded | Why |
|---|---|
| Kubernetes / k3s | Single-VPS deploy. K8s adds 5GB and 10× operational complexity for zero current benefit. |
| Celery, RQ, Dramatiq | Asyncio + Redis Streams covers our queueing needs. Adding a worker queue layer is YAGNI today. |
| Message bus alternatives (NATS, Kafka, RabbitMQ) | Redis Streams is sufficient at our event volume (target <1k events/sec). Revisit only when Redis Streams falls over. |
| Distributed tracing across nodes | Single process; intra-process trace is enough until Stage 7. |
| Microservice split | Would require service discovery, mutual TLS, separate observability for each. Single binary stays the contract. |
| n8n agent invocation | n8n is in the broader home dir but unrelated to Zyvora's runtime. If we ever want n8n-driven workflows, expose them as webhooks; don't merge architectures. |
| Multi-region | Out of scope until tenant count + revenue justify it. |
| Claude Code subagents (the IDE feature) | These run in this conversation, not in the deployed system. They are a development tool, not a runtime architecture. |

---

## 10. Open questions

These need user input before Stage 1 starts:

1. **Should the orchestrator live in `dashboard_api.py` or a new module?** Recommend new module `orchestrator.py` to keep `dashboard_api.py` purely HTTP. Confirms or contradicts the eventual `dashboard_api.py` split (TECH_DEBT §2).

2. **Per-tenant agent isolation — needed?** Today, intel is computed once and fanned out. If clients need *bespoke* intel (different watchlists driving different reasoning), some Family 3.2 agents need per-tenant variants. Cost can scale linearly with tenant count. Assume **no** until a paying client asks.

3. ~~**LLM cost cap — daily $ ceiling?**~~ **RESOLVED 2026-05-18: $5/day (~$150/mo).** `BudgetAgent` throttles to cheaper model when 80% consumed; hard-stops at 100%.

4. **Critics-as-LLMs — acceptable?** Deterministic critics are free and fast. LLM-backed critics catch hallucinations a regex won't. But they double LLM cost on the critic'd path. Recommend: deterministic critics in Sprint 3; one LLM critic (probably on `MacroReasoningAgent`) in Sprint 4 if metrics show false-positive narrative rate > 5%.

5. **`/metrics` endpoint authentication?** Today `/api/health` is open. Prometheus scraping usually wants no-auth (or token in a config). Risk: leaks operational data. Recommend bind to internal interface (`127.0.0.1`) only; Caddy doesn't proxy `/metrics`.

6. **Redis HA?** Single-instance Redis is a single point of failure. AOF persistence is on (per docker-compose snapshot interval). Redis Sentinel adds complexity. Recommend: accept single-instance until tenant count makes recovery time unacceptable.

---

## 11. Sequencing summary

| Sprint | Stage | Concrete deliverables |
|---|---|---|
| Sprint 2 (current) | Phase A logging + Stage 1 foundation | `logging_config.py`, `BaseAgent`, `orchestrator.py`, ONE migrated agent, `/api/agents` |
| Sprint 3 | Stages 2 + 3 | All ingest agents migrated; Redis Streams for `events:news` and `events:price_update`; deterministic critics in observe mode |
| Sprint 4 | Stages 4 + 5 | LangGraph for reasoning chains; `BudgetAgent` + `CircuitBreakerAgent` live |
| Sprint 5 | Stage 6 | Prometheus `/metrics`; Grafana dashboard |
| Sprint 6+ | Stage 7 (gated) | Scale-out only if metrics demand it |

**Total greenfield code estimate**: ~2,500 LOC across `orchestrator.py`, `base_agent.py`, family-specific agent classes, critic classes. Existing 27,376 LOC is wrapped, not rewritten — see §7 Stage 2 mitigation.

---

## 12. Acceptance criteria for this plan

For the user to approve before any implementation:

- [ ] Five agent families (§3) cover the requested scope (news / market intel / signal / UI / risk)
- [ ] LangGraph scope (§4.2) is intentionally narrow — only reasoning, not orchestration
- [ ] Critic pattern (§4.5) acceptable as the primary safety mechanism
- [ ] Migration strategy (§7) is staged, reversible, with no big-bang
- [ ] Open questions (§10) answered

Once these are confirmed, Sprint 2 can extend its scope from "Phase A logging only" to "Phase A logging + Stage 1 foundation" — adding `BaseAgent` + one wrapped agent without changing production behavior.
