import yfinance as yf
import pandas as pd


def get_data():
    df = yf.download("GC=F", period="5d", interval="5m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.reset_index(inplace=True)
    return df


# 🔹 Break of Structure (BOS)
def detect_bos(df):
    highs = df["High"]
    lows  = df["Low"]

    bos = None

    if highs.iloc[-1] > highs.iloc[-5]:
        bos = "BULLISH_BOS"

    elif lows.iloc[-1] < lows.iloc[-5]:
        bos = "BEARISH_BOS"

    return bos


# 🔹 Liquidity zones (equal highs/lows)
def detect_liquidity(df):
    highs     = df["High"]
    lows      = df["Low"]
    liquidity = []

    if abs(float(highs.iloc[-1]) - float(highs.iloc[-2])) < 1:
        liquidity.append("Equal Highs (Sell-side liquidity)")

    if abs(float(lows.iloc[-1]) - float(lows.iloc[-2])) < 1:
        liquidity.append("Equal Lows (Buy-side liquidity)")

    return liquidity


# 🔹 Order Block (last opposite candle)
def detect_order_block(df):
    last = df.iloc[-2]

    if float(last["Close"]) < float(last["Open"]):
        return ("Bullish OB", round(float(last["Low"]), 2))
    else:
        return ("Bearish OB", round(float(last["High"]), 2))


# 🔹 Combine SMC
def get_smc_analysis():
    df        = get_data()
    bos       = detect_bos(df)
    liquidity = detect_liquidity(df)
    ob        = detect_order_block(df)

    return {
        "bos":         bos,
        "liquidity":   liquidity,
        "order_block": ob,
    }


# 🔹 Format for AI / display
def format_smc(data):
    text  = "=== SMC ANALYSIS ===\n\n"
    text += f"BOS          : {data['bos'] or 'None'}\n"
    text += f"Order Block  : {data['order_block'][0]} @ {data['order_block'][1]}\n"
    text += "Liquidity    :\n"
    if data["liquidity"]:
        for l in data["liquidity"]:
            text += f"  - {l}\n"
    else:
        text += "  - No equal highs/lows detected\n"
    return text


if __name__ == "__main__":
    print("🔍 Running SMC analysis for Gold (GC=F)...\n")
    data = get_smc_analysis()
    print(format_smc(data))
