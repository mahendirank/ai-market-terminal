"""
signal_memory.py — Self-learning signal memory, accuracy, and performance engine.

Every signal is stored. After 24h price is verified.
Regime performance is tracked. Confidence adapts from history.
AI prompts are enriched with historical pattern data.
"""
import os, json, time, sqlite3, threading
from datetime import datetime, timezone, timedelta

IST      = timezone(timedelta(hours=5, minutes=30))
DB_PATH  = os.path.join(os.path.dirname(__file__), "db", "signal_memory.db")
_db_lock = threading.Lock()

# ── Asset → yfinance symbol ────────────────────────────────────────────────────
VERIFY_MAP = {
    "NIFTY50":   "^NSEI",    "NIFTY":      "^NSEI",
    "BANKNIFTY": "^NSEBANK", "BANKN":      "^NSEBANK",
    "SENSEX":    "^BSESN",
    "GOLD":      "GC=F",     "XAUUSD":     "GC=F",
    "CRUDE":     "CL=F",     "OIL":        "CL=F",     "WTI":  "CL=F",
    "NASDAQ":    "^IXIC",    "NAS100":     "^IXIC",    "NDX":  "^IXIC",
    "SPX":       "^GSPC",    "S&P500":     "^GSPC",
    "DOW":       "^DJI",     "US30":       "^DJI",
    "BTC":       "BTC-USD",  "BITCOIN":    "BTC-USD",
    "DXY":       "DX-Y.NYB",
    "SILVER":    "SI=F",
}

SIGNAL_DIRECTION = {
    "BUY": 1, "STRONG BUY": 1,
    "SELL": -1, "STRONG SELL": -1,
    "NO TRADE": 0, "WAIT": 0, "NEUTRAL": 0,
}

# Quality label thresholds
QUALITY_HIGH     = "HIGH PROBABILITY"
QUALITY_MODERATE = "MODERATE"
QUALITY_LOW      = "LOW CONFIDENCE"


# ── DB setup ───────────────────────────────────────────────────────────────────

def _get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock:
        conn = _get_conn()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL    NOT NULL,
            timestamp_ist   TEXT    NOT NULL,
            headline        TEXT,
            regime          TEXT,
            regime_label    TEXT,
            regime_conf     INTEGER DEFAULT 0,
            signal          TEXT    NOT NULL,
            score           REAL    DEFAULT 0,
            confidence      INTEGER DEFAULT 0,
            session         TEXT,
            quality_label   TEXT    DEFAULT 'MODERATE',
            insights        TEXT,
            affected_assets TEXT,
            primary_asset   TEXT,
            entry_price     REAL    DEFAULT 0,
            live_prices     TEXT,
            verified        INTEGER DEFAULT 0,
            verify_ts       REAL    DEFAULT 0,
            price_at_24h    REAL    DEFAULT 0,
            pct_move        REAL    DEFAULT 0,
            outcome         TEXT    DEFAULT 'PENDING',
            outcome_note    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_signals_ts       ON signals(ts);
        CREATE INDEX IF NOT EXISTS idx_signals_verified ON signals(verified);
        CREATE INDEX IF NOT EXISTS idx_signals_regime   ON signals(regime);
        CREATE INDEX IF NOT EXISTS idx_signals_outcome  ON signals(outcome);

        CREATE TABLE IF NOT EXISTS analytics_cache (
            key  TEXT PRIMARY KEY,
            data TEXT,
            ts   REAL
        );

        CREATE TABLE IF NOT EXISTS regime_confidence (
            regime      TEXT PRIMARY KEY,
            boost       INTEGER DEFAULT 0,
            updated_ts  REAL    DEFAULT 0
        );
        """)
        # Safe column migrations for existing DBs
        for col, defn in [
            ("live_prices",  "TEXT"),
            ("quality_label","TEXT DEFAULT 'MODERATE'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {defn}")
                conn.commit()
            except Exception:
                pass  # column already exists
        conn.commit()
        conn.close()


# ── Quality label ──────────────────────────────────────────────────────────────

def compute_quality_label(regime_key: str, confidence: int, score: float) -> str:
    """
    Compute signal quality based on:
    - Historical regime win rate (self-learning)
    - Current confidence score
    - Signal strength (abs score)
    """
    abs_score = abs(score)

    # Read historical regime win rate
    regime_wr = 0
    try:
        with _db_lock:
            conn = _get_conn()
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins
                FROM signals
                WHERE regime=? AND verified=1
            """, (regime_key,)).fetchone()
            conn.close()
        if row and row["total"] >= 3:
            decisive = row["wins"] or 0
            regime_wr = round(decisive / row["total"] * 100, 1)
    except Exception:
        pass

    # HIGH PROBABILITY: strong regime history + confident signal
    if (regime_wr >= 65 and confidence >= 65 and abs_score >= 4):
        return QUALITY_HIGH
    if (confidence >= 75 and abs_score >= 5):
        return QUALITY_HIGH

    # LOW CONFIDENCE: weak history or very low confidence
    if confidence < 45 or abs_score < 1.5:
        return QUALITY_LOW
    if regime_wr > 0 and regime_wr < 40:
        return QUALITY_LOW

    return QUALITY_MODERATE


