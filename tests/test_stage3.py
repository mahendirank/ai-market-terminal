"""
test_stage3.py — Unit tests for regime synthesis with hierarchy override.

Runs without pytest:
    docker exec market-terminal python /app/tests/test_stage3.py
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
    synthesize_regime, analyze_stage3,
    REGIME_CRISIS, REGIME_TIGHTENING_PANIC, REGIME_STAGFLATION,
    REGIME_INFLATIONARY, REGIME_RISK_OFF, REGIME_GOLDILOCKS,
    REGIME_RISK_ON, REGIME_MIXED,
    LAYER_YIELDS, LAYER_USD, LAYER_VOL, LAYER_SENTIMENT, LAYER_NONE,
    DIR_RISING, DIR_FALLING, DIR_FLAT,
    DIR_STRONG, DIR_WEAK, DIR_RANGE,
    BIAS_HAWKISH, BIAS_DOVISH, BIAS_NEUTRAL,
    VOL_COMPRESSED, VOL_NORMAL, VOL_HIGH, VOL_EXTREME,
    SENT_BULLISH, SENT_BEARISH, SENT_NEUTRAL,
    MOVER_FIRST, MOVER_SECOND, MOVER_NONE,
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


# ─── Stub builders for analyzer outputs ────────────────────────────────────
def y_dict(direction=DIR_FLAT, delta_bp=0.0, fed_bias=BIAS_NEUTRAL, fed_score=0.0):
    return {"us10y_level": 4.4, "us10y_delta_bp": delta_bp,
            "direction": direction, "fed_bias": fed_bias,
            "fed_score": fed_score, "term_premium_signal": "stable"}


def u_dict(direction=DIR_RANGE, delta_pct=0.0):
    return {"dxy_level": 100.0, "dxy_delta_pct": delta_pct,
            "direction": direction,
            "liquidity_stance": {DIR_STRONG: "tightening", DIR_WEAK: "easing",
                                  DIR_RANGE: "neutral"}[direction]}


def v_dict(regime=VOL_NORMAL, level=15.0, score=55):
    return {"vix_level": level, "regime": regime, "score": score}


def s_dict(label=SENT_NEUTRAL, tilt=0.0, n=10):
    return {"tilt": tilt, "label": label, "sample_size": n, "strength": abs(tilt)}


def e_dict(dominant=None, mover=MOVER_NONE, window=8):
    return {"dominant_event": dominant,
            "first_or_second_mover": mover,
            "catalyst_window_hours": window}


# ═══════════════════════════════════════════════════════════════════════════
# TIGHTENING_PANIC
# ═══════════════════════════════════════════════════════════════════════════
def test_tightening_panic():
    print("\n═══ TIGHTENING_PANIC ═══")
    r = synthesize_regime(
        y_dict(DIR_RISING, delta_bp=12, fed_bias=BIAS_HAWKISH),
        u_dict(DIR_STRONG, delta_pct=0.5),
        v_dict(VOL_HIGH, level=22),
        s_dict(SENT_BEARISH, tilt=-0.4),
        e_dict(),
    )
    _check("tightening_panic_regime", r["regime"] == REGIME_TIGHTENING_PANIC,
           f"got {r['regime']}")
    _check("tightening_panic_layer_is_yields", r["override_layer"] == LAYER_YIELDS,
           f"got {r['override_layer']}")
    _check("tightening_panic_driver_yields_rising",
           r["dominant_driver"].startswith("yields_rising"),
           f"got {r['dominant_driver']}")


# ═══════════════════════════════════════════════════════════════════════════
# STAGFLATION
# ═══════════════════════════════════════════════════════════════════════════
def test_stagflation():
    print("\n═══ STAGFLATION ═══")
    # Rising yields + bearish sentiment + high vol, USD not loud
    r = synthesize_regime(
        y_dict(DIR_RISING, delta_bp=10, fed_bias=BIAS_HAWKISH),
        u_dict(DIR_RANGE, delta_pct=0.05),   # USD calm
        v_dict(VOL_HIGH, level=20),
        s_dict(SENT_BEARISH, tilt=-0.4, n=30),
        e_dict(),
    )
    _check("stagflation_regime", r["regime"] == REGIME_STAGFLATION,
           f"got {r['regime']}")
    _check("stagflation_layer_is_yields", r["override_layer"] == LAYER_YIELDS,
           f"got {r['override_layer']}")


# ═══════════════════════════════════════════════════════════════════════════
# INFLATIONARY (controlled)
# ═══════════════════════════════════════════════════════════════════════════
def test_inflationary():
    print("\n═══ INFLATIONARY ═══")
    # Yields rising + Fed hawkish + vol still normal
    r = synthesize_regime(
        y_dict(DIR_RISING, delta_bp=6, fed_bias=BIAS_HAWKISH, fed_score=0.5),
        u_dict(DIR_RANGE, delta_pct=0.05),
        v_dict(VOL_NORMAL, level=16),
        s_dict(SENT_NEUTRAL, tilt=0.0),
        e_dict(),
    )
    _check("inflationary_regime_controlled",
           r["regime"] == REGIME_INFLATIONARY,
           f"got {r['regime']}")


# ═══════════════════════════════════════════════════════════════════════════
# CRISIS — vol extreme
# ═══════════════════════════════════════════════════════════════════════════
def test_crisis():
    print("\n═══ CRISIS ═══")
    r = synthesize_regime(
        y_dict(DIR_FLAT),
        u_dict(DIR_STRONG, delta_pct=0.8),
        v_dict(VOL_EXTREME, level=38),
        s_dict(SENT_BEARISH, tilt=-0.6),
        e_dict(),
    )
    _check("crisis_regime_on_extreme_vol", r["regime"] == REGIME_CRISIS,
           f"got {r['regime']}")
    _check("crisis_layer_is_volatility", r["override_layer"] == LAYER_VOL,
           f"got {r['override_layer']}")


# ═══════════════════════════════════════════════════════════════════════════
# RISK_OFF — vol high + bearish, no yield spike
# ═══════════════════════════════════════════════════════════════════════════
def test_risk_off():
    print("\n═══ RISK_OFF ═══")
    r = synthesize_regime(
        y_dict(DIR_FLAT, delta_bp=1.0),
        u_dict(DIR_RANGE),
        v_dict(VOL_HIGH, level=21),
        s_dict(SENT_BEARISH, tilt=-0.3),
        e_dict(),
    )
    _check("risk_off_regime_no_yield_catalyst", r["regime"] == REGIME_RISK_OFF,
           f"got {r['regime']}")
    _check("risk_off_layer_is_volatility", r["override_layer"] == LAYER_VOL,
           f"got {r['override_layer']}")

    # USD-led risk-off (yields/vol calm)
    r2 = synthesize_regime(
        y_dict(DIR_FLAT, delta_bp=0.5),
        u_dict(DIR_STRONG, delta_pct=0.5),
        v_dict(VOL_NORMAL, level=15),
        s_dict(SENT_BEARISH, tilt=-0.3),
        e_dict(),
    )
    _check("risk_off_regime_usd_led", r2["regime"] == REGIME_RISK_OFF,
           f"got {r2['regime']}")
    _check("risk_off_usd_layer", r2["override_layer"] == LAYER_USD,
           f"got {r2['override_layer']}")


# ═══════════════════════════════════════════════════════════════════════════
# GOLDILOCKS — vol compressed + Fed dovish + bullish
# ═══════════════════════════════════════════════════════════════════════════
def test_goldilocks():
    print("\n═══ GOLDILOCKS ═══")
    r = synthesize_regime(
        y_dict(DIR_FALLING, delta_bp=-3, fed_bias=BIAS_DOVISH),
        u_dict(DIR_WEAK, delta_pct=-0.35),
        v_dict(VOL_COMPRESSED, level=11),
        s_dict(SENT_BULLISH, tilt=0.4, n=30),
        e_dict(),
    )
    _check("goldilocks_regime", r["regime"] == REGIME_GOLDILOCKS,
           f"got {r['regime']}")


# ═══════════════════════════════════════════════════════════════════════════
# RISK_ON — vol low/normal + bullish (no goldilocks conditions)
# ═══════════════════════════════════════════════════════════════════════════
def test_risk_on():
    print("\n═══ RISK_ON ═══")
    r = synthesize_regime(
        y_dict(DIR_FLAT, delta_bp=1.0, fed_bias=BIAS_HAWKISH),   # Fed not dovish
        u_dict(DIR_RANGE),
        v_dict(VOL_NORMAL, level=15),
        s_dict(SENT_BULLISH, tilt=0.25, n=20),
        e_dict(),
    )
    _check("risk_on_regime", r["regime"] == REGIME_RISK_ON,
           f"got {r['regime']}")


# ═══════════════════════════════════════════════════════════════════════════
# MIXED — no rule fires
# ═══════════════════════════════════════════════════════════════════════════
def test_mixed_fallback():
    print("\n═══ MIXED fallback ═══")
    r = synthesize_regime(
        y_dict(DIR_FLAT, delta_bp=0.5),
        u_dict(DIR_RANGE),
        v_dict(VOL_NORMAL, level=15),
        s_dict(SENT_NEUTRAL, tilt=0.0),
        e_dict(),
    )
    _check("mixed_when_no_rule_fires", r["regime"] == REGIME_MIXED,
           f"got {r['regime']}")
    _check("mixed_layer_is_none", r["override_layer"] == LAYER_NONE,
           f"got {r['override_layer']}")


# ═══════════════════════════════════════════════════════════════════════════
# Internal consistency scoring
# ═══════════════════════════════════════════════════════════════════════════
def test_internal_consistency():
    print("\n═══ internal_consistency ═══")
    # All risk-off aligned
    r = synthesize_regime(
        y_dict(DIR_RISING, delta_bp=10),
        u_dict(DIR_STRONG, delta_pct=0.5),
        v_dict(VOL_HIGH, level=23),
        s_dict(SENT_BEARISH, tilt=-0.4),
        e_dict(),
    )
    _check("consistency_high_when_layers_align",
           r["internal_consistency"] >= 0.95,
           f"got {r['internal_consistency']}")

    # Conflicting: bullish sentiment but high vol + strong USD
    r2 = synthesize_regime(
        y_dict(DIR_FLAT),
        u_dict(DIR_STRONG, delta_pct=0.4),
        v_dict(VOL_HIGH, level=22),
        s_dict(SENT_BULLISH, tilt=0.3),
        e_dict(),
    )
    _check("consistency_lower_on_conflict",
           r2["internal_consistency"] < r["internal_consistency"],
           f"got {r2['internal_consistency']} vs {r['internal_consistency']}")


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3 composer + latency
# ═══════════════════════════════════════════════════════════════════════════
def test_stage3_composer_and_latency():
    print("\n═══ analyze_stage3 ═══")
    snap = {
        "macro_snapshot": {
            "us10y": {"price": 4.50, "change_pct": 2.5},
            "dxy":   {"price": 100.5, "change_pct": 0.5},
            "vix":   {"price": 23.0},
        },
        "sentiment": {"tilt_score": -0.45, "sample_size": 40},
        "events_classified": {
            "by_category": {"MONETARY": {"count": 2, "max_sev": 9, "avg_sev": 8}},
            "directional":  {"bull_count": 0, "bear_count": 2,
                              "bull_weighted": 0, "bear_weighted": 16},
            "total_classified": 2,
        },
        "news": {"clusters": []},
    }
    t0 = time.time()
    out = analyze_stage3(snap)
    elapsed_ms = (time.time() - t0) * 1000
    _check("stage3_composes_synthesis",
           "regime_synthesis" in out,
           f"got keys={list(out.keys())}")
    _check("stage3_latency_under_100ms_cold",
           elapsed_ms < 100,
           f"elapsed_ms={elapsed_ms:.1f}")
    _check("stage3_yields_tightening_panic_on_synthetic_input",
           out["regime_synthesis"]["regime"] == REGIME_TIGHTENING_PANIC,
           f"got {out['regime_synthesis']['regime']}")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("Stage 3 regime synthesis tests")
    print("═" * 60)

    for test in (test_tightening_panic, test_stagflation, test_inflationary,
                  test_crisis, test_risk_off, test_goldilocks, test_risk_on,
                  test_mixed_fallback, test_internal_consistency,
                  test_stage3_composer_and_latency):
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
