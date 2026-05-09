"""
regime.py — Institutional Market Regime Engine

Classifies the current market into one of 10 institutional regimes using:
  - Live price signals (DXY, NASDAQ, Gold, Crude, VIX, US_10Y) from live_prices cache
  - News keyword scoring from recent headlines

Cache: 60 seconds. Target latency: < 2 seconds.
"""
import time
import threading
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

_cache_lock = threading.Lock()
_cache: dict = {}
CACHE_TTL    = 60   # seconds

# ── Regime metadata ───────────────────────────────────────────────────────────

REGIME_META = {
    "risk_on": {
        "label":     "RISK ON",
        "color":     "#00c087",
        "bg":        "#001a11",
        "icon":      "▲",
        "bullish":   ["NASDAQ", "SPX", "BTC", "BANKNIFTY", "NIFTY50"],
        "bearish":   ["DXY", "GOLD"],
        "defensive": ["T-Bills"],
    },
    "risk_off": {
        "label":     "RISK OFF",
        "color":     "#f87171",
        "bg":        "#1a0505",
        "icon":      "▼",
        "bullish":   ["GOLD", "DXY", "US BONDS"],
        "bearish":   ["NASDAQ", "BTC", "NIFTY50", "CRUDE"],
        "defensive": ["GOLD", "USD", "BONDS"],
    },
    "inflationary": {
        "label":     "INFLATIONARY",
        "color":     "#f59e0b",
        "bg":        "#1a1000",
        "icon":      "🔥",
        "bullish":   ["GOLD", "CRUDE", "COPPER", "ENERGY"],
        "bearish":   ["BONDS", "GROWTH STOCKS"],
        "defensive": ["COMMODITIES", "REAL ASSETS"],
    },
    "stagflation": {
        "label":     "STAGFLATION",
        "color":     "#ef4444",
        "bg":        "#1a0000",
        "icon":      "⚠",
        "bullish":   ["GOLD", "CRUDE", "ENERGY"],
        "bearish":   ["EQUITIES", "BONDS", "GROWTH STOCKS"],
        "defensive": ["GOLD", "SHORT-TERM BONDS"],
    },
    "recession_fear": {
        "label":     "RECESSION FEAR",
        "color":     "#dc2626",
        "bg":        "#1a0000",
        "icon":      "📉",
        "bullish":   ["GOLD", "US BONDS", "USD"],
        "bearish":   ["CRUDE", "EQUITIES", "COPPER", "BANKNIFTY"],
        "defensive": ["LONG-DATED BONDS", "GOLD"],
    },
    "liquidity_crisis": {
        "label":     "LIQUIDITY CRISIS",
        "color":     "#fca5a5",
        "bg":        "#2d0000",
        "icon":      "🚨",
        "bullish":   ["DXY", "T-Bills"],
        "bearish":   ["ALL ASSETS", "CRYPTO", "EQUITIES", "GOLD"],
        "defensive": ["CASH", "USD ONLY"],
    },
    "ai_growth_boom": {
        "label":     "AI GROWTH BOOM",
        "color":     "#a78bfa",
        "bg":        "#0f0020",
        "icon":      "🤖",
        "bullish":   ["NASDAQ", "SEMICONDUCTORS", "NIFTY IT", "BTC"],
        "bearish":   ["DXY", "ENERGY", "VALUE"],
        "defensive": ["DIVERSIFIED TECH"],
    },
    "commodity_supercycle": {
        "label":     "COMMODITY SUPERCYCLE",
        "color":     "#d97706",
        "bg":        "#1a0e00",
        "icon":      "⛽",
        "bullish":   ["CRUDE", "GOLD", "COPPER", "ENERGY STOCKS"],
        "bearish":   ["DXY", "BONDS", "TECH GROWTH"],
        "defensive": ["COMMODITY ETFs"],
    },
    "central_bank_dovish": {
        "label":     "CB DOVISH",
        "color":     "#34d399",
        "bg":        "#001510",
        "icon":      "🕊",
        "bullish":   ["GOLD", "NASDAQ", "NIFTY50", "BTC", "REAL ESTATE"],
        "bearish":   ["DXY", "BOND YIELDS"],
        "defensive": ["EQUITY INDEX"],
    },
    "central_bank_hawkish": {
        "label":     "CB HAWKISH",
        "color":     "#fb923c",
        "bg":        "#1a0a00",
        "icon":      "🦅",
        "bullish":   ["DXY", "BANKS", "FINANCIALS", "T-Bills"],
        "bearish":   ["GOLD", "NASDAQ", "GROWTH", "BTC", "NIFTY"],
        "defensive": ["SHORT-DURATION BONDS"],
    },
}

