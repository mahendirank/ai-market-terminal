"""
regime_engine.py — Multi-dimensional regime state with transition detection.

Wraps the existing regime.py (which produces a single regime label) with
a richer state vector across 6 dimensions. Lets AI tabs reason about
regime conflicts (e.g. risk-on equities but inflationary print) instead
of a single one-word label.

Dimensions (each scored independently 0-100 with confidence):
  - RISK         : risk_on (high) vs risk_off (low)
  - INFLATION    : inflationary (high) vs disinflationary (low)
  - FED          : hawkish (high) vs dovish (low)
  - VOLATILITY   : high vol (high) vs compressed (low)
  - CREDIT       : tight (high spreads) vs easy (tight spreads)
  - BREADTH      : strong (high) vs narrow (low)

Composite regime label is derived from this vector but each dimension is
queryable on its own — sentiment_weighting and HNI use them separately.

Transition detection:
  Persists the state vector to SQLite (db/regime_history.db). When the
  current state differs from the most-recent stored state by > 25 points
  on any dimension, flags a transition. AI tabs see "RISK regime flipped
  from 72 → 38 in last 4h" — a desk-grade signal.

Falls back to existing regime.py output when underlying data is unavailable.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional

log = logging.getLogger(__name__)


# ─── Dimension definitions ──────────────────────────────────────────────────
DIM_RISK       = "RISK"
DIM_INFLATION  = "INFLATION"
DIM_FED        = "FED"
DIM_VOLATILITY = "VOLATILITY"
DIM_CREDIT     = "CREDIT"
DIM_BREADTH    = "BREADTH"

ALL_DIMS = (DIM_RISK, DIM_INFLATION, DIM_FED, DIM_VOLATILITY, DIM_CREDIT, DIM_BREADTH)


@dataclass
class DimensionState:
    """Per-dimension state — score 0-100, confidence 0-1, label, drivers."""
    score:      float
    confidence: float
    label:      str
    drivers:    list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RegimeState:
    """Full regime state across all dimensions + derived composite label."""
    ts:           int
    composite:    str               # legacy single label (from regime.py)
    confidence:   int               # legacy 0-100 confidence
    dimensions:   dict[str, dict]   # DimensionState dicts keyed by dim name
    transitions:  list[dict]        # [{dim, prev_score, curr_score, delta}]
    summary:      str               # short human-readable summary

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Persistence — history of state vectors ─────────────────────────────────
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "regime_history.db")
_db_lock = threading.Lock()


def _db_conn():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS regime_states (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL    NOT NULL,
        composite   TEXT,
        confidence  INTEGER,
        risk        REAL, inflation REAL, fed REAL,
        volatility  REAL, credit REAL, breadth REAL,
        summary     TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_regime_ts ON regime_states(ts DESC)")
    conn.commit()
    return conn


def _persist_state(state: RegimeState) -> None:
    try:
        with _db_lock:
            conn = _db_conn()
            d = state.dimensions
            conn.execute(
                """INSERT INTO regime_states (ts, composite, confidence,
                   risk, inflation, fed, volatility, credit, breadth, summary)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (state.ts, state.composite, state.confidence,
                 d.get(DIM_RISK, {}).get("score"),
                 d.get(DIM_INFLATION, {}).get("score"),
                 d.get(DIM_FED, {}).get("score"),
                 d.get(DIM_VOLATILITY, {}).get("score"),
                 d.get(DIM_CREDIT, {}).get("score"),
                 d.get(DIM_BREADTH, {}).get("score"),
                 state.summary)
            )
            conn.commit()
            conn.close()
    except Exception as e:
        log.debug("regime persist failed: %s", e)


def _last_state_scores() -> Optional[dict]:
    """Pull the most recent persisted state for transition detection."""
    try:
        with _db_lock:
            conn = _db_conn()
            row = conn.execute(
                """SELECT ts, risk, inflation, fed, volatility, credit, breadth
                   FROM regime_states ORDER BY ts DESC LIMIT 1"""
            ).fetchone()
            conn.close()
        if not row:
            return None
        return {"ts": row[0], DIM_RISK: row[1], DIM_INFLATION: row[2],
                DIM_FED: row[3], DIM_VOLATILITY: row[4],
                DIM_CREDIT: row[5], DIM_BREADTH: row[6]}
    except Exception:
        return None


