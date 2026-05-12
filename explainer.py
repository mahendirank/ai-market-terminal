"""
explainer.py — "Why Did It Move?" institutional explainer engine.

For each tracked asset, detects strong intraday moves and generates a
structured institutional desk commentary (6 sections):

  1. What moved        — asset + magnitude in one line
  2. Why it moved      — 2-3 sentences linking to macro drivers
  3. Supporting evidence — bullet citations from LIVE data
  4. Historical similarity — one short analogue
  5. Risk to thesis    — what would invalidate
  6. Forward implication — second-order effects & coming days

Always cites only live data. Confidence is computed programmatically from
signal alignment. Last 100 explanations persist in SQLite.

Tracked assets and move thresholds:
  - Gold       0.7%
  - DXY        0.3%
  - EUR/USD    0.5%
  - USD/JPY    0.5%
  - NASDAQ     1.0%
  - Oil        1.5%
  - BTC        2.0%

Vocabulary the LLM is steered toward:
  carry unwind, dollar liquidity, duration bid, real yield compression,
  safe haven demand, inflation repricing, growth scare, risk rotation
"""
import os
import json
import time
import sqlite3
import threading
import requests
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Tracked assets + thresholds ──────────────────────────────────────────────

ASSETS = [
    {"key": "GOLD",      "display": "Gold",      "threshold": 0.7, "live_path": ("commodities", "GOLD"),  "kind": "metal"},
    {"key": "DXY",       "display": "DXY",       "threshold": 0.3, "live_path": ("fx", "DXY"),            "kind": "fx"},
    {"key": "EURUSD",    "display": "EUR/USD",   "threshold": 0.5, "live_path": None,                     "kind": "fx_pair", "fx_pair": "EUR/USD"},
    {"key": "USDJPY",    "display": "USD/JPY",   "threshold": 0.5, "live_path": None,                     "kind": "fx_pair", "fx_pair": "USD/JPY"},
    {"key": "NASDAQ",    "display": "NASDAQ",    "threshold": 1.0, "live_path": ("global", "NASDAQ"),     "kind": "equity"},
    {"key": "OIL",       "display": "Crude Oil", "threshold": 1.5, "live_path": ("commodities", "CRUDE"), "kind": "commodity"},
    {"key": "BTC",       "display": "Bitcoin",   "threshold": 2.0, "live_path": ("crypto", "BTC"),        "kind": "crypto"},
]

# ─── Groq config ──────────────────────────────────────────────────────────────

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

# ─── SQLite store ─────────────────────────────────────────────────────────────

_DB_DIR  = os.path.join(os.path.dirname(__file__), "db")
os.makedirs(_DB_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DB_DIR, "explainer.db")
_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS explanations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ist          TEXT NOT NULL,
                asset_key       TEXT NOT NULL,
                asset_display   TEXT NOT NULL,
                price           REAL,
                change_pct      REAL,
                direction       TEXT,
                signature       TEXT NOT NULL,
                confidence      INTEGER,
                what_moved      TEXT,
                why_it_moved    TEXT,
                evidence        TEXT,             -- JSON list of bullet strings
                historical      TEXT,
                risk_to_thesis  TEXT,
                forward_implic  TEXT,
                tags            TEXT,             -- JSON list of institutional terms
                context_snapshot TEXT              -- JSON of macro context used
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_explainer_sig ON explanations(signature)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_explainer_ts  ON explanations(ts_ist)")
        c.commit()

_init_db()


# ─── Live data gather ─────────────────────────────────────────────────────────

def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
        if cur is None: return default
    return cur


def _get_asset_state(asset_def: dict, lp: dict, fx: dict) -> dict:
    """Return {price, change_pct} for the asset, regardless of source."""
    kind = asset_def["kind"]
    if kind == "fx_pair":
        pair = asset_def["fx_pair"]
        p = fx.get(pair, {})
        return {"price": float(p.get("price") or 0), "change_pct": float(p.get("change_pct") or 0)}
    cat, key = asset_def["live_path"]
    v = lp.get(cat, {}).get(key, {})
    price = float((v or {}).get("price") or 0)
    chg = float((v or {}).get("change") or 0)
    return {"price": price, "change_pct": chg}


