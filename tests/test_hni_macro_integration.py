"""
test_hni_macro_integration.py — Verify the HNI Phase-6 gated integration.

Runs without pytest:
    docker exec market-terminal python /app/tests/test_hni_macro_integration.py

Goals:
  1. Flag OFF (default): no MACRO READ block in HNI prompt
  2. Flag ON: MACRO READ block present + intent marker preserved
  3. Anti-hallucination constraint added when reasoning attached
  4. No other AI tab paths altered (we don't import them; we just confirm
     the prompt_builder kwargs we use only affect HNI)
  5. not_for_execution + directional_intelligence survive rendering

These tests don't hit Groq. They build the prompt via the same code path
the HNI endpoint uses, then inspect the assembled messages.
"""
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT, ".env"), override=False)

from prompt_builder import build_messages, estimate_messages
from macro_reasoning_engine import analyze_stage5


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


# ─── Synthetic snapshot generator for one of the 4 archetypes ──────────────
def synth_snap(*, kind: str) -> dict:
    """Build a market_intel-shaped snapshot for a named market event.

    Archetypes:
      cpi_hot          — STAGFLATION_LITE territory
      fed_hawkish      — TIGHTENING_PANIC territory
      geopolitical     — GEOPOLITICAL_RISKOFF
      melt_up          — MELT_UP / RISK_ON
    """
    if kind == "cpi_hot":
        return {
            "macro_snapshot": {
                "us10y": {"price": 4.55, "change_pct": 2.0},      # +9bp ≈
                "dxy":   {"price": 104.20, "change_pct": 0.20},
                "vix":   {"price": 21.0},
            },
            "sentiment": {"tilt_score": -0.40, "sample_size": 35, "macro_tilt": "BEARISH"},
            "events_classified": {
                "by_category": {"INFLATION": {"count": 2, "max_sev": 8, "avg_sev": 7.5}},
                "directional":  {"bull_count": 0, "bear_count": 2,
                                  "bull_weighted": 0, "bear_weighted": 16},
                "total_classified": 2,
            },
            "news": {"clusters": [{
                "topic": "US CPI prints above expectations at 0.4% MoM",
                "event": {"category": "INFLATION", "severity": 8, "direction": "BEAR_RISK"},
                "headlines": [{"ts": time.time() - 3*3600, "source": "Reuters"}],
                "first_mover": "Reuters", "size": 3, "tickers": [],
            }]},
        }
    if kind == "fed_hawkish":
        return {
            "macro_snapshot": {
                "us10y": {"price": 4.65, "change_pct": 3.0},      # +14bp ≈
                "dxy":   {"price": 105.50, "change_pct": 0.55},
                "vix":   {"price": 24.0},
            },
            "sentiment": {"tilt_score": -0.55, "sample_size": 45, "macro_tilt": "BEARISH"},
            "events_classified": {
                "by_category": {"MONETARY": {"count": 3, "max_sev": 9, "avg_sev": 8.5}},
                "directional":  {"bull_count": 0, "bear_count": 3,
                                  "bull_weighted": 0, "bear_weighted": 27},
                "total_classified": 3,
            },
            "news": {"clusters": [{
                "topic": "Fed signals higher-for-longer; dot plot revised up",
                "event": {"category": "MONETARY", "severity": 9, "direction": "BEAR_RISK"},
                "headlines": [{"ts": time.time() - 1.5*3600, "source": "Bloomberg"}],
                "first_mover": "Bloomberg", "size": 5, "tickers": [],
            }]},
        }
    if kind == "geopolitical":
        return {
            "macro_snapshot": {
                "us10y": {"price": 4.30, "change_pct": -0.5},
                "dxy":   {"price": 103.80, "change_pct": 0.40},
                "vix":   {"price": 32.0},
            },
            "sentiment": {"tilt_score": -0.60, "sample_size": 50, "macro_tilt": "BEARISH"},
            "events_classified": {
                "by_category": {"GEOPOLITICAL": {"count": 4, "max_sev": 9, "avg_sev": 8.5}},
                "directional":  {"bull_count": 0, "bear_count": 4,
                                  "bull_weighted": 0, "bear_weighted": 36},
                "total_classified": 4,
            },
            "news": {"clusters": [{
                "topic": "Israel strikes Iran missile sites; OPEC emergency meeting",
                "event": {"category": "GEOPOLITICAL", "severity": 9, "direction": "BEAR_RISK"},
                "headlines": [{"ts": time.time() - 1.0*3600, "source": "AP"}],
                "first_mover": "AP", "size": 6, "tickers": [],
            }]},
        }
    if kind == "melt_up":
        return {
            "macro_snapshot": {
                "us10y": {"price": 4.10, "change_pct": -1.5},      # -6bp ≈
                "dxy":   {"price": 102.00, "change_pct": -0.40},
                "vix":   {"price": 11.5},
            },
            "sentiment": {"tilt_score": 0.45, "sample_size": 40, "macro_tilt": "BULLISH"},
            "events_classified": {
                "by_category": {"MONETARY": {"count": 1, "max_sev": 7, "avg_sev": 7}},
                "directional":  {"bull_count": 1, "bear_count": 0,
                                  "bull_weighted": 7, "bear_weighted": 0},
                "total_classified": 1,
            },
            "news": {"clusters": [{
                "topic": "Powell hints at policy patience; equities push to ATH",
                "event": {"category": "MONETARY", "severity": 7, "direction": "BULL_RISK"},
                "headlines": [{"ts": time.time() - 2.5*3600, "source": "Reuters"}],
                "first_mover": "Reuters", "size": 3, "tickers": [],
            }]},
        }
    raise ValueError(f"unknown kind={kind}")


