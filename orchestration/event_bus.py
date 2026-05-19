"""Sprint 3 — EventBus.

Two implementations:

  - RedisEventBus    : production — Redis Streams + consumer groups
  - InMemoryEventBus : tests, dev without Redis — same interface, asyncio.Queue

Stream naming convention:

    events:<family>:<event_type>          primary stream
    dlq:<family>:<event_type>             dead-letter queue (failed N retries)

The DLQ is a separate stream rather than a separate concept. Anyone
can XREAD it for debugging or replay. There is no automatic replay in
Sprint 3 — DLQ events stay there until a human looks.

Backpressure: each stream has a hard cap (MAXLEN ~ N) so a slow consumer
can't fill Redis. When full, the OLDEST events are evicted. If you need
strong "no event loss", consumers must read fast enough OR pre-acquire
larger Redis memory.

NOT in Sprint 3:
  - Idempotency dedup (envelope.idempotency_key is reserved but unused)
  - Cross-process / cross-host stream sharing details
  - Stream cleanup / consumer-group pruning
  - Lag / depth metrics emission (Sprint 5 with Prometheus)
"""

from __future__ import annotations

import abc
import asyncio
import logging
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

from orchestration.event_envelope import EventEnvelope

if TYPE_CHECKING:
    import redis.asyncio as _redis  # for type hints only


_log = logging.getLogger("agents.event_bus")


# ──────────────────────────────────────────────────────────────────────
# Naming
# ──────────────────────────────────────────────────────────────────────

def stream_name(family: str, event_type: str) -> str:
    """events:<family>:<event_type> — used by emit_event() default routing."""
    return f"events:{family}:{event_type}"


def dlq_stream_name(original: str) -> str:
    """dlq:<original-without-leading-events-prefix>"""
    if original.startswith("events:"):
        return "dlq:" + original[len("events:"):]
    return "dlq:" + original


# ──────────────────────────────────────────────────────────────────────
# Abstract base
# ──────────────────────────────────────────────────────────────────────

class EventBus(abc.ABC):
    """Producer/consumer interface. All methods async-safe."""

    @abc.abstractmethod
    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        """Publish to a stream. Returns the message id."""

    @abc.abstractmethod
    async def try_consume_one(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int = 0,
    ) -> EventEnvelope | None:
        """Read at most one envelope from the stream.

        block_ms=0 → return immediately if empty (None).
        block_ms>0 → wait up to block_ms for an event before returning None.
        """

    @abc.abstractmethod
    async def ack(
        self,
        stream: str,
        group: str,
        envelope: EventEnvelope,
    ) -> None:
        """Mark envelope as processed. Must be called after handle_event."""

    @abc.abstractmethod
    async def publish_to_dlq(
        self,
        *,
        original_stream: str,
        envelope: EventEnvelope,
        reason: str,
    ) -> str:
        """Move a failed envelope to the dead-letter queue. Returns DLQ msg id."""

    @abc.abstractmethod
    async def stream_length(self, stream: str) -> int:
        """Approx number of events currently in the stream. For health."""

    # Sprint 3 does NOT define a `consume_loop` — the consumer pattern is
    # one-event-per-tick, driven by Orchestrator (which respects stop signals).
    # This is the "no autonomous loop" rule from the user spec.

    @abc.abstractmethod
    async def ensure_group(self, stream: str, group: str) -> None:
        """Idempotent: create consumer group if it doesn't exist."""


# ──────────────────────────────────────────────────────────────────────
# In-memory implementation (tests + dev without Redis)
# ──────────────────────────────────────────────────────────────────────

