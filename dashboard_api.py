import sys
import os
import re
import json as _json
sys.path.insert(0, os.path.dirname(__file__))

from contextlib import asynccontextmanager
from fastapi import FastAPI, Body, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse, PlainTextResponse
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
    # Restore market-wide HNI from disk (per-symbol caches load lazily on request)
    saved_hni = _disk_load("hni_v3__market_", HNI_CACHE_TTL)
    if saved_hni:
        _hni_cache["_market_"] = {"data": saved_hni, "ts": _time.time()}
        print("[HNI] market-wide cache restored from disk", flush=True)
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
    # Grounded global morning report: warm caches + staggered refresh
    try:
        mreport_task = asyncio.create_task(_morning_report_scheduler())
        _bg_tasks.add(mreport_task)
        mreport_task.add_done_callback(_bg_tasks.discard)
    except Exception as _e:
        print(f"[MORNING_REPORT] init error: {_e}", flush=True)
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
    # Macro desk: snapshot every 15 minutes for historical memory
    try:
        macro_task = asyncio.create_task(_macro_desk_snapshot_loop())
        _bg_tasks.add(macro_task)
        macro_task.add_done_callback(_bg_tasks.discard)
    except Exception as _e:
        print(f"[MACRO_DESK] init error: {_e}", flush=True)
    # Explainer: scan tracked assets every 7 min, auto-generate move commentary
    try:
        expl_task = asyncio.create_task(_explainer_scan_loop())
        _bg_tasks.add(expl_task)
        expl_task.add_done_callback(_bg_tasks.discard)
    except Exception as _e:
        print(f"[EXPLAINER] init error: {_e}", flush=True)
    # Telegram alert engine: run every 3 min, cooldown-throttled
    try:
        alerts_task = asyncio.create_task(_alert_engine_loop())
        _bg_tasks.add(alerts_task)
        alerts_task.add_done_callback(_bg_tasks.discard)
    except Exception as _e:
        print(f"[ALERTS] init error: {_e}", flush=True)
    # Economic-calendar pre-print gate: fires event_bus on HIGH-impact prints
    # 5-15 min before they happen so morning_report + yield_watch caches
    # drop in time to serve fresh narratives the moment the print lands.
    try:
        econ_task = asyncio.create_task(_econ_publisher_loop())
        _bg_tasks.add(econ_task)
        econ_task.add_done_callback(_bg_tasks.discard)
    except Exception as _e:
        print(f"[ECON_PUB] init error: {_e}", flush=True)
    # WS price publisher: stream live prices every 2 seconds
    try:
        ws_task = asyncio.create_task(_price_publisher_loop())
        _bg_tasks.add(ws_task)
        ws_task.add_done_callback(_bg_tasks.discard)
    except Exception as _e:
        print(f"[WS_PRICES] init error: {_e}", flush=True)
    # HNI watchlist scanner: 90s scan for institutional-flow / pre-market hits
    try:
        hni_task = asyncio.create_task(_hni_watch_loop())
        _bg_tasks.add(hni_task)
        hni_task.add_done_callback(_bg_tasks.discard)
    except Exception as _e:
        print(f"[HNI_WATCH] init error: {_e}", flush=True)

    # ── Sprint 4 Stage 4.1: orchestrator lifecycle (feature-flagged) ──
    # When AGENT_ORCHESTRATOR_ENABLED is unset/false (default), this
    # block is a no-op: orchestrator + event_bus stay None and the
    # /api/agents, /api/circuits, /api/streams/health endpoints return
    # {"enabled": false, ...}. Zero behavior change in flags-off mode.
    app.state.orchestrator = None
    app.state.event_bus = None
    try:
        import logging as _logging  # local alias; avoid shadowing existing names
        # Lazy import: orchestration package is NOT loaded unless the flag is on.
        from orchestration.runtime import (
            build_event_bus,
            build_orchestrator,
            orchestrator_enabled,
        )
        if orchestrator_enabled():
            try:
                app.state.event_bus = await build_event_bus()
                app.state.orchestrator = await build_orchestrator()

                # ── Sprint 4 Stage 4.3: NewsFetchAgent (shadow / dual-run) ──
                # Default OFF. When AGENT_NEWS_FETCH_ENABLED=true:
                #   - Agent ticks at NEWS_FETCH_TICK_INTERVAL (default 120s)
                #   - Calls news.get_all_news() in a thread + emits news.raw event
                #   - Legacy pipeline UNCHANGED — agent is observational only
                #   - Failures swallowed by tick() → no cascade
                registered_agents = 0
                if os.environ.get("AGENT_NEWS_FETCH_ENABLED", "false").strip().lower() in (
                    "1", "true", "yes", "on",
                ):
                    try:
                        from orchestration.agents import NewsFetchAgent
                        nf_agent = NewsFetchAgent()
                        nf_agent.event_bus = app.state.event_bus
                        app.state.orchestrator.register(nf_agent)
                        await app.state.orchestrator.start_agent(nf_agent.name)
                        registered_agents += 1
                        _logging.getLogger("orchestration.lifespan").info(
                            "agent_registered_and_started",
                            extra={"agent": nf_agent.name, "version": nf_agent.version},
                        )
                    except Exception:
                        _logging.getLogger("orchestration.lifespan").exception(
                            "news_fetch_agent_registration_failed"
                        )
                        # Boot continues; orchestrator stays without this agent.

                # ── Sprint 4 Stage 4.4: SignalCriticAgent (OBSERVE-ONLY) ──
                # Default OFF. When AGENT_SIGNAL_CRITIC_ENABLED=true:
                #   - Agent consumes events:signal:candidate
                #   - Evaluates Schema + ConfidenceFloor + RecentBar critics
                #   - Logs verdict + emits signal.critique event (metadata only)
                #   - NEVER blocks or DLQs the original signal
                #   - Fail-open on chain exceptions
                #   - No producer for the candidate stream yet — agent ticks
                #     but processes 0 events in Sprint 4.4
                if os.environ.get("AGENT_SIGNAL_CRITIC_ENABLED", "false").strip().lower() in (
                    "1", "true", "yes", "on",
                ):
                    try:
                        from orchestration.agents import SignalCriticAgent
                        sc_agent = SignalCriticAgent()
                        sc_agent.event_bus = app.state.event_bus
                        app.state.orchestrator.register(sc_agent)
                        await app.state.orchestrator.start_agent(sc_agent.name)
                        registered_agents += 1
                        _logging.getLogger("orchestration.lifespan").info(
                            "agent_registered_and_started",
                            extra={"agent": sc_agent.name, "version": sc_agent.version},
                        )
                    except Exception:
                        _logging.getLogger("orchestration.lifespan").exception(
                            "signal_critic_agent_registration_failed"
                        )
                        # Boot continues; legacy + news.fetch unaffected.

                _logging.getLogger("orchestration.lifespan").info(
                    "orchestrator_lifespan_started",
                    extra={
                        "registered_agents": registered_agents,
                        "bus": type(app.state.event_bus).__name__,
                    },
                )
            except Exception:
                _logging.getLogger("orchestration.lifespan").exception(
                    "orchestrator_init_failed_falling_back_to_disabled"
                )
                # Reset state so endpoints report disabled cleanly.
                app.state.event_bus = None
                app.state.orchestrator = None
    except Exception:
        # Even the IMPORT of orchestration shouldn't crash boot.
        print("[ORCHESTRATOR] import failed; running without orchestration", flush=True)

    yield

    # ── Sprint 4 Stage 4.1: orchestrator shutdown ──
    _orch = getattr(app.state, "orchestrator", None)
    if _orch is not None:
        try:
            await _orch.stop_all(timeout=30.0)
            import logging as _logging
            _logging.getLogger("orchestration.lifespan").info("orchestrator_lifespan_stopped")
        except Exception:
            import logging as _logging
            _logging.getLogger("orchestration.lifespan").exception(
                "orchestrator_stop_failed"
            )


async def _signal_verify_loop():
    """Run 24h signal verification every hour."""
    await asyncio.sleep(3600)   # wait 1h after boot before first pass
    while True:
        try:
            import signal_memory as _sm
            await asyncio.to_thread(_sm.run_verification_pass)
        except Exception as _e:
            print(f"[SIGNAL_MEM] verify error: {_e}", flush=True)
        try:
            from production import heartbeat
            heartbeat("signal_verify")
        except Exception: pass
        await asyncio.sleep(3600)


async def _macro_desk_snapshot_loop():
    """Persist a macro regime snapshot every 15 min + publish to ws subscribers."""
    await asyncio.sleep(120)   # wait 2 min after boot for caches to warm
    while True:
        try:
            from macro_desk import get_macro_regime_view, store_snapshot
            from production import heartbeat
            view = await asyncio.to_thread(get_macro_regime_view)
            await asyncio.to_thread(store_snapshot, view)
            heartbeat("macro_desk_snap")
            try:
                from streaming import publish_macro_snapshot
                await publish_macro_snapshot(view)
            except Exception: pass
        except Exception as _e:
            print(f"[MACRO_DESK] snapshot error: {_e}", flush=True)
        await asyncio.sleep(900)   # 15 min


async def _price_publisher_loop():
    """Stream live price changes to ws subscribers every 2 seconds."""
    try:
        from streaming import PricePublisher
        publisher = PricePublisher(interval=2.0)
        await publisher.run()
    except Exception as _e:
        print(f"[WS_PRICES] publisher init error: {_e}", flush=True)


