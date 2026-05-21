"""
test_event_graph.py — Unit tests for the causal event graph.

Covers node/edge integrity, change-% parsing, bounded propagation,
impact chains and contradiction detection (including the two spec
examples: bullish equities + rising VIX, bullish gold + rising real
yields), plus the analyze() output cache, the fail-soft degraded
path and the analyze_async() entry point.

Runs without pytest:
    docker exec market-terminal python /app/tests/test_event_graph.py
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import event_graph as eg


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
# Graph integrity
# ═══════════════════════════════════════════════════════════════════════════
def test_node_sets():
    print("\n═══ node sets ═══")
    _check("eight_nodes", len(eg.NODES) == 8, f"got {len(eg.NODES)}")
    _check("six_observed", len(eg.OBSERVED_NODES) == 6, f"got {eg.OBSERVED_NODES}")
    _check("two_derived", set(eg.DERIVED_NODES) == {"equities", "liquidity"},
           f"got {eg.DERIVED_NODES}")
    _check("observed_plus_derived_is_all",
           set(eg.OBSERVED_NODES) | set(eg.DERIVED_NODES) == set(eg.NODES), "")
    expected = {"yields", "dxy", "gold", "oil", "equities",
                "volatility", "liquidity", "macro_events"}
    _check("expected_node_names", set(eg.NODES) == expected,
           f"missing={expected - set(eg.NODES)}")


def test_edge_integrity():
    print("\n═══ edge integrity ═══")
    bad_node = bad_sign = bad_weight = 0
    for (u, v, sign, w) in eg.EDGES:
        if u not in eg.NODES or v not in eg.NODES:
            bad_node += 1
        if sign not in (-1, 1):
            bad_sign += 1
        if not (0.0 < w <= 1.0):
            bad_weight += 1
    _check("all_edges_reference_valid_nodes", bad_node == 0, f"{bad_node} bad")
    _check("all_edge_signs_are_plus_minus_one", bad_sign == 0, f"{bad_sign} bad")
    _check("all_edge_weights_in_0_1", bad_weight == 0, f"{bad_weight} bad")
    _check("has_edges", len(eg.EDGES) >= 12, f"got {len(eg.EDGES)}")


# ═══════════════════════════════════════════════════════════════════════════
# change-% parsing
# ═══════════════════════════════════════════════════════════════════════════
def test_chg_parsing():
    print("\n═══ _chg parsing ═══")
    _check("chg_dict", eg._chg({"change_pct": 1.5}) == 1.5, "")
    _check("chg_dict_change_key", eg._chg({"change": -0.8}) == -0.8, "")
    _check("chg_bare_scalar", eg._chg(2.3) == 2.3, "")
    _check("chg_string_pct", eg._chg("+1.2%") == 1.2, "")
    _check("chg_string_neg", eg._chg("-3.4%") == -3.4, "")
    _check("chg_none_zero", eg._chg(None) == 0.0, "")
    _check("chg_garbage_zero", eg._chg("n/a") == 0.0, "")


def test_derive_node_states():
    print("\n═══ derive_node_states ═══")
    macro = {
        "us10y": {"change_pct": 7.0}, "dxy": {"change_pct": 0.6},
        "gold": {"change_pct": -2.0}, "oil": {"change_pct": 2.8},
        "vix": {"change_pct": 18.0},
    }
    s = eg.derive_node_states(macro, events_tilt=-0.4)
    _check("yields_positive", s["yields"] > 0, f"got {s['yields']}")
    _check("yields_clamped", s["yields"] <= 1.0, f"got {s['yields']}")
    _check("gold_negative", s["gold"] < 0, f"got {s['gold']}")
    _check("vix_positive", s["volatility"] > 0, f"got {s['volatility']}")
    _check("macro_events_passthrough", abs(s["macro_events"] - (-0.4)) < 1e-9,
           f"got {s['macro_events']}")
    _check("only_observed_nodes", set(s) == set(eg.OBSERVED_NODES),
           f"got {set(s)}")
    _check("empty_macro_safe", eg.derive_node_states(None) is not None, "")


# ═══════════════════════════════════════════════════════════════════════════
# Propagation
# ═══════════════════════════════════════════════════════════════════════════
def test_propagate_empty():
    print("\n═══ propagate: empty → all zero ═══")
    p = eg.propagate({})
    _check("all_nodes_present", set(p) == set(eg.NODES), f"got {set(p)}")
    _check("all_zero", all(abs(v) < 1e-9 for v in p.values()), f"got {p}")


def test_propagate_risk_off():
    print("\n═══ propagate: risk-off shock → equities pressure negative ═══")
    # yields up, dxy up, vol up → equities should be pushed DOWN
    states = {"yields": 0.7, "dxy": 0.6, "volatility": 0.8}
    p = eg.propagate(states)
    _check("equities_pressure_negative", p["equities"] < -0.2,
           f"got {p['equities']}")
    _check("liquidity_pressure_negative", p["liquidity"] < 0,
           f"got {p['liquidity']}")
    _check("all_clamped", all(-1.0 <= v <= 1.0 for v in p.values()),
           f"got {p}")


def test_propagate_risk_on():
    print("\n═══ propagate: risk-on shock → equities pressure positive ═══")
    # vol down, risk-positive events → equities should be pushed UP
    states = {"volatility": -0.6, "macro_events": 0.7}
    p = eg.propagate(states)
    _check("equities_pressure_positive", p["equities"] > 0.2,
           f"got {p['equities']}")


def test_propagate_derived_nodes_fill():
    print("\n═══ propagate: derived nodes get values from propagation ═══")
    # liquidity is never observed — it must acquire a value purely via edges
    p = eg.propagate({"volatility": 0.9})
    _check("liquidity_filled", abs(p["liquidity"]) > 0.05,
           f"liquidity={p['liquidity']}")
    _check("equities_filled", abs(p["equities"]) > 0.05,
           f"equities={p['equities']}")


def test_propagate_terminates_with_cycles():
    print("\n═══ propagate: bounded — terminates despite graph cycles ═══")
    # The graph contains cycles (oil→yields→dxy→oil, liquidity↔volatility).
    # A maxed-out shock must still return finite, clamped values.
    p = eg.propagate({n: 1.0 for n in eg.OBSERVED_NODES})
    _check("finite_and_clamped",
           all(-1.0 <= v <= 1.0 for v in p.values()), f"got {p}")
    _check("returns_all_nodes", len(p) == 8, f"got {len(p)}")


def test_propagate_deterministic():
    print("\n═══ propagate: deterministic ═══")
    states = {"yields": 0.5, "dxy": -0.3, "volatility": 0.4}
    _check("same_input_same_output",
           eg.propagate(states) == eg.propagate(states), "non-deterministic!")


# ═══════════════════════════════════════════════════════════════════════════
# Impact chains
# ═══════════════════════════════════════════════════════════════════════════
def test_impact_chain():
    print("\n═══ impact_chain ═══")
    chain = eg.impact_chain({"volatility": 0.8, "yields": 0.6}, "equities")
    _check("chain_non_empty", len(chain) > 0, "no paths")
    _check("chain_sorted_by_magnitude",
           all(abs(chain[i]["contribution"]) >= abs(chain[i + 1]["contribution"])
               for i in range(len(chain) - 1)), f"got {chain}")
    _check("paths_target_equities",
           all(p["path"].endswith("equities") for p in chain), f"got {chain}")
    _check("volatility_drives_equities_down",
           any(p["path"].startswith("volatility") and p["contribution"] < 0
               for p in chain), f"got {chain}")


# ═══════════════════════════════════════════════════════════════════════════
# Contradiction detection — incl. the two spec examples
# ═══════════════════════════════════════════════════════════════════════════
def test_contradiction_equities_vix():
    print("\n═══ contradiction: bullish equities + rising VIX (spec) ═══")
    hits = eg.detect_contradictions({"equities": 0.6, "volatility": 0.6})
    _check("equities_vix_contradiction_fires", len(hits) >= 1, f"got {hits}")
    _check("pair_is_equities_volatility",
           any(h["pair"] == "equities|volatility" for h in hits), f"got {hits}")


def test_contradiction_gold_yields():
    print("\n═══ contradiction: bullish gold + rising real yields (spec) ═══")
    hits = eg.detect_contradictions({"gold": 0.6, "yields": 0.5})
    _check("gold_yields_contradiction_fires",
           any(h["pair"] == "gold|yields" for h in hits), f"got {hits}")
    _check("label_mentions_yields",
           any("yield" in h["label"].lower() for h in hits), f"got {hits}")


def test_no_contradiction_when_consistent():
    print("\n═══ contradiction: consistent state → none ═══")
    # equities up + volatility DOWN is causally consistent → no contradiction
    hits = eg.detect_contradictions({"equities": 0.6, "volatility": -0.6})
    _check("consistent_no_equities_vol",
           not any(h["pair"] == "equities|volatility" for h in hits),
           f"got {hits}")


def test_contradiction_threshold():
    print("\n═══ contradiction: sub-threshold moves ignored ═══")
    hits = eg.detect_contradictions({"gold": 0.05, "yields": 0.05})
    _check("tiny_moves_no_contradiction", len(hits) == 0, f"got {hits}")


def test_contradiction_skips_absent_equities():
    print("\n═══ contradiction: equities rules skipped when absent ═══")
    # No equities key → equities-side rules simply don't run (no crash)
    hits = eg.detect_contradictions({"gold": 0.6, "yields": 0.6})
    _check("no_crash_without_equities", isinstance(hits, list), "")
    _check("equities_rule_absent",
           not any("equities" in h["pair"] for h in hits), f"got {hits}")


# ═══════════════════════════════════════════════════════════════════════════
# analyze() one-shot
# ═══════════════════════════════════════════════════════════════════════════
def test_analyze_shape():
    print("\n═══ analyze: full envelope ═══")
    macro = {"us10y": {"change_pct": 5.0}, "dxy": {"change_pct": 0.5},
             "gold": {"change_pct": 1.0}, "oil": {"change_pct": -1.0},
             "vix": {"change_pct": 12.0}}
    out = eg.analyze(macro, events_tilt=-0.3, equities_observed=0.4)
    required = {"observed", "pressures", "equity_pressure",
                "liquidity_pressure", "impact_chain", "contradictions"}
    _check("has_all_keys", required.issubset(out), f"missing={required - set(out)}")
    _check("equity_pressure_in_range", -1.0 <= out["equity_pressure"] <= 1.0,
           f"got {out['equity_pressure']}")
    _check("pressures_all_nodes", set(out["pressures"]) == set(eg.NODES), "")
    _check("contradictions_is_list", isinstance(out["contradictions"], list), "")


def test_analyze_deterministic():
    print("\n═══ analyze: deterministic ═══")
    macro = {"us10y": {"change_pct": 3.0}, "vix": {"change_pct": 9.0}}
    a = eg.analyze(macro, events_tilt=0.1)
    b = eg.analyze(macro, events_tilt=0.1)
    _check("analyze_repeatable",
           a["equity_pressure"] == b["equity_pressure"]
           and a["pressures"] == b["pressures"], "non-deterministic!")


# ═══════════════════════════════════════════════════════════════════════════
# analyze() — caching, fail-soft, async entry point
# ═══════════════════════════════════════════════════════════════════════════
def test_analyze_degraded_flag():
    print("\n═══ analyze: degraded flag on happy path ═══")
    out = eg.analyze({"vix": {"change_pct": 9.0}})
    _check("degraded_key_present", "degraded" in out, f"got {set(out)}")
    _check("degraded_false_when_ok", out["degraded"] is False,
           f"got {out['degraded']}")


def test_analyze_fail_soft():
    print("\n═══ analyze: fail-soft never raises on bad input ═══")
    # A non-dict, non-None macro forces an internal error — analyze() must
    # swallow it and return a neutral, correctly-shaped result.
    out = eg.analyze(["not", "a", "dict"])
    _check("fail_soft_returns_dict", isinstance(out, dict), f"got {type(out)}")
    _check("fail_soft_marked_degraded", out.get("degraded") is True,
           f"got {out.get('degraded')}")
    required = {"observed", "pressures", "equity_pressure",
                "liquidity_pressure", "impact_chain", "contradictions"}
    _check("fail_soft_keeps_full_shape", required.issubset(out),
           f"missing={required - set(out)}")
    _check("fail_soft_neutral_pressure", out["equity_pressure"] == 0.0,
           f"got {out['equity_pressure']}")


def test_analyze_cache_hit():
    print("\n═══ analyze: identical inputs hit the cache ═══")
    eg.clear_cache()
    macro = {"us10y": {"change_pct": 4.0}, "vix": {"change_pct": 11.0}}
    first  = eg.analyze(macro, events_tilt=0.2)
    second = eg.analyze(macro, events_tilt=0.2)
    _check("cache_hit_result_equal", first == second, "cached result differs")
    stats = eg.cache_stats()
    _check("cache_recorded_one_miss", stats["misses"] == 1, f"got {stats}")
    _check("cache_recorded_one_hit", stats["hits"] == 1, f"got {stats}")


def test_analyze_cache_isolation():
    print("\n═══ analyze: cached copy is isolated from caller mutation ═══")
    eg.clear_cache()
    macro = {"gold": {"change_pct": 1.5}}
    a = eg.analyze(macro)
    a["pressures"]["equities"] = 999.0      # tamper with the caller's copy
    a["contradictions"].append("tamper")
    b = eg.analyze(macro)                   # served from cache
    _check("scalar_mutation_did_not_leak", b["pressures"]["equities"] != 999.0,
           f"got {b['pressures']['equities']}")
    _check("list_mutation_did_not_leak", "tamper" not in b["contradictions"],
           f"got {b['contradictions']}")


def test_analyze_no_cache_flag():
    print("\n═══ analyze: use_cache=False bypasses the cache ═══")
    eg.clear_cache()
    macro = {"oil": {"change_pct": 2.0}}
    eg.analyze(macro, use_cache=False)
    eg.analyze(macro, use_cache=False)
    stats = eg.cache_stats()
    _check("no_cache_no_hits", stats["hits"] == 0, f"got {stats}")
    _check("no_cache_no_store", stats["size"] == 0, f"got {stats}")


def test_analyze_async_matches_sync():
    print("\n═══ analyze_async: matches the sync analyze() ═══")
    import asyncio as _aio
    eg.clear_cache()
    macro = {"us10y": {"change_pct": 3.0}, "dxy": {"change_pct": 0.4},
             "vix": {"change_pct": 8.0}}
    sync_out  = eg.analyze(macro, events_tilt=-0.2, equities_observed=0.3)
    async_out = _aio.run(
        eg.analyze_async(macro, events_tilt=-0.2, equities_observed=0.3))
    _check("async_equals_sync", sync_out == async_out, "async result differs")
    _check("async_not_degraded", async_out["degraded"] is False,
           f"got {async_out['degraded']}")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("event_graph — causal propagation unit tests")
    print("═" * 60)

    tests = (
        test_node_sets, test_edge_integrity,
        test_chg_parsing, test_derive_node_states,
        test_propagate_empty, test_propagate_risk_off, test_propagate_risk_on,
        test_propagate_derived_nodes_fill, test_propagate_terminates_with_cycles,
        test_propagate_deterministic, test_impact_chain,
        test_contradiction_equities_vix, test_contradiction_gold_yields,
        test_no_contradiction_when_consistent, test_contradiction_threshold,
        test_contradiction_skips_absent_equities,
        test_analyze_shape, test_analyze_deterministic,
        test_analyze_degraded_flag, test_analyze_fail_soft,
        test_analyze_cache_hit, test_analyze_cache_isolation,
        test_analyze_no_cache_flag, test_analyze_async_matches_sync,
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
