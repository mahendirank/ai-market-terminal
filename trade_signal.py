import yfinance as yf
import pandas as pd


def _download(symbol, period="1mo", interval="1h"):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except:
        return pd.DataFrame()


# 🔹 EMA crossover signal
def ema_signal(df):
    if df.empty or len(df) < 50:
        return "NEUTRAL"
    df["EMA20"] = df["Close"].ewm(span=20).mean()
    df["EMA50"] = df["Close"].ewm(span=50).mean()
    last = df.iloc[-1]
    if last["EMA20"] > last["EMA50"]:
        return "BUY"
    elif last["EMA20"] < last["EMA50"]:
        return "SELL"
    return "NEUTRAL"


# 🔹 RSI signal
def rsi_signal(df, period=14):
    if df.empty or len(df) < period + 1:
        return "NEUTRAL"
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    rsi   = 100 - (100 / (1 + rs))
    val   = float(rsi.iloc[-1])
    if val < 30:
        return "BUY"       # oversold
    elif val > 70:
        return "SELL"      # overbought
    return "NEUTRAL"


# 🔹 VWAP signal (intraday)
def vwap_signal(df):
    if df.empty or "Volume" not in df.columns:
        return "NEUTRAL"
    try:
        df = df.copy()
        df["TP"]   = (df["High"] + df["Low"] + df["Close"]) / 3
        df["VWAP"] = (df["TP"] * df["Volume"]).cumsum() / df["Volume"].cumsum()
        price = float(df["Close"].iloc[-1])
        vwap  = float(df["VWAP"].iloc[-1])
        if price > vwap:
            return "BUY"
        elif price < vwap:
            return "SELL"
    except:
        pass
    return "NEUTRAL"


# 🔹 Structure Break (BOS) — simple higher high / lower low
def bos_signal(df):
    if df.empty or len(df) < 10:
        return "NEUTRAL"
    highs = df["High"].values
    lows  = df["Low"].values
    if highs[-1] > highs[-2] and highs[-2] > highs[-3]:
        return "BUY"     # bullish BOS
    if lows[-1] < lows[-2] and lows[-2] < lows[-3]:
        return "SELL"    # bearish BOS
    return "NEUTRAL"


# 🔹 Vote — combine all signals
def get_combined_signal(symbol="GC=F"):
    df_h  = _download(symbol, period="1mo",  interval="1h")
    df_5m = _download(symbol, period="5d",   interval="5m")

    signals = {
        "EMA_20_50":  ema_signal(df_h),
        "RSI_14":     rsi_signal(df_h),
        "VWAP":       vwap_signal(df_5m),
        "BOS":        bos_signal(df_5m),
    }

    buys  = sum(1 for v in signals.values() if v == "BUY")
    sells = sum(1 for v in signals.values() if v == "SELL")

    if buys >= 3:
        final = "STRONG BUY"
    elif buys == 2:
        final = "BUY"
    elif sells >= 3:
        final = "STRONG SELL"
    elif sells == 2:
        final = "SELL"
    else:
        final = "NEUTRAL"

    return {
        "symbol":   symbol,
        "signals":  signals,
        "verdict":  final,
        "score":    f"{buys}B / {sells}S"
    }


import datetime
from learning import adjust_strategy
from interpreter import interpret_macro


# 🔹 Session filter (important)
def get_session():
    hour = datetime.datetime.utcnow().hour

    if 6 <= hour < 12:
        return "London"
    elif 12 <= hour < 17:
        return "New York"
    else:
        return "Asia"


# 🔹 Volatility filter (simple)
def check_volatility(macro_text):
    if "high impact" in macro_text.lower() or "inflation" in macro_text.lower():
        return "HIGH"
    return "NORMAL"


# 🔹 News impact scoring
def score_news(news_text):
    score = 0
    news = news_text.lower()

    if "fed" in news or "interest rate" in news:
        score += 2
    if "inflation" in news:
        score += 2
    if "war" in news or "geopolitical" in news:
        score += 3
    if "recession" in news:
        score += 2
    if "strong economy" in news:
        score -= 1

    return score


# 🔹 Macro scoring
def score_macro(macro_text):
    score = 0
    text = macro_text.lower()

    if "dxy" in text:
        if "strong" in text:
            score -= 2
        elif "weak" in text:
            score += 2

    if "10y" in text:
        if "rising" in text:
            score -= 2
        elif "falling" in text:
            score += 2

    if "oil" in text:
        if "rising" in text:
            score += 1

    return score


# 🔹 Stock sentiment
def score_stocks(stock_text):
    score = 0
    text = stock_text.lower()

    if "selloff" in text or "crash" in text:
        score += 2
    if "rally" in text:
        score -= 2

    return score


# 🔹 FINAL SIGNAL ENGINE
def generate_signal(macro_text, news_text, stock_text, econ_events):

    session    = get_session()
    volatility = check_volatility(macro_text)

    brain = interpret_macro(macro_text, news_text, stock_text, econ_events)

    total_score = brain["score"]
    insights    = brain["insights"]

    # 🔹 Learning bias from past trades
    learning = adjust_strategy()
    total_score += learning["bias"]

    # 🔹 Session weighting
    if session == "London":
        total_score *= 1.2
    elif session == "New York":
        total_score *= 1.5

    # 🔹 Volatility adjustment
    if volatility == "HIGH":
        total_score *= 1.3

    # 🔹 Decision logic
    if abs(total_score) < 2:
        decision = "NO TRADE"
    elif total_score >= 3:
        decision = "BUY"
    elif total_score <= -3:
        decision = "SELL"
    else:
        decision = "NO TRADE"

    return {
        "decision":   decision,
        "score":      round(total_score, 2),
        "session":    session,
        "volatility": volatility,
        "insights":   insights
    }


# 🔹 Entry levels
def get_entry_levels(symbol="GC=F"):
    df = _download(symbol, period="5d", interval="5m")
    if df.empty:
        return {}
    price = float(df["Close"].iloc[-1])
    atr   = float((df["High"] - df["Low"]).rolling(14).mean().iloc[-1])
    return {
        "price":  round(price, 2),
        "sl_buy": round(price - atr * 1.5, 2),
        "tp_buy": round(price + atr * 3.0, 2),
        "sl_sel": round(price + atr * 1.5, 2),
        "tp_sel": round(price - atr * 3.0, 2),
        "atr":    round(atr, 2),
    }


# 🔹 Format for AI / display
def format_signal(result, levels):
    verdict = result["verdict"]
    emoji   = "🟢" if "BUY" in verdict else "🔴" if "SELL" in verdict else "⚪"
    text    = f"SIGNAL ENGINE — {result['symbol']}\n\n"
    text   += f"VERDICT: {emoji} {verdict}  ({result['score']})\n\n"
    text   += "INDICATORS:\n"
    for k, v in result["signals"].items():
        icon = "✅" if v == "BUY" else "❌" if v == "SELL" else "➖"
        text += f"  {icon} {k}: {v}\n"
    if levels:
        text += f"\nENTRY LEVELS:\n"
        text += f"  Price : {levels['price']}\n"
        text += f"  SL    : {levels['sl_buy']} (long) / {levels['sl_sel']} (short)\n"
        text += f"  TP    : {levels['tp_buy']} (long) / {levels['tp_sel']} (short)\n"
        text += f"  ATR   : {levels['atr']}\n"
    return text


if __name__ == "__main__":
    print("⚡ Running signal engine...\n")
    result = get_combined_signal("GC=F")
    levels = get_entry_levels("GC=F")
    print(format_signal(result, levels))
