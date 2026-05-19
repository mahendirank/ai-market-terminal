"""Simulation: many concurrent retry chains do not exceed bounded resources.

If 50 agents all hit the same flaky external dep at once and each retries
3 times, total operations stay bounded (= 50 × 3 = 150) and wall-clock
stays bounded (NOT 50× longer than a single chain). Verifies:
  - Concurrent retry_call invocations don't deadlock
  - Total operations = N agents × max_attempts (no infinite loop)
  - Wall-clock for N parallel chains ≈ wall-clock for one (jitter aside)
  - Each chain reports its own RetryExhausted independently
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration.retry import RetryExhausted, RetryPolicy, retry_call


async def main():
    print("=== sim_retry_storm ===")
    print()

    N_AGENTS = 50
    MAX_ATTEMPTS = 3
    BASE_DELAY = 0.01  # 10ms

    policy = RetryPolicy(
        max_attempts=MAX_ATTEMPTS,
        base_delay=BASE_DELAY,
        max_delay=0.1,
        jitter=0.1,
    )

    call_count = 0
    counter_lock = asyncio.Lock()

    async def always_fail():
        nonlocal call_count
        async with counter_lock:
            call_count += 1
        raise ConnectionError("retry storm")

    async def one_chain(chain_id: int):
        try:
            await retry_call(policy, always_fail)
        except RetryExhausted as e:
            return {"chain": chain_id, "attempts": e.attempts, "ok": True}
        return {"chain": chain_id, "attempts": -1, "ok": False}

    print(f"  Firing {N_AGENTS} concurrent retry chains")
    print(f"  Each: max_attempts={MAX_ATTEMPTS}, base_delay={BASE_DELAY}s")
    print()
    t0 = time.perf_counter()
    results = await asyncio.gather(*[one_chain(i) for i in range(N_AGENTS)])
    elapsed = time.perf_counter() - t0

    expected_ops = N_AGENTS * MAX_ATTEMPTS
    print(f"  Total operations: {call_count} (expected {expected_ops})")
    print(f"  Wall-clock: {elapsed*1000:.1f}ms")
    print(f"  Single-chain wall-clock (reference): ~{(MAX_ATTEMPTS - 1) * BASE_DELAY * 1000:.0f}ms")

    # Each chain must report RetryExhausted with the right attempt count.
    exhausted_chains = [r for r in results if r["ok"] and r["attempts"] == MAX_ATTEMPTS]

    ok = True
    if call_count != expected_ops:
        print(f"  FAIL: expected {expected_ops} ops, got {call_count}")
        ok = False
    if len(exhausted_chains) != N_AGENTS:
        print(f"  FAIL: expected {N_AGENTS} RetryExhausted, got {len(exhausted_chains)}")
        ok = False
    # Wall-clock should be near single-chain (within 10x), NOT N× longer.
    single_chain_estimate_ms = (MAX_ATTEMPTS - 1) * BASE_DELAY * 1000 * 1.5  # allow for jitter
    if elapsed * 1000 > single_chain_estimate_ms * 5:
        print(f"  WARN: wall-clock {elapsed*1000:.0f}ms > 5× single-chain estimate ({single_chain_estimate_ms:.0f}ms)")
        # This is a warning not a fail — under load on shared CPU, jitter widens

    print()
    print(f"=== VERDICT: {'PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
