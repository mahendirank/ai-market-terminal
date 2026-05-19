"""Simulation: one slow agent does NOT cascade into other agents' timeouts.

Verifies:
  - Agent A with timeout=0.05 cancels its slow run_once cleanly
  - Agent B running concurrently isn't slowed by A's timeout
  - The cancellation is recorded as a failure (not a success)
  - asyncio event loop stays responsive (other tasks make progress)
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration.base_agent import TickAgent


async def scenario_1_timeout_fires_cleanly():
    print("  Scenario 1: tick honors timeout; run_once cancelled at deadline")

    class SlowAgent(TickAgent):
        name = "slow"
        family = "test"
        timeout = 0.05

        async def run_once(self):
            await asyncio.sleep(1.0)  # would block 20× the timeout
        async def handle_failure(self, exc, *, stats):
            pass

    a = SlowAgent()
    t0 = time.perf_counter()
    stats = await a.tick()
    elapsed = time.perf_counter() - t0

    if not stats.success and 0.04 < elapsed < 0.25:
        print(f"    OK — failure recorded; tick wall-clock {elapsed*1000:.1f}ms (≈ timeout)")
        return True
    print(f"    FAIL: success={stats.success}, elapsed={elapsed*1000:.1f}ms, expected ~50ms")
    return False


async def scenario_2_no_cascade_to_other_agents():
    print("  Scenario 2: slow agent A doesn't slow agent B running concurrently")

    class SlowA(TickAgent):
        name = "A"
        family = "test"
        timeout = 0.05
        async def run_once(self):
            await asyncio.sleep(1.0)
        async def handle_failure(self, exc, *, stats):
            pass

    class FastB(TickAgent):
        name = "B"
        family = "test"
        async def run_once(self):
            pass  # instant

    a = SlowA()
    b = FastB()

    t0 = time.perf_counter()
    # Run them at the SAME time via asyncio.gather (simulating orchestrator).
    stats_a, stats_b = await asyncio.gather(a.tick(), b.tick())
    elapsed = time.perf_counter() - t0

    if (not stats_a.success and stats_b.success
            and elapsed < 0.25):  # B doesn't wait for A
        print(f"    OK — A failed (timeout), B succeeded, total {elapsed*1000:.1f}ms (no cascade)")
        return True
    print(f"    FAIL: stats_a.success={stats_a.success}, stats_b.success={stats_b.success}, elapsed={elapsed*1000:.1f}ms")
    return False


async def scenario_3_event_loop_responsive_during_timeout():
    print("  Scenario 3: event loop schedules other tasks during agent timeout")

    progress_marks: list[float] = []

    async def background():
        # Should fire ~ every 10ms while the slow agent is hung.
        for _ in range(5):
            await asyncio.sleep(0.01)
            progress_marks.append(time.perf_counter())

    class SlowAgent(TickAgent):
        name = "slow"
        family = "test"
        timeout = 0.06
        async def run_once(self):
            await asyncio.sleep(0.5)
        async def handle_failure(self, exc, *, stats):
            pass

    a = SlowAgent()
    t0 = time.perf_counter()
    _stats, _bg = await asyncio.gather(a.tick(), background())
    elapsed = time.perf_counter() - t0

    # Background should have ticked 5 times during the agent's timeout window.
    if len(progress_marks) == 5 and elapsed < 0.25:
        print(f"    OK — background made 5/5 progress during agent timeout (loop responsive)")
        return True
    print(f"    FAIL: background made {len(progress_marks)}/5 progress; elapsed={elapsed*1000:.1f}ms")
    return False


async def main():
    print("=== sim_timeout_cascade ===")
    print()
    scenarios = [
        scenario_1_timeout_fires_cleanly,
        scenario_2_no_cascade_to_other_agents,
        scenario_3_event_loop_responsive_during_timeout,
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