class InMemoryEventBus(EventBus):
    """asyncio.Queue-backed bus.

    Same interface as RedisEventBus but in-process. Useful for tests and
    for local dev when you don't want to run Redis. Does NOT survive
    process restarts.

    Per-stream message cap (default 5000) approximates Redis MAXLEN
    behavior — when full, oldest events are dropped.
    """

    DEFAULT_MAX_LEN = 5000

    def __init__(self, *, max_len: int = DEFAULT_MAX_LEN):
        self._streams: dict[str, deque[tuple[str, EventEnvelope]]] = defaultdict(deque)
        self._groups: dict[tuple[str, str], dict[str, EventEnvelope]] = defaultdict(dict)
        # group → pending (acked-or-not) map keyed by msg_id
        self._max_len = max_len
        self._counter = 0
        self._lock = asyncio.Lock()

    def _next_id(self) -> str:
        self._counter += 1
        # Mimics Redis "timestamp-counter" shape so callers can sort.
        return f"0-{self._counter}"

    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        async with self._lock:
            msg_id = self._next_id()
            self._streams[stream].append((msg_id, envelope))
            if len(self._streams[stream]) > self._max_len:
                self._streams[stream].popleft()
            return msg_id

    async def try_consume_one(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,  # noqa: ARG002 — name not used in in-memory impl
        block_ms: int = 0,  # noqa: ARG002 — no real blocking in-memory
    ) -> EventEnvelope | None:
        # Use a real (lock-free) check + remove. We need atomicity to avoid
        # two consumers grabbing the same event.
        async with self._lock:
            q = self._streams.get(stream)
            if not q:
                return None
            msg_id, env = q.popleft()
            # Track pending until ack.
            self._groups[(stream, group)][msg_id] = env
            # Stash msg_id on the envelope as a non-dataclass attribute so
            # ack() can look it up without polluting payload.
            env_copy = EventEnvelope.from_dict(env.to_dict())
            env_copy._bus_msg_id = msg_id  # type: ignore[attr-defined]
            return env_copy

    async def ack(
        self,
        stream: str,
        group: str,
        envelope: EventEnvelope,
    ) -> None:
        msg_id = getattr(envelope, "_bus_msg_id", None)
        if not msg_id:
            return  # unknown msg, ack is a no-op
        async with self._lock:
            self._groups[(stream, group)].pop(msg_id, None)

    async def publish_to_dlq(
        self,
        *,
        original_stream: str,
        envelope: EventEnvelope,
        reason: str,
    ) -> str:
        dlq = dlq_stream_name(original_stream)
        # Tag the envelope with the DLQ reason and origin.
        dlq_env = EventEnvelope.from_dict(envelope.to_dict())
        dlq_env.payload = {
            **envelope.payload,
            "_dlq_reason": reason,
            "_dlq_original_stream": original_stream,
        }
        return await self.publish(dlq, dlq_env)

    async def stream_length(self, stream: str) -> int:
        return len(self._streams.get(stream, ()))

    async def ensure_group(self, stream: str, group: str) -> None:
        # No-op for in-memory — groups are created lazily on first use.
        # We initialize the entry so dictionaries report 0 pending.
        async with self._lock:
            _ = self._groups[(stream, group)]
            _ = self._streams[stream]  # also touch the stream

    # ── Test helpers ──────────────────────────────────────────────────

    def _peek_pending(self, stream: str, group: str) -> list[EventEnvelope]:
        return list(self._groups.get((stream, group), {}).values())


# ──────────────────────────────────────────────────────────────────────
# Redis implementation
# ──────────────────────────────────────────────────────────────────────

class RedisEventBus(EventBus):
    """Redis Streams via redis.asyncio (already in requirements.txt).

    Usage:
        import redis.asyncio as aioredis
        bus = RedisEventBus(aioredis.from_url("redis://redis:6379"))

    Sprint 3 keeps this minimal — just enough to round-trip envelopes.
    Sprint 4+ adds: consumer-group claim-stale, XAUTOCLAIM, lag metrics.
    """

    # MAXLEN approximate cap — Redis can keep slightly more for efficiency.
    DEFAULT_MAX_LEN = 5000

    def __init__(
        self,
        redis: "_redis.Redis",
        *,
        max_len: int = DEFAULT_MAX_LEN,
    ):
        self._r = redis
        self._max_len = max_len

    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        # Redis Streams fields must be a flat dict[str, str|bytes|int|float].
        # We store the whole envelope under a single "json" field — simpler
        # than spreading 12 fields, and consumers don't need partial reads.
        fields = {"json": envelope.to_json()}
        msg_id = await self._r.xadd(
            stream,
            fields,
            maxlen=self._max_len,
            approximate=True,
        )
        # redis-py returns bytes for msg_id; normalize to str.
        return msg_id.decode() if isinstance(msg_id, bytes) else msg_id

    async def try_consume_one(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int = 0,
    ) -> EventEnvelope | None:
        # XREADGROUP > count=1 + ">" reads the next undelivered message.
        result = await self._r.xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={stream: ">"},
            count=1,
            block=block_ms if block_ms > 0 else None,
        )
        if not result:
            return None
        # result is [(stream_name, [(msg_id, {field: value, ...})])]
        _, entries = result[0]
        if not entries:
            return None
        msg_id, fields = entries[0]
        # fields may be bytes-keyed depending on the redis-py decoder.
        raw = fields.get(b"json") if b"json" in fields else fields.get("json")
        if isinstance(raw, bytes):
            raw = raw.decode()
        envelope = EventEnvelope.from_json(raw)
        # Stash msg_id on the envelope as a non-dataclass attribute so
        # ack() can find it without polluting payload.
        envelope._bus_msg_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id  # type: ignore[attr-defined]
        return envelope

    async def ack(
        self,
        stream: str,
        group: str,
        envelope: EventEnvelope,
    ) -> None:
        msg_id = getattr(envelope, "_bus_msg_id", None)
        if not msg_id:
            return
        await self._r.xack(stream, group, msg_id)

    async def publish_to_dlq(
        self,
        *,
        original_stream: str,
        envelope: EventEnvelope,
        reason: str,
    ) -> str:
        dlq = dlq_stream_name(original_stream)
        dlq_env = EventEnvelope.from_dict(envelope.to_dict())
        dlq_env.payload = {
            **envelope.payload,
            "_dlq_reason": reason,
            "_dlq_original_stream": original_stream,
        }
        return await self.publish(dlq, dlq_env)

    async def stream_length(self, stream: str) -> int:
        try:
            return await self._r.xlen(stream)
        except Exception as e:
            _log.warning("xlen_failed", extra={"stream": stream, "err": repr(e)})
            return -1

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._r.xgroup_create(
                name=stream,
                groupname=group,
                id="0",
                mkstream=True,
            )
        except Exception as e:
            # BUSYGROUP error means the group already exists — that's fine.
            if "BUSYGROUP" in str(e):
                return
            raise
