import sys
import os
import json as _json
sys.path.insert(0, os.path.dirname(__file__))

from contextlib import asynccontextmanager
from fastapi import FastAPI, Body, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncio
import threading
import time as _time
from datetime import datetime, timezone, timedelta

import auth as _auth
from auth import COOKIE_NAME


_bg_tasks: set = set()   # hold strong refs so asyncio doesn't GC tasks

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start all background tasks when server boots."""
    # Restore HNI + morning note from disk (survives Railway restarts)
    saved_hni = _disk_load("hni_summary", HNI_CACHE_TTL)
    if saved_hni:
        _hni_cache["data"] = saved_hni
        _hni_cache["ts"]   = _time.time()
        print("[HNI] restored from disk cache", flush=True)
    saved_note = _disk_load("morning_note", 86400)
    if saved_note and saved_note.get("date") == datetime.now(IST).strftime("%Y-%m-%d"):
        _morning_note.update(saved_note)
        print("[MORNING] restored from disk cache", flush=True)

    threading.Thread(target=_warm,               daemon=True).start()
    threading.Thread(target=_continuous_refresh, daemon=True).start()
    task = asyncio.create_task(_async_digest_loop())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    note_task = asyncio.create_task(_morning_note_scheduler())
    _bg_tasks.add(note_task)
    note_task.add_done_callback(_bg_tasks.discard)
    try:
        from notify import start_watchdog
        start_watchdog()
    except Exception:
        pass
    # Signal memory: init DB + start hourly verification loop
    try:
        import signal_memory as _sm
        _sm.init_db()
        verify_task = asyncio.create_task(_signal_verify_loop())
        _bg_tasks.add(verify_task)
        verify_task.add_done_callback(_bg_tasks.discard)
    except Exception as _e:
        print(f"[SIGNAL_MEM] init error: {_e}", flush=True)
    yield


async def _signal_verify_loop():
    """Run 24h signal verification every hour."""
    await asyncio.sleep(3600)   # wait 1h after boot before first pass
    while True:
        try:
            import signal_memory as _sm
            await asyncio.to_thread(_sm.run_verification_pass)
        except Exception as _e:
            print(f"[SIGNAL_MEM] verify error: {_e}", flush=True)
        await asyncio.sleep(3600)


app = FastAPI(title="AI Market Terminal", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Auth middleware — gates every request except public paths ──────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse as _StarletteRedirect

_NO_AUTH_PATHS    = {"/health", "/login", "/logout", "/favicon.ico"}
_NO_AUTH_PREFIXES = ("/static/", "/api/live-ticker")

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        # Always allow public paths
        if path in _NO_AUTH_PATHS or any(path.startswith(p) for p in _NO_AUTH_PREFIXES):
            return await call_next(request)
        # Verify session cookie
        token = request.cookies.get(COOKIE_NAME)
        user  = _auth.verify_session(token) if token else None
        if not user:
            # API calls → 401 JSON, page requests → redirect to login
            if path.startswith("/api/") or path.startswith("/admin/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return _StarletteRedirect(f"/login")
        return await call_next(request)

app.add_middleware(AuthMiddleware)

# Serve static files (images, icons, etc.) from /static/
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

IST = timezone(timedelta(hours=5, minutes=30))
_cache = {}
_cache_lock = threading.Lock()
_startup_done = False

# ── File-based persistent cache (survives Railway restarts) ───
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "cache")
try:
    os.makedirs(_CACHE_DIR, exist_ok=True)
except Exception:
    _CACHE_DIR = "/tmp"

def _disk_save(key: str, data: dict) -> None:
    try:
        path = os.path.join(_CACHE_DIR, f"{key}.json")
        with open(path, "w") as f:
            _json.dump({"data": data, "ts": _time.time()}, f)
    except Exception:
        pass

def _disk_load(key: str, ttl: int):
    try:
        path = os.path.join(_CACHE_DIR, f"{key}.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            entry = _json.load(f)
        if (_time.time() - entry.get("ts", 0)) < ttl:
            return entry.get("data")
    except Exception:
        pass
    return None

# ── HNI deduplicator lock + morning note state ────────────────
_hni_lock          = asyncio.Lock()
_morning_note: dict = {}   # {"date": "YYYY-MM-DD", "data": {...}}
_morning_note_lock = asyncio.Lock()

# Pre-declare so lifespan can reference them before definition
HNI_CACHE_TTL = 600
_hni_cache: dict = {}


def now_ist():
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")


def _cached(key, ttl, fn):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (_time.time() - entry["ts"]) < ttl:
            return entry["data"]
    data = fn()
    with _cache_lock:
        _cache[key] = {"data": data, "ts": _time.time()}
    return data


def _bg_refresh(key, ttl, fn, empty=None):
    with _cache_lock:
        entry = _cache.get(key)
    if entry:
        if (_time.time() - entry["ts"]) > ttl:
            threading.Thread(target=_cached, args=(key, ttl, fn), daemon=True).start()
        return entry["data"]
    threading.Thread(target=_cached, args=(key, ttl, fn), daemon=True).start()
    return empty if empty is not None else []


def _lazy(module_name, fn_name, *args, **kwargs):
    """Import a function lazily and call it. Returns {} on any error."""
    try:
        mod = __import__(module_name)
        fn = getattr(mod, fn_name)
        return fn(*args, **kwargs)
    except Exception:
        return {}


# Detect Railway cloud environment
ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_SERVICE_NAME"))

# ── Continuous background refresh loop ───────────────────────
def _continuous_refresh():
    """Keeps news + prices always fresh in background — clients get near-instant data."""
    _time.sleep(15)  # wait for initial warmup to finish
    while True:
        try: _cached("news",    15, _build_news)
        except: pass
        _time.sleep(5)
        try: _cached("indices", 20, lambda: _lazy("indices", "get_indices"))
        except: pass
        _time.sleep(5)
        try: _cached("macro",   30, lambda: _lazy("macro", "get_macro_data"))
        except: pass
        _time.sleep(10)  # total loop ~20s — news is always < 20s old


# ── Background warm-up — gentle sequential loading ────────────
def _warm():
    # On Railway: load one module at a time with long gaps to stay under 512MB RAM
    # Locally: load everything quickly
    gap = 8 if ON_RAILWAY else 2

    _time.sleep(gap)
    try: _cached("macro",   30,  lambda: _lazy("macro",   "get_macro_data"))
    except: pass

    _time.sleep(gap)
    try: _cached("indices", 20,  lambda: _lazy("indices", "get_indices"))
    except: pass

    _time.sleep(gap)
    try: _cached("news",    15,  _build_news)
    except: pass

    if ON_RAILWAY:
        # On Railway skip heavy modules that push RAM over limit
        return

    _time.sleep(3)
    try: _cached("stocks",   120,  _build_stocks)
    except: pass
    _time.sleep(3)
    try: _cached("earnings", 1800, _build_earnings)
    except: pass
    _time.sleep(3)
    try: _cached("nse",      300,  _build_nse)
    except: pass
    try:
        from earnings_telegram import get_telegram_earnings
        get_telegram_earnings(force_refresh=True)
    except: pass


def _build_news():
    try:
        from news import get_all_news
        from priority import prioritize_news
        scored = prioritize_news(get_all_news(), summarize=False)
        # Immediate buzz alert for score 8+ breaking news (always instant)
        try:
            from notify import alert_high_news
            for entry in scored[:10]:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    score, item = entry
                    if score >= 8 and isinstance(item, dict):
                        alert_high_news(item.get("text",""), item.get("source",""), score)
        except Exception:
            pass
        return scored
    except Exception as e:
        print(f"[_build_news] {type(e).__name__}: {e}", flush=True)
        return []


# ── Shared Telegram sender (used by loop + test endpoint) ─────
_TG_BOT   = os.environ.get("TELEGRAM_BOT_TOKEN", "8475057388:AAGUlt5Qu3Ei2_3xeUF8S1TWvygDKVVxb8I").strip()
_TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",   "-1001379475837").strip()

def _tg_send(text: str, silent: bool = False) -> bool:
    import requests as _rq
    try:
        r = _rq.post(
            f"https://api.telegram.org/bot{_TG_BOT}/sendMessage",
            json={"chat_id": _TG_CHAT, "text": text,
                  "parse_mode": "HTML", "disable_notification": silent},
            timeout=12
        )
        if r.status_code != 200:
            print(f"[TG] {r.status_code}: {r.text[:300]}", flush=True)
        return r.status_code == 200
    except Exception as e:
        print(f"[TG] exception: {e}", flush=True)
        return False


def _build_digest_message(scored: list) -> tuple:
    """Build digest text from scored news list. Returns (message, buzz)."""
    cutoff       = _time.time() - 310
    fresh_high   = []
    fresh_medium = []
    top_stories  = []

    for entry in scored:
        try:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            score, item = entry
            if not isinstance(item, dict):
                continue
            headline = (item.get("text") or "").strip()
            if not headline:
                continue
            if score >= 4:
                top_stories.append((score, item))
            pub_utc = item.get("pub_utc", "")
            if not pub_utc:
                continue
            try:
                pub_ts = datetime.fromisoformat(pub_utc.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if pub_ts < cutoff:
                continue
            if score >= 8:
                fresh_high.append((score, item))
            elif score >= 5:
                fresh_medium.append((score, item))
        except Exception:
            continue

    fresh_high.sort(key=lambda x: -x[0])
    fresh_medium.sort(key=lambda x: -x[0])
    top_stories.sort(key=lambda x: -x[0])

    ist_now = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")
    lines   = [f"📊 <b>AI MARKET TERMINAL</b>  •  {ist_now}", ""]

    if fresh_high or fresh_medium:
        n = len(fresh_high) + len(fresh_medium)
        lines.append(f"<b>🔔 {n} new stor{'y' if n==1 else 'ies'} in last 5 min:</b>")
        lines.append("")
        if fresh_high:
            lines.append("🔴 <b>BREAKING NEWS:</b>")
            for score, item in fresh_high[:5]:
                src = item.get("source", "")
                s = f"  <i>[{src}]</i>" if src else ""
                lines.append(f"• {item['text']}{s}  <b>({score}/10)</b>")
            lines.append("")
        if fresh_medium:
            lines.append("🟡 <b>IMPORTANT:</b>")
            for score, item in fresh_medium[:5]:
                src = item.get("source", "")
                s = f"  <i>[{src}]</i>" if src else ""
                lines.append(f"• {item['text']}{s}  ({score}/10)")
    elif top_stories:
        lines.append("📰 <b>TOP MARKET STORIES:</b>")
        lines.append("")
        for score, item in top_stories[:5]:
            src = item.get("source", "")
            s = f"  <i>[{src}]</i>" if src else ""
            lines.append(f"• {item['text']}{s}  ({score}/10)")
        lines.append("")
        lines.append("<i>No major breaking news in last 5 min</i>")
    else:
        lines.append("🔕 <b>Markets Quiet</b>")
        lines.append("<i>No significant news at this time. Monitoring live...</i>")

    lines.append("")
    lines.append("⚡️ <i>Updates every 5 min  |  AI Market Terminal</i>")
    return "\n".join(lines), len(fresh_high) > 0


# ── Digest loop state (readable via /telegram/status) ─────────
_digest_state  = {"count": 0, "last_sent": None, "last_ok": None, "running": False}
_sent_headlines: set = set()   # dedup — never re-send the same headline

def _run_one_digest_cycle():
    """Blocking work for one digest cycle — only sends when there is genuinely new breaking news."""
    global _digest_state, _sent_headlines
    cycle_num = _digest_state["count"] + 1
    ist_t     = datetime.now(IST).strftime("%H:%M IST")
    print(f"[DIGEST] ── cycle #{cycle_num} @ {ist_t} ──", flush=True)

    try:
        scored = _cache.get("news", {}).get("data") or []
        print(f"[DIGEST] cache: {len(scored)} items", flush=True)

        # Only look at fresh items (last 5 min) with score >= 8 (breaking) or >= 5 (important)
        cutoff     = _time.time() - 310
        fresh_high = []
        fresh_med  = []

        for entry in scored:
            try:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                score, item = entry
                if not isinstance(item, dict):
                    continue
                headline = (item.get("text") or "").strip()
                if not headline:
                    continue
                # Skip already sent headlines
                if headline in _sent_headlines:
                    continue
                pub_utc = item.get("pub_utc", "")
                if not pub_utc:
                    continue
                try:
                    pub_ts = datetime.fromisoformat(pub_utc.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if pub_ts < cutoff:
                    continue
                if score >= 8:
                    fresh_high.append((score, item, headline))
                elif score >= 5:
                    fresh_med.append((score, item, headline))
            except Exception:
                continue

        # Skip cycle completely if nothing genuinely new
        if not fresh_high and not fresh_med:
            print(f"[DIGEST] no new breaking stories — skipping Telegram", flush=True)
            _digest_state["count"] = cycle_num
            return

        # Build message from only new headlines
        fresh_high.sort(key=lambda x: -x[0])
        fresh_med.sort(key=lambda x: -x[0])
        ist_now = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")
        lines   = [f"📊 <b>AI MARKET TERMINAL</b>  •  {ist_now}", ""]
        n       = len(fresh_high) + len(fresh_med)
        lines.append(f"<b>🔔 {n} new stor{'y' if n==1 else 'ies'}:</b>")
        lines.append("")

        new_sent = []
        if fresh_high:
            lines.append("🔴 <b>BREAKING:</b>")
            for score, item, hl in fresh_high[:5]:
                src = item.get("source", "")
                s = f"  <i>[{src}]</i>" if src else ""
                lines.append(f"• {item['text']}{s}  <b>({score}/10)</b>")
                new_sent.append(hl)
            lines.append("")
        if fresh_med:
            lines.append("🟡 <b>IMPORTANT:</b>")
            for score, item, hl in fresh_med[:5]:
                src = item.get("source", "")
                s = f"  <i>[{src}]</i>" if src else ""
                lines.append(f"• {item['text']}{s}  ({score}/10)")
                new_sent.append(hl)

        lines.append("")
        lines.append("⚡️ <i>AI Market Terminal  |  Live</i>")
        msg = "\n".join(lines)
        buzz = len(fresh_high) > 0

        ok = _tg_send(msg, silent=not buzz)
        print(f"[DIGEST] sent={ok}  buzz={buzz}  new={len(new_sent)}", flush=True)

        # Mark sent so we never repeat them
        _sent_headlines.update(new_sent)
        if len(_sent_headlines) > 500:   # prevent unbounded memory growth
            _sent_headlines = set(list(_sent_headlines)[-300:])

        _digest_state["count"]     = cycle_num
        _digest_state["last_sent"] = datetime.now(IST).strftime("%d %b %H:%M IST")
        _digest_state["last_ok"]   = ok

    except Exception as e:
        print(f"[DIGEST] cycle error: {e}", flush=True)
        import traceback; traceback.print_exc()
        # Do NOT send Telegram on error — that was the source of spam


async def _async_digest_loop():
    """
    Asyncio task — managed by uvicorn's event loop, never dropped.
    Runs blocking digest work in a thread pool (asyncio.to_thread).
    First message in 10s, then every 5 minutes forever.
    """
    global _digest_state
    _digest_state["running"] = True
    print("[DIGEST] asyncio task started", flush=True)

    await asyncio.sleep(180)   # 3-min boot wait — prevents spam on restarts

    while True:
        t0 = _time.time()
        await asyncio.to_thread(_run_one_digest_cycle)
        elapsed  = _time.time() - t0
        sleep_for = max(10, 300 - int(elapsed))
        print(f"[DIGEST] next in {sleep_for}s", flush=True)
        await asyncio.sleep(sleep_for)

def _build_stocks():
    try:
        from stocks import get_mag7, get_semiconductors, get_india_indices, get_gold_etfs, detect_movers
        return {
            "mag7":   get_mag7(),
            "semis":  get_semiconductors(),
            "india":  get_india_indices(),
            "etfs":   get_gold_etfs(),
            "movers": detect_movers(),
        }
    except Exception as e:
        print(f"[_build_indices] {type(e).__name__}: {e}", flush=True)
        return {}

def _build_earnings():
    try:
        from earnings import get_earnings
        return get_earnings()
    except Exception as e:
        print(f"[_build_earnings] {type(e).__name__}: {e}", flush=True)
        return []

def _build_nse():
    try:
        from nse_data import get_nse_snapshot
        return get_nse_snapshot()
    except Exception as e:
        print(f"[_build_nse] {type(e).__name__}: {e}", flush=True)
        return {}

def _build_signal():
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from macro import get_macro_data, format_macro
        from news import get_all_news, format_news
        from stocks import format_stocks
        from econ import get_economic_data
        macro_txt  = format_macro(get_macro_data())
        news_txt   = format_news(get_all_news())
        stocks_txt = format_stocks()
        econ       = get_economic_data()

        # Fetch regime in parallel with other signal tasks
        regime_data = {}
        try:
            from regime import detect_market_regime
            regime_data = detect_market_regime() or {}
        except Exception:
            pass

        results = {}
        def _run(key, mod, fn, *a):
            try:
                m = __import__(mod)
                results[key] = getattr(m, fn)(*a)
            except: results[key] = None

        tasks = [
            ("signal",    "trade_signal", "generate_signal",  macro_txt, news_txt, stocks_txt, econ),
            ("brain",     "interpreter",  "interpret_macro",   macro_txt, news_txt, stocks_txt, econ),
            ("smc",       "smc",          "get_smc_analysis"),
            ("mtf",       "mtf",          "get_mtf_bias"),
            ("structure", "structure",    "get_structure"),
        ]
        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = [pool.submit(_run, t[0], t[1], t[2], *t[3:]) for t in tasks]
            for f in as_completed(futs, timeout=15): pass

        signal    = results.get("signal")    or {}
        brain     = results.get("brain")     or {"insights": []}
        smc       = results.get("smc")       or {}
        mtf       = results.get("mtf")       or {}
        structure = results.get("structure") or {"high":0,"low":0,"pivot":0,"r1":0,"s1":0,"fib":{}}

        sniper = {}
        try:
            from sniper import sniper_entry
            sniper = sniper_entry(signal)
        except: sniper = {"entry":"—","htf":"—","sweep":"—","reason":"—"}

        result = {
            "signal":    signal,
            "regime":    regime_data,
            "insights":  brain.get("insights", []),
            "smc":       {"bos": smc.get("bos",""), "ob": smc.get("order_block",""), "liquidity": smc.get("liquidity","")},
            "mtf":       mtf,
            "sniper":    {"entry": sniper.get("entry","—"), "htf": sniper.get("htf","—"),
                          "sweep": sniper.get("sweep","—"), "reason": sniper.get("reason","—")},
            "structure": {
                "high":  round(float(structure.get("high",0)), 2),
                "low":   round(float(structure.get("low",0)),  2),
                "pivot": round(float(structure.get("pivot",0)),2),
                "r1":    round(float(structure.get("r1",0)),   2),
                "s1":    round(float(structure.get("s1",0)),   2),
                "fib":   {k: round(float(v),2) for k,v in structure.get("fib",{}).items()},
            },
            "timestamp": now_ist(),
        }
        # Fire-and-forget: log signal to memory DB without blocking the response
        try:
            import signal_memory as _sm
            threading.Thread(target=_sm.log_signal, args=(result,), daemon=True).start()
            # Attach quality label + confidence boost to the response
            s     = result.get("signal") or {}
            sc    = float(s.get("score", 0) or 0)
            rkey  = result.get("regime", {}).get("regime", "")
            rconf = int(result.get("regime", {}).get("confidence", 0) or 0)
            boost = _sm.get_confidence_boost(rkey)
            qlbl  = _sm.compute_quality_label(rkey, rconf + boost, sc)
            result["quality_label"]      = qlbl
            result["confidence_boost"]   = boost
        except Exception:
            pass
        return result
    except Exception as e:
        return {"error": str(e), "timestamp": now_ist()}


# Background threads are started inside lifespan() above — do NOT start them here too.


# ── Endpoints ─────────────────────────────────────────────────

# ── Auth helpers ──────────────────────────────────────────────────────────────

# Public paths that never need a login check
_PUBLIC_PATHS = {"/health", "/login", "/logout", "/api/live-ticker"}
_PUBLIC_PREFIX = ("/static/",)


def _get_session_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    return _auth.verify_session(token) if token else None


def _require_auth(request: Request) -> dict | None:
    """Return user dict or None. Caller redirects if None."""
    return _get_session_user(request)


def _require_admin(request: Request) -> dict | None:
    user = _get_session_user(request)
    if user and user.get("role") == "admin":
        return user
    return None


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _get_session_user(request):
        return RedirectResponse("/", status_code=302)
    path = os.path.join(os.path.dirname(__file__), "templates", "login.html")
    with open(path) as f:
        return f.read()


@app.post("/login")
async def login_post(request: Request, response: Response):
    try:
        body     = await request.json()
        username = str(body.get("username", "")).strip().lower()
        password = str(body.get("password", ""))
    except Exception:
        return JSONResponse({"ok": False, "message": "Invalid request"}, status_code=400)

    token = _auth.login(username, password)
    if not token:
        return JSONResponse({"ok": False, "message": "Invalid username or password"})

    user = _auth.get_user(username)
    redirect = "/admin" if user and user.get("role") == "admin" else "/"

    resp = JSONResponse({"ok": True, "redirect": redirect})
    resp.set_cookie(
        key=COOKIE_NAME, value=token,
        max_age=86400 * 30,
        httponly=True, samesite="lax",
        secure=False,   # set True in production (Railway has HTTPS)
    )
    return resp


@app.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        _auth.delete_session(token)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/logout")
def logout_get(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        _auth.delete_session(token)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ── Admin panel ────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin_panel(request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    path = os.path.join(os.path.dirname(__file__), "templates", "admin.html")
    with open(path) as f:
        return f.read()


@app.get("/admin/api/stats")
def admin_stats(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return _auth.get_stats()


@app.get("/admin/api/users")
def admin_list_users(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return _auth.list_users()


@app.post("/admin/api/users/add")
async def admin_add_user(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        b = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "Invalid request"})
    username = str(b.get("username", "")).strip().lower()
    password = str(b.get("password", ""))
    email    = str(b.get("email", "")).strip()
    days     = int(b.get("days", 30))
    role     = str(b.get("role", "subscriber"))
    notes    = str(b.get("notes", ""))
    if not username or not password:
        return JSONResponse({"ok": False, "message": "Username and password required"})
    ok = _auth.create_user(username, password, email, role, days)
    if ok and notes:
        _auth.update_user(username, notes=notes)
    return JSONResponse({"ok": ok, "message": "User already exists" if not ok else ""})


@app.post("/admin/api/users/update")
async def admin_update_user(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    b = await request.json()
    username = str(b.get("username", "")).strip().lower()
    if not username:
        return JSONResponse({"ok": False})
    kwargs = {k: v for k, v in b.items() if k != "username"}
    ok = _auth.update_user(username, **kwargs)
    return JSONResponse({"ok": ok})


@app.post("/admin/api/users/delete")
async def admin_delete_user(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    b = await request.json()
    username = str(b.get("username", "")).strip().lower()
    if username == "admin":
        return JSONResponse({"ok": False, "message": "Cannot delete admin"})
    ok = _auth.delete_user(username)
    return JSONResponse({"ok": ok})


@app.post("/admin/api/users/password")
async def admin_reset_password(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    b = await request.json()
    username = str(b.get("username", "")).strip().lower()
    password = str(b.get("password", ""))
    if not username or not password:
        return JSONResponse({"ok": False, "message": "Missing fields"})
    ok = _auth.change_password(username, password)
    return JSONResponse({"ok": ok})


@app.post("/admin/api/change-password")
async def admin_change_my_password(request: Request):
    user = _require_admin(request)
    if not user:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    b        = await request.json()
    current  = str(b.get("current_password", ""))
    new_pw   = str(b.get("new_password", ""))
    if not _auth.login(user["username"], current):
        return JSONResponse({"ok": False, "message": "Current password incorrect"})
    if len(new_pw) < 8:
        return JSONResponse({"ok": False, "message": "Password must be at least 8 characters"})
    ok = _auth.change_password(user["username"], new_pw)
    return JSONResponse({"ok": ok})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/regime")
def api_regime(force: bool = False):
    """Market Regime Engine — classifies current macro environment into 10 institutional regimes."""
    def _build():
        from regime import detect_market_regime
        return detect_market_regime(force=force)
    return _bg_refresh("regime", 60, _build, empty={
        "regime": "risk_on", "label": "LOADING...", "icon": "◌",
        "color": "#4b5563", "bg": "#0d1117", "confidence": 0,
        "explanation": [], "bullish_assets": [], "bearish_assets": [],
        "defensive_assets": [], "secondary_regime": None, "secondary_label": None,
        "all_scores": {}, "signals_used": {}, "generated_at": "—",
    })


@app.get("/telegram/digest")
def telegram_digest():
    """
    Send one digest to the Telegram channel RIGHT NOW.
    Call this endpoint every 5 minutes via an external cron (cron-job.org).
    This is the production-grade approach — no daemon threads, no asyncio, 100% reliable.
    """
    scored = _cache.get("news", {}).get("data") or []

    # If cache cold, quick fallback fetch
    if not scored:
        try:
            import feedparser, requests as _rq2
            QUICK = {
                "Economic Times": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
                "MoneyControl":   "https://www.moneycontrol.com/rss/MCtopnews.xml",
                "Reuters":        "https://feeds.reuters.com/reuters/topNews",
                "ET Markets":     "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
                "Hindu BizLine":  "https://www.thehindubusinessline.com/markets/feeder/default.rss",
            }
            raw = []
            for src, url in QUICK.items():
                try:
                    resp = _rq2.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
                    feed = feedparser.parse(resp.content)
                    for e in feed.entries[:8]:
                        t = (e.get("title") or "").strip()
                        if t:
                            raw.append({"text": t, "source": src, "pub_utc": "", "category": "INDIA"})
                except Exception:
                    pass
            if raw:
                from priority import prioritize_news
                scored = prioritize_news(raw, summarize=False) or []
        except Exception:
            pass

    msg, buzz = _build_digest_message(scored)

    # Send with full error detail returned in response
    import requests as _rqd
    tg_status, tg_body = None, None
    try:
        r = _rqd.post(
            f"https://api.telegram.org/bot{_TG_BOT}/sendMessage",
            json={"chat_id": _TG_CHAT, "text": msg,
                  "parse_mode": "HTML", "disable_notification": not buzz},
            timeout=12
        )
        tg_status = r.status_code
        tg_body   = r.text[:400]
        ok = r.status_code == 200
    except Exception as e:
        tg_body = str(e)
        ok = False

    print(f"[CRON] digest sent={ok}  items={len(scored)}  tg={tg_status}", flush=True)
    return {
        "sent":       ok,
        "items":      len(scored),
        "time":       datetime.now(IST).strftime("%H:%M IST"),
        "tg_status":  tg_status,
        "tg_error":   tg_body if not ok else None,
        "bot_token_len":    len(_TG_BOT),
        "bot_token_prefix": _TG_BOT[:20] + "...",
        "chat_id":    _TG_CHAT,
        "token_from_env": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
    }


@app.get("/telegram/test")
def telegram_test():
    """Quick connectivity test + loop health check."""
    scored = _cache.get("news", {}).get("data") or []
    msg, buzz = _build_digest_message(scored)
    ok = _tg_send(msg, silent=not buzz)
    return {
        "sent":           ok,
        "chat_id":        _TG_CHAT,
        "cached_items":   len(scored),
        "loop_running":   _digest_state["running"],
        "loop_cycles":    _digest_state["count"],
        "last_auto_sent": _digest_state["last_sent"],
        "last_auto_ok":   _digest_state["last_ok"],
        "message_preview": msg[:300],
    }


@app.get("/api/test-alert")
def test_alert():
    """Send a test Telegram alert to confirm bot is working."""
    try:
        from notify import send_telegram
        from datetime import datetime, timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        ok = send_telegram(
            f"✅ <b>Test Alert — AI Market Terminal</b>\n\n"
            f"Bot is working correctly on Railway.\n"
            f"You will receive alerts when:\n"
            f"🔴 High impact news (score 8+)\n"
            f"🏦 FII net crosses ±₹5,000 Cr\n"
            f"⚠️ VIX backwardation detected\n"
            f"🏛 Congress cluster buy\n"
            f"🚀 Broad rally / selloff\n\n"
            f"🕐 {datetime.now(IST).strftime('%d-%b-%Y %H:%M IST')}"
        )
        return {"sent": ok, "message": "Test alert sent!" if ok else "Failed — check token/chat ID"}
    except Exception as e:
        return {"sent": False, "error": str(e)}


def _build_ai_news():
    """Enrich top high-impact news with Groq AI sentiment + summary."""
    try:
        from ai_layer import enrich_news, get_market_sentiment
        from priority import prioritize_news
        # Get cached news
        scored = _cache.get("news", {}).get("data") or []
        # Take top 30 by score for AI enrichment
        top = []
        for entry in scored[:60]:
            if isinstance(entry, (list,tuple)) and len(entry)==2:
                score, item = entry
                if isinstance(item, dict) and score >= 3:
                    top.append(item)
        if not top:
            return {"news": [], "sentiment": {}, "source": "none"}
        enriched  = enrich_news(top[:30])
        sentiment = get_market_sentiment(enriched)
        return {"news": enriched, "sentiment": sentiment, "source": "ai"}
    except Exception as e:
        return {"news": [], "sentiment": {}, "error": str(e)}


def _build_decisions():
    """Generate AI trade decisions for key assets."""
    try:
        from decision_engine import generate_decisions, get_overall_bias
        ai_data  = _cache.get("ai_news", {}).get("data") or {}
        enriched = ai_data.get("news", [])
        decisions = generate_decisions(enriched_news=enriched)
        overall   = get_overall_bias(decisions)
        return {"decisions": decisions, "overall": overall}
    except Exception as e:
        return {"decisions": [], "overall": {}, "error": str(e)}


@app.get("/api/news/ai")
def api_news_ai():
    """AI-enriched news: sentiment, summary, impact, affected assets."""
    return _bg_refresh("ai_news", 600, _build_ai_news, empty={"news":[],"sentiment":{}})


@app.get("/api/decisions")
def api_decisions():
    """Trade decisions per asset combining AI news + technicals."""
    return _bg_refresh("decisions", 300, _build_decisions, empty={"decisions":[],"overall":{}})


@app.get("/api/macro")
def api_macro():
    data = _bg_refresh("macro", 30, lambda: _lazy("macro", "get_macro_data"), empty={})
    if not isinstance(data, dict): data = {}
    return {
        "fx":            data.get("FX", {}),
        "yields":        data.get("US_YIELDS", {}),
        "global_yields": data.get("GLOBAL_YIELDS", {}),
        "oil":           data.get("OIL"),
        "gold":          data.get("GOLD_SPOT"),
    }


@app.get("/api/stocks")
def api_stocks():
    ttl = 300 if ON_RAILWAY else 120
    return _bg_refresh("stocks", ttl, _build_stocks, empty={})


@app.get("/api/econ")
def api_econ():
    try:
        from econ import get_econ_data, get_economic_data
        data = get_econ_data()
        econ = get_economic_data()
        return {
            "us_economy":    data.get("US_ECONOMY", {}),
            "inflation":     data.get("INFLATION", {}),
            "global_growth": data.get("GLOBAL_GROWTH", {}),
            "yield_curve":   data.get("YIELD_CURVE", {}),
            "calendar":      econ[:10],
        }
    except Exception as e:
        print(f"[/api/econ] {type(e).__name__}: {e}", flush=True)
        return {}


@app.get("/api/news")
def api_news():
    scored = _bg_refresh("news", 30, _build_news, empty=[])
    result = []
    for entry in scored:
        try:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                score, item = entry
            else: continue
            if isinstance(item, dict):
                result.append({
                    "score":      score,
                    "priority":   "high" if score >= 8 else "med" if score >= 4 else "low",
                    "headline":   item.get("text", ""),
                    "source":     item.get("source", ""),
                    "time":       item.get("time", ""),
                    "pub_utc":    item.get("pub_utc", ""),
                    "category":   item.get("category", "MARKETS"),
                    "summarized": item.get("summarized", False),
                    "tickers":    item.get("tickers", []),
                    "url":        item.get("url", ""),
                })
        except: pass
    return result


# ── AI Summary proxy → forwards to Bun news service (localhost:4000) ──────────
NEWS_SERVICE_URL = os.environ.get("NEWS_SERVICE_URL", "http://localhost:4000")

@app.post("/api/summary")
async def api_summary_proxy(request: Request):
    try:
        body = await request.body()
        import httpx
        async with httpx.AsyncClient(timeout=245) as client:
            resp = await client.post(
                f"{NEWS_SERVICE_URL}/api/summary",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                return JSONResponse(content=resp.json(), status_code=resp.status_code)
            except ValueError:
                # Upstream returned non-JSON — surface the upstream status + body
                # so the failure isn't an opaque 500.
                snippet = resp.text[:500]
                print(f"[SUMMARY] upstream {resp.status_code} non-JSON: {snippet[:200]}", flush=True)
                return JSONResponse(
                    {"error": "upstream returned non-JSON", "upstream_status": resp.status_code, "body": snippet},
                    status_code=502,
                )
    except httpx.ConnectError:
        return JSONResponse({"error": "news service unavailable"}, status_code=503)
    except httpx.TimeoutException:
        return JSONResponse({"error": "news service timeout"}, status_code=504)
    except Exception as e:
        print(f"[SUMMARY] proxy error: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/indices")
def api_indices_cached():
    return _bg_refresh("indices", 30, lambda: _lazy("indices", "get_indices"), empty=[])


@app.get("/api/signal")
def api_signal():
    return _bg_refresh("signal", 300, _build_signal, empty={})


@app.get("/api/nse")
def api_nse():
    return _cached("nse", 300, _build_nse)


@app.get("/api/nse/bulk")
def api_bulk_deals():
    try:
        from nse_data import get_bulk_deals
        return _cached("bulk", 300, get_bulk_deals)
    except Exception as e:
        print(f"[/api/nse/bulk] {type(e).__name__}: {e}", flush=True)
        return []


@app.get("/api/sentiment")
def api_sentiment():
    """Combined Fear & Greed: CNN (US stocks) + alternative.me (crypto)."""
    try:
        from market_sentiment import get_combined_sentiment
        return _cached("sentiment", 1800, get_combined_sentiment)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/nse/pcr")
def api_pcr():
    try:
        from nse_data import get_nifty_pcr, get_banknifty_pcr
        return {
            "nifty":     _cached("pcr_nifty", 300, get_nifty_pcr),
            "banknifty": _cached("pcr_bnf",   300, get_banknifty_pcr),
        }
    except Exception as e: return {"error": str(e)}


@app.get("/api/nse/fii")
def api_fii():
    try:
        from nse_data import get_fii_dii, get_fii_cumulative
        return {
            "today":      _cached("fii_today", 900,  get_fii_dii),
            "cumulative": _cached("fii_cumul", 3600, get_fii_cumulative),
        }
    except Exception as e: return {"error": str(e)}


@app.get("/api/cot")
def api_cot():
    try:
        from cot_data import get_cot
        return _cached("cot", 86400, get_cot)
    except Exception as e: return {"error": str(e)}


@app.get("/api/insider")
def api_insider():
    try:
        from insider_tracker import get_insider_data
        return _cached("insider", 1800, get_insider_data)
    except Exception as e: return {"error": str(e)}


@app.get("/api/correlations")
def api_correlations():
    try:
        from correlations import get_correlations
        return _cached("correlations", 3600, get_correlations)
    except Exception as e: return {"error": str(e)}


@app.get("/api/liquidity")
def api_liquidity():
    try:
        from fred_data import get_liquidity
        return _cached("liquidity", 21600, get_liquidity)
    except Exception as e: return {"error": str(e)}


@app.get("/api/earnings")
def api_earnings():
    return _bg_refresh("earnings", 1800, _build_earnings, empty=[])


@app.get("/api/earnings/live")
def api_earnings_live():
    try:
        from earnings_telegram import _cache_get_all
        tg = _cache_get_all()
    except Exception as e:
        print(f"[/api/earnings/live] cache fetch failed: {type(e).__name__}: {e}", flush=True)
        return []
    try:
        from earnings import NAMES, REGION_MAP, WATCH_LIST, SECTOR_MAP
    except:
        NAMES = REGION_MAP = WATCH_LIST = SECTOR_MAP = {}
    results = []
    for ticker, d in tg.items():
        name   = NAMES.get(ticker, NAMES.get(ticker+".NS", ticker))
        grp    = next((g for g, syms in WATCH_LIST.items()
                       if ticker in syms or ticker+".NS" in syms), "")
        region = d.get("region") or REGION_MAP.get(grp, "GLOBAL")
        score  = d.get("score", 50)
        n      = round(score / 20)
        results.append({
            "symbol": ticker, "name": name, "region": region,
            "group": grp, "sector": SECTOR_MAP.get(grp, ""),
            "currency": "INR" if region == "INDIA" else "USD",
            "earn_date": d.get("quarter", "—"),
            "eps_act": d.get("eps"), "eps_prev": None, "eps_yoy": d.get("yoy_growth"),
            "revenue": (f"₹{d['revenue_cr']/1000:.1f}K Cr" if d.get("revenue_cr") and d["revenue_cr"] >= 1000
                        else f"₹{d['revenue_cr']:.0f} Cr" if d.get("revenue_cr")
                        else f"${d['revenue_b']:.2f}B" if d.get("revenue_b") else "—"),
            "rev_growth": None,
            "net_interest_income": (f"₹{d['pat_cr']/1000:.1f}K Cr" if d.get("pat_cr") and d["pat_cr"] >= 1000
                                    else f"₹{d['pat_cr']:.0f} Cr" if d.get("pat_cr") else "—"),
            "gross_margin": None, "margin_bps": None, "net_margin": None, "nim_bps": None,
            "guidance": d.get("guidance", "—") or "—",
            "beat_miss": d.get("beat_miss"),
            "commentary": d.get("commentary", ""),
            "score": score, "stars": "★" * n + "☆" * (5 - n),
            "data_source": "LIVE-TG", "price": None, "price_chg_pct": None,
        })
    results.sort(key=lambda x: -x["score"])
    return results


@app.get("/api/earnings/social")
def api_earnings_social():
    try:
        from earnings_social import get_earnings_social
        return _cached("earn_social", 120, get_earnings_social)
    except Exception as e:
        print(f"[/api/earnings/social] {type(e).__name__}: {e}", flush=True)
        return []


@app.get("/api/sources")
def api_sources():
    try:
        from sources_config import get_all_sources
        return get_all_sources()
    except Exception as e:
        print(f"[/api/sources] {type(e).__name__}: {e}", flush=True)
        return []


@app.post("/api/sources/approve")
def api_approve(payload: dict = Body(...)):
    try:
        from sources_config import approve
        approve(payload["name"])
    except: pass
    return {"ok": True, "name": payload["name"], "status": "approved"}


@app.post("/api/sources/reject")
def api_reject(payload: dict = Body(...)):
    try:
        from sources_config import reject
        reject(payload["name"])
    except: pass
    return {"ok": True, "name": payload["name"], "status": "rejected"}


@app.post("/api/sources/add")
def api_add_source(payload: dict = Body(...)):
    try:
        from sources_config import add_pending
        add_pending(payload["name"], payload["url"],
                    payload.get("category", "MARKETS"),
                    payload.get("type", "telegram"))
    except: pass
    return {"ok": True, "name": payload["name"], "status": "pending"}


def _get_news_cache():
    """Return raw news cache list for research context."""
    return _cache.get("news", {}).get("data") or []


@app.get("/api/research/{asset}")
def api_research_asset(asset: str):
    """Groq deep analysis for a specific asset segment using live news feed."""
    try:
        from groq_research import research_asset
        return research_asset(asset.upper(), all_news=_get_news_cache())
    except Exception as e:
        return {"error": str(e), "asset": asset}


@app.post("/api/research/query")
def api_research_query(payload: dict = Body(...)):
    """Groq free-form market research query against live news feed."""
    try:
        from groq_research import research_query
        q = payload.get("query", "").strip()
        if not q:
            return {"error": "No query provided"}
        return research_query(q, all_news=_get_news_cache())
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/research")
def api_research_all():
    """Research all asset panels via Groq (cached 15 min per panel)."""
    try:
        from groq_research import get_cache_status
        return {"cache": get_cache_status()}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/crypto")
def api_crypto():
    try:
        from coingecko import get_crypto_snapshot
        return _cached("crypto", 300, get_crypto_snapshot)
    except Exception as e: return {"error": str(e)}


@app.get("/api/congress")
def api_congress():
    try:
        from capitol_trades import get_congress_trades
        return _cached("congress", 3600, get_congress_trades)
    except Exception as e: return {"error": str(e)}


@app.get("/api/whales")
def api_whales():
    try:
        from whale_tracker import get_whale_data
        return _cached("whales", 21600, get_whale_data)
    except Exception as e: return {"error": str(e)}


@app.get("/api/sectors")
def api_sectors():
    try:
        from sector_pulse import get_sector_pulse
        return _cached("sectors", 300, get_sector_pulse)
    except Exception as e: return {"error": str(e)}


@app.get("/api/vix")
def api_vix():
    try:
        from vix_term import get_vix_signals
        return _cached("vix", 300, get_vix_signals)
    except Exception as e: return {"error": str(e)}


@app.get("/api/article")
def api_article(url: str):
    """Fetch full article text from a news URL."""
    try:
        from news_fetch import fetch_article
        return fetch_article(url)
    except Exception as e:
        return {"error": str(e), "paywall": False}


@app.get("/api/all")
def api_all():
    return {
        "indices": api_indices_cached(),
        "macro":   api_macro(),
        "stocks":  api_stocks(),
        "news":    api_news(),
        "signal":  api_signal(),
        "time":    now_ist(),
    }


@app.post("/api/explain")
async def api_explain(request: Request):
    """ChatGPT-quality news analysis for the Read modal — HNI trader perspective."""
    import json as _json
    try:
        body    = await request.body()
        payload = _json.loads(body)
        title   = (payload.get("title")   or "").strip()
        summary = (payload.get("summary") or payload.get("content") or "").strip()
        if not title:
            return JSONResponse({"error": "no title"}, status_code=400)

        cache_key = f"explain:{title[:60]}"
        cached = _cache.get(cache_key)
        if cached:
            return JSONResponse(cached)

        prompt = f"""You are a senior HNI institutional trader with 20 years experience across NSE, global equities, gold, crude and FX.

