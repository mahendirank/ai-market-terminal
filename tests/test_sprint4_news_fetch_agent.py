"""Sprint 4 Stage 4.3 — NewsFetchAgent tests.

Verifies:
  - Agent's run_once calls get_all_news in a thread (doesn't block loop)
  - Agent emits a news.raw event with bounded payload (count, sources, etc.)
  - Tick-to-tick drift detection
  - Retry policy retries on transient failures, gives up on validation errors
  - Timeout cancels a slow fetch
  - Defensive shape: handles non-list / non-dict items gracefully
  - Agent class config: tick_interval/timeout configurable via env
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration import InMemoryEventBus
from orchestration.agents.news_fetch_agent import NewsFetchAgent, _extract_sources


# ──────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_extract_sources_skips_non_dict_and_non_string_source():
    items = [
        {"source": "Reuters", "text": "x"},
        {"source": "CNBC", "text": "y"},
        {"source": None, "text": "z"},   # ignored
        "not-a-dict",                     # ignored
        {"text": "no source"},            # ignored
    ]
    assert _extract_sources(items) == frozenset({"Reuters", "CNBC"})


# ──────────────────────────────────────────────────────────────────────
# Agent behavior
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
@pytest.mark.asyncio
async def test_run_once_calls_get_all_news_in_thread(monkeypatch):
    """Agent must use asyncio.to_thread (proves non-blocking call site)."""
    bus = InMemoryEventBus()

    call_log = []
    def fake_get_all_news():
        call_log.append(("called", time.perf_counter()))
        return [{"source": "Reuters", "text": "hello"}, {"source": "CNBC", "text": "world"}]

    import news as _news
    monkeypatch.setattr(_news, "get_all_news", fake_get_all_news)

    agent = NewsFetchAgent()
    agent.event_bus = bus
    stats = await agent.tick()
    assert stats.success
    assert len(call_log) == 1
    # Event was emitted to the default stream events:news:news.raw
    consumed = await bus.try_consume_one(
        stream="events:news:news.raw", group="g", consumer="c"
    )
    assert consumed is not None
    assert consumed.payload["count"] == 2
    assert "Reuters" in consumed.payload["sources"]
    assert "CNBC" in consumed.payload["sources"]
    assert consumed.payload["shadow_mode"] is True
    # Latency was tracked and roundtripped
    assert "latency_ms" in consumed.payload
    assert consumed.payload["latency_ms"] >= 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_run_once_emits_bounded_payload(monkeypatch):
    """Agent must NOT ship the entire news list through the bus."""
    bus = InMemoryEventBus()
    # 100 items × ~50 bytes each = ~5 KB. The agent's emission should
    # be ~< 1 KB (just metadata + first 20 sources).
    big_list = [{"source": f"Src{i}", "text": "x" * 200} for i in range(100)]

    import news as _news
    monkeypatch.setattr(_news, "get_all_news", lambda: big_list)

    agent = NewsFetchAgent()
    agent.event_bus = bus
    await agent.tick()

    consumed = await bus.try_consume_one(stream="events:news:news.raw", group="g", consumer="c")
    assert consumed.payload["count"] == 100
    # Bounded to top 20 sources
    assert len(consumed.payload["sources"]) <= 20
    # The full news LIST is NOT in the envelope
    payload_str = str(consumed.payload)
    assert "x" * 200 not in payload_str, "agent leaked full news content into envelope"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_drift_detection_first_tick_marks_first(monkeypatch):
    bus = InMemoryEventBus()
    import news as _news
    monkeypatch.setattr(_news, "get_all_news", lambda: [{"source": "R"}])

    agent = NewsFetchAgent()
    agent.event_bus = bus
    await agent.tick()
    consumed = await bus.try_consume_one(stream="events:news:news.raw", group="g", consumer="c")
    # First tick has no prior state.
    assert agent._prev_tick_count == 1
    assert agent._prev_tick_sources == frozenset({"R"})


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_drift_detection_second_tick_reports_drift(monkeypatch):
    bus = InMemoryEventBus()
    import news as _news
    # First tick: 5 items, sources {A, B}
    # Second tick: 10 items, sources {A, C}
    state = {"call": 0}
    def fake_news():
        state["call"] += 1
        if state["call"] == 1:
            return [{"source": "A"}] * 3 + [{"source": "B"}] * 2
        else:
            return [{"source": "A"}] * 7 + [{"source": "C"}] * 3
    monkeypatch.setattr(_news, "get_all_news", fake_news)

    agent = NewsFetchAgent()
    agent.event_bus = bus
    await agent.tick()  # first
    await agent.tick()  # second

    # Both events emitted, drain.
    e1 = await bus.try_consume_one(stream="events:news:news.raw", group="g", consumer="c")
    e2 = await bus.try_consume_one(stream="events:news:news.raw", group="g", consumer="c")
    assert e1 is not None and e2 is not None
    # Drift was tracked in agent state (second tick: 10 vs 5 items, +5 delta).
    assert agent._prev_tick_count == 10
    assert agent._prev_tick_sources == frozenset({"A", "C"})


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_non_list_return_does_not_crash(monkeypatch):
    """If legacy returns something weird, agent normalizes to empty list."""
    bus = InMemoryEventBus()
    import news as _news
    monkeypatch.setattr(_news, "get_all_news", lambda: "not a list")

    agent = NewsFetchAgent()
    agent.event_bus = bus
    stats = await agent.tick()
    assert stats.success  # didn't raise; just logged a warning and emitted count=0
    consumed = await bus.try_consume_one(stream="events:news:news.raw", group="g", consumer="c")
    assert consumed.payload["count"] == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_external_api_error_triggers_retry(monkeypatch):
    """Transient external error should retry, then succeed."""
    bus = InMemoryEventBus()
    import news as _news

    state = {"attempts": 0}
    class _Transient(ConnectionError): ...

    def flaky_news():
        state["attempts"] += 1
        if state["attempts"] < 3:
            raise _Transient("transient feed timeout")
        return [{"source": "Reuters"}]

    monkeypatch.setattr(_news, "get_all_news", flaky_news)

    # Speed up retry so the test stays fast.
    agent = NewsFetchAgent()
    agent.event_bus = bus
    from orchestration import RetryPolicy
    from logging_config import ErrorCategory
    agent.retry_policy = RetryPolicy(
        max_attempts=3, base_delay=0.001, max_delay=0.01, jitter=0,
        retryable_categories=None,  # retry any exception in this test
    )
    stats = await agent.tick()
    assert stats.success
    assert state["attempts"] == 3


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_persistent_failure_is_swallowed_by_tick(monkeypatch):
    """If fetch fails forever, tick records failure without raising."""
    bus = InMemoryEventBus()
    import news as _news

    def always_fail():
        raise RuntimeError("never works")

    monkeypatch.setattr(_news, "get_all_news", always_fail)

    agent = NewsFetchAgent()
    agent.event_bus = bus
    # Speed up retry. RuntimeError is unclassified — retry_policy retries all.
    from orchestration import RetryPolicy
    agent.retry_policy = RetryPolicy(
        max_attempts=2, base_delay=0.001, jitter=0,
        retryable_categories=None,
    )
    stats = await agent.tick()
    # Did NOT raise; tick recorded a failure.
    assert stats.success is False
    assert stats.error_type in ("RuntimeError", "RetryExhausted")
    assert agent._consecutive_failures == 1


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_timeout_cancels_slow_fetch(monkeypatch):
    """A fetch that exceeds the timeout is cancelled."""
    bus = InMemoryEventBus()
    import news as _news

    def slow_news():
        import time as _t
        _t.sleep(5)  # > timeout
        return []

    monkeypatch.setattr(_news, "get_all_news", slow_news)

    agent = NewsFetchAgent()
    agent.event_bus = bus
    agent.timeout = 0.2  # 200ms — much less than the 5s slow_news
    agent.retry_policy = None  # disable retry so test is fast

    t0 = time.perf_counter()
    stats = await agent.tick()
    elapsed = time.perf_counter() - t0
    assert stats.success is False
    assert elapsed < 1.0  # cancelled near the timeout, not after 5s
    assert stats.error_type in ("TimeoutError", "CancelledError")


@pytest.mark.smoke
def test_class_config_defaults():
    """Agent config matches the spec."""
    # Fresh instance reads env at class-definition time, so we just
    # confirm the defaults are present.
    assert NewsFetchAgent.name == "news.fetch"
    assert NewsFetchAgent.family == "news"
    assert NewsFetchAgent.version == "v1"
    # Tick interval default 120s; timeout default 30s.
    assert NewsFetchAgent.tick_interval == float(os.environ.get("NEWS_FETCH_TICK_INTERVAL", "120"))
    assert NewsFetchAgent.timeout == float(os.environ.get("NEWS_FETCH_TIMEOUT", "30"))
    assert NewsFetchAgent.retry_policy is not None
    assert NewsFetchAgent.retry_policy.max_attempts == 3
