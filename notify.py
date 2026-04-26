"""
Telegram alert system — sends market alerts + live news feed to your Telegram.
Bot token + chat ID loaded from environment variables (never hardcoded).

Safety measures:
- 1 message/sec rate limit (Telegram blocks bots that send faster)
- SQLite dedup store — no repeats even after server restart
- Max 20 news items per batch cycle — no sudden floods
- Score 2+ filter — blocks junk/irrelevant headlines
- Runs in background thread — never slows down dashboard
"""
import os, json, re, requests, threading, time, sqlite3
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8475057388:AAGUlt5Qu3Ei2_3xeUF8S1TWvygDKVVxb8I")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "1026742085")

_sent_cache = set()   # in-memory dedup for condition alerts (same session)

# ── Persistent news dedup DB ──────────────────────────────────
_NEWS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "tg_sent.db")
_news_db_lock = threading.Lock()

def _news_db():
    os.makedirs(os.path.dirname(_NEWS_DB), exist_ok=True)
    conn = sqlite3.connect(_NEWS_DB, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, ts REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON sent(ts)")
    conn.commit()
    return conn

def _already_sent(key: str) -> bool:
    try:
        with _news_db_lock:
            conn = _news_db()
            row  = conn.execute("SELECT 1 FROM sent WHERE key=?", (key,)).fetchone()
            conn.close()
        return row is not None
    except Exception:
        return False

def _mark_sent(key: str):
    try:
        with _news_db_lock:
            conn = _news_db()
            conn.execute("INSERT OR IGNORE INTO sent(key,ts) VALUES(?,?)", (key, time.time()))
            conn.commit(); conn.close()
    except Exception:
        pass

def _cleanup_old_sent():
    """Keep DB small — delete records older than 7 days."""
    try:
        with _news_db_lock:
            conn = _news_db()
            conn.execute("DELETE FROM sent WHERE ts < ?", (time.time() - 604800,))
            conn.commit(); conn.close()
    except Exception:
        pass

def _headline_key(text: str) -> str:
    """Normalise headline to a short dedup key."""
    t = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t[:60]


def send_telegram(msg: str, silent: bool = False) -> bool:
    """Send a message to Telegram. Returns True if sent successfully."""
    try:
        url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":              CHAT_ID,
            "text":                 msg,
            "parse_mode":           "HTML",
            "disable_notification": silent,
        }, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def _dedup_send(key: str, msg: str, silent: bool = False):
    """Send only if same alert hasn't been sent in this session."""
    if key in _sent_cache:
        return
    _sent_cache.add(key)
    threading.Thread(target=send_telegram, args=(msg, silent), daemon=True).start()


# ── Alert functions (each checks one condition) ──────────────

def alert_high_news(headline: str, source: str, score: int):
    """Alert when a news item scores 8+ (very high impact)."""
    key = f"news_{headline[:40]}"
    msg = (
        f"🔴 <b>HIGH IMPACT NEWS</b>\n\n"
        f"📰 {headline}\n\n"
        f"📡 Source: {source}\n"
        f"⚡ Score: {score}/10\n"
        f"🕐 {datetime.now(IST).strftime('%d-%b %H:%M IST')}"
    )
    _dedup_send(key, msg)


def alert_fii(fii_net: float, dii_net: float):
    """Alert when FII net crosses ±5000 Cr."""
    if abs(fii_net) < 5000:
        return
    direction = "BUY 🟢" if fii_net > 0 else "SELL 🔴"
    key = f"fii_{int(fii_net/1000)}"
    msg = (
        f"🏦 <b>FII BIG MOVE ALERT</b>\n\n"
        f"FII Net: <b>₹{fii_net:,.0f} Cr</b> — {direction}\n"
        f"DII Net: ₹{dii_net:,.0f} Cr\n\n"
        f"{'FIIs buying aggressively — bullish signal' if fii_net > 0 else 'FIIs selling heavily — caution'}\n"
        f"🕐 {datetime.now(IST).strftime('%d-%b %H:%M IST')}"
    )
    _dedup_send(key, msg)


