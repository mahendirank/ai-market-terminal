"""
macro_integration_examples.py — Side-by-side HNI prompt snapshots for 4
canonical market events. Use for visual diff review of what changes when
ENABLE_MACRO_REASONING is flipped on.

Run:
    docker exec market-terminal python /app/tests/macro_integration_examples.py

Produces stdout output, no files written. Pure synthetic data — no API
calls to yfinance / Groq / Redis.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from prompt_builder import build_messages, estimate_messages
from macro_reasoning_engine import analyze_stage5

# Reuse the synth_snap builder from the integration test
from tests.test_hni_macro_integration import synth_snap


def render_pair(label: str, kind: str, *, symbol="GOLD",
                focus_display="Gold (XAU/USD)", focus_ticker="GC=F") -> None:
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
    e_off = estimate_messages(msgs_off)
    e_on  = estimate_messages(msgs_on)

    print()
    print("═" * 78)
    print(f"  {label}")
    print(f"  archetype={kind}   scenario_matched={reasoning.get('scenario_name','?')}")
    print(f"  confidence={reasoning.get('overall_confidence')}   "
          f"driver={reasoning.get('dominant_driver')}")
    print("═" * 78)

    # User-message diff — system message is identical across both
    user_off = msgs_off[1]["content"]
    user_on  = msgs_on[1]["content"]

    print()
    print("─── WITHOUT MACRO BLOCK ─── (flag off)")
    print(f"  tokens(sys/user/total) = {e_off['messages'][0]['tokens']} / "
          f"{e_off['messages'][1]['tokens']} / {e_off['total_tokens']}")
    print(f"  user-msg first 200 chars:")
    print("    " + user_off[:200].replace("\n", "\n    "))

    print()
    print("─── WITH MACRO BLOCK ─── (flag on, reasoning_mode=compact)")
    print(f"  tokens(sys/user/total) = {e_on['messages'][0]['tokens']} / "
          f"{e_on['messages'][1]['tokens']} / {e_on['total_tokens']}    "
          f"Δ_total=+{e_on['total_tokens']-e_off['total_tokens']}")
    # Extract the MACRO READ block alone
    if "=== MACRO READ ===" in user_on:
        macro = user_on.split("=== MACRO READ ===")[1]
        # Stop at the next === header
        if "=== STATE ===" in macro:
            macro = macro.split("=== STATE ===")[0]
        print("  rendered MACRO READ block:")
        for line in macro.strip().splitlines():
            print(f"    {line}")
    else:
        print("  (no MACRO READ block — unexpected)")

    # Directional intelligence sanity
    print()
    print("  hallucination-check assertions:")
    print(f"    intent='{reasoning.get('intent')}'  "
          f"not_for_execution={reasoning.get('not_for_execution')}  "
          f"output_schema_version={reasoning.get('output_schema_version')}")
    print(f"    intent marker in rendered prompt: "
          f"{'YES' if 'not_for_execution' in user_on else 'NO'}")
    print(f"    anti-hallucination constraint present: "
          f"{'YES' if 'Do NOT copy' in user_on else 'NO'}")


def main():
    print()
    print("HNI Phase-6 — side-by-side prompts (WITHOUT vs WITH macro block)")
    print("ENABLE_MACRO_REASONING — gates this integration in production")
    print("All inputs are synthetic. No Groq, no yfinance, no broker.")

    render_pair("1. CPI HOT PRINT",           "cpi_hot")
    render_pair("2. FED HAWKISH SURPRISE",    "fed_hawkish")
    render_pair("3. GEOPOLITICAL SPIKE",      "geopolitical")
    render_pair("4. RISK-ON MELT-UP",         "melt_up")

    print()
    print("═" * 78)
    print("Done. Use these prompts to compare AI tab output quality once enabled.")
    print("═" * 78)


if __name__ == "__main__":
    main()
