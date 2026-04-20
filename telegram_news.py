import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

BOT_TOKEN = "YOUR_BOT_TOKEN"
IST = timezone(timedelta(hours=5, minutes=30))
MAX_AGE_HOURS = 24


CHANNELS = {
    "WalterBloomberg":  "https://t.me/s/WalterBloomberg",
    "DreamCatcher":     "https://t.me/s/thedreamcatcher0",
    "FinancialJuice":   "https://t.me/s/financialjuice",
    "ForexLive":        "https://t.me/s/forexlive",
    "ZeroHedge":        "https://t.me/s/zerohedge",
    "MarketCurrents":   "https://t.me/s/marketcurrents",
    "GoldTelegraph":    "https://t.me/s/GoldTelegraph",
    "BusinessInsider":  "https://t.me/s/Business_Insider",
    "BBCWorld":         "https://t.me/s/bbcnewsworld",
    "OilPrice":         "https://t.me/s/oilpricedotcom",
    "KitcoNews":        "https://t.me/s/KitcoNews",
}


def _to_ist(dt_utc):
    return dt_utc.astimezone(IST).strftime("%d-%b %I:%M%p IST")


# 🔹 Use public Telegram channel via HTML (no API needed)
def get_telegram_news():
    news    = []
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)

    for source, url in CHANNELS.items():
        try:
            res  = requests.get(url, timeout=8)
            soup = BeautifulSoup(res.text, "html.parser")
            msgs = soup.select(".tgme_widget_message")[-4:]

            for msg in msgs:
                try:
                    # Text
                    txt_el = msg.select_one(".tgme_widget_message_text")
                    if not txt_el:
                        continue
                    text = txt_el.get_text(" ", strip=True)
                    if not text or len(text) > 300:
                        continue

                    # Timestamp
                    time_el = msg.select_one("time")
                    ts_str  = time_el["datetime"] if time_el else None
                    if ts_str:
                        dt_utc = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if dt_utc < cutoff:
                            continue
                        ts_ist = _to_ist(dt_utc)
                    else:
                        ts_ist = "unknown time"

                    news.append({
                        "text":   text,
                        "source": source,
                        "time":   ts_ist,
                    })
                except:
                    pass
        except:
            pass

    return news[:20]


def format_telegram_news(news):
    if not news:
        return "No Telegram news available."
    lines = []
    for n in news:
        if isinstance(n, dict):
            lines.append(f"- [{n['source']} | {n['time']}] {n['text']}")
        else:
            lines.append(f"- {n}")
    return "\n".join(lines)


# 🔹 Send trade alert via Telegram bot
def send_trade_alert(chat_id, message):
    if BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("⚠️  Set BOT_TOKEN in telegram_news.py before sending alerts")
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
    msg = (
        f"*XAUUSD — {emoji}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 Entry : `{entry}`\n"
        f"🛑 SL    : `{sl}`\n"
        f"🎯 TP    : `{tp}`\n"
        f"━━━━━━━━━━━━━━━\n"
    )
    if reason:
        msg += f"📝 _{reason}_"
    return msg


if __name__ == "__main__":
    print("📡 Fetching Telegram channel news...\n")
    news = get_telegram_news()

    if news:
        print(f"✅ Found {len(news)} items:\n")
        for n in news:
            print(n)
    else:
        print("⚠️  No news found (channels may be blocking scraping)")

    print("\n--- Alert format test ---")
    print(format_alert("BUY", 3285.50, 3270.00, 3315.00,
                       "EMA crossover + bullish structure break"))