A client just showed you this news headline:
HEADLINE: {title}
{f'CONTEXT: {summary[:300]}' if summary else ''}

Give a sharp, opinionated, ChatGPT-quality trading analysis in this EXACT format:

**WHAT HAPPENED**
2-3 sentences. State the facts clearly — what this news actually means, not just a restatement.

**WHY IT MATTERS**
Explain the macro chain reaction. Be specific: how does this move Fed expectations / RBI policy / DXY / INR / gold / equities / crude? What second-order effects will institutions price in over the next 72 hours?

**SMART MONEY POSITIONING**
What are hedge funds, FII, and institutions likely doing RIGHT NOW based on this? Where is the liquidity flowing? Who is buying, who is selling, and why?

**TRADE BIAS: [BUY / SELL / WAIT]**
Give a clear directional call. Which specific instrument (NIFTY / GOLD / USDINR / BANKNIFTY / crude)? Entry zone, stop loss, target. Timeframe.

**RISK TO THIS VIEW**
One key scenario that invalidates this trade. What data or event would flip the direction?

Be direct. Be opinionated. HNI clients pay for a clear POV, not hedged neutral commentary."""

        try:
            from groq_research import _call_groq_research
            text = _call_groq_research(prompt)
        except Exception:
            text = None

        if not text:
            return JSONResponse({"error": "groq_unavailable"}, status_code=503)

        result = {"analysis": text, "title": title}
        _cache[cache_key] = result
        return JSONResponse(result)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/hni-summary")
async def hni_summary_standalone(request: Request):
    """Standalone HNI regime analysis using Groq + live terminal data. No Docker required."""
    import requests as _rq

    # ── Fast path: memory cache (no lock) ─────────────────────
    cached = _hni_cache.get("data")
    if cached and (_time.time() - _hni_cache.get("ts", 0)) < HNI_CACHE_TTL:
        return JSONResponse(cached)

    # ── Slow path: deduplicated lock — only ONE Groq call ──────
    # All concurrent requests queue here; only the first calls Groq,
    # the rest exit the lock and hit the now-populated memory cache.
    async with _hni_lock:
        # Double-check inside lock — another request may have just filled it
        cached = _hni_cache.get("data")
        if cached and (_time.time() - _hni_cache.get("ts", 0)) < HNI_CACHE_TTL:
            return JSONResponse(cached)
        # Check file cache (survives Railway restarts)
        disk = _disk_load("hni_summary", HNI_CACHE_TTL)
        if disk:
            _hni_cache["data"] = disk
            _hni_cache["ts"]   = _time.time()
            return JSONResponse(disk)

    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        return JSONResponse({"error": "hni_unavailable", "detail": "No GROQ_API_KEY"}, status_code=503)

    try:
        # ── Gather live data from terminal caches ──────────────
        indices_raw = _bg_refresh("indices", 30, lambda: _lazy("indices", "get_indices"), empty=[])
        macro_raw   = _bg_refresh("macro",   30, lambda: _lazy("macro", "get_macro_data"), empty={})
        news_raw    = _bg_refresh("news",    30, _build_news, empty=[])

        # Format indices — api/indices returns a dict {NAME: {price, change, arrow}}
        idx_lines = []
        if isinstance(indices_raw, dict):
            for name, vals in list(indices_raw.items())[:10]:
                if isinstance(vals, dict):
                    price = vals.get("price", "")
                    chg   = vals.get("change", "")
                    idx_lines.append(f"{name}: {price} ({chg:+.2f}%)" if isinstance(chg, (int, float)) else f"{name}: {price}")
        elif isinstance(indices_raw, list):
            for ix in indices_raw[:8]:
                if isinstance(ix, dict):
                    name  = ix.get("name", ix.get("symbol", ""))
                    price = ix.get("price", ix.get("last", ""))
                    chg   = ix.get("change_pct", ix.get("change", ix.get("pct", "")))
                    if name:
                        idx_lines.append(f"{name}: {price} ({chg}%)")

        # Format macro — api/macro returns {fx:{...}, yields:{...}, oil, gold}
        macro = macro_raw if isinstance(macro_raw, dict) else {}
        fx     = macro.get("fx", macro.get("FX", {}))
        yields = macro.get("yields", macro.get("US_YIELDS", {}))
        oil    = macro.get("oil",  macro.get("OIL"))
        gold   = macro.get("gold", macro.get("GOLD_SPOT"))
        macro_lines = []
        if isinstance(fx, dict):
            for k, v in list(fx.items())[:5]:
                macro_lines.append(f"{k}: {v}")
        if isinstance(yields, dict):
            for k, v in list(yields.items())[:3]:
                macro_lines.append(f"{k}: {v}")
        if oil:  macro_lines.append(f"WTI Crude: {oil}")
        if gold: macro_lines.append(f"Gold: {gold}")

        # Format news headlines
        top_headlines = []
        for entry in (news_raw or [])[:20]:
            try:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    score, item = entry
                    if isinstance(item, dict) and score >= 4:
                        top_headlines.append(item.get("text", ""))
            except Exception:
                pass

        idx_block  = "\n".join(idx_lines)  or "Data loading..."
        macro_block = "\n".join(macro_lines) or "Data loading..."
        news_block  = "\n".join(f"• {h}" for h in top_headlines[:12] if h) or "No high-priority news."

        # Inject regime context into AI prompt
        regime_block = ""
        try:
            from regime import detect_market_regime, format_regime_for_prompt
            regime_data = detect_market_regime()
            regime_block = format_regime_for_prompt(regime_data)
        except Exception:
            regime_block = "Regime data unavailable."

        # Inject historical performance memory into AI prompt
        perf_block = ""
        try:
            import signal_memory as _sm
            perf_block = _sm.format_performance_for_prompt()
        except Exception:
            pass

        prompt = f"""You are a senior institutional trader and HNI advisor covering NSE/global markets.
