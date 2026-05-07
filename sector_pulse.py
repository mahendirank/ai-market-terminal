"""
sector_pulse.py — NSE sector index live prices + US broad market.
NSE sectors: real-time via tvdatafeed (CNXIT, BANKNIFTY, CNXFMCG, etc.)
US broad: yfinance ETFs (SPY, QQQ, etc.) for global context.
"""

import os, json, time, threading
from datetime import datetime, timezone, timedelta

IST       = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 60    # 1 minute for sector data

_cache_lock = threading.Lock()
_mem_cache  : dict = {}   # {"sectors": {data, ts}, "broad": {data, ts}}

# US broad market ETFs (yfinance) for global context
_US_BROAD = {
    "SPY":  "S&P 500 ETF",
    "QQQ":  "NASDAQ 100 ETF",
    "IWM":  "Russell 2000",
    "GLD":  "Gold ETF",
    "TLT":  "20Y Bond ETF",
    "UUP":  "Dollar ETF",
    "EEM":  "Emerging Markets",
}


def _mem_get(key: str):
    with _cache_lock:
        entry = _mem_cache.get(key)
        if entry and (time.time() - entry["ts"]) < CACHE_TTL:
            return entry["data"]
    return None


def _mem_set(key: str, data):
    with _cache_lock:
        _mem_cache[key] = {"data": data, "ts": time.time()}


def _yf_etf(sym: str, label: str) -> dict | None:
    try:
        import yfinance as yf
        fi   = yf.Ticker(sym).fast_info
        last = float(fi.last_price)
        prev = float(fi.previous_close)
        if last > 0 and prev > 0:
            chg = round((last - prev) / prev * 100, 2)
            return {
                "symbol": sym, "label": label,
                "price":  round(last, 2), "chg": chg,
                "arrow":  "▲" if chg > 0 else "▼",
                "color":  "bull" if chg > 0 else "bear",
            }
    except Exception:
        pass
    return None


def get_nse_sectors_live() -> list:
    """
    NSE sector indices via tvdatafeed.
    Returns list of sector dicts sorted by change desc.
    """
    cached = _mem_get("nse_sectors")
    if cached:
        return cached

    try:
        from tvdata import get_nse_sectors, NSE_SECTORS, NSE_STOCKS
        raw = get_nse_sectors()

        sectors_out = []
        for label, data in raw.items():
            chg = data.get("change", 0.0)
            # pick top mover from sector stocks (optional enrichment)
            sectors_out.append({
                "label":      label,
                "symbol":     NSE_SECTORS.get(label, ("", ""))[0],
                "exchange":   NSE_SECTORS.get(label, ("", ""))[1],
                "price":      data["price"],
                "chg":        chg,
                "arrow":      data["arrow"],
                "open":       data.get("open", 0),
                "high":       data.get("high", 0),
                "low":        data.get("low",  0),
                "color":      "bull" if chg > 0 else "bear" if chg < 0 else "neu",
                "change_pct": chg,   # alias for dashboard_api sector rotation
            })

        sectors_out.sort(key=lambda x: -x["chg"])
        _mem_set("nse_sectors", sectors_out)
        return sectors_out

    except Exception as e:
        print(f"[sector_pulse] NSE sectors failed: {e}", flush=True)
        return []


def get_us_broad() -> list:
    """US broad market ETFs via yfinance for global context."""
    cached = _mem_get("us_broad")
    if cached:
        return cached

    from concurrent.futures import ThreadPoolExecutor, as_completed
    broad = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_yf_etf, sym, lbl): sym for sym, lbl in _US_BROAD.items()}
        for fut in as_completed(futs, timeout=15):
            try:
                r = fut.result()
                if r:
                    broad.append(r)
            except Exception:
                pass

    broad.sort(key=lambda x: -x["chg"])
    if broad:
        _mem_set("us_broad", broad)
    return broad


def get_sector_pulse() -> dict:
    """
    Main entry point — used by dashboard_api sector rotation.
    Returns NSE sectors + US broad + breadth signal.
    """
    nse_sectors = get_nse_sectors_live()
    us_broad    = get_us_broad()

    advancing = sum(1 for s in nse_sectors if s.get("chg", 0) > 0)
    total     = len(nse_sectors) or 1

    if   advancing >= 7: breadth = "BROAD RALLY"
    elif advancing >= 5: breadth = "BULLISH"
    elif advancing >= 3: breadth = "MIXED"
    elif advancing >= 1: breadth = "BEARISH"
    else:                breadth = "BROAD SELLOFF"
    breadth_cls = "bull" if advancing > total // 2 else "bear" if advancing < total // 2 else "neu"

    # Build dict keyed by sector label (for dashboard_api sector rotation lookup)
    sectors_by_label = {
        s["label"]: {"change_pct": s["chg"], "price": s["price"]}
        for s in nse_sectors
    }

    import gc; gc.collect()
    return {
        "sectors":      nse_sectors,
        "sectors_dict": sectors_by_label,   # quick lookup by label
        "broad":        us_broad,
        "breadth":      breadth,
        "breadth_cls":  breadth_cls,
        "advancing":    advancing,
        "declining":    total - advancing,
        "total":        total,
        "timestamp":    datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
    }
