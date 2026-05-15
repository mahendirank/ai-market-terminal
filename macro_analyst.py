"""
macro_analyst.py — AI Macro Analyst chat panel.

A senior macro strategist persona that answers questions about:
  - why assets move
  - macro relationships (yield differentials, dollar liquidity, etc.)
  - forex pair outlooks
  - gold outlook
  - risk regime analysis
  - central bank impact
  - correlation analysis

Grounded on LIVE data at every query:
  - live_prices.get_live_prices()  → DXY, US10Y, VIX, NASDAQ, Gold, Oil, BTC
  - macro_desk.get_macro_regime_view()  → 6 regime dimensions + commentary
  - forex.get_forex_intel()          → 6 FX majors with direction inference
  - cb_calendar.get_cb_calendar()    → next central bank meetings
  - regime.detect_market_regime()    → 10-state regime classifier
  - news.get_all_news()               → 80 most recent headlines (cached)

Conversation memory:
  - Primary: Redis (if REDIS_URL env var set), keyed by session_id
  - Fallback: SQLite at /app/db/macro_analyst.db
  - Last 20 message exchanges per session are persisted
"""
import os
import json
import time
import sqlite3
import threading
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Groq config (matches existing ai_layer pattern) ─────────────────────────

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = "llama-3.3-70b-versatile"   # smarter than 8b-instant for analysis
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

# ─── Redis / SQLite history storage ──────────────────────────────────────────

_redis_client = None
_redis_ok     = False


def _init_redis():
    global _redis_client, _redis_ok
    url = os.environ.get("REDIS_URL", "")
    if not url:
        print("[macro_analyst] REDIS_URL not set — SQLite history only", flush=True)
        return
    try:
        import redis
        c = redis.from_url(url, socket_connect_timeout=4, socket_timeout=4, decode_responses=True)
        c.ping()
        _redis_client, _redis_ok = c, True
        print(f"[macro_analyst] Redis history connected: {url[:30]}...", flush=True)
    except Exception as e:
        _redis_ok = False
        print(f"[macro_analyst] Redis unavailable ({e}) — SQLite fallback", flush=True)

_init_redis()

_DB_DIR  = os.path.join(os.path.dirname(__file__), "db")
os.makedirs(_DB_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DB_DIR, "macro_analyst.db")
_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                role         TEXT NOT NULL,    -- 'user' or 'assistant'
                content      TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                context_used TEXT              -- JSON snapshot of macro context
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_session_id ON chat_history(session_id, id)")
        c.commit()

_init_db()


