"""Sprint 4 Stage 4.1 — admin endpoint tests.

Tests the 3 endpoints in isolation (not via dashboard_api, which is
heavyweight). Mounts a thin FastAPI app + uses the same helpers.

Endpoints under test:
  GET /api/agents
  GET /api/circuits
  GET /api/streams/health
"""
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_app_no_orchestrator():
    """App with no orchestrator (flag-off equivalent)."""
    app = FastAPI()
    app.state.orchestrator = None
    app.state.event_bus = None

    @app.get("/api/agents")
    async def agents():
        from orchestration.admin import agents_snapshot
        return await agents_snapshot(app)

    @app.get("/api/circuits")
    async def circuits():
        from orchestration.admin import circuits_snapshot
        return await circuits_snapshot()

    @app.get("/api/streams/health")
    async def streams_health():
        from orchestration.admin import streams_health_snapshot
        return await streams_health_snapshot(app)

    return app


def _make_app_with_orchestrator():
    """App with empty orchestrator + in-memory bus."""
    from orchestration import InMemoryEventBus, Orchestrator
    app = FastAPI()
    app.state.event_bus = InMemoryEventBus()
    app.state.orchestrator = Orchestrator()

    @app.get("/api/agents")
    async def agents():
        from orchestration.admin import agents_snapshot
        return await agents_snapshot(app)

    @app.get("/api/circuits")
    async def circuits():
        from orchestration.admin import circuits_snapshot
        return await circuits_snapshot()

    @app.get("/api/streams/health")
    async def streams_health():
        from orchestration.admin import streams_health_snapshot
        return await streams_health_snapshot(app)

    return app


# ──────────────────────────────────────────────────────────────────────
# Flag-off (orchestrator disabled) responses
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_api_agents_disabled():
    client = TestClient(_make_app_no_orchestrator())
    r = client.get("/api/agents")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["agents"] == []


@pytest.mark.smoke
def test_api_circuits_always_returns_list():
    """Circuits live in default_registry (process-global); endpoint is
    always available, even when orchestrator is disabled."""
    client = TestClient(_make_app_no_orchestrator())
    r = client.get("/api/circuits")
    assert r.status_code == 200
    body = r.json()
    assert "circuits" in body
    assert isinstance(body["circuits"], list)


@pytest.mark.smoke
def test_api_streams_health_disabled():
    client = TestClient(_make_app_no_orchestrator())
    r = client.get("/api/streams/health")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["streams"] == []


# ──────────────────────────────────────────────────────────────────────
# Flag-on (orchestrator enabled, agents=0) responses
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_api_agents_enabled_with_zero_agents():
    client = TestClient(_make_app_with_orchestrator())
    r = client.get("/api/agents")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["agents"] == []


@pytest.mark.smoke
def test_api_streams_health_enabled_returns_zero_lengths():
    client = TestClient(_make_app_with_orchestrator())
    r = client.get("/api/streams/health")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    # Sprint 4.1: 0 agents → 0 producers → all known streams length 0.
    for s in body["streams"]:
        assert s["length"] == 0


@pytest.mark.smoke
def test_api_streams_health_reports_known_stream_names():
    """Sprint 4.1 ships a static list of known streams."""
    client = TestClient(_make_app_with_orchestrator())
    body = client.get("/api/streams/health").json()
    names = {s["stream"] for s in body["streams"]}
    # Expected starting set per orchestration/admin.py KNOWN_STREAMS.
    assert "events:news:raw" in names
    assert "events:signal:candidate" in names
    assert "dlq:news:raw" in names


@pytest.mark.smoke
def test_admin_endpoints_response_under_100ms():
    """Acceptance criterion from SPRINT_4_PLAN.md §3.2."""
    import time
    client = TestClient(_make_app_with_orchestrator())
    for path in ("/api/agents", "/api/circuits", "/api/streams/health"):
        t0 = time.perf_counter()
        r = client.get(path)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200, f"{path} returned {r.status_code}"
        assert elapsed_ms < 100, f"{path} took {elapsed_ms:.1f}ms (>100ms)"
