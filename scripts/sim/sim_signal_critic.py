"""Simulation: SignalCriticAgent observe-only invariants.

Verifies the critical safety property: NO MATTER what the critic does,
the original signal flow continues unaffected.

  1. Healthy events → log + emit critique, ack
  2. Rejectable events → log + emit critique, ack (no DLQ)
  3. Critic chain bug → fail-open: log + ack (no crash, no propagation)
  4. Bus emission failure → fail-open: log + ack (no crash)
  5. Garbage payload → schema critic rejects, but still ack + log
  6. Concurrent events → each gets one verdict
"""
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration import InMemoryEventBus
from orchestration.agents.signal_critic_agent import SignalCriticAgent
from orchestration.event_envelope import EventEnvelope


def _fresh_envelope(payload: dict, trace_id: str = "tx") -> EventEnvelope:
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return EventEnvelope(
        trace_id=trace_id,
        request_id=trace_id,
        tenant_id="-",
        agent_name="test_producer",
        timestamp=ts,
        event_type="signal.candidate",
        payload=payload,
    )


async def scenario_1_healthy_event_accepted():
    print("  Scenario 1: healthy signal → verdict=accept, event acked, critique emitted")
    bus = InMemoryEventBus()
    agent = SignalCriticAgent()
    agent.event_bus = bus
    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish(
        "events:signal:candidate",
        _fresh_envelope({"asset": "NQ", "confidence": 80, "decision": "BUY"}),
    )

    stats = await agent.tick()
    if not stats.success:
        print(f"    FAIL: tick.success={stats.success}")
        return False

    pending = bus._peek_pending("events:signal:candidate", "signal.critic.observe")
    if pending != []:
        print(f"    FAIL: pending should be empty after ack, got {pending}")
        return False

    critique = await bus.try_consume_one(
        stream="events:signal:signal.critique", group="probe", consumer="c"
    )
    if critique is None:
        print("    FAIL: no critique emitted")
        return False
    if critique.payload["verdict"] != "accept":
        print(f"    FAIL: expected accept, got {critique.payload['verdict']}")
        return False
    print("    OK")
    return True


async def scenario_2_reject_path_still_acks():
    print("  Scenario 2: rejectable signal (low confidence) → reject logged but acked + no DLQ")
    bus = InMemoryEventBus()
    agent = SignalCriticAgent()
    agent.event_bus = bus
    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish(
        "events:signal:candidate",
        _fresh_envelope({"asset": "NQ", "confidence": 5, "decision": "BUY"}),
    )

    await agent.tick()

    pending = bus._peek_pending("events:signal:candidate", "signal.critic.observe")
    dlq_len = await bus.stream_length("dlq:signal:candidate")
    if pending != [] or dlq_len != 0:
        print(f"    FAIL: pending={pending} dlq={dlq_len} (both should be empty)")
        return False

    critique = await bus.try_consume_one(
        stream="events:signal:signal.critique", group="probe", consumer="c"
    )
    if critique is None or critique.payload["verdict"] != "reject":
        print(f"    FAIL: critique not as expected")
        return False
    print(f"    OK — rejected as 'below_confidence_floor' but no enforcement")
    return True


async def scenario_3_chain_exception_fails_open():
    print("  Scenario 3: critic chain raises → fail-open: log, ack, no crash")
    bus = InMemoryEventBus()
    agent = SignalCriticAgent()
    agent.event_bus = bus

    # Sabotage chain
    async def _explode(envelope):
        raise RuntimeError("forced chain failure")

    agent._chain.evaluate = _explode  # type: ignore[method-assign]

    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish(
        "events:signal:candidate",
        _fresh_envelope({"asset": "NQ", "confidence": 80, "decision": "BUY"}),
    )

    stats = await agent.tick()
    if not stats.success:
        print(f"    FAIL: tick should have succeeded (fail-open); got {stats.success}")
        return False

    pending = bus._peek_pending("events:signal:candidate", "signal.critic.observe")
    if pending != []:
        print(f"    FAIL: pending should be empty, got {pending}")
        return False

    print("    OK — chain exception swallowed; event acked; tick.success=True")
    return True


async def scenario_4_bus_emit_failure_fails_open():
    print("  Scenario 4: bus emit_event raises → fail-open: log, ack, no crash")
    bus = InMemoryEventBus()
    agent = SignalCriticAgent()
    agent.event_bus = bus

    # Sabotage emit_event
    original_emit = agent.emit_event

    async def _fail_emit(*args, **kwargs):
        raise ConnectionError("simulated bus down")

    agent.emit_event = _fail_emit  # type: ignore[method-assign]

    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish(
        "events:signal:candidate",
        _fresh_envelope({"asset": "NQ", "confidence": 80, "decision": "BUY"}),
    )

    stats = await agent.tick()
    if not stats.success:
        print(f"    FAIL: tick should have succeeded; got {stats.success}")
        return False

    print("    OK — emit failure swallowed; tick.success=True")
    return True


async def scenario_5_malformed_payload():
    print("  Scenario 5: payload missing required fields → schema rejects, no DLQ, no propagation")
    bus = InMemoryEventBus()
    agent = SignalCriticAgent()
    agent.event_bus = bus
    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish(
        "events:signal:candidate",
        _fresh_envelope({}),  # totally empty
    )

    await agent.tick()

    dlq = await bus.stream_length("dlq:signal:candidate")
    pending = bus._peek_pending("events:signal:candidate", "signal.critic.observe")
    if dlq != 0 or pending != []:
        print(f"    FAIL: dlq={dlq} pending={pending}")
        return False

    critique = await bus.try_consume_one(
        stream="events:signal:signal.critique", group="probe", consumer="c"
    )
    if critique is None or critique.payload["reason"] != "missing_asset":
        print(f"    FAIL: critique={critique}")
        return False
    print(f"    OK — schema reject logged, no enforcement")
    return True


async def scenario_6_concurrent_events():
    print("  Scenario 6: 5 events queued → 5 verdicts emitted in order")
    bus = InMemoryEventBus()
    agent = SignalCriticAgent()
    agent.event_bus = bus
    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    for i in range(5):
        await bus.publish(
            "events:signal:candidate",
            _fresh_envelope(
                {"asset": f"asset_{i}", "confidence": 50 + i * 10, "decision": "BUY"},
                trace_id=f"trace-{i}",
            ),
        )

    for _ in range(5):
        await agent.tick()

    # 5 verdicts in critique stream
    seen = []
    while (c := await bus.try_consume_one(
        stream="events:signal:signal.critique", group="p", consumer="c"
    )):
        seen.append(c.payload["original_trace_id"])

    expected = [f"trace-{i}" for i in range(5)]
    if seen != expected:
        print(f"    FAIL: expected {expected}, got {seen}")
        return False
    print(f"    OK — 5 verdicts in order, all observe-only")
    return True


async def main():
    print("=== sim_signal_critic — observe-only invariants ===")
    print()
    scenarios = [
        scenario_1_healthy_event_accepted,
        scenario_2_reject_path_still_acks,
        scenario_3_chain_exception_fails_open,
        scenario_4_bus_emit_failure_fails_open,
        scenario_5_malformed_payload,
        scenario_6_concurrent_events,
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
