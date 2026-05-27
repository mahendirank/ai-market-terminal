"""
market_state_aggregator.py — Unified MARKET STATE snapshot for the dashboard.

Pulls together everything that's already computed across regime_engine,
pressure_vector, yield_watch, live_prices, and market_memory into a single
flat dict that the frontend can render as one Bloomberg-style overview card.

This module deliberately does NOT compute anything new — its job is
**consolidation** of work the existing modules already do. The genuinely
new logic is the "last hour delta": comparing the current regime state +
key macro readings against the closest market_memory snapshot from ~60 min
ago, so the dashboard can answer "what changed in the last hour."

Output schema (flat, JSON-safe):

    {
      "regime": {composite, confidence, summary, dimensions, transitions},
      "pressure": {dominant_driver, net_risk, vector},
      "yields": {yields, big_movers, narrative, any_breaking},
      "last_hour": {
          "window_min", "regime_changed", "regime_prev",
          "feature_deltas": {us10y, dxy, vix, fng_local, sentiment_tilt},
      },
      "key_prices": {NASDAQ, SPX, DOW, GOLD, OIL, DXY, VIX, US_10Y},
      "ai_read": "...",            # optional LLM paragraph
      "data_quality": "OK" | "DEGRADED",
      "degraded_assets": [...],
      "generated_at": "...",
    }

Failure mode: every sub-call is wrapped. A single failing component sets
``data_quality=DEGRADED`` and lists which sub-call failed, but never raises
to the API layer. The LLM read is suppressed when degraded — better to
render the deterministic fields alone than to confabulate prose on bad data
(same principle the Wave 6 grounding guard applies in explainer.py and
morning_report.narrate_brief).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Window we treat as "last hour" — flexible because market_memory snapshots
# every 15 min, so the closest historical row is rarely exactly 60 min back.
_LOOKBACK_MIN = 60
_LOOKBACK_TOLERANCE_MIN = 30   # accept snapshots between 30-90 min old

# Assets the MARKET STATE card displays in its key-prices strip.
_KEY_PRICE_PATHS: tuple[tuple[str, str, str], ...] = (
    ("indices",     "NIFTY50",  "NIFTY"),
    ("global",      "NASDAQ",   "NDX"),
    ("global",      "SPX",      "SPX"),
    ("global",      "DOW",      "DOW"),
    ("commodities", "GOLD",     "GOLD"),
    ("commodities", "CRUDE",    "OIL"),
    ("fx",          "DXY",      "DXY"),
    ("vix",         "VIX",      "VIX"),
    ("bonds",       "US_10Y",   "US10Y"),
    ("bonds",       "JP_10Y",   "JP10Y"),
)


def _safe(fn, *args, label: str, errors: list, **kwargs):
    """Call ``fn`` and append a (label, exception) tuple to ``errors`` on failure.
    Returns ``None`` on failure so the caller can decide a sensible default."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("[market_state] %s failed: %s", label, e)
        errors.append((label, type(e).__name__))
        return None


# ─── Last-hour delta ───────────────────────────────────────────────────────
def _historical_snapshot(target_age_min: int = _LOOKBACK_MIN,
                         tolerance_min: int = _LOOKBACK_TOLERANCE_MIN) -> Optional[dict]:
    """Return the market_memory snapshot closest to ``target_age_min`` minutes ago.
    None when no snapshot is within tolerance — common right after a fresh boot
    before any snapshot has aged enough.
    """
    try:
        from market_memory import get_history
    except Exception:
        return None
    rows = get_history(limit=24) or []
    if not rows:
        return None
    now = time.time()
    target_ts = now - (target_age_min * 60)
    lo = now - (target_age_min + tolerance_min) * 60
    hi = now - max(5, target_age_min - tolerance_min) * 60
    in_range = [r for r in rows if isinstance(r.get("ts"), (int, float))
                and lo <= r["ts"] <= hi]
    if not in_range:
        return None
    in_range.sort(key=lambda r: abs(r["ts"] - target_ts))
    return in_range[0]