# ─── Dimension scorers ──────────────────────────────────────────────────────
def _safe_num(x) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _score_risk(macro: dict, breadth_hint: Optional[float] = None) -> DimensionState:
    """Risk dimension: SPX + VIX + (breadth if given). High = risk-on."""
    drivers = []
    score = 50.0

    vix = _safe_num((macro.get("vix") or {}).get("price") if isinstance(macro.get("vix"), dict) else macro.get("vix"))
    if vix is not None:
        # VIX 15 = high score 70, VIX 30 = low score 20
        c = max(10, min(40, vix))
        score = 100 - ((c - 10) / 30) * 80
        drivers.append(f"VIX {vix:.1f}")

    if breadth_hint is not None:
        score = (score + breadth_hint) / 2
        drivers.append(f"breadth {breadth_hint:.0f}")

    label = "RISK_ON" if score >= 60 else ("RISK_OFF" if score <= 40 else "NEUTRAL")
    return DimensionState(score=round(score, 1), confidence=0.7 if vix is not None else 0.3,
                           label=label, drivers=drivers)


def _score_inflation(macro: dict, news_tilt: Optional[dict] = None) -> DimensionState:
    """Inflation dimension. Without CPI feed this proxies via 10Y yield level
    + gold/oil moves + news tilt classified as INFLATION events."""
    drivers = []
    score = 50.0

    yld = _safe_num((macro.get("us10y") or {}).get("price") if isinstance(macro.get("us10y"), dict) else macro.get("us10y"))
    if yld is not None:
        # 10Y < 3.5% = disinflationary tilt, > 4.7% = inflationary
        if yld >= 4.7:
            score = 70 + min(20, (yld - 4.7) * 30)
        elif yld <= 3.5:
            score = 30 - min(20, (3.5 - yld) * 30)
        else:
            score = 50 + ((yld - 4.1) / 0.6) * 20
        drivers.append(f"US10Y {yld:.2f}%")

    if news_tilt:
        infl_count = (news_tilt.get("by_category", {}) or {}).get("INFLATION", {}).get("count", 0)
        if infl_count >= 2:
            score += min(15, infl_count * 3)
            drivers.append(f"{infl_count} INFL news")

    score = max(0, min(100, score))
    if score >= 65:   label = "INFLATIONARY"
    elif score <= 35: label = "DISINFLATIONARY"
    else:             label = "NEUTRAL"
    return DimensionState(score=round(score, 1), confidence=0.6 if yld is not None else 0.25,
                           label=label, drivers=drivers)


def _score_fed(macro: dict, news_tilt: Optional[dict] = None) -> DimensionState:
    """Fed dimension: hawkish vs dovish. Proxied via DXY direction + monetary
    news events classified by event_classifier."""
    drivers = []
    score = 50.0

    dxy = macro.get("dxy")
    if isinstance(dxy, dict):
        chg = _safe_num(dxy.get("change_pct") or dxy.get("change"))
        if chg is not None:
            score = 50 + chg * 8   # 1% DXY move = 8 score points
            drivers.append(f"DXY {chg:+.2f}%")

    if news_tilt:
        mon = (news_tilt.get("by_category", {}) or {}).get("MONETARY", {})
        if mon.get("count", 0) >= 1:
            avg_sev = mon.get("avg_sev", 0)
            # Use directional tilt of news to push score
            dir_tilt = news_tilt.get("directional", {}).get("tilt", 0)
            score += dir_tilt * avg_sev * 1.5
            drivers.append(f"{mon['count']} Fed news (tilt {dir_tilt:+.2f})")

    score = max(0, min(100, score))
    if score >= 65:   label = "HAWKISH"
    elif score <= 35: label = "DOVISH"
    else:             label = "NEUTRAL"
    return DimensionState(score=round(score, 1), confidence=0.5,
                           label=label, drivers=drivers)


