from macro import get_macro_data, format_macro
from news import get_all_news, format_news
from stocks import format_stocks
from econ import get_economic_data
from trade_signal import generate_signal
from interpreter import interpret_macro
from smc import get_smc_analysis, format_smc
from sniper import sniper_entry, format_sniper
from mtf import get_mtf_bias, format_mtf
from structure import get_structure, format_structure_output
from indices import format_indices
from priority import prioritize_news, format_priority_news
from news import get_all_news


def run_terminal():

    print("\n🚀 MINI BLOOMBERG TERMINAL\n")

    # 🔹 DATA COLLECTION
    macro    = format_macro(get_macro_data())
    raw_news = get_all_news()
    news     = format_news([n for _, n in prioritize_news(raw_news)])
    stocks   = format_stocks()
    econ     = get_economic_data()

    # 🔹 DISPLAY RAW DATA
    print("=== MACRO ===")
    print(macro)

    print("=== INDICES ===")
    print(format_indices())

    print("=== STOCKS ===")
    print(stocks)

    print(format_priority_news(raw_news))

    # 🔹 AI BRAIN
    brain = interpret_macro(macro, news, stocks, econ)

    print("\n=== MACRO INSIGHTS ===")
    for i in brain["insights"]:
        print("-", i)

    # 🔹 SIGNAL
    signal = generate_signal(macro, news, stocks, econ)

    print("\n=== MARKET SIGNAL ===")
    print("Decision:", signal["decision"])
    print("Score:",    signal["score"])
    print("Session:",  signal["session"])

    # 🔹 STRUCTURE + MTF
    structure = get_structure()
    print(format_structure_output(structure))

    mtf = get_mtf_bias()
    print(format_mtf(mtf))

    # 🔹 SMC
    smc_data = get_smc_analysis()
    print(format_smc(smc_data))

    # 🔹 SNIPER
    sniper = sniper_entry(signal)
    print(format_sniper(sniper))

    # 🔹 TRADE IDEAS
    print("\n=== TRADE IDEAS ===")

    if signal["decision"] == "BUY":
        print("➡ BUY GOLD (Scalp on pullback)")
        print("➡ Swing: Bullish bias")

    elif signal["decision"] == "SELL":
        print("➡ SELL GOLD (Rally sell)")
        print("➡ Swing: Bearish bias")

    else:
        print("➡ No clear trade — wait for stronger setup")

    print("\n✅ TERMINAL READY\n")


if __name__ == "__main__":
    run_terminal()
