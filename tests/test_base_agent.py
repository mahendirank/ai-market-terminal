"""Sprint 3 — BaseAgent / TickAgent / StreamAgent tests."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration.base_agent import StreamAgent, TickAgent
from orchestration.critic import CritiqueResult, SchemaCritic
from orchestration.event_bus import InMemoryEventBus
from orchestration.event_envelope import new_envelope
from orchestration.retry import RetryPolicy


class _CounterAgent(TickAgent):
    name = "test.counter"
    family = "test"
    tick_interval = 0.0

    def __init__(self, *, fail_n=0, event_bus=None):
        self.fail_n = fail_n
        self.call_count = 0
        self.event_bus = event_bus
        super().__init__()

    async def run_once(self):
        self.call_count += 1
        if self.call_count <= self.fail_n:
            raise RuntimeError(f"planned failure #{self.call_count}")


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_tick_records_success():
    agent = _CounterAgent()
    stats = await agent.tick()
    assert stats.success
    assert agent._total_ticks == 1
    assert agent._consecutive_failures == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_tick_records_failure_and_swallows():
    agent = _CounterAgent(fail_n=999)
    stats = await agent.tick()  # should NOT raise
    assert not stats.success
    assert stats.error_type == "RuntimeError"
    assert agent._consecutive_failures == 1


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_tick_consecutive_failure_counter():
    agent = _CounterAgent(fail_n=999)
    for _ in range(3):
        await agent.tick()
    assert agent._consecutive_failures == 3
    assert agent._total_ticks == 3
    assert agent._total_failures == 3


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_tick_retry_policy_recovers():
    agent = _CounterAgent(fail_n=2)
    agent.retry_policy = RetryPolicy(max_attempts=3, base_delay=0.001, jitter=0)
    stats = await agent.tick()
    assert stats.success  # retry got us past the failures
    assert agent.call_count == 3  # 2 fails + 1 success
    assert agent._consecutive_failures == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_tick_timeout_classified_as_failure():
    class _SlowAgent(TickAgent):
        name = "test.slow"
        family = "test"
        timeout = 0.05

        async def run_once(self):
            await asyncio.sleep(1)  # exceeds timeout

    agent = _SlowAgent()
    stats = await agent.tick()
    assert not stats.success
    assert stats.error_type in ("TimeoutError", "CancelledError")


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_tick_emits_event_via_bus():
    bus = InMemoryEventBus()

    class _EmitAgent(TickAgent):
        name = "test.emit"
        family = "test"

        async def run_once(self):
            await self.emit_event(event_type="test.emitted", payload={"value": 42})

    agent = _EmitAgent()
    agent.event_bus = bus
    stats = await agent.tick()
    assert stats.success
    assert stats.events_emitted == 1
    # Event landed on the default stream.
    consumed = await bus.try_consume_one(
        stream="events:test:test.emitted", group="g", consumer="c"
    )
    assert consumed is not None
    assert consumed.payload["value"] == 42


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_health_snapshot_shape():
    agent = _CounterAgent()
    await agent.tick()
    h = agent.health()
    assert h["name"] == "test.counter"
    assert h["family"] == "test"
    assert h["total_ticks"] == 1
    assert h["last_tick_success"] is True


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_stream_agent_dispatches_to_handle_event():
    bus = InMemoryEventBus()
    received = []

    class _MyStreamAgent(StreamAgent):
        name = "test.consumer"
        family = "test"
        stream = "events:test:emitted"
        consumer_group = "default"

        async def handle_event(self, envelope):
            received.append(envelope.payload)

    await bus.ensure_group("events:test:emitted", "default")
    env = new_envelope(event_type="test.emitted", payload={"v": "hello"}, agent_name="prod")
    await bus.publish("events:test:emitted", env)

    consumer = _MyStreamAgent()
    consumer.event_bus = bus
    stats = await consumer.tick()
    assert stats.success
    assert received == [{"v": "hello"}]


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_stream_agent_critic_reject_acks_without_calling_handler():
    bus = InMemoryEventBus()
    handler_calls = 0

    def predicate(payload):
        return False, "always_reject"

    class _PickyAgent(StreamAgent):
        name = "test.picky"
        family = "test"
        stream = "events:test:emitted"
        consumer_group = "g"

        async def handle_event(self, envelope):
            nonlocal handler_calls
            handler_calls += 1

    await bus.ensure_group("events:test:emitted", "g")
    env = new_envelope(event_type="test.emitted", payload={}, agent_name="prod")
    await bus.publish("events:test:emitted", env)

    agent = _PickyAgent()
    agent.event_bus = bus
    agent.input_critic = SchemaCritic(name="t", predicate=predicate)
    await agent.tick()
    assert handler_calls == 0
    # Event was acked (else pending would still have it).
    assert bus._peek_pending("events:test:emitted", "g") == []
