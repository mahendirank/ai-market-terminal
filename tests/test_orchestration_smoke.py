"""Sprint 3 — end-to-end orchestration smoke test.

Wires an emitter agent → InMemoryEventBus → consumer agent (with critic)
→ ack. Verifies the full data path before Sprint 4 attaches it to FastAPI.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration.base_agent import StreamAgent, TickAgent
from orchestration.critic import SchemaCritic
from orchestration.event_bus import InMemoryEventBus
from orchestration.orchestrator import Orchestrator


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_end_to_end_flow():
    """Producer emits → bus delivers → consumer with critic processes."""
    bus = InMemoryEventBus()
    received: list[dict] = []
    rejected: list[str] = []

    class ProducerAgent(TickAgent):
        name = "producer"
        family = "smoke"
        tick_interval = 0.0
        _counter = 0

        async def run_once(self):
            self._counter += 1
            # Half the events are "good", half are "bad" (no 'asset' key).
            if self._counter % 2 == 0:
                await self.emit_event(
                    event_type="signal.candidate",
                    payload={"asset": "NQ", "conf": 80, "n": self._counter},
                )
            else:
                await self.emit_event(
                    event_type="signal.candidate",
                    payload={"conf": 80, "n": self._counter},  # missing asset
                )

    class ConsumerAgent(StreamAgent):
        name = "consumer"
        family = "smoke"
        stream = "events:smoke:signal.candidate"
        consumer_group = "smoke_group"

        async def handle_event(self, envelope):
            received.append(envelope.payload)

        async def handle_failure(self, exc, *, stats):
            # Don't log noise during the test.
            pass

    def signal_predicate(payload):
        if "asset" not in payload:
            return False, "missing_asset"
        return True, None

    # Wire up
    await bus.ensure_group("events:smoke:signal.candidate", "smoke_group")
    producer = ProducerAgent()
    producer.event_bus = bus
    consumer = ConsumerAgent()
    consumer.event_bus = bus
    consumer.input_critic = SchemaCritic(name="signal.schema", predicate=signal_predicate)

    orch = Orchestrator()
    orch.register(producer)
    orch.register(consumer)

    # Tick producer 4 times → 2 good + 2 bad events.
    for _ in range(4):
        await orch.tick_agent("producer")
    assert await bus.stream_length("events:smoke:signal.candidate") == 4

    # Tick consumer 4 times. Critic should reject the 2 bad ones.
    for _ in range(4):
        await orch.tick_agent("consumer")

    # Only the 2 with 'asset' should have reached the handler.
    assert len(received) == 2
    assert all("asset" in r for r in received)
    # Stream is drained.
    assert await bus.stream_length("events:smoke:signal.candidate") == 0
    # Pending acks all cleared.
    assert bus._peek_pending("events:smoke:signal.candidate", "smoke_group") == []


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_health_reflects_real_activity():
    bus = InMemoryEventBus()

    class TestAgent(TickAgent):
        name = "telemetry_test"
        family = "smoke"
        tick_interval = 0.0

        async def run_once(self):
            await self.emit_event(event_type="x.y", payload={"k": 1})

    orch = Orchestrator()
    a = TestAgent()
    a.event_bus = bus
    orch.register(a)

    # Tick 3 times manually.
    for _ in range(3):
        await orch.tick_agent("telemetry_test")

    health = orch.health()[0]
    assert health.total_ticks == 3
    assert health.total_failures == 0
    assert health.last_tick_success is True
    snap = health.to_dict()
    assert snap["status"] == "registered"
    assert snap["last_tick_age_s"] is not None and snap["last_tick_age_s"] >= 0