def _compute_last_hour(regime_state, errors: list) -> dict:
    """Compare current regime + features to the snapshot closest to 1h ago."""
    prev = _safe(_historical_snapshot, label="market_memory.history", errors=errors)
    if not prev:
        return {
            "window_min":     _LOOKBACK_MIN,
            "regime_changed": False,
            "regime_prev":    None,
            "feature_deltas": {},
            "note":           "no historical snapshot in the lookback window yet",
        }

    age_min = round((time.time() - prev["ts"]) / 60)
    curr_regime = (regime_state.composite if regime_state else None)
    prev_regime = prev.get("regime")

    # Per-feature deltas — only the few that matter for the UI strip.
    track = ("us10y", "dxy", "vix", "fng_local", "sentiment_tilt",
             "risk_score", "fed_score", "vol_score")
    deltas: dict = {}
    if regime_state:
        curr_dims = regime_state.dimensions or {}
        curr_scores = {
            "risk_score":   curr_dims.get("risk", {}).get("score"),
            "fed_score":    curr_dims.get("fed", {}).get("score"),
            "vol_score":    curr_dims.get("volatility", {}).get("score"),
        }
    else:
        curr_scores = {}
    # Pull current macro readings from live_prices for us10y/dxy/vix.
    curr_macro: dict = {}
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        b = lp.get("bonds", {}) or {}
        f = lp.get("fx", {}) or {}
        v = lp.get("vix", {}) or {}
        curr_macro = {
            "us10y": (b.get("US_10Y") or {}).get("price"),
            "dxy":   (f.get("DXY")    or {}).get("price"),
            "vix":   (v.get("VIX")    or {}).get("price"),
        }
    except Exception:
        pass
    for col in track:
        prev_v = prev.get(col)
        if col in curr_scores:
            curr_v = curr_scores.get(col)
        else:
            curr_v = curr_macro.get(col)
        if prev_v is None or curr_v is None:
            continue
        try:
            d = round(float(curr_v) - float(prev_v), 3)
        except (TypeError, ValueError):
            continue
        deltas[col] = {"prev": round(float(prev_v), 3),
                       "curr": round(float(curr_v), 3),
                       "delta": d}

    return {
        "window_min":     age_min,
        "regime_changed": bool(curr_regime and prev_regime and curr_regime != prev_regime),
        "regime_prev":    prev_regime,
        "feature_deltas": deltas,
    }


# ─── Key prices strip ──────────────────────────────────────────────────────
def _key_prices(lp: dict) -> dict:
    """Pluck the 10 assets the MARKET STATE strip displays."""
    out: dict = {}
    for cat, key, display in _KEY_PRICE_PATHS:
        row = (lp.get(cat) or {}).get(key)
        if not row:
            continue
        out[display] = {
            "price":   row.get("price"),
            "change":  row.get("change"),
            "arrow":   row.get("arrow"),
            "quality": row.get("quality", "OK"),
        }
    return out


