"""Sprint 3 — CircuitBreaker tests."""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitRegistry,
    CircuitState,
)


@pytest.mark.smoke
def test_breaker_starts_closed():
    cb = CircuitBreaker(service="x")
    assert cb.state == CircuitState.CLOSED
    assert cb.is_closed() is True
    assert cb.can_attempt() is True


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_breaker_opens_after_threshold():
    cb = CircuitBreaker(service="x", failure_threshold=3)
    for _ in range(2):
        await cb.record_failure()
    assert cb.is_closed()
    await cb.record_failure()
    assert cb.is_open()
    assert not cb.can_attempt()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_breaker_success_resets_failure_counter():
    cb = CircuitBreaker(service="x", failure_threshold=3)
    await cb.record_failure()
    await cb.record_failure()
    await cb.record_success()  # resets
    await cb.record_failure()
    assert cb.is_closed()  # 1 failure since reset


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_breaker_half_open_after_recovery():
    cb = CircuitBreaker(service="x", failure_threshold=2, recovery_timeout=0.05)
    await cb.record_failure()
    await cb.record_failure()
    assert cb.is_open()
    # Wait past recovery_timeout
    await asyncio.sleep(0.06)
    # Read transitions OPEN → HALF_OPEN lazily on the next query
    assert cb.can_attempt()
    assert cb.state == CircuitState.HALF_OPEN


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_half_open_failure_reopens():
    cb = CircuitBreaker(service="x", failure_threshold=1, recovery_timeout=0.01)
    await cb.record_failure()
    await asyncio.sleep(0.02)
    cb._maybe_half_open()
    assert cb.state == CircuitState.HALF_OPEN
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_half_open_success_closes():
    cb = CircuitBreaker(service="x", failure_threshold=1, recovery_timeout=0.01, half_open_success_threshold=1)
    await cb.record_failure()
    await asyncio.sleep(0.02)
    cb._maybe_half_open()
    assert cb.state == CircuitState.HALF_OPEN
    await cb.record_success()
    assert cb.state == CircuitState.CLOSED


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_call_raises_circuit_open_when_open():
    cb = CircuitBreaker(service="x", failure_threshold=1, recovery_timeout=60.0)
    async def fn():
        raise ConnectionError("nope")
    with pytest.raises(ConnectionError):
        await cb.call(fn)
    # Now open. Next call should be rejected without invoking fn.
    called = 0
    async def fn_safe():
        nonlocal called
        called += 1
    with pytest.raises(CircuitOpenError) as exc:
        await cb.call(fn_safe)
    assert exc.value.service == "x"
    assert called == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_force_open_and_force_close():
    cb = CircuitBreaker(service="x")
    await cb.force_open()
    assert cb.is_open()
    await cb.force_close()
    assert cb.is_closed()


@pytest.mark.smoke
def test_registry_get_or_create():
    reg = CircuitRegistry()
    a = reg.get_or_create("groq", failure_threshold=10)
    b = reg.get_or_create("groq", failure_threshold=999)  # config ignored on existing
    assert a is b
    assert a.failure_threshold == 10


@pytest.mark.smoke
def test_registry_snapshot():
    reg = CircuitRegistry()
    reg.get_or_create("groq")
    reg.get_or_create("anthropic")
    snap = reg.snapshot()
    services = {s["service"] for s in snap}
    assert services == {"groq", "anthropic"}
    for s in snap:
        assert s["state"] == "closed"


@pytest.mark.smoke
def test_snapshot_shape():
    cb = CircuitBreaker(service="groq")
    snap = cb.snapshot()
    assert snap["service"] == "groq"
    assert snap["state"] == "closed"
    assert "failure_threshold" in snap
