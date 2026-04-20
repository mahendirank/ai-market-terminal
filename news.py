import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram_news import get_telegram_news

FEED_TIMEOUT = 5   # seconds per RSS source

IST = timezone(timedelta(hours=5, minutes=30))
MAX_AGE_HOURS = 24


def _to_ist(dt):
    try:
        return dt.astimezone(IST).strftime("%H:%M IST")
    except:
        return ""


SOURCE_CATEGORY = {
    # Markets
    "Reuters Markets":    "MARKETS",
    "BBC Business":       "MARKETS",
    "MarketWatch":        "MARKETS",
    "CNBC Markets":       "MARKETS",
    "Yahoo Finance":      "MARKETS",
    "Investing.com":      "MARKETS",
    "Seeking Alpha":      "MARKETS",
    "Barrons":            "MARKETS",
    # FX
    "ForexLive":          "FX",
    "DailyFX":            "FX",
    "FXStreet":           "FX",
    # Geopolitics
    "Reuters World":      "GEOPOLITICS",
    "BBC World":          "GEOPOLITICS",
    "Sky News World":     "GEOPOLITICS",
    "Al Jazeera":         "GEOPOLITICS",
    "AP News":            "GEOPOLITICS",
    # Bonds / Macro
    "FT Markets":         "BONDS",
    "WSJ Markets":        "BONDS",
    "ZeroHedge":          "MACRO",
    "Bloomberg Econ":     "BONDS",
    "BondBuyer":          "BONDS",
    # HNI / Institutional
    "FinancialJuice":     "HNI",
    "WalterBloomberg":    "HNI",
    "DreamCatcher":       "HNI",
    "Unusual Whales":     "HNI",
    # Commodities
    "Kitco Gold":         "COMMODITIES",
    "Kitco Silver":       "COMMODITIES",
    "OilPrice.com":       "COMMODITIES",
    "Mining.com":         "COMMODITIES",
    "GoldSeek":           "COMMODITIES",
    "Natural Gas Intel":  "COMMODITIES",
    "Rigzone":            "COMMODITIES",
    "Metal Bulletin":     "COMMODITIES",
    # India
    "Economic Times":     "INDIA",
    "Livemint":           "INDIA",
    "MoneyControl":       "INDIA",
    "Business Standard":  "INDIA",
    "NDTV Profit":        "INDIA",
    "Hindu Business":     "INDIA",
    # Tech / Chips / Semi
    "The Register":       "TECH",
    "Ars Technica":       "TECH",
    "SemiWiki":           "TECH",
    "EE Times":           "TECH",
    "Tom's Hardware":     "TECH",
    "AnandTech":          "TECH",
    "SemiEngineering":    "TECH",
    "Digit":              "TECH",
}

