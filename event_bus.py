"""
event_bus.py — Redis pub/sub channel for cross-module breaking-event signals.

Until this module landed, every consumer of news intel had its own TTL clock
and would happily serve a 30-minute-old "morning brief" through the first
hour of a geopolitical shock. event_bus replaces that with a push model:
news_deduper publishes the moment a HIGH-severity cluster forms, and every
subscriber (morning_report, yield_watch, ai_persona...) drops their cache so
the next read recomputes against fresh intel.

Channel: ``events:breaking``
Payload: ``{"topic": str, "severity": int, "ts": float, ...optional fields}``

Public API:
    publish_breaking(topic, severity, extra=None) -> bool
        Best-effort publish. Returns False if Redis is not configured.
    subscribe(callback)
        Register a function called for every event. Callbacks run on the
        listener thread — keep them short (just drop a cache key).
    start_listener()
        Idempotent. Spawns the daemon thread that pumps the pub/sub.

Failure mode: when Redis is unavailable the module silently no-ops. Cache
invalidation falls back to the TTL clock — degraded but never broken.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

CHANNEL = "events:breaking"

_redis_client = None
_redis_lock   = threading.Lock()
_subscribers: list[Callable[[dict], None]] = []
_listener_thread: Optional[threading.Thread] = None
_listener_lock = threading.Lock()

# Per-process dedup window — same topic published twice in this many seconds
# only fires once. Stops a sticky cluster from invalidating caches every poll.
_PUBLISH_DEDUP_SECS = 90
_recent_publishes: dict[str, float] = {}
_recent_lock = threading.Lock()


def _client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            import redis
            c = redis.from_url(
                url,
                socket_connect_timeout=4,
                socket_timeout=4,
                decode_responses=True,
            )
            c.ping()
            _redis_client = c
        except Exception as e:  # noqa: BLE001
            log.warning("[event_bus] Redis unavailable: %s", e)
            _redis_client = None
    return _redis_client


def publish_breaking(topic: str, severity: int, extra: Optional[dict] = None) -> bool:
    """Publish a breaking event. Dedup'd within _PUBLISH_DEDUP_SECS."""
    if not topic:
        return False
    c = _client()
    if c is None:
        return False
    key = topic.strip().lower()[:200]
    now = time.time()
    with _recent_lock:
        last = _recent_publishes.get(key, 0)
        if now - last < _PUBLISH_DEDUP_SECS:
            return False
        _recent_publishes[key] = now
        # Trim memory: drop entries older than 10 minutes
        if len(_recent_publishes) > 256:
            cutoff = now - 600
            for k in [kk for kk, tt in _recent_publishes.items() if tt < cutoff]:
                _recent_publishes.pop(k, None)
    try:
        payload = {"topic": topic, "severity": int(severity), "ts": now}
        if extra:
            payload.update(extra)
        c.publish(CHANNEL, json.dumps(payload, default=str))
        log.info("[event_bus] published breaking: sev=%d topic=%s", severity, topic[:80])
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("[event_bus] publish failed: %s", e)
        return False


def subscribe(callback: Callable[[dict], None]) -> None:
    """Register an invalidation callback. Idempotent for the same fn ref."""
    if callback in _subscribers:
        return
    _subscribers.append(callback)


def _listener_loop() -> None:
    c = _client()
    if c is None:
        return
    while True:
        try:
            p = c.pubsub(ignore_subscribe_messages=True)
            p.subscribe(CHANNEL)
            log.info("[event_bus] listener subscribed to %s", CHANNEL)
            for msg in p.listen():
                if msg is None or msg.get("type") != "message":
                    continue
                try:
                    ev = json.loads(msg["data"])
                except Exception:  # noqa: BLE001
                    continue
                for cb in list(_subscribers):
                    try:
                        cb(ev)
                    except Exception as e:  # noqa: BLE001
                        log.warning("[event_bus] subscriber %s raised: %s",
                                    getattr(cb, "__qualname__", cb), e)
        except Exception as e:  # noqa: BLE001
            # Redis connection blip — log and retry after backoff.
            log.warning("[event_bus] listener error, reconnecting in 5s: %s", e)
            time.sleep(5)


def start_listener() -> None:
    """Start the pub/sub listener thread. Safe to call repeatedly."""
    global _listener_thread
    with _listener_lock:
        if _listener_thread is not None and _listener_thread.is_alive():
            return
        if _client() is None:
            log.debug("[event_bus] no Redis — listener not started")
            return
        _listener_thread = threading.Thread(
            target=_listener_loop, daemon=True, name="event_bus_listener",
        )
        _listener_thread.start()
