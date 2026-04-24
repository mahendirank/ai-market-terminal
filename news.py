import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from telegram_news import get_telegram_news

FEED_TIMEOUT  = 7
IST           = timezone(timedelta(hours=5, minutes=30))
MAX_AGE_HOURS = 8
MAX_ITEMS_PER_SOURCE = 20
MAX_TOTAL     = 800

SOURCE_CATEGORY = {
    # Markets
    "Reuters Top":      "MARKETS",
    "Reuters Finance":  "MARKETS",
    "BBC Business":     "MARKETS",
    "MarketWatch":      "MARKETS",
    "CNBC Markets":     "MARKETS",
    "Yahoo Finance":    "MARKETS",
    "Benzinga":         "MARKETS",
    "Motley Fool":      "MARKETS",
    "Seeking Alpha":    "MARKETS",
    "SA Market":        "MARKETS",
    "SA Analysis":      "MARKETS",
    "SA Earnings":      "EARNINGS",
    "Bloomberg Mkts":   "MARKETS",
    "WSJ Mkt":          "MARKETS",
    "Valuewalk":        "MARKETS",
    # FX
    "ForexLive":        "FX",
    "DailyFX":          "FX",
    "FXStreet":         "FX",
    # Geopolitics
    "BBC World":        "GEOPOLITICS",
    "PBS NewsHour":     "GEOPOLITICS",
    "Al Jazeera":       "GEOPOLITICS",
    "AP News":          "GEOPOLITICS",
    "Foreign Policy":   "GEOPOLITICS",
    "Defense News":     "GEOPOLITICS",
    "Sky News World":   "GEOPOLITICS",
    # Bonds / Macro
    "FT Markets":       "BONDS",
    "ZeroHedge":        "MACRO",
    "BondBuyer":        "BONDS",
    "Calculated Risk":  "MACRO",
    "Mish Talk":        "MACRO",
    # HNI / Institutional
    "FinancialJuice":   "HNI",
    "WalterBloomberg":  "HNI",
    "DreamCatcher":     "HNI",
    "Unusual Whales":   "HNI",
    "MarketCurrents":   "HNI",
    "ZeroHedgeTG":      "HNI",
    # Commodities
    "Kitco News":       "COMMODITIES",
    "Hellenic Ship":    "COMMODITIES",
    "OilPrice.com":     "COMMODITIES",
    "Mining.com":       "COMMODITIES",
    "GoldSeek":         "COMMODITIES",
    "Natural Gas Intel":"COMMODITIES",
    "Rigzone":          "COMMODITIES",
    "GoldTelegraph":    "COMMODITIES",
    # India
    "Economic Times":   "INDIA",
    "ET Stocks":        "INDIA",
    "ET Global":        "INDIA",
    "Livemint":         "INDIA",
    "Mint Companies":   "INDIA",
    "Mint Economy":     "INDIA",
    "MoneyControl":     "INDIA",
    "MC Tech":          "INDIA",
    "Business Standard":"INDIA",
    "NDTV Profit":      "INDIA",
    "Hindu Business":   "INDIA",
    "The Hindu Biz":    "INDIA",
    "Finshots":         "INDIA",
    # Tech / AI / Chips
    "The Register":     "TECH",
    "Ars Technica":     "TECH",
    "SemiWiki":         "TECH",
    "SemiEngineering":  "TECH",
    "EE Times":         "TECH",
    "TechCrunch":       "TECH",
    "VentureBeat AI":   "TECH",
    "MIT Tech Rev":     "TECH",
    "Tom's Hardware":   "TECH",
    # Crypto
    "CoinDesk":         "CRYPTO",
    # Global / Asia
    "Nikkei Asia":      "GLOBAL",
    "Globe Mail":       "GLOBAL",
    # India fast
    "NDTV Business":    "INDIA",
    "Mint Opinion":     "INDIA",
    "Hindu Economy":    "INDIA",
    # Extra Markets
    "TheStreet":        "MARKETS",
    "Investing News":   "MARKETS",
    "Investing Stocks": "MARKETS",
    "FT Home":          "MARKETS",
    # Earnings wire
    "Globe Newswire":   "EARNINGS",
}