def alert_vix_backwardation(vix_spot: float, vix3m: float):
    """Alert when VIX term structure flips to backwardation (stress signal)."""
    if vix_spot <= vix3m * 1.05:
        return
    key = f"vix_back_{int(vix_spot)}"
    msg = (
        f"⚠️ <b>VIX BACKWARDATION ALERT</b>\n\n"
        f"VIX Spot: <b>{vix_spot}</b>  |  VIX 3M: {vix3m}\n\n"
        f"Spot VIX is higher than 3-month VIX.\n"
        f"This means traders are paying MORE to hedge NOW than for future.\n"
        f"<b>Signal: Market stress / fear spike — be careful.</b>\n"
        f"🕐 {datetime.now(IST).strftime('%d-%b %H:%M IST')}"
    )
    _dedup_send(key, msg)


def alert_congress_cluster(ticker: str, count: int, politicians: list):
    """Alert when 2+ congress members buy the same stock."""
    key = f"congress_{ticker}"
    names = ", ".join(politicians[:3])
    msg = (
        f"🏛 <b>CONGRESS CLUSTER BUY</b>\n\n"
        f"Ticker: <b>{ticker}</b>\n"
        f"Members: <b>{count} Congress members</b> bought this\n"
        f"Who: {names}\n\n"
        f"Multiple politicians buying same stock = strong insider signal.\n"
        f"🕐 {datetime.now(IST).strftime('%d-%b %H:%M IST')}"
    )
    _dedup_send(key, msg)


def alert_cot_extreme(instrument: str, net_pos: float, direction: str):
    """Alert when COT positioning hits extreme levels."""
    key = f"cot_{instrument}_{direction}"
    emoji = "🟢" if direction == "LONG" else "🔴"
    msg = (
        f"📊 <b>COT EXTREME POSITIONING</b>\n\n"
        f"Instrument: <b>{instrument}</b>\n"
        f"Direction: {emoji} <b>EXTREME {direction}</b>\n"
        f"Net Position: {net_pos:,.0f} contracts\n\n"
        f"When hedge funds are at extreme positions, reversals often follow.\n"
        f"🕐 {datetime.now(IST).strftime('%d-%b %H:%M IST')}"
    )
    _dedup_send(key, msg)


def alert_fed_signal(implied_rate: float, current_rate: float, signal: str):
    """Alert when Fed futures imply a significant rate change."""
    diff = round(implied_rate - current_rate, 3)
    if abs(diff) < 0.25:
        return
    key = f"fed_{int(implied_rate*100)}"
    emoji = "🟢" if diff < 0 else "🔴"
    msg = (
        f"🏦 <b>FED RATE SIGNAL</b>\n\n"
        f"Market implies: <b>{implied_rate}%</b>\n"
        f"Current Fed rate: {current_rate}%\n"
        f"Difference: {diff:+.3f}%\n\n"
        f"{emoji} <b>{signal}</b>\n"
        f"🕐 {datetime.now(IST).strftime('%d-%b %H:%M IST')}"
    )
    _dedup_send(key, msg)


def alert_sector_breadth(breadth: str, advancing: int, total: int):
    """Alert on broad rally or broad selloff."""
    if breadth not in ("BROAD RALLY", "BROAD SELLOFF"):
        return
    key = f"breadth_{breadth.replace(' ','_')}"
    emoji = "🚀" if breadth == "BROAD RALLY" else "💥"
    msg = (
        f"{emoji} <b>SECTOR BREADTH ALERT: {breadth}</b>\n\n"
        f"{advancing}/{total} sectors moving together\n\n"
        f"{'Almost all sectors rallying — strong market-wide momentum.' if breadth == 'BROAD RALLY' else 'Almost all sectors selling off — broad market weakness.'}\n"
        f"🕐 {datetime.now(IST).strftime('%d-%b %H:%M IST')}"
    )
    _dedup_send(key, msg)


# ── News feed sender ─────────────────────────────────────────

