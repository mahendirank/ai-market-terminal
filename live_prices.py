"""
live_prices.py — Unified real-time price feed.

Source priority (best accuracy first):
  1. Stooq.com    — free spot prices, matches TradingView (daily limit: ~500 req)
  2. Swissquote   — institutional spot bid/ask mid (no limit, no key needed)
  3. yfinance     — 15-min delayed; futures for commodities
  4. FRED         — authoritative for US bond yields

Stooq is blocked for the rest of the day once limit is hit.
Swissquote covers Gold/Silver/major FX spot.
yfinance covers indices, crypto, VIX, bonds, and EM FX.
"""
import time, threading, gc, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ── Rate-limit state ──────────────────────────────────────────────────────────
# Stooq's "Exceeded" daily-limit response used to flip _stooq_blocked permanently
# for the process lifetime, leaving the terminal on yfinance-fallback (which has
# the futures-rollover prev_close bug) for hours. Now the block carries a
# timestamp and expires after _STOOQ_COOLDOWN_SECS, so a transient rate-limit
# stops being a half-day data-quality outage.
_stooq_blocked      = False
_stooq_blocked_at   = 0.0        # epoch when the block was set
_STOOQ_COOLDOWN_SECS = 30 * 60   # 30 min — long enough to honour the limit,
                                  # short enough that the block doesn't span a session
_stooq_lock         = threading.Lock()
STOOQ_HEADERS   = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SQ_HEADERS      = {"User-Agent": "Mozilla/5.0"}


def _stooq_check_unblock() -> None:
    """Clear the Stooq block if the cooldown has elapsed. Cheap O(1)."""
    global _stooq_blocked, _stooq_blocked_at
    if not _stooq_blocked:
        return
    if (time.time() - _stooq_blocked_at) < _STOOQ_COOLDOWN_SECS:
        return
    with _stooq_lock:
        if _stooq_blocked and (time.time() - _stooq_blocked_at) >= _STOOQ_COOLDOWN_SECS:
            _stooq_blocked = False
            _stooq_blocked_at = 0.0
            print("[live_prices] Stooq cooldown elapsed — re-enabling Stooq fetches", flush=True)

# ── Symbol tables ─────────────────────────────────────────────────────────────

# Stooq spot symbols  (key, stooq_sym, multiplier)
STOOQ_MAP = {
    "GOLD":    ("xauusd",  1.0),
    "SILVER":  ("xagusd",  1.0),
    "CRUDE":   ("cl.f",    1.0),
    "NATGAS":  ("ng.f",    1.0),
    "COPPER":  ("hg.f",    0.01),   # Stooq gives cents/lb → $/lb
    "DXY":     ("dx.f",    1.0),
    "EURUSD":  ("eurusd",  1.0),
    "GBPUSD":  ("gbpusd",  1.0),
    "USDJPY":  ("usdjpy",  1.0),
    "USDINR":  ("usdinr",  1.0),
    "AUDUSD":  ("audusd",  1.0),
    "USDCAD":  ("usdcad",  1.0),
    "USDCNY":  ("usdcny",  1.0),
    "SPX":     ("^spx",    1.0),
    "NASDAQ":  ("^ndx",    1.0),
    "DAX":     ("^dax",    1.0),
    "HSI":     ("^hsi",    1.0),
}

# Swissquote forex-data-feed pairs  (key, base, quote)
SQ_MAP = {
    "GOLD":    ("XAU", "USD"),
    "SILVER":  ("XAG", "USD"),
    "EURUSD":  ("EUR", "USD"),
    "GBPUSD":  ("GBP", "USD"),
    "USDJPY":  ("USD", "JPY"),
    "AUDUSD":  ("AUD", "USD"),
    "USDCAD":  ("USD", "CAD"),
    "USDCNY":  ("USD", "CNH"),   # CNH ≈ CNY
}