Analyze the live data below and generate a complete market regime assessment.

IMPORTANT: The regime engine has already classified current conditions. Use this as your PRIMARY CONTEXT — your trade bias, confidence and hni_view must be CONSISTENT with the detected regime below.

=== MARKET REGIME ENGINE OUTPUT ===
{regime_block}

{perf_block}
=== LIVE INDICES ===
{idx_block}

=== MACRO DATA ===
{macro_block}

=== HIGH-PRIORITY NEWS FEED ===
{news_block}

Respond ONLY with a valid JSON object — no markdown, no explanation, just the JSON.
Use this EXACT structure:

{{
  "macro_regime": "one of: BULL_MOMENTUM | BEAR_PRESSURE | RISK_OFF | RISK_ON | SIDEWAYS | BREAKOUT | DISTRIBUTION | ACCUMULATION",
  "trade_bias": "BUY or SELL or WAIT",
  "confidence": <integer 0-100>,
  "hni_view": "<2-3 sentence opinionated trader view with specific levels and reasoning>",
  "instruments": [
    {{"name": "NIFTY50",    "signal": "BUY or SELL or WAIT", "rationale": "<20 words max>"}},
    {{"name": "BANKNIFTY",  "signal": "BUY or SELL or WAIT", "rationale": "<20 words max>"}},
    {{"name": "USDINR",     "signal": "BUY or SELL or WAIT", "rationale": "<20 words max>"}},
    {{"name": "GOLD",       "signal": "BUY or SELL or WAIT", "rationale": "<20 words max>"}},
    {{"name": "CRUDEOIL",   "signal": "BUY or SELL or WAIT", "rationale": "<20 words max>"}}
  ],
  "scalp_setup": {{
    "bias": "BUY or SELL or WAIT",
    "instrument": "<e.g. BANKNIFTY or NIFTY50>",
    "entry_zone": "<price range, e.g. 52200-52250>",
    "stop_loss": "<specific price>",
    "tp1": "<first target price>",
    "tp2": "<second target price>",
    "trigger_condition": "<what must happen for entry, e.g. break above 52250 with volume>"
  }},
  "swing_setup": {{
    "bias": "BUY or SELL or WAIT",
    "instrument": "<e.g. NIFTY50>",
    "entry_zone": "<price range>",
    "stop_loss": "<specific price>",
    "tp": "<swing target with timeframe, e.g. 24800 in 5-7 sessions>",
    "catalyst": "<what event or data will drive this move>"
  }}
}}"""

        resp = _rq.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "You are a professional institutional trader. Always respond with valid JSON only — no markdown fences, no explanation."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 900,
                "temperature": 0.2,
            },
            timeout=30,
        )

        if resp.status_code != 200:
            return JSONResponse({"error": "groq_error", "detail": resp.text[:200]}, status_code=503)

        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # Strip any accidental markdown fences
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()

        result = _json.loads(raw)

        # Save to memory + disk (disk survives Railway restarts)
        _hni_cache["data"] = result
        _hni_cache["ts"]   = _time.time()
        _disk_save("hni_summary", result)

        return JSONResponse(result)

    except _json.JSONDecodeError as e:
        return JSONResponse({"error": "json_parse_error", "detail": str(e)}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Morning Market Note ───────────────────────────────────────

async def _build_morning_note_data() -> dict:
    """Call Groq to generate morning market note. Returns structured dict."""
    import requests as _rq
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        return {"error": "no_groq_key"}

    indices_raw = _bg_refresh("indices", 30, lambda: _lazy("indices", "get_indices"), empty=[])
    macro_raw   = _bg_refresh("macro",   30, lambda: _lazy("macro", "get_macro_data"), empty={})
    news_raw    = _bg_refresh("news",    30, _build_news, empty=[])

    idx_lines, macro_lines, headlines = [], [], []
    if isinstance(indices_raw, dict):
        for name, vals in list(indices_raw.items())[:10]:
            if isinstance(vals, dict):
                price = vals.get("price", "")
                chg   = vals.get("change", "")
                idx_lines.append(f"{name}: {price} ({chg:+.2f}%)" if isinstance(chg, (int, float)) else f"{name}: {price}")
    macro = macro_raw if isinstance(macro_raw, dict) else {}
    for k, v in list(macro.get("fx", macro.get("FX", {})).items())[:4]:
        macro_lines.append(f"{k}: {v}")
    for k, v in list(macro.get("yields", macro.get("US_YIELDS", {})).items())[:3]:
        macro_lines.append(f"{k}: {v}")
    oil  = macro.get("oil",  macro.get("OIL"))
    gold = macro.get("gold", macro.get("GOLD_SPOT"))
    if oil:  macro_lines.append(f"WTI Crude: {oil}")
    if gold: macro_lines.append(f"Gold: {gold}")
    for entry in (news_raw or [])[:20]:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            score, item = entry
            if isinstance(item, dict) and score >= 5:
                headlines.append(item.get("text", ""))

    today_str = datetime.now(IST).strftime("%d %b %Y")
    prompt = f"""You are a senior NSE institutional trader writing the morning briefing for HNI clients.
