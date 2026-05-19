"""Sprint 4 Stage 4.1 — runtime factory tests.

Covers:
  - orchestrator_enabled() reads env correctly
  - build_event_bus() selection logic (memory / redis / auto)
  - build_orchestrator() respects AGENT_MAX_FAILURES
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.mark.smoke
def test_orchestrator_enabled_default_false(monkeypatch):
    monkeypatch.delenv("AGENT_ORCHESTRATOR_ENABLED", raising=False)
    from orchestration.runtime import orchestrator_enabled
    assert orchestrator_enabled() is False


@pytest.mark.smoke
@pytest.mark.parametrize("val,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("", False), ("anything_else", False),
])
def test_orchestrator_enabled_truthy_values(monkeypatch, val, expected):
    monkeypatch.setenv("AGENT_ORCHESTRATOR_ENABLED", val)
    from orchestration.runtime import orchestrator_enabled
    assert orchestrator_enabled() is expected


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_build_event_bus_explicit_memory(monkeypatch):
    monkeypatch.setenv("AGENT_BUS", "memory")
    # REDIS_URL would normally trigger redis path, but explicit memory wins.
    monkeypatch.setenv("REDIS_URL", "redis://should-not-be-used:6379/0")
    from orchestration.runtime import build_event_bus
    from orchestration.event_bus import InMemoryEventBus
    bus = await build_event_bus()
    assert isinstance(bus, InMemoryEventBus)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_build_event_bus_auto_no_redis_url_falls_back(monkeypatch):
    monkeypatch.setenv("AGENT_BUS", "auto")
    monkeypatch.delenv("REDIS_URL", raising=False)
    from orchestration.runtime import build_event_bus
    from orchestration.event_bus import InMemoryEventBus
    bus = await build_event_bus()
    assert isinstance(bus, InMemoryEventBus)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_build_event_bus_redis_mode_requires_url(monkeypatch):
    monkeypatch.setenv("AGENT_BUS", "redis")
    monkeypatch.delenv("REDIS_URL", raising=False)
    from orchestration.runtime import build_event_bus
    with pytest.raises(RuntimeError, match="REDIS_URL is empty"):
        await build_event_bus()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_build_event_bus_auto_with_unreachable_redis_falls_back(monkeypatch):
    monkeypatch.setenv("AGENT_BUS", "auto")
    # Unreachable port — ping should fail; factory should degrade to InMemory.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from orchestration.runtime import build_event_bus
    from orchestration.event_bus import InMemoryEventBus
    bus = await build_event_bus()
    assert isinstance(bus, InMemoryEventBus)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_build_orchestrator_uses_env_max_failures(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_FAILURES", "8")
    from orchestration.runtime import build_orchestrator
    orch = await build_orchestrator()
    assert orch._max_consecutive_failures == 8


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_build_orchestrator_default_max_failures(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_FAILURES", raising=False)
    from orchestration.runtime import build_orchestrator
    orch = await build_orchestrator()
    assert orch._max_consecutive_failures == 5


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_build_orchestrator_invalid_max_failures_uses_default(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_FAILURES", "not-a-number")
    from orchestration.runtime import build_orchestrator
    orch = await build_orchestrator()
    assert orch._max_consecutive_failures == 5


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_build_orchestrator_zero_clamps_to_one(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_FAILURES", "0")
    from orchestration.runtime import build_orchestrator
    orch = await build_orchestrator()
    assert orch._max_consecutive_failures == 1
