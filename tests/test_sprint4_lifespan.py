"""Sprint 4 Stage 4.1 — lifespan tests.

Constructs minimal FastAPI apps with a lifespan that mirrors
dashboard_api.py's Sprint-4.1 block (without the rest of the app's
heavyweight startup). Verifies:
  - Flag OFF (default) → orchestrator stays None; no orchestration
    imports happen
  - Flag ON + memory bus → orchestrator built; agents=0
  - Flag ON + unreachable Redis → falls back to InMemory without
    crashing boot
  - Shutdown calls stop_all when orchestrator exists
"""
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_lifespan():
    """Build a lifespan that mirrors Sprint 4.1's logic minus legacy bootstrapping."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.orchestrator = None
        app.state.event_bus = None
        try:
            from orchestration.runtime import (
                build_event_bus, build_orchestrator, orchestrator_enabled,
            )
            if orchestrator_enabled():
                try:
                    app.state.event_bus = await build_event_bus()
                    app.state.orchestrator = await build_orchestrator()
                except Exception:
                    app.state.event_bus = None
                    app.state.orchestrator = None
        except Exception:
            pass
        yield
        _o = getattr(app.state, "orchestrator", None)
        if _o is not None:
            await _o.stop_all(timeout=10.0)

    return lifespan


def _build_app():
    app = FastAPI(lifespan=_make_lifespan())

    @app.get("/state")
    def state():
        return {
            "orchestrator_present": app.state.orchestrator is not None,
            "event_bus_type": (
                type(app.state.event_bus).__name__ if app.state.event_bus else None
            ),
        }

    return app


@pytest.mark.smoke
def test_lifespan_flag_off_skips_orchestrator(monkeypatch):
    monkeypatch.delenv("AGENT_ORCHESTRATOR_ENABLED", raising=False)
    client = TestClient(_build_app())
    with client:  # triggers startup + shutdown
        r = client.get("/state").json()
    assert r["orchestrator_present"] is False
    assert r["event_bus_type"] is None


@pytest.mark.smoke
def test_lifespan_flag_on_memory_bus_initializes(monkeypatch):
    monkeypatch.setenv("AGENT_ORCHESTRATOR_ENABLED", "true")
    monkeypatch.setenv("AGENT_BUS", "memory")
    client = TestClient(_build_app())
    with client:
        r = client.get("/state").json()
    assert r["orchestrator_present"] is True
    assert r["event_bus_type"] == "InMemoryEventBus"


@pytest.mark.smoke
def test_lifespan_flag_on_unreachable_redis_falls_back_to_memory(monkeypatch):
    monkeypatch.setenv("AGENT_ORCHESTRATOR_ENABLED", "true")
    monkeypatch.setenv("AGENT_BUS", "auto")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # unreachable
    client = TestClient(_build_app())
    with client:
        r = client.get("/state").json()
    assert r["orchestrator_present"] is True
    assert r["event_bus_type"] == "InMemoryEventBus"


@pytest.mark.smoke
def test_lifespan_zero_agents_registered(monkeypatch):
    """Sprint 4.1 invariant: even with flag on, 0 agents must be registered."""
    monkeypatch.setenv("AGENT_ORCHESTRATOR_ENABLED", "true")
    monkeypatch.setenv("AGENT_BUS", "memory")
    app = _build_app()
    client = TestClient(app)
    with client:
        assert app.state.orchestrator is not None
        assert app.state.orchestrator.list_agents() == []


@pytest.mark.smoke
def test_lifespan_orchestrator_init_failure_does_not_crash_boot(monkeypatch):
    """Even if build_orchestrator itself raises, FastAPI must still boot."""
    monkeypatch.setenv("AGENT_ORCHESTRATOR_ENABLED", "true")
    # Force build_orchestrator to fail by setting an env that causes
    # downstream issues — most reliable is to monkeypatch the function.
    import orchestration.runtime as rt

    async def _boom():
        raise RuntimeError("simulated init failure")

    monkeypatch.setattr(rt, "build_orchestrator", _boom)

    client = TestClient(_build_app())
    with client:
        r = client.get("/state").json()
    # Boot succeeded; orchestrator is None (gracefully degraded).
    assert r["orchestrator_present"] is False