# yfinance fallback  (key, yf_symbol)
YF_MAP = {
    "GOLD":     "GC=F",   "SILVER":  "SI=F",    "CRUDE":   "CL=F",
    "NATGAS":   "NG=F",   "COPPER":  "HG=F",
    "DXY":      "DX-Y.NYB","EURUSD": "EURUSD=X","GBPUSD":  "GBPUSD=X",
    "USDJPY":   "USDJPY=X","USDINR": "USDINR=X","AUDUSD":  "AUDUSD=X",
    "USDCAD":   "USDCAD=X","USDCNY": "USDCNY=X",
    "SPX":      "^GSPC",  "NASDAQ":  "^IXIC",   "DOW":     "^DJI",
    "DAX":      "^GDAXI", "FTSE":    "^FTSE",   "NIKKEI":  "^N225",   "HSI": "^HSI",
    "US_3M":    "^IRX",   "US_5Y":   "^FVX",    "US_10Y":  "^TNX",    "US_30Y": "^TYX",
    "BTC":      "BTC-USD","ETH":     "ETH-USD",
    "VIX":      "^VIX",   "INDIA_VIX": "^INDIAVIX",
    "NIFTY50":  "^NSEI",  "BANKNIFTY": "^NSEBANK","SENSEX": "^BSESN",
}

# Non-US sovereign 10Y yields via TradingView (tvdata.py).
# Exchange "TVC" hosts standard government bond yield symbols.
TV_BOND_MAP = {
    "JP_10Y": ("JP10Y", "TVC"),
    "DE_10Y": ("DE10Y", "TVC"),   # Bund
    "UK_10Y": ("GB10Y", "TVC"),   # Gilt
    "IN_10Y": ("IN10Y", "TVC"),
}

# Valid ranges to reject garbage
_VALID = {
    "GOLD":   (2000,9000),"SILVER":(10,200),"CRUDE":(20,200),"NATGAS":(0.5,20),"COPPER":(1,15),
    "DXY":(80,120),"EURUSD":(0.9,1.5),"GBPUSD":(1.0,1.7),"USDJPY":(100,175),
    "USDINR":(70,115),"AUDUSD":(0.5,0.9),"USDCAD":(1.0,1.6),"USDCNY":(6.0,8.0),
    "SPX":(2000,12000),"NASDAQ":(5000,35000),"DOW":(20000,65000),"DAX":(5000,25000),
    "FTSE":(5000,12000),"NIKKEI":(10000,80000),"HSI":(10000,40000),
    "NIFTY50":(10000,35000),"BANKNIFTY":(30000,80000),"SENSEX":(30000,95000),
    "US_3M":(0,8),"US_5Y":(0,8),"US_10Y":(0,8),"US_30Y":(0,8),"US_2Y":(0,8),
    "JP_10Y":(0,5),"DE_10Y":(0,8),"UK_10Y":(0,10),"IN_10Y":(4,12),
    "VIX":(5,90),"INDIA_VIX":(5,90),"BTC":(5000,300000),"ETH":(50,25000),
}

def _ok(name, val):
    lo, hi = _VALID.get(name, (None, None))
    return lo is None or lo <= float(val) <= hi

def _entry(price, prev, source, quality="OK"):
    if not price or price <= 0:
        return None
    chg = round((price - prev) / prev * 100, 3) if prev and prev > 0 else 0.0
    dp  = 4 if price < 10 else 3 if price < 100 else 2
    return {
        "price":  round(price, dp),
        "prev":   round(prev,  dp),
        "change": chg,
        "arrow":  "▲" if chg > 0 else "▼" if chg < 0 else "─",
        "source": source,
        "quality": quality,   # "OK" | "DEGRADED" — see prev_close_cache.reconcile_with_quality
    }

# ── Source 1: Stooq ───────────────────────────────────────────────────────────

def _stooq_one(name, sym, mult=1.0):
    global _stooq_blocked, _stooq_blocked_at
    _stooq_check_unblock()
    if _stooq_blocked:
        return None
    try:
        r = requests.get(
            f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv",
            headers=STOOQ_HEADERS, timeout=8
        )
        body = r.text.strip()
        if "Exceeded" in body or "limit" in body.lower():
            with _stooq_lock:
                _stooq_blocked = True
                _stooq_blocked_at = time.time()
            print("[live_prices] Stooq daily limit hit — cooling down for 30 min", flush=True)
            return None
        lines = [l for l in body.splitlines() if l and not l.startswith("Symbol") and "," in l]
        if not lines:
            return None
        parts = lines[-1].split(",")
        if len(parts) < 7:
            return None
        close = float(parts[6]) * mult
        open_ = float(parts[3]) * mult
        if close <= 0 or not _ok(name, close):
            return None
        # Stooq's intraday CSV gives today's OPEN as the only stable "prev"
        # reference. For commodity futures + FX that trade nearly 24h this is
        # ~= yesterday's close, so it's the most reliable day-change anchor
        # we have. Publish it to the prev_close cache so yfinance fallback
        # doesn't poison the change% with its stale futures-rollover prev
        # (yfinance reported CL=F prev=$96.60 while reality was $91.02).
        try:
            from prev_close_cache import put as _pc_put
            _pc_put(name, open_, source="stooq")
        except Exception:
            pass
        return _entry(close, open_, "stooq")
    except Exception:
        return None

