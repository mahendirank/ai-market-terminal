import yfinance as yf
import pandas as pd


def get_price_data():
    df = yf.download("GC=F", period="5d", interval="5m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# 🔹 Support / Resistance
def get_levels(df):
    high = df["High"].max()
    low  = df["Low"].min()
    return high, low


# 🔹 Fibonacci levels
def get_fib_levels(high, low):
    diff   = high - low
    levels = {
        "0.236": high - diff * 0.236,
        "0.382": high - diff * 0.382,
        "0.5":   high - diff * 0.5,
        "0.618": high - diff * 0.618,
        "0.786": high - diff * 0.786,
    }
    return levels


# 🔹 Pivot points
def get_pivot(df):
    last  = df.iloc[-1]
    pivot = (last["High"] + last["Low"] + last["Close"]) / 3
    r1    = 2 * pivot - last["Low"]
    s1    = 2 * pivot - last["High"]
    return pivot, r1, s1


# 🔹 Basic SMC (swing high/low)
def get_smc_zones(df):
    highs = df["High"].rolling(10).max()
    lows  = df["Low"].rolling(10).min()
    return highs.iloc[-1], lows.iloc[-1]


# 🔹 Combine everything
def get_structure():
    df = get_price_data()

    high, low       = get_levels(df)
    fib             = get_fib_levels(high, low)
    pivot, r1, s1   = get_pivot(df)
    smc_high, smc_low = get_smc_zones(df)

    return {
        "high":     high,
        "low":      low,
        "fib":      fib,
        "pivot":    pivot,
        "r1":       r1,
        "s1":       s1,
        "smc_high": smc_high,
        "smc_low":  smc_low,
    }


# 🔹 Format for AI / display
def format_structure_output(data):
    text  = "=== PRICE STRUCTURE ===\n\n"
    text += f"Range  : High {round(float(data['high']), 2)} | Low {round(float(data['low']), 2)}\n"
    text += f"Pivot  : {round(float(data['pivot']), 2)}  R1: {round(float(data['r1']), 2)}  S1: {round(float(data['s1']), 2)}\n"
    text += f"SMC    : Swing High {round(float(data['smc_high']), 2)} | Swing Low {round(float(data['smc_low']), 2)}\n\n"
    text += "FIBONACCI LEVELS:\n"
    for k, v in data["fib"].items():
        text += f"  {k} : {round(float(v), 2)}\n"
    return text


if __name__ == "__main__":
    print("📐 Fetching price structure for Gold (GC=F)...\n")
    data = get_structure()
    print(format_structure_output(data))
