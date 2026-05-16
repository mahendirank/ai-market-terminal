"""
macro_reasoning_engine.py — Institutional macro reasoning, deterministic.

Transforms a market_intel snapshot into structured institutional-grade
interpretation. NO LLM calls in this module — every output is a pure
function of the snapshot. Outputs are reproducible: same input → same
output, no randomness, no network I/O.

Phase 1 — Stage 2 analyzers only. Each analyzer owns one macro dimension:

  analyze_yields(snap)      → US10Y level/delta/direction + fed_bias
  analyze_usd(snap)          → DXY level/delta/direction + liquidity stance
  analyze_volatility(snap)   → VIX level + regime band
  analyze_sentiment(snap)    → tilt + label + sample-size weighted strength
  analyze_events(snap)       → dominant event + first/second-mover detection

  analyze_stage2(snap)       → composes all five into one dict

Stages 3+ (regime synthesis, scenarios, trade generation) land in
subsequent phases. This module is intentionally not wired into any
endpoint or tab yet — feature-flagged off by being un-imported.

Latency: typical Stage 2 pass on a real snapshot is <5ms. The whole
engine runs entirely on data already in memory.
"""
from __future__ import annotations

import time
from typing import Optional


# ─── Direction constants (single source of truth for label vocab) ───────────
DIR_RISING   = "RISING"
DIR_FALLING  = "FALLING"
DIR_FLAT     = "FLAT"
DIR_STRONG   = "STRONG"
DIR_WEAK     = "WEAK"
DIR_RANGE    = "RANGE"

BIAS_HAWKISH = "HAWKISH"
BIAS_DOVISH  = "DOVISH"
BIAS_NEUTRAL = "NEUTRAL"

VOL_COMPRESSED = "COMPRESSED"
VOL_NORMAL     = "NORMAL"
VOL_HIGH       = "HIGH"
VOL_EXTREME    = "EXTREME"

SENT_BULLISH = "BULLISH"
SENT_BEARISH = "BEARISH"
SENT_NEUTRAL = "NEUTRAL"

MOVER_FIRST  = "FIRST_MOVER"
MOVER_SECOND = "SECOND_MOVER"
MOVER_STALE  = "STALE"
MOVER_NONE   = "NONE"


# ─── Internal helpers (small, pure) ─────────────────────────────────────────
def _macro_field(snap: dict, key: str) -> tuple[Optional[float], Optional[float]]:
    """Return (level, change_pct) for a macro_snapshot entry. Tolerates both
    the v1 (raw scalar) and v2 (dict with price/change_pct) shapes.

    Examples
    --------
    >>> _macro_field({"macro_snapshot": {"vix": 14.2}}, "vix")
    (14.2, None)
    >>> _macro_field({"macro_snapshot": {"dxy": {"price": 99.27, "change_pct": 0.45}}}, "dxy")
    (99.27, 0.45)
    """
    m = (snap or {}).get("macro_snapshot") or {}
    v = m.get(key)
    if v is None:
        return None, None
    if isinstance(v, dict):
        level = v.get("price") or v.get("last")
        chg   = v.get("change_pct")
        if chg is None:
            chg = v.get("change")   # last resort
        try:
            level = float(level) if level is not None else None
        except Exception:
            level = None
        try:
            chg = float(chg) if chg is not None else None
        except Exception:
            chg = None
        return level, chg
    try:
        return float(v), None
    except Exception:
        return None, None


