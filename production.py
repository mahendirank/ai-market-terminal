"""
production.py — Production stability layer.

Provides:
  - structured logging (JSON-style key=value records)
  - retry decorator with exponential backoff
  - rate limiter (per-IP, sliding window, in-memory)
  - graceful fallback wrapper
  - health probe registry
  - get_health() aggregator

All modules can:
    from production import log, retry, rate_limit, register_health, healthy_or
"""
import os
import sys
import time
import json
import threading
import functools
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Structured logging ──────────────────────────────────────────────────────

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


def log(level: str, scope: str, msg: str, **fields) -> None:
    """Structured log line. fields are k=v pairs appended."""
    if _LEVELS.get(level.upper(), 20) < _LEVELS.get(LOG_LEVEL, 20):
        return
    parts = [
        datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        level.upper(),
        f"[{scope}]",
        msg,
    ]
    if fields:
        parts.append(" ".join(f"{k}={v}" for k, v in fields.items()))
    print(" ".join(parts), flush=True)


# ─── Retry with exponential backoff ──────────────────────────────────────────

def retry(max_attempts: int = 3, base_delay: float = 0.5, max_delay: float = 8.0,
          exceptions: tuple = (Exception,), scope: str = "retry"):
    """Decorator — retry on exception with exp backoff. Logs each attempt."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_err = e
                    if attempt == max_attempts:
                        log("ERROR", scope, f"{fn.__name__} failed after {max_attempts}",
                            err=type(e).__name__, msg=str(e)[:120])
                        raise
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    log("WARN", scope, f"{fn.__name__} attempt {attempt} failed, retrying in {delay:.1f}s",
                        err=type(e).__name__)
                    time.sleep(delay)
            raise last_err  # unreachable
        return wrapper
    return decorator


# ─── Graceful fallback ───────────────────────────────────────────────────────

def healthy_or(fallback, scope: str = "fallback"):
    """Decorator — if fn raises, log and return fallback value instead."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                log("ERROR", scope, f"{fn.__name__} failed, using fallback",
                    err=type(e).__name__, msg=str(e)[:120])
                return fallback() if callable(fallback) else fallback
        return wrapper
    return decorator


# ─── Rate limiter (per-IP sliding window, in-memory) ──────────────────────────

_RATE_BUCKETS = defaultdict(lambda: deque())
_RATE_LOCK = threading.Lock()


def rate_limit_check(key: str, max_per_window: int, window_secs: int) -> tuple:
    """Returns (allowed: bool, remaining: int, reset_in_secs: int)."""
    now = time.time()
    cutoff = now - window_secs
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS[key]
        # drop expired
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_per_window:
            reset_in = max(0, int(bucket[0] + window_secs - now))
            return False, 0, reset_in
        bucket.append(now)
        return True, max_per_window - len(bucket), window_secs


# ─── Health probes registry ──────────────────────────────────────────────────

_HEALTH_PROBES = {}
_BG_LAST_RUN  = {}     # name → ts


def register_health(name: str, probe_fn) -> None:
    """Register a callable that returns dict {ok: bool, ...details}."""
    _HEALTH_PROBES[name] = probe_fn


def heartbeat(name: str) -> None:
    """Background worker calls this each iteration so we can detect staleness."""
    _BG_LAST_RUN[name] = time.time()


def _get_bg_loop_status() -> dict:
    """How fresh is each background loop?

    Lists every RECURRING background loop and the max age (seconds) of its
    last heartbeat before it is considered stale (a loop stays "healthy"
    while its last heartbeat is within 2× this value). Every loop here must
    call heartbeat("<name>") once per iteration — a name with no heartbeat
    reports "no-heartbeat", which means the loop is unmonitored, not dead.

    The one-shot boot warm-up (`_warm`) is intentionally excluded: it runs
    once and exits, so staleness detection does not apply to it.
    """
    now = time.time()
    out = {}
    expected = {
        "continuous_refresh":  60,
        "price_publisher":     30,
        "alert_engine":       180,
        "morning_note":       180,
        "digest":             300,
        "explainer_scan":     420,
        "macro_desk_snap":    900,
        "morning_report":    1500,
        "signal_verify":     7200,
    }
    for name, max_age in expected.items():
        last = _BG_LAST_RUN.get(name)
        if last is None:
            out[name] = {"status": "no-heartbeat", "expected_every_secs": max_age}
        else:
            age = int(now - last)
            out[name] = {
                "status":      "healthy" if age <= max_age * 2 else "stale",
                "last_run_secs_ago": age,
                "expected_every_secs": max_age,
            }
    return out