def _store_message(session_id: str, role: str, content: str, context: dict | None = None) -> None:
    ts = datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S IST")
    ctx_json = json.dumps(context, default=str)[:8000] if context else None

    # Try Redis first
    if _redis_ok and _redis_client:
        try:
            key = f"chat:{session_id}"
            entry = json.dumps({"role": role, "content": content, "ts": ts})
            _redis_client.rpush(key, entry)
            _redis_client.ltrim(key, -40, -1)  # keep last 40 messages (20 exchanges)
            _redis_client.expire(key, 86400 * 30)  # 30-day TTL
            return
        except Exception as e:
            print(f"[macro_analyst] redis store error: {e}", flush=True)

    # SQLite fallback
    try:
        with _db_lock, _conn() as c:
            c.execute("""
                INSERT INTO chat_history (session_id, role, content, created_at, context_used)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, role, content, ts, ctx_json))
            # Keep last 40 rows per session
            c.execute("""
                DELETE FROM chat_history WHERE id NOT IN (
                  SELECT id FROM chat_history WHERE session_id=? ORDER BY id DESC LIMIT 40
                ) AND session_id=?
            """, (session_id, session_id))
            c.commit()
    except Exception as e:
        print(f"[macro_analyst] sqlite store error: {e}", flush=True)


def get_chat_history(session_id: str, limit: int = 20) -> list:
    """Return chronological list of {role, content, ts}."""
    if _redis_ok and _redis_client:
        try:
            key = f"chat:{session_id}"
            entries = _redis_client.lrange(key, -(limit * 2), -1)
            return [json.loads(e) for e in entries]
        except Exception:
            pass
    try:
        with _db_lock, _conn() as c:
            rows = c.execute("""
                SELECT role, content, created_at FROM chat_history
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            """, (session_id, limit * 2)).fetchall()
        return [{"role": r["role"], "content": r["content"], "ts": r["created_at"]} for r in reversed(rows)]
    except Exception:
        return []


def clear_chat_history(session_id: str) -> bool:
    if _redis_ok and _redis_client:
        try: _redis_client.delete(f"chat:{session_id}")
        except: pass
    try:
        with _db_lock, _conn() as c:
            c.execute("DELETE FROM chat_history WHERE session_id = ?", (session_id,))
            c.commit()
        return True
    except Exception:
        return False


# ─── Macro context builder — what the LLM sees with every query ───────────────

def _build_context_snapshot() -> dict:
    """Pull live snapshot of everything the analyst needs to ground its answer."""
    ctx = {"macro": {}, "regime": {}, "fx": {}, "cb_calendar": [], "news_top": []}
    try:
        from live_prices import get_live_prices
        lp = get_live_prices() or {}
        ctx["macro"] = {
            "dxy_price":   _safe(lp, "fx", "DXY", "price"),
            "dxy_change":  _safe(lp, "fx", "DXY", "change"),
            "us10y_yield": _safe(lp, "bonds", "US_10Y", "price"),
            "us10y_chg":   _safe(lp, "bonds", "US_10Y", "change"),
            "vix":         _safe(lp, "vix", "VIX", "price"),
            "nasdaq_chg":  _safe(lp, "global", "NASDAQ", "change"),
            "spx_chg":     _safe(lp, "global", "SPX", "change"),
            "gold_price":  _safe(lp, "commodities", "GOLD", "price"),
            "gold_chg":    _safe(lp, "commodities", "GOLD", "change"),
            "oil_price":   _safe(lp, "commodities", "CRUDE", "price"),
            "oil_chg":     _safe(lp, "commodities", "CRUDE", "change"),
            "btc_price":   _safe(lp, "crypto", "BTC", "price"),
            "btc_chg":     _safe(lp, "crypto", "BTC", "change"),
        }
    except Exception as e:
        print(f"[macro_analyst] live_prices: {e}", flush=True)
    try:
        from macro_desk import get_macro_regime_view
        view = get_macro_regime_view() or {}
        ctx["regime"] = {
            "commentary":       view.get("commentary", ""),
            "dominant_driver":  view.get("dominant_driver", ""),
            "overall_conf":     view.get("overall_confidence", 0),
            "dimensions":       {k: {"state": v["state"], "confidence": v["confidence"], "driver": v["driver"]}
                                 for k, v in (view.get("dimensions") or {}).items()},
        }
    except Exception as e:
        print(f"[macro_analyst] macro_desk: {e}", flush=True)
    try:
        from forex import get_forex_intel
        fx = get_forex_intel() or {}
        ctx["fx"] = {pair: {
            "price":      p.get("price"),
            "change_pct": p.get("change_pct"),
            "direction":  p.get("direction"),
            "confidence": p.get("confidence"),
            "driver":     p.get("driver"),
        } for pair, p in (fx.get("pairs") or {}).items()}
    except Exception as e:
        print(f"[macro_analyst] forex: {e}", flush=True)
    try:
        from cb_calendar import get_cb_calendar
        cc = get_cb_calendar(days_ahead=60, limit=8) or {}
        ctx["cb_calendar"] = [{
            "cb":       e["cb"],
            "date":     e["date_display"],
            "days_to":  e["days_to_event"],
            "label":    e["label"],
            "vol":      e["volatility"],
            "bias":     e["expected_bias"],
            "prev_rate": e["prev_rate"],
        } for e in (cc.get("events") or [])][:8]
    except Exception as e:
        print(f"[macro_analyst] cb_calendar: {e}", flush=True)
    try:
        from news import get_all_news
        items = (get_all_news() or [])[:25]
        ctx["news_top"] = [(it.get("text") or it.get("title", ""))[:160] for it in items if it]
    except Exception as e:
        print(f"[macro_analyst] news: {e}", flush=True)
    return ctx


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
        if cur is None: return default
    return cur


def _format_context_for_llm(ctx: dict) -> str:
    """Compress the live snapshot into a compact context block the LLM can read."""
    m = ctx.get("macro", {}) or {}
    r = ctx.get("regime", {}) or {}
    fx = ctx.get("fx", {}) or {}
    cb = ctx.get("cb_calendar", []) or []
    news = ctx.get("news_top", []) or []

    lines = ["=== LIVE MACRO SNAPSHOT (use this data — do not make up numbers) ==="]
    if m:
        lines.append(
            f"DXY: {m.get('dxy_price')} ({_fmt_pct(m.get('dxy_change'))})  "
            f"| US 10Y: {m.get('us10y_yield')}% ({_fmt_pct(m.get('us10y_chg'))})  "
            f"| VIX: {m.get('vix')}"
        )
        lines.append(
            f"NDX: {_fmt_pct(m.get('nasdaq_chg'))}  SPX: {_fmt_pct(m.get('spx_chg'))}  "
            f"Gold: ${m.get('gold_price')} ({_fmt_pct(m.get('gold_chg'))})  "
            f"Oil: ${m.get('oil_price')} ({_fmt_pct(m.get('oil_chg'))})  "
            f"BTC: ${m.get('btc_price')} ({_fmt_pct(m.get('btc_chg'))})"
        )

    if r.get("commentary"):
        lines.append(f"\nREGIME (overall {r.get('overall_conf', 0)}% conf):")
        lines.append(f"  Commentary: {r['commentary']}")
        lines.append(f"  Dominant driver: {r.get('dominant_driver', '—')}")
        dims = r.get("dimensions", {})
        for name in ("risk", "dollar", "fed", "yields", "inflation", "commodities"):
            dim = dims.get(name)
            if dim:
                lines.append(f"  - {name.upper():12} {dim['state']} ({dim['confidence']}%) — {dim['driver']}")

    if fx:
        lines.append("\nFX MAJORS (direction inferred from macro):")
        for pair, p in fx.items():
            lines.append(f"  {pair}: {p.get('price')} {_fmt_pct(p.get('change_pct'))}  "
                         f"→ {p.get('direction')} {p.get('confidence')}% ({p.get('driver')})")

    if cb:
        lines.append("\nNEXT CENTRAL BANK MEETINGS (next ~60 days):")
        for e in cb[:6]:
            lines.append(f"  {e['cb']} {e['date']} (in {e['days_to']}d): {e['label']}  "
                         f"VOL={e['vol']} · Prev rate: {e['prev_rate']} · Expected: {e['bias']}")

    if news:
        lines.append("\nRECENT NEWS HEADLINES (top 12):")
        for h in news[:12]:
            if h: lines.append(f"  - {h}")

    return "\n".join(lines)


def _fmt_pct(v):
    if v is None: return "—"
    try: return f"{float(v):+.2f}%"
    except: return str(v)


# ─── System prompt — pulled from shared ai_persona + macro-specific overlay ──
try:
    from ai_persona import SYSTEM_PROMPT as _PERSONA_SYS, FEW_SHOTS_MACRO_ANALYST as _FS
except Exception:
    _PERSONA_SYS = ""
    _FS = ""

_MACRO_OVERLAY = """═══ MACRO ANALYST OVERLAY (chat mode, free-form prose) ═══

You are answering a trader's macro question in chat. Output is plain prose
(NOT JSON), 3-5 short paragraphs. Keep the desk voice from the rules above,
plus these macro-specific rules:

LIVE DATA ACCESS: You receive DXY, US10Y, VIX, indices, gold, oil, BTC,
the 10-state regime, six binary macro dimensions, six FX direction signals,
and upcoming central bank meetings with every query. Ground every claim in
the actual numbers — never invent figures.

MACRO LENSES (always think through at least one):
- Yield differentials  → carry trades, FX direction
- Dollar liquidity     → cross-asset risk regime
- Real yields          → gold, REITs, growth equity duration
- Central bank policy  → curve shape, term premium, vol

TONE:
- Institutional vocabulary: "bid", "offered", "compressed", "steepening",
  "carry unwind", "rotational", "rich/cheap", "carry-into-fade".
- Avoid retail vocab: "moon", "rip", "dump", "to the moon".
- Always state what would INVALIDATE your view. A call without an invalidator
  is malpractice on a desk.

FOR SPECIFIC QUESTIONS:
- Correlations: cite observable cross-asset behaviour with a level threshold
  ("typical until VIX breaks 25").
- Central banks: tie the meeting to the FX/yield/equity setup BEFORE it and
  the most likely tape reaction in each scenario.
- Gold: triangulate real yields, DXY, risk regime — never treat it as
  standalone.
"""

SYSTEM_PROMPT = "\n\n".join(p.strip() for p in (_PERSONA_SYS, _MACRO_OVERLAY, _FS) if p)


def _call_groq_chat(messages: list, max_tokens: int = 700, temperature: float = 0.3) -> Optional[str]:
    """Send conversation to Groq, return assistant content (or None on failure)."""
    if not GROQ_API_KEY:
        return None
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model":       GROQ_MODEL,
                "messages":    messages,
                "max_tokens":  max_tokens,
                "temperature": temperature,
            },
            timeout=45,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        # Fallback to smaller/faster model if 70B model is rate-limited
        if resp.status_code in (429, 503):
            time.sleep(2)
            resp2 = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.1-8b-instant", "messages": messages,
                      "max_tokens": max_tokens, "temperature": temperature},
                timeout=30,
            )
            if resp2.status_code == 200:
                return resp2.json()["choices"][0]["message"]["content"]
        print(f"[macro_analyst] groq {resp.status_code}: {resp.text[:200]}", flush=True)
        return None
    except Exception as e:
        print(f"[macro_analyst] groq error: {type(e).__name__}: {e}", flush=True)
        return None


# ─── Main entry point ────────────────────────────────────────────────────────

def ask_analyst(session_id: str, user_question: str) -> dict:
    """Ask the macro analyst a question. Returns dict with answer + context used."""
    if not user_question or not user_question.strip():
        return {"error": "empty question"}
    if not GROQ_API_KEY:
        return {"error": "GROQ_API_KEY not configured"}

    user_question = user_question.strip()[:2000]

    # 1) Pull live context
    ctx = _build_context_snapshot()
    context_block = _format_context_for_llm(ctx)

    # 2) Build message thread: system + context + history + new question
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": context_block},
    ]
    # Add last 8 historical exchanges (16 messages max) for continuity
    history = get_chat_history(session_id, limit=8)
    for h in history[-16:]:
        if h.get("role") in ("user", "assistant"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_question})

    # 3) Call Groq
    answer = _call_groq_chat(messages, max_tokens=750, temperature=0.25)
    if not answer:
        return {"error": "AI service unavailable — try again in a moment"}

    # 4) Persist both turns
    _store_message(session_id, "user", user_question)
    _store_message(session_id, "assistant", answer, context=ctx)

    return {
        "answer":        answer,
        "ts":            datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S IST"),
        "context_used": {
            "regime_commentary":    ctx["regime"].get("commentary", ""),
            "dominant_driver":      ctx["regime"].get("dominant_driver", ""),
            "fx_pairs_seen":        len(ctx["fx"]),
            "news_headlines_seen":  len(ctx["news_top"]),
            "cb_events_seen":       len(ctx["cb_calendar"]),
        },
        "storage_backend": "redis" if _redis_ok else "sqlite",
    }


def storage_status() -> dict:
    return {
        "redis_connected":  _redis_ok,
        "redis_url_set":    bool(os.environ.get("REDIS_URL")),
        "sqlite_path":      _DB_PATH,
        "groq_configured":  bool(GROQ_API_KEY),
        "model":            GROQ_MODEL,
    }
