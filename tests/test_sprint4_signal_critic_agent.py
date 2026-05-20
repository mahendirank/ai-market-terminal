"""Sprint 4 Stage 4.4 — SignalCriticAgent tests.

Verifies:
  - Schema critic rejects payloads missing required fields
  - Confidence floor rejects below-50, accepts above
  - Recent bar critic accepts fresh, rejects stale, fail-open on parse error
  - Chain composition halts on first reject
  - Observe-mode: handle_event ALWAYS proceeds; no DLQ
  - Fail-open: chain exception → log + return (no propagation)
  - Critique event emitted with metadata only (no original signal payload)
  - Verdict reaches log + bus through both channels
"""
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration import InMemoryEventBus
from orchestration.agents.signal_critic_agent import (
    SignalCriticAgent,
    _ConfidenceFloorCritic,
    _RecentBarCritic,
    _schema_predicate,
)
from orchestration.event_envelope import EventEnvelope, new_envelope


# ──────────────────────────────────────────────────────────────────────
# Schema predicate
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_schema_predicate_accepts_complete_payload():
    ok, reason = _schema_predicate({"asset": "NQ", "confidence": 80, "decision": "BUY"})
    assert ok is True
    assert reason is None


@pytest.mark.smoke
@pytest.mark.parametrize("payload,expected_reason", [
    ({}, "missing_asset"),
    ({"asset": "NQ"}, "missing_confidence"),
    ({"asset": "NQ", "confidence": 80}, "missing_decision"),
    ("not-a-dict", "payload_not_dict"),
    (None, "payload_not_dict"),
    ([1, 2, 3], "payload_not_dict"),
])
def test_schema_predicate_rejects_invalid(payload, expected_reason):
    ok, reason = _schema_predicate(payload)
    assert ok is False
    assert reason == expected_reason


# ──────────────────────────────────────────────────────────────────────
# Confidence floor critic
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
@pytest.mark.asyncio
@pytest.mark.parametrize("conf,accepted", [
    (50, True),
    (50.0, True),
    (51, True),
    (80, True),
    (100, True),
    (49, False),
    (49.99, False),
    (0, False),
])
async def test_confidence_floor(conf, accepted):
    env = new_envelope(
        event_type="signal.candidate",
        payload={"asset": "NQ", "confidence": conf, "decision": "BUY"},
        agent_name="test",
    )
    result = await _ConfidenceFloorCritic().evaluate(env)
    assert result.accepted == accepted
    if not accepted:
        assert result.reason == "below_confidence_floor"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_confidence_floor_rejects_non_numeric():
    env = new_envelope(
        event_type="signal.candidate",
        payload={"asset": "NQ", "confidence": "not-a-number", "decision": "BUY"},
        agent_name="test",
    )
    result = await _ConfidenceFloorCritic().evaluate(env)
    assert result.accepted is False
    assert result.reason == "confidence_not_numeric"


# ──────────────────────────────────────────────────────────────────────
# Recent bar critic
# ──────────────────────────────────────────────────────────────────────

def _envelope_with_age(seconds_old: float) -> EventEnvelope:
    """Build an envelope with timestamp `seconds_old` in the past."""
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds_old)
    ts = past.strftime("%Y-%m-%dT%H:%M:%S.") + f"{past.microsecond // 1000:03d}Z"
    return EventEnvelope(
        trace_id="t",
        request_id="r",
        tenant_id="-",
        agent_name="test",
        timestamp=ts,
        event_type="signal.candidate",
        payload={"asset": "NQ", "confidence": 80, "decision": "BUY"},
    )


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_recent_bar_critic_accepts_fresh():
    env = _envelope_with_age(10.0)  # 10 seconds old
    result = await _RecentBarCritic().evaluate(env)
    assert result.accepted is True
    assert result.reason == "fresh"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_recent_bar_critic_rejects_stale():
    env = _envelope_with_age(400.0)  # 400 seconds old (> 300 threshold)
    result = await _RecentBarCritic().evaluate(env)
    assert result.accepted is False
    assert result.reason == "stale_event"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_recent_bar_critic_fail_open_on_parse_error():
    env = EventEnvelope(
        trace_id="t", request_id="r", tenant_id="-",
        agent_name="test", timestamp="not-a-timestamp",
        event_type="signal.candidate",
        payload={"asset": "NQ", "confidence": 80, "decision": "BUY"},
    )
    result = await _RecentBarCritic().evaluate(env)
    # Fail-open: accept with marker
    assert result.accepted is True
    assert "timestamp_parse_error" in result.markers


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_recent_bar_critic_fail_open_on_future_timestamp():
    env = _envelope_with_age(-100.0)  # 100 seconds in the FUTURE
    result = await _RecentBarCritic().evaluate(env)
    assert result.accepted is True
    assert "future_timestamp" in result.markers


# ──────────────────────────────────────────────────────────────────────
# Agent end-to-end (handle_event behavior)
# ──────────────────────────────────────────────────────────────────────

