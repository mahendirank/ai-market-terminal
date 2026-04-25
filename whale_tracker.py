"""
Whale Tracker — Superinvestor 13F filings via dataroma.com (free).
Tracks what Buffett, Soros, Burry, Dalio are buying/selling each quarter.
13F filings are public (SEC), dataroma aggregates them for free.
"""
import os, json, time, sqlite3, threading, requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

IST       = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 21600  # 6 hours — 13F data is quarterly, no need to refresh often
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "whale_cache.db")
_db_lock  = threading.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Referer": "https://www.dataroma.com/",
}

# Known superinvestors for context
SUPERINVESTORS = {
    "Berkshire Hathaway": "Buffett",
    "Soros Fund":         "Soros",
    "Pershing Square":    "Ackman",
    "Appaloosa":          "Tepper",
    "Third Point":        "Loeb",
    "Renaissance":        "Simons",
    "Greenlight":         "Einhorn",
}


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS wt (key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL)")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row  = conn.execute("SELECT data,ts FROM wt WHERE key=?", (key,)).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO wt(key,data,ts) VALUES(?,?,?)",
                         (key, json.dumps(data), time.time()))
            conn.commit(); conn.close()
    except: pass


def _scrape_recent_trades():
    """Get recent 13F trades from dataroma home page."""
    resp = requests.get("https://www.dataroma.com/m/home.php", headers=HEADERS, timeout=12)
    soup = BeautifulSoup(resp.text, "html.parser")
    trades = []
    table  = soup.find("table")
    if not table:
        return []
    for row in table.find_all("tr")[1:26]:  # skip header, take 25 rows
        cols = row.find_all("td")
        if len(cols) < 4:
            continue
        try:
            date_filed = cols[0].get_text(strip=True)
            stock_text = cols[1].get_text(" ", strip=True)
            val_text   = cols[2].get_text(strip=True).replace(",", "")
            price_text = cols[3].get_text(strip=True).replace(",", "")

            # stock_text format: "AAPL - APPLE INC" or "BMI - BADGER METER INC"
            parts  = stock_text.split(" - ", 1)
            ticker = parts[0].strip() if parts else stock_text
            name   = parts[1].strip()[:35] if len(parts) > 1 else ""

            val   = float(val_text) if val_text.replace(".","").isdigit() else 0
            price = float(price_text) if price_text.replace(".","").isdigit() else 0

            if ticker and val > 0:
                trades.append({
                    "date":   date_filed,
                    "ticker": ticker,
                    "name":   name,
                    "value":  val,
                    "price":  price,
                })
        except Exception:
            continue
    return trades


def _scrape_top_holdings():
    """Get most owned stocks by superinvestors."""
    resp = requests.get("https://www.dataroma.com/m/home.php", headers=HEADERS, timeout=12)
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    holdings = []
    if len(tables) < 2:
        return holdings
    # Table 1 has superinvestor stats including top stocks
    for row in tables[1].find_all("tr")[1:6]:
        text = row.get_text(" ", strip=True)
        if text:
            holdings.append(text[:80])
    return holdings


def get_whale_data():
    cached = _cache_get("whales")
    if cached: return cached
    try:
        trades   = _scrape_recent_trades()
        holdings = _scrape_top_holdings()

        # Find tickers appearing multiple times (consensus buys)
        from collections import Counter
        ticker_counts = Counter(t["ticker"] for t in trades)
        consensus = [
            {"ticker": tk, "count": cnt, "total_value": sum(t["value"] for t in trades if t["ticker"] == tk)}
            for tk, cnt in ticker_counts.most_common(5) if cnt >= 2
        ]

        result = {
            "recent_trades": trades[:20],
            "consensus":     consensus,
            "top_holdings":  holdings,
            "timestamp":     datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
            "note":          "Source: dataroma.com — Superinvestor 13F SEC filings (quarterly)",
        }
        if trades:
            _cache_set("whales", result)
        return result
    except Exception as e:
        return {"recent_trades": [], "consensus": [], "error": str(e)}
