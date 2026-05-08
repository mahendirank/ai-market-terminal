"""
live_prices.py — Unified real-time price feed.
All asset classes: NSE indices, global indices, FX, bonds, commodities, crypto.
15-second cache. Single source of truth for the dashboard ticker.
"""
import time, threading, gc
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
_lock  = threading.Lock()
_cache: dict = {}
CACHE_TTL = 15  # seconds — fast refresh for live feel

# ── Symbol maps ───────────────────────────────────────────────────────────────

_YF_GLOBAL_INDICES = {
    "SPX":     "^GSPC",
    "NASDAQ":  "^IXIC",
    "DOW":     "^DJI",
    "DAX":     "^GDAXI",
    "FTSE":    "^FTSE",
    "NIKKEI":  "^N225",
    "HSI":     "^HSI",
}

_YF_FX = {
    "DXY":     "DX-Y.NYB",
    "USDINR":  "USDINR=X",
    "EURUSD":  "EURUSD=X",
    "GBPUSD":  "GBPUSD=X",
    "USDJPY":  "USDJPY=X",
    "AUDUSD":  "AUDUSD=X",
    "USDCAD":  "USDCAD=X",
    "USDCNY":  "USDCNY=X",
}

_YF_COMMODITIES = {
    "GOLD":    "GC=F",
    "CRUDE":   "CL=F",
    "SILVER":  "SI=F",
    "COPPER":  "HG=F",
    "NATGAS":  "NG=F",
}

_YF_BONDS = {
    "US_3M":  "^IRX",
    "US_2Y":  None,         # FRED only
    "US_5Y":  "^FVX",
    "US_10Y": "^TNX",
    "US_30Y": "^TYX",
}

_YF_CRYPTO = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
}

_YF_VIX = {
    "VIX":       "^VIX",
    "INDIA_VIX": "^INDIAVIX",
}

# FRED series for yields
_FRED_YIELDS = {
    "US_2Y":  "DGS2",
    "US_10Y": "DGS10",
}

# Frankfurter API (ECB rates — authoritative, free, no key)
_FRANK_PAIRS = ["INR", "JPY", "CNY", "CAD"]  # EUR/GBP inverted separately; AUD via yfinance

# Valid range guards
_VALID = {
    "SPX": (2000, 12000), "NASDAQ": (5000, 30000), "DOW": (20000, 60000),
    "DAX": (5000, 25000), "FTSE": (5000, 12000), "NIKKEI": (10000, 80000),
    "HSI": (10000, 40000),
    "NIFTY50": (10000, 35000), "BANKNIFTY": (30000, 80000), "SENSEX": (30000, 90000),
    "FINNIFTY": (15000, 50000), "MIDCPNIFTY": (5000, 20000),
    "DXY": (80, 120), "USDINR": (70, 110), "EURUSD": (0.9, 1.5),
    "GBPUSD": (1.0, 1.7), "USDJPY": (100, 175), "AUDUSD": (0.5, 0.9),
    "USDCAD": (1.0, 1.5), "USDCNY": (6.0, 8.0),
    "GOLD": (1500, 6000), "CRUDE": (30, 200), "SILVER": (10, 100),
    "COPPER": (2, 10), "NATGAS": (1, 20),
    "US_3M": (0, 8), "US_5Y": (0, 8), "US_10Y": (0, 8), "US_30Y": (0, 8), "US_2Y": (0, 8),
    "VIX": (5, 90), "INDIA_VIX": (5, 90),
    "BTC": (1000, 200000), "ETH": (100, 20000),
}


def _chk(name: str, val: float) -> bool:
    lo, hi = _VALID.get(name, (None, None))
    return lo is None or lo <= val <= hi


def _yf_quote(sym: str, name: str) -> dict | None:
    try:
        import yfinance as yf
        fi   = yf.Ticker(sym).fast_info
        last = float(fi.last_price)
        prev = float(fi.previous_close)
        if last <= 0 or not _chk(name, last):
            return None
        chg = round((last - prev) / prev * 100, 2) if prev > 0 else 0.0
        return {
            "price":  round(last, 4 if last < 10 else 2),
            "prev":   round(prev, 4 if prev < 10 else 2),
            "change": chg,
            "arrow":  "▲" if chg > 0 else "▼" if chg < 0 else "─",
            "source": "yfinance",
        }
    except Exception:
        return None