def _stooq_batch(keys: list) -> dict:
    """Fetch symbols from Stooq with max 3 concurrent connections."""
    _stooq_check_unblock()
    if _stooq_blocked:
        return {}
    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {
            pool.submit(_stooq_one, k, STOOQ_MAP[k][0], STOOQ_MAP[k][1]): k
            for k in keys if k in STOOQ_MAP
        }
        for fut in futs:
            k = futs[fut]
            try:
                v = fut.result(timeout=15)
                if v:
                    results[k] = v
            except Exception:
                pass
    return results

# ── Source 2: Swissquote ──────────────────────────────────────────────────────

def _sq_prev_closes(keys: list) -> dict:
    """Get previous closes from yfinance for Swissquote instruments (for % change)."""
    out = {}
    try:
        import yfinance as yf
        for k in keys:
            if k not in YF_MAP:
                continue
            try:
                fi = yf.Ticker(YF_MAP[k]).fast_info
                out[k] = float(fi.previous_close)
            except Exception:
                pass
    except Exception:
        pass
    return out

def _sq_one(name, base, quote, prev=0.0):
    try:
        r = requests.get(
            f"https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{base}/{quote}",
            headers=SQ_HEADERS, timeout=7
        )
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list):
                p   = data[0].get("spreadProfilePrices", [{}])[0]
                bid = float(p.get("bid", 0))
                ask = float(p.get("ask", 0))
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2
                    if _ok(name, mid):
                        return _entry(mid, prev if prev > 0 else mid, "swissquote")
    except Exception:
        pass
    return None

def _sq_batch(keys: list) -> dict:
    """Fetch Swissquote spot prices — no rate limit. Includes % change via yfinance prev close."""
    results = {}
    # Get prev closes in parallel with SQ fetches
    with ThreadPoolExecutor(max_workers=6) as pool:
        prev_fut = pool.submit(_sq_prev_closes, keys)
        sq_futs  = {
            pool.submit(_sq_one, k, SQ_MAP[k][0], SQ_MAP[k][1]): k
            for k in keys if k in SQ_MAP
        }
        prevs = {}
        try:
            prevs = prev_fut.result(timeout=15) or {}
        except Exception:
            pass
        for fut in sq_futs:
            k = sq_futs[fut]
            try:
                raw = fut.result(timeout=10)
                if raw:
                    # patch in prev close for proper % change
                    if k in prevs and prevs[k] > 0:
                        raw = _entry(raw["price"], prevs[k], "swissquote")
                    results[k] = raw
            except Exception:
                pass
    return results

# ── Source 3: yfinance ────────────────────────────────────────────────────────

def _yf_one(name, sym):
    try:
        import yfinance as yf
        fi   = yf.Ticker(sym).fast_info
        last = float(fi.last_price)
        prev = float(fi.previous_close)
        if last > 0 and _ok(name, last):
            quality = "OK"
            try:
                from prev_close_cache import reconcile_with_quality as _pc_rec
                prev, quality = _pc_rec(name, prev, max_drift_pct=5.0)
            except Exception:
                pass
            return _entry(last, prev, "yfinance", quality=quality)
    except Exception:
        pass
    return None

def _yf_batch(keys: list) -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {
            pool.submit(_yf_one, k, YF_MAP[k]): k
            for k in keys if k in YF_MAP
        }
        for fut in futs:
            k = futs[fut]
            try:
                v = fut.result(timeout=20)
                if v:
                    results[k] = v
            except Exception:
                pass
    return results

# ── Source 3b: TradingView (tvdata) — non-US sovereign yields ────────────────

