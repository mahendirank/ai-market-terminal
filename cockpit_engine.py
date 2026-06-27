"""
cockpit_engine.py — Trade Cockpit: HNI-desk entry/exit read for the dashboard.

Like market_state_aggregator, this module does NOT invent new market data — it
**fuses** signals the existing engines already compute into a single
trade-ready view, then applies the discipline a wise institutional/HNI trader
applies before risking capital:

  1. MACRO CONTEXT   (market_state_aggregator) → regime, net-risk, driver
                       → a deterministic "gold macro tilt".
  2. EVENT GATE      (econ_publisher imminent + econ_calendar upcoming)
                       → never scalp into a high-impact print; flag swing
                         event-risk.
  3. SCALP setup     (trade_signal.get_combined_signal for direction,
                       smc_entry.get_entry_setup for SMC OB/FVG/BOS levels).
  4. SWING setup     (indicators.compute_consensus multi-timeframe,
                       daily-ATR levels around price).

For each mode the engine returns a VERDICT — TRADE / WAIT / STAND ASIDE —
with a 0-100 conviction that is *adjusted*, not just reported:
  + macro alignment is confluence (size up), conflict is counter-trend (size
    down or skip);
  + R:R below a per-mode floor downgrades to WAIT;
  + an imminent high-impact event forces STAND ASIDE on scalps and caps
    swing conviction.

Failure mode mirrors market_state_aggregator: every sub-call is wrapped, a
single failure sets data_quality=DEGRADED but never raises to the API layer.

This is EDUCATIONAL market analysis, not financial advice — surfaced in the
payload so the UI can show it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Instruments the cockpit knows how to resolve to a yfinance ticker. Gold is
# the primary instrument (XAUUSD); the map keeps room to extend later.
_SYMBOL_MAP: dict[str, str] = {
    "GOLD": "GC=F", "XAUUSD": "GC=F", "GC=F": "GC=F",
}
_DISPLAY: dict[str, str] = {"GC=F": "XAUUSD (GOLD)"}

# R:R floors — a scalp can run thinner than a swing, but both need an edge.
_SCALP_RR_FLOOR = 1.5
_SWING_RR_FLOOR = 2.0


def _safe(fn, *args, label: str, errors: list, **kwargs):
    """Call ``fn``; on failure append (label, exc-name) to ``errors`` and
    return None so the caller can fall back. Mirrors market_state_aggregator."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("[cockpit] %s failed: %s", label, e)
        errors.append((label, type(e).__name__))
        return None


# ─── Direction normalisation ────────────────────────────────────────────────
def _norm_dir(verdict: Optional[str]) -> str:
    """Map any '...BUY...'/'...SELL...' verdict to BUY / SELL / NEUTRAL."""
    v = (verdict or "").upper()
    if "BUY" in v:
        return "BUY"
    if "SELL" in v:
        return "SELL"
    return "NEUTRAL"


# ─── Macro tilt for gold ────────────────────────────────────────────────────
def _gold_macro_tilt(market_state: dict) -> dict:
    """Translate the macro regime into a gold directional tilt.

    Convention (matches the MARKET STATE card colour logic): net-risk
    direction > 0 == risk-ON (green) == headwind for gold; < 0 == risk-OFF ==
    tailwind for gold. Regime composite is a tie-breaker. This is a *context*
    tilt that nudges conviction — not a hard signal.
    """
    pressure = (market_state or {}).get("pressure") or {}
    net_risk = pressure.get("net_risk") or {}
    nr_dir = net_risk.get("direction", 0) or 0
    nr_label = net_risk.get("label") or net_risk.get("score")
    regime = ((market_state or {}).get("regime") or {})
    composite = (regime.get("composite") or "").upper()
    driver = (pressure.get("dominant_driver") or {}).get("node")

    tilt = "NEUTRAL"
    if nr_dir < 0:
        tilt = "BULLISH"
    elif nr_dir > 0:
        tilt = "BEARISH"
    # Regime tie-breaker when net-risk is flat.
    if tilt == "NEUTRAL":
        if "RISK_OFF" in composite or "RISK OFF" in composite:
            tilt = "BULLISH"
        elif "RISK_ON" in composite or "RISK ON" in composite:
            tilt = "BEARISH"

    note = {
        "BULLISH": "Risk-off backdrop — safe-haven bid supports gold.",
        "BEARISH": "Risk-on backdrop — haven demand fades, headwind for gold.",
        "NEUTRAL": "Mixed macro — no clear haven tilt; let price structure lead.",
    }[tilt]

    return {
        "tilt": tilt,
        "note": note,
        "regime": composite.replace("_", " ") or None,
        "regime_conf": regime.get("confidence"),
        "net_risk": nr_label,
        "net_risk_dir": nr_dir,
        "driver": driver,
    }


