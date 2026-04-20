from mtf import get_htf, get_ltf
from liquidity import detect_sweep
from smc import get_smc_analysis


def sniper_entry(signal):

    htf      = get_htf()
    df       = get_ltf()
    sweep    = detect_sweep(df)
    smc      = get_smc_analysis()
    decision = signal["decision"]

    entry   = None
    reasons = []

    # 🔹 BUY setup
    if decision == "BUY" and htf == "BULLISH":
        if sweep == "BUY_SIDE_SWEEP" and smc["bos"] == "BULLISH_BOS":
            entry = smc["order_block"][1]
            reasons.append("Liquidity sweep + BOS + OB alignment")

    # 🔹 SELL setup
    elif decision == "SELL" and htf == "BEARISH":
        if sweep == "SELL_SIDE_SWEEP" and smc["bos"] == "BEARISH_BOS":
            entry = smc["order_block"][1]
            reasons.append("Liquidity sweep + BOS + OB alignment")

    else:
        reasons.append("No sniper confluence")

    return {
        "entry":  entry,
        "htf":    htf,
        "sweep":  sweep,
        "reason": reasons,
    }


# 🔹 Format for AI / display
def format_sniper(data):
    text  = "=== SNIPER ENTRY ===\n\n"
    text += f"HTF Bias  : {data['htf']}\n"
    text += f"Sweep     : {data['sweep'] or 'None'}\n"
    text += f"Entry     : {data['entry'] or 'No setup'}\n"
    text += f"Reason    : {', '.join(data['reason'])}\n"
    return text


if __name__ == "__main__":
    from trade_signal import generate_signal
    from macro import get_macro_data, format_macro
    from news import get_all_news, format_news
    from stocks import format_stocks
    from econ import get_economic_data

    macro_text  = format_macro(get_macro_data())
    news_text   = format_news(get_all_news())
    stock_text  = format_stocks()
    econ_events = get_economic_data()
    signal      = generate_signal(macro_text, news_text, stock_text, econ_events)

    print(f"Signal: {signal['decision']} | Score: {signal['score']}\n")
    result = sniper_entry(signal)
    print(format_sniper(result))
