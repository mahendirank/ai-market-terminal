"""Sprint 3 — Orchestrator.

Holds the agent registry and provides start/stop/health. The loop
itself is opt-in via run_tick_loop() — Sprint 3 ships it but Sprint 4
is the first sprint that actually starts agents on boot.

Sprint 3 explicitly avoids:
  - Auto-starting agents on import or registration
  - Hidden timers / background threads
  - Recursive agent chains
  - Any LangGraph wiring

The loop helper run_tick_loop() is bounded by:
  - A stop_event (asyncio.Event) — caller can request graceful stop
  - max_consecutive_failures — after N tick failures in a row, agent
    self-disables and the loop exits cleanly
  - timeout per tick (inherited from BaseAgent.timeout)

Lifecycle:
    orchestrator = Orchestrator()
    orchestrator.register(MyAgent(event_bus=bus))
    # ... in FastAPI lifespan startup:
    await orchestrator.start_all()
    # ... shutdown:
    await orchestrator.stop_all()
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestration.base_agent import BaseAgent
    from orchestration.event_bus import EventBus


_log = logging.getLogger("agents.orchestrator")


class AgentStatus(str, enum.Enum):
    """Lifecycle states for a registered agent."""

    REGISTERED = "registered"   # in registry, not started
    RUNNING = "running"         # loop active
    STOPPING = "stopping"       # stop_event set, waiting for current tick
    STOPPED = "stopped"         # loop exited cleanly
    DISABLED = "disabled"       # too many consecutive failures; manual reset needed


@dataclass
class AgentHealth:
    """Per-agent record. Snapshot of state for /api/agents."""

    name: str
    status: AgentStatus
    family: str
    version: str
    total_ticks: int
    total_failures: int
    consecutive_failures: int
    last_tick_success: bool | None
    last_tick_at: float | None  # monotonic seconds; consumer should compute relative
    tick_interval: float | None  # for periodic agents

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "family": self.family,
            "version": self.version,
            "total_ticks": self.total_ticks,
            "total_failures": self.total_failures,
            "consecutive_failures": self.consecutive_failures,
            "last_tick_success": self.last_tick_success,
            "last_tick_age_s": (
                (time.monotonic() - self.last_tick_at)
                if self.last_tick_at is not None else None
            ),
            "tick_interval": self.tick_interval,
        }


@dataclass
class _AgentRecord:
    agent: "BaseAgent"
    status: AgentStatus = AgentStatus.REGISTERED
    task: asyncio.Task | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


# ──────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────

class Orchestrator:
    """Registry + lifecycle. Does NOT autostart agents.

    Concurrency: each agent's loop runs as one asyncio.Task. Multiple
    agents run cooperatively in the FastAPI event loop. Sprint 6+ may
    move heavy agents to their own process, but the contract here
    works either way.
    """

    DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

    def __init__(
        self,
        *,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ):
        self._agents: dict[str, _AgentRecord] = {}
        self._max_consecutive_failures = max_consecutive_failures

    # ── Registry ──────────────────────────────────────────────────────

    def register(self, agent: "BaseAgent") -> None:
        """Add an agent. Name must be unique. Does NOT start it."""
        if agent.name in self._agents:
            raise ValueError(f"agent {agent.name!r} already registered")
        self._agents[agent.name] = _AgentRecord(agent=agent)
        _log.info(
            "agent_registered",
            extra={"agent": agent.name, "family": agent.family, "version": agent.version},
        )

    def unregister(self, name: str) -> None:
        """Remove agent. Caller is responsible for stopping it first."""
        rec = self._agents.get(name)
        if rec is None:
            return
        if rec.status == AgentStatus.RUNNING:
            raise RuntimeError(
                f"agent {name!r} is RUNNING — call stop_agent before unregister"
            )
        self._agents.pop(name, None)

    def list_agents(self) -> list[str]:
        return sorted(self._agents.keys())

    def get_agent(self, name: str) -> "BaseAgent | None":
        rec = self._agents.get(name)
        return rec.agent if rec else None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start_agent(self, name: str) -> None:
        """Spawn the loop task. Returns immediately; task runs in background."""
        rec = self._agents.get(name)
        if rec is None:
            raise KeyError(f"agent {name!r} not registered")
        if rec.status == AgentStatus.RUNNING:
            return  # idempotent
        if rec.status == AgentStatus.DISABLED:
            raise RuntimeError(
                f"agent {name!r} DISABLED due to consecutive failures — reset first"
            )

        rec.stop_event.clear()
        rec.status = AgentStatus.RUNNING
        rec.task = asyncio.create_task(
            self._run_loop(rec),
            name=f"agent_loop:{name}",
        )
        _log.info("agent_started", extra={"agent": name})

    async def stop_agent(self, name: str, *, timeout: float = 10.0) -> None:
        """Signal graceful stop. Awaits up to `timeout` for the task to exit."""
        rec = self._agents.get(name)
        if rec is None or rec.task is None:
            return
        if rec.status not in (AgentStatus.RUNNING, AgentStatus.STOPPING):
            return  # already stopped

        rec.status = AgentStatus.STOPPING
        rec.stop_event.set()
        try:
            await asyncio.wait_for(rec.task, timeout=timeout)
        except asyncio.TimeoutError:
            _log.warning(
                "agent_stop_timeout",
                extra={"agent": name, "timeout": timeout},
            )
            rec.task.cancel()
            # Suppress the CancelledError so the orchestrator caller doesn't
            # see it propagate.
            try:
                await rec.task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            rec.status = AgentStatus.STOPPED
            _log.info("agent_stopped", extra={"agent": name})

    async def start_all(self) -> None:
        """Start every registered agent."""
        await asyncio.gather(*[self.start_agent(n) for n in self._agents])

    async def stop_all(self, *, timeout: float = 10.0) -> None:
        """Stop every running agent. Gathers in parallel."""
        await asyncio.gather(
            *[self.stop_agent(n, timeout=timeout) for n in self._agents],
            return_exceptions=True,
        )

    async def reset_disabled(self, name: str) -> None:
        """Re-enable an agent that was DISABLED. Caller still needs to start it."""
        rec = self._agents.get(name)
        if rec is None:
            raise KeyError(f"agent {name!r} not registered")
        if rec.status != AgentStatus.DISABLED:
            return
        rec.status = AgentStatus.REGISTERED
        rec.agent._consecutive_failures = 0  # type: ignore[attr-defined]
        _log.info("agent_reset", extra={"agent": name})

    # ── Tick helpers (synchronous; called by tests too) ───────────────

    async def tick_agent(self, name: str) -> dict[str, Any]:
        """Drive ONE tick of one agent. Returns its AgentTickStats as dict.

        Used by tests and by manual triggers. The autonomous loop is
        run_tick_loop() — caller's choice.
        """
        rec = self._agents.get(name)
        if rec is None:
            raise KeyError(f"agent {name!r} not registered")
        stats = await rec.agent.tick()
        return {
            "tick_id": stats.tick_id,
            "success": stats.success,
            "duration_s": (
                (stats.ended_at - stats.started_at)
                if stats.ended_at else None
            ),
            "error_type": stats.error_type,
            "events_emitted": stats.events_emitted,
        }

    # ── Loop driver ───────────────────────────────────────────────────

    async def _run_loop(self, rec: _AgentRecord) -> None:
        """Internal: drive an agent's ticks until stop_event is set or it
        accumulates max_consecutive_failures.

        Cadence:
          - TickAgent: respects agent.tick_interval
          - StreamAgent: ticks immediately if last tick consumed an event;
                         otherwise pauses 1s to avoid hot-spinning an empty bus.
        """
        # Lazy import — avoid circular dependency.
        from orchestration.base_agent import StreamAgent

        agent = rec.agent
        tick_interval = getattr(agent, "tick_interval", 1.0)
        is_stream = isinstance(agent, StreamAgent)

        while not rec.stop_event.is_set():
            stats = await agent.tick()

            if agent._consecutive_failures >= self._max_consecutive_failures:  # type: ignore[attr-defined]
                _log.error(
                    "agent_disabled",
                    extra={
                        "agent": agent.name,
                        "consecutive_failures": agent._consecutive_failures,  # type: ignore[attr-defined]
                    },
                )
                rec.status = AgentStatus.DISABLED
                return

            # Decide how long to wait before the next tick.
            if is_stream and stats.events_emitted == 0 and stats.success:
                # Empty bus — back off a bit so we don't hot-loop.
                wait = 1.0
            else:
                wait = tick_interval

            # Wait, but wake up immediately if stop is signaled.
            try:
                await asyncio.wait_for(rec.stop_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue
            else:
                break  # stop_event was set

    # ── Health snapshot ───────────────────────────────────────────────

    def health(self) -> list[AgentHealth]:
        """Return one record per registered agent. Used by /api/agents.

        Lock-free read — values can be a tick stale, which is fine for
        a status endpoint.
        """
        out: list[AgentHealth] = []
        for name, rec in self._agents.items():
            h = rec.agent.health()
            out.append(
                AgentHealth(
                    name=name,
                    status=rec.status,
                    family=h["family"],
                    version=h["version"],
                    total_ticks=h["total_ticks"],
                    total_failures=h["total_failures"],
                    consecutive_failures=h["consecutive_failures"],
                    last_tick_success=h["last_tick_success"],
                    last_tick_at=h["last_tick_at"],
                    tick_interval=getattr(rec.agent, "tick_interval", None),
                )
            )
        return out
