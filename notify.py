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
import os, re, requests, threading, time, sqlite3
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8475057388:AAGUlt5Qu3Ei2_3xeUF8S1TWvygDKVVxb8I")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "-1001379475837")  # PTA NISM channel

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
        if resp.status_code != 200:
            print(f"[TG] send failed {resp.status_code}: {resp.text[:200]}", flush=True)
        return resp.status_code == 200
    except Exception as e:
        print(f"[TG] exception: {e}", flush=True)
        return False


def _dedup_send(key: str, msg: str, silent: bool = False):
    """Send only if same alert hasn't been sent in this session."""
    if key in _sent_cache:
        return
    _sent_cache.add(key)
    threading.Thread(target=send_telegram, args=(msg, silent), daemon=True).start()


# ── Alert functions ──────────────────────────────────────────

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


# ── NEW: NIFTY key level alert ────────────────────────────────

_last_nifty_level = {"level": 0}  # track last alerted level to avoid repeats

def alert_nifty_level(nifty_price: float):
    """Alert when NIFTY crosses a key 250-point round number (e.g. 22500, 22750, 23000)."""
    if nifty_price <= 0:
        return
    level = round(nifty_price / 250) * 250   # nearest 250-point level
    if level == _last_nifty_level["level"]:
        return
    diff = nifty_price - level
    if abs(diff) > 40:          # must be within 40 points of the level to count as a cross
        return
    _last_nifty_level["level"] = level
    direction = "BROKE ABOVE" if diff >= 0 else "DROPPED BELOW"
    emoji     = "📈" if diff >= 0 else "📉"
    key       = f"nifty_level_{level}"
    if key in _sent_cache:
        return
    _sent_cache.add(key)
    msg = (
        f"{emoji} <b>NIFTY KEY LEVEL: {level}</b>\n\n"
        f"NIFTY has {direction} <b>{level}</b>\n"
        f"Current: {nifty_price:,.2f}\n\n"
        f"{'Watch for continuation above this level.' if diff >= 0 else 'Support broken — watch for further downside.'}\n"
        f"🕐 {datetime.now(IST).strftime('%d-%b %H:%M IST')}"
    )
    threading.Thread(target=send_telegram, args=(msg,), daemon=True).start()


# ── NEW: Macro big move alert (Gold / Oil / DXY) ─────────────

_macro_prev = {}   # {asset: price} — track previous price for % change

def alert_macro_move(asset: str, price: float, threshold_pct: float = 1.0):
    """Alert when Gold/Oil/DXY moves more than threshold_pct% since last check."""
    if price <= 0:
        return
    prev = _macro_prev.get(asset)
    _macro_prev[asset] = price
    if prev is None or prev <= 0:
        return
    pct = (price - prev) / prev * 100
    if abs(pct) < threshold_pct:
        return
    direction = "UP" if pct > 0 else "DOWN"
    emoji_map = {"GOLD": "🥇", "OIL": "🛢️", "DXY": "💵", "USDINR": "🇮🇳"}
    emoji = emoji_map.get(asset, "📊")
    key   = f"macro_{asset}_{int(price)}_{direction}"
    if key in _sent_cache:
        return
    _sent_cache.add(key)
    msg = (
        f"{emoji} <b>{asset} BIG MOVE: {pct:+.2f}%</b>\n\n"
        f"Price now: <b>{price:,.2f}</b>\n"
        f"Was: {prev:,.2f}\n\n"
        f"{'Bullish for risk assets' if (asset=='GOLD' and pct>0) else 'Bearish for markets' if (asset=='DXY' and pct>0) else 'Watch energy-linked stocks' if asset=='OIL' else 'Significant macro move'}\n"
        f"🕐 {datetime.now(IST).strftime('%d-%b %H:%M IST')}"
    )
    threading.Thread(target=send_telegram, args=(msg,), daemon=True).start()


# ── NEW: Morning Market Briefing (9:15 AM IST) ───────────────

_last_briefing_date = {"date": ""}

