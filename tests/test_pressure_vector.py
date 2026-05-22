"""
test_pressure_vector.py — Unit tests for the pressure-vector engine.

Covers the central-bank overlay, the nine-force vector, dominant-driver
selection, the net-risk vector, market contagion, plus the fail-soft,
cache and async-entrypoint contracts.

Runs without pytest:
    docker exec market-terminal python /app/tests/test_pressure_vector.py
"""
import os
import sys
import asyncio
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pressure_vector as pv


PASS, FAIL = 0, 0
FAILURES: list[str] = []

# Moderate macro — non-saturated, so the CB overlay is visibly measurable.
MACRO = {
    "us10y": {"change_pct": 1.4}, "dxy": {"change_pct": 0.18},
    "vix":   {"change_pct": 2.7}, "gold": {"change_pct": 0.6},
    "oil":   {"change_pct": 0.8},
}


def _check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  ({detail})")


# ═══════════════════════════════════════════════════════════════════════════
# Output shape
# ═══════════════════════════════════════════════════════════════════════════
def test_output_shape():
    print("\n═══ output shape ═══")
    out = pv.compute_pressure_vector(MACRO, events_tilt=-0.2, cb_action=-0.5)
    required = {"pressures", "base_pressures", "cb_pressure", "vector",
                "dominant_driver", "net_risk", "contagion", "degraded"}
    _check("has_all_keys", required.issubset(out), f"missing={required - set(out)}")
    _check("vector_has_9_forces", len(out["vector"]) == 9, f"got {len(out['vector'])}")
    _check("central_bank_in_vector", "central_bank" in out["vector"], "")
    _check("not_degraded", out["degraded"] is False, f"got {out['degraded']}")


# ═══════════════════════════════════════════════════════════════════════════
# Central-bank overlay
# ═══════════════════════════════════════════════════════════════════════════
def test_cb_zero_equals_base():
    print("\n═══ cb_action=0 → pressures == base ═══")
    out = pv.compute_pressure_vector(MACRO, cb_action=0.0)
    _check("zero_cb_no_overlay", out["pressures"] == out["base_pressures"],
           "cb=0 still shifted pressures")
    _check("zero_cb_no_contribution",
           all(abs(v) < 1e-9 for v in out["cb_pressure"].values()),
           f"got {out['cb_pressure']}")


def test_cb_hawkish_pressures_equities_down():
    print("\n═══ hawkish CB → equities + liquidity pressured down ═══")
    out = pv.compute_pressure_vector(MACRO, cb_action=-0.6)
    _check("hawkish_drags_equities", out["cb_pressure"]["equities"] < 0,
           f"got {out['cb_pressure']['equities']}")
    _check("hawkish_drains_liquidity", out["cb_pressure"]["liquidity"] < 0,
           f"got {out['cb_pressure']['liquidity']}")


def test_cb_dovish_lifts_equities():
    print("\n═══ dovish CB → equities lifted ═══")
    out = pv.compute_pressure_vector(MACRO, cb_action=0.6)
    _check("dovish_lifts_equities", out["cb_pressure"]["equities"] > 0,
           f"got {out['cb_pressure']['equities']}")


def test_central_bank_vector():
    print("\n═══ central_bank force tracked in the vector ═══")
    out = pv.compute_pressure_vector(MACRO, cb_action=-0.7)
    cbv = out["vector"]["central_bank"]
    _check("cb_direction_hawkish", cbv["direction"] == -1, f"got {cbv['direction']}")
    _check("cb_magnitude", abs(cbv["magnitude"] - 0.7) < 1e-6, f"got {cbv['magnitude']}")


# ═══════════════════════════════════════════════════════════════════════════
# Dominant driver + net risk
# ═══════════════════════════════════════════════════════════════════════════
def test_dominant_driver():
    print("\n═══ dominant driver = the hardest-pushing force ═══")
    out = pv.compute_pressure_vector({"vix": {"change_pct": 15.0}})
    _check("vol_shock_is_dominant", out["dominant_driver"]["node"] == "volatility",
           f"got {out['dominant_driver']}")
    _check("dominant_not_derived",
           out["dominant_driver"]["node"] not in ("equities", "liquidity"), "")


