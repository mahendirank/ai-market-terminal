"""
correlation_engine.py — Cross-asset correlation with anomaly surfacing.

Extends existing correlations.py with:
  - Multi-window rolling correlations (20d, 60d, 250d)
  - Regime-conditional EXPECTED correlations (different baselines per regime)
  - σ-deviation alerts when current 20d corr breaks > 1.5σ from baseline
  - Network view: most-influential nodes by sum of |corr|
  - Cached snapshots so AI tabs can compose without recomputing

Why regime-conditional expected values?
  GOLD/DXY correlation is normally -0.6 in stable risk_on regimes but flips
  to -0.2 or even +0.1 during stagflation. A simple "+1.5σ vs all-time mean"
  generates false positives. A regime-conditional baseline catches genuine
  decouplings — the kind a desk analyst would flag.

Asset universe defaults to: GOLD, DXY, US10Y, VIX, SPX, NDX, NIFTY, BTC, OIL.
Configurable via `set_universe()`.
"""
from __future__ import annotations

import logging
import math
import os
import statistics
import time
from typing import Optional

log = logging.getLogger(__name__)


# ─── Default universe + regime priors ───────────────────────────────────────
# Each universe ticker maps to a yfinance symbol the upstream correlations.py
# module should already know how to fetch. We keep names simple here.
DEFAULT_UNIVERSE = [
    ("GOLD",   "GC=F"),
    ("DXY",    "DX-Y.NYB"),
    ("US10Y",  "^TNX"),
    ("VIX",    "^VIX"),
    ("SPX",    "^GSPC"),
    ("NDX",    "^NDX"),
    ("NIFTY",  "^NSEI"),
    ("BTC",    "BTC-USD"),
    ("OIL",    "CL=F"),
]

# Regime-conditional "normal" correlation priors.
# Built from a mix of historical references + textbook macro assumptions.
# Used as the baseline against which current 20d corr is compared.
#
# Schema: REGIME_PRIORS[regime_key][("A","B")] = (expected_corr, sd_of_expectation)
_REGIME_PRIORS: dict[str, dict[tuple[str, str], tuple[float, float]]] = {
    "risk_on": {
        ("GOLD", "DXY"):    (-0.55, 0.20),
        ("GOLD", "US10Y"):  (-0.50, 0.20),
        ("GOLD", "VIX"):    (+0.35, 0.20),
        ("DXY",  "US10Y"):  (+0.55, 0.20),
        ("SPX",  "VIX"):    (-0.85, 0.10),
        ("SPX",  "US10Y"):  (-0.25, 0.30),
        ("SPX",  "DXY"):    (-0.40, 0.25),
        ("NDX",  "BTC"):    (+0.55, 0.20),
        ("OIL",  "DXY"):    (-0.40, 0.20),
        ("NIFTY","SPX"):    (+0.65, 0.15),
    },
    "risk_off": {
        ("GOLD", "DXY"):    (-0.20, 0.30),   # safe-haven shifts can flip this
        ("GOLD", "VIX"):    (+0.50, 0.20),
        ("GOLD", "US10Y"):  (-0.65, 0.20),
        ("SPX",  "VIX"):    (-0.80, 0.15),
        ("SPX",  "DXY"):    (-0.55, 0.20),
        ("NDX",  "BTC"):    (+0.40, 0.25),
    },
    "stagflation": {
        ("GOLD", "DXY"):    (-0.10, 0.30),   # both can rally together
        ("GOLD", "US10Y"):  (+0.10, 0.30),
        ("OIL",  "DXY"):    (+0.20, 0.30),
        ("SPX",  "VIX"):    (-0.70, 0.20),
    },
    "inflationary": {
        ("GOLD", "DXY"):    (-0.35, 0.25),
        ("GOLD", "OIL"):    (+0.40, 0.25),
        ("OIL",  "US10Y"):  (+0.40, 0.25),
        ("SPX",  "US10Y"):  (-0.55, 0.20),
    },
}


# ─── Pair helpers ────────────────────────────────────────────────────────────
def _norm_pair(a: str, b: str) -> tuple[str, str]:
    """Sort pair so (A,B) and (B,A) collapse to the same key."""
    return (a, b) if a <= b else (b, a)


def _pair_prior(regime: str, a: str, b: str) -> Optional[tuple[float, float]]:
    """Look up the regime-conditional expected correlation for a pair."""
    priors = _REGIME_PRIORS.get(regime) or {}
    return priors.get(_norm_pair(a, b))


# ─── Multi-window rolling correlation ────────────────────────────────────────
def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation on two equal-length lists. Returns None if degenerate."""
    n = len(xs)
    if n < 5 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx2 = sum((x - mx) ** 2 for x in xs)
    dy2 = sum((y - my) ** 2 for y in ys)
    if dx2 == 0 or dy2 == 0:
        return None
    return num / math.sqrt(dx2 * dy2)


def _fetch_returns(ticker: str, days: int) -> list[float]:
    """Pull daily returns for an asset via yfinance. Used by multi-window
    compute. Falls back to empty list on any error so caller degrades."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=f"{days + 20}d", interval="1d",
                                          auto_adjust=False)
        if hist is None or hist.empty:
            return []
        closes = hist["Close"].dropna().tolist()
        if len(closes) < 2:
            return []
        return [(closes[i] - closes[i - 1]) / closes[i - 1]
                for i in range(1, len(closes))][-days:]
    except Exception as e:
        log.debug("returns fetch failed for %s: %s", ticker, e)
        return []


