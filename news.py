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
        return dt.astimezone(IST).strftime("%H:%M IST")
    except:
        return ""


# Category mapping per source
SOURCE_CATEGORY = {
    "Reuters Markets":    "MARKETS",
    "BBC Business":       "MARKETS",
    "MarketWatch":        "MARKETS",
    "CNBC Markets":       "MARKETS",
    "Yahoo Finance":      "MARKETS",
    "Investing.com":      "MARKETS",
    "ForexLive":          "FX",
    "Reuters World":      "GEOPOLITICS",
    "BBC World":          "GEOPOLITICS",
    "Sky News World":     "GEOPOLITICS",
    "Al Jazeera":         "GEOPOLITICS",
    "FT Markets":         "BONDS",
    "WSJ Markets":        "BONDS",
    "ZeroHedge":          "MACRO",
    "FinancialJuice":     "HNI",
    "WalterBloomberg":    "HNI",
    "DreamCatcher":       "HNI",
    "Kitco Gold":         "COMMODITIES",
    "OilPrice.com":       "COMMODITIES",
    "Economic Times":     "INDIA",
    "Livemint":           "INDIA",
    "MoneyControl":       "INDIA",
    "The Register":       "TECH",
    "Ars Technica":       "TECH",
}

RSS_SOURCES = {
    # ── Markets ─────────────────────────────────────────────
    "Reuters Markets":    "https://feeds.reuters.com/reuters/businessNews",
    "BBC Business":       "https://feeds.bbci.co.uk/news/business/rss.xml",
    "MarketWatch":        "https://feeds.marketwatch.com/marketwatch/topstories/",
    "CNBC Markets":       "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "Yahoo Finance":      "https://finance.yahoo.com/news/rssindex",
    "Investing.com":      "https://www.investing.com/rss/news.rss",

    # ── FX / Currencies ─────────────────────────────────────
    "ForexLive":          "https://www.forexlive.com/feed/news",

    # ── Geopolitics / War ───────────────────────────────────
    "Reuters World":      "https://feeds.reuters.com/Reuters/worldNews",
    "BBC World":          "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Sky News World":     "https://feeds.skynews.com/feeds/rss/world.xml",
    "Al Jazeera":         "https://www.aljazeera.com/xml/rss/all.xml",

    # ── Bonds / Macro ───────────────────────────────────────
    "FT Markets":         "https://www.ft.com/rss/home/uk",
    "WSJ Markets":        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "ZeroHedge":          "https://nitter.net/zerohedge/rss",

    # ── HNI / Institutional ─────────────────────────────────
    "FinancialJuice":     "https://nitter.net/financialjuice/rss",
    "WalterBloomberg":    "https://nitter.net/WalterBloomberg/rss",
    "DreamCatcher":       "https://nitter.net/DreamCatcher/rss",

    # ── Commodities ─────────────────────────────────────────
    "Kitco Gold":         "https://www.kitco.com/rss/news.xml",
    "OilPrice.com":       "https://oilprice.com/rss/main",

    # ── India ───────────────────────────────────────────────
    "Economic Times":     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Livemint":           "https://www.livemint.com/rss/markets",
    "MoneyControl":       "https://www.moneycontrol.com/rss/marketreports.xml",

    # ── Tech / Semiconductors ───────────────────────────────
    "The Register":       "https://www.theregister.com/headlines.atom",
    "Ars Technica":       "https://feeds.arstechnica.com/arstechnica/technology-lab",
}


def get_rss_news():
    news   = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)

    for source, url in RSS_SOURCES.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
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
                            ts_ist = ""
                    else:
                        ts_ist = ""
                    cat = SOURCE_CATEGORY.get(source, "MARKETS")
                    news.append({"text": title, "source": source, "time": ts_ist, "category": cat})
                except:
                    t = entry.get("title", "")
                    if t:
                        cat = SOURCE_CATEGORY.get(source, "MARKETS")
                        news.append({"text": t, "source": source, "time": "", "category": cat})
        except:
            pass

    return news


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

    # Deduplicate
    seen   = set()
    unique = []
    for n in news:
        key = (n["text"][:60].lower() if isinstance(n, dict) else n[:60].lower())
        if key not in seen:
            seen.add(key)
            unique.append(n)

    return unique[:80]


def format_news(news_list):
    lines = []
    for n in news_list:
        if isinstance(n, dict):
            lines.append(f"- [{n['source']} | {n.get('time','')}] {n['text']}")
        else:
            lines.append(f"- {n}")
    return "\n".join(lines)