# ── Signal extraction ─────────────────────────────────────────────────────────

def _get_price_signals() -> dict:
    """Pull from live_prices cache — zero latency (already refreshed by main engine)."""
    signals = {
        "dxy_chg":    0.0, "nasdaq_chg": 0.0, "spx_chg":    0.0,
        "gold_chg":   0.0, "crude_chg":  0.0, "vix":        18.0,
        "us10y_chg":  0.0, "us10y_lvl":  4.2, "btc_chg":    0.0,
        "nifty_chg":  0.0,
    }
    try:
        from live_prices import get_live_prices
        lp = get_live_prices()

        def _chg(cat, key):
            v = lp.get(cat, {}).get(key, {})
            return float(v.get("change", 0) or 0) if v else 0.0

        def _price(cat, key):
            v = lp.get(cat, {}).get(key, {})
            return float(v.get("price", 0) or 0) if v else 0.0

        signals["dxy_chg"]    = _chg("fx", "DXY")
        signals["nasdaq_chg"] = _chg("global", "NASDAQ")
        signals["spx_chg"]    = _chg("global", "SPX")
        signals["gold_chg"]   = _chg("commodities", "GOLD")
        signals["crude_chg"]  = _chg("commodities", "CRUDE")
        signals["btc_chg"]    = _chg("crypto", "BTC")
        signals["nifty_chg"]  = _chg("indices", "NIFTY50")

        vix_raw = _price("vix", "VIX")
        if vix_raw > 0:
            signals["vix"] = vix_raw

        us10y_raw = _price("bonds", "US_10Y")
        if us10y_raw > 0:
            signals["us10y_lvl"] = us10y_raw
            signals["us10y_chg"] = _chg("bonds", "US_10Y")

    except Exception as e:
        print(f"[regime] price signal error: {e}", flush=True)

    return signals


def _get_news_text() -> str:
    """Get recent headlines as one lowercased string for keyword scoring."""
    try:
        from news import get_all_news
        items = get_all_news() or []
        return " ".join(
            str(item.get("title", "") + " " + item.get("summary", ""))
            for item in items[:40]
        ).lower()
    except Exception:
        return ""


# ── Regime scoring ────────────────────────────────────────────────────────────

def _kw(text: str, *words: str) -> int:
    """Count how many keyword phrases appear in text."""
    return sum(1 for w in words if w in text)