def _gather_macro_context() -> dict:
    """Pull everything needed: prices, regime, fx, news, CB calendar."""
    ctx = {"lp": {}, "fx": {}, "regime": {}, "fx_intel": {}, "cb": [], "news": []}
    try:
        from live_prices import get_live_prices
        ctx["lp"] = get_live_prices() or {}
    except Exception as e:
        print(f"[explainer] live_prices: {e}", flush=True)
    try:
        from forex import get_forex_intel
        fxi = get_forex_intel() or {}
        ctx["fx_intel"] = fxi
        ctx["fx"] = fxi.get("pairs", {})
    except Exception as e:
        print(f"[explainer] forex: {e}", flush=True)
    try:
        from macro_desk import get_macro_regime_view
        ctx["regime"] = get_macro_regime_view() or {}
    except Exception as e:
        print(f"[explainer] regime: {e}", flush=True)
    try:
        from cb_calendar import get_cb_calendar
        cc = get_cb_calendar(days_ahead=30, limit=6) or {}
        ctx["cb"] = cc.get("events", [])
    except Exception as e:
        print(f"[explainer] cb: {e}", flush=True)
    try:
        from news import get_all_news
        items = (get_all_news() or [])[:30]
        ctx["news"] = [(it.get("text") or it.get("title", ""))[:200] for it in items if it]
    except Exception as e:
        print(f"[explainer] news: {e}", flush=True)
    return ctx


# ─── Programmatic confidence — how aligned are the signals with the move? ─────

def _compute_signal_alignment(asset_key: str, direction: str, ctx: dict) -> tuple:
    """Returns (confidence_int, supporting_signals_list)."""
    lp = ctx.get("lp", {})
    regime = ctx.get("regime", {})
    dims = regime.get("dimensions", {})

    dxy_chg = float(_safe(lp, "fx", "DXY", "change", default=0) or 0)
    y10c    = float(_safe(lp, "bonds", "US_10Y", "change", default=0) or 0)
    vix     = float(_safe(lp, "vix", "VIX", "price", default=20) or 20)
    gold_c  = float(_safe(lp, "commodities", "GOLD", "change", default=0) or 0)
    oil_c   = float(_safe(lp, "commodities", "CRUDE", "change", default=0) or 0)
    ndx_c   = float(_safe(lp, "global", "NASDAQ", "change", default=0) or 0)
    btc_c   = float(_safe(lp, "crypto", "BTC", "change", default=0) or 0)

    supports = []
    is_up = direction == "UP"

    def _add(condition, label):
        if condition: supports.append(label)

    if asset_key == "GOLD":
        _add(is_up == (dxy_chg < 0),    "DXY direction supports gold")
        _add(is_up == (y10c   < 0),    "US 10Y yield direction supports real-yield read")
        _add(is_up == (vix    > 22),   "VIX risk signal supports safe-haven bid")
        _add(dims.get("inflation", {}).get("state") == "HOT" if is_up else False, "Inflation regime HOT")
        _add(dims.get("dollar", {}).get("state") == "WEAK" if is_up else dims.get("dollar", {}).get("state") == "STRONG",
             "Dollar regime alignment")
    elif asset_key == "DXY":
        _add(is_up == (y10c   > 0),    "US yield direction supports DXY")
        _add(is_up == (vix    > 22),   "Risk-off supports USD haven flow" if is_up else "Risk-on weighs on USD")
        _add(dims.get("fed", {}).get("state") == ("HAWKISH" if is_up else "DOVISH"), "Fed bias aligned")
    elif asset_key == "EURUSD":
        _add(is_up == (dxy_chg < 0),   "DXY direction supports EUR/USD")
        _add(is_up == (y10c    < 0),   "US yield decline supportive of EUR/USD")
        _add(dims.get("dollar", {}).get("state") == ("WEAK" if is_up else "STRONG"), "Dollar regime aligned")
    elif asset_key == "USDJPY":
        _add(is_up == (y10c   > 0),    "US yields rising supports USD/JPY carry")
        _add(is_up == (vix    < 18),   "VIX low supports carry trade")
        _add(is_up == (dims.get("risk", {}).get("state") == "ON"), "Risk-on supports JPY weakness")
    elif asset_key == "NASDAQ":
        _add(is_up == (y10c   < 0),    "Yields falling supports duration/tech")
        _add(is_up == (vix    < 18),   "VIX subdued supports risk")
        _add(is_up == (dims.get("risk", {}).get("state") == "ON"), "Risk regime aligned")
    elif asset_key == "OIL":
        _add(is_up == (dxy_chg < 0),   "DXY weakness supports oil")
        _add(dims.get("commodities", {}).get("state") == ("BULL" if is_up else "BEAR"), "Commodities regime aligned")
        _add(dims.get("inflation", {}).get("state") == "HOT" if is_up else False, "Inflation regime HOT supportive")
    elif asset_key == "BTC":
        _add(is_up == (vix    < 18),   "VIX low — risk appetite supportive")
        _add(is_up == (dxy_chg < 0),   "DXY weakness supports BTC")
        _add(is_up == (ndx_c  > 0),    "NDX correlation supportive")
        _add(is_up == (dims.get("risk", {}).get("state") == "ON"), "Risk-on aligned")

    n = len(supports)
    if n >= 4: conf = 88
    elif n == 3: conf = 78
    elif n == 2: conf = 68
    elif n == 1: conf = 58
    else: conf = 48
    return conf, supports


