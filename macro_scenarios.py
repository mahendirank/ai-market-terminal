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


# ════════════════════════════════════════════════════════════════════════════
# TRADE MATRIX — per-scenario, per-timeframe trade templates
# ════════════════════════════════════════════════════════════════════════════
# Pure data. The engine looks up the matched scenario's name in this dict
# and emits Stage-5 trade decisions. Each timeframe entry holds:
#   direction        LONG | SHORT | WAIT
#   instrument       primary asset for that timeframe
#   rationale_tags   list of compact tags (no prose)
#   invalidation     one-line condition that would invalidate the read
#   avoid_conditions list of soft "don't pull the trigger if..." lines
#
# Conviction order: scalp < intraday < swing typically — but each cell can
# override based on the regime's dominant pressure (e.g. CARRY_UNWIND
# favours intraday-strength fade calls over swing-out commitments).
TRADE_MATRIX: dict[str, dict] = {
    "TIGHTENING_PANIC": {
        "scalp": {
            "bias": "SHORT_BIAS", "primary_asset": "NDX",
            "rationale_tags": ["yields_spike", "usd_strong", "vol_expansion"],
            "thesis_invalidator":   "US10Y reverses -5bp OR DXY drops below -0.2%",
            "posture_avoid_conditions": ["if VIX retreats below 17",
                                   "if MONETARY news softens"],
        },
        "intraday": {
            "bias": "SHORT_BIAS", "primary_asset": "growth_tech",
            "rationale_tags": ["fed_hawkish", "duration_repricing"],
            "thesis_invalidator":   "Fed-speak turns dovish OR US10Y closes flat",
            "posture_avoid_conditions": ["if breadth widens", "into pre-market on data"],
        },
        "swing": {
            "bias": "LONG_BIAS", "primary_asset": "DXY",
            "rationale_tags": ["tightening_cycle", "global_dollar_squeeze"],
            "thesis_invalidator":   "DXY closes below 20d MA for 2 sessions",
            "posture_avoid_conditions": ["into FOMC blackout", "into NFP if positioning crowded"],
        },
    },
    "MELT_UP": {
        "scalp": {
            "bias": "LONG_BIAS", "primary_asset": "NDX",
            "rationale_tags": ["dips_get_bid", "low_vol_grind"],
            "thesis_invalidator":   "VIX breaks above 17 OR NDX loses 20d MA intraday",
            "posture_avoid_conditions": ["if breadth narrows to <55% adv", "if DXY catches a bid"],
        },
        "intraday": {
            "bias": "LONG_BIAS", "primary_asset": "growth_tech",
            "rationale_tags": ["dovish_anchor", "carry_friendly"],
            "thesis_invalidator":   "Term-premium expansion OR Fed-speak rejection",
            "posture_avoid_conditions": ["into CPI", "if BTC breaks key support"],
        },
        "swing": {
            "bias": "LONG_BIAS", "primary_asset": "BTC",
            "rationale_tags": ["risk_on", "liquidity_friendly"],
            "thesis_invalidator":   "Vol regime breaks COMPRESSED OR DXY > +0.5% 2 sessions",
            "posture_avoid_conditions": ["into FOMC", "if regime composite drops below 60"],
        },
    },
    "STAGFLATION_LITE": {
        "scalp": {
            "bias": "SHORT_BIAS", "primary_asset": "cyclicals",
            "rationale_tags": ["growth_scare", "yields_pressure"],
            "thesis_invalidator":   "PMI surprise upside OR yields retreat",
            "posture_avoid_conditions": ["into close on Friday", "if commodity_complex rallies"],
        },
        "intraday": {
            "bias": "LONG_BIAS", "primary_asset": "GOLD",
            "rationale_tags": ["safe_haven", "real_yield_compression"],
            "thesis_invalidator":   "US10Y +10bp without DXY drop",
            "posture_avoid_conditions": ["if DXY breaks higher", "into US session if liquidity thins"],
        },
        "swing": {
            "bias": "LONG_BIAS", "primary_asset": "GOLD",
            "rationale_tags": ["fed_overlap", "structural_safe_haven"],
            "thesis_invalidator":   "Fed pivot priced in OR real yields > +200bp",
            "posture_avoid_conditions": ["into Fed minutes", "after large COMEX positioning shift"],
        },
    },
    "GROWTH_SCARE": {
        "scalp": {
            "bias": "SHORT_BIAS", "primary_asset": "small_caps",
            "rationale_tags": ["recession_pricing", "credit_widening"],
            "thesis_invalidator":   "Jobless claims surprise downside OR HY spreads tighten",
            "posture_avoid_conditions": ["if Fed-cut odds spike", "into close on benchmark days"],
        },
        "intraday": {
            "bias": "LONG_BIAS", "primary_asset": "TLT",
            "rationale_tags": ["duration_bid", "flight_to_quality"],
            "thesis_invalidator":   "Yields stop falling OR risk reversal in equities",
            "posture_avoid_conditions": ["if curve steepens sharply", "into supply auctions"],
        },
        "swing": {
            "bias": "LONG_BIAS", "primary_asset": "TLT",
            "rationale_tags": ["recession_hedge", "data_dependent"],
            "thesis_invalidator":   "PMI rebound OR NFP surprises upside",
            "posture_avoid_conditions": ["into Treasury auctions", "post-CPI dovish surprise"],
        },
    },
    "CARRY_UNWIND": {
        "scalp": {
            "bias": "SHORT_BIAS", "primary_asset": "USDJPY",
            "rationale_tags": ["carry_unwind", "vol_premium"],
            "thesis_invalidator":   "USDJPY closes back above 20d MA",
            "posture_avoid_conditions": ["if BoJ jawboning fades", "if VIX retreats"],
        },
        "intraday": {
            "bias": "LONG_BIAS", "primary_asset": "JPY",
            "rationale_tags": ["safe_haven_funding", "risk_off"],
            "thesis_invalidator":   "Risk asset reversal OR JPY positioning extreme",
            "posture_avoid_conditions": ["into Tokyo fix", "if MOF intervention rhetoric softens"],
        },
        "swing": {
            "bias": "SHORT_BIAS", "primary_asset": "EM_FX",
            "rationale_tags": ["global_risk_off", "funding_squeeze"],
            "thesis_invalidator":   "Central bank coordinated dovish action",
            "posture_avoid_conditions": ["into Asian month-end fix", "if China stimulus headlines"],
        },
    },
    "GEOPOLITICAL_RISKOFF": {
        "scalp": {
            "bias": "LONG_BIAS", "primary_asset": "GOLD",
            "rationale_tags": ["panic_bid", "safe_haven"],
            "thesis_invalidator":   "De-escalation headline OR DXY breaks higher",
            "posture_avoid_conditions": ["if event headlines stabilise", "into US close"],
        },
        "intraday": {
            "bias": "LONG_BIAS", "primary_asset": "OIL",
            "rationale_tags": ["supply_risk", "geopolitical_premium"],
            "thesis_invalidator":   "Ceasefire OR strategic-reserve release announcement",
            "posture_avoid_conditions": ["if API inventory builds large", "into expiry"],
        },
        "swing": {
            "bias": "LONG_BIAS", "primary_asset": "DXY",
            "rationale_tags": ["risk_off_dollar_bid", "structural_safe_haven"],
            "thesis_invalidator":   "Coordinated de-escalation OR Fed dovish surprise",
            "posture_avoid_conditions": ["into Friday close", "if BOJ intervention rhetoric"],
        },
    },
    "REFLATION": {
        "scalp": {
            "bias": "LONG_BIAS", "primary_asset": "cyclicals",
            "rationale_tags": ["growth_acceleration", "commodity_bid"],
            "thesis_invalidator":   "PMI surprise downside OR DXY catches structural bid",
            "posture_avoid_conditions": ["into CPI", "if breadth narrows"],
        },
        "intraday": {
            "bias": "LONG_BIAS", "primary_asset": "COPPER",
            "rationale_tags": ["industrial_demand", "weak_dollar"],
            "thesis_invalidator":   "China stimulus disappointment OR inventories spike",
            "posture_avoid_conditions": ["into LME settlement", "if DXY breaks higher"],
        },
        "swing": {
            "bias": "LONG_BIAS", "primary_asset": "EM_equity",
            "rationale_tags": ["dollar_weakness", "growth_friendly"],
            "thesis_invalidator":   "DXY reclaims 20d MA OR EM-credit spreads widen",
            "posture_avoid_conditions": ["into Fed", "if EM CB hawkish surprise"],
        },
    },
    "RANGE_BOUND_CHOP": {
        "scalp": {
            "bias": "NEUTRAL", "primary_asset": "—",
            "rationale_tags": ["no_trend", "fade_extremes"],
            "thesis_invalidator":   "Volume expansion OR ATR breakout",
            "posture_avoid_conditions": ["chasing breakouts", "size up before breakout confirms"],
        },
        "intraday": {
            "bias": "NEUTRAL", "primary_asset": "—",
            "rationale_tags": ["scale_at_levels", "no_signal"],
            "thesis_invalidator":   "Regime composite shifts OR catalyst hits",
            "posture_avoid_conditions": ["initiating high-conviction directional bets"],
        },
        "swing": {
            "bias": "NEUTRAL", "primary_asset": "—",
            "rationale_tags": ["stay_small", "wait_for_signal"],
            "thesis_invalidator":   "Regime transition flagged",
            "posture_avoid_conditions": ["pyramiding into chop"],
        },
    },
    "NO_CLEAN_SCENARIO": {
        "scalp": {
            "bias": "NEUTRAL", "primary_asset": "—",
            "rationale_tags": ["no_clean_signal"],
            "thesis_invalidator":   "Scenario match strength rises above 0.50",
            "posture_avoid_conditions": ["initiating high-conviction bets"],
        },
        "intraday": {
            "bias": "NEUTRAL", "primary_asset": "—",
            "rationale_tags": ["wait_for_setup"],
            "thesis_invalidator":   "Confirmed regime synthesis",
            "posture_avoid_conditions": ["sizing up before data clarifies"],
        },
        "swing": {
            "bias": "NEUTRAL", "primary_asset": "—",
            "rationale_tags": ["no_swing_until_signal"],
            "thesis_invalidator":   "Scenario solidifies",
            "posture_avoid_conditions": ["committing to multi-day positioning"],
        },
    },
}


# Preferred / weak asset shortcuts per scenario (subset of trade_lean).
# Used by generate_trades to populate Stage-5 preferred_assets / weak_assets
# without duplicating the trade_lean dict above.
PREFERRED_ASSETS: dict[str, list[str]] = {
    s["name"]: (s.get("trade_lean") or {}).get("long", []) for s in MACRO_SCENARIOS
}
WEAK_ASSETS: dict[str, list[str]] = {
    s["name"]: (s.get("trade_lean") or {}).get("short", []) for s in MACRO_SCENARIOS
}


def trade_template(scenario_name: str, horizon: str) -> dict:
    """Look up a trade template by scenario name + horizon.

    Horizons: ``scalp`` | ``intraday`` | ``swing``.
    Returns the NO_CLEAN_SCENARIO template if unknown.
    """
    entry = TRADE_MATRIX.get(scenario_name)
    if not entry:
        entry = TRADE_MATRIX["NO_CLEAN_SCENARIO"]
    return dict(entry.get(horizon) or TRADE_MATRIX["NO_CLEAN_SCENARIO"][horizon])
