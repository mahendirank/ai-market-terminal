"""
event_graph.py — Directed causal graph for macro propagation.

Models the institutional cause-and-effect web between macro forces and
turns an observed set of market readings into:

  1. PRESSURE SCORES   — propagated directional pressure on every node,
                         most importantly the implied pressure on
                         `equities` and `liquidity`.
  2. IMPACT CHAINS     — the dominant causal paths behind a pressure
                         (Event → impact chain → pressure score).
  3. CONTRADICTIONS    — observed states that are causally inconsistent
                         (e.g. bullish equities + rising VIX;
                          bullish gold + rising real yields).

Design constraints (per spec):
  - Lightweight + deterministic: pure Python, 8 nodes, ~18 edges, no
    numpy, no I/O. Same input → same output.
  - No autonomous agents, no LLM.
  - No recursive loops: propagation is an ITERATIVE bounded sweep
    (fixed MAX_HOPS with per-hop decay). The graph contains cycles
    (oil→yields→dxy→oil, liquidity↔volatility); bounded iteration makes
    them terminate and damp naturally — there is no recursive call.
  - VPS-friendly: a full analyze() is a few hundred float ops.

The LLM layer never sees this module's internals — it only narrates the
finished conclusions surfaced by morning_report.
"""
from __future__ import annotations

from typing import Optional


# ─── Nodes ───────────────────────────────────────────────────────────────────
# Observed nodes are read from live macro data. Derived nodes carry no direct
# observation — their value is computed purely by propagation, which is what
# makes `equities` pressure a genuine cross-asset signal (distinct from a
# direct index reading).
OBSERVED_NODES = ("yields", "dxy", "gold", "oil", "volatility", "macro_events")
DERIVED_NODES  = ("equities", "liquidity")
NODES = OBSERVED_NODES + DERIVED_NODES


# ─── Directed causal edges  (from, to, sign, weight) ─────────────────────────
# sign  : +1 → a rise in `from` pushes `to` UP;  -1 → pushes `to` DOWN
# weight: causal strength in (0, 1]
EDGES: tuple[tuple[str, str, int, float], ...] = (
    # Rates transmission
    ("yields",       "dxy",        +1, 0.55),   # higher US yields attract capital
    ("yields",       "gold",       -1, 0.65),   # real-yield opportunity cost
    ("yields",       "equities",   -1, 0.50),   # discount-rate / valuation drag
    ("yields",       "liquidity",  -1, 0.45),   # tighter financial conditions
    # Dollar transmission
    ("dxy",          "gold",       -1, 0.55),   # gold is USD-priced
    ("dxy",          "oil",        -1, 0.40),   # commodities are USD-priced
    ("dxy",          "equities",   -1, 0.30),   # USD strength pressures earnings/EM
    ("dxy",          "liquidity",  -1, 0.40),   # global USD funding tightness
    # Oil transmission
    ("oil",          "yields",     +1, 0.35),   # inflation expectations
    ("oil",          "equities",   -1, 0.25),   # input-cost / consumer drag
    # Volatility transmission
    ("volatility",   "equities",   -1, 0.70),   # vol spike → risk-off in equities
    ("volatility",   "liquidity",  -1, 0.55),   # vol → liquidity withdrawn
    ("volatility",   "gold",       +1, 0.30),   # haven bid
    # Liquidity transmission
    ("liquidity",    "equities",   +1, 0.65),   # liquidity lifts risk assets
    ("liquidity",    "volatility", -1, 0.45),   # ample liquidity suppresses vol
    # Macro-event transmission (event node: +1 = risk-positive)
    ("macro_events", "equities",   +1, 0.55),
    ("macro_events", "volatility", -1, 0.40),
    ("macro_events", "liquidity",  +1, 0.30),
)

# Propagation controls
MAX_HOPS = 3      # bounded → cycles terminate, no recursion
DECAY    = 0.5    # per-hop attenuation; hop h contributes DECAY**h


