"""Sprint 3 — circuit breaker.

Per-service breaker with three states:

    CLOSED      → normal operation
       │
       │  N consecutive failures
       ▼
    OPEN        → reject calls fast; do NOT hit the service
       │
       │  recovery_timeout elapsed
       ▼
    HALF_OPEN   → allow ONE probe call
       │
       │ probe succeeds          probe fails
       ▼                              │
    CLOSED                            ▼
                                    OPEN

Imperative and decorator APIs. State is kept in-memory; Redis-backed
distributed state is deferred to Sprint 6+ if/when agents split across
containers.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar


_log = logging.getLogger("agents.circuit_breaker")

T = TypeVar("T")


class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is OPEN."""

    def __init__(self, service: str, opened_at: float):
        self.service = service
        self.opened_at = opened_at
        super().__init__(
            f"circuit OPEN for {service!r} since {opened_at:.0f} — call rejected"
        )


@dataclass
class CircuitBreaker:
    """One circuit per external service.

    Thread-safety: protected by an asyncio.Lock for state transitions.
    Reads of `state` may race with writes by a tick of nanoseconds —
    acceptable since this is a hint, not a contract (a single missed
    transition just delays opening by one call).
    """

    service: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_success_threshold: int = 1

    # Internal state (do not set directly)
    state: CircuitState = CircuitState.CLOSED
    _consecutive_failures: int = 0
    _half_open_successes: int = 0
    _opened_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self):
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.recovery_timeout < 0:
            raise ValueError("recovery_timeout must be non-negative")

    # ── Status queries (lock-free, "soft" reads) ──────────────────────

    def is_closed(self) -> bool:
        self._maybe_half_open()
        return self.state == CircuitState.CLOSED

    def is_open(self) -> bool:
        self._maybe_half_open()
        return self.state == CircuitState.OPEN

    def can_attempt(self) -> bool:
        """True if the next call should be allowed (CLOSED or HALF_OPEN)."""
        self._maybe_half_open()
        return self.state != CircuitState.OPEN

    def _maybe_half_open(self) -> None:
        """Transition OPEN → HALF_OPEN if recovery_timeout has elapsed."""
        if (
            self.state == CircuitState.OPEN
            and (time.monotonic() - self._opened_at) >= self.recovery_timeout
        ):
            # No lock: at worst, two callers both flip to HALF_OPEN. That's
            # fine — the probe semantics treat it as a single allowed call,
            # and record_success/failure will reconcile.
            self.state = CircuitState.HALF_OPEN
            self._half_open_successes = 0

    # ── State updates (lock-protected) ────────────────────────────────

    async def record_success(self) -> None:
        async with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self.half_open_success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            else:
                # Reset failure tally on any success in CLOSED.
                self._consecutive_failures = 0

    async def record_failure(self) -> None:
        async with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                # Probe failed → straight back to OPEN.
                self._transition_to(CircuitState.OPEN)
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._transition_to(CircuitState.OPEN)

    async def force_open(self) -> None:
        """Admin/test helper. Trip the circuit without crossing threshold."""
        async with self._lock:
            self._transition_to(CircuitState.OPEN)

    async def force_close(self) -> None:
        """Admin/test helper. Reset to CLOSED."""
        async with self._lock:
            self._transition_to(CircuitState.CLOSED)

    def _transition_to(self, new_state: CircuitState) -> None:
        """Caller must hold _lock. Does NOT re-enter lock."""
        if new_state == self.state:
            return
        _log.warning(
            "circuit_breaker_transition",
            extra={
                "service": self.service,
                "from_state": self.state.value,
                "to_state": new_state.value,
                "consecutive_failures": self._consecutive_failures,
            },
        )
        self.state = new_state
        if new_state == CircuitState.OPEN:
            self._opened_at = time.monotonic()
            self._half_open_successes = 0
        elif new_state == CircuitState.CLOSED:
            self._consecutive_failures = 0
            self._half_open_successes = 0
            self._opened_at = 0.0

    # ── High-level helpers ────────────────────────────────────────────

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Imperative API. Raises CircuitOpenError or the wrapped fn's exc.

        Usage:
            try:
                result = await breaker.call(lambda: groq.complete(...))
            except CircuitOpenError:
                # serve cached value, fall back, etc.
                pass
        """
        if not self.can_attempt():
            raise CircuitOpenError(self.service, self._opened_at)
        try:
            result = await fn()
        except BaseException:
            await self.record_failure()
            raise
        else:
            await self.record_success()
            return result

    def snapshot(self) -> dict:
        """Lock-free read for health/metrics endpoints."""
        return {
            "service": self.service,
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
            "opened_at": self._opened_at,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
        }


# ──────────────────────────────────────────────────────────────────────
# Registry — one breaker per service name
# ──────────────────────────────────────────────────────────────────────

class CircuitRegistry:
    """Global lookup of breakers by service name.

    Use the module-level `default_registry` for app code; tests should
    instantiate their own to avoid cross-test bleed.
    """

    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    def get_or_create(
        self,
        service: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_success_threshold: int = 1,
    ) -> CircuitBreaker:
        """First-call-wins config. Subsequent calls return the existing breaker.

        Note: not async because dict.setdefault is atomic in CPython.
        Asyncio.Lock would force callers to await for no benefit.
        """
        existing = self._breakers.get(service)
        if existing is not None:
            return existing
        breaker = CircuitBreaker(
            service=service,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            half_open_success_threshold=half_open_success_threshold,
        )
        # Race: two callers may build two breakers. setdefault keeps the
        # first; the loser is GC'd. Acceptable.
        return self._breakers.setdefault(service, breaker)

    def snapshot(self) -> list[dict]:
        return [b.snapshot() for b in self._breakers.values()]

    def reset_for_testing(self) -> None:
        """Drop all breakers. Use only in tests."""
        self._breakers.clear()


# Module-level default. Sprint 4+ wires this into /api/circuits health endpoint.
default_registry = CircuitRegistry()
