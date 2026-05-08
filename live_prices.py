"""
live_prices.py — Unified real-time price feed.
Primary source: Stooq.com (spot prices — matches TradingView exactly, free, no key).
Fallback: yfinance fast_info (15-min delayed futures/ETF prices).
Frankfurter (ECB) for FX cross-rates.
All asset classes: NSE, global indices, FX, bonds, commodities, crypto, VIX.
"""
import time, threading, gc, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ── Stooq symbol map (spot prices — match TradingView) ────────────────────────
# Stooq: https://stooq.com/q/l/?s=SYMBOL&f=sd2t2ohlcv&h&e=csv
STOOQ_SYMBOLS = {
    # Commodities — SPOT (not futures, so price = TradingView spot chart)
    "GOLD":    ("xauusd",   1.0,    "Spot USD/oz"),
    "SILVER":  ("xagusd",   1.0,    "Spot USD/oz"),
    "CRUDE":   ("cl.f",     1.0,    "WTI Futures"),
    "NATGAS":  ("ng.f",     1.0,    "Henry Hub Futures"),
    "COPPER":  ("hg.f",     0.01,   "COMEX cents→$/lb"),   # Stooq gives cents/lb

    # FX — spot mid-rates
    "DXY":     ("dx.f",     1.0,    "DXY Index"),
    "EURUSD":  ("eurusd",   1.0,    "EUR/USD spot"),
    "GBPUSD":  ("gbpusd",   1.0,    "GBP/USD spot"),
    "USDJPY":  ("usdjpy",   1.0,    "USD/JPY spot"),
    "USDINR":  ("usdinr",   1.0,    "USD/INR spot"),
    "AUDUSD":  ("audusd",   1.0,    "AUD/USD spot"),
    "USDCAD":  ("usdcad",   1.0,    "USD/CAD spot"),
    "USDCNY":  ("usdcny",   1.0,    "USD/CNY spot"),

    # Global indices
    "SPX":     ("^spx",     1.0,    "S&P 500"),
    "NASDAQ":  ("^ndx",     1.0,    "NASDAQ 100"),
    "DAX":     ("^dax",     1.0,    "DAX"),
    "HSI":     ("^hsi",     1.0,    "Hang Seng"),
}

# Instruments not on Stooq → yfinance fallback
YF_FALLBACK = {
    # Global indices
    "DOW":    "^DJI",
    "FTSE":   "^FTSE",
    "NIKKEI": "^N225",
    # Bonds
    "US_3M":  "^IRX",
    "US_5Y":  "^FVX",
    "US_10Y": "^TNX",
    "US_30Y": "^TYX",
    # Crypto
    "BTC":    "BTC-USD",
    "ETH":    "ETH-USD",
    # VIX
    "VIX":    "^VIX",
}

# NSE via yfinance (tvdatafeed fallback if available)
YF_NSE = {
    "NIFTY50":    "^NSEI",
    "BANKNIFTY":  "^NSEBANK",
    "SENSEX":     "^BSESN",
    "INDIA_VIX":  "^INDIAVIX",
}

# Valid range guards — reject garbage data
_VALID = {
    "GOLD":    (2000, 8000), "SILVER": (10, 200), "CRUDE": (20, 200),
    "NATGAS":  (0.5, 20),    "COPPER": (1, 15),
    "DXY":     (80, 120),    "EURUSD": (0.9, 1.5), "GBPUSD": (1.0, 1.7),
    "USDJPY":  (100, 175),   "USDINR": (70, 110),  "AUDUSD": (0.5, 0.9),
    "USDCAD":  (1.0, 1.6),   "USDCNY": (6.0, 8.0),
    "SPX":     (2000,12000), "NASDAQ": (5000,35000), "DOW": (20000,65000),
    "DAX":     (5000,25000), "FTSE": (5000,12000),   "NIKKEI": (10000,80000),
    "HSI":     (10000,40000),
    "NIFTY50": (10000,35000),"BANKNIFTY":(30000,80000),"SENSEX":(30000,90000),
    "INDIA_VIX":(5,90),      "VIX": (5,90),
    "US_3M":   (0,8), "US_5Y":(0,8), "US_10Y":(0,8), "US_30Y":(0,8),
    "BTC":     (5000,300000),"ETH":(50,20000),
}