# ── Self-learning confidence boost ────────────────────────────────────────────

def get_confidence_boost(regime_key: str) -> int:
    """
    Returns extra confidence % to add based on historical regime performance.
    Checks last 10 signals + overall regime win rate.
    Max boost: +15.
    """
    if not regime_key:
        return 0
    try:
        with _db_lock:
            conn = _get_conn()

            # Last 10 verified signals in this regime
            recent = conn.execute("""
                SELECT outcome FROM signals
                WHERE regime=? AND verified=1
                ORDER BY ts DESC LIMIT 10
            """, (regime_key,)).fetchall()

            # Overall regime verified stats
            overall = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins
                FROM signals
                WHERE regime=? AND verified=1
            """, (regime_key,)).fetchone()
            conn.close()

        boost = 0

        if recent:
            wins_recent = sum(1 for r in recent if r["outcome"] == "WIN")
            recent_wr   = wins_recent / len(recent) * 100
            if recent_wr >= 75:
                boost += 10
            elif recent_wr >= 60:
                boost += 5

        if overall and overall["total"] >= 5:
            overall_wr = (overall["wins"] or 0) / overall["total"] * 100
            if overall_wr >= 70:
                boost += 5
            elif overall_wr >= 55:
                boost += 2

        boost = min(boost, 15)

        # Cache the boost value
        if boost > 0:
            with _db_lock:
                conn = _get_conn()
                conn.execute(
                    "INSERT OR REPLACE INTO regime_confidence VALUES (?,?,?)",
                    (regime_key, boost, time.time())
                )
                conn.commit()
                conn.close()

        return boost

    except Exception as e:
        print(f"[signal_memory] boost error: {e}", flush=True)
        return 0


# ── AI Prompt Memory injection ─────────────────────────────────────────────────

def format_performance_for_prompt() -> str:
    """
    Returns a formatted block injected into AI prompts.
    Gives the LLM historical pattern context to improve signal quality.
    """
    try:
        a = get_analytics()
        if not a or a.get("total_signals", 0) < 3:
            return ""

        lines = [
            "=== AI SIGNAL MEMORY — HISTORICAL PERFORMANCE ===",
            f"Total signals tracked: {a['total_signals']} | "
            f"Win rate: {a['win_rate']}% | "
            f"Avg win move: +{a['avg_win_move']}% | "
            f"Profit factor: {a['profit_factor']}",
        ]

        best  = a.get("best_regime",  "—")
        worst = a.get("worst_regime", "—")
        if best != "—":
            lines.append(f"Best regime: {best} ({a.get('best_regime_wr', 0)}% win rate)")
        if worst != "—":
            lines.append(f"Worst regime: {worst} ({a.get('worst_regime_wr', 0)}% win rate)")

        # Per-regime performance (top 5)
        regime_bd = a.get("regime_breakdown", [])[:5]
        if regime_bd:
            lines.append("\nRegime historical accuracy:")
            for r in regime_bd:
                wr  = r.get("win_rate", 0)
                avg = r.get("avg_move", 0)
                tag = r.get("label") or r.get("regime", "")
                lines.append(f"  • {tag}: {wr}% win rate, avg move {avg:+.1f}%")

        # Asset accuracy
        top_asset    = a.get("top_asset",    "—")
        top_asset_mv = a.get("top_asset_avg_move", 0)
        if top_asset != "—":
            lines.append(f"\nTop asset: {top_asset} (avg +{top_asset_mv:.1f}% when correct)")

        lines.append(
            "\nIMPORTANT: Use this historical data to calibrate your confidence. "
            "If current regime historically underperforms, reduce conviction. "
            "If it historically outperforms, increase conviction.\n"
        )

        return "\n".join(lines)

    except Exception as e:
        print(f"[signal_memory] prompt format error: {e}", flush=True)
        return ""


# ── Logging ────────────────────────────────────────────────────────────────────

def log_signal(signal_result: dict) -> int | None:
    """
    Call after every signal generation.
    signal_result = full dict from _build_signal().
    """
    try:
        sig    = signal_result.get("signal") or {}
        regime = signal_result.get("regime") or {}

        decision   = str(sig.get("decision", "NO TRADE")).upper().strip()
        score      = float(sig.get("score", 0) or 0)
        session    = str(sig.get("session", "") or "")
        insights   = sig.get("insights", []) or []

        regime_key  = str(regime.get("regime", "") or "")
        regime_lbl  = str(regime.get("label", "") or "")
        regime_conf = int(regime.get("confidence", 0) or 0)

        # Top headline
        headline = ""
        try:
            from news import get_all_news
            news = get_all_news() or []
            if news:
                first = news[0]
                if isinstance(first, (list, tuple)) and len(first) == 2:
                    first = first[1]
                headline = str(first.get("headline") or first.get("title") or "")[:200]
        except Exception:
            pass

        # Affected assets
        bullish  = regime.get("bullish_assets", []) or []
        bearish  = regime.get("bearish_assets", []) or []
        all_assets = list({a for a in (bullish + bearish) if a})

        primary     = _pick_primary_asset(decision, regime, all_assets)
        entry_price = _get_current_price(primary)

        # Live macro prices at signal time (gold, dxy, nasdaq, oil, btc)
        live_prices = _capture_live_prices()

        # Self-learning confidence boost
        boost      = get_confidence_boost(regime_key)
        abs_score  = abs(score)
        confidence = min(95, int(40 + abs_score * 8 + boost)) if abs_score > 0 else min(45, 30 + boost)

        # Quality label (uses historical DB — must come after confidence calc)
        quality = compute_quality_label(regime_key, confidence, score)

        now_ts  = time.time()
        now_ist = datetime.now(IST).strftime("%d-%b-%Y %H:%M IST")

        with _db_lock:
            conn = _get_conn()
            cur  = conn.execute("""
                INSERT INTO signals
                  (ts, timestamp_ist, headline, regime, regime_label, regime_conf,
                   signal, score, confidence, session, quality_label,
                   insights, affected_assets, primary_asset, entry_price, live_prices)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                now_ts, now_ist, headline, regime_key, regime_lbl, regime_conf,
                decision, score, confidence, session, quality,
                json.dumps(insights[:5]),
                json.dumps(all_assets[:10]),
                primary, entry_price,
                json.dumps(live_prices),
            ))
            row_id = cur.lastrowid
            conn.commit()
            conn.close()

        print(
            f"[signal_memory] #{row_id}: {decision} | {quality} | "
            f"{regime_key} | {primary} @ {entry_price} | boost+{boost}",
            flush=True
        )

        # Also push to Redis/SQLite signal_store for fast retrieval
        try:
            from signal_store import push_signal
            push_signal({
                "id":               f"sig_{row_id}",
                "ts":               now_ts,
                "timestamp_ist":    now_ist,
                "headline":         headline,
                "regime":           regime_key,
                "regime_label":     regime_lbl,
                "confidence":       confidence,
                "quality_label":    quality,
                "signal":           decision,
                "score":            score,
                "affected_assets":  all_assets[:10],
                "primary_asset":    primary,
                "entry_price":      entry_price,
                "live_prices_at_signal": live_prices,
                "session":          session,
            })
        except Exception as _e:
            print(f"[signal_memory] store push error: {_e}", flush=True)

        # Invalidate analytics cache
        try:
            with _db_lock:
                conn = _get_conn()
                conn.execute("DELETE FROM analytics_cache WHERE key='main'")
                conn.commit()
                conn.close()
        except Exception:
            pass

        return row_id

    except Exception as e:
        print(f"[signal_memory] log error: {e}", flush=True)
        return None


