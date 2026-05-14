"""
indicators.py — Technical indicator engine for the Indicators panel.

12 curated indicators across 4 categories:
  Trend     : EMA20, EMA50, EMA200, ADX, Ichimoku
  Momentum  : RSI(14), MACD, Stochastic
  Volatility: Bollinger Bands, ATR
  Volume    : OBV, MFI

For each indicator: value, signal (BUY/SELL/NEUTRAL), strength [-1..+1],
explanation (one-liner). Composite engine then produces:
  - score      : weighted bullish/bearish vote, normalised to [-100, +100]
  - confidence : agreement among indicators × per-indicator strength
  - label      : STRONG BUY / BUY / NEUTRAL / SELL / STRONG SELL
  - bullish/bearish strength meter for the UI bar
  - multi-timeframe consensus across 1h / 4h / 1d / 1w
  - pluggable sentiment overlay via :mod:`sentiment_provider`

Caching: Redis-backed when REDIS_URL is set (mirrors alert_engine.py pattern),
falls back to in-process LRU otherwise. Different TTLs per timeframe.
"""
from __future__ import annotations

import json
import os
import time
import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ─── Redis (optional, mirrors alert_engine.py pattern) ───────────────────────
_redis_client = None
_redis_ok = False


def _init_redis() -> None:
    global _redis_client, _redis_ok
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return
    try:
        import redis
        c = redis.from_url(url, socket_connect_timeout=4, socket_timeout=4, decode_responses=True)
        c.ping()
        _redis_client, _redis_ok = c, True
        log.info("[indicators] Redis cache: connected")
    except Exception as e:  # noqa: BLE001
        _redis_ok = False
        log.warning("[indicators] Redis unavailable (%s) — using in-process cache", e)


_init_redis()

_inproc_cache: dict[str, tuple[float, dict]] = {}


# ─── Timeframe map ───────────────────────────────────────────────────────────
# yfinance interval+period combos. Periods chosen so that ta has enough bars
# (≥ 200) to compute EMA200 cleanly.
TIMEFRAMES: dict[str, dict] = {
    "1h": {"interval": "60m", "period": "60d",  "cache_ttl": 300,  "label": "1H"},
    "4h": {"interval": "1h",  "period": "180d", "cache_ttl": 600,  "label": "4H", "resample": "4H"},
    "1d": {"interval": "1d",  "period": "2y",   "cache_ttl": 900,  "label": "1D"},
    "1w": {"interval": "1wk", "period": "10y",  "cache_ttl": 1800, "label": "1W"},
}

DEFAULT_TIMEFRAME = "1d"

# Multi-timeframe weights for the consensus score. Daily anchors the read,
# weekly confirms trend, intraday tilts.
TF_WEIGHTS: dict[str, float] = {"1h": 0.15, "4h": 0.20, "1d": 0.40, "1w": 0.25}

# Per-indicator weight inside a single timeframe (sums to 1.0).
INDICATOR_WEIGHTS: dict[str, float] = {
    "EMA20":     0.08,
    "EMA50":     0.10,
    "EMA200":    0.12,
    "ADX":       0.10,
    "ICHIMOKU":  0.10,
    "RSI":       0.10,
    "MACD":      0.12,
    "STOCH":     0.08,
    "BBANDS":    0.06,
    "ATR":       0.02,  # info only, near-zero directional weight
    "OBV":       0.06,
    "MFI":       0.06,
}


# ─── Cache helpers ───────────────────────────────────────────────────────────
def _cache_get(key: str) -> Optional[dict]:
    if _redis_ok and _redis_client:
        try:
            raw = _redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
    entry = _inproc_cache.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    return None


def _cache_put(key: str, value: dict, ttl: int) -> None:
    if _redis_ok and _redis_client:
        try:
            _redis_client.setex(key, ttl, json.dumps(value, default=str))
            return
        except Exception:  # noqa: BLE001
            pass
    _inproc_cache[key] = (time.time() + ttl, value)