STOOQ_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _chk(name, val):
    lo, hi = _VALID.get(name, (None, None))
    return lo is None or lo <= float(val) <= hi


def _stooq_one(key: str, sym: str, mult: float) -> tuple:
    """Fetch one Stooq symbol. Returns (key, dict|None)."""
    for attempt in range(2):
        try:
            if attempt > 0:
                time.sleep(0.4)
            r = requests.get(
                f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv",
                headers=STOOQ_HEADERS, timeout=8
            )
            lines = [l for l in r.text.strip().splitlines()
                     if l and not l.startswith("Symbol") and "," in l]
            if not lines:
                continue
            parts = lines[-1].split(",")
            if len(parts) < 7:
                continue
            close = float(parts[6]) * mult
            open_ = float(parts[3]) * mult
            if close <= 0 or not _chk(key, close):
                continue
            chg = round((close - open_) / open_ * 100, 3) if open_ > 0 else 0.0
            return key, {
                "price":  round(close, 4 if close < 10 else 2),
                "prev":   round(open_, 4 if open_ < 10 else 2),
                "change": chg,
                "arrow":  "▲" if chg > 0 else "▼" if chg < 0 else "─",
                "source": "stooq",
            }
        except Exception:
            pass
    return key, None


def _stooq_batch(symbols: dict) -> dict:
    """
    Fetch multiple Stooq symbols with limited concurrency (3 max) to avoid rate-limits.
    symbols = {"KEY": ("stooq_sym", multiplier, label)}
    """
    results = {}
    # Max 3 concurrent requests to Stooq to stay under rate limit
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {
            pool.submit(_stooq_one, k, sym, mult): k
            for k, (sym, mult, _) in symbols.items()
        }
        for fut in futs:
            k = futs[fut]
            try:
                _, v = fut.result(timeout=15)
                if v:
                    results[k] = v
            except Exception:
                pass
    return results


def _yf_quote(sym: str, name: str) -> dict | None:
    """yfinance fast_info quote with validation."""
    try:
        import yfinance as yf
        fi   = yf.Ticker(sym).fast_info
        last = float(fi.last_price)
        prev = float(fi.previous_close)
        if last <= 0 or not _chk(name, last):
            return None
        chg = round((last - prev) / prev * 100, 3) if prev > 0 else 0.0
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
    """FRED bond yields — authoritative, free."""
    try:
        r = requests.get(
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
            timeout=7, headers=STOOQ_HEADERS
        )
        lines = [l for l in r.text.strip().splitlines()
                 if not l.startswith("DATE") and "." in l]
        if len(lines) >= 2:
            prev = float(lines[-2].split(",")[1])
            val  = float(lines[-1].split(",")[1])
            if _chk(name, val):
                chg = round((val - prev) / prev * 100, 3) if prev > 0 else 0.0
                return {
                    "price":  round(val, 3),
                    "prev":   round(prev, 3),
                    "change": chg,
                    "arrow":  "▲" if chg > 0 else "▼" if chg < 0 else "─",
                    "source": "fred",
                }
    except Exception:
        pass
    return None


def _fetch_nse() -> dict:
    """NSE indices: tvdatafeed → yfinance fallback."""
    out = {}
    # Try tvdatafeed
    try:
        import sys
        sys.path.insert(0, "/Users/mahendiran/ai-system/core")
        from tvdata import get_nse_indices
        for k, v in (get_nse_indices() or {}).items():
            if v and _chk(k, v.get("price", 0)):
                out[k] = {**v, "source": "tvdatafeed"}
    except Exception:
        pass
    # yfinance fallback for any missing
    import yfinance as yf
    for label, sym in YF_NSE.items():
        if label in out:
            continue
        try:
            fi   = yf.Ticker(sym).fast_info
            last = float(fi.last_price)
            prev = float(fi.previous_close)
            name_key = label.replace("_", "")
            if last > 0 and _chk(name_key, last):
                chg = round((last - prev) / prev * 100, 3) if prev > 0 else 0.0
                out[label] = {
                    "price": round(last, 2), "prev": round(prev, 2),
                    "change": chg,
                    "arrow": "▲" if chg > 0 else "▼" if chg < 0 else "─",
                    "source": "yfinance",
                }
        except Exception:
            pass
    return out