def _score_all(sig: dict, nl: str) -> dict:
    d   = sig["dxy_chg"]
    nq  = sig["nasdaq_chg"]
    spx = sig["spx_chg"]
    gld = sig["gold_chg"]
    oil = sig["crude_chg"]
    vix = sig["vix"]
    t10c = sig["us10y_chg"]
    t10l = sig["us10y_lvl"]
    btc = sig["btc_chg"]

    scores = {}

    # ─ risk_on ─
    s = 0
    if nq > 0.3:    s += 18
    if nq > 1.0:    s += 12
    if d < -0.2:    s += 15
    if vix < 18:    s += 20
    if vix < 15:    s += 10
    if spx > 0.3:   s += 10
    if gld < 0:     s += 5
    if btc > 1:     s += 5
    s += _kw(nl, "risk on", "bull market", "rally", "growth optimism", "soft landing") * 5
    scores["risk_on"] = min(s, 100)

    # ─ risk_off ─
    s = 0
    if vix > 22:    s += 20
    if vix > 28:    s += 15
    if vix > 35:    s += 20
    if nq < -1:     s += 18
    if nq < -2:     s += 12
    if gld > 0.5:   s += 12
    if d > 0.3:     s += 10
    if t10c < -0.5: s += 8
    s += _kw(nl, "risk off", "fear", "panic", "selloff", "uncertainty", "war risk",
              "safe haven", "flight to safety", "volatility spike") * 5
    scores["risk_off"] = min(s, 100)

    # ─ inflationary ─
    s = 0
    if oil > 1.5:   s += 22
    if oil > 3.0:   s += 15
    if gld > 0.5:   s += 15
    if t10c > 0.5:  s += 15
    if t10l > 5.0:  s += 10
    if d < -0.3:    s += 8
    s += _kw(nl, "inflation", "cpi", "pce", "prices rise", "oil surge",
              "supply shock", "cost push", "price pressure", "hot inflation") * 6
    scores["inflationary"] = min(s, 100)

    # ─ stagflation ─
    s = 0
    if oil > 1.0 and nq < -0.5:         s += 30
    if gld > 0.5 and spx < -0.5:        s += 20
    if t10l > 5.0 and vix > 20:         s += 15
    if oil > 0 and spx < -1:            s += 10
    s += _kw(nl, "stagflation", "growth slowdown", "stag", "high inflation low growth",
              "supply chain crisis") * 8
    scores["stagflation"] = min(s, 100)

    # ─ recession_fear ─
    s = 0
    if vix > 28:    s += 20
    if nq < -2:     s += 18
    if oil < -2:    s += 15
    if spx < -1.5:  s += 12
    if t10c < -0.8: s += 10
    s += _kw(nl, "recession", "gdp contraction", "unemployment", "layoffs",
              "hard landing", "economic slowdown", "job losses", "yield curve") * 6
    scores["recession_fear"] = min(s, 100)

    # ─ liquidity_crisis ─
    s = 0
    if vix > 40:                          s += 35
    if vix > 50:                          s += 25
    if d > 1.0:                           s += 20
    if gld < -1.0 and vix > 30:          s += 20
    s += _kw(nl, "bank failure", "credit crunch", "liquidity crisis", "margin call",
              "default", "contagion", "systemic risk", "bank run") * 10
    scores["liquidity_crisis"] = min(s, 100)

    # ─ ai_growth_boom ─
    s = 0
    if nq > 1.0:    s += 18
    if nq > 2.0:    s += 12
    if spx > 0.5:   s += 8
    s += _kw(nl, "ai", "artificial intelligence", "nvidia", "semiconductor",
              "chip", "data center", "llm", "generative ai", "ai spending",
              "ai infrastructure", "openai", "microsoft ai", "google ai",
              "anthropic", "mega cap tech", "tech earnings beat") * 7
    scores["ai_growth_boom"] = min(s, 100)

    # ─ commodity_supercycle ─
    s = 0
    if oil > 2:     s += 22
    if gld > 1:     s += 18
    if d < -0.5:    s += 12
    if oil > 0 and gld > 0: s += 10
    s += _kw(nl, "commodity", "raw material", "opec", "energy crisis",
              "copper surge", "metals rally", "supply deficit", "commodity boom") * 6
    scores["commodity_supercycle"] = min(s, 100)

    # ─ central_bank_dovish ─
    s = 0
    if t10c < -0.5:   s += 22
    if t10l < 3.5:    s += 12
    if d < -0.3:      s += 15
    if gld > 0.3:     s += 12
    if nq > 0.5:      s += 8
    s += _kw(nl, "rate cut", "dovish", "fed pause", "easing", "pivot",
              "lower rates", "rate cuts", "rate reduction", "fed signals cut",
              "inflation cools", "disinflation", "soft pivot") * 8
    scores["central_bank_dovish"] = min(s, 100)

    # ─ central_bank_hawkish ─
    s = 0
    if t10c > 0.5:    s += 22
    if t10l > 5.0:    s += 12
    if d > 0.3:       s += 15
    if nq < -0.5:     s += 8
    s += _kw(nl, "rate hike", "hawkish", "tightening", "higher for longer",
              "inflation fight", "hike rates", "50bp", "75bp", "aggressive fed",
              "no rate cut") * 8
    scores["central_bank_hawkish"] = min(s, 100)

    return scores


# ── Explanation builder ───────────────────────────────────────────────────────