# ─── Event gate ─────────────────────────────────────────────────────────────
def _event_gate(errors: list) -> dict:
    """High-impact event proximity → CLEAR / CAUTION / BLOCK.

    BLOCK   : a High-impact event prints within 30 min (don't scalp the print).
    CAUTION : High-impact within 45 min, or one printed in the last 10 min
              (post-event whipsaw).
    """
    imminent = _safe(_imminent_events, label="econ_publisher.imminent", errors=errors) or []
    highs = [e for e in imminent if (e.get("impact") or "").lower() == "high"]

    status = "CLEAR"
    headline = "No high-impact events in the next 45 min."
    pre_high = [e for e in highs if e.get("delta_secs", 0) > 0]
    post_high = [e for e in highs if -600 <= e.get("delta_secs", 0) <= 0]

    if pre_high:
        nearest = min(pre_high, key=lambda e: e["delta_secs"])
        mins = nearest.get("minutes_until", 0)
        if mins <= 30:
            status = "BLOCK"
            headline = f"⚠ {nearest['event']} in {mins} min — high impact."
        elif mins <= 45:
            status = "CAUTION"
            headline = f"{nearest['event']} in {mins} min — high impact ahead."
    if status == "CLEAR" and post_high:
        status = "CAUTION"
        last = post_high[0]
        headline = f"{last['event']} just printed — post-event whipsaw risk."

    # Upcoming high-star events over the next ~2 days for the swing horizon.
    next_days: list = []
    cal = _safe(_calendar, label="econ_calendar.get_calendar", errors=errors) or {}
    for ev in (cal.get("events") or []):
        if ev.get("stars", 0) >= 3 and (ev.get("days_away") if ev.get("days_away") is not None else 99) <= 2:
            next_days.append({
                "event": ev.get("event"),
                "when": ev.get("days_label") or ev.get("date"),
                "time": ev.get("time"),
                "stars": ev.get("stars"),
                "cname": ev.get("cname"),
            })
        if len(next_days) >= 5:
            break

    return {
        "status": status,
        "headline": headline,
        "imminent": [
            {"event": e.get("event"), "minutes_until": e.get("minutes_until"),
             "impact": e.get("impact"), "forecast": e.get("forecast"),
             "previous": e.get("previous"), "actual": e.get("actual")}
            for e in highs[:4]
        ],
        "next_24_48h": next_days,
    }


