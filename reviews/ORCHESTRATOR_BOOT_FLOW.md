# ORCHESTRATOR_BOOT_FLOW.md

> How the orchestrator boots inside the FastAPI lifespan after Sprint 4.1.

---

## 1. Boot sequence with flag OFF (default — no change from pre-Sprint-4)

```mermaid
sequenceDiagram
    autonumber
    participant U as uvicorn
    participant L as dashboard_api.lifespan
    participant E as existing startup
    participant A as app.state

    U->>L: enter lifespan
    L->>E: HNI restore, morning note, threading.Thread × 2,<br/>signal_verify_loop, macro_desk_snap_loop,<br/>explainer_scan_loop, alert_engine_loop,<br/>price_publisher_loop (all unchanged)
    Note over L: ── Sprint 4.1 block starts ──
    L->>A: orchestrator = None
    L->>A: event_bus = None
    L->>L: try import orchestration.runtime
    L->>L: orchestrator_enabled() → False
    Note over L: branch NOT taken
    Note over L: ── Sprint 4.1 block ends ──
    L->>U: yield
    Note over U: serving traffic
    U->>L: shutdown signal
    L->>L: orchestrator is None → skip stop_all
    L-->>U: exit lifespan
```

**Total Sprint-4.1 overhead in flag-off mode**: 1 `getattr`, 1 `try/except`, 1 env var read, 2 attribute assignments. ~50µs at boot. Zero ongoing cost.

---

## 2. Boot sequence with flag ON + in-memory bus

```mermaid
sequenceDiagram
    autonumber
    participant U as uvicorn
    participant L as dashboard_api.lifespan
    participant R as orchestration.runtime
    participant O as Orchestrator
    participant B as InMemoryEventBus
    participant A as app.state
    participant LG as logger

    U->>L: enter lifespan
    L->>L: existing startup (unchanged)
    L->>R: orchestrator_enabled() → True
    L->>R: build_event_bus()
    Note over R: AGENT_BUS=memory or<br/>AGENT_BUS=auto + no REDIS_URL
    R->>B: InMemoryEventBus()
    R-->>L: bus
    L->>R: build_orchestrator()
    R->>O: Orchestrator(max_consecutive_failures=5)
    R-->>L: orch
    L->>A: app.state.event_bus = bus
    L->>A: app.state.orchestrator = orch
    L->>LG: log "orchestrator_lifespan_started"<br/>{registered_agents: 0, bus: InMemoryEventBus}
    L->>U: yield
    Note over U: serving traffic
    U->>L: shutdown signal
    L->>O: stop_all(timeout=30)
    Note over O: zero agents → no-op
    L->>LG: log "orchestrator_lifespan_stopped"
    L-->>U: exit lifespan
```

---

## 3. Boot sequence with flag ON + Redis available

```mermaid
sequenceDiagram
    autonumber
    participant L as lifespan
    participant R as runtime
    participant RP as redis.asyncio.from_url
    participant B as RedisEventBus
    participant LG as logger

    L->>R: build_event_bus()
    Note over R: AGENT_BUS=auto, REDIS_URL set
    R->>RP: aioredis.from_url(REDIS_URL)
    RP-->>R: client
    R->>RP: await client.ping()
    RP-->>R: PONG
    Note over R: fail-fast ping — surface<br/>unreachable Redis at startup
    R->>B: RedisEventBus(client)
    R-->>L: bus
    L->>LG: log "event_bus_init"<br/>{mode: redis, url_prefix: ...}
```

---

## 4. Failure modes during boot

```mermaid
flowchart TD
    Start([lifespan begins]) --> Existing[All existing startup runs]
    Existing --> Sprint41[Sprint 4.1 block starts]
    Sprint41 --> InitState[app.state.orchestrator = None<br/>app.state.event_bus = None]
    InitState --> ImportTry{import orchestration.runtime}
    ImportTry -- fails --> LogImport[print 'ORCHESTRATOR import failed']
    LogImport --> Yield([yield — serving traffic])
    ImportTry -- ok --> FlagCheck{orchestrator_enabled?}
    FlagCheck -- false --> Yield
    FlagCheck -- true --> BuildTry{build_event_bus + build_orchestrator}
    BuildTry -- raises --> LogInit[log 'orchestrator_init_failed_falling_back_to_disabled']
    LogInit --> ResetState[state stays None]
    ResetState --> Yield
    BuildTry -- ok --> SetState[app.state.event_bus = bus<br/>app.state.orchestrator = orch]
    SetState --> LogStart[log 'orchestrator_lifespan_started']
    LogStart --> Yield

    style Yield fill:#d6f0d6
    style LogImport fill:#ffe4d6
    style LogInit fill:#ffe4d6
```

