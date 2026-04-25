import requests
import yfinance as yf

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MacroBot/1.0)"}

# ── Valid ranges — reject yfinance garbage outside these bands ────────────────
VALID_RANGES = {
    "DXY":    (80,  115),
    "EURUSD": (0.9,  1.5),
    "GBPUSD": (1.0,  1.6),
    "USDJPY": (100,  175),
    "USDCNY": (6.5,  7.5),
    "USDINR": (75,  110),
    "US_2Y":  (0.1,  8.0),
    "US_10Y": (0.5,  8.0),
    "US_30Y": (1.0,  8.0),
    "OIL":    (40,   200),
    "GOLD":   (1500, 6000),
}

def _in_range(name, val):
    lo, hi = VALID_RANGES.get(name, (None, None))
    if lo is None:
        return True
    return lo <= val <= hi

def _yf_last(symbol, period="5d", interval="1h"):
    """Use fast_info for live price — yf.download returns stale/incorrect data for many symbols."""
    try:
        fi = yf.Ticker(symbol).fast_info
        v = fi.last_price
        if v and float(v) > 0:
            return float(v)
    except Exception:
        pass
    # fallback to download
    try:
        import pandas as pd
        df = yf.download(symbol, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if not df.empty:
            v = df["Close"].dropna().iloc[-1]
            return float(v.item() if hasattr(v, "item") else v)
    except Exception:
        pass
    return None

# ── FRED fallback (St. Louis Fed — authoritative, free, no key) ───────────────
_FRED_SERIES = {
    "DXY":    "DTWEXBGS",   # Broad Dollar Index (close to DXY)
    "US_2Y":  "DGS2",
    "US_10Y": "DGS10",
    "US_30Y": "DGS30",
}

def _fred(series_id):
    try:
        url  = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        resp = requests.get(url, timeout=8, headers=HEADERS)
        lines = [l for l in resp.text.strip().splitlines() if not l.startswith("DATE") and "." in l]
        if lines:
            val = float(lines[-1].split(",")[1])
            return val
    except:
        pass
    return None

def _get(name, yf_symbol, fred_series=None, period="5d", interval="1h", decimals=2):
    val = _yf_last(yf_symbol, period, interval)
    if val and _in_range(name, val):
        return round(val, decimals)
    # yfinance value bad — try FRED
    if fred_series:
        val = _fred(fred_series)
        if val and _in_range(name, val):
            return round(val, decimals)
    return None


# ── Currencies ─────────────────────────────────────────────────────────────────
def get_fx_data():
    return {k: v for k, v in {
        "DXY":    _get("DXY",    "DX-Y.NYB", "DTWEXBGS", decimals=2),
        "EURUSD": _get("EURUSD", "EURUSD=X",  decimals=4),
        "GBPUSD": _get("GBPUSD", "GBPUSD=X",  decimals=4),
        "USDJPY": _get("USDJPY", "USDJPY=X",  decimals=2),
        "USDCNY": _get("USDCNY", "USDCNY=X",  decimals=4),
        "USDINR": _get("USDINR", "USDINR=X",  decimals=2),
    }.items() if v is not None}


# ── US Yields — yfinance ^TNX/^IRX return value * 10 sometimes, validate hard ─
def get_us_yields():
    """FRED is authoritative for yields — use it first, yfinance as fallback."""
    results = {}
    for name, symbol, fred_id in [
        ("US_2Y",  "^IRX", "DGS2"),
        ("US_10Y", "^TNX", "DGS10"),
        ("US_30Y", "^TYX", "DGS30"),
    ]:
        # Try FRED first (daily, authoritative, no auth needed)
        val = _fred(fred_id)
        if val and _in_range(name, val):
            results[name] = round(val, 3)
            continue
        # FRED fallback — try yfinance
        val = _yf_last(symbol)
        if val and val > 15:
            val = round(val / 10, 3)   # ^TNX/^TYX sometimes * 10
        if val and _in_range(name, val):
            results[name] = round(val, 3)
    return results


# ── Global yields — FRED CSV (free, no key, monthly) ─────────────────────────
_GLOBAL_YIELD_FRED = {
    "GER_BUND_10Y": "IRLTLT01DEM156N",
    "UK_GILT_10Y":  "IRLTLT01GBM156N",
    "JPN_JGB_10Y":  "IRLTLT01JPM156N",
}

def get_global_yields():
    results = {}
    for name, series in _GLOBAL_YIELD_FRED.items():
        v = _fred(series)
        if v and 0 < v < 20:
            results[name] = round(v, 3)
    return results


# ── Oil ────────────────────────────────────────────────────────────────────────
def get_oil():
    for symbol in ["CL=F", "BZ=F"]:
        val = _yf_last(symbol)
        if val and _in_range("OIL", val):
            return round(val, 2)
    return None


# ── Gold spot ──────────────────────────────────────────────────────────────────
def get_gold_spot():
    val = _yf_last("GC=F")
    if val and _in_range("GOLD", val):
        return round(val, 2)
    return None


# ── Combine ────────────────────────────────────────────────────────────────────
def get_macro_data():
    import gc
    data = {
        "FX":            get_fx_data(),
        "US_YIELDS":     get_us_yields(),
        "GLOBAL_YIELDS": get_global_yields(),
        "OIL":           get_oil(),
        "GOLD_SPOT":     get_gold_spot(),
    }
    gc.collect()
    return data


def format_macro(data):
    text = f"\n{'MACRO DASHBOARD':─<44}\n"
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
        text += f"\n{'GLOBAL YIELDS':─<44}\n"
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
    print("Fetching macro data...\n")
    data = get_macro_data()
    print(format_macro(data))
