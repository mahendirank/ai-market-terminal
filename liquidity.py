import yfinance as yf
import pandas as pd


def _download(symbol="GC=F", period="5d", interval="5m"):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except:
        return pd.DataFrame()


# 🔹 Liquidity sweep (stop hunt detection)
def detect_sweep(df):
    if df is None or df.empty or len(df) < 3:
        return None

    high = df["High"]
    low  = df["Low"]

    if float(high.iloc[-1]) > float(high.iloc[-3]) and float(high.iloc[-1]) < float(high.iloc[-2]):
        return "SELL_SIDE_SWEEP"

    if float(low.iloc[-1]) < float(low.iloc[-3]) and float(low.iloc[-1]) > float(low.iloc[-2]):
        return "BUY_SIDE_SWEEP"

    return None


# 🔹 Equal highs / equal lows (liquidity pools)
def detect_equal_levels(df, tolerance=0.5):
    highs     = df["High"]
    lows      = df["Low"]
    eq_highs  = []
    eq_lows   = []

    for i in range(len(highs) - 1):
        if abs(float(highs.iloc[i]) - float(highs.iloc[-1])) < tolerance:
            eq_highs.append(round(float(highs.iloc[i]), 2))
        if abs(float(lows.iloc[i]) - float(lows.iloc[-1])) < tolerance:
            eq_lows.append(round(float(lows.iloc[i]), 2))

    return {
        "equal_highs": eq_highs[-3:] if eq_highs else [],
        "equal_lows":  eq_lows[-3:]  if eq_lows  else [],
    }


# 🔹 Swing highs / lows (liquidity targets above/below)
def detect_swing_levels(df, lookback=20):
    if len(df) < lookback:
        return {}
    recent = df.tail(lookback)
    swing_high = round(float(recent["High"].max()), 2)
    swing_low  = round(float(recent["Low"].min()),  2)
    return {
        "swing_high": swing_high,
        "swing_low":  swing_low,
    }


# 🔹 Full liquidity analysis
def get_liquidity_analysis(symbol="GC=F"):
    df     = _download(symbol)
    sweep  = detect_sweep(df)
    levels = detect_equal_levels(df)
    swings = detect_swing_levels(df)

    return {
        "sweep":       sweep,
        "equal_highs": levels["equal_highs"],
        "equal_lows":  levels["equal_lows"],
        "swing_high":  swings.get("swing_high"),
        "swing_low":   swings.get("swing_low"),
    }


# 🔹 Format for AI / display
def format_liquidity(data):
    text  = "=== LIQUIDITY ANALYSIS ===\n\n"
    text += f"Sweep        : {data['sweep'] or 'None detected'}\n"
    text += f"Swing High   : {data['swing_high']}\n"
    text += f"Swing Low    : {data['swing_low']}\n"
    text += f"Equal Highs  : {data['equal_highs'] or 'None'}\n"
    text += f"Equal Lows   : {data['equal_lows']  or 'None'}\n"
    return text


if __name__ == "__main__":
    print("💧 Running liquidity analysis for Gold (GC=F)...\n")
    data = get_liquidity_analysis()
    print(format_liquidity(data))
