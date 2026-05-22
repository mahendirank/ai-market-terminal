"""
test_contradiction_engine.py — Unit tests for the contradiction engine.

Covers macro-layer aggregation (from event_graph), the three cross-layer
checks (regime-vs-pressure, central-bank-vs-market, pressure-vs-observed),
the contradiction_score / consistency aggregation, plus the fail-soft,
cache and async-entrypoint contracts.

Runs without pytest:
    docker exec market-terminal python /app/tests/test_contradiction_engine.py
"""
import os
import sys
import asyncio
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import contradiction_engine as ce


PASS, FAIL = 0, 0
FAILURES: list[str] = []

# A risk-off macro — yields up, VIX up → strongly negative equity pressure.
RISK_OFF = {"us10y": {"change_pct": 3.0}, "vix": {"change_pct": 12.0}}


def _check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  ({detail})")


def _layers(out):
    return {c["layer"] for c in out["contradictions"]}


# ═══════════════════════════════════════════════════════════════════════════
# Output shape
# ═══════════════════════════════════════════════════════════════════════════
def test_output_shape():
    print("\n═══ output shape ═══")
    out = ce.assess_contradictions(RISK_OFF, equities_observed=0.5)
    required = {"contradictions", "count", "contradiction_score",
                "consistency", "dominant_contradiction", "degraded"}
    _check("has_all_keys", required.issubset(out), f"missing={required - set(out)}")
    _check("score_in_range", 0.0 <= out["contradiction_score"] <= 1.0,
           f"got {out['contradiction_score']}")
    _check("consistency_is_inverse",
           abs(out["consistency"] - (1.0 - out["contradiction_score"])) < 1e-6,
           f"score={out['contradiction_score']} consistency={out['consistency']}")
    _check("count_matches_list", out["count"] == len(out["contradictions"]), "")


# ═══════════════════════════════════════════════════════════════════════════
# Layers
# ═══════════════════════════════════════════════════════════════════════════
def test_macro_layer_aggregated():
    print("\n═══ macro-layer contradictions aggregated from event_graph ═══")
    # equities bid (observed +0.6) while VIX rises → event_graph macro contradiction
    out = ce.assess_contradictions({"vix": {"change_pct": 12.0}},
                                   equities_observed=0.6)
    _check("macro_layer_present", "macro" in _layers(out), f"got {_layers(out)}")
    _check("every_hit_tagged_layer",
           all("layer" in c for c in out["contradictions"]), "")


def test_regime_vs_pressure():
    print("\n═══ cross-layer: regime read vs causal pressure ═══")
    # regime says RISK_ON, but the macro pressure is firmly risk-off
    out = ce.assess_contradictions(
        RISK_OFF, regime_transition={"projected_regime": "RISK_ON"})
    _check("regime_layer_fires", "regime" in _layers(out), f"got {_layers(out)}")


def test_central_bank_vs_market():
    print("\n═══ cross-layer: dovish CB vs risk-off market ═══")
    # dovish CB while the equity pressure stays firmly risk-off
    out = ce.assess_contradictions(RISK_OFF, cb_action=0.6)
    _check("cb_layer_fires", "central_bank" in _layers(out), f"got {_layers(out)}")


def test_pressure_vs_observed():
    print("\n═══ cross-layer: propagated pressure vs observed tape ═══")
    # risk-off pressure but the observed equity tape is firmly bid
    out = ce.assess_contradictions(RISK_OFF, equities_observed=0.6)
    _check("observed_layer_fires", "observed" in _layers(out), f"got {_layers(out)}")


# ═══════════════════════════════════════════════════════════════════════════
# Scoring
# ═══════════════════════════════════════════════════════════════════════════
def test_clean_state_no_contradictions():
    print("\n═══ clean state → no contradictions, full consistency ═══")
    out = ce.assess_contradictions({})
    _check("clean_count_zero", out["count"] == 0, f"got {out['count']}")
    _check("clean_score_zero", out["contradiction_score"] == 0.0,
           f"got {out['contradiction_score']}")
    _check("clean_consistency_one", out["consistency"] == 1.0,
           f"got {out['consistency']}")
    _check("clean_no_dominant", out["dominant_contradiction"] is None, "")


def test_dominant_is_highest_severity():
    print("\n═══ dominant_contradiction = highest severity ═══")
    out = ce.assess_contradictions(RISK_OFF, equities_observed=0.6,
                                   regime_transition={"projected_regime": "RISK_ON"})
    hits = out["contradictions"]
    if len(hits) >= 2:
        _check("sorted_by_severity",
               all(hits[i]["severity"] >= hits[i + 1]["severity"]
                   for i in range(len(hits) - 1)), f"got {hits}")
        _check("dominant_is_first",
               out["dominant_contradiction"] == hits[0], "")
    else:
        _check("enough_contradictions_to_rank", False,
               f"expected >=2, got {len(hits)}")


# ═══════════════════════════════════════════════════════════════════════════
# Fail-soft · cache · async · determinism
# ═══════════════════════════════════════════════════════════════════════════
def test_fail_soft():
    print("\n═══ fail-soft on bad input ═══")
    out = ce.assess_contradictions(["not", "a", "dict"])
    _check("fail_soft_returns_dict", isinstance(out, dict), f"got {type(out)}")
    _check("fail_soft_degraded", out.get("degraded") is True, f"got {out.get('degraded')}")
    _check("fail_soft_neutral_consistency", out.get("consistency") == 1.0,
           f"got {out.get('consistency')}")


def test_cache_hit():
    print("\n═══ cache — identical inputs hit the cache ═══")
    ce.clear_cache()
    a = ce.assess_contradictions(RISK_OFF, cb_action=0.5, equities_observed=0.6)
    b = ce.assess_contradictions(RISK_OFF, cb_action=0.5, equities_observed=0.6)
    _check("cache_hit_equal", a == b, "cached result differs")
    _check("cache_recorded_hit", ce.cache_stats()["hits"] >= 1,
           f"got {ce.cache_stats()}")


def test_async_matches_sync():
    print("\n═══ async entrypoint matches sync ═══")
    ce.clear_cache()
    sync = ce.assess_contradictions(RISK_OFF, cb_action=0.5, equities_observed=0.6)
    asy = asyncio.run(ce.assess_contradictions_async(
        RISK_OFF, cb_action=0.5, equities_observed=0.6))
    _check("async_equals_sync", sync == asy, "async result differs")


def test_deterministic():
    print("\n═══ deterministic ═══")
    a = ce.assess_contradictions(RISK_OFF, equities_observed=0.6, use_cache=False)
    b = ce.assess_contradictions(RISK_OFF, equities_observed=0.6, use_cache=False)
    _check("same_input_same_output", a == b, "non-deterministic!")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("contradiction_engine — unit tests")
    print("═" * 60)
    tests = (
        test_output_shape,
        test_macro_layer_aggregated, test_regime_vs_pressure,
        test_central_bank_vs_market, test_pressure_vs_observed,
        test_clean_state_no_contradictions, test_dominant_is_highest_severity,
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
