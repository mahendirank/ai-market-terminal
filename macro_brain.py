import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from macro import get_macro_data, format_macro
from news import get_all_news, format_news
from stocks import format_stocks
from econ import get_econ_data, format_econ, get_economic_data
from trade_signal import generate_signal
from interpreter import interpret_macro
from surprise import calculate_surprise


def get_macro_brain(econ_events=None):
    macro_data  = format_macro(get_macro_data())
    news_data   = format_news(get_all_news())
    stock_data  = format_stocks()
    econ_data   = format_econ(get_econ_data())
    if econ_events is None:
        econ_events = get_economic_data()
    surprise    = calculate_surprise(econ_events)
    signal      = generate_signal(macro_data, news_data, stock_data, econ_events)

    return {
        "macro":    macro_data,
        "news":     news_data,
        "stocks":   stock_data,
        "econ":     econ_data,
        "surprise": surprise,
        "signal":   signal,
    }


def format_brain(brain):
    signal = brain["signal"]
    text   = "=== MACRO BRAIN ===\n\n"
    text  += brain["macro"] + "\n"
    text  += brain["stocks"] + "\n"
    text  += brain["econ"] + "\n"
    text  += "LIVE NEWS:\n" + brain["news"] + "\n\n"
    text  += "=== SURPRISE SCORE ===\n"
    text  += f"Score: {brain['surprise']['score']}\n"
    for i in brain["surprise"]["insights"]:
        text += f"  - {i}\n"
    text  += "\n=== SIGNAL ===\n"
    text  += f"Decision   : {signal['decision']}\n"
    text  += f"Score      : {signal['score']}\n"
    text  += f"Session    : {signal['session']}\n"
    text  += f"Volatility : {signal['volatility']}\n"
    text  += "Insights:\n"
    for i in signal["insights"]:
        text += f"  - {i}\n"
    return text


if __name__ == "__main__":
    print("🧠 Running Macro Brain...\n")
    brain = get_macro_brain()
    print(format_brain(brain))