Date: {today_str}  |  Market opens in 15 minutes.

LIVE INDICES:
{chr(10).join(idx_lines) or 'Loading...'}

MACRO DATA:
{chr(10).join(macro_lines) or 'Loading...'}

TOP OVERNIGHT NEWS:
{chr(10).join(f'• {h}' for h in headlines[:10] if h) or 'No major news.'}

Write a sharp morning note in this EXACT JSON format — no markdown, just JSON:
{{
  "date": "{today_str}",
  "headline": "<one bold market theme for today, max 12 words>",
  "global_cues": "<2-3 sentences on overnight US/Asia cues and impact on India>",
  "key_levels": {{
    "nifty":     {{"support": "<level>", "resistance": "<level>", "bias": "BUY or SELL or WAIT"}},
    "banknifty": {{"support": "<level>", "resistance": "<level>", "bias": "BUY or SELL or WAIT"}}
  }},
  "top_3_ideas": [
    {{"instrument": "<name>", "direction": "BUY or SELL", "rationale": "<20 words max>", "entry": "<level>", "sl": "<level>", "target": "<level>"}},
    {{"instrument": "<name>", "direction": "BUY or SELL", "rationale": "<20 words max>", "entry": "<level>", "sl": "<level>", "target": "<level>"}},
    {{"instrument": "<name>", "direction": "BUY or SELL", "rationale": "<20 words max>", "entry": "<level>", "sl": "<level>", "target": "<level>"}}
  ],
  "watch_out_for": "<key risk or event to watch today, 1 sentence>",
  "overall_bias": "BULLISH or BEARISH or NEUTRAL"
}}"""

    try:
        resp = _rq.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "You are a professional institutional trader. Respond with valid JSON only."},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens": 800, "temperature": 0.3,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return {"error": "groq_error"}
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
        data = _json.loads(raw)
        data["generated_at"] = datetime.now(IST).strftime("%I:%M %p IST")
        return data
    except Exception as e:
        return {"error": str(e)}


async def _morning_note_scheduler():
    """Background task: generates morning note at 9:15 AM IST every trading day."""
    print("[MORNING] scheduler started", flush=True)
    await asyncio.sleep(60)   # wait for server to warm up
    while True:
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        # Generate at 9:15 AM IST on weekdays (Mon=0 ... Fri=4)
        if now.weekday() < 5 and now.hour == 9 and now.minute >= 15:
            if _morning_note.get("date") != today:
                async with _morning_note_lock:
                    if _morning_note.get("date") != today:
                        print("[MORNING] generating morning note...", flush=True)
                        data = await _build_morning_note_data()
                        if "error" not in data:
                            _morning_note["date"] = today
                            _morning_note["data"] = data
                            _disk_save("morning_note", {"date": today, "data": data})
                            print(f"[MORNING] note ready: {data.get('headline','')}", flush=True)
        await asyncio.sleep(60)   # check every minute


@app.get("/api/morning-note")
async def api_morning_note():
    """Today's pre-generated morning market note (auto-generated at 9:15 AM IST)."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    # Serve from memory if today's note exists
    if _morning_note.get("date") == today and _morning_note.get("data"):
        return JSONResponse(_morning_note["data"])
    # On-demand generation
    async with _morning_note_lock:
        if _morning_note.get("date") == today and _morning_note.get("data"):
            return JSONResponse(_morning_note["data"])
        data = await _build_morning_note_data()
        err = data.get("error")
        if err:
            msg = ("GROQ_API_KEY not configured on Railway — add it in Railway Variables"
                   if err == "no_groq_key" else f"AI generation failed: {err}")
            return JSONResponse({"error": msg}, status_code=503)
        _morning_note["date"] = today
        _morning_note["data"] = data
        _disk_save("morning_note", {"date": today, "data": data})
        return JSONResponse(data)