def _tv_bond_one(name, sym, exch):
    # tvdatafeed often SSL-times-out on the first call per process — retry
    # twice with short backoff so JGB/Bund/Gilt/India don't drop on cold-start.
    try:
        from tvdata import _fetch_one
    except Exception:
        return None
    d = None
    for attempt in range(3):
        try:
            d = _fetch_one(sym, exch, n_bars=2)
        except Exception:
            d = None
        if d:
            break
        time.sleep(0.4)
    if not d:
        return None
    try:
        price = float(d.get("price", 0))
        change_pct = float(d.get("change", 0))
        prev = price / (1 + change_pct/100.0) if change_pct else price
        if price > 0 and _ok(name, price):
            return _entry(price, prev, "tvdata")
    except Exception:
        pass
    return None

def _tv_bond_batch(keys: list) -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {
            pool.submit(_tv_bond_one, k, TV_BOND_MAP[k][0], TV_BOND_MAP[k][1]): k
            for k in keys if k in TV_BOND_MAP
        }
        for fut in futs:
            k = futs[fut]
            try:
                v = fut.result(timeout=12)
                if v:
                    results[k] = v
            except Exception:
                pass
    return results

# ── Source 4: FRED ────────────────────────────────────────────────────────────

def _fred_yield(name, series_id):
    try:
        r = requests.get(
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
            timeout=8, headers=STOOQ_HEADERS
        )
        lines = [l for l in r.text.strip().splitlines()
                 if not l.startswith("DATE") and "." in l and "," in l]
        if len(lines) >= 2:
            prev = float(lines[-2].split(",")[1])
            val  = float(lines[-1].split(",")[1])
            if _ok(name, val):
                return _entry(val, prev, "fred")
    except Exception:
        pass
    return None

# ── NSE via tvdatafeed + yfinance fallback ────────────────────────────────────

def _fetch_nse() -> dict:
    out = {}
    import yfinance as yf
    for label in ["NIFTY50", "BANKNIFTY", "SENSEX", "INDIA_VIX"]:
        sym = YF_MAP.get(label)
        if not sym:
            continue
        try:
            fi   = yf.Ticker(sym).fast_info
            last = float(fi.last_price)
            prev = float(fi.previous_close)
            if last > 0 and _ok(label, last):
                out[label] = _entry(last, prev, "yfinance")
        except Exception:
            pass
    return out

# ── Main fetch ────────────────────────────────────────────────────────────────

# All keys by category
_CMDTY_KEYS  = ["GOLD","SILVER","CRUDE","NATGAS","COPPER"]
_FX_KEYS     = ["DXY","EURUSD","GBPUSD","USDJPY","USDINR","AUDUSD","USDCAD","USDCNY"]
_GLOBAL_KEYS = ["SPX","NASDAQ","DAX","HSI","DOW","FTSE","NIKKEI"]
_BOND_KEYS   = ["US_3M","US_5Y","US_10Y","US_30Y","US_2Y"]
_TV_BOND_KEYS = ["JP_10Y","DE_10Y","UK_10Y","IN_10Y"]
_CRYPTO_KEYS = ["BTC","ETH"]
_VIX_KEYS    = ["VIX"]


