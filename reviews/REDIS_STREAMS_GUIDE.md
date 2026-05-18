# REDIS_STREAMS_GUIDE.md

> Sprint 3 deliverable. How streams are named, who consumes what,
> what happens when an event fails, and how backpressure works.

---

## 1. Stream naming

```
events:<family>:<event_type>          primary stream
dlq:<family>:<event_type>             dead-letter for that stream
```

Examples:
```
events:news:fetched
events:news:deduped
events:signal:candidate
events:signal:approved
events:signal:rejected
events:risk:circuit_opened

dlq:news:fetched
dlq:signal:candidate
```

Helpers:
```python
from orchestration import stream_name, dlq_stream_name

stream_name("news", "fetched")        # "events:news:fetched"
dlq_stream_name("events:news:fetched") # "dlq:news:fetched"
```

`emit_event(event_type="news.fetched")` on a `family="news"` agent
auto-resolves to `events:news:fetched` — no explicit stream needed.

### When to deviate

- Internal stage between two agents in the same family: use the natural
  `events:<family>:<verb>` form anyway. Don't introduce a `pipeline:`
  prefix.
- One-off ad-hoc streams (testing, replay): prefix with `tmp:` or `replay:`.
  These are ignored by production consumers.

### What NOT to do
- Don't put tenant IDs in stream names. Tenant routing is a payload
  concern, not a topology concern.
- Don't put environments in stream names (`events:prod:news:fetched`).
  Different envs have different Redis instances.
- Don't add a trailing version (`events:news:fetched_v2`). Bump
  `SCHEMA_VERSION` in the envelope instead.

---

## 2. Consumer groups

One Redis Streams **consumer group** per logical consumer agent.
Multiple **consumers within a group** can run in parallel (Sprint 7+
when we may run multiple worker processes).

```
events:news:fetched
  │
  ├── group: news.dedup
  │     ├── consumer: news.dedup (this process)
  │     └── consumer: news.dedup-worker-2 (future Sprint 7+)
  │
  ├── group: news.archive
  │     └── consumer: news.archive
  │
  └── group: metrics.collector
        └── consumer: metrics.collector
```

Each group gets a copy of each event (Redis Streams fan-out). Within a
group, each event goes to exactly one consumer (load-balanced).

Default `consumer_group` on `StreamAgent` is `"default"` — fine for
single-consumer streams. Override to the agent's name when:
- More than one agent type consumes the same stream
- You want isolated retry state per consumer

---

## 3. Envelope ↔ Redis Streams mapping

`XADD events:news:fetched * json '{"trace_id":"...", ...}'`

Redis Streams fields must be flat dict[str, value]. We use a single
`json` field carrying the full envelope. Rationale:
- Consumers don't need partial reads — they always parse the whole envelope.
- One field is cheaper than 12.
- Adding new envelope fields is non-breaking — the JSON parser is tolerant.

The msg_id assigned by Redis is **stashed as a non-dataclass attribute**
on the envelope (`envelope._bus_msg_id`) so `ack()` can find it without
polluting `envelope.payload`.

---

## 4. Backpressure

Each stream has a max length (default 5000, set on the `EventBus` instance):

```python
RedisEventBus(redis, max_len=5000)
InMemoryEventBus(max_len=5000)
```

When the stream reaches `max_len`, `XADD` with `MAXLEN ~ N` evicts the
**oldest** entry. Properties:
- Constant memory ceiling.
- **Slow consumers lose events** if they fall behind by more than max_len.
- "Approximate" form (the `~`) lets Redis trim opportunistically — slightly cheaper.

### When max_len 5000 isn't enough

Symptoms: consumer reads events that are already "older" than its
checkpoint, suggesting eviction happened. Solutions in order:
1. Raise `max_len` to 50000 or 100000 (memory cost: ~100MB for 50k
   small events).
2. Add a dedicated consumer process (Sprint 7+).
3. Move that family to a separate Redis instance.

### When events come in faster than the consumer can possibly drain

The stream cap saves Redis but loses data. If you must guarantee no
loss:
- Reduce upstream rate (slow the producer)
- Increase consumer throughput
- Split the stream by partition (e.g. by tenant hash)

Sprint 3 deliberately does NOT implement partitioning. Add it only when
single-stream throughput is a real bottleneck.

---

## 5. The dead-letter queue (DLQ)