# ─── OHLC fetch ──────────────────────────────────────────────────────────────
def _fetch_ohlc(ticker: str, tf: str) -> Optional[pd.DataFrame]:
    """Pull OHLCV via yfinance for the requested timeframe."""
    cfg = TIMEFRAMES[tf]
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(
            period=cfg["period"], interval=cfg["interval"], auto_adjust=False,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("[indicators] yfinance fetch failed for %s/%s: %s", ticker, tf, e)
        return None

    if df is None or df.empty:
        return None

    # Optional resample (4h is built from 1h bars)
    resample = cfg.get("resample")
    if resample:
        try:
            df = df.resample(resample).agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()
        except Exception:  # noqa: BLE001
            pass

    df = df.dropna(subset=["Close"])
    return df if not df.empty else None


# ─── Indicator computations ──────────────────────────────────────────────────
def _safe_last(series: pd.Series) -> Optional[float]:
    try:
        v = float(series.iloc[-1])
        return v if np.isfinite(v) else None
    except Exception:  # noqa: BLE001
        return None


def _signal_from_score(strength: float, hi: float = 0.4, lo: float = -0.4) -> str:
    """Convert a [-1..1] strength to a discrete BUY/SELL/NEUTRAL label."""
    if strength >= hi: return "BUY"
    if strength <= lo: return "SELL"
    return "NEUTRAL"


def _ema_signal(close: pd.Series, period: int, name: str) -> dict:
    from ta.trend import EMAIndicator
    ema = EMAIndicator(close=close, window=period, fillna=False).ema_indicator()
    val = _safe_last(ema)
    last = _safe_last(close)
    if val is None or last is None:
        return _empty(name)
    # Strength: % distance from EMA, capped at ±5%
    pct = (last - val) / val if val else 0.0
    strength = float(np.clip(pct / 0.05, -1.0, 1.0))
    signal = _signal_from_score(strength, hi=0.15, lo=-0.15)
    return {
        "name": name, "value": round(val, 4), "signal": signal,
        "strength": round(strength, 3),
        "explanation": f"Price {('above' if last>val else 'below')} {name} ({last:.2f} vs {val:.2f}) — {'bullish' if last>val else 'bearish'} bias.",
    }


def _rsi_signal(close: pd.Series) -> dict:
    from ta.momentum import RSIIndicator
    rsi = RSIIndicator(close=close, window=14, fillna=False).rsi()
    v = _safe_last(rsi)
    if v is None:
        return _empty("RSI")
    if v >= 70:    strength, label = -0.8, "overbought — pullback risk"
    elif v >= 60:  strength, label = -0.3, "elevated, still bullish"
    elif v >= 40:  strength, label = 0.0,  "neutral momentum"
    elif v >= 30:  strength, label = 0.3,  "weak, potential bounce setup"
    else:          strength, label = 0.8,  "oversold — bounce probable"
    return {
        "name": "RSI(14)", "value": round(v, 2),
        "signal": _signal_from_score(strength), "strength": round(strength, 3),
        "explanation": f"RSI {v:.1f} — {label}.",
    }


def _macd_signal(close: pd.Series) -> dict:
    from ta.trend import MACD
    m = MACD(close=close, fillna=False)
    line = m.macd(); sig = m.macd_signal(); hist = m.macd_diff()
    lv, sv, hv = _safe_last(line), _safe_last(sig), _safe_last(hist)
    if lv is None or sv is None or hv is None:
        return _empty("MACD")
    # Strength = sign(hist) * min(|hist|/|price|*1000, 1.0)
    last = _safe_last(close) or 1.0
    scaled = (hv / last) * 1000 if last else 0
    strength = float(np.clip(scaled, -1.0, 1.0))
    above = lv > sv
    return {
        "name": "MACD", "value": round(hv, 4),
        "signal": _signal_from_score(strength, hi=0.1, lo=-0.1),
        "strength": round(strength, 3),
        "explanation": f"MACD line {('above' if above else 'below')} signal, histogram {('expanding' if abs(hv)>0 else 'flat')} — {('bullish' if above else 'bearish')} momentum.",
    }


def _stoch_signal(high: pd.Series, low: pd.Series, close: pd.Series) -> dict:
    from ta.momentum import StochasticOscillator
    s = StochasticOscillator(high=high, low=low, close=close, window=14, smooth_window=3, fillna=False)
    k = _safe_last(s.stoch()); d = _safe_last(s.stoch_signal())
    if k is None or d is None:
        return _empty("STOCH")
    if k >= 80:   strength, label = -0.7, "overbought"
    elif k <= 20: strength, label = 0.7,  "oversold"
    else:         strength = (50 - k) / 50.0 * 0.5; label = "mid-range"
    cross = " — bullish %K>%D" if k > d else " — bearish %K<%D"
    return {
        "name": "Stoch(14)", "value": round(k, 2),
        "signal": _signal_from_score(strength), "strength": round(strength, 3),
        "explanation": f"%K {k:.1f}, %D {d:.1f} — {label}{cross}.",
    }


def _bbands_signal(close: pd.Series) -> dict:
    from ta.volatility import BollingerBands
    bb = BollingerBands(close=close, window=20, window_dev=2, fillna=False)
    hi = _safe_last(bb.bollinger_hband()); lo = _safe_last(bb.bollinger_lband())
    mid = _safe_last(bb.bollinger_mavg()); last = _safe_last(close)
    if None in (hi, lo, mid, last):
        return _empty("BBANDS")
    width = hi - lo if hi != lo else 1e-9
    pos = (last - lo) / width  # 0=lower band, 1=upper band
    if pos <= 0.15:   strength, label = 0.7, "touching lower band — mean-reversion buy"
    elif pos >= 0.85: strength, label = -0.7, "touching upper band — mean-reversion sell"
    else:             strength = (0.5 - pos) * 0.6; label = "within band"
    return {
        "name": "BBands(20,2)", "value": round(pos, 3),
        "signal": _signal_from_score(strength), "strength": round(strength, 3),
        "explanation": f"Price {pos*100:.0f}% across the band — {label}.",
    }


def _adx_signal(high: pd.Series, low: pd.Series, close: pd.Series) -> dict:
    from ta.trend import ADXIndicator
    a = ADXIndicator(high=high, low=low, close=close, window=14, fillna=False)
    adx = _safe_last(a.adx()); pdi = _safe_last(a.adx_pos()); ndi = _safe_last(a.adx_neg())
    if None in (adx, pdi, ndi):
        return _empty("ADX")
    if adx < 20:
        return {"name": "ADX(14)", "value": round(adx, 2), "signal": "NEUTRAL",
                "strength": 0.0,
                "explanation": f"ADX {adx:.1f} — no trend; sideways."}
    direction = 1 if pdi > ndi else -1
    strength = float(np.clip((adx - 20) / 40.0, 0.0, 1.0)) * direction
    return {
        "name": "ADX(14)", "value": round(adx, 2),
        "signal": _signal_from_score(strength, hi=0.25, lo=-0.25),
        "strength": round(strength, 3),
        "explanation": f"ADX {adx:.1f}, +DI {pdi:.1f}, -DI {ndi:.1f} — {'bullish' if direction>0 else 'bearish'} trend strength.",
    }


def _ichimoku_signal(high: pd.Series, low: pd.Series, close: pd.Series) -> dict:
    from ta.trend import IchimokuIndicator
    i = IchimokuIndicator(high=high, low=low, window1=9, window2=26, window3=52, fillna=False)
    a = _safe_last(i.ichimoku_a()); b = _safe_last(i.ichimoku_b())
    last = _safe_last(close)
    if None in (a, b, last):
        return _empty("ICHIMOKU")
    cloud_top = max(a, b); cloud_bot = min(a, b)
    if last > cloud_top:
        strength = 0.6; label = "above the cloud — bullish"
    elif last < cloud_bot:
        strength = -0.6; label = "below the cloud — bearish"
    else:
        strength = 0.0; label = "inside the cloud — undecided"
    return {
        "name": "Ichimoku", "value": round(last, 4),
        "signal": _signal_from_score(strength), "strength": round(strength, 3),
        "explanation": f"Price {label} ({last:.2f} vs cloud {cloud_bot:.2f}-{cloud_top:.2f}).",
    }


def _atr_signal(high: pd.Series, low: pd.Series, close: pd.Series) -> dict:
    """ATR is volatility, not direction. Reported as info; strength stays near 0."""
    from ta.volatility import AverageTrueRange
    atr = AverageTrueRange(high=high, low=low, close=close, window=14, fillna=False).average_true_range()
    v = _safe_last(atr); last = _safe_last(close)
    if v is None or last is None:
        return _empty("ATR")
    pct = (v / last) * 100 if last else 0
    if   pct >= 4.0: label = "extreme volatility"
    elif pct >= 2.0: label = "elevated volatility"
    elif pct >= 1.0: label = "normal volatility"
    else:            label = "low volatility"
    return {
        "name": "ATR(14)", "value": round(v, 4),
        "signal": "NEUTRAL", "strength": 0.0,
        "explanation": f"ATR {v:.2f} ({pct:.2f}% of price) — {label}.",
    }


def _obv_signal(close: pd.Series, volume: pd.Series) -> dict:
    from ta.volume import OnBalanceVolumeIndicator
    obv = OnBalanceVolumeIndicator(close=close, volume=volume, fillna=False).on_balance_volume()
    if len(obv) < 20:
        return _empty("OBV")
    recent = obv.iloc[-20:]
    slope = (recent.iloc[-1] - recent.iloc[0]) / (abs(recent.iloc[0]) + 1e-9)
    strength = float(np.clip(slope / 0.1, -1.0, 1.0))
    return {
        "name": "OBV", "value": round(float(obv.iloc[-1]), 2),
        "signal": _signal_from_score(strength, hi=0.15, lo=-0.15),
        "strength": round(strength, 3),
        "explanation": f"OBV {'rising' if slope>0 else 'falling'} over last 20 bars — volume {'confirms' if abs(strength)>0.2 else 'flat-to'} price.",
    }


def _mfi_signal(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> dict:
    from ta.volume import MFIIndicator
    mfi = MFIIndicator(high=high, low=low, close=close, volume=volume, window=14, fillna=False).money_flow_index()
    v = _safe_last(mfi)
    if v is None:
        return _empty("MFI")
    if v >= 80:    strength, label = -0.7, "overbought (volume-weighted)"
    elif v <= 20:  strength, label = 0.7,  "oversold (volume-weighted)"
    else:          strength = (50 - v) / 50.0 * 0.5; label = "neutral money flow"
    return {
        "name": "MFI(14)", "value": round(v, 2),
        "signal": _signal_from_score(strength), "strength": round(strength, 3),
        "explanation": f"MFI {v:.1f} — {label}.",
    }


def _empty(name: str) -> dict:
    return {"name": name, "value": None, "signal": "NEUTRAL", "strength": 0.0,
            "explanation": "Not enough data."}


# ─── Single-timeframe pipeline ───────────────────────────────────────────────
def compute_indicators(ticker: str, tf: str = DEFAULT_TIMEFRAME) -> Optional[dict]:
    """Compute all 12 indicators + composite score for a single timeframe.

    Returns ``None`` if OHLC data is unavailable (yfinance miss).
    """
    if tf not in TIMEFRAMES:
        tf = DEFAULT_TIMEFRAME

    cache_key = f"ind:tf:{ticker}:{tf}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    df = _fetch_ohlc(ticker, tf)
    if df is None or len(df) < 30:
        return None

    close, high, low = df["Close"], df["High"], df["Low"]
    volume = df.get("Volume", pd.Series([0] * len(df), index=df.index))

    indicators = {
        "EMA20":     _ema_signal(close, 20,  "EMA20"),
        "EMA50":     _ema_signal(close, 50,  "EMA50"),
        "EMA200":    _ema_signal(close, 200, "EMA200"),
        "ADX":       _adx_signal(high, low, close),
        "ICHIMOKU":  _ichimoku_signal(high, low, close),
        "RSI":       _rsi_signal(close),
        "MACD":      _macd_signal(close),
        "STOCH":     _stoch_signal(high, low, close),
        "BBANDS":    _bbands_signal(close),
        "ATR":       _atr_signal(high, low, close),
        "OBV":       _obv_signal(close, volume),
        "MFI":       _mfi_signal(high, low, close, volume),
    }

    composite = _composite(indicators)
    last_price = _safe_last(close)
    prev_close = float(close.iloc[-2]) if len(close) >= 2 else last_price
    chg_pct = ((last_price - prev_close) / prev_close * 100) if prev_close else 0.0

    result = {
        "ticker": ticker, "timeframe": tf,
        "timeframe_label": TIMEFRAMES[tf]["label"],
        "last_price": last_price, "change_pct": round(chg_pct, 3),
        "bars": len(df),
        "indicators": indicators,
        "composite": composite,
        "ts": int(time.time()),
    }
    _cache_put(cache_key, result, TIMEFRAMES[tf]["cache_ttl"])
    return result


# ─── Composite score ─────────────────────────────────────────────────────────
def _composite(indicators: dict) -> dict:
    """Aggregate per-indicator strengths into a composite score + confidence.

    score      : weighted sum in [-100, +100]
    confidence : agreement × avg-|strength|, in [0, 1]
    bullish_strength / bearish_strength : sums for the UI meter
    label      : STRONG BUY / BUY / NEUTRAL / SELL / STRONG SELL
    """
    weighted_sum = 0.0
    weight_total = 0.0
    bull_strength = 0.0
    bear_strength = 0.0
    bull_count = 0
    bear_count = 0
    valid = 0

    for key, ind in indicators.items():
        w = INDICATOR_WEIGHTS.get(key, 0.0)
        if w == 0 or ind.get("value") is None:
            continue
        s = float(ind.get("strength") or 0.0)
        weighted_sum += s * w
        weight_total += w
        if s > 0:
            bull_strength += s * w
            if ind.get("signal") == "BUY": bull_count += 1
        elif s < 0:
            bear_strength += abs(s) * w
            if ind.get("signal") == "SELL": bear_count += 1
        valid += 1

    if weight_total == 0:
        return {"score": 0.0, "confidence": 0.0, "label": "NEUTRAL",
                "bullish_strength": 0.0, "bearish_strength": 0.0,
                "bullish_count": 0, "bearish_count": 0, "neutral_count": 0}

    raw = weighted_sum / weight_total  # in [-1, 1]
    score = round(raw * 100, 1)

    # Confidence: how aligned are the indicators × how strong individually?
    avg_abs = sum(abs(i.get("strength") or 0) for i in indicators.values()) / max(valid, 1)
    alignment = abs(bull_count - bear_count) / max(valid, 1)
    confidence = round(float(np.clip(0.6 * alignment + 0.4 * avg_abs, 0.0, 1.0)), 3)

    if   score >= 50:  label = "STRONG BUY"
    elif score >= 15:  label = "BUY"
    elif score <= -50: label = "STRONG SELL"
    elif score <= -15: label = "SELL"
    else:              label = "NEUTRAL"

    total_dir = bull_strength + bear_strength
    bull_pct = round((bull_strength / total_dir * 100), 1) if total_dir else 50.0
    bear_pct = round((bear_strength / total_dir * 100), 1) if total_dir else 50.0

    return {
        "score": score,
        "confidence": confidence,
        "label": label,
        "bullish_strength": bull_pct,
        "bearish_strength": bear_pct,
        "bullish_count": bull_count,
        "bearish_count": bear_count,
        "neutral_count": valid - bull_count - bear_count,
    }


# ─── Multi-timeframe consensus ───────────────────────────────────────────────
def compute_consensus(ticker: str, asset_class: str = "us_stock") -> dict:
    """Run all 4 timeframes + overlay sentiment hook, return consolidated view."""
    from sentiment_provider import get_sentiment

    cache_key = f"ind:consensus:{ticker}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    per_tf: dict[str, Optional[dict]] = {}
    for tf in TIMEFRAMES.keys():
        per_tf[tf] = compute_indicators(ticker, tf)

    available = {k: v for k, v in per_tf.items() if v is not None}
    if not available:
        return {"ticker": ticker, "error": "No OHLC data available", "ts": int(time.time())}

    # Weighted consensus across timeframes
    score_sum = 0.0
    conf_sum = 0.0
    weight_total = 0.0
    bull_pct_sum = 0.0
    bear_pct_sum = 0.0
    for tf, payload in available.items():
        w = TF_WEIGHTS.get(tf, 0.0)
        if w == 0:
            continue
        comp = payload["composite"]
        score_sum += comp["score"] * w
        conf_sum  += comp["confidence"] * w
        bull_pct_sum += comp["bullish_strength"] * w
        bear_pct_sum += comp["bearish_strength"] * w
        weight_total += w

    score      = round(score_sum / weight_total, 1) if weight_total else 0.0
    confidence = round(conf_sum  / weight_total, 3) if weight_total else 0.0
    bull_pct   = round(bull_pct_sum / weight_total, 1) if weight_total else 50.0
    bear_pct   = round(bear_pct_sum / weight_total, 1) if weight_total else 50.0

    if   score >= 50:  label = "STRONG BUY"
    elif score >= 15:  label = "BUY"
    elif score <= -50: label = "STRONG SELL"
    elif score <= -15: label = "SELL"
    else:              label = "NEUTRAL"

    # Sentiment overlay — currently a neutral stub, swap providers later
    sentiment = dict(get_sentiment(ticker, asset_class))

    # Blend: 85% technicals, 15% sentiment (sentiment ranges [-1,1], rescale to ±100)
    blended_score = round(score * 0.85 + (sentiment.get("score", 0.0) * 100) * 0.15, 1)
    blended_conf  = round(confidence * 0.85 + sentiment.get("confidence", 0.0) * 0.15, 3)

    last_price = available[max(available.keys(), key=lambda k: TF_WEIGHTS.get(k, 0))]["last_price"]

    out = {
        "ticker": ticker,
        "last_price": last_price,
        "consensus": {
            "score": score, "confidence": confidence, "label": label,
            "bullish_strength": bull_pct, "bearish_strength": bear_pct,
        },
        "ai_blended": {
            "score": blended_score, "confidence": blended_conf,
            "label": _blended_label(blended_score),
            "weights": {"technicals": 0.85, "sentiment": 0.15},
        },
        "sentiment": sentiment,
        "timeframes": per_tf,
        "ts": int(time.time()),
    }
    _cache_put(cache_key, out, 300)
    return out


def _blended_label(score: float) -> str:
    if   score >= 50:  return "STRONG BUY"
    elif score >= 15:  return "BUY"
    elif score <= -50: return "STRONG SELL"
    elif score <= -15: return "SELL"
    return "NEUTRAL"


# ─── Public introspection ────────────────────────────────────────────────────
def list_timeframes() -> list[dict]:
    return [{"key": k, "label": v["label"]} for k, v in TIMEFRAMES.items()]


def list_indicators() -> list[dict]:
    """Static metadata for the UI legend."""
    return [
        {"key": "EMA20",    "category": "trend",      "label": "EMA 20"},
        {"key": "EMA50",    "category": "trend",      "label": "EMA 50"},
        {"key": "EMA200",   "category": "trend",      "label": "EMA 200"},
        {"key": "ADX",      "category": "trend",      "label": "ADX(14)"},
        {"key": "ICHIMOKU", "category": "trend",      "label": "Ichimoku"},
        {"key": "RSI",      "category": "momentum",   "label": "RSI(14)"},
        {"key": "MACD",     "category": "momentum",   "label": "MACD"},
        {"key": "STOCH",    "category": "momentum",   "label": "Stoch(14)"},
        {"key": "BBANDS",   "category": "volatility", "label": "BBands(20,2)"},
        {"key": "ATR",      "category": "volatility", "label": "ATR(14)"},
        {"key": "OBV",      "category": "volume",     "label": "OBV"},
        {"key": "MFI",      "category": "volume",     "label": "MFI(14)"},
    ]
