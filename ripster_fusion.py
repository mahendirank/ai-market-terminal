"""
ripster_fusion.py — Real-time per-ticker fusion of the Ripster technical
engine with live news sentiment, producing ONE directional call.

WHY THIS EXISTS
---------------
The TradingView/Pine indicator is sandboxed: it can't receive news. So the
fusion of (technicals) + (news/HNI sentiment) happens HERE, in the terminal,
where both data sources already live. The Pine chart stays a visual; this is
the brain.

PLUGS INTO THE EXISTING TERMINAL (no reinvention)
-------------------------------------------------
  • news sentiment  ->  sentiment_provider.get_sentiment(ticker)
  • consensus math  ->  bias_consensus_engine.compute_consensus([Signal,...])

What this module ADDS:
  1. RipsterTechnicalEngine — ports the v9 indicator logic (EMA clouds, session
     VWAP, ADX/chop, RVOL, day-type, unusual-volume) to a [-1,+1] technical score.
  2. fuse() — combines that technical Signal with the news Signal through the
     existing consensus engine, then applies the CATALYST layer (the part news
     uniquely unlocks): unusual volume + fresh news = confirmed trend day -> follow,
     don't fade; volume with no news = rotation -> levels hold.

Pure Python + pandas/numpy. No LLM, no network in the core path (yfinance only
in the __main__ demo). Deterministic: same inputs -> same call.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

# Terminal modules (flat imports — this file lives in ai-system/core)
from bias_consensus_engine import Signal, compute_consensus, _label_for  # type: ignore
try:
    from sentiment_provider import get_sentiment  # type: ignore
except Exception:  # pragma: no cover - defensive if path differs in tests
    def get_sentiment(ticker: str, asset_class: str = "us_stock") -> dict:
        return {"score": 0.0, "confidence": 0.0, "label": "NEUTRAL",
                "rationale": "sentiment_provider unavailable", "sources": [], "ts": int(time.time())}


# ════════════════════════════════════════════════════════════════════════════
#  1) RIPSTER TECHNICAL ENGINE  (port of the v9 indicator)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class RipsterState:
    """Snapshot of the technical read at the latest bar."""
    score: float            # [-1, +1] directional technical score
    regime: str             # "TREND UP" | "TREND DN" | "CHOP RANGE" | "NEUTRAL"
    day_type: str           # "TREND DAY" | "ROTATION" | "MIXED"
    unusual: bool           # RVOL spike + range expansion this bar
    above_vwap: Optional[bool]
    rvol: Optional[float]
    cloud_5_12: str         # "Bullish" | "Bearish" | "Flat"
    cloud_34_50: str
    gap_pct: Optional[float]
    detail: str = ""


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def _rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


_EQUITY_CLASSES = {"us_stock", "stock", "equity", "index"}


def compute_technical(df: pd.DataFrame, tz: str = "America/New_York",
                      rth: tuple[int, int] = (930, 1600),
                      asset_class: str = "us_stock") -> RipsterState:
    """Run the Ripster v9 technical logic on an intraday OHLCV frame and
    return the state at the LAST bar.

    df: DataFrame indexed by tz-aware (or naive-UTC) DatetimeIndex with
        columns open/high/low/close/volume (case-insensitive).
    asset_class: "us_stock"/"stock"/"equity"/"index" -> RTH session logic
        (session VWAP gated to 09:30-16:00, overnight gaps). Anything else
        (forex/crypto/futures) -> 24h mode: VWAP anchors to the whole day,
        no RTH gate, gap logic disabled.
    """
    if df is None or len(df) < 60:
        return RipsterState(0.0, "NEUTRAL", "MIXED", False, None, None, "Flat", "Flat", None,
                            detail="insufficient bars")
    d = df.rename(columns=str.lower).copy()
    idx = d.index
    if getattr(idx, "tz", None) is None:
        d.index = idx.tz_localize("UTC").tz_convert(tz)
    else:
        d.index = idx.tz_convert(tz)
    d["date"] = d.index.date
    d["hhmm"] = d.index.hour * 100 + d.index.minute
    is24 = asset_class.lower() not in _EQUITY_CLASSES
    # equity: VWAP gated to RTH. 24h: accumulate every bar in the day.
    in_rth = pd.Series(True, index=d.index) if is24 else ((d["hhmm"] >= rth[0]) & (d["hhmm"] < rth[1]))

    c, h, l, o, v = d["close"], d["high"], d["low"], d["open"], d["volume"]
    e5, e12, e20, e34, e50 = _ema(c, 5), _ema(c, 12), _ema(c, 20), _ema(c, 34), _ema(c, 50)

    # ADX / DMI(14)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    up, dn = h.diff(), -l.diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=d.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=d.index)
    atr = _rma(tr, 14)
    p_di = 100 * _rma(plus, 14) / _rma(tr, 14)
    m_di = 100 * _rma(minus, 14) / _rma(tr, 14)
    adx = _rma((100 * (p_di - m_di).abs() / (p_di + m_di).replace(0, np.nan)).fillna(0), 14)

    # session VWAP (RTH, reset daily)
    tp = (h + l + c) / 3
    pv = (tp * v).where(in_rth, 0.0)
    vv = v.where(in_rth, 0.0)
    vwap = (pv.groupby(d["date"]).cumsum() / vv.groupby(d["date"]).cumsum()).where(in_rth)

    # prior-day close + today's open (gap) + running day H/L + ADR
    daily = d.groupby("date").agg(H=("high", "max"), L=("low", "min"),
                                  C=("close", "last"), O=("open", "first"))
    pdc_map = daily["C"].shift(1)
    pdc = d["date"].map(pdc_map); pdc.index = d.index
    day_open_map = daily["O"]
    day_open = d["date"].map(day_open_map); day_open.index = d.index
    adr_map = (daily["H"] - daily["L"]).rolling(14).mean().shift(1)   # prior-completed avg range
    adr = d["date"].map(adr_map); adr.index = d.index
    rvol = v / v.rolling(20).mean()

    # latest-bar booleans
    i = -1
    cv = float(c.iloc[i])
    above_vwap = bool(cv > vwap.iloc[i]) if pd.notna(vwap.iloc[i]) else None
    fast_bull = bool(e5.iloc[i] > e12.iloc[i] and cv > e12.iloc[i])
    fast_bear = bool(e5.iloc[i] < e12.iloc[i] and cv < e12.iloc[i])
    bias_bull = bool(cv > max(e34.iloc[i], e50.iloc[i]))
    bias_bear = bool(cv < min(e34.iloc[i], e50.iloc[i]))
    stack_bull = bool(e5.iloc[i] > e12.iloc[i] > e20.iloc[i] > e34.iloc[i] > e50.iloc[i])
    stack_bear = bool(e5.iloc[i] < e12.iloc[i] < e20.iloc[i] < e34.iloc[i] < e50.iloc[i])
    fast_ctr = (e5.iloc[i] + e12.iloc[i]) / 2
    tangled = (min(e34.iloc[i], e50.iloc[i]) <= fast_ctr <= max(e34.iloc[i], e50.iloc[i]))
    adx_v = float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0.0
    is_trend = adx_v >= 25 and (stack_bull or stack_bear)
    is_chop = adx_v < 18 or (tangled and not is_trend)
    pdc_v = pdc.iloc[i]
    # overnight gap only meaningful for instruments that gap (equities). 24h -> None.
    gap_pct = None if is24 else ((float(day_open.iloc[i]) / float(pdc_v) - 1) * 100
                                 if pd.notna(pdc_v) and pdc_v else None)

    # technical score in [-1, +1]
    votes = [
        (1 if above_vwap else -1) if above_vwap is not None else 0,
        (1 if fast_bull else -1 if fast_bear else 0),
        (1 if bias_bull else -1 if bias_bear else 0),
        (1 if stack_bull else -1 if stack_bear else 0),
        (1 if pd.notna(pdc_v) and cv > pdc_v else -1),
    ]
    raw = sum(votes) / len(votes)
    score = max(-1.0, min(1.0, raw * (0.5 if is_chop else 1.0)))   # dampen in chop

    # day-type
    day_hi = float(h[d["date"] == d["date"].iloc[i]].max())
    day_lo = float(l[d["date"] == d["date"].iloc[i]].min())
    day_range = day_hi - day_lo
    adr_v = adr.iloc[i]
    expansion = bool(pd.notna(adr_v) and day_range > adr_v)
    extended = bool(pd.notna(vwap.iloc[i]) and atr.iloc[i] > 0 and abs(cv - vwap.iloc[i]) > 2 * atr.iloc[i])
    big_gap = bool(gap_pct is not None and abs(gap_pct) >= 1.0)
    rvol_v = float(rvol.iloc[i]) if pd.notna(rvol.iloc[i]) else None
    hi_rvol = bool(rvol_v is not None and rvol_v >= 1.5)
    trend_score = sum([big_gap, hi_rvol, is_trend, extended, expansion])
    day_type = "TREND DAY" if trend_score >= 3 else "ROTATION" if trend_score <= 1 else "MIXED"
    bar_rng = float(h.iloc[i] - l.iloc[i])
    unusual = bool(rvol_v is not None and rvol_v >= 2.0 and atr.iloc[i] > 0 and bar_rng / atr.iloc[i] >= 1.5)

    regime = "CHOP RANGE" if is_chop else ("TREND UP" if stack_bull else "TREND DN") if is_trend else "NEUTRAL"
    return RipsterState(
        score=round(score, 3), regime=regime, day_type=day_type, unusual=unusual,
        above_vwap=above_vwap, rvol=round(rvol_v, 2) if rvol_v is not None else None,
        cloud_5_12="Bullish" if fast_bull else "Bearish" if fast_bear else "Flat",
        cloud_34_50="Bullish" if bias_bull else "Bearish" if bias_bear else "Flat",
        gap_pct=round(gap_pct, 2) if gap_pct is not None else None,
        detail=f"adx={adx_v:.0f} regime={regime} dayType={day_type} rvol={rvol_v}",
    )


# ════════════════════════════════════════════════════════════════════════════
#  2) FUSION  (technical Signal + news Signal -> consensus -> catalyst layer)
# ════════════════════════════════════════════════════════════════════════════
def make_signals(tech: RipsterState, news: dict,
                 w_tech: float = 0.55, w_news: float = 0.45) -> list[Signal]:
    """Build the two Signals for the consensus engine. Names use the existing
    SOURCE_WEIGHTS keys' spirit but pass explicit weights for a focused
    real-time 2-source fusion. Caller can append macro/regime/etc. signals."""
    news_eff = float(news.get("score", 0.0)) * float(news.get("confidence", 0.0))
    return [
        Signal(source="ripster_technical", score=tech.score, weight=w_tech,
               detail=tech.detail),
        Signal(source="sentiment", score=max(-1.0, min(1.0, news_eff)), weight=w_news,
               detail=f"{news.get('label','NEUTRAL')} ({news.get('rationale','')[:60]})"),
    ]


def fuse(ticker: str, df: pd.DataFrame, asset_class: str = "us_stock",
         news: Optional[dict] = None, w_tech: float = 0.55, w_news: float = 0.45) -> dict:
    """Produce ONE fused directional call for a ticker.

    Returns a dict:
      direction   : "BUY" | "SELL" | "NEUTRAL"   (consensus bias)
      conviction  : float [0,1]                  (catalyst-adjusted)
      catalyst    : "CONFIRMED" | "CONFLICT" | "ROTATION" | "NONE"
      day_type    : "TREND DAY" | "ROTATION" | "MIXED"
      action_hint : short human guidance
      technical   : RipsterState as dict
      news        : the sentiment dict used
      consensus   : raw compute_consensus() output
      rationale   : one-line explanation
    """
    tech = compute_technical(df, asset_class=asset_class)
    if news is None:
        news = get_sentiment(ticker, asset_class)

    signals = make_signals(tech, news, w_tech, w_news)
    consensus = compute_consensus(signals)
    cscore = consensus["score"]
    direction = consensus["bias"]

    news_eff = float(news.get("score", 0.0)) * float(news.get("confidence", 0.0))
    base_conv = abs(cscore)

    # ── CATALYST LAYER — the value news uniquely unlocks ────────────────────
    catalyst = "NONE"
    conv = base_conv
    same_side = (news_eff > 0 and tech.score > 0) or (news_eff < 0 and tech.score < 0)
    opp_side = (news_eff > 0 and tech.score < 0) or (news_eff < 0 and tech.score > 0)

    if tech.unusual and abs(news_eff) >= 0.40 and same_side:
        catalyst, conv = "CONFIRMED", min(1.0, base_conv * 1.35)
        hint = "Confirmed catalyst + volume -> TREND DAY: follow, do NOT fade."
    elif opp_side and abs(news_eff) >= 0.40:
        catalyst, conv = "CONFLICT", base_conv * 0.5
        hint = "News opposes technicals -> conflict: reduce size or stand aside."
    elif tech.day_type == "ROTATION" and abs(news_eff) < 0.20:
        catalyst = "ROTATION"
        hint = "No catalyst, rotation regime -> levels hold, mean-reversion OK."
    else:
        hint = "Mixed read -> trade only with your own confirmation."

    conv = round(max(0.0, min(1.0, conv)), 3)
    rationale = (f"{ticker}: tech {tech.score:+.2f} ({tech.regime}), "
                 f"news {news_eff:+.2f} ({news.get('label','NEUTRAL')}), "
                 f"consensus {cscore:+.2f} -> {direction} | {catalyst} conv={conv}")

    return {
        "ticker": ticker,
        "direction": direction,
        "conviction": conv,
        "catalyst": catalyst,
        "day_type": tech.day_type,
        "action_hint": hint,
        "technical": tech.__dict__,
        "news": {k: news.get(k) for k in ("score", "confidence", "label", "rationale")},
        "consensus": consensus,
        "rationale": rationale,
        "ts": int(time.time()),
    }


# ════════════════════════════════════════════════════════════════════════════
#  Demo / smoke test:  python3 ripster_fusion.py MU [news_score] [news_conf]
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    tkr = sys.argv[1] if len(sys.argv) > 1 else "MU"
    # optional manual news injection so you can see the catalyst layer react
    manual_news = None
    if len(sys.argv) > 2:
        sc = float(sys.argv[2]); cf = float(sys.argv[3]) if len(sys.argv) > 3 else 0.8
        lbl = "BULLISH" if sc > 0 else "BEARISH" if sc < 0 else "NEUTRAL"
        manual_news = {"score": sc, "confidence": cf, "label": lbl,
                       "rationale": "manual injection (demo)", "sources": [], "ts": int(time.time())}
    try:
        import yfinance as yf
        data = yf.download(tkr, period="5d", interval="5m", auto_adjust=False, progress=False)
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
    except Exception as e:
        print(f"yfinance fetch failed: {e}"); sys.exit(1)

    out = fuse(tkr, data, news=manual_news)
    import json
    print(json.dumps({k: v for k, v in out.items() if k != "technical"}, indent=2, default=str))
    print("\nTECHNICAL:", json.dumps(out["technical"], indent=2, default=str))
