"""Simulation: duplicate-event semantics (intentional non-dedup in Sprint 3).

Sprint 3 does NOT implement automatic dedup. The envelope has an
`idempotency_key` field that consumers MAY use, but the runtime doesn't
enforce. This sim:
  - Verifies that publishing the same envelope twice produces TWO deliveries
  - Verifies that idempotency_key field roundtrips correctly
  - Verifies that a consumer-side dedup pattern (using a Redis SET) works
    when explicitly written
  - Documents the residual: agents that need idempotency MUST opt in
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration.event_bus import InMemoryEventBus
from orchestration.event_envelope import EventEnvelope, new_envelope


async def scenario_1_double_publish_doubles_delivery():
    print("  Scenario 1: same envelope published 2× → consumer sees 2 events")
    bus = InMemoryEventBus()
    env = new_envelope(
        event_type="signal.candidate",
        payload={"asset": "NQ"},
        agent_name="decision",
        idempotency_key="same-key-12345",
    )
    await bus.publish("events:signal:candidate", env)
    await bus.publish("events:signal:candidate", env)
    consumed = []
    for _ in range(3):
        e = await bus.try_consume_one(stream="events:signal:candidate", group="g", consumer="c")
        if e is None:
            break
        consumed.append(e)
    if len(consumed) == 2:
        # Both have the same idempotency_key — consumer can dedup if it wants.
        keys = [c.idempotency_key for c in consumed]
        if keys == ["same-key-12345", "same-key-12345"]:
            print(f"    OK — 2 deliveries with matching idempotency_key (consumer decides)")
            return True
    print(f"    FAIL: consumed {len(consumed)}, keys mismatch")
    return False


async def scenario_2_idempotency_key_roundtrips_via_json():
    print("  Scenario 2: idempotency_key survives JSON serialization")
    env = new_envelope(
        event_type="x.y", payload={}, agent_name="a",
        idempotency_key="abc-123",
    )
    raw = env.to_json()
    restored = EventEnvelope.from_json(raw)
    if restored.idempotency_key == "abc-123":
        print(f"    OK — idempotency_key preserved through ser/deser")
        return True
    print(f"    FAIL: restored.idempotency_key = {restored.idempotency_key}")
    return False


async def scenario_3_consumer_side_dedup_pattern():
    print("  Scenario 3: explicit consumer-side dedup using a SET (the recommended pattern)")
    bus = InMemoryEventBus()
    for _ in range(3):
        await bus.publish(
            "events:x:y",
            new_envelope(event_type="x.y", payload={"v": 1}, agent_name="a",
                         idempotency_key="dup-key"),
        )

    seen: set[str] = set()
    processed = 0
    while (e := await bus.try_consume_one(stream="events:x:y", group="g", consumer="c")):
        if e.idempotency_key in seen:
            await bus.ack("events:x:y", "g", e)  # drop dup
            continue
        seen.add(e.idempotency_key)
        processed += 1
        await bus.ack("events:x:y", "g", e)

    if processed == 1:
        print(f"    OK — consumer-side dedup deduplicated 3 events → 1 processed")
        return True
    print(f"    FAIL: processed = {processed}, expected 1")
    return False


async def scenario_4_no_idempotency_key_means_dedup_skipped():
    print("  Scenario 4: events WITHOUT idempotency_key are processed each time")
    bus = InMemoryEventBus()
    for _ in range(3):
        await bus.publish(
            "events:x:y",
            new_envelope(event_type="x.y", payload={"v": 1}, agent_name="a"),
        )
    seen: set[str] = set()
    processed = 0
    while (e := await bus.try_consume_one(stream="events:x:y", group="g", consumer="c")):
        # Caller's dedup logic must handle None key:
        if e.idempotency_key is not None and e.idempotency_key in seen:
            await bus.ack("events:x:y", "g", e)
            continue
        if e.idempotency_key:
            seen.add(e.idempotency_key)
        processed += 1
        await bus.ack("events:x:y", "g", e)
    if processed == 3:
        print(f"    OK — without idempotency_key, all 3 events processed (dedup correctly opted-out)")
        return True
    print(f"    FAIL: processed = {processed}, expected 3")
    return False


async def main():
    print("=== sim_duplicate_events ===")
    print()
    scenarios = [
        scenario_1_double_publish_doubles_delivery,
        scenario_2_idempotency_key_roundtrips_via_json,
        scenario_3_consumer_side_dedup_pattern,
        scenario_4_no_idempotency_key_means_dedup_skipped,
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
