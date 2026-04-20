import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

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
from datetime import datetime, timezone, timedelta

app = FastAPI(title="AI Market Terminal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")


# ─── Endpoints ────────────────────────────────────────────────

@app.get("/api/indices")
def api_indices():
    return get_indices()


@app.get("/api/macro")
def api_macro():
    data = get_macro_data()
    return {
        "fx":     data.get("FX", {}),
        "yields": data.get("US_YIELDS", {}),
        "global_yields": data.get("GLOBAL_YIELDS", {}),
        "oil":    data.get("OIL"),
        "gold":   data.get("GOLD_SPOT"),
    }


@app.get("/api/stocks")
def api_stocks():
    return {
        "mag7":   get_mag7(),
        "semis":  get_semiconductors(),
        "india":  get_india_indices(),
        "etfs":   get_gold_etfs(),
        "movers": detect_movers(),
    }


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
    raw    = get_all_news()
    scored = prioritize_news(raw)
    result = []
    for score, item in scored:
        if isinstance(item, dict):
            result.append({
                "score":      score,
                "priority":   "high" if score >= 8 else "med" if score >= 4 else "low",
                "headline":   item["text"],
                "source":     item.get("source", ""),
                "time":       item.get("time", ""),
                "category":   item.get("category", "MARKETS"),
                "summarized": item.get("summarized", False),
            })
        else:
            result.append({"score": score, "priority": "low", "headline": item,
                           "source": "", "time": "", "category": "MARKETS", "summarized": False})
    return result


@app.get("/api/signal")
def api_signal():
    macro_txt  = format_macro(get_macro_data())
    news_txt   = format_news(get_all_news())
    stocks_txt = format_stocks()
    econ       = get_economic_data()
    signal     = generate_signal(macro_txt, news_txt, stocks_txt, econ)
    brain      = interpret_macro(macro_txt, news_txt, stocks_txt, econ)
    smc        = get_smc_analysis()
    mtf        = get_mtf_bias()
    sniper     = sniper_entry(signal)
    structure  = get_structure()

    return {
        "signal":    signal,
        "insights":  brain["insights"],
        "smc":       {"bos": smc["bos"], "ob": smc["order_block"], "liquidity": smc["liquidity"]},
        "mtf":       mtf,
        "sniper":    {"entry": sniper["entry"], "htf": sniper["htf"], "sweep": sniper["sweep"], "reason": sniper["reason"]},
        "structure": {
            "high":  round(float(structure["high"]), 2),
            "low":   round(float(structure["low"]), 2),
            "pivot": round(float(structure["pivot"]), 2),
            "r1":    round(float(structure["r1"]), 2),
            "s1":    round(float(structure["s1"]), 2),
            "fib":   {k: round(float(v), 2) for k, v in structure["fib"].items()},
        },
        "timestamp": now_ist(),
    }


@app.get("/api/earnings")
def api_earnings():
    return get_earnings()


@app.get("/api/all")
def api_all():
    return {
        "indices": api_indices(),
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
