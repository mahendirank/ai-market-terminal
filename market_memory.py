"""
market_memory.py — Historical market-state memory + analog finder.

When the AI says "tape feels like Aug-2024 pre-FOMC squeeze", we want a real
database answer behind that — not LLM hallucination. This module:

  1. Persists market state snapshots over time (regime + macro + fng + tilt)
  2. On query, finds the K nearest historical snapshots using L2 distance
     over normalized state features
  3. Returns labelled analogs with date, summary, and what happened next
     (forward returns if available)

Storage: SQLite at db/market_memory.db. Snapshots are stored both from:
  - Realtime ticks (every ~hour, via record_snapshot())
  - Backfilled historical periods (one-shot seed scripts can call record_snapshot()
    with synthetic ts to anchor known analog dates)

Forward-return tracking: each snapshot's `forward_returns` JSON is filled in
on a scheduled pass once enough time has elapsed. Lets AI say:
  "Aug-2024 pre-FOMC: SPX +1.2% over next 5 sessions"
with real numbers, not made-up.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "db", "market_memory.db")
_db_lock = threading.Lock()


# ─── Schema ─────────────────────────────────────────────────────────────────
def _db_conn():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              REAL    NOT NULL,
        date_label      TEXT,            -- human label "2024-08-21 pre-FOMC"
        regime          TEXT,
        regime_conf     INTEGER,
        risk_score      REAL,
        infl_score      REAL,
        fed_score       REAL,
        vol_score       REAL,
        credit_score    REAL,
        breadth_score   REAL,
        fng_local       REAL,
        fng_cnn         REAL,
        vix             REAL,
        us10y           REAL,
        dxy             REAL,
        gold            REAL,
        oil             REAL,
        btc             REAL,
        spx             REAL,
        sentiment_tilt  REAL,
        commentary      TEXT,
        forward_returns TEXT             -- JSON: {"spx_5d":0.012, ...}, filled later
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mm_ts ON snapshots(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mm_regime ON snapshots(regime)")
    conn.commit()
    return conn


# ─── Recording ──────────────────────────────────────────────────────────────
_FEATURE_COLUMNS = [
    "risk_score", "infl_score", "fed_score", "vol_score",
    "credit_score", "breadth_score",
    "fng_local", "vix", "us10y", "dxy", "sentiment_tilt",
]


def record_snapshot(
    *,
    regime_state: Optional[dict] = None,
    macro: Optional[dict] = None,
    fng: Optional[dict] = None,
    sentiment_tilt: Optional[float] = None,
    commentary: str = "",
    date_label: str = "",
    ts: Optional[float] = None,
) -> Optional[int]:
    """Persist a market state snapshot. Pulls fresh from the intel layer if
    components aren't supplied. Returns the inserted row id.

    Idempotency: snapshots are append-only; if you want to avoid spam, call
    no more than once per hour or check `last_snapshot_ts()` first.
    """
    ts = ts or time.time()

    # Fill missing components from the live intel layer
    try:
        if regime_state is None:
            from regime_engine import compute_regime_state
            regime_state = compute_regime_state(persist=False).to_dict()
        if macro is None:
            from market_intel import _pull_macro_levels
            macro = _pull_macro_levels() or {}
        if fng is None:
            try:
                from market_sentiment import get_cnn_fng
                fng = {"cnn": get_cnn_fng()}
            except Exception:
                fng = {}
    except Exception as e:
        log.debug("snapshot prerequisites missing: %s", e)
        regime_state = regime_state or {}
        macro = macro or {}
        fng = fng or {}

    def _macro_num(key):
        v = macro.get(key)
        if isinstance(v, dict):
            return v.get("price") or v.get("last")
        return v

    dims = regime_state.get("dimensions", {})
    fng_cnn_raw = fng.get("cnn") or {}

    row = {
        "ts":             ts,
        "date_label":     date_label or time.strftime("%Y-%m-%d %H:%M IST", time.localtime(ts)),
        "regime":         regime_state.get("composite"),
        "regime_conf":    regime_state.get("confidence"),
        "risk_score":     (dims.get("RISK") or {}).get("score"),
        "infl_score":     (dims.get("INFLATION") or {}).get("score"),
        "fed_score":      (dims.get("FED") or {}).get("score"),
        "vol_score":      (dims.get("VOLATILITY") or {}).get("score"),
        "credit_score":   (dims.get("CREDIT") or {}).get("score"),
        "breadth_score":  (dims.get("BREADTH") or {}).get("score"),
        "fng_local":      (fng.get("local") or {}).get("score"),
        "fng_cnn":        fng_cnn_raw.get("score") or fng_cnn_raw.get("now"),
        "vix":            _macro_num("vix"),
        "us10y":          _macro_num("us10y"),
        "dxy":            _macro_num("dxy"),
        "gold":           _macro_num("gold"),
        "oil":            _macro_num("oil"),
        "btc":            _macro_num("btc"),
        "spx":            None,
        "sentiment_tilt": sentiment_tilt,
        "commentary":     commentary,
        "forward_returns": None,
    }

    try:
        with _db_lock:
            conn = _db_conn()
            cols = ",".join(row.keys())
            placeholders = ",".join(["?"] * len(row))
            cur = conn.execute(
                f"INSERT INTO snapshots ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )
            conn.commit()
            rid = cur.lastrowid
            conn.close()
        return rid
    except Exception as e:
        log.debug("snapshot insert failed: %s", e)
        return None


def last_snapshot_ts() -> Optional[float]:
    try:
        with _db_lock:
            conn = _db_conn()
            row = conn.execute(
                "SELECT MAX(ts) FROM snapshots"
            ).fetchone()
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ─── Analog search ──────────────────────────────────────────────────────────
def _featurize(row_or_state: dict) -> Optional[list[float]]:
    """Extract a fixed-length feature vector from either a DB row dict or
    a live regime+macro+fng composite. Returns None if too many features
    are missing (degenerate vector hurts analog search)."""
    vec: list[float] = []
    for col in _FEATURE_COLUMNS:
        v = row_or_state.get(col)
        if v is None:
            return None
        try:
            vec.append(float(v))
        except Exception:
            return None
    return vec


def _normalize(vecs: list[list[float]]) -> tuple[list[float], list[float]]:
    """Compute column-wise mean+std for normalization. Returns (means, stds)."""
    n_cols = len(vecs[0])
    means  = [0.0] * n_cols
    for v in vecs:
        for i, x in enumerate(v):
            means[i] += x
    means = [m / len(vecs) for m in means]
    stds = [0.0] * n_cols
    for v in vecs:
        for i, x in enumerate(v):
            stds[i] += (x - means[i]) ** 2
    stds = [math.sqrt(s / len(vecs)) if s > 0 else 1.0 for s in stds]
    return means, stds


def _l2(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(len(a))))


def find_analogs(
    current_state: dict,
    *,
    k: int = 3,
    min_age_hours: float = 24.0,
    same_regime_bonus: float = 0.5,
) -> list[dict]:
    """Return the K nearest historical snapshots to the current state.

    Parameters
    ----------
    current_state
        Dict with the same feature keys recorded in snapshots (see
        ``_FEATURE_COLUMNS``).
    k
        Number of analogs to return.
    min_age_hours
        Skip snapshots newer than this — recent ticks aren't useful analogs.
    same_regime_bonus
        Multiplier (< 1.0) applied to L2 distance when the analog has the
        same regime as current. Encourages regime-consistent matches.
    """
    curr_vec = _featurize(current_state)
    if curr_vec is None:
        return []

    cutoff_ts = time.time() - min_age_hours * 3600

    try:
        with _db_lock:
            conn = _db_conn()
            rows = conn.execute(
                f"""SELECT id, ts, date_label, regime, regime_conf,
                          {",".join(_FEATURE_COLUMNS)},
                          commentary, forward_returns
                   FROM snapshots WHERE ts <= ?""",
                (cutoff_ts,)
            ).fetchall()
            conn.close()
    except Exception:
        return []
    if not rows:
        return []

    # Build list of (row_dict, feature_vec)
    candidates: list[tuple[dict, list[float]]] = []
    for r in rows:
        d = {
            "id":       r[0],
            "ts":       r[1],
            "date_label": r[2],
            "regime":   r[3],
            "regime_conf": r[4],
        }
        for i, col in enumerate(_FEATURE_COLUMNS):
            d[col] = r[5 + i]
        d["commentary"]      = r[5 + len(_FEATURE_COLUMNS)]
        d["forward_returns"] = r[5 + len(_FEATURE_COLUMNS) + 1]
        v = _featurize(d)
        if v is not None:
            candidates.append((d, v))

    if not candidates:
        return []

    # Normalize using all candidates + current vec
    all_vecs = [v for _, v in candidates] + [curr_vec]
    means, stds = _normalize(all_vecs)
    def norm(v): return [(v[i] - means[i]) / stds[i] for i in range(len(v))]
    curr_n = norm(curr_vec)
    current_regime = current_state.get("regime")

    scored: list[tuple[float, dict]] = []
    for row, vec in candidates:
        d = _l2(curr_n, norm(vec))
        if current_regime and row.get("regime") == current_regime:
            d *= same_regime_bonus
        scored.append((d, row))

    scored.sort(key=lambda x: x[0])
    out: list[dict] = []
    for distance, row in scored[:k]:
        # Parse forward_returns JSON if present
        fr_raw = row.get("forward_returns")
        try:
            fr = json.loads(fr_raw) if fr_raw else {}
        except Exception:
            fr = {}
        out.append({
            "id":          row["id"],
            "ts":          row["ts"],
            "date_label":  row["date_label"],
            "regime":      row["regime"],
            "distance":    round(distance, 3),
            "commentary":  row.get("commentary") or "",
            "forward_returns": fr,
            "summary": (
                f"{row.get('date_label','?')} — {row.get('regime','?')} "
                f"VIX {row.get('vol_score','?')} | "
                f"RISK {row.get('risk_score','?')} | "
                f"INFL {row.get('infl_score','?')}"
            ),
        })
    return out


def format_analogs_for_prompt(analogs: list[dict]) -> str:
    """Compact rendering for AI prompts. Includes forward returns when known."""
    if not analogs:
        return "ANALOGS: (memory empty — first run or insufficient history)"
    lines = ["HISTORICAL ANALOGS (closest matches by state vector):"]
    for i, a in enumerate(analogs, 1):
        bits = [a.get("date_label","?")]
        if a.get("regime"):
            bits.append(f"regime={a['regime']}")
        bits.append(f"dist={a['distance']}")
        fr = a.get("forward_returns") or {}
        if fr:
            fr_bits = [f"{k}:{v:+.2f}%" for k, v in list(fr.items())[:4]]
            bits.append("fwd: " + ", ".join(fr_bits))
        comm = a.get("commentary","")
        if comm:
            bits.append(comm[:80])
        lines.append(f"  {i}. " + "  ·  ".join(bits))
    return "\n".join(lines)


# ─── Convenience: seed common historical analogs ────────────────────────────
def seed_classic_analogs() -> int:
    """Insert a handful of well-known historical episodes so the analog
    finder has anchor points even on a fresh DB. Returns count inserted.

    Idempotent: skipped if any prior anchor exists.
    """
    with _db_lock:
        conn = _db_conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE date_label LIKE '%ANCHOR%'"
        ).fetchone()[0]
        conn.close()
    if n > 0:
        return 0

    # ts: synthetic past timestamps for the anchor episodes. Year-month roughly
    # accurate; exact day not critical for analog search.
    classics = [
        # 2020 Mar — Covid risk-off
        (1583020800, "2020-03-12 ANCHOR Covid crash", "risk_off",
         {"risk_score": 5, "infl_score": 20, "fed_score": 10, "vol_score": 95,
          "credit_score": 90, "breadth_score": 8,
          "fng_local": 5, "vix": 75.5, "us10y": 0.79, "dxy": 99.4,
          "sentiment_tilt": -0.8},
         "Covid pandemic risk-off. VIX 75. Fed emergency cuts. SPX -34% peak-trough."),
        # 2022 Jun — Inflationary peak
        (1655942400, "2022-06-22 ANCHOR Peak inflation", "inflationary",
         {"risk_score": 30, "infl_score": 92, "fed_score": 90, "vol_score": 70,
          "credit_score": 75, "breadth_score": 30,
          "fng_local": 22, "vix": 30.2, "us10y": 3.16, "dxy": 105.2,
          "sentiment_tilt": -0.5},
         "Peak CPI 9.1%. Fed 75bp hike. Equities bear market trough Oct."),
        # 2024 Aug — Carry unwind
        (1723680000, "2024-08-15 ANCHOR Carry unwind", "risk_off",
         {"risk_score": 25, "infl_score": 55, "fed_score": 65, "vol_score": 80,
          "credit_score": 60, "breadth_score": 25,
          "fng_local": 28, "vix": 38.5, "us10y": 3.92, "dxy": 102.8,
          "sentiment_tilt": -0.6},
         "BoJ rate hike + soft NFP = USDJPY -7% in 3 sessions. SPX -8%."),
        # 2024 Nov — Election rally
        (1731283200, "2024-11-11 ANCHOR Trump rally", "risk_on",
         {"risk_score": 78, "infl_score": 55, "fed_score": 55, "vol_score": 28,
          "credit_score": 35, "breadth_score": 75,
          "fng_local": 72, "vix": 14.8, "us10y": 4.45, "dxy": 105.3,
          "sentiment_tilt": +0.5},
         "US election + dovish Fed. SPX ATH, BTC 88k. Dollar strength."),
        # 2025 Apr — Tariff scare (synthetic)
        (1744156800, "2025-04-08 ANCHOR Tariff scare", "risk_off",
         {"risk_score": 35, "infl_score": 68, "fed_score": 45, "vol_score": 75,
          "credit_score": 65, "breadth_score": 30,
          "fng_local": 25, "vix": 34, "us10y": 4.20, "dxy": 102.5,
          "sentiment_tilt": -0.6},
         "Tariff escalation. Equity sell-off. Gold to ATH on safe-haven bid."),
    ]

    inserted = 0
    for ts, label, regime, dims, commentary in classics:
        row = {
            "ts": ts, "date_label": label,
            "regime": regime, "regime_conf": 80,
            **dims,
            "fng_cnn": dims.get("fng_local"),
            "gold": None, "oil": None, "btc": None, "spx": None,
            "commentary": commentary, "forward_returns": None,
        }
        try:
            with _db_lock:
                conn = _db_conn()
                cols = ",".join(row.keys())
                placeholders = ",".join(["?"] * len(row))
                conn.execute(
                    f"INSERT INTO snapshots ({cols}) VALUES ({placeholders})",
                    list(row.values()),
                )
                conn.commit()
                conn.close()
            inserted += 1
        except Exception as e:
            log.debug("seed analog %s failed: %s", label, e)
    return inserted


def get_history(limit: int = 50) -> list[dict]:
    """Return the most recent snapshots — useful for inspecting the DB."""
    try:
        with _db_lock:
            conn = _db_conn()
            rows = conn.execute(
                f"""SELECT id, ts, date_label, regime, regime_conf,
                          {",".join(_FEATURE_COLUMNS)}, commentary
                   FROM snapshots ORDER BY ts DESC LIMIT ?""",
                (limit,)
            ).fetchall()
            conn.close()
        out = []
        for r in rows:
            d = {"id": r[0], "ts": r[1], "date_label": r[2],
                 "regime": r[3], "regime_conf": r[4]}
            for i, col in enumerate(_FEATURE_COLUMNS):
                d[col] = r[5 + i]
            d["commentary"] = r[5 + len(_FEATURE_COLUMNS)]
            out.append(d)
        return out
    except Exception:
        return []