RSS_SOURCES = {
    # ── Markets ─────────────────────────────────────────────
    "Reuters Markets":    "https://feeds.reuters.com/reuters/businessNews",
    "BBC Business":       "https://feeds.bbci.co.uk/news/business/rss.xml",
    "MarketWatch":        "https://feeds.marketwatch.com/marketwatch/topstories/",
    "CNBC Markets":       "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "Yahoo Finance":      "https://finance.yahoo.com/news/rssindex",
    "Investing.com":      "https://www.investing.com/rss/news.rss",
    "Seeking Alpha":      "https://seekingalpha.com/feed.xml",

    # ── FX / Currencies ─────────────────────────────────────
    "ForexLive":          "https://www.forexlive.com/feed/news",
    "FXStreet":           "https://www.fxstreet.com/rss/news",
    "DailyFX":            "https://www.dailyfx.com/feeds/all",

    # ── Geopolitics / War ───────────────────────────────────
    "Reuters World":      "https://feeds.reuters.com/Reuters/worldNews",
    "BBC World":          "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Sky News World":     "https://feeds.skynews.com/feeds/rss/world.xml",
    "Al Jazeera":         "https://www.aljazeera.com/xml/rss/all.xml",
    "AP News":            "https://rsshub.app/apnews/topics/business",

    # ── Bonds / Rates / Macro ───────────────────────────────
    "FT Markets":         "https://www.ft.com/rss/home/uk",
    "WSJ Markets":        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "ZeroHedge":          "https://nitter.net/zerohedge/rss",
    "BondBuyer":          "https://www.bondbuyer.com/feed",

    # ── HNI / Institutional ─────────────────────────────────
    "FinancialJuice":     "https://nitter.net/financialjuice/rss",
    "WalterBloomberg":    "https://nitter.net/WalterBloomberg/rss",
    "DreamCatcher":       "https://nitter.net/DreamCatcher/rss",
    "Unusual Whales":     "https://nitter.net/unusual_whales/rss",

    # ── Gold ────────────────────────────────────────────────
    "Kitco Gold":         "https://www.kitco.com/rss/news.xml",
    "GoldSeek":           "https://news.goldseek.com/goldseek/rss.php",

    # ── Silver ──────────────────────────────────────────────
    "Kitco Silver":       "https://www.kitco.com/rss/silver_news.xml",

    # ── Crude Oil / Energy ──────────────────────────────────
    "OilPrice.com":       "https://oilprice.com/rss/main",
    "Rigzone":            "https://www.rigzone.com/news/rss/rigzone_latest.aspx",

    # ── Copper / Metals / Mining ────────────────────────────
    "Mining.com":         "https://www.mining.com/feed/",

    # ── Natural Gas ─────────────────────────────────────────
    "Natural Gas Intel":  "https://naturalgasintel.com/feed/",

    # ── India ───────────────────────────────────────────────
    "Economic Times":     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Livemint":           "https://www.livemint.com/rss/markets",
    "MoneyControl":       "https://www.moneycontrol.com/rss/marketreports.xml",
    "Business Standard":  "https://www.business-standard.com/rss/markets-106.rss",
    "NDTV Profit":        "https://www.ndtvprofit.com/rss",
    "Hindu Business":     "https://www.thehindubusinessline.com/markets/feeder/default.rss",

    # ── Tech / Semiconductors / Chips ──────────────────────
    "The Register":       "https://www.theregister.com/headlines.atom",
    "Ars Technica":       "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "SemiWiki":           "https://semiwiki.com/feed/",
    "SemiEngineering":    "https://semiengineering.com/feed/",
    "EE Times":           "https://www.eetimes.com/rss/",
    "Tom's Hardware":     "https://www.tomshardware.com/feeds/all",
}


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

def _fetch_one(source, url):
    """Fetch a single RSS source using requests (hard timeout) then parse."""
    items  = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    try:
        resp = requests.get(url, timeout=FEED_TIMEOUT, headers=HEADERS)
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:8]:
            try:
                title = entry.get("title", "").strip()
                if not title or len(title) > 400:
                    continue
                pub    = entry.get("published", "") or entry.get("updated", "")
                ts_ist = ""
                if pub:
                    try:
                        dt_utc = parsedate_to_datetime(pub).astimezone(timezone.utc)
                        if dt_utc < cutoff:
                            continue
                        ts_ist = _to_ist(dt_utc)
                    except:
                        pass
                cat = SOURCE_CATEGORY.get(source, "MARKETS")
                items.append({"text": title, "source": source, "time": ts_ist, "category": cat})
            except:
                pass
    except:
        pass
    return items


def get_rss_news(allowed_sources=None):
    """Fetch all RSS sources in parallel with hard per-source timeout."""
    sources = {k: v for k, v in RSS_SOURCES.items()
               if allowed_sources is None or k in allowed_sources}
    all_items = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(_fetch_one, src, url): src
                   for src, url in sources.items()}
        for fut in as_completed(futures, timeout=18):
            try:
                all_items.extend(fut.result())
            except:
                pass
    return all_items


def get_all_news():
    news = []

    try:
        tg = get_telegram_news()
        for item in tg:
            if isinstance(item, dict) and "category" not in item:
                item["category"] = "HNI"
            news.append(item)
    except:
        pass

    try:
        news += get_rss_news()
    except:
        pass

    seen, unique = set(), []
    for n in news:
        key = (n["text"][:60].lower() if isinstance(n, dict) else n[:60].lower())
        if key not in seen:
            seen.add(key)
            unique.append(n)

    return unique[:100]


def format_news(news_list):
    lines = []
    for n in news_list:
        if isinstance(n, dict):
            lines.append(f"- [{n['source']} | {n.get('time','')}] {n['text']}")
        else:
            lines.append(f"- {n}")
    return "\n".join(lines)
