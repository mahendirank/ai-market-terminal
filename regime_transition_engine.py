"""
regime_transition_engine.py — Deterministic regime transition scoring.

Where regime_engine answers "what regime are we in?", this module answers
"is that regime CHANGING, and toward what?".

It scores the market state against five regime signatures:

  RISK_ON              risk assets bid, vol low, liquidity ample
  RISK_OFF             flight to safety, vol up, gold/USD bid
  PANIC                disorderly de-risking, vol spike, liquidity collapse
  LIQUIDITY_EXPANSION  easing backdrop lifting everything risk
  TIGHTENING           rising yields/USD draining liquidity

The transition signal is the gap between two scorings:
  - the raw OBSERVED snapshot  → the regime today
  - the event_graph PRESSURES  → the regime the causal mechanics imply

When those two disagree, the regime is in flux. That gap becomes a
0-1 `transition_score` and its inverse a `stability` score that
confidence_engine uses to discount conviction during regime change.

Design constraints (per spec):
  - Lightweight + deterministic: pure Python, five fixed signatures,
    a couple of dot products. No numpy, no I/O, no LLM, no agents.
  - No recursive loops: scoring is a flat pass over fixed dicts.
  - VPS-friendly: a full compute_transition() is a few dozen float ops.
"""
from __future__ import annotations

from typing import Optional


# ─── Regime signatures ───────────────────────────────────────────────────────
# Each signature is the canonical node-state vector for that regime, in
# [-1, +1]. Scoring is done only over the keys a signature actually defines.
RISK_ON             = "RISK_ON"
RISK_OFF            = "RISK_OFF"
PANIC               = "PANIC"
LIQUIDITY_EXPANSION = "LIQUIDITY_EXPANSION"
TIGHTENING          = "TIGHTENING"
# Reported when no regime signature fits strongly enough to assert one.
INDETERMINATE       = "INDETERMINATE"

REGIME_SIGNATURES: dict[str, dict[str, float]] = {
    RISK_ON: {
        "equities": +0.6, "volatility": -0.5, "liquidity": +0.4,
        "gold": -0.2, "dxy": -0.2, "yields": +0.1,
    },
    RISK_OFF: {
        "equities": -0.6, "volatility": +0.6, "gold": +0.5,
        "dxy": +0.4, "yields": -0.3, "liquidity": -0.3,
    },
    PANIC: {
        "equities": -0.9, "volatility": +0.9, "liquidity": -0.8,
        "dxy": +0.6, "gold": +0.2, "yields": -0.4,
    },
    LIQUIDITY_EXPANSION: {
        "liquidity": +0.8, "equities": +0.5, "volatility": -0.5,
        "dxy": -0.4, "yields": -0.2, "gold": +0.2,
    },
    TIGHTENING: {
        "yields": +0.7, "dxy": +0.5, "liquidity": -0.6,
        "equities": -0.3, "volatility": +0.3, "gold": -0.3,
    },
}
REGIMES = tuple(REGIME_SIGNATURES.keys())

# Risk polarity — used to label the direction of a transition.
_RISK_BUILDING = {RISK_ON, LIQUIDITY_EXPANSION}
_RISK_REDUCING = {RISK_OFF, PANIC, TIGHTENING}

# A transition must clear this gap before it is flagged as "transitioning".
TRANSITION_MIN = 0.15
# Below this best-fit, no regime is asserted — the state is reported
# INDETERMINATE rather than naming a regime the data barely supports.
_WEAK_FIT      = 0.15


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def score_regime_fit(state: dict, signature: dict) -> float:
    """How well an observed node-state vector matches a regime signature.

    Returns a fit in roughly [-1, +1]: +1 = state aligns fully with the
    signature, -1 = state is the signature's mirror image, 0 = unrelated.
    Scored only over keys present in both, normalised by the signature's
    total magnitude so partial state vectors still produce a fair score.
    """
    shared = [k for k in signature if k in state]
    if not shared:
        return 0.0
    num = sum(_clamp(state[k]) * signature[k] for k in shared)
    den = sum(abs(signature[k]) for k in shared)
    return round(num / den, 4) if den else 0.0


def score_all_regimes(state: dict) -> dict[str, float]:
    """Fit score for every regime against one state vector."""
    return {r: score_regime_fit(state, sig) for r, sig in REGIME_SIGNATURES.items()}


