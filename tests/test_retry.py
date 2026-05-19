"""Sprint 3 — retry primitives."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration.retry import RetryPolicy, RetryExhausted, retry_call, with_retry


@pytest.mark.smoke
def test_policy_validation():
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="almost certainly a bug"):
        RetryPolicy(max_attempts=21)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=-1)
    with pytest.raises(ValueError):
        RetryPolicy(jitter=2.0)


@pytest.mark.smoke
def test_delay_for_attempt_one_is_zero():
    p = RetryPolicy(max_attempts=5, base_delay=2.0, jitter=0)
    assert p.delay_for(1) == 0.0


@pytest.mark.smoke
def test_delay_for_caps_at_max_delay():
    p = RetryPolicy(max_attempts=20, base_delay=1.0, max_delay=5.0, backoff_multiplier=10.0, jitter=0)
    # attempt 4: 1 * 10^2 = 100, capped to 5
    assert p.delay_for(4) == 5.0


@pytest.mark.smoke
def test_is_retryable_with_no_categories_retries_any():
    p = RetryPolicy()
    assert p.is_retryable(None) is True
    assert p.is_retryable("external_api") is True
    assert p.is_retryable("anything") is True


@pytest.mark.smoke
def test_is_retryable_respects_whitelist():
    p = RetryPolicy(retryable_categories=frozenset({"external_api", "timeout"}))
    assert p.is_retryable("external_api") is True
    assert p.is_retryable("validation") is False
    assert p.is_retryable(None) is False  # unclassified = don't retry when list is set


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_retry_call_succeeds_on_first_try():
    p = RetryPolicy(max_attempts=3, base_delay=0.001, jitter=0)
    calls = 0
    async def fn():
        nonlocal calls
        calls += 1
        return "ok"
    result = await retry_call(p, fn)
    assert result == "ok"
    assert calls == 1


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_retry_call_retries_then_succeeds():
    p = RetryPolicy(max_attempts=3, base_delay=0.001, jitter=0)
    calls = 0
    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("transient")
        return "ok"
    result = await retry_call(p, fn)
    assert result == "ok"
    assert calls == 3


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_retry_call_raises_after_exhaustion():
    p = RetryPolicy(max_attempts=2, base_delay=0.001, jitter=0)
    async def fn():
        raise ConnectionError("perma")
    with pytest.raises(RetryExhausted) as exc_info:
        await retry_call(p, fn)
    assert exc_info.value.attempts == 2
    assert isinstance(exc_info.value.last_exc, ConnectionError)
    assert exc_info.value.__cause__ is exc_info.value.last_exc


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_retry_call_fails_fast_on_non_retryable_category():
    p = RetryPolicy(
        max_attempts=3,
        base_delay=0.001,
        jitter=0,
        retryable_categories=frozenset({"external_api"}),
    )
    calls = 0
    async def fn():
        nonlocal calls
        calls += 1
        raise ValueError("bad input")  # classified as "validation"

    def classify(e):
        if isinstance(e, ValueError):
            return "validation"
        return "external_api"

    with pytest.raises(ValueError):
        await retry_call(p, fn, classify=classify)
    assert calls == 1  # no retry


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_with_retry_decorator():
    p = RetryPolicy(max_attempts=2, base_delay=0.001, jitter=0)
    calls = 0

    @with_retry(p)
    async def fetch(x):
        nonlocal calls
        calls += 1
        if calls < 2:
            raise RuntimeError("retry me")
        return x * 2

    assert await fetch(5) == 10
    assert calls == 2
    # Decorator preserves wrapped fn for introspection.
    assert hasattr(fetch, "__wrapped__")


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_on_attempt_callback_called():
    p = RetryPolicy(max_attempts=3, base_delay=0.001, jitter=0)
    attempts = []
    async def fn():
        raise RuntimeError("nope")
    with pytest.raises(RetryExhausted):
        await retry_call(p, fn, on_attempt=lambda n, e: attempts.append((n, type(e).__name__)))
    assert attempts == [(1, "RuntimeError"), (2, "RuntimeError"), (3, "RuntimeError")]