# ─── Dedup signature ──────────────────────────────────────────────────────────

def _signature(asset_key: str, change_pct: float) -> str:
    """Same asset + direction + magnitude bucket on the same day → same sig."""
    direction = "UP" if change_pct > 0 else "DN"
    today = datetime.now(IST).strftime("%Y-%m-%d")
    bucket = round(abs(change_pct) / 0.5) * 0.5  # 0.5%, 1.0%, 1.5% buckets
    return f"{asset_key}:{today}:{direction}:{bucket:.1f}"


def already_explained(sig: str) -> bool:
    try:
        with _db_lock, _conn() as c:
            row = c.execute("SELECT 1 FROM explanations WHERE signature = ? LIMIT 1", (sig,)).fetchone()
        return row is not None
    except Exception:
        return False


# ─── LLM call ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior macro strategist writing institutional desk commentary in real time. Your output is JSON ONLY — no preamble, no markdown fences.

When given a strong asset move and current macro context, produce a structured explanation as a single JSON object with these exact keys:

{
  "what_moved":        "one sentence stating the asset and the magnitude with specific numbers",
  "why_it_moved":      "2-3 sentences linking the move to specific macro drivers cited in the live snapshot",
  "evidence":          ["bullet 1 citing live data", "bullet 2", "bullet 3"],
  "historical":        "one short analogue if applicable, e.g. 'similar to the late-2024 carry unwind after BoJ surprise hike'",
  "risk_to_thesis":    "what data point or event would invalidate this thesis",
  "forward_implication": "what the move suggests for related assets and the coming session/week",
  "tags":              ["institutional term 1", "institutional term 2"]
}

VOCABULARY to use (institutional, not retail):
  carry unwind, dollar liquidity, duration bid, real yield compression,
  safe haven demand, inflation repricing, growth scare, risk rotation,
  steepening, term-premium, bid-cover, basis squeeze, cross-asset spillover,
  positioning unwind, vol regime shift, equity-bond correlation flip