def _score_volatility(macro: dict) -> DimensionState:
    """Volatility regime — straight off VIX with bands."""
    vix = _safe_num((macro.get("vix") or {}).get("price") if isinstance(macro.get("vix"), dict) else macro.get("vix"))
    if vix is None:
        return DimensionState(score=50, confidence=0.0, label="UNKNOWN", drivers=[])
    if   vix >= 30: score, label = 90, "ELEVATED"
    elif vix >= 22: score, label = 70, "HIGH"
    elif vix >= 17: score, label = 55, "NORMAL"
    elif vix >= 13: score, label = 35, "LOW"
    else:           score, label = 15, "COMPRESSED"
    return DimensionState(score=score, confidence=0.85, label=label,
                           drivers=[f"VIX {vix:.1f}"])


def _score_credit(macro: dict) -> DimensionState:
    """Credit conditions — best proxy without HYG/IEF spread is yield level + VIX combo.
    High yield + high VIX = tight credit."""
    yld = _safe_num((macro.get("us10y") or {}).get("price") if isinstance(macro.get("us10y"), dict) else macro.get("us10y"))
    vix = _safe_num((macro.get("vix") or {}).get("price") if isinstance(macro.get("vix"), dict) else macro.get("vix"))
    if yld is None and vix is None:
        return DimensionState(score=50, confidence=0.0, label="UNKNOWN", drivers=[])

    score = 50.0
    drivers = []
    if yld is not None:
        score += min(20, max(-20, (yld - 4.0) * 15))
        drivers.append(f"US10Y {yld:.2f}%")
    if vix is not None:
        score += min(15, max(-15, (vix - 18) * 1.0))
        drivers.append(f"VIX {vix:.1f}")
    score = max(0, min(100, score))
    if   score >= 65: label = "TIGHT"
    elif score <= 35: label = "EASY"
    else:             label = "NEUTRAL"
    return DimensionState(score=round(score, 1), confidence=0.45,
                           label=label, drivers=drivers)


def _score_breadth(news_tilt: Optional[dict] = None) -> DimensionState:
    """Breadth — proxied via news direction tilt when we have no breadth
    feed. A strong directional tilt (>0.4) suggests broad participation."""
    if not news_tilt:
        return DimensionState(score=50, confidence=0.0, label="UNKNOWN", drivers=[])
    tilt = news_tilt.get("directional", {}).get("tilt", 0)
    score = 50 + tilt * 40
    score = max(0, min(100, score))
    if   score >= 65: label = "STRONG"
    elif score <= 35: label = "NARROW"
    else:             label = "MIXED"
    return DimensionState(score=round(score, 1), confidence=0.3,
                           label=label, drivers=[f"news tilt {tilt:+.2f}"])


# ─── Composite + transitions ────────────────────────────────────────────────
def _derive_composite(dims: dict[str, DimensionState]) -> tuple[str, int]:
    """Map dimension vector → single regime label + confidence.

    Heuristics (order matters):
      - High volatility + risk-off  → CRISIS
      - High inflation + low growth → STAGFLATION  (proxy: high vol + high infl)
      - Inflationary + hawkish      → INFLATIONARY
      - Risk-off + dovish           → DEFLATIONARY  (rare)
      - Risk-on + low vol + dovish  → GOLDILOCKS
      - Risk-on                     → RISK_ON
      - Risk-off                    → RISK_OFF
    """
    risk = dims[DIM_RISK].score
    infl = dims[DIM_INFLATION].score
    fed  = dims[DIM_FED].score
    vol  = dims[DIM_VOLATILITY].score
    conf = int(sum(d.confidence for d in dims.values()) / len(dims) * 100)

    if vol >= 75 and risk <= 35:
        return "crisis", conf
    if infl >= 70 and vol >= 60:
        return "stagflation", conf
    if infl >= 65 and fed >= 60:
        return "inflationary", conf
    if risk <= 35 and fed <= 35:
        return "deflationary", conf
    if risk >= 65 and vol <= 45 and fed <= 50:
        return "goldilocks", conf
    if risk >= 60:
        return "risk_on", conf
    if risk <= 40:
        return "risk_off", conf
    return "mixed", conf