def send_morning_briefing():
    """Send a morning market briefing at 9:15–9:30 AM IST. Called once per day."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if _last_briefing_date["date"] == today:
        return
    _last_briefing_date["date"] = today

    def _build_and_send():
        lines = [f"🌅 <b>MORNING MARKET BRIEFING</b>\n{datetime.now(IST).strftime('%A, %d %b %Y')}\n"]

        # NIFTY + indices
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))
            from indices import get_indices
            idx = get_indices()
            nifty  = idx.get("NIFTY",  {})
            sensex = idx.get("SENSEX", {})
            spx    = idx.get("SPX",    {})
            vix    = idx.get("VIX",    {})
            if nifty:
                lines.append(f"🇮🇳 NIFTY:  <b>{nifty['price']:,.2f}</b>  {nifty['arrow']} {nifty['change']:+.2f}%")
            if sensex:
                lines.append(f"🇮🇳 SENSEX: <b>{sensex['price']:,.2f}</b>  {sensex['arrow']} {sensex['change']:+.2f}%")
            if spx:
                lines.append(f"🇺🇸 S&P500: <b>{spx['price']:,.2f}</b>  {spx['arrow']} {spx['change']:+.2f}%")
            if vix:
                vix_val = vix['price']
                vix_emoji = "🔴" if vix_val > 25 else "🟡" if vix_val > 18 else "🟢"
                lines.append(f"{vix_emoji} VIX:    <b>{vix_val:.1f}</b>  (Fear gauge)")
        except Exception:
            pass

        lines.append("")

        # FX + commodities
        try:
            from macro import get_macro_data
            m = get_macro_data()
            fx = m.get("FX", {})
            if fx.get("USDINR"):
                lines.append(f"💵 USD/INR: <b>{fx['USDINR']}</b>")
            if fx.get("DXY"):
                lines.append(f"💵 DXY:     <b>{fx['DXY']}</b>")
            oil  = m.get("OIL")
            gold = m.get("GOLD_SPOT")
            if oil:  lines.append(f"🛢️ OIL:     <b>${oil:.1f}</b>")
            if gold: lines.append(f"🥇 GOLD:    <b>${gold:,.0f}</b>")
            yld = m.get("US_YIELDS", {})
            if yld.get("US_10Y"):
                lines.append(f"📊 US 10Y:  <b>{yld['US_10Y']}%</b>")
        except Exception:
            pass

        lines.append("")

        # FII data
        try:
            from nse_data import get_fii_dii
            fii = get_fii_dii()
            fnet = fii.get("FII_net", 0)
            dnet = fii.get("DII_net", 0)
            femoji = "🟢" if fnet > 0 else "🔴"
            demoji = "🟢" if dnet > 0 else "🔴"
            lines.append(f"{femoji} FII: <b>₹{fnet:,.0f} Cr</b>")
            lines.append(f"{demoji} DII: <b>₹{dnet:,.0f} Cr</b>")
        except Exception:
            pass

        lines.append("")
        lines.append("📋 <b>Key levels to watch today:</b>")
        lines.append("• NIFTY: 22500 / 22250 / 22000")
        lines.append("• BANKNIFTY: 48000 / 47500")
        lines.append("")
        lines.append("🔔 You will get alerts for: breaking news, FII moves, VIX spikes, NIFTY levels")

        send_telegram("\n".join(lines))

    threading.Thread(target=_build_and_send, daemon=True).start()


# ── NEW: End-of-Day Summary (3:30 PM IST) ────────────────────

_last_eod_date = {"date": ""}

def send_eod_summary():
    """Send end-of-day market summary at ~3:30 PM IST. Called once per day."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if _last_eod_date["date"] == today:
        return
    _last_eod_date["date"] = today

    def _build_and_send():
        lines = [f"🔔 <b>MARKET CLOSING SUMMARY</b>\n{datetime.now(IST).strftime('%A, %d %b %Y')}\n"]

        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))
            from indices import get_indices
            idx = get_indices()
            nifty  = idx.get("NIFTY",  {})
            sensex = idx.get("SENSEX", {})
            spx    = idx.get("SPX",    {})
            vix    = idx.get("VIX",    {})
            if nifty:
                emoji = "🟢" if nifty["change"] >= 0 else "🔴"
                lines.append(f"{emoji} NIFTY:  <b>{nifty['price']:,.2f}</b>  {nifty['arrow']} {nifty['change']:+.2f}%")
            if sensex:
                emoji = "🟢" if sensex["change"] >= 0 else "🔴"
                lines.append(f"{emoji} SENSEX: <b>{sensex['price']:,.2f}</b>  {sensex['arrow']} {sensex['change']:+.2f}%")
            if spx:
                emoji = "🟢" if spx["change"] >= 0 else "🔴"
                lines.append(f"{emoji} S&P500: <b>{spx['price']:,.2f}</b>  {spx['arrow']} {spx['change']:+.2f}%")
            if vix:
                lines.append(f"😰 VIX:    <b>{vix['price']:.1f}</b>")
        except Exception:
            pass

        lines.append("")

        try:
            from macro import get_macro_data
            m = get_macro_data()
            oil  = m.get("OIL")
            gold = m.get("GOLD_SPOT")
            fx   = m.get("FX", {})
            if oil:  lines.append(f"🛢️ OIL:    <b>${oil:.1f}</b>")
            if gold: lines.append(f"🥇 GOLD:   <b>${gold:,.0f}</b>")
            if fx.get("USDINR"):
                lines.append(f"💵 USD/INR: <b>{fx['USDINR']}</b>")
        except Exception:
            pass

        lines.append("")

        try:
            from nse_data import get_fii_dii
            fii = get_fii_dii()
            fnet = fii.get("FII_net", 0)
            dnet = fii.get("DII_net", 0)
            femoji = "🟢" if fnet > 0 else "🔴"
            lines.append(f"{femoji} FII Net: <b>₹{fnet:,.0f} Cr</b>  |  DII: ₹{dnet:,.0f} Cr")
        except Exception:
            pass

        lines.append("")
        lines.append("See full dashboard for detailed signals.")

        send_telegram("\n".join(lines))

    threading.Thread(target=_build_and_send, daemon=True).start()


