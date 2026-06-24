"""
tvdata.py — TradingView live price feed via tvdatafeed.
Single price source for all NSE indices, sector indices, and top stocks.
Falls back to yfinance for global (US/EU) indices.
Set TV_USERNAME + TV_PASSWORD env vars for full TradingView access.
"""

import os, time, threading, gc, json
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FTimeout

try:
    from tvDatafeed import TvDatafeed, Interval
    _TV_AVAILABLE = True
except ImportError:
    _TV_AVAILABLE = False

_tv_instance   = None
_tv_lock       = threading.Lock()
_price_cache   : dict = {}            # L1 in-proc: key -> {"data", "ts"}
_CACHE_TTL     = 30                    # seconds — "fresh" window
_STALE_TTL     = 86400                 # keep last-good up to 24h for stale-serve

# ── Rate-limit / pacing state ──────────────────────────────────────────────────
# TradingView rate-limits guest sessions aggressively. We (a) pace requests with a
# global minimum gap to avoid bursts, and (b) back off hard on HTTP 429 instead of
# re-hammering every poll cycle. Set TV_USERNAME/TV_PASSWORD to raise the ceiling.
_RL_LOCK       = threading.Lock()
_MIN_GAP       = float(os.getenv("TV_MIN_REQUEST_GAP", "0.30"))   # seconds between TV calls
_MAX_WORKERS   = int(os.getenv("TV_MAX_WORKERS", "4"))            # was 12 — bursts tripped 429
_next_req_at   = 0.0

_COOLDOWN_BASE = int(os.getenv("TV_COOLDOWN_BASE", "60"))         # seconds, first 429
_COOLDOWN_MAX  = int(os.getenv("TV_COOLDOWN_MAX",  "900"))        # cap (15 min)
_INIT_COOLDOWN = 120                                              # pause after login/init failure
_CD_LOCK       = threading.Lock()                                 # guards cooldown escalate/clear
_cooldown_until      = 0.0
_consec_429          = 0
_tv_init_blocked_until = 0.0

# Don't pass off arbitrarily old cached values as a live quote. Beyond this age the
# stale-serve returns None so callers fall back (yfinance) instead of showing it.
_STALE_SERVE_MAX = int(os.getenv("TV_STALE_SERVE_MAX", "86400"))  # 24h cap, configurable

# ── Redis (optional, mirrors indicators.py pattern) ─────────────────────────────
_redis_client = None
_redis_ok     = False
_CD_KEY       = "tvdata:cooldown_until"
_N429_KEY     = "tvdata:consec_429"


def _init_redis() -> None:
    global _redis_client, _redis_ok, _consec_429
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return
    try:
        import redis
        c = redis.from_url(url, socket_connect_timeout=4, socket_timeout=4, decode_responses=True)
        c.ping()
        _redis_client, _redis_ok = c, True
        # Resume backoff state across restarts so we don't slam TV right after a bounce.
        try:
            n = c.get(_N429_KEY)
            if n:
                _consec_429 = int(n)
        except Exception:
            pass
        print("[tvdata] Redis cache: connected", flush=True)
    except Exception as e:
        _redis_ok = False
        print(f"[tvdata] Redis unavailable ({e}) — in-process cache only", flush=True)


_init_redis()


# ── Cooldown helpers (Redis-shared so workers/restarts coordinate) ──────────────

def _in_cooldown() -> bool:
    if _cooldown_until > time.time():
        return True
    if _redis_ok and _redis_client:
        try:
            v = _redis_client.get(_CD_KEY)
            if v and float(v) > time.time():
                return True
        except Exception:
            pass
    return False


