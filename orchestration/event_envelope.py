"""Sprint 3 — EventEnvelope.

Stable JSON contract carried by every event that flows through the
agent runtime. Designed for:
  - Trace propagation across Redis Streams + future LangGraph nodes
  - Idempotency (idempotency_key in payload)
  - Retry bookkeeping (retry_count, last_error)
  - Observability (envelope fields land directly in JSON log envelope)

Forward compatibility:
  - Adding a NEW optional field is safe (consumers ignore unknowns)
  - Removing or renaming a field is a breaking change — bump SCHEMA_VERSION
  - Reordering fields has no effect (JSON is unordered)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


SCHEMA_VERSION = 1


@dataclass
class EventEnvelope:
    """Wire format for every event in the orchestration layer.

    All fields are required by name; producers that omit optional fields
    must pass None or the documented default explicitly. This keeps
    parsing deterministic on the consumer side.

    Field semantics:

      trace_id      : unique per logical operation; persists across all
                      agents that handle the same logical flow.
                      Maps to OpenTelemetry trace ID when OTel lands.
      request_id    : unique per "tick" of an agent or per HTTP request.
                      A single trace can contain many request_ids
                      (e.g. fanout through N agents).
      tenant_id     : "-" for global events; otherwise the tenant_id_var
                      value at emission.
      agent_name    : producing agent (or "http" for HTTP-originated events).
      timestamp     : ISO-8601 UTC ms — same format as the log envelope.
      event_type    : dot-namespaced (e.g. "news.fetched", "signal.candidate").
                      Used for stream routing and consumer filtering.
      payload       : the event body. Must be JSON-serializable. No size
                      cap enforced here — EventBus may enforce one.
      retry_count   : incremented by the consumer before re-publish to the
                      original stream. Compared against RetryPolicy.
      source_agent  : same as agent_name on emit; consumers don't mutate.
      target_agent  : optional routing hint. None = "any consumer of the
                      stream may handle this".
      schema_version: increments only on breaking changes. Consumers
                      should reject envelopes with unknown major versions.
      idempotency_key: optional. If set, consumers should skip if they've
                      seen this key in the last ttl. Defaults to None
                      (i.e. caller-supplied or absent).
    """

    trace_id: str
    request_id: str
    tenant_id: str
    agent_name: str
    timestamp: str
    event_type: str
    payload: dict
    retry_count: int = 0
    source_agent: str | None = None
    target_agent: str | None = None
    schema_version: int = SCHEMA_VERSION
    idempotency_key: str | None = None

    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict. Stable shape — safe to log directly."""
        return asdict(self)

    def to_json(self) -> str:
        """One-line JSON. Used by Redis Streams XADD."""
        return json.dumps(self.to_dict(), default=str, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventEnvelope:
        """Construct from a dict. Tolerates extra unknown fields."""
        # Filter to known dataclass fields so unknown keys (added in newer
        # producer schema) don't crash older consumers.
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    @classmethod
    def from_json(cls, raw: str) -> EventEnvelope:
        return cls.from_dict(json.loads(raw))

    # ── Convenience ───────────────────────────────────────────────────

    def with_retry_incremented(self, *, last_error: str | None = None) -> EventEnvelope:
        """Return a copy with retry_count+1 and optionally last_error in payload."""
        new = EventEnvelope(**self.to_dict())
        new.retry_count = self.retry_count + 1
        if last_error is not None:
            # Attach to payload so it survives JSON ser/deser without
            # polluting the envelope shape with optional fields.
            new.payload = {**self.payload, "_last_error": last_error}
        return new

    def __repr__(self) -> str:  # short form — full content via to_dict()
        return (
            f"EventEnvelope(type={self.event_type!r}, "
            f"trace={self.trace_id[:8]}..., "
            f"agent={self.agent_name!r}, retries={self.retry_count})"
        )


# ──────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────

def new_envelope(
    *,
    event_type: str,
    payload: dict,
    agent_name: str,
    trace_id: str | None = None,
    request_id: str | None = None,
    tenant_id: str = "-",
    target_agent: str | None = None,
    idempotency_key: str | None = None,
) -> EventEnvelope:
    """Build a fresh envelope with sane defaults.

    Reads ContextVars (request_id_var, trace_id_var, tenant_id_var) if
    available so emitters don't have to thread them manually — but
    callers can override any field.
    """
    # Lazy import to avoid circular dependency at package load time.
    from logging_config import (
        request_id_var,
        tenant_id_var,
        trace_id_var,
    )

    trace_id = trace_id or _resolve(trace_id_var) or uuid.uuid4().hex
    request_id = request_id or _resolve(request_id_var) or uuid.uuid4().hex[:12]
    tenant_id = tenant_id if tenant_id != "-" else _resolve(tenant_id_var) or "-"

    return EventEnvelope(
        trace_id=trace_id,
        request_id=request_id,
        tenant_id=tenant_id,
        agent_name=agent_name,
        timestamp=_iso_now(),
        event_type=event_type,
        payload=payload,
        retry_count=0,
        source_agent=agent_name,
        target_agent=target_agent,
        idempotency_key=idempotency_key,
    )


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────

def _resolve(ctxvar) -> str | None:
    """Return the ContextVar's current value, or None if it's the '-' default."""
    val = ctxvar.get()
    return val if val and val != "-" else None


def _iso_now() -> str:
    now = time.time()
    ms = int((now - int(now)) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{ms:03d}Z"