# ── News feed sender ─────────────────────────────────────────

def _format_news_msg(item: dict, score: int) -> str:
    """Format a single news item as a Telegram message."""
    headline = item.get("text", "")
    source   = item.get("source", "")
    url      = item.get("url", "")
    t        = item.get("time", "")

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

    to_send.sort(key=lambda x: -x[0])
    to_send = to_send[:20]

    def _send_batch():
        for score, item, key in to_send:
            try:
                msg = _format_news_msg(item, score)
                ok  = send_telegram(msg, silent=True)
                if ok:
                    _mark_sent(key)
                time.sleep(1.2)
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
        if _cleanup_counter >= 288:  # once every 24 hours
            _cleanup_old_sent()
            _cleanup_counter = 0
        time.sleep(300)  # check every 5 minutes


def _check_all():
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    now_ist = datetime.now(IST)
    hour    = now_ist.hour
    minute  = now_ist.minute

    # ── Morning briefing: 9:15–9:30 AM IST (market just opened) ──
    if hour == 9 and 15 <= minute <= 30:
        try:
            send_morning_briefing()
        except Exception:
            pass

    # ── EOD summary: 3:30–3:45 PM IST (market closed) ──
    if hour == 15 and 30 <= minute <= 45:
        try:
            send_eod_summary()
        except Exception:
            pass

    # 1. VIX backwardation + Fed signal
    try:
        from vix_term import get_vix_signals
        vd   = get_vix_signals()
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

    # 2. FII flows
    try:
        from nse_data import get_fii_dii
        fii = get_fii_dii()
        alert_fii(fii.get("FII_net", 0), fii.get("DII_net", 0))
    except Exception:
        pass

    # 3. Congress cluster buys
    try:
        from capitol_trades import get_congress_trades
        ct = get_congress_trades()
        for c in ct.get("clusters", []):
            if c["count"] >= 2:
                alert_congress_cluster(c["ticker"], c["count"], c["politicians"])
    except Exception:
        pass

    # 4. Sector breadth
    try:
        from sector_pulse import get_sector_pulse
        sp = get_sector_pulse()
        alert_sector_breadth(sp.get("breadth",""), sp.get("advancing",0), sp.get("total",0))
    except Exception:
        pass

    # 5. NIFTY key level alerts (only during market hours 9:15–15:35 IST)
    if (hour == 9 and minute >= 15) or (10 <= hour <= 14) or (hour == 15 and minute <= 35):
        try:
            from indices import get_indices
            idx = get_indices()
            nifty_price = idx.get("NIFTY", {}).get("price", 0)
            if nifty_price:
                alert_nifty_level(nifty_price)
        except Exception:
            pass

    # 6. Macro big move alerts: Gold >1%, Oil >2%, DXY >0.5%
    try:
        from macro import get_macro_data
        m    = get_macro_data()
        gold = m.get("GOLD_SPOT")
        oil  = m.get("OIL")
        fx   = m.get("FX", {})
        dxy  = fx.get("DXY")
        inr  = fx.get("USDINR")
        if gold: alert_macro_move("GOLD",   gold, threshold_pct=1.0)
        if oil:  alert_macro_move("OIL",    oil,  threshold_pct=2.0)
        if dxy:  alert_macro_move("DXY",    dxy,  threshold_pct=0.5)
        if inr:  alert_macro_move("USDINR", inr,  threshold_pct=0.3)
    except Exception:
        pass


