"""
market_intel.py — Unified market intelligence snapshot.

Single entry point for AI tabs. Replaces ad-hoc "fetch indices + macro + news +
regime + correlations" boilerplate that lived in HNI / Why Move / Morning Note
/ AI Research with one call:

    from market_intel import get_intel_snapshot, format_intel_for_prompt
    snap = get_intel_snapshot(symbol="GOLD")
    prompt_block = format_intel_for_prompt(snap)

Aggregates:
  1. Multi-source news       — news.get_all_news()  (RSS + Alpaca + Finviz)
  2. Smart dedup + clusters  — intel_cluster.cluster_headlines()
  3. Importance scoring      — score field from news.py + cluster aggregation
  4. Macro event tags        — cb_calendar.get_upcoming()
  5. Earnings impact         — earnings.py recent + upcoming
  6. Market regime           — regime.detect_market_regime()
  7. Cross-asset corr        — correlations.compute_correlations()
  8. Sentiment weighting     — per-asset weighted from ai_layer enrichment
  9. Fear/greed composite    — local composite + CNN F&G overlay
 10. Per-symbol focus        — when symbol given, filter to its asset class
                                + relevant correlations + relevant clusters

Cached 90s in Redis (falls back to in-process). The snapshot is computed
on demand when an AI tab requests it — not on every dashboard tick.

Goal: AI tabs reason from structured intel, not raw headlines.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

log = logging.getLogger(__name__)

# ─── Redis cache (mirrors indicators.py pattern) ─────────────────────────────
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
        log.info("[market_intel] Redis cache: connected")
    except Exception as e:  # noqa: BLE001
        _redis_ok = False
        log.warning("[market_intel] Redis unavailable (%s) — using in-process cache", e)


_init_redis()

_INPROC_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 90  # seconds


def _cache_get(key: str) -> Optional[dict]:
    if _redis_ok and _redis_client:
        try:
            raw = _redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
    entry = _INPROC_CACHE.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    return None


def _cache_put(key: str, value: dict, ttl: int = _CACHE_TTL) -> None:
    if _redis_ok and _redis_client:
        try:
            _redis_client.setex(key, ttl, json.dumps(value, default=str))
        except Exception:  # noqa: BLE001
            pass
    _INPROC_CACHE[key] = (time.time() + ttl, value)


# ─── Component pulls — each wrapped so failures degrade gracefully ───────────
def _pull_regime() -> dict:
    try:
        from regime import detect_market_regime
        r = detect_market_regime() or {}
        return {
            "regime": r.get("regime") or r.get("market_phase") or "mixed",
            "confidence": r.get("confidence", 0),
            "commentary": r.get("commentary", ""),
            "dominant_driver": r.get("dominant_driver", ""),
            "dimensions": r.get("dimensions", {}),
        }
    except Exception as e:  # noqa: BLE001
        log.debug("regime pull failed: %s", e)
        return {"regime": "unknown", "confidence": 0, "commentary": "", "dominant_driver": ""}


def _pull_correlations() -> dict:
    try:
        from correlations import compute_correlations
        c = compute_correlations() or {}
        # Expect shape: {"pairs": [{"a","b","corr","expected","sigma"}, ...]}
        if isinstance(c, dict):
            pairs = c.get("pairs", c.get("data", []))
        else:
            pairs = list(c) if isinstance(c, list) else []
        anomalies = [p for p in pairs
                     if isinstance(p, dict) and abs(p.get("sigma") or 0) >= 1.5][:6]
        return {"current": pairs[:12], "anomalies": anomalies}
    except Exception as e:  # noqa: BLE001
        log.debug("correlations pull failed: %s", e)
        return {"current": [], "anomalies": []}


def _pull_cb_events(days: int = 7) -> list[dict]:
    try:
        import cb_calendar as cb
        if hasattr(cb, "get_upcoming"):
            return cb.get_upcoming(days=days) or []
    except Exception as e:  # noqa: BLE001
        log.debug("cb_calendar pull failed: %s", e)
    return []


def _pull_econ_events(days: int = 3) -> list[dict]:
    try:
        from econ_calendar import get_calendar
        data = get_calendar(days_ahead=days) or {}
        return data.get("events", [])[:10]
    except Exception as e:  # noqa: BLE001
        log.debug("econ_calendar pull failed: %s", e)
    return []


def _pull_earnings() -> dict:
    try:
        from earnings import get_earnings_data
        d = get_earnings_data() or {}
        return {
            "today":   d.get("today", [])[:8],
            "this_week": d.get("upcoming", d.get("this_week", []))[:10],
            "high_impact": d.get("high_impact", [])[:6],
        }
    except Exception:
        return {"today": [], "this_week": [], "high_impact": []}


def _pull_cnn_fng() -> dict:
    try:
        from market_sentiment import get_cnn_fng
        f = get_cnn_fng() or {}
        if "error" in f:
            return {"score": None, "label": None, "error": f.get("error")}
        return {
            "score": f.get("score") or f.get("now"),
            "label": f.get("label") or f.get("classification"),
            "previous_close": f.get("previous_close"),
            "one_week_ago":   f.get("one_week_ago"),
            "one_month_ago":  f.get("one_month_ago"),
        }
    except Exception:
        return {"score": None, "label": None}


def _pull_macro_levels() -> dict:
    """Pull DXY / US10Y / VIX / GOLD / OIL / BTC current levels."""
    try:
        from macro import get_macro_data
        m = get_macro_data() or {}
    except Exception:
        m = {}
    out = {
        "dxy":    None, "us10y":  None, "vix":    None,
        "gold":   None, "oil":    None, "btc":    None,
    }
    fx     = m.get("fx",     m.get("FX", {})) or {}
    yields = m.get("yields", m.get("US_YIELDS", {})) or {}
    if isinstance(fx, dict):
        out["dxy"] = fx.get("DXY")
    if isinstance(yields, dict):
        out["us10y"] = yields.get("US_10Y") or yields.get("10Y")
    out["vix"]  = m.get("vix",  m.get("VIX"))
    out["gold"] = m.get("gold", m.get("GOLD_SPOT"))
    out["oil"]  = m.get("oil",  m.get("OIL"))
    out["btc"]  = m.get("btc",  m.get("BTC"))
    return out


def _pull_news_enriched() -> list[dict]:
    """Pull AI-enriched news (with sentiment/impact/assets fields).
    Falls back to raw news if enrichment is unavailable.
    """
    try:
        from news import get_all_news
        items = get_all_news() or []
    except Exception:
        return []
    # Flatten any (score, item) tuples to dicts with score embedded
    flat: list[dict] = []
    for entry in items:
        if isinstance(entry, dict):
            flat.append(entry)
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            s, it = entry
            if isinstance(it, dict):
                flat.append({**it, "score": float(s)})
    return flat[:120]   # bound the clustering work


# ─── Derived intelligence ────────────────────────────────────────────────────
_KNOWN_ASSETS = {"GOLD","SILVER","DXY","BTC","ETH","OIL","SPX","NDX","NIFTY",
                 "BANKNIFTY","SENSEX","EUR","GBP","JPY","INR","COPPER","NATGAS"}


def _aggregate_sentiment_by_asset(news_items: list[dict]) -> dict:
    """Per-asset sentiment: impact-weighted average of BULL/NEU/BEAR scores
    across all enriched news items.

    Returns:
      {"asset_scores": {GOLD: 0.4, NIFTY: -0.2, ...},   # -1..+1
       "macro_tilt": "BULLISH"|"BEARISH"|"NEUTRAL",
       "tilt_score": float -1..+1,
       "sample_size": N}
    """
    SENTI_MAP = {"BULL": 1.0, "BULLISH": 1.0,
                 "NEU":  0.0, "NEUTRAL":  0.0, "MIXED": 0.0,
                 "BEAR": -1.0, "BEARISH": -1.0}

    asset_acc: dict[str, list[tuple[float, float]]] = {}   # asset → [(weight, score)]
    total_w, total_s = 0.0, 0.0
    sampled = 0

    for it in news_items:
        senti = SENTI_MAP.get(str(it.get("sentiment", "")).upper())
        impact = it.get("impact")
        if senti is None or not isinstance(impact, (int, float)):
            continue
        assets = it.get("assets") or []
        if not assets and it.get("ai_assets"):
            assets = it["ai_assets"]
        weight = max(0.0, float(impact))
        if weight == 0:
            continue
        sampled += 1
        total_w += weight
        total_s += senti * weight
        for a in assets:
            au = str(a).upper().strip()
            if au in _KNOWN_ASSETS:
                asset_acc.setdefault(au, []).append((weight, senti))

    asset_scores = {}
    for a, rows in asset_acc.items():
        w_sum = sum(r[0] for r in rows)
        if w_sum:
            asset_scores[a] = round(sum(r[0] * r[1] for r in rows) / w_sum, 3)

    tilt_score = round(total_s / total_w, 3) if total_w else 0.0
    if tilt_score >= 0.15:    label = "BULLISH"
    elif tilt_score <= -0.15: label = "BEARISH"
    else:                     label = "NEUTRAL"

    return {
        "asset_scores": asset_scores,
        "macro_tilt":   label,
        "tilt_score":   tilt_score,
        "sample_size":  sampled,
    }


def _safe_float(x) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _local_fear_greed(macro: dict, sentiment_agg: dict, regime: dict,
                      cnn: dict) -> dict:
    """Composite local F&G [0..100]. Blends:
      - VIX (lower = greed, higher = fear)
      - News sentiment tilt
      - Regime confidence × direction
      - CNN F&G as anchor (if available)

    Each component scaled to 0..100, weighted average. Returns score + the
    component breakdown so the UI can show the decomposition.
    """
    components: dict[str, dict] = {}

    # VIX component — clip to 10..40, invert so high VIX = low component
    vix_obj = macro.get("vix")
    vix_val = None
    if isinstance(vix_obj, dict):
        vix_val = _safe_float(vix_obj.get("price"))
    else:
        vix_val = _safe_float(vix_obj)
    if vix_val is not None:
        clipped = max(10.0, min(40.0, vix_val))
        # 10 VIX → 100 (greed),  40 VIX → 0 (fear)
        vix_score = round(100 - ((clipped - 10) / 30) * 100, 1)
        components["vix"] = {"value": vix_val, "score": vix_score, "weight": 0.25}

    # News sentiment tilt
    tilt = sentiment_agg.get("tilt_score", 0.0)
    sample = sentiment_agg.get("sample_size", 0)
    if sample >= 5:
        senti_score = round(50 + (tilt * 50), 1)
        components["news_sentiment"] = {
            "tilt": tilt, "label": sentiment_agg.get("macro_tilt"),
            "score": senti_score, "weight": 0.25,
        }

    # Regime — bullish regimes → greed, bearish → fear
    regime_name = (regime.get("regime") or "").lower()
    regime_conf = float(regime.get("confidence") or 0)
    if regime_name:
        direction = 0.0
        if any(k in regime_name for k in ("bull", "risk_on", "goldilocks", "breakout", "accumulation")):
            direction = 1.0
        elif any(k in regime_name for k in ("bear", "risk_off", "crisis", "deflation", "distribution")):
            direction = -1.0
        regime_score = round(50 + direction * (regime_conf / 2.0), 1)
        components["regime"] = {
            "name": regime_name, "confidence": regime_conf,
            "score": regime_score, "weight": 0.20,
        }

    # CNN F&G as anchor
    cnn_score = _safe_float(cnn.get("score"))
    if cnn_score is not None:
        components["cnn_fng"] = {
            "score": cnn_score, "label": cnn.get("label"), "weight": 0.30,
        }

    if not components:
        return {"score": None, "label": None, "components": {}}

    total_w = sum(c["weight"] for c in components.values())
    composite = sum(c["score"] * c["weight"] for c in components.values()) / total_w
    score = round(composite, 1)

    if   score >= 75: label = "Extreme Greed"
    elif score >= 55: label = "Greed"
    elif score >= 45: label = "Neutral"
    elif score >= 25: label = "Fear"
    else:             label = "Extreme Fear"

    return {"score": score, "label": label, "components": components}


# ─── Public entry point ──────────────────────────────────────────────────────
def get_intel_snapshot(symbol: Optional[str] = None, *,
                       max_clusters: int = 20, force: bool = False) -> dict:
    """Return the full structured market intelligence snapshot.

    Parameters
    ----------
    symbol : str, optional
        When provided (e.g. "GOLD"), filters correlations + relevant news
        clusters to that asset class. Other components stay market-wide.
    max_clusters : int
        Cap on returned news clusters.
    force : bool
        Skip cache and recompute.
    """
    # Bump cache key on v2 so old shape doesn't get served from cache after rollout
    cache_key = f"intel:snap:v2:{symbol or '_market_'}:{max_clusters}"
    if not force:
        cached = _cache_get(cache_key)
        if cached:
            return cached

    start = time.time()

    # ── Parallel pulls — each component is independent + most are I/O bound.
    # Sequential was ~70s on cold start (one slow pull blocking the rest).
    # ThreadPool brings it under ~5s typically, capped by the slowest pull.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as _ex:
        futures = {
            "regime":   _ex.submit(_pull_regime),
            "correl":   _ex.submit(_pull_correlations),
            "cb":       _ex.submit(_pull_cb_events, 7),
            "econ":     _ex.submit(_pull_econ_events, 3),
            "earnings": _ex.submit(_pull_earnings),
            "cnn":      _ex.submit(_pull_cnn_fng),
            "macro":    _ex.submit(_pull_macro_levels),
            "news":     _ex.submit(_pull_news_enriched),
        }

        def _await(name: str, default):
            try:
                return futures[name].result(timeout=10)
            except Exception as e:  # noqa: BLE001
                log.debug("intel pull %s failed/timed out: %s", name, e)
                return default

        regime   = _await("regime",   {"regime":"unknown","confidence":0,"commentary":"","dominant_driver":""})
        correl   = _await("correl",   {"current": [], "anomalies": []})
        cb       = _await("cb",       [])
        econ     = _await("econ",     [])
        earnings = _await("earnings", {"today": [], "this_week": [], "high_impact": []})
        cnn      = _await("cnn",      {"score": None, "label": None})
        macro    = _await("macro",    {})
        news_raw = _await("news",     [])

    # ── v2 INTEL LAYER — event classification → dedup → regime engine →
    #    sentiment weighting → market memory (analog finder)
    # Each step degrades gracefully: an import or compute failure on any
    # v2 module falls back to the v1 path so existing behaviour is preserved.

    # Step 1: classify each news item by event taxonomy. Adds `event` field
    # in-place. Powers downstream sentiment_weighting + regime FED/INFL dims.
    try:
        from event_classifier import classify_batch, summarize_distribution
        news_raw = classify_batch(news_raw)
        events_summary = summarize_distribution(news_raw)
    except Exception as e:
        log.debug("event_classifier failed: %s", e)
        events_summary = {}

    # Step 2: dedup. Use the stronger news_deduper when available, fall back
    # to intel_cluster otherwise.
    try:
        from news_deduper import dedupe_news, compression_stats as v2_stats
        clusters = dedupe_news(news_raw, max_clusters=max_clusters)
        cluster_stats = v2_stats(len(news_raw), clusters)
    except Exception as e:
        log.debug("news_deduper failed, falling back to intel_cluster: %s", e)
        from intel_cluster import cluster_headlines, compression_stats
        clusters = cluster_headlines(news_raw, max_clusters=max_clusters)
        cluster_stats = compression_stats(len(news_raw), clusters)

    # Step 3: per-asset weighted sentiment (uses event classifications).
    # If classifications missing, falls back to the v1 aggregator.
    try:
        from sentiment_weighting import aggregate as weighted_aggregate
        sentiment_v2 = weighted_aggregate(news_raw)
        if sentiment_v2.get("sample_size", 0) > 0:
            sentiment_agg = {
                "asset_scores": {k: v["score"] for k, v in sentiment_v2.get("by_asset", {}).items()},
                "macro_tilt":   sentiment_v2.get("macro_tilt", "NEUTRAL"),
                "tilt_score":   sentiment_v2.get("tilt_score", 0.0),
                "sample_size":  sentiment_v2.get("sample_size", 0),
                "by_asset":     sentiment_v2.get("by_asset", {}),   # rich form
            }
        else:
            sentiment_agg = _aggregate_sentiment_by_asset(news_raw)
    except Exception as e:
        log.debug("sentiment_weighting failed: %s", e)
        sentiment_agg = _aggregate_sentiment_by_asset(news_raw)

    # Step 4: multi-dimensional regime state (overlays the v1 regime label).
    try:
        from regime_engine import compute_regime_state
        regime_state = compute_regime_state(macro=macro, news_tilt=events_summary,
                                            persist=True).to_dict()
    except Exception as e:
        log.debug("regime_engine failed: %s", e)
        regime_state = {}

    # Step 5: local composite F&G — uses regime + sentiment + macro + CNN
    fng = _local_fear_greed(macro=macro, sentiment_agg=sentiment_agg,
                             regime=regime, cnn=cnn)

    # Step 6: historical analogs — find K nearest past states. Record this
    # snapshot for future analog searches. Skip on first run if memory empty.
    analogs: list[dict] = []
    try:
        from market_memory import (
            record_snapshot, find_analogs, seed_classic_analogs,
            last_snapshot_ts,
        )
        # One-time seed of well-known historical episodes
        seed_classic_analogs()
        # Build a feature vector matching market_memory's expected schema
        dims = regime_state.get("dimensions", {}) if regime_state else {}
        current_state = {
            "regime":         regime_state.get("composite") or regime.get("regime"),
            "risk_score":     (dims.get("RISK") or {}).get("score"),
            "infl_score":     (dims.get("INFLATION") or {}).get("score"),
            "fed_score":      (dims.get("FED") or {}).get("score"),
            "vol_score":      (dims.get("VOLATILITY") or {}).get("score"),
            "credit_score":   (dims.get("CREDIT") or {}).get("score"),
            "breadth_score":  (dims.get("BREADTH") or {}).get("score"),
            "fng_local":      (fng or {}).get("score"),
            "vix":            (macro.get("vix") or {}).get("price") if isinstance(macro.get("vix"), dict) else macro.get("vix"),
            "us10y":          (macro.get("us10y") or {}).get("price") if isinstance(macro.get("us10y"), dict) else macro.get("us10y"),
            "dxy":            (macro.get("dxy") or {}).get("price") if isinstance(macro.get("dxy"), dict) else macro.get("dxy"),
            "sentiment_tilt": sentiment_agg.get("tilt_score", 0.0),
        }
        analogs = find_analogs(current_state, k=3, min_age_hours=24)
        # Persist current — but throttle to hourly to avoid DB spam
        prior_ts = last_snapshot_ts()
        if not prior_ts or (time.time() - prior_ts) > 3600:
            record_snapshot(
                regime_state=regime_state, macro=macro,
                fng={"local": fng, "cnn": cnn},
                sentiment_tilt=sentiment_agg.get("tilt_score"),
                commentary=regime.get("commentary", "")[:160],
            )
    except Exception as e:
        log.debug("market_memory failed: %s", e)

    # Optional per-symbol filter
    symbol_focus = None
    if symbol:
        sym_upper = symbol.upper()
        related_clusters = [c for c in clusters
                            if sym_upper in (c.get("tickers") or [])
                            or any(sym_upper in (h.get("text", "").upper())
                                   for h in c.get("headlines", []))]
        symbol_focus = {
            "symbol": sym_upper,
            "sentiment_score": sentiment_agg["asset_scores"].get(sym_upper),
            "related_clusters": related_clusters[:6],
        }

    snap = {
        "ts": int(time.time()),
        "computed_in_ms": int((time.time() - start) * 1000),
        "schema_version": 2,
        # Legacy single-label regime (kept for backwards-compat)
        "regime": regime,
        # NEW — multi-dimensional state vector from regime_engine
        "regime_state": regime_state,
        "macro_snapshot": macro,
        "correlations": correl,
        "fear_greed": {
            "local": fng,
            "cnn":   cnn,
        },
        "sentiment": sentiment_agg,
        # NEW — event taxonomy summary
        "events_classified": events_summary,
        "events": {
            "central_bank":     cb,
            "economic":         econ,
            "earnings_today":   earnings.get("today", []),
            "earnings_week":    earnings.get("this_week", []),
            "earnings_high_impact": earnings.get("high_impact", []),
        },
        "news": {
            "clusters":     clusters,
            "stats":        cluster_stats,
            "macro_tilt":   sentiment_agg["macro_tilt"],
        },
        # NEW — historical analog matches
        "analogs": analogs,
        "symbol_focus": symbol_focus,
    }

    _cache_put(cache_key, snap)
    return snap


# ════════════════════════════════════════════════════════════════════════════
# LAYER 2 — Compact state block (used by prompt_builder)
# ════════════════════════════════════════════════════════════════════════════
# Tight syntax. No verbose section headers. Drops anything redundant with
# Layer 1 (persona) or Layer 3 (task/schema). The model reads, doesn't reason
# about the format.
#
# Target: ≤ 1000 chars for typical snapshots (~250 tokens). Compare to
# format_intel_for_prompt's ~2000 chars (~500 tokens).
def format_state_compact(snap: dict, *, max_clusters: int = 8,
                          max_anomalies: int = 4, max_events: int = 5) -> str:
    """Compact L2 state block for new-architecture prompt_builder.

    Drops the verbose human-readable section headers used in the legacy
    formatter. Single short line per concept where possible.
    """
    if not snap or snap.get("error"):
        return "STATE: unavailable"

    lines: list[str] = []

    # Regime — prefer v2 state vector summary when meaningful
    rs = snap.get("regime_state") or {}
    r  = snap.get("regime", {}) or {}
    if rs.get("composite") and rs.get("confidence", 0) > 0:
        # Dimensions as a single dense line: "RISK=70 INFL=55 FED=50 VOL=15 CRD=45 BRD=60"
        dims = rs.get("dimensions") or {}
        dim_bits = " ".join(
            f"{k[:4]}={int((dims.get(k) or {}).get('score') or 0)}"
            for k in ("RISK", "INFLATION", "FED", "VOLATILITY", "CREDIT", "BREADTH")
            if dims.get(k)
        )
        lines.append(f"REG: {rs['composite']}/{rs.get('confidence',0)}%  {dim_bits}")
        for t in (rs.get("transitions") or [])[:2]:
            lines.append(f"  Δ{t['dim']}:{t['prev_score']:.0f}→{t['curr_score']:.0f}")
    elif r.get("regime"):
        lines.append(f"REG: {r.get('regime','?')}/{r.get('confidence',0)}%")

    # Fear/greed — both gauges on one line
    fg = snap.get("fear_greed") or {}
    local = (fg.get("local") or {}).get("score")
    cnn   = (fg.get("cnn")   or {}).get("score")
    if local is not None or cnn is not None:
        lines.append(f"FNG: local={local}  cnn={cnn}")

    # Sentiment tilt
    s = snap.get("sentiment") or {}
    if s.get("sample_size"):
        bits = [f"score={s.get('tilt_score',0):+.2f}", f"n={s.get('sample_size',0)}"]
        # Top 4 per-asset (handles both v1 and v2 shapes)
        rich = s.get("by_asset") if isinstance(s.get("by_asset"), dict) else None
        if rich:
            ranked = sorted(rich.items(),
                            key=lambda kv: abs(kv[1].get("score",0)), reverse=True)[:4]
            bits.append(" ".join(f"{k}{v['score']:+.2f}" for k, v in ranked))
        elif s.get("asset_scores"):
            ranked = sorted(s["asset_scores"].items(),
                            key=lambda kv: abs(kv[1]), reverse=True)[:4]
            bits.append(" ".join(f"{k}{v:+.2f}" for k, v in ranked))
        lines.append(f"TILT[{s.get('macro_tilt','?')}]: " + " ".join(bits))

    # Macro snapshot — single line, key levels only
    m = snap.get("macro_snapshot") or {}
    macro_bits = []
    for k in ("dxy", "us10y", "vix", "gold", "oil", "btc"):
        v = m.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            p = v.get("price") or v.get("last")
            macro_bits.append(f"{k.upper()}:{p}")
        else:
            macro_bits.append(f"{k.upper()}:{v}")
    if macro_bits:
        lines.append("MAC: " + " ".join(macro_bits))

    # Correlation anomalies (one per line, σ-deviation only)
    anomalies = (snap.get("correlations") or {}).get("anomalies", [])
    for a in anomalies[:max_anomalies]:
        try:
            lines.append(
                f"COR: {a.get('a','?')}/{a.get('b','?')} "
                f"now={a.get('corr',0):+.2f} exp={a.get('expected',0):+.2f} "
                f"({a.get('sigma',0):+.1f}σ)"
            )
        except Exception:
            continue

    # Event distribution one-liner
    by_cat = ((snap.get("events_classified") or {}).get("by_category")) or {}
    if by_cat:
        top = sorted(by_cat.items(), key=lambda kv: kv[1].get("max_sev", 0), reverse=True)[:4]
        lines.append("EVT: " + " ".join(
            f"{c[:6]}={d.get('count',0)}/s{d.get('max_sev',0)}"
            for c, d in top
        ))

    # Upcoming events
    events = snap.get("events") or {}
    cb   = events.get("central_bank", [])
    econ = events.get("economic", [])
    ern  = events.get("earnings_high_impact", [])
    upcoming = []
    for e in cb[:2]:
        upcoming.append(f"CB {(e.get('date') or '')[:10]} {e.get('bank','')[:4]} {(e.get('event') or '')[:35]}")
    for e in econ[:2]:
        upcoming.append(f"ECON {(e.get('date') or '')[:10]} {e.get('country','')[:4]} {(e.get('event') or '')[:35]}")
    for e in ern[:1]:
        tk = e.get('ticker') or e.get('symbol','')
        upcoming.append(f"ERN {(e.get('date','') or '')[:10]} {tk} {(e.get('company','') or '')[:30]}")
    if upcoming:
        lines.append("UPCOMING:")
        for u in upcoming[:max_events]:
            lines.append(f"  {u}")

    # Top clusters — terse
    clusters = (snap.get("news") or {}).get("clusters", [])[:max_clusters]
    if clusters:
        lines.append("CLUSTERS:")
        for c in clusters:
            srcs = ",".join(c.get("sources", [])[:2])
            ev   = (c.get("event") or {}).get("category", "")
            sev  = (c.get("event") or {}).get("severity", "")
            tag  = f" [{ev}/s{sev}]" if ev else ""
            lines.append(f"  • {c.get('topic','')[:110]}  ({srcs},n={c.get('size',1)}){tag}")

    # Historical analogs — one per line, terse
    analogs = snap.get("analogs") or []
    if analogs:
        lines.append("ANALOGS:")
        for a in analogs[:3]:
            fr = a.get("forward_returns") or {}
            fr_bits = ""
            if fr:
                fr_bits = "  fwd:" + ",".join(f"{k}{v:+.2f}%" for k, v in list(fr.items())[:2])
            lines.append(
                f"  • {(a.get('date_label') or '?')[:30]}  d={a.get('distance',0):.2f}{fr_bits}  "
                f"{(a.get('commentary') or '')[:60]}"
            )

    # Symbol focus (compact)
    sf = snap.get("symbol_focus")
    if sf:
        lines.append(
            f"FOCUS[{sf.get('symbol','')}]: senti={sf.get('sentiment_score','-')}  "
            f"related={len(sf.get('related_clusters', []))}"
        )

    return "\n".join(lines)


# ─── Prompt formatter — what AI tabs paste into their user message ──────────
def format_intel_for_prompt(snap: dict, *, include_clusters: int = 10) -> str:
    """Render the snapshot as a tight text block AI prompts can paste in.

    Replaces the ad-hoc news_block + regime_block + macro_block boilerplate
    in HNI / Why Move / Morning Note with a single structured intel section.
    """
    if not snap or snap.get("error"):
        return "=== STRUCTURED INTEL ===\n(unavailable)"

    lines: list[str] = ["=== STRUCTURED MARKET INTEL ==="]

    # Regime — prefer v2 state vector when available (richer + more reliable)
    rs = snap.get("regime_state") or {}
    r  = snap.get("regime", {}) or {}
    legacy_unknown = (not r.get("regime")) or r.get("regime") == "unknown"
    if rs.get("composite") and (rs.get("confidence", 0) > 0 or legacy_unknown):
        # Show summary from regime_state (full dimension breakdown follows below)
        lines.append(f"REGIME:    {rs['composite']} ({rs.get('confidence',0)}% conf)")
    elif r:
        conf = r.get("confidence", 0)
        lines.append(f"REGIME:    {r.get('regime','?')} ({conf}% conf) — {r.get('commentary','')[:120]}")
        if r.get("dominant_driver"):
            lines.append(f"  driver:  {r['dominant_driver']}")

    # Fear/Greed
    fg = snap.get("fear_greed", {})
    local = fg.get("local") or {}
    cnn   = fg.get("cnn") or {}
    if local.get("score") is not None or cnn.get("score") is not None:
        bits = []
        if local.get("score") is not None:
            bits.append(f"local {local['score']} ({local.get('label','')})")
        if cnn.get("score") is not None:
            bits.append(f"CNN {cnn['score']} ({cnn.get('label','')})")
        lines.append(f"FEAR/GREED:  " + "  ·  ".join(bits))

    # Sentiment tilt
    s = snap.get("sentiment", {})
    if s.get("sample_size"):
        lines.append(f"NEWS TILT: {s.get('macro_tilt','?')}  "
                     f"(score {s.get('tilt_score',0):+.2f}, n={s.get('sample_size',0)})")
        top_assets = sorted(s.get("asset_scores", {}).items(),
                            key=lambda x: abs(x[1]), reverse=True)[:5]
        if top_assets:
            lines.append("  per-asset: " + "  ".join(
                f"{a}{v:+.2f}" for a, v in top_assets))

    # Macro snapshot
    m = snap.get("macro_snapshot", {})
    macro_bits = []
    for k in ("dxy", "us10y", "vix", "gold", "oil", "btc"):
        v = m.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            p = v.get("price") or v.get("last") or ""
            c = v.get("change_pct") or v.get("change") or ""
            macro_bits.append(f"{k.upper()}:{p}({c}%)" if c != "" else f"{k.upper()}:{p}")
        else:
            macro_bits.append(f"{k.upper()}:{v}")
    if macro_bits:
        lines.append("MACRO:     " + "  ".join(macro_bits))

    # Cross-asset anomalies (correlations that broke)
    anomalies = snap.get("correlations", {}).get("anomalies", [])
    if anomalies:
        lines.append("CORR ANOMALIES:")
        for a in anomalies[:4]:
            try:
                lines.append(f"  {a.get('a','?')}/{a.get('b','?')}  "
                             f"now {a.get('corr',0):+.2f}  exp {a.get('expected',0):+.2f}  "
                             f"({a.get('sigma',0):+.1f}σ)")
            except Exception:
                continue

    # Upcoming events (CB + econ + earnings)
    cb = snap.get("events", {}).get("central_bank", [])
    econ = snap.get("events", {}).get("economic", [])
    eweek = snap.get("events", {}).get("earnings_high_impact", [])
    if cb or econ or eweek:
        lines.append("UPCOMING (next 7d):")
        for e in (cb or [])[:3]:
            lines.append(f"  CB    {e.get('date','')[:10]}  {e.get('bank','')}  {e.get('event','')}")
        for e in (econ or [])[:3]:
            lines.append(f"  ECON  {e.get('date','')[:10]}  {e.get('country','')}  {e.get('event','')}")
        for e in (eweek or [])[:3]:
            ticker = e.get('ticker') or e.get('symbol','')
            when   = e.get('date','') or e.get('reportDate','')
            lines.append(f"  ERN   {when[:10]}  {ticker}  {e.get('company','')[:40]}")

    # Top clusters — the meat
    clusters = snap.get("news", {}).get("clusters", [])[:include_clusters]
    if clusters:
        stats = snap.get("news", {}).get("stats", {})
        lines.append(f"TOP STORIES (clustered {stats.get('raw',0)}→{stats.get('clusters',0)}):")
        for c in clusters:
            srcs = ",".join(c.get("sources", [])[:3])
            ticks = ",".join(c.get("tickers", [])[:4])
            extra = f" [{ticks}]" if ticks else ""
            lines.append(f"  • {c.get('topic','')[:120]}  ({srcs}, n={c.get('size',1)}, "
                         f"imp={c.get('max_score',0):.0f}){extra}")

    # v2: Regime state vector dimensions (one line each)
    rs = snap.get("regime_state") or {}
    if rs.get("dimensions"):
        lines.append(f"REGIME STATE: {rs.get('summary','')}")
        if rs.get("transitions"):
            for t in rs["transitions"][:3]:
                lines.append(
                    f"  ⚠ TRANSITION {t['dim']}: {t['prev_score']} → {t['curr_score']} "
                    f"(Δ{t['delta']:+.1f}, age {t.get('age_minutes',0)}min)"
                )

    # v2: Event taxonomy directional tilt
    ev_sum = snap.get("events_classified") or {}
    by_cat = ev_sum.get("by_category") or {}
    if by_cat:
        top_cats = sorted(by_cat.items(), key=lambda kv: kv[1].get("count", 0), reverse=True)[:4]
        bits = "  ".join(f"{c}({d.get('count',0)},sev{d.get('max_sev',0)})"
                          for c, d in top_cats)
        lines.append(f"EVENT MIX: {bits}")

    # v2: Top sentiment drivers (one strongest BULL + one strongest BEAR)
    rich_sent = snap.get("sentiment", {}).get("by_asset", {})
    if isinstance(rich_sent, dict) and rich_sent:
        # find the asset with strongest absolute conviction
        ranked = sorted(rich_sent.items(),
                        key=lambda kv: abs(kv[1].get("score",0)) * kv[1].get("confidence",0),
                        reverse=True)
        for asset, info in ranked[:2]:
            if info.get("drivers"):
                d = info["drivers"][0]
                lines.append(f"DRIVER {asset}: + {d.get('source','?')}: {d.get('text','')[:90]}")
            if info.get("opposing"):
                d = info["opposing"][0]
                lines.append(f"OPPOSE {asset}: - {d.get('source','?')}: {d.get('text','')[:90]}")

    # v2: Historical analogs (only if we have non-trivial matches)
    analogs = snap.get("analogs") or []
    if analogs:
        lines.append("ANALOGS (closest historical states):")
        for a in analogs[:3]:
            extras = ""
            fr = a.get("forward_returns") or {}
            if fr:
                extras = "  fwd: " + ", ".join(f"{k}:{v:+.2f}%" for k, v in list(fr.items())[:3])
            lines.append(f"  • {a.get('date_label','?')}  dist={a.get('distance',0):.2f}  "
                         f"{(a.get('commentary') or '')[:90]}{extras}")

    # Symbol focus
    sf = snap.get("symbol_focus")
    if sf:
        lines.append(f"FOCUS {sf.get('symbol','')}: sentiment {sf.get('sentiment_score','—')}, "
                     f"{len(sf.get('related_clusters', []))} related clusters")

    return "\n".join(lines)
