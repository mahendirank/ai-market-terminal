# REDIS_STREAMS_TOPOLOGY.md

> Concrete topology of Redis Streams for the five planned agent families.
> Sprint 3 ships the bus; Sprint 4 starts producing/consuming. This file
> is the contract for which agents own which streams.

---

## 1. Stream / consumer-group map (Sprint 4–5 target)

```
                       ┌────────────────────────────────┐
                       │ events:news:raw                │
                       │   producer: NewsFetchAgent     │
                       │   group: news.dedup            │
                       │   group: news.archive          │
                       └─────────────┬──────────────────┘
                                     │
                       ┌─────────────▼──────────────────┐
                       │ events:news:deduped            │
                       │   producer: NewsDedupAgent     │
                       │   group: news.classify         │
                       └─────────────┬──────────────────┘
                                     │
                       ┌─────────────▼──────────────────┐
                       │ events:news:classified         │
                       │   producer: NewsClassifyAgent  │
                       │   group: intel.cluster         │
                       │   group: signal.candidate_gen  │
                       └─────────────┬──────────────────┘
                                     │
       ┌─────────────────────────────┼─────────────────────────────┐
       │                             │                             │
       ▼                             ▼                             ▼
┌─────────────────┐         ┌─────────────────┐         ┌────────────────┐
│ events:intel:   │         │ events:signal:  │         │ events:ui:     │
│   cluster       │         │   candidate     │         │   broadcast    │
│   (intel pods)  │         │   (raw signals) │         │   (UI updates) │
└────────┬────────┘         └────────┬────────┘         └────────────────┘
         │                           │                     ▲
         ▼                           ▼                     │
┌─────────────────┐         ┌─────────────────┐            │
│ events:intel:   │         │ events:signal:  │            │
│   narrative     │         │   approved      │────────────┤
│   (LLM-narrated)│         │   (critic OK)   │            │
└─────────────────┘         └────────┬────────┘            │
                                     │                     │
                                     │                     │
                            ┌────────▼────────┐            │
                            │ events:signal:  │            │
                            │   rejected      │            │
                            │   (audit only)  │            │
                            └─────────────────┘            │
                                                           │
                       ┌───────────────────────────────────┘
                       │
              ┌────────▼────────┐
              │  WebSocket pipe │
              │  (per-client    │
              │   fan-out)      │
              └─────────────────┘
```

**Risk family attaches as cross-cutting observers, not in the pipeline**:

```
events:signal:approved ──┐
events:signal:rejected ──┼──► [CooldownAgent, BudgetAgent,
events:llm:call_made   ──┘     DrawdownAgent, CircuitBreakerAgent]
                                      │
                                      ▼
                            system:circuit_open:{service}
                            system:budget:exceeded
                            (pub/sub, not streams)
```

Risk agents read; they don't sit inside the main flow. They emit
"system" signals via Redis pub/sub for fast broadcast.

---

## 2. Naming canon

| Pattern | Use |
|---|---|
| `events:<family>:<verb>` | Primary streams. Producer-owned. |
| `dlq:<family>:<verb>` | Dead-letter parallel stream. Auto-routed by `publish_to_dlq`. |
| `system:<key>` | Pub/sub key for ephemeral system signals (circuit_open, budget exceeded). NOT streams. |
| `state:<key>` | Redis hash for latest-value reads (current regime, current price tile). NOT streams. |
| `tmp:*` | Test or replay streams. Prod consumers ignore. |
| `replay:*` | One-off replay scenarios. Manually populated, manually drained. |

Examples in flight today (in tests):
```
events:news:fetched
events:smoke:signal.candidate    (test stream)
events:test:emitted              (test stream)
events:test:test.emitted         (test stream)
dlq:news:fetched
dlq:smoke:signal.candidate
```

---

## 3. Consumer-group rules of thumb

| Question | Answer |
|---|---|
| "Multiple agent TYPES consume one stream?" | Yes — each type uses its OWN consumer group. Stream events fan out: each group gets a copy. |
| "Multiple INSTANCES of one agent type (Sprint 7+)?" | Yes — same consumer group, different consumer names. Load-balanced within the group. |
| "An agent that ONLY observes (audit, metrics)?" | Its own consumer group. Doesn't block production consumers. |
| "Replay an old event?" | Manually XADD into the original stream (or a `replay:` stream and have consumers temporarily subscribe). |

