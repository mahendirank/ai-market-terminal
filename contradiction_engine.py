"""
contradiction_engine.py — Cross-layer contradiction detection + scoring.

Layers on event_graph, pressure_vector and (when supplied) the
regime_transition_engine. event_graph already flags MACRO-INTERNAL
contradictions (bullish gold + rising real yields, bullish equities +
rising VIX, …); this module aggregates those and adds the CROSS-LAYER
checks a single causal graph cannot see:

  - REGIME vs PRESSURE     — the regime engine's read disagrees with where
                             the causal pressure vector points.
  - CENTRAL-BANK vs MARKET — a hawkish central bank while risk assets stay
                             bid (or a dovish bank into a risk-off tape).
  - PRESSURE vs OBSERVED   — the propagated equity pressure points one way,
                             the observed equity tape the other.

Everything rolls into a single deterministic `contradiction_score` (0-1)
and its inverse `consistency` — a number confidence_engine can use to
discount conviction when the macro picture is internally incoherent.

Design constraints (per spec):
  - Lightweight + deterministic: pure Python, no numpy, no I/O. A flat pass
    over a handful of rules; same input → same output.
  - Async-safe: assess_contradictions_async() offloads to a worker thread.
  - Timeout-safe: no I/O to time out; the async offload keeps the loop free.
  - Fail-soft: assess_contradictions() NEVER raises — on any error it logs
    once and returns a neutral result flagged degraded=True.
  - Cached: memoised on input content (TTL + bounded).
  - Contradiction-aware by construction; no autonomous agents, no LLM, no
    recursion.

The LLM never sees this module's internals — it narrates the finished
contradiction list + score only.
"""
from __future__ import annotations

import asyncio
import copy
import os
import threading
import time
from typing import Optional

import event_graph as _eg
import pressure_vector as _pv


# Both sides of a cross-layer contradiction must clear this magnitude.
_SIGNIFICANT = 0.20

# Regime label → risk polarity (matches regime_transition_engine's families).
_RISK_ON_REGIMES  = {"RISK_ON", "LIQUIDITY_EXPANSION"}
_RISK_OFF_REGIMES = {"RISK_OFF", "PANIC", "TIGHTENING"}


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _word(x: float) -> str:
    return "rising" if x > 0 else "falling"


# ─── Output cache + fail-soft ────────────────────────────────────────────────
_CACHE_TTL  = max(1, int(os.environ.get("CONTRADICTION_CACHE_TTL", "300")))
_CACHE_MAX  = 64
_cache: dict = {}
_cache_lock = threading.Lock()
_cache_stats = {"hits": 0, "misses": 0}


def _cache_key(macro, events_tilt, cb_action, equities_observed,
               regime_transition) -> tuple:
    """Content-derived key. regime_transition / pressure_vector are pure
    functions of the same macro inputs, so only the regime label needs to
    enter the key — the rest is captured by the macro-derived state."""
    obs = _eg.derive_node_states(macro, events_tilt)
    obs_key = tuple(round(float(obs.get(n, 0.0)), 4) for n in _eg.OBSERVED_NODES)
    eq = None if equities_observed is None else round(float(equities_observed), 4)
    regime = ""
    if regime_transition:
        regime = str(regime_transition.get("projected_regime")
                     or regime_transition.get("current_regime") or "")
    return (obs_key, round(float(cb_action or 0.0), 4), eq, regime)


def _cache_get(key: tuple) -> Optional[dict]:
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            _cache_stats["misses"] += 1
            return None
        ts, value = entry
        if now - ts > _CACHE_TTL:
            _cache.pop(key, None)
            _cache_stats["misses"] += 1
            return None
        _cache_stats["hits"] += 1
        return copy.deepcopy(value)


def _cache_put(key: tuple, value: dict) -> None:
    with _cache_lock:
        if key not in _cache and len(_cache) >= _CACHE_MAX:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
        _cache[key] = (time.time(), copy.deepcopy(value))


def clear_cache() -> None:
    """Drop every memoised result. For tests and explicit invalidation."""
    with _cache_lock:
        _cache.clear()
        _cache_stats["hits"] = 0
        _cache_stats["misses"] = 0


def cache_stats() -> dict:
    """Hit/miss counters + current size."""
    with _cache_lock:
        return {**_cache_stats, "size": len(_cache), "ttl_secs": _CACHE_TTL}


def _safe_default() -> dict:
    """Neutral, correctly-shaped result for the fail-soft path."""
    return {
        "contradictions":         [],
        "count":                  0,
        "contradiction_score":    0.0,
        "consistency":            1.0,
        "dominant_contradiction": None,
        "degraded":               True,
    }