def compute_correlation_matrix(
    universe: Optional[list[tuple[str, str]]] = None,
    *,
    windows: tuple[int, ...] = (20, 60, 250),
    regime: Optional[str] = None,
) -> dict:
    """Compute pairwise correlations across the universe at multiple windows.

    Returns a structured dict:
      {
        "windows": {20: {("A","B"): corr, ...}, 60: ..., 250: ...},
        "pairs":   [{"a","b","corr_20d","corr_60d","corr_250d",
                     "expected","sigma","anomaly"} ...],
        "anomalies": [<pairs sorted by abs(sigma) desc>],
        "network": {asset: total_abs_corr, ...},
        "regime":  <str>,
        "ts":      <unix>,
      }
    """
    uni = universe or DEFAULT_UNIVERSE
    names = [n for n, _ in uni]
    tickers = {n: t for n, t in uni}

    # Fetch returns once per window
    max_days = max(windows)
    series: dict[str, list[float]] = {}
    for name, ticker in uni:
        series[name] = _fetch_returns(ticker, max_days)

    # Compute windows
    by_window: dict[int, dict] = {}
    for w in windows:
        m: dict[tuple[str, str], float] = {}
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                xa = series.get(a, [])[-w:]
                yb = series.get(b, [])[-w:]
                if len(xa) >= 5 and len(yb) >= 5:
                    n = min(len(xa), len(yb))
                    c = _pearson(xa[-n:], yb[-n:])
                    if c is not None:
                        m[_norm_pair(a, b)] = round(c, 3)
        by_window[w] = m

    # Compose pair rows with anomaly detection (current 20d vs regime prior)
    primary_w = 20 if 20 in windows else windows[0]
    pairs: list[dict] = []
    network: dict[str, float] = {n: 0.0 for n in names}
    for (a, b), corr_now in by_window[primary_w].items():
        row = {
            "a": a, "b": b,
            "corr_20d":  by_window.get(20,  {}).get(_norm_pair(a, b)),
            "corr_60d":  by_window.get(60,  {}).get(_norm_pair(a, b)),
            "corr_250d": by_window.get(250, {}).get(_norm_pair(a, b)),
        }
        # Anomaly: how many σ from expected for current regime?
        prior = _pair_prior(regime or "risk_on", a, b)
        if prior is not None:
            expected, sd = prior
            sigma = (corr_now - expected) / max(sd, 0.01)
            row["expected"] = expected
            row["sigma"]    = round(sigma, 2)
            row["anomaly"]  = abs(sigma) >= 1.5
        else:
            row["expected"] = None
            row["sigma"]    = None
            row["anomaly"]  = False
        pairs.append(row)
        network[a] += abs(corr_now)
        network[b] += abs(corr_now)

    anomalies = sorted(
        [p for p in pairs if p.get("anomaly")],
        key=lambda p: abs(p.get("sigma") or 0),
        reverse=True,
    )

    # Sort network by total magnitude (most-coupled asset first)
    network_sorted = dict(sorted(network.items(), key=lambda kv: kv[1], reverse=True))

    return {
        "windows":   {w: {f"{k[0]}|{k[1]}": v for k, v in m.items()}
                      for w, m in by_window.items()},
        "pairs":     pairs,
        "anomalies": anomalies,
        "network":   {k: round(v, 3) for k, v in network_sorted.items()},
        "regime":    regime or "risk_on",
        "ts":        int(time.time()),
    }


# ─── Cache layer ─────────────────────────────────────────────────────────────
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
    except Exception:
        _redis_ok = False


_init_redis()

_INPROC: dict[str, tuple[float, dict]] = {}
_TTL = 3600   # correlations move slowly — hourly cache fine


def get_correlation_snapshot(*, regime: Optional[str] = None,
                              force: bool = False) -> dict:
    """Cached wrapper around :func:`compute_correlation_matrix`.

    Cached 1 hour (correlations move slowly). Regime is part of the key so
    a regime transition triggers a fresh compute next call.
    """
    import json as _json
    key = f"corr_engine:{regime or 'default'}"

    if not force:
        if _redis_ok and _redis_client:
            try:
                raw = _redis_client.get(key)
                if raw:
                    return _json.loads(raw)
            except Exception:
                pass
        entry = _INPROC.get(key)
        if entry and entry[0] > time.time():
            return entry[1]

    snap = compute_correlation_matrix(regime=regime)
    if _redis_ok and _redis_client:
        try:
            _redis_client.setex(key, _TTL, _json.dumps(snap, default=str))
        except Exception:
            pass
    _INPROC[key] = (time.time() + _TTL, snap)
    return snap


def format_for_prompt(snap: dict, *, max_anomalies: int = 5) -> str:
    """Pretty-print the most-relevant correlation info for an AI prompt."""
    if not snap or snap.get("error"):
        return "CORRELATIONS: (unavailable)"
    lines: list[str] = []
    anomalies = snap.get("anomalies", [])
    if anomalies:
        lines.append(f"CORR ANOMALIES (regime: {snap.get('regime','?')}):")
        for a in anomalies[:max_anomalies]:
            lines.append(
                f"  {a.get('a','?')}/{a.get('b','?')}  20d={a.get('corr_20d','?')}  "
                f"expected={a.get('expected','?')}  ({a.get('sigma',0):+.1f}σ)"
            )
    else:
        lines.append("CORR: no anomalies vs regime baseline")

    network = snap.get("network", {})
    if network:
        top = list(network.items())[:5]
        lines.append("MOST-COUPLED:  " + "  ".join(f"{k}={v}" for k, v in top))

    return "\n".join(lines)
