import yfinance as yf
import pandas as pd


def _download(symbol, period="2d", interval="1d"):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except:
        return pd.DataFrame()


def get_indices():
    symbols = {
        "SPX":    "^GSPC",
        "NASDAQ": "^IXIC",
        "DOW":    "^DJI",
        "DAX":    "^GDAXI",
        "FTSE":   "^FTSE",
        "NIKKEI": "^N225",
        "HSI":    "^HSI",
        "NIFTY":  "^NSEI",
        "SENSEX": "^BSESN",
        "VIX":    "^VIX",
    }

    data = {}
    for k, v in symbols.items():
        df = _download(v)
        if not df.empty and len(df) >= 2:
            price = round(float(df["Close"].iloc[-1]), 2)
            prev  = round(float(df["Close"].iloc[-2]), 2)
            pct   = round((price - prev) / prev * 100, 2)
            arrow = "▲" if pct > 0 else "▼"
            data[k] = {"price": price, "change": pct, "arrow": arrow}
        elif not df.empty:
            price = round(float(df["Close"].iloc[-1]), 2)
            data[k] = {"price": price, "change": 0.0, "arrow": "─"}

    return data


def format_indices():
    data = get_indices()

    text  = f"\n{'═'*44}\n  GLOBAL INDICES\n{'═'*44}\n"
    text += f"  {'INDEX':<10} {'PRICE':>10}   {'CHG':>7}\n"
    text += f"  {'─'*40}\n"

    for k, v in data.items():
        text += f"  {k:<10} {v['price']:>10}   {v['arrow']} {abs(v['change'])}%\n"

    text += f"{'═'*44}\n"
    return text


if __name__ == "__main__":
    print("📊 Fetching global indices...\n")
    print(format_indices())