# ─── Per-mode verdict (the HNI discipline) ──────────────────────────────────
def _verdict(bias: str, conv_base: float, rr: Optional[float], rr_floor: float,
             macro_tilt: str, gate_status: str, is_scalp: bool) -> dict:
    reasons: list = []
    conv = float(conv_base)

    if bias == "NEUTRAL":
        return {"verdict": "WAIT", "conviction": 0,
                "reasons": ["Technical bias is neutral — no edge. Stand down "
                            "until structure picks a side."]}

    aligned = (bias == "BUY" and macro_tilt == "BULLISH") or \
              (bias == "SELL" and macro_tilt == "BEARISH")
    conflict = (bias == "BUY" and macro_tilt == "BEARISH") or \
               (bias == "SELL" and macro_tilt == "BULLISH")
    if aligned:
        conv += 15
        reasons.append(f"{bias} aligns with the macro tilt ({macro_tilt}) — confluence, full size.")
    elif conflict:
        conv -= 25
        reasons.append(f"{bias} fights the macro tilt ({macro_tilt}) — counter-trend; size down or skip.")
    conv = max(0.0, min(100.0, conv))

    rr_ok = rr is not None and rr >= rr_floor
    if not rr_ok:
        reasons.append(f"R:R {rr} is below the {rr_floor} floor — risk not worth it yet.")

    # Event discipline.
    if is_scalp and gate_status == "BLOCK":
        reasons.append("High-impact event imminent — scalping the print is gambling, not trading.")
        return {"verdict": "STAND ASIDE", "conviction": round(conv), "reasons": reasons}
    if gate_status == "CAUTION":
        conv = min(conv, 55.0)
        reasons.append("Event risk nearby — reduce size, widen stops, or wait for the dust to settle.")

    if not rr_ok:
        verdict = "WAIT"
        reasons.insert(0, f"Conviction {round(conv)} — setup not yet payable on risk.")
    elif conv >= 60:
        verdict = "TRADE"
        reasons.insert(0, f"Conviction {round(conv)} — clean {bias}; execute to plan, manage at TP1.")
    elif conv >= 40:
        verdict = "WAIT"
        reasons.insert(0, f"Conviction {round(conv)} — watchlist only; wait for confirmation / pullback to zone.")
    else:
        verdict = "STAND ASIDE"
        reasons.insert(0, f"Conviction {round(conv)} — edge too thin to risk capital.")

    return {"verdict": verdict, "conviction": round(conv), "reasons": reasons}


# ─── Scalp mode ─────────────────────────────────────────────────────────────
def _scalp(ticker: str, macro_tilt: str, gate_status: str, errors: list) -> dict:
    combined = _safe(_combined_signal, ticker, label="trade_signal.combined", errors=errors) or {}
    bias = _norm_dir(combined.get("verdict"))

    # Conviction base from how many of the 4 intraday votes agree.
    score_str = combined.get("score", "")  # e.g. "3B / 1S"
    votes = 0
    try:
        buys = int(score_str.split("B")[0].strip())
        sells = int(score_str.split("/")[1].strip().split("S")[0].strip())
        votes = max(buys, sells)
    except Exception:
        votes = 0
    conv_base = (40 + votes * 12) if bias != "NEUTRAL" else 0

    setup = {}
    if bias in ("BUY", "SELL"):
        setup = _safe(_entry_setup, bias, label="smc_entry.get_entry_setup", errors=errors) or {}

    rr = setup.get("rr")
    verdict = _verdict(bias, conv_base, rr, _SCALP_RR_FLOOR, macro_tilt, gate_status, is_scalp=True)

    confluence = setup.get("confluence") or []
    ob = setup.get("ob")
    return {
        "mode": "SCALP · M5",
        "bias": bias,
        "intraday_votes": combined.get("signals") or {},
        "vote_score": score_str,
        "price": setup.get("price"),
        "entry": setup.get("entry"),
        "sl": setup.get("sl"),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
        "atr": setup.get("atr"),
        "rr": rr,
        "bos": setup.get("bos"),
        "ob": f"{ob[0]} @ {ob[1]}" if isinstance(ob, (list, tuple)) and len(ob) == 2 else None,
        "fvg_count": setup.get("fvg_count"),
        "confluence": confluence,
        "session": combined_session(),
        **verdict,
    }


