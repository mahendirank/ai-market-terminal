import yfinance as yf
import pandas as pd


def _download(symbol="GC=F", period="5d", interval="1h"):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except:
        return pd.DataFrame()


# 🔹 HTF trend (1h) — above/below mean
def get_htf():
    df = _download("GC=F", period="5d", interval="1h")
    if df.empty:
        return "NEUTRAL"
    price = float(df["Close"].iloc[-1])
    mean  = float(df["Close"].mean())
    return "BULLISH" if price > mean else "BEARISH"


# 🔹 LTF data (5m) — for entry timing
def get_ltf():
    return _download("GC=F", period="5d", interval="5m")


# 🔹 LTF trend — EMA20 vs EMA50
def get_ltf_bias(df=None):
    if df is None:
        df = get_ltf()
    if df.empty or len(df) < 50:
        return "NEUTRAL"
    ema20 = float(df["Close"].ewm(span=20).mean().iloc[-1])
    ema50 = float(df["Close"].ewm(span=50).mean().iloc[-1])
    if ema20 > ema50:
        return "BULLISH"
    elif ema20 < ema50:
        return "BEARISH"
    return "NEUTRAL"


# 🔹 MTF confluence — HTF + LTF must agree
def get_mtf_bias():
    htf = get_htf()
    ltf = get_ltf_bias()

    if htf == "BULLISH" and ltf == "BULLISH":
        confluence = "STRONG BULLISH"
    elif htf == "BEARISH" and ltf == "BEARISH":
        confluence = "STRONG BEARISH"
    elif htf == "BULLISH" and ltf == "BEARISH":
        confluence = "PULLBACK (HTF bull, LTF bear — wait for LTF flip)"
    elif htf == "BEARISH" and ltf == "BULLISH":
        confluence = "COUNTER-TREND (HTF bear, LTF bull — risky)"
    else:
        confluence = "NEUTRAL"

    return {
        "htf":         htf,
        "ltf":         ltf,
        "confluence":  confluence,
    }


# 🔹 Format for AI / display
def format_mtf(data):
    text  = "=== MULTI-TIMEFRAME ANALYSIS ===\n\n"
    text += f"HTF (1h)     : {data['htf']}\n"
    text += f"LTF (5m)     : {data['ltf']}\n"
    text += f"Confluence   : {data['confluence']}\n"
    return text


if __name__ == "__main__":
    print("📊 Running MTF analysis for Gold (GC=F)...\n")
    data = get_mtf_bias()
    print(format_mtf(data))
