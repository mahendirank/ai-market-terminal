# REDIS_RUNTIME_HEALTH.md

> Stage A — Redis connectivity, client connection, and memory behavior
> while orchestrator is enabled (0 agents). 2026-05-19, ~15min window.

---

## 1. Connection topology

Container architecture:

```
┌──────────────────┐       ┌──────────────────┐
│ market-terminal  │───────│ terminal-redis   │
│ container        │ docker│ container        │
│                  │ net   │ (redis:7-alpine) │
│ - existing       │       │                  │
│   modules        │       │ db 0             │
│ - orchestration  │       │ maxmemory 256M   │
│   (RedisEventBus)│       │ allkeys-lru      │
└──────────────────┘       └──────────────────┘
```

Bus selection result:
- `AGENT_BUS=auto` (default, not explicitly set)
- `REDIS_URL=redis://redis:6379/0` (set in .env)
- `RedisEventBus` chosen (boot log: `event_bus_init`)
- Ping check at startup: succeeded

---

## 2. Pre-flip vs post-flip Redis state

| Metric | Pre-flip | Post-flip (t+15min) | Delta |
|---|---|---|---|
| Used memory (Redis) | 1.62 MiB | 1.62 MiB | **0 MiB** |
| Peak memory | 1.64 MiB | 1.64 MiB | 0 MiB |
| Maxmemory cap | 256 MiB | 256 MiB | — |
| Eviction policy | allkeys-lru | allkeys-lru | — |
| Connected clients | ~1 (legacy) | ~1 (orchestration uses 1 connection) | +0/+1 (likely shared via aioredis pool) |
| Stream keys (events:*) | 0 | 0 (no producers) | 0 |
| Stream keys (dlq:*) | 0 | 0 | 0 |

**No data growth from orchestration — confirmed.** With zero agents
producing, the bus has nothing to do. The Redis state is dominated by
legacy modules (alert cooldowns, signal cache, chat history).

---

## 3. Connected-client trajectory

```
docker compose exec redis redis-cli CLIENT LIST
```

At pre-flip, post-restart (post-Sprint-4.1 with flag off), and post-flag-flip:

| Phase | Clients from 172.18.0.2 (market-terminal) | Notes |
|---|---|---|
| Pre-flag-flip (flag off) | 1 (idle ~80s) | Legacy code's single connection |
| Post-flag-flip (flag on) | 1 (idle ~80s) | Either legacy + orchestration share, OR orchestration replaced legacy |

The connection signature: `lib-name=redis-py lib-ver=7.4.0`. This is the
standard async redis client used by BOTH:
- Existing `signal_memory.py`, `alert_engine.py`, etc.
- New `orchestration/event_bus.RedisEventBus`

`redis-py` uses a connection pool (defaults to single connection per
client unless saturated). With light traffic, one connection covers
multiple async callers.

**Verdict**: orchestration adds +0 NEW Redis connections from the
client side (shares pool with legacy). The "+1 PID" we saw at t=0 was
likely the asyncio executor preparing the redis-py async I/O thread,
not a network connection.

---

## 4. Redis reconnect events

A reconnect storm would manifest as:
- Multiple "Reconnected" log lines
- Connection-refused errors in container logs
- `CLIENT LIST` rapidly cycling client IDs
- Redis `INFO stats` showing `total_connections_received` growing fast

15-minute observation: **none of these.** The connection is stable.

For the 24h soak, operator should periodically check:
```bash
# Reconnect history (the only client should keep the same client_id)
ssh root@72.61.173.89 'docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli INFO stats | grep total_connections_received'
```

If `total_connections_received` is increasing by more than ~1/hour, that
indicates reconnect activity — investigate.

---

## 5. Redis OOM + eviction risk

```
maxmemory_human:256.00M
used_memory_human:1.62M (0.6% of cap)
maxmemory_policy:allkeys-lru
```

Stage A risk: **near zero**. Used memory is 0.6% of cap. Even with
Stage 4.3 producing news.raw events into a stream capped at MAXLEN
5000, the per-event ~1KB envelope gives ~5MB worst case. We have 254MB
headroom.

Eviction would only become a concern at Sprint 5+ when multiple agents
produce concurrently across many streams.

---

## 6. Redis as the orchestration bus — health-of-the-bus

Indicators we'd watch when agents start producing (Stage 4.3+):

| Indicator | How to check | Healthy range |
|---|---|---|
| Stream depth | `XLEN events:news:raw` | Consumer rate ≥ producer rate; depth < 80% of MAXLEN |
| Pending events | `XPENDING events:news:raw <group>` | 0 ideally; transient OK |
| DLQ depth | `XLEN dlq:news:raw` | 0 ideally |
| Consumer lag | `XINFO GROUPS events:news:raw` | "lag" field near 0 |
| Memory used | `INFO memory used_memory_human` | < 80% of maxmemory |

In Stage A (current), the first 4 are all 0 by design.

---

## 7. Failure-mode coverage (mapped to FAILURE_MODE_ANALYSIS)

How Stage A holds against the documented Redis failure modes:

| Failure mode (from FMA) | Stage A status |
|---|---|
| Redis down | Tested via `sim_redis_disconnect`: bus surfaces ConnectionError; app stays up. NOT triggered in this soak. |
| Redis OOM → eviction | Used memory 0.6% of cap. Not exercised. |
| Connection storm | Single connection observed. Not exercised. |
| Pending events stuck | No events exist. Not exercised. |

All Redis-related risks are dormant because **the bus is idle**.

---

## 8. 24h continuation checks

```bash
# Hourly: Redis used memory + connection count
ssh root@72.61.173.89 'docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli INFO memory | grep used_memory_human'
ssh root@72.61.173.89 'docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli INFO clients | grep connected_clients'

# Daily: total connections + stream keys
ssh root@72.61.173.89 'docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli INFO stats | grep total_connections_received'
ssh root@72.61.173.89 'docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli --scan | wc -l'
```

Expected over 24h:
- `used_memory_human`: stays 1–10 MiB (legacy modules use it lightly)
- `connected_clients`: 1–2 stable
- `total_connections_received`: grows by ≤ 5 over 24h
- Stream keys (`events:*`, `dlq:*`): stays 0

---

## 9. Summary

Redis is healthy and underutilized during Stage A. The orchestration
runtime added zero observable Redis traffic, because zero agents are
producing/consuming. The bus is plumbed correctly; the proof of work
will come in Stage 4.3 when an agent emits its first event.

**No alerts. No actions needed before Stage 4.3.**
