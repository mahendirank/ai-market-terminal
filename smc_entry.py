import yfinance as yf
import pandas as pd
from smc import get_smc_analysis
from structure import get_structure


def _download(symbol="GC=F", period="5d", interval="5m"):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except:
        return pd.DataFrame()


# 🔹 SMC confluence check (BOS + OB alignment)
def smc_entry(signal, smc_data):
    decision        = signal["decision"]
    bos             = smc_data["bos"]
    ob_type, ob_price = smc_data["order_block"]

    entry  = None
    reason = []

    if decision == "BUY" and bos == "BULLISH_BOS":
        if "Bullish" in ob_type:
            entry = ob_price
            reason.append("Bullish OB + BOS alignment")

    elif decision == "SELL" and bos == "BEARISH_BOS":
        if "Bearish" in ob_type:
            entry = ob_price
            reason.append("Bearish OB + BOS alignment")

    else:
        reason.append("No SMC confluence")

    return {
        "entry":  entry,
        "reason": reason,
    }


# 🔹 ATR for SL/TP sizing
def get_atr(df, period=14):
    if df.empty or len(df) < period:
        return None
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return round(float(tr.rolling(period).mean().iloc[-1]), 2)


# 🔹 FVG detection (3-candle imbalance)
def find_fvg(df):
    fvgs = []
    for i in range(1, len(df) - 1):
        prev = df.iloc[i - 1]
        nxt  = df.iloc[i + 1]
        if float(nxt["Low"]) > float(prev["High"]):
            fvgs.append({
                "type":   "BULLISH",
                "top":    round(float(nxt["Low"]), 2),
                "bottom": round(float(prev["High"]), 2),
            })
        if float(nxt["High"]) < float(prev["Low"]):
            fvgs.append({
                "type":   "BEARISH",
                "top":    round(float(prev["Low"]), 2),
                "bottom": round(float(nxt["High"]), 2),
            })
    return fvgs[-3:] if fvgs else []


# 🔹 Check if price is near an OB or FVG (within ATR distance)
def near_zone(price, level, atr):
    if atr is None:
        return False
    return abs(price - level) <= atr * 1.5


# 🔹 Build entry setup
def get_entry_setup(signal_decision):
    df        = _download()
    smc       = get_smc_analysis()
    structure = get_structure()

    if df.empty:
        return None

    price = round(float(df["Close"].iloc[-1]), 2)
    atr   = get_atr(df)
    fvgs  = find_fvg(df)
    bos   = smc["bos"]
    ob    = smc["order_block"]

    # 🔹 Check BOS + OB confluence first
    confluence = smc_entry({"decision": signal_decision}, smc)
    if confluence["entry"]:
        refined_entry = confluence["entry"]
    else:
        refined_entry = None

    # 🔹 Confirm direction
    if signal_decision == "BUY":
        entry = round(price, 2)
        sl    = round(price - atr * 1.5, 2) if atr else round(price - 10, 2)
        tp1   = round(price + atr * 2.0, 2) if atr else round(price + 20, 2)
        tp2   = round(price + atr * 3.5, 2) if atr else round(price + 35, 2)

        # Prefer BOS+OB confluence entry, then FVG, then price
        if refined_entry:
            entry = refined_entry
            sl    = round(entry - atr * 1.5, 2)
        elif ob[0] == "Bullish OB" and near_zone(price, ob[1], atr):
            entry = ob[1]
            sl    = round(entry - atr * 1.5, 2)

        for fvg in fvgs:
            if fvg["type"] == "BULLISH" and near_zone(price, fvg["bottom"], atr):
                entry = fvg["bottom"]
                sl    = round(entry - atr * 1.5, 2)
                break

    elif signal_decision == "SELL":
        entry = round(price, 2)
        sl    = round(price + atr * 1.5, 2) if atr else round(price + 10, 2)
        tp1   = round(price - atr * 2.0, 2) if atr else round(price - 20, 2)
        tp2   = round(price - atr * 3.5, 2) if atr else round(price - 35, 2)

        if refined_entry:
            entry = refined_entry
            sl    = round(entry + atr * 1.5, 2)
        elif ob[0] == "Bearish OB" and near_zone(price, ob[1], atr):
            entry = ob[1]
            sl    = round(entry + atr * 1.5, 2)

        for fvg in fvgs:
            if fvg["type"] == "BEARISH" and near_zone(price, fvg["top"], atr):
                entry = fvg["top"]
                sl    = round(entry + atr * 1.5, 2)
                break

    else:
        return {
            "decision": "NO TRADE",
            "reason":   "Signal score too low — waiting for clearer setup",
        }

    rr = round(abs(tp1 - entry) / abs(sl - entry), 2) if sl != entry else 0

    return {
        "decision":  signal_decision,
        "price":     price,
        "entry":     entry,
        "sl":        sl,
        "tp1":       tp1,
        "tp2":       tp2,
        "atr":       atr,
        "rr":        rr,
        "bos":        bos,
        "ob":         ob,
        "fvg_count":  len(fvgs),
        "confluence": confluence["reason"],
    }


# 🔹 Format for display / AI
def format_entry(data):
    if not data or data.get("decision") == "NO TRADE":
        return "=== SMC ENTRY ===\nNO TRADE — waiting for clearer setup\n"

    text  = "=== SMC ENTRY SETUP ===\n\n"
    text += f"Decision  : {data['decision']}\n"
    text += f"Price     : {data['price']}\n"
    text += f"Entry     : {data['entry']}\n"
    text += f"Stop Loss : {data['sl']}\n"
    text += f"TP1       : {data['tp1']}\n"
    text += f"TP2       : {data['tp2']}\n"
    text += f"ATR       : {data['atr']}\n"
    text += f"R:R       : 1:{data['rr']}\n"
    text += f"BOS       : {data['bos'] or 'None'}\n"
    text += f"OB        : {data['ob'][0]} @ {data['ob'][1]}\n"
    text += f"FVGs      : {data['fvg_count']} detected\n"
    text += f"Confluence: {', '.join(data['confluence'])}\n"
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
    data = get_entry_setup(signal["decision"])
    print(format_entry(data))
