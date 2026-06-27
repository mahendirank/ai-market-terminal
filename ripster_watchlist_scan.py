"""
ripster_watchlist_scan.py — Scan a watchlist through the fusion engine and
push high-conviction, catalyst-confirmed calls to Telegram.

Pipeline per ticker:
  yfinance 5m OHLCV  ->  ripster_fusion.fuse()  (technicals + hni_news sentiment)
  -> keep only actionable calls -> de-dupe vs recent alerts -> Telegram.

Run as a one-shot (cron-friendly):
  python3 ripster_watchlist_scan.py                      # default US list, live send
  python3 ripster_watchlist_scan.py NVDA AAPL MU         # explicit tickers
  python3 ripster_watchlist_scan.py --dry                # print, don't send
  python3 ripster_watchlist_scan.py --min-conv 0.6       # raise the bar

Telegram creds come from .env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).
De-dupe state lives in ripster_scan_state.json next to this file.
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

import pandas as pd
import requests

from hni_sentiment_provider import register
from ripster_fusion import fuse
from user_settings import get_user_settings, all_users_with_settings

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATE = os.path.join(_HERE, "ripster_scan_state.json")

# Default watchlist — liquid US stocks (the technical engine is US-equity tuned).
DEFAULT_TICKERS = ["NVDA", "AAPL", "MU", "TSLA", "AMD", "META", "AMZN", "MSFT", "GOOGL"]

# Map common watchlist aliases -> yfinance symbols. Anything not here passes through
# (a plain stock ticker like "NVDA" needs no mapping).
SYMBOL_MAP = {
    "GOLD": "GC=F", "XAUUSD": "GC=F", "SILVER": "SI=F", "OIL": "CL=F", "WTI": "CL=F",
    "DXY": "DX-Y.NYB", "EURUSD": "EURUSD=X", "USDJPY": "JPY=X", "GBPUSD": "GBPUSD=X",
    "NASDAQ": "^IXIC", "NAS100": "^IXIC", "US100": "^IXIC", "SPX": "^GSPC", "US500": "^GSPC",
    "DOW": "^DJI", "US30": "^DJI", "BTC": "BTC-USD", "ETH": "ETH-USD",
}


def _resolve_yf(sym: str) -> str:
    """Watchlist alias -> yfinance fetch symbol (original alias kept for news match)."""
    return SYMBOL_MAP.get(sym.upper(), sym.upper())


def _asset_class(alias: str, yf_sym: str) -> str:
    """Classify so the technical engine picks RTH vs 24h session logic."""
    a, y = alias.upper(), yf_sym.upper()
    if y.endswith("-USD") or a in {"BTC", "ETH"}:
        return "crypto"          # 24h
    if y.endswith("=X") or a in {"DXY", "EURUSD", "USDJPY", "GBPUSD"}:
        return "forex"           # 24h
    if y.endswith("=F"):
        return "futures"         # 24h (gold/oil/etc.)
    if y.startswith("^"):
        return "index"           # RTH (e.g. ^IXIC trades US session)
    return "us_stock"            # RTH


def tickers_from_watchlist(username: str = "") -> list[str]:
    """Pull a user's watchlist (original aliases) from user_settings."""
    wl = get_user_settings(username).get("watchlist", []) or []
    return [str(a).upper() for a in wl]

# Alert gates
MIN_CONVICTION = 0.50      # below this, not worth an alert
COOLDOWN_H     = 4.0       # don't re-send same direction within this window


# ─── env / .env loader (no hard dep on python-dotenv) ────────────────────────
def _load_env() -> None:
    p = os.path.join(_HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _send_telegram(text: str, chat_id: str = "") -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat or token == "YOUR_BOT_TOKEN":
        print("  [telegram] creds missing — skipping send")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text, "parse_mode": "Markdown",
                                "disable_web_page_preview": True}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  [telegram] send failed: {e}")
        return False


# ─── de-dupe state ───────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        return json.load(open(_STATE))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        json.dump(st, open(_STATE, "w"))
    except Exception:
        pass


def _should_send(state: dict, tkr: str, direction: str, catalyst: str) -> bool:
    """Send if new direction, or catalyst upgraded to CONFIRMED, or cooldown passed."""
    prev = state.get(tkr)
    now = time.time()
    if not prev:
        return True
    if prev.get("direction") != direction:
        return True                                  # direction flipped
    if catalyst == "CONFIRMED" and prev.get("catalyst") != "CONFIRMED":
        return True                                  # upgraded to confirmed catalyst
    return (now - prev.get("ts", 0)) >= COOLDOWN_H * 3600.0


