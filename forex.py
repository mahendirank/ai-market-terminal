"""
forex.py — Live FX major prices + macro-inferred direction signals.

For each of 6 major pairs returns:
  price, change %, direction (BULLISH/BEARISH/NEUTRAL), confidence,
  macro driver (1-3 word descriptor), volatility state.

Data sources, in priority:
  1. TwelveData (if TWELVEDATA_API_KEY env var set)
  2. Polygon.io  (if POLYGON_API_KEY env var set)
  3. yfinance    (fallback — no key required)

Inference uses DXY trend, US 10Y yield, regime state (CB DOVISH/HAWKISH,
RISK ON/OFF, COMMODITY SUPERCYCLE), commodity prices, VIX, and the pair's
own recent move for sanity-checking.

Does not touch news, gold, regime, or any other existing module.
"""
import os
import time
import threading
from typing import Dict
from concurrent.futures import ThreadPoolExecutor

import requests

# (display name, yfinance ticker, twelvedata/polygon symbol, base, quote)
PAIRS = [
    ("EUR/USD", "EURUSD=X", "EUR/USD", "EUR", "USD"),
    ("GBP/USD", "GBPUSD=X", "GBP/USD", "GBP", "USD"),
    ("USD/JPY", "USDJPY=X", "USD/JPY", "USD", "JPY"),
    ("AUD/USD", "AUDUSD=X", "AUD/USD", "AUD", "USD"),
    ("USD/CAD", "USDCAD=X", "USD/CAD", "USD", "CAD"),
    ("USD/CHF", "USDCHF=X", "USD/CHF", "USD", "CHF"),
]

_CACHE_TTL    = 30   # seconds
_cache        = {"data": None, "ts": 0.0}
_cache_lock   = threading.Lock()


# ─── Price fetchers ───────────────────────────────────────────────────────────

def _fetch_twelvedata(symbol: str) -> dict | None:
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.twelvedata.com/quote",
            params={"symbol": symbol, "apikey": key},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        j = r.json()
        price = float(j.get("close") or 0)
        if price <= 0:
            return None
        return {
            "price":      round(price, 5),
            "change":     float(j.get("change") or 0),
            "change_pct": float(j.get("percent_change") or 0),
            "high":       float(j.get("high") or 0),
            "low":        float(j.get("low") or 0),
            "source":     "twelvedata",
        }
    except Exception:
        return None


def _fetch_polygon(symbol: str) -> dict | None:
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        return None
    ticker = "C:" + symbol.replace("/", "")
    try:
        # Last quote
        r = requests.get(
            f"https://api.polygon.io/v3/quotes/{ticker}",
            params={"apiKey": key, "limit": 1},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        results = r.json().get("results", [])
        if not results:
            return None
        q = results[0]
        bid = float(q.get("bid_price") or 0)
        ask = float(q.get("ask_price") or 0)
        price = (bid + ask) / 2 if (bid and ask) else (bid or ask)
        if price <= 0:
            return None
        # Polygon doesn't return % change in this call — compute none, leave 0
        return {
            "price":      round(price, 5),
            "change":     0,
            "change_pct": 0,
            "high":       0, "low": 0,
            "source":     "polygon",
        }
    except Exception:
        return None


def _fetch_yfinance(yf_ticker: str) -> dict | None:
    """Fallback that always works (no API key)."""
    try:
        import yfinance as yf
        hist = yf.Ticker(yf_ticker).history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) > 1 else last
        price = float(last["Close"])
        prev_close = float(prev["Close"])
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0
        return {
            "price":      round(price, 5),
            "change":     round(change, 5),
            "change_pct": round(change_pct, 3),
            "high":       float(last["High"]),
            "low":        float(last["Low"]),
            "source":     "yfinance",
        }
    except Exception:
        return None


def _fetch_one(pair_tuple) -> tuple:
    display, yf_t, sym, _, _ = pair_tuple
    for fn, arg in ((_fetch_twelvedata, sym), (_fetch_polygon, sym), (_fetch_yfinance, yf_t)):
        data = fn(arg)
        if data and data["price"] > 0:
            return display, data
    return display, {"price": 0, "change": 0, "change_pct": 0, "high": 0, "low": 0, "source": "unavailable"}