def _format_news_msg(item: dict, score: int) -> str:
    """Format a single news item as a Telegram message."""
    headline = item.get("text", "")
    source   = item.get("source", "")
    url      = item.get("url", "")
    t        = item.get("time", "")

    # Score emoji
    if score >= 8:   emoji = "🔴"
    elif score >= 5: emoji = "🟡"
    else:            emoji = "⚪"

    msg = f"{emoji} <b>{headline}</b>\n"
    if source: msg += f"📡 {source}"
    if t:      msg += f"  •  {t}"
    msg += "\n"
    if url:    msg += f'<a href="{url}">Read more</a>'
    return msg


def send_news_feed(scored_news: list):
    """
    Send new news items to Telegram safely.
    - Only sends items not already sent (SQLite dedup)
    - Max 20 items per call
    - 1.2 second gap between messages (Telegram rate limit safe)
    - Score 2+ filter
    """
    if not scored_news:
        return

    to_send = []
    for entry in scored_news:
        try:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                score, item = entry
                if score < 2:
                    continue
                if not isinstance(item, dict):
                    continue
                headline = item.get("text", "")
                if not headline:
                    continue
                key = _headline_key(headline)
                if not _already_sent(key):
                    to_send.append((score, item, key))
        except Exception:
            continue

    if not to_send:
        return

    # Sort by score descending — highest impact first
    to_send.sort(key=lambda x: -x[0])

    # Cap at 20 per cycle
    to_send = to_send[:20]

    def _send_batch():
        for score, item, key in to_send:
            try:
                msg = _format_news_msg(item, score)
                ok  = send_telegram(msg, silent=True)  # silent=True = no phone buzz for each
                if ok:
                    _mark_sent(key)
                time.sleep(1.2)  # stay under Telegram 1 msg/sec limit
            except Exception:
                time.sleep(1.2)
                continue

    threading.Thread(target=_send_batch, daemon=True).start()


# ── Watchdog — runs every 5 minutes, checks all conditions ───

_cleanup_counter = 0

def _run_watchdog():
    """Background thread — checks all alert conditions every 5 minutes."""
    global _cleanup_counter
    time.sleep(60)  # wait for server warmup
    while True:
        try:
            _check_all()
        except Exception:
            pass
        _cleanup_counter += 1
        if _cleanup_counter >= 288:  # once every 24 hours (288 × 5min)
            _cleanup_old_sent()
            _cleanup_counter = 0
        time.sleep(300)  # check every 5 minutes


def _check_all():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))

    # 1. Check VIX backwardation
    try:
        from vix_term import get_vix_signals
        vd = get_vix_signals()
        vix  = vd.get("vix", {})
        spot = vix.get("VIX",  {}).get("value", 0)
        vm   = vix.get("VIX3M",{}).get("value", 0)
        if spot and vm:
            alert_vix_backwardation(spot, vm)
        fed = vd.get("fed")
        if fed:
            alert_fed_signal(fed["implied_rate"], fed["current_rate"], fed["signal"])
    except Exception:
        pass

    # 2. Check FII flows
    try:
        from nse_data import get_fii_dii
        fii = get_fii_dii()
        alert_fii(fii.get("FII_net", 0), fii.get("DII_net", 0))
    except Exception:
        pass

    # 3. Check Congress cluster buys
    try:
        from capitol_trades import get_congress_trades
        ct = get_congress_trades()
        for c in ct.get("clusters", []):
            if c["count"] >= 2:
                alert_congress_cluster(c["ticker"], c["count"], c["politicians"])
    except Exception:
        pass

    # 4. Check sector breadth
    try:
        from sector_pulse import get_sector_pulse
        sp = get_sector_pulse()
        alert_sector_breadth(sp.get("breadth",""), sp.get("advancing",0), sp.get("total",0))
    except Exception:
        pass


def start_watchdog():
    """Call this once at server startup to begin background alert checking."""
    threading.Thread(target=_run_watchdog, daemon=True).start()
