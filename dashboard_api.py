import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, Body
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import threading
import time as _time
from datetime import datetime, timezone, timedelta

app = FastAPI(title="AI Market Terminal")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

IST = timezone(timedelta(hours=5, minutes=30))
_cache = {}
_cache_lock = threading.Lock()
_startup_done = False


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

# ── Background warm-up — gentle sequential loading ────────────
def _warm():
    # On Railway: load one module at a time with long gaps to stay under 512MB RAM
    # Locally: load everything quickly
    gap = 8 if ON_RAILWAY else 2

    _time.sleep(gap)
    try: _cached("macro",   30,  lambda: _lazy("macro",   "get_macro_data"))
    except: pass

    _time.sleep(gap)
    try: _cached("indices", 30,  lambda: _lazy("indices", "get_indices"))
    except: pass

    _time.sleep(gap)
    try: _cached("news",    30,  _build_news)
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
        return prioritize_news(get_all_news(), summarize=False)
    except: return []

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
    except: return {}

def _build_earnings():
    try:
        from earnings import get_earnings
        return get_earnings()
    except: return []

def _build_nse():
    try:
        from nse_data import get_nse_snapshot
        return get_nse_snapshot()
    except: return {}

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

        return {
            "signal":    signal,
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
    except Exception as e:
        return {"error": str(e), "timestamp": now_ist()}


threading.Thread(target=_warm, daemon=True).start()


# ── Endpoints ─────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


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
    except: return {}


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
                })
        except: pass
    return result


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
    except: return []


@app.get("/api/earnings")
def api_earnings():
    return _bg_refresh("earnings", 1800, _build_earnings, empty=[])


@app.get("/api/earnings/live")
def api_earnings_live():
    try:
        from earnings_telegram import _cache_get_all
        tg = _cache_get_all()
    except: return []
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
    except: return []


@app.get("/api/sources")
def api_sources():
    try:
        from sources_config import get_all_sources
        return get_all_sources()
    except: return []


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


@app.get("/", response_class=HTMLResponse)
def dashboard():
    path = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
    with open(path) as f:
        return f.read()
