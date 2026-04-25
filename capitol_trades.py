"""
Capitol Trades — US Congress member stock trades (legal insider signal).
Politicians must disclose trades within 45 days by law (STOCK Act).
Cluster buys = multiple congress members buying same ticker = strong signal.
"""
import os, json, time, sqlite3, threading, requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from collections import defaultdict

IST       = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 3600  # 1 hour
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "capitol_cache.db")
_db_lock  = threading.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS ct (key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL)")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row  = conn.execute("SELECT data,ts FROM ct WHERE key=?", (key,)).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO ct(key,data,ts) VALUES(?,?,?)",
                         (key, json.dumps(data), time.time()))
            conn.commit(); conn.close()
    except: pass


def _scrape_trades():
    resp = requests.get("https://www.capitoltrades.com/trades", headers=HEADERS, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")
    trades = []
    for row in soup.select("tbody tr")[:30]:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        try:
            # Cell layout: 0=politician, 1=company+ticker, 2=filed, 3=traded, 4=gap, 5=owner, 6=type, 7=amount, 8=price
            politician  = cells[0].get_text(" ", strip=True)
            company_raw = cells[1].get_text(" ", strip=True)
            traded      = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            tx_type     = cells[6].get_text(strip=True).lower() if len(cells) > 6 else ""
            amount      = cells[7].get_text(strip=True) if len(cells) > 7 else ""
            price       = cells[8].get_text(strip=True) if len(cells) > 8 else ""

            party = "R" if "Republican" in politician else "D" if "Democrat" in politician else "?"
            name  = politician.split("Republican")[0].split("Democrat")[0].strip()

            # company_raw: "Farmers & Merchants Bancorp Inc FMAO:US"
            parts  = company_raw.rsplit(" ", 1)
            ticker_raw = parts[-1] if len(parts) > 1 else ""
            company    = parts[0].strip()[:40] if len(parts) > 1 else company_raw[:40]
            ticker     = ticker_raw.split(":")[0].strip()

            if ticker and tx_type in ("buy", "sell", "purchase", "exchange"):
                trades.append({
                    "name":    name,
                    "party":   party,
                    "ticker":  ticker,
                    "company": company,
                    "type":    "BUY" if tx_type in ("buy", "purchase", "exchange") else "SELL",
                    "amount":  amount,
                    "price":   price,
                    "traded":  traded,
                })
        except Exception:
            continue
    return trades


def _detect_clusters(trades):
    """Find tickers bought by 2+ DISTINCT congress members."""
    buys = defaultdict(set)
    for t in trades:
        if t["type"] == "BUY":
            buys[t["ticker"]].add(t["name"])
    return sorted([
        {"ticker": tk, "count": len(names), "politicians": list(names)[:5]}
        for tk, names in buys.items() if len(names) >= 2
    ], key=lambda x: -x["count"])


def get_congress_trades():
    cached = _cache_get("congress")
    if cached: return cached
    try:
        trades   = _scrape_trades()
        clusters = _detect_clusters(trades)
        result   = {
            "trades":   trades[:20],
            "clusters": sorted(clusters, key=lambda x: -x["count"]),
            "total":    len(trades),
            "timestamp": datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        }
        if trades:
            _cache_set("congress", result)
        return result
    except Exception as e:
        return {"trades": [], "clusters": [], "error": str(e)}