RULES:
- Cite ONLY data points present in the LIVE SNAPSHOT given to you. Do not fabricate numbers.
- Tags must use institutional vocabulary (pick 2-4 from the list above or similar).
- No retail vocabulary ("moon", "rip", "pump").
- No emoji.
- Output must be valid JSON parseable by json.loads().
- If a section doesn't apply, write a brief note rather than inventing content.
"""


def _format_context_for_llm(asset_def: dict, move: dict, ctx: dict) -> str:
    lp = ctx.get("lp", {})
    regime = ctx.get("regime", {})
    fx = ctx.get("fx", {})
    cb = ctx.get("cb", [])
    news = ctx.get("news", [])

    lines = [f"=== STRONG MOVE DETECTED ==="]
    lines.append(f"Asset: {asset_def['display']} ({asset_def['key']})")
    lines.append(f"Price: {move['price']:.4f}  |  Change: {move['change_pct']:+.2f}%  |  Direction: {'UP' if move['change_pct'] > 0 else 'DOWN'}")
    lines.append(f"Move threshold for this asset: {asset_def['threshold']}%")

    lines.append("\n=== LIVE MACRO SNAPSHOT (cite these numbers only) ===")
    def chg(cat, key):
        return _safe(lp, cat, key, "change", default=None)
    def lvl(cat, key):
        return _safe(lp, cat, key, "price", default=None)
    lines.append(f"DXY: {lvl('fx','DXY')} ({_pct(chg('fx','DXY'))})  | US 10Y: {lvl('bonds','US_10Y')}% ({_pct(chg('bonds','US_10Y'))})  | VIX: {lvl('vix','VIX')}")
    lines.append(f"Gold: {lvl('commodities','GOLD')} ({_pct(chg('commodities','GOLD'))})  | Oil: {lvl('commodities','CRUDE')} ({_pct(chg('commodities','CRUDE'))})")
    lines.append(f"NASDAQ: {_pct(chg('global','NASDAQ'))}  | SPX: {_pct(chg('global','SPX'))}  | BTC: {lvl('crypto','BTC')} ({_pct(chg('crypto','BTC'))})")

    if regime:
        lines.append(f"\n=== REGIME ===")
        lines.append(f"Commentary: {regime.get('commentary', '—')}")
        lines.append(f"Dominant driver: {regime.get('dominant_driver', '—')}")
        for name in ("risk", "dollar", "fed", "yields", "inflation", "commodities"):
            dim = (regime.get("dimensions") or {}).get(name)
            if dim:
                lines.append(f"  {name.upper():12} {dim['state']} ({dim['confidence']}%) — {dim['driver']}")

    if fx:
        lines.append("\n=== FX MAJORS ===")
        for pair, p in fx.items():
            lines.append(f"  {pair}: {p.get('price')} {_pct(p.get('change_pct'))}  → {p.get('direction')} {p.get('confidence')}% ({p.get('driver')})")

    if cb:
        lines.append("\n=== UPCOMING CB MEETINGS (next 30d) ===")
        for e in cb[:5]:
            lines.append(f"  {e['cb']} {e['date_display']} (in {e['days_to_event']}d): {e['label']} | VOL {e['volatility']} | Bias: {e['expected_bias']}")

    if news:
        lines.append("\n=== RECENT NEWS HEADLINES (top 15) ===")
        for h in news[:15]:
            if h: lines.append(f"  - {h}")

    return "\n".join(lines)


def _pct(v):
    if v is None: return "—"
    try: return f"{float(v):+.2f}%"
    except: return str(v)


def _call_groq_json(messages: list, max_tokens: int = 700, temp: float = 0.25):
    if not GROQ_API_KEY:
        return None
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temp,
                "response_format": {"type": "json_object"},
            },
            timeout=45,
        )
        if resp.status_code != 200:
            print(f"[explainer] groq {resp.status_code}: {resp.text[:200]}", flush=True)
            return None
        text = resp.json()["choices"][0]["message"]["content"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON if wrapped
            import re
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try: return json.loads(m.group(0))
                except: pass
            return None
    except Exception as e:
        print(f"[explainer] groq error: {type(e).__name__}: {e}", flush=True)
        return None


# ─── Generate one explanation ─────────────────────────────────────────────────

def generate_explanation_for(asset_def: dict, force: bool = False) -> dict | None:
    """Generate (and persist) one explanation if asset has a strong move.
    Returns the explanation dict or None if no move / already explained / failed."""
    ctx = _gather_macro_context()
    move = _get_asset_state(asset_def, ctx["lp"], ctx["fx"])
    if abs(move["change_pct"]) < asset_def["threshold"] and not force:
        return None

    sig = _signature(asset_def["key"], move["change_pct"])
    if already_explained(sig) and not force:
        return None

    direction = "UP" if move["change_pct"] > 0 else "DOWN"
    confidence, supports = _compute_signal_alignment(asset_def["key"], direction, ctx)

    context_block = _format_context_for_llm(asset_def, move, ctx)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": context_block + "\n\nWrite the institutional desk explanation now, as JSON only."},
    ]
    parsed = _call_groq_json(messages, max_tokens=700)
    if not parsed:
        return None

    expl = {
        "ts_ist":          datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        "asset_key":       asset_def["key"],
        "asset_display":   asset_def["display"],
        "price":           round(move["price"], 4),
        "change_pct":      round(move["change_pct"], 2),
        "direction":       direction,
        "signature":       sig,
        "confidence":      confidence,
        "what_moved":      str(parsed.get("what_moved", "")).strip()[:500],
        "why_it_moved":    str(parsed.get("why_it_moved", "")).strip()[:1500],
        "evidence":        parsed.get("evidence", []) if isinstance(parsed.get("evidence"), list) else [str(parsed.get("evidence", ""))[:300]],
        "historical":      str(parsed.get("historical", "")).strip()[:500],
        "risk_to_thesis":  str(parsed.get("risk_to_thesis", "")).strip()[:500],
        "forward_implic":  str(parsed.get("forward_implication", "")).strip()[:800],
        "tags":            parsed.get("tags", []) if isinstance(parsed.get("tags"), list) else [],
        "alignment_signals": supports,
    }
    _save_explanation(expl, ctx)
    return expl


def _save_explanation(e: dict, ctx: dict) -> None:
    try:
        ctx_brief = {
            "regime_commentary": (ctx.get("regime") or {}).get("commentary"),
            "dominant_driver":   (ctx.get("regime") or {}).get("dominant_driver"),
            "alignment_signals": e.get("alignment_signals", []),
        }
        with _db_lock, _conn() as c:
            c.execute("""
                INSERT INTO explanations (
                    ts_ist, asset_key, asset_display, price, change_pct, direction,
                    signature, confidence,
                    what_moved, why_it_moved, evidence, historical, risk_to_thesis, forward_implic, tags,
                    context_snapshot
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                e["ts_ist"], e["asset_key"], e["asset_display"], e["price"], e["change_pct"], e["direction"],
                e["signature"], e["confidence"],
                e["what_moved"], e["why_it_moved"], json.dumps(e["evidence"]),
                e["historical"], e["risk_to_thesis"], e["forward_implic"], json.dumps(e["tags"]),
                json.dumps(ctx_brief)
            ))
            # Rotate at 100 rows
            c.execute("""
                DELETE FROM explanations WHERE id NOT IN (
                  SELECT id FROM explanations ORDER BY id DESC LIMIT 100
                )
            """)
            c.commit()
    except Exception as ex:
        print(f"[explainer] save error: {ex}", flush=True)


