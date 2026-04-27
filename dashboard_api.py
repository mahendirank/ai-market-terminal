import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from contextlib import asynccontextmanager
from fastapi import FastAPI, Body
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import threading
import time as _time
from datetime import datetime, timezone, timedelta


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start all background tasks when server boots."""
    threading.Thread(target=_warm,               daemon=True).start()
    threading.Thread(target=_continuous_refresh, daemon=True).start()
    asyncio.create_task(_async_digest_loop())          # asyncio task — reliable on Railway
    try:
        from notify import start_watchdog
        start_watchdog()
    except Exception:
        pass
    yield   # server runs here


app = FastAPI(title="AI Market Terminal", lifespan=lifespan)
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
    except: return []


# ── Shared Telegram sender (used by loop + test endpoint) ─────
_TG_BOT   = os.environ.get("TELEGRAM_BOT_TOKEN", "8475057388:AAGUlt5Qu3Ei2_3xeUF8S1TWvygDKVVxb8I")
_TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",   "-1001379475837")

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
_digest_state = {"count": 0, "last_sent": None, "last_ok": None, "running": False}

def _run_one_digest_cycle():
    """Blocking work for one digest cycle — runs in thread pool via asyncio.to_thread."""
    global _digest_state
    cycle_num = _digest_state["count"] + 1
    ist_t     = datetime.now(IST).strftime("%H:%M IST")
    print(f"[DIGEST] ── cycle #{cycle_num} @ {ist_t} ──", flush=True)

    try:
        # Step 1 — read dashboard cache (zero extra cost, already fetched for UI)
        scored = _cache.get("news", {}).get("data") or []
        print(f"[DIGEST] cache: {len(scored)} items", flush=True)

        # Step 2 — if cache cold, quick 5-feed fallback
        if not scored:
            print("[DIGEST] cache empty → fallback fetch", flush=True)
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
                print(f"[DIGEST] fallback: {len(scored)} items", flush=True)
            except Exception as fe:
                print(f"[DIGEST] fallback error: {fe}", flush=True)

        # Step 3 — build and send
        msg, buzz = _build_digest_message(scored)
        ok = _tg_send(msg, silent=not buzz)
        print(f"[DIGEST] sent={ok}  buzz={buzz}  items={len(scored)}", flush=True)

        _digest_state["count"]     = cycle_num
        _digest_state["last_sent"] = datetime.now(IST).strftime("%d %b %H:%M IST")
        _digest_state["last_ok"]   = ok

    except Exception as e:
        print(f"[DIGEST] cycle error: {e}", flush=True)
        import traceback; traceback.print_exc()
        _tg_send(
            f"📊 <b>AI MARKET TERMINAL</b>  •  {datetime.now(IST).strftime('%H:%M IST')}\n\n"
            f"⚡️ <i>Market feed active — data refreshing...</i>",
            silent=True
        )


async def _async_digest_loop():
    """
    Asyncio task — managed by uvicorn's event loop, never dropped.
    Runs blocking digest work in a thread pool (asyncio.to_thread).
    First message in 10s, then every 5 minutes forever.
    """
    global _digest_state
    _digest_state["running"] = True
    print("[DIGEST] asyncio task started", flush=True)

    await asyncio.sleep(10)   # short boot wait, then first message

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


# Background threads for cache warming (start at import time, before lifespan)
threading.Thread(target=_warm,               daemon=True).start()
threading.Thread(target=_continuous_refresh, daemon=True).start()
# _async_digest_loop and start_watchdog are started inside lifespan (see top of file)


# ── Endpoints ─────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/telegram/test")
def telegram_test():
    """Fire one digest immediately + show loop health."""
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
                    "url":        item.get("url", ""),
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


@app.get("/", response_class=HTMLResponse)
def dashboard():
    path = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
    with open(path) as f:
        return f.read()