def _capture_live_prices() -> dict:
    """Snapshot gold, dxy, nasdaq, oil, btc at signal time."""
    try:
        from live_prices import get_live_prices
        lp   = get_live_prices()
        snap = {}
        targets = {
            "gold":   [("commodities", "GOLD"),  ("commodities", "XAU")],
            "dxy":    [("fx", "DXY"),             ("fx", "USD")],
            "nasdaq": [("global", "NASDAQ"),      ("global", "NAS100")],
            "oil":    [("commodities", "CRUDE"),   ("commodities", "OIL")],
            "btc":    [("crypto", "BTC"),          ("crypto", "BITCOIN")],
        }
        for label, paths in targets.items():
            for cat, key in paths:
                val = lp.get(cat, {}).get(key, {}).get("price", 0)
                if val and float(val) > 0:
                    snap[label] = round(float(val), 4)
                    break
        return snap
    except Exception:
        return {}


def _pick_primary_asset(decision: str, regime: dict, all_assets: list) -> str:
    if "BUY" in decision:
        cands = regime.get("bullish_assets", []) or []
    elif "SELL" in decision:
        cands = regime.get("bearish_assets", []) or []
    else:
        cands = all_assets

    for c in cands:
        key = c.upper().replace(" ", "").replace("&", "").replace("500", "")
        if key in VERIFY_MAP or c.upper() in VERIFY_MAP:
            return c.upper()
    return "NIFTY50"


