"""
pressure_vector.py — Directional pressure vectors + market contagion.

Layers on event_graph: takes event_graph's propagated cross-asset pressures,
folds in a ninth force — CENTRAL-BANK ACTIONS — and turns the result into:

  1. A PRESSURE VECTOR — per node, a {direction, magnitude} reading across
     all nine forces (yields, DXY, gold, oil, equities, VIX/volatility,
     liquidity, macro events, central-bank actions).
  2. The DOMINANT DRIVER — which force is pushing hardest right now.
  3. A NET RISK vector — the headline risk-on / risk-off pull.
  4. MARKET CONTAGION — from the dominant shock, which markets it has
     spread to and how broad / severe the spread is.

Central-bank actions are layered on WITHOUT modifying event_graph's graph.
A central bank transmits to markets through the two channels it actually
moves — policy rates (→ yields) and the currency (→ DXY) — so the CB force
is folded into those two observed states and event_graph's own propagation
carries it through to liquidity, equities, gold, etc. No new graph node,
no edit to event_graph.

Design constraints (per spec):
  - Lightweight + deterministic: pure Python, no numpy, no I/O. One extra
    event_graph.propagate() pass; same input → same output.
  - Async-safe: compute_pressure_vector_async() offloads to a worker thread
    so the event loop is never touched.
  - Timeout-safe: the module does no I/O — nothing to time out; the async
    offload is the hard non-blocking guarantee.
  - Fail-soft: compute_pressure_vector() NEVER raises — on any error it
    logs once and returns a neutral result flagged degraded=True.
  - Cached: memoised on input content (TTL + bounded).
  - No autonomous agents, no LLM, no recursion (event_graph.propagate is a
    bounded iterative sweep).

The LLM never sees this module's internals — it narrates the finished
vector + contagion conclusions only.
"""
from __future__ import annotations

import asyncio
import copy
import os
import threading
import time
from typing import Optional

import event_graph as _eg


# ─── Central-bank transmission ───────────────────────────────────────────────
# cb_action is a directional tilt in [-1, +1]:
#   +1 = fully dovish / easing      (risk-positive)
#   -1 = fully hawkish / tightening (risk-negative)
# A central bank acts on markets primarily through two channels — policy
# rates and the currency — so its force is folded into those two OBSERVED
# event_graph nodes and event_graph's propagation does the rest (a hawkish
# CB lifting yields + DXY already drains liquidity and pressures equities
# through event_graph's existing edges). Weights are the strength of a
# decisive (±1) action as a shock to that node's state.
CB_TO_YIELDS = 0.70   # hawkish CB → yields up
CB_TO_DXY    = 0.50   # hawkish CB → dollar up

# Driving forces — the inputs that can be a "dominant driver". equities and
# liquidity are derived OUTCOMES of propagation, never drivers themselves.
DRIVER_NODES = ("yields", "dxy", "gold", "oil", "volatility",
                "macro_events", "central_bank")

# event_graph node → asset class, for contagion reporting.
ASSET_CLASS = {
    "yields":     "rates",
    "dxy":        "fx",
    "gold":       "commodities",
    "oil":        "commodities",
    "equities":   "equities",
    "volatility": "volatility",
    "liquidity":  "liquidity",
}

_SIGNIFICANT   = 0.12   # |pressure| below this is treated as directionally flat
_CONTAGION_HIT = 0.20   # |pressure| at/above this = a market caught the shock


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _direction(x: float) -> int:
    """+1 / -1 / 0 — directional sign with a flat dead-band."""
    if x > _SIGNIFICANT:
        return 1
    if x < -_SIGNIFICANT:
        return -1
    return 0


# ─── Output cache + fail-soft ────────────────────────────────────────────────
_CACHE_TTL  = max(1, int(os.environ.get("PRESSURE_VECTOR_CACHE_TTL", "300")))
_CACHE_MAX  = 64
_cache: dict = {}
_cache_lock = threading.Lock()
_cache_stats = {"hits": 0, "misses": 0}


