"""
Telegram alert system — sends market alerts to your Telegram.
Bot token + chat ID loaded from environment variables (never hardcoded).
"""
import os, requests, threading, time
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8475057388:AAGUlt5Qu3Ei2_3xeUF8S1TWvygDKVVxb8I")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "1026742085")

_sent_cache = set()   # prevent duplicate alerts within same session


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


# ── Watchdog — runs every 5 minutes, checks all conditions ───

def _run_watchdog():
    """Background thread — checks all alert conditions every 5 minutes."""
    time.sleep(60)  # wait for server warmup
    while True:
        try:
            _check_all()
        except Exception:
            pass
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