def _trigger_cooldown(reason: str) -> None:
    global _cooldown_until, _consec_429
    with _CD_LOCK:
        # Escalate at most once per window: a concurrent batch of 429s (4 workers all
        # hitting the limit in one cycle) must not multiply the backoff exponent.
        if _cooldown_until > time.time():
            return
        _consec_429 += 1
        secs  = min(_COOLDOWN_BASE * (2 ** (_consec_429 - 1)), _COOLDOWN_MAX)
        until = time.time() + secs
        _cooldown_until = until
        if _redis_ok and _redis_client:
            try:
                _redis_client.set(_CD_KEY, until, ex=int(secs) + 5)
                _redis_client.set(_N429_KEY, _consec_429, ex=_COOLDOWN_MAX * 2)
            except Exception:
                pass
    print(f"[tvdata] rate-limited ({reason}) — pausing TradingView for {int(secs)}s "
          f"(#{_consec_429}); serving cached/yfinance", flush=True)


def _clear_cooldown() -> None:
    global _cooldown_until, _consec_429
    with _CD_LOCK:
        # Only reset the backoff once the window has fully elapsed. A success that
        # races with a concurrent worker's fresh 429 must NOT wipe the new cooldown
        # (that would defeat the escalating backoff mid-storm).
        if _cooldown_until > time.time():
            return
        if _consec_429 or _cooldown_until:
            _cooldown_until = 0.0
            _consec_429     = 0
            if _redis_ok and _redis_client:
                try:
                    _redis_client.delete(_CD_KEY, _N429_KEY)
                except Exception:
                    pass


# ── Two-tier price cache with stale-serve ───────────────────────────────────────

def _cache_read(cache_key: str):
    """Return (data, age_seconds) for the last-good price, or (None, None)."""
    entry = _price_cache.get(cache_key)
    if entry:
        return entry["data"], time.time() - entry["ts"]
    if _redis_ok and _redis_client:
        try:
            raw = _redis_client.get(f"tvdata:price:{cache_key}")
            if raw:
                obj = json.loads(raw)
                _price_cache[cache_key] = obj          # warm L1
                return obj["data"], time.time() - obj["ts"]
        except Exception:
            pass
    return None, None


def _cache_write(cache_key: str, data: dict) -> None:
    entry = {"data": data, "ts": time.time()}
    _price_cache[cache_key] = entry
    if _redis_ok and _redis_client:
        try:
            _redis_client.set(f"tvdata:price:{cache_key}", json.dumps(entry), ex=_STALE_TTL)
        except Exception:
            pass


def _serve_stale(data, age):
    """Return last-good data flagged as stale (so callers/UI can tell it isn't live),
    or None if it's too old to pass off as a quote. The in-proc L1 never expires, so
    the age cap is enforced here at serve time rather than relying on the entry TTL."""
    if data is None or age is None or age >= _STALE_SERVE_MAX:
        return None
    out = dict(data)
    out["stale"] = True
    out["age_s"] = int(age)
    return out


def _throttle() -> None:
    """Pace requests to one per _MIN_GAP. Reserve a staggered slot under the lock,
    then sleep OUTSIDE it so workers' get_hist calls overlap instead of serializing
    to concurrency 1 (which would blow _fetch_many's deadline on large batches)."""
    global _next_req_at
    with _RL_LOCK:
        now  = time.time()
        slot = _next_req_at if _next_req_at > now else now
        _next_req_at = slot + _MIN_GAP
    wait = slot - time.time()
    if wait > 0:
        time.sleep(wait)


# ── Per-symbol negative cache ───────────────────────────────────────────────────
# A symbol that just failed (timeout / no-data / 429) is skipped for a short window
# instead of re-attempted every poll cycle. Fewer wasted ~5s blocking calls = less
# load on TradingView = far less chance of escalating into a 429.
_neg_cache: dict = {}
_NEG_TTL   = int(os.getenv("TV_NEG_CACHE_TTL", "60"))

# Only suppress a symbol after this many CONSECUTIVE failures, so the FIRST transient
# failure (e.g. the documented cold-start SSL timeout) doesn't immediately block the
# tight retry loops that callers like live_prices._tv_bond_one depend on.
_fail_count: dict = {}
_NEG_THRESHOLD = int(os.getenv("TV_NEG_THRESHOLD", "2"))


