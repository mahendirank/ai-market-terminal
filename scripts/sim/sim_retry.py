"""Simulation: retry behavior under varied failure shapes.

Verifies:
  - Transient failure: succeeds on attempt N
  - Persistent failure: raises RetryExhausted after max_attempts
  - Category-filtered fast-fail: non-retryable category aborts on attempt 1
  - Backoff is bounded and monotonic (modulo jitter)
  - on_attempt callback fires for each failed attempt
  - Decorator form preserves __wrapped__
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration.retry import (
    RetryExhausted,
    RetryPolicy,
    retry_call,
    with_retry,
)


async def scenario_1_transient_then_success():
    print("  Scenario 1: 2 transient fails → 3rd attempt succeeds")
    p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter=0)
    attempts = []

    async def flaky():
        attempts.append(time.perf_counter())
        if len(attempts) < 3:
            raise ConnectionError("transient")
        return "got it"

    t0 = time.perf_counter()
    result = await retry_call(p, flaky)
    elapsed = time.perf_counter() - t0
    print(f"    attempts: {len(attempts)}; result: {result!r}; elapsed: {elapsed*1000:.1f}ms")
    assert result == "got it"
    assert len(attempts) == 3
    print("    OK")


async def scenario_2_exhausted():
    print("  Scenario 2: persistent failure → RetryExhausted")
    p = RetryPolicy(max_attempts=3, base_delay=0.01, jitter=0)
    attempts = 0

    async def always_fail():
        nonlocal attempts
        attempts += 1
        raise ConnectionError("perma")

    try:
        await retry_call(p, always_fail)
        print("    FAIL: should have raised RetryExhausted")
        return False
    except RetryExhausted as e:
        assert e.attempts == 3
        assert isinstance(e.last_exc, ConnectionError)
        assert e.__cause__ is e.last_exc
        print(f"    attempts: {attempts}; raised: {type(e).__name__}({e.attempts} tries); cause: {type(e.last_exc).__name__}")
        print("    OK")
        return True


async def scenario_3_category_fast_fail():
    print("  Scenario 3: non-retryable category → no retry")
    p = RetryPolicy(
        max_attempts=5, base_delay=0.01, jitter=0,
        retryable_categories=frozenset({"external_api"}),
    )
    attempts = 0

    async def validation_error():
        nonlocal attempts
        attempts += 1
        raise ValueError("bad input")

    def classify(e):
        if isinstance(e, ValueError):
            return "validation"
        return "external_api"

    try:
        await retry_call(p, validation_error, classify=classify)
        print("    FAIL: should have raised ValueError immediately")
        return False
    except ValueError:
        assert attempts == 1, f"expected 1 attempt, got {attempts}"
        print(f"    attempts: {attempts}; aborted on non-retryable category")
        print("    OK")
        return True


async def scenario_4_backoff_monotonic():
    print("  Scenario 4: backoff delays are bounded and monotonic (no jitter)")
    p = RetryPolicy(max_attempts=4, base_delay=0.05, max_delay=1.0, backoff_multiplier=2.0, jitter=0)
    delays = [p.delay_for(n) for n in range(1, 6)]
    # attempt 1 = 0, attempt 2 = base, attempt 3 = base*2, attempt 4 = base*4 (capped)
    print(f"    delays per attempt: {[f'{d:.3f}s' for d in delays]}")
    assert delays[0] == 0.0
    assert delays[1] == 0.05
    assert delays[2] == 0.10
    assert delays[3] == 0.20
    # Sanity: monotonically non-decreasing
    assert all(delays[i] <= delays[i+1] for i in range(len(delays)-1))
    print("    OK")


async def scenario_5_on_attempt_callback():
    print("  Scenario 5: on_attempt callback records each failed attempt")
    p = RetryPolicy(max_attempts=3, base_delay=0.005, jitter=0)
    seen = []

    async def fn():
        raise RuntimeError(f"attempt #{len(seen) + 1}")

    def on_attempt(n, exc):
        seen.append((n, type(exc).__name__, str(exc)))

    try:
        await retry_call(p, fn, on_attempt=on_attempt)
    except RetryExhausted:
        pass

    print(f"    on_attempt fired: {len(seen)} times")
    for n, t, msg in seen:
        print(f"      attempt #{n} → {t}: {msg}")
    assert len(seen) == 3
    print("    OK")


async def scenario_6_decorator_form():
    print("  Scenario 6: with_retry decorator preserves __wrapped__")
    p = RetryPolicy(max_attempts=2, base_delay=0.001, jitter=0)
    calls = 0

    @with_retry(p)
    async def fetch(x):
        nonlocal calls
        calls += 1
        if calls < 2:
            raise RuntimeError("retry me")
        return x * 10

    result = await fetch(7)
    assert result == 70
    assert calls == 2
    assert hasattr(fetch, "__wrapped__")
    print(f"    result: {result}; attempts: {calls}; __wrapped__: {fetch.__wrapped__.__name__}")
    print("    OK")


async def main():
    print("=== sim_retry ===")
    print()
    scenarios = [
        scenario_1_transient_then_success,
        scenario_2_exhausted,
        scenario_3_category_fast_fail,
        scenario_4_backoff_monotonic,
        scenario_5_on_attempt_callback,
        scenario_6_decorator_form,
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