def _best(scores: dict[str, float]) -> tuple[str, float]:
    """Argmax regime + its fit. Deterministic tie-break by REGIMES order."""
    best_r, best_s = REGIMES[0], scores.get(REGIMES[0], 0.0)
    for r in REGIMES:
        if scores.get(r, 0.0) > best_s:
            best_r, best_s = r, scores[r]
    return best_r, best_s


def _direction(from_regime: str, to_regime: str) -> str:
    """Label the polarity of a regime shift."""
    if from_regime == to_regime:
        return "stable"
    if to_regime in _RISK_REDUCING and from_regime in _RISK_BUILDING:
        return "deteriorating"
    if to_regime in _RISK_BUILDING and from_regime in _RISK_REDUCING:
        return "improving"
    if to_regime in _RISK_REDUCING:
        return "deteriorating"
    if to_regime in _RISK_BUILDING:
        return "improving"
    return "rotating"


def compute_transition(node_states: dict,
                       pressures: Optional[dict] = None,
                       *,
                       regime_engine_hint: Optional[str] = None) -> dict:
    """Score the current regime and the regime transition under way.

    Parameters
    ----------
    node_states : dict
        The raw OBSERVED node states (event_graph.derive_node_states()).
        Missing nodes (equities/liquidity may be absent) score as 0.
    pressures : dict, optional
        The event_graph.propagate() output — the settled, causally
        propagated state across all 8 nodes. This is the forward-looking
        view. When omitted, the projected view falls back to node_states
        (→ no transition signal).
    regime_engine_hint : str, optional
        regime_engine's own label, surfaced for cross-reference only —
        it does not override this engine's data-driven computation.

    Returns
    -------
    dict
        current_regime / projected_regime / transition_score (0-1) /
        stability (0-1) / direction / regime_scores / note.
    """
    if pressures is None:
        pressures = node_states

    # Score "current" over the SAME key set as "projected" so the two views
    # are comparable. equities/liquidity are not observed directly — treat an
    # absent reading as flat (0.0) rather than dropping the dimension, which
    # would otherwise inflate the current-state fit and mask transitions.
    current_state = dict(node_states)
    current_state.setdefault("equities", 0.0)
    current_state.setdefault("liquidity", 0.0)

    scores_current   = score_all_regimes(current_state)
    scores_projected = score_all_regimes(pressures)

    current_regime, current_fit     = _best(scores_current)
    projected_regime, projected_fit = _best(scores_projected)

    # Transition score: under the causally-projected state, how far does the
    # projected-winner pull ahead of the current regime? Same regime → ~0.
    gap = scores_projected[projected_regime] - scores_projected.get(current_regime, 0.0)
    transition_score = round(max(0.0, min(1.0, gap)), 4)
    stability = round(max(0.0, min(1.0, 1.0 - transition_score)), 4)

    # Weak signal: no regime fits strongly enough on either view → do not
    # name a regime the data barely supports.
    weak = current_fit < _WEAK_FIT and projected_fit < _WEAK_FIT

    if weak:
        current_display   = INDETERMINATE
        projected_display = INDETERMINATE
        transitioning     = False
        direction         = "stable"
        note              = "Weak / mixed signal — no regime asserts itself clearly"
    else:
        current_display   = current_regime
        projected_display = projected_regime
        transitioning     = (projected_regime != current_regime) and \
                            (transition_score >= TRANSITION_MIN)
        direction         = _direction(current_regime, projected_regime) \
                            if transitioning else "stable"
        if transitioning:
            note = (f"Regime transitioning {current_regime} → {projected_regime} "
                    f"({direction}, score {transition_score:.2f})")
        else:
            note = f"{current_regime} holding — stable (fit {current_fit:.2f})"

    return {
        "current_regime":     current_display,
        "current_fit":        round(current_fit, 4),
        "projected_regime":   projected_display,
        "projected_fit":      round(projected_fit, 4),
        "transitioning":      transitioning,
        "transition_score":   transition_score,
        "stability":          stability,
        "direction":          direction,
        "weak_signal":        weak,
        "regime_scores":      {r: round(s, 4) for r, s in scores_projected.items()},
        "regime_engine_hint": regime_engine_hint,
        "note":               note,
    }
