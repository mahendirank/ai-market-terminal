"""Sprint 3 — Critic foundation.

A Critic inspects an EventEnvelope and emits a CritiqueResult deciding
whether the event may propagate downstream. Critics are:

  - Idempotent: same input → same verdict.
  - Side-effect-free: read-only on the envelope.
  - Bounded latency: deterministic critics in <10ms; LLM-backed ones
    (Sprint 4+) carry a timeout enforced by the agent runtime.
  - Composable: ChainCritic runs N critics with halt-on-first-reject.

Sprint 3 ships:
  - BaseCritic ABC
  - CritiqueResult dataclass
  - SchemaCritic (validates payload shape via callable predicate)
  - ChainCritic (composition)

NO LLM critics in Sprint 3 (deferred to Sprint 4+ per spec).
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Callable

from orchestration.event_envelope import EventEnvelope


_log = logging.getLogger("agents.critic")


@dataclass(frozen=True)
class CritiqueResult:
    """Verdict on a single envelope.

    accepted        : True → event may propagate; False → block it.
    reason          : short machine-readable code (e.g. "schema_invalid",
                      "low_confidence", "duplicate"). Used for metrics
                      labels — keep cardinality low (<20 distinct values).
    confidence      : critic's own confidence in its verdict, 0.0–1.0.
                      Sprint 4+ uses this to decide between "halt now"
                      and "warn but allow" for low-confidence rejections.
    markers         : optional set of hallucination/risk markers that
                      were triggered (e.g. {"future_date", "impossible_price"}).
                      Empty set = no markers.
    detail          : free-form human-readable detail. Logged but not
                      used for routing.
    """

    accepted: bool
    reason: str
    confidence: float = 1.0
    markers: frozenset[str] = field(default_factory=frozenset)
    detail: str | None = None

    def __post_init__(self):
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")

    @classmethod
    def accept(cls, reason: str = "ok", confidence: float = 1.0) -> CritiqueResult:
        return cls(accepted=True, reason=reason, confidence=confidence)

    @classmethod
    def reject(
        cls,
        reason: str,
        *,
        confidence: float = 1.0,
        markers: frozenset[str] | set[str] = frozenset(),
        detail: str | None = None,
    ) -> CritiqueResult:
        return cls(
            accepted=False,
            reason=reason,
            confidence=confidence,
            markers=frozenset(markers),
            detail=detail,
        )


# ──────────────────────────────────────────────────────────────────────
# Abstract base
# ──────────────────────────────────────────────────────────────────────

class BaseCritic(abc.ABC):
    """Inherit and implement `evaluate`. Critic implementations must
    be safe to call concurrently from multiple asyncio tasks — keep
    instance state immutable or use locks."""

    name: str = "critic"

    @abc.abstractmethod
    async def evaluate(self, envelope: EventEnvelope) -> CritiqueResult:
        """Return a verdict. Must not mutate `envelope`."""


# ──────────────────────────────────────────────────────────────────────
# Built-in critics
# ──────────────────────────────────────────────────────────────────────

class SchemaCritic(BaseCritic):
    """Validates envelope.payload against a caller-supplied predicate.

    Example:
        def is_valid_signal(payload: dict) -> tuple[bool, str | None]:
            if "asset" not in payload:
                return False, "missing 'asset'"
            if payload.get("confidence", 0) < 50:
                return False, "low_confidence"
            return True, None

        critic = SchemaCritic(name="signal.schema", predicate=is_valid_signal)
    """

    def __init__(
        self,
        *,
        name: str,
        predicate: Callable[[dict], tuple[bool, str | None]],
    ):
        self.name = name
        self._predicate = predicate

    async def evaluate(self, envelope: EventEnvelope) -> CritiqueResult:
        try:
            ok, reason = self._predicate(envelope.payload)
        except Exception as e:
            _log.exception(
                "schema_critic_predicate_raised",
                extra={"critic": self.name, "error": repr(e)},
            )
            return CritiqueResult.reject(
                reason="critic_internal_error",
                confidence=0.0,
                detail=repr(e),
            )
        if ok:
            return CritiqueResult.accept(reason="schema_ok")
        return CritiqueResult.reject(
            reason=reason or "schema_invalid",
            detail=f"predicate failed for event_type={envelope.event_type!r}",
        )


class ChainCritic(BaseCritic):
    """Run multiple critics in order. First reject halts the chain.

    The chain accepts only if ALL critics accept. The first rejection
    determines the returned `reason`. Markers from all preceding
    accepted critics are union'd into the final result.
    """

    def __init__(self, *, name: str, critics: list[BaseCritic]):
        if not critics:
            raise ValueError("ChainCritic requires at least one critic")
        self.name = name
        self._critics = critics

    async def evaluate(self, envelope: EventEnvelope) -> CritiqueResult:
        acc_markers: set[str] = set()
        min_confidence = 1.0
        for c in self._critics:
            result = await c.evaluate(envelope)
            if not result.accepted:
                # Halt on first reject. Surface upstream markers too.
                return CritiqueResult.reject(
                    reason=result.reason,
                    confidence=min(min_confidence, result.confidence),
                    markers=acc_markers | set(result.markers),
                    detail=f"{c.name}: {result.detail}" if result.detail else c.name,
                )
            acc_markers |= set(result.markers)
            min_confidence = min(min_confidence, result.confidence)
        return CritiqueResult(
            accepted=True,
            reason="chain_ok",
            confidence=min_confidence,
            markers=frozenset(acc_markers),
        )


class AlwaysAcceptCritic(BaseCritic):
    """No-op critic. Useful as a default placeholder before real critics exist."""

    name = "always_accept"

    async def evaluate(self, envelope: EventEnvelope) -> CritiqueResult:
        return CritiqueResult.accept(reason="default_accept")
