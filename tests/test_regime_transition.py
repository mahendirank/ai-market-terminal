"""
test_regime_transition.py — Unit tests for regime transition scoring.

Covers signature integrity, fit scoring, regime identification for all
five regimes, the INDETERMINATE weak-signal path, and transition
detection (current vs causally-projected regime).

Runs without pytest:
    docker exec market-terminal python /app/tests/test_regime_transition.py
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import regime_transition_engine as rt


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


# ═══════════════════════════════════════════════════════════════════════════
# Signature integrity
# ═══════════════════════════════════════════════════════════════════════════
def test_signatures():
    print("\n═══ regime signatures ═══")
    _check("five_regimes", len(rt.REGIMES) == 5, f"got {len(rt.REGIMES)}")
    expected = {"RISK_ON", "RISK_OFF", "PANIC", "LIQUIDITY_EXPANSION", "TIGHTENING"}
    _check("expected_regimes", set(rt.REGIMES) == expected,
           f"missing={expected - set(rt.REGIMES)}")
    in_range = all(-1.0 <= v <= 1.0
                   for sig in rt.REGIME_SIGNATURES.values()
                   for v in sig.values())
    _check("signature_values_in_range", in_range, "a signature value out of [-1,1]")


# ═══════════════════════════════════════════════════════════════════════════
# Fit scoring
# ═══════════════════════════════════════════════════════════════════════════
def test_fit_scoring():
    print("\n═══ score_regime_fit ═══")
    sig = rt.REGIME_SIGNATURES[rt.RISK_ON]
    # State equal to the signature → strong positive fit
    aligned = rt.score_regime_fit(dict(sig), sig)
    _check("aligned_fit_positive", aligned > 0.3, f"got {aligned}")
    # Mirror-image state → negative fit
    mirror = rt.score_regime_fit({k: -v for k, v in sig.items()}, sig)
    _check("mirror_fit_negative", mirror < -0.3, f"got {mirror}")
    # Empty state → 0
    _check("empty_state_zero_fit", rt.score_regime_fit({}, sig) == 0.0, "")
    _check("score_all_returns_five", len(rt.score_all_regimes(dict(sig))) == 5, "")


# ═══════════════════════════════════════════════════════════════════════════
# Regime identification — one test per regime
# ═══════════════════════════════════════════════════════════════════════════
def _regime_of(regime_key):
    """Feed a regime's own signature in as state → it should identify itself."""
    sig = rt.REGIME_SIGNATURES[regime_key]
    out = rt.compute_transition(dict(sig), dict(sig))
    return out["current_regime"]


def test_identifies_each_regime():
    print("\n═══ identify each regime from its signature ═══")
    for r in rt.REGIMES:
        got = _regime_of(r)
        _check(f"identifies_{r}", got == r, f"got {got}")


def test_stable_when_no_pressure():
    print("\n═══ stable: current == projected ═══")
    sig = rt.REGIME_SIGNATURES[rt.RISK_OFF]
    out = rt.compute_transition(dict(sig), dict(sig))
    _check("not_transitioning", out["transitioning"] is False, f"got {out}")
    _check("high_stability", out["stability"] >= 0.85, f"got {out['stability']}")
    _check("direction_stable", out["direction"] == "stable", f"got {out['direction']}")
    _check("transition_score_zero", out["transition_score"] == 0.0,
           f"got {out['transition_score']}")


# ═══════════════════════════════════════════════════════════════════════════
# Weak signal → INDETERMINATE
# ═══════════════════════════════════════════════════════════════════════════
def test_weak_signal_indeterminate():
    print("\n═══ weak/mixed state → INDETERMINATE ═══")
    out = rt.compute_transition({}, {})
    _check("weak_flag_true", out["weak_signal"] is True, f"got {out}")
    _check("current_indeterminate", out["current_regime"] == rt.INDETERMINATE,
           f"got {out['current_regime']}")
    _check("projected_indeterminate", out["projected_regime"] == rt.INDETERMINATE,
           f"got {out['projected_regime']}")
    _check("not_transitioning_when_weak", out["transitioning"] is False, "")