# ── Catalyst Calendar ─────────────────────────────────────────

@app.get("/api/catalyst-calendar")
def api_catalyst_calendar():
    """Economic calendar from ForexFactory (real star-rated events) + India fixed events."""
    def _build():
        try:
            from econ_calendar import get_calendar
            return get_calendar(days_ahead=30)
        except Exception as e:
            print(f"[api] catalyst-calendar error: {e}", flush=True)
            return {"events": [], "total": 0, "source": "error", "generated_at": now_ist()}
    return _bg_refresh("catalyst_calendar", 1800, _build,
                       empty={"events": [], "total": 0, "source": "loading", "generated_at": now_ist()})


@app.get("/api/nse-earnings")
def api_nse_earnings(force: bool = False):
    """Real NSE/BSE quarterly results + upcoming earnings dates for Nifty50."""
    def _build():
        try:
            from nse_earnings import get_nse_earnings
            return get_nse_earnings(force=force)
        except Exception as e:
            print(f"[api] nse-earnings error: {e}", flush=True)
            return {"recent": [], "upcoming": [], "generated_at": now_ist(), "nse_ok": False}
    return _bg_refresh("nse_earnings", 1800, _build,
                       empty={"recent": [], "upcoming": [], "generated_at": now_ist(), "nse_ok": False})