async def _explainer_scan_loop():
    """Scan tracked assets every 7 min, generate explanations + publish to ws."""
    await asyncio.sleep(180)   # wait 3 min after boot
    while True:
        try:
            from explainer import scan_and_explain, get_recent_explanations
            from production import heartbeat
            summary = await asyncio.to_thread(scan_and_explain, 3)
            heartbeat("explainer_scan")
            if summary.get("generated"):
                print(f"[EXPLAINER] generated for: {summary['generated']}", flush=True)
                # Publish each newly-generated explanation
                try:
                    from streaming import publish_explainer
                    recents = await asyncio.to_thread(get_recent_explanations, 5)
                    for asset_key in summary["generated"]:
                        match = next((r for r in recents if r.get("asset_key") == asset_key), None)
                        if match: await publish_explainer(match)
                except Exception: pass
        except Exception as _e:
            print(f"[EXPLAINER] loop error: {_e}", flush=True)
        await asyncio.sleep(420)   # 7 min


async def _alert_engine_loop():
    """Run all alert triggers every 3 min. Cooldown prevents spam.
    Publish each new alert to the ws 'alerts' channel."""
    await asyncio.sleep(240)   # wait 4 min after boot for caches to settle
    while True:
        try:
            from alert_engine import run_all_checks, get_alert_history
            from production import heartbeat
            summary = await asyncio.to_thread(run_all_checks, True)
            heartbeat("alert_engine")
            if summary.get("sent"):
                print(f"[ALERTS] sent={summary['sent']} cooldown={summary.get('in_cooldown',0)} candidates={summary.get('candidates',0)}", flush=True)
                try:
                    from streaming import publish_alert
                    recents = await asyncio.to_thread(get_alert_history, summary["sent"])
                    for ev in recents[:summary["sent"]]:
                        await publish_alert(ev)
                except Exception: pass
        except Exception as _e:
            print(f"[ALERTS] loop error: {_e}", flush=True)
        await asyncio.sleep(180)   # 3 min


async def _econ_publisher_loop():
    """Watch the FF economic calendar and publish HIGH-impact prints to
    event_bus 5-15 min BEFORE they fire. morning_report + yield_watch
    are subscribed and drop their caches on receipt, so the dashboard
    serves a fresh narrative the moment the print lands instead of a
    pre-event stale brief."""
    await asyncio.sleep(45)  # let event_bus listener attach first
    while True:
        try:
            from econ_publisher import scan_and_publish
            from production import heartbeat
            summary = await asyncio.to_thread(scan_and_publish)
            heartbeat("econ_publisher")
            if summary.get("published"):
                print(f"[ECON_PUB] published={summary['published']} "
                      f"checked={summary['checked']}", flush=True)
        except Exception as _e:
            print(f"[ECON_PUB] loop error: {_e}", flush=True)
        await asyncio.sleep(60)


async def _hni_watch_loop():
    """Scan the HNI feed for watchlist hits every 90s and fire instant alerts.
    Faster cadence than the 5-min watchdog so pre-market institutional flow
    (analyst initiations, big-fund buys) is caught within ~1.5 min of posting.
    Also triggers the once-daily US pre-market briefing inside the window."""
    await asyncio.sleep(90)   # let the news cache warm first
    while True:
        try:
            from hni_watch import scan_and_alert, is_premarket
            from production import heartbeat
            summary = await asyncio.to_thread(scan_and_alert)
            heartbeat("hni_watch")
            if summary.get("sent"):
                print(f"[HNI_WATCH] sent={summary['sent']} "
                      f"matched={summary.get('matched',0)} "
                      f"premarket={summary.get('premarket')}", flush=True)
            if is_premarket():
                from notify import send_premarket_briefing
                await asyncio.to_thread(send_premarket_briefing)
        except Exception as _e:
            print(f"[HNI_WATCH] loop error: {_e}", flush=True)
        await asyncio.sleep(90)


app = FastAPI(title="AI Market Terminal", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Auth middleware — gates every request except public paths ──────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse as _StarletteRedirect

_NO_AUTH_PATHS    = {"/health", "/login", "/logout", "/favicon.ico"}
_NO_AUTH_PREFIXES = ("/static/", "/api/live-ticker", "/ws", "/api/tenant/active", "/api/tenant/list", "/api/health")

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


# ── Rate-limit middleware (per-IP sliding window) ─────────────────────────────
# Limits write/expensive endpoints. Read-only ticks/news polls bypass.
_RL_WHITELIST_PREFIXES = ("/static/", "/health", "/api/health", "/login", "/logout", "/favicon.ico", "/ws")
_RL_HEAVY_PREFIXES     = ("/api/analyst/chat", "/api/explainer/generate", "/api/alerts/run-now")
_RL_DEFAULT = (120, 60)   # 120 req per 60s per IP for general endpoints
_RL_HEAVY   = (10,  60)   # 10 req per 60s per IP for AI-call endpoints


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in _RL_WHITELIST_PREFIXES):
            return await call_next(request)
        ip = (request.client.host if request.client else "unknown")
        cap, win = _RL_HEAVY if any(path.startswith(p) for p in _RL_HEAVY_PREFIXES) else _RL_DEFAULT
        try:
            from production import rate_limit_check
            allowed, remaining, reset_in = rate_limit_check(f"{ip}:{cap}:{win}", cap, win)
            if not allowed:
                return JSONResponse(
                    {"error": "rate_limited", "retry_after_secs": reset_in,
                     "limit": cap, "window_secs": win},
                    status_code=429,
                    headers={"Retry-After": str(reset_in), "X-RateLimit-Limit": str(cap)}
                )
        except Exception:
            pass  # fail open — never block on RL errors
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)

# Sprint 2 Phase A — Add RequestContextMiddleware LAST so it wraps
# everything else. The last-added middleware is outermost in FastAPI's
# stack, so request_id_var is set before Auth/RateLimit/CORS run.
from logging_middleware import RequestContextMiddleware
app.add_middleware(RequestContextMiddleware)

# Serve static files (images, icons, etc.) from /static/
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

IST = timezone(timedelta(hours=5, minutes=30))
_cache = {}
_cache_lock = threading.Lock()
_refresh_inflight: set = set()   # cache keys with a background build running
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

# ── HNI deduplicator lock + per-symbol cache state ─────────────
# _hni_cache maps cache_key (ticker slug or "_market_") → {data, ts}
# This replaces the previous global single-slot cache so different symbols
# don't clobber each other's analysis.
_hni_lock          = asyncio.Lock()


def _hni_cache_slug(ticker: str | None) -> str:
    """Filesystem + dict-key safe slug for the HNI per-symbol cache."""
    if not ticker:
        return "_market_"
    s = re.sub(r"[^A-Za-z0-9]+", "_", ticker)
    return s or "_market_"


def _hni_cache_get(slug: str) -> dict | None:
    entry = _hni_cache.get(slug)
    if entry and (_time.time() - entry.get("ts", 0)) < HNI_CACHE_TTL:
        return entry.get("data")
    return None


def _hni_cache_put(slug: str, data: dict) -> None:
    _hni_cache[slug] = {"data": data, "ts": _time.time()}
    try:
        _disk_save(f"hni_v3_{slug}", data)
    except Exception:
        pass


def _hni_cache_load_disk(slug: str) -> dict | None:
    return _disk_load(f"hni_v3_{slug}", HNI_CACHE_TTL)
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


def _spawn_refresh(key, ttl, fn):
    """Kick off a background _cached() build for `key` — but only if one
    isn't already running. Without this guard a burst of cold-cache
    requests (e.g. the dashboard's 8s cold-start retry on /api/signal)
    each spawn their own build, piling up many concurrent heavy builds
    that thrash shared upstream data sources and slow each other down."""
    with _cache_lock:
        if key in _refresh_inflight:
            return
        _refresh_inflight.add(key)
    def _job():
        try:
            _cached(key, ttl, fn)
        except Exception as e:
            # Surface background build failures — without this they die silently in
            # the daemon thread and the endpoint just serves an empty/stale payload.
            print(f"[bg_refresh] build failed for {key!r}: {type(e).__name__}: {e}", flush=True)
        finally:
            with _cache_lock:
                _refresh_inflight.discard(key)
    threading.Thread(target=_job, daemon=True).start()