def _cache_key(macro, events_tilt, cb_action, equities_observed) -> tuple:
    """Content-derived key — same observed-state basis event_graph caches on."""
    obs = _eg.derive_node_states(macro, events_tilt)
    obs_key = tuple(round(float(obs.get(n, 0.0)), 4) for n in _eg.OBSERVED_NODES)
    eq = None if equities_observed is None else round(float(equities_observed), 4)
    return (obs_key, round(float(cb_action or 0.0), 4), eq)


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
    forces = _eg.NODES + ("central_bank",)
    return {
        "pressures":       {n: 0.0 for n in _eg.NODES},
        "base_pressures":  {n: 0.0 for n in _eg.NODES},
        "cb_pressure":     {n: 0.0 for n in _eg.NODES},
        "vector":          {n: {"direction": 0, "magnitude": 0.0, "pressure": 0.0}
                            for n in forces},
        "dominant_driver": {"node": None, "direction": 0, "magnitude": 0.0},
        "net_risk":        {"score": 0.0, "direction": 0, "magnitude": 0.0,
                            "label": "neutral"},
        "contagion":       {"origin": None, "affected": [], "breadth": 0,
                            "severity": 0.0, "paths": [],
                            "summary": "no signal — degraded"},
        "degraded":        True,
    }


# ─── Market contagion ────────────────────────────────────────────────────────
def _contagion(origin: Optional[str], pressures: dict) -> dict:
    """From the dominant shock `origin`, the markets it has spread to.

    Contagion = the set of OTHER nodes now carrying a significant pressure,
    plus a 0-1 `severity` that blends how BROAD (how many markets) and how
    DEEP (how hard) the spread is. Direct origin→node causal edges from
    event_graph are surfaced as the transmission paths.
    """
    if not origin:
        return {"origin": None, "affected": [], "breadth": 0, "severity": 0.0,
                "paths": [], "summary": "no dominant shock — flat tape"}

    affected = []
    for n in _eg.NODES:
        if n == origin:
            continue
        p = pressures.get(n, 0.0)
        if abs(p) >= _CONTAGION_HIT:
            affected.append({"node": n,
                             "asset_class": ASSET_CLASS.get(n, n),
                             "pressure": round(p, 4),
                             "direction": _direction(p)})
    affected.sort(key=lambda a: abs(a["pressure"]), reverse=True)

    breadth = len(affected)
    avg_mag = (sum(abs(a["pressure"]) for a in affected) / breadth) if breadth else 0.0
    # severity: breadth fraction (max 7 other nodes) × depth, scaled to 0-1.
    severity = round(_clamp((breadth / 7.0) * avg_mag * 2.0, 0.0, 1.0), 4)

    hit_nodes = {a["node"] for a in affected}
    paths = [f"{u}→{v}" for (u, v, _s, _w) in _eg.EDGES
             if u == origin and v in hit_nodes]

    if breadth == 0:
        summary = f"{origin} shock contained — no cross-market contagion"
    else:
        classes = sorted({a["asset_class"] for a in affected})
        summary = (f"{origin} shock spreading to {breadth} market(s) across "
                   f"{', '.join(classes)} — severity {severity:.2f}")
    return {"origin": origin, "affected": affected, "breadth": breadth,
            "severity": severity, "paths": paths, "summary": summary}


