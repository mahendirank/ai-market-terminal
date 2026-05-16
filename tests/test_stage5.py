"""
test_stage5.py — Unit tests for deterministic trade generation.

Runs without pytest:
    docker exec market-terminal python /app/tests/test_stage5.py

Covers:
  - Each scenario emits a trade dict per timeframe
  - NO_CLEAN_SCENARIO emits WAIT across all timeframes
  - Conflict detection (5 rule types) fires correctly
  - Confidence math: base + conflict + match + vol + consistency
  - volatility_warning + catalyst_risk fire at appropriate bands
  - Composer analyze_stage5 returns the full envelope
  - Latency assert <100ms
"""
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from macro_reasoning_engine import (
    generate_trades, analyze_stage4, analyze_stage5,
    _detect_conflicts, _compute_confidence,
    _volatility_warning, _catalyst_risk,
    CFL_SENTIMENT_VS_REGIME_BULL, CFL_SENTIMENT_VS_REGIME_BEAR,
    CFL_YIELDS_VS_REGIME_RISING, CFL_USD_VS_REGIME,
    DIR_RISING, DIR_FALLING, DIR_FLAT,
    DIR_STRONG, DIR_WEAK, DIR_RANGE,
    VOL_COMPRESSED, VOL_NORMAL, VOL_HIGH, VOL_EXTREME,
    SENT_BULLISH, SENT_BEARISH, SENT_NEUTRAL,
    BIAS_HAWKISH, BIAS_DOVISH, BIAS_NEUTRAL,
    MOVER_FIRST, MOVER_SECOND, MOVER_NONE,
)
from macro_scenarios import (
    MACRO_SCENARIOS, TRADE_MATRIX, trade_template,
    PREFERRED_ASSETS, WEAK_ASSETS,
)


PASS, FAIL = 0, 0
FAILURES: list[str] = []


def _check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  ({detail})")


