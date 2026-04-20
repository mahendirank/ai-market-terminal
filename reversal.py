def _f(v):
    try:
        return round(float(v), 2)
    except:
        return v


def analyze_reversal(structure, macro_signal):
    zones = []
    bias  = macro_signal["decision"]

    # 🔹 Fib confluence
    for k, v in structure["fib"].items():
        zones.append((k, _f(v)))

    # 🔹 SMC zones
    zones.append(("SMC_HIGH", _f(structure["smc_high"])))
    zones.append(("SMC_LOW",  _f(structure["smc_low"])))

    # 🔹 Decision logic
    if bias == "BUY":
        entry     = _f(structure["s1"])
        direction = "Buy from support"

    elif bias == "SELL":
        entry     = _f(structure["r1"])
        direction = "Sell from resistance"

    else:
        entry     = None
        direction = "No clear setup"

    return {
        "direction":  direction,
        "entry_zone": entry,
        "zones":      zones,
    }


# 🔹 Format for AI / display
def format_reversal(data):
    text  = "=== REVERSAL ANALYSIS ===\n\n"
    text += f"Direction  : {data['direction']}\n"
    text += f"Entry Zone : {round(float(data['entry_zone']), 2) if data['entry_zone'] is not None else 'N/A'}\n\n"
    text += "KEY ZONES:\n"
    for label, level in data["zones"]:
        text += f"  {label:8s} : {round(float(level), 2)}\n"
    return text


if __name__ == "__main__":
    from structure import get_structure
    from trade_signal import generate_signal
    from macro import get_macro_data, format_macro
    from news import get_all_news, format_news
    from stocks import format_stocks
    from econ import get_economic_data

    structure    = get_structure()
    macro_text   = format_macro(get_macro_data())
    news_text    = format_news(get_all_news())
    stock_text   = format_stocks()
    econ_events  = get_economic_data()
    macro_signal = generate_signal(macro_text, news_text, stock_text, econ_events)

    result = analyze_reversal(structure, macro_signal)
    print(format_reversal(result))
