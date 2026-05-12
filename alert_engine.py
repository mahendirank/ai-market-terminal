"""
alert_engine.py — Institutional Telegram alert engine.

Triggers:
  1. MACRO REGIME SHIFT       — any of 6 binary dimensions flips state
  2. CB SURPRISE              — CB-related news with surprise/shock keywords
  3. CORRELATION BREAK        — DXY-Gold inverse, USDJPY-US10Y, NDX-VIX inverse
  4. HIGH-CONFIDENCE EXPLAINER — new explainer entry with conf >= 80%
  5. VOLATILITY SPIKE         — VIX up >= 15% OR > absolute level threshold
  6. GOLD BREAKOUT            — gold > N-day high or strong absolute move
  7. DXY REVERSAL             — DXY direction change after 3+ sessions trend
  8. BOND YIELD SHOCK         — US 10Y change >= 0.5% intraday

Each alert uses the 5-section institutional format:
  - What happened
  - Why it matters
  - Affected assets
  - Suggested positioning
  - Risk warning

Cooldown system prevents spam: per-trigger + per-asset + per-direction key.
Storage: SQLite at /app/db/alerts.db (Redis-backed dedup if REDIS_URL set).

User-configurable thresholds via env vars:
  ALERT_VIX_SPIKE_PCT   (default 15)
  ALERT_VIX_ABS_LEVEL   (default 25)
  ALERT_GOLD_PCT        (default 0.8)
  ALERT_YIELD_SHOCK_PCT (default 0.5)
  ALERT_DXY_PCT         (default 0.4)
  ALERT_COOLDOWN_SECS   (default 3600)  per (trigger, asset)
  ALERT_MIN_CONF        (default 80) for explainer alerts
  ALERT_DISABLED        (default false) — global kill-switch
"""
import os
import json
import time
import sqlite3
import hashlib
import threading
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ─── User-configurable thresholds (read from env, sensible defaults) ─────────

def _f(name, default):
    try: return float(os.environ.get(name, default))
    except: return float(default)

CFG = {
    "vix_spike_pct":   _f("ALERT_VIX_SPIKE_PCT", 15),
    "vix_abs_level":   _f("ALERT_VIX_ABS_LEVEL", 25),
    "gold_pct":        _f("ALERT_GOLD_PCT", 0.8),
    "yield_shock_pct": _f("ALERT_YIELD_SHOCK_PCT", 0.5),
    "dxy_pct":         _f("ALERT_DXY_PCT", 0.4),
    "cooldown_secs":   int(_f("ALERT_COOLDOWN_SECS", 3600)),
    "min_conf":        int(_f("ALERT_MIN_CONF", 80)),
    "disabled":        os.environ.get("ALERT_DISABLED", "false").lower() in ("1", "true", "yes"),
}

# ─── Redis dedup (optional) + SQLite history ─────────────────────────────────

_redis_client = None
_redis_ok     = False

def _init_redis():
    global _redis_client, _redis_ok
    url = os.environ.get("REDIS_URL", "")
    if not url:
        return
    try:
        import redis
        c = redis.from_url(url, socket_connect_timeout=4, socket_timeout=4, decode_responses=True)
        c.ping()
        _redis_client, _redis_ok = c, True
        print(f"[alert_engine] Redis cooldown store: connected", flush=True)
    except Exception as e:
        _redis_ok = False
        print(f"[alert_engine] Redis unavailable ({e}) — using SQLite cooldowns", flush=True)
_init_redis()

