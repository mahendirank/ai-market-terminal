"""
tvdata.py — TradingView live price feed via tvdatafeed.
Single price source for all NSE indices, sector indices, and top stocks.
Falls back to yfinance for global (US/EU) indices.
Set TV_USERNAME + TV_PASSWORD env vars for full TradingView access.
"""

import os, time, threading, gc
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from tvDatafeed import TvDatafeed, Interval
    _TV_AVAILABLE = True
except ImportError:
    _TV_AVAILABLE = False

_tv_instance   = None
_tv_lock       = threading.Lock()
_price_cache   : dict = {}
_CACHE_TTL     = 30   # seconds — refresh every 30s during market hours

# ── NSE Indices ───────────────────────────────────────────────────────────────
NSE_INDICES = {
    "NIFTY50":    ("NIFTY50",    "NSE"),
    "BANKNIFTY":  ("BANKNIFTY",  "NSE"),
    "FINNIFTY":   ("FINNIFTY",   "NSE"),
    "MIDCPNIFTY": ("MIDCPNIFTY", "NSE"),
    "SENSEX":     ("SENSEX",     "BSE"),
}

# ── NSE Sector Indices ────────────────────────────────────────────────────────
NSE_SECTORS = {
    "IT":      ("CNXIT",     "NSE"),
    "BANKING": ("BANKNIFTY", "NSE"),
    "FMCG":    ("CNXFMCG",   "NSE"),
    "AUTO":    ("CNXAUTO",   "NSE"),
    "PHARMA":  ("CNXPHARMA", "NSE"),
    "METAL":   ("CNXMETAL",  "NSE"),
    "REALTY":  ("CNXREALTY", "NSE"),
    "ENERGY":  ("CNXENERGY", "NSE"),
}

# ── NSE Top Stocks per Sector ─────────────────────────────────────────────────
NSE_STOCKS = {
    "IT":      ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
    "BANKING": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK"],
    "FMCG":    ["HINDUNILVR", "ITC", "NESTLEIND", "DABUR", "MARICO"],
    "AUTO":    ["MARUTI", "TATAMOTORS", "BAJAJ-AUTO", "M&M", "EICHERMOT"],
    "PHARMA":  ["SUNPHARMA", "CIPLA", "DRREDDY", "DIVISLAB", "APOLLOHOSP"],
    "METAL":   ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "COALINDIA"],
    "REALTY":  ["DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "PHOENIXLTD"],
    "ENERGY":  ["RELIANCE", "ONGC", "NTPC", "POWERGRID", "BPCL"],
}

# ── Global Indices (yfinance fallback) ────────────────────────────────────────
GLOBAL_YF = {
    "SPX":    "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW":    "^DJI",
    "DAX":    "^GDAXI",
    "FTSE":   "^FTSE",
    "NIKKEI": "^N225",
    "HSI":    "^HSI",
    "VIX":    "^VIX",
    "GOLD":   "GC=F",
    "CRUDE":  "CL=F",
    "DXY":    "DX-Y.NYB",
}


# ── TvDatafeed singleton ──────────────────────────────────────────────────────

def _get_tv():
    global _tv_instance
    if not _TV_AVAILABLE:
        return None
    if _tv_instance is None:
        with _tv_lock:
            if _tv_instance is None:
                username = os.environ.get("TV_USERNAME", "").strip()
                password = os.environ.get("TV_PASSWORD", "").strip()
                try:
                    _tv_instance = TvDatafeed(
                        username=username or None,
                        password=password or None,
                    )
                except Exception as e:
                    print(f"[tvdata] TvDatafeed init failed: {e}", flush=True)
                    _tv_instance = None
    return _tv_instance


# ── Core fetch ────────────────────────────────────────────────────────────────

def _fetch_one(symbol: str, exchange: str, n_bars: int = 5) -> dict | None:
    """Fetch latest price for one TradingView symbol. Returns price dict or None."""
    cache_key = f"{exchange}:{symbol}"
    cached = _price_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]

    tv = _get_tv()
    if tv is None:
        return None

    try:
        df = tv.get_hist(
            symbol=symbol,
            exchange=exchange,
            interval=Interval.in_1_minute,
            n_bars=n_bars,
        )
        if df is None or df.empty:
            return None

        row      = df.iloc[-1]
        prev_row = df.iloc[-2] if len(df) >= 2 else None
        close    = float(row["close"])
        prev_cls = float(prev_row["close"]) if prev_row is not None else close
        chg      = round((close - prev_cls) / prev_cls * 100, 2) if prev_cls > 0 else 0.0

        data = {
            "price":  round(close, 2),
            "open":   round(float(row["open"]), 2),
            "high":   round(float(row["high"]), 2),
            "low":    round(float(row["low"]),  2),
            "volume": int(row.get("volume", 0) or 0),
            "change": chg,
            "arrow":  "▲" if chg > 0 else "▼" if chg < 0 else "–",
        }
        _price_cache[cache_key] = {"data": data, "ts": time.time()}
        return data

    except Exception as e:
        print(f"[tvdata] fetch failed {exchange}:{symbol} — {e}", flush=True)
        return None