def _bg_refresh(key, ttl, fn, empty=None):
    with _cache_lock:
        entry = _cache.get(key)
    if entry:
        if (_time.time() - entry["ts"]) > ttl:
            _spawn_refresh(key, ttl, fn)
        return entry["data"]
    _spawn_refresh(key, ttl, fn)
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
        try:
            from production import heartbeat
            heartbeat("continuous_refresh")
        except Exception: pass
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

    # Warm the AI sidebar caches FIRST — they feed the right-hand column
    # the user watches, so prioritising them ahead of the heavier
    # earnings/nse modules keeps the cold-start window short (panels show
    # "—" / "Loading..." for a few seconds rather than ~40s+).
    # Ordering: _build_signal is self-contained; _build_ai_news reads the
    # already-warm "news" cache, and _build_decisions reads "ai_news" —
    # so signal can go anywhere but ai_news must precede decisions.
    _time.sleep(3)
    try: _cached("signal",   300,  _build_signal)
    except: pass
    _time.sleep(3)
    try: _cached("ai_news",   600,  _build_ai_news)
    except: pass
    _time.sleep(3)
    try: _cached("decisions", 300,  _build_decisions)
    except: pass

    # Heavier panels (commodities/ETFs, earnings tables, NSE) warm after —
    # they have long TTLs and their own frontend retry, so a later warm
    # is fine.
    _time.sleep(3)
    try: _cached("stocks",   120,  _build_stocks)
    except: pass
    _time.sleep(3)
    try: _cached("earnings", 1800, _build_earnings)
    except: pass
    _time.sleep(3)
    try: _cached("nse",      300,  _build_nse)
    except: pass

    # Pre-warm the panels converted from synchronous _cached to _bg_refresh, so the
    # first poll after boot gets data instead of the empty placeholder. Call the
    # builder directly (NOT via _lazy) — _lazy swallows a transient failure into {}
    # which _cached would then pin for the whole TTL; letting it raise leaves the key
    # empty so the next poll retries, matching the endpoints' request-path semantics.
    for _key, _ttl, _mod, _fn in (
        ("forex",        30,  "forex",        "get_forex_intel"),
        ("macro_regime", 60,  "macro_desk",   "get_macro_regime_view"),
        ("sectors",      300, "sector_pulse", "get_sector_pulse"),
        ("vix",          300, "vix_term",     "get_vix_signals"),
    ):
        _time.sleep(3)
        try: _cached(_key, _ttl, getattr(__import__(_mod), _fn))
        except Exception: pass

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
        try:
            from production import heartbeat
            heartbeat("digest")
        except Exception: pass
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
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutTimeout, wait as _fwait
        from macro import get_macro_data, format_macro
        from news import get_all_news, format_news
        from stocks import format_stocks
        from econ import get_economic_data
        # Pre-fetch the four context inputs concurrently under a single hard
        # 25s budget — a hung data provider must not stall the whole build.
        # All four share the same 25s window (via futures.wait), so a slow
        # source can't starve the others; whatever hasn't finished degrades
        # to its fallback ("" / []). Pool is managed manually (never
        # shutdown(wait=True)) for the same reason as the task pool below.
        prefetch = ThreadPoolExecutor(max_workers=4)
        try:
            f_macro  = prefetch.submit(lambda: format_macro(get_macro_data()))
            f_news   = prefetch.submit(lambda: format_news(get_all_news()))
            f_stocks = prefetch.submit(format_stocks)
            f_econ   = prefetch.submit(get_economic_data)
            _fwait([f_macro, f_news, f_stocks, f_econ], timeout=25)
            def _grab(fut, label, fallback):
                if not fut.done():
                    print(f"[_build_signal] prefetch '{label}' timed out — using fallback", flush=True)
                    return fallback
                try:
                    return fut.result()
                except Exception as e:
                    print(f"[_build_signal] prefetch '{label}' failed: {type(e).__name__}", flush=True)
                    return fallback
            macro_txt  = _grab(f_macro,  "macro",  "")
            news_txt   = _grab(f_news,   "news",   "")
            stocks_txt = _grab(f_stocks, "stocks", "")
            econ       = _grab(f_econ,   "econ",   [])
        finally:
            prefetch.shutdown(wait=False, cancel_futures=True)

        results = {}
        def _run(key, mod, fn, *a):
            try:
                m = __import__(mod)
                results[key] = getattr(m, fn)(*a)
            except: results[key] = None

        # detect_market_regime() runs as a pool task too — it does its own
        # network I/O, so keeping it out of the pool (as a bare synchronous
        # call) would leave it unbounded and able to hang the whole build.
        tasks = [
            ("signal",    "trade_signal", "generate_signal",  macro_txt, news_txt, stocks_txt, econ),
            ("brain",     "interpreter",  "interpret_macro",   macro_txt, news_txt, stocks_txt, econ),
            ("smc",       "smc",          "get_smc_analysis"),
            ("mtf",       "mtf",          "get_mtf_bias"),
            ("structure", "structure",    "get_structure"),
            ("regime",    "regime",       "detect_market_regime"),
        ]
        # Run the 6 task functions concurrently with a hard 15s budget.
        # NOTE: a plain `with ThreadPoolExecutor() as pool` would block forever
        # on __exit__ — its shutdown(wait=True) waits on every submitted task,
        # so a single hung upstream call would hang the whole signal build (and
        # leave /api/signal returning {} indefinitely). Manage the pool manually
        # and shut down WITHOUT waiting. _run() already records None for any
        # task that doesn't finish, so partial results degrade gracefully.
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            futs = [pool.submit(_run, t[0], t[1], t[2], *t[3:]) for t in tasks]
            try:
                for f in as_completed(futs, timeout=15):
                    pass
            except FutTimeout:
                print("[_build_signal] task budget exceeded — using partial results", flush=True)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        regime_data = results.get("regime") or {}

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


# ── Sprint 4 Stage 4.1: orchestrator admin endpoints ──
# Read-only. Always available (handlers return {"enabled": false, ...}
# when AGENT_ORCHESTRATOR_ENABLED is off, which is the default).
#
# Auth: gated by the existing AuthMiddleware that protects /api/*.
@app.get("/api/agents")
async def api_agents():
    """List registered agents + their health. Sprint 4.1: orchestrator
    enabled but agents=0. Sprint 4.3+ will start to register real agents."""
    from orchestration.admin import agents_snapshot
    return await agents_snapshot(app)


@app.get("/api/circuits")
async def api_circuits():
    """Snapshot of every circuit breaker in the default_registry.
    Empty list until Sprint 4.5 wraps external calls."""
    from orchestration.admin import circuits_snapshot
    return await circuits_snapshot()


@app.get("/api/streams/health")
async def api_streams_health():
    """Length of each known Redis Stream. Reports -1 if the stream
    can't be queried (e.g. Redis unreachable). Sprint 4.1: streams
    return length=0 because no producers exist yet."""
    from orchestration.admin import streams_health_snapshot
    return await streams_health_snapshot(app)


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
                    "tags":       item.get("tags", []),
                    "summarized": item.get("summarized", False),
                    "tickers":    item.get("tickers", []),
                    "url":        item.get("url", ""),
                })
        except: pass
    return result


@app.get("/api/news/history")
def api_news_history(q: str = None, source: str = None, ticker: str = None,
                     category: str = None, hours: float = None, limit: int = 100):
    """Searchable archive of HNI/Telegram news — survives the live window.

    Examples:
      /api/news/history?ticker=SPCX
      /api/news/history?source=WalterBloomberg&hours=48
      /api/news/history?q=SpaceX
    """
    try:
        from hni_news_store import search, stats
        items = search(q=q, source=source, ticker=ticker, category=category,
                       since_hours=hours, limit=min(int(limit), 500))
        return {"count": len(items), "items": items, "archive": stats()}
    except Exception as e:
        return {"count": 0, "items": [], "error": str(e)}


@app.get("/api/hni-watch")
def api_hni_watch(hours: float = 48, limit: int = 150, country: str = None):
    """Classified HNI watchlist hits for the dashboard panel.

    Returns only items that hit the keyword/entity watchlist (the same ones
    that fire a Telegram alert), tagged high/medium, newest first.
    """
    try:
        from hni_news_store import search
        from hni_watch import classify, is_premarket, detect_countries, countries_meta
        # Scan ALL archived sources (HNI desks + country-relevant RSS), not just
        # the US-centric HNI category, so non-US country filters populate.
        rows = search(since_hours=hours, limit=1500)
        hits, seen = [], set()
        for it in rows:
            terms, prio = classify(it)
            if not prio:
                continue
            k = (it.get("text", "") or "")[:60].lower()
            if k in seen:
                continue
            seen.add(k)
            ccs = detect_countries(it)
            # Optional server-side country filter (?country=IN)
            if country and country.upper() not in ccs:
                continue
            hits.append({
                "text":      it.get("text", ""),
                "source":    it.get("source", ""),
                "time":      it.get("time", ""),
                "seen":      it.get("first_seen_ist", ""),
                "tickers":   it.get("tickers", []),
                "url":       it.get("url", ""),
                "priority":  prio,
                "matched":   list(dict.fromkeys(terms))[:5],
                "countries": ccs,
            })
            if len(hits) >= limit:
                break
        hits.sort(key=lambda h: 0 if h["priority"] == "high" else 1)
        return {"count": len(hits), "premarket": is_premarket(),
                "items": hits, "countries": countries_meta()}
    except Exception as e:
        return {"count": 0, "premarket": False, "items": [], "countries": [], "error": str(e)}


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
    return _bg_refresh("nse", 300, _build_nse, empty={})


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


@app.get("/api/forex")
def api_forex():
    """Live FX majors (EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, USD/CHF)
    with macro-inferred direction signals: BULLISH/BEARISH/NEUTRAL,
    confidence %, macro driver, volatility state."""
    try:
        from forex import get_forex_intel
        # _bg_refresh runs the build (incl. its on-path regime call) off the request
        # thread, so /api/forex returns instantly instead of blocking ~13s on a miss.
        return _bg_refresh("forex", 30, get_forex_intel, empty={})
    except Exception as e:
        print(f"[/api/forex] {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/macro-regime")
def api_macro_regime():
    """Institutional macro desk panel: 6 binary regime dimensions
    (Risk / Dollar / Fed / Yields / Inflation / Commodities), each with
    confidence + driver. Includes desk-style commentary and last 10 snapshots."""
    try:
        from macro_desk import get_macro_regime_view
        return _bg_refresh("macro_regime", 60, get_macro_regime_view, empty={})
    except Exception as e:
        print(f"[/api/macro-regime] {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/cb-calendar")
