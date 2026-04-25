"""
Sector Pulse — US sector ETF performance + market breadth.
Uses SPDR sector ETFs (free via yfinance fast_info).
Replaces Finviz (JS-rendered, hard to scrape) with direct ETF data.
Sectors: Tech, Finance, Energy, Health, Industrials, Consumer, Utilities, Materials, RE
"""
import os, json, time, sqlite3, threading
from datetime import datetime, timezone, timedelta

IST       = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 300   # 5 minutes
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "sector_cache.db")
_db_lock  = threading.Lock()

SECTORS = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Healthcare",
    "XLI":  "Industrials",
    "XLY":  "Consumer Disc.",
    "XLP":  "Consumer Staples",
    "XLU":  "Utilities",
    "XLB":  "Materials",
    "XLRE": "Real Estate",
    "XLC":  "Comm. Services",
}

BROAD = {
    "SPY":  "S&P 500",
    "QQQ":  "NASDAQ 100",
    "IWM":  "Russell 2000",
    "EEM":  "Emerging Markets",
    "GLD":  "Gold ETF",
    "TLT":  "20Y Bond ETF",
    "UUP":  "Dollar ETF",
}


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS sp (key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL)")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row  = conn.execute("SELECT data,ts FROM sp WHERE key=?", (key,)).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO sp(key,data,ts) VALUES(?,?,?)",
                         (key, json.dumps(data), time.time()))
            conn.commit(); conn.close()
    except: pass


def _get_etf(sym, label):
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


def get_sector_pulse():
    cached = _cache_get("sectors")
    if cached: return cached
    import gc
    from concurrent.futures import ThreadPoolExecutor

    sectors = {}
    broad   = {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        sec_futs  = {sym: pool.submit(_get_etf, sym, lbl) for sym, lbl in SECTORS.items()}
        broad_futs = {sym: pool.submit(_get_etf, sym, lbl) for sym, lbl in BROAD.items()}

    for sym, fut in sec_futs.items():
        try:
            r = fut.result(timeout=8)
            if r: sectors[sym] = r
        except: pass

    for sym, fut in broad_futs.items():
        try:
            r = fut.result(timeout=8)
            if r: broad[sym] = r
        except: pass

    # Sort sectors by performance
    sorted_sectors = sorted(sectors.values(), key=lambda x: -x["chg"])

    # Market breadth signal
    advancing = sum(1 for s in sectors.values() if s["chg"] > 0)
    total     = len(sectors)
    if advancing >= 8:        breadth = "BROAD RALLY"
    elif advancing >= 6:      breadth = "BULLISH"
    elif advancing >= 4:      breadth = "MIXED"
    elif advancing >= 2:      breadth = "BEARISH"
    else:                     breadth = "BROAD SELLOFF"
    breadth_cls = "bull" if advancing >= 6 else "bear" if advancing <= 3 else "neu"

    gc.collect()
    result = {
        "sectors":     sorted_sectors,
        "broad":       list(broad.values()),
        "breadth":     breadth,
        "breadth_cls": breadth_cls,
        "advancing":   advancing,
        "declining":   total - advancing,
        "total":       total,
        "timestamp":   datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
    }
    if sorted_sectors:
        _cache_set("sectors", result)
    return result