def _record_fail(cache_key: str) -> None:
    n = _fail_count.get(cache_key, 0) + 1
    _fail_count[cache_key] = n
    if n >= _NEG_THRESHOLD:
        _mark_neg(cache_key)


def _record_ok(cache_key: str) -> None:
    _fail_count.pop(cache_key, None)
    _clear_neg(cache_key)


def _is_neg(cache_key: str) -> bool:
    if _neg_cache.get(cache_key, 0) > time.time():
        return True
    if _redis_ok and _redis_client:
        try:
            if _redis_client.get(f"tvdata:neg:{cache_key}"):
                return True
        except Exception:
            pass
    return False


def _mark_neg(cache_key: str) -> None:
    _neg_cache[cache_key] = time.time() + _NEG_TTL
    if _redis_ok and _redis_client:
        try:
            _redis_client.set(f"tvdata:neg:{cache_key}", "1", ex=_NEG_TTL)
        except Exception:
            pass


def _clear_neg(cache_key: str) -> None:
    _neg_cache.pop(cache_key, None)
    if _redis_ok and _redis_client:
        try:
            _redis_client.delete(f"tvdata:neg:{cache_key}")
        except Exception:
            pass

# ── NSE Indices ───────────────────────────────────────────────────────────────
NSE_INDICES = {
    "NIFTY50":    ("NIFTY50",    "NSE"),
    "BANKNIFTY":  ("BANKNIFTY",  "NSE"),
    "FINNIFTY":   ("FINNIFTY",   "NSE"),
    "MIDCPNIFTY": ("MIDCPNIFTY", "NSE"),
    "SENSEX":     ("SENSEX",     "BSE"),
}

# ── NSE Sector Indices ────────────────────────────────────────────────────────
NSE_SECTORS = {
    "IT":      ("CNXIT",     "NSE"),
    "BANKING": ("BANKNIFTY", "NSE"),
    "FMCG":    ("CNXFMCG",   "NSE"),
    "AUTO":    ("CNXAUTO",   "NSE"),
    "PHARMA":  ("CNXPHARMA", "NSE"),
    "METAL":   ("CNXMETAL",  "NSE"),
    "REALTY":  ("CNXREALTY", "NSE"),
    "ENERGY":  ("CNXENERGY", "NSE"),
}

# ── NSE Top Stocks per Sector ─────────────────────────────────────────────────
NSE_STOCKS = {
    "IT":      ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
    "BANKING": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK"],
    "FMCG":    ["HINDUNILVR", "ITC", "NESTLEIND", "DABUR", "MARICO"],
    "AUTO":    ["MARUTI", "TATAMOTORS", "BAJAJ-AUTO", "M&M", "EICHERMOT"],
    "PHARMA":  ["SUNPHARMA", "CIPLA", "DRREDDY", "DIVISLAB", "APOLLOHOSP"],
    "METAL":   ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "COALINDIA"],
    "REALTY":  ["DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "PHOENIXLTD"],
    "ENERGY":  ["RELIANCE", "ONGC", "NTPC", "POWERGRID", "BPCL"],
}

# ── Global Indices (yfinance fallback) ────────────────────────────────────────
GLOBAL_YF = {
    "SPX":    "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW":    "^DJI",
    "DAX":    "^GDAXI",
    "FTSE":   "^FTSE",
    "NIKKEI": "^N225",
    "HSI":    "^HSI",
    "VIX":    "^VIX",
    "GOLD":   "GC=F",
    "CRUDE":  "CL=F",
    "DXY":    "DX-Y.NYB",
}


# ── TvDatafeed singleton ──────────────────────────────────────────────────────