def _get_current_price(asset: str) -> float:
    try:
        from live_prices import get_live_prices
        lp   = get_live_prices()
        cats = [lp.get("indices", {}), lp.get("global", {}),
                lp.get("commodities", {}), lp.get("fx", {}),
                lp.get("crypto", {}), lp.get("bonds", {})]
        for cat in cats:
            for k, v in cat.items():
                if k.upper() == asset.upper():
                    p = v.get("price", 0)
                    if p and float(p) > 0:
                        return float(p)
    except Exception:
        pass
    try:
        sym = VERIFY_MAP.get(asset.upper())
        if sym:
            import yfinance as yf
            fi = yf.Ticker(sym).fast_info
            return float(fi.last_price)
    except Exception:
        pass
    return 0.0


# ── 24h Verification ──────────────────────────────────────────────────────────

def run_verification_pass():
    """Check all unverified signals older than 24h. Run hourly."""
    cutoff = time.time() - 86400
    try:
        with _db_lock:
            conn = _get_conn()
            rows = conn.execute("""
                SELECT id, ts, signal, primary_asset, entry_price
                FROM signals
                WHERE verified=0 AND ts < ?
                LIMIT 20
            """, (cutoff,)).fetchall()
            conn.close()

        for row in rows:
            _verify_one(row["id"], row["signal"], row["primary_asset"], row["entry_price"])

        # Recompute confidence boosts after each pass
        _refresh_all_regime_boosts()

    except Exception as e:
        print(f"[signal_memory] verify pass error: {e}", flush=True)


def _verify_one(row_id: int, signal: str, asset: str, entry_price: float):
    try:
        current = _get_current_price(asset)
        if not current or not entry_price or entry_price <= 0:
            return

        direction = SIGNAL_DIRECTION.get(signal.upper(), 0)
        pct_raw   = (current - entry_price) / entry_price * 100
        dir_move  = pct_raw * direction if direction != 0 else 0

        if direction == 0:
            outcome = "NEUTRAL"
            note    = f"No-trade signal; {asset} moved {pct_raw:+.2f}%"
        elif direction == 1:
            if pct_raw > 0.3:
                outcome, note = "WIN",     f"{asset} rose {pct_raw:+.2f}% after BUY"
            elif pct_raw < -0.3:
                outcome, note = "LOSS",    f"{asset} fell {pct_raw:+.2f}% after BUY"
            else:
                outcome, note = "NEUTRAL", f"{asset} barely moved ({pct_raw:+.2f}%)"
        else:
            if pct_raw < -0.3:
                outcome, note = "WIN",     f"{asset} fell {pct_raw:+.2f}% after SELL"
            elif pct_raw > 0.3:
                outcome, note = "LOSS",    f"{asset} rose {pct_raw:+.2f}% after SELL"
            else:
                outcome, note = "NEUTRAL", f"{asset} barely moved ({pct_raw:+.2f}%)"

        with _db_lock:
            conn = _get_conn()
            conn.execute("""
                UPDATE signals SET
                    verified=1, verify_ts=?, price_at_24h=?,
                    pct_move=?, outcome=?, outcome_note=?
                WHERE id=?
            """, (time.time(), current, dir_move, outcome, note, row_id))
            conn.commit()
            conn.close()

        print(f"[signal_memory] verified #{row_id}: {outcome} | {note}", flush=True)

    except Exception as e:
        print(f"[signal_memory] verify #{row_id} error: {e}", flush=True)