# ─── One-shot entry point ────────────────────────────────────────────────────
def compute_pressure_vector(macro: Optional[dict], events_tilt: float = 0.0, *,
                            cb_action: float = 0.0,
                            equities_observed: Optional[float] = None,
                            use_cache: bool = True) -> dict:
    """Build the full pressure vector + contagion read.

    Fail-soft: never raises — on any internal error it logs once and returns
    a neutral result flagged degraded=True. Memoised on input content.

    Parameters
    ----------
    macro : dict
        market_intel macro_snapshot (dxy/us10y/vix/gold/oil tiles).
    events_tilt : float
        Risk-directional event tilt in [-1, +1] (+ = risk-positive).
    cb_action : float
        Central-bank action tilt in [-1, +1] — +1 dovish/easing,
        -1 hawkish/tightening. Folded into the yields + DXY channels.
    equities_observed : float, optional
        A direct equities reading in [-1, +1], passed through to event_graph.
    use_cache : bool
        When False, skip the cache lookup and recompute.

    Returns
    -------
    dict
        pressures / base_pressures / cb_pressure (per-node CB contribution) /
        vector (9 forces) / dominant_driver / net_risk / contagion / degraded.
    """
    try:
        cb = _clamp(cb_action)
        key = _cache_key(macro, events_tilt, cb, equities_observed) if use_cache else None
        if key is not None:
            hit = _cache_get(key)
            if hit is not None:
                return hit

        base = _eg.analyze(macro, events_tilt, equities_observed=equities_observed)
        base_pressures = dict(base.get("pressures", {}))

        # Fold the CB force into the two channels it transmits through, then
        # let event_graph's propagation carry it across the rest of the graph.
        if cb:
            observed = dict(base.get("observed", {}))
            observed["yields"] = _clamp(observed.get("yields", 0.0) + (-cb) * CB_TO_YIELDS)
            observed["dxy"]    = _clamp(observed.get("dxy", 0.0)    + (-cb) * CB_TO_DXY)
            pressures = _eg.propagate(observed)
        else:
            pressures = dict(base_pressures)

        # The CB's isolated contribution per node (explainability).
        cb_pressure = {n: round(pressures.get(n, 0.0) - base_pressures.get(n, 0.0), 4)
                       for n in _eg.NODES}

        # ── Pressure vector — nine forces ──
        vector = {}
        for n in _eg.NODES:
            p = pressures.get(n, 0.0)
            vector[n] = {"direction": _direction(p),
                         "magnitude": round(abs(p), 4),
                         "pressure":  round(p, 4)}
        vector["central_bank"] = {"direction": _direction(cb),
                                  "magnitude": round(abs(cb), 4),
                                  "pressure":  round(cb, 4)}

        # ── Dominant driver — the hardest-pushing input force ──
        dominant = {"node": None, "direction": 0, "magnitude": 0.0}
        for n in DRIVER_NODES:
            mag = vector[n]["magnitude"]
            if mag > dominant["magnitude"]:
                dominant = {"node": n, "direction": vector[n]["direction"],
                            "magnitude": mag}

        # ── Net risk vector — risk-on when equities + liquidity bid, vol low ──
        net = _clamp((pressures.get("equities", 0.0)
                      + pressures.get("liquidity", 0.0)
                      - pressures.get("volatility", 0.0)) / 3.0)
        net_risk = {"score": round(net, 4),
                    "direction": _direction(net),
                    "magnitude": round(abs(net), 4),
                    "label": ("risk-on"  if net > _SIGNIFICANT else
                              "risk-off" if net < -_SIGNIFICANT else "neutral")}

        result = {
            "pressures":       {n: round(pressures.get(n, 0.0), 4) for n in _eg.NODES},
            "base_pressures":  {n: round(base_pressures.get(n, 0.0), 4) for n in _eg.NODES},
            "cb_pressure":     cb_pressure,
            "vector":          vector,
            "dominant_driver": dominant,
            "net_risk":        net_risk,
            "contagion":       _contagion(dominant["node"], pressures),
            "degraded":        bool(base.get("degraded", False)),
        }
        if key is not None:
            _cache_put(key, result)
        return result

    except Exception as e:   # fail-soft — the causal layer must never break a report
        try:
            from production import log
            log("ERROR", "pressure_vector",
                "compute failed — returning neutral degraded result",
                err=type(e).__name__, msg=str(e)[:140])
        except Exception:
            pass
        return _safe_default()


# ─── Async-safe entry point ──────────────────────────────────────────────────
async def compute_pressure_vector_async(macro: Optional[dict],
                                        events_tilt: float = 0.0, *,
                                        cb_action: float = 0.0,
                                        equities_observed: Optional[float] = None,
                                        use_cache: bool = True) -> dict:
    """Async-safe wrapper — call this from async code (FastAPI handlers,
    background loops). Offloads the pure-CPU computation to a worker thread so
    the event loop is guaranteed untouched. Same fail-soft + caching as the
    sync entry point.
    """
    return await asyncio.to_thread(
        compute_pressure_vector, macro, events_tilt,
        cb_action=cb_action, equities_observed=equities_observed,
        use_cache=use_cache,
    )