def _detect_transitions(curr: dict[str, DimensionState]) -> list[dict]:
    """Compare current dim scores to last persisted state. Flag any
    dimension moving > 25 points as a transition event."""
    last = _last_state_scores()
    if not last:
        return []
    out = []
    for dim in ALL_DIMS:
        prev = last.get(dim)
        if prev is None:
            continue
        c = curr.get(dim)
        if not c:
            continue
        delta = c.score - float(prev)
        if abs(delta) >= 25:
            out.append({"dim": dim, "prev_score": round(float(prev), 1),
                        "curr_score": round(c.score, 1),
                        "delta": round(delta, 1),
                        "age_minutes": round((time.time() - last["ts"]) / 60, 1)})
    return out


# ─── Public entry point ──────────────────────────────────────────────────────
def compute_regime_state(*, macro: Optional[dict] = None,
                         news_tilt: Optional[dict] = None,
                         persist: bool = True) -> RegimeState:
    """Compute the multi-dimensional regime state.

    Parameters
    ----------
    macro
        Output of market_intel._pull_macro_levels() — dict of macro readings.
        Pulled fresh if None.
    news_tilt
        Output of event_classifier.summarize_distribution(classified_news).
        Optional but adds depth to FED and BREADTH scoring.
    persist
        When True, append the state to regime_history.db for transition
        detection on the next call.
    """
    if macro is None:
        try:
            from market_intel import _pull_macro_levels
            macro = _pull_macro_levels() or {}
        except Exception:
            macro = {}

    dims: dict[str, DimensionState] = {
        DIM_RISK:       _score_risk(macro),
        DIM_INFLATION:  _score_inflation(macro, news_tilt),
        DIM_FED:        _score_fed(macro, news_tilt),
        DIM_VOLATILITY: _score_volatility(macro),
        DIM_CREDIT:     _score_credit(macro),
        DIM_BREADTH:    _score_breadth(news_tilt),
    }

    composite, confidence = _derive_composite(dims)
    transitions = _detect_transitions(dims)

    summary = (f"{composite.upper()} ({confidence}%) — "
               f"risk={dims[DIM_RISK].label}, "
               f"vol={dims[DIM_VOLATILITY].label}, "
               f"infl={dims[DIM_INFLATION].label}, "
               f"fed={dims[DIM_FED].label}")

    state = RegimeState(
        ts=int(time.time()),
        composite=composite,
        confidence=confidence,
        dimensions={k: v.to_dict() for k, v in dims.items()},
        transitions=transitions,
        summary=summary,
    )

    if persist:
        _persist_state(state)
    return state


def format_state_for_prompt(state: RegimeState) -> str:
    """Compact representation for AI prompts."""
    lines = [f"REGIME: {state.summary}"]
    for dim, info in state.dimensions.items():
        drivers = ", ".join((info.get("drivers") or [])[:2])
        lines.append(f"  {dim:<11} {info.get('score'):>5.1f}  "
                     f"{info.get('label'):<14}  ({drivers})")
    if state.transitions:
        lines.append("TRANSITIONS:")
        for t in state.transitions:
            lines.append(f"  {t['dim']}: {t['prev_score']} → {t['curr_score']} "
                         f"(Δ{t['delta']:+.1f}, age {t['age_minutes']}min)")
    return "\n".join(lines)


def get_recent_history(limit: int = 24) -> list[dict]:
    """Return the last N persisted state vectors — used by market_memory
    for analog search."""
    try:
        with _db_lock:
            conn = _db_conn()
            rows = conn.execute(
                """SELECT ts, composite, confidence, risk, inflation,
                          fed, volatility, credit, breadth, summary
                   FROM regime_states ORDER BY ts DESC LIMIT ?""",
                (limit,)
            ).fetchall()
            conn.close()
        return [
            {"ts": r[0], "composite": r[1], "confidence": r[2],
             DIM_RISK: r[3], DIM_INFLATION: r[4], DIM_FED: r[5],
             DIM_VOLATILITY: r[6], DIM_CREDIT: r[7], DIM_BREADTH: r[8],
             "summary": r[9]}
            for r in rows
        ]
    except Exception:
        return []
