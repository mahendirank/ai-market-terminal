"""Sprint 3 — EventBus tests. InMemoryEventBus only; Redis impl tested
in integration when a live container is available (deferred to Sprint 4)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration.event_bus import InMemoryEventBus, dlq_stream_name, stream_name
from orchestration.event_envelope import new_envelope


@pytest.mark.smoke
def test_stream_name_format():
    assert stream_name("news", "fetched") == "events:news:fetched"


@pytest.mark.smoke
def test_dlq_stream_name_strips_events_prefix():
    assert dlq_stream_name("events:news:fetched") == "dlq:news:fetched"
    # No 'events:' prefix → still produces a dlq: stream
    assert dlq_stream_name("custom:stream") == "dlq:custom:stream"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_publish_and_consume_roundtrip():
    bus = InMemoryEventBus()
    env = new_envelope(event_type="news.fetched", payload={"h": ["x"]}, agent_name="news.fetch")
    await bus.ensure_group("events:news:fetched", "default")
    msg_id = await bus.publish("events:news:fetched", env)
    assert msg_id.startswith("0-")

    consumed = await bus.try_consume_one(stream="events:news:fetched", group="default", consumer="c1")
    assert consumed is not None
    assert consumed.event_type == "news.fetched"
    # The bus stashes msg_id as a non-dataclass attribute (not in payload).
    assert hasattr(consumed, "_bus_msg_id")
    # Original payload preserved and not polluted.
    assert consumed.payload == {"h": ["x"]}


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_try_consume_one_returns_none_when_empty():
    bus = InMemoryEventBus()
    await bus.ensure_group("events:x:y", "g")
    result = await bus.try_consume_one(stream="events:x:y", group="g", consumer="c")
    assert result is None


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_two_consumers_dont_get_same_event():
    bus = InMemoryEventBus()
    env = new_envelope(event_type="x.y", payload={"v": 1}, agent_name="a")
    await bus.publish("events:x:y", env)
    first = await bus.try_consume_one(stream="events:x:y", group="g", consumer="c1")
    second = await bus.try_consume_one(stream="events:x:y", group="g", consumer="c2")
    assert first is not None
    assert second is None  # the single event was taken by c1


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_ack_removes_from_pending():
    bus = InMemoryEventBus()
    env = new_envelope(event_type="x.y", payload={"v": 1}, agent_name="a")
    await bus.publish("events:x:y", env)
    consumed = await bus.try_consume_one(stream="events:x:y", group="g", consumer="c")
    assert consumed is not None
    assert len(bus._peek_pending("events:x:y", "g")) == 1
    await bus.ack("events:x:y", "g", consumed)
    assert len(bus._peek_pending("events:x:y", "g")) == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_publish_to_dlq_tags_envelope():
    bus = InMemoryEventBus()
    env = new_envelope(event_type="x.y", payload={"v": 1}, agent_name="a")
    await bus.publish_to_dlq(
        original_stream="events:x:y",
        envelope=env,
        reason="retry_exhausted",
    )
    dlq_env = await bus.try_consume_one(stream="dlq:x:y", group="audit", consumer="c")
    assert dlq_env is not None
    assert dlq_env.payload["_dlq_reason"] == "retry_exhausted"
    assert dlq_env.payload["_dlq_original_stream"] == "events:x:y"
    assert dlq_env.payload["v"] == 1  # original payload preserved


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_stream_length_grows_and_shrinks():
    bus = InMemoryEventBus()
    assert await bus.stream_length("events:x:y") == 0
    env = new_envelope(event_type="x.y", payload={}, agent_name="a")
    await bus.publish("events:x:y", env)
    await bus.publish("events:x:y", env)
    assert await bus.stream_length("events:x:y") == 2
    await bus.try_consume_one(stream="events:x:y", group="g", consumer="c")
    # length tracks "available", not "pending ack" — InMemory pops on consume
    assert await bus.stream_length("events:x:y") == 1


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_backpressure_drops_oldest_when_capped():
    bus = InMemoryEventBus(max_len=3)
    for i in range(5):
        env = new_envelope(event_type="x.y", payload={"i": i}, agent_name="a")
        await bus.publish("events:x:y", env)
    # Only the most recent 3 survive.
    assert await bus.stream_length("events:x:y") == 3
    survivors = []
    while (e := await bus.try_consume_one(stream="events:x:y", group="g", consumer="c")):
        survivors.append(e.payload["i"])
    assert survivors == [2, 3, 4]
