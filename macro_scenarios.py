"""
macro_scenarios.py — Named institutional macro scenarios.

Pure data. No logic. Imported by macro_reasoning_engine.match_scenario()
which evaluates the current state vector against these rules and returns
the best-matching entry.

Each scenario carries:
  name              — canonical identifier (UPPER_SNAKE_CASE)
  description       — one-line desk-grade summary
  conditions        — list of predicate functions; each gets the stage3 dict
                       and returns True/False. Match_strength = fraction of
                       conditions satisfied.
  trade_lean        — {long: [...], short: [...], avoid: [...]} as direction
                       hints for Stage 5 (not yet implemented).
  analog_keywords   — strings used to filter market_memory analogs.
  horizon_bias      — {scalping, intraday, swing} → short text bias hint.
  conviction_baseline — int 0-100 — caps high_conviction trade strength.

Design notes:
  - Conditions are tiny lambdas reading from the stage3 dict. Pure functions.
  - Order of scenarios in MACRO_SCENARIOS does NOT matter; matcher picks
    the highest match_strength.
  - 8 scenarios is the starting set. Production-grade desks track 20-30;
    add new entries as state space grows. Pre-seed list is intentionally
    conservative — better to miss-match than over-match.
"""
from __future__ import annotations

from typing import Callable


# Type alias for clarity — a condition is a function taking stage3 dict.
ConditionFn = Callable[[dict], bool]


def _yields_direction(stage3: dict) -> str:
    return ((stage3 or {}).get("yields") or {}).get("direction", "FLAT")


def _yields_delta(stage3: dict) -> float:
    d = ((stage3 or {}).get("yields") or {}).get("us10y_delta_bp")
    return float(d) if d is not None else 0.0


def _yields_fed_bias(stage3: dict) -> str:
    return ((stage3 or {}).get("yields") or {}).get("fed_bias", "NEUTRAL")


def _usd_direction(stage3: dict) -> str:
    return ((stage3 or {}).get("usd") or {}).get("direction", "RANGE")


def _usd_delta(stage3: dict) -> float:
    d = ((stage3 or {}).get("usd") or {}).get("dxy_delta_pct")
    return float(d) if d is not None else 0.0


def _vol_regime(stage3: dict) -> str:
    return ((stage3 or {}).get("volatility") or {}).get("regime", "NORMAL")


def _sentiment_label(stage3: dict) -> str:
    return ((stage3 or {}).get("sentiment") or {}).get("label", "NEUTRAL")


def _sentiment_tilt(stage3: dict) -> float:
    t = ((stage3 or {}).get("sentiment") or {}).get("tilt")
    return float(t) if t is not None else 0.0


def _event_category(stage3: dict) -> str:
    ev = ((stage3 or {}).get("events") or {}).get("dominant_event")
    if not ev:
        return "UNKNOWN"
    return ev.get("category", "UNKNOWN")


def _event_severity(stage3: dict) -> int:
    ev = ((stage3 or {}).get("events") or {}).get("dominant_event")
    if not ev:
        return 0
    return int(ev.get("severity", 0) or 0)


def _regime_label(stage3: dict) -> str:
    """Composite regime from synthesize_regime output (when stage3 also includes it)."""
    return ((stage3 or {}).get("regime_synthesis") or {}).get("regime", "MIXED")