RSS_SOURCES = {
    # ── Core Markets ─────────────────────────────────────────
    "Reuters Top":      "https://feeds.reuters.com/reuters/topNews",
    "Reuters Finance":  "https://feeds.reuters.com/reuters/financialNews",
    "BBC Business":     "https://feeds.bbci.co.uk/news/business/rss.xml",
    "MarketWatch":      "https://feeds.marketwatch.com/marketwatch/topstories/",
    "CNBC Markets":     "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "Yahoo Finance":    "https://finance.yahoo.com/news/rssindex",
    "Benzinga":         "https://www.benzinga.com/feed",
    "Bloomberg Mkts":   "https://feeds.bloomberg.com/markets/news.rss",
    "WSJ Mkt":          "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
    "Motley Fool":      "https://www.fool.com/feeds/index.aspx",
    "Valuewalk":        "https://www.valuewalk.com/feed/",

    # ── Earnings-specific ────────────────────────────────────
    "SA Earnings":      "https://seekingalpha.com/tag/earnings/feed.xml",
    "SA Market":        "https://seekingalpha.com/market-outlook/rss.xml",
    "SA Analysis":      "https://seekingalpha.com/stock-ideas/rss.xml",

    # ── FX / Currencies ─────────────────────────────────────
    "ForexLive":        "https://www.forexlive.com/feed/news",
    "FXStreet":         "https://www.fxstreet.com/rss/news",
    "DailyFX":          "https://www.dailyfx.com/feeds/all",

    # ── Geopolitics / War ───────────────────────────────────
    "BBC World":        "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Sky News World":   "https://feeds.skynews.com/feeds/rss/world.xml",
    "Al Jazeera":       "https://www.aljazeera.com/xml/rss/all.xml",
    "AP News":          "https://rsshub.app/apnews/topics/business",
    "PBS NewsHour":     "https://www.pbs.org/newshour/feeds/rss/world",
    "Foreign Policy":   "https://foreignpolicy.com/feed/",
    "Defense News":     "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml",

    # ── Bonds / Rates / Macro ───────────────────────────────
    "FT Markets":       "https://www.ft.com/rss/home/uk",
    "ZeroHedge":        "https://nitter.net/zerohedge/rss",
    "BondBuyer":        "https://www.bondbuyer.com/feed",
    "Calculated Risk":  "https://www.calculatedriskblog.com/feeds/posts/default",
    "Mish Talk":        "https://mishtalk.com/feed",

    # ── HNI / Institutional ─────────────────────────────────
    "FinancialJuice":   "https://nitter.net/financialjuice/rss",
    "WalterBloomberg":  "https://nitter.net/WalterBloomberg/rss",
    "Unusual Whales":   "https://nitter.net/unusual_whales/rss",

    # ── Commodities ─────────────────────────────────────────
    "Kitco News":       "https://news.google.com/rss/search?q=site:kitco.com+gold+silver&hl=en-US&gl=US&ceid=US:en",
    "Hellenic Ship":    "https://www.hellenicshippingnews.com/feed/",
    "OilPrice.com":     "https://oilprice.com/rss/main",
    "Rigzone":          "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
    "Mining.com":       "https://www.mining.com/feed/",
    "Natural Gas Intel":"https://naturalgasintel.com/feed/",
    "GoldSeek":         "https://news.goldseek.com/goldseek/rss.php",

    # ── India — core ────────────────────────────────────────
    "Economic Times":   "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Stocks":        "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "ET Global":        "https://economictimes.indiatimes.com/news/international/rssfeeds/1715249553.cms",
    "Livemint":         "https://www.livemint.com/rss/markets",
    "Mint Companies":   "https://www.livemint.com/rss/companies",
    "Mint Economy":     "https://www.livemint.com/rss/economy",
    "MoneyControl":     "https://www.moneycontrol.com/rss/marketreports.xml",
    "MC Tech":          "https://www.moneycontrol.com/rss/technology.xml",
    "Business Standard":"https://www.business-standard.com/rss/markets-106.rss",
    "NDTV Profit":      "https://www.ndtvprofit.com/rss",
    "Hindu Business":   "https://www.thehindubusinessline.com/markets/feeder/default.rss",
    "The Hindu Biz":    "https://www.thehindu.com/business/feeder/default.rss",
    "Finshots":         "https://finshots.in/feed",

    # ── Tech / AI / Semiconductors ───────────────────────────
    "The Register":     "https://www.theregister.com/headlines.atom",
    "Ars Technica":     "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "SemiWiki":         "https://semiwiki.com/feed/",
    "SemiEngineering":  "https://semiengineering.com/feed/",
    "EE Times":         "https://www.eetimes.com/rss/",
    "TechCrunch":       "https://techcrunch.com/feed/",
    "VentureBeat AI":   "https://venturebeat.com/category/ai/feed/",
    "MIT Tech Rev":     "https://www.technologyreview.com/feed/",
    "Tom's Hardware":   "https://www.tomshardware.com/feeds/all",

    # ── Crypto ──────────────────────────────────────────────
    "CoinDesk":         "https://www.coindesk.com/arc/outboundfeeds/rss/",

    # ── Global / Asia ────────────────────────────────────────
    "Nikkei Asia":      "https://asia.nikkei.com/rss/feed/nar",

    # ── India Fast (verified working) ────────────────────────
    "NDTV Business":    "https://feeds.feedburner.com/ndtvprofit-latest",
    "Mint Opinion":     "https://www.livemint.com/rss/opinion",
    "Hindu Economy":    "https://www.thehindubusinessline.com/economy/feeder/default.rss",

    # ── More Markets (verified working) ──────────────────────
    "TheStreet":        "https://www.thestreet.com/.rss/full/",
    "Investing News":   "https://www.investing.com/rss/news.rss",
    "Investing Stocks": "https://www.investing.com/rss/stock_stock_picks.rss",
    "FT Home":          "https://www.ft.com/rss/home",

    # ── Earnings Wire (verified working) ─────────────────────
    "Globe Newswire":   "https://www.globenewswire.com/RssFeed/subjectcode/17-Earnings",
}

