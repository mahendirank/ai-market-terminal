"""Sprint 3 — bounded retry layer.

NO infinite loops. Every retry path has a finite max_attempts cap.
NO sleeps inside the retry primitive — uses asyncio.sleep so the event
loop stays responsive.

Failure classification: the caller declares which ErrorCategory values
are retryable. An unclassified exception falls through immediately
(don't retry our bugs).

Usage:

    policy = RetryPolicy(max_attempts=3, base_delay=1.0)

    @with_retry(policy)
    async def fetch():
        ...

    # OR imperative:
    result = await retry_call(policy, fetch)
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar


_log = logging.getLogger("agents.retry")

T = TypeVar("T")


class RetryExhausted(Exception):
    """Raised when all retry attempts have been used up.

    The original exception is preserved as __cause__ so callers can
    `except RetryExhausted as e: e.__cause__` to inspect the root cause.
    """

    def __init__(self, attempts: int, last_exc: BaseException):
        super().__init__(f"retry exhausted after {attempts} attempt(s): {last_exc!r}")
        self.attempts = attempts
        self.last_exc = last_exc


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry config.

    max_attempts        : total tries including the first. 1 = no retry.
                          Hard cap of 20 enforced — anything higher is
                          almost certainly a logic bug.
    base_delay          : seconds between attempt 1 and attempt 2.
    max_delay           : ceiling for the exponential backoff.
    backoff_multiplier  : delay(n) = min(base_delay * multiplier^(n-1), max_delay).
    jitter              : 0.0 = no jitter, 1.0 = full jitter (delay * random).
                          Default 0.1 = ±10% noise to avoid thundering herd.
    retryable_categories: set of ErrorCategory strings. Empty set = retry
                          ANY exception. None = same as empty (any).
    """

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    backoff_multiplier: float = 2.0
    jitter: float = 0.1
    retryable_categories: frozenset[str] | None = None

    def __post_init__(self):
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.max_attempts > 20:
            raise ValueError("max_attempts > 20 is almost certainly a bug")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must be non-negative")
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError("jitter must be in [0.0, 1.0]")

    def delay_for(self, attempt: int) -> float:
        """Delay before `attempt` (1-based). delay_for(1) == 0."""
        if attempt <= 1:
            return 0.0
        raw = self.base_delay * (self.backoff_multiplier ** (attempt - 2))
        capped = min(raw, self.max_delay)
        if self.jitter > 0:
            noise = random.uniform(1.0 - self.jitter, 1.0 + self.jitter)
            return capped * noise
        return capped

    def is_retryable(self, error_category: str | None) -> bool:
        """Check if a category should be retried.

        retryable_categories is None or empty -> retry any exception
        (legacy / convenience default).
        """
        if not self.retryable_categories:
            return True
        if error_category is None:
            return False
        return error_category in self.retryable_categories


# ──────────────────────────────────────────────────────────────────────
# Async runner
# ──────────────────────────────────────────────────────────────────────

async def retry_call(
    policy: RetryPolicy,
    fn: Callable[[], Awaitable[T]],
    *,
    classify: Callable[[BaseException], str | None] | None = None,
    on_attempt: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Execute `fn` with retry. Returns its result or raises RetryExhausted.

    classify(exc) -> ErrorCategory string or None. If a category is
    returned and policy.retryable_categories doesn't include it, the
    exception is re-raised immediately (no further attempts).

    on_attempt(attempt_num, exception) is called after each failed
    attempt — useful for logging / metric counters without coupling
    this primitive to either.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        # Wait before attempts 2..N. attempt 1 is immediate.
        delay = policy.delay_for(attempt)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            return await fn()
        except BaseException as e:  # noqa: BLE001 — broad on purpose; we re-raise via RetryExhausted
            last_exc = e
            category = classify(e) if classify else None
            if on_attempt:
                try:
                    on_attempt(attempt, e)
                except Exception as cb_e:
                    _log.warning("on_attempt callback raised: %r", cb_e)

            if not policy.is_retryable(category):
                # Fail fast — caller said this category isn't retryable.
                raise

            if attempt == policy.max_attempts:
                break  # fall through to RetryExhausted

    assert last_exc is not None  # logically reachable only on failure
    raise RetryExhausted(policy.max_attempts, last_exc) from last_exc


# ──────────────────────────────────────────────────────────────────────
# Decorator form
# ──────────────────────────────────────────────────────────────────────

def with_retry(
    policy: RetryPolicy,
    *,
    classify: Callable[[BaseException], str | None] | None = None,
    on_attempt: Callable[[int, BaseException], None] | None = None,
):
    """Decorator factory: wraps an async callable with retry_call().

    Usage:
        @with_retry(RetryPolicy(max_attempts=3))
        async def fetch():
            ...
    """

    def deco(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def wrapper(*args, **kwargs) -> T:
            async def attempt() -> T:
                return await fn(*args, **kwargs)
            return await retry_call(
                policy, attempt,
                classify=classify,
                on_attempt=on_attempt,
            )
        wrapper.__name__ = getattr(fn, "__name__", "retried")
        wrapper.__qualname__ = getattr(fn, "__qualname__", wrapper.__name__)
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    return deco