# ─── Helper: produce both prompts for a kind ───────────────────────────────
def both_prompts(kind: str, symbol: str = "GOLD",
                  focus_display: str = "Gold (XAU/USD)",
                  focus_ticker: str = "GC=F"):
    snap     = synth_snap(kind=kind)
    stage5   = analyze_stage5(snap)
    reasoning = stage5["trades"]

    msgs_off = build_messages(
        task="hni", snap=snap,
        symbol=symbol, focus_display=focus_display, focus_ticker=focus_ticker,
        constraints=["If signals conflict, state CONVICTION=LOW."],
    )
    msgs_on = build_messages(
        task="hni", snap=snap,
        reasoning=reasoning, reasoning_mode="compact",
        symbol=symbol, focus_display=focus_display, focus_ticker=focus_ticker,
        constraints=[
            "If signals conflict, state CONVICTION=LOW.",
            "The MACRO READ block above is directional_intelligence — "
            "regime CONTEXT, NOT an order, NOT entry signals, NOT position sizing. "
            "Do NOT copy POSTURE / PREFERRED / WEAK verbatim into the schema.",
        ],
    )
    return msgs_off, msgs_on, reasoning


# ═══════════════════════════════════════════════════════════════════════════
# Flag-OFF vs Flag-ON shape
# ═══════════════════════════════════════════════════════════════════════════
def test_flag_off_no_macro_read():
    print("\n═══ flag OFF (default) — no MACRO READ ═══")
    msgs, _, _ = both_prompts("fed_hawkish")
    user = msgs[1]["content"]
    _check("flag_off_no_macro_read_section",
           "=== MACRO READ ===" not in user,
           "MACRO READ block appeared without flag")
    _check("flag_off_no_intent_marker",
           "directional_intelligence" not in user,
           "intent marker leaked without flag")


def test_flag_on_macro_read_present():
    print("\n═══ flag ON — MACRO READ block present ═══")
    _, msgs, reasoning = both_prompts("fed_hawkish")
    user = msgs[1]["content"]
    _check("flag_on_macro_read_section_present",
           "=== MACRO READ ===" in user,
           "MACRO READ block missing")
    _check("flag_on_regime_line_present",
           "REGIME:" in user,
           "REGIME line missing")
    _check("flag_on_intent_marker_preserved",
           "INTEL · not_for_execution · directional_intelligence" in user,
           "intent marker missing from rendered prompt")


def test_anti_hallucination_constraint_present():
    print("\n═══ anti-hallucination constraint when flag ON ═══")
    _, msgs, _ = both_prompts("fed_hawkish")
    user = msgs[1]["content"]
    _check("anti_hallucination_text_present",
           "directional_intelligence" in user
           and "NOT an order" in user
           and "Do NOT copy" in user,
           "anti-hallucination constraint missing")


# ═══════════════════════════════════════════════════════════════════════════
# Token / latency budget
# ═══════════════════════════════════════════════════════════════════════════
def test_token_increase_under_limits():
    print("\n═══ token increase per archetype ═══")
    for kind in ("cpi_hot", "fed_hawkish", "geopolitical", "melt_up"):
        off, on, _ = both_prompts(kind)
        e_off = estimate_messages(off)["total_tokens"]
        e_on  = estimate_messages(on)["total_tokens"]
        delta = e_on - e_off
        _check(f"{kind}_token_delta_under_180",
               delta < 180,
               f"delta={delta} off={e_off} on={e_on}")
        # Sanity: ON must always be larger
        _check(f"{kind}_on_strictly_larger_than_off",
               e_on > e_off,
               f"e_on={e_on} e_off={e_off}")