---

## 4. Stream sizing (initial guesses, tune in Sprint 5)

| Stream | Producer rate (steady) | max_len | TTL approximation |
|---|---|---|---|
| `events:news:raw` | ~10/min | 5000 | ~8 hours at steady rate |
| `events:news:deduped` | ~5/min | 5000 | ~16 hours |
| `events:news:classified` | ~5/min | 5000 | ~16 hours |
| `events:intel:cluster` | ~1/min | 1000 | ~16 hours |
| `events:intel:narrative` | ~0.2/min (LLM cost gates) | 500 | ~40 hours |
| `events:signal:candidate` | ~2/min | 2000 | ~16 hours |
| `events:signal:approved` | ~1/min | 5000 | ~3.5 days |
| `events:signal:rejected` | ~1/min | 2000 | ~30 hours |
| `events:ui:broadcast` | ~30/min (fan-out) | 1000 | ~30 minutes |
| `dlq:*` | <0.1/min | 200 | ~30 hours |

**Total Redis usage at these caps**: ~30k events × ~1KB envelope = ~30MB. Well within the 256MB cap.

---

## 5. Health probes

Sprint 4 adds `GET /api/streams/health` (proposed):

```json
{
  "streams": [
    {
      "stream": "events:news:raw",
      "length": 12,
      "max_len": 5000,
      "fill_pct": 0.24,
      "consumer_groups": [
        {"name": "news.dedup", "pending": 0, "last_delivered": "..."},
        {"name": "news.archive", "pending": 1, "last_delivered": "..."}
      ]
    },
    {
      "stream": "dlq:news:raw",
      "length": 2,
      "alert": "non-empty"
    }
  ]
}
```

**Alerting**:
- Stream length > 80% of max_len → consumer is falling behind, raise consumer count or grow cap.
- Group `pending` > some threshold for >5 minutes → consumer crashed mid-handle; reclaim via XAUTOCLAIM (Sprint 4 todo).
- ANY `dlq:*` stream non-empty → operator should inspect.

---

## 6. Ordering & idempotency caveats

### Within one stream
Events are delivered in publish order to consumers in a group. No
cross-stream ordering — if you need ordered processing across stages,
put them in the same stream OR design consumers to tolerate out-of-order.

### Within one consumer group with N consumers (Sprint 7+)
Each event goes to exactly ONE consumer in the group, but the ORDER
across consumers is NOT preserved. If you need strict order, run one
consumer per group.

### Idempotency
`EventEnvelope.idempotency_key` is reserved. Consumers that need it
should:
1. Check `seen:{idempotency_key}` in Redis (SET with TTL 24h).
2. If present, ack and return without processing.
3. If absent, process; on success, write `seen:{key}` with TTL.

Sprint 4 adds this only for consumers where double-processing is
expensive (Telegram dispatch, signal emission). Most consumers can
skip.

---

## 7. Topology evolution: where this goes

| Stage | Topology |
|---|---|
| **Sprint 3 (now)** | One smoke test stream (`events:test:*`). InMemory only. |
| **Sprint 4 (first agent)** | One real stream (`events:news:raw`), one producer, one observer consumer (logs only). |
| **Sprint 4 (more agents)** | News pipeline (raw → deduped → classified). Three consumer groups. |
| **Sprint 5 (intel + signal)** | Signal candidate/approved/rejected streams. SignalCritic in observe mode. |
| **Sprint 5 (UI fan-out)** | UI broadcast stream. WebSocket subscribers per tenant. |
| **Sprint 6 (risk)** | Risk family wired as audit consumers. Pub/sub for system signals. |
| **Sprint 7+ (scale)** | Possibly partition by tenant hash for high-volume streams. |

---

## 8. Anti-topologies (don't do these)

| Don't | Why |
|---|---|
| One mega-stream `events:all` | Loses fan-out semantics; every consumer reads everything. |
| Tenant ID in stream name (`events:news:fetched:tenant42`) | Routing by topology doesn't scale; use payload-based routing. |
| Environment in stream name (`events:prod:news:*`) | Different envs = different Redis instances. |
| Cyclical stream references (A → B → A) | Recipe for runaway. Always one-directional. |
| Critic-as-stream-of-critics (event:critic:critic:critic) | Critics are inline functions, not stream stages. |
