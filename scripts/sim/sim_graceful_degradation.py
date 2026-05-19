"""Simulation: graceful degradation patterns when external deps fail.

Three canonical degradation strategies (from CIRCUIT_BREAKER_PLAN.md §5):
  A. Serve stale — when freshness has tolerance (intel, regime)
  B. Fall back to simpler logic — when LLM-quality matters but rules suffice
  C. Queue for later — when freshness doesn't matter (notifications)

Each scenario uses a circuit breaker in OPEN state and demonstrates the
recommended pattern.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration.circuit_breaker import CircuitBreaker, CircuitOpenError


async def scenario_a_serve_stale():
    print("  Pattern A: Serve stale cached value when circuit is OPEN")
    cb = CircuitBreaker(service="intel_llm", failure_threshold=1, recovery_timeout=60.0)
    cache = {"intel:current": "stale-but-valid-narrative"}

    async def call_llm():
        raise ConnectionError("provider down")

    # First call: trips the circuit.
    try:
        await cb.call(call_llm)
    except ConnectionError:
        pass
    assert cb.is_open()

    # Second call: degraded path.
    try:
        result = await cb.call(call_llm)
    except CircuitOpenError:
        result = cache["intel:current"]

    if result == "stale-but-valid-narrative":
        print(f"    OK — served stale: {result!r}")
        return True
    print(f"    FAIL: result = {result}")
    return False


async def scenario_b_fall_back_to_rules():
    print("  Pattern B: Fall back to deterministic logic when AI is down")
    cb = CircuitBreaker(service="reasoning_llm", failure_threshold=1, recovery_timeout=60.0)

    async def llm_judgement(asset_data):
        raise TimeoutError("LLM timeout")

    def rule_based_judgement(asset_data):
        # Simple deterministic logic.
        return "BUY" if asset_data["score"] > 70 else "WAIT"

    asset_data = {"asset": "NQ", "score": 85}

    # Trip the circuit.
    try:
        await cb.call(lambda: llm_judgement(asset_data))
    except TimeoutError:
        pass
    assert cb.is_open()

    # Degraded path: rules.
    try:
        decision = await cb.call(lambda: llm_judgement(asset_data))
    except CircuitOpenError:
        decision = rule_based_judgement(asset_data)

    if decision == "BUY":
        print(f"    OK — fell back to rules; decision={decision}")
        return True
    print(f"    FAIL: decision = {decision}")
    return False


async def scenario_c_queue_for_later():
    print("  Pattern C: Queue non-critical work (e.g. Telegram notifications)")
    cb = CircuitBreaker(service="telegram", failure_threshold=1, recovery_timeout=60.0)
    retry_queue = []

    async def send_alert(message):
        raise ConnectionRefusedError("telegram unreachable")

    # Trip the circuit.
    try:
        await cb.call(lambda: send_alert("first alert"))
    except ConnectionRefusedError:
        pass
    assert cb.is_open()

    # Subsequent alerts queue instead of failing the caller.
    pending = ["alert 1", "alert 2", "alert 3"]
    for msg in pending:
        try:
            await cb.call(lambda: send_alert(msg))
        except CircuitOpenError:
            retry_queue.append(msg)

    if retry_queue == pending:
        print(f"    OK — {len(retry_queue)} alerts queued for retry when circuit closes")
        return True
    print(f"    FAIL: retry_queue = {retry_queue}")
    return False


async def scenario_d_recovery_after_timeout():
    print("  Recovery: circuit allows probe after recovery_timeout, then closes on success")
    cb = CircuitBreaker(
        service="x", failure_threshold=1, recovery_timeout=0.05,
        half_open_success_threshold=1,
    )

    async def flaky_then_healthy():
        if not getattr(flaky_then_healthy, "_healed", False):
            flaky_then_healthy._healed = True
            raise ConnectionError("first call fails")
        return "all good"

    # First call: fails, trips.
    try:
        await cb.call(flaky_then_healthy)
    except ConnectionError:
        pass
    assert cb.is_open()
    # Wait past recovery_timeout.
    await asyncio.sleep(0.06)
    # Next call: HALF_OPEN; succeeds; CLOSED.
    result = await cb.call(flaky_then_healthy)
    if result == "all good" and cb.is_closed():
        print(f"    OK — circuit recovered after timeout (probe succeeded, state=CLOSED)")
        return True
    print(f"    FAIL: result={result}, state={cb.state.value}")
    return False


async def main():
    print("=== sim_graceful_degradation ===")
    print()
    scenarios = [
        scenario_a_serve_stale,
        scenario_b_fall_back_to_rules,
        scenario_c_queue_for_later,
        scenario_d_recovery_after_timeout,
    ]
    passed = 0
    for fn in scenarios:
        try:
            if await fn():
                passed += 1
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}")
        print()
    print(f"=== VERDICT: {'PASS' if passed == len(scenarios) else 'FAIL'} ({passed}/{len(scenarios)}) ===")
    return 0 if passed == len(scenarios) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