# ─── Background scanner ───────────────────────────────────────────────────────

def scan_and_explain(max_new: int = 4) -> dict:
    """Scan all 7 assets, generate explanations for any unexplained strong moves.
    Returns summary {generated: N, skipped: M, errors: K}."""
    summary = {"generated": [], "skipped": 0, "errors": 0, "scanned": []}
    for asset_def in ASSETS:
        try:
            if len(summary["generated"]) >= max_new:
                summary["skipped"] += 1
                continue
            res = generate_explanation_for(asset_def)
            summary["scanned"].append({
                "asset": asset_def["key"],
                "generated": res is not None,
            })
            if res:
                summary["generated"].append(asset_def["key"])
            else:
                summary["skipped"] += 1
        except Exception as e:
            print(f"[explainer] scan error for {asset_def['key']}: {e}", flush=True)
            summary["errors"] += 1
    return summary


# ─── Public readers ───────────────────────────────────────────────────────────

def get_recent_explanations(limit: int = 30, asset: str | None = None) -> list:
    """Newest-first list of recent explanations."""
    try:
        with _db_lock, _conn() as c:
            if asset:
                rows = c.execute("""
                    SELECT * FROM explanations WHERE asset_key = ?
                    ORDER BY id DESC LIMIT ?
                """, (asset.upper(), limit)).fetchall()
            else:
                rows = c.execute("""
                    SELECT * FROM explanations
                    ORDER BY id DESC LIMIT ?
                """, (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try: d["evidence"] = json.loads(d.get("evidence") or "[]")
            except: d["evidence"] = []
            try: d["tags"] = json.loads(d.get("tags") or "[]")
            except: d["tags"] = []
            try: d["context_snapshot"] = json.loads(d.get("context_snapshot") or "{}")
            except: d["context_snapshot"] = {}
            out.append(d)
        return out
    except Exception as e:
        print(f"[explainer] get_recent error: {e}", flush=True)
        return []


def get_tracked_assets() -> list:
    return [{"key": a["key"], "display": a["display"], "threshold": a["threshold"]} for a in ASSETS]