def _fetch_many(symbols_map: dict, max_workers: int = 12) -> dict:
    """
    Fetch multiple symbols concurrently.
    symbols_map = {"LABEL": ("SYMBOL", "EXCHANGE"), ...}
    Returns {"LABEL": price_dict, ...}
    """
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_fetch_one, sym, exch): label
            for label, (sym, exch) in symbols_map.items()
        }
        for fut in as_completed(futures, timeout=20):
            label = futures[fut]
            try:
                data = fut.result()
                if data:
                    results[label] = data
            except Exception:
                pass
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def get_nse_indices() -> dict:
    """
    Returns NSE indices with live prices.
    Format: {"NIFTY50": {price, change, arrow, open, high, low}, ...}
    Falls back to yfinance if tvdatafeed unavailable.
    """
    if _TV_AVAILABLE:
        data = _fetch_many(NSE_INDICES)
        if data:
            gc.collect()
            return data
    # yfinance fallback
    return _yf_fallback_indices()


def get_nse_sectors() -> dict:
    """
    Returns NSE sector index prices.
    Format: {"IT": {price, change, arrow, ...}, "BANKING": {...}, ...}
    """
    if _TV_AVAILABLE:
        data = _fetch_many(NSE_SECTORS)
        if data:
            gc.collect()
            return data
    return {}


def get_nse_stocks(sectors: list = None) -> dict:
    """
    Returns top NSE stocks for given sectors (or all sectors if None).
    Format: {"IT": [{"symbol": "TCS", "price": 3800, ...}, ...], ...}
    """
    sectors = sectors or list(NSE_STOCKS.keys())
    if not _TV_AVAILABLE:
        return {}

    all_symbols = {}
    for sec in sectors:
        for sym in NSE_STOCKS.get(sec, []):
            all_symbols[f"{sec}:{sym}"] = (sym, "NSE")

    raw = _fetch_many(all_symbols)

    result = {sec: [] for sec in sectors}
    for key, data in raw.items():
        sec, sym = key.split(":", 1)
        result[sec].append({"symbol": sym, **data})

    # Sort each sector by absolute change descending
    for sec in result:
        result[sec].sort(key=lambda x: abs(x.get("change", 0)), reverse=True)

    gc.collect()
    return result


def get_all_indices() -> dict:
    """
    Returns NSE + global indices merged.
    NSE via tvdatafeed, global via yfinance.
    """
    nse    = get_nse_indices()
    global_ = _yf_global()
    return {**nse, **global_}


def get_price(symbol: str, exchange: str = "NSE") -> dict | None:
    """Single symbol lookup. e.g. get_price('TCS', 'NSE')"""
    return _fetch_one(symbol, exchange)


# ── yfinance fallbacks ────────────────────────────────────────────────────────

def _yf_fallback_indices() -> dict:
    """Fallback: NSE indices via yfinance fast_info."""
    try:
        import yfinance as yf
        YF_NSE = {
            "NIFTY50":   "^NSEI",
            "BANKNIFTY": "^NSEBANK",
            "SENSEX":    "^BSESN",
        }
        data = {}
        for label, ticker in YF_NSE.items():
            try:
                fi   = yf.Ticker(ticker).fast_info
                last = float(fi.last_price)
                prev = float(fi.previous_close)
                if last > 0 and prev > 0:
                    chg = round((last - prev) / prev * 100, 2)
                    data[label] = {
                        "price": round(last, 2), "change": chg,
                        "arrow": "▲" if chg > 0 else "▼",
                    }
            except Exception:
                pass
        return data
    except Exception:
        return {}


def _yf_global() -> dict:
    """Global indices via yfinance fast_info."""
    try:
        import yfinance as yf
        data = {}
        for label, ticker in GLOBAL_YF.items():
            try:
                fi   = yf.Ticker(ticker).fast_info
                last = float(fi.last_price)
                prev = float(fi.previous_close)
                if last > 0 and prev > 0:
                    chg = round((last - prev) / prev * 100, 2)
                    data[label] = {
                        "price": round(last, 2), "change": chg,
                        "arrow": "▲" if chg > 0 else "▼",
                    }
            except Exception:
                pass
        return data
    except Exception:
        return {}
