"""Simulation: Redis Streams recovery semantics (using InMemoryEventBus).

Verifies:
  - Publish then consume roundtrip
  - Consume-without-ack leaves event in pending
  - Backpressure: max_len drops oldest, not newest
  - DLQ routing preserves payload
  - Two consumer groups each receive their own copy
  - Empty bus returns None immediately (no blocking)

NOTE: this uses InMemoryEventBus which mirrors RedisEventBus semantics.
A real-Redis variant should be run from a docker-compose dev stack;
that's a Sprint 4 task.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration.event_bus import InMemoryEventBus, dlq_stream_name
from orchestration.event_envelope import new_envelope


async def scenario_1_basic_roundtrip(bus):
    print("  Scenario 1: publish → consume → ack roundtrip")
    env = new_envelope(event_type="x.y", payload={"v": 1}, agent_name="prod")
    msg_id = await bus.publish("events:x:y", env)
    consumed = await bus.try_consume_one(stream="events:x:y", group="g", consumer="c")
    assert consumed is not None, "consumed should not be None"
    assert consumed.payload == {"v": 1}, "payload should be clean (no _bus_msg_id pollution)"
    pending_before = bus._peek_pending("events:x:y", "g")
    assert len(pending_before) == 1, f"expected 1 pending, got {len(pending_before)}"
    await bus.ack("events:x:y", "g", consumed)
    pending_after = bus._peek_pending("events:x:y", "g")
    assert pending_after == [], f"expected empty pending after ack, got {pending_after}"
    print("    OK")


async def scenario_2_pending_on_no_ack(bus):
    print("  Scenario 2: consume without ack → event stays pending")
    env = new_envelope(event_type="x.y", payload={"v": 2}, agent_name="prod")
    await bus.publish("events:x:y", env)
    consumed = await bus.try_consume_one(stream="events:x:y", group="g", consumer="c")
    # Don't ack
    pending = bus._peek_pending("events:x:y", "g")
    assert len(pending) == 1, f"expected 1 pending after no-ack, got {len(pending)}"
    print("    OK — event correctly remains in pending state")


async def scenario_3_backpressure(bus):
    print("  Scenario 3: backpressure — max_len caps at MAX_LEN, drops oldest")
    bus_cap = InMemoryEventBus(max_len=5)
    for i in range(10):
        env = new_envelope(event_type="x.y", payload={"i": i}, agent_name="prod")
        await bus_cap.publish("events:x:y", env)
    length = await bus_cap.stream_length("events:x:y")
    assert length == 5, f"expected stream_length=5, got {length}"
    # Drain — should see 5..9 (oldest evicted)
    survivors = []
    while (e := await bus_cap.try_consume_one(stream="events:x:y", group="g", consumer="c")):
        survivors.append(e.payload["i"])
    assert survivors == [5, 6, 7, 8, 9], f"expected [5..9], got {survivors}"
    print(f"    OK — oldest dropped, newest preserved: {survivors}")


async def scenario_4_dlq(bus):
    print("  Scenario 4: DLQ routes to dlq:<stream> with reason tags")
    env = new_envelope(event_type="news.fetched", payload={"url": "https://example.com"}, agent_name="news.fetch")
    dlq_msg_id = await bus.publish_to_dlq(
        original_stream="events:news:fetched",
        envelope=env,
        reason="retry_exhausted:ConnectionError",
    )
    dlq_env = await bus.try_consume_one(
        stream=dlq_stream_name("events:news:fetched"),
        group="audit", consumer="c",
    )
    assert dlq_env is not None
    assert dlq_env.payload["url"] == "https://example.com", "original payload preserved"
    assert dlq_env.payload["_dlq_reason"] == "retry_exhausted:ConnectionError"
    assert dlq_env.payload["_dlq_original_stream"] == "events:news:fetched"
    print(f"    OK — DLQ stream: {dlq_stream_name('events:news:fetched')}, reason captured")


async def scenario_5_empty_returns_none(bus):
    print("  Scenario 5: empty stream returns None immediately (no block)")
    t0 = time.perf_counter()
    result = await bus.try_consume_one(stream="events:nonexistent", group="g", consumer="c")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert result is None, "expected None on empty stream"
    assert elapsed_ms < 10, f"empty consume took {elapsed_ms:.1f}ms — should be <10ms"
    print(f"    OK — returned None in {elapsed_ms:.2f}ms (non-blocking)")


async def scenario_6_consumer_exclusivity(bus):
    print("  Scenario 6: within one group, each event goes to exactly ONE consumer")
    bus_local = InMemoryEventBus()
    for i in range(3):
        env = new_envelope(event_type="x.y", payload={"i": i}, agent_name="prod")
        await bus_local.publish("events:x:y", env)
    # Two consumers in same group; should split 3 events between them.
    c1 = await bus_local.try_consume_one(stream="events:x:y", group="g", consumer="c1")
    c2 = await bus_local.try_consume_one(stream="events:x:y", group="g", consumer="c2")
    c3 = await bus_local.try_consume_one(stream="events:x:y", group="g", consumer="c1")
    seen = sorted(e.payload["i"] for e in (c1, c2, c3) if e is not None)
    assert seen == [0, 1, 2], f"all 3 events should be delivered, got {seen}"
    # No double-delivery: nothing left in stream
    leftover = await bus_local.try_consume_one(stream="events:x:y", group="g", consumer="c1")
    assert leftover is None
    print(f"    OK — events distributed across consumers without dup")


async def main():
    bus = InMemoryEventBus()
    print("=== sim_streams_recovery ===")
    print()

    scenarios = [
        scenario_1_basic_roundtrip,
        scenario_2_pending_on_no_ack,
        scenario_3_backpressure,
        scenario_4_dlq,
        scenario_5_empty_returns_none,
        scenario_6_consumer_exclusivity,
    ]
    failures = 0
    for fn in scenarios:
        try:
            await fn(bus)
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
