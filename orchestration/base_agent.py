"""Sprint 3 — BaseAgent contract.

BaseAgent is the abstract contract every agent inherits. It provides:

  - Identity:        name (unique), version, family
  - Lifecycle:       run_once() — the only thing subclasses MUST implement
  - Inputs:          validate_input(envelope) -> CritiqueResult (default: accept)
  - Outputs:         emit_event(event_type, payload) -> EventEnvelope (helper)
  - Error handling:  handle_failure(exc) -> None (override for custom behavior)
  - Retry:           retry_policy attribute (None = no retry)
  - Timeout:         per-tick timeout (None = no timeout)
  - Trace propagation: per-tick request_id_var / agent_name_var set in run_loop
  - Structured logging: a per-agent logger named "agent.<family>.<name>"
  - Metrics hooks:    on_tick_start / on_tick_end called for every tick;
                      subclasses override OR an external metric module wraps them.

Sprint 3 does NOT autostart anything. Sprint 4 wires the Orchestrator
into FastAPI lifespan, and that's when run_loop() actually fires.

Two concrete shapes are provided:

  - TickAgent      : runs run_once() on a periodic timer. The loop is
                     driven by Orchestrator (or a caller for tests),
                     NOT by the agent itself.
  - StreamAgent    : consumes events from an EventBus stream. Subclasses
                     implement handle_event(envelope) instead of run_once().
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from orchestration.critic import AlwaysAcceptCritic, BaseCritic, CritiqueResult
from orchestration.event_envelope import EventEnvelope, new_envelope
from orchestration.retry import RetryPolicy, retry_call

if TYPE_CHECKING:
    from orchestration.event_bus import EventBus


@dataclass
class AgentTickStats:
    """Per-tick metrics surface. Sprint 5 wires Prometheus into this."""

    tick_id: str
    started_at: float
    ended_at: float | None = None
    success: bool = False
    error_type: str | None = None
    error_msg: str | None = None
    events_emitted: int = 0


# ──────────────────────────────────────────────────────────────────────
# BaseAgent
# ──────────────────────────────────────────────────────────────────────

class BaseAgent(abc.ABC):
    """Abstract contract. Subclasses MUST implement `run_once`.

    Subclasses MAY override:
      - validate_input  (default: AlwaysAcceptCritic)
      - handle_failure  (default: log + record stats)
      - on_tick_start / on_tick_end  (default: no-op)
    """

    # ── Identity (set by subclass) ────────────────────────────────────
    name: str = "agent"
    version: str = "0"
    family: str = "generic"

    # ── Config (override on subclass or instance) ─────────────────────
    retry_policy: RetryPolicy | None = None
    timeout: float | None = None  # seconds per tick; None = unbounded

    # ── Wiring (provided by Orchestrator at registration) ─────────────
    event_bus: "EventBus | None" = None
    input_critic: BaseCritic | None = None

    # ── Internal state ────────────────────────────────────────────────
    _last_stats: AgentTickStats | None = None
    _consecutive_failures: int = 0
    _total_ticks: int = 0
    _total_failures: int = 0

    def __init__(self):
        self.log = logging.getLogger(f"agent.{self.family}.{self.name}")
        if self.input_critic is None:
            self.input_critic = AlwaysAcceptCritic()

    # ── Subclass contract ─────────────────────────────────────────────

    @abc.abstractmethod
    async def run_once(self) -> None:
        """Do one unit of work.

        Implementations should:
          1. Call self.validate_input(envelope) for any inbound data
             (or rely on the consumer wrapper in StreamAgent).
          2. Use self.emit_event() to publish results.
          3. NOT catch their own exceptions unless they have a specific
             recovery — the tick wrapper handles retry + circuit.
        """

    # ── Default behaviors (override as needed) ────────────────────────

    async def validate_input(self, envelope: EventEnvelope) -> CritiqueResult:
        """Default: delegate to input_critic. Override for custom logic."""
        assert self.input_critic is not None  # set in __init__
        return await self.input_critic.evaluate(envelope)

    async def handle_failure(
        self,
        exc: BaseException,
        *,
        stats: AgentTickStats,
    ) -> None:
        """Called from tick() when run_once raises. Default: log + record.

        Override for custom recovery (e.g. flush a cache, notify admin).
        Do NOT re-raise — return cleanly and tick() will record the failure.
        """
        self.log.exception(
            "agent_tick_failed",
            extra={
                "agent": self.name,
                "tick_id": stats.tick_id,
                "consecutive_failures": self._consecutive_failures + 1,
            },
        )

    async def on_tick_start(self, stats: AgentTickStats) -> None:
        """Metrics hook — Sprint 5 wires Prometheus counters here."""

    async def on_tick_end(self, stats: AgentTickStats) -> None:
        """Metrics hook — symmetric to on_tick_start."""

    # ── Public emit helper ────────────────────────────────────────────

    async def emit_event(
        self,
        *,
        event_type: str,
        payload: dict,
        stream: str | None = None,
        target_agent: str | None = None,
    ) -> EventEnvelope:
        """Build an envelope and publish it via the agent's event_bus.

        If `stream` is None, falls back to `events:{family}:{event_type}`.

        Returns the envelope (for tests and chained calls). If event_bus
        is None (test/dev without bus), the envelope is still returned
        and logged but not published.
        """
        envelope = new_envelope(
            event_type=event_type,
            payload=payload,
            agent_name=self.name,
            target_agent=target_agent,
        )
        if self.event_bus is None:
            self.log.warning(
                "emit_without_bus",
                extra={"event_type": event_type, "trace_id": envelope.trace_id},
            )
        else:
            # Lazy import to avoid circular dependency.
            from orchestration.event_bus import stream_name as _stream_name
            target_stream = stream or _stream_name(self.family, event_type)
            await self.event_bus.publish(target_stream, envelope)
        if self._last_stats is not None:
            self._last_stats.events_emitted += 1
        return envelope

    # ── Tick driver — called by Orchestrator or tests ─────────────────

    async def tick(self) -> AgentTickStats:
        """Drive one execution of run_once() with timeout + retry + tracing.

        This is the entry point Orchestrator calls. Tests can call it
        directly. Does NOT loop — exactly one tick per call.
        """
        # Lazy import to avoid hard dep on Sprint 2 module at type-check time.
        from logging_config import (
            agent_name_var, new_request_id, request_id_var, trace_id_var,
        )

        stats = AgentTickStats(tick_id=new_request_id(), started_at=time.monotonic())
        self._last_stats = stats

        # Bind context for the duration of the tick.
        tokens = [
            agent_name_var.set(self.name),
            request_id_var.set(stats.tick_id),
            trace_id_var.set(uuid.uuid4().hex),  # one trace per tick by default
        ]

        await self.on_tick_start(stats)
        self._total_ticks += 1

        try:
            await self._run_with_policy()
            stats.success = True
            self._consecutive_failures = 0
        except BaseException as e:  # noqa: BLE001 — record-and-swallow
            stats.success = False
            stats.error_type = type(e).__name__
            stats.error_msg = str(e)[:500]
            self._consecutive_failures += 1
            self._total_failures += 1
            await self.handle_failure(e, stats=stats)
        finally:
            stats.ended_at = time.monotonic()
            await self.on_tick_end(stats)
            # Reset context in reverse order.
            for t in reversed(tokens):
                t.var.reset(t)
        return stats

    async def _run_with_policy(self) -> None:
        """Apply retry_policy and timeout, then call run_once()."""

        async def attempt() -> None:
            if self.timeout is not None:
                await asyncio.wait_for(self.run_once(), timeout=self.timeout)
            else:
                await self.run_once()

        if self.retry_policy is None:
            await attempt()
            return

        await retry_call(self.retry_policy, attempt)

    # ── Health snapshot ───────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """Read-only status. Used by Orchestrator.health()."""
        return {
            "name": self.name,
            "family": self.family,
            "version": self.version,
            "total_ticks": self._total_ticks,
            "total_failures": self._total_failures,
            "consecutive_failures": self._consecutive_failures,
            "last_tick_success": (
                self._last_stats.success if self._last_stats else None
            ),
            "last_tick_at": (
                self._last_stats.ended_at if self._last_stats else None
            ),
        }


# ──────────────────────────────────────────────────────────────────────
# Concrete shapes
# ──────────────────────────────────────────────────────────────────────

class TickAgent(BaseAgent):
    """Periodic agent. Subclass implements run_once(); Orchestrator drives
    the cadence via tick_interval.

    Sprint 3 sets tick_interval but does NOT autostart. Sprint 4 starts
    one such agent and observes."""

    tick_interval: float = 60.0  # seconds


class StreamAgent(BaseAgent):
    """Event-driven agent. Consumes from a Redis Stream.

    Subclasses implement handle_event(envelope) instead of run_once().
    run_once() in this class fetches one envelope from the bus, validates
    it, and dispatches to handle_event. The Orchestrator-driven loop
    will repeatedly tick this agent — each tick consumes one event.
    """

    stream: str = ""  # set on subclass: e.g. "events:news:fetched"
    consumer_group: str = "default"  # one group per logical consumer

    @abc.abstractmethod
    async def handle_event(self, envelope: EventEnvelope) -> None:
        """Process one envelope. Called only AFTER validate_input accepted."""

    async def run_once(self) -> None:
        """Fetch one envelope, validate, dispatch.

        If no envelope is available, returns without error. The next
        tick will try again.
        """
        if self.event_bus is None:
            raise RuntimeError(f"StreamAgent {self.name!r} requires an event_bus")
        if not self.stream:
            raise RuntimeError(f"StreamAgent {self.name!r} has no stream configured")

        envelope = await self.event_bus.try_consume_one(
            stream=self.stream,
            group=self.consumer_group,
            consumer=self.name,
        )
        if envelope is None:
            return  # no event waiting

        verdict = await self.validate_input(envelope)
        if not verdict.accepted:
            self.log.warning(
                "input_rejected_by_critic",
                extra={
                    "agent": self.name,
                    "reason": verdict.reason,
                    "event_type": envelope.event_type,
                    "trace_id": envelope.trace_id,
                },
            )
            # ACK so we don't reprocess — rejection is durable.
            await self.event_bus.ack(self.stream, self.consumer_group, envelope)
            return

        try:
            await self.handle_event(envelope)
        finally:
            await self.event_bus.ack(self.stream, self.consumer_group, envelope)
