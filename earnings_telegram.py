"""
Live earnings extraction pipeline:
  Telegram + Web → Regex/Ollama → Structured earnings data

Sources:
  DreamCatcher   — India earnings (CNBC TV18 / ET NOW live)
  MoneyControl   — India results with PAT/revenue numbers
  WalterBloomberg — US earnings beats/misses
  FinancialJuice  — Global macro + earnings wire (structured $TICKER format)
  NSE RSS        — Official NSE board meeting / results announcements
  SearXNG        — Real-time web search for specific company results
"""
import os
import re
import json
import time
import sqlite3
import requests
import threading
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

# Read from env (Docker) or fall back to localhost
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434/api/generate")
SEARXNG_URL  = os.getenv("SEARXNG_URL",  "http://localhost:8888")
OLLAMA_MODEL = "llama3.2:latest"
DB_PATH      = (os.getenv("DB_PATH")
               or ("/app/db/earn_tg_cache.db" if os.path.isdir("/app/db") else
               os.path.join(os.path.dirname(os.path.abspath(__file__)), "earn_tg_cache.db")))
CACHE_TTL    = 600   # 10 minutes

CHANNELS = {
    "DreamCatcher":    "https://t.me/s/thedreamcatcher0",
    "MoneyControl":    "https://t.me/s/moneycontrolcom",
    "WalterBloomberg": "https://t.me/s/WalterBloomberg",
    "FinancialJuice":  "https://t.me/s/financialjuice",
}

# NSE official board results RSS
NSE_RSS_URLS = [
    "https://www.nseindia.com/api/latest-circular?category=results",
]

LOOKBACK_HOURS = 120   # 5 days — catches recently reported quarters

EARN_KEYWORDS = [
    "result", "results", "earnings", "q4", "q3", "q2", "q1",
    "quarter", "pat", "profit", "revenue", "eps", "beat", "beats",
    "miss", "misses", "guidance", "nim", "margin", "fy26", "fy25",
    "fy2026", "annual", "quarterly", "declares", "reports",
]

# NSE symbol lookup — company name / keyword → ticker
INDIA_LOOKUP = {
    "hdfc bank": "HDFCBANK", "hdfcbank": "HDFCBANK", "hdfc": "HDFCBANK",
    "icici bank": "ICICIBANK", "icicibank": "ICICIBANK", "icici": "ICICIBANK",
    "tcs": "TCS", "tata consultancy": "TCS",
    "infosys": "INFY", "infy": "INFY",
    "wipro": "WIPRO",
    "hcl tech": "HCLTECH", "hcltech": "HCLTECH", "hcl technologies": "HCLTECH",
    "reliance": "RELIANCE", "ril": "RELIANCE",
    "sbi": "SBIN", "state bank": "SBIN",
    "kotak bank": "KOTAKBANK", "kotakbank": "KOTAKBANK",
    "axis bank": "AXISBANK", "axisbank": "AXISBANK",
    "bajaj finance": "BAJFINANCE", "bajajfinance": "BAJFINANCE",
    "bajaj finserv": "BAJAJFINSV",
    "sun pharma": "SUNPHARMA", "sunpharma": "SUNPHARMA",
    "dr reddy": "DRREDDY", "drreddy": "DRREDDY",
    "cipla": "CIPLA",
    "nestle": "NESTLEIND", "nestlé": "NESTLEIND",
    "tata motors": "TATAMOTORS", "tatamotors": "TATAMOTORS",
    "maruti": "MARUTI", "maruti suzuki": "MARUTI",
    "l&t": "LT", "larsen": "LT",
    "ntpc": "NTPC",
    "ongc": "ONGC",
    "titan": "TITAN",
    "asian paints": "ASIANPAINT",
    "ltimindtree": "LTIM", "lti": "LTIM",
    "persistent": "PERSISTENT",
    "tech mahindra": "TECHM", "techmahindra": "TECHM",
    "indusind bank": "INDUSINDBK",
    "hindustan unilever": "HINDUNILVR", "hul": "HINDUNILVR",
    "itc": "ITC",
    "power grid": "POWERGRID",
    "tata steel": "TATASTEEL",
    "jsw steel": "JSWSTEEL",
    "hindalco": "HINDALCO",
    "vedanta": "VEDL",
    "bpcl": "BPCL",
    "apollo": "APOLLOHOSP",
    "zomato": "ZOMATO", "eternal": "ZOMATO",
    "dmart": "DMART",
}


