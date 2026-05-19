"""Sprint 4 Stage 4.1 — read-only admin endpoint helpers.

Returns dict snapshots of orchestrator/breaker/stream state for HTTP
endpoints. All helpers tolerate the "orchestrator disabled" case
(returning `{"enabled": False, ...}`) so the routes always succeed.

Used by:
  GET /api/agents
  GET /api/circuits
  GET /api/streams/health
"""

from __future__ import annotations

import logging
from typing import Any


_log = logging.getLogger("orchestration.admin")

# Sprint 4 starting set of known streams. Sprint 5+ may switch to
# dynamic enumeration (e.g. Redis SCAN over 'events:*'), but a static
# list is sufficient when only 0–2 streams exist.
KNOWN_STREAMS = [
    "events:news:raw",
    "events:signal:candidate",
    "dlq:news:raw",
    "dlq:signal:candidate",
]


async def agents_snapshot(app) -> dict[str, Any]:
    """Return registered agents + their health. Safe when orchestrator disabled."""
    orch = getattr(app.state, "orchestrator", None)
    if orch is None:
        return {"enabled": False, "agents": []}
    try:
        return {
            "enabled": True,
            "agents": [h.to_dict() for h in orch.health()],
        }
    except Exception as e:
        _log.exception("agents_snapshot_failed")
        return {"enabled": True, "agents": [], "error": type(e).__name__}


async def circuits_snapshot() -> dict[str, Any]:
    """Return all circuit breakers known to default_registry."""
    try:
        from orchestration.circuit_breaker import default_registry
        return {"circuits": default_registry.snapshot()}
    except Exception as e:
        _log.exception("circuits_snapshot_failed")
        return {"circuits": [], "error": type(e).__name__}


async def streams_health_snapshot(app) -> dict[str, Any]:
    """Return length of each known stream. Safe when bus disabled."""
    bus = getattr(app.state, "event_bus", None)
    if bus is None:
        return {"enabled": False, "streams": []}

    out: list[dict] = []
    for name in KNOWN_STREAMS:
        try:
            length = await bus.stream_length(name)
        except Exception as e:
            length = -1
            _log.warning(
                "stream_length_failed",
                extra={"stream": name, "err": type(e).__name__},
            )
        out.append({"stream": name, "length": length})
    return {"enabled": True, "streams": out}