def _fred_yield(series_id: str, name: str) -> dict | None:
    try:
        import requests
        r = requests.get(
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
            timeout=6, headers={"User-Agent": "Mozilla/5.0"}
        )
        lines = [l for l in r.text.strip().splitlines()
                 if not l.startswith("DATE") and "." in l]
        if len(lines) >= 2:
            prev  = float(lines[-2].split(",")[1])
            val   = float(lines[-1].split(",")[1])
            if _chk(name, val):
                chg = round(val - prev, 3)
                return {
                    "price":  round(val, 3),
                    "prev":   round(prev, 3),
                    "change": round((val - prev) / prev * 100, 2) if prev > 0 else 0.0,
                    "arrow":  "▲" if chg > 0 else "▼" if chg < 0 else "─",
                    "source": "fred",
                }
    except Exception:
        pass
    return None


def _frankfurter_fx() -> dict:
    """ECB-sourced FX rates — more accurate for INR/JPY/CAD/CNY."""
    try:
        import requests
        r = requests.get("https://api.frankfurter.app/latest?base=USD",
                         timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            rates = r.json().get("rates", {})
            out   = {}
            # Direct USD pairs (USD is base)
            for pair in _FRANK_PAIRS:
                rate = rates.get(pair)
                if rate:
                    name = f"USD{pair}"
                    val  = round(float(rate), 4 if float(rate) < 10 else 2)
                    out[name] = {
                        "price": val, "prev": val, "change": 0.0,
                        "arrow": "─", "source": "frankfurter",
                    }
            # EUR/GBP — inverted from USD base
            for pair in ("EUR", "GBP"):
                rate = rates.get(pair)
                if rate:
                    val = round(1 / float(rate), 4)
                    out[f"{pair}USD"] = {
                        "price": val, "prev": val, "change": 0.0,
                        "arrow": "─", "source": "frankfurter",
                    }
            return out
    except Exception:
        pass
    return {}


_YF_NSE_FALLBACK = {
    "NIFTY50":    "^NSEI",
    "BANKNIFTY":  "^NSEBANK",
    "SENSEX":     "^BSESN",
    "FINNIFTY":   "^NSEMDCP50",   # approximate; actual FINNIFTY not on yfinance
    "INDIA_VIX":  "^INDIAVIX",
}

def _fetch_nse_indices() -> dict:
    out = {}
    # Try tvdatafeed first
    try:
        import sys
        sys.path.insert(0, "/Users/mahendiran/ai-system/core")
        from tvdata import get_nse_indices
        data = get_nse_indices()
        for k, v in data.items():
            if v and _chk(k, v.get("price", 0)):
                out[k] = {**v, "source": "tvdatafeed"}
    except Exception:
        pass

    # yfinance fallback for any missing NSE index
    import yfinance as yf
    for label, sym in _YF_NSE_FALLBACK.items():
        if label in out:
            continue
        try:
            fi   = yf.Ticker(sym).fast_info
            last = float(fi.last_price)
            prev = float(fi.previous_close)
            if last > 0 and _chk(label.replace("_", ""), last):
                chg = round((last - prev) / prev * 100, 2) if prev > 0 else 0.0
                out[label] = {
                    "price": round(last, 2), "prev": round(prev, 2),
                    "change": chg, "arrow": "▲" if chg > 0 else "▼" if chg < 0 else "─",
                    "source": "yfinance",
                }
        except Exception:
            pass
    return out


def _fetch_all() -> dict:
    """Fetch all price categories concurrently."""
    result = {
        "indices":    {},
        "global":     {},
        "fx":         {},
        "bonds":      {},
        "commodities":{},
        "crypto":     {},
        "vix":        {},
        "ts":         datetime.now(IST).strftime("%H:%M:%S IST"),
        "ts_epoch":   time.time(),
    }

    tasks = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        # NSE indices via tvdatafeed
        tasks["_nse"] = pool.submit(_fetch_nse_indices)

        # Global indices
        for name, sym in _YF_GLOBAL_INDICES.items():
            tasks[f"g_{name}"] = pool.submit(_yf_quote, sym, name)

        # FX (yfinance — will merge with Frankfurter)
        for name, sym in _YF_FX.items():
            tasks[f"fx_{name}"] = pool.submit(_yf_quote, sym, name)

        # Frankfurter FX override
        tasks["_frank"] = pool.submit(_frankfurter_fx)

        # Commodities
        for name, sym in _YF_COMMODITIES.items():
            tasks[f"c_{name}"] = pool.submit(_yf_quote, sym, name)

        # Bonds via yfinance
        for name, sym in _YF_BONDS.items():
            if sym:
                tasks[f"b_{name}"] = pool.submit(_yf_quote, sym, name)

        # US_2Y via FRED (most accurate)
        tasks["b_US_2Y"] = pool.submit(_fred_yield, "DGS2", "US_2Y")

        # Crypto
        for name, sym in _YF_CRYPTO.items():
            tasks[f"k_{name}"] = pool.submit(_yf_quote, sym, name)

        # VIX
        for name, sym in _YF_VIX.items():
            tasks[f"v_{name}"] = pool.submit(_yf_quote, sym, name)

        done = {}
        for key, fut in tasks.items():
            try:
                done[key] = fut.result(timeout=20)
            except Exception:
                done[key] = None

    # NSE indices
    nse = done.get("_nse") or {}
    for k, v in nse.items():
        result["indices"][k] = v

    # Global indices
    for name in _YF_GLOBAL_INDICES:
        v = done.get(f"g_{name}")
        if v:
            result["global"][name] = v

    # FX — start with yfinance, override with Frankfurter if available
    frank = done.get("_frank") or {}
    for name in _YF_FX:
        v = done.get(f"fx_{name}")
        if v:
            result["fx"][name] = v
    # Frankfurter override for INR/EUR/GBP (more accurate)
    for k, v in frank.items():
        mapped = k  # e.g. USDINR, EURUSD, GBPUSD
        if mapped in result["fx"]:
            # Use frankfurter price but keep yfinance change%
            yf_entry = result["fx"][mapped]
            result["fx"][mapped] = {
                **yf_entry,
                "price":  v["price"],
                "source": "frankfurter+yf",
            }
        else:
            result["fx"][mapped] = v

    # Bonds
    for name in _YF_BONDS:
        v = done.get(f"b_{name}")
        if v and _chk(name, v["price"]):
            result["bonds"][name] = v

    # Commodities
    for name in _YF_COMMODITIES:
        v = done.get(f"c_{name}")
        if v:
            result["commodities"][name] = v

    # Crypto
    for name in _YF_CRYPTO:
        v = done.get(f"k_{name}")
        if v:
            result["crypto"][name] = v

    # VIX
    for name in _YF_VIX:
        v = done.get(f"v_{name}")
        if v:
            result["vix"][name] = v

    gc.collect()
    return result


def get_live_prices(force: bool = False) -> dict:
    """Return all live prices. No internal caching — caller handles TTL."""
    return _fetch_all()


def get_ticker_items() -> list:
    """
    Flat list of {symbol, price, change, arrow, category} for the scrolling ticker.
    Ordered: NSE → Global → FX → Commodities → Bonds → Crypto → VIX
    """
    d = get_live_prices()
    items = []

    ORDER = [
        ("indices",     d.get("indices", {}),     "NSE"),
        ("global",      d.get("global", {}),      "GLOBAL"),
        ("fx",          d.get("fx", {}),          "FX"),
        ("commodities", d.get("commodities", {}), "CMDTY"),
        ("bonds",       d.get("bonds", {}),       "BONDS"),
        ("crypto",      d.get("crypto", {}),      "CRYPTO"),
        ("vix",         d.get("vix", {}),         "VIX"),
    ]

    for cat, grp, label in ORDER:
        for sym, v in grp.items():
            if not v:
                continue
            items.append({
                "symbol":   sym,
                "price":    v.get("price", 0),
                "change":   v.get("change", 0),
                "arrow":    v.get("arrow", "─"),
                "category": label,
            })

    return items
