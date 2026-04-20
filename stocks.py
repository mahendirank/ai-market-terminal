import yfinance as yf
import pandas as pd


def _last(symbol, period="5d", interval="1h"):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if not df.empty:
            val = df["Close"].iloc[-1]
            return float(val.item() if hasattr(val, "item") else val)
    except:
        pass
    return None


# 🔹 Magnificent 7
def get_mag7():
    symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
    data = {}
    for s in symbols:
        val = _last(s)
        if val:
            data[s] = round(val, 2)
    return data


# 🔹 Semiconductor focus
def get_semiconductors():
    symbols = ["NVDA", "AMD", "TSM", "INTC"]
    data = {}
    for s in symbols:
        val = _last(s)
        if val:
            data[s] = round(val, 2)
    return data


# 🔹 Detect big movers — returns price + % change
def detect_movers():
    symbols = ["AAPL", "MSFT", "NVDA", "TSLA", "AMD", "GOOGL", "META", "AMZN"]
    movers  = []
    for s in symbols:
        try:
            df = yf.download(s, period="2d", interval="1d", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if len(df) >= 2:
                prev  = float(df["Close"].iloc[-2].item() if hasattr(df["Close"].iloc[-2], "item") else df["Close"].iloc[-2])
                close = float(df["Close"].iloc[-1].item() if hasattr(df["Close"].iloc[-1], "item") else df["Close"].iloc[-1])
                pct   = round((close - prev) / prev * 100, 2)
                arrow = "▲" if pct > 0 else "▼"
                movers.append({"symbol": s, "price": round(close, 2), "change_pct": pct, "arrow": arrow})
        except:
            pass
    return movers


# 🔹 Indian Indices
def get_india_indices():
    symbols = {
        "NIFTY50":    ("^NSEI",    2),
        "BANKNIFTY":  ("^NSEBANK", 2),
        "SENSEX":     ("^BSESN",   2),
        "NIFTYIT":    ("^CNXIT",   2),
    }
    data = {}
    for name, (ticker, dec) in symbols.items():
        val = _last(ticker)
        if val:
            data[name] = round(val, dec)
    return data


# 🔹 Global Indices
def get_global_indices():
    symbols = {
        "SP500":    ("^GSPC",  2),
        "NASDAQ":   ("^IXIC",  2),
        "DOW":      ("^DJI",   2),
        "FTSE100":  ("^FTSE",  2),
        "NIKKEI":   ("^N225",  2),
        "HANGSENG": ("^HSI",   2),
        "DAX":      ("^GDAXI", 2),
        "VIX":      ("^VIX",   2),
    }
    data = {}
    for name, (ticker, dec) in symbols.items():
        val = _last(ticker)
        if val:
            data[name] = round(val, dec)
    return data


# 🔹 Gold-linked ETFs
def get_gold_etfs():
    symbols = {
        "GLD": ("GLD", 2),   # Gold ETF
        "SLV": ("SLV", 2),   # Silver ETF
        "TLT": ("TLT", 2),   # US 20Y Bond ETF (inverse yield signal)
        "UUP": ("UUP", 3),   # DXY proxy
    }
    data = {}
    for name, (ticker, dec) in symbols.items():
        val = _last(ticker)
        if val:
            data[name] = round(val, dec)
    return data


# 🔹 Bloomberg-style format
def format_stocks():
    mag7   = get_mag7()
    semi   = get_semiconductors()
    movers = detect_movers()
    india  = get_india_indices()
    etfs   = get_gold_etfs()

    text = f"\n{'═'*44}\n  MARKET TERMINAL — LIVE DATA\n{'═'*44}\n"

    text += f"\n{'INDIA INDICES':─<44}\n"
    for k, v in india.items():
        text += f"  {k:<12} {v:>10}\n"

    text += f"\n{'MAG7 STOCKS':─<44}\n"
    for k, v in mag7.items():
        text += f"  {k:<8} ${v:>10}\n"

    text += f"\n{'SEMICONDUCTORS':─<44}\n"
    for k, v in semi.items():
        text += f"  {k:<8} ${v:>10}\n"

    text += f"\n{'DAILY MOVERS':─<44}\n"
    if movers:
        for m in movers:
            text += f"  {m['symbol']:<8} ${m['price']:>8}   {m['arrow']} {abs(m['change_pct'])}%\n"
    else:
        text += "  No major movers\n"

    text += f"\n{'GOLD-LINKED ETFs':─<44}\n"
    for k, v in etfs.items():
        text += f"  {k:<8} ${v:>10}\n"

    text += f"{'═'*44}\n"
    return text


if __name__ == "__main__":
    print("📊 Fetching stock market data...\n")
    print(format_stocks())