# ─── Stage-4-shaped builder ────────────────────────────────────────────────
def make_stage4(scenario_name: str, *,
                 yields_dir=DIR_FLAT, yields_delta=0, fed_bias=BIAS_NEUTRAL,
                 usd_dir=DIR_RANGE, usd_delta=0.0,
                 vol_regime=VOL_NORMAL, vix_level=15,
                 sentiment=SENT_NEUTRAL, tilt=0,
                 event_cat=None, event_sev=0, event_mover=MOVER_NONE,
                 catalyst_window=8,
                 internal_consistency=0.7,
                 match_strength=0.85,
                 conviction_baseline=70,
                 regime=None):
    """Build a synthetic stage4 dict that generate_trades can consume."""
    regime = regime or scenario_name   # use scenario_name as regime by default
    dominant = scenario_name.lower()
    ev = ({"category": event_cat, "severity": event_sev,
            "direction": "BEAR_RISK", "age_hours": 2.0}
           if event_cat else None)
    return {
        "yields": {"direction": yields_dir, "us10y_delta_bp": yields_delta,
                    "fed_bias": fed_bias, "us10y_level": 4.4},
        "usd":    {"direction": usd_dir, "dxy_delta_pct": usd_delta,
                    "dxy_level": 100.0},
        "volatility": {"regime": vol_regime, "vix_level": vix_level},
        "sentiment":  {"label": sentiment, "tilt": tilt, "sample_size": 20},
        "events":     {"dominant_event": ev,
                        "first_or_second_mover": event_mover,
                        "catalyst_window_hours": catalyst_window},
        "regime_synthesis": {
            "regime": regime, "dominant_driver": dominant,
            "internal_consistency": internal_consistency,
        },
        "scenario": {
            "name":                scenario_name,
            "match_strength":      match_strength,
            "conviction_baseline": conviction_baseline,
            "trade_lean": next((s.get("trade_lean") for s in MACRO_SCENARIOS
                                  if s["name"] == scenario_name),
                                 {"long": [], "short": [], "avoid": []}),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. Every scenario produces a structured trade dict
# ═══════════════════════════════════════════════════════════════════════════
def test_every_scenario_emits_trades():
    print("\n═══ each scenario emits trades ═══")
    expected = ["TIGHTENING_PANIC","MELT_UP","STAGFLATION_LITE","GROWTH_SCARE",
                 "CARRY_UNWIND","GEOPOLITICAL_RISKOFF","REFLATION",
                 "RANGE_BOUND_CHOP","NO_CLEAN_SCENARIO"]
    for name in expected:
        s4 = make_stage4(name)
        out = generate_trades(s4)
        for tf in ("scalp", "intraday", "swing"):
            trade = out[tf]
            _check(f"{name.lower()}_{tf}_has_bias",
                   "bias" in trade and trade["bias"] in {"LONG_BIAS","SHORT_BIAS","NEUTRAL"},
                   f"got {trade.get('bias')}")
            _check(f"{name.lower()}_{tf}_has_rationale_tags",
                   isinstance(trade["rationale_tags"], list)
                   and len(trade["rationale_tags"]) > 0,
                   f"got {trade.get('rationale_tags')}")
            _check(f"{name.lower()}_{tf}_has_thesis_invalidator",
                   bool(trade["thesis_invalidator"]),
                   f"got {trade.get('thesis_invalidator')!r}")
            _check(f"{name.lower()}_{tf}_has_posture_avoid_conditions",
                   isinstance(trade["posture_avoid_conditions"], list),
                   f"got {trade.get('posture_avoid_conditions')}")
            _check(f"{name.lower()}_{tf}_has_kind_regime_bias",
                   trade.get("kind") == "regime_bias",
                   f"got {trade.get('kind')}")


def test_no_clean_scenario_emits_wait():
    print("\n═══ NO_CLEAN_SCENARIO → WAIT ═══")
    s4 = make_stage4("NO_CLEAN_SCENARIO", match_strength=0.0, conviction_baseline=30)
    out = generate_trades(s4)
    _check("no_clean_scalp_neutral",    out["scalp"]["bias"]    == "NEUTRAL")
    _check("no_clean_intraday_neutral", out["intraday"]["bias"] == "NEUTRAL")
    _check("no_clean_swing_neutral",    out["swing"]["bias"]    == "NEUTRAL")
    _check("no_clean_no_high_conviction",
           out["high_conviction_assets"] == [],
           f"got {out['high_conviction_assets']}")
    _check("no_clean_intent_directional_intelligence",
           out["intent"] == "directional_intelligence" and out["not_for_execution"] is True,
           f"intent={out.get('intent')} not_for_execution={out.get('not_for_execution')}")


# ═══════════════════════════════════════════════════════════════════════════
# 2. Conflict detection — 5 rule types
# ═══════════════════════════════════════════════════════════════════════════
def test_conflict_bullish_under_tightening_panic():
    print("\n═══ conflict: bullish sentiment under TIGHTENING_PANIC ═══")
    s4 = make_stage4("TIGHTENING_PANIC",
                      yields_dir=DIR_RISING, yields_delta=12,
                      usd_dir=DIR_STRONG, usd_delta=0.5,
                      vol_regime=VOL_HIGH, vix_level=22,
                      sentiment=SENT_BULLISH, tilt=0.3,
                      conviction_baseline=80)
    out = generate_trades(s4)
    types = [c["type"] for c in out["conflicts"]]
    _check("bullish_riskoff_conflict_detected",
           CFL_SENTIMENT_VS_REGIME_BULL in types,
           f"got {types}")
    _check("bullish_riskoff_confidence_downgraded",
           out["overall_confidence"] <= 65,
           f"conf={out['overall_confidence']}")


def test_conflict_bearish_under_melt_up():
    print("\n═══ conflict: bearish sentiment under MELT_UP ═══")
    s4 = make_stage4("MELT_UP",
                      yields_dir=DIR_FALLING, yields_delta=-3, fed_bias=BIAS_DOVISH,
                      vol_regime=VOL_COMPRESSED, vix_level=12,
                      sentiment=SENT_BEARISH, tilt=-0.3,
                      regime="MELT_UP",
                      conviction_baseline=75)
    out = generate_trades(s4)
    types = [c["type"] for c in out["conflicts"]]
    _check("bearish_riskon_conflict_detected",
           CFL_SENTIMENT_VS_REGIME_BEAR in types,
           f"got {types}")
    _check("bearish_riskon_confidence_downgraded",
           out["overall_confidence"] <= 60,
           f"conf={out['overall_confidence']}")


def test_conflict_yields_rising_in_risk_on():
    print("\n═══ conflict: yields rising under RISK_ON ═══")
    s4 = make_stage4("MELT_UP",
                      yields_dir=DIR_RISING, yields_delta=8,
                      vol_regime=VOL_COMPRESSED, vix_level=12,
                      sentiment=SENT_BULLISH, tilt=0.3,
                      regime="RISK_ON",
                      conviction_baseline=75)
    out = generate_trades(s4)
    types = [c["type"] for c in out["conflicts"]]
    _check("yields_rising_in_riskon_detected",
           CFL_YIELDS_VS_REGIME_RISING in types,
           f"got {types}")


def test_conflict_usd_strong_in_melt_up():
    print("\n═══ conflict: USD STRONG under MELT_UP ═══")
    s4 = make_stage4("MELT_UP",
                      vol_regime=VOL_NORMAL, vix_level=15,
                      sentiment=SENT_BULLISH, tilt=0.3,
                      usd_dir=DIR_STRONG, usd_delta=0.5,
                      regime="MELT_UP",
                      conviction_baseline=75)
    out = generate_trades(s4)
    types = [c["type"] for c in out["conflicts"]]
    _check("usd_strong_in_melt_up_detected",
           CFL_USD_VS_REGIME in types,
           f"got {types}")


def test_clean_state_no_conflicts():
    print("\n═══ clean state → no conflicts ═══")
    s4 = make_stage4("TIGHTENING_PANIC",
                      yields_dir=DIR_RISING, yields_delta=12,
                      usd_dir=DIR_STRONG, usd_delta=0.5,
                      vol_regime=VOL_HIGH, vix_level=22,
                      sentiment=SENT_BEARISH, tilt=-0.4,
                      conviction_baseline=80,
                      internal_consistency=0.95)
    out = generate_trades(s4)
    _check("clean_state_zero_conflicts", out["conflicts"] == [],
           f"got {out['conflicts']}")
    _check("clean_state_high_confidence", out["overall_confidence"] >= 75,
           f"conf={out['overall_confidence']}")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Confidence math
# ═══════════════════════════════════════════════════════════════════════════
def test_match_strength_penalty():
    print("\n═══ match_strength penalty ═══")
    # Baseline 80, match_strength 0.55 → 2 steps below 0.75 → -10
    s4 = make_stage4("TIGHTENING_PANIC",
                      yields_dir=DIR_RISING, yields_delta=12,
                      usd_dir=DIR_STRONG, usd_delta=0.5,
                      vol_regime=VOL_HIGH, vix_level=22,
                      sentiment=SENT_BEARISH, tilt=-0.4,
                      match_strength=0.55,
                      conviction_baseline=80,
                      internal_consistency=0.7)
    out = generate_trades(s4)
    breakdown = out["confidence_breakdown"]
    _check("match_strength_penalty_applied",
           breakdown["match_strength_penalty"] == -10,
           f"got {breakdown}")


def test_internal_consistency_bonus():
    print("\n═══ internal_consistency bonus ═══")
    s4 = make_stage4("TIGHTENING_PANIC",
                      yields_dir=DIR_RISING, yields_delta=12,
                      usd_dir=DIR_STRONG, usd_delta=0.5,
                      vol_regime=VOL_HIGH, vix_level=22,
                      sentiment=SENT_BEARISH, tilt=-0.4,
                      match_strength=0.92,
                      internal_consistency=0.95,
                      conviction_baseline=80)
    out = generate_trades(s4)
    _check("high_consistency_bonus_applied",
           out["confidence_breakdown"]["internal_consistency_bonus"] == +5,
           f"got {out['confidence_breakdown']}")


def test_vol_extreme_long_lean_penalty():
    print("\n═══ vol extreme + long-lean scenario penalty ═══")
    # MELT_UP has long lean; if vol is EXTREME, penalize -25
    s4 = make_stage4("MELT_UP",
                      vol_regime=VOL_EXTREME, vix_level=35,
                      sentiment=SENT_BULLISH, tilt=0.3,
                      regime="MELT_UP",
                      conviction_baseline=75,
                      internal_consistency=0.7,
                      match_strength=0.8)
    out = generate_trades(s4)
    _check("vol_extreme_long_lean_penalty_applied",
           out["confidence_breakdown"]["vol_alignment_penalty"] == -25,
           f"got {out['confidence_breakdown']}")


def test_confidence_floor_and_ceiling():
    print("\n═══ confidence clamped to [0, 100] ═══")
    # Set up massive negative scenario
    s4 = make_stage4("TIGHTENING_PANIC",
                      sentiment=SENT_BULLISH, tilt=0.5,
                      yields_dir=DIR_FALLING, yields_delta=-12,
                      vol_regime=VOL_NORMAL,
                      match_strength=0.30,
                      conviction_baseline=80,
                      internal_consistency=0.20)
    out = generate_trades(s4)
    _check("confidence_floored_at_zero",
           0 <= out["overall_confidence"] <= 100,
           f"conf={out['overall_confidence']}")


# ═══════════════════════════════════════════════════════════════════════════
# 4. Volatility + catalyst warnings
# ═══════════════════════════════════════════════════════════════════════════
def test_volatility_warning_high():
    print("\n═══ volatility_warning HIGH band ═══")
    out = _volatility_warning({"volatility": {"regime": VOL_HIGH, "vix_level": 22}})
    _check("vol_high_warning_present", out is not None and "HIGH" in out,
           f"got {out!r}")


def test_volatility_warning_extreme():
    print("\n═══ volatility_warning EXTREME band ═══")
    out = _volatility_warning({"volatility": {"regime": VOL_EXTREME, "vix_level": 38}})
    _check("vol_extreme_warning_present", out is not None and "EXTREME" in out,
           f"got {out!r}")


def test_volatility_warning_none_for_normal():
    print("\n═══ no warning for VOL_NORMAL ═══")
    out = _volatility_warning({"volatility": {"regime": VOL_NORMAL, "vix_level": 15}})
    _check("vol_normal_no_warning", out is None, f"got {out!r}")


def test_catalyst_risk_high_severity():
    print("\n═══ catalyst_risk severity 9 ═══")
    stage3 = {"events": {
        "dominant_event": {"category": "MONETARY", "severity": 9, "age_hours": 1.5},
        "first_or_second_mover": MOVER_FIRST,
        "catalyst_window_hours": 72,
    }}
    out = _catalyst_risk(stage3)
    _check("catalyst_risk_sev9_present",
           out is not None and "9" in out,
           f"got {out!r}")


def test_catalyst_risk_low_severity():
    print("\n═══ catalyst_risk severity 4 → None ═══")
    stage3 = {"events": {
        "dominant_event": {"category": "EARNINGS", "severity": 4, "age_hours": 8},
        "first_or_second_mover": MOVER_SECOND,
        "catalyst_window_hours": 8,
    }}
    out = _catalyst_risk(stage3)
    _check("catalyst_low_severity_returns_none", out is None, f"got {out!r}")


# ═══════════════════════════════════════════════════════════════════════════
# 5. Preferred / weak asset wiring
# ═══════════════════════════════════════════════════════════════════════════
def test_preferred_weak_assets_populated():
    print("\n═══ preferred/weak assets populated ═══")
    s4 = make_stage4("TIGHTENING_PANIC",
                      yields_dir=DIR_RISING, yields_delta=12,
                      usd_dir=DIR_STRONG, usd_delta=0.5,
                      vol_regime=VOL_HIGH, vix_level=22,
                      sentiment=SENT_BEARISH, tilt=-0.4)
    out = generate_trades(s4)
    _check("tightening_panic_preferred_includes_dxy",
           "DXY" in out["preferred_assets"],
           f"got {out['preferred_assets']}")
    _check("tightening_panic_weak_includes_ndx",
           "NDX" in out["weak_assets"],
           f"got {out['weak_assets']}")
    _check("assets_to_avoid_populated",
           len(out["assets_to_avoid"]) > 0,
           f"got {out['assets_to_avoid']}")


# ═══════════════════════════════════════════════════════════════════════════
# 6. Stage 5 composer + latency
# ═══════════════════════════════════════════════════════════════════════════
def test_stage5_composer_and_latency():
    print("\n═══ analyze_stage5 ═══")
    snap = {
        "macro_snapshot": {
            "us10y": {"price": 4.50, "change_pct": 2.5},
            "dxy":   {"price": 100.5, "change_pct": 0.5},
            "vix":   {"price": 23.0},
        },
        "sentiment": {"tilt_score": -0.45, "sample_size": 40},
        "events_classified": {
            "by_category": {"MONETARY": {"count": 1, "max_sev": 9}},
            "directional":  {"bull_weighted": 0, "bear_weighted": 9},
            "total_classified": 1,
        },
        "news": {"clusters": []},
    }
    t0 = time.time()
    out = analyze_stage5(snap)
    elapsed_ms = (time.time() - t0) * 1000
    _check("stage5_composes_trades", "trades" in out, f"keys={list(out.keys())}")
    _check("stage5_latency_under_100ms",
           elapsed_ms < 100,
           f"elapsed_ms={elapsed_ms:.1f}")
    _check("stage5_trades_has_required_fields",
           all(k in out["trades"] for k in
                ("intent","not_for_execution","usage_note",
                 "scalp","intraday","swing","high_conviction_assets",
                 "assets_to_avoid","preferred_assets","weak_assets",
                 "volatility_warning","catalyst_risk","conflicts",
                 "overall_confidence","confidence_breakdown")),
           f"got keys={list(out['trades'].keys())}")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("Stage 5 deterministic trade generation tests")
    print("═" * 60)

    for test in (
        test_every_scenario_emits_trades, test_no_clean_scenario_emits_wait,
        test_conflict_bullish_under_tightening_panic,
        test_conflict_bearish_under_melt_up,
        test_conflict_yields_rising_in_risk_on,
        test_conflict_usd_strong_in_melt_up,
        test_clean_state_no_conflicts,
        test_match_strength_penalty,
        test_internal_consistency_bonus,
        test_vol_extreme_long_lean_penalty,
        test_confidence_floor_and_ceiling,
        test_volatility_warning_high,
        test_volatility_warning_extreme,
        test_volatility_warning_none_for_normal,
        test_catalyst_risk_high_severity,
        test_catalyst_risk_low_severity,
        test_preferred_weak_assets_populated,
        test_stage5_composer_and_latency,
    ):
        try:
            test()
        except Exception:
            print(f"  EXCEPTION in {test.__name__}:")
            print(traceback.format_exc())
            global FAIL
            FAIL += 1
            FAILURES.append(f"{test.__name__}: EXCEPTION")

    print()
    print("═" * 60)
    print(f"  {PASS} passed   {FAIL} failed")
    if FAILURES:
        print("  failures:")
        for f in FAILURES:
            print(f"    - {f}")
    print("═" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
