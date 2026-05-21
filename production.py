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


# ── Cache-only data probes ───────────────────────────────────────────────────
# These probes must NEVER call the synchronous, network-bound getters
# (get_live_prices / get_all_news / detect_market_regime / get_forex_intel):
# on a cold cache those fetch over the network and block — exactly what made
# /api/health take ~13 s right after a restart. Instead they peek the in-memory
# caches the background loops keep warm (the price publisher refreshes prices +
# FX every ~2 s, continuous_refresh refreshes news every ~20 s), so every probe
# is a dict lookup. A cold or stale cache is reported, never waited on.

def _peek_cache(module: str, attr: str, sub: str = "") -> tuple:
    """Read a data module's in-memory cache WITHOUT triggering a fetch.

    Returns (data, age_seconds). data is None when the module isn't loaded,
    the cache attribute is missing, or the cache is still cold; age is None
    in that case. Fully defensive — a probe must never raise from here.
    """
    try:
        mod = sys.modules.get(module)
        if mod is None:
            return None, None
        cache = getattr(mod, attr, None)
        if not isinstance(cache, dict):
            return None, None
        entry = cache.get(sub) if sub else cache
        if not isinstance(entry, dict):
            return None, None
        data = entry.get("data")
        if not data:
            return None, None
        return data, max(0.0, time.time() - float(entry.get("ts") or 0.0))
    except Exception:
        return None, None


def _cache_verdict(data, age, stale_after: float, what: str) -> dict:
    """Shared cache-only verdict: ok when the cache holds data and that data
    is fresh; not-ok when the cache is cold or has gone stale (a stale cache
    also flags a background refresh loop that has stopped)."""
    if data is None:
        return {"ok": False, "cache": "cold", "note": f"no cached {what} yet"}
    stale = age is not None and age > stale_after
    return {
        "ok":          not stale,
        "cache":       "stale" if stale else "warm",
        "cache_age_s": round(age or 0.0, 1),
    }


def _probe_live_data():
    """Cache-only — peeks the live_prices cache the price publisher refreshes
    every ~2 s. Never fetches."""
    data, age = _peek_cache("live_prices", "_lp_cache")
    verdict = _cache_verdict(data, age, stale_after=180, what="price snapshot")
    if data is not None:
        verdict["DXY"]    = bool(data.get("fx", {}).get("DXY"))
        verdict["GOLD"]   = bool(data.get("commodities", {}).get("GOLD"))
        verdict["NASDAQ"] = bool(data.get("global", {}).get("NASDAQ"))
        verdict["US10Y"]  = bool(data.get("bonds", {}).get("US_10Y"))
        verdict["source_count"] = sum(
            len(v) for v in data.values() if isinstance(v, dict))
        verdict["ok"] = verdict["ok"] and all(
            verdict[k] for k in ("DXY", "GOLD", "NASDAQ", "US10Y"))
    return verdict


def _probe_news():
    """Cache-only — peeks the news cache continuous_refresh warms every ~20 s."""
    data, age = _peek_cache("news", "_all_news_cache")
    verdict = _cache_verdict(data, age, stale_after=600, what="news")
    if data is not None:
        verdict["headline_count"] = len(data)
        verdict["ok"] = verdict["ok"] and len(data) > 0
    return verdict


def _probe_regime():
    """Cache-only — peeks the last regime classification (computed by
    macro_desk / morning_report). Never recomputes."""
    data, age = _peek_cache("regime", "_cache", sub="regime")
    verdict = _cache_verdict(data, age, stale_after=1800,
                             what="regime classification")
    if data is not None:
        verdict["label"]         = data.get("label")
        verdict["confidence"]    = data.get("confidence")
        verdict["fallback_mode"] = data.get("fallback", False)
    return verdict


def _probe_fx():
    """Cache-only — peeks the forex price cache the price publisher refreshes
    every ~2 s (via get_forex_intel). Never fetches."""
    data, age = _peek_cache("forex", "_cache")
    verdict = _cache_verdict(data, age, stale_after=180, what="FX prices")
    if data is not None:
        verdict["pair_count"] = len(data)
        verdict["ok"] = verdict["ok"] and len(data) >= 6
    return verdict


# Register built-ins
register_health("redis",      _probe_redis)
register_health("sqlite",     _probe_sqlite)
register_health("groq",       _probe_groq)
register_health("telegram",   _probe_telegram)
register_health("live_data",  _probe_live_data)
register_health("news",       _probe_news)
register_health("regime",     _probe_regime)
register_health("forex",      _probe_fx)
