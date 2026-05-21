"""
confidence_engine.py — Deterministic confidence scoring for market briefs.

Takes a consensus dict (from bias_consensus_engine) plus its signal list
and produces a 0-100 confidence score with a HIGH/MEDIUM/LOW tier and a
full component breakdown so the morning report can explain WHY confidence
is what it is.

Confidence formula (each component 0..1, then weighted):
  agreement        0.40  — how aligned the deterministic sources are
  signal_strength  0.27  — mean absolute score across sources
  source_coverage  0.13  — how many of the 7 sources actually reported
  stability        0.12  — regime stability (1 - transition/contradiction)
  freshness        0.08  — how recent the underlying data is

Tiers:
  >= 70  HIGH
  >= 45  MEDIUM
  <  45  LOW

Pure Python, no I/O, deterministic. When confidence is LOW the morning
report downgrades the brief — no high-conviction language is allowed.
The `stability` factor lets a regime transition or a causal contradiction
(from event_graph / regime_transition_engine) cut conviction even when
the static sources happen to agree.
"""
from __future__ import annotations

from typing import Optional


WEIGHT_AGREEMENT       = 0.40
WEIGHT_SIGNAL_STRENGTH = 0.27
WEIGHT_SOURCE_COVERAGE = 0.13
WEIGHT_STABILITY       = 0.12
WEIGHT_FRESHNESS       = 0.08

TIER_HIGH   = "HIGH"
TIER_MEDIUM = "MEDIUM"
TIER_LOW    = "LOW"

# Total deterministic sources the consensus engine can draw on.
_MAX_SOURCES = 7


def _tier(score: float) -> str:
    if score >= 70:
        return TIER_HIGH
    if score >= 45:
        return TIER_MEDIUM
    return TIER_LOW


def compute_confidence(consensus: dict,
                       *,
                       freshness: float = 1.0,
                       stability: float = 1.0) -> dict:
    """Compute a confidence score for a consensus result.

    Parameters
    ----------
    consensus : dict
        Output of bias_consensus_engine.compute_consensus().
    freshness : float
        0..1 — how fresh the underlying data is. 1.0 = just computed,
        decays toward 0 as the cached brief ages. morning_report passes
        a decayed value for stale cache hits.
    stability : float
        0..1 — regime stability. morning_report derives this from
        regime_transition_engine (1 - transition_score) and the
        event_graph contradiction count: a regime mid-transition or a
        market full of causal contradictions is inherently less
        trustworthy, so conviction is cut even if the static sources
        agree. Defaults to 1.0 (fully stable) when not supplied.

    Returns
    -------
    dict
        {score: 0-100, tier: HIGH|MEDIUM|LOW, components: {...},
         note: <short human-readable reason>}
    """
    if not isinstance(consensus, dict):
        return {"score": 0, "tier": TIER_LOW, "components": {},
                "note": "no consensus data"}

    votes = consensus.get("votes") or []
    source_count = consensus.get("source_count", len(votes))

    # No deterministic sources at all → confidence is 0, full stop.
    # (Freshness must not manufacture confidence out of an empty signal set.)
    if source_count <= 0:
        return {"score": 0, "tier": TIER_LOW,
                "components": {"agreement": 0.0, "signal_strength": 0.0,
                               "source_coverage": 0.0, "stability": 0.0,
                               "freshness": 0.0},
                "note": "LOW confidence — no deterministic sources reported"}

    # ── Component 1: agreement (already computed by the consensus engine)
    agreement = float(consensus.get("agreement") or 0.0)

    # ── Component 2: signal strength — mean |score| across sources
    if votes:
        strength = sum(abs(float(v.get("score") or 0)) for v in votes) / len(votes)
    else:
        strength = 0.0
    strength = max(0.0, min(1.0, strength))

    # ── Component 3: source coverage — how many of 7 engines reported
    coverage = max(0.0, min(1.0, source_count / _MAX_SOURCES))

    # ── Component 4: stability — regime not transitioning, no contradictions
    stab = max(0.0, min(1.0, float(stability)))

    # ── Component 5: freshness (passed in)
    fresh = max(0.0, min(1.0, float(freshness)))

    raw = (
        WEIGHT_AGREEMENT       * agreement +
        WEIGHT_SIGNAL_STRENGTH * strength +
        WEIGHT_SOURCE_COVERAGE * coverage +
        WEIGHT_STABILITY       * stab +
        WEIGHT_FRESHNESS       * fresh
    )
    score = round(raw * 100, 1)
    tier  = _tier(score)

    # Short reason — surfaces the weakest component so the report can explain
    components = {
        "agreement":       round(agreement, 3),
        "signal_strength": round(strength, 3),
        "source_coverage": round(coverage, 3),
        "stability":       round(stab, 3),
        "freshness":       round(fresh, 3),
    }
    weakest = min(components, key=components.get)
    note_map = {
        "agreement":       "sources disagree on direction",
        "signal_strength": "individual signals are weak/mixed",
        "source_coverage": "some deterministic engines did not report",
        "stability":       "regime is transitioning / causal contradictions present",
        "freshness":       "underlying data is ageing — refresh due",
    }
    note = (f"{tier} confidence — limited by: {note_map.get(weakest, weakest)}"
            if tier != TIER_HIGH else "HIGH confidence — sources aligned")

    return {
        "score":      score,
        "tier":       tier,
        "components": components,
        "note":       note,
    }


def is_high_conviction(confidence: dict) -> bool:
    """True only when the brief earns high-conviction language. The morning
    report gates strong wording behind this."""
    return isinstance(confidence, dict) and confidence.get("tier") == TIER_HIGH
