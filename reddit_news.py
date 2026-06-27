"""
reddit_news.py — curated subreddit headlines via Reddit's free RSS.

Reddit is a SENTIMENT / early-signal layer, NOT authoritative news: items are
tagged tier "B" and category SENTIMENT/MARKETS/MACRO/INDIA, and the caller
(news._get_all_news_uncached) only surfaces ones cross-confirmed by a mainstream
headline. Reddit rate-limits aggressively (HTTP 429 on rapid/parallel requests),
so subreddits are fetched SERIALLY with a gap, and the whole set is cached for a
few minutes behind a non-blocking lock — a given news build reuses the cache
instead of re-paying the serial fetch.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time, threading, requests, feedparser
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

IST           = timezone(timedelta(hours=5, minutes=30))
MAX_AGE_HOURS = 12
TIMEOUT       = 6
MAX_ITEMS_PER_SUB = 15
REQUEST_GAP   = float(os.getenv("REDDIT_REQUEST_GAP", "2.5"))   # space requests to dodge 429
CACHE_TTL     = int(os.getenv("REDDIT_CACHE_TTL", "300"))       # serial fetch at most ~1/5min
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ZyvoraTerminal/1.0; +admin@zyvoratech.co)"}

# label -> (subreddit, category)
SUBREDDITS = {
    "r/wallstreetbets":   ("wallstreetbets",   "SENTIMENT"),
    "r/stocks":           ("stocks",           "MARKETS"),
    "r/StockMarket":      ("StockMarket",      "MARKETS"),
    "r/investing":        ("investing",        "MARKETS"),
    "r/economics":        ("economics",        "MACRO"),
    "r/options":          ("options",          "SENTIMENT"),
    "r/IndiaInvestments": ("IndiaInvestments", "INDIA"),
}

_cache = {"ts": 0.0, "items": []}
_lock  = threading.Lock()


def _to_ist(dt_utc):
    return dt_utc.astimezone(IST).strftime("%H:%M IST")


def _fetch_sub(label, sub, cat, cutoff):
    items = []
    url   = f"https://www.reddit.com/r/{sub}/hot/.rss"
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
        if resp.status_code != 200:        # 429 / transient — skip this round
            return items
        feed = feedparser.parse(resp.content)
        for e in feed.entries[:MAX_ITEMS_PER_SUB]:
            title = (e.get("title") or "").strip()
            if not title or len(title) > 300:
                continue
            pub = e.get("published", "") or e.get("updated", "")
            ts_ist = ""; pub_utc = ""
            if pub:
                try:
                    dt = parsedate_to_datetime(pub).astimezone(timezone.utc)
                    if dt < cutoff:
                        continue
                    ts_ist  = _to_ist(dt)
                    pub_utc = dt.isoformat()
                except Exception:
                    pass
            items.append({
                "text": title, "source": label, "time": ts_ist, "pub_utc": pub_utc,
                "category": cat, "tier": "B", "platform": "reddit",
                "url": e.get("link", ""), "confirmed": False,
            })
    except Exception:
        pass
    return items


def _fetch_all():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    out = []
    for i, (label, (sub, cat)) in enumerate(SUBREDDITS.items()):
        out.extend(_fetch_sub(label, sub, cat, cutoff))
        if i < len(SUBREDDITS) - 1:        # gap between requests (not after the last)
            time.sleep(REQUEST_GAP)
    return out


def _refresh_job():
    try:
        items = _fetch_all()
        if items:                          # keep good cache on a fully-429'd round
            _cache["items"] = items
        _cache["ts"] = time.time()         # back off either way (don't hammer Reddit)
    finally:
        _lock.release()


def get_reddit_news():
    """Non-blocking. Returns the cached sentiment-tier items immediately (possibly []
    on cold start) and refreshes in a background thread when stale — the serial,
    rate-limited fetch (up to ~tens of seconds) NEVER blocks the news build. Items are
    tier 'B', confirmed=False; the caller cross-confirms before surfacing them."""
    if (time.time() - _cache["ts"]) >= CACHE_TTL and _lock.acquire(blocking=False):
        threading.Thread(target=_refresh_job, daemon=True).start()
    return _cache["items"]