def _build_explanation(regime: str, sig: dict, nl: str) -> list:
    lines = []
    d   = sig["dxy_chg"]
    nq  = sig["nasdaq_chg"]
    gld = sig["gold_chg"]
    oil = sig["crude_chg"]
    vix = sig["vix"]
    t10c = sig["us10y_chg"]
    t10l = sig["us10y_lvl"]
    spx = sig["spx_chg"]

    if abs(d) > 0.1:
        lines.append(f"DXY {'weakening' if d < 0 else 'strengthening'} ({d:+.2f}%)")
    if abs(nq) > 0.1:
        lines.append(f"NASDAQ {'bullish' if nq > 0 else 'bearish'} ({nq:+.2f}%)")
    if abs(spx) > 0.1:
        lines.append(f"S&P500 {'advancing' if spx > 0 else 'declining'} ({spx:+.2f}%)")
    if vix != 18.0:
        tag = "elevated — fear rising" if vix > 25 else ("extreme fear" if vix > 35 else ("calm" if vix < 15 else "moderate"))
        lines.append(f"VIX at {vix:.1f} — {tag}")
    if abs(gld) > 0.1:
        lines.append(f"Gold {'rising' if gld > 0 else 'falling'} ({gld:+.2f}%) — {'safe-haven demand' if gld > 0 else 'risk appetite'}")
    if abs(oil) > 0.2:
        lines.append(f"Crude {'surging' if oil > 1.5 else ('rising' if oil > 0 else 'falling')} ({oil:+.2f}%)")
    if abs(t10c) > 0.2:
        lines.append(f"US 10Y yield {'rising' if t10c > 0 else 'falling'} — {t10l:.2f}% level")

    kw_hits = {
        "rate cut":    "Rate cut expectations in headlines",
        "inflation":   "Inflation narrative dominant in news",
        "recession":   "Recession fears visible in news flow",
        "ai":          "AI/semiconductor narrative driving sentiment",
        "war":         "Geopolitical risk elevated in news",
        "bank failure":"Banking stress keywords in headlines",
        "dovish":      "Dovish Fed language in news",
        "hawkish":     "Hawkish Fed language in news",
        "oil surge":   "Oil supply shock narrative",
        "stagflation": "Stagflation narrative in media",
    }
    for kw, desc in kw_hits.items():
        if kw in nl and desc not in lines:
            lines.append(desc)
            if len(lines) >= 5:
                break

    return lines[:5] if lines else [f"{REGIME_META[regime]['label']} conditions detected from market data"]


# ── Confidence calculation ────────────────────────────────────────────────────

def _calc_confidence(scores: dict, winner: str) -> int:
    """
    Confidence = how dominant the winner is vs the field.
    If winner score >> runner-up → high confidence.
    """
    top = scores[winner]
    if top == 0:
        return 30
    sorted_scores = sorted(scores.values(), reverse=True)
    runner_up = sorted_scores[1] if len(sorted_scores) > 1 else 0
    gap = top - runner_up
    # Base: 40–90 range based on gap and absolute score
    base = min(90, 40 + int(gap * 1.5) + int(top * 0.3))
    return max(35, min(95, base))


# ── Main entry point ──────────────────────────────────────────────────────────

def detect_market_regime(force: bool = False) -> dict:
    """
    Returns full regime dict. Cached 60 seconds.
    Never raises — always returns a valid result.
    """
    with _cache_lock:
        entry = _cache.get("regime")
        if entry and not force and (_time_now() - entry["ts"]) < CACHE_TTL:
            return entry["data"]

    try:
        result = _compute_regime()
    except Exception as e:
        print(f"[regime] compute error: {e}", flush=True)
        result = _fallback_regime()

    with _cache_lock:
        _cache["regime"] = {"data": result, "ts": _time_now()}

    return result


def _time_now() -> float:
    return time.time()


