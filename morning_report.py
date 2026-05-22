"""
morning_report.py — Grounded global pre-market briefing.

Covers 8 markets: China, Japan, India, Germany, UK, Italy, France, USA.

Directional bias is decided ONLY by deterministic engines via
bias_consensus_engine. The LLM layer (narrate_brief) may explain the
deterministic output but can never change the direction — a contradiction
guard rejects any LLM narration that opposes the consensus.

Per-market brief:
  computed_bias      — from bias_consensus_engine (deterministic)
  confidence         — from confidence_engine (deterministic)
  support/resistance — derived from EMA levels (deterministic)
  overnight_catalysts— filtered news clusters
  macro_drivers      — global regime / F&G / DXY / yields / VIX
  risk_warnings      — vol band, event proximity, correlation anomalies
  narrative          — OPTIONAL LLM prose, gated + contradiction-checked

Performance design (VPS-friendly):
  - Global engines (macro_reasoning, regime, events, sentiment,
    correlation) computed ONCE per report, shared across all 8 markets.
  - Only indicators.compute_indicators() runs per-market — and that is
    itself Redis-cached.
  - Per-market briefs cached with staggered TTLs so refreshes don't all
    fire together (avoids an 8× yfinance burst).
  - All blocking work is sync and meant to run off the event loop
    (the endpoint serves from cache; refresh happens in a worker thread).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from bias_consensus_engine import Signal, compute_consensus, scan_for_contradiction
from confidence_engine import compute_confidence, is_high_conviction

log = logging.getLogger(__name__)


# ─── Market registry ─────────────────────────────────────────────────────────
# Each market: display name, primary index ticker (for indicators + S/R),
# secondary tickers (context), and keyword set for catalyst filtering.
MARKETS: dict[str, dict] = {
    "CHINA":   {"name": "China",   "primary": "^HSI",      "secondary": ["000001.SS"],
                "keywords": ["china", "chinese", "pboc", "yuan", "renminbi",
                              "hang seng", "shanghai", "beijing", "hong kong"]},
    "JAPAN":   {"name": "Japan",   "primary": "^N225",     "secondary": [],
                "keywords": ["japan", "japanese", "boj", "yen", "nikkei", "tokyo"]},
    "INDIA":   {"name": "India",   "primary": "^NSEI",     "secondary": ["^NSEBANK"],
                "keywords": ["india", "indian", "rbi", "rupee", "nifty", "sensex",
                              "mumbai", "nse", "bse"]},
    "GERMANY": {"name": "Germany", "primary": "^GDAXI",    "secondary": [],
                "keywords": ["germany", "german", "dax", "bundesbank", "frankfurt"]},
    "UK":      {"name": "UK",      "primary": "^FTSE",     "secondary": [],
                "keywords": ["uk", "britain", "british", "boe", "sterling",
                              "pound", "ftse", "london"]},
    "ITALY":   {"name": "Italy",   "primary": "FTSEMIB.MI","secondary": [],
                "keywords": ["italy", "italian", "ftse mib", "milan", "btp"]},
    "FRANCE":  {"name": "France",  "primary": "^FCHI",     "secondary": [],
                "keywords": ["france", "french", "cac", "paris"]},
    "USA":     {"name": "USA",     "primary": "^GSPC",     "secondary": ["^NDX", "^DJI"],
                "keywords": ["us ", "u.s.", "usa", "fed", "fomc", "powell",
                              "dollar", "s&p", "nasdaq", "dow", "wall street"]},
}

# Order the report renders markets in (follows the trading day west→east→west).
MARKET_ORDER = ["CHINA", "JAPAN", "INDIA", "GERMANY", "UK", "ITALY", "FRANCE", "USA"]


# ─── Regime → directional score map (shared by macro_reasoning + regime) ─────
_REGIME_DIRECTION: dict[str, float] = {
    "RISK_ON": 0.55, "MELT_UP": 0.70, "GOLDILOCKS": 0.60, "REFLATION": 0.40,
    "BULL_MOMENTUM": 0.55, "BREAKOUT": 0.40, "ACCUMULATION": 0.30,
    "RISK_OFF": -0.60, "CRISIS": -0.90, "TIGHTENING_PANIC": -0.70,
    "STAGFLATION": -0.45, "GROWTH_SCARE": -0.60, "BEAR_PRESSURE": -0.55,
    "DISTRIBUTION": -0.40, "INFLATIONARY": -0.20,
    "SIDEWAYS": 0.0, "MIXED": 0.0, "UNKNOWN": 0.0,
}


# ─── Redis cache (mirrors market_intel.py pattern) ──────────────────────────
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
        log.info("[morning_report] Redis cache connected")
    except Exception as e:  # noqa: BLE001
        _redis_ok = False
        log.warning("[morning_report] Redis unavailable (%s) — in-process cache", e)


_init_redis()

_INPROC: dict[str, tuple[float, dict]] = {}

# Staggered TTLs — base 150 min, each market offset so they don't all expire
# together (prevents an 8-index yfinance burst). Market i refreshes ~12 min
# apart from market i-1.
_BASE_TTL = 150 * 60
_STAGGER_STEP = 12 * 60
_GLOBAL_TTL = 90 * 60   # the shared global-signal bundle


def _market_ttl(market_key: str) -> int:
    idx = MARKET_ORDER.index(market_key) if market_key in MARKET_ORDER else 0
    return _BASE_TTL + idx * _STAGGER_STEP


def _cache_get(key: str) -> Optional[dict]:
    if _redis_ok and _redis_client:
        try:
            raw = _redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
    entry = _INPROC.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    return None


def _cache_put(key: str, value: dict, ttl: int) -> None:
    if _redis_ok and _redis_client:
        try:
            _redis_client.setex(key, ttl, json.dumps(value, default=str))
        except Exception:  # noqa: BLE001
            pass
    _INPROC[key] = (time.time() + ttl, value)


# Macro tickers for the event_graph causal nodes — change% drives node states.
_MACRO_NODE_TICKERS = {
    "us10y": "^TNX", "dxy": "DX-Y.NYB", "gold": "GC=F",
    "oil": "CL=F", "vix": "^VIX",
}


def _macro_change_snapshot() -> dict:
    """Pull 1-day change% for the macro nodes event_graph propagates.

    macro_snapshot carries only price levels, but the causal graph needs
    directional moves — so change% is read via indicators.compute_indicators
    (each call is itself Redis-cached). Runs once per global-signals build.
    """
    out: dict = {}
    try:
        from indicators import compute_indicators
    except Exception:  # noqa: BLE001
        return out
    for node_key, ticker in _MACRO_NODE_TICKERS.items():
        try:
            r = compute_indicators(ticker, "1d")
            out[node_key] = {"change_pct": (r or {}).get("change_pct", 0.0)}
        except Exception:  # noqa: BLE001
            out[node_key] = {"change_pct": 0.0}
    return out


# ─── Global deterministic signal bundle (computed once per report) ──────────
def _build_global_signals() -> dict:
    """Compute the engines that are market-WIDE (same backdrop for all 8
    markets): macro_reasoning, regime, events, sentiment, correlation,
    plus event_graph causal propagation and regime-transition scoring.

    Returns a dict the per-market builder folds into each consensus.
    Cached for _GLOBAL_TTL so a full report build only does this once.
    """
    cached = _cache_get("morning:global_signals")
    if cached:
        return cached

    out: dict = {
        "regime_label": "MIXED", "regime_score": 0.0, "regime_conf": 0,
        "macro_scenario": "NO_CLEAN_SCENARIO", "macro_score": 0.0,
        "events_tilt": 0.0, "sentiment_tilt": 0.0,
        "correlation_score": 0.0, "correlation_anomalies": 0,
        "snapshot": None, "fng": None, "vix": None,
        "dxy": None, "us10y": None,
        # event_graph + regime_transition (filled when a snapshot is available)
        "event_graph_pressure": 0.0, "event_graph_liquidity": 0.0,
        "event_graph_observed": {}, "event_graph_pressures": {},
        "impact_chain": [], "contradictions": [], "regime_transition": None,
        "causal_overlay": None,
    }

    try:
        from market_intel import get_intel_snapshot
        snap = get_intel_snapshot()
    except Exception as e:  # noqa: BLE001
        log.debug("[morning_report] intel snapshot failed: %s", e)
        snap = None

    if snap:
        out["snapshot"] = {
            "fear_greed": snap.get("fear_greed"),
            "macro_snapshot": snap.get("macro_snapshot"),
        }
        # regime
        rs = snap.get("regime_state") or {}
        out["regime_label"] = rs.get("composite") or (snap.get("regime") or {}).get("regime") or "MIXED"
        out["regime_conf"]  = rs.get("confidence") or 0
        out["regime_score"] = _REGIME_DIRECTION.get(str(out["regime_label"]).upper(), 0.0)
        # events directional tilt
        ec = snap.get("events_classified") or {}
        out["events_tilt"] = float((ec.get("directional") or {}).get("tilt") or 0.0)
        # sentiment tilt
        sent = snap.get("sentiment") or {}
        out["sentiment_tilt"] = float(sent.get("tilt_score") or 0.0)
        # correlation — anomalies tilt slightly risk-off
        corr = snap.get("correlations") or {}
        anomalies = corr.get("anomalies") or []
        out["correlation_anomalies"] = len(anomalies)
        out["correlation_score"] = -0.3 if len(anomalies) >= 3 else (-0.15 if anomalies else 0.0)
        # macro snapshot levels
        m = snap.get("macro_snapshot") or {}
        def _lvl(k):
            v = m.get(k)
            return (v.get("price") if isinstance(v, dict) else v)
        out["vix"]   = _lvl("vix")
        out["dxy"]   = _lvl("dxy")
        out["us10y"] = _lvl("us10y")
        out["fng"]   = (snap.get("fear_greed") or {}).get("local") or {}

        # ── event_graph: causal propagation across the macro nodes ──────
        try:
            import event_graph as _eg
            eg_result = _eg.analyze(_macro_change_snapshot(),
                                    events_tilt=out["events_tilt"])
            out["event_graph_pressure"]   = eg_result["equity_pressure"]
            out["event_graph_liquidity"]  = eg_result["liquidity_pressure"]
            out["event_graph_observed"]   = eg_result["observed"]
            out["event_graph_pressures"]  = eg_result["pressures"]
            out["impact_chain"]           = eg_result["impact_chain"]
            out["contradictions"]         = eg_result["contradictions"]
        except Exception as e:  # noqa: BLE001
            log.debug("[morning_report] event_graph failed: %s", e)

        # ── regime_transition: is the prevailing regime changing? ───────
        try:
            import regime_transition_engine as _rt
            out["regime_transition"] = _rt.compute_transition(
                out.get("event_graph_observed") or {},
                out.get("event_graph_pressures") or {},
                regime_engine_hint=out["regime_label"])
        except Exception as e:  # noqa: BLE001
            log.debug("[morning_report] regime_transition failed: %s", e)

        # ── causal_overlay: pressure vector + contradiction scoring ─────
        try:
            from macro_reasoning_engine import causal_overlay
            out["causal_overlay"] = causal_overlay(
                _macro_change_snapshot(), events_tilt=out["events_tilt"],
                regime_transition=out.get("regime_transition"))
        except Exception as e:  # noqa: BLE001
            log.debug("[morning_report] causal_overlay failed: %s", e)

    # macro_reasoning scenario (deterministic regime synthesis)
    try:
        from macro_reasoning_engine import analyze_stage4
        if snap:
            s4 = analyze_stage4(snap)
            syn = s4.get("regime_synthesis") or {}
            out["macro_scenario"] = (s4.get("scenario") or {}).get("name", "NO_CLEAN_SCENARIO")
            out["macro_score"]    = _REGIME_DIRECTION.get(str(syn.get("regime", "MIXED")).upper(), 0.0)
    except Exception as e:  # noqa: BLE001
        log.debug("[morning_report] macro_reasoning failed: %s", e)

    _cache_put("morning:global_signals", out, _GLOBAL_TTL)
    return out


# ─── Per-market deterministic pieces ────────────────────────────────────────
def _indicator_signal(index_ticker: str) -> tuple[Optional[dict], Optional[Signal]]:
    """Run indicators.compute_indicators on the market's index.
    Returns (raw indicator result, Signal) — Signal is None if data missing."""
    try:
        from indicators import compute_indicators
        res = compute_indicators(index_ticker, "1d")
    except Exception as e:  # noqa: BLE001
        log.debug("[morning_report] indicators failed for %s: %s", index_ticker, e)
        return None, None
    if not res:
        return None, None
    comp = res.get("composite") or {}
    score = float(comp.get("score") or 0) / 100.0   # -100..100 → -1..1
    return res, Signal(
        source="indicators",
        score=score,
        bias=comp.get("label", "NEUTRAL"),
        detail=(f"composite {comp.get('score',0):+.0f} "
                f"({comp.get('bullish_count',0)}B/{comp.get('bearish_count',0)}S)"),
    )


def _extract_levels(indicator_result: Optional[dict]) -> dict:
    """Derive support/resistance from the EMA values indicators already
    computed — no extra yfinance fetch. Support = nearest EMA below price,
    resistance = nearest EMA above price. ATR-offset fallback if all EMAs
    sit one side."""
    if not indicator_result:
        return {"support": None, "resistance": None, "last_price": None}
    last = indicator_result.get("last_price")
    inds = indicator_result.get("indicators") or {}
    ema_vals = []
    for k in ("EMA20", "EMA50", "EMA200"):
        v = (inds.get(k) or {}).get("value")
        if isinstance(v, (int, float)):
            ema_vals.append(float(v))
    atr = (inds.get("ATR") or {}).get("value")

    support = resistance = None
    if last is not None:
        below = [e for e in ema_vals if e < last]
        above = [e for e in ema_vals if e > last]
        support    = round(max(below), 2) if below else None
        resistance = round(min(above), 2) if above else None
        # Fallback when EMAs are all one side — use ATR (or 1%) offset
        offset = float(atr) if isinstance(atr, (int, float)) and atr else last * 0.01
        if support is None:
            support = round(last - offset, 2)
        if resistance is None:
            resistance = round(last + offset, 2)
    last_out = round(float(last), 2) if isinstance(last, (int, float)) else last
    return {"support": support, "resistance": resistance, "last_price": last_out}


def _overnight_catalysts(market_cfg: dict, snap: Optional[dict]) -> list[dict]:
    """Filter the snapshot's news clusters to ones mentioning this market.
    Returns up to 3 compact catalyst records."""
    if not snap:
        return []
    clusters = ((snap.get("news") or {}).get("clusters")) or []
    kws = [k.lower() for k in market_cfg.get("keywords", [])]
    hits = []
    for c in clusters:
        topic = (c.get("topic") or "").lower()
        if any(kw in topic for kw in kws):
            ev = c.get("event") or {}
            hits.append({
                "topic":    c.get("topic", "")[:130],
                "category": ev.get("category", "NEWS"),
                "severity": ev.get("severity", 0),
                "sources":  c.get("sources", [])[:2],
            })
    hits.sort(key=lambda h: h.get("severity", 0), reverse=True)
    return hits[:3]


def _macro_drivers(g: dict) -> dict:
    """Global macro context block — same for every market."""
    fng = g.get("fng") or {}
    return {
        "regime":        g.get("regime_label"),
        "regime_conf":   g.get("regime_conf"),
        "macro_scenario": g.get("macro_scenario"),
        "fear_greed":    fng.get("score") if isinstance(fng, dict) else fng,
        "vix":           g.get("vix"),
        "dxy":           g.get("dxy"),
        "us10y":         g.get("us10y"),
    }


def _risk_warnings(g: dict, indicator_result: Optional[dict],
                   confidence: dict) -> list[str]:
    """Deterministic risk flags for the brief."""
    warns: list[str] = []
    vix = g.get("vix")
    try:
        vix = float(vix) if vix is not None else None
    except Exception:
        vix = None
    if vix is not None:
        if vix >= 28:
            warns.append(f"VIX {vix:.1f} — EXTREME volatility regime; size down, widen stops")
        elif vix >= 20:
            warns.append(f"VIX {vix:.1f} — elevated volatility; reduce overnight exposure")
    if g.get("correlation_anomalies", 0) >= 3:
        warns.append(f"{g['correlation_anomalies']} cross-asset correlation anomalies — "
                     f"regime may be transitioning")
    if not indicator_result:
        warns.append("Index price data unavailable — bias is global-backdrop only, treat as LOW conviction")
    if confidence.get("tier") == "LOW":
        warns.append("LOW confidence — deterministic sources disagree or are stale; not actionable alone")
    return warns


# ─── Per-market brief ───────────────────────────────────────────────────────
def build_market_brief(market_key: str, *, force: bool = False,
                       global_signals: Optional[dict] = None) -> dict:
    """Build one market's grounded brief. Cached with a staggered TTL."""
    market_key = market_key.upper()
    cfg = MARKETS.get(market_key)
    if not cfg:
        return {"error": f"unknown market: {market_key}"}

    cache_key = f"morning:brief:{market_key}"
    if not force:
        cached = _cache_get(cache_key)
        if cached:
            cached["_cache_hit"] = True
            return cached

    g = global_signals or _build_global_signals()
    snap = None
    try:
        from market_intel import get_intel_snapshot
        snap = get_intel_snapshot()
    except Exception:
        snap = None

    # ── Per-market technical signal (the differentiator) ────────────────
    indicator_result, ind_signal = _indicator_signal(cfg["primary"])

    # ── event_graph causal pressure (global) + per-market contradictions ─
    eg_pressure = _clamp01(g.get("event_graph_pressure", 0.0))
    contradictions = list(g.get("contradictions") or [])
    if ind_signal is not None:
        # Fold THIS market's equity reading in so equities-side contradictions
        # (e.g. this market bullish while VIX rises) can surface per-market.
        try:
            import event_graph as _eg
            mkt_states = dict(g.get("event_graph_observed") or {})
            mkt_states["equities"]  = ind_signal.score
            mkt_states["liquidity"] = g.get("event_graph_liquidity", 0.0)
            seen = {c["pair"] for c in contradictions}
            for c in _eg.detect_contradictions(mkt_states):
                if c["pair"] not in seen:
                    contradictions.append(c)
                    seen.add(c["pair"])
        except Exception:  # noqa: BLE001
            pass

    # ── Assemble the deterministic signal set ───────────────────────────
    signals: list[Signal] = []
    if ind_signal:
        signals.append(ind_signal)
    signals.append(Signal("macro_reasoning", g.get("macro_score", 0.0),
                          detail=f"scenario {g.get('macro_scenario','?')}"))
    signals.append(Signal("regime", g.get("regime_score", 0.0),
                          detail=f"{g.get('regime_label','?')} ({g.get('regime_conf',0)}%)"))
    # event_graph signal — fed by the pressure-vector net-risk reading when
    # available (a richer cross-asset causal signal than raw equity pressure);
    # falls back to event_graph's equity pressure if the overlay is absent.
    _net_risk = (g.get("causal_overlay") or {}).get("net_risk") or {}
    causal_score = _clamp01(_net_risk.get("score", eg_pressure))
    signals.append(Signal("event_graph", causal_score,
                          detail=f"causal net-risk {causal_score:+.2f}"))
    signals.append(Signal("events", _clamp01(g.get("events_tilt", 0.0)),
                          detail=f"news tilt {g.get('events_tilt',0):+.2f}"))
    signals.append(Signal("sentiment", _clamp01(g.get("sentiment_tilt", 0.0)),
                          detail=f"sentiment tilt {g.get('sentiment_tilt',0):+.2f}"))
    signals.append(Signal("correlation", g.get("correlation_score", 0.0),
                          detail=f"{g.get('correlation_anomalies',0)} anomalies"))

    consensus = compute_consensus(signals)

    # ── Stability: regime-transition + contradiction count → confidence ─
    transition = g.get("regime_transition") or {}
    base_stability = float(transition.get("stability", 1.0))
    contra_penalty = min(0.45, 0.15 * len(contradictions))
    stability = max(0.0, round(base_stability - contra_penalty, 4))
    # contradiction_engine's cross-layer consistency can only TIGHTEN
    # stability — never raise it — so existing behaviour is unchanged when
    # the overlay finds nothing (consistency 1.0).
    _consistency = (g.get("causal_overlay") or {}).get("consistency")
    if _consistency is not None:
        stability = round(min(stability, float(_consistency)), 4)

    confidence = compute_confidence(consensus, freshness=1.0, stability=stability)
    levels     = _extract_levels(indicator_result)
    catalysts  = _overnight_catalysts(cfg, snap)
    drivers    = _macro_drivers(g)
    warnings   = _risk_warnings(g, indicator_result, confidence)

    # Causal contradictions + regime transition become explicit risk flags.
    for c in contradictions[:3]:
        warnings.append("Causal contradiction — " + c["label"])
    if transition.get("transitioning"):
        warnings.append("Regime shift — " + transition.get("note", "regime transitioning"))

    brief = {
        "market":         cfg["name"],
        "market_key":     market_key,
        "index":          cfg["primary"],
        "last_price":     levels.get("last_price"),
        "computed_bias":  consensus["bias"],          # deterministic — FIXED
        "consensus_score": consensus["score"],
        "confidence":     confidence["score"],
        "confidence_tier": confidence["tier"],
        "confidence_note": confidence["note"],
        "stability":      stability,
        "support":        levels.get("support"),
        "resistance":     levels.get("resistance"),
        "overnight_catalysts": catalysts,
        "macro_drivers":  drivers,
        "risk_warnings":  warnings,
        "causal_pressures": {
            "equities":  g.get("event_graph_pressure", 0.0),
            "liquidity": g.get("event_graph_liquidity", 0.0),
        },
        "impact_chain":   g.get("impact_chain", []),
        "contradictions": contradictions,
        "causal_overlay": g.get("causal_overlay"),
        "regime_transition": {
            "current":          transition.get("current_regime"),
            "projected":        transition.get("projected_regime"),
            "transitioning":    transition.get("transitioning", False),
            "transition_score": transition.get("transition_score", 0.0),
            "direction":        transition.get("direction", "stable"),
            "note":             transition.get("note", ""),
        } if transition else None,
        "votes":          consensus["votes"],
        "dissent":        consensus["dissent"],
        "agreement":      consensus["agreement"],
        "narrative":      None,        # filled by narrate_brief() if enabled
        "data_available": indicator_result is not None,
        "generated_at":   int(time.time()),
        "_cache_hit":     False,
    }

    _cache_put(cache_key, brief, _market_ttl(market_key))
    return brief


