"""
Cross-Asset Correlation Engine
Tracks live correlations between key assets and signals when they BREAK.
Correlation breaks = biggest trading opportunities.
"""
import os, json, time, sqlite3, threading
import numpy as np
from datetime import datetime, timezone, timedelta

IST       = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 3600   # 1 hour
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "corr_cache.db")
_db_lock  = threading.Lock()

# Key pairs: (asset_a, asset_b, normal_direction, description)
PAIRS = [
    ("GC=F",   "DX-Y.NYB", "negative", "Gold vs DXY — breaks during crisis (buy gold)"),
    ("^TNX",   "^NSEI",    "negative", "US 10Y Yield vs Nifty — breaks during earnings"),
    ("CL=F",   "USDINR=X", "positive", "Oil vs USD/INR — rupee weakens when oil rises"),
    ("^VIX",   "^GSPC",    "negative", "VIX vs S&P 500 — fear gauge vs equity"),
    ("GC=F",   "^TNX",     "negative", "Gold vs US Yields — rates up = gold pressure"),
    ("^NSEI",  "^GSPC",    "positive", "Nifty vs S&P 500 — global risk-on correlation"),
    ("BTC-USD", "^GSPC",   "positive", "Bitcoin vs S&P — risk asset correlation"),
    ("GC=F",   "SI=F",     "positive", "Gold vs Silver — ratio divergence signal"),
]

LABELS = {
    "GC=F":    "Gold",     "DX-Y.NYB": "DXY",    "^TNX": "US 10Y",
    "^NSEI":   "Nifty",    "CL=F":     "Oil",     "USDINR=X": "USD/INR",
    "^VIX":    "VIX",      "^GSPC":    "S&P 500", "BTC-USD": "Bitcoin",
    "SI=F":    "Silver",
}


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS corr (
        key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL
    )""")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row  = conn.execute("SELECT data,ts FROM corr WHERE key=?", (key,)).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO corr(key,data,ts) VALUES(?,?,?)",
                         (key, json.dumps(data), time.time()))
            conn.commit()
            conn.close()
    except: pass


def _download_prices(symbols, period="20d", interval="1d"):
    """Download one symbol at a time to keep peak memory low (Railway 512MB limit)."""
    try:
        import gc
        import yfinance as yf
        import pandas as pd
        frames = {}
        for sym in symbols:
            try:
                t   = yf.Ticker(sym)
                df  = t.history(period=period, interval=interval, auto_adjust=True)
                if not df.empty:
                    frames[sym] = df["Close"].dropna()
                del df
            except Exception:
                pass
        gc.collect()
        if not frames:
            return None
        return pd.DataFrame(frames).dropna(how="all")
    except Exception:
        return None


def _corr_signal(corr, normal_dir):
    """
    Returns signal based on how far correlation has deviated from normal.
    normal_dir: 'positive' or 'negative'
    """
    if corr is None:
        return "UNKNOWN"
    expected = 1.0 if normal_dir == "positive" else -1.0
    deviation = abs(corr - expected)
    if deviation > 1.2:   return "STRONG_BREAK"
    if deviation > 0.8:   return "BREAK"
    if deviation > 0.4:   return "WEAKENING"
    return "NORMAL"


def get_correlations():
    import gc
    cached = _cache_get("correlations")
    if cached:
        cached["cached"] = True
        return cached

    all_symbols = list(set(s for pair in PAIRS for s in pair[:2]))
    prices      = _download_prices(all_symbols)

    results = []
    for sym_a, sym_b, normal_dir, description in PAIRS:
        try:
            if prices is None or sym_a not in prices.columns or sym_b not in prices.columns:
                results.append({
                    "pair": f"{LABELS.get(sym_a, sym_a)} / {LABELS.get(sym_b, sym_b)}",
                    "corr_30d": None, "corr_7d": None,
                    "signal": "NO_DATA", "normal": normal_dir,
                    "description": description,
                })
                continue

            s_a = prices[sym_a].dropna()
            s_b = prices[sym_b].dropna()
            idx = s_a.index.intersection(s_b.index)

            corr_30d = float(s_a[idx].corr(s_b[idx])) if len(idx) >= 5 else None
            corr_7d  = float(s_a[idx[-7:]].corr(s_b[idx[-7:]])) if len(idx) >= 7 else None

            # Use 7d vs 30d divergence to detect recent break
            signal = _corr_signal(corr_7d, normal_dir)
            # Extra: if 7d correlation has shifted significantly vs 30d
            corr_shift = abs(corr_7d - corr_30d) if corr_7d is not None and corr_30d is not None else 0

            results.append({
                "pair":        f"{LABELS.get(sym_a, sym_a)} / {LABELS.get(sym_b, sym_b)}",
                "sym_a":       LABELS.get(sym_a, sym_a),
                "sym_b":       LABELS.get(sym_b, sym_b),
                "corr_30d":    round(corr_30d, 3) if corr_30d is not None else None,
                "corr_7d":     round(corr_7d, 3)  if corr_7d  is not None else None,
                "corr_shift":  round(corr_shift, 3),
                "signal":      signal,
                "normal":      normal_dir,
                "description": description,
                "alert":       signal in ("STRONG_BREAK", "BREAK"),
            })
        except Exception:
            continue

    # Sort: breaks first
    results.sort(key=lambda x: (0 if x.get("alert") else 1, -abs(x.get("corr_shift", 0))))

    del prices
    gc.collect()

    data = {
        "pairs":     results,
        "breaks":    [r for r in results if r.get("alert")],
        "cached":    False,
        "timestamp": datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
    }
    _cache_set("correlations", data)
    return data
