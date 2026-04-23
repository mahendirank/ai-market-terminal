import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import threading
import time as _time

from datetime import datetime, timezone, timedelta

# Lazy import helpers — app starts even if a module has issues
_import_errors = {}

def _safe_import(name, fromlist):
    try:
        mod = __import__(name, fromlist=fromlist)
        return mod
    except Exception as e:
        _import_errors[name] = str(e)
        return None

_macro_mod    = _safe_import("macro",          ["get_macro_data","format_macro"])
_stocks_mod   = _safe_import("stocks",         ["get_mag7","get_semiconductors","get_india_indices","get_gold_etfs","detect_movers","format_stocks"])
_indices_mod  = _safe_import("indices",        ["get_indices"])
_econ_mod     = _safe_import("econ",           ["get_econ_data","get_economic_data"])
_news_mod     = _safe_import("news",           ["get_all_news","format_news","_detect_tickers"])
_priority_mod = _safe_import("priority",       ["prioritize_news"])
_signal_mod   = _safe_import("trade_signal",   ["generate_signal"])
_interp_mod   = _safe_import("interpreter",    ["interpret_macro"])
_smc_mod      = _safe_import("smc",            ["get_smc_analysis"])
_sniper_mod   = _safe_import("sniper",         ["sniper_entry"])
_mtf_mod      = _safe_import("mtf",            ["get_mtf_bias"])
_struct_mod   = _safe_import("structure",      ["get_structure"])
_earn_mod     = _safe_import("earnings",       ["get_earnings","NAMES","REGION_MAP","WATCH_LIST","SECTOR_MAP"])
_earn_soc_mod = _safe_import("earnings_social",["get_earnings_social"])
_src_mod      = _safe_import("sources_config", ["get_all_sources","approve","reject","add_pending"])
_nse_mod      = _safe_import("nse_data",       ["get_nse_snapshot","get_bulk_deals"])
_tgnews_mod   = _safe_import("telegram_news",  ["get_telegram_news"])

def _fn(mod, attr, fallback=None):
    if mod is None: return fallback or (lambda *a, **k: {})
    return getattr(mod, attr, fallback or (lambda *a, **k: {}))

get_macro_data      = _fn(_macro_mod,    "get_macro_data")
format_macro        = _fn(_macro_mod,    "format_macro",   lambda *a: "")
get_mag7            = _fn(_stocks_mod,   "get_mag7",       list)
get_semiconductors  = _fn(_stocks_mod,   "get_semiconductors", list)
get_india_indices   = _fn(_stocks_mod,   "get_india_indices",  list)
get_gold_etfs       = _fn(_stocks_mod,   "get_gold_etfs",      list)
detect_movers       = _fn(_stocks_mod,   "detect_movers",      list)
format_stocks       = _fn(_stocks_mod,   "format_stocks",  lambda: "")
get_indices         = _fn(_indices_mod,  "get_indices",    list)
get_econ_data       = _fn(_econ_mod,     "get_econ_data")
get_economic_data   = _fn(_econ_mod,     "get_economic_data",  list)
get_all_news        = _fn(_news_mod,     "get_all_news",   list)
format_news         = _fn(_news_mod,     "format_news",    lambda *a: "")
_detect_tickers     = _fn(_news_mod,     "_detect_tickers",    lambda *a: [])
prioritize_news     = _fn(_priority_mod, "prioritize_news",    lambda x,**k: x)
generate_signal     = _fn(_signal_mod,   "generate_signal")
interpret_macro     = _fn(_interp_mod,   "interpret_macro")
get_smc_analysis    = _fn(_smc_mod,      "get_smc_analysis")
sniper_entry        = _fn(_sniper_mod,   "sniper_entry")
get_mtf_bias        = _fn(_mtf_mod,      "get_mtf_bias")
get_structure       = _fn(_struct_mod,   "get_structure")
get_earnings        = _fn(_earn_mod,     "get_earnings",   list)
get_earnings_social = _fn(_earn_soc_mod, "get_earnings_social", list)
get_all_sources     = _fn(_src_mod,      "get_all_sources",     list)
approve             = _fn(_src_mod,      "approve")
reject              = _fn(_src_mod,      "reject")
add_pending         = _fn(_src_mod,      "add_pending")
get_nse_snapshot    = _fn(_nse_mod,      "get_nse_snapshot")
get_bulk_deals      = _fn(_nse_mod,      "get_bulk_deals",      list)
get_telegram_news   = _fn(_tgnews_mod,   "get_telegram_news",   list)

