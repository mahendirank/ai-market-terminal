"""Sprint 3 — Orchestrator tests."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration.base_agent import TickAgent
from orchestration.orchestrator import AgentStatus, Orchestrator


class _NoopAgent(TickAgent):
    name = "noop"
    family = "test"
    tick_interval = 0.01

    async def run_once(self):
        pass


class _CrashAgent(TickAgent):
    name = "crash"
    family = "test"
    tick_interval = 0.01

    async def run_once(self):
        raise RuntimeError("always crash")


@pytest.mark.smoke
def test_register_and_list():
    orch = Orchestrator()
    orch.register(_NoopAgent())
    assert orch.list_agents() == ["noop"]


@pytest.mark.smoke
def test_register_duplicate_name_raises():
    orch = Orchestrator()
    orch.register(_NoopAgent())
    with pytest.raises(ValueError, match="already registered"):
        orch.register(_NoopAgent())


@pytest.mark.smoke
def test_get_agent_returns_instance():
    orch = Orchestrator()
    a = _NoopAgent()
    orch.register(a)
    assert orch.get_agent("noop") is a
    assert orch.get_agent("missing") is None


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_tick_agent_runs_one_tick():
    orch = Orchestrator()
    orch.register(_NoopAgent())
    result = await orch.tick_agent("noop")
    assert result["success"] is True
    assert result["duration_s"] >= 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_start_and_stop_agent_lifecycle():
    orch = Orchestrator()
    orch.register(_NoopAgent())
    await orch.start_agent("noop")
    # Let it tick a couple of times.
    await asyncio.sleep(0.05)
    # Health shows it as RUNNING.
    h = orch.health()
    assert h[0].status == AgentStatus.RUNNING
    await orch.stop_agent("noop", timeout=1.0)
    # After stop, status is STOPPED.
    h = orch.health()
    assert h[0].status == AgentStatus.STOPPED


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_disabled_after_max_consecutive_failures():
    orch = Orchestrator(max_consecutive_failures=3)
    orch.register(_CrashAgent())
    await orch.start_agent("crash")
    # Wait long enough for >3 ticks to fail.
    await asyncio.sleep(0.2)
    # The loop self-exits when threshold is hit.
    h = orch.health()
    assert h[0].status == AgentStatus.DISABLED


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_reset_disabled_brings_it_back_to_registered():
    orch = Orchestrator(max_consecutive_failures=2)
    orch.register(_CrashAgent())
    await orch.start_agent("crash")
    await asyncio.sleep(0.1)
    h = orch.health()
    assert h[0].status == AgentStatus.DISABLED
    await orch.reset_disabled("crash")
    h = orch.health()
    assert h[0].status == AgentStatus.REGISTERED


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_stop_all_does_not_raise_on_unregistered():
    orch = Orchestrator()
    # No agents registered.
    await orch.stop_all()  # should be a no-op


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_health_includes_tick_interval():
    orch = Orchestrator()
    orch.register(_NoopAgent())
    h = orch.health()
    assert h[0].tick_interval == 0.01
    d = h[0].to_dict()
    assert d["tick_interval"] == 0.01
    assert d["status"] == "registered"


@pytest.mark.smoke
def test_unregister_running_raises():
    orch = Orchestrator()
    orch.register(_NoopAgent())
    # Manually flip status (not realistic but proves the guard).
    orch._agents["noop"].status = AgentStatus.RUNNING
    with pytest.raises(RuntimeError, match="RUNNING"):
        orch.unregister("noop")