# ─── One-shot entry point ────────────────────────────────────────────────────
def assess_contradictions(macro: Optional[dict], events_tilt: float = 0.0, *,
                          cb_action: float = 0.0,
                          regime_transition: Optional[dict] = None,
                          pressure_vector: Optional[dict] = None,
                          equities_observed: Optional[float] = None,
                          use_cache: bool = True) -> dict:
    """Aggregate macro-internal + cross-layer contradictions into one score.

    Fail-soft: never raises — on any internal error it logs once and returns
    a neutral result flagged degraded=True. Memoised on input content.

    Parameters
    ----------
    macro : dict
        market_intel macro_snapshot.
    events_tilt : float
        Risk-directional event tilt in [-1, +1].
    cb_action : float
        Central-bank action tilt in [-1, +1] (+ dovish, - hawkish).
    regime_transition : dict, optional
        Output of regime_transition_engine.compute_transition(). When given,
        the regime-vs-pressure check runs.
    pressure_vector : dict, optional
        Output of pressure_vector.compute_pressure_vector(). Computed here
        if not supplied (it is a pure function of the same macro inputs).
    equities_observed : float, optional
        A direct equities reading in [-1, +1]. When given, the
        pressure-vs-observed check runs.
    use_cache : bool
        When False, skip the cache lookup and recompute.

    Returns
    -------
    dict
        contradictions (each tagged with `layer`) / count /
        contradiction_score (0-1) / consistency (1 - score) /
        dominant_contradiction / degraded.
    """
    try:
        cb = _clamp(cb_action)
        key = (_cache_key(macro, events_tilt, cb, equities_observed,
                          regime_transition) if use_cache else None)
        if key is not None:
            hit = _cache_get(key)
            if hit is not None:
                return hit

        pv = pressure_vector
        if pv is None:
            pv = _pv.compute_pressure_vector(
                macro, events_tilt, cb_action=cb,
                equities_observed=equities_observed)
        pressures = pv.get("pressures", {}) or {}
        net = (pv.get("net_risk", {}) or {}).get("score", 0.0)

        eg_result = _eg.analyze(macro, events_tilt,
                                equities_observed=equities_observed)

        hits: list[dict] = []

        # ── Layer "macro" — event_graph's macro-internal contradictions ──
        for c in eg_result.get("contradictions", []) or []:
            hits.append({"pair":     c.get("pair"),
                         "label":    c.get("label"),
                         "severity": round(float(c.get("severity", 0.0)), 4),
                         "layer":    "macro"})

        # ── Layer "regime" — regime read vs causal pressure ──
        if regime_transition:
            regime = (regime_transition.get("projected_regime")
                      or regime_transition.get("current_regime"))
            if regime in _RISK_ON_REGIMES and net < -_SIGNIFICANT:
                hits.append({
                    "pair": "regime|pressure",
                    "label": f"Regime reads {regime} but causal pressure is "
                             f"risk-off ({net:+.2f})",
                    "severity": round(abs(net), 4), "layer": "regime"})
            elif regime in _RISK_OFF_REGIMES and net > _SIGNIFICANT:
                hits.append({
                    "pair": "regime|pressure",
                    "label": f"Regime reads {regime} but causal pressure is "
                             f"risk-on ({net:+.2f})",
                    "severity": round(abs(net), 4), "layer": "regime"})

        # ── Layer "central_bank" — CB stance vs equity pressure ──
        eq_p = pressures.get("equities", 0.0)
        if cb < -_SIGNIFICANT and eq_p > _SIGNIFICANT:
            hits.append({
                "pair": "central_bank|equities",
                "label": f"Hawkish central bank ({cb:+.2f}) while equity "
                         f"pressure stays risk-on ({eq_p:+.2f})",
                "severity": round(min(abs(cb), abs(eq_p)), 4),
                "layer": "central_bank"})
        elif cb > _SIGNIFICANT and eq_p < -_SIGNIFICANT:
            hits.append({
                "pair": "central_bank|equities",
                "label": f"Dovish central bank ({cb:+.2f}) while equity "
                         f"pressure stays risk-off ({eq_p:+.2f})",
                "severity": round(min(abs(cb), abs(eq_p)), 4),
                "layer": "central_bank"})

        # ── Layer "observed" — propagated pressure vs the observed tape ──
        if equities_observed is not None:
            obs_eq  = _clamp(equities_observed)
            prop_eq = pressures.get("equities", 0.0)
            if (abs(obs_eq) >= _SIGNIFICANT and abs(prop_eq) >= _SIGNIFICANT
                    and obs_eq * prop_eq < 0):
                hits.append({
                    "pair": "pressure|observed",
                    "label": f"Causal pressure implies equities "
                             f"{_word(prop_eq)} but the tape is {_word(obs_eq)}",
                    "severity": round(min(abs(obs_eq), abs(prop_eq)), 4),
                    "layer": "observed"})

        hits.sort(key=lambda h: h["severity"], reverse=True)

        # ── Aggregate score — worst contradiction (0.6) + breadth (0.4) ──
        if hits:
            max_sev = hits[0]["severity"]
            breadth = min(1.0, len(hits) / 4.0)
            score = round(_clamp(max_sev * 0.6 + breadth * 0.4, 0.0, 1.0), 4)
        else:
            score = 0.0
        consistency = round(1.0 - score, 4)

        result = {
            "contradictions":         hits,
            "count":                  len(hits),
            "contradiction_score":    score,
            "consistency":            consistency,
            "dominant_contradiction": hits[0] if hits else None,
            "degraded":               bool(eg_result.get("degraded", False)
                                           or pv.get("degraded", False)),
        }
        if key is not None:
            _cache_put(key, result)
        return result

    except Exception as e:   # fail-soft — must never break a report
        try:
            from production import log
            log("ERROR", "contradiction_engine",
                "assess failed — returning neutral degraded result",
                err=type(e).__name__, msg=str(e)[:140])
        except Exception:
            pass
        return _safe_default()


# ─── Async-safe entry point ──────────────────────────────────────────────────
async def assess_contradictions_async(macro: Optional[dict],
                                      events_tilt: float = 0.0, *,
                                      cb_action: float = 0.0,
                                      regime_transition: Optional[dict] = None,
                                      pressure_vector: Optional[dict] = None,
                                      equities_observed: Optional[float] = None,
                                      use_cache: bool = True) -> dict:
    """Async-safe wrapper — call this from async code. Offloads the pure-CPU
    computation to a worker thread so the event loop is never touched. Same
    fail-soft + caching as the sync entry point.
    """
    return await asyncio.to_thread(
        assess_contradictions, macro, events_tilt,
        cb_action=cb_action, regime_transition=regime_transition,
        pressure_vector=pressure_vector, equities_observed=equities_observed,
        use_cache=use_cache,
    )