app = FastAPI(title="AI Market Terminal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

IST = timezone(timedelta(hours=5, minutes=30))

# ── In-memory cache with TTL ──────────────────────────────
_cache = {}
_cache_lock = threading.Lock()

def _cached(key, ttl_seconds, fn):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (_time.time() - entry["ts"]) < ttl_seconds:
            return entry["data"]
    data = fn()
    with _cache_lock:
        _cache[key] = {"data": data, "ts": _time.time()}
    return data

def _bg_refresh(key, ttl_seconds, fn, empty=None):
    """Return cached immediately. If no cache yet, trigger background fetch and return empty."""
    with _cache_lock:
        entry = _cache.get(key)
    if entry:
        if (_time.time() - entry["ts"]) > ttl_seconds:
            threading.Thread(target=_cached, args=(key, ttl_seconds, fn), daemon=True).start()
        return entry["data"]
    # No cache — start background fetch, return empty placeholder immediately
    threading.Thread(target=_cached, args=(key, ttl_seconds, fn), daemon=True).start()
    return empty if empty is not None else []


def now_ist():
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")


# ── Warm cache on startup ─────────────────────────────────
def _warm():
    # Stage 1: fast sources immediately (Telegram ~5s, indices, macro)
    import threading as _t
    def _fast():
        try: _cached("news",    30, _build_news_fast)
        except: pass
        try: _cached("indices", 30, get_indices)
        except: pass
        try: _cached("macro",   30, get_macro_data)
        except: pass
        try: _cached("stocks",  120, _build_stocks)
        except: pass
    # Stage 2: full RSS build — force overwrites fast cache
    def _full():
        import time
        time.sleep(3)  # let stage 1 complete first
        try:
            data = _build_news()
            with _cache_lock:
                _cache["news"] = {"data": data, "ts": _time.time()}
        except: pass
        try:
            from earnings_telegram import get_telegram_earnings
            get_telegram_earnings(force_refresh=True)
        except: pass
        try: _cached("earnings",   1800, get_earnings)   # 30-min TTL
        except: pass
        try: _cached("earn_social", 120, get_earnings_social)
        except: pass
        try: _cached("nse",         300, get_nse_snapshot)
        except: pass
    _t.Thread(target=_fast, daemon=True).start()
    _t.Thread(target=_full, daemon=True).start()

def _build_news_fast():
    tg = []
    try:
        items = get_telegram_news()
        for item in items:
            if isinstance(item, dict):
                if "category" not in item: item["category"] = "HNI"
                if not item.get("tickers"): item["tickers"] = _detect_tickers(item.get("text",""))
            tg.append(item)
    except: pass
    scored = prioritize_news(tg, summarize=False)
    return scored

def _build_news():
    items = get_all_news()
    return prioritize_news(items, summarize=False)

threading.Thread(target=_warm, daemon=True).start()


# ─── Endpoints ────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "import_errors": len(_import_errors)}

@app.get("/api/errors")
def api_errors():
    return {"import_errors": _import_errors}


@app.get("/api/macro")
def api_macro():
    data = _bg_refresh("macro", 30, get_macro_data, empty={})
    if not isinstance(data, dict): data = {}
    return {
        "fx":            data.get("FX", {}),
        "yields":        data.get("US_YIELDS", {}),
        "global_yields": data.get("GLOBAL_YIELDS", {}),
        "oil":           data.get("OIL"),
        "gold":          data.get("GOLD_SPOT"),
    }


def _build_stocks():
    return {
        "mag7":   get_mag7(),
        "semis":  get_semiconductors(),
        "india":  get_india_indices(),
        "etfs":   get_gold_etfs(),
        "movers": detect_movers(),
    }

@app.get("/api/stocks")
def api_stocks():
    return _bg_refresh("stocks", 120, _build_stocks, empty={})


@app.get("/api/econ")
def api_econ():
    data  = get_econ_data()
    econ  = get_economic_data()
    yc    = data.get("YIELD_CURVE", {})
    return {
        "us_economy":    data.get("US_ECONOMY", {}),
        "inflation":     data.get("INFLATION", {}),
        "global_growth": data.get("GLOBAL_GROWTH", {}),
        "yield_curve":   yc,
        "calendar":      econ[:10],
    }


@app.get("/api/news")
def api_news():
    scored = _bg_refresh("news", 30, _build_news)
    result = []
    for entry in scored:
        try:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                score, item = entry
            else:
                continue
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
            elif isinstance(item, str):
                result.append({"score": score, "priority": "low", "headline": item,
                               "source": "", "time": "", "pub_utc": "", "category": "MARKETS",
                               "summarized": False, "tickers": []})
        except Exception:
            pass
    return result


@app.get("/api/indices")
def api_indices_cached():
    return _bg_refresh("indices", 30, get_indices)


def _build_signal():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    macro_txt  = format_macro(get_macro_data())
    news_txt   = format_news(get_all_news())
    stocks_txt = format_stocks()
    econ       = get_economic_data()

    results = {}
    def _run(key, fn, *args):
        try: results[key] = fn(*args)
        except: results[key] = None

    tasks = [
        ("signal",    generate_signal,   macro_txt, news_txt, stocks_txt, econ),
        ("brain",     interpret_macro,   macro_txt, news_txt, stocks_txt, econ),
        ("smc",       get_smc_analysis),
        ("mtf",       get_mtf_bias),
        ("structure", get_structure),
    ]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(_run, t[0], t[1], *t[2:]): t[0] for t in tasks}
        for f in as_completed(futs, timeout=15):
            pass

    signal    = results.get("signal") or {}
    brain     = results.get("brain")  or {"insights": []}
    smc       = results.get("smc")    or {"bos": "", "order_block": "", "liquidity": ""}
    mtf       = results.get("mtf")    or {}
    structure = results.get("structure") or {"high":0,"low":0,"pivot":0,"r1":0,"s1":0,"fib":{}}

    sniper = {}
    try: sniper = sniper_entry(signal)
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
            "fib":   {k: round(float(v), 2) for k, v in structure.get("fib",{}).items()},
        },
        "timestamp": now_ist(),
    }