# ── SQLite cache ──────────────────────────────────────────────────────────────
_db_lock = threading.Lock()

def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS earn_tg (
            ticker TEXT PRIMARY KEY,
            data   TEXT NOT NULL,
            ts     REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_messages (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            text   TEXT,
            ts     REAL
        )
    """)
    conn.commit()
    return conn


def _cache_get_all():
    try:
        with _db_lock:
            conn = _db()
            cutoff = time.time() - CACHE_TTL
            rows = conn.execute(
                "SELECT ticker, data FROM earn_tg WHERE ts > ?", (cutoff,)
            ).fetchall()
            conn.close()
        return {r[0]: json.loads(r[1]) for r in rows}
    except Exception:
        return {}


def _cache_set(ticker, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute(
                "INSERT OR REPLACE INTO earn_tg(ticker,data,ts) VALUES(?,?,?)",
                (ticker, json.dumps(data), time.time())
            )
            conn.commit()
            conn.close()
    except Exception:
        pass


# ── Telegram channel fetcher ──────────────────────────────────────────────────
def _fetch_channel(name, url):
    messages = []
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    try:
        r    = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        msgs = soup.select(".tgme_widget_message")
        for msg in msgs:
            txt = msg.select_one(".tgme_widget_message_text")
            if not txt:
                continue
            text = txt.get_text(" ", strip=True)
            tl   = text.lower()
            if not any(k in tl for k in EARN_KEYWORDS):
                continue
            if len(text) < 25:
                continue
            messages.append({"source": name, "text": text[:1200]})
    except Exception:
        pass
    return messages


# ── SearXNG web search for earnings ──────────────────────────────────────────
_SEARXNG_OK = None   # None = untested, True/False = cached result

def _check_searxng():
    global _SEARXNG_OK
    if _SEARXNG_OK is None:
        try:
            r = requests.get(f"{SEARXNG_URL}/search",
                params={"q":"test","format":"json"}, timeout=5)
            _SEARXNG_OK = r.status_code == 200
        except Exception:
            _SEARXNG_OK = False
    return _SEARXNG_OK


def _searxng_earnings(ticker, company_name, quarter="Q4 FY26"):
    """Search SearXNG for latest earnings of a specific company. Returns parsed records."""
    if not _check_searxng():
        return []
    try:
        r = requests.get(f"{SEARXNG_URL}/search", params={
            "q": f"{company_name} {quarter} results profit revenue crore",
            "format": "json", "categories": "news,general",
            "time_range": "week", "language": "en",
        }, timeout=8)
        if r.status_code != 200:
            return []
        results = r.json().get("results", [])
        records = []
        for res in results[:4]:
            title   = res.get("title","")
            content = res.get("content","")
            text    = f"{title} | {content}"[:500]

            # Extract PAT from snippet: "Profit grows X% to Rs Y crore" or "PAT ₹Y crore"
            pat_m = re.search(
                r'(?:profit|PAT)[^\d]*?(?:rises?|grows?|falls?|dips?|at|to|of|:)[^\d]*?'
                r'(?:Rs\.?\s*|₹\s*)([\d,]+\.?\d*)\s*crore',
                text, re.IGNORECASE
            )
            pat_cr = float(pat_m.group(1).replace(",","")) if pat_m else None

            # Extract NII/revenue from snippet
            rev_m = re.search(
                r'(?:revenue|NII|income)[^\d]*?(?:rises?|grows?|at|to|of|:)[^\d]*?'
                r'(?:Rs\.?\s*|₹\s*)([\d,]+\.?\d*)\s*crore',
                text, re.IGNORECASE
            )
            rev_cr = float(rev_m.group(1).replace(",","")) if rev_m else None

            # YoY growth
            yoy_m = re.search(r'([\d.]+)\s*%\s*(?:YoY|year[-\s]on[-\s]year)', text, re.IGNORECASE)
            yoy   = float(yoy_m.group(1)) if yoy_m else None
            # Check if it was a fall
            if yoy and re.search(r'(?:fall|fall|drop|declin|dip|down).*?' + str(int(yoy)) + r'.*?%', text, re.IGNORECASE):
                yoy = -yoy

            # NIM for banks
            nim_m = re.search(r'NIM[^\d]*?([\d.]+)\s*%', text, re.IGNORECASE)
            nim = float(nim_m.group(1)) if nim_m else None

            if pat_cr or rev_cr:
                qm = re.search(r'Q(\d)\s*FY(\d+)', text, re.IGNORECASE)
                quarter_str = f"Q{qm.group(1)} FY{qm.group(2)}" if qm else quarter
                records.append({
                    "ticker": ticker, "company": company_name,
                    "region": "INDIA", "quarter": quarter_str,
                    "eps": None, "pat_cr": pat_cr, "revenue_cr": rev_cr,
                    "net_income_b": None, "revenue_b": None,
                    "yoy_growth": yoy, "beat_miss": None, "guidance": None,
                    "nim_pct": nim, "confidence": "MED",
                    "source": "SearXNG",
                })
        return records
    except Exception:
        return []


# ── Fast regex parser (primary — works without LLM) ──────────────────────────
def _parse_fast(messages):
    """
    Regex-based parser for structured Telegram/web formats.
    Handles FinancialJuice ($TICKER), DreamCatcher (COMPANY: METRIC),
    MoneyControl (Company Q4: revenue/profit with % change) patterns.
    """
    records = []

    for msg in messages:
        text   = msg["text"]
        source = msg["source"]
        tl     = text.lower()

        # ── FinancialJuice: $TICKER Company Q1 2026 Earnings Adj EPS $X.XX Rev. $X.XXB ──
        fj = re.match(
            r'^\$([A-Z]{1,5})\s+(.*?)\s+Q(\d)\s+(\d{4})\s+Earnings?\s+'
            r'(?:Adj(?:usted)?\s+)?EPS\s+\$?([\d.]+).*?Rev(?:enue)?\.?\s+\$?([\d.]+)([BMK]?)',
            text, re.IGNORECASE
        )
        if fj:
            ticker   = fj.group(1)
            company  = fj.group(2).strip()
            qnum     = fj.group(3)
            year     = fj.group(4)
            eps      = float(fj.group(5))
            rev_val  = float(fj.group(6))
            rev_unit = fj.group(7).upper()
            rev_b = rev_val if rev_unit in ("B", "") else (rev_val/1000 if rev_unit=="M" else rev_val*1000)

            # Beat/miss vs estimate
            est_m = re.search(r'est\.\s+\$?([\d.]+)', text, re.IGNORECASE)
            beat_miss = None
            if est_m:
                est = float(est_m.group(1))
                beat_miss = "BEAT" if eps > est else ("MISS" if eps < est else "INLINE")

            # Guidance
            guidance = None
            if re.search(r'raises?|rais(?:ing|ed)', tl): guidance = "RAISED"
            elif re.search(r'lower|cut|reduc', tl): guidance = "LOWERED"
            elif re.search(r'maintain|reaffirm|confirm', tl): guidance = "MAINTAINED"

            records.append({
                "ticker": ticker, "company": company,
                "region": "USA",
                "quarter": f"Q{qnum} {year}",
                "eps": eps, "revenue_b": round(rev_b, 2),
                "pat_cr": None, "revenue_cr": None,
                "net_income_b": None, "yoy_growth": None,
                "beat_miss": beat_miss, "guidance": guidance,
                "nim_pct": None, "confidence": "HIGH",
                "source": source,
            })
            continue

        # ── FinancialJuice short form: $TICKER Company Q1/Q2 EPS ... ──
        fj2 = re.match(
            r'^\$([A-Z]{1,5})\s+.*?(?:Adj(?:usted)?\s+)?EPS\s+\$?([\d.]+)',
            text, re.IGNORECASE
        )
        if fj2 and any(k in tl for k in ["earnings", "q1", "q2", "q3", "q4"]):
            ticker = fj2.group(1)
            eps    = float(fj2.group(2))
            qm = re.search(r'Q(\d)\s+(\d{4})', text, re.IGNORECASE)
            quarter = f"Q{qm.group(1)} {qm.group(2)}" if qm else "Latest"

            rev_m = re.search(r'Rev(?:enue)?\.?\s+\$?([\d.]+)([BMK]?)', text, re.IGNORECASE)
            rev_b = None
            if rev_m:
                rv = float(rev_m.group(1))
                ru = rev_m.group(2).upper()
                rev_b = rv if ru in ("B","") else (rv/1000 if ru=="M" else rv*1000)

            est_m = re.search(r'est\.\s+\$?([\d.]+)', text, re.IGNORECASE)
            beat_miss = None
            if est_m:
                est = float(est_m.group(1))
                beat_miss = "BEAT" if eps > est else ("MISS" if eps < est else "INLINE")

            guidance = None
            if re.search(r'raises?|rais(?:ing|ed)', tl): guidance = "RAISED"
            elif re.search(r'lower|cut|reduc', tl): guidance = "LOWERED"
            elif re.search(r'maintain|reaffirm|confirm', tl): guidance = "MAINTAINED"

            records.append({
                "ticker": ticker, "company": ticker,
                "region": "USA", "quarter": quarter,
                "eps": eps, "revenue_b": rev_b,
                "pat_cr": None, "revenue_cr": None,
                "net_income_b": None, "yoy_growth": None,
                "beat_miss": beat_miss, "guidance": guidance,
                "nim_pct": None, "confidence": "MED",
                "source": source,
            })
            continue

        # ── DreamCatcher / CNBCTV18: COMPANY: Q4 PROFIT/REVENUE ₹X CR VS Y (YOY) ──
        dc = re.match(r'^([A-Z][A-Z0-9 &\.]+?):\s+(?:CO\s+)?Q(\d)(?:FY\d{2,4})?\s+', text)
        if dc:
            company = dc.group(1).strip()
            qnum    = dc.group(2)
            qm2 = re.search(r'Q\d\s*FY(\d{2,4})', text, re.IGNORECASE)
            fy  = qm2.group(1) if qm2 else "26"
            quarter = f"Q{qnum} FY{fy[-2:]}"

            # Lookup NSE ticker
            ticker = None
            cl = company.lower()
            for k, v in INDIA_LOOKUP.items():
                if k in cl:
                    ticker = v
                    break
            if not ticker:
                words = cl.split()
                ticker = words[0].upper()[:10]

            # Revenue (₹ crore)
            rev_m = re.search(r'(?:REVENUE|TOTAL INCOME)\s+[₹]?([\d,]+\.?\d*)\s*(?:CR|CRORE)', text, re.IGNORECASE)
            rev_cr = float(rev_m.group(1).replace(",","")) if rev_m else None

            # PAT / Net Profit
            pat_m = re.search(r'(?:NET PROFIT|PAT)\s+[₹]?([\d,]+\.?\d*)\s*(?:CR|CRORE)', text, re.IGNORECASE)
            pat_cr = float(pat_m.group(1).replace(",","")) if pat_m else None

            # YoY from "VS X (YOY)" or "+X% YOY"
            yoy = None
            yoy_m = re.search(r'([+\-]?\d+\.?\d*)\s*%\s*(?:YOY|Y-O-Y)', text, re.IGNORECASE)
            if yoy_m:
                yoy = float(yoy_m.group(1))

            if rev_cr or pat_cr:
                records.append({
                    "ticker": ticker, "company": company,
                    "region": "INDIA", "quarter": quarter,
                    "eps": None, "pat_cr": pat_cr, "revenue_cr": rev_cr,
                    "net_income_b": None, "revenue_b": None,
                    "yoy_growth": yoy, "beat_miss": None, "guidance": None,
                    "nim_pct": None, "confidence": "MED",
                    "source": source,
                })
            continue

        # ── DreamCatcher short: COMPANY Q4: Revenue X% ──
        dc2 = re.match(r'^#\w+\s*\|\s*([A-Z][A-Z0-9 &\.]+?)\s+Q(\d):', text)
        if dc2:
            company = dc2.group(1).strip()
            qnum    = dc2.group(2)
            cl = company.lower()
            ticker = None
            for k, v in INDIA_LOOKUP.items():
                if k in cl:
                    ticker = v
                    break
            if not ticker:
                ticker = cl.split()[0].upper()[:10]

            yoy = None
            yoy_m = re.search(r'([+\-]?\d+\.?\d*)\s*%', text)
            if yoy_m:
                yoy = float(yoy_m.group(1))

            if yoy is not None:
                records.append({
                    "ticker": ticker, "company": company,
                    "region": "INDIA", "quarter": f"Q{qnum} FY26",
                    "eps": None, "pat_cr": None, "revenue_cr": None,
                    "net_income_b": None, "revenue_b": None,
                    "yoy_growth": yoy, "beat_miss": None, "guidance": None,
                    "nim_pct": None, "confidence": "LOW",
                    "source": source,
                })

        # ── MoneyControl: "Company Q4 net profit jumps/falls X% to Rs Y crore" ──
        mc = re.search(
            r'([A-Z][A-Za-z0-9 &\.]+?)\s+Q(\d)(?:FY\d+)?'
            r'.*?(?:net\s+profit|PAT|profit)[\s\w]*?'
            r'([+\-]?\d+\.?\d*)\s*%.*?Rs\s+([\d,]+\.?\d*)\s*crore',
            text, re.IGNORECASE
        )
        if mc and source == "MoneyControl":
            company = mc.group(1).strip()
            if len(company) < 4:   # skip garbage matches like "AS", "MC"
                continue
            qnum    = mc.group(2)
            yoy     = float(mc.group(3))
            pat_cr  = float(mc.group(4).replace(",",""))
            cl = company.lower()
            ticker = None
            for k, v in INDIA_LOOKUP.items():
                if k in cl:
                    ticker = v
                    break
            if not ticker:
                # Use first meaningful word (min 4 chars) as ticker
                words = [w for w in cl.split() if len(w) >= 4]
                ticker = (words[0] if words else cl.split()[0]).upper()[:10]
            records.append({
                "ticker": ticker, "company": company,
                "region": "INDIA", "quarter": f"Q{qnum} FY26",
                "eps": None, "pat_cr": pat_cr, "revenue_cr": None,
                "net_income_b": None, "revenue_b": None,
                "yoy_growth": yoy, "beat_miss": None, "guidance": None,
                "nim_pct": None, "confidence": "MED",
                "source": source,
            })
            continue

        # ── MoneyControl/generic: "Company Q4FY26 revenue ₹X crore, profit ₹Y crore" ──
        gen = re.search(
            r'([A-Z][A-Za-z0-9 &\.]{3,}?)\s+Q(\d)FY\d+.*?'
            r'revenue\s+(?:rises?\s+[\d.]+%\s+YoY\s+to\s+)?[₹]?([\d,]+\.?\d*)\s*crore',
            text, re.IGNORECASE
        )
        if gen and not mc:
            company = gen.group(1).strip()
            qnum    = gen.group(2)
            rev_cr  = float(gen.group(3).replace(",",""))
            pat_m2  = re.search(r'profit\s+(?:of\s+)?[₹]?([\d,]+\.?\d*)\s*crore', text, re.IGNORECASE)
            pat_cr  = float(pat_m2.group(1).replace(",","")) if pat_m2 else None
            yoy_m2  = re.search(r'([\d.]+)%\s*YoY', text, re.IGNORECASE)
            yoy     = float(yoy_m2.group(1)) if yoy_m2 else None
            cl = company.lower()
            ticker = next((v for k,v in INDIA_LOOKUP.items() if k in cl), cl.split()[0].upper()[:10])
            records.append({
                "ticker": ticker, "company": company,
                "region": "INDIA", "quarter": f"Q{qnum} FY26",
                "eps": None, "pat_cr": pat_cr, "revenue_cr": rev_cr,
                "net_income_b": None, "revenue_b": None,
                "yoy_growth": yoy, "beat_miss": None, "guidance": None,
                "nim_pct": None, "confidence": "MED",
                "source": source,
            })

    return records


# ── Ollama extraction (runs in background after fast parse) ──────────────────
_PROMPT_TEMPLATE = """You are a financial data extractor. Extract earnings from these messages.
For each company with ACTUAL reported numbers output a JSON object.
Fields: ticker,company,region,quarter,eps,pat_cr,revenue_cr,revenue_b,yoy_growth,beat_miss,guidance,nim_pct,confidence
Only return a JSON array, no text.

MESSAGES:
{messages}

JSON:"""


def _call_ollama(messages):
    """Ollama fallback — runs on messages not already parsed by regex."""
    if not messages:
        return []
    combined = "\n---\n".join(f"[{m['source']}]\n{m['text']}" for m in messages[:8])
    prompt = _PROMPT_TEMPLATE.format(messages=combined)
    try:
        r = requests.post(OLLAMA_URL, json={
            "model":   OLLAMA_MODEL,
            "prompt":  prompt,
            "stream":  False,
            "options": {"temperature": 0.05},
        }, timeout=120)
        if r.status_code != 200:
            return []
        raw = r.json().get("response", "")
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return []


# ── Cross-validation ──────────────────────────────────────────────────────────
def _merge_records(records):
    """
    Merge multiple records for the same ticker.
    Average numerical fields, promote confidence if 2+ sources.
    """
    by_ticker = {}
    for rec in records:
        t = (rec.get("ticker") or "").upper().strip()
        if not t:
            continue
        if t not in by_ticker:
            by_ticker[t] = []
        by_ticker[t].append(rec)

    merged = {}
    for ticker, recs in by_ticker.items():
        base = recs[0].copy()
        if len(recs) > 1:
            # Average numerical fields across sources
            for field in ["eps", "pat_cr", "revenue_cr", "net_income_b", "revenue_b", "yoy_growth", "nim_pct"]:
                vals = [r[field] for r in recs if r.get(field) is not None]
                if vals:
                    base[field] = round(sum(vals) / len(vals), 2)
            base["confidence"] = "HIGH"
            base["sources"]    = len(recs)
        else:
            base["sources"] = 1
        merged[ticker] = base

    return merged


# ── Public API ────────────────────────────────────────────────────────────────
def get_telegram_earnings(force_refresh=False):
    """
    Main entry point. Returns dict keyed by ticker symbol.
    Uses cache (10 min TTL). Pass force_refresh=True to bypass.
    """
    if not force_refresh:
        cached = _cache_get_all()
        if cached:
            return cached

    # Fetch all channels in parallel
    all_msgs = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_channel, n, u): n for n, u in CHANNELS.items()}
    for fut in futures:
        try:
            all_msgs.extend(fut.result(timeout=15))
        except Exception:
            pass

    if not all_msgs:
        return {}

    # Fast regex parse (instant, no LLM needed)
    records = _parse_fast(all_msgs)

    # SearXNG boost: search web for top stocks not yet captured from Telegram
    SEARXNG_LOOKUP = {
        "HDFCBANK": "HDFC Bank",   "ICICIBANK": "ICICI Bank",
        "TCS": "TCS Tata Consultancy", "INFY": "Infosys",
        "KOTAKBANK": "Kotak Bank", "AXISBANK": "Axis Bank",
        "WIPRO": "Wipro",          "HCLTECH": "HCL Technologies",
        "SBIN": "SBI State Bank",  "BAJFINANCE": "Bajaj Finance",
        "RELIANCE": "Reliance Industries", "ZOMATO": "Zomato Eternal",
        "TATAMOTORS": "Tata Motors", "SUNPHARMA": "Sun Pharma",
        "LTIM": "LTIMindtree",     "NESTLEIND": "Nestle India",
    }
    found_tickers = {r.get("ticker","") for r in records}
    if _check_searxng():
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(_searxng_earnings, sym, name): sym
                    for sym, name in SEARXNG_LOOKUP.items()
                    if sym not in found_tickers}
        for fut in futs:
            try:
                records.extend(fut.result(timeout=10))
            except Exception:
                pass

    # Ollama enrichment only when regex finds nothing at all
    if not records:
        ollama_extra = _call_ollama(all_msgs[:8])
        records.extend(ollama_extra)

    # Merge + validate
    merged = _merge_records(records)

    # Build final result + cache
    result = {}
    for ticker, data in merged.items():
        # Compute score from data quality
        score = 50
        yoy = data.get("yoy_growth")
        if yoy:
            if yoy > 20: score += 25
            elif yoy > 10: score += 15
            elif yoy > 0: score += 8
            elif yoy < -20: score -= 25
            elif yoy < -10: score -= 15
            else: score -= 8
        bm = (data.get("beat_miss") or "").upper()
        if bm == "BEAT":   score += 10
        if bm == "MISS":   score -= 10
        guide = (data.get("guidance") or "").upper()
        if guide == "RAISED":  score += 10
        if guide == "LOWERED": score -= 10
        score = max(0, min(100, score))
        data["score"] = score
        data["data_source"] = "LIVE-TG"
        result[ticker] = data
        _cache_set(ticker, data)

    return result


def build_earnings_row(ticker, tg_data, base_result):
    """
    Overlay Telegram live data onto a base yfinance result dict.
    base_result is modified in-place. Returns it.
    """
    if not tg_data:
        return base_result

    is_india = base_result.get("region") == "INDIA"

    # Quarter/date
    if tg_data.get("quarter"):
        base_result["earn_date"]    = tg_data["quarter"]

    # EPS
    if tg_data.get("eps") is not None:
        base_result["eps_act"]      = round(float(tg_data["eps"]), 2)

    # Revenue
    if is_india and tg_data.get("revenue_cr"):
        cr = tg_data["revenue_cr"]
        base_result["revenue"]      = f"₹{cr/1000:.1f}K Cr" if cr >= 1000 else f"₹{cr:.0f} Cr"
    elif tg_data.get("revenue_b"):
        base_result["revenue"]      = f"${tg_data['revenue_b']:.2f}B"

    # PAT / Net income → net margin proxy
    if is_india and tg_data.get("pat_cr"):
        cr = tg_data["pat_cr"]
        base_result["net_interest_income"] = f"₹{cr/1000:.1f}K Cr" if cr >= 1000 else f"₹{cr:.0f} Cr"
        if tg_data.get("revenue_cr") and cr:
            base_result["net_margin"] = round(cr / tg_data["revenue_cr"] * 100, 1)

    # YoY growth
    if tg_data.get("yoy_growth") is not None:
        base_result["eps_yoy"]      = tg_data["yoy_growth"]

    # NIM
    if tg_data.get("nim_pct") is not None:
        pass   # store as commentary hint

    # Beat/miss
    if tg_data.get("beat_miss"):
        base_result["beat_miss"] = tg_data["beat_miss"].upper()

    # Guidance
    if tg_data.get("guidance"):
        g = tg_data["guidance"].upper()
        base_result["guidance"]     = g

    # Score
    base_result["score"]        = tg_data.get("score", base_result.get("score", 50))

    # Commentary
    parts = []
    if tg_data.get("yoy_growth") is not None:
        yoy = tg_data["yoy_growth"]
        parts.append(f"PAT {'▲' if yoy>0 else '▼'}{abs(yoy):.1f}% YoY")
    if is_india and tg_data.get("pat_cr"):
        parts.append(f"PAT ₹{tg_data['pat_cr']:.0f}Cr")
    if tg_data.get("beat_miss"):
        bm = tg_data["beat_miss"].upper()
        parts.append(f"{'✓ BEAT' if bm=='BEAT' else '✗ MISS' if bm=='MISS' else '≈ INLINE'} est.")
    if tg_data.get("guidance"):
        parts.append(f"Guide: {tg_data['guidance']}")
    if tg_data.get("nim_pct"):
        parts.append(f"NIM {tg_data['nim_pct']}%")
    if tg_data.get("confidence") == "HIGH":
        parts.append(f"({tg_data.get('sources',2)} sources)")

    if parts:
        base_result["commentary"]   = " · ".join(parts)
    base_result["data_source"]  = "LIVE-TG"

    n = round(base_result["score"] / 20)
    base_result["stars"]        = "★"*n + "☆"*(5-n)

    return base_result


if __name__ == "__main__":
    print("Fetching Telegram earnings data...")
    t = time.time()
    data = get_telegram_earnings(force_refresh=True)
    print(f"Found {len(data)} companies in {time.time()-t:.1f}s\n")
    for ticker, d in data.items():
        eps = str(d.get('eps', '?') or '—')
        print(f"  {ticker:15} {str(d.get('quarter','?')):10} "
              f"EPS:{eps:<8} "
              f"PAT:{d.get('pat_cr','?')} Cr  "
              f"Beat:{d.get('beat_miss','?')}  "
              f"[conf:{d.get('confidence','?')}]")
