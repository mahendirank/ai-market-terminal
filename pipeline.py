from loader import detect_domains, load_skills, run_qwen
from agents import planner_agent, executor_agent, reviewer_agent
from executor import handle_output
from ppt_generator import create_ppt
from data import get_gold_data, format_data
from backtest import backtest_ema
from macro_brain import get_macro_brain, format_brain
from econ import get_economic_data
from structure import get_structure
from reversal import analyze_reversal
from smc import get_smc_analysis
from smc_entry import smc_entry
from priority import format_priority_news
from mtf import get_mtf_bias, format_mtf
from sniper import sniper_entry


def run_pipeline(user_input):

    print("\n🚀 STARTING AI PIPELINE...\n")

    # Step 1 — Plan
    plan = run_qwen(planner_agent(user_input))
    print("=== PLAN ===\n", plan)

    # Step 2 — Load skills + live data
    domains = detect_domains(user_input)
    skills  = load_skills(domains)

    if "trading" in domains:
        print("\n📡 Fetching live gold data...\n")
        market_data = format_data(get_gold_data())

        print("\n📊 Running backtest...\n")
        profit, bt_data = backtest_ema()
        print("=== BACKTEST RESULT ===")
        print("Profit Score:", profit)

        print("\n🧠 Running Macro Brain...\n")
        econ_events  = get_economic_data()
        brain        = get_macro_brain(econ_events)
        priority_txt = format_priority_news(brain.get("news", "").split("\n") if isinstance(brain.get("news"), str) else [])
        signal      = brain["signal"]

        structure = get_structure()
        reversal  = analyze_reversal(structure, signal)

        print("\n=== REVERSAL ANALYSIS ===")
        print("Direction:", reversal["direction"])
        print("Entry Zone:", reversal["entry_zone"])
        print("Key Zones:")
        for z in reversal["zones"]:
            print("-", z)

        mtf       = get_mtf_bias()
        mtf_txt   = format_mtf(mtf)
        print(mtf_txt)

        smc_data  = get_smc_analysis()
        smc_trade = smc_entry(signal, smc_data)

        print("\n=== SMC ANALYSIS ===")
        print("BOS:", smc_data["bos"])
        print("Liquidity:", smc_data["liquidity"])
        print("Order Block:", smc_data["order_block"])

        print("\n=== SMC ENTRY ===")
        print("Entry:", smc_trade["entry"])
        print("Reason:", smc_trade["reason"])

        sniper = sniper_entry(signal)

        print("\n=== SNIPER ENTRY ===")
        print("HTF:", sniper["htf"])
        print("Liquidity Sweep:", sniper["sweep"])
        print("Entry:", sniper["entry"])
        print("Reason:", sniper["reason"])

        print("\n=== TRADE SIGNAL ===")
        print(f"Decision: {signal['decision']}")
        print(f"Score: {signal['score']}")
        print(f"Session: {signal['session']}")
        print(f"Insights:")
        for i in signal["insights"]:
            print("-", i)

        user_input = f"""
LIVE MARKET DATA:
{market_data}

BACKTEST RESULT (EMA 20/50 — 1 month):
Profit Score: {profit}

{priority_txt}

{mtf_txt}

SMC ANALYSIS:
BOS: {smc_data["bos"]}
Order Block: {smc_data["order_block"]}
Liquidity: {smc_data["liquidity"]}

SMC ENTRY:
Entry: {smc_trade["entry"]}
Reason: {smc_trade["reason"]}

SNIPER ENTRY:
HTF: {sniper["htf"]}
Sweep: {sniper["sweep"]}
Entry: {sniper["entry"]}
Reason: {sniper["reason"]}

{format_brain(brain)}

TASK:
{user_input}
"""

    # Step 3 — Execute
    execution = run_qwen(executor_agent(user_input, plan, skills))
    print("\n=== EXECUTION ===\n", execution)

    # Step 4 — Review
    final_output = run_qwen(reviewer_agent(user_input, execution))
    print("\n=== FINAL OUTPUT ===\n", final_output)

    # Step 5 — Save outputs
    handle_output(user_input, final_output)

    # Step 6 — PPT if required
    if "ppt" in user_input.lower():
        create_ppt(final_output)

    if "trading" in domains:
        print("\n=== BACKTEST RESULT ===")
        print("Profit Score:", profit)

    print("\n✅ PIPELINE COMPLETED\n")


if __name__ == "__main__":
    task = input("Enter full project task: ")
    run_pipeline(task)