# ── Sector Rotation Signal ────────────────────────────────────

@app.get("/api/sector-rotation")
def api_sector_rotation():
    """Hourly NSE sector momentum — Leading / Lagging / Reversing with strength score."""
    def _build():
        # Fetch sector price data with strict 8s timeout so we never block the HTTP response
        raw: dict = {}
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(__import__("sector_pulse").get_sector_pulse)
                try:
                    raw = _fut.result(timeout=8)
                except concurrent.futures.TimeoutError:
                    print("[sector_rotation] get_sector_pulse timed out — using news-only", flush=True)
        except Exception as _e:
            print(f"[sector_rotation] sector_pulse failed: {_e}", flush=True)

        # sectors_dict: {"IT": {"change_pct": x, "price": y}, ...} keyed by label
        sectors_dict = raw.get("sectors_dict", {}) if isinstance(raw, dict) else {}

        # News sentiment per sector
        news_raw = _cache.get("news", {}).get("data") or []
        sector_news: dict = {}
        sector_keywords = {
            "IT":      ["infosys", "tcs", "wipro", "hcl", "tech mahindra", "software", "it sector"],
            "BANKING": ["hdfc bank", "sbi", "icici", "kotak", "axis bank", "banking", "npa", "rbi"],
            "FMCG":    ["hindustan unilever", "itc", "nestle", "fmcg", "consumer", "dabur"],
            "AUTO":    ["maruti", "tata motors", "bajaj auto", "auto", "vehicle", "ev"],
            "PHARMA":  ["sun pharma", "cipla", "dr reddy", "pharma", "drug"],
            "METAL":   ["tata steel", "jsw", "hindalco", "metal", "steel", "copper"],
            "REALTY":  ["dlf", "godrej properties", "real estate", "realty"],
            "ENERGY":  ["reliance", "ongc", "oil", "crude", "gas", "power"],
        }
        for entry in (news_raw or [])[:60]:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            score, item = entry
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").lower()
            for sec, keywords in sector_keywords.items():
                if any(kw in text for kw in keywords):
                    sector_news.setdefault(sec, []).append(score)

        rotation_data = []
        sector_list = ["IT", "BANKING", "FMCG", "AUTO", "PHARMA", "METAL", "REALTY", "ENERGY"]
        for sec in sector_list:
            news_scores    = sector_news.get(sec, [])
            news_sentiment = round(sum(news_scores) / len(news_scores), 1) if news_scores else 5.0

            # Real price change from tvdatafeed via sector_pulse
            sec_data  = sectors_dict.get(sec, {})
            price_chg = float(sec_data.get("change_pct", 0) or 0)
            price_now = sec_data.get("price", 0)

            if price_chg > 0.5 and news_sentiment >= 6:
                status = "LEADING"
            elif price_chg < -0.5 and news_sentiment <= 4:
                status = "LAGGING"
            elif abs(price_chg) > 0.3:
                status = "REVERSING"
            else:
                status = "NEUTRAL"

            rotation_data.append({
                "sector":       sec,
                "status":       status,
                "price_change": round(price_chg, 2),
                "price":        price_now,
                "news_score":   news_sentiment,
                "news_count":   len(news_scores),
                "signal":       "BUY" if status == "LEADING" else "SELL" if status == "LAGGING" else "WATCH",
                "source":       "tvdatafeed" if sec_data else "news_only",
            })

        order = {"LEADING": 0, "REVERSING": 1, "NEUTRAL": 2, "LAGGING": 3}
        rotation_data.sort(key=lambda x: order.get(x["status"], 2))
        # Compute breadth from rotation data if tvdatafeed not available
        breadth = raw.get("breadth", "")
        if not breadth:
            leading = sum(1 for s in rotation_data if s["status"] == "LEADING")
            lagging = sum(1 for s in rotation_data if s["status"] == "LAGGING")
            if leading >= 5:      breadth = "BROAD RALLY"
            elif leading >= 3:    breadth = "BULLISH"
            elif lagging >= 5:    breadth = "BROAD SELL"
            elif lagging >= 3:    breadth = "BEARISH"
            elif leading > lagging: breadth = "MILD BULLISH"
            elif lagging > leading: breadth = "MILD BEARISH"
            else:                 breadth = "NEUTRAL"
        return {"sectors": rotation_data, "generated_at": now_ist(),
                "breadth": breadth, "nse_live": bool(sectors_dict)}

    # Return immediately with neutral sectors; background thread builds real data.
    # This prevents the tvdatafeed 429 delays from blocking the HTTP response.
    _NEUTRAL = {
        "sectors": [
            {"sector": sec, "status": "NEUTRAL", "price_change": 0.0, "price": 0,
             "news_score": 5.0, "news_count": 0, "signal": "WATCH", "source": "loading"}
            for sec in ["IT", "BANKING", "FMCG", "AUTO", "PHARMA", "METAL", "REALTY", "ENERGY"]
        ],
        "generated_at": now_ist(), "breadth": "LOADING", "nse_live": False
    }
    return _bg_refresh("sector_rotation", 60, _build, empty=_NEUTRAL)