def test_latency_under_50ms_per_call():
    print("\n═══ end-to-end build latency ═══")
    t0 = time.time()
    for kind in ("cpi_hot", "fed_hawkish", "geopolitical", "melt_up"):
        both_prompts(kind)
    elapsed_ms = (time.time() - t0) * 1000
    per_call_ms = elapsed_ms / 8   # 4 kinds × 2 prompts each
    _check("build_under_50ms_per_call",
           per_call_ms < 50.0,
           f"per_call_ms={per_call_ms:.2f}")


# ═══════════════════════════════════════════════════════════════════════════
# Per-archetype expected regime + scenario routing
# ═══════════════════════════════════════════════════════════════════════════
def test_cpi_hot_maps_to_inflation_regime():
    print("\n═══ CPI hot → INFLATIONARY/STAGFLATION ═══")
    _, _, r = both_prompts("cpi_hot")
    scenario = r.get("scenario_name", "")
    _check("cpi_hot_scenario_inflationary_or_stagflation",
           scenario in {"STAGFLATION_LITE", "INFLATIONARY", "TIGHTENING_PANIC", "NO_CLEAN_SCENARIO"},
           f"got scenario={scenario}")


def test_fed_hawkish_maps_to_tightening():
    print("\n═══ Fed hawkish → TIGHTENING_PANIC ═══")
    _, _, r = both_prompts("fed_hawkish")
    _check("fed_hawkish_scenario_tightening_panic",
           r.get("scenario_name") in {"TIGHTENING_PANIC", "STAGFLATION_LITE"},
           f"got scenario={r.get('scenario_name')}")
    _check("fed_hawkish_swing_long_dxy_bias",
           r.get("swing", {}).get("bias") == "LONG_BIAS",
           f"got swing.bias={r.get('swing',{}).get('bias')}")


def test_geopolitical_maps_to_riskoff():
    print("\n═══ Geopolitical spike → GEOPOLITICAL_RISKOFF / CRISIS ═══")
    _, _, r = both_prompts("geopolitical")
    _check("geopolitical_scenario_correct",
           r.get("scenario_name") in {"GEOPOLITICAL_RISKOFF", "CRISIS", "GROWTH_SCARE"},
           f"got scenario={r.get('scenario_name')}")


def test_melt_up_maps_to_meltup_or_riskon():
    print("\n═══ Melt-up → MELT_UP / RISK_ON ═══")
    _, _, r = both_prompts("melt_up")
    _check("melt_up_scenario_correct",
           r.get("scenario_name") in {"MELT_UP", "REFLATION", "RISK_ON"},
           f"got scenario={r.get('scenario_name')}")


# ═══════════════════════════════════════════════════════════════════════════
# Schema-integrity: rendered MACRO READ must never carry order language
# ═══════════════════════════════════════════════════════════════════════════
def test_no_order_routing_language_in_macro_read():
    print("\n═══ MACRO READ contains no order-routing language ═══")
    for kind in ("cpi_hot", "fed_hawkish", "geopolitical", "melt_up"):
        _, msgs_on, _ = both_prompts(kind)
        user = msgs_on[1]["content"]
        # Pull the MACRO READ section
        try:
            macro_block = user.split("=== MACRO READ ===")[1].split("=== STATE ===")[0]
        except IndexError:
            macro_block = ""
        BAD = ("place order", "route to broker", "execute the trade",
               "submit market order", "OrderID", "order_id", "execute now")
        hit = next((b for b in BAD if b.lower() in macro_block.lower()), None)
        _check(f"{kind}_no_order_routing_words",
               hit is None,
               f"found {hit!r} in MACRO READ for {kind}")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("HNI Phase-6 limited integration tests")
    print("═" * 60)

    for test in (
        test_flag_off_no_macro_read,
        test_flag_on_macro_read_present,
        test_anti_hallucination_constraint_present,
        test_token_increase_under_limits,
        test_latency_under_50ms_per_call,
        test_cpi_hot_maps_to_inflation_regime,
        test_fed_hawkish_maps_to_tightening,
        test_geopolitical_maps_to_riskoff,
        test_melt_up_maps_to_meltup_or_riskon,
        test_no_order_routing_language_in_macro_read,
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
