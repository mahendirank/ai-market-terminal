"""Sprint 3 — EventEnvelope tests."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration.event_envelope import EventEnvelope, new_envelope, SCHEMA_VERSION


@pytest.mark.smoke
def test_envelope_required_fields():
    env = EventEnvelope(
        trace_id="t1",
        request_id="r1",
        tenant_id="-",
        agent_name="test",
        timestamp="2026-05-18T00:00:00.000Z",
        event_type="test.event",
        payload={"k": "v"},
    )
    assert env.retry_count == 0
    assert env.schema_version == SCHEMA_VERSION
    assert env.source_agent is None
    assert env.target_agent is None


@pytest.mark.smoke
def test_envelope_json_roundtrip():
    env = EventEnvelope(
        trace_id="t1", request_id="r1", tenant_id="acme",
        agent_name="news.fetch", timestamp="2026-05-18T00:00:00.000Z",
        event_type="news.fetched", payload={"headlines": ["x", "y"]},
        retry_count=2, source_agent="news.fetch", target_agent="news.dedup",
    )
    raw = env.to_json()
    parsed = json.loads(raw)
    assert parsed["trace_id"] == "t1"
    assert parsed["retry_count"] == 2
    restored = EventEnvelope.from_json(raw)
    assert restored == env


@pytest.mark.smoke
def test_envelope_from_dict_tolerates_unknown_fields():
    """A newer producer may add fields the older consumer doesn't know."""
    data = {
        "trace_id": "t", "request_id": "r", "tenant_id": "-",
        "agent_name": "a", "timestamp": "2026-05-18T00:00:00.000Z",
        "event_type": "x.y", "payload": {}, "retry_count": 0,
        "source_agent": None, "target_agent": None,
        "schema_version": SCHEMA_VERSION, "idempotency_key": None,
        "future_field_v2": "ignored_safely",
    }
    env = EventEnvelope.from_dict(data)
    assert env.event_type == "x.y"


@pytest.mark.smoke
def test_with_retry_incremented_attaches_last_error():
    env = EventEnvelope(
        trace_id="t", request_id="r", tenant_id="-", agent_name="a",
        timestamp="ts", event_type="x", payload={"k": 1},
    )
    next_env = env.with_retry_incremented(last_error="ConnectionRefused")
    assert next_env.retry_count == 1
    assert next_env.payload["_last_error"] == "ConnectionRefused"
    # Original is unchanged.
    assert env.retry_count == 0
    assert "_last_error" not in env.payload


@pytest.mark.smoke
def test_new_envelope_factory_fills_defaults():
    env = new_envelope(event_type="signal.candidate", payload={"asset": "NQ"}, agent_name="decision")
    assert env.event_type == "signal.candidate"
    assert env.agent_name == "decision"
    assert env.source_agent == "decision"
    assert len(env.trace_id) > 0
    assert len(env.request_id) > 0


@pytest.mark.smoke
def test_envelope_repr_is_short():
    env = new_envelope(event_type="x.y", payload={}, agent_name="a")
    r = repr(env)
    assert "EventEnvelope" in r
    assert "x.y" in r
    # Should NOT dump the entire trace_id.
    assert env.trace_id not in r