# ─── Swing mode ─────────────────────────────────────────────────────────────
def _swing(ticker: str, macro_tilt: str, gate_status: str, errors: list) -> dict:
    df = _safe(_daily_frame, ticker, label="cockpit.daily_frame", errors=errors)
    atr = _atr_from_df(df) if df is not None else None

    tf_view: dict = {}
    source = None
    # Try the rich engine quietly — a miss falls back to the daily read below
    # and must NOT mark the whole cockpit degraded (the fallback is valid).
    try:
        cons = _consensus(ticker) or {}
    except Exception as e:  # noqa: BLE001
        log.info("[cockpit] consensus unavailable (%s) — using daily fallback", type(e).__name__)
        cons = {}
    consensus = cons.get("consensus")
    if consensus:
        # Rich multi-timeframe engine (needs `ta`, present in prod).
        blended = cons.get("ai_blended") or {}
        label = blended.get("label") or consensus.get("label")
        bias = _norm_dir(label)
        score = consensus.get("score", 0) or 0
        confidence = consensus.get("confidence", 0) or 0
        price = cons.get("last_price")
        for tf, payload in (cons.get("timeframes") or {}).items():
            if payload and isinstance(payload, dict):
                tf_view[tf] = (payload.get("composite") or {}).get("label")
        source = "multi-timeframe consensus"
    elif df is not None:
        # Lightweight fallback — daily EMA/RSI, no external TA lib.
        fb = _swing_fallback(df)
        bias = fb["bias"]
        label = fb["label"]
        score = fb["score"]
        confidence = 0.5
        price = fb["price"]
        tf_view = fb["tf"]
        source = "daily EMA/RSI (fallback)"
    else:
        bias, label, score, confidence, price = "NEUTRAL", None, 0, 0, None

    entry_zone = sl = tp1 = tp2 = rr = None
    if price and atr and bias in ("BUY", "SELL"):
        sign = 1 if bias == "BUY" else -1
        # Enter on a shallow pullback (0.5 ATR) into structure, not at market.
        z1 = round(price - sign * 0.3 * atr, 2)
        z2 = round(price - sign * 0.8 * atr, 2)
        entry_zone = sorted([z1, z2])
        anchor = round((z1 + z2) / 2, 2)
        # Swing risk model: 1.5-ATR stop, 3-ATR first target (R:R 2.0), 5-ATR
        # runner — wide enough that a clean swing can clear the 2.0 floor.
        sl = round(anchor - sign * 1.5 * atr, 2)
        tp1 = round(anchor + sign * 3.0 * atr, 2)
        tp2 = round(anchor + sign * 5.0 * atr, 2)
        rr = round(abs(tp1 - anchor) / abs(sl - anchor), 2) if sl != anchor else None

    conv_base = round(min(90.0, 35 + abs(score) * 0.5 + confidence * 20)) if bias != "NEUTRAL" else 0
    verdict = _verdict(bias, conv_base, rr, _SWING_RR_FLOOR, macro_tilt, gate_status, is_scalp=False)

    return {
        "mode": "SWING · H4/D1/W",
        "bias": bias,
        "source": source,
        "consensus_label": label,
        "consensus_score": score,
        "confidence": round(confidence * 100) if confidence else None,
        "timeframes": tf_view,
        "price": price,
        "entry_zone": entry_zone,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "atr": round(atr, 2) if atr else None,
        "rr": rr,
        **verdict,
    }


