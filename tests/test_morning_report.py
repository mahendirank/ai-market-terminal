"""
test_morning_report.py — Unit tests for the grounded morning-report stack.

Covers the three deterministic modules:
  - bias_consensus_engine  (the single source of directional truth)
  - confidence_engine      (deterministic confidence scoring)
  - morning_report         (pure helpers + the contradiction guard)

Network-touching paths (build_global_report / build_market_brief) are NOT
exercised here — only the deterministic, pure-function surface is, so the
suite runs fast and offline.

Runs without pytest:
    docker exec market-terminal python /app/tests/test_morning_report.py
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import bias_consensus_engine as bce
import confidence_engine as ce
import morning_report as mr
from bias_consensus_engine import Signal


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
# bias_consensus_engine — consensus computation
# ═══════════════════════════════════════════════════════════════════════════
def test_empty_signals_neutral():
    print("\n═══ consensus: empty → NEUTRAL ═══")
    c = bce.compute_consensus([])
    _check("empty_bias_neutral", c["bias"] == bce.BIAS_NEUTRAL, f"got {c['bias']}")
    _check("empty_score_zero", c["score"] == 0.0, f"got {c['score']}")
    _check("empty_source_count_zero", c["source_count"] == 0, f"got {c['source_count']}")


def test_all_sell_signals():
    print("\n═══ consensus: all bearish → SELL ═══")
    sigs = [
        Signal("indicators", -0.8),
        Signal("macro_reasoning", -0.5),
        Signal("regime", -0.6),
    ]
    c = bce.compute_consensus(sigs)
    _check("all_sell_bias", c["bias"] == bce.BIAS_SELL, f"got {c['bias']}")
    _check("all_sell_score_negative", c["score"] < bce._SELL_THRESHOLD, f"got {c['score']}")
    _check("all_sell_agreement_full", c["agreement"] == 1.0, f"got {c['agreement']}")
    _check("all_sell_no_dissent", c["dissent"] == [], f"got {c['dissent']}")


def test_all_buy_signals():
    print("\n═══ consensus: all bullish → BUY ═══")
    sigs = [
        Signal("indicators", 0.7),
        Signal("regime", 0.5),
        Signal("sentiment", 0.6),
    ]
    c = bce.compute_consensus(sigs)
    _check("all_buy_bias", c["bias"] == bce.BIAS_BUY, f"got {c['bias']}")
    _check("all_buy_score_positive", c["score"] > bce._BUY_THRESHOLD, f"got {c['score']}")


def test_strong_indicator_dominates():
    print("\n═══ consensus: heavy indicator weight dominates ═══")
    # Indicators (weight 0.35) strongly SELL; three light global sources weakly BUY.
    sigs = [
        Signal("indicators", -0.95),
        Signal("correlation", 0.30),
        Signal("events", 0.25),
        Signal("sentiment", 0.20),
    ]
    c = bce.compute_consensus(sigs)
    _check("indicator_pulls_sell", c["bias"] == bce.BIAS_SELL,
           f"got {c['bias']} score={c['score']}")


def test_mixed_signals_neutral_band():
    print("\n═══ consensus: offsetting signals → NEUTRAL band ═══")
    sigs = [
        Signal("indicators", 0.10),
        Signal("regime", -0.10),
        Signal("macro_reasoning", 0.05),
    ]
    c = bce.compute_consensus(sigs)
    _check("mixed_in_neutral_band",
           bce._SELL_THRESHOLD < c["score"] < bce._BUY_THRESHOLD,
           f"score={c['score']}")
    _check("mixed_bias_neutral", c["bias"] == bce.BIAS_NEUTRAL, f"got {c['bias']}")


def test_dissent_detected():
    print("\n═══ consensus: opposing source recorded as dissent ═══")
    sigs = [
        Signal("indicators", -0.8),
        Signal("regime", -0.6),
        Signal("sentiment", 0.7),   # genuine opposite vote
    ]
    c = bce.compute_consensus(sigs)
    _check("dissent_has_sentiment", "sentiment" in c["dissent"], f"got {c['dissent']}")
    _check("dissent_agreement_below_one", c["agreement"] < 1.0, f"got {c['agreement']}")


def test_effective_weight():
    print("\n═══ Signal.effective_weight ═══")
    _check("default_weight_from_table",
           Signal("indicators", 0.0).effective_weight() == bce.SOURCE_WEIGHTS["indicators"],
           "indicators weight mismatch")
    _check("override_weight_respected",
           Signal("indicators", 0.0, weight=0.99).effective_weight() == 0.99,
           "override ignored")
    _check("unknown_source_zero_weight",
           Signal("does_not_exist", 0.5).effective_weight() == 0.0,
           "unknown source should be 0")


def test_votes_derive_label_when_bias_blank():
    print("\n═══ consensus: blank bias → label derived from score ═══")
    sigs = [Signal("indicators", -0.5), Signal("regime", -0.4)]
    c = bce.compute_consensus(sigs)
    by_src = {v["source"]: v for v in c["votes"]}
    _check("blank_bias_derives_sell",
           by_src["indicators"]["bias"] == bce.BIAS_SELL,
           f"got {by_src['indicators']['bias']}")


def test_partial_source_set_normalises():
    print("\n═══ consensus: partial source set still normalises ═══")
    # Only one source present — score should equal that source's clamped score.
    c = bce.compute_consensus([Signal("indicators", -0.5)])
    _check("single_source_score_equals_input", abs(c["score"] - (-0.5)) < 1e-6,
           f"got {c['score']}")
    _check("single_source_count", c["source_count"] == 1, f"got {c['source_count']}")


# ═══════════════════════════════════════════════════════════════════════════
# bias_consensus_engine — contradiction guard  (user requirement #7)
# ═══════════════════════════════════════════════════════════════════════════
def test_contradicts_opposites():
    print("\n═══ contradicts(): BUY vs SELL ═══")
    _check("sell_vs_buy_contradicts", bce.contradicts("SELL", "BUY") is True, "")
    _check("buy_vs_sell_contradicts", bce.contradicts("BUY", "SELL") is True, "")
    _check("sell_vs_sell_ok", bce.contradicts("SELL", "SELL") is False, "")
    _check("buy_vs_buy_ok", bce.contradicts("BUY", "BUY") is False, "")
    _check("neutral_never_contradicts",
           bce.contradicts("NEUTRAL", "BUY") is False
           and bce.contradicts("SELL", "NEUTRAL") is False, "")
    _check("case_insensitive", bce.contradicts("sell", "buy") is True, "")


def test_indicators_sell_blocks_llm_buy():
    print("\n═══ guard: indicators SELL ⇒ LLM may not say BUY (req #7) ═══")
    # Build a consensus where the dominant indicators signal is SELL.
    c = bce.compute_consensus([
        Signal("indicators", -0.9),
        Signal("regime", -0.3),
    ])
    _check("consensus_is_sell", c["bias"] == bce.BIAS_SELL, f"got {c['bias']}")
    # An LLM narration that flips to a BUY recommendation must be caught.
    bad = "Despite the weak tape, this is a clear buy signal — go long into the open."
    hit = bce.scan_for_contradiction(c["bias"], bad)
    _check("buy_narration_rejected", hit is not None, f"scan returned {hit!r}")
    # A compliant narration that explains the SELL must pass clean.
    good = ("Momentum and trend indicators are firmly negative; the deterministic "
            "read is bearish and price sits below all key moving averages.")
    _check("compliant_narration_passes",
           bce.scan_for_contradiction(c["bias"], good) is None, "")


def test_scan_neutral_consensus_no_flag():
    print("\n═══ scan: NEUTRAL consensus flags nothing ═══")
    _check("neutral_scan_none",
           bce.scan_for_contradiction("NEUTRAL", "this could be a buy or a sell") is None, "")
    _check("empty_text_none",
           bce.scan_for_contradiction("SELL", "") is None, "")


def test_scan_buy_consensus_catches_bear_words():
    print("\n═══ scan: BUY consensus catches bearish phrases ═══")
    hit = bce.scan_for_contradiction("BUY", "the setup favours a short setup here")
    _check("buy_consensus_flags_short", hit is not None, f"got {hit!r}")


# ═══════════════════════════════════════════════════════════════════════════
# confidence_engine
# ═══════════════════════════════════════════════════════════════════════════
def test_confidence_high_when_aligned():
    print("\n═══ confidence: aligned strong sources → HIGH ═══")
    c = bce.compute_consensus([
        Signal("indicators", -0.9),
        Signal("macro_reasoning", -0.8),
        Signal("regime", -0.85),
        Signal("sentiment", -0.8),
        Signal("events", -0.75),
        Signal("correlation", -0.7),
    ])
    conf = ce.compute_confidence(c, freshness=1.0)
    _check("high_tier", conf["tier"] == ce.TIER_HIGH, f"got {conf['tier']} score={conf['score']}")
    _check("high_score_range", conf["score"] >= 70, f"got {conf['score']}")
    _check("high_conviction_true", ce.is_high_conviction(conf) is True, "")


def test_confidence_low_when_split():
    print("\n═══ confidence: disagreeing sources → lower tier ═══")
    c = bce.compute_consensus([
        Signal("indicators", -0.6),
        Signal("regime", 0.6),
        Signal("sentiment", 0.1),
    ])
    conf = ce.compute_confidence(c, freshness=1.0)
    _check("split_not_high", conf["tier"] != ce.TIER_HIGH,
           f"got {conf['tier']} score={conf['score']}")
    _check("split_conviction_false", ce.is_high_conviction(conf) is False, "")


def test_confidence_empty_consensus():
    print("\n═══ confidence: empty consensus → LOW ═══")
    conf = ce.compute_confidence(bce.compute_consensus([]))
    _check("empty_low_tier", conf["tier"] == ce.TIER_LOW, f"got {conf['tier']}")
    _check("empty_score_zero", conf["score"] == 0, f"got {conf['score']}")


def test_confidence_freshness_penalty():
    print("\n═══ confidence: stale data lowers score ═══")
    c = bce.compute_consensus([
        Signal("indicators", -0.8), Signal("regime", -0.7), Signal("macro_reasoning", -0.75),
    ])
    fresh = ce.compute_confidence(c, freshness=1.0)["score"]
    stale = ce.compute_confidence(c, freshness=0.0)["score"]
    _check("stale_score_lower", stale < fresh, f"fresh={fresh} stale={stale}")


def test_confidence_components_present():
    print("\n═══ confidence: component breakdown present ═══")
    conf = ce.compute_confidence(bce.compute_consensus([Signal("indicators", -0.5)]))
    comps = conf.get("components", {})
    _check("has_all_components",
           {"agreement", "signal_strength", "source_coverage", "freshness"}.issubset(comps),
           f"got {list(comps)}")
    _check("note_is_string", isinstance(conf.get("note"), str) and conf["note"], "")


def test_confidence_bad_input():
    print("\n═══ confidence: non-dict input → safe LOW ═══")
    conf = ce.compute_confidence("not a dict")  # type: ignore[arg-type]
    _check("bad_input_low", conf["tier"] == ce.TIER_LOW, f"got {conf}")


# ═══════════════════════════════════════════════════════════════════════════
# morning_report — market registry
# ═══════════════════════════════════════════════════════════════════════════
def test_market_registry():
    print("\n═══ morning_report: 8-market registry ═══")
    _check("eight_markets", len(mr.MARKETS) == 8, f"got {len(mr.MARKETS)}")
    _check("order_has_eight", len(mr.MARKET_ORDER) == 8, f"got {len(mr.MARKET_ORDER)}")
    _check("order_matches_registry",
           set(mr.MARKET_ORDER) == set(mr.MARKETS), "order/registry mismatch")
    expected = {"CHINA", "JAPAN", "INDIA", "GERMANY", "UK", "ITALY", "FRANCE", "USA"}
    _check("expected_market_keys", set(mr.MARKETS) == expected,
           f"missing={expected - set(mr.MARKETS)}")
    _check("each_market_has_primary",
           all(c.get("primary") for c in mr.MARKETS.values()), "a market lacks a primary index")
    _check("list_markets_returns_eight", len(mr.list_markets()) == 8, "")


def test_staggered_ttls():
    print("\n═══ morning_report: staggered cache TTLs ═══")
    ttls = [mr._market_ttl(m) for m in mr.MARKET_ORDER]
    _check("ttls_all_distinct", len(set(ttls)) == 8, f"got {ttls}")
    _check("ttls_ascending", ttls == sorted(ttls), f"got {ttls}")
    _check("first_ttl_is_base", ttls[0] == mr._BASE_TTL, f"got {ttls[0]}")


# ═══════════════════════════════════════════════════════════════════════════
# morning_report — support/resistance extraction
# ═══════════════════════════════════════════════════════════════════════════
def test_extract_levels_normal():
    print("\n═══ morning_report: S/R between straddling EMAs ═══")
    res = {
        "last_price": 100.0,
        "indicators": {
            "EMA20": {"value": 98.0}, "EMA50": {"value": 95.0},
            "EMA200": {"value": 105.0}, "ATR": {"value": 2.0},
        },
    }
    lv = mr._extract_levels(res)
    _check("support_is_nearest_ema_below", lv["support"] == 98.0, f"got {lv['support']}")
    _check("resistance_is_nearest_ema_above", lv["resistance"] == 105.0, f"got {lv['resistance']}")


def test_extract_levels_atr_fallback():
    print("\n═══ morning_report: ATR fallback when EMAs one-sided ═══")
    # All EMAs below price → no EMA resistance → ATR-offset fallback.
    res = {
        "last_price": 100.0,
        "indicators": {
            "EMA20": {"value": 98.0}, "EMA50": {"value": 95.0},
            "EMA200": {"value": 90.0}, "ATR": {"value": 3.0},
        },
    }
    lv = mr._extract_levels(res)
    _check("support_still_ema", lv["support"] == 98.0, f"got {lv['support']}")
    _check("resistance_atr_fallback", lv["resistance"] == 103.0, f"got {lv['resistance']}")


def test_extract_levels_none():
    print("\n═══ morning_report: missing indicator data → None levels ═══")
    lv = mr._extract_levels(None)
    _check("none_support", lv["support"] is None, f"got {lv['support']}")
    _check("none_resistance", lv["resistance"] is None, f"got {lv['resistance']}")
    _check("none_last_price", lv["last_price"] is None, f"got {lv['last_price']}")


# ═══════════════════════════════════════════════════════════════════════════
# morning_report — regime direction map + helpers
# ═══════════════════════════════════════════════════════════════════════════
def test_regime_direction_map():
    print("\n═══ morning_report: regime → directional sign ═══")
    _check("risk_off_negative", mr._REGIME_DIRECTION["RISK_OFF"] < 0, "")
    _check("crisis_most_negative",
           mr._REGIME_DIRECTION["CRISIS"] == min(mr._REGIME_DIRECTION.values()), "")
    _check("risk_on_positive", mr._REGIME_DIRECTION["RISK_ON"] > 0, "")
    _check("mixed_is_zero", mr._REGIME_DIRECTION["MIXED"] == 0.0, "")
    _check("unknown_key_defaults_zero",
           mr._REGIME_DIRECTION.get("SOMETHING_ELSE", 0.0) == 0.0, "")


def test_clamp01():
    print("\n═══ morning_report: _clamp01 ═══")
    _check("clamp_high", mr._clamp01(5.0) == 1.0, f"got {mr._clamp01(5.0)}")
    _check("clamp_low", mr._clamp01(-5.0) == -1.0, f"got {mr._clamp01(-5.0)}")
    _check("clamp_passthrough", mr._clamp01(0.3) == 0.3, f"got {mr._clamp01(0.3)}")
    _check("clamp_bad_input_zero", mr._clamp01("nan-ish") == 0.0, "")


def test_risk_warnings():
    print("\n═══ morning_report: risk warnings fire on extreme vol ═══")
    g = {"vix": 32.0, "correlation_anomalies": 4}
    warns = mr._risk_warnings(g, indicator_result={"x": 1},
                              confidence={"tier": "MEDIUM"})
    _check("extreme_vix_warned", any("EXTREME" in w for w in warns), f"got {warns}")
    _check("anomaly_warned", any("anomal" in w.lower() for w in warns), f"got {warns}")
    # Missing index data must downgrade to LOW-conviction warning.
    warns2 = mr._risk_warnings({"vix": 14}, indicator_result=None,
                               confidence={"tier": "MEDIUM"})
    _check("missing_data_warned",
           any("unavailable" in w.lower() for w in warns2), f"got {warns2}")


# ═══════════════════════════════════════════════════════════════════════════
# morning_report — LLM narration gating
# ═══════════════════════════════════════════════════════════════════════════
def test_narration_gated_off_by_default():
    print("\n═══ morning_report: narration disabled ⇒ None (no LLM call) ═══")
    saved = os.environ.pop("ENABLE_MORNING_NARRATION", None)
    try:
        brief = {"computed_bias": "SELL", "market": "India"}
        _check("narration_none_when_gated", mr.narrate_brief(brief) is None, "")
    finally:
        if saved is not None:
            os.environ["ENABLE_MORNING_NARRATION"] = saved


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("Grounded morning-report stack — unit tests")
    print("═" * 60)

    tests = (
        test_empty_signals_neutral, test_all_sell_signals, test_all_buy_signals,
        test_strong_indicator_dominates, test_mixed_signals_neutral_band,
        test_dissent_detected, test_effective_weight,
        test_votes_derive_label_when_bias_blank, test_partial_source_set_normalises,
        test_contradicts_opposites, test_indicators_sell_blocks_llm_buy,
        test_scan_neutral_consensus_no_flag, test_scan_buy_consensus_catches_bear_words,
        test_confidence_high_when_aligned, test_confidence_low_when_split,
        test_confidence_empty_consensus, test_confidence_freshness_penalty,
        test_confidence_components_present, test_confidence_bad_input,
        test_market_registry, test_staggered_ttls,
        test_extract_levels_normal, test_extract_levels_atr_fallback,
        test_extract_levels_none, test_regime_direction_map, test_clamp01,
        test_risk_warnings, test_narration_gated_off_by_default,
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
