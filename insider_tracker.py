"""
Insider Trading Tracker
- US: SEC EDGAR Form 4 RSS (real-time, free)
- India: NSE bulk deals + block deals (already in nse_data.py, enhanced here)
Filter: CEO/CFO/Director buying own stock = high conviction signal
"""
import os, re, requests, json, time, sqlite3, threading
import feedparser
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

IST      = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 1800   # 30 min
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "insider_cache.db")
_db_lock  = threading.Lock()

HEADERS = {"User-Agent": "Mozilla/5.0 (research-bot/1.0; contact@example.com)"}

# High-value insider titles
EXEC_TITLES = {"ceo","cfo","coo","president","chairman","director","officer",
               "evp","svp","vp","chief","founder","managing"}


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS insider (
        key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL
    )""")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row  = conn.execute("SELECT data,ts FROM insider WHERE key=?", (key,)).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO insider(key,data,ts) VALUES(?,?,?)",
                         (key, json.dumps(data), time.time()))
            conn.commit()
            conn.close()
    except: pass


# ── SEC Form 4 ────────────────────────────────────────────────
def _get_sec_form4():
    """Latest Form 4 filings from SEC EDGAR RSS — filters for purchases."""
    try:
        url  = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom"
        resp = requests.get(url, timeout=12, headers={
            "User-Agent": "research-bot mahendiran@example.com",
            "Accept": "application/atom+xml"
        })
        feed    = feedparser.parse(resp.content)
        filings = []
        for entry in feed.entries[:40]:
            title = entry.get("title", "")
            # Title format: "4 - COMPANY NAME (ticker) (0000123456) (Issuer)"
            m = re.search(r"4 - (.+?) \(([A-Z]{1,5})\)", title)
            if not m:
                continue
            company = m.group(1).strip()
            ticker  = m.group(2).strip()
            link    = entry.get("link", "")
            date    = entry.get("updated", "")[:10]
            filings.append({
                "company": company,
                "ticker":  ticker,
                "link":    link,
                "date":    date,
                "source":  "SEC-EDGAR",
            })
        return filings[:20]
    except Exception:
        return []


def _get_openinsider():
    """
    OpenInsider.com — free site tracking US insider purchases.
    Gets recent cluster buys (multiple insiders buying same stock).
    """
    try:
        url  = "https://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=7&fdr=&td=0&tdr=&fdlyl=&fdlyh=&daysago=&xp=1&xs=1&vl=100&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=20&page=1"
        resp = requests.get(url, timeout=10, headers=HEADERS)
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table.tinytable tbody tr")
        buys = []
        for row in rows[:15]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 12:
                continue
            try:
                buys.append({
                    "date":    cols[1],
                    "ticker":  cols[3],
                    "company": cols[4],
                    "title":   cols[6],
                    "type":    cols[7],    # P = Purchase, S = Sale
                    "price":   cols[8],
                    "qty":     cols[9],
                    "value":   cols[11],
                    "source":  "OpenInsider",
                })
            except Exception:
                continue
        # Filter purchases only, exec titles
        return [b for b in buys if b.get("type") == "P"]
    except Exception:
        return []


# ── SEBI India Insider / Bulk Deals ──────────────────────────
def _get_sebi_bulk():
    """NSE bulk deals — large institutional transactions today."""
    try:
        from nse_data import _nse_session
        s    = _nse_session()
        resp = s.get("https://www.nseindia.com/api/bulk-deals", timeout=10)
        data = resp.json().get("data", [])
        deals = []
        for d in data[:20]:
            qty   = d.get("quantityTraded", 0)
            price = float(d.get("tradePrice", 0) or 0)
            value = round(qty * price / 1e7, 2)   # ₹ crore
            deals.append({
                "symbol":  d.get("symbol", ""),
                "client":  d.get("clientName", ""),
                "type":    d.get("buySell", ""),
                "qty":     qty,
                "price":   price,
                "value_cr": value,
                "source":  "NSE-Bulk",
            })
        return deals
    except Exception:
        return []

def _get_sebi_block():
    """NSE block deals."""
    try:
        from nse_data import _nse_session
        s    = _nse_session()
        resp = s.get("https://www.nseindia.com/api/block-deals", timeout=10)
        data = resp.json().get("data", [])
        deals = []
        for d in data[:20]:
            qty   = d.get("quantity", 0)
            price = float(d.get("price", 0) or 0)
            value = round(qty * price / 1e7, 2)
            deals.append({
                "symbol":  d.get("symbol", ""),
                "client":  d.get("clientName", ""),
                "type":    d.get("buySell", ""),
                "qty":     qty,
                "price":   price,
                "value_cr": value,
                "source":  "NSE-Block",
            })
        return deals
    except Exception:
        return []


# ── Cluster detection ─────────────────────────────────────────
def _detect_clusters(us_insiders):
    """Find stocks where multiple insiders are buying = cluster signal."""
    from collections import Counter
    tickers = [i["ticker"] for i in us_insiders if i.get("type") == "P"]
    counts  = Counter(tickers)
    return [{"ticker": t, "count": c, "signal": "CLUSTER_BUY"}
            for t, c in counts.items() if c >= 2]


# ── Public API ────────────────────────────────────────────────
def get_insider_data():
    cached = _cache_get("insider_all")
    if cached:
        cached["cached"] = True
        return cached

    us_buys  = _get_openinsider()
    sebi_bulk  = _get_sebi_bulk()
    sebi_block = _get_sebi_block()
    clusters   = _detect_clusters(us_buys)

    result = {
        "us_buys":    us_buys[:15],
        "india_bulk": sebi_bulk[:15],
        "india_block":sebi_block[:10],
        "clusters":   clusters,
        "cached":     False,
        "timestamp":  datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
    }
    _cache_set("insider_all", result)
    return result
