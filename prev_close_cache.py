"""
prev_close_cache.py — Single source of truth for "yesterday's close".

The bug this fixes: yfinance's ``Ticker.fast_info.previous_close`` is unreliable
for commodity futures (CL=F showed $96.60 today while the actual prior close
was $91.02 per Stooq, flipping crude's daily change from +3% to -3% and
cascading wrong "risk-on" narratives into the morning brief).

Pattern: the **most trusted source for prev_close** writes to this cache on
every successful fetch; **less trusted sources read from it** before computing
their change %. Within a single trading day, every consumer sees the same
prev_close regardless of which source served the live price.

Source-trust ladder (high → low):
    Stooq daily close → Swissquote previous → yfinance Ticker.history → yfinance fast_info

Keys: ``prev_close:<SYMBOL>:<YYYY-MM-DD-IST>``
Value: float (price per the symbol's native units)
TTL: ~24h (expires at next IST midnight + 1h)

Falls back to an in-process dict when Redis is unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# ── Redis (best-effort, in-process fallback) ────────────────────────────────
_redis_client = None
_redis_ok = False
_redis_lock = threading.Lock()


def _init_redis() -> None:
    global _redis_client, _redis_ok
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return
    with _redis_lock:
        if _redis_client is not None:
            return
        try:
            import redis
            c = redis.from_url(url, socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
            c.ping()
            _redis_client, _redis_ok = c, True
        except Exception:  # noqa: BLE001
            _redis_ok = False


_init_redis()

_INPROC: dict[str, tuple[float, float]] = {}  # key → (prev, ts)
_inproc_lock = threading.Lock()


def _today_ist_key() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _key(symbol: str) -> str:
    return f"prev_close:{symbol.upper()}:{_today_ist_key()}"


# ── Public API ─────────────────────────────────────────────────────────────

def put(symbol: str, prev: float, source: str = "") -> None:
    """Set the trusted prev_close for ``symbol`` for today (IST).

    First-write-wins to keep behaviour predictable: once a high-trust source
    has written a value, lower-trust fallbacks can't overwrite it within the
    same day. Use ``force_put`` when re-fetching from the same trusted source.
    """
    if not symbol or not prev or prev <= 0:
        return
    k = _key(symbol)
    # First-write-wins
    if get(symbol) is not None:
        return
    _set(k, prev)
    log.debug("[prev_close] set %s=%.4f (src=%s)", k, prev, source)


def force_put(symbol: str, prev: float) -> None:
    """Overwrite the cached prev_close. Reserved for the highest-trust source
    refreshing its own value."""
    if not symbol or not prev or prev <= 0:
        return
    _set(_key(symbol), prev)


def get(symbol: str) -> float | None:
    """Read the trusted prev_close for ``symbol`` today, or None if not set."""
    if not symbol:
        return None
    k = _key(symbol)
    if _redis_ok and _redis_client:
        try:
            raw = _redis_client.get(k)
            if raw is not None:
                return float(raw)
        except Exception:  # noqa: BLE001
            pass
    with _inproc_lock:
        entry = _INPROC.get(k)
        if entry:
            return entry[0]
    return None


def reconcile(symbol: str, candidate_prev: float, *, max_drift_pct: float = 5.0) -> float:
    """Return the trusted prev_close for ``symbol`` (cached vs candidate).

    See ``reconcile_with_quality`` for the version that also returns whether
    a drift was detected — preferred for new callers so downstream consumers
    can flag degraded data instead of silently using the corrected value."""
    value, _ = reconcile_with_quality(symbol, candidate_prev, max_drift_pct=max_drift_pct)
    return value


def reconcile_with_quality(symbol: str, candidate_prev: float, *,
                           max_drift_pct: float = 5.0) -> tuple[float, str]:
    """Like ``reconcile`` but also returns a quality tag.

    Returns
    -------
    (prev_close, quality) where quality is one of:
      "OK"       — no cache, OR cache and candidate agree
      "DEGRADED" — candidate disagreed with cache by > max_drift_pct;
                   the cached value is being used. Downstream should treat
                   the asset as low-confidence (skip narrative, lower bias
                   confidence tier) until the source recovers.
    """
    cached = get(symbol)
    if cached is None:
        if candidate_prev and candidate_prev > 0:
            put(symbol, candidate_prev, source="reconcile_bootstrap")
        return candidate_prev, "OK"
    if not candidate_prev or candidate_prev <= 0:
        return cached, "OK"
    drift = abs(candidate_prev - cached) / cached * 100
    if drift > max_drift_pct:
        log.warning(
            "[prev_close] %s: candidate %.4f drifts %.2f%% from cached %.4f — using cached, quality=DEGRADED",
            symbol, candidate_prev, drift, cached,
        )
        return cached, "DEGRADED"
    return cached, "OK"


# ── Internals ──────────────────────────────────────────────────────────────

def _set(k: str, prev: float) -> None:
    # Redis TTL = remaining seconds until 1 hour past IST midnight tonight
    now_ist = datetime.now(_IST)
    midnight_next = (now_ist + timedelta(days=1)).replace(hour=1, minute=0, second=0, microsecond=0)
    ttl = max(60, int((midnight_next - now_ist).total_seconds()))
    if _redis_ok and _redis_client:
        try:
            _redis_client.setex(k, ttl, str(prev))
        except Exception:  # noqa: BLE001
            pass
    with _inproc_lock:
        _INPROC[k] = (prev, time.time())
