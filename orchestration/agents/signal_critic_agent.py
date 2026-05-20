"""Sprint 4 Stage 4.4 — SignalCriticAgent (OBSERVE-ONLY mode).

Consumes `events:signal:candidate` and runs a deterministic critic
chain (Schema + ConfidenceFloor + RecentBar). Emits per-event:

  1. A structured log line: `signal_critic_observed` with verdict
  2. A `signal.critique` event to the bus — METADATA ONLY (verdict,
     reason, original trace_id; does NOT replicate the signal payload)

CRITICAL: this is OBSERVE-ONLY. The agent does **NOT**:
  - Reject / DLQ candidate events
  - Block downstream consumers of `events:signal:candidate`
  - Modify the signal itself
  - Change routing
  - Trigger trade execution

The original `signal:candidate` event is ACKed regardless of verdict.
The agent's only output is observability — fitting for Sprint 4.4's
"observe-mode" gate.

Sprint 4.4 has NO producer for `events:signal:candidate` yet. The
agent ticks but processes nothing. This commit ships the topology +
verdict surface so Sprint 5+ can flip enforcement on without further
infrastructure work.

Fail-open guarantee:
  - If the critic chain raises, the agent logs `signal_critic_chain_exception_fail_open`
    and returns. The event is still ACKed (no retry storm).
  - If the verdict emission to the bus fails, the agent logs and proceeds.
  - In NO case does the critic's failure propagate to the original signal flow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orchestration import (
    BaseCritic,
    ChainCritic,
    CritiqueResult,
    SchemaCritic,
    StreamAgent,
)
from orchestration.event_envelope import EventEnvelope


# ──────────────────────────────────────────────────────────────────────
# Critic chain components
# ──────────────────────────────────────────────────────────────────────

def _schema_predicate(payload: dict) -> tuple[bool, str | None]:
    """Validates a signal.candidate payload's required fields.

    Returns (ok, reason_if_rejected). Reasons keep cardinality low
    (suitable for metrics labels in Sprint 5+).
    """
    if not isinstance(payload, dict):
        return False, "payload_not_dict"
    if "asset" not in payload:
        return False, "missing_asset"
    if "confidence" not in payload:
        return False, "missing_confidence"
    if "decision" not in payload:
        return False, "missing_decision"
    return True, None


class _ConfidenceFloorCritic(BaseCritic):
    """Reject signals with confidence below the floor. Default 50."""

    name = "signal.confidence_floor"
    FLOOR: float = 50.0

    async def evaluate(self, envelope: EventEnvelope) -> CritiqueResult:
        c = envelope.payload.get("confidence")
        if not isinstance(c, (int, float)):
            # Schema critic SHOULD have caught this — defensive.
            return CritiqueResult.reject(
                reason="confidence_not_numeric",
                confidence=1.0,
                detail=f"got type={type(c).__name__}",
            )
        if c >= self.FLOOR:
            return CritiqueResult.accept(
                reason="above_floor",
                confidence=1.0,
            )
        return CritiqueResult.reject(
            reason="below_confidence_floor",
            confidence=1.0,
            detail=f"got {c}, need >= {self.FLOOR}",
        )


class _RecentBarCritic(BaseCritic):
    """Reject signals based on stale envelope timestamps.

    Default: envelope.timestamp must be within the last 300s.

    Fail-open: if timestamp parsing fails, accept with a marker — we
    never block on our own bug.
    """

    name = "signal.recent_bar"
    STALE_THRESHOLD_S: float = 300.0

    async def evaluate(self, envelope: EventEnvelope) -> CritiqueResult:
        try:
            ts_str = envelope.timestamp
            # Strip trailing Z if present (ISO 8601 UTC)
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1]
            event_dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        except Exception as e:
            # Fail-open: a parse error is OUR bug, not the signal's. Accept.
            # Use the full constructor — .accept() doesn't carry markers/detail.
            return CritiqueResult(
                accepted=True,
                reason="timestamp_parse_failed_fail_open",
                confidence=0.5,
                markers=frozenset({"timestamp_parse_error"}),
                detail=f"{type(e).__name__}: {e}",
            )

        age = (datetime.now(timezone.utc) - event_dt).total_seconds()
        if age < 0:
            # Future-dated envelope. Suspicious, but fail-open.
            return CritiqueResult(
                accepted=True,
                reason="future_timestamp_fail_open",
                confidence=0.5,
                markers=frozenset({"future_timestamp"}),
                detail=f"age={age:.1f}s (negative)",
            )
        if age > self.STALE_THRESHOLD_S:
            return CritiqueResult.reject(
                reason="stale_event",
                confidence=1.0,
                detail=f"age={age:.1f}s > threshold={self.STALE_THRESHOLD_S}s",
            )
        return CritiqueResult.accept(
            reason="fresh",
            confidence=1.0,
        )


# ──────────────────────────────────────────────────────────────────────
# The agent
# ──────────────────────────────────────────────────────────────────────

class SignalCriticAgent(StreamAgent):
    """Observe-only critic. Consumes signal candidates; logs verdicts
    and emits critique events; NEVER blocks the original signal."""

    name = "signal.critic"
    family = "signal"
    version = "v1"

    stream = "events:signal:candidate"
    consumer_group = "signal.critic.observe"

    # NOTE: no input_critic. The whole point of this agent is to RUN
    # the critic logic inside handle_event so the observe-only path
    # is explicit (and the verdict goes to both log + bus).

    def __init__(self):
        super().__init__()
        self._chain = ChainCritic(
            name="signal.observe_chain",
            critics=[
                SchemaCritic(name="signal.schema", predicate=_schema_predicate),
                _ConfidenceFloorCritic(),
                _RecentBarCritic(),
            ],
        )

    async def handle_event(self, envelope: EventEnvelope) -> None:
        """Evaluate the candidate; log + emit critique; ALWAYS proceed.

        Fail-open across two failure layers:
          1. Critic chain raises → log + return (event still acked).
          2. Critique emission fails → log + return.
        """
        # Layer 1: critic evaluation. Fail-open if it raises.
        try:
            verdict = await self._chain.evaluate(envelope)
        except Exception as e:
            self.log.exception(
                "signal_critic_chain_exception_fail_open",
                extra={
                    "trace_id": envelope.trace_id,
                    "request_id": envelope.request_id,
                    "exc_type": type(e).__name__,
                },
            )
            return  # ack happens in StreamAgent.run_once finally

        # Build the verdict payload — metadata only, no signal content.
        verdict_payload = {
            "original_event_type": envelope.event_type,
            "original_trace_id": envelope.trace_id,
            "original_request_id": envelope.request_id,
            "verdict": "accept" if verdict.accepted else "reject",
            "reason": verdict.reason,
            "confidence": verdict.confidence,
            "markers": sorted(verdict.markers),
            "detail": verdict.detail,
            "observe_only": True,
            "enforced": False,
        }

        # Layer A: structured log line
        self.log.info(
            "signal_critic_observed",
            extra={
                **verdict_payload,
                "asset": _safe_get(envelope.payload, "asset"),
                "envelope_confidence": _safe_get(envelope.payload, "confidence"),
                "envelope_decision": _safe_get(envelope.payload, "decision"),
            },
        )

        # Layer B: critique event on a separate stream. Fail-open: if
        # the bus is unavailable, log + return — never propagate.
        try:
            await self.emit_event(
                event_type="signal.critique",
                payload=verdict_payload,
            )
        except Exception:
            self.log.exception(
                "signal_critic_emit_failed_fail_open",
                extra={
                    "trace_id": envelope.trace_id,
                    "request_id": envelope.request_id,
                },
            )
            return

        # OBSERVE MODE: no DLQ, no halt. The original event will be
        # acked by StreamAgent.run_once regardless of verdict.


def _safe_get(d, key, default=None):
    """Defensive payload getter — payload might not be a dict."""
    if isinstance(d, dict):
        return d.get(key, default)
    return default