# ─── The 8 scenarios ────────────────────────────────────────────────────────
MACRO_SCENARIOS: list[dict] = [
    {
        "name":        "TIGHTENING_PANIC",
        "description": "Yields spike + USD bid + vol expands — classic Fed-front-running scare",
        "conditions": [
            lambda s: _yields_direction(s) == "RISING" and abs(_yields_delta(s)) >= 8.0,
            lambda s: _usd_direction(s) == "STRONG",
            lambda s: _vol_regime(s) in ("HIGH", "EXTREME"),
            lambda s: _sentiment_label(s) in ("BEARISH", "NEUTRAL"),
        ],
        "trade_lean": {
            "long":  ["DXY", "VIX_CALLS"],
            "short": ["NDX", "SPX", "BTC"],
            "avoid": ["long_duration", "growth_tech", "EM_equity"],
        },
        "analog_keywords":     ["2022", "peak_inflation", "tightening", "yields_spike"],
        "horizon_bias":        {"scalping": "sell_rallies",
                                 "intraday": "short_equity",
                                 "swing":    "long_dxy_until_invalidation"},
        "conviction_baseline": 80,
    },
    {
        "name":        "MELT_UP",
        "description": "Goldilocks: dovish Fed + low vol + bullish breadth — buy-the-dip regime",
        "conditions": [
            lambda s: _regime_label(s) in ("GOLDILOCKS", "RISK_ON"),
            lambda s: _yields_fed_bias(s) in ("DOVISH", "NEUTRAL"),
            lambda s: _vol_regime(s) == "COMPRESSED",
            lambda s: _sentiment_label(s) == "BULLISH",
        ],
        "trade_lean": {
            "long":  ["NDX", "SPX", "BTC", "growth_tech"],
            "short": [],
            "avoid": ["short_index", "long_vol"],
        },
        "analog_keywords":     ["goldilocks", "melt_up", "2024-11", "rally"],
        "horizon_bias":        {"scalping": "buy_dips",
                                 "intraday": "long_growth",
                                 "swing":    "long_until_vol_breaks"},
        "conviction_baseline": 75,
    },
    {
        "name":        "STAGFLATION_LITE",
        "description": "Rising yields + bearish sentiment + elevated vol — growth scare overlay",
        "conditions": [
            lambda s: _yields_direction(s) == "RISING",
            lambda s: _sentiment_label(s) == "BEARISH",
            lambda s: _vol_regime(s) == "HIGH",
            lambda s: _vol_regime(s) != "EXTREME",  # not crisis-level
        ],
        "trade_lean": {
            "long":  ["GOLD", "DXY"],
            "short": ["growth_tech", "long_duration"],
            "avoid": ["long_EM_equity", "long_cyclicals"],
        },
        "analog_keywords":     ["stagflation", "2022-06", "growth_scare"],
        "horizon_bias":        {"scalping": "fade_equity_bounces",
                                 "intraday": "long_gold",
                                 "swing":    "long_gold_into_fed"},
        "conviction_baseline": 70,
    },
    {
        "name":        "GROWTH_SCARE",
        "description": "Falling yields + bearish sentiment + vol high — flight to quality",
        "conditions": [
            lambda s: _yields_direction(s) == "FALLING",
            lambda s: _vol_regime(s) in ("HIGH", "EXTREME"),
            lambda s: _sentiment_label(s) == "BEARISH",
            lambda s: _usd_direction(s) in ("STRONG", "RANGE"),
        ],
        "trade_lean": {
            "long":  ["long_duration", "TLT", "DXY", "GOLD"],
            "short": ["cyclicals", "small_caps", "OIL"],
            "avoid": ["short_duration", "long_high_yield_credit"],
        },
        "analog_keywords":     ["growth_scare", "recession_fear", "flight_to_quality"],
        "horizon_bias":        {"scalping": "fade_cyclicals",
                                 "intraday": "long_duration",
                                 "swing":    "long_TLT_into_data"},
        "conviction_baseline": 70,
    },
    {
        "name":        "CARRY_UNWIND",
        "description": "Sharp USD reversal + vol spike — funding currencies bid, risk fades",
        "conditions": [
            lambda s: _usd_direction(s) == "WEAK" and _usd_delta(s) <= -0.5,
            lambda s: _vol_regime(s) in ("HIGH", "EXTREME"),
            lambda s: _sentiment_label(s) == "BEARISH",
        ],
        "trade_lean": {
            "long":  ["JPY", "CHF", "GOLD"],
            "short": ["USDJPY", "EM_FX", "growth_tech"],
            "avoid": ["carry_trades", "high_beta_FX"],
        },
        "analog_keywords":     ["carry_unwind", "2024-08", "boj", "yen_rally"],
        "horizon_bias":        {"scalping": "short_usdjpy",
                                 "intraday": "fade_carry",
                                 "swing":    "short_risk_into_close"},
        "conviction_baseline": 72,
    },
    {
        "name":        "GEOPOLITICAL_RISKOFF",
        "description": "Severe geopolitical event drives flight to gold + oil + DXY",
        "conditions": [
            lambda s: _event_category(s) == "GEOPOLITICAL",
            lambda s: _event_severity(s) >= 8,
            lambda s: _vol_regime(s) in ("HIGH", "EXTREME"),
        ],
        "trade_lean": {
            "long":  ["GOLD", "OIL", "DXY", "JPY"],
            "short": ["risk_assets", "EM_equity"],
            "avoid": ["short_gold", "short_oil"],
        },
        "analog_keywords":     ["geopolitical", "war", "missile", "sanctions"],
        "horizon_bias":        {"scalping": "long_gold_spikes",
                                 "intraday": "long_safe_haven",
                                 "swing":    "trim_into_de-escalation"},
        "conviction_baseline": 65,
    },
    {
        "name":        "REFLATION",
        "description": "Rising yields with bullish sentiment + Fed neutral — pro-growth reflation",
        "conditions": [
            lambda s: _yields_direction(s) == "RISING",
            lambda s: _sentiment_label(s) == "BULLISH",
            lambda s: _yields_fed_bias(s) == "NEUTRAL",
            lambda s: _vol_regime(s) in ("NORMAL", "COMPRESSED"),
        ],
        "trade_lean": {
            "long":  ["COPPER", "OIL", "EM_equity", "cyclicals"],
            "short": ["long_duration", "defensive_sectors"],
            "avoid": ["short_commodities"],
        },
        "analog_keywords":     ["reflation", "2021", "commodity_supercycle"],
        "horizon_bias":        {"scalping": "long_cyclicals",
                                 "intraday": "long_commodities",
                                 "swing":    "long_EM_into_data"},
        "conviction_baseline": 70,
    },
    {
        "name":        "RANGE_BOUND_CHOP",
        "description": "All layers neutral — scale entries, fade extremes, no high-conviction calls",
        "conditions": [
            lambda s: _yields_direction(s) == "FLAT",
            lambda s: _usd_direction(s) == "RANGE",
            lambda s: _vol_regime(s) == "NORMAL",
            lambda s: _sentiment_label(s) == "NEUTRAL",
        ],
        "trade_lean": {
            "long":  [],
            "short": [],
            "avoid": ["high_conviction_trades", "breakout_chases"],
        },
        "analog_keywords":     ["range_bound", "chop", "consolidation"],
        "horizon_bias":        {"scalping": "fade_extremes",
                                 "intraday": "scale_at_levels",
                                 "swing":    "stay_small"},
        "conviction_baseline": 40,
    },
]


# ─── Constants for the matcher ──────────────────────────────────────────────
MATCH_THRESHOLD_GOOD = 0.75   # at/above this = high-confidence match
MATCH_THRESHOLD_MIN  = 0.50   # below this = "NO_CLEAN_SCENARIO"

NO_CLEAN_SCENARIO = {
    "name":              "NO_CLEAN_SCENARIO",
    "description":       "No named scenario hit the minimum threshold — proceed with caution",
    "conditions":        [],
    "trade_lean":        {"long": [], "short": [], "avoid": ["high_conviction_trades"]},
    "analog_keywords":   [],
    "horizon_bias":      {"scalping": "stand_aside",
                           "intraday": "wait_for_setup",
                           "swing":    "no_swing_until_signal"},
    "conviction_baseline": 30,
}


def list_scenarios() -> list[str]:
    """Return the registered scenario names — useful for introspection."""
    return [s["name"] for s in MACRO_SCENARIOS]