def _compute_regime() -> dict:
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_sig  = pool.submit(_get_price_signals)
        fut_news = pool.submit(_get_news_text)
        sig  = fut_sig.result(timeout=8)
        nl   = fut_news.result(timeout=5)

    scores  = _score_all(sig, nl)
    winner  = max(scores, key=scores.get)
    meta    = REGIME_META[winner]
    conf    = _calc_confidence(scores, winner)
    expl    = _build_explanation(winner, sig, nl)

    # Runner-up (secondary regime)
    sorted_r = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    runner   = sorted_r[1][0] if len(sorted_r) > 1 and sorted_r[1][1] > 20 else None

    return {
        "regime":          winner,
        "label":           meta["label"],
        "icon":            meta["icon"],
        "color":           meta["color"],
        "bg":              meta["bg"],
        "confidence":      conf,
        "explanation":     expl,
        "bullish_assets":  meta["bullish"],
        "bearish_assets":  meta["bearish"],
        "defensive_assets":meta["defensive"],
        "secondary_regime":runner,
        "secondary_label": REGIME_META[runner]["label"] if runner else None,
        "all_scores":      {k: round(v, 1) for k, v in sorted(scores.items(), key=lambda x: x[1], reverse=True)},
        "signals_used": {
            "dxy_chg":    round(sig["dxy_chg"], 3),
            "nasdaq_chg": round(sig["nasdaq_chg"], 3),
            "spx_chg":    round(sig["spx_chg"], 3),
            "gold_chg":   round(sig["gold_chg"], 3),
            "crude_chg":  round(sig["crude_chg"], 3),
            "vix":        round(sig["vix"], 2),
            "us10y_lvl":  round(sig["us10y_lvl"], 3),
            "us10y_chg":  round(sig["us10y_chg"], 3),
        },
        "generated_at": datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        "cache_ttl_s":  CACHE_TTL,
    }


def _fallback_regime() -> dict:
    """Keyword-only fallback when live data is unavailable."""
    nl = _get_news_text()
    # Simple keyword vote
    votes = {
        "central_bank_dovish":  _kw(nl, "rate cut", "dovish", "easing", "pivot"),
        "central_bank_hawkish": _kw(nl, "rate hike", "hawkish", "tightening"),
        "ai_growth_boom":       _kw(nl, "ai", "nvidia", "semiconductor", "chip"),
        "inflationary":         _kw(nl, "inflation", "cpi", "oil surge"),
        "recession_fear":       _kw(nl, "recession", "slowdown", "gdp"),
        "risk_off":             _kw(nl, "fear", "selloff", "panic", "war"),
        "risk_on":              _kw(nl, "rally", "bull", "growth"),
        "stagflation":          _kw(nl, "stagflation"),
        "liquidity_crisis":     _kw(nl, "bank failure", "credit crunch"),
        "commodity_supercycle": _kw(nl, "commodity", "opec"),
    }
    winner = max(votes, key=votes.get) if max(votes.values()) > 0 else "risk_on"
    meta   = REGIME_META[winner]
    return {
        "regime":          winner,
        "label":           meta["label"],
        "icon":            meta["icon"],
        "color":           meta["color"],
        "bg":              meta["bg"],
        "confidence":      35,
        "explanation":     ["Live data unavailable — keyword inference only"],
        "bullish_assets":  meta["bullish"],
        "bearish_assets":  meta["bearish"],
        "defensive_assets":meta["defensive"],
        "secondary_regime":None,
        "secondary_label": None,
        "all_scores":      votes,
        "signals_used":    {},
        "generated_at":    datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        "cache_ttl_s":     CACHE_TTL,
        "fallback":        True,
    }


def format_regime_for_prompt(r: dict) -> str:
    """Inject regime context into any Groq AI prompt."""
    lines = [
        f"CURRENT MARKET REGIME: {r['label']} (confidence {r['confidence']}%)",
        f"Regime icon/signal: {r['icon']}",
    ]
    if r.get("explanation"):
        lines.append("Key signals: " + " | ".join(r["explanation"]))
    if r.get("bullish_assets"):
        lines.append("Bullish assets in this regime: " + ", ".join(r["bullish_assets"]))
    if r.get("bearish_assets"):
        lines.append("Bearish/at-risk assets: " + ", ".join(r["bearish_assets"]))
    if r.get("secondary_label"):
        lines.append(f"Secondary regime overlay: {r['secondary_label']}")
    return "\n".join(lines)
