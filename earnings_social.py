"""
Earnings Social Scraper
Pulls live earnings commentary from Telegram channels + Reddit.
Extracts structured metrics: PAT, NIM, GNPA, NNPA, Provisions, EPS, Revenue, Outlook.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketBot/1.0)"}
TIMEOUT = 8

# ── Telegram channels with earnings commentary ───────────────
TELEGRAM_CHANNELS = [
    # Rich earnings detail: PAT, NIM, GNPA, provisions, commentary
    ("DreamCatcher",   "https://t.me/s/thedreamcatcher0"),
    ("EarnWhispers",   "https://t.me/s/EarningsWhispers"),
    ("UnusualWhales",  "https://t.me/s/unusual_whales"),
    ("FinancialJuice", "https://t.me/s/financialjuice"),
    ("WalterBB",       "https://t.me/s/WalterBloomberg"),
    ("ZeroHedge",      "https://t.me/s/zerohedge"),
    ("MarketCurrents", "https://t.me/s/MarketCurrents"),
    ("ForexLive",      "https://t.me/s/ForexLive"),
]

# ── Reddit subreddits (public JSON API, no key needed) ────────
REDDIT_SUBS = [
    ("r/IndiaInvest",   "https://www.reddit.com/r/IndiaInvestments/new.json?limit=25"),
    ("r/IndianStock",   "https://www.reddit.com/r/IndianStockMarket/new.json?limit=25"),
    ("r/investing",     "https://www.reddit.com/r/investing/new.json?limit=25"),
    ("r/stocks",        "https://www.reddit.com/r/stocks/new.json?limit=25"),
    ("r/wallstreetbets","https://www.reddit.com/r/wallstreetbets/search.json?q=earnings+results&sort=new&limit=20"),
    ("r/SecurityAnalys","https://www.reddit.com/r/SecurityAnalysis/new.json?limit=20"),
]

# ── Earnings keyword triggers ─────────────────────────────────
EARN_KEYWORDS = [
    "earnings", "results", "q1 fy", "q2 fy", "q3 fy", "q4 fy",
    "quarterly", "annual results", "pat", "nim", "gnpa", "nnpa",
    "net profit", "revenue", "eps ", "beat", "miss", "guidance",
    "fy25", "fy26", "q1 2025", "q2 2025", "q3 2025", "q4 2025",
    "q1 2026", "q2 2026", "q3 2026", "q4 2026",
]

# ── Stock symbol → search terms ───────────────────────────────
STOCK_ALIASES = {
    "HDFCBANK.NS": ["hdfc bank", "hdfcbank"],
    "ICICIBANK.NS": ["icici bank", "icicibank"],
    "KOTAKBANK.NS": ["kotak bank", "kotakbank", "kotak mahindra bank"],
    "AXISBANK.NS":  ["axis bank", "axisbank"],
    "SBIN.NS":      ["state bank of india", "sbin", " sbi "],
    "INDUSINDBK.NS":["indusind bank", "indusindbk"],
    "BANDHANBNK.NS":["bandhan bank", "bandhanbnk"],
    "TCS.NS":       ["tata consultancy", " tcs "],
    "INFY.NS":      ["infosys", " infy "],
    "WIPRO.NS":     ["wipro"],
    "HCLTECH.NS":   ["hcl tech", "hcltech", "hcl technologies"],
    "TECHM.NS":     ["tech mahindra", "techm"],
    "RELIANCE.NS":  ["reliance industries", "reliance jio", "mukesh ambani", " ril "],
    "BAJFINANCE.NS":["bajaj finance", "bajfinance"],
    "SUNPHARMA.NS": ["sun pharma", "sunpharma"],
    "MARUTI.NS":    ["maruti suzuki", "maruti"],
    "HINDUNILVR.NS":["hindustan unilever", " hul "],
    "LT.NS":        ["larsen & toubro", "l&t limited", "l&t india"],
    "ADANIPORTS.NS":["adani ports", "adaniports"],
    "AAPL":  ["apple inc", "$aapl", " aapl "],
    "MSFT":  ["microsoft", "$msft", " msft "],
    "NVDA":  ["nvidia", "$nvda", " nvda "],
    "GOOGL": ["alphabet", "google llc", "$googl"],
    "AMZN":  ["amazon.com", "$amzn", " amzn "],
    "META":  ["meta platforms", "$meta "],
    "TSLA":  ["tesla inc", "$tsla", " tsla "],
    "JPM":   ["jpmorgan chase", "jp morgan", "$jpm"],
    "GS":    ["goldman sachs", "$gs "],
    "BAC":   ["bank of america", "bofa", "$bac"],
    "MS":    ["morgan stanley", "$ms "],
    "WFC":   ["wells fargo", "$wfc"],
    "V":     ["visa inc", "$v "],
    "MA":    ["mastercard", "$ma "],
    "NFLX":  ["netflix", "$nflx"],
    "SAP":   ["sap se", "sap ag", "sap software"],
    "SIEGY": ["siemens"],
    "TM":    ["toyota motor", "$tm "],
    "SONY":  ["sony group", "sony corporation"],
    "MUFG":  ["mitsubishi ufj", "mufg"],
}

# ── Metric extractors ─────────────────────────────────────────
def _extract_metrics(text):
    """Extract structured financial metrics from free text."""
    t = text.lower()
    metrics = {}

    # PAT / Net Profit
    for pat in [
        r'pat[:\s]+(?:rs\.?|₹|inr)?\s*([\d,\.]+)\s*(cr|crore|bn|billion|m|million)',
        r'net profit[:\s]+(?:rs\.?|₹|inr)?\s*([\d,\.]+)\s*(cr|crore|bn|billion)',
        r'profit after tax[:\s]+(?:rs\.?|₹)?\s*([\d,\.]+)\s*(cr|crore)',
    ]:
        m = re.search(pat, t)
        if m:
            metrics['pat'] = f"₹{m.group(1)}{m.group(2).upper()}"
            break

    # PAT growth YoY
    for pat in [
        r'pat.*?([+-]?[\d\.]+)%\s*(?:yoy|y-o-y|year)',
        r'net profit.*?([+-]?[\d\.]+)%\s*(?:yoy|year)',
        r'([+-]?[\d\.]+)%\s*yoy.*?pat',
    ]:
        m = re.search(pat, t)
        if m:
            try:
                metrics['pat_yoy'] = float(m.group(1))
            except:
                pass
            break

    # NIM
    m = re.search(r'nim[:\s]+(\d+\.?\d*)%', t)
    if m:
        metrics['nim'] = float(m.group(1))

    # NIM BPS change
    for pat in [r'nim.*?([+-]?\d+)\s*bps', r'([+-]?\d+)\s*bps.*?nim']:
        m = re.search(pat, t)
        if m:
            try:
                metrics['nim_bps'] = int(m.group(1))
            except:
                pass
            break

    # GNPA
    m = re.search(r'gnpa[:\s]+(\d+\.?\d*)%', t)
    if m:
        metrics['gnpa'] = float(m.group(1))

    # NNPA
    m = re.search(r'nnpa[:\s]+(\d+\.?\d*)%', t)
    if m:
        metrics['nnpa'] = float(m.group(1))

    # EPS
    for pat in [
        r'eps[:\s]+\$?([\d\.]+)\s*vs\.?\s*\$?([\d\.]+)',
        r'eps[:\s]+\$?([\d\.]+)',
    ]:
        m = re.search(pat, t)
        if m:
            metrics['eps_act'] = float(m.group(1))
            if len(m.groups()) > 1 and m.group(2):
                metrics['eps_est'] = float(m.group(2))
            break

    # Revenue
    for pat in [
        r'revenue[:\s]+\$?([\d,\.]+)\s*(b|bn|billion|m|mn|million|cr|crore)',
        r'rev[:\s]+\$?([\d,\.]+)\s*(b|bn|billion|m|mn|million|cr|crore)',
    ]:
        m = re.search(pat, t)
        if m:
            metrics['revenue'] = f"${m.group(1)}{m.group(2).upper()}"
            break

    # Beat/Miss signal
    if any(w in t for w in ['beat', 'topped', 'surpassed', 'above estimate']):
        metrics['beat'] = True
    if any(w in t for w in ['miss', 'below estimate', 'fell short', 'disappointed']):
        metrics['miss'] = True

    # Guidance
    if any(w in t for w in ['raised guidance', 'guidance raised', 'raised outlook', 'upgraded outlook']):
        metrics['guidance'] = 'raised'
    elif any(w in t for w in ['lowered guidance', 'guidance cut', 'lowered outlook', 'cut outlook', 'below guidance']):
        metrics['guidance'] = 'lowered'
    elif any(w in t for w in ['maintained guidance', 'reaffirmed', 'in line']):
        metrics['guidance'] = 'maintained'

    # Sentiment
    pos = sum(t.count(w) for w in ['strong', 'beat', 'robust', 'outperform', 'positive', 'growth', 'improve', 'raised'])
    neg = sum(t.count(w) for w in ['miss', 'weak', 'decline', 'fall', 'lower', 'concern', 'pressure', 'disappoint'])
    if pos > neg + 1:
        metrics['sentiment'] = 'POSITIVE'
    elif neg > pos + 1:
        metrics['sentiment'] = 'NEGATIVE'
    else:
        metrics['sentiment'] = 'NEUTRAL'

    return metrics


def _match_symbol(text):
    """Return list of stock symbols mentioned in text."""
    t = text.lower()
    matched = []
    for sym, aliases in STOCK_ALIASES.items():
        if any(a in t for a in aliases):
            matched.append(sym)
    return matched


def _is_earnings_post(text):
    """Require at least 2 earnings signals to reduce noise."""
    t = text.lower()
    hits = sum(1 for k in EARN_KEYWORDS if k in t)
    # Also require financial number presence (e.g. ₹, $, %, Cr, billion)
    has_numbers = bool(re.search(r'[\$₹]\s*[\d,]+|[\d,]+\s*(cr|crore|bn|billion|million|%)', t))
    return hits >= 2 or (hits >= 1 and has_numbers)


# ── Scrapers ──────────────────────────────────────────────────
def _scrape_telegram(source, url):
    items = []
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        messages = soup.select(".tgme_widget_message")[-20:]
        now_utc = datetime.now(timezone.utc)
        cutoff  = now_utc - timedelta(hours=48)

        for msg in messages:
            txt_el = msg.select_one(".tgme_widget_message_text")
            if not txt_el:
                continue
            text = txt_el.get_text(" ", strip=True)
            if not _is_earnings_post(text):
                continue

            # Parse time
            time_el = msg.select_one("time")
            msg_time = ""
            try:
                dt = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
                if dt < cutoff:
                    continue
                msg_time = dt.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%H:%M IST")
            except:
                pass

            # Message link
            link_el = msg.select_one(".tgme_widget_message_date")
            link = link_el["href"] if link_el and link_el.get("href") else url

            symbols  = _match_symbol(text)
            metrics  = _extract_metrics(text)
            snippet  = text[:300].replace("\n", " ")

            items.append({
                "source":   source,
                "platform": "telegram",
                "time":     msg_time,
                "text":     snippet,
                "link":     link,
                "symbols":  symbols,
                "metrics":  metrics,
                "sentiment":metrics.get("sentiment", "NEUTRAL"),
            })
    except Exception:
        pass
    return items


def _scrape_reddit(source, url):
    items = []
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; EarningsBot/1.0)"})
        if r.status_code != 200:
            return items
        data  = r.json()
        posts = data.get("data", {}).get("children", [])
        now   = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=72)

        for post in posts:
            d = post.get("data", {})
            title   = d.get("title", "")
            selftext = d.get("selftext", "")
            text    = title + " " + selftext
            if not _is_earnings_post(text):
                continue

            created = d.get("created_utc", 0)
            dt = datetime.fromtimestamp(created, tz=timezone.utc)
            if dt < cutoff:
                continue
            msg_time = dt.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%H:%M IST")

            symbols  = _match_symbol(text)
            metrics  = _extract_metrics(text)
            score    = d.get("score", 0)
            comments = d.get("num_comments", 0)
            link     = f"https://reddit.com{d.get('permalink','')}"
            snippet  = (title + (" — " + selftext[:200] if selftext else "")).strip()

            items.append({
                "source":   source,
                "platform": "reddit",
                "time":     msg_time,
                "text":     snippet[:350],
                "link":     link,
                "symbols":  symbols,
                "metrics":  metrics,
                "sentiment":metrics.get("sentiment", "NEUTRAL"),
                "score":    score,
                "comments": comments,
            })
    except Exception:
        pass
    return items


# ── Main fetch ────────────────────────────────────────────────
def get_earnings_social():
    """Fetch earnings commentary from all social sources. Returns list sorted by time."""
    all_items = []
    tasks_tg  = [(src, url) for src, url in TELEGRAM_CHANNELS]
    tasks_rd  = [(src, url) for src, url in REDDIT_SUBS]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs_tg = {pool.submit(_scrape_telegram, s, u): s for s, u in tasks_tg}
        futs_rd = {pool.submit(_scrape_reddit,   s, u): s for s, u in tasks_rd}

        for fut in as_completed({**futs_tg, **futs_rd}, timeout=25):
            try:
                result = fut.result(timeout=2)
                all_items.extend(result)
            except Exception:
                pass

    # Sort: posts with matched symbols first, then by time
    def _sort_key(item):
        sym_bonus = 10 if item.get("symbols") else 0
        metric_bonus = len(item.get("metrics", {})) * 2
        return -(sym_bonus + metric_bonus)

    all_items.sort(key=_sort_key)
    return all_items[:80]


def get_earnings_by_symbol(symbol):
    """Get all social posts mentioning a specific symbol."""
    all_items = get_earnings_social()
    return [i for i in all_items if symbol in i.get("symbols", [])]


if __name__ == "__main__":
    items = get_earnings_social()
    print(f"Total earnings posts: {len(items)}")
    for item in items[:10]:
        syms = ",".join(item.get("symbols", [])) or "—"
        m    = item.get("metrics", {})
        print(f"\n[{item['platform'].upper()}] {item['source']} | {item['time']} | Symbols: {syms}")
        print(f"  Sentiment: {item['sentiment']} | Metrics: {list(m.keys())}")
        print(f"  {item['text'][:150]}")