def _get_tv():
    global _tv_instance, _tv_init_blocked_until
    if not _TV_AVAILABLE:
        return None
    if _tv_instance is not None:
        return _tv_instance
    # Avoid re-attempting (and re-rate-limiting) login on every call after a failure.
    if time.time() < _tv_init_blocked_until:
        return None
    with _tv_lock:
        if _tv_instance is None:
            username = os.environ.get("TV_USERNAME", "").strip()
            password = os.environ.get("TV_PASSWORD", "").strip()
            try:
                _tv_instance = TvDatafeed(
                    username=username or None,
                    password=password or None,
                )
            except Exception as e:
                print(f"[tvdata] TvDatafeed init failed: {e}", flush=True)
                _tv_instance = None
                _tv_init_blocked_until = time.time() + _INIT_COOLDOWN
    return _tv_instance


# ── Core fetch ────────────────────────────────────────────────────────────────

def _fetch_one(symbol: str, exchange: str, n_bars: int = 5) -> dict | None:
    """Fetch latest price for one TradingView symbol. Returns price dict or None."""
    cache_key = f"{exchange}:{symbol}"
    data, age = _cache_read(cache_key)
    if data is not None and age is not None and age < _CACHE_TTL:
        return data                       # fresh cache hit

    # While rate-limited, never touch the network — serve last-good (flagged stale) or
    # None → caller falls back to yfinance. This is what stops the 429 storms.
    if _in_cooldown():
        return _serve_stale(data, age)

    # This symbol just failed repeatedly — don't re-hammer it until the window elapses.
    if _is_neg(cache_key):
        return _serve_stale(data, age)

    tv = _get_tv()
    if tv is None:
        return _serve_stale(data, age)    # serve stale if we have it

    try:
        _throttle()
        df = tv.get_hist(
            symbol=symbol,
            exchange=exchange,
            interval=Interval.in_1_minute,
            n_bars=n_bars,
        )
        if df is None or df.empty:
            _record_fail(cache_key)       # neg-cache only after _NEG_THRESHOLD fails
            return _serve_stale(data, age)  # serve stale instead of dropping the symbol

        row      = df.iloc[-1]
        prev_row = df.iloc[-2] if len(df) >= 2 else None
        close    = float(row["close"])
        prev_cls = float(prev_row["close"]) if prev_row is not None else close
        chg      = round((close - prev_cls) / prev_cls * 100, 2) if prev_cls > 0 else 0.0

        out = {
            "price":  round(close, 2),
            "open":   round(float(row["open"]), 2),
            "high":   round(float(row["high"]), 2),
            "low":    round(float(row["low"]),  2),
            "volume": int(row.get("volume", 0) or 0),
            "change": chg,
            "arrow":  "▲" if chg > 0 else "▼" if chg < 0 else "–",
        }
        _cache_write(cache_key, out)
        _record_ok(cache_key)             # recovered: reset fail count + clear neg
        _clear_cooldown()                 # success → reset backoff (if window elapsed)
        return out

    except Exception as e:
        msg = str(e)
        if "429" in msg or "too many requests" in msg.lower():
            _trigger_cooldown("HTTP 429")
        else:
            print(f"[tvdata] fetch failed {exchange}:{symbol} — {e}", flush=True)
        _record_fail(cache_key)           # neg-cache only after _NEG_THRESHOLD fails
        return _serve_stale(data, age)    # serve stale (flagged) on any failure


def _fetch_many(symbols_map: dict, max_workers: int = None) -> dict:
    """
    Fetch multiple symbols concurrently.
    symbols_map = {"LABEL": ("SYMBOL", "EXCHANGE"), ...}
    Returns {"LABEL": price_dict, ...}
    """
    workers = max_workers or _MAX_WORKERS
    # Pacing serializes calls, so the wall-clock floor scales with symbol count.
    # Budget for it (capped) instead of aborting mid-batch on a fixed 8s timeout.
    deadline = min(45.0, max(8.0, len(symbols_map) * _MIN_GAP + 8.0))
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_one, sym, exch): label
            for label, (sym, exch) in symbols_map.items()
        }
        try:
            for fut in as_completed(futures, timeout=deadline):
                label = futures[fut]
                try:
                    data = fut.result()
                    if data:
                        results[label] = data
                except Exception:
                    pass
        except _FTimeout:
            # Return whatever completed; stragglers serve stale next cycle.
            pass
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def get_nse_indices() -> dict:
    """
    Returns NSE indices with live prices.
    Format: {"NIFTY50": {price, change, arrow, open, high, low}, ...}
    Falls back to yfinance if tvdatafeed unavailable.
    """
    if _TV_AVAILABLE:
        data = _fetch_many(NSE_INDICES)
        if data:
            gc.collect()
            return data
    # yfinance fallback
    return _yf_fallback_indices()