# ─── change% → node-state normalisation ──────────────────────────────────────
# A node state is a directional reading in [-1, +1]. These scales convert a
# daily change-% into that range (a move of `scale`% maps to ±1.0). Tuned so a
# normal daily move lands near ±0.3-0.5 and only an outsized move saturates.
_CHG_SCALE = {
    "yields":     3.5,   # 10Y-yield index change-%
    "dxy":        0.6,   # DXY barely moves — small move is significant
    "gold":       2.0,
    "oil":        2.8,   # oil is intrinsically volatile
    "volatility": 9.0,   # VIX change-%
}


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _chg(v) -> float:
    """Extract a change-% number from a macro tile value.

    Tile values come as {price, change_pct} dicts or bare scalars, and
    change_pct is sometimes a string like '+1.2%'. Returns 0.0 on anything
    unparseable — macro data is a system boundary, so this is real defence.
    """
    if v is None:
        return 0.0
    if isinstance(v, dict):
        v = v.get("change_pct", v.get("change", 0.0))
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace("%", "").replace("+", "").replace(",", "")
        try:
            return float(s)
        except ValueError:
            return 0.0
    return 0.0


def derive_node_states(macro: Optional[dict], events_tilt: float = 0.0) -> dict:
    """Turn a market_intel macro_snapshot + event tilt into observed node
    states. Pure transform — no I/O. Only the observed nodes are filled;
    equities / liquidity are left for propagation to compute.
    """
    macro = macro or {}
    return {
        "yields":       _clamp(_chg(macro.get("us10y")) / _CHG_SCALE["yields"]),
        "dxy":          _clamp(_chg(macro.get("dxy"))   / _CHG_SCALE["dxy"]),
        "gold":         _clamp(_chg(macro.get("gold"))  / _CHG_SCALE["gold"]),
        "oil":          _clamp(_chg(macro.get("oil"))   / _CHG_SCALE["oil"]),
        "volatility":   _clamp(_chg(macro.get("vix"))   / _CHG_SCALE["volatility"]),
        "macro_events": _clamp(events_tilt or 0.0),
    }


# ─── Propagation ─────────────────────────────────────────────────────────────
def propagate(node_states: dict, *, max_hops: int = MAX_HOPS,
              decay: float = DECAY) -> dict:
    """Propagate observed shocks through the causal graph.

    Iterative bounded sweep — NOT recursive. Hop 1 spreads the observed
    states one edge out (×decay), hop 2 spreads that result another edge
    out (×decay again → decay²), etc. Cycles in the graph are damped by
    the compounding decay and the hard `max_hops` cap.

    Returns {node: accumulated pressure in [-1, +1]}.
    """
    accumulated = {n: _clamp(node_states.get(n, 0.0)) for n in NODES}
    frontier    = dict(accumulated)

    for _ in range(max_hops):
        nxt = {n: 0.0 for n in NODES}
        for (u, v, sign, w) in EDGES:
            nxt[v] += frontier[u] * sign * w
        frontier = {n: nxt[n] * decay for n in NODES}
        for n in NODES:
            accumulated[n] += frontier[n]

    return {n: round(_clamp(accumulated[n]), 4) for n in NODES}


def impact_chain(node_states: dict, target: str = "equities",
                 *, top_n: int = 3) -> list[dict]:
    """Trace the dominant causal paths feeding `target` (Event → impact
    chain → pressure score). Returns up to `top_n` paths, strongest first.

    Considers direct (1-hop) and 2-hop paths from each observed shock —
    enough to explain the headline pressure cheaply without walking the
    whole graph.
    """
    paths: list[dict] = []
    # 1-hop: observed u → target
    for (u, v, sign, w) in EDGES:
        if v != target:
            continue
        src = _clamp(node_states.get(u, 0.0))
        if abs(src) < 1e-6:
            continue
        contrib = src * sign * w * DECAY
        paths.append({"path": f"{u}→{target}", "contribution": round(contrib, 4)})
    # 2-hop: observed u → mid → target
    for (u, mid, s1, w1) in EDGES:
        src = _clamp(node_states.get(u, 0.0))
        if abs(src) < 1e-6:
            continue
        for (m2, v2, s2, w2) in EDGES:
            if m2 != mid or v2 != target or u == target:
                continue
            contrib = src * s1 * w1 * s2 * w2 * (DECAY ** 2)
            if abs(contrib) < 0.01:
                continue
            paths.append({"path": f"{u}→{mid}→{target}",
                          "contribution": round(contrib, 4)})
    paths.sort(key=lambda p: abs(p["contribution"]), reverse=True)
    return paths[:top_n]