def _fetch_all() -> dict:
    result = {
        "indices":     {},
        "global":      {},
        "fx":          {},
        "bonds":       {},
        "commodities": {},
        "crypto":      {},
        "vix":         {},
        "ts":          datetime.now(IST).strftime("%H:%M:%S IST"),
        "ts_epoch":    time.time(),
        "stooq_ok":    not _stooq_blocked,
    }

    # Keys we still need after trying each source
    needed_cmdty  = set(_CMDTY_KEYS)
    needed_fx     = set(_FX_KEYS)
    needed_global = set(_GLOBAL_KEYS)

    with ThreadPoolExecutor(max_workers=6) as outer:
        # All sources run in parallel
        fut_nse    = outer.submit(_fetch_nse)
        fut_stooq  = outer.submit(_stooq_batch, _CMDTY_KEYS + _FX_KEYS + ["SPX","NASDAQ","DAX","HSI"])
        fut_yf_glo = outer.submit(_yf_batch, ["DOW","FTSE","NIKKEI","HSI"])
        fut_yf_bnd = outer.submit(_yf_batch, _BOND_KEYS[:-1])   # US_2Y from FRED
        fut_tv_bnd = outer.submit(_tv_bond_batch, _TV_BOND_KEYS)  # JGB/Bund/Gilt/India
        fut_yf_cry = outer.submit(_yf_batch, _CRYPTO_KEYS)
        fut_yf_vix = outer.submit(_yf_batch, _VIX_KEYS)
        fut_fred   = outer.submit(_fred_yield, "US_2Y", "DGS2")

        # NSE
        try:
            for k, v in (fut_nse.result(timeout=35) or {}).items():
                result["indices"][k] = v
        except: pass

        # Stooq (spot — best accuracy)
        stooq_got = {}
        try:
            stooq_got = fut_stooq.result(timeout=20) or {}
        except: pass

        for k in _CMDTY_KEYS:
            if k in stooq_got:
                result["commodities"][k] = stooq_got[k]
                needed_cmdty.discard(k)
        for k in _FX_KEYS:
            if k in stooq_got:
                result["fx"][k] = stooq_got[k]
                needed_fx.discard(k)
        for k in ["SPX","NASDAQ","DAX","HSI"]:
            if k in stooq_got:
                result["global"][k] = stooq_got[k]
                needed_global.discard(k)

    # Round 2: Swissquote for anything Stooq missed (Gold/Silver/FX spot)
    sq_keys = ([k for k in needed_cmdty if k in SQ_MAP] +
               [k for k in needed_fx    if k in SQ_MAP])
    if sq_keys:
        sq_got = _sq_batch(sq_keys)
        for k in list(needed_cmdty):
            if k in sq_got:
                result["commodities"][k] = sq_got[k]
                needed_cmdty.discard(k)
        for k in list(needed_fx):
            if k in sq_got:
                result["fx"][k] = sq_got[k]
                needed_fx.discard(k)

    # Round 3: yfinance for anything still missing
    yf_still = list(needed_cmdty) + list(needed_fx) + list(needed_global)
    if yf_still:
        yf_got = _yf_batch(yf_still)
        for k in list(needed_cmdty):
            if k in yf_got: result["commodities"][k] = yf_got[k]
        for k in list(needed_fx):
            if k in yf_got: result["fx"][k] = yf_got[k]
        for k in list(needed_global):
            if k in yf_got: result["global"][k] = yf_got[k]

    # Collect remaining (bonds, crypto, VIX, global indices from outer futures)
    try:
        bnd = fut_yf_bnd.result(timeout=5) or {}
        for k,v in bnd.items():
            result["bonds"][k] = v
    except: pass
    try:
        fred2y = fut_fred.result(timeout=5)
        if fred2y: result["bonds"]["US_2Y"] = fred2y
    except: pass
    try:
        tvbnd = fut_tv_bnd.result(timeout=15) or {}
        for k,v in tvbnd.items():
            result["bonds"][k] = v
    except: pass
    try:
        for k,v in (fut_yf_cry.result(timeout=5) or {}).items():
            result["crypto"][k] = v
    except: pass
    try:
        for k,v in (fut_yf_vix.result(timeout=5) or {}).items():
            result["vix"][k] = v
    except: pass
    try:
        for k,v in (fut_yf_glo.result(timeout=5) or {}).items():
            if k not in result["global"]:
                result["global"][k] = v
    except: pass

    # Move INDIA_VIX from indices → vix panel
    if "INDIA_VIX" in result["indices"]:
        result["vix"]["INDIA_VIX"] = result["indices"].pop("INDIA_VIX")

    gc.collect()
    return result


_lp_cache      = {"data": None, "ts": 0.0}
_lp_cache_lock = threading.Lock()
_LP_TTL        = 30  # seconds — regime engine and ticker reuse this

def get_live_prices(force: bool = False) -> dict:
    now = time.time()
    with _lp_cache_lock:
        if not force and _lp_cache["data"] and (now - _lp_cache["ts"]) < _LP_TTL:
            return _lp_cache["data"]
    result = _fetch_all()
    with _lp_cache_lock:
        _lp_cache["data"] = result
        _lp_cache["ts"]   = time.time()
    return result


def get_ticker_items() -> list:
    d = get_live_prices()
    items = []
    for cat, grp in [("NSE",d["indices"]),("GLOBAL",d["global"]),("FX",d["fx"]),
                      ("CMDTY",d["commodities"]),("BONDS",d["bonds"]),
                      ("CRYPTO",d["crypto"]),("VIX",d["vix"])]:
        for sym, v in grp.items():
            if v:
                items.append({"symbol":sym,"price":v.get("price",0),
                               "change":v.get("change",0),"arrow":v.get("arrow","─"),
                               "category":cat,"source":v.get("source","")})
    return items