When an event has failed N retries (per the agent's `retry_policy`),
the agent should send it to the DLQ rather than retry forever:

```python
async def handle_event(self, envelope):
    try:
        await do_work(envelope)
    except RetryExhausted as e:
        await self.event_bus.publish_to_dlq(
            original_stream=self.stream,
            envelope=envelope,
            reason=f"retry_exhausted:{e.__cause__!r}",
        )
        # Then ack — the runtime ack happens automatically after
        # handle_event returns. Don't re-raise.
```

DLQ envelopes have:
- `payload["_dlq_reason"]`: the reason string
- `payload["_dlq_original_stream"]`: where it came from
- Everything else identical to the original envelope

### Inspecting the DLQ

```bash
# In production:
docker exec -it redis redis-cli
> XLEN dlq:news:fetched
> XRANGE dlq:news:fetched - + COUNT 10
> XADD events:news:fetched * json '{"...original envelope..."}'   # replay
```

There is **no automatic replay** in Sprint 3. A human inspects, fixes
the root cause, and either:
- Re-publishes manually (one event at a time)
- Writes a one-off replay script
- Accepts the loss and `XDEL`s the entries

Sprint 5+ may add a `/api/dlq` admin endpoint with a button. Out of
scope for Sprint 3.

---

## 6. Retry semantics

Retries happen at TWO levels:

### Level 1: Inside the agent's `run_once` / `handle_event`
- Driven by `agent.retry_policy` via `with_retry` or `retry_call`
- Bounded by `RetryPolicy.max_attempts`
- The event is NOT re-published — same in-flight attempt is retried
- ACK happens AFTER the retry chain completes

### Level 2: At the event level (cross-process)
- Driven by `envelope.retry_count`
- A consumer can choose to NACK (Redis: no XACK) so the event becomes
  "pending" — XAUTOCLAIM (Sprint 4+) reclaims it for another consumer
- Sprint 3 doesn't expose NACK explicitly; ack-or-die.

### When to use which
- Transient external failures inside one tick → Level 1
- Worker process crashed mid-handle → Level 2 (NACK on next consumer)
- Validation rejection → neither; ack + DLQ + move on

---

## 7. Ordering guarantees

Within a single stream: events are delivered in publish order to consumers
in a group.

Across streams: no ordering guarantee. If you need ordered processing
across multiple stages, single-stream + sequential agents is the simple
pattern.

Within a consumer group with multiple consumers: order is preserved
**per consumer**, not globally. If you NEED total order, run one
consumer per group.

---

## 8. Memory and persistence

| Aspect | Behavior |
|---|---|
| In-memory storage | Yes (Redis). AOF persistence for durability across restarts (per existing `docker-compose.prod.yml`). |
| Stream survives Redis restart | Yes (with AOF). |
| Stream survives container recreate without volume | No — same as any other Redis data. |
| Per-event TTL | None. Events live until evicted by MAXLEN or trimmed manually. |
| Group state (last-delivered id) | Persists with the stream. |
| Pending list (unacked events) | Persists. Use `XPENDING` to inspect. Sprint 4+ handles reclaim. |

---

## 9. Cheat sheet — common Redis-CLI debug

```bash
# List active streams
KEYS events:*

# Length of a stream
XLEN events:news:fetched

# Tail
XRANGE events:news:fetched - + COUNT 10

# Consumer group state
XINFO GROUPS events:news:fetched

# Pending (unacked) per consumer
XPENDING events:news:fetched news.dedup

# Force-create a group at HEAD (start fresh)
XGROUP CREATE events:news:fetched my_new_group $ MKSTREAM

# DLQ contents
XLEN dlq:news:fetched
XRANGE dlq:news:fetched - + COUNT 20
```

---

## 10. Sprint 3 limitations

- **No automatic claim of pending events** (`XAUTOCLAIM`). If a consumer
  crashes mid-handle, its pending events stay pending until manual
  reclaim. Sprint 4+ adds an `XAUTOCLAIM` sweep on each tick.
- **No partitioning** by key. One stream = one logical queue.
- **No idempotency dedup**. `envelope.idempotency_key` is reserved but
  not used. Add when a consumer demonstrates the need.
- **No backpressure feedback to producer**. Producers don't know if
  consumers are behind. Add lag metrics in Sprint 5 (Prometheus).
- **Single-Redis topology**. Stream sharing across multiple Redis hosts
  needs Redis Cluster + consistent hashing. Defer.
