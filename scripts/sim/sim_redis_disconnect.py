"""Simulation: RedisEventBus behavior when Redis is unreachable.

Uses a fake-disconnect Redis client that raises ConnectionError on every
operation. Verifies:
  - publish() surfaces the ConnectionError to the caller (NOT swallowed)
  - try_consume_one() surfaces the ConnectionError
  - ensure_group() surfaces the ConnectionError (or BUSYGROUP no-op when reconnected)
  - stream_length() degrades gracefully — returns -1 instead of raising

The reason for "surface vs. swallow": the bus is dumb. Higher layers
(agent retry policy + circuit breaker) decide what to do with a Redis
outage. Swallowing would hide a real outage from the metrics/log surface.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration.event_bus import RedisEventBus
from orchestration.event_envelope import new_envelope


class _DeadRedis:
    """Mock that raises ConnectionError on every call."""

    async def xadd(self, *a, **kw):
        raise ConnectionError("redis unreachable")

    async def xreadgroup(self, *a, **kw):
        raise ConnectionError("redis unreachable")

    async def xack(self, *a, **kw):
        raise ConnectionError("redis unreachable")

    async def xlen(self, *a, **kw):
        raise ConnectionError("redis unreachable")

    async def xgroup_create(self, *a, **kw):
        raise ConnectionError("redis unreachable")


async def scenario_1_publish_surfaces_error():
    print("  Scenario 1: publish() surfaces ConnectionError to caller")
    bus = RedisEventBus(_DeadRedis())
    env = new_envelope(event_type="x.y", payload={}, agent_name="a")
    try:
        await bus.publish("events:x:y", env)
        print("    FAIL: expected ConnectionError")
        return False
    except ConnectionError:
        print("    OK — ConnectionError surfaced, caller can react")
        return True


async def scenario_2_consume_surfaces_error():
    print("  Scenario 2: try_consume_one() surfaces ConnectionError")
    bus = RedisEventBus(_DeadRedis())
    try:
        await bus.try_consume_one(stream="events:x:y", group="g", consumer="c")
        print("    FAIL: expected ConnectionError")
        return False
    except ConnectionError:
        print("    OK — ConnectionError surfaced")
        return True


async def scenario_3_ensure_group_surfaces_error():
    print("  Scenario 3: ensure_group() surfaces ConnectionError when NOT BUSYGROUP")
    bus = RedisEventBus(_DeadRedis())
    try:
        await bus.ensure_group("events:x:y", "g")
        print("    FAIL: expected ConnectionError")
        return False
    except ConnectionError:
        print("    OK — ConnectionError surfaced")
        return True


class _BusyGroupRedis:
    async def xgroup_create(self, *a, **kw):
        raise RuntimeError("BUSYGROUP Consumer Group name already exists")


async def scenario_4_ensure_group_swallows_busygroup():
    print("  Scenario 4: ensure_group() treats BUSYGROUP as no-op (idempotent)")
    bus = RedisEventBus(_BusyGroupRedis())
    try:
        await bus.ensure_group("events:x:y", "g")
        print("    OK — BUSYGROUP swallowed, ensure_group is idempotent")
        return True
    except Exception as e:
        print(f"    FAIL: expected no exception, got {type(e).__name__}: {e}")
        return False


async def scenario_5_stream_length_degrades_gracefully():
    print("  Scenario 5: stream_length() returns -1 instead of raising")
    bus = RedisEventBus(_DeadRedis())
    length = await bus.stream_length("events:x:y")
    if length == -1:
        print("    OK — degraded to -1 (sentinel; safe for health endpoints)")
        return True
    print(f"    FAIL: expected -1, got {length}")
    return False


async def main():
    print("=== sim_redis_disconnect ===")
    print()
    scenarios = [
        scenario_1_publish_surfaces_error,
        scenario_2_consume_surfaces_error,
        scenario_3_ensure_group_surfaces_error,
        scenario_4_ensure_group_swallows_busygroup,
        scenario_5_stream_length_degrades_gracefully,
    ]
    passed = 0
    for fn in scenarios:
        try:
            if await fn():
                passed += 1
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}")
        print()
    total = len(scenarios)
    print(f"=== VERDICT: {'PASS' if passed == total else 'FAIL'} ({passed}/{total}) ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