def _clamp01(x) -> float:
    try:
        return max(-1.0, min(1.0, float(x)))
    except Exception:
        return 0.0


# ─── Full global report ─────────────────────────────────────────────────────
def build_global_report(*, force: bool = False, narrate: bool = False) -> dict:
    """Assemble all 8 market briefs into one report.

    Per-market briefs use staggered caches, so a typical call mostly hits
    cache and only recomputes the markets whose TTL lapsed.
    """
    started = time.time()
    g = _build_global_signals() if not force else _force_global()
    briefs = []
    for mk in MARKET_ORDER:
        b = build_market_brief(mk, force=force, global_signals=g)
        if narrate:
            try:
                b["narrative"] = narrate_brief(b)
            except Exception as e:  # noqa: BLE001
                log.debug("[morning_report] narration failed for %s: %s", mk, e)
        briefs.append(b)

    # Global overview — pure aggregation, no LLM
    bull = sum(1 for b in briefs if b.get("computed_bias") == "BUY")
    bear = sum(1 for b in briefs if b.get("computed_bias") == "SELL")
    neut = len(briefs) - bull - bear
    if bull > bear and bull >= 3:   global_tone = "RISK-ON"
    elif bear > bull and bear >= 3: global_tone = "RISK-OFF"
    else:                           global_tone = "MIXED"

    transition = g.get("regime_transition") or {}
    contradictions = g.get("contradictions") or []

    return {
        "report_type": "grounded_global_premarket",
        "generated_at": int(time.time()),
        "computed_in_ms": int((time.time() - started) * 1000),
        "global_overview": {
            "tone": global_tone,
            "bullish_markets": bull,
            "bearish_markets": bear,
            "neutral_markets": neut,
            "regime": g.get("regime_label"),
            "macro_scenario": g.get("macro_scenario"),
            "regime_transition": {
                "current":          transition.get("current_regime"),
                "projected":        transition.get("projected_regime"),
                "transitioning":    transition.get("transitioning", False),
                "transition_score": transition.get("transition_score", 0.0),
                "direction":        transition.get("direction", "stable"),
                "note":             transition.get("note", ""),
            } if transition else None,
            "causal_equity_pressure": g.get("event_graph_pressure", 0.0),
            "causal_liquidity_pressure": g.get("event_graph_liquidity", 0.0),
            "contradiction_count": len(contradictions),
        },
        "markets": briefs,
        "disclaimer": (
            "Directional bias and levels are computed by deterministic "
            "engines (indicators, macro reasoning, regime, event graph, "
            "regime transition, events, sentiment, correlation). This is "
            "market intelligence for human review — NOT trade orders, NOT "
            "entry signals, NOT financial advice. No autonomous execution."
        ),
    }