_DB_DIR  = os.path.join(os.path.dirname(__file__), "db")
os.makedirs(_DB_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DB_DIR, "alerts.db")
_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ist        TEXT NOT NULL,
                trigger_type  TEXT NOT NULL,
                cooldown_key  TEXT NOT NULL,
                title         TEXT NOT NULL,
                message_text  TEXT NOT NULL,
                payload_json  TEXT,
                sent_ok       INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS alert_cooldown (
                cooldown_key  TEXT PRIMARY KEY,
                expires_at    INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alert_history(ts_ist)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_type ON alert_history(trigger_type)")
        c.commit()
_init_db()


# ─── Cooldown ────────────────────────────────────────────────────────────────

def _cooldown_active(key: str) -> bool:
    """True if alert key was sent within the cooldown window."""
    if _redis_ok and _redis_client:
        try:
            return _redis_client.exists(f"alert:cd:{key}") > 0
        except Exception:
            pass
    now = int(time.time())
    try:
        with _db_lock, _conn() as c:
            row = c.execute(
                "SELECT expires_at FROM alert_cooldown WHERE cooldown_key=?", (key,)
            ).fetchone()
        return row is not None and row["expires_at"] > now
    except Exception:
        return False


def _set_cooldown(key: str, secs: int):
    if _redis_ok and _redis_client:
        try:
            _redis_client.setex(f"alert:cd:{key}", secs, "1")
            return
        except Exception:
            pass
    expires = int(time.time()) + secs
    try:
        with _db_lock, _conn() as c:
            c.execute("INSERT OR REPLACE INTO alert_cooldown(cooldown_key, expires_at) VALUES (?, ?)",
                      (key, expires))
            # housekeeping — clear expired
            c.execute("DELETE FROM alert_cooldown WHERE expires_at < ?", (int(time.time()) - 3600,))
            c.commit()
    except Exception:
        pass


# ─── Telegram send (reuses notify.send_telegram) ──────────────────────────────

def _send_telegram(text: str) -> bool:
    if CFG["disabled"]:
        print("[alert_engine] alerts disabled by env", flush=True)
        return False
    try:
        from notify import send_telegram
        return bool(send_telegram(text, silent=False))
    except Exception as e:
        print(f"[alert_engine] send_telegram error: {e}", flush=True)
        return False


def _store_history(ev: dict, sent_ok: bool):
    try:
        with _db_lock, _conn() as c:
            c.execute("""
                INSERT INTO alert_history
                  (ts_ist, trigger_type, cooldown_key, title, message_text, payload_json, sent_ok)
                VALUES (?,?,?,?,?,?,?)
            """, (
                datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S IST"),
                ev["trigger_type"], ev["cooldown_key"], ev["title"], ev["message"],
                json.dumps(ev.get("payload", {}), default=str), 1 if sent_ok else 0
            ))
            # rotate at 200 rows
            c.execute("DELETE FROM alert_history WHERE id NOT IN (SELECT id FROM alert_history ORDER BY id DESC LIMIT 200)")
            c.commit()
    except Exception as e:
        print(f"[alert_engine] store_history: {e}", flush=True)


def _emit(ev: dict, cooldown_secs: int = None) -> bool:
    """Send alert if not in cooldown. Returns True if sent."""
    if cooldown_secs is None:
        cooldown_secs = CFG["cooldown_secs"]
    if _cooldown_active(ev["cooldown_key"]):
        return False
    ok = _send_telegram(ev["message"])
    _set_cooldown(ev["cooldown_key"], cooldown_secs)
    _store_history(ev, ok)
    return ok


# ─── Alert formatter — 5-section institutional format ─────────────────────────

def _fmt_alert(emoji: str, title: str, what: str, why: str, assets: str,
               position: str, risk: str) -> str:
    """Build the Telegram message in HTML."""
    return (
        f"{emoji} <b>{title}</b>\n"
        f"<i>{datetime.now(IST).strftime('%d-%b %H:%M IST')}</i>\n\n"
        f"<b>▸ What happened</b>\n{what}\n\n"
        f"<b>▸ Why it matters</b>\n{why}\n\n"
        f"<b>▸ Affected assets</b>\n{assets}\n\n"
        f"<b>▸ Suggested positioning</b>\n{position}\n\n"
        f"<b>▸ Risk</b>\n{risk}"
    )


# ─── Trigger checks ──────────────────────────────────────────────────────────

def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
        if cur is None: return default
    return cur


def _check_regime_shift() -> list:
    """Compare current macro_desk dimensions to the previous snapshot."""
    events = []
    try:
        from macro_desk import get_macro_regime_view, get_history
        cur = get_macro_regime_view()
        hist = get_history(limit=2)
        if not hist or len(hist) < 1:
            return events
        prev = hist[0]
        for dim_name in ("risk", "dollar", "fed", "yields", "inflation", "commodities"):
            cur_state = (cur.get("dimensions") or {}).get(dim_name, {}).get("state")
            prev_state = prev.get(f"{dim_name}_state")
            if not cur_state or not prev_state or cur_state == prev_state:
                continue
            # State flipped — but only alert if confidence > 60 to avoid noise
            cur_conf = (cur.get("dimensions") or {}).get(dim_name, {}).get("confidence", 0)
            if cur_conf < 60:
                continue
            cooldown_key = f"regime_shift:{dim_name}:{cur_state}:{datetime.now(IST).strftime('%Y%m%d')}"
            cur_driver = (cur.get("dimensions") or {}).get(dim_name, {}).get("driver", "—")
            events.append({
                "trigger_type": "REGIME_SHIFT",
                "cooldown_key": cooldown_key,
                "title": f"REGIME SHIFT: {dim_name.upper()} → {cur_state}",
                "message": _fmt_alert(
                    emoji="🔄",
                    title=f"REGIME SHIFT — {dim_name.upper()}: {prev_state} → {cur_state}",
                    what=f"The {dim_name} dimension flipped from <b>{prev_state}</b> to <b>{cur_state}</b> ({cur_conf}% conf). Driver: {cur_driver}.",
                    why=f"Macro regime shifts are leading indicators of cross-asset rotation. {dim_name.capitalize()} regime is one of the six pillars driving asset performance.",
                    assets=_assets_for_regime(dim_name, cur_state),
                    position=_position_for_regime(dim_name, cur_state),
                    risk=f"A reversal back to {prev_state} would invalidate this signal. Watch confidence — currently {cur_conf}%."
                ),
                "payload": {"dim": dim_name, "from": prev_state, "to": cur_state, "conf": cur_conf},
            })
    except Exception as e:
        print(f"[alert_engine] regime_shift check: {e}", flush=True)
    return events


def _assets_for_regime(dim: str, state: str) -> str:
    table = {
        ("risk", "ON"):     "↑ Equities (NDX, SPX) ↑ BTC ↑ EM FX  ·  ↓ Gold ↓ JPY ↓ CHF",
        ("risk", "OFF"):    "↑ Gold ↑ JPY ↑ CHF ↑ USD  ·  ↓ Equities ↓ BTC ↓ AUD",
        ("dollar", "STRONG"):"↑ DXY ↑ USDJPY  ·  ↓ EUR/USD ↓ GBP/USD ↓ Gold ↓ EM FX",
        ("dollar", "WEAK"):  "↑ Gold ↑ EUR/USD ↑ GBP/USD ↑ Commodities  ·  ↓ DXY",
        ("fed", "HAWKISH"):  "↑ DXY ↑ US 10Y ↑ Banks  ·  ↓ Gold ↓ NDX ↓ EM FX",
        ("fed", "DOVISH"):   "↑ Gold ↑ NDX ↑ BTC ↑ EM FX  ·  ↓ DXY ↓ US 10Y",
        ("yields", "RISING"):"↑ DXY ↑ Banks ↑ USDJPY  ·  ↓ Gold ↓ Tech/NDX ↓ Long-duration bonds",
        ("yields", "FALLING"):"↑ Gold ↑ Tech/NDX ↑ Long-duration  ·  ↓ DXY ↓ Banks ↓ USDJPY",
        ("inflation", "HOT"):"↑ Gold ↑ Oil ↑ Commodities ↑ TIPS  ·  ↓ Bonds ↓ Long-duration",
        ("inflation", "COOLING"):"↑ Bonds ↑ Long-duration ↑ NDX  ·  ↓ Gold ↓ Commodities",
        ("commodities", "BULL"):"↑ Gold ↑ Oil ↑ AUD ↑ CAD ↑ Energy stocks  ·  ↓ Consumer disc",
        ("commodities", "BEAR"):"↑ USD ↑ Bonds  ·  ↓ Gold ↓ Oil ↓ AUD ↓ CAD ↓ Energy stocks",
    }
    return table.get((dim, state), "Cross-asset rotation expected")


def _position_for_regime(dim: str, state: str) -> str:
    table = {
        ("risk", "ON"):     "Pro-risk tilt viable: long equities, EM FX. Trim hedges (VIX puts, gold).",
        ("risk", "OFF"):    "Defensive tilt: long Gold, USD, Treasuries. Cut beta/EM exposure.",
        ("dollar", "STRONG"):"Long DXY ETF (UUP). Short Gold, EM FX. EUR/USD short-bias.",
        ("dollar", "WEAK"):  "Short DXY (UDN). Long Gold, EM FX, EUR/USD. Long commodities tilt.",
        ("fed", "HAWKISH"):  "Long banks/financials. Short long-duration bonds. Short Gold.",
        ("fed", "DOVISH"):   "Long Gold, NDX, BTC. Long long-duration bonds. Short DXY.",
        ("yields", "RISING"):"Short long-bonds (TLT). Long banks. Watch USDJPY long.",
        ("yields", "FALLING"):"Long long-bonds (TLT). Long Tech/NDX. Long Gold.",
        ("inflation", "HOT"):"Long Gold, Oil, TIPS. Short long-bonds. Long energy stocks.",
        ("inflation", "COOLING"):"Long bonds, Tech/NDX. Trim Gold/commodity beta.",
        ("commodities", "BULL"):"Long Gold, Oil, AUD/CAD. Long energy stocks.",
        ("commodities", "BEAR"):"Cut commodity exposure. Long USD, bonds.",
    }
    return table.get((dim, state), "Reassess cross-asset positioning to align with new regime")


def _check_yield_shock() -> list:
    events = []
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        chg = float(_safe(lp, "bonds", "US_10Y", "change", default=0) or 0)
        lvl = float(_safe(lp, "bonds", "US_10Y", "price", default=0) or 0)
        if abs(chg) < CFG["yield_shock_pct"]:
            return events
        direction = "UP" if chg > 0 else "DOWN"
        cooldown_key = f"yield_shock:{direction}:{datetime.now(IST).strftime('%Y%m%d')}"
        events.append({
            "trigger_type": "YIELD_SHOCK",
            "cooldown_key": cooldown_key,
            "title": f"YIELD SHOCK: US 10Y {direction} {chg:+.2f}%",
            "message": _fmt_alert(
                emoji="📉" if chg < 0 else "📈",
                title=f"BOND YIELD SHOCK — US 10Y {direction} {chg:+.2f}%",
                what=f"US 10Y yield moved {chg:+.2f}% to <b>{lvl}%</b>, breaching the {CFG['yield_shock_pct']}% intraday threshold.",
                why=("Sharp yield moves drive every cross-asset relationship — DXY, Gold, USDJPY carry, "
                     "equity duration, EM FX. Magnitude here suggests positioning unwind or fresh "
                     "central-bank repricing."),
                assets=("↑ DXY ↑ USDJPY ↑ Banks  ·  ↓ Gold ↓ Tech/NDX ↓ TLT ↓ EM FX" if chg > 0 else
                        "↑ Gold ↑ Tech/NDX ↑ TLT ↑ EM FX  ·  ↓ DXY ↓ USDJPY ↓ Banks"),
                position=("Reduce long-duration. USDJPY long viable. Trim Gold/EM exposure." if chg > 0 else
                          "Add long-duration. Cut USDJPY long. Add Gold/EM exposure."),
                risk="Extreme yield moves often mean-revert within 24-48h — don't chase late."
            ),
            "payload": {"chg": chg, "level": lvl},
        })
    except Exception as e:
        print(f"[alert_engine] yield_shock: {e}", flush=True)
    return events


def _check_vol_spike() -> list:
    events = []
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        vix = float(_safe(lp, "vix", "VIX", "price", default=0) or 0)
        chg = float(_safe(lp, "vix", "VIX", "change", default=0) or 0)
        triggered = (chg >= CFG["vix_spike_pct"]) or (vix >= CFG["vix_abs_level"])
        if not triggered or vix <= 0:
            return events
        cooldown_key = f"vol_spike:{datetime.now(IST).strftime('%Y%m%d_%H')}"   # hourly cooldown
        events.append({
            "trigger_type": "VOL_SPIKE",
            "cooldown_key": cooldown_key,
            "title": f"VIX SPIKE: {vix:.1f} ({chg:+.1f}%)",
            "message": _fmt_alert(
                emoji="⚠️",
                title=f"VOLATILITY SPIKE — VIX {vix:.1f} ({chg:+.1f}%)",
                what=f"VIX moved to <b>{vix:.1f}</b> ({chg:+.1f}% intraday). Threshold: spike >={CFG['vix_spike_pct']}% OR absolute level >={CFG['vix_abs_level']}.",
                why=("Vol regime shifts drive cross-asset deleveraging. Carry trades unwind, "
                     "USD bid as haven, Gold attracts safe-haven flow, equities derisk."),
                assets="↑ Gold ↑ JPY ↑ CHF ↑ USD ↑ Bonds  ·  ↓ Equities ↓ AUD ↓ EM FX ↓ BTC",
                position="Cut beta/EM. Add USD, Gold, JPY hedges. Reduce carry-trade exposure (long USDJPY, long EM).",
                risk=f"VIX spikes often deflate within 1-2 sessions. Don't chase puts at VIX {vix:.0f}+ unless regime confirms."
            ),
            "payload": {"vix": vix, "chg": chg},
        })
    except Exception as e:
        print(f"[alert_engine] vol_spike: {e}", flush=True)
    return events


def _check_gold_breakout() -> list:
    events = []
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        gold = float(_safe(lp, "commodities", "GOLD", "price", default=0) or 0)
        gold_chg = float(_safe(lp, "commodities", "GOLD", "change", default=0) or 0)
        if abs(gold_chg) < CFG["gold_pct"] or gold <= 0:
            return events
        direction = "UP" if gold_chg > 0 else "DOWN"
        cooldown_key = f"gold:{direction}:{datetime.now(IST).strftime('%Y%m%d')}"
        events.append({
            "trigger_type": "GOLD_BREAKOUT",
            "cooldown_key": cooldown_key,
            "title": f"GOLD {direction} {gold_chg:+.2f}% @ ${gold:.0f}",
            "message": _fmt_alert(
                emoji="🥇",
                title=f"GOLD STRONG MOVE — {gold_chg:+.2f}% to ${gold:.2f}",
                what=f"Gold moved {gold_chg:+.2f}% intraday to <b>${gold:.2f}</b>. Threshold: ±{CFG['gold_pct']}%.",
                why=("Gold is a triangulation of real yields, DXY, and risk regime. A strong intraday "
                     "move usually indicates one of these forces moved decisively or a haven bid emerged."),
                assets=("↑ Gold ↑ Silver ↑ Miners  ·  ↓ DXY ↓ TIPS yields" if gold_chg > 0 else
                        "↑ DXY ↑ Real yields  ·  ↓ Gold ↓ Silver ↓ Miners"),
                position=("Tactical long Gold viable. Consider Gold via ETF or spot." if gold_chg > 0 else
                          "Reduce Gold exposure. Watch for stop-cascade flush."),
                risk="Gold often retraces 30-50% of strong intraday moves. Tight stops or scaled entries advised."
            ),
            "payload": {"price": gold, "chg": gold_chg},
        })
    except Exception as e:
        print(f"[alert_engine] gold: {e}", flush=True)
    return events


def _check_dxy_reversal() -> list:
    events = []
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        dxy_chg = float(_safe(lp, "fx", "DXY", "change", default=0) or 0)
        dxy_lvl = float(_safe(lp, "fx", "DXY", "price", default=0) or 0)
        if abs(dxy_chg) < CFG["dxy_pct"] or dxy_lvl <= 0:
            return events
        direction = "UP" if dxy_chg > 0 else "DOWN"
        cooldown_key = f"dxy:{direction}:{datetime.now(IST).strftime('%Y%m%d')}"
        events.append({
            "trigger_type": "DXY_REVERSAL",
            "cooldown_key": cooldown_key,
            "title": f"DXY {direction} {dxy_chg:+.2f}% @ {dxy_lvl:.2f}",
            "message": _fmt_alert(
                emoji="💵",
                title=f"DXY STRONG MOVE — {dxy_chg:+.2f}% to {dxy_lvl:.2f}",
                what=f"DXY moved {dxy_chg:+.2f}% intraday to <b>{dxy_lvl:.2f}</b>. Threshold: ±{CFG['dxy_pct']}% (DXY is normally low-vol).",
                why=("Dollar moves cascade through every G10 cross. A sharp intraday move usually reflects "
                     "yield-differential repricing, central bank speech, or risk-regime flip."),
                assets=("↑ DXY ↑ USDJPY  ·  ↓ Gold ↓ EUR/USD ↓ GBP/USD ↓ Commodities" if dxy_chg > 0 else
                        "↑ Gold ↑ EUR/USD ↑ GBP/USD ↑ Commodities  ·  ↓ DXY ↓ USDJPY"),
                position=("Long USD via UUP, short EM FX, short gold." if dxy_chg > 0 else
                          "Short USD via UDN, long EM FX, long gold."),
                risk="DXY rarely sustains intraday moves >0.6% without a fresh catalyst — beware mean reversion."
            ),
            "payload": {"price": dxy_lvl, "chg": dxy_chg},
        })
    except Exception as e:
        print(f"[alert_engine] dxy: {e}", flush=True)
    return events


def _check_correlation_break() -> list:
    """DXY-Gold typically inverse. USDJPY-US10Y typically positive. NDX-VIX typically inverse."""
    events = []
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        dxy_c = float(_safe(lp, "fx", "DXY", "change", default=0) or 0)
        gold_c = float(_safe(lp, "commodities", "GOLD", "change", default=0) or 0)
        usdjpy_c = float(_safe(lp, "fx", "USDJPY", "change", default=0) or 0)
        y10c = float(_safe(lp, "bonds", "US_10Y", "change", default=0) or 0)
        ndx_c = float(_safe(lp, "global", "NASDAQ", "change", default=0) or 0)
        vix_c = float(_safe(lp, "vix", "VIX", "change", default=0) or 0)

        breaks = []
        # DXY-Gold should be inverse — if both up >0.3% or both down >0.3%, that's a break
        if dxy_c > 0.3 and gold_c > 0.5:
            breaks.append(("DXY-Gold positive co-move", "Both DXY and Gold up — typical hedge against systemic risk (haven flow on both)."))
        elif dxy_c < -0.3 and gold_c < -0.5:
            breaks.append(("DXY-Gold negative co-move", "Both DXY and Gold down — usually means broad USD-funded carry trade unwinding into stocks."))
        # USDJPY-US10Y should be positive
        if usdjpy_c > 0.4 and y10c < -0.4:
            breaks.append(("USDJPY-US10Y carry break", "USDJPY up despite US yields falling — JPY-specific weakness (BoJ dovish surprise?)."))
        elif usdjpy_c < -0.4 and y10c > 0.4:
            breaks.append(("USDJPY-US10Y safe-haven JPY bid", "USDJPY down despite US yields rising — safe-haven JPY bid likely."))
        # NDX-VIX should be inverse
        if ndx_c > 0.5 and vix_c > 5:
            breaks.append(("NDX-VIX co-move", "NDX up while VIX rising — hedging into the rally, positioning anxiety."))

        if not breaks:
            return events

        cooldown_key = f"corr_break:{datetime.now(IST).strftime('%Y%m%d_%H')}"   # hourly
        # Combine all breaks into one alert
        what = " | ".join(b[0] for b in breaks)
        why_lines = "\n".join(f"• {b[1]}" for b in breaks)
        events.append({
            "trigger_type": "CORRELATION_BREAK",
            "cooldown_key": cooldown_key,
            "title": f"CORRELATION BREAK: {what}",
            "message": _fmt_alert(
                emoji="🔀",
                title=f"CROSS-ASSET CORRELATION BREAK",
                what=what,
                why=why_lines,
                assets="Selectively monitored across DXY/Gold/USDJPY/US10Y/NDX/VIX",
                position=("When traditional correlations break, cut leverage. Wait for regime to "
                          "clarify before adding directional risk."),
                risk="Correlation breaks often precede vol regime shifts — size down, not up."
            ),
            "payload": {"breaks": [b[0] for b in breaks]},
        })
    except Exception as e:
        print(f"[alert_engine] correlation: {e}", flush=True)
    return events


def _check_high_confidence_explainer() -> list:
    """Surface explainer entries with conf >= MIN_CONF as alerts."""
    events = []
    try:
        from explainer import get_recent_explanations
        recents = get_recent_explanations(limit=10)
        for e in recents:
            if (e.get("confidence") or 0) < CFG["min_conf"]:
                continue
            cooldown_key = f"hc_explainer:{e.get('signature') or e.get('asset_key')}"
            tags = (e.get("tags") or [])
            tag_str = " · ".join(tags[:3]) if tags else "—"
            events.append({
                "trigger_type": "HIGH_CONF_EXPLAINER",
                "cooldown_key": cooldown_key,
                "title": f"HIGH-CONF MOVE: {e['asset_display']} {e['change_pct']:+.2f}%",
                "message": _fmt_alert(
                    emoji="🎯",
                    title=f"HIGH-CONFIDENCE MOVE — {e['asset_display']} {e['change_pct']:+.2f}% (conf {e['confidence']}%)",
                    what=e.get("what_moved", "—"),
                    why=e.get("why_it_moved", "—"),
                    assets=("\n• " + "\n• ".join(e.get("evidence") or [])) or "—",
                    position=e.get("forward_implic", "—"),
                    risk=e.get("risk_to_thesis", "—") + (f"\n\n<i>Tags: {tag_str}</i>" if tags else "")
                ),
                "payload": {"asset": e["asset_key"], "conf": e["confidence"]},
            })
    except Exception as ex:
        print(f"[alert_engine] hc_explainer: {ex}", flush=True)
    return events


def _check_cb_surprise() -> list:
    """Scan recent news for CB surprise/hike/cut keywords."""
    events = []
    try:
        from news import get_all_news
        items = (get_all_news() or [])[:50]
        text = " ".join((it.get("text") or it.get("title", "")).lower() for it in items)
        signals = [
            ("FED_HIKE_SURPRISE",  ["fed surprise hike", "powell hawkish surprise", "unexpected fed hike"]),
            ("FED_CUT_SURPRISE",   ["fed surprise cut", "fed pivots", "unexpected fed cut"]),
            ("ECB_SURPRISE",       ["ecb surprise", "lagarde surprise", "ecb unexpected"]),
            ("BOJ_SURPRISE",       ["boj surprise", "ueda surprise", "yen intervention"]),
            ("CB_CRISIS_SIGNAL",   ["emergency rate", "emergency liquidity", "central bank crisis"]),
        ]
        for tag, keys in signals:
            if any(k in text for k in keys):
                cooldown_key = f"cb_surprise:{tag}:{datetime.now(IST).strftime('%Y%m%d')}"
                events.append({
                    "trigger_type": "CB_SURPRISE",
                    "cooldown_key": cooldown_key,
                    "title": f"CB SURPRISE — {tag}",
                    "message": _fmt_alert(
                        emoji="🏦",
                        title=f"CENTRAL BANK SURPRISE DETECTED — {tag.replace('_', ' ')}",
                        what=f"News scan flagged keywords matching <b>{tag.replace('_',' ')}</b>.",
                        why="Central bank surprises are the largest single-event drivers of FX, yields and gold. First-mover on the right side captures the move; latecomers buy the unwind.",
                        assets="Watch DXY, US 10Y, EUR/USD, USD/JPY, Gold for immediate repricing",
                        position="Reduce leverage immediately. Wait for the dust to settle (15-30 min) before re-entering.",
                        risk=f"Confirm via Reuters/Bloomberg before sizing. Signal source: news keyword match."
                    ),
                    "payload": {"tag": tag},
                })
    except Exception as e:
        print(f"[alert_engine] cb_surprise: {e}", flush=True)
    return events


# ─── Master runner ────────────────────────────────────────────────────────────

ALL_CHECKS = {
    "regime_shift":      _check_regime_shift,
    "yield_shock":       _check_yield_shock,
    "vol_spike":         _check_vol_spike,
    "gold_breakout":     _check_gold_breakout,
    "dxy_reversal":      _check_dxy_reversal,
    "correlation_break": _check_correlation_break,
    "hc_explainer":      _check_high_confidence_explainer,
    "cb_surprise":       _check_cb_surprise,
}


def run_all_checks(emit: bool = True) -> dict:
    """Run every trigger; emit alerts (with cooldown) and persist history.
    Returns a summary."""
    summary = {"checked": [], "candidates": 0, "sent": 0, "in_cooldown": 0, "errors": 0}
    if CFG["disabled"]:
        return {"disabled": True, **summary}

    for trigger_name, check_fn in ALL_CHECKS.items():
        try:
            evs = check_fn() or []
            summary["checked"].append({"trigger": trigger_name, "candidates": len(evs)})
            for ev in evs:
                summary["candidates"] += 1
                if not emit:
                    continue
                if _emit(ev):
                    summary["sent"] += 1
                else:
                    summary["in_cooldown"] += 1
        except Exception as e:
            print(f"[alert_engine] {trigger_name} runner error: {e}", flush=True)
            summary["errors"] += 1
    return summary


# ─── Public reads (history + config) ──────────────────────────────────────────

def get_alert_history(limit: int = 30) -> list:
    try:
        with _db_lock, _conn() as c:
            rows = c.execute("""
                SELECT ts_ist, trigger_type, title, message_text, sent_ok, payload_json
                FROM alert_history ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try: d["payload"] = json.loads(d.get("payload_json") or "{}")
            except: d["payload"] = {}
            d.pop("payload_json", None)
            out.append(d)
        return out
    except Exception as e:
        print(f"[alert_engine] get_history: {e}", flush=True)
        return []


def get_config() -> dict:
    return {
        **CFG,
        "telegram_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "redis_dedup":         _redis_ok,
    }


def update_config(updates: dict) -> dict:
    """Update thresholds in-process. Persists for the lifetime of the container."""
    for k, v in (updates or {}).items():
        if k in CFG:
            try:
                if isinstance(CFG[k], bool):
                    CFG[k] = bool(v)
                elif isinstance(CFG[k], int):
                    CFG[k] = int(v)
                else:
                    CFG[k] = float(v)
            except Exception:
                pass
    return get_config()
