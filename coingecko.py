"""
CoinGecko — Free Crypto Market Data (no API key needed).
Top 10 coins, global market cap, BTC dominance, 24h change.
"""
import os, json, time, sqlite3, threading, requests
from datetime import datetime, timezone, timedelta

IST       = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 300   # 5 minutes
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "coingecko_cache.db")
_db_lock  = threading.Lock()
BASE_URL  = "https://api.coingecko.com/api/v3"
HEADERS   = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

TOP_COINS = ["bitcoin","ethereum","ripple","binancecoin","solana",
             "cardano","dogecoin","tron","avalanche-2","chainlink"]


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS cg (key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL)")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row  = conn.execute("SELECT data,ts FROM cg WHERE key=?", (key,)).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO cg(key,data,ts) VALUES(?,?,?)",
                         (key, json.dumps(data), time.time()))
            conn.commit(); conn.close()
    except: pass


def get_crypto_markets():
    """Top 10 coins — price, 24h change, market cap, volume."""
    cached = _cache_get("markets")
    if cached: return cached
    try:
        resp = requests.get(f"{BASE_URL}/coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 10, "page": 1, "sparkline": "false",
            "price_change_percentage": "24h,7d",
        }, headers=HEADERS, timeout=10)
        coins = []
        for c in resp.json():
            chg24 = c.get("price_change_percentage_24h") or 0
            chg7d = c.get("price_change_percentage_7d_in_currency") or 0
            coins.append({
                "symbol":   c["symbol"].upper(),
                "name":     c["name"],
                "price":    c["current_price"],
                "chg24h":   round(chg24, 2),
                "chg7d":    round(chg7d, 2),
                "mcap_b":   round((c.get("market_cap") or 0) / 1e9, 2),
                "vol_b":    round((c.get("total_volume") or 0) / 1e9, 2),
                "arrow":    "▲" if chg24 > 0 else "▼",
            })
        _cache_set("markets", coins)
        return coins
    except: return []


def get_global_crypto():
    """Total market cap, BTC dominance, 24h volume, active coins."""
    cached = _cache_get("global")
    if cached: return cached
    try:
        d = requests.get(f"{BASE_URL}/global", headers=HEADERS, timeout=8).json().get("data", {})
        result = {
            "total_mcap_t":   round((d.get("total_market_cap", {}).get("usd") or 0) / 1e12, 3),
            "total_vol_b":    round((d.get("total_volume", {}).get("usd") or 0) / 1e9, 1),
            "btc_dominance":  round(d.get("market_cap_percentage", {}).get("btc") or 0, 1),
            "eth_dominance":  round(d.get("market_cap_percentage", {}).get("eth") or 0, 1),
            "active_coins":   d.get("active_cryptocurrencies", 0),
            "mcap_chg_24h":   round(d.get("market_cap_change_percentage_24h_usd") or 0, 2),
            "timestamp":      datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        }
        _cache_set("global", result)
        return result
    except: return {}


def get_crypto_snapshot():
    return {
        "markets": get_crypto_markets(),
        "global":  get_global_crypto(),
    }