# ─── Aggregator: get_health() — full system probe ─────────────────────────────

def get_health() -> dict:
    started_at = time.time()
    summary = {
        "ts":          datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S IST"),
        "service":     "ai-market-terminal",
        "checks":      {},
        "bg_loops":    _get_bg_loop_status(),
    }

    # Run all registered probes
    for name, probe in _HEALTH_PROBES.items():
        try:
            t0 = time.time()
            res = probe()
            res["latency_ms"] = int((time.time() - t0) * 1000)
            summary["checks"][name] = res
        except Exception as e:
            summary["checks"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Overall status
    all_ok = all(c.get("ok", False) for c in summary["checks"].values())
    bg_ok  = all(b.get("status") in ("healthy", "no-heartbeat") for b in summary["bg_loops"].values())
    summary["status"] = "healthy" if all_ok and bg_ok else ("degraded" if all_ok or bg_ok else "unhealthy")
    summary["probe_duration_ms"] = int((time.time() - started_at) * 1000)
    return summary


# ─── Built-in default probes (registered on import) ──────────────────────────

def _probe_redis():
    url = os.environ.get("REDIS_URL", "")
    if not url:
        return {"ok": True, "configured": False, "note": "REDIS_URL not set — using SQLite fallback"}
    try:
        import redis
        c = redis.from_url(url, socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
        pong = c.ping()
        info = c.info("memory")
        return {
            "ok":            bool(pong),
            "configured":    True,
            "url_prefix":    url[:30],
            "memory_used_mb": round(info.get("used_memory", 0) / 1024 / 1024, 2),
        }
    except Exception as e:
        return {"ok": False, "configured": True, "error": f"{type(e).__name__}: {e}"}


def _probe_sqlite():
    import sqlite3, glob
    db_dir = os.path.join(os.path.dirname(__file__), "db")
    files = glob.glob(os.path.join(db_dir, "*.db"))
    out = {"ok": True, "db_count": len(files), "dbs": {}}
    for f in files[:25]:
        try:
            with sqlite3.connect(f, timeout=3) as c:
                tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                size_kb = os.path.getsize(f) // 1024
                out["dbs"][os.path.basename(f)] = {"tables": len(tables), "size_kb": size_kb}
        except Exception as e:
            out["dbs"][os.path.basename(f)] = {"error": str(e)[:80]}
            out["ok"] = False
    return out


def _probe_groq():
    key = os.environ.get("GROQ_API_KEY", "").strip()
    return {"ok": bool(key), "configured": bool(key), "model": "llama-3.3-70b-versatile"}


def _probe_telegram():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return {"ok": bool(token and chat), "token_set": bool(token), "chat_set": bool(chat)}


def _probe_live_data():
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        dxy_ok    = bool(lp.get("fx", {}).get("DXY"))
        gold_ok   = bool(lp.get("commodities", {}).get("GOLD"))
        ndx_ok    = bool(lp.get("global", {}).get("NASDAQ"))
        us10y_ok  = bool(lp.get("bonds", {}).get("US_10Y"))
        return {
            "ok": dxy_ok and gold_ok and ndx_ok and us10y_ok,
            "DXY":   dxy_ok, "GOLD": gold_ok, "NASDAQ": ndx_ok, "US10Y": us10y_ok,
            "source_count": sum(len(v) for v in lp.values() if isinstance(v, dict)),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _probe_news():
    try:
        from news import get_all_news
        items = get_all_news() or []
        return {"ok": len(items) > 0, "headline_count": len(items)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _probe_regime():
    try:
        from regime import detect_market_regime
        r = detect_market_regime() or {}
        return {"ok": True, "label": r.get("label"), "confidence": r.get("confidence"), "fallback_mode": r.get("fallback", False)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _probe_fx():
    try:
        from forex import get_forex_intel
        d = get_forex_intel() or {}
        pairs = (d.get("pairs") or {})
        return {"ok": len(pairs) >= 6, "pair_count": len(pairs)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# Register built-ins
register_health("redis",      _probe_redis)
register_health("sqlite",     _probe_sqlite)
register_health("groq",       _probe_groq)
register_health("telegram",   _probe_telegram)
register_health("live_data",  _probe_live_data)
register_health("news",       _probe_news)
register_health("regime",     _probe_regime)
register_health("forex",      _probe_fx)