# ── Live Prices — all asset classes in one call ───────────────────────────────

@app.get("/api/live-prices")
def api_live_prices(force: bool = False):
    """
    Unified live price feed: NSE indices, global indices, FX, bonds, commodities, crypto, VIX.
    Cached 15 seconds. Fast-path returns cached data immediately.
    """
    def _build():
        from live_prices import get_live_prices
        return get_live_prices(force=force)

    empty = {
        "indices": {}, "global": {}, "fx": {}, "bonds": {},
        "commodities": {}, "crypto": {}, "vix": {},
        "ts": now_ist(), "ts_epoch": _time.time(),
    }
    return _bg_refresh("live_prices", 15, _build, empty=empty)


@app.get("/api/live-ticker")
def api_live_ticker():
    """Flat ticker list for the scrolling price bar. Returns [{symbol, price, change, arrow, category}]"""
    def _build():
        from live_prices import get_ticker_items
        return get_ticker_items()
    return _bg_refresh("live_ticker", 15, lambda: _build(), empty=[])


@app.get("/api/stream")
async def api_stream():
    """
    Server-Sent Events stream — pushes live price updates every 15 seconds.
    Frontend connects once: const es = new EventSource('/api/stream');
    """
    import asyncio

    async def _gen():
        import json
        yield "retry: 15000\n\n"   # tell browser to reconnect after 15s if disconnected
        last_ts = 0
        while True:
            try:
                from live_prices import get_live_prices
                data = get_live_prices()
                ts   = data.get("ts_epoch", 0)
                if ts != last_ts:
                    last_ts = ts
                    payload = json.dumps({
                        "type": "prices",
                        "data": data,
                        "ts":   data.get("ts", ""),
                    })
                    yield f"data: {payload}\n\n"
                else:
                    yield ": heartbeat\n\n"
            except Exception as e:
                yield f"data: {{\"type\":\"error\",\"msg\":\"{str(e)[:60]}\"}}\n\n"
            await asyncio.sleep(15)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering":"no",
        },
    )