def test_tiny_state_is_weak():
    print("\n═══ near-zero state is weak ═══")
    tiny = {"equities": 0.03, "volatility": -0.02}
    out = rt.compute_transition(tiny, tiny)
    _check("tiny_state_weak", out["weak_signal"] is True, f"got {out}")


# ═══════════════════════════════════════════════════════════════════════════
# Transition detection
# ═══════════════════════════════════════════════════════════════════════════
def test_transition_detected():
    print("\n═══ transition: RISK_ON observed → RISK_OFF projected ═══")
    risk_on  = dict(rt.REGIME_SIGNATURES[rt.RISK_ON])
    risk_off = dict(rt.REGIME_SIGNATURES[rt.RISK_OFF])
    out = rt.compute_transition(risk_on, risk_off)
    _check("current_is_risk_on", out["current_regime"] == rt.RISK_ON,
           f"got {out['current_regime']}")
    _check("projected_is_risk_off", out["projected_regime"] == rt.RISK_OFF,
           f"got {out['projected_regime']}")
    _check("transitioning_true", out["transitioning"] is True, f"got {out}")
    _check("transition_score_positive", out["transition_score"] > 0,
           f"got {out['transition_score']}")
    _check("direction_deteriorating", out["direction"] == "deteriorating",
           f"got {out['direction']}")


def test_transition_improving():
    print("\n═══ transition: RISK_OFF observed → RISK_ON projected ═══")
    out = rt.compute_transition(dict(rt.REGIME_SIGNATURES[rt.RISK_OFF]),
                                dict(rt.REGIME_SIGNATURES[rt.RISK_ON]))
    _check("direction_improving", out["direction"] == "improving",
           f"got {out['direction']}")


def test_stability_inverse_of_transition():
    print("\n═══ stability = 1 - transition_score ═══")
    out = rt.compute_transition(dict(rt.REGIME_SIGNATURES[rt.RISK_ON]),
                                dict(rt.REGIME_SIGNATURES[rt.PANIC]))
    expected = round(1.0 - out["transition_score"], 4)
    _check("stability_complements_transition",
           abs(out["stability"] - expected) < 1e-6,
           f"stability={out['stability']} transition={out['transition_score']}")


def test_output_shape():
    print("\n═══ output envelope ═══")
    out = rt.compute_transition(dict(rt.REGIME_SIGNATURES[rt.TIGHTENING]),
                                dict(rt.REGIME_SIGNATURES[rt.TIGHTENING]),
                                regime_engine_hint="TIGHTENING")
    required = {"current_regime", "current_fit", "projected_regime",
                "projected_fit", "transitioning", "transition_score",
                "stability", "direction", "weak_signal", "regime_scores",
                "regime_engine_hint", "note"}
    _check("has_all_keys", required.issubset(out), f"missing={required - set(out)}")
    _check("regime_scores_five", len(out["regime_scores"]) == 5, "")
    _check("hint_passed_through", out["regime_engine_hint"] == "TIGHTENING", "")
    _check("note_is_string", isinstance(out["note"], str) and out["note"], "")


def test_deterministic():
    print("\n═══ deterministic ═══")
    a = dict(rt.REGIME_SIGNATURES[rt.RISK_ON])
    b = dict(rt.REGIME_SIGNATURES[rt.PANIC])
    _check("repeatable", rt.compute_transition(a, b) == rt.compute_transition(a, b),
           "non-deterministic!")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("regime_transition_engine — unit tests")
    print("═" * 60)

    tests = (
        test_signatures, test_fit_scoring, test_identifies_each_regime,
        test_stable_when_no_pressure, test_weak_signal_indeterminate,
        test_tiny_state_is_weak, test_transition_detected,
        test_transition_improving, test_stability_inverse_of_transition,
        test_output_shape, test_deterministic,
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