def _refresh_all_regime_boosts():
    """Recalculate and cache confidence boosts for all known regimes."""
    try:
        with _db_lock:
            conn = _get_conn()
            regimes = [r[0] for r in conn.execute(
                "SELECT DISTINCT regime FROM signals WHERE regime != ''"
            ).fetchall()]
            conn.close()
        for r in regimes:
            get_confidence_boost(r)
    except Exception:
        pass


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics() -> dict:
    """Full performance analytics. Cached 5 minutes."""
    try:
        with _db_lock:
            conn   = _get_conn()
            cached = conn.execute(
                "SELECT data, ts FROM analytics_cache WHERE key='main'"
            ).fetchone()
            conn.close()
        if cached and (time.time() - cached["ts"]) < 300:
            return json.loads(cached["data"])
    except Exception:
        pass

    result = _compute_analytics()

    try:
        with _db_lock:
            conn = _get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO analytics_cache VALUES (?,?,?)",
                ("main", json.dumps(result), time.time())
            )
            conn.commit()
            conn.close()
    except Exception:
        pass

    return result


def _compute_analytics() -> dict:
    try:
        with _db_lock:
            conn = _get_conn()

            total    = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            verified = conn.execute("SELECT COUNT(*) FROM signals WHERE verified=1").fetchone()[0]
            wins     = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='WIN'").fetchone()[0]
            losses   = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='LOSS'").fetchone()[0]
            neutral  = conn.execute("SELECT COUNT(*) FROM signals WHERE outcome='NEUTRAL'").fetchone()[0]
            pending  = total - verified

            decisive = wins + losses
            win_rate = round(wins / decisive * 100, 1) if decisive > 0 else 0.0

            avg_win_move  = conn.execute(
                "SELECT AVG(pct_move) FROM signals WHERE outcome='WIN'"
            ).fetchone()[0] or 0
            avg_loss_move = conn.execute(
                "SELECT AVG(pct_move) FROM signals WHERE outcome='LOSS'"
            ).fetchone()[0] or 0

            # Regime breakdown
            regime_rows = conn.execute("""
                SELECT regime, regime_label,
                       COUNT(*) as total,
                       SUM(CASE WHEN verified=1 THEN 1 ELSE 0 END) as total_verified,
                       SUM(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
                       AVG(CASE WHEN verified=1 THEN pct_move END) as avg_move
                FROM signals WHERE regime != ''
                GROUP BY regime
                ORDER BY wins DESC
            """).fetchall()

            regime_breakdown  = []
            best_regime       = "—"; best_regime_wr  = 0; best_regime_lbl  = "—"
            worst_regime      = "—"; worst_regime_wr = 100; worst_regime_lbl = "—"

            for r in regime_rows:
                d_count  = (r["wins"] or 0) + (r["losses"] or 0)
                wr       = round((r["wins"] or 0) / d_count * 100, 1) if d_count > 0 else 0
                avg_mv   = round(r["avg_move"] or 0, 2)
                lbl      = r["regime_label"] or r["regime"]
                regime_breakdown.append({
                    "regime":         r["regime"],
                    "label":          lbl,
                    "total":          r["total"],
                    "total_verified": r["total_verified"] or 0,
                    "wins":           r["wins"] or 0,
                    "losses":         r["losses"] or 0,
                    "win_rate":       wr,
                    "avg_move":       avg_mv,
                })
                if wr > best_regime_wr and d_count >= 2:
                    best_regime_wr  = wr
                    best_regime     = r["regime"]
                    best_regime_lbl = lbl
                if wr < worst_regime_wr and d_count >= 2:
                    worst_regime_wr  = wr
                    worst_regime     = r["regime"]
                    worst_regime_lbl = lbl

            # Signal type breakdown
            sig_rows = conn.execute("""
                SELECT signal,
                       COUNT(*) as total,
                       SUM(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
                       AVG(CASE WHEN verified=1 THEN pct_move END) as avg_move
                FROM signals GROUP BY signal
            """).fetchall()
            signal_breakdown = []
            for r in sig_rows:
                d_c = (r["wins"] or 0) + (r["losses"] or 0)
                wr  = round((r["wins"] or 0) / d_c * 100, 1) if d_c > 0 else 0
                signal_breakdown.append({
                    "signal":   r["signal"],
                    "total":    r["total"],
                    "wins":     r["wins"] or 0,
                    "losses":   r["losses"] or 0,
                    "win_rate": wr,
                    "avg_move": round(r["avg_move"] or 0, 2),
                })

            # Session breakdown
            sess_rows = conn.execute("""
                SELECT session,
                       COUNT(*) as total,
                       SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins
                FROM signals WHERE session != '' GROUP BY session
            """).fetchall()
            session_breakdown = [
                {"session": r["session"], "total": r["total"], "wins": r["wins"] or 0}
                for r in sess_rows
            ]

            # Asset performance — parse primary_asset from verified signals
            asset_rows = conn.execute("""
                SELECT primary_asset,
                       COUNT(*) as total,
                       SUM(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END) as wins,
                       AVG(CASE WHEN outcome='WIN'  THEN pct_move END) as avg_win_move
                FROM signals WHERE verified=1 AND primary_asset != ''
                GROUP BY primary_asset
                ORDER BY wins DESC
            """).fetchall()
            asset_breakdown = []
            top_asset = "—"; top_asset_avg_move = 0
            for r in asset_rows:
                d_c = r["wins"] or 0
                asset_breakdown.append({
                    "asset":        r["primary_asset"],
                    "total":        r["total"],
                    "wins":         r["wins"] or 0,
                    "avg_win_move": round(r["avg_win_move"] or 0, 2),
                })
                if (r["avg_win_move"] or 0) > top_asset_avg_move and d_c >= 1:
                    top_asset          = r["primary_asset"]
                    top_asset_avg_move = round(r["avg_win_move"] or 0, 2)

            # Quality label breakdown
            quality_rows = conn.execute("""
                SELECT quality_label, COUNT(*) as total,
                       SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins
                FROM signals WHERE quality_label IS NOT NULL
                GROUP BY quality_label
            """).fetchall()
            quality_breakdown = []
            for r in quality_rows:
                d_c = r["total"] or 1
                quality_breakdown.append({
                    "label":    r["quality_label"],
                    "total":    r["total"],
                    "wins":     r["wins"] or 0,
                    "win_rate": round((r["wins"] or 0) / d_c * 100, 1),
                })

            # Recent signals (last 30)
            recent_rows = conn.execute("""
                SELECT id, timestamp_ist, headline, regime_label, signal, confidence,
                       quality_label, primary_asset, entry_price, outcome, pct_move,
                       outcome_note, verified, live_prices
                FROM signals
                ORDER BY ts DESC LIMIT 30
            """).fetchall()
            recent = []
            for r in recent_rows:
                lp_snap = {}
                try:
                    lp_snap = json.loads(r["live_prices"] or "{}")
                except Exception:
                    pass
                recent.append({
                    "id":           r["id"],
                    "time":         r["timestamp_ist"],
                    "headline":     (r["headline"] or "")[:80],
                    "regime":       r["regime_label"] or "—",
                    "signal":       r["signal"],
                    "confidence":   r["confidence"],
                    "quality":      r["quality_label"] or "MODERATE",
                    "asset":        r["primary_asset"],
                    "entry_price":  r["entry_price"],
                    "outcome":      r["outcome"],
                    "pct_move":     round(r["pct_move"] or 0, 2),
                    "outcome_note": r["outcome_note"] or "",
                    "verified":     bool(r["verified"]),
                    "live_prices":  lp_snap,
                })

            conn.close()

        pf = round(abs(avg_win_move / avg_loss_move), 2) if avg_loss_move and avg_loss_move != 0 else "—"
        if worst_regime_wr == 100:
            worst_regime = "—"; worst_regime_wr = 0; worst_regime_lbl = "—"

        return {
            "total_signals":     total,
            "verified":          verified,
            "pending":           pending,
            "wins":              wins,
            "losses":            losses,
            "neutral":           neutral,
            "win_rate":          win_rate,
            "avg_win_move":      round(avg_win_move, 2),
            "avg_loss_move":     round(avg_loss_move, 2),
            "profit_factor":     pf,
            "best_regime":       best_regime_lbl,
            "best_regime_key":   best_regime,
            "best_regime_wr":    best_regime_wr,
            "worst_regime":      worst_regime_lbl,
            "worst_regime_key":  worst_regime,
            "worst_regime_wr":   worst_regime_wr,
            "top_asset":         top_asset,
            "top_asset_avg_move":top_asset_avg_move,
            "regime_breakdown":  regime_breakdown,
            "signal_breakdown":  signal_breakdown,
            "session_breakdown": session_breakdown,
            "asset_breakdown":   asset_breakdown,
            "quality_breakdown": quality_breakdown,
            "recent_signals":    recent,
            "generated_at":      datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        }

    except Exception as e:
        print(f"[signal_memory] analytics error: {e}", flush=True)
        return {
            "total_signals": 0, "verified": 0, "pending": 0,
            "wins": 0, "losses": 0, "neutral": 0, "win_rate": 0.0,
            "avg_win_move": 0, "avg_loss_move": 0, "profit_factor": "—",
            "best_regime": "—", "best_regime_key": "", "best_regime_wr": 0,
            "worst_regime": "—", "worst_regime_key": "", "worst_regime_wr": 0,
            "top_asset": "—", "top_asset_avg_move": 0,
            "regime_breakdown": [], "signal_breakdown": [],
            "session_breakdown": [], "asset_breakdown": [],
            "quality_breakdown": [], "recent_signals": [],
            "generated_at": datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        }


