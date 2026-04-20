import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from telegram_news import get_telegram_news

IST = timezone(timedelta(hours=5, minutes=30))
MAX_AGE_HOURS = 24


def _to_ist(dt):
    try:
        return dt.astimezone(IST).strftime("%d-%b %I:%M%p IST")
    except:
        return "unknown time"


RSS_SOURCES = {
    # ── Global Markets ──────────────────────────────────────
    "Reuters Markets":    "https://feeds.reuters.com/reuters/businessNews",
    "BBC Business":       "https://feeds.bbci.co.uk/news/business/rss.xml",
    "MarketWatch":        "https://feeds.marketwatch.com/marketwatch/topstories/",
    "CNBC Markets":       "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "Yahoo Finance":      "https://finance.yahoo.com/news/rssindex",
    "Investing.com":      "https://www.investing.com/rss/news.rss",
    "ForexLive":          "https://www.forexlive.com/feed/news",

    # ── Geopolitics / War / Politics ───────────────────────
    "Reuters World":      "https://feeds.reuters.com/Reuters/worldNews",
    "BBC World":          "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Sky News World":     "https://feeds.skynews.com/feeds/rss/world.xml",
    "Al Jazeera":         "https://www.aljazeera.com/xml/rss/all.xml",

    # ── Fed / Economy / Central Banks ─────────────────────
    "FT Markets":         "https://www.ft.com/rss/home/uk",
    "WSJ Markets":        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "ZeroHedge":          "https://nitter.net/zerohedge/rss",
    "FinancialJuice":     "https://nitter.net/financialjuice/rss",

    # ── Commodities / Gold / Oil ───────────────────────────
    "Kitco Gold":         "https://www.kitco.com/rss/news.xml",
    "OilPrice.com":       "https://oilprice.com/rss/main",

    # ── India Markets ──────────────────────────────────────
    "Economic Times":     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Livemint":           "https://www.livemint.com/rss/markets",
    "MoneyControl":       "https://www.moneycontrol.com/rss/marketreports.xml",

    # ── Sectors: Tech / Chips / Semi ──────────────────────
    "The Register":       "https://www.theregister.com/headlines.atom",
    "Ars Technica":       "https://feeds.arstechnica.com/arstechnica/technology-lab",

    # ── Banking / Finance ──────────────────────────────────
    "Bloomberg Law":      "https://nitter.net/WalterBloomberg/rss",
    "HNI Watch":          "https://nitter.net/DreamCatcher/rss",
}


def get_rss_news():
    news   = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)

    for source, url in RSS_SOURCES.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:6]:
                try:
                    title = entry.get("title", "").strip()
                    if not title or len(title) > 400:
                        continue
                    pub = entry.get("published", "") or entry.get("updated", "")
                    if pub:
                        try:
                            dt_utc = parsedate_to_datetime(pub).astimezone(timezone.utc)
                            if dt_utc < cutoff:
                                continue
                            ts_ist = _to_ist(dt_utc)
                        except:
                            ts_ist = "unknown time"
                    else:
                        ts_ist = "unknown time"
                    news.append({"text": title, "source": source, "time": ts_ist})
                except:
                    t = entry.get("title", "")
                    if t:
                        news.append({"text": t, "source": source, "time": "unknown time"})
        except:
            pass

    return news


def get_all_news():
    news = []

    try:
        news += get_telegram_news()
    except:
        pass

    try:
        news += get_rss_news()
    except:
        pass

    # deduplicate by headline
    seen = set()
    unique = []
    for n in news:
        key = (n["text"][:60].lower() if isinstance(n, dict) else n[:60].lower())
        if key not in seen:
            seen.add(key)
            unique.append(n)

    return unique[:60]


def filter_news(news):
    """Keep only market-relevant news."""
    keywords = [
        "fed", "fomc", "rate", "inflation", "cpi", "gdp", "nfp", "unemployment",
        "war", "iran", "russia", "china", "ukraine", "israel", "conflict", "sanction",
        "oil", "gold", "dollar", "yield", "bond", "treasury", "debt",
        "recession", "growth", "economy", "fiscal", "monetary",
        "hni", "hedge fund", "goldman", "jp morgan", "blackrock", "morgan stanley",
        "nvidia", "semiconductor", "chip", "tsmc", "intel", "amd",
        "bank", "banking", "finance", "credit", "liquidity",
        "nifty", "sensex", "india", "rbi", "rupee",
        "ecb", "boj", "pboc", "BoE", "central bank",
        "earnings", "revenue", "profit", "outlook", "guidance",
        "trump", "election", "tariff", "trade", "geopolit",
        "tech", "ai", "artificial intelligence", "it service",
    ]

    def matches(item):
        text = item["text"].lower() if isinstance(item, dict) else item.lower()
        return any(k in text for k in keywords)

    return [n for n in news if matches(n)]


def format_news(news_list):
    lines = []
    for n in news_list:
        if isinstance(n, dict):
            lines.append(f"- [{n['source']} | {n['time']}] {n['text']}")
        else:
            lines.append(f"- {n}")
    return "\n".join(lines)
