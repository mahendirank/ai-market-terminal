"""
sentiment_weighting.py — Per-asset weighted sentiment with explainable drivers.

Replaces the loose "asset_scores" dict in market_intel._aggregate_sentiment_by_asset
with a richer aggregation that:

  - Pulls signal from classified events (event_classifier output)
  - Weights each contribution by:
      * Event severity (0-10)
      * Source credibility (event_classifier.SOURCE_CREDIBILITY tier)
      * Recency decay (older news weighs less)
  - Optionally folds in price action signal (RSI/MFI direction if indicators
    pipeline ran for that asset)
  - Surfaces TOP DRIVERS and OPPOSING FACTORS per asset so AI tabs can quote
    them in the response

Output schema per asset:
  {
    "score":       -1.0 .. +1.0   (sentiment),
    "confidence":  0.0 .. 1.0     (sample size × source quality),
    "label":       "BULLISH" | "BEARISH" | "NEUTRAL",
    "sample_size": <count of events folded in>,
    "drivers": [
      {"text": "...", "source": "Reuters", "severity": 8, "weight": 1.2}
    ],
    "opposing": [<same shape, evidence pointing the other way>],
  }

Designed to slot into market_intel.get_intel_snapshot() at the
`sentiment` field, replacing the current simpler version.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

log = logging.getLogger(__name__)


# Universe of assets the aggregator scores. Keep aligned with
# event_classifier asset tags + market_intel._KNOWN_ASSETS.
KNOWN_ASSETS = {
    "GOLD", "SILVER", "DXY", "BTC", "ETH", "OIL", "SPX", "NDX", "NIFTY",
    "BANKNIFTY", "SENSEX", "EUR", "GBP", "JPY", "INR", "USDJPY", "EURUSD",
    "COPPER", "NATGAS", "US10Y", "VIX",
}

# Direction tags from event_classifier → sentiment sign
_DIRECTION_SIGN: dict[str, float] = {
    "BULL_RISK":  +1.0,   # supports buying risk assets
    "BULL_NAME":  +1.0,   # supports the named asset
    "BEAR_RISK":  -1.0,
    "BEAR_NAME":  -1.0,
    "TWO_WAY":    0.0,    # depends on print — neutral until classified per-side
    "NEUTRAL":    0.0,
}


def _recency_weight(age_seconds: float, half_life_hours: float = 12.0) -> float:
    """Exponential decay with given half-life. Recent news weighs more.

    age=0 → 1.0,  age=half_life → 0.5,  age=2×half_life → 0.25
    """
    if age_seconds < 0:
        return 1.0
    half_life_s = half_life_hours * 3600
    return math.exp(-math.log(2) * age_seconds / half_life_s)


def _credibility_weight(source: str) -> float:
    """Return a multiplier 1.0-2.0 from source tier (1-5)."""
    try:
        from event_classifier import SOURCE_CREDIBILITY
    except Exception:
        return 1.0
    tier = SOURCE_CREDIBILITY.get(source or "", 0)
    return 1.0 + (tier * 0.20)   # tier 5 = 2.0×, tier 0 = 1.0×


def aggregate(
    classified_items: list[dict],
    *,
    text_key: str = "text",
    source_key: str = "source",
    ts_key: str = "ts",
    now_ts: Optional[float] = None,
    half_life_hours: float = 12.0,
) -> dict:
    """Aggregate per-asset sentiment from a list of classified news items.

    Each item must have an ``event`` dict from event_classifier (call
    event_classifier.classify_batch() first if needed). Items without
    event tags are skipped.

    Returns:
      {
        "by_asset":   {asset: {score, confidence, label, sample_size,
                                drivers, opposing}, ...},
        "macro_tilt": "BULLISH" | "BEARISH" | "NEUTRAL",
        "tilt_score": <-1.0 .. +1.0>,
        "sample_size": <total events used>,
        "ts":         <unix>,
      }
    """
    now = now_ts or time.time()
    by_asset: dict[str, dict] = {}
    total_w_signed = 0.0
    total_w_abs    = 0.0
    sampled = 0

    for it in classified_items:
        if not isinstance(it, dict):
            continue
        ev = it.get("event")
        if not isinstance(ev, dict):
            continue
        cat       = ev.get("category", "UNKNOWN")
        sev       = float(ev.get("severity") or 0)
        direction = ev.get("direction") or "NEUTRAL"
        assets    = ev.get("affected_assets") or []
        if sev <= 0 or direction == "NEUTRAL":
            continue

        sign = _DIRECTION_SIGN.get(direction, 0.0)
        if sign == 0:
            continue

        # Compute weight: severity × credibility × recency
        cred   = _credibility_weight(it.get(source_key, ""))
        ts_val = float(it.get(ts_key) or now)
        recency = _recency_weight(now - ts_val, half_life_hours=half_life_hours)
        weight = sev * cred * recency

        signed = sign * weight
        sampled += 1
        total_w_abs    += weight
        total_w_signed += signed

        # Apply to each affected asset
        text = it.get(text_key, "")[:140]
        driver = {
            "text":     text,
            "source":   it.get(source_key, ""),
            "category": cat,
            "severity": sev,
            "weight":   round(weight, 2),
            "sign":     +1 if sign > 0 else -1,
        }
        for a in assets:
            au = a.upper()
            if au not in KNOWN_ASSETS:
                continue
            slot = by_asset.setdefault(au, {
                "signed_sum": 0.0, "abs_sum": 0.0, "n": 0,
                "drivers": [], "opposing": [],
            })
            slot["signed_sum"] += signed
            slot["abs_sum"]    += weight
            slot["n"]          += 1
            (slot["drivers"] if sign > 0 else slot["opposing"]).append(driver)

    # Finalize per-asset records
    finalized: dict[str, dict] = {}
    for asset, slot in by_asset.items():
        if slot["abs_sum"] == 0:
            continue
        score = slot["signed_sum"] / slot["abs_sum"]
        # Confidence: log-scaled sample size, capped at 1.0
        confidence = min(1.0, math.log10(1 + slot["n"]) / math.log10(20))
        if   score >= 0.15: label = "BULLISH"
        elif score <= -0.15: label = "BEARISH"
        else:                label = "NEUTRAL"
        # Keep top-3 drivers per side, ranked by weight
        slot["drivers"].sort(key=lambda d: d["weight"], reverse=True)
        slot["opposing"].sort(key=lambda d: d["weight"], reverse=True)
        finalized[asset] = {
            "score":       round(score, 3),
            "confidence":  round(confidence, 3),
            "label":       label,
            "sample_size": slot["n"],
            "drivers":     slot["drivers"][:3],
            "opposing":    slot["opposing"][:3],
        }

    tilt = round(total_w_signed / total_w_abs, 3) if total_w_abs else 0.0
    if   tilt >= 0.15: macro_tilt = "BULLISH"
    elif tilt <= -0.15: macro_tilt = "BEARISH"
    else:               macro_tilt = "NEUTRAL"

    return {
        "by_asset":    finalized,
        "macro_tilt":  macro_tilt,
        "tilt_score":  tilt,
        "sample_size": sampled,
        "ts":          int(now),
    }


def format_for_prompt(agg: dict, *, top_assets: int = 5) -> str:
    """Compact rendering for AI prompts. Shows top drivers per leading asset
    so the model has cited evidence, not just numbers."""
    if not agg or not agg.get("by_asset"):
        return "SENTIMENT: (no classified events in window)"

    lines = [
        f"SENTIMENT TILT: {agg['macro_tilt']} "
        f"(score {agg['tilt_score']:+.2f}, n={agg['sample_size']})"
    ]

    # Rank assets by absolute conviction (|score| × confidence)
    rows = sorted(
        agg["by_asset"].items(),
        key=lambda kv: abs(kv[1]["score"]) * kv[1]["confidence"],
        reverse=True,
    )[:top_assets]

    for asset, info in rows:
        lines.append(
            f"  {asset:<8} {info['label']:<8} "
            f"score {info['score']:+.2f}  conf {info['confidence']:.2f}  "
            f"(n={info['sample_size']})"
        )
        # Show the strongest driver if present
        if info["drivers"]:
            d = info["drivers"][0]
            lines.append(f"    + {d['source']}: {d['text'][:90]}")
        if info["opposing"]:
            d = info["opposing"][0]
            lines.append(f"    - {d['source']}: {d['text'][:90]}")

    return "\n".join(lines)
