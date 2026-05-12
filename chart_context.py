"""
chart_context.py — Per-asset sidebar context for the Charts panel.

For each chart-able asset, returns:
  - AI commentary (best-fit explainer entry, or fresh analyst sentence)
  - Macro regime overlay (current state of relevant dimensions)
  - Support / resistance zones (computed from recent OHLC via yfinance)
  - Volatility indicator (HV20 + ATR + 1d range)
  - Relevant central bank events from cb_calendar
  - Live price + change

Uses yfinance for OHLC (already a project dependency).
Cached 5 minutes — S/R and HV are intraday-stable.
"""
import os
import time
import threading
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# Asset → (display, yf_ticker, tradingview_symbol, related_cb_codes, live_price_path)
CHART_ASSETS = {
    "GOLD":   {"display": "Gold (XAU/USD)",  "yf": "GC=F",       "tv": "OANDA:XAUUSD",      "cb": ["FED"],                       "live": ("commodities", "GOLD")},
    "DXY":    {"display": "US Dollar Index", "yf": "DX-Y.NYB",   "tv": "TVC:DXY",           "cb": ["FED"],                       "live": ("fx", "DXY")},
    "EURUSD": {"display": "EUR/USD",         "yf": "EURUSD=X",   "tv": "OANDA:EURUSD",      "cb": ["FED", "ECB"],                "live": None,  "fx_pair": "EUR/USD"},
    "USDJPY": {"display": "USD/JPY",         "yf": "USDJPY=X",   "tv": "OANDA:USDJPY",      "cb": ["FED", "BOJ"],                "live": None,  "fx_pair": "USD/JPY"},
    "NASDAQ": {"display": "NASDAQ 100",      "yf": "^NDX",       "tv": "NASDAQ:NDX",        "cb": ["FED"],                       "live": ("global", "NASDAQ")},
    "BTC":    {"display": "Bitcoin",         "yf": "BTC-USD",    "tv": "BINANCE:BTCUSDT",   "cb": ["FED"],                       "live": ("crypto", "BTC")},
    "OIL":    {"display": "Crude Oil (WTI)", "yf": "CL=F",       "tv": "TVC:USOIL",         "cb": ["FED"],                       "live": ("commodities", "CRUDE")},
}

_CACHE_TTL = 300   # 5 min — S/R and HV are intraday-stable
_cache: dict = {}
_cache_lock = threading.Lock()


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
        if cur is None: return default
    return cur


# ─── Support / Resistance from recent OHLC ───────────────────────────────────