def test_net_risk_off():
    print("\n═══ net risk — risk-off shock ═══")
    out = pv.compute_pressure_vector({"vix": {"change_pct": 12.0},
                                      "us10y": {"change_pct": 3.0}})
    _check("net_risk_off_label", out["net_risk"]["label"] == "risk-off",
           f"got {out['net_risk']}")
    _check("net_risk_off_direction", out["net_risk"]["direction"] == -1,
           f"got {out['net_risk']}")


def test_net_risk_on():
    print("\n═══ net risk — risk-on shock ═══")
    out = pv.compute_pressure_vector({"vix": {"change_pct": -9.0}},
                                     events_tilt=0.7, cb_action=0.6)
    _check("net_risk_on_label", out["net_risk"]["label"] == "risk-on",
           f"got {out['net_risk']}")


# ═══════════════════════════════════════════════════════════════════════════
# Market contagion
# ═══════════════════════════════════════════════════════════════════════════
def test_contagion_spreads():
    print("\n═══ contagion — a real shock spreads across markets ═══")
    out = pv.compute_pressure_vector({"vix": {"change_pct": 12.0},
                                      "us10y": {"change_pct": 3.0}})
    cont = out["contagion"]
    _check("contagion_has_origin", cont["origin"] is not None, f"got {cont}")
    _check("contagion_breadth_positive", cont["breadth"] > 0, f"got {cont['breadth']}")
    _check("contagion_severity_in_range", 0.0 <= cont["severity"] <= 1.0,
           f"got {cont['severity']}")
    _check("affected_carry_asset_class",
           all("asset_class" in a for a in cont["affected"]), f"got {cont['affected']}")


def test_contagion_flat_tape():
    print("\n═══ contagion — flat tape → no contagion ═══")
    out = pv.compute_pressure_vector({})
    _check("flat_no_origin", out["contagion"]["origin"] is None,
           f"got {out['contagion']}")
    _check("flat_zero_breadth", out["contagion"]["breadth"] == 0, "")


# ═══════════════════════════════════════════════════════════════════════════
# Fail-soft · cache · async · determinism
# ═══════════════════════════════════════════════════════════════════════════
def test_fail_soft():
    print("\n═══ fail-soft on bad input ═══")
    out = pv.compute_pressure_vector(["not", "a", "dict"])
    _check("fail_soft_returns_dict", isinstance(out, dict), f"got {type(out)}")
    _check("fail_soft_degraded", out.get("degraded") is True, f"got {out.get('degraded')}")
    _check("fail_soft_full_shape",
           {"vector", "net_risk", "contagion", "dominant_driver"}.issubset(out),
           f"got {set(out)}")


def test_cache_hit():
    print("\n═══ cache — identical inputs hit the cache ═══")
    pv.clear_cache()
    a = pv.compute_pressure_vector(MACRO, events_tilt=0.1, cb_action=-0.3)
    b = pv.compute_pressure_vector(MACRO, events_tilt=0.1, cb_action=-0.3)
    _check("cache_hit_equal", a == b, "cached result differs")
    s = pv.cache_stats()
    _check("cache_recorded_hit", s["hits"] >= 1, f"got {s}")


def test_async_matches_sync():
    print("\n═══ async entrypoint matches sync ═══")
    pv.clear_cache()
    sync = pv.compute_pressure_vector(MACRO, events_tilt=-0.2, cb_action=-0.5)
    asy = asyncio.run(pv.compute_pressure_vector_async(
        MACRO, events_tilt=-0.2, cb_action=-0.5))
    _check("async_equals_sync", sync == asy, "async result differs")


def test_deterministic():
    print("\n═══ deterministic ═══")
    pv.clear_cache()
    a = pv.compute_pressure_vector(MACRO, cb_action=-0.4, use_cache=False)
    b = pv.compute_pressure_vector(MACRO, cb_action=-0.4, use_cache=False)
    _check("same_input_same_output", a == b, "non-deterministic!")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("pressure_vector — unit tests")
    print("═" * 60)
    tests = (
        test_output_shape,
        test_cb_zero_equals_base, test_cb_hawkish_pressures_equities_down,
        test_cb_dovish_lifts_equities, test_central_bank_vector,
        test_dominant_driver, test_net_risk_off, test_net_risk_on,
        test_contagion_spreads, test_contagion_flat_tape,
        test_fail_soft, test_cache_hit, test_async_matches_sync,
        test_deterministic,
    )
    for test in tests:
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
