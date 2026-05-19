"""Simulation: circuit breaker state transitions under realistic call patterns.

Verifies:
  - Threshold crossing: CLOSED → OPEN
  - Recovery timeout: OPEN → HALF_OPEN (lazy)
  - HALF_OPEN success: → CLOSED
  - HALF_OPEN failure: → OPEN again, opened_at reset
  - call() raises CircuitOpenError when OPEN; doesn't dial the service
  - Registry returns the same breaker on repeated lookup
  - Force open / force close work
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitRegistry,
    CircuitState,
)


async def scenario_1_threshold_crossing():
    print("  Scenario 1: CLOSED → OPEN after failure_threshold")
    cb = CircuitBreaker(service="x", failure_threshold=3, recovery_timeout=10.0)
    for i in range(2):
        await cb.record_failure()
    assert cb.state == CircuitState.CLOSED, f"after 2 failures expected CLOSED, got {cb.state}"
    print(f"    after 2 failures: {cb.state.value} (threshold=3)")
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN
    print(f"    after 3 failures: {cb.state.value} → tripped")
    print("    OK")


async def scenario_2_open_to_half_open():
    print("  Scenario 2: OPEN → HALF_OPEN after recovery_timeout")
    cb = CircuitBreaker(service="x", failure_threshold=1, recovery_timeout=0.1)
    await cb.record_failure()
    assert cb.is_open()
    print(f"    initial: {cb.state.value} (just opened)")
    await asyncio.sleep(0.12)
    # The transition is LAZY — needs a query to materialize.
    can = cb.can_attempt()
    assert cb.state == CircuitState.HALF_OPEN, f"expected HALF_OPEN, got {cb.state}"
    print(f"    after 0.12s + can_attempt(): {cb.state.value} (probe allowed)")
    print("    OK")


async def scenario_3_half_open_probe_success():
    print("  Scenario 3: HALF_OPEN probe success → CLOSED")
    cb = CircuitBreaker(service="x", failure_threshold=1, recovery_timeout=0.05, half_open_success_threshold=1)
    await cb.record_failure()
    await asyncio.sleep(0.06)
    cb._maybe_half_open()
    assert cb.state == CircuitState.HALF_OPEN
    await cb.record_success()
    assert cb.state == CircuitState.CLOSED
    print(f"    final: {cb.state.value} (probe succeeded, breaker reset)")
    print("    OK")


async def scenario_4_half_open_probe_failure():
    print("  Scenario 4: HALF_OPEN probe failure → OPEN (with fresh opened_at)")
    cb = CircuitBreaker(service="x", failure_threshold=1, recovery_timeout=0.05)
    await cb.record_failure()
    first_opened_at = cb._opened_at
    await asyncio.sleep(0.06)
    cb._maybe_half_open()
    assert cb.state == CircuitState.HALF_OPEN
    await cb.record_failure()  # probe fails
    assert cb.state == CircuitState.OPEN
    second_opened_at = cb._opened_at
    assert second_opened_at > first_opened_at, "opened_at should be refreshed"
    print(f"    final: {cb.state.value}, opened_at refreshed (Δ={second_opened_at - first_opened_at:.3f}s)")
    print("    OK")


async def scenario_5_call_rejects_when_open():
    print("  Scenario 5: call(fn) raises CircuitOpenError without dialing")
    cb = CircuitBreaker(service="x", failure_threshold=1, recovery_timeout=10.0)
    invocations = 0

    async def real_call():
        nonlocal invocations
        invocations += 1
        raise ConnectionError("flake")

    # First call: fails, opens the breaker.
    try:
        await cb.call(real_call)
    except ConnectionError:
        pass
    assert cb.is_open()
    assert invocations == 1, f"first call should dial, got {invocations} invocations"

    # Second call: rejected, real_call NOT invoked.
    async def safe_call():
        nonlocal invocations
        invocations += 1

    try:
        await cb.call(safe_call)
        print("    FAIL: should have raised CircuitOpenError")
        return False
    except CircuitOpenError as e:
        assert e.service == "x"
        assert invocations == 1, f"safe_call should NOT have run, got {invocations}"
        print(f"    rejected without dial: invocations={invocations}, error.service={e.service!r}")
        print("    OK")
        return True


async def scenario_6_registry_singleton():
    print("  Scenario 6: registry returns same breaker on repeat lookup")
    reg = CircuitRegistry()
    a = reg.get_or_create("groq", failure_threshold=10)
    b = reg.get_or_create("groq", failure_threshold=999)  # config ignored
    assert a is b
    assert a.failure_threshold == 10
    snap = reg.snapshot()
    assert len(snap) == 1
    assert snap[0]["service"] == "groq"
    print(f"    registered: {[s['service'] for s in snap]}; snapshot.state: {snap[0]['state']}")
    print("    OK")


async def scenario_7_force_open_close():
    print("  Scenario 7: force_open / force_close")
    cb = CircuitBreaker(service="x")
    assert cb.is_closed()
    await cb.force_open()
    assert cb.is_open()
    print(f"    after force_open: {cb.state.value}")
    await cb.force_close()
    assert cb.is_closed()
    print(f"    after force_close: {cb.state.value}")
    print("    OK")


async def main():
    print("=== sim_circuit_breaker ===")
    print()
    scenarios = [
        scenario_1_threshold_crossing,
        scenario_2_open_to_half_open,
        scenario_3_half_open_probe_success,
        scenario_4_half_open_probe_failure,
        scenario_5_call_rejects_when_open,
        scenario_6_registry_singleton,
        scenario_7_force_open_close,
    ]
    failures = 0
    for fn in scenarios:
        try:
            await fn()
        except AssertionError as e:
            print(f"    FAIL: {e}")
            failures += 1
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}")
            failures += 1
        print()
    print(f"=== VERDICT: {'PASS' if failures == 0 else 'FAIL'} ({len(scenarios) - failures}/{len(scenarios)}) ===")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
