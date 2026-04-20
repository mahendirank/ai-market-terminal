import sys
if sys.platform != "win32":
    print("⚠️  mt5_bot.py requires Windows + MetaTrader5. Use webhook.py on Mac.")
    sys.exit(0)

import MetaTrader5 as mt5
from notify import send_telegram
from trade_signal import generate_signal
from macro import get_macro_data, format_macro
from news import get_all_news, format_news
from stocks import format_stocks
from logger import log_trade
from smc import get_smc_analysis
from smc_entry import smc_entry
from sniper import sniper_entry

def connect_mt5():
    if not mt5.initialize():
        print("❌ MT5 connection failed")
        return False
    print("✅ MT5 Connected")
    return True


def get_price(symbol="XAUUSD"):
    tick = mt5.symbol_info_tick(symbol)
    return tick.ask if tick else None


def place_trade(symbol="XAUUSD", lot=0.01, order_type="buy"):
    price = get_price(symbol)

    if price is None:
        print("❌ Price fetch failed")
        return

    sl = price - 10 if order_type == "buy" else price + 10
    tp = price + 20 if order_type == "buy" else price - 20

    order_type_mt5 = mt5.ORDER_BUY if order_type == "buy" else mt5.ORDER_SELL

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type_mt5,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": 10001,
        "comment": "AI Trade",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    print("Trade Result:", result)
    send_telegram(f"Trade executed: {order_type}")


def simple_strategy():  # TEMP: replace random with AI decision later
    import random

    # TEMP: Replace later with AI decision
    decision = random.choice(["buy", "sell"])

    print("AI Decision:", decision)

    place_trade(order_type=decision)


def ai_strategy():
    send_telegram("🤖 AI analyzing gold market...")

    macro_data = format_macro(get_macro_data())
    news_data  = format_news(get_all_news())
    stock_data = format_stocks()

    signal = generate_signal(macro_data, news_data, stock_data)

    print("\n=== TRADE SIGNAL ===")
    print("Decision:", signal["decision"])
    print("Score:",    signal["score"])
    print("Reasons:",  signal["reasons"])

    if signal["decision"] == "BUY":
        send_telegram(f"📈 BUY | Score: {signal['score']} | {signal['reasons']}")
        place_trade(order_type="buy")
    elif signal["decision"] == "SELL":
        send_telegram(f"📉 SELL | Score: {signal['score']} | {signal['reasons']}")
        place_trade(order_type="sell")
    else:
        send_telegram("⚠️ NO TRADE — signal unclear")
        print("No clear decision")


def ai_trade():
    macro   = format_macro(get_macro_data())
    news    = format_news(get_all_news())
    stocks  = format_stocks()

    signal  = generate_signal(macro, news, stocks)

    print("Decision:", signal["decision"])

    price  = get_price()
    sniper = sniper_entry(signal)

    if sniper["entry"] is not None:
        if signal["decision"] == "BUY":
            place_trade(order_type="buy")
            log_trade(signal, price)
        elif signal["decision"] == "SELL":
            place_trade(order_type="sell")
            log_trade(signal, price)
    else:
        print("No sniper setup → No trade")


if __name__ == "__main__":
    if connect_mt5():
        ai_trade()