# ── Stock ticker detection ────────────────────────────────────
# Maps keyword → ticker for inline tagging on news items
TICKER_MAP = {
    # India
    "hdfc bank": "HDFCBANK", "hdfcbank": "HDFCBANK",
    "icici bank": "ICICIBANK", "icicibank": "ICICIBANK",
    "axis bank": "AXISBANK", "kotak bank": "KOTAKBANK",
    "state bank of india": "SBIN", " sbi ": "SBIN",
    "infosys": "INFY", "tata consultancy": "TCS", " tcs ": "TCS",
    "wipro": "WIPRO", "hcl tech": "HCLTECH",
    "reliance industries": "RELIANCE", " ril ": "RELIANCE",
    "bajaj finance": "BAJFIN", "sun pharma": "SUNPHARMA",
    "maruti suzuki": "MARUTI", "hindustan unilever": "HUL",
    "adani": "ADANI", "larsen & toubro": "L&T", "l&t ": "L&T",
    "tata motors": "TATAMOTORS", "bajaj auto": "BAJAJ-AUTO",
    "power grid": "POWERGRID", "ntpc": "NTPC",
    "tech mahindra": "TECHM", "indusind bank": "INDUSINDBK",
    # USA
    "apple inc": "AAPL", "apple iphone": "AAPL", "apple's": "AAPL", " aapl": "AAPL",
    "microsoft": "MSFT", " msft": "MSFT",
    "nvidia": "NVDA", " nvda": "NVDA",
    "alphabet": "GOOGL", "google llc": "GOOGL",
    "amazon.com": "AMZN", " amzn": "AMZN",
    "meta platforms": "META", "facebook": "META",
    "tesla inc": "TSLA", " tsla": "TSLA",
    "jpmorgan": "JPM", "jp morgan": "JPM",
    "goldman sachs": "GS",
    "bank of america": "BAC",
    "morgan stanley": "MS",
    "netflix": "NFLX", " nflx": "NFLX",
    "visa inc": "VISA",
    "mastercard": "MA",
    "walmart": "WMT",
    "berkshire": "BRK",
    "exxon mobil": "XOM", "chevron": "CVX",
    "johnson & johnson": "JNJ",
    "pfizer": "PFE",
    "eli lilly": "LLY",
    # Global
    "tsmc": "TSM", "taiwan semi": "TSM",
    "samsung": "SMSN",
    "toyota": "TM",
    "sony": "SONY",
    "siemens": "SIEGY",
    "sap se": "SAP", "sap ag": "SAP",
    "asml": "ASML",
    "shell": "SHEL",
    "hsbc": "HSBC",
    # Indices / macro
    "nifty": "NIFTY", "sensex": "SENSEX",
    "s&p 500": "SPX", "s&p500": "SPX",
    "nasdaq": "NDX",
    "dow jones": "DJIA",
    "dax": "DAX",
    "ftse": "FTSE",
    "nikkei": "NKY",
    # Commodities
    "gold": "GOLD", "silver": "SILVER",
    "crude oil": "OIL", "brent": "BRENT", "wti": "WTI",
    "natural gas": "NATGAS",
    "copper": "COPPER",
    "bitcoin": "BTC",
    # Macro
    "fed": "FED", "fomc": "FOMC",
    "rbi": "RBI",
    "ecb": "ECB",
    "boj": "BOJ",
}


def _detect_tickers(text):
    """Return list of up to 3 ticker tags found in headline."""
    t = " " + text.lower() + " "
    found = []
    seen  = set()
    for keyword, ticker in TICKER_MAP.items():
        if keyword in t and ticker not in seen:
            found.append(ticker)
            seen.add(ticker)
        if len(found) >= 3:
            break
    return found


def _to_ist(dt):
    try:
        return dt.astimezone(IST).strftime("%H:%M IST")
    except:
        return ""


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}


