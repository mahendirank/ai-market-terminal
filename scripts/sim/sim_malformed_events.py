"""Simulation: malformed event payloads / envelopes.

Verifies:
  - EventEnvelope.from_json raises clearly on garbage input
  - EventEnvelope.from_dict tolerates unknown future fields (forward compat)
  - Consumer's input_critic rejects malformed payloads with a clear reason
  - A predicate that raises returns critic_internal_error (doesn't propagate)
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestration.critic import SchemaCritic
from orchestration.event_envelope import EventEnvelope, new_envelope


async def scenario_1_garbage_json_raises():
    print("  Scenario 1: from_json on garbage raises ValueError (clear, not silent)")
    try:
        EventEnvelope.from_json("not json at all")
        print("    FAIL: expected JSON decode error")
        return False
    except (ValueError, json.JSONDecodeError):
        print("    OK — clear exception, caller can DLQ")
        return True


async def scenario_2_missing_required_field_raises():
    print("  Scenario 2: from_dict missing 'trace_id' raises TypeError")
    try:
        EventEnvelope.from_dict({"event_type": "x.y", "payload": {}})
        print("    FAIL: expected TypeError (missing required field)")
        return False
    except TypeError:
        print("    OK — TypeError on missing required field")
        return True


async def scenario_3_unknown_future_field_tolerated():
    print("  Scenario 3: unknown future fields ignored (forward compat)")
    data = {
        "trace_id": "t", "request_id": "r", "tenant_id": "-",
        "agent_name": "a", "timestamp": "ts", "event_type": "x.y",
        "payload": {}, "retry_count": 0, "source_agent": None,
        "target_agent": None, "schema_version": 1, "idempotency_key": None,
        "future_field_v9": "from_a_newer_producer",
    }
    try:
        env = EventEnvelope.from_dict(data)
        print(f"    OK — built envelope; unknown 'future_field_v9' silently ignored")
        return True
    except Exception as e:
        print(f"    FAIL: {type(e).__name__}: {e}")
        return False


async def scenario_4_critic_rejects_invalid_payload():
    print("  Scenario 4: SchemaCritic rejects payload missing required key")

    def is_valid_signal(payload):
        if "asset" not in payload:
            return False, "missing_asset"
        return True, None

    critic = SchemaCritic(name="signal.schema", predicate=is_valid_signal)
    env = new_envelope(event_type="signal.candidate", payload={"conf": 80}, agent_name="d")
    result = await critic.evaluate(env)
    if not result.accepted and result.reason == "missing_asset":
        print(f"    OK — rejected with reason='{result.reason}'")
        return True
    print(f"    FAIL: accepted={result.accepted}, reason={result.reason}")
    return False


async def scenario_5_predicate_exception_safe():
    print("  Scenario 5: predicate that RAISES → critic returns critic_internal_error")

    def boom(payload):
        raise RuntimeError("predicate buggy")

    critic = SchemaCritic(name="t", predicate=boom)
    env = new_envelope(event_type="x.y", payload={}, agent_name="a")
    result = await critic.evaluate(env)
    if not result.accepted and result.reason == "critic_internal_error" and result.confidence == 0.0:
        print(f"    OK — handled cleanly: reason='{result.reason}', confidence={result.confidence}")
        return True
    print(f"    FAIL: result={result}")
    return False


async def main():
    print("=== sim_malformed_events ===")
    print()
    scenarios = [
        scenario_1_garbage_json_raises,
        scenario_2_missing_required_field_raises,
        scenario_3_unknown_future_field_tolerated,
        scenario_4_critic_rejects_invalid_payload,
        scenario_5_predicate_exception_safe,
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
