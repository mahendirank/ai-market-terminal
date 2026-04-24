"""
FRED (St. Louis Fed) — Global Liquidity Monitor
Tracks: Fed balance sheet, M2 money supply, Reverse Repo,
        Real rates, yield curve spread, dollar liquidity.
FRED API key: free at fred.stlouisfed.org/docs/api/api_key.html
Falls back to web scraping if no key.
"""
import os, json, time, sqlite3, threading, requests
from datetime import datetime, timezone, timedelta

IST       = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 21600   # 6 hours
FRED_KEY  = os.environ.get("FRED_API_KEY", "")
FRED_URL  = "https://api.stlouisfed.org/fred/series/observations"
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "fred_cache.db")
_db_lock  = threading.Lock()

# Key series
SERIES = {
    "FED_BALANCE":  ("WALCL",     "Fed Balance Sheet ($B)",      1e9),
    "M2":           ("M2SL",      "M2 Money Supply ($B)",        1e9),
    "REVERSE_REPO": ("RRPONTSYD", "Fed Reverse Repo ($B)",       1e9),
    "YIELD_10Y":    ("DGS10",     "US 10Y Treasury Yield (%)",   1),
    "YIELD_2Y":     ("DGS2",      "US 2Y Treasury Yield (%)",    1),
    "REAL_RATE":    ("DFII10",    "10Y Real Rate (TIPS) (%)",    1),
    "CPI_YOY":      ("CPIAUCSL",  "CPI YoY (%)",                 1),
}


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS fred (
        key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL
    )""")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row  = conn.execute("SELECT data,ts FROM fred WHERE key=?", (key,)).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO fred(key,data,ts) VALUES(?,?,?)",
                         (key, json.dumps(data), time.time()))
            conn.commit()
            conn.close()
    except: pass


def _fetch_series(series_id, limit=3):
    """Fetch latest N observations for a FRED series."""
    if not FRED_KEY:
        return []
    try:
        resp = requests.get(
            FRED_URL,
            params={
                "series_id":      series_id,
                "api_key":        FRED_KEY,
                "file_type":      "json",
                "sort_order":     "desc",
                "limit":          limit,
                "observation_start": "2020-01-01",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            obs = resp.json().get("observations", [])
            return [(o["date"], o["value"]) for o in obs if o["value"] != "."]
    except Exception:
        pass
    return []


def _fetch_without_key():
    """
    Fallback: get key liquidity metrics from yfinance when FRED key not set.
    Uses treasury ETFs and Fed-related tickers as proxies.
    """
    try:
        import yfinance as yf
        tickers = {"^TNX": "yield_10y", "^IRX": "yield_3m", "^FVX": "yield_5y"}
        result  = {}
        data    = yf.download(list(tickers.keys()), period="5d",
                               interval="1d", auto_adjust=True, progress=False)
        closes  = data["Close"] if hasattr(data.columns, "levels") else data
        for sym, key in tickers.items():
            if sym in closes.columns:
                val = closes[sym].dropna().iloc[-1]
                result[key] = round(float(val), 3)
        # Yield curve spread
        if "yield_10y" in result and "yield_3m" in result:
            result["yield_curve_spread"] = round(result["yield_10y"] - result["yield_3m"], 3)
            result["yield_curve_signal"] = (
                "INVERTED" if result["yield_curve_spread"] < 0 else
                "FLAT"     if result["yield_curve_spread"] < 0.5 else "NORMAL"
            )
        return result
    except Exception:
        return {}


def get_liquidity():
    """Main function — returns global liquidity dashboard."""
    cached = _cache_get("liquidity")
    if cached:
        cached["cached"] = True
        return cached

    result  = {}
    has_key = bool(FRED_KEY)

    if has_key:
        for key, (sid, label, divisor) in SERIES.items():
            obs = _fetch_series(sid, limit=4)
            if len(obs) >= 2:
                curr_val  = float(obs[0][1]) / divisor
                prev_val  = float(obs[1][1]) / divisor
                change    = curr_val - prev_val
                change_pct= round(change / prev_val * 100, 2) if prev_val else 0
                result[key] = {
                    "label":      label,
                    "value":      round(curr_val, 2),
                    "prev":       round(prev_val, 2),
                    "change":     round(change, 2),
                    "change_pct": change_pct,
                    "date":       obs[0][0],
                    "direction":  "UP" if change > 0 else "DOWN",
                }
            time.sleep(0.15)   # FRED rate limit: 120 req/min

        # Derived: yield curve spread
        y10 = result.get("YIELD_10Y", {}).get("value")
        y2  = result.get("YIELD_2Y",  {}).get("value")
        if y10 and y2:
            spread = round(y10 - y2, 3)
            result["YIELD_CURVE"] = {
                "spread":  spread,
                "signal":  "INVERTED" if spread < 0 else "FLAT" if spread < 0.5 else "NORMAL",
                "label":   "10Y-2Y Spread",
            }

        # Liquidity regime signal
        fed_dir = result.get("FED_BALANCE", {}).get("direction")
        m2_dir  = result.get("M2", {}).get("direction")
        if fed_dir == "UP" and m2_dir == "UP":
            regime = {"signal": "EXPANDING", "bias": "RISK-ON", "color": "green"}
        elif fed_dir == "DOWN" and m2_dir == "DOWN":
            regime = {"signal": "CONTRACTING", "bias": "RISK-OFF", "color": "red"}
        else:
            regime = {"signal": "MIXED", "bias": "NEUTRAL", "color": "yellow"}
        result["REGIME"] = regime

    else:
        # No FRED key — use yfinance proxy
        proxy = _fetch_without_key()
        result["no_key"] = True
        result["note"]   = "Add FRED_API_KEY in Railway Variables (free at fred.stlouisfed.org)"
        result.update(proxy)

        y10 = proxy.get("yield_10y")
        y3m = proxy.get("yield_3m")
        if y10 and y3m:
            spread = round(y10 - y3m, 3)
            result["YIELD_CURVE"] = {
                "spread": spread,
                "signal": "INVERTED" if spread < 0 else "FLAT" if spread < 0.5 else "NORMAL",
                "label":  "10Y-3M Spread",
            }

    result["cached"]    = False
    result["has_key"]   = has_key
    result["timestamp"] = datetime.now(IST).strftime("%d-%b-%Y %H:%M IST")
    _cache_set("liquidity", result)
    return result