def _fetch_one(source, url):
    items  = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    try:
        resp = requests.get(url, timeout=FEED_TIMEOUT, headers=HEADERS)
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
            try:
                title = entry.get("title", "").strip()
                if not title or len(title) > 400:
                    continue
                pub     = entry.get("published", "") or entry.get("updated", "")
                ts_ist  = ""
                pub_utc = ""
                if pub:
                    try:
                        dt_utc  = parsedate_to_datetime(pub).astimezone(timezone.utc)
                        if dt_utc < cutoff:
                            continue
                        ts_ist  = _to_ist(dt_utc)
                        pub_utc = dt_utc.isoformat()
                    except:
                        pass
                cat     = SOURCE_CATEGORY.get(source, "MARKETS")
                tickers = _detect_tickers(title)
                item_url = entry.get("link", "") or entry.get("id", "")
                items.append({
                    "text":     title,
                    "source":   source,
                    "time":     ts_ist,
                    "pub_utc":  pub_utc,
                    "category": cat,
                    "tickers":  tickers,
                    "url":      item_url,
                })
            except:
                pass
    except:
        pass
    return items


def get_rss_news(allowed_sources=None):
    sources = {k: v for k, v in RSS_SOURCES.items()
               if allowed_sources is None or k in allowed_sources}
    all_items = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(_fetch_one, src, url)
                   for src, url in sources.items()]
    # executor context exit waits for all futures; each request has FEED_TIMEOUT=6s
    for fut in futures:
        try:
            all_items.extend(fut.result(timeout=0.01))
        except Exception:
            pass
    return all_items


def get_alpaca_news():
    """Free Alpaca markets news API — no key required, Reuters/Benzinga wire quality."""
    items = []
    try:
        url  = "https://data.alpaca.markets/v1beta1/news?limit=50&sort=desc"
        resp = requests.get(url, timeout=8, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return items
        data = resp.json().get("news", [])
        now  = datetime.now(timezone.utc)
        cut  = now - timedelta(hours=MAX_AGE_HOURS)
        for art in data:
            try:
                headline = (art.get("headline") or art.get("summary", "")).strip()
                if not headline or len(headline) > 400:
                    continue
                created  = art.get("created_at", "")
                pub_utc  = ""
                ts_ist   = ""
                if created:
                    dt_utc  = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if dt_utc < cut:
                        continue
                    pub_utc = dt_utc.isoformat()
                    ts_ist  = _to_ist(dt_utc)
                syms    = art.get("symbols", [])
                tickers = syms[:3] if syms else _detect_tickers(headline)
                items.append({
                    "text":     headline,
                    "source":   art.get("source", "Alpaca"),
                    "time":     ts_ist,
                    "pub_utc":  pub_utc,
                    "category": "MARKETS",
                    "tickers":  tickers,
                    "url":      art.get("url", ""),
                })
            except:
                pass
    except:
        pass
    return items


def _norm(text):
    """Normalise headline for dedup — lowercase, strip punctuation, collapse spaces."""
    t = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return re.sub(r"\s+", " ", t).strip()


def get_all_news():
    news = []

    try:
        tg = get_telegram_news()
        for item in tg:
            if isinstance(item, dict):
                if "category" not in item:
                    item["category"] = "HNI"
                if "tickers" not in item:
                    item["tickers"] = _detect_tickers(item.get("text", ""))
                if "pub_utc" not in item:
                    item["pub_utc"] = ""
            news.append(item)
    except:
        pass

    try:
        news += get_rss_news()
    except:
        pass

    try:
        news += get_alpaca_news()
    except:
        pass

    # Smart dedup: same story from multiple sources → keep first, merge source list
    norm_map = {}   # norm_key → index in unique
    unique   = []
    for n in news:
        if not isinstance(n, dict):
            continue
        key = _norm(n.get("text", ""))[:80]
        if not key:
            continue
        if key in norm_map:
            # Merge: append source name so UI can show "Reuters +2"
            existing = unique[norm_map[key]]
            prev_src = existing.get("source", "")
            new_src  = n.get("source", "")
            if new_src and new_src not in prev_src:
                existing["source"] = f"{prev_src} +1" if "+" not in prev_src else re.sub(r"\+\d+", lambda m: f"+{int(m.group()[1:])+1}", prev_src)
            # Keep highest-quality tickers
            if not existing.get("tickers") and n.get("tickers"):
                existing["tickers"] = n["tickers"]
        else:
            norm_map[key] = len(unique)
            unique.append(n)

    return unique[:MAX_TOTAL]


def format_news(news_list):
    lines = []
    for n in news_list:
        if isinstance(n, dict):
            lines.append(f"- [{n['source']} | {n.get('time','')}] {n['text']}")
        else:
            lines.append(f"- {n}")
    return "\n".join(lines)