**Key property**: at every branch the worst-case outcome is "orchestrator stays None, app boots normally". No branch can prevent FastAPI from serving traffic.

---

## 5. Shutdown sequence

```
        ┌───────────────────────────┐
        │  shutdown signal received │
        └────────────┬──────────────┘
                     │
                     ▼
        ┌──────────────────────────────────┐
        │ getattr(app.state, "orchestrator")│
        └────────────┬─────────────────────┘
                     │
              ┌──────┴───────┐
              │              │
            None         Orchestrator
              │              │
              │              ▼
              │   await orch.stop_all(timeout=30)
              │              │
              │     ┌────────┴────────┐
              │     │                 │
              │  succeeds         times out
              │     │                 │
              │     ▼                 ▼
              │  log success      log failure
              │     │                 │
              ▼     ▼                 ▼
        ┌──────────────────────────────────┐
        │           lifespan exits         │
        └──────────────────────────────────┘
```

`stop_all` is a no-op when no agents are registered (Sprint 4.1
condition). Stage 4.3 onwards, when real agents register, each gets
~30s to drain.

---

## 6. Resource cost of the empty orchestrator

| Resource | Flag OFF | Flag ON (Stage 4.1, 0 agents) | Delta |
|---|---|---|---|
| Memory (Python heap) | baseline | + ~2 MB | negligible |
| Open Redis connection | 0 | 1 (if AGENT_BUS=redis) | +1 |
| Asyncio tasks | (unchanged) | 0 new (no agent loops) | 0 |
| Background threads | (unchanged) | 0 new | 0 |
| File descriptors | (unchanged) | +1 (Redis socket) | +1 |
| CPU | (unchanged) | ~0% steady (no work to do) | ~0 |

Measured during testing: 0 agents = orchestrator is dormant data. The
overhead is constant and tiny.

---

## 7. Where to find the boot details in logs

When `AGENT_ORCHESTRATOR_ENABLED=true`:

```
# Console format:
2026-05-XX HH:MM:SS INFO    [orchestration.runtime] [-] event_bus_init {"mode":"memory"}
2026-05-XX HH:MM:SS INFO    [orchestration.lifespan] [-] orchestrator_lifespan_started {"registered_agents":0,"bus":"InMemoryEventBus"}

# JSON format (LOG_FORMAT=json):
{"ts":"...","level":"INFO","logger":"orchestration.runtime","msg":"event_bus_init","mode":"memory",...}
{"ts":"...","level":"INFO","logger":"orchestration.lifespan","msg":"orchestrator_lifespan_started","registered_agents":0,...}
```

When flag is OFF: **no `orchestration.*` log lines**. Absence confirms the gate worked.

---

## 8. Sprint 4 boot evolution

The boot flow will gradually accrete responsibilities:

| Stage | Additional boot step |
|---|---|
| **4.1 (now)** | Construct empty orchestrator + bus (when flag on). |
| 4.3 | Register `NewsFetchAgent` if `AGENT_NEWS_FETCH_ENABLED=true`. Call `start_agent('news.fetch')`. |
| 4.4 | Register `SignalCriticAgent` (observe-only). Auto-registered when orchestrator enabled — no separate flag needed. |
| 4.5 | Wrap `ai_router.chat()` and `notify.send_telegram()` calls with `with_circuit(...)` — no boot impact, just module-level config. |
| 4.6 | Start a `reclaim_loop` task that calls `RedisEventBus.reclaim_stale_pending` every 60s for each StreamAgent's stream. |

Each stage adds at most ~50ms to boot. Total Sprint 4 boot overhead
projection: ≤500ms at flag-on with all agents registered.