def _force_global() -> dict:
    """Recompute the global signal bundle, bypassing its cache."""
    try:
        if _redis_ok and _redis_client:
            _redis_client.delete("morning:global_signals")
    except Exception:
        pass
    _INPROC.pop("morning:global_signals", None)
    return _build_global_signals()


# ─── OPTIONAL LLM narration — explains, never decides ───────────────────────
def narrate_brief(brief: dict) -> Optional[str]:
    """Produce a 2-3 sentence prose narrative for a market brief.

    The bias is passed to the LLM as a FIXED FACT. The model may only
    explain the deterministic drivers. The output is scanned by
    bias_consensus_engine.scan_for_contradiction() — if the narration
    asserts the opposite direction, it is REJECTED and None is returned
    (the brief stays deterministic-only).

    Gated by ENABLE_MORNING_NARRATION. Default off → pure-deterministic,
    zero LLM tokens, lowest latency.
    """
    if os.environ.get("ENABLE_MORNING_NARRATION", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    bias = brief.get("computed_bias", "NEUTRAL")
    market = brief.get("market", "?")
    try:
        from ai_router import chat
    except Exception:
        return None

    drivers = brief.get("macro_drivers") or {}
    cats = "; ".join(c.get("topic", "") for c in (brief.get("overnight_catalysts") or [])[:2])
    prompt = (
        f"Market: {market}.  DETERMINISTIC BIAS = {bias} (FIXED — do not change it).\n"
        f"Confidence: {brief.get('confidence')}/100 ({brief.get('confidence_tier')}).\n"
        f"Support {brief.get('support')} / Resistance {brief.get('resistance')}.\n"
        f"Regime: {drivers.get('regime')}.  Scenario: {drivers.get('macro_scenario')}.\n"
        f"Overnight catalysts: {cats or 'none'}.\n\n"
        f"Write 2-3 sentences explaining WHY the deterministic engines produced "
        f"a {bias} read for {market}. You are NARRATING a fixed conclusion — you "
        f"must not suggest the opposite direction. No hedge words. No order "
        f"language. No price targets beyond the support/resistance given."
    )
    messages = [
        {"role": "system", "content": (
            "You narrate pre-computed deterministic market reads. You never "
            "decide or reverse a direction. You explain the given bias only.")},
        {"role": "user", "content": prompt},
    ]
    try:
        result = chat(task="fast_summary", messages=messages,
                      temperature=0.2, max_tokens=160, timeout=15)
    except Exception:
        return None
    if not result.ok or not result.content:
        return None

    text = result.content.strip()
    # Contradiction guard — reject narration that opposes the consensus.
    offending = scan_for_contradiction(bias, text)
    if offending:
        log.warning("[morning_report] narration rejected for %s — contradicted %s (%r)",
                    market, bias, offending)
        return None
    return text


# ─── Introspection ──────────────────────────────────────────────────────────
def list_markets() -> list[str]:
    return list(MARKET_ORDER)