def get_forex_prices() -> Dict[str, dict]:
    """Live prices for all 6 majors. Cached 30s, single-flight."""
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]
    with _cache_lock:
        now = time.time()
        if _cache["data"] and (now - _cache["ts"]) < _CACHE_TTL:
            return _cache["data"]
        result = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            for display, data in pool.map(_fetch_one, PAIRS):
                result[display] = data
        _cache["data"] = result
        _cache["ts"]   = time.time()
        return result


# ─── Macro inference ──────────────────────────────────────────────────────────

def _gather_macro_context() -> dict:
    """Pull DXY change, yields, commodities, VIX, regime — all sources tolerant of failure."""
    ctx = {"dxy_chg": 0, "us10y_chg": 0, "us10y_lvl": 4.2,
           "gold_chg": 0, "oil_chg": 0, "vix": 20.0,
           "btc_chg": 0, "regime": "neutral"}
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        def chg(cat, key, default=0):
            v = lp.get(cat, {}).get(key, {})
            try: return float(v.get("change", default) or default)
            except: return default
        def lvl(cat, key, default=0):
            v = lp.get(cat, {}).get(key, {})
            try: return float(v.get("price", default) or default)
            except: return default
        ctx["dxy_chg"]   = chg("fx", "DXY")
        ctx["gold_chg"]  = chg("commodities", "GOLD")
        ctx["oil_chg"]   = chg("commodities", "CRUDE")
        ctx["us10y_chg"] = chg("bonds", "US_10Y")
        ctx["us10y_lvl"] = lvl("bonds", "US_10Y", 4.2)
        ctx["vix"]       = lvl("vix", "VIX", 20.0)
        ctx["btc_chg"]   = chg("crypto", "BTC")
    except Exception as e:
        print(f"[forex] live_prices error: {e}", flush=True)
    try:
        from regime import detect_market_regime
        r = detect_market_regime() or {}
        ctx["regime"] = r.get("regime", "neutral")
    except Exception as e:
        print(f"[forex] regime error: {e}", flush=True)
    return ctx