def _fetch_all() -> dict:
    """Fetch all prices concurrently. Returns full price dict."""
    result = {
        "indices":     {},
        "global":      {},
        "fx":          {},
        "bonds":       {},
        "commodities": {},
        "crypto":      {},
        "vix":         {},
        "ts":          datetime.now(IST).strftime("%H:%M:%S IST"),
        "ts_epoch":    time.time(),
    }

    # Split Stooq symbols by category
    stooq_commodities = {k: v for k, v in STOOQ_SYMBOLS.items()
                         if k in ("GOLD","SILVER","CRUDE","NATGAS","COPPER")}
    stooq_fx          = {k: v for k, v in STOOQ_SYMBOLS.items()
                         if k in ("DXY","EURUSD","GBPUSD","USDJPY","USDINR","AUDUSD","USDCAD","USDCNY")}
    stooq_global      = {k: v for k, v in STOOQ_SYMBOLS.items()
                         if k in ("SPX","NASDAQ","DAX","HSI")}

    with ThreadPoolExecutor(max_workers=8) as pool:
        fut_nse   = pool.submit(_fetch_nse)
        fut_cmd   = pool.submit(_stooq_batch, stooq_commodities)
        fut_fx    = pool.submit(_stooq_batch, stooq_fx)
        fut_glo_s = pool.submit(_stooq_batch, stooq_global)

        # yfinance for DOW, FTSE, NIKKEI, crypto, VIX, bonds
        yf_tasks = {}
        for k, sym in YF_FALLBACK.items():
            yf_tasks[k] = pool.submit(_yf_quote, sym, k)

        # FRED for US_2Y (most authoritative)
        fut_2y = pool.submit(_fred_yield, "DGS2", "US_2Y")

        # Collect Stooq results
        try: result["indices"].update(fut_nse.result(timeout=25))
        except: pass

        try: result["commodities"].update(fut_cmd.result(timeout=12))
        except: pass

        try: result["fx"].update(fut_fx.result(timeout=12))
        except: pass

        try: result["global"].update(fut_glo_s.result(timeout=12))
        except: pass

        # yfinance results
        for k, fut in yf_tasks.items():
            try:
                v = fut.result(timeout=15)
                if not v:
                    continue
                if k in ("DOW","FTSE","NIKKEI"):
                    result["global"][k] = v
                elif k in ("US_3M","US_5Y","US_10Y","US_30Y"):
                    result["bonds"][k] = v
                elif k in ("BTC","ETH"):
                    result["crypto"][k] = v
                elif k == "VIX":
                    result["vix"][k] = v
            except:
                pass

        # FRED US_2Y
        try:
            v2y = fut_2y.result(timeout=10)
            if v2y:
                result["bonds"]["US_2Y"] = v2y
        except:
            pass

    # Move INDIA_VIX from indices → vix panel
    if "INDIA_VIX" in result["indices"]:
        result["vix"]["INDIA_VIX"] = result["indices"].pop("INDIA_VIX")

    gc.collect()
    return result


def get_live_prices(force: bool = False) -> dict:
    """Return all live prices. No internal caching — caller (_bg_refresh) handles TTL."""
    return _fetch_all()


def get_ticker_items() -> list:
    """Flat list for scrolling ticker. Ordered: NSE→Global→FX→Cmdty→Bonds→Crypto→VIX."""
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
    for _, grp, label in ORDER:
        for sym, v in grp.items():
            if not v:
                continue
            items.append({
                "symbol":   sym,
                "price":    v.get("price", 0),
                "change":   v.get("change", 0),
                "arrow":    v.get("arrow", "─"),
                "category": label,
                "source":   v.get("source", ""),
            })
    return items
