"""
test_stage4.py — Unit tests for scenario library + matcher.

Runs without pytest:
    docker exec market-terminal python /app/tests/test_stage4.py
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
    match_scenario, analyze_stage4, analyze_stage3,
    DIR_RISING, DIR_FALLING, DIR_FLAT,
    DIR_STRONG, DIR_WEAK, DIR_RANGE,
    BIAS_HAWKISH, BIAS_DOVISH, BIAS_NEUTRAL,
    VOL_COMPRESSED, VOL_NORMAL, VOL_HIGH, VOL_EXTREME,
    SENT_BULLISH, SENT_BEARISH, SENT_NEUTRAL,
)
from macro_scenarios import (
    MACRO_SCENARIOS, NO_CLEAN_SCENARIO, list_scenarios,
    MATCH_THRESHOLD_MIN, MATCH_THRESHOLD_GOOD,
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


# ─── Stage3 stub builder (fakes upstream analyzer output) ──────────────────
def stage3(yields_dir=DIR_FLAT, yields_delta=0, fed_bias=BIAS_NEUTRAL,
            usd_dir=DIR_RANGE, usd_delta=0,
            vol_regime=VOL_NORMAL,
            sentiment=SENT_NEUTRAL, tilt=0,
            event_cat="UNKNOWN", event_sev=0,
            regime="MIXED"):
    return {
        "yields": {"direction": yields_dir, "us10y_delta_bp": yields_delta,
                    "fed_bias": fed_bias},
        "usd":    {"direction": usd_dir, "dxy_delta_pct": usd_delta},
        "volatility": {"regime": vol_regime, "vix_level": 16},
        "sentiment": {"label": sentiment, "tilt": tilt, "sample_size": 20},
        "events": {"dominant_event": ({"category": event_cat, "severity": event_sev,
                                         "direction": "BEAR_RISK"}
                                        if event_sev > 0 else None)},
        "regime_synthesis": {"regime": regime},
    }


# ═══════════════════════════════════════════════════════════════════════════
# Each scenario matches its trigger state
# ═══════════════════════════════════════════════════════════════════════════
def test_tightening_panic_matches():
    print("\n═══ TIGHTENING_PANIC match ═══")
    s = stage3(yields_dir=DIR_RISING, yields_delta=12,
                usd_dir=DIR_STRONG, usd_delta=0.5,
                vol_regime=VOL_HIGH,
                sentiment=SENT_BEARISH, tilt=-0.4,
                fed_bias=BIAS_HAWKISH)
    r = match_scenario(s)
    _check("tightening_panic_name", r["name"] == "TIGHTENING_PANIC",
           f"got {r['name']} strength={r['match_strength']}")
    _check("tightening_panic_full_match", r["match_strength"] >= 0.75,
           f"strength={r['match_strength']}")


def test_melt_up_matches():
    print("\n═══ MELT_UP match ═══")
    s = stage3(yields_dir=DIR_FALLING, yields_delta=-3, fed_bias=BIAS_DOVISH,
                usd_dir=DIR_WEAK, usd_delta=-0.4,
                vol_regime=VOL_COMPRESSED,
                sentiment=SENT_BULLISH, tilt=0.4,
                regime="GOLDILOCKS")
    r = match_scenario(s)
    _check("melt_up_name", r["name"] == "MELT_UP",
           f"got {r['name']} strength={r['match_strength']}")
    _check("melt_up_strong_match", r["match_strength"] >= MATCH_THRESHOLD_GOOD,
           f"strength={r['match_strength']}")


def test_stagflation_lite_matches():
    print("\n═══ STAGFLATION_LITE match ═══")
    s = stage3(yields_dir=DIR_RISING, yields_delta=8,
                vol_regime=VOL_HIGH,
                sentiment=SENT_BEARISH, tilt=-0.4)
    r = match_scenario(s)
    _check("stagflation_lite_name", r["name"] == "STAGFLATION_LITE",
           f"got {r['name']} strength={r['match_strength']}")


def test_growth_scare_matches():
    print("\n═══ GROWTH_SCARE match ═══")
    s = stage3(yields_dir=DIR_FALLING, yields_delta=-10,
                vol_regime=VOL_HIGH,
                sentiment=SENT_BEARISH, tilt=-0.4,
                usd_dir=DIR_STRONG, usd_delta=0.4)
    r = match_scenario(s)
    _check("growth_scare_name", r["name"] == "GROWTH_SCARE",
           f"got {r['name']} strength={r['match_strength']}")


def test_carry_unwind_matches():
    print("\n═══ CARRY_UNWIND match ═══")
    s = stage3(usd_dir=DIR_WEAK, usd_delta=-0.7,
                vol_regime=VOL_HIGH,
                sentiment=SENT_BEARISH, tilt=-0.4)
    r = match_scenario(s)
    _check("carry_unwind_name", r["name"] == "CARRY_UNWIND",
           f"got {r['name']} strength={r['match_strength']}")


def test_geopolitical_riskoff_matches():
    print("\n═══ GEOPOLITICAL_RISKOFF match ═══")
    s = stage3(vol_regime=VOL_EXTREME,
                event_cat="GEOPOLITICAL", event_sev=9,
                sentiment=SENT_BEARISH, tilt=-0.5)
    r = match_scenario(s)
    _check("geopolitical_riskoff_name", r["name"] == "GEOPOLITICAL_RISKOFF",
           f"got {r['name']} strength={r['match_strength']}")


def test_reflation_matches():
    print("\n═══ REFLATION match ═══")
    s = stage3(yields_dir=DIR_RISING, yields_delta=5,
                fed_bias=BIAS_NEUTRAL,
                vol_regime=VOL_NORMAL,
                sentiment=SENT_BULLISH, tilt=0.3)
    r = match_scenario(s)
    _check("reflation_name", r["name"] == "REFLATION",
           f"got {r['name']} strength={r['match_strength']}")


def test_range_bound_chop_matches():
    print("\n═══ RANGE_BOUND_CHOP match ═══")
    s = stage3(yields_dir=DIR_FLAT, usd_dir=DIR_RANGE,
                vol_regime=VOL_NORMAL,
                sentiment=SENT_NEUTRAL)
    r = match_scenario(s)
    _check("range_bound_chop_name", r["name"] == "RANGE_BOUND_CHOP",
           f"got {r['name']} strength={r['match_strength']}")


# ═══════════════════════════════════════════════════════════════════════════
# NO_CLEAN_SCENARIO fallback
# ═══════════════════════════════════════════════════════════════════════════
def test_no_clean_scenario_fallback():
    print("\n═══ NO_CLEAN_SCENARIO fallback ═══")
    # Conflicting signals: rising yields + dovish Fed + extreme vol + bullish
    s = stage3(yields_dir=DIR_RISING, yields_delta=15, fed_bias=BIAS_DOVISH,
                vol_regime=VOL_EXTREME,
                sentiment=SENT_BULLISH, tilt=0.3,
                usd_dir=DIR_RANGE)
    r = match_scenario(s)
    # Could either no-clean OR match something weakly; verify either it's
    # no-clean OR strength below GOOD threshold.
    _check("no_clean_or_weak_on_conflict",
           r["name"] == "NO_CLEAN_SCENARIO" or r["match_strength"] < MATCH_THRESHOLD_GOOD,
           f"got name={r['name']} strength={r['match_strength']}")


def test_empty_state_returns_no_clean():
    print("\n═══ empty stage3 → NO_CLEAN ═══")
    r = match_scenario({})
    _check("empty_state_no_clean", r["name"] == "NO_CLEAN_SCENARIO",
           f"got {r['name']}")
    _check("empty_state_avoid_high_conviction",
           "high_conviction_trades" in (r["trade_lean"]["avoid"] or []),
           f"got trade_lean.avoid={r['trade_lean'].get('avoid')}")


# ═══════════════════════════════════════════════════════════════════════════
# Match envelope fields
# ═══════════════════════════════════════════════════════════════════════════
def test_envelope_shape():
    print("\n═══ envelope fields ═══")
    s = stage3(yields_dir=DIR_RISING, yields_delta=12,
                usd_dir=DIR_STRONG, usd_delta=0.5,
                vol_regime=VOL_HIGH,
                sentiment=SENT_BEARISH, tilt=-0.4)
    r = match_scenario(s)
    required = {"name","description","match_strength","matched_conditions",
                "failed_conditions","trade_lean","analog_keywords",
                "horizon_bias","conviction_baseline"}
    _check("envelope_has_all_required_fields",
           required.issubset(set(r.keys())),
           f"missing={required - set(r.keys())}")
    _check("trade_lean_has_long_short_avoid",
           all(k in r["trade_lean"] for k in ("long","short","avoid")),
           f"got trade_lean={r['trade_lean']}")
    _check("conviction_baseline_is_int",
           isinstance(r["conviction_baseline"], int),
           f"got {type(r['conviction_baseline'])}")


# ═══════════════════════════════════════════════════════════════════════════
# Scenario library introspection
# ═══════════════════════════════════════════════════════════════════════════
def test_scenario_library_introspection():
    print("\n═══ library introspection ═══")
    names = list_scenarios()
    _check("library_has_8_scenarios", len(names) == 8,
           f"got {len(names)} ({names})")
    expected = {"TIGHTENING_PANIC","MELT_UP","STAGFLATION_LITE","GROWTH_SCARE",
                 "CARRY_UNWIND","GEOPOLITICAL_RISKOFF","REFLATION","RANGE_BOUND_CHOP"}
    _check("library_has_expected_names",
           set(names) == expected,
           f"missing={expected - set(names)} extra={set(names) - expected}")


# ═══════════════════════════════════════════════════════════════════════════
# Stage 4 composer + latency
# ═══════════════════════════════════════════════════════════════════════════
def test_stage4_composer_and_latency():
    print("\n═══ analyze_stage4 ═══")
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
    out = analyze_stage4(snap)
    elapsed_ms = (time.time() - t0) * 1000
    _check("stage4_composes_scenario", "scenario" in out, f"keys={list(out.keys())}")
    _check("stage4_latency_under_100ms", elapsed_ms < 100,
           f"elapsed_ms={elapsed_ms:.1f}")
    _check("stage4_picks_tightening_panic_on_synthetic",
           out["scenario"]["name"] == "TIGHTENING_PANIC",
           f"got {out['scenario']['name']}")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("Stage 4 scenario library + matcher tests")
    print("═" * 60)

    for test in (test_tightening_panic_matches, test_melt_up_matches,
                  test_stagflation_lite_matches, test_growth_scare_matches,
                  test_carry_unwind_matches, test_geopolitical_riskoff_matches,
                  test_reflation_matches, test_range_bound_chop_matches,
                  test_no_clean_scenario_fallback, test_empty_state_returns_no_clean,
                  test_envelope_shape, test_scenario_library_introspection,
                  test_stage4_composer_and_latency):
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