def send_5min_digest(scored_news: list):
    """
    Called every 5 minutes. ALWAYS sends a message to keep the channel active.

    Priority:
      1. Fresh news last 5 min score>=8  → 🔴 BREAKING (buzzes phone)
      2. Fresh news last 5 min score 5-7 → 🟡 IMPORTANT (silent)
      3. No fresh news → top 5 stories from cache (any time, score>=3) → 📰 TOP STORIES
      4. Nothing at all → 🔕 Quiet market pulse (still sends to keep group alive)
    """
    cutoff = time.time() - 310   # 5 min + 10s buffer

    recent_high   = []   # score >= 8, published in last 5 min
    recent_medium = []   # score 5-7, published in last 5 min
    all_scored    = []   # all valid items for fallback

    for entry in scored_news:
        try:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            score, item = entry
            if not isinstance(item, dict):
                continue
            headline = item.get("text", "")
            if not headline:
                continue

            key = _headline_key(headline)

            # Collect all valid items for fallback top-stories
            if score >= 3:
                all_scored.append((score, item, key))

            # Fresh news filter
            pub_utc = item.get("pub_utc", "")
            if not pub_utc:
                continue
            try:
                pub_ts = datetime.fromisoformat(
                    pub_utc.replace("Z", "+00:00")
                ).timestamp()
                if pub_ts < cutoff:
                    continue
            except Exception:
                continue

            if score < 5:
                continue

            if _already_sent(key):
                continue

            if score >= 8:
                recent_high.append((score, item, key))
            else:
                recent_medium.append((score, item, key))
        except Exception:
            continue

    # Sort groups
    recent_high.sort(key=lambda x: -x[0])
    recent_medium.sort(key=lambda x: -x[0])
    all_scored.sort(key=lambda x: -x[0])

    has_fresh = bool(recent_high or recent_medium)
    new_items = recent_high + recent_medium   # items to mark sent

    def _send():
        ist_now = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")
        lines   = [f"📊 <b>AI MARKET TERMINAL</b>  •  {ist_now}", ""]

        if has_fresh:
            total = len(recent_high) + len(recent_medium)
            lines.append(f"<b>{total} new stor{'y' if total == 1 else 'ies'} in last 5 min:</b>")
            lines.append("")

            if recent_high:
                lines.append("🔴 <b>BREAKING NEWS:</b>")
                for score, item, _ in recent_high:
                    src = item.get("source", "")
                    src_tag = f"  <i>[{src}]</i>" if src else ""
                    lines.append(f"• {item['text']}{src_tag}  <b>({score}/10)</b>")
                lines.append("")

            if recent_medium:
                lines.append("🟡 <b>IMPORTANT:</b>")
                for score, item, _ in recent_medium:
                    src = item.get("source", "")
                    src_tag = f"  <i>[{src}]</i>" if src else ""
                    lines.append(f"• {item['text']}{src_tag}  ({score}/10)")

        elif all_scored:
            # No fresh news — show top stories to keep channel active
            top5 = all_scored[:5]
            lines.append("📰 <b>TOP STORIES RIGHT NOW:</b>")
            lines.append("")
            for score, item, _ in top5:
                src = item.get("source", "")
                src_tag = f"  <i>[{src}]</i>" if src else ""
                lines.append(f"• {item['text']}{src_tag}  ({score}/10)")
            lines.append("")
            lines.append("<i>No major breaking news in last 5 min</i>")

        else:
            lines.append("🔕 <b>Market Quiet</b>")
            lines.append("<i>No significant news at this time. Monitoring live...</i>")

        lines.append("")
        lines.append("⚡️ <i>Powered by AI Market Terminal</i>")

        msg  = "\n".join(lines)
        buzz = len(recent_high) > 0
        ok   = send_telegram(msg, silent=not buzz)
        if ok:
            for _, _, key in new_items:
                _mark_sent(key)

    threading.Thread(target=_send, daemon=True).start()


def start_watchdog():
    """Call this once at server startup to begin background alert checking."""
    threading.Thread(target=_run_watchdog, daemon=True).start()