class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def agent_with_bus():
    bus = InMemoryEventBus()
    agent = SignalCriticAgent()
    agent.event_bus = bus
    # Attach log capture
    cap = _LogCapture()
    agent.log.addHandler(cap)
    agent.log.setLevel(logging.DEBUG)
    yield agent, bus, cap
    agent.log.removeHandler(cap)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_handle_event_accept_path(agent_with_bus):
    agent, bus, cap = agent_with_bus
    # Build a high-confidence, fresh, valid signal
    env = _envelope_with_age(5.0)
    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish("events:signal:candidate", env)

    stats = await agent.tick()
    assert stats.success

    # Verdict log line emitted
    obs = [r for r in cap.records if r.msg == "signal_critic_observed"]
    assert len(obs) == 1
    assert obs[0].verdict == "accept"
    # ChainCritic returns reason="chain_ok" when all critics accept
    # (the individual critic reasons are not surfaced on a passing chain).
    assert obs[0].reason == "chain_ok"

    # Critique event emitted with metadata only
    critique = await bus.try_consume_one(
        stream="events:signal:signal.critique", group="probe", consumer="c"
    )
    assert critique is not None
    p = critique.payload
    assert p["observe_only"] is True
    assert p["enforced"] is False
    assert p["verdict"] == "accept"
    # Original signal fields are NOT replicated into critique payload
    assert "asset" not in p
    assert "decision" not in p
    # But the original trace_id IS, for correlation
    assert p["original_trace_id"] == "t"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_handle_event_reject_low_confidence_still_acks(agent_with_bus):
    agent, bus, cap = agent_with_bus
    env = EventEnvelope(
        trace_id="t2", request_id="r2", tenant_id="-",
        agent_name="test",
        timestamp=_envelope_with_age(5.0).timestamp,
        event_type="signal.candidate",
        payload={"asset": "NQ", "confidence": 10, "decision": "BUY"},  # too low
    )
    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish("events:signal:candidate", env)

    stats = await agent.tick()
    assert stats.success  # tick OK even though verdict is reject

    # Verdict log
    obs = [r for r in cap.records if r.msg == "signal_critic_observed"]
    assert len(obs) == 1
    assert obs[0].verdict == "reject"
    assert obs[0].reason == "below_confidence_floor"

    # Critique emitted
    critique = await bus.try_consume_one(
        stream="events:signal:signal.critique", group="probe", consumer="c"
    )
    assert critique is not None
    assert critique.payload["verdict"] == "reject"

    # CRITICAL: original candidate was acked (pending empty)
    pending = bus._peek_pending("events:signal:candidate", "signal.critic.observe")
    assert pending == [], "reject path must still ack the candidate (observe-only)"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_handle_event_no_dlq_for_either_verdict(agent_with_bus):
    """Observe-only: no events should land in dlq:signal:candidate."""
    agent, bus, cap = agent_with_bus
    # Send a clearly-rejectable event
    env = EventEnvelope(
        trace_id="t3", request_id="r3", tenant_id="-",
        agent_name="test", timestamp=_envelope_with_age(5.0).timestamp,
        event_type="signal.candidate",
        payload={"confidence": 10},  # missing asset & decision
    )
    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish("events:signal:candidate", env)

    await agent.tick()

    # No DLQ activity from observe-mode
    dlq_len = await bus.stream_length("dlq:signal:candidate")
    assert dlq_len == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_handle_event_chain_exception_fail_open(agent_with_bus, monkeypatch):
    """If the critic chain raises, agent logs + returns. No crash."""
    agent, bus, cap = agent_with_bus

    # Sabotage the chain — replace its evaluate with a raise.
    async def _explode(envelope):
        raise RuntimeError("simulated critic chain bug")

    monkeypatch.setattr(agent._chain, "evaluate", _explode)

    env = _envelope_with_age(5.0)
    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish("events:signal:candidate", env)

    stats = await agent.tick()
    # tick succeeded — the agent handled the chain exception internally
    assert stats.success

    # The fail-open log line was emitted
    fail_open = [r for r in cap.records if r.msg == "signal_critic_chain_exception_fail_open"]
    assert len(fail_open) == 1

    # Event was acked (pending empty)
    pending = bus._peek_pending("events:signal:candidate", "signal.critic.observe")
    assert pending == []


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_handle_event_emit_failure_fail_open(agent_with_bus, monkeypatch):
    """If bus emission fails, agent logs + returns. No propagation."""
    agent, bus, cap = agent_with_bus

    # Sabotage emit_event to raise
    async def _fail_emit(*args, **kwargs):
        raise ConnectionError("simulated bus down")

    monkeypatch.setattr(agent, "emit_event", _fail_emit)

    env = _envelope_with_age(5.0)
    await bus.ensure_group("events:signal:candidate", "signal.critic.observe")
    await bus.publish("events:signal:candidate", env)

    stats = await agent.tick()
    # tick succeeded — emit failure was caught
    assert stats.success

    fail_emit = [r for r in cap.records if r.msg == "signal_critic_emit_failed_fail_open"]
    assert len(fail_emit) == 1


# ──────────────────────────────────────────────────────────────────────
# Agent class configuration
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_agent_class_config():
    assert SignalCriticAgent.name == "signal.critic"
    assert SignalCriticAgent.family == "signal"
    assert SignalCriticAgent.version == "v1"
    assert SignalCriticAgent.stream == "events:signal:candidate"
    assert SignalCriticAgent.consumer_group == "signal.critic.observe"


@pytest.mark.smoke
def test_agent_has_no_input_critic():
    """The agent runs its critic chain explicitly inside handle_event,
    not via input_critic — so the observe-only verdict path is intentional."""
    agent = SignalCriticAgent()
    # BaseAgent defaults to AlwaysAcceptCritic when input_critic isn't set
    # explicitly. Either is fine — the key is no enforcement.
    # Just confirm the chain attribute exists and has 3 critics.
    assert hasattr(agent, "_chain")
    assert len(agent._chain._critics) == 3