# ─── formatting ──────────────────────────────────────────────────────────────
def _fmt(call: dict) -> str:
    d = call["direction"]
    emoji = "🟢" if d == "BUY" else "🔴" if d == "SELL" else "⚪"
    cat = call["catalyst"]
    badge = " ⚡CONFIRMED" if cat == "CONFIRMED" else " ⚠️CONFLICT" if cat == "CONFLICT" else ""
    t = call["technical"]
    rvol = t.get("rvol")
    n = call["news"]
    bar = "━━━━━━━━━━━━━━━"
    msg = (f"{emoji} *{call['ticker']} — {d}*{badge}\n{bar}\n"
           f"🎯 Conviction : `{call['conviction']:.2f}`\n"
           f"📈 Day-type   : {call['day_type']}  |  RVOL `{rvol}x`\n"
           f"☁️ Tech       : {t.get('regime')}  (5/12 {t.get('cloud_5_12')}, 34/50 {t.get('cloud_34_50')})\n"
           f"📰 News       : {n.get('label')} `{n.get('score')}` (conf {n.get('confidence')})\n"
           f"💡 {call['action_hint']}")
    return msg


# ─── scan ────────────────────────────────────────────────────────────────────
def scan(tickers: list[str], min_conv: float = MIN_CONVICTION, dry: bool = False,
         chat_id: str = "", label: str = "") -> list[dict]:
    _load_env()
    register()                                       # install real hni_news sentiment
    state = _load_state()
    fired: list[dict] = []
    print(f"Scanning {len(tickers)} tickers{(' for '+label) if label else ''} "
          f"(min conv {min_conv}, dry={dry}) @ {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z")

    for tkr in tickers:
        yf_sym = _resolve_yf(tkr)
        ac = _asset_class(tkr, yf_sym)
        try:
            import yfinance as yf
            df = yf.download(yf_sym, period="5d", interval="5m", auto_adjust=False, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            call = fuse(tkr, df, asset_class=ac)      # original alias -> matches news DB; ac -> RTH/24h
        except Exception as e:
            print(f"  {tkr}: skip ({e.__class__.__name__})")
            continue

        d, conv, cat = call["direction"], call["conviction"], call["catalyst"]
        actionable = d != "NEUTRAL" and (cat == "CONFIRMED" or conv >= min_conv) and cat != "CONFLICT"
        tag = f"{d} conv={conv} {cat}"
        skey = f"{label or 'global'}:{tkr}"          # de-dupe namespaced per user/chat
        if not actionable:
            print(f"  {tkr}: — {tag}")
            continue
        if not _should_send(state, skey, d, cat):
            print(f"  {tkr}: (held, cooldown) {tag}")
            continue

        msg = _fmt(call)
        ok = True if dry else _send_telegram(msg, chat_id)
        print(f"  {tkr}: {'DRY' if dry else ('SENT' if ok else 'FAIL')} {tag}")
        if dry:
            print("    " + msg.replace("\n", "\n    "))
        if ok and not dry:
            state[skey] = {"direction": d, "catalyst": cat, "ts": time.time()}
        fired.append(call)

    if not dry:
        _save_state(state)
    print(f"Done. {len(fired)} alert(s).")
    return fired


def scan_user(username: str, dry: bool = False, min_conv: float | None = None) -> list[dict]:
    """Scan one user's watchlist, honour their telegram chat + min_conf."""
    s = get_user_settings(username)
    if not s.get("telegram_enabled", True):
        print(f"[{username}] telegram disabled — skipping"); return []
    tickers = tickers_from_watchlist(username)
    if not tickers:
        print(f"[{username}] empty watchlist"); return []
    chat = s.get("telegram_chat_id") or ""           # per-user override; falls back to global env
    # user's alert_thresholds.min_conf is 0-100; map to 0-1 conviction (CONFIRMED still always passes)
    user_min = min_conv if min_conv is not None else (s.get("alert_thresholds", {}).get("min_conf", 50) / 100.0)
    return scan(tickers, min_conv=user_min, dry=dry, chat_id=str(chat), label=username)


def scan_all_users(dry: bool = False) -> None:
    """Scan every user who has saved settings, each to their own chat."""
    users = all_users_with_settings()
    if not users:
        print("No users with settings — falling back to default watchlist user.")
        scan_user("", dry=dry); return
    for u in users:
        scan_user(u["username"], dry=dry)


if __name__ == "__main__":
    args = sys.argv[1:]
    dry = "--dry" in args
    all_users = "--all-users" in args
    user = ""
    min_conv = None
    if "--user" in args:
        i = args.index("--user"); user = args[i + 1]; del args[i:i + 2]
    if "--min-conv" in args:
        i = args.index("--min-conv"); min_conv = float(args[i + 1]); del args[i:i + 2]
    pos = [a for a in args if not a.startswith("--")]

    if all_users:
        scan_all_users(dry=dry)
    elif user or not pos:
        # user's watchlist (named user, or default-settings watchlist when blank)
        scan_user(user, dry=dry, min_conv=min_conv)
    else:
        scan([a.upper() for a in pos], min_conv=min_conv or MIN_CONVICTION, dry=dry)