# ─── Contradiction detection ─────────────────────────────────────────────────
# (node_a, node_b, relation, label)
#   relation +1 → the two nodes SHOULD move together
#   relation -1 → the two nodes SHOULD move oppositely
# A contradiction fires when both nodes are significantly moved AND the
# observed co-movement violates the causal relation.
_CONTRADICTION_RULES: tuple[tuple[str, str, int, str], ...] = (
    ("equities", "volatility", -1,
     "Equities {da} while volatility {db} — risk appetite contradicts the fear gauge"),
    ("gold", "yields", -1,
     "Gold {da} while real yields {db} — haven bid contradicts rising opportunity cost"),
    ("gold", "dxy", -1,
     "Gold {da} while the dollar {db} — gold contradicts USD strength"),
    ("equities", "liquidity", +1,
     "Equities {da} while liquidity {db} — risk rally contradicts the liquidity backdrop"),
    ("oil", "dxy", -1,
     "Oil {da} while the dollar {db} — commodity strength contradicts USD strength"),
    ("liquidity", "volatility", -1,
     "Liquidity {da} while volatility {db} — easing conditions contradict rising stress"),
)

_CONTRA_THRESHOLD = 0.22   # both magnitudes must clear this to count


def _dir_word(x: float) -> str:
    return "rising" if x > 0 else "falling"


def detect_contradictions(node_states: dict,
                          *, threshold: float = _CONTRA_THRESHOLD) -> list[dict]:
    """Find causally inconsistent pairs in the observed/derived states.

    A rule needs both its nodes present and both magnitudes ≥ threshold.
    Rules touching `equities` are simply skipped when no equities reading
    is supplied (the global pass omits it; the per-market pass includes
    that market's own composite).

    Returns [{pair, label, severity, states}, ...], strongest first.
    """
    hits: list[dict] = []
    for (a, b, relation, label) in _CONTRADICTION_RULES:
        if a not in node_states or b not in node_states:
            continue
        va = _clamp(node_states.get(a, 0.0))
        vb = _clamp(node_states.get(b, 0.0))
        if abs(va) < threshold or abs(vb) < threshold:
            continue
        observed_relation = 1 if (va * vb) > 0 else -1
        if observed_relation == relation:
            continue   # consistent — no contradiction
        severity = round(min(abs(va), abs(vb)), 4)
        hits.append({
            "pair":     f"{a}|{b}",
            "label":    label.format(da=_dir_word(va), db=_dir_word(vb)),
            "severity": severity,
            "states":   {a: round(va, 3), b: round(vb, 3)},
        })
    hits.sort(key=lambda h: h["severity"], reverse=True)
    return hits


# ─── One-shot entry point ────────────────────────────────────────────────────
def analyze(macro: Optional[dict], events_tilt: float = 0.0,
            *, equities_observed: Optional[float] = None) -> dict:
    """Full pass: derive → propagate → contradictions → impact chain.

    Parameters
    ----------
    macro : dict
        market_intel macro_snapshot (dxy/us10y/vix/gold/oil tiles).
    events_tilt : float
        Risk-directional event tilt in [-1, +1] (+ = risk-positive).
    equities_observed : float, optional
        A direct equities reading in [-1, +1]. When supplied, contradiction
        rules involving `equities` are evaluated (e.g. the per-market call
        passes that market's indicator composite). When omitted, only the
        macro-internal contradictions are checked.

    Returns
    -------
    dict
        {
          observed:        {node: state},
          pressures:       {node: propagated pressure},
          equity_pressure: float,   # headline cross-asset pressure on risk
          liquidity_pressure: float,
          impact_chain:    [{path, contribution}, ...],
          contradictions:  [{pair, label, severity, states}, ...],
        }
    """
    observed = derive_node_states(macro, events_tilt)
    pressures = propagate(observed)

    contra_input = dict(observed)
    if equities_observed is not None:
        contra_input["equities"] = _clamp(equities_observed)
    # liquidity is never observed directly — expose its propagated value so
    # the equities|liquidity contradiction rule has something to test.
    contra_input["liquidity"] = pressures["liquidity"]

    return {
        "observed":           observed,
        "pressures":          pressures,
        "equity_pressure":    pressures["equities"],
        "liquidity_pressure": pressures["liquidity"],
        "impact_chain":       impact_chain(observed, "equities"),
        "contradictions":     detect_contradictions(contra_input),
    }
