import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

BOT_TOKEN = "YOUR_BOT_TOKEN"
IST        = timezone(timedelta(hours=5, minutes=30))
MAX_AGE_HOURS = 24
TIMEOUT    = 6   # seconds per channel

CHANNELS = {
    # ── HNI / Bloomberg style ──────────────────────────────
    "WalterBloomberg":  "https://t.me/s/WalterBloomberg",
    "DreamCatcher":     "https://t.me/s/thedreamcatcher0",
    "FinancialJuice":   "https://t.me/s/financialjuice",
    "Unusual Whales":   "https://t.me/s/unusual_whales",
    # ── Markets / Macro ────────────────────────────────────
    "MarketCurrents":   "https://t.me/s/marketcurrents",
    "ZeroHedge":        "https://t.me/s/zerohedge",
    "ForexLive":        "https://t.me/s/forexlive",
    # ── Commodities ────────────────────────────────────────
    "GoldTelegraph":    "https://t.me/s/GoldTelegraph",
    "KitcoNews":        "https://t.me/s/KitcoNews",
    "OilPrice":         "https://t.me/s/oilpricedotcom",
    # ── India ──────────────────────────────────────────────
    "BusinessInsider":  "https://t.me/s/Business_Insider",
}

SOURCE_CATEGORY = {
    "WalterBloomberg": "HNI",
    "DreamCatcher":    "HNI",
    "FinancialJuice":  "HNI",
    "Unusual Whales":  "HNI",
    "MarketCurrents":  "MARKETS",
    "ZeroHedge":       "MACRO",
    "ForexLive":       "FX",
    "GoldTelegraph":   "COMMODITIES",
    "KitcoNews":       "COMMODITIES",
    "OilPrice":        "COMMODITIES",
    "BusinessInsider": "MARKETS",
}


def _to_ist(dt_utc):
    return dt_utc.astimezone(IST).strftime("%H:%M IST")


def _fetch_channel(source, url):
    """Fetch a single Telegram channel. Returns list of news items."""
    news   = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    try:
        res  = requests.get(url, timeout=TIMEOUT,
                            headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(res.text, "html.parser")
        msgs = soup.select(".tgme_widget_message")[-6:]
        for msg in msgs:
            try:
                txt_el = msg.select_one(".tgme_widget_message_text")
                if not txt_el:
                    continue
                text = txt_el.get_text(" ", strip=True)
                if not text or len(text) > 500:
                    continue
                time_el = msg.select_one("time")
                ts_str  = time_el["datetime"] if time_el else None
                if ts_str:
                    dt_utc = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if dt_utc < cutoff:
                        continue
                    ts_ist = _to_ist(dt_utc)
                else:
                    ts_ist = ""
                cat = SOURCE_CATEGORY.get(source, "MARKETS")
                msg_link = msg.select_one("a.tgme_widget_message_date")
                msg_url  = msg_link["href"] if msg_link and msg_link.get("href") else ""
                news.append({"text": text, "source": source, "time": ts_ist, "category": cat, "url": msg_url})
            except:
                pass
    except:
        pass
    return news


def get_telegram_news(allowed_sources=None):
    """Fetch all Telegram channels in parallel."""
    channels = {k: v for k, v in CHANNELS.items()
                if allowed_sources is None or k in allowed_sources}
    all_news = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_channel, src, url): src
                   for src, url in channels.items()}
        for fut in as_completed(futures, timeout=15):
            try:
                all_news.extend(fut.result())
            except:
                pass
    return all_news


# ── Telegram bot alert ──────────────────────────────────────
def send_trade_alert(chat_id, message):
    if BOT_TOKEN == "YOUR_BOT_TOKEN":
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        res = requests.post(url, data={"chat_id": chat_id, "text": message,
                                       "parse_mode": "Markdown"}, timeout=10)
        return res.status_code == 200
    except:
        return False


def format_alert(signal, entry, sl, tp, reason=""):
    emoji = "🟢 LONG" if signal.upper() == "BUY" else "🔴 SHORT"
    msg = (f"*XAUUSD — {emoji}*\n━━━━━━━━━━━━━━━\n"
           f"📍 Entry : `{entry}`\n🛑 SL    : `{sl}`\n🎯 TP    : `{tp}`\n━━━━━━━━━━━━━━━\n")
    if reason:
        msg += f"📝 _{reason}_"
    return msg


if __name__ == "__main__":
    import time
    t0   = time.time()
    news = get_telegram_news()
    print(f"Fetched {len(news)} items in {time.time()-t0:.1f}s")
    for n in news[:5]:
        print(f"  [{n['source']}] {n['text'][:80]}")