def get_history(limit: int = 30, offset: int = 0) -> list:
    """Paginated raw signal rows, newest first."""
    try:
        with _db_lock:
            conn = _get_conn()
            rows = conn.execute("""
                SELECT id, timestamp_ist, headline, regime, regime_label, regime_conf,
                       signal, score, confidence, session, quality_label, affected_assets,
                       primary_asset, entry_price, verified,
                       price_at_24h, pct_move, outcome, outcome_note, live_prices
                FROM signals
                ORDER BY ts DESC LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
            conn.close()
        result = []
        for r in rows:
            lp_snap = {}
            try:
                lp_snap = json.loads(r["live_prices"] or "{}")
            except Exception:
                pass
            result.append({
                "id":            r["id"],
                "time":          r["timestamp_ist"],
                "headline":      (r["headline"] or "")[:100],
                "regime":        r["regime"] or "",
                "regime_label":  r["regime_label"] or "—",
                "regime_conf":   r["regime_conf"] or 0,
                "signal":        r["signal"],
                "score":         round(r["score"] or 0, 2),
                "confidence":    r["confidence"] or 0,
                "quality":       r["quality_label"] or "MODERATE",
                "session":       r["session"] or "—",
                "assets":        json.loads(r["affected_assets"] or "[]"),
                "primary_asset": r["primary_asset"] or "—",
                "entry_price":   round(r["entry_price"] or 0, 2),
                "verified":      bool(r["verified"]),
                "price_at_24h":  round(r["price_at_24h"] or 0, 2),
                "pct_move":      round(r["pct_move"] or 0, 2),
                "outcome":       r["outcome"] or "PENDING",
                "outcome_note":  r["outcome_note"] or "",
                "live_prices":   lp_snap,
            })
        return result
    except Exception as e:
        print(f"[signal_memory] get_history error: {e}", flush=True)
        return []


def get_regime_performance() -> dict:
    """Returns per-regime performance dict for confidence boost display."""
    try:
        a = get_analytics()
        result = {}
        for r in a.get("regime_breakdown", []):
            result[r["regime"]] = {
                "label":    r["label"],
                "win_rate": r["win_rate"],
                "total":    r["total_verified"],
                "avg_move": r["avg_move"],
                "boost":    get_confidence_boost(r["regime"]),
            }
        return result
    except Exception:
        return {}


# Init DB on import
init_db()
