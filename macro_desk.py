"""
macro_desk.py — Institutional macro regime panel.

Computes 6 INDEPENDENT binary dimensions from live market data + news:

  1. Risk ON / Risk OFF
  2. Dollar Strong / Weak
  3. Fed Hawkish / Dovish
  4. Bond yields Rising / Falling
  5. Inflation Hot / Cooling
  6. Commodities Bullish / Bearish

Each dimension carries: state, confidence %, dominant driver.
Generates desk-style commentary. Persists last 100 snapshots in SQLite.

Inputs (all already maintained elsewhere — this module is read-only on those):
  - live_prices.get_live_prices()  → DXY, US10Y, VIX, NASDAQ, Gold, Oil, BTC
  - regime.detect_market_regime()  → current 10-state classification (for context)
  - news.get_all_news()             → cached headlines, scanned for keywords
"""
import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ── SQLite history ────────────────────────────────────────────────────────────

_DB_DIR = os.path.join(os.path.dirname(__file__), "db")
os.makedirs(_DB_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DB_DIR, "macro_desk.db")
_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db_lock:
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS macro_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ist           TEXT NOT NULL,
                    risk_state       TEXT, risk_conf       INTEGER,
                    dollar_state     TEXT, dollar_conf     INTEGER,
                    fed_state        TEXT, fed_conf        INTEGER,
                    yields_state     TEXT, yields_conf     INTEGER,
                    inflation_state  TEXT, inflation_conf  INTEGER,
                    commodities_state TEXT, commodities_conf INTEGER,
                    overall_conf     INTEGER,
                    dominant_driver  TEXT,
                    commentary       TEXT,
                    raw_signals      TEXT
                )
            """)
            c.commit()

_init_db()


# ── Live data gathering ───────────────────────────────────────────────────────

def _gather_signals() -> dict:
    """Pull prices + regime + news headlines. Tolerant of failures."""
    sig = {
        "dxy_chg": 0.0, "us10y_chg": 0.0, "us10y_lvl": 4.2, "vix": 20.0,
        "nasdaq_chg": 0.0, "spx_chg": 0.0,
        "gold_chg": 0.0, "oil_chg": 0.0, "btc_chg": 0.0,
        "regime": "neutral", "regime_label": "—",
        "news_text": "",
    }
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        def chg(cat, key):
            v = lp.get(cat, {}).get(key, {})
            try: return float((v or {}).get("change", 0) or 0)
            except: return 0.0
        def lvl(cat, key, d=0):
            v = lp.get(cat, {}).get(key, {})
            try: return float((v or {}).get("price", d) or d)
            except: return d
        sig["dxy_chg"]    = chg("fx", "DXY")
        sig["nasdaq_chg"] = chg("global", "NASDAQ")
        sig["spx_chg"]    = chg("global", "SPX")
        sig["gold_chg"]   = chg("commodities", "GOLD")
        sig["oil_chg"]    = chg("commodities", "CRUDE")
        sig["btc_chg"]    = chg("crypto", "BTC")
        sig["us10y_chg"]  = chg("bonds", "US_10Y")
        sig["us10y_lvl"]  = lvl("bonds", "US_10Y", 4.2)
        sig["vix"]        = lvl("vix", "VIX", 20.0)
    except Exception as e:
        print(f"[macro_desk] live_prices: {e}", flush=True)
    try:
        from regime import detect_market_regime
        r = detect_market_regime() or {}
        sig["regime"]       = r.get("regime", "neutral")
        sig["regime_label"] = r.get("label", "—")
    except Exception as e:
        print(f"[macro_desk] regime: {e}", flush=True)
    try:
        from news import get_all_news
        items = (get_all_news() or [])[:60]
        sig["news_text"] = " ".join(
            str(it.get("text") or it.get("title", "") or "") for it in items
        ).lower()
    except Exception as e:
        print(f"[macro_desk] news: {e}", flush=True)
    return sig


# ── Per-dimension scoring ─────────────────────────────────────────────────────

def _kw(text: str, *words: str) -> int:
    return sum(1 for w in words if w in text)


def _score_risk(sig) -> dict:
    """Risk ON vs Risk OFF."""
    s = 0
    drivers = []
    nq, spx, vix = sig["nasdaq_chg"], sig["spx_chg"], sig["vix"]
    gold, btc, dxy = sig["gold_chg"], sig["btc_chg"], sig["dxy_chg"]
    if vix < 15:  s += 25; drivers.append(f"VIX low ({vix:.1f})")
    elif vix < 18: s += 12; drivers.append(f"VIX subdued ({vix:.1f})")
    elif vix > 25: s -= 25; drivers.append(f"VIX elevated ({vix:.1f})")
    elif vix > 22: s -= 12; drivers.append(f"VIX rising ({vix:.1f})")
    if nq > 1.0:  s += 18; drivers.append(f"NDX bullish ({nq:+.1f}%)")
    elif nq > 0.3: s += 10
    elif nq < -1.0: s -= 18; drivers.append(f"NDX selling ({nq:+.1f}%)")
    elif nq < -0.3: s -= 10
    if spx >  0.5: s +=  8
    elif spx < -0.5: s -=  8
    if gold > 0.5: s -=  6
    elif gold < -0.3: s += 5
    if btc > 1.5:  s += 5
    elif btc < -1.5: s -= 5
    if dxy > 0.3:  s -= 5
    elif dxy < -0.3: s += 5
    return _classify(s, ("ON", "OFF"), drivers)


def _score_dollar(sig) -> dict:
    """Dollar Strong vs Weak."""
    s = 0
    drivers = []
    dxy = sig["dxy_chg"]
    y10c = sig["us10y_chg"]
    reg = sig["regime"]
    vix = sig["vix"]
    if dxy > 0.3:  s += 28; drivers.append(f"DXY +{dxy:.2f}%")
    elif dxy > 0.1: s += 14; drivers.append(f"DXY mildly up")
    elif dxy < -0.3: s -= 28; drivers.append(f"DXY {dxy:+.2f}%")
    elif dxy < -0.1: s -= 14; drivers.append(f"DXY mildly down")
    if y10c > 0.5:  s += 12; drivers.append("US yields supportive")
    elif y10c < -0.5: s -= 12
    if reg == "liquidity_crisis": s += 15; drivers.append("Liquidity crisis → USD bid")
    if reg == "risk_off" and vix > 25: s += 10; drivers.append("Risk-off USD haven flow")
    if reg == "central_bank_dovish": s -= 12
    if reg == "central_bank_hawkish": s += 12
    return _classify(s, ("STRONG", "WEAK"), drivers)


def _score_fed(sig) -> dict:
    """Fed Hawkish vs Dovish."""
    s = 0
    drivers = []
    nl = sig["news_text"]
    y10c = sig["us10y_chg"]
    y10l = sig["us10y_lvl"]
    reg = sig["regime"]
    # News keywords
    hawk = _kw(nl, "rate hike", "hawkish", "tightening", "higher for longer", "fed hawkish", "tough on inflation")
    dove = _kw(nl, "rate cut", "dovish", "easing", "fed pivot", "fed dovish", "lower rates", "rate cuts")
    if hawk: s += 12 * min(hawk, 3); drivers.append(f"News hawkish ({hawk}x)")
    if dove: s -= 12 * min(dove, 3); drivers.append(f"News dovish ({dove}x)")
    # Yields = market's pricing of Fed
    if y10c > 0.5: s += 14; drivers.append(f"10Y +{y10c:.2f}% → hawkish pricing")
    elif y10c < -0.5: s -= 14; drivers.append(f"10Y {y10c:+.2f}% → dovish pricing")
    if y10l > 5.0: s += 8
    elif y10l < 3.8: s -= 8
    if reg == "central_bank_dovish": s -= 25; drivers.append("Regime: CB DOVISH")
    if reg == "central_bank_hawkish": s += 25; drivers.append("Regime: CB HAWKISH")
    return _classify(s, ("HAWKISH", "DOVISH"), drivers)


def _score_yields(sig) -> dict:
    """Bond yields Rising vs Falling."""
    s = 0
    drivers = []
    y10c = sig["us10y_chg"]
    y10l = sig["us10y_lvl"]
    if y10c >  0.7:  s += 30; drivers.append(f"US10Y +{y10c:.2f}%")
    elif y10c >  0.3: s += 18; drivers.append(f"US10Y +{y10c:.2f}%")
    elif y10c < -0.7: s -= 30; drivers.append(f"US10Y {y10c:+.2f}%")
    elif y10c < -0.3: s -= 18; drivers.append(f"US10Y {y10c:+.2f}%")
    if y10l > 4.8: s += 6; drivers.append(f"10Y at {y10l:.2f}%")
    elif y10l < 3.8: s -= 6
    return _classify(s, ("RISING", "FALLING"), drivers)


def _score_inflation(sig) -> dict:
    """Inflation Hot vs Cooling."""
    s = 0
    drivers = []
    nl = sig["news_text"]
    oil, gold, y10c = sig["oil_chg"], sig["gold_chg"], sig["us10y_chg"]
    hot = _kw(nl, "inflation", "cpi rise", "ppi", "hot inflation", "price pressure", "sticky inflation", "oil surge", "supply shock")
    cool = _kw(nl, "disinflation", "inflation cool", "cpi cool", "soft inflation", "cooling prices", "price drop", "deflation")
    if hot:  s += 8 * min(hot, 4); drivers.append(f"News inflation ({hot}x)")
    if cool: s -= 10 * min(cool, 3); drivers.append(f"News disinflation ({cool}x)")
    if oil > 1.5: s += 12; drivers.append(f"Oil +{oil:.1f}%")
    elif oil < -1.5: s -= 12
    if gold > 0.5: s += 5
    if y10c > 0.5: s += 6
    elif y10c < -0.5: s -= 6
    if sig["regime"] == "inflationary": s += 18; drivers.append("Regime: INFLATIONARY")
    if sig["regime"] == "stagflation": s += 12; drivers.append("Regime: STAGFLATION")
    return _classify(s, ("HOT", "COOLING"), drivers)


def _score_commodities(sig) -> dict:
    """Commodities Bullish vs Bearish (aggregate)."""
    s = 0
    drivers = []
    g, o = sig["gold_chg"], sig["oil_chg"]
    avg = (g + o) / 2
    if avg >  0.6: s += 25; drivers.append(f"Gold+Oil avg {avg:+.1f}%")
    elif avg >  0.2: s += 14
    elif avg < -0.6: s -= 25; drivers.append(f"Gold+Oil avg {avg:+.1f}%")
    elif avg < -0.2: s -= 14
    if g > 0.5: s += 6; drivers.append(f"Gold +{g:.2f}%")
    elif g < -0.5: s -= 6
    if o > 1.0: s += 8; drivers.append(f"Oil +{o:.2f}%")
    elif o < -1.0: s -= 8
    if sig["regime"] == "commodity_supercycle": s += 18; drivers.append("Regime: COMMODITY SUPERCYCLE")
    return _classify(s, ("BULL", "BEAR"), drivers)


def _classify(score: int, labels: tuple, drivers: list) -> dict:
    """Score → (state, confidence, top driver). labels = (positive_label, negative_label)."""
    pos, neg = labels
    if score >= 15:
        state = pos
        conf = min(60 + score // 2, 92)
    elif score <= -15:
        state = neg
        conf = min(60 + (-score) // 2, 92)
    else:
        state = "NEUTRAL"
        conf = max(42 + abs(score), 50)
    return {
        "state":      state,
        "confidence": int(conf),
        "driver":     drivers[0] if drivers else "Mixed signals",
        "all_drivers": drivers[:4],
        "score":      score,
    }


# ── Desk-style commentary generator ───────────────────────────────────────────

def _generate_commentary(dims: dict, sig: dict) -> str:
    """Generate 2-3 sentence institutional desk commentary."""
    parts = []
    risk = dims["risk"]
    dollar = dims["dollar"]
    fed = dims["fed"]
    yields = dims["yields"]
    infl = dims["inflation"]
    comm = dims["commodities"]

    # 1) Opening — risk regime
    if risk["state"] == "ON":
        if risk["confidence"] >= 75:
            parts.append("Markets are firmly risk-on.")
        elif risk["confidence"] >= 60:
            parts.append("Markets are leaning risk-on.")
        else:
            parts.append("Markets show mildly risk-on tone.")
    elif risk["state"] == "OFF":
        if risk["confidence"] >= 75:
            parts.append("Risk-off conditions are dominating.")
        elif risk["confidence"] >= 60:
            parts.append("Markets are leaning risk-off.")
        else:
            parts.append("Markets show mild risk-off bias.")
    else:
        parts.append("Markets are caught between risk on and risk off.")

    # 2) The drivers — yields + dollar tend to be paired
    cause_clauses = []
    if yields["state"] == "FALLING" and yields["confidence"] >= 60:
        cause_clauses.append("falling US yields")
    elif yields["state"] == "RISING" and yields["confidence"] >= 60:
        cause_clauses.append("rising yields")

    if dollar["state"] == "WEAK" and dollar["confidence"] >= 60:
        cause_clauses.append("a softer dollar")
    elif dollar["state"] == "STRONG" and dollar["confidence"] >= 60:
        cause_clauses.append("a stronger dollar")

    if fed["state"] == "DOVISH" and fed["confidence"] >= 65:
        cause_clauses.append("dovish Fed positioning")
    elif fed["state"] == "HAWKISH" and fed["confidence"] >= 65:
        cause_clauses.append("hawkish Fed pricing")

    if infl["state"] == "COOLING" and infl["confidence"] >= 60:
        cause_clauses.append("softer inflation expectations")
    elif infl["state"] == "HOT" and infl["confidence"] >= 60:
        cause_clauses.append("sticky inflation")

    if cause_clauses:
        joined = cause_clauses[0] if len(cause_clauses) == 1 else (
                 " and ".join(cause_clauses) if len(cause_clauses) == 2 else
                 ", ".join(cause_clauses[:-1]) + " and " + cause_clauses[-1])
        if risk["state"] == "ON":
            parts.append(f"Driven by {joined}, supporting equities and risk assets.")
        elif risk["state"] == "OFF":
            parts.append(f"Driven by {joined}, pressuring equities and high-beta assets.")
        else:
            parts.append(f"Cross-currents from {joined} keep the picture mixed.")

    # 3) Commodities tone
    if comm["state"] == "BULL" and comm["confidence"] >= 65:
        parts.append("Commodities are bid — gold and oil supportive.")
    elif comm["state"] == "BEAR" and comm["confidence"] >= 65:
        parts.append("Commodities under pressure across the complex.")

    return " ".join(parts)


def _dominant_driver(dims: dict, sig: dict) -> str:
    """The single most-confident driving force across all dimensions."""
    # Sort dimensions by confidence (excluding NEUTRAL)
    candidates = [(k, v) for k, v in dims.items() if v["state"] != "NEUTRAL"]
    if not candidates:
        return "Mixed signals across all macro dimensions"
    candidates.sort(key=lambda x: x[1]["confidence"], reverse=True)
    top_key, top = candidates[0]
    return f"{top_key.upper()}: {top['state']} ({top['confidence']}%) — {top['driver']}"


# ── Main entry point ──────────────────────────────────────────────────────────

def get_macro_regime_view() -> dict:
    """Returns the full institutional macro regime panel payload."""
    sig = _gather_signals()
    dims = {
        "risk":        _score_risk(sig),
        "dollar":      _score_dollar(sig),
        "fed":         _score_fed(sig),
        "yields":      _score_yields(sig),
        "inflation":   _score_inflation(sig),
        "commodities": _score_commodities(sig),
    }
    commentary = _generate_commentary(dims, sig)
    driver = _dominant_driver(dims, sig)
    overall_conf = int(sum(d["confidence"] for d in dims.values()) / len(dims))
    return {
        "generated_at":     datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        "dimensions":       dims,
        "commentary":       commentary,
        "dominant_driver":  driver,
        "overall_confidence": overall_conf,
        "signals_used": {
            "dxy_chg":    round(sig["dxy_chg"], 2),
            "us10y_chg":  round(sig["us10y_chg"], 2),
            "us10y_lvl":  round(sig["us10y_lvl"], 2),
            "vix":        round(sig["vix"], 1),
            "nasdaq_chg": round(sig["nasdaq_chg"], 2),
            "spx_chg":    round(sig["spx_chg"], 2),
            "gold_chg":   round(sig["gold_chg"], 2),
            "oil_chg":    round(sig["oil_chg"], 2),
            "btc_chg":    round(sig["btc_chg"], 2),
            "regime":     sig["regime"],
            "regime_label": sig["regime_label"],
        },
        "history": get_history(limit=10),
    }


# ── Snapshot persistence ──────────────────────────────────────────────────────

def store_snapshot(view: dict) -> None:
    """Persist a snapshot for historical memory."""
    try:
        dims = view["dimensions"]
        with _db_lock:
            with _conn() as c:
                c.execute("""
                    INSERT INTO macro_snapshots (
                        ts_ist, risk_state, risk_conf, dollar_state, dollar_conf,
                        fed_state, fed_conf, yields_state, yields_conf,
                        inflation_state, inflation_conf,
                        commodities_state, commodities_conf,
                        overall_conf, dominant_driver, commentary, raw_signals
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    view["generated_at"],
                    dims["risk"]["state"], dims["risk"]["confidence"],
                    dims["dollar"]["state"], dims["dollar"]["confidence"],
                    dims["fed"]["state"], dims["fed"]["confidence"],
                    dims["yields"]["state"], dims["yields"]["confidence"],
                    dims["inflation"]["state"], dims["inflation"]["confidence"],
                    dims["commodities"]["state"], dims["commodities"]["confidence"],
                    view["overall_confidence"],
                    view["dominant_driver"],
                    view["commentary"],
                    json.dumps(view.get("signals_used", {}))
                ))
                # Keep only last 200 rows
                c.execute("""
                    DELETE FROM macro_snapshots WHERE id NOT IN (
                        SELECT id FROM macro_snapshots ORDER BY id DESC LIMIT 200
                    )
                """)
                c.commit()
    except Exception as e:
        print(f"[macro_desk] store_snapshot: {e}", flush=True)


def get_history(limit: int = 10) -> list:
    """Last N snapshots, newest first."""
    try:
        with _db_lock:
            with _conn() as c:
                rows = c.execute("""
                    SELECT ts_ist, risk_state, risk_conf, dollar_state, dollar_conf,
                           fed_state, fed_conf, yields_state, yields_conf,
                           inflation_state, inflation_conf,
                           commodities_state, commodities_conf,
                           overall_conf, dominant_driver, commentary
                    FROM macro_snapshots
                    ORDER BY id DESC
                    LIMIT ?
                """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[macro_desk] get_history: {e}", flush=True)
        return []
