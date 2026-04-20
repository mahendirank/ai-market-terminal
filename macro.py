import yfinance as yf


def _last(symbol, period="5d", interval="1h"):
    """Fetch last available close for a symbol. Returns None on failure."""
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if not df.empty:
            return float(df["Close"].iloc[-1].item() if hasattr(df["Close"].iloc[-1], "item") else df["Close"].iloc[-1])
    except:
        pass
    return None


# 🔹 Currencies
def get_fx_data():
    symbols = {
        "DXY":    ("UUP",        3),   # Invesco DXY ETF proxy (tracks DX index)
        "EURUSD": ("EURUSD=X",  4),
        "GBPUSD": ("GBPUSD=X",  4),
        "USDJPY": ("JPY=X",     3),
    }

    data = {}
    for name, (ticker, decimals) in symbols.items():
        val = _last(ticker)
        if val:
            data[name] = round(val, decimals)

    return data


# 🔹 US Yields
def get_us_yields():
    symbols = {
        "US_2Y":  ("^IRX", 3),
        "US_10Y": ("^TNX", 3),
        "US_30Y": ("^TYX", 3),
    }

    data = {}
    for name, (ticker, decimals) in symbols.items():
        val = _last(ticker)
        if val:
            data[name] = round(val, decimals)

    return data


# 🔹 Global yields (ETF proxies — direct yield tickers not on yfinance)
def get_global_yields():
    symbols = {
        "GER_BUND_ETF": ("IBGL.L", 3),   # iShares EUR Govt Bond 15-30yr
        "JPN_JGB_ETF":  ("2621.T", 3),   # iShares JP Govt Bond ETF (Tokyo)
    }

    data = {}
    for name, (ticker, decimals) in symbols.items():
        val = _last(ticker)
        if val:
            data[name] = round(val, decimals)

    return data


# 🔹 Oil — try multiple symbols
def get_oil():
    for symbol in ["CL=F", "BZ=F", "USO"]:
        val = _last(symbol, period="5d", interval="1h")
        if val:
            return round(val, 2)
    return None


# 🔹 Gold spot (cross-check)
def get_gold_spot():
    val = _last("GC=F", period="5d", interval="1h")
    return round(val, 2) if val else None


# 🔹 Combine all macro
def get_macro_data():
    return {
        "FX":            get_fx_data(),
        "US_YIELDS":     get_us_yields(),
        "GLOBAL_YIELDS": get_global_yields(),
        "OIL":           get_oil(),
        "GOLD_SPOT":     get_gold_spot(),
    }


# 🔹 Bloomberg-style macro format
def format_macro(data):
    text  = f"\n{'MACRO DASHBOARD':─<44}\n"

    fx = data.get("FX", {})
    if fx:
        text += f"\n{'FX / DOLLAR':─<44}\n"
        for k, v in fx.items():
            text += f"  {k:<12} {v:>10}\n"

    yields = data.get("US_YIELDS", {})
    if yields:
        text += f"\n{'US YIELDS':─<44}\n"
        for k, v in yields.items():
            text += f"  {k:<12} {v:>10}%\n"

    gyields = data.get("GLOBAL_YIELDS", {})
    if gyields:
        text += f"\n{'GLOBAL YIELDS (ETF)':─<44}\n"
        for k, v in gyields.items():
            text += f"  {k:<20} {v:>6}\n"

    oil = data.get("OIL")
    if oil:
        text += f"\n{'COMMODITIES':─<44}\n"
        text += f"  {'OIL (WTI)':<12} ${oil:>9}\n"

    gold = data.get("GOLD_SPOT")
    if gold:
        text += f"  {'GOLD SPOT':<12} ${gold:>9}\n"

    return text


if __name__ == "__main__":
    print("📡 Fetching macro data...\n")
    data = get_macro_data()
    print(format_macro(data))
