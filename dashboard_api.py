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

from macro import get_macro_data
from stocks import get_mag7, get_semiconductors, get_india_indices, get_gold_etfs, detect_movers
from indices import get_indices
from econ import get_econ_data, get_economic_data
from news import get_all_news
from priority import prioritize_news
from trade_signal import generate_signal
from interpreter import interpret_macro
from macro import format_macro
from news import format_news
from stocks import format_stocks
from smc import get_smc_analysis
from sniper import sniper_entry
from mtf import get_mtf_bias
from structure import get_structure
from earnings import get_earnings
from earnings_social import get_earnings_social
from sources_config import get_all_sources, approve, reject, add_pending
from nse_data import get_nse_snapshot, get_bulk_deals
from datetime import datetime, timezone, timedelta

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
    """Quick first-load: Telegram only (~5s)."""
    from telegram_news import get_telegram_news
    from news import _detect_tickers
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
    from news import get_all_news
    items = get_all_news()
    return prioritize_news(items, summarize=False)

threading.Thread(target=_warm, daemon=True).start()


# ─── Endpoints ────────────────────────────────────────────────

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
    """Fast endpoint — returns only LIVE-TG stocks from cache only (never triggers fresh fetch)."""
    from earnings_telegram import _cache_get_all
    from earnings import NAMES, REGION_MAP, WATCH_LIST, SECTOR_MAP
    tg = _cache_get_all()   # read-only SQLite cache, instant
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