# ─── Public entry point ─────────────────────────────────────────────────────
def get_cockpit(symbol: str = "GOLD") -> dict:
    """Full trade-cockpit payload for ``symbol`` (gold by default)."""
    errors: list = []
    ticker = _SYMBOL_MAP.get(symbol.upper(), "GC=F")
    display = _DISPLAY.get(ticker, symbol.upper())

    market_state = _safe(_market_state, label="market_state.get", errors=errors) or {}
    context = _gold_macro_tilt(market_state)
    gate = _event_gate(errors)

    scalp = _scalp(ticker, context["tilt"], gate["status"], errors)
    swing = _swing(ticker, context["tilt"], gate["status"], errors)

    # Headline price: prefer scalp (freshest 5m close), fall back to swing.
    price = scalp.get("price") or swing.get("price")

    data_quality = "DEGRADED" if errors else "OK"

    return {
        "symbol": display,
        "ticker": ticker,
        "price": price,
        "context": context,
        "event_gate": gate,
        "scalp": scalp,
        "swing": swing,
        "disclaimer": "Educational market analysis — not financial advice. "
                      "Defined-risk only; you size your own trade.",
        "data_quality": data_quality,
        "failed_components": [{"name": n, "error": e} for n, e in errors] or None,
        "generated_at": datetime.now(_IST).strftime("%d-%b-%Y %H:%M IST"),
    }


# ─── Indirection wrappers (named so error labels stay readable) ──────────────
def _market_state() -> dict:
    from market_state_aggregator import get_market_state
    return get_market_state()


def _imminent_events() -> list:
    from econ_publisher import get_imminent_events
    return get_imminent_events(window_pre_min=45, window_post_min=10)


def _calendar() -> dict:
    from econ_calendar import get_calendar
    return get_calendar(days_ahead=2)


def _combined_signal(ticker: str) -> dict:
    from trade_signal import get_combined_signal
    return get_combined_signal(ticker)


def combined_session() -> str:
    from trade_signal import get_session
    return get_session()


def _entry_setup(decision: str) -> dict:
    from smc_entry import get_entry_setup
    return get_entry_setup(decision)


def _consensus(ticker: str) -> dict:
    from indicators import compute_consensus
    return compute_consensus(ticker, asset_class="commodity")


def _daily_frame(ticker: str):
    """One year of daily bars — enough for EMA200 + a clean ATR."""
    import yfinance as yf
    import pandas as pd
    df = yf.download(ticker, period="1y", interval="1d", progress=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Close"]) if not df.empty else None


def _atr_from_df(df, period: int = 14) -> Optional[float]:
    import pandas as pd
    try:
        tr = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift()).abs(),
            (df["Low"] - df["Close"].shift()).abs(),
        ], axis=1).max(axis=1)
        val = tr.rolling(period).mean().iloc[-1]
        return round(float(val), 2) if val == val else None  # NaN guard
    except Exception:
        return None


def _swing_fallback(df) -> dict:
    """`ta`-free daily swing read: EMA20/50/200 alignment + RSI(14) vote."""
    close = df["Close"]
    price = round(float(close.iloc[-1]), 2)
    ema20 = float(close.ewm(span=20).mean().iloc[-1])
    ema50 = float(close.ewm(span=50).mean().iloc[-1])
    ema200 = float(close.ewm(span=200).mean().iloc[-1]) if len(close) >= 200 else None

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + (gain / loss)))
    rv = float(rsi.iloc[-1])

    bull = bear = 0
    bull += 1 if ema20 > ema50 else 0
    bear += 1 if ema20 <= ema50 else 0
    if ema200 is not None:
        bull += 1 if price > ema200 else 0
        bear += 1 if price <= ema200 else 0
    if rv > 55:
        bull += 1
    elif rv < 45:
        bear += 1

    if bull > bear:
        bias, n = "BUY", bull
    elif bear > bull:
        bias, n = "SELL", bear
    else:
        bias, n = "NEUTRAL", 0
    total = (bull + bear) or 1
    score = round((bull - bear) / total * 100)
    tf = {
        "trend": "UP" if ema20 > ema50 else "DOWN",
        "vs200": ("ABOVE" if ema200 and price > ema200 else "BELOW") if ema200 else "n/a",
        "rsi": round(rv, 1),
    }
    return {"bias": bias, "label": bias, "score": score, "price": price, "tf": tf, "n": n}


if __name__ == "__main__":
    import json
    print(json.dumps(get_cockpit("GOLD"), indent=2, default=str))