def get_nse_sectors() -> dict:
    """
    Returns NSE sector index prices.
    Format: {"IT": {price, change, arrow, ...}, "BANKING": {...}, ...}
    """
    if _TV_AVAILABLE:
        data = _fetch_many(NSE_SECTORS)
        if data:
            gc.collect()
            return data
    return {}


def get_nse_stocks(sectors: list = None) -> dict:
    """
    Returns top NSE stocks for given sectors (or all sectors if None).
    Format: {"IT": [{"symbol": "TCS", "price": 3800, ...}, ...], ...}
    """
    sectors = sectors or list(NSE_STOCKS.keys())
    if not _TV_AVAILABLE:
        return {}

    all_symbols = {}
    for sec in sectors:
        for sym in NSE_STOCKS.get(sec, []):
            all_symbols[f"{sec}:{sym}"] = (sym, "NSE")

    raw = _fetch_many(all_symbols)

    result = {sec: [] for sec in sectors}
    for key, data in raw.items():
        sec, sym = key.split(":", 1)
        result[sec].append({"symbol": sym, **data})

    # Sort each sector by absolute change descending
    for sec in result:
        result[sec].sort(key=lambda x: abs(x.get("change", 0)), reverse=True)

    gc.collect()
    return result


def get_all_indices() -> dict:
    """
    Returns NSE + global indices merged.
    NSE via tvdatafeed, global via yfinance.
    """
    nse    = get_nse_indices()
    global_ = _yf_global()
    return {**nse, **global_}


def get_price(symbol: str, exchange: str = "NSE") -> dict | None:
    """Single symbol lookup. e.g. get_price('TCS', 'NSE')"""
    return _fetch_one(symbol, exchange)


# ── yfinance fallbacks ────────────────────────────────────────────────────────

def _yf_fallback_indices() -> dict:
    """Fallback: NSE indices via yfinance fast_info."""
    try:
        import yfinance as yf
        YF_NSE = {
            "NIFTY50":   "^NSEI",
            "BANKNIFTY": "^NSEBANK",
            "SENSEX":    "^BSESN",
        }
        data = {}
        for label, ticker in YF_NSE.items():
            try:
                fi   = yf.Ticker(ticker).fast_info
                last = float(fi.last_price)
                prev = float(fi.previous_close)
                if last > 0 and prev > 0:
                    chg = round((last - prev) / prev * 100, 2)
                    data[label] = {
                        "price": round(last, 2), "change": chg,
                        "arrow": "▲" if chg > 0 else "▼",
                    }
            except Exception:
                pass
        return data
    except Exception:
        return {}


def _yf_global() -> dict:
    """Global indices via yfinance fast_info."""
    try:
        import yfinance as yf
        data = {}
        for label, ticker in GLOBAL_YF.items():
            try:
                fi   = yf.Ticker(ticker).fast_info
                last = float(fi.last_price)
                prev = float(fi.previous_close)
                if last > 0 and prev > 0:
                    chg = round((last - prev) / prev * 100, 2)
                    data[label] = {
                        "price": round(last, 2), "change": chg,
                        "arrow": "▲" if chg > 0 else "▼",
                    }
            except Exception:
                pass
        return data
    except Exception:
        return {}