@app.get("/api/nse/live")
def api_nse_live(sector: str = None):
    """Live NSE stock prices via tvdatafeed. ?sector=IT|BANKING|FMCG|AUTO|PHARMA|METAL|REALTY|ENERGY"""
    def _build():
        try:
            from tvdata import get_nse_stocks
            sectors = [sector.upper()] if sector else None
            return get_nse_stocks(sectors)
        except Exception as e:
            return {"error": str(e)}
    return _bg_refresh(f"nse_live_{sector or 'all'}", 30, _build, empty={})


@app.get("/api/nse/price")
def api_nse_price(symbol: str, exchange: str = "NSE"):
    """Single symbol live price. ?symbol=TCS or ?symbol=NIFTY50&exchange=NSE"""
    try:
        from tvdata import get_price
        data = get_price(symbol.upper(), exchange.upper())
        if data:
            return {"symbol": symbol.upper(), "exchange": exchange.upper(), **data}
        return JSONResponse({"error": "not_found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/signal-memory/analytics")
def api_signal_analytics():
    """Signal accuracy analytics — win rate, regime breakdown, session breakdown."""
    def _build():
        try:
            import signal_memory as _sm
            return _sm.get_analytics()
        except Exception as e:
            return {"error": str(e)}
    return _bg_refresh("signal_analytics", 300, _build, empty={
        "total_signals": 0, "verified": 0, "pending": 0,
        "wins": 0, "losses": 0, "neutral": 0, "win_rate": 0.0,
        "avg_win_move": 0, "avg_loss_move": 0, "profit_factor": "—",
        "best_regime": "—", "best_regime_key": "", "best_regime_wr": 0,
        "worst_regime": "—", "worst_regime_key": "", "worst_regime_wr": 0,
        "top_asset": "—", "top_asset_avg_move": 0,
        "regime_breakdown": [], "signal_breakdown": [], "session_breakdown": [],
        "asset_breakdown": [], "quality_breakdown": [], "recent_signals": [],
        "generated_at": now_ist()
    })


@app.get("/api/signal-memory/regime-performance")
def api_regime_performance():
    """Per-regime win rates with confidence boost values."""
    try:
        import signal_memory as _sm
        return _sm.get_regime_performance()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/signal-store/status")
def api_signal_store_status():
    """Redis / SQLite storage health check."""
    try:
        from signal_store import storage_status
        return storage_status()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/signal-memory/history")
def api_signal_history(limit: int = 30, offset: int = 0):
    """Paginated raw signal history."""
    try:
        import signal_memory as _sm
        rows = _sm.get_history(limit=min(limit, 100), offset=offset)
        return {"rows": rows, "limit": limit, "offset": offset}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    # Middleware already checks auth — this is just the page serve
    path = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
    with open(path) as f:
        return f.read()
