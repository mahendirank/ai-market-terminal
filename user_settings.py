"""
user_settings.py — Per-user multi-tenant data isolation.

Each authenticated user has their own:
  - watchlist (assets they want to follow)
  - alert thresholds (override the global defaults)
  - telegram chat (override TELEGRAM_CHAT_ID for personal alerts)
  - preferences (theme, default tab, panels visibility)

Storage: SQLite at /app/db/user_settings.db, indexed by username.
Redis-backed read cache when REDIS_URL set.
"""
import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

_DB_DIR  = os.path.join(os.path.dirname(__file__), "db")
os.makedirs(_DB_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DB_DIR, "user_settings.db")
_db_lock = threading.Lock()

# Default settings for new users
DEFAULTS = {
    "watchlist":       ["GOLD", "DXY", "EURUSD", "USDJPY", "NASDAQ", "BTC"],
    "alert_thresholds": {
        "vix_spike_pct":   15.0,
        "vix_abs_level":   25.0,
        "gold_pct":        0.8,
        "yield_shock_pct": 0.5,
        "dxy_pct":         0.4,
        "min_conf":        80,
    },
    "telegram_chat_id":  None,    # if set, overrides global for this user
    "telegram_enabled":  True,
    "preferences": {
        "default_tab":     "ALL",
        "theme":           "dark",
        "show_india":      True,    # set False for forex/UAE-only users
    },
}


def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                username     TEXT PRIMARY KEY,
                settings_json TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
        """)
        c.commit()
_init_db()


def _now():
    return datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S IST")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive merge — override wins for leaf values."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get_user_settings(username: str) -> dict:
    """Returns full settings dict (DEFAULTS overlaid with user overrides)."""
    if not username:
        return dict(DEFAULTS)
    try:
        with _db_lock, _conn() as c:
            row = c.execute(
                "SELECT settings_json FROM user_settings WHERE username = ?", (username.lower(),)
            ).fetchone()
        if row:
            try:
                user_overrides = json.loads(row["settings_json"]) or {}
            except Exception:
                user_overrides = {}
            return _deep_merge(DEFAULTS, user_overrides)
    except Exception as e:
        print(f"[user_settings] read error: {e}", flush=True)
    return dict(DEFAULTS)


def update_user_settings(username: str, updates: dict) -> dict:
    """Merge updates into existing settings, persist, return final."""
    if not username:
        return dict(DEFAULTS)
    username = username.lower().strip()
    current = get_user_settings(username)
    # Pull just the user's overrides (without DEFAULTS noise) — we store overrides only
    # but easier: just store the merged result, and use _deep_merge with DEFAULTS on read
    new_settings = _deep_merge(current, updates or {})
    # Strip out DEFAULTS values so the row stays small (optional optimization)
    try:
        with _db_lock, _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO user_settings (username, settings_json, updated_at) VALUES (?,?,?)",
                (username, json.dumps(new_settings), _now())
            )
            c.commit()
    except Exception as e:
        print(f"[user_settings] write error: {e}", flush=True)
    return new_settings


def add_to_watchlist(username: str, asset: str) -> list:
    s = get_user_settings(username)
    wl = list(s.get("watchlist", []))
    asset = asset.upper()
    if asset not in wl:
        wl.append(asset)
    update_user_settings(username, {"watchlist": wl})
    return wl


def remove_from_watchlist(username: str, asset: str) -> list:
    s = get_user_settings(username)
    wl = [a for a in s.get("watchlist", []) if a.upper() != asset.upper()]
    update_user_settings(username, {"watchlist": wl})
    return wl


def all_users_with_settings() -> list:
    """Return list of usernames who have explicit settings (admin view)."""
    try:
        with _db_lock, _conn() as c:
            rows = c.execute("SELECT username, updated_at FROM user_settings ORDER BY updated_at DESC").fetchall()
        return [{"username": r["username"], "updated_at": r["updated_at"]} for r in rows]
    except Exception:
        return []