# ─── Optional LLM narrative ────────────────────────────────────────────────
def _ai_read(regime_state, pressure, yields, last_hour, data_quality: str) -> Optional[str]:
    """3-4 sentence cross-asset paragraph. Gated, grounded, refused on
    DEGRADED data quality (same pattern as Wave 6 morning_report)."""
    if os.environ.get("ENABLE_MARKET_STATE_NARRATION", "1").strip().lower() in {"0", "false", "off"}:
        return None
    if data_quality == "DEGRADED":
        return None
    if not regime_state or not pressure:
        return None
    try:
        from ai_router import chat
    except Exception:
        return None

    # Build a compact, NUMERIC context — the only data the LLM is allowed to
    # cite. The grounding guard in explainer.py uses 1% tolerance against
    # exactly this string format; we mirror it.
    dom = pressure.get("dominant_driver") or {}
    net_risk = pressure.get("net_risk") or {}
    yw_movers = (yields or {}).get("big_movers") or []
    deltas = (last_hour or {}).get("feature_deltas") or {}

    ctx_lines = [
        f"REGIME: {regime_state.composite}  conf={regime_state.confidence}%",
        f"DIMENSIONS:",
    ]
    for dim, info in regime_state.dimensions.items():
        ctx_lines.append(f"  {dim:<11} score={info.get('score'):>5.1f}  label={info.get('label')}")
    ctx_lines.append(
        f"PRESSURE: dominant_driver={dom.get('node')} dir={dom.get('direction')} "
        f"mag={dom.get('magnitude')} | net_risk={net_risk.get('score')}"
    )
    if yw_movers:
        movers = []
        for k in yw_movers:
            y = (yields.get("yields") or {}).get(k, {})
            movers.append(f"{y.get('label', k)} {y.get('delta_bp', 0):+.1f}bp")
        ctx_lines.append("YIELDS MOVING: " + " · ".join(movers))
    if deltas:
        ctx_lines.append("LAST-HOUR DELTAS:")
        for k, d in deltas.items():
            ctx_lines.append(f"  {k:<14} {d['prev']:>7.2f} → {d['curr']:>7.2f}  Δ {d['delta']:+.3f}")
    if last_hour and last_hour.get("regime_changed"):
        ctx_lines.append(f"REGIME TRANSITION: {last_hour.get('regime_prev')} → {regime_state.composite}")

    context = "\n".join(ctx_lines)

    prompt = (
        f"{context}\n\n"
        "Write 3-4 sentences explaining what's driving markets RIGHT NOW. "
        "Cover, in order: (1) the dominant force (yields, dollar, vol, or "
        "events); (2) which asset class is leading / lagging and why; "
        "(3) the cross-asset linkage that ties them together; (4) IF a "
        "regime transition happened in the last hour, what flipped. "
        "Cite ONLY numbers present in the data above. No price targets. "
        "No hedge words ('might', 'could'). Direct desk-note voice."
    )
    messages = [
        {"role": "system", "content": (
            "You write Bloomberg-style market-state notes for institutional "
            "traders. Three to four crisp sentences. Direct voice. Cite only "
            "numbers from the snapshot — no fabrication.")},
        {"role": "user", "content": prompt},
    ]
    try:
        result = chat(task="fast_summary", messages=messages,
                      temperature=0.25, max_tokens=240, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.warning("[market_state] LLM call failed: %s", e)
        return None
    if not result.ok or not result.content:
        return None
    return result.content.strip()


# ─── Public entry point ────────────────────────────────────────────────────
def get_market_state() -> dict:
    """Consolidated MARKET STATE for the dashboard card.

    Cheap to call repeatedly: sub-modules have their own caches
    (regime_engine persists, pressure_vector memoises, yield_watch caches
    5 min, live_prices caches 30s). The aggregator itself is intentionally
    uncached so a frontend poll always sees the freshest sub-states.
    Wrap externally with _bg_refresh if you want a stricter cadence.
    """
    errors: list = []

    # Macro snapshot — shared input for regime + pressure.
    macro = _safe(_pull_macro, label="market_intel.macro", errors=errors) or {}

    # Regime + transitions.
    regime_state = _safe(_compute_regime, macro, label="regime_engine.compute", errors=errors)

    # Pressure vector.
    pressure = _safe(_compute_pressure, macro, label="pressure_vector.compute", errors=errors) or {}

    # Yield watch (Wave 2).
    yields = _safe(_yields, label="yield_watch.get", errors=errors) or {}

    # Live prices for the key-prices strip + last-hour macro deltas.
    lp = _safe(_live_prices, label="live_prices.get", errors=errors) or {}
    key_prices = _key_prices(lp)

    # Last-hour delta — depends on regime_state for current scores, on
    # market_memory for the historical snapshot.
    last_hour = _compute_last_hour(regime_state, errors)

    # Data quality — degraded if any key sub-call failed, or any key
    # price entry carries quality=DEGRADED (set by live_prices when
    # yfinance/Stooq disagreed and the prev_close cache had to step in).
    degraded_assets = [d for d, row in key_prices.items() if row.get("quality") == "DEGRADED"]
    data_quality = "DEGRADED" if errors or degraded_assets else "OK"

    # LLM read — gated, only when data is clean.
    ai_read = _ai_read(regime_state, pressure, yields, last_hour, data_quality)

    return {
        "regime": _serialise_regime(regime_state),
        "pressure": {
            "dominant_driver": pressure.get("dominant_driver"),
            "net_risk":        pressure.get("net_risk"),
            "vector":          pressure.get("vector"),
        },
        "yields": {
            "yields":       yields.get("yields") or {},
            "big_movers":   yields.get("big_movers") or [],
            "narrative":    yields.get("narrative"),
            "any_breaking": yields.get("any_breaking", False),
        },
        "last_hour":      last_hour,
        "key_prices":     key_prices,
        "ai_read":        ai_read,
        "data_quality":   data_quality,
        "degraded_assets": degraded_assets,
        "failed_components": [{"name": n, "error": e} for n, e in errors] or None,
        "generated_at":   datetime.now(_IST).strftime("%d-%b-%Y %H:%M IST"),
    }


# ─── Indirection wrappers (kept named so error labels stay readable) ───────
def _pull_macro() -> dict:
    from market_intel import _pull_macro_levels
    return _pull_macro_levels() or {}


def _compute_regime(macro: dict):
    from regime_engine import compute_regime_state
    return compute_regime_state(macro=macro)


def _compute_pressure(macro: dict) -> dict:
    from pressure_vector import compute_pressure_vector
    return compute_pressure_vector(macro)


def _yields() -> dict:
    from yield_watch import get_yield_watch
    return get_yield_watch()


def _live_prices() -> dict:
    from live_prices import get_live_prices
    return get_live_prices()


def _serialise_regime(state) -> dict:
    if state is None:
        return {"composite": None, "confidence": 0, "summary": None,
                "dimensions": {}, "transitions": []}
    return {
        "composite":   state.composite,
        "confidence":  state.confidence,
        "summary":     state.summary,
        "dimensions":  state.dimensions,
        "transitions": state.transitions,
    }