@app.get("/api/signal")
def api_signal():
    return _bg_refresh("signal", 300, _build_signal, empty={})


@app.get("/api/nse")
def api_nse():
    return _cached("nse", 300, get_nse_snapshot)


@app.get("/api/nse/bulk")
def api_bulk_deals():
    return _cached("bulk", 300, get_bulk_deals)


@app.get("/api/earnings")
def api_earnings():
    return _bg_refresh("earnings", 1800, get_earnings, empty=[])


@app.get("/api/earnings/live")
def api_earnings_live():
    try:
        from earnings_telegram import _cache_get_all
        tg = _cache_get_all()
    except Exception:
        return []
    NAMES      = getattr(_earn_mod, "NAMES",      {}) if _earn_mod else {}
    REGION_MAP = getattr(_earn_mod, "REGION_MAP", {}) if _earn_mod else {}
    WATCH_LIST = getattr(_earn_mod, "WATCH_LIST", {}) if _earn_mod else {}
    SECTOR_MAP = getattr(_earn_mod, "SECTOR_MAP", {}) if _earn_mod else {}
    results = []
    for ticker, d in tg.items():
        name = NAMES.get(ticker, NAMES.get(ticker+".NS", ticker))
        grp  = next((g for g, syms in WATCH_LIST.items()
                     if ticker in syms or ticker+".NS" in syms), "")
        region = d.get("region") or REGION_MAP.get(grp, "GLOBAL")
        score = d.get("score", 50)
        n = round(score / 20)
    results = []
    for ticker, d in tg.items():
        name = NAMES.get(ticker, NAMES.get(ticker+".NS", ticker))
        grp  = next((g for g, syms in WATCH_LIST.items()
                     if ticker in syms or ticker+".NS" in syms), "")
        region = d.get("region") or REGION_MAP.get(grp, "GLOBAL")
        score = d.get("score", 50)
        n = round(score / 20)
        results.append({
            "symbol": ticker, "name": name, "region": region,
            "group": grp, "sector": SECTOR_MAP.get(grp,""),
            "currency": "INR" if region=="INDIA" else "USD",
            "earn_date": d.get("quarter","—"),
            "eps_act": d.get("eps"), "eps_prev": None, "eps_yoy": d.get("yoy_growth"),
            "revenue": (f"₹{d['revenue_cr']/1000:.1f}K Cr" if d.get("revenue_cr") and d["revenue_cr"]>=1000
                       else f"₹{d['revenue_cr']:.0f} Cr" if d.get("revenue_cr")
                       else f"${d['revenue_b']:.2f}B" if d.get("revenue_b") else "—"),
            "rev_growth": None,
            "net_interest_income": (f"₹{d['pat_cr']/1000:.1f}K Cr" if d.get("pat_cr") and d["pat_cr"]>=1000
                                   else f"₹{d['pat_cr']:.0f} Cr" if d.get("pat_cr") else "—"),
            "gross_margin": None, "margin_bps": None, "net_margin": None, "nim_bps": None,
            "guidance": d.get("guidance","—") or "—",
            "beat_miss": d.get("beat_miss"),
            "commentary": d.get("commentary",""),
            "score": score, "stars": "★"*n+"☆"*(5-n),
            "data_source": "LIVE-TG", "price": None, "price_chg_pct": None,
        })
    results.sort(key=lambda x: -x["score"])
    return results


@app.get("/api/earnings/social")
def api_earnings_social():
    # Social data is fast (<25s) — fetch synchronously if no cache yet
    return _cached("earn_social", 120, get_earnings_social)


@app.get("/api/sources")
def api_sources():
    return get_all_sources()


@app.post("/api/sources/approve")
def api_approve(payload: dict = Body(...)):
    approve(payload["name"])
    return {"ok": True, "name": payload["name"], "status": "approved"}


@app.post("/api/sources/reject")
def api_reject(payload: dict = Body(...)):
    reject(payload["name"])
    return {"ok": True, "name": payload["name"], "status": "rejected"}


@app.post("/api/sources/add")
def api_add_source(payload: dict = Body(...)):
    add_pending(payload["name"], payload["url"],
                payload.get("category", "MARKETS"),
                payload.get("type", "telegram"))
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


if __name__ == "__main__":
    print("\n🚀 AI Market Terminal starting...\n")
    print("   Local:   http://localhost:8001")
    print("   API:     http://localhost:8001/api/all\n")
    uvicorn.run("dashboard_api:app", host="0.0.0.0", port=8001, reload=False)
