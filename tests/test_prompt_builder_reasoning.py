"""
test_prompt_builder_reasoning.py — Unit tests for Phase-6 reasoning payload
integration in prompt_builder.

Runs without pytest:
    docker exec market-terminal python /app/tests/test_prompt_builder_reasoning.py
"""
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from prompt_builder import (
    build_messages,
    format_reasoning_compact, format_reasoning_verbose,
    REASONING_MODES, DEFAULT_REASONING_MODE,
    estimate_tokens,
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


# ─── Reasoning stub builder — produces a Stage-5-shaped dict ───────────────
def make_reasoning(*,
                    scenario="TIGHTENING_PANIC",
                    confidence=78,
                    driver="yields_rising",
                    scalp_bias="SHORT_BIAS",
                    intraday_bias="SHORT_BIAS",
                    swing_bias="LONG_BIAS",
                    preferred=None,
                    weak=None,
                    conflicts=None,
                    vol_warning=None,
                    catalyst_risk=None,
                    include_thesis=True):
    if preferred is None: preferred = ["DXY", "JPY", "VIX_calls"]
    if weak      is None: weak      = ["NDX", "BTC", "growth_tech"]
    return {
        "intent": "directional_intelligence",
        "not_for_execution": True,
        "usage_note": "Macro posture for human review.",
        "output_schema_version": "5.1",
        "scenario_name": scenario,
        "overall_confidence": confidence,
        "dominant_driver": driver,
        "scalp": {
            "kind": "regime_bias", "horizon": "1-15m",
            "bias": scalp_bias, "primary_asset": "NDX",
            "rationale_tags": ["yields_spike"],
            "thesis_invalidator": "US10Y reverses -5bp" if include_thesis else "",
            "dominant_driver": driver,
            "regime_contribution_weight": 23,
            "posture_avoid_conditions": ["if VIX retreats below 17"],
        },
        "intraday": {
            "kind": "regime_bias", "horizon": "1-4h",
            "bias": intraday_bias, "primary_asset": "growth_tech",
            "rationale_tags": ["fed_hawkish"],
            "thesis_invalidator": "Fed-speak turns dovish" if include_thesis else "",
            "dominant_driver": driver,
            "regime_contribution_weight": 25,
            "posture_avoid_conditions": ["if breadth widens"],
        },
        "swing": {
            "kind": "regime_bias", "horizon": "1-5d",
            "bias": swing_bias, "primary_asset": "DXY",
            "rationale_tags": ["tightening_cycle"],
            "thesis_invalidator": "DXY closes below 20d MA for 2 sessions" if include_thesis else "",
            "dominant_driver": driver,
            "regime_contribution_weight": 30,
            "posture_avoid_conditions": ["into FOMC blackout"],
        },
        "high_conviction_assets": [
            {"asset": "DXY", "bias": "LONG_BIAS", "rationale_tag": scenario.lower()},
        ],
        "assets_to_avoid":  [{"asset": "long_duration", "reason": f"{scenario.lower()} regime"}],
        "preferred_assets": preferred,
        "weak_assets":      weak,
        "volatility_warning": vol_warning,
        "catalyst_risk":      catalyst_risk,
        "conflicts":          conflicts or [],
        "overall_confidence": confidence,
        "confidence_breakdown": {
            "base": 80, "conflict_penalty": 0,
            "match_strength_penalty": -2, "vol_alignment_penalty": 0,
            "internal_consistency_bonus": 0,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. format_reasoning_compact — content + token budget
# ═══════════════════════════════════════════════════════════════════════════
def test_compact_contains_all_required_sections():
    print("\n═══ compact block — section presence ═══")
    r = make_reasoning(
        vol_warning="VIX 22 in HIGH regime — wide stops",
        catalyst_risk="MONETARY event severity 9 within 72h",
    )
    block = format_reasoning_compact(r)
    for tag in ("=== MACRO READ ===", "REGIME:", "DRIVER:", "POSTURE:",
                 "PREFERRED:", "WEAK:", "VOL:", "CATALYST:",
                 "INTEL · not_for_execution · directional_intelligence"):
        _check(f"compact_contains_{tag.replace(' ','_').replace(':','').lower()[:30]}",
               tag in block,
               f"missing {tag!r} in block")


def test_compact_under_120_tokens():
    print("\n═══ compact ≤ 120 tokens ═══")
    r = make_reasoning(
        vol_warning="VIX 22 in HIGH regime — wide stops, halved size; reduce overnight risk",
        catalyst_risk="MONETARY event severity 9 (FIRST_MOVER, ~2.0h old) — flow still expanding; expect volatility 72h",
        conflicts=[
            {"type": "sentiment_vs_regime_bullish_in_riskoff",
             "description": "BULLISH sentiment under TIGHTENING_PANIC",
             "penalty": -20},
        ],
    )
    block = format_reasoning_compact(r)
    tokens = estimate_tokens(block)
    _check("compact_under_120_tokens",
           tokens < 120,
           f"got {tokens} tokens, block_chars={len(block)}")


def test_compact_minimum_size_with_no_warnings():
    print("\n═══ compact baseline (no warnings/conflicts) ═══")
    r = make_reasoning()  # no vol_warning, no catalyst, no conflicts
    block = format_reasoning_compact(r)
    tokens = estimate_tokens(block)
    _check("compact_baseline_under_80_tokens",
           tokens < 80,
           f"got {tokens} tokens")


# ═══════════════════════════════════════════════════════════════════════════
# 2. format_reasoning_verbose
# ═══════════════════════════════════════════════════════════════════════════
def test_verbose_more_than_compact():
    print("\n═══ verbose > compact, < 300 tokens ═══")
    r = make_reasoning(
        vol_warning="VIX 22 in HIGH regime — wide stops",
        catalyst_risk="MONETARY event severity 9 within 72h",
        conflicts=[
            {"type": "sentiment_vs_regime_bullish_in_riskoff",
             "description": "BULLISH sentiment under TIGHTENING_PANIC regime",
             "penalty": -20},
        ],
    )
    compact = format_reasoning_compact(r)
    verbose = format_reasoning_verbose(r)
    c_tok = estimate_tokens(compact)
    v_tok = estimate_tokens(verbose)
    _check("verbose_strictly_larger_than_compact",
           v_tok > c_tok,
           f"compact={c_tok} verbose={v_tok}")
    _check("verbose_under_300_tokens",
           v_tok < 300,
           f"got {v_tok}")
    _check("verbose_includes_thesis_breakdown",
           "THESIS_BREAKS_IF" in verbose,
           "thesis_breaks_if line missing")
    _check("verbose_includes_conflict_details",
           "CONFLICT_DETAILS:" in verbose,
           "conflict_details section missing")
    _check("verbose_includes_conf_breakdown",
           "CONF_BREAKDOWN:" in verbose,
           "conf_breakdown section missing")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Empty / None handling
# ═══════════════════════════════════════════════════════════════════════════
def test_none_reasoning_returns_empty():
    print("\n═══ None / empty reasoning ═══")
    _check("compact_none_returns_empty",
           format_reasoning_compact(None) == "",
           "compact(None) should be empty")
    _check("verbose_none_returns_empty",
           format_reasoning_verbose(None) == "",
           "verbose(None) should be empty")
    _check("compact_empty_dict_returns_empty",
           format_reasoning_compact({}) == "" or
           format_reasoning_compact({}).startswith("=== MACRO READ ==="),
           "empty dict edge case")


# ═══════════════════════════════════════════════════════════════════════════
# 4. not_for_execution marker survives rendering
# ═══════════════════════════════════════════════════════════════════════════
def test_not_for_execution_marker_present():
    print("\n═══ not_for_execution marker in rendered block ═══")
    r = make_reasoning()
    compact = format_reasoning_compact(r)
    verbose = format_reasoning_verbose(r)
    _check("compact_preserves_not_for_execution",
           "not_for_execution" in compact and "directional_intelligence" in compact,
           f"compact text: {compact[-200:]!r}")
    _check("verbose_preserves_not_for_execution",
           "not_for_execution" in verbose and "directional_intelligence" in verbose,
           f"verbose tail: {verbose[-200:]!r}")


# ═══════════════════════════════════════════════════════════════════════════
# 5. build_messages — three modes
# ═══════════════════════════════════════════════════════════════════════════
def test_build_messages_compact_mode():
    print("\n═══ build_messages: compact mode ═══")
    r = make_reasoning()
    msgs = build_messages(task="hni", reasoning=r, reasoning_mode="compact")
    user_msg = msgs[1]["content"]
    _check("compact_macro_read_appears_in_user_msg",
           "=== MACRO READ ===" in user_msg,
           "MACRO READ section missing")
    _check("compact_intent_marker_in_user_msg",
           "not_for_execution" in user_msg,
           "intent marker missing")


def test_build_messages_verbose_mode():
    print("\n═══ build_messages: verbose mode ═══")
    r = make_reasoning()
    msgs = build_messages(task="hni", reasoning=r, reasoning_mode="verbose")
    user_msg = msgs[1]["content"]
    _check("verbose_macro_read_appears",
           "=== MACRO READ ===" in user_msg,
           "MACRO READ section missing")
    _check("verbose_includes_thesis_lines",
           "THESIS_BREAKS_IF" in user_msg,
           "verbose-only thesis lines missing")


def test_build_messages_hidden_mode():
    print("\n═══ build_messages: hidden mode ═══")
    r = make_reasoning()
    msgs = build_messages(task="hni", reasoning=r, reasoning_mode="hidden")
    user_msg = msgs[1]["content"]
    _check("hidden_skips_macro_read",
           "=== MACRO READ ===" not in user_msg,
           "hidden mode leaked block")
    _check("hidden_skips_intent_marker",
           "not_for_execution" not in user_msg,
           "hidden mode leaked marker")


def test_build_messages_no_reasoning_no_block():
    print("\n═══ build_messages: reasoning omitted → no block ═══")
    msgs = build_messages(task="hni")
    user_msg = msgs[1]["content"]
    _check("omitted_reasoning_no_block",
           "=== MACRO READ ===" not in user_msg,
           "block appeared without reasoning kwarg")


def test_build_messages_invalid_mode_defaults_compact():
    print("\n═══ build_messages: invalid mode → compact ═══")
    r = make_reasoning()
    msgs = build_messages(task="hni", reasoning=r, reasoning_mode="nonsense")
    user_msg = msgs[1]["content"]
    _check("invalid_mode_falls_back_to_compact",
           "=== MACRO READ ===" in user_msg
           and "THESIS_BREAKS_IF" not in user_msg,
           "invalid mode did not fall back to compact")


# ═══════════════════════════════════════════════════════════════════════════
# 6. Layer order — MACRO READ above STATE above TASK
# ═══════════════════════════════════════════════════════════════════════════
def test_build_messages_layer_order():
    print("\n═══ layer order: MACRO READ → STATE → TASK ═══")
    r = make_reasoning()
    fake_snap = {
        "macro_snapshot": {"dxy": {"price": 99.27, "change_pct": 0.45},
                            "us10y": {"price": 4.46},
                            "vix": {"price": 18}},
        "sentiment": {"tilt_score": -0.3, "sample_size": 20},
    }
    msgs = build_messages(task="hni", snap=fake_snap, reasoning=r)
    user = msgs[1]["content"]
    pos_macro = user.find("=== MACRO READ ===")
    pos_state = user.find("=== STATE ===")
    pos_task  = user.find("=== TASK ===")
    _check("macro_read_before_state",
           pos_macro >= 0 and pos_state > pos_macro,
           f"pos_macro={pos_macro} pos_state={pos_state}")
    _check("state_before_task",
           pos_state >= 0 and pos_task > pos_state,
           f"pos_state={pos_state} pos_task={pos_task}")


# ═══════════════════════════════════════════════════════════════════════════
# 7. Conflicts + warnings render
# ═══════════════════════════════════════════════════════════════════════════
def test_conflicts_render_with_type_tags():
    print("\n═══ conflicts render ═══")
    r = make_reasoning(conflicts=[
        {"type": "sentiment_vs_regime_bullish_in_riskoff",
         "description": "BULLISH sentiment under TIGHTENING_PANIC", "penalty": -20},
        {"type": "yields_rising_in_riskon",
         "description": "YIELDS RISING under RISK_ON", "penalty": -10},
    ])
    block = format_reasoning_compact(r)
    _check("conflict_count_rendered",
           "CONFLICTS(2):" in block,
           f"got block: {block!r}")
    _check("first_conflict_type_rendered",
           "sentiment_vs_regime_bullish_in_riskoff" in block,
           "first conflict type missing")


def test_vol_warning_renders():
    print("\n═══ vol warning rendering ═══")
    r = make_reasoning(vol_warning="VIX 38 EXTREME — halve size")
    block = format_reasoning_compact(r)
    _check("vol_warning_in_block",
           "VOL: VIX 38 EXTREME" in block,
           f"got block: {block!r}")


def test_catalyst_risk_renders():
    print("\n═══ catalyst risk rendering ═══")
    r = make_reasoning(catalyst_risk="GEOPOLITICAL event severity 9 within 72h")
    block = format_reasoning_compact(r)
    _check("catalyst_in_block",
           "CATALYST: GEOPOLITICAL" in block,
           f"got block: {block!r}")


# ═══════════════════════════════════════════════════════════════════════════
# 8. Round-trip: real Stage-5 output → compact block
# ═══════════════════════════════════════════════════════════════════════════
def test_real_stage5_roundtrip():
    print("\n═══ real Stage-5 → compact block round-trip ═══")
    from macro_reasoning_engine import analyze_stage5
    fake_snap = {
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
    out = analyze_stage5(fake_snap)
    trades = out["trades"]
    block = format_reasoning_compact(trades)
    _check("real_stage5_renders_to_block",
           "=== MACRO READ ===" in block and "REGIME:" in block,
           f"got block first 400 chars: {block[:400]!r}")
    _check("real_stage5_preserves_intent",
           "not_for_execution" in block,
           "intent marker missing in real-data render")


# ═══════════════════════════════════════════════════════════════════════════
# 9. Latency
# ═══════════════════════════════════════════════════════════════════════════
def test_format_latency_under_2ms():
    print("\n═══ format latency ═══")
    r = make_reasoning(
        vol_warning="long vol warning text",
        catalyst_risk="long catalyst risk text",
        conflicts=[{"type": f"t{i}", "description": f"d{i}", "penalty": -5} for i in range(4)],
    )
    iters = 200
    t0 = time.time()
    for _ in range(iters):
        format_reasoning_compact(r)
        format_reasoning_verbose(r)
    elapsed_ms = (time.time() - t0) * 1000
    per_call_ms = elapsed_ms / (iters * 2)
    _check("format_under_2ms_per_call",
           per_call_ms < 2.0,
           f"per_call_ms={per_call_ms:.3f} ({iters*2} calls in {elapsed_ms:.1f}ms)")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("Phase-6 prompt_builder reasoning tests")
    print("═" * 60)

    for test in (
        test_compact_contains_all_required_sections,
        test_compact_under_120_tokens,
        test_compact_minimum_size_with_no_warnings,
        test_verbose_more_than_compact,
        test_none_reasoning_returns_empty,
        test_not_for_execution_marker_present,
        test_build_messages_compact_mode,
        test_build_messages_verbose_mode,
        test_build_messages_hidden_mode,
        test_build_messages_no_reasoning_no_block,
        test_build_messages_invalid_mode_defaults_compact,
        test_build_messages_layer_order,
        test_conflicts_render_with_type_tags,
        test_vol_warning_renders,
        test_catalyst_risk_renders,
        test_real_stage5_roundtrip,
        test_format_latency_under_2ms,
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
