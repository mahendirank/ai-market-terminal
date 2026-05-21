"""
bias_consensus_engine.py — The single source of directional truth.

Combines the deterministic engines into ONE bias per market. The LLM layer
in morning_report.py is FORBIDDEN from contradicting this output — it may
only narrate it.

Signal sources (each contributes a normalised score in [-1, +1]):
  indicators       — per-market technical composite  (weight 0.35, strongest)
  macro_reasoning  — global regime/scenario lean     (weight 0.20)
  regime           — regime_engine state vector       (weight 0.15)
  events           — event_classifier directional tilt(weight 0.12)
  sentiment        — sentiment_weighting per-asset     (weight 0.13)
  correlation      — correlation_engine context        (weight 0.05, light)

Output bias bands (on the weighted consensus score):
  score >= +0.15  → BUY
  score <= -0.15  → SELL
  else            → NEUTRAL

Pure Python. No LLM. No network I/O. Deterministic: same signals in →
same bias out. Designed to be cheap (a few dict ops) so it stays
VPS-friendly under load.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ─── Source weights (sum normalised at runtime, so partial sets are fine) ────
SOURCE_WEIGHTS: dict[str, float] = {
    "indicators":      0.35,   # per-market technicals — the dominant signal
    "macro_reasoning": 0.20,   # global regime/scenario
    "regime":          0.15,   # regime_engine composite
    "sentiment":       0.13,   # sentiment_weighting
    "events":          0.12,   # event_classifier directional tilt
    "correlation":     0.05,   # cross-asset context (light — informational)
}

BIAS_BUY     = "BUY"
BIAS_SELL    = "SELL"
BIAS_NEUTRAL = "NEUTRAL"

# Consensus thresholds
_BUY_THRESHOLD  = 0.15
_SELL_THRESHOLD = -0.15


@dataclass
class Signal:
    """One deterministic engine's contribution to the consensus.

    score   : normalised directional reading in [-1, +1]
              (+1 = max bullish, -1 = max bearish, 0 = neutral)
    bias    : the engine's own label (BUY/SELL/NEUTRAL) — for reporting.
              Leave "" to have it derived from ``score``.
    weight  : pulled from SOURCE_WEIGHTS unless explicitly overridden
    detail  : free-form short string for the report's "votes" breakdown
    """
    source: str
    score:  float
    bias:   str = ""
    weight: Optional[float] = None
    detail: str = ""

    def effective_weight(self) -> float:
        if self.weight is not None:
            return self.weight
        return SOURCE_WEIGHTS.get(self.source, 0.0)


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _label_for(score: float) -> str:
    if score >= _BUY_THRESHOLD:
        return BIAS_BUY
    if score <= _SELL_THRESHOLD:
        return BIAS_SELL
    return BIAS_NEUTRAL


def compute_consensus(signals: list[Signal]) -> dict:
    """Combine deterministic signals into one consensus bias.

    Returns:
      {
        "bias":        "BUY" | "SELL" | "NEUTRAL",
        "score":       float in [-1, +1]   (weighted consensus),
        "agreement":   float in [0, 1]     (fraction of weight agreeing
                                            with the consensus label),
        "votes":       [{source, bias, score, weight, detail}, ...],
        "dissent":     [<source names that disagree with consensus>],
        "source_count": int,
      }

    This dict is the directional contract for the whole morning report.
    morning_report.py and its LLM narration must treat ``bias`` as fixed.
    """
    valid = [s for s in signals if isinstance(s, Signal) and s.effective_weight() > 0]
    if not valid:
        return {
            "bias": BIAS_NEUTRAL, "score": 0.0, "agreement": 0.0,
            "votes": [], "dissent": [], "source_count": 0,
        }

    total_weight   = sum(s.effective_weight() for s in valid)
    weighted_score = sum(_clamp(s.score) * s.effective_weight() for s in valid)
    consensus_score = round(weighted_score / total_weight, 4) if total_weight else 0.0
    consensus_bias  = _label_for(consensus_score)

    # Agreement: weight-share of sources whose own label matches the consensus.
    # A source is "agreeing" if its score sits on the same side as the
    # consensus (or both are neutral).
    agree_weight = 0.0
    dissent: list[str] = []
    for s in valid:
        s_label = _label_for(_clamp(s.score))
        if s_label == consensus_bias:
            agree_weight += s.effective_weight()
        elif consensus_bias != BIAS_NEUTRAL and s_label != BIAS_NEUTRAL:
            # genuine opposite-direction vote
            dissent.append(s.source)
    agreement = round(agree_weight / total_weight, 3) if total_weight else 0.0

    votes = [
        {
            "source": s.source,
            "bias":   s.bias or _label_for(_clamp(s.score)),
            "score":  round(_clamp(s.score), 3),
            "weight": round(s.effective_weight(), 3),
            "detail": s.detail,
        }
        for s in valid
    ]

    return {
        "bias":         consensus_bias,
        "score":        consensus_score,
        "agreement":    agreement,
        "votes":        votes,
        "dissent":      dissent,
        "source_count": len(valid),
    }


# ─── Contradiction guard — used by morning_report's LLM narration layer ─────
_OPPOSITE = {BIAS_BUY: BIAS_SELL, BIAS_SELL: BIAS_BUY}


def contradicts(consensus_bias: str, candidate_bias: str) -> bool:
    """Return True if ``candidate_bias`` directly opposes the consensus.

    NEUTRAL never contradicts. BUY vs SELL (either direction) does. This is
    the gate the LLM narration must pass — if an LLM-produced directional
    word opposes the deterministic consensus, the narration is rejected.
    """
    if not consensus_bias or not candidate_bias:
        return False
    cb = consensus_bias.upper().strip()
    xb = candidate_bias.upper().strip()
    return _OPPOSITE.get(cb) == xb


def scan_for_contradiction(consensus_bias: str, text: str) -> Optional[str]:
    """Scan free-text (LLM narration) for a directional word that opposes
    the consensus. Returns the offending word, or None if clean.

    Conservative: only flags explicit bias verbs/nouns, not soft language.
    """
    if not text or not consensus_bias:
        return None
    cb = consensus_bias.upper().strip()
    opposite = _OPPOSITE.get(cb)
    if not opposite:
        return None   # consensus is NEUTRAL — nothing to contradict

    lo = text.lower()
    # Phrases that assert the OPPOSITE direction.
    bear_words = ["go short", "short setup", "sell signal", "bearish bias",
                  "downside bias", "favour sells", "favor sells", "expect a fall"]
    bull_words = ["go long", "long setup", "buy signal", "bullish bias",
                  "upside bias", "favour buys", "favor buys", "expect a rally"]
    watch = bear_words if opposite == BIAS_SELL else bull_words
    for w in watch:
        if w in lo:
            return w
    return None