def api_cb_calendar(days: int = 90, limit: int = 12):
    """Next central-bank meetings for Fed, ECB, BOJ, BOE, RBA, SNB.
    Returns date/time (IST), expected volatility (RED/YELLOW/GREEN),
    previous decision, news-inferred expected bias, impacted assets."""
    try:
        from cb_calendar import get_cb_calendar
        return _cached(f"cb_cal_{days}_{limit}", 600,
                       lambda: get_cb_calendar(days_ahead=days, limit=limit))
    except Exception as e:
        print(f"[/api/cb-calendar] {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── AI Macro Analyst chat ─────────────────────────────────────────────────────
@app.post("/api/analyst/chat")
async def api_analyst_chat(request: Request):
    """Ask the AI macro analyst a question. Grounded on live prices, regime,
    FX, central bank calendar, and recent news. Maintains per-user chat history."""
    try:
        body = await request.json()
        question = str(body.get("question", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid request body"}, status_code=400)
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)
    # Per-user session_id derived from auth session cookie
    user = _auth.verify_session(request.cookies.get(COOKIE_NAME)) if request.cookies.get(COOKIE_NAME) else None
    session_id = f"u:{user['username']}" if user else f"anon:{request.client.host if request.client else 'unknown'}"
    try:
        from macro_analyst import ask_analyst
        return await asyncio.to_thread(ask_analyst, session_id, question)
    except Exception as e:
        print(f"[/api/analyst/chat] {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/analyst/history")
def api_analyst_history(request: Request, limit: int = 20):
    """Return the current user's analyst chat history (last N exchanges)."""
    user = _auth.verify_session(request.cookies.get(COOKIE_NAME)) if request.cookies.get(COOKIE_NAME) else None
    session_id = f"u:{user['username']}" if user else f"anon:{request.client.host if request.client else 'unknown'}"
    try:
        from macro_analyst import get_chat_history, storage_status
        return {
            "session_id":      session_id,
            "messages":        get_chat_history(session_id, limit=limit),
            "storage_status":  storage_status(),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/analyst/clear")
def api_analyst_clear(request: Request):
    """Clear current user's chat history."""
    user = _auth.verify_session(request.cookies.get(COOKIE_NAME)) if request.cookies.get(COOKIE_NAME) else None
    session_id = f"u:{user['username']}" if user else f"anon:{request.client.host if request.client else 'unknown'}"
    try:
        from macro_analyst import clear_chat_history
        return {"ok": clear_chat_history(session_id)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── "Why Did It Move?" explainer ─────────────────────────────────────────────
@app.get("/api/explainer/feed")
def api_explainer_feed(limit: int = 30, asset: str = ""):
    """Recent institutional move explanations, newest first. Optionally filter by asset."""
    try:
        from explainer import get_recent_explanations, get_tracked_assets
        return {
            "explanations":    get_recent_explanations(limit=limit, asset=asset or None),
            "tracked_assets":  get_tracked_assets(),
        }
    except Exception as e:
        print(f"[/api/explainer/feed] {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/explainer/generate")
async def api_explainer_generate(request: Request):
    """Generate a fresh explanation for a specific asset on demand.
    Body: {"asset": "GOLD" | "DXY" | "EURUSD" | ...}"""
    try:
        body = await request.json()
        asset_key = str(body.get("asset", "")).upper()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    try:
        from explainer import ASSETS, generate_explanation_for
        match = next((a for a in ASSETS if a["key"] == asset_key), None)
        if not match:
            return JSONResponse({"error": f"unknown asset: {asset_key}"}, status_code=400)
        res = await asyncio.to_thread(generate_explanation_for, match, True)   # force=True for on-demand
        if not res:
            return JSONResponse({"error": "could not generate (no AI response or no move data)"}, status_code=503)
        return res
    except Exception as e:
        print(f"[/api/explainer/generate] {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/explainer/scan")
async def api_explainer_scan():
    """Trigger a scan of all assets (admin / debug). Background does this every 7 min anyway."""
    try:
        from explainer import scan_and_explain
        return await asyncio.to_thread(scan_and_explain, 4)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Telegram Alert Engine ───────────────────────────────────────────────────
@app.get("/api/alerts/history")
def api_alerts_history(limit: int = 30):
    """Return last N alerts that fired (or were attempted)."""
    try:
        from alert_engine import get_alert_history, get_config
        return {"history": get_alert_history(limit=limit), "config": get_config()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/alerts/config")
def api_alerts_config():
    """Return user-configurable alert thresholds + status."""
    try:
        from alert_engine import get_config
        return get_config()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/alerts/config")
async def api_alerts_config_update(request: Request):
    """Update thresholds (in-process). Body: any subset of CFG keys."""
    try:
        body = await request.json()
        from alert_engine import update_config
        return update_config(body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/alerts/run-now")
async def api_alerts_run_now(emit: bool = True):
    """Force-run all checks. emit=true sends to Telegram (with cooldown)."""
    try:
        from alert_engine import run_all_checks
        return await asyncio.to_thread(run_all_checks, emit)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Charts (TradingView side context) ───────────────────────────────────────
@app.get("/api/chart-context")
def api_chart_context(asset: str = "GOLD"):
    """Per-asset side context for the charts panel: AI commentary, regime
    overlay, support/resistance, volatility, relevant CB events."""
    try:
        from chart_context import get_chart_context
        return _cached(f"chartctx_{asset.upper()}", 300, lambda: get_chart_context(asset.upper()))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/chart-assets")
def api_chart_assets():
    """List of chart-able assets with TradingView symbols."""
    try:
        from chart_context import get_chart_assets
        return {"assets": get_chart_assets()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── PRODUCTION: /api/health — comprehensive system status ───────────────────
@app.get("/api/health")
def api_health_full():
    """Full health probe across Redis, SQLite, Groq, Telegram, live data,
    news, regime, FX, and all background loops. Use for uptime monitoring."""
    try:
        from production import get_health
        h = get_health()
        # Set HTTP status: 200 healthy, 200 degraded, 503 unhealthy
        # (uptime monitors typically alert on non-2xx; degraded keeps them quiet)
        status_code = 200 if h.get("status") in ("healthy", "degraded") else 503
        return JSONResponse(content=h, status_code=status_code)
    except Exception as e:
        return JSONResponse({"status": "unhealthy", "error": str(e)}, status_code=503)


# ── MULTI-USER: per-user settings (watchlist, alert thresholds, telegram) ───
def _current_user(request: Request) -> dict | None:
    return _auth.verify_session(request.cookies.get(COOKIE_NAME)) if request.cookies.get(COOKIE_NAME) else None


@app.get("/api/me/settings")
def api_my_settings(request: Request):
    """Return current user's full settings (defaults overlaid with their overrides)."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    try:
        from user_settings import get_user_settings
        return {"username": user["username"], "settings": get_user_settings(user["username"])}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/me/settings")
async def api_update_my_settings(request: Request):
    """Merge updates into current user's settings. Body: {key: value, ...}"""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    try:
        body = await request.json()
        from user_settings import update_user_settings
        return {"username": user["username"], "settings": update_user_settings(user["username"], body)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/me/watchlist/add")
async def api_watchlist_add(request: Request):
    user = _current_user(request)
    if not user: return JSONResponse({"error": "not authenticated"}, status_code=401)
    try:
        body = await request.json()
        from user_settings import add_to_watchlist
        return {"watchlist": add_to_watchlist(user["username"], str(body.get("asset", "")))}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/me/watchlist/remove")
async def api_watchlist_remove(request: Request):
    user = _current_user(request)
    if not user: return JSONResponse({"error": "not authenticated"}, status_code=401)
    try:
        body = await request.json()
        from user_settings import remove_from_watchlist
        return {"watchlist": remove_from_watchlist(user["username"], str(body.get("asset", "")))}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── WebSocket streaming endpoint ──────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Live-stream channel: prices · alerts · macro · explainers.

    Client protocol (JSON):
      {"action":"subscribe","channels":["prices","alerts","macro","explainers"]}
      {"action":"unsubscribe","channels":["prices"]}
      {"action":"ping","client_ts_ms":1715518000000}

    Server messages:
      {"channel":"prices",     "payload":{"GOLD":{"price":..,"change":..}}, "server_ts_ms":...}
      {"channel":"alerts",     "payload":{"trigger_type":..,"title":..}, ...}
      {"channel":"macro",      "payload":{"commentary":..,"dominant_driver":..}, ...}
      {"channel":"explainers", "payload":{"asset_key":..,"what_moved":..}, ...}
      {"channel":"system",     "payload":{"type":"pong","client_ts_ms":..}}
    """
    from streaming import hub
    import json as _json
    await hub.connect(ws)
    try:
        # Welcome
        await ws.send_text(_json.dumps({
            "channel": "system",
            "payload": {"type": "welcome", "channels": list(["prices","alerts","macro","explainers"])},
            "server_ts_ms": int(_time.time() * 1000),
        }))
        while True:
            raw = await ws.receive_text()
            try:
                msg = _json.loads(raw)
            except Exception:
                continue
            action = msg.get("action")
            if action == "subscribe":
                granted = await hub.subscribe(ws, msg.get("channels", []))
                await ws.send_text(_json.dumps({
                    "channel": "system",
                    "payload": {"type": "subscribed", "channels": granted},
                    "server_ts_ms": int(_time.time() * 1000),
                }))
            elif action == "unsubscribe":
                await hub.unsubscribe(ws, msg.get("channels", []))
            elif action == "ping":
                await ws.send_text(_json.dumps({
                    "channel": "system",
                    "payload": {"type": "pong", "client_ts_ms": msg.get("client_ts_ms")},
                    "server_ts_ms": int(_time.time() * 1000),
                }))
    except WebSocketDisconnect:
        await hub.disconnect(ws)
    except Exception:
        await hub.disconnect(ws)


@app.get("/api/ws-stats")
def api_ws_stats():
    """How many clients are connected per channel."""
    try:
        from streaming import get_streaming_stats
        return get_streaming_stats()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── White-label / multi-tenant endpoints ────────────────────────────────────
TENANT_COOKIE = "terminal_tenant"


def _resolve_tenant_id(request: Request) -> str:
    """Priority: ?tenant=... → cookie → user setting → 'default'."""
    q = request.query_params.get("tenant")
    if q: return q
    c = request.cookies.get(TENANT_COOKIE)
    if c: return c
    user = _current_user(request)
    if user:
        try:
            from user_settings import get_user_settings
            return (get_user_settings(user["username"]).get("tenant_id") or "default")
        except Exception:
            pass
    return "default"


@app.get("/api/tenant/active")
def api_tenant_active(request: Request):
    """Return the active tenant config for this client + branding payload
    that the frontend uses to apply theme + module visibility."""
    try:
        from tenants import get_tenant
        tid = _resolve_tenant_id(request)
        t   = get_tenant(tid)
        # If tenant came from query, set the cookie so it persists
        resp = JSONResponse(t)
        if request.query_params.get("tenant"):
            resp.set_cookie(TENANT_COOKIE, tid, max_age=30 * 86400, samesite="lax", httponly=False)
        return resp
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/tenant/list")
def api_tenant_list():
    """List of available tenants for the switcher UI."""
    try:
        from tenants import list_tenants
        return {"tenants": list_tenants()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/tenant/switch")
async def api_tenant_switch(request: Request):
    """Set the tenant cookie for this client. Body: {'tenant_id': 'uae_forex'}."""
    try:
        body = await request.json()
        tid = str(body.get("tenant_id", "default"))
        from tenants import get_tenant
        t = get_tenant(tid)
        resp = JSONResponse({"ok": True, "tenant": t["id"], "name": t["name"]})
        resp.set_cookie(TENANT_COOKIE, tid, max_age=30 * 86400, samesite="lax", httponly=False)
        return resp
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/tenant/upsert")
async def api_tenant_upsert(request: Request):
    """Admin: create/update a custom tenant. Body must include 'id' + config fields."""
    user = _current_user(request)
    if not user or user.get("role") != "admin":
        return JSONResponse({"error": "admin required"}, status_code=403)
    try:
        body = await request.json()
        from tenants import upsert_custom_tenant
        return upsert_custom_tenant(str(body.get("id", "")), body)
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


@app.get("/api/yield-watch")
def api_yield_watch(force: bool = False):
    """Sovereign 10Y yields (US/JGB/Bund/Gilt/India) with a one-paragraph
    cross-asset narrative when any |Δ| >= 5bp. Cached 5 min normally,
    1 min when any yield is moving 10bp+."""
    def _build():
        try:
            from yield_watch import get_yield_watch
            return get_yield_watch(force=force)
        except Exception as e:
            return {"error": str(e), "yields": {}, "narrative": None}
    return _bg_refresh("yield_watch", 300, _build, empty={
        "yields": {}, "narrative": None, "big_movers": [],
        "any_breaking": False, "generated_at": 0,
    })


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


@app.get("/api/physical")
def api_physical():
    """Physical metals ETF vault flows (GLD/SLV tonnes-in-trust) — daily data."""
    try:
        from physical_metals import get_physical_metals
        return _cached("physical", 3600, get_physical_metals)
    except Exception as e:
        print(f"[/api/physical] {type(e).__name__}: {e}", flush=True)
        return {}


@app.get("/api/feeds/health")
def api_feeds_health():
    """Per-source RSS feed health from the in-process tracker (news.get_feed_health).
    suspect = request failed OR zero raw entries for >= 6 consecutive fetches."""
    try:
        from news import get_feed_health, RSS_SOURCES
        health = get_feed_health()
        suspects = sorted(
            [{"name": s, **h} for s, h in health.items() if h.get("suspect")],
            key=lambda x: -x.get("empty_streak", 0))
        return {
            "total": len(RSS_SOURCES),
            "tracked": len(health),
            "ok": sum(1 for h in health.values() if not h.get("suspect")),
            "suspects": suspects,
            "sources": health,
        }
    except Exception as e:
        print(f"[/api/feeds/health] {type(e).__name__}: {e}", flush=True)
        return {"total": 0, "tracked": 0, "ok": 0, "suspects": [], "sources": {}}


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
        return _bg_refresh("sectors", 300, get_sector_pulse, empty={})
    except Exception as e: return {"error": str(e)}


@app.get("/api/vix")
def api_vix():
    try:
        from vix_term import get_vix_signals
        return _bg_refresh("vix", 300, get_vix_signals, empty={})
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
    """HNI desk read. Per-symbol when `symbol` is provided in the JSON body,
    otherwise a market-wide multi-asset read.

    Body schema: ``{"symbol": "GOLD"}`` (preferred) or legacy ``{"context": "..."}``.

    Debug: pass ``?debug=1`` to get a ``_debug`` block in the response containing
    the request payload, resolved symbol, cache slug, cache hit/miss, prompt
    excerpt, Groq metadata, and timings. Server-side logs ``[HNI]`` lines for
    every request regardless of the debug flag.
    """
    import requests as _rq

    req_start_ms = int(_time.time() * 1000)
    debug_mode = request.query_params.get("debug", "").lower() in {"1", "true", "yes"}

    # ── Parse body: extract symbol (preferred) or legacy context ──────────
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_symbol = (body.get("symbol") or body.get("context") or "").strip()
    client_ip = (request.client.host if request.client else "?") or "?"
    print(f"[HNI] req ip={client_ip} body_keys={list(body.keys())} raw_symbol={raw_symbol!r} debug={debug_mode}", flush=True)

    # ── Resolve symbol if provided ────────────────────────────────────────
    resolved = None
    if raw_symbol:
        try:
            import symbol_resolver as _sr
            resolved = _sr.resolve(raw_symbol)
            if not resolved:
                suggestions = _sr.suggest(raw_symbol, limit=6)
                print(f"[HNI] resolve MISS raw={raw_symbol!r} suggestions={[s['ticker'] for s in suggestions]}", flush=True)
                return JSONResponse({
                    "error": f"Cannot resolve symbol '{raw_symbol}'",
                    "query": raw_symbol,
                    "suggestions": suggestions,
                }, status_code=400)
            print(f"[HNI] resolve OK raw={raw_symbol!r} -> ticker={resolved['ticker']} "
                  f"class={resolved['asset_class']} source={resolved.get('source','?')}", flush=True)
        except Exception as e:
            print(f"[HNI] resolver ERROR raw={raw_symbol!r}: {e}", flush=True)
            return JSONResponse({"error": "resolver_error", "detail": str(e)}, status_code=500)
    else:
        print(f"[HNI] no symbol provided -> market-wide read", flush=True)

    cache_slug = _hni_cache_slug(resolved["ticker"] if resolved else None)

    # Build the debug payload incrementally so we can attach it at the end
    _dbg: dict = {
        "request": {"raw_symbol": raw_symbol, "body_keys": list(body.keys()),
                    "client_ip": client_ip, "ts_ms": req_start_ms},
        "resolved": resolved,
        "cache_slug": cache_slug,
        "cache_hit": None,        # filled below
        "cache_source": None,     # "memory" | "disk" | None
        "groq": None,             # filled before/after Groq call
        "elapsed_ms": None,
    }

    # ── Fast path: per-symbol memory cache (no lock) ──────────────────────
    cached = _hni_cache_get(cache_slug)
    if cached:
        elapsed = int(_time.time() * 1000) - req_start_ms
        print(f"[HNI] cache HIT (memory) slug={cache_slug} elapsed_ms={elapsed}", flush=True)
        if debug_mode:
            _dbg.update(cache_hit=True, cache_source="memory", elapsed_ms=elapsed)
            out = dict(cached); out["_debug"] = _dbg
            return JSONResponse(out)
        return JSONResponse(cached)

    # ── Slow path: deduplicated lock — only ONE Groq call per slug ────────
    async with _hni_lock:
        cached = _hni_cache_get(cache_slug)
        if cached:
            elapsed = int(_time.time() * 1000) - req_start_ms
            print(f"[HNI] cache HIT (memory, post-lock) slug={cache_slug} elapsed_ms={elapsed}", flush=True)
            if debug_mode:
                _dbg.update(cache_hit=True, cache_source="memory", elapsed_ms=elapsed)
                out = dict(cached); out["_debug"] = _dbg
                return JSONResponse(out)
            return JSONResponse(cached)
        disk = _hni_cache_load_disk(cache_slug)
        if disk:
            _hni_cache_put(cache_slug, disk)
            elapsed = int(_time.time() * 1000) - req_start_ms
            print(f"[HNI] cache HIT (disk) slug={cache_slug} elapsed_ms={elapsed}", flush=True)
            if debug_mode:
                _dbg.update(cache_hit=True, cache_source="disk", elapsed_ms=elapsed)
                out = dict(disk); out["_debug"] = _dbg
                return JSONResponse(out)
            return JSONResponse(disk)

    print(f"[HNI] cache MISS slug={cache_slug} -> calling Groq", flush=True)
    _dbg["cache_hit"] = False

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

        # Inject desk persona context: recent calls + upcoming events
        # ── 3-layer prompt composition (Phase 3 — HNI canary migration) ──────
        # L1 SYSTEM PERSONA  ← ai_persona.SYSTEM_PERSONA via prompt_builder
        # L2 STATE BLOCK     ← market_intel.format_state_compact(snap)
        # L3 TASK BLOCK      ← ai_schemas.SCHEMA_HNI + per-call constraints
        # The composer owns layer assembly so no layer ever repeats another's
        # content. Tab-specific instructions stay parameters, not persona lines.

        from prompt_builder import build_messages, estimate_messages
        from ai_persona import (
            build_recent_calls_block, build_upcoming_events_block, contains_banned,
        )
        recent_calls_block = build_recent_calls_block(limit=5)
        upcoming_block     = build_upcoming_events_block(days=7)

        # Pull intel snapshot — same source as before, just rendered via L2
        # compact formatter inside prompt_builder.
        try:
            from market_intel import get_intel_snapshot
            intel_snap = get_intel_snapshot(symbol=(resolved or {}).get("display"))
        except Exception as _e:
            print(f"[HNI] market_intel unavailable ({_e}) — proceeding without state block", flush=True)
            intel_snap = None

        # ── OPT-IN MACRO REASONING PAYLOAD (Phase 6 limited integration) ───
        # Gated by env var ENABLE_MACRO_REASONING. Read at request time so a
        # flag flip takes effect without restart. When off (default), HNI
        # behaviour is identical to pre-P6 (no MACRO READ block, zero extra
        # tokens, zero engine call cost). When on, attaches the deterministic
        # Stage-5 reasoning as directional_intelligence — NOT execution.
        reasoning_payload = None
        macro_reasoning_enabled = (
            os.environ.get("ENABLE_MACRO_REASONING", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        if macro_reasoning_enabled and intel_snap is not None:
            try:
                from macro_reasoning_engine import analyze_stage5
                stage5 = analyze_stage5(intel_snap)
                reasoning_payload = stage5.get("trades")
                print(f"[HNI] macro_reasoning ON  scenario={reasoning_payload.get('scenario_name','?')} "
                      f"conf={reasoning_payload.get('overall_confidence','?')} "
                      f"intent={reasoning_payload.get('intent','?')}", flush=True)
            except Exception as _e:
                print(f"[HNI] macro_reasoning unavailable ({_e}) — proceeding without payload", flush=True)
                reasoning_payload = None
        elif not macro_reasoning_enabled:
            # Quiet log so production deploys can confirm gate is closed
            pass

        # Build per-call constraints. Composer adds the FOCUS + scalp/swing
        # instrument constraint automatically when focus_ticker is given;
        # we only add multi-asset coverage hint when no symbol is supplied.
        constraints: list[str] = []
        if not resolved:
            constraints.append(
                "MULTI-ASSET DESK READ — cover NIFTY50, BANKNIFTY, USDINR, "
                "GOLD, CRUDEOIL in the instruments[] array. Pick the cleanest "
                "scalp and swing across covered instruments."
            )
        constraints.append(
            "If signals conflict (e.g. regime bullish but sentiment tilt bearish), "
            "state CONVICTION=LOW and call out the conflict in hni_view."
        )
        # Anti-hallucination guard — only added when reasoning payload is
        # attached. Tells the model the MACRO READ block is context, not
        # an order spec. Prevents the LLM from echoing the bias/posture
        # values as if they were executable entries.
        if reasoning_payload is not None:
            constraints.append(
                "The MACRO READ block above is directional_intelligence — "
                "regime CONTEXT for your read. It is NOT an order, NOT entry "
                "signals, NOT position sizing. Do NOT copy its POSTURE / "
                "PREFERRED / WEAK lines verbatim into the schema fields. "
                "Treat scalp/intraday/swing bias as macro posture only. "
                "All scalp_setup / swing_setup price levels still belong to "
                "you to derive from the STATE block."
            )

        # Compose extra context from sources that aren't part of the snapshot:
        # signal_memory performance + last 5 desk calls + 7-day events.
        extra_blocks = [b for b in (perf_block, recent_calls_block, upcoming_block) if b]
        extra_context = "\n\n".join(extra_blocks) if extra_blocks else None

        messages = build_messages(
            task="hni",
            snap=intel_snap,
            reasoning=reasoning_payload,
            reasoning_mode="compact",
            symbol=raw_symbol or None,
            focus_display=(resolved or {}).get("display"),
            focus_ticker=(resolved or {}).get("ticker"),
            constraints=constraints,
            extra_context=extra_context,
            include_few_shots=False,   # turn on if persona-drift detected post-rollout
        )
        est = estimate_messages(messages)
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        print(f"[HNI] router REQ slug={cache_slug} task=hni msgs={len(messages)} "
              f"prompt_chars={prompt_chars} prompt_tokens~{est['total_tokens']} "
              f"(sys~{est['messages'][0]['tokens']}, user~{est['messages'][1]['tokens']}) "
              f"temp=0.15", flush=True)

        # Route via ai_router — automatic fallback chain (70b → 8b → ollama).
        from ai_router import chat as _ai_chat
        rr = _ai_chat(
            task="hni",
            messages=messages,
            temperature=0.15,
            max_tokens=1200,
            timeout=30,
        )

        if not rr.ok:
            print(f"[HNI] router FAIL slug={cache_slug} requested={rr.requested_model} "
                  f"err={rr.error!r}", flush=True)
            return JSONResponse({"error": "ai_router_failed", "detail": rr.error}, status_code=503)

        raw = rr.content.strip()
        print(f"[HNI] router OK slug={cache_slug} model={rr.model_key} "
              f"elapsed_ms={rr.latency_ms} tok={rr.prompt_tokens}/{rr.completion_tokens} "
              f"cost=${rr.estimated_cost_usd:.6f} fallback_depth={rr.fallback_depth} "
              f"raw_chars={len(raw)}", flush=True)

        _dbg["groq"] = {
            "requested_model": rr.requested_model,
            "served_by_model": rr.model_key,
            "provider":        rr.provider,
            "fallback_depth":  rr.fallback_depth,
            "msgs": len(messages),
            "prompt_chars": prompt_chars,
            "temperature": 0.15,
            "elapsed_ms": rr.latency_ms,
            "usage": {"prompt_tokens": rr.prompt_tokens,
                      "completion_tokens": rr.completion_tokens},
            "estimated_cost_usd": rr.estimated_cost_usd,
            "raw_chars": len(raw),
            # Excerpt only — full prompt is too long for routine logs but useful in debug mode
            "prompt_excerpt": (messages[1]["content"][:600] if len(messages) > 1 else "")
                              + ("…" if len(messages) > 1 and len(messages[1]["content"]) > 600 else ""),
        }
        # Phase-6 gate visibility — confirms whether MACRO READ was injected
        _dbg["macro_reasoning"] = {
            "enabled":    macro_reasoning_enabled,
            "attached":   reasoning_payload is not None,
            "mode":       "compact" if reasoning_payload is not None else None,
            "scenario":   (reasoning_payload or {}).get("scenario_name"),
            "confidence": (reasoning_payload or {}).get("overall_confidence"),
            "intent":     (reasoning_payload or {}).get("intent"),
        }

        # Strip any accidental markdown fences
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()

        result = _json.loads(raw)

        # Quality check — flag if the model fell back to chatbot defaults
        try:
            blob = _json.dumps(result, ensure_ascii=False)
            hits = contains_banned(blob)
            if hits:
                print(f"[HNI] persona drift — banned phrases detected: {hits}", flush=True)
                result["_persona_warnings"] = hits
        except Exception:
            pass

        # Attach symbol binding metadata so the frontend can render ACTIVE SYMBOL
        result["active_symbol"] = (resolved or {"ticker": "_market_", "display": "Market-wide read",
                                                 "asset_class": "market", "exchange": "—"})
        result["generated_at"]  = int(_time.time())
        result["cache_key"]     = cache_slug

        # Save to per-symbol memory + disk
        _hni_cache_put(cache_slug, result)

        # Final response log — confirms what's actually shipping to the client
        total_elapsed = int(_time.time() * 1000) - req_start_ms
        scalp_instr = (result.get("scalp_setup") or {}).get("instrument", "?")
        swing_instr = (result.get("swing_setup") or {}).get("instrument", "?")
        active_tk   = result["active_symbol"].get("ticker", "?")
        print(f"[HNI] RESPONSE active={active_tk} bias={result.get('trade_bias','?')} "
              f"conviction={result.get('conviction_tier','?')} "
              f"scalp.instr={scalp_instr!r} swing.instr={swing_instr!r} "
              f"warnings={len(result.get('warnings', []))} "
              f"elapsed_ms={total_elapsed}", flush=True)

        if debug_mode:
            _dbg["elapsed_ms"] = total_elapsed
            _dbg["persona_warnings"] = result.get("_persona_warnings", [])
            _dbg["response_shape"] = {
                "active_symbol": result["active_symbol"],
                "scalp_instrument": scalp_instr,
                "swing_instrument": swing_instr,
                "trade_bias": result.get("trade_bias"),
                "conviction_tier": result.get("conviction_tier"),
                "instrument_count": len(result.get("instruments", [])),
                "warning_count": len(result.get("warnings", [])),
            }
            out = dict(result); out["_debug"] = _dbg
            return JSONResponse(out)

        return JSONResponse(result)

    except _json.JSONDecodeError as e:
        return JSONResponse({"error": "json_parse_error", "detail": str(e)}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Morning Market Note ───────────────────────────────────────

def _build_morning_note_data() -> dict:
    """Call Groq to generate morning market note. Returns structured dict.

    Synchronous on purpose — the body does a blocking Groq HTTP call plus a
    few local reads and never awaits. Callers run it via asyncio.to_thread so
    it cannot freeze the event loop.
    """
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

    # 3-layer prompt composition (Morning Note cascade — matches HNI pattern).
    from prompt_builder import build_messages
    from ai_persona import (
        build_recent_calls_block, build_upcoming_events_block,
        contains_banned, attach_meta,
    )
    recent_calls_block = build_recent_calls_block(limit=5)
    upcoming_block     = build_upcoming_events_block(days=3)

    # Tab-specific morning-note context (live indices/macro/overnight news).
    # Lives in extra_context; the L3 schema (SCHEMA_MORNING_NOTE) is added
    # automatically by the composer.
    morning_context = "\n\n".join(p for p in (
        f"DATE: {today_str}  |  Market opens in 15 minutes.",
        "LIVE INDICES:\n" + ("\n".join(idx_lines) or "Loading..."),
        "MACRO DATA:\n" + ("\n".join(macro_lines) or "Loading..."),
        "TOP OVERNIGHT NEWS:\n" + ("\n".join(f"• {h}" for h in headlines[:10] if h) or "No major news."),
        recent_calls_block,
        upcoming_block,
    ) if p)

    try:
        messages = build_messages(
            task="morning_note",
            snap=None,                 # context already inlined above
            extra_context=morning_context,
            constraints=[f"Today's date in the response MUST be '{today_str}'."],
            include_few_shots=False,   # flip True if persona drift observed
        )
        resp = _rq.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": messages,
                # 1500 (was 1000) — headroom for 3 full trade ideas so the
                # JSON can't truncate mid-object.
                "max_tokens": 1500, "temperature": 0.2,
                # JSON mode — Groq then guarantees a parseable object, so the
                # response can't come back as prose/markdown (which caused
                # intermittent "Expecting value" json.loads failures). The
                # prompt already says "Return JSON" (prompt_builder.py).
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        if resp.status_code != 200:
            # Distinguish WHY — a 429 rate-limit, a 401 bad key, and a 5xx
            # outage need very different operator responses. Capture Groq's
            # own message and log it so the cause shows in the container
            # logs instead of collapsing to an opaque 503.
            try:
                groq_msg = ((resp.json() or {}).get("error") or {}).get("message", "")
            except Exception:
                groq_msg = (resp.text or "")[:300]
            kind = "groq_rate_limited" if resp.status_code == 429 else "groq_error"
            print(f"[MORNING] groq {resp.status_code} ({kind}): {groq_msg[:240]}",
                  flush=True)
            return {"error": kind, "groq_status": resp.status_code,
                    "groq_detail": groq_msg[:300],
                    "retry_after": resp.headers.get("retry-after")}
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
        data = _json.loads(raw)

        # Persona drift check
        try:
            drift = contains_banned(_json.dumps(data, ensure_ascii=False))
            if drift:
                print(f"[MORNING] persona drift — banned phrases: {drift}", flush=True)
        except Exception:
            drift = []

        # Standardized envelope (active_symbol, generated_at, cache_key, ...)
        data = attach_meta(data, tab="morning_note", persona_drift=drift)
        data["generated_at_label"] = datetime.now(IST).strftime("%I:%M %p IST")
        return data
    except Exception as e:
        return {"error": str(e)}


# Refresh the working market note every 20 min so it tracks the day in
# (near) real time instead of freezing the 9:15 AM snapshot. The note's DATA
# inputs (indices/macro/news) are already live on 30s caches; this refreshes
# the AI narrative on top so the headline + ideas stay current.
_MORNING_TTL = 20 * 60


def _morning_note_fresh(today: str) -> bool:
    return (
        _morning_note.get("date") == today
        and bool(_morning_note.get("data"))
        and (_time.time() - _morning_note.get("generated_at", 0)) < _MORNING_TTL
    )


async def _morning_note_scheduler():
    """Background task: (re)generate the working market note every ~20 min so it
    stays current through the trading day — not frozen at the 9:15 AM open."""
    print("[MORNING] scheduler started", flush=True)
    await asyncio.sleep(60)   # wait for server to warm up
    while True:
        try:
            today = datetime.now(IST).strftime("%Y-%m-%d")
            if not _morning_note_fresh(today):
                async with _morning_note_lock:
                    if not _morning_note_fresh(today):
                        print("[MORNING] (re)generating working note...", flush=True)
                        data = await asyncio.to_thread(_build_morning_note_data)
                        if "error" not in data:
                            _morning_note["date"] = today
                            _morning_note["data"] = data
                            _morning_note["generated_at"] = _time.time()
                            _disk_save("morning_note", {"date": today, "data": data,
                                                        "generated_at": _time.time()})
                            print(f"[MORNING] note refreshed: {data.get('headline','')}", flush=True)
                        else:
                            print(f"[MORNING] refresh skipped (error: {data.get('error')}) — keeping last note", flush=True)
        except Exception as e:
            print(f"[MORNING] scheduler error: {e}", flush=True)
        try:
            from production import heartbeat
            heartbeat("morning_note")
        except Exception: pass
        await asyncio.sleep(60)   # check every minute


@app.get("/api/morning-note")
async def api_morning_note(force: int = 0):
    """Today's pre-generated morning market note (auto-generated at 9:15 AM IST).

    Query params:
      force=1 — bypass the cache and regenerate the note now.
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    # Serve from memory if today's note is still fresh (< TTL). force=1 skips it.
    if not force and _morning_note_fresh(today):
        return JSONResponse(_morning_note["data"])
    # Stale or missing → regenerate (the lock caps concurrent LLM calls)
    async with _morning_note_lock:
        if not force and _morning_note_fresh(today):
            return JSONResponse(_morning_note["data"])
        data = await asyncio.to_thread(_build_morning_note_data)
        err = data.get("error")
        if err:
            if err == "no_groq_key":
                return JSONResponse(
                    {"error": "GROQ_API_KEY not configured — set it in core/.env"},
                    status_code=503)
            if err == "groq_rate_limited":
                # Rate limit is a distinct, self-resolving condition — say so,
                # return 429 (not 503), and pass Groq's Retry-After through.
                retry = data.get("retry_after")
                return JSONResponse(
                    {"error": "Groq rate limit reached — try again later",
                     "detail": data.get("groq_detail"),
                     "retry_after": retry},
                    status_code=429,
                    headers={"Retry-After": str(retry)} if retry else None)
            return JSONResponse(
                {"error": "AI generation failed",
                 "detail": data.get("groq_detail") or err,
                 "groq_status": data.get("groq_status")},
                status_code=503)
        _morning_note["date"] = today
        _morning_note["data"] = data
        _morning_note["generated_at"] = _time.time()
        _disk_save("morning_note", {"date": today, "data": data, "generated_at": _time.time()})
        return JSONResponse(data)


# ── Grounded Global Morning Report (8 markets, deterministic bias) ──────────

async def _morning_report_scheduler():
    """Keep the 8-market grounded report warm.

    build_global_report() reads per-market caches with staggered TTLs, so a
    plain (non-force) call only recomputes markets whose TTL lapsed. Running
    it every 20 min therefore gives a naturally staggered refresh without an
    8-index yfinance burst — and the endpoint always serves warm cache.
    """
    print("[MORNING_REPORT] scheduler started", flush=True)
    await asyncio.sleep(90)   # let the server warm up first
    while True:
        try:
            from morning_report import build_global_report
            rep = await asyncio.to_thread(build_global_report)
            ov = rep.get("global_overview", {})
            print(f"[MORNING_REPORT] warmed — tone={ov.get('tone')} "
                  f"{ov.get('bullish_markets')}B/{ov.get('bearish_markets')}S "
                  f"in {rep.get('computed_in_ms')}ms", flush=True)
        except Exception as e:
            print(f"[MORNING_REPORT] refresh error: {e}", flush=True)
        try:
            from production import heartbeat
            heartbeat("morning_report")
        except Exception: pass
        await asyncio.sleep(20 * 60)   # staggered TTLs decide what recomputes


@app.get("/api/morning-report")
async def api_morning_report(force: int = 0, market: str = "", narrate: int = 0):
    """Grounded global pre-market report for 8 markets.

    Directional bias is computed ONLY by deterministic engines; the LLM (if
    narrate=1 and ENABLE_MORNING_NARRATION is set) may explain but never
    reverse it. Serves from staggered Redis cache — warm calls are instant.

    Query params:
      force=1     — bypass cache, recompute everything
      market=XXX  — return a single market brief (CHINA/JAPAN/INDIA/...)
      narrate=1   — request LLM narration (still gated server-side)
    """
    try:
        import morning_report as _mr
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"morning_report module unavailable: {e}"},
                            status_code=503)

    if market:
        mk = market.strip().upper()
        if mk not in _mr.MARKETS:
            return JSONResponse(
                {"error": f"unknown market {mk!r}",
                 "valid": _mr.list_markets()}, status_code=400)
        brief = await asyncio.to_thread(_mr.build_market_brief, mk, force=bool(force))
        if narrate:
            try:
                brief["narrative"] = await asyncio.to_thread(_mr.narrate_brief, brief)
            except Exception:
                pass
        return JSONResponse(brief)

    report = await asyncio.to_thread(
        _mr.build_global_report, force=bool(force), narrate=bool(narrate))
    return JSONResponse(report)


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


@app.get("/api/market-state")
def api_market_state(force: bool = False):
    """Unified MARKET STATE snapshot — consolidates regime_engine,
    pressure_vector, yield_watch, live_prices, and market_memory into one
    flat payload for the dashboard card.

    Cached 30s normally; event_bus drops the cache on breaking events
    (see _market_state_event_bus_init below) so high-impact news refreshes
    the card immediately rather than waiting out the TTL.
    """
    def _build():
        try:
            from market_state_aggregator import get_market_state
            return get_market_state()
        except Exception as e:
            print(f"[api] market-state error: {type(e).__name__}: {e}", flush=True)
            return {"error": str(e)[:160], "data_quality": "DEGRADED",
                    "generated_at": now_ist()}
    return _bg_refresh("market_state", 30, _build, empty={
        "regime": {}, "pressure": {}, "yields": {}, "last_hour": {},
        "key_prices": {}, "ai_read": None, "data_quality": "LOADING",
        "generated_at": now_ist(),
    })


def _market_state_event_bus_init():
    """Subscribe the market_state cache to event_bus so HIGH-severity
    events (severity >= 7 — geopolitical, FOMC, NFP-grade prints) drop
    the cache and the next read recomputes against fresh intel."""
    try:
        from event_bus import subscribe as _bus_subscribe, start_listener as _bus_start

        def _on_breaking(ev: dict) -> None:
            try:
                with _cache_lock:
                    _cache.pop("market_state", None)
                print(f"[market_state] cache dropped on breaking event sev={ev.get('severity')}",
                      flush=True)
            except Exception:
                pass

        _bus_subscribe(_on_breaking)
        _bus_start()
    except Exception as _e:
        print(f"[market_state] event_bus init skipped: {_e}", flush=True)


_market_state_event_bus_init()


@app.get("/api/cockpit")
def api_cockpit(symbol: str = "GOLD"):
    """Trade Cockpit — HNI-desk scalp + swing entry/exit read for ``symbol``
    (gold by default). Fuses market-state, the economic-event gate, SMC scalp
    levels and the multi-timeframe consensus into a TRADE/WAIT/STAND-ASIDE
    verdict per mode. Background-refreshed (90s) like /api/market-state."""
    def _build():
        try:
            from cockpit_engine import get_cockpit
            return get_cockpit(symbol)
        except Exception as e:
            print(f"[api] cockpit error: {type(e).__name__}: {e}", flush=True)
            return {"error": str(e)[:160], "data_quality": "DEGRADED",
                    "generated_at": now_ist()}
    return _bg_refresh(f"cockpit_{symbol}", 90, _build, empty={
        "symbol": symbol, "context": {}, "event_gate": {},
        "scalp": {}, "swing": {}, "data_quality": "LOADING",
        "generated_at": now_ist(),
    })


@app.get("/api/calendar/imminent")
def api_calendar_imminent(pre: int = 30, post: int = 5):
    """Events firing within [-post, +pre] minutes from now. Powers the
    dashboard's 'FOMC IN 12 MIN' pre-print banner.

    Query params:
        pre  — lookahead window in minutes (default 30)
        post — look-back window in minutes (default 5, for showing 'PRINTED' results)
    """
    def _build():
        try:
            from econ_publisher import get_imminent_events, _severity_for
            events = get_imminent_events(window_pre_min=pre, window_post_min=post)
            # Enrich with severity so the frontend can colour-code without a second pass
            for ev in events:
                ev["severity"] = _severity_for(ev)
                ev["phase"]    = "PRE" if ev["delta_secs"] > 0 else "POST"
            return {"events": events, "total": len(events),
                    "pre_min": pre, "post_min": post,
                    "generated_at": now_ist()}
        except Exception as e:
            print(f"[api] calendar/imminent error: {e}", flush=True)
            return {"events": [], "total": 0, "error": str(e)[:120],
                    "generated_at": now_ist()}
    # 30s cache — much shorter than the parent calendar because "imminent"
    # is time-sensitive. Worth recomputing every poll cycle.
    return _bg_refresh("calendar_imminent", 30, _build,
                       empty={"events": [], "total": 0, "generated_at": now_ist()})


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
                # get_live_prices() is synchronous (yfinance). Offload it so a
                # slow fetch can't freeze the event loop for every SSE client.
                data = await asyncio.to_thread(get_live_prices)
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


# ─── INDICATORS PANEL ────────────────────────────────────────────────────────
# Modular routes backed by indicators.py + symbol_resolver.py.
# Single-timeframe, multi-TF consensus, autocomplete, and sentiment placeholder.

@app.get("/api/indicators/meta")
def api_indicators_meta():
    """Static metadata used by the UI (timeframes + indicator list)."""
    try:
        import indicators as _ind
        return {
            "timeframes": _ind.list_timeframes(),
            "indicators": _ind.list_indicators(),
            "tf_weights": _ind.TF_WEIGHTS,
            "indicator_weights": _ind.INDICATOR_WEIGHTS,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/indicators/resolve")
def api_indicators_resolve(q: str = ""):
    """Autocomplete: returns ranked candidate symbols for the search box."""
    try:
        import symbol_resolver as _sr
        results = _sr.search(q, limit=12)
        return {"query": q, "results": results}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/indicators/{symbol}")
def api_indicators_single(symbol: str, tf: str = "1d"):
    """Compute all 12 indicators + composite for one timeframe."""
    try:
        import indicators as _ind
        import symbol_resolver as _sr
        rec = _sr.resolve(symbol)
        if not rec:
            suggestions = _sr.suggest(symbol, limit=6)
            return JSONResponse({
                "error": f"Cannot resolve '{symbol}'",
                "query": symbol,
                "suggestions": suggestions,
            }, status_code=404)
        result = _ind.compute_indicators(rec["ticker"], tf)
        if result is None:
            return JSONResponse({
                "error": f"No OHLC data for {rec['ticker']} ({tf})",
                "query": symbol,
                "resolved": rec,
                "suggestions": _sr.suggest(symbol, limit=6),
            }, status_code=404)
        result["resolved"] = rec
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/indicators/{symbol}/consensus")
def api_indicators_consensus(symbol: str):
    """All 4 timeframes + composite + sentiment overlay (multi-TF view)."""
    try:
        import indicators as _ind
        import symbol_resolver as _sr
        rec = _sr.resolve(symbol)
        if not rec:
            suggestions = _sr.suggest(symbol, limit=6)
            return JSONResponse({
                "error": f"Cannot resolve '{symbol}'",
                "query": symbol,
                "suggestions": suggestions,
            }, status_code=404)
        result = _ind.compute_consensus(rec["ticker"], rec["asset_class"])
        if result.get("error"):
            result["query"] = symbol
            result["resolved"] = rec
            result["suggestions"] = _sr.suggest(symbol, limit=6)
            return JSONResponse(result, status_code=404)
        result["resolved"] = rec
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/indicators/{symbol}/sentiment")
def api_indicators_sentiment(symbol: str):
    """News/AI sentiment for a symbol (currently neutral stub, swap provider later)."""
    try:
        import symbol_resolver as _sr
        from sentiment_provider import get_sentiment
        rec = _sr.resolve(symbol)
        if not rec:
            return JSONResponse({"error": f"Cannot resolve symbol '{symbol}'"}, status_code=404)
        s = dict(get_sentiment(rec["ticker"], rec["asset_class"]))
        s["resolved"] = rec
        return s
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── MARKET INTELLIGENCE ─────────────────────────────────────────────────
# Unified structured snapshot AI tabs reason from. See market_intel.py.

@app.get("/api/intel/snapshot")
def api_intel_snapshot(symbol: str = "", clusters: int = 20, force: bool = False):
    """Full market intel snapshot — regime + correlations + F&G + clusters +
    per-asset sentiment + upcoming events. Optional `symbol=...` adds a focus
    block with related clusters."""
    try:
        from market_intel import get_intel_snapshot
        sym = symbol.strip() or None
        return get_intel_snapshot(symbol=sym, max_clusters=max(5, min(int(clusters), 50)),
                                  force=bool(force))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/intel/clusters")
def api_intel_clusters(limit: int = 20):
    """Just the clustered news view — useful for the dashboard's news panel
    or quick scans without pulling the full snapshot."""
    try:
        from market_intel import get_intel_snapshot
        snap = get_intel_snapshot(max_clusters=max(5, min(int(limit), 50)))
        return {
            "clusters": snap.get("news", {}).get("clusters", []),
            "stats":    snap.get("news", {}).get("stats", {}),
            "ts":       snap.get("ts"),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/intel/prompt")
def api_intel_prompt(symbol: str = "", clusters: int = 10):
    """Returns the formatted prompt block AI tabs paste into their context.
    Useful for verifying what the AI actually sees."""
    try:
        from market_intel import get_intel_snapshot, format_intel_for_prompt
        sym = symbol.strip() or None
        snap = get_intel_snapshot(symbol=sym)
        block = format_intel_for_prompt(snap, include_clusters=max(1, min(int(clusters), 30)))
        return PlainTextResponse(block)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── AI ROUTER STATS ──────────────────────────────────────────────────────
# Aggregate latency / token / cost metrics from ai_calls.db. Use for capacity
# planning, cost monitoring, and verifying fallback behaviour in production.

@app.get("/api/ai/stats")
def api_ai_stats(hours: int = 24):
    """Per-model + per-task latency, success rate, token usage, and cost
    estimate over the last ``hours`` (default 24)."""
    try:
        from ai_router import stats as _stats
        return _stats(hours=max(1, min(int(hours), 168)))   # clamp 1h..7d
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/ai/health")
def api_ai_health():
    """Which providers are reachable + currently-configured task routes."""
    try:
        from ai_router import healthcheck
        return healthcheck()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    # Middleware already checks auth — this is just the page serve
    path = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
    with open(path) as f:
        return f.read()