def _infer_pair(pair: str, price_data: dict, ctx: dict) -> dict:
    """Direction + confidence + driver for one pair, given macro context."""
    dxy  = ctx["dxy_chg"]
    y10c = ctx["us10y_chg"]
    gold = ctx["gold_chg"]
    oil  = ctx["oil_chg"]
    vix  = ctx["vix"]
    reg  = ctx["regime"]
    own  = price_data.get("change_pct", 0)

    score = 0          # +ve = bullish for BASE currency in pair
    drivers = []

    if pair == "EUR/USD":
        if dxy <= -0.2: score += 25; drivers.append("DXY weakening")
        elif dxy >=  0.2: score -= 25; drivers.append("DXY strong")
        if reg == "central_bank_dovish":  score += 18; drivers.append("Fed dovish")
        if reg == "central_bank_hawkish": score -= 18; drivers.append("Fed hawkish")
        if reg in ("risk_on", "ai_growth_boom"): score += 8
        if reg in ("risk_off", "liquidity_crisis"): score -= 8
        if y10c >  0.4: score -= 8
        if y10c < -0.4: score += 8

    elif pair == "GBP/USD":
        if dxy <= -0.2: score += 22; drivers.append("DXY weakening")
        elif dxy >=  0.2: score -= 22; drivers.append("DXY strong")
        if reg == "central_bank_dovish":  score += 15
        if reg == "central_bank_hawkish": score -= 12
        if reg in ("risk_on", "ai_growth_boom"): score += 10; drivers.append("Risk on")
        if reg in ("risk_off", "recession_fear"): score -= 12; drivers.append("Risk off")

    elif pair == "USD/JPY":
        # USD/JPY rises = USD strong vs JPY safe-haven
        if y10c >  0.3: score += 25; drivers.append("US yields rising")
        elif y10c < -0.3: score -= 22; drivers.append("US yields falling")
        if vix > 25: score -= 18; drivers.append("VIX spike → JPY haven")
        elif vix < 15: score += 10; drivers.append("Carry trade")
        if reg in ("risk_off", "liquidity_crisis"): score -= 15
        if reg in ("risk_on",): score += 8
        if dxy >= 0.2: score += 10

    elif pair == "AUD/USD":
        if dxy <= -0.2: score += 16; drivers.append("DXY weakening")
        elif dxy >=  0.2: score -= 16
        if gold >  0.3 or oil >  0.5: score += 14; drivers.append("Commodity rally")
        if gold < -0.3 and oil < -0.3: score -= 12; drivers.append("Commodity slump")
        if reg == "commodity_supercycle": score += 18; drivers.append("Commodity supercycle")
        if reg == "inflationary":         score += 10; drivers.append("Inflationary → AUD bid")
        if reg in ("risk_on", "ai_growth_boom"): score += 10; drivers.append("Risk on")
        if reg in ("risk_off", "recession_fear", "liquidity_crisis"): score -= 18

    elif pair == "USD/CAD":
        # USD/CAD up = USD strong (and CAD weak — CAD is oil-linked)
        if oil >  0.5: score -= 22; drivers.append("Oil surge → CAD strong")
        elif oil < -0.5: score += 18; drivers.append("Oil weak → CAD weak")
        if dxy >=  0.2: score += 14; drivers.append("DXY strong")
        elif dxy <= -0.2: score -= 14
        if reg in ("inflationary", "commodity_supercycle"): score -= 8; drivers.append("Inflationary → CAD bid")
        if reg == "risk_off": score += 8
        if reg == "risk_on":  score -= 5

    elif pair == "USD/CHF":
        # USD/CHF rises = USD strong (CHF is haven)
        if dxy >=  0.2: score += 18; drivers.append("DXY strong")
        elif dxy <= -0.2: score -= 18; drivers.append("DXY weakening")
        if vix > 25 or reg in ("risk_off", "liquidity_crisis"):
            score -= 20; drivers.append("Risk off → CHF haven")
        if reg == "risk_on": score += 12; drivers.append("Risk on")

    # Direction — lower thresholds so weak-but-clear signals don't show NEUTRAL
    if score >=  15:
        direction, conf = "BULLISH",  min(58 + score // 2, 92)
    elif score <= -15:
        direction, conf = "BEARISH",  min(58 + (-score) // 2, 92)
    else:
        direction, conf = "NEUTRAL", max(40 + abs(score) // 2, 50)

    # Sanity check: pair already moving opposite to our call this session?
    if direction == "BULLISH" and own < -0.3:
        conf = max(conf - 12, 45)
    elif direction == "BEARISH" and own > 0.3:
        conf = max(conf - 12, 45)

    # Volatility state from own day's move magnitude
    abs_chg = abs(own)
    if   abs_chg < 0.3:  vol = "LOW"
    elif abs_chg < 0.7:  vol = "NORMAL"
    elif abs_chg < 1.3:  vol = "ELEVATED"
    else:                vol = "EXTREME"

    driver = drivers[0] if drivers else "Mixed signals"

    return {
        "direction":  direction,
        "confidence": int(conf),
        "driver":     driver,
        "vol":        vol,
        "all_drivers": drivers[:3],
    }


def get_forex_intel() -> dict:
    """Full enriched FX view: prices + macro-inferred signals for all 6 majors."""
    prices = get_forex_prices()
    ctx    = _gather_macro_context()
    pairs  = {}
    for display, _, _, _, _ in PAIRS:
        pd = prices.get(display, {})
        sig = _infer_pair(display, pd, ctx)
        pairs[display] = {**pd, **sig}
    return {
        "pairs":         pairs,
        "macro_context": {
            "dxy_chg":   round(ctx["dxy_chg"], 2),
            "us10y_chg": round(ctx["us10y_chg"], 2),
            "gold_chg":  round(ctx["gold_chg"], 2),
            "oil_chg":   round(ctx["oil_chg"], 2),
            "vix":       round(ctx["vix"], 1),
            "regime":    ctx["regime"],
        },
    }
