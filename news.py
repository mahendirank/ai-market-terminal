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


# 🔹 1. Reuters / Bloomberg style (RSS)
def get_rss_news():
    sources = {
        "Reuters":       "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
        "BBC Business":  "https://feeds.bbci.co.uk/news/business/rss.xml",
        "Sky News":      "https://feeds.skynews.com/feeds/rss/world.xml",
        "FinancialJuice":"https://nitter.net/financialjuice/rss",
        "ZeroHedge":     "https://nitter.net/zerohedge/rss",
    }

    news    = []
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)

    for source, url in sources.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                try:
                    title = entry.get("title", "").strip()
                    if not title or len(title) > 300:
                        continue
                    pub = entry.get("published", "")
                    if pub:
                        dt_utc = parsedate_to_datetime(pub).astimezone(timezone.utc)
                        if dt_utc < cutoff:
                            continue
                        ts_ist = _to_ist(dt_utc)
                    else:
                        ts_ist = "unknown time"
                    news.append({"text": title, "source": source, "time": ts_ist})
                except:
                    news.append({"text": entry.get("title", ""), "source": source, "time": "unknown time"})
        except:
            pass

    return news


# 🔹 2. ForexFactory scraping
def get_forex_factory_news():
    url = "https://www.forexfactory.com/calendar"
    headers = {"User-Agent": "Mozilla/5.0"}

    res = requests.get(url, headers=headers)
    soup = BeautifulSoup(res.text, "html.parser")

    events = []

    rows = soup.select("tr.calendar__row")[:10]

    for row in rows:
        title = row.select_one(".calendar__event-title")

        if title:
            events.append(title.text.strip())

    return events


# 🔹 3. Twitter/X via Nitter RSS
def get_twitter_news():
    accounts = ["GoldTelegraph", "KitcoNews", "zerohedge", "federalreserve", "financialjuice"]
    news = []

    for username in accounts:
        try:
            url = f"https://nitter.net/{username}/rss"
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                news.append(f"[{username}] {entry.title}")
        except:
            pass

    return news


# 🔹 4. Combine all (Telegram first — fastest & most relevant)
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

    try:
        for e in get_forex_factory_news():
            news.append({"text": e, "source": "ForexFactory", "time": "today"})
    except:
        pass

    return filter_news(news)[:15]


# 🔹 5. Filter by gold-relevant keywords
def filter_news(news):
    keywords = [
        "fed", "inflation", "interest rate", "gdp",
        "unemployment", "war", "oil", "dollar",
        "yen", "euro", "bond", "yield",
        "hni", "hedge fund", "goldman", "jp morgan", "blackrock",
    ]

    def matches(item):
        text = item["text"].lower() if isinstance(item, dict) else item.lower()
        return any(k in text for k in keywords)

    return [n for n in news if matches(n)]


# 🔹 6. Format for AI (accepts dicts or plain strings)
def format_news(news_list):
    lines = []
    for n in news_list:
        if isinstance(n, dict):
            lines.append(f"- [{n['source']} | {n['time']}] {n['text']}")
        else:
            lines.append(f"- {n}")
    return "\n".join(lines)