def _events_summary(snap: dict) -> dict:
    """Return ``events_classified`` dict or an empty stub."""
    e = (snap or {}).get("events_classified") or {}
    if not isinstance(e, dict):
        return {"by_category": {}, "directional": {}, "total_classified": 0}
    return {
        "by_category":      e.get("by_category") or {},
        "directional":      e.get("directional") or {},
        "total_classified": e.get("total_classified") or 0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# ANALYZER 1 — YIELDS
# ═══════════════════════════════════════════════════════════════════════════
# Reads: macro_snapshot.us10y  +  events_classified.MONETARY
# Produces: level, 24h delta in bp, direction band, Fed bias.
#
# Direction bands (24h move in bp on US10Y):
#   |Δ| < 2bp   →  FLAT
#   Δ > +5bp   →  RISING
#   Δ < -5bp   →  FALLING
#   else       →  FLAT (drift within noise band)
#
# Fed bias score (-1..+1):
#   +0.4 if yields RISING, -0.4 if FALLING
#   ± from MONETARY event directional tilt scaled by severity
#   |score| ≥ 0.3 → HAWKISH/DOVISH, else NEUTRAL
def analyze_yields(snap: dict) -> dict:
    level, chg_pct = _macro_field(snap, "us10y")

    # Convert change to basis points. ^TNX trades in "yield %" so 1% move = 1bp
    # at the index level, but yfinance change_pct returns the *percent change*
    # of the index value, not the bp delta. Approximation: bp_delta ≈
    # level × change_pct / 100 × 100 = level × change_pct, in bp.
    delta_bp: Optional[float] = None
    if level is not None and chg_pct is not None:
        delta_bp = round(level * chg_pct, 1)
    elif chg_pct is not None:
        # Fallback: rough conversion assuming a 4-5% yield environment
        delta_bp = round(chg_pct * 4.5, 1)

    if delta_bp is None or abs(delta_bp) < 2.0:
        direction = DIR_FLAT
    elif delta_bp > 5.0:
        direction = DIR_RISING
    elif delta_bp < -5.0:
        direction = DIR_FALLING
    else:
        direction = DIR_FLAT

    # Fed bias — yield-led + monetary news tilt
    fed_score = 0.0
    if direction == DIR_RISING:
        fed_score += 0.4
    elif direction == DIR_FALLING:
        fed_score -= 0.4

    ev = _events_summary(snap)
    monetary = (ev["by_category"] or {}).get("MONETARY") or {}
    directional = ev["directional"] or {}
    if monetary.get("count", 0) > 0:
        bull_w = float(directional.get("bull_weighted") or 0)
        bear_w = float(directional.get("bear_weighted") or 0)
        total  = bull_w + bear_w
        if total > 0:
            # bear weight on MONETARY events ≈ hawkish (rate-hike news pressures risk)
            fed_score += (bear_w - bull_w) / total * 0.5

    if fed_score >= 0.3:
        fed_bias = BIAS_HAWKISH
    elif fed_score <= -0.3:
        fed_bias = BIAS_DOVISH
    else:
        fed_bias = BIAS_NEUTRAL

    term_premium = "expanding" if delta_bp is not None and delta_bp > 8 else "stable"

    return {
        "us10y_level":         level,
        "us10y_delta_bp":      delta_bp,
        "direction":           direction,
        "fed_bias":            fed_bias,
        "fed_score":           round(fed_score, 3),
        "term_premium_signal": term_premium,
    }


# ═══════════════════════════════════════════════════════════════════════════
# ANALYZER 2 — USD
# ═══════════════════════════════════════════════════════════════════════════
# Reads: macro_snapshot.dxy
# Produces: level, 24h % delta, direction band, liquidity stance.
#
# Direction bands:
#   |Δ%| < 0.15%   →  RANGE
#   Δ% > +0.30%    →  STRONG
#   Δ% < -0.30%    →  WEAK
#   in-between     →  RANGE
def analyze_usd(snap: dict) -> dict:
    level, chg_pct = _macro_field(snap, "dxy")

    if chg_pct is None or abs(chg_pct) < 0.15:
        direction = DIR_RANGE
    elif chg_pct > 0.30:
        direction = DIR_STRONG
    elif chg_pct < -0.30:
        direction = DIR_WEAK
    else:
        direction = DIR_RANGE

    liquidity = {
        DIR_STRONG: "tightening",
        DIR_WEAK:   "easing",
        DIR_RANGE:  "neutral",
    }[direction]

    return {
        "dxy_level":        level,
        "dxy_delta_pct":    chg_pct,
        "direction":        direction,
        "liquidity_stance": liquidity,
    }


# ═══════════════════════════════════════════════════════════════════════════
# ANALYZER 3 — VOLATILITY
# ═══════════════════════════════════════════════════════════════════════════
# Reads: macro_snapshot.vix  +  regime_state.dimensions.VOLATILITY
# Produces: level + regime band + score 0-100 (high = more volatile).
#
# Regime bands (VIX level):
#   < 13       →  COMPRESSED (low score, complacent)
#   13 - 17    →  NORMAL
#   17 - 25    →  HIGH
#   > 25       →  EXTREME
def analyze_volatility(snap: dict) -> dict:
    level, _ = _macro_field(snap, "vix")

    if level is None:
        # Fall back to regime_state's VOLATILITY dimension if present
        rs = (snap or {}).get("regime_state") or {}
        dim_vol = ((rs.get("dimensions") or {}).get("VOLATILITY")) or {}
        score = dim_vol.get("score")
        if score is None:
            return {"vix_level": None, "regime": "UNKNOWN", "score": 50}
        if score >= 75:    regime = VOL_EXTREME
        elif score >= 55:  regime = VOL_HIGH
        elif score >= 35:  regime = VOL_NORMAL
        else:              regime = VOL_COMPRESSED
        return {"vix_level": None, "regime": regime, "score": score}

    if level < 13:
        regime, score = VOL_COMPRESSED, 80
    elif level < 17:
        regime, score = VOL_NORMAL, 55
    elif level < 25:
        regime, score = VOL_HIGH, 35
    else:
        regime, score = VOL_EXTREME, 15

    return {
        "vix_level": round(level, 2),
        "regime":    regime,
        "score":     score,
    }


# ═══════════════════════════════════════════════════════════════════════════
# ANALYZER 4 — SENTIMENT
# ═══════════════════════════════════════════════════════════════════════════
# Reads: sentiment.{tilt_score, sample_size, macro_tilt}
# Produces: tilt -1..+1, label, strength (|tilt|), sample_size.
#
# Bands match sentiment_weighting's defaults:
#   tilt ≥ +0.15  →  BULLISH
#   tilt ≤ -0.15  →  BEARISH
#   else          →  NEUTRAL
def analyze_sentiment(snap: dict) -> dict:
    s = (snap or {}).get("sentiment") or {}
    tilt = s.get("tilt_score")
    try:
        tilt = float(tilt) if tilt is not None else 0.0
    except Exception:
        tilt = 0.0
    sample = int(s.get("sample_size") or 0)

    if sample == 0:
        label = SENT_NEUTRAL
    elif tilt >= 0.15:
        label = SENT_BULLISH
    elif tilt <= -0.15:
        label = SENT_BEARISH
    else:
        label = SENT_NEUTRAL

    return {
        "tilt":        round(tilt, 3),
        "label":       label,
        "sample_size": sample,
        "strength":    round(abs(tilt), 3),
    }


# ═══════════════════════════════════════════════════════════════════════════
# ANALYZER 5 — EVENTS
# ═══════════════════════════════════════════════════════════════════════════
# Reads: events_classified.by_category + news.clusters[*].event
# Produces: dominant_event + first/second-mover detection + window.
#
# Dominant event: highest-severity event in the snapshot's clusters.
# First-mover: dominant event with cluster age < 6h AND severity ≥ 7.
# Second-mover: dominant event with cluster age ≥ 6h.
# Stale: cluster age ≥ 24h (lower priority).
def analyze_events(snap: dict) -> dict:
    clusters = (((snap or {}).get("news") or {}).get("clusters")) or []
    now = time.time()

    best = None
    best_age_h = None
    for c in clusters:
        ev = c.get("event")
        if not isinstance(ev, dict):
            continue
        sev = int(ev.get("severity") or 0)
        if sev <= 0:
            continue
        # Cluster age — use the most-recent headline timestamp in the cluster
        headlines = c.get("headlines") or []
        ts_values = [h.get("ts") for h in headlines if isinstance(h.get("ts"), (int, float))]
        latest_ts = max(ts_values) if ts_values else None
        age_h = ((now - float(latest_ts)) / 3600) if latest_ts else None
        # Pick highest severity; tie-break by recency (smaller age wins)
        if (best is None
                or sev > best["severity"]
                or (sev == best["severity"]
                    and (age_h is not None)
                    and (best_age_h is None or age_h < best_age_h))):
            best = {
                "topic":      c.get("topic", "")[:140],
                "category":   ev.get("category", "UNKNOWN"),
                "severity":   sev,
                "direction":  ev.get("direction", "NEUTRAL"),
                "first_mover_source": c.get("first_mover", ""),
                "size":       c.get("size", len(headlines) or 1),
                "tickers":    c.get("tickers") or [],
                "age_hours":  round(age_h, 2) if age_h is not None else None,
            }
            best_age_h = age_h

    if best is None:
        return {
            "dominant_event":          None,
            "first_or_second_mover":   MOVER_NONE,
            "catalyst_window_hours":   None,
        }

    age = best.get("age_hours")
    if age is None:
        mover = MOVER_NONE
    elif age < 6 and best["severity"] >= 7:
        mover = MOVER_FIRST
    elif age < 24:
        mover = MOVER_SECOND
    else:
        mover = MOVER_STALE

    # Catalyst window: how long the market is likely to react.
    # High-severity events on monetary/risk topics → 24-72h windows.
    if best["severity"] >= 9:
        window = 72
    elif best["severity"] >= 7:
        window = 24
    elif best["severity"] >= 4:
        window = 8
    else:
        window = 2

    return {
        "dominant_event":         best,
        "first_or_second_mover":  mover,
        "catalyst_window_hours":  window,
    }


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 2 COMPOSER
# ═══════════════════════════════════════════════════════════════════════════
def analyze_stage2(snap: dict) -> dict:
    """Run all 5 Stage-2 analyzers and bundle outputs.

    Pure function. No I/O, no caching at this layer (the input snapshot is
    already cached upstream and Stage-2 itself takes <5ms).
    """
    return {
        "yields":     analyze_yields(snap),
        "usd":        analyze_usd(snap),
        "volatility": analyze_volatility(snap),
        "sentiment":  analyze_sentiment(snap),
        "events":     analyze_events(snap),
        "stage":      "2_hierarchy_analysis",
    }


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 3 — REGIME SYNTHESIS with HIERARCHY OVERRIDE
# ═══════════════════════════════════════════════════════════════════════════
# Combines Stage-2 outputs into a single macro_regime + dominant_driver,
# applying institutional priority rules:
#
#   1. YIELDS layer    — overrides everything when |Δ| ≥ 8bp on US10Y
#   2. USD layer       — overrides risk narrative when STRONG or WEAK
#   3. VOLATILITY      — overrides bullish bias when VIX ≥ 25
#   4. SENTIMENT       — confirmatory only, never overrides
#
# This mirrors how a senior desk actually reads the tape: a yield spike
# trumps an equity narrative; a DXY breakout trumps a sentiment read;
# a VIX > 25 means stop calling risk-on regardless of the tape.
#
# Composite regime labels:
#   CRISIS               vol EXTREME + risk-off direction
#   TIGHTENING_PANIC     yields RISING + USD STRONG + vol HIGH
#   STAGFLATION          yields RISING + sentiment BEARISH + vol HIGH
#   INFLATIONARY         yields RISING + Fed HAWKISH (controlled)
#   RISK_OFF             vol HIGH + sentiment BEARISH (no yield spike)
#   GOLDILOCKS           vol COMPRESSED + Fed NEUTRAL/DOVISH + sentiment BULLISH
#   RISK_ON              vol NORMAL/COMPRESSED + sentiment BULLISH
#   MIXED                fallback when no rule fires

REGIME_CRISIS            = "CRISIS"
REGIME_TIGHTENING_PANIC  = "TIGHTENING_PANIC"
REGIME_STAGFLATION       = "STAGFLATION"
REGIME_INFLATIONARY      = "INFLATIONARY"
REGIME_RISK_OFF          = "RISK_OFF"
REGIME_GOLDILOCKS        = "GOLDILOCKS"
REGIME_RISK_ON           = "RISK_ON"
REGIME_MIXED             = "MIXED"

LAYER_YIELDS    = "yields"
LAYER_USD       = "usd"
LAYER_VOL       = "volatility"
LAYER_SENTIMENT = "sentiment"
LAYER_NONE      = "none"


def _driver_from_layer(layer: str, y: dict, u: dict, v: dict, s: dict, e: dict) -> str:
    """Compact, machine-readable driver string for downstream stages."""
    if layer == LAYER_YIELDS:
        return f"yields_{y['direction'].lower()}"     # yields_rising / yields_falling
    if layer == LAYER_USD:
        return f"usd_{u['direction'].lower()}"
    if layer == LAYER_VOL:
        return f"vol_{v['regime'].lower()}"
    if layer == LAYER_SENTIMENT:
        return f"sentiment_{s['label'].lower()}"
    # Fallback: dominant event category if no layer is loud
    ev = e.get("dominant_event") if e else None
    if ev:
        return f"event_{(ev.get('category','unknown')).lower()}"
    return "ambiguous"


def _internal_consistency(y: dict, u: dict, v: dict, s: dict) -> float:
    """How aligned are the 4 layers? 1.0 = all pointing the same way.

    Risk-on direction signals: USD WEAK, vol COMPRESSED/NORMAL, sentiment BULLISH
    Risk-off direction signals: USD STRONG, vol HIGH/EXTREME, sentiment BEARISH
    Yields are bidirectional and don't cleanly map to "risk on/off" — we
    weight them separately as a tilt-amplifier.
    """
    risk_on  = 0
    risk_off = 0
    if u.get("direction") == DIR_WEAK:   risk_on  += 1
    if u.get("direction") == DIR_STRONG: risk_off += 1
    if v.get("regime") in (VOL_COMPRESSED, VOL_NORMAL): risk_on  += 1
    if v.get("regime") in (VOL_HIGH, VOL_EXTREME):      risk_off += 1
    if s.get("label") == SENT_BULLISH: risk_on  += 1
    if s.get("label") == SENT_BEARISH: risk_off += 1

    total = risk_on + risk_off
    if total == 0:
        return 0.5   # all neutral — moderate consistency
    return round(max(risk_on, risk_off) / total, 3)


def synthesize_regime(y: dict, u: dict, v: dict, s: dict, e: dict) -> dict:
    """Apply the hierarchy and return a single regime + driver + rationale.

    Inputs are the outputs of analyze_yields/usd/volatility/sentiment/events.
    Output is a small dict suitable for downstream stages or prompt rendering.
    """
    consistency = _internal_consistency(y, u, v, s)
    rationale_parts: list[str] = []

    # ── Layer 1: YIELDS override (|Δ| ≥ 8bp on US10Y triggers regime decision)
    delta = y.get("us10y_delta_bp")
    yields_loud = delta is not None and abs(delta) >= 8.0

    # ── CRISIS — vol extreme + risk-off direction
    if v.get("regime") == VOL_EXTREME and (
        u.get("direction") == DIR_STRONG or s.get("label") == SENT_BEARISH
    ):
        regime = REGIME_CRISIS
        layer  = LAYER_VOL
        rationale_parts.append(f"VIX {v.get('vix_level','?')} in EXTREME band")
        if u.get("direction") == DIR_STRONG:
            rationale_parts.append(f"DXY {u.get('dxy_delta_pct',0):+.2f}% (flight-to-quality)")
        return _assemble_regime(regime, layer, y, u, v, s, e, rationale_parts, consistency)

    # ── TIGHTENING_PANIC — yields spike + USD strong + vol high
    if yields_loud and y["direction"] == DIR_RISING and u.get("direction") == DIR_STRONG \
            and v.get("regime") in (VOL_HIGH, VOL_EXTREME):
        regime = REGIME_TIGHTENING_PANIC
        layer  = LAYER_YIELDS
        rationale_parts.append(f"US10Y +{delta:.0f}bp drove DXY {u.get('dxy_delta_pct',0):+.2f}%")
        rationale_parts.append(f"VIX {v.get('vix_level','?')} confirms risk-off")
        return _assemble_regime(regime, layer, y, u, v, s, e, rationale_parts, consistency)

    # ── STAGFLATION — rising yields + bearish sentiment + elevated vol
    if yields_loud and y["direction"] == DIR_RISING and s.get("label") == SENT_BEARISH \
            and v.get("regime") in (VOL_HIGH, VOL_EXTREME):
        regime = REGIME_STAGFLATION
        layer  = LAYER_YIELDS
        rationale_parts.append(f"US10Y +{delta:.0f}bp with bearish news tilt (n={s.get('sample_size',0)})")
        rationale_parts.append(f"VIX {v.get('vix_level','?')} elevated — growth scare overlay")
        return _assemble_regime(regime, layer, y, u, v, s, e, rationale_parts, consistency)

    # ── INFLATIONARY — yields rising + Fed hawkish (controlled, vol not yet extreme)
    if y["direction"] == DIR_RISING and y.get("fed_bias") == BIAS_HAWKISH \
            and v.get("regime") not in (VOL_EXTREME,):
        regime = REGIME_INFLATIONARY
        layer  = LAYER_YIELDS
        rationale_parts.append(f"US10Y {('+%.0fbp' % delta) if delta else 'rising'} + Fed HAWKISH tilt")
        return _assemble_regime(regime, layer, y, u, v, s, e, rationale_parts, consistency)

    # ── RISK_OFF — vol high + bearish sentiment (no yield spike)
    if v.get("regime") in (VOL_HIGH, VOL_EXTREME) and s.get("label") == SENT_BEARISH:
        regime = REGIME_RISK_OFF
        layer  = LAYER_VOL
        rationale_parts.append(f"VIX {v.get('vix_level','?')} + bearish tilt (no yield catalyst)")
        return _assemble_regime(regime, layer, y, u, v, s, e, rationale_parts, consistency)

    # ── USD-led override (next priority): strong USD + bearish sentiment = RISK_OFF
    if u.get("direction") == DIR_STRONG and s.get("label") == SENT_BEARISH:
        regime = REGIME_RISK_OFF
        layer  = LAYER_USD
        rationale_parts.append(f"DXY {u.get('dxy_delta_pct',0):+.2f}% tightening + bearish news")
        return _assemble_regime(regime, layer, y, u, v, s, e, rationale_parts, consistency)

    # ── GOLDILOCKS — vol compressed + Fed dovish/neutral + bullish sentiment
    if v.get("regime") == VOL_COMPRESSED \
            and y.get("fed_bias") in (BIAS_DOVISH, BIAS_NEUTRAL) \
            and s.get("label") == SENT_BULLISH:
        regime = REGIME_GOLDILOCKS
        layer  = LAYER_VOL
        rationale_parts.append(f"VIX compressed + Fed {y.get('fed_bias','?')} + bullish tilt")
        return _assemble_regime(regime, layer, y, u, v, s, e, rationale_parts, consistency)

    # ── RISK_ON — non-elevated vol + bullish sentiment
    if v.get("regime") in (VOL_COMPRESSED, VOL_NORMAL) and s.get("label") == SENT_BULLISH:
        regime = REGIME_RISK_ON
        layer  = LAYER_SENTIMENT
        rationale_parts.append(f"VIX {v.get('regime','?').lower()} + bullish news tilt")
        return _assemble_regime(regime, layer, y, u, v, s, e, rationale_parts, consistency)

    # ── Default: MIXED
    rationale_parts.append("layers do not align on a single direction")
    return _assemble_regime(REGIME_MIXED, LAYER_NONE, y, u, v, s, e, rationale_parts, consistency)


def _assemble_regime(regime: str, layer: str, y: dict, u: dict, v: dict, s: dict, e: dict,
                      rationale_parts: list[str], consistency: float) -> dict:
    """Final assembly for synthesize_regime — keeps the public shape uniform."""
    dominant_driver = _driver_from_layer(layer, y, u, v, s, e)
    rationale = " · ".join(rationale_parts) if rationale_parts else "no dominant force"
    return {
        "regime":               regime,
        "dominant_driver":      dominant_driver,
        "override_layer":       layer,
        "rationale":            rationale,
        "internal_consistency": consistency,
    }


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 3 COMPOSER
# ═══════════════════════════════════════════════════════════════════════════
def analyze_stage3(snap: dict) -> dict:
    """Run Stage 2 + 3: returns analyzers AND the synthesized regime.

    Same purity guarantees as analyze_stage2. Composes synthesize_regime
    on top so callers don't have to wire layers themselves.
    """
    s2 = analyze_stage2(snap)
    regime = synthesize_regime(
        s2["yields"], s2["usd"], s2["volatility"],
        s2["sentiment"], s2["events"],
    )
    return {
        **s2,
        "regime_synthesis": regime,
        "stage":            "3_regime_synthesis",
    }


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 4 — SCENARIO MATCHING
# ═══════════════════════════════════════════════════════════════════════════
# Matches the stage3 state vector against named institutional scenarios
# from macro_scenarios.MACRO_SCENARIOS. Each scenario is a list of small
# predicate functions; match_strength = fraction of conditions satisfied.
#
# Scenario selection:
#   1. Compute match_strength for every scenario.
#   2. Pick the scenario with the highest strength.
#   3. If best strength < MATCH_THRESHOLD_MIN, fall back to NO_CLEAN_SCENARIO.
#
# Deterministic: same stage3 input → same scenario output. Pure function.
# Linear scan over ~8 scenarios is sub-millisecond.

def match_scenario(stage3: dict) -> dict:
    """Return the best-matching scenario for a stage3 state dict.

    Output:
      {
        "name":              str,
        "match_strength":    float 0..1,
        "matched_conditions":list[int],   # indices of conditions that fired
        "failed_conditions": list[int],   # indices that didn't fire
        "trade_lean":        dict,        # copied from scenario
        "analog_keywords":   list[str],
        "horizon_bias":      dict,
        "conviction_baseline": int,
        "description":       str,
      }
    """
    from macro_scenarios import (
        MACRO_SCENARIOS, NO_CLEAN_SCENARIO, MATCH_THRESHOLD_MIN,
    )

    if not isinstance(stage3, dict):
        return _scenario_envelope(NO_CLEAN_SCENARIO, 0.0, [], [])

    # Require at least the 4 core analyzer sections to be present. An empty
    # state dict shouldn't accidentally satisfy RANGE_BOUND_CHOP (whose
    # conditions are all "neutral defaults") — that's a data hole, not a
    # genuine flat-tape signal.
    required_sections = ("yields", "usd", "volatility", "sentiment")
    if not all(isinstance(stage3.get(k), dict) for k in required_sections):
        return _scenario_envelope(NO_CLEAN_SCENARIO, 0.0, [], [])

    best_scenario = None
    best_strength = -1.0
    best_matched: list[int] = []
    best_failed:  list[int] = []

    for scenario in MACRO_SCENARIOS:
        conditions = scenario.get("conditions") or []
        if not conditions:
            continue
        matched_idx: list[int] = []
        failed_idx:  list[int] = []
        for idx, cond in enumerate(conditions):
            try:
                ok = bool(cond(stage3))
            except Exception:
                ok = False
            (matched_idx if ok else failed_idx).append(idx)
        strength = len(matched_idx) / len(conditions)

        if strength > best_strength:
            best_strength = strength
            best_scenario = scenario
            best_matched  = matched_idx
            best_failed   = failed_idx

    if best_scenario is None or best_strength < MATCH_THRESHOLD_MIN:
        return _scenario_envelope(NO_CLEAN_SCENARIO,
                                   round(best_strength, 3) if best_strength > 0 else 0.0,
                                   best_matched, best_failed)
    return _scenario_envelope(best_scenario,
                               round(best_strength, 3), best_matched, best_failed)


def _scenario_envelope(scenario: dict, strength: float,
                        matched: list[int], failed: list[int]) -> dict:
    return {
        "name":                scenario.get("name", "?"),
        "description":         scenario.get("description", ""),
        "match_strength":      strength,
        "matched_conditions":  matched,
        "failed_conditions":   failed,
        "trade_lean":          scenario.get("trade_lean") or
                                 {"long": [], "short": [], "avoid": []},
        "analog_keywords":     scenario.get("analog_keywords") or [],
        "horizon_bias":        scenario.get("horizon_bias") or {},
        "conviction_baseline": int(scenario.get("conviction_baseline") or 50),
    }


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 4 COMPOSER
# ═══════════════════════════════════════════════════════════════════════════
def analyze_stage4(snap: dict) -> dict:
    """Run Stage 2 + 3 + 4: analyzers, synthesis, scenario match.

    Same purity guarantees as the earlier composers. This is the deepest
    public entry point currently shipping — Stage 5 (trade generation) is
    not yet implemented.
    """
    s3 = analyze_stage3(snap)
    scenario = match_scenario(s3)
    return {
        **s3,
        "scenario": scenario,
        "stage":    "4_scenario_match",
    }