def _compute_sr_levels(yf_ticker: str, current_price: float) -> dict:
    """Pull last 60 daily candles, identify swing highs/lows clustered near price."""
    try:
        import yfinance as yf
        hist = yf.Ticker(yf_ticker).history(period="60d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return {"resistance": [], "support": [], "atr14": None, "range_1d_pct": None}

        highs = hist["High"].dropna().tolist()
        lows  = hist["Low"].dropna().tolist()
        closes = hist["Close"].dropna().tolist()
        if not highs or not lows or not closes or current_price <= 0:
            return {"resistance": [], "support": [], "atr14": None, "range_1d_pct": None}

        # Swing pivots: a high/low surrounded by lower/higher bars (window=2)
        pivot_highs = []
        pivot_lows  = []
        for i in range(2, len(highs) - 2):
            if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                pivot_highs.append(highs[i])
            if lows[i]  < lows[i-1]  and lows[i]  < lows[i-2]  and lows[i]  < lows[i+1]  and lows[i]  < lows[i+2]:
                pivot_lows.append(lows[i])

        # Add absolute highs/lows for reference
        pivot_highs.append(max(highs))
        pivot_lows.append(min(lows))

        # Cluster levels within 0.4% of each other → take cluster average
        def _cluster(levels, tol_pct=0.4):
            if not levels: return []
            levels_sorted = sorted(levels)
            clusters = [[levels_sorted[0]]]
            for v in levels_sorted[1:]:
                if abs(v - clusters[-1][-1]) / max(clusters[-1][-1], 1e-9) * 100 < tol_pct:
                    clusters[-1].append(v)
                else:
                    clusters.append([v])
            return [round(sum(c) / len(c), 5) for c in clusters]

        clustered_highs = _cluster(pivot_highs)
        clustered_lows  = _cluster(pivot_lows)

        # Resistance = clustered highs ABOVE current price; Support = below
        resistance = sorted([h for h in clustered_highs if h > current_price])[:3]
        support    = sorted([l for l in clustered_lows  if l < current_price], reverse=True)[:3]

        # ATR-14 (simple)
        n = min(14, len(highs) - 1)
        trs = []
        for i in range(1, n + 1):
            tr = max(
                highs[-i] - lows[-i],
                abs(highs[-i] - closes[-i-1]) if i+1 <= len(closes) else 0,
                abs(lows[-i]  - closes[-i-1]) if i+1 <= len(closes) else 0,
            )
            trs.append(tr)
        atr14 = round(sum(trs) / len(trs), 5) if trs else None

        # 1-day range as % of close
        range_1d_pct = None
        if len(highs) >= 1 and len(lows) >= 1 and closes:
            r = (highs[-1] - lows[-1]) / closes[-1] * 100 if closes[-1] else None
            range_1d_pct = round(r, 2) if r is not None else None

        return {
            "resistance":   resistance,
            "support":      support,
            "atr14":        atr14,
            "range_1d_pct": range_1d_pct,
        }
    except Exception as e:
        print(f"[chart_context] sr error for {yf_ticker}: {e}", flush=True)
        return {"resistance": [], "support": [], "atr14": None, "range_1d_pct": None}


# ─── Volatility regime ────────────────────────────────────────────────────────

def _classify_volatility(atr14: float | None, current_price: float, range_1d_pct: float | None) -> dict:
    """Tag volatility state: LOW / NORMAL / ELEVATED / EXTREME."""
    if range_1d_pct is None:
        return {"state": "NORMAL", "label": "—"}
    r = abs(range_1d_pct)
    if   r < 0.4:  state = "LOW"
    elif r < 0.9:  state = "NORMAL"
    elif r < 1.6:  state = "ELEVATED"
    else:          state = "EXTREME"
    label = f"{r:.2f}% intraday range"
    if atr14 and current_price:
        atr_pct = atr14 / current_price * 100
        label += f" · ATR14 ≈ {atr_pct:.2f}%"
    return {"state": state, "label": label}


# ─── Live price extraction ───────────────────────────────────────────────────

def _get_live_price(asset_def: dict) -> dict:
    if asset_def.get("fx_pair"):
        try:
            from forex import get_forex_intel
            fx = get_forex_intel() or {}
            p = (fx.get("pairs") or {}).get(asset_def["fx_pair"], {})
            return {"price": float(p.get("price") or 0), "change_pct": float(p.get("change_pct") or 0)}
        except Exception:
            return {"price": 0, "change_pct": 0}
    cat, key = asset_def["live"]
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        v = lp.get(cat, {}).get(key, {})
        return {"price": float((v or {}).get("price") or 0), "change_pct": float((v or {}).get("change") or 0)}
    except Exception:
        return {"price": 0, "change_pct": 0}


# ─── Regime overlay (which dimensions matter for this asset) ─────────────────

def _regime_overlay_for(asset_key: str) -> dict:
    """Pick the regime dimensions most relevant to this asset."""
    try:
        from macro_desk import get_macro_regime_view
        view = get_macro_regime_view() or {}
        dims = view.get("dimensions", {}) or {}
    except Exception:
        return {"primary": [], "commentary": ""}

    # Per-asset relevance map
    relevance = {
        "GOLD":   ["dollar", "yields", "inflation", "risk"],
        "DXY":    ["dollar", "fed", "yields", "risk"],
        "EURUSD": ["dollar", "fed", "yields"],
        "USDJPY": ["yields", "fed", "risk"],
        "NASDAQ": ["risk", "yields", "fed"],
        "BTC":    ["risk", "dollar"],
        "OIL":    ["commodities", "inflation", "dollar"],
    }
    keys = relevance.get(asset_key, ["risk", "dollar"])
    primary = []
    for k in keys:
        d = dims.get(k)
        if d:
            primary.append({
                "name":       k,
                "state":      d.get("state"),
                "confidence": d.get("confidence"),
                "driver":     d.get("driver"),
            })
    return {
        "primary":          primary,
        "commentary":       view.get("commentary", ""),
        "dominant_driver":  view.get("dominant_driver", ""),
        "overall_conf":     view.get("overall_confidence", 0),
    }


# ─── Latest AI commentary for this asset ─────────────────────────────────────

def _latest_explainer_for(asset_key: str) -> dict | None:
    try:
        from explainer import get_recent_explanations
        recents = get_recent_explanations(limit=8, asset=asset_key)
        if not recents: return None
        e = recents[0]
        return {
            "ts":             e.get("ts_ist"),
            "what_moved":     e.get("what_moved"),
            "why_it_moved":   e.get("why_it_moved"),
            "tags":           e.get("tags", []),
            "confidence":     e.get("confidence"),
            "forward":        e.get("forward_implic"),
        }
    except Exception:
        return None


# ─── Relevant CB events for this asset ───────────────────────────────────────

def _relevant_cb_events(asset_key: str, asset_def: dict) -> list:
    try:
        from cb_calendar import get_cb_calendar
        cal = get_cb_calendar(days_ahead=60, limit=12) or {}
        codes = set(asset_def.get("cb", []))
        return [{
            "cb":         e["cb"],
            "flag":       e["flag"],
            "date":       e["date_display"],
            "time":       e["time_ist"],
            "days_to":    e["days_to_event"],
            "label":      e["label"],
            "vol":        e["volatility"],
            "bias":       e["expected_bias"],
        } for e in (cal.get("events") or []) if e["cb"] in codes][:5]
    except Exception:
        return []


# ─── Public entry point ──────────────────────────────────────────────────────

def get_chart_context(asset_key: str) -> dict:
    asset_key = asset_key.upper()
    asset_def = CHART_ASSETS.get(asset_key)
    if not asset_def:
        return {"error": f"Unknown asset: {asset_key}"}

    # Cache check
    now = time.time()
    cached = _cache.get(asset_key)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        return cached["data"]

    with _cache_lock:
        cached = _cache.get(asset_key)
        if cached and (now - cached["ts"]) < _CACHE_TTL:
            return cached["data"]

        live  = _get_live_price(asset_def)
        sr    = _compute_sr_levels(asset_def["yf"], live["price"])
        vol   = _classify_volatility(sr.get("atr14"), live["price"], sr.get("range_1d_pct"))
        regime = _regime_overlay_for(asset_key)
        explainer = _latest_explainer_for(asset_key)
        cb = _relevant_cb_events(asset_key, asset_def)

        data = {
            "asset_key":       asset_key,
            "display":         asset_def["display"],
            "tv_symbol":       asset_def["tv"],
            "generated_at":    datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
            "live":            live,
            "support":         sr.get("support", []),
            "resistance":      sr.get("resistance", []),
            "atr14":           sr.get("atr14"),
            "range_1d_pct":    sr.get("range_1d_pct"),
            "volatility":      vol,
            "regime":          regime,
            "ai_commentary":   explainer,
            "cb_events":       cb,
        }
        _cache[asset_key] = {"data": data, "ts": now}
        return data


def get_chart_assets() -> list:
    return [{
        "key":     k,
        "display": v["display"],
        "tv":      v["tv"],
    } for k, v in CHART_ASSETS.items()]
