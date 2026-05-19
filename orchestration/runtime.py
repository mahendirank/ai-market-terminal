"""Sprint 4 Stage 4.1 — orchestration runtime factory.

Separate from `orchestrator.py` so `dashboard_api.py` doesn't have to
know about Redis client construction or env-var parsing.

Public surface (all async-safe):

    orchestrator_enabled() -> bool
    build_event_bus()       -> EventBus
    build_orchestrator()    -> Orchestrator

Used by `dashboard_api.py`'s lifespan hook. Sprint 4.1 wires only the
LIFECYCLE — no agents are registered. Sprint 4.3+ adds agent
registration.

Design rules:
  - All imports lazy where they touch optional deps (redis client only
    loaded if REDIS_URL is set + orchestrator enabled).
  - Failures during init log + degrade to None (orchestrator off).
  - Default bus is in-memory: SAFE for any environment, no external
    connections.
"""

from __future__ import annotations

import logging
import os

from orchestration.event_bus import EventBus, InMemoryEventBus
from orchestration.orchestrator import Orchestrator


_log = logging.getLogger("orchestration.runtime")


# ──────────────────────────────────────────────────────────────────────
# Env-var read
# ──────────────────────────────────────────────────────────────────────

def orchestrator_enabled() -> bool:
    """Read AGENT_ORCHESTRATOR_ENABLED env. Default 'false'."""
    return os.environ.get("AGENT_ORCHESTRATOR_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ──────────────────────────────────────────────────────────────────────
# Event bus factory
# ──────────────────────────────────────────────────────────────────────

async def build_event_bus() -> EventBus:
    """Construct the event bus per env config.

    Selection:
      AGENT_BUS=memory     → always in-memory
      AGENT_BUS=redis      → require REDIS_URL; raise if missing
      AGENT_BUS=auto (def) → REDIS_URL set → redis; else in-memory

    Memory fallback is intentional: an empty orchestrator with no agents
    has no use for Redis, so we never force the dependency.
    """
    bus_mode = os.environ.get("AGENT_BUS", "auto").strip().lower()
    redis_url = os.environ.get("REDIS_URL", "").strip()

    if bus_mode == "memory":
        _log.info("event_bus_init", extra={"mode": "memory", "reason": "explicit_AGENT_BUS"})
        return InMemoryEventBus()

    if bus_mode == "redis":
        if not redis_url:
            raise RuntimeError(
                "AGENT_BUS=redis but REDIS_URL is empty — refusing to silently fall back"
            )
        return await _build_redis_bus(redis_url)

    # auto
    if redis_url:
        try:
            return await _build_redis_bus(redis_url)
        except Exception:
            _log.exception(
                "redis_bus_init_failed_falling_back_to_memory",
                extra={"redis_url_prefix": redis_url.split("@")[-1][:20]},
            )
            return InMemoryEventBus()
    else:
        _log.warning(
            "event_bus_init",
            extra={"mode": "memory_fallback", "reason": "no_REDIS_URL"},
        )
        return InMemoryEventBus()


async def _build_redis_bus(redis_url: str):
    """Lazy import + construct RedisEventBus. Caller catches exceptions."""
    import redis.asyncio as aioredis  # lazy import — only on the redis path
    from orchestration.event_bus import RedisEventBus

    # decode_responses=False keeps bytes; matches what XAUTOCLAIM etc. expect.
    client = aioredis.from_url(redis_url, decode_responses=False)
    # Light ping to fail fast if Redis is unreachable.
    await client.ping()
    _log.info(
        "event_bus_init",
        extra={
            "mode": "redis",
            "redis_url_prefix": redis_url.split("@")[-1][:20],
        },
    )
    return RedisEventBus(client)


# ──────────────────────────────────────────────────────────────────────
# Orchestrator factory
# ──────────────────────────────────────────────────────────────────────

async def build_orchestrator() -> Orchestrator:
    """Construct the orchestrator with env-configurable bounds.

    AGENT_MAX_FAILURES (int, default 5) — auto-DISABLE threshold per agent.
    """
    try:
        max_failures = int(os.environ.get("AGENT_MAX_FAILURES", "5"))
    except ValueError:
        _log.warning("invalid_AGENT_MAX_FAILURES_using_default")
        max_failures = 5
    if max_failures < 1:
        max_failures = 1
    return Orchestrator(max_consecutive_failures=max_failures)
