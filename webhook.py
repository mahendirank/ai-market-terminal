import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
from datetime import datetime

from macro import get_macro_data, format_macro
from news import get_all_news, format_news
from stocks import format_stocks
from trade_signal import generate_signal
from notify import send_telegram


app = FastAPI(title="AI Trade Webhook")

trade_log = []   # in-memory log of all received alerts


# ─── Health check ──────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "running", "time": str(datetime.now())}


# ─── TradingView webhook ────────────────────────────────────────────────────
# In TradingView alert → Webhook URL → http://<your-ip>:8000/webhook
# Alert message format (JSON):
# {"symbol": "XAUUSD", "action": "BUY", "price": "{{close}}", "tf": "5m"}

@app.post("/webhook")
async def receive_alert(request: Request):
    try:
        data = await request.json()
    except:
        data = {}

    symbol  = data.get("symbol", "XAUUSD")
    tv_action = data.get("action", "").upper()   # BUY / SELL from Pine Script
    price   = data.get("price", "N/A")
    tf      = data.get("tf", "N/A")

    print(f"\n📩 Alert received: {symbol} | {tv_action} | {price} | TF:{tf}")

    # Run signal engine to confirm
    macro_data  = format_macro(get_macro_data())
    news_data   = format_news(get_all_news())
    stock_data  = format_stocks()
    signal      = generate_signal(macro_data, news_data, stock_data)

    confirmed = (
        (tv_action == "BUY"  and signal["decision"] == "BUY")  or
        (tv_action == "SELL" and signal["decision"] == "SELL")
    )

    result = {
        "time":       str(datetime.now()),
        "symbol":     symbol,
        "tv_action":  tv_action,
        "price":      price,
        "tf":         tf,
        "ai_decision": signal["decision"],
        "score":      signal["score"],
        "reasons":    signal["reasons"],
        "confirmed":  confirmed,
        "action_taken": "TRADE CONFIRMED" if confirmed else "SKIPPED — no AI confirmation"
    }

    trade_log.append(result)

    # Telegram alert
    if confirmed:
        emoji = "📈" if tv_action == "BUY" else "📉"
        msg = (
            f"{emoji} *{tv_action} CONFIRMED — {symbol}*\n"
            f"Price  : {price}\n"
            f"TF     : {tf}\n"
            f"Score  : {signal['score']}\n"
            f"Reason : {', '.join(signal['reasons'])}"
        )
        send_telegram(msg)
        print(f"✅ Trade confirmed → Telegram sent")
    else:
        print(f"⚠️  Skipped — TV:{tv_action} vs AI:{signal['decision']}")

    return JSONResponse(result)


# ─── Manual signal check ────────────────────────────────────────────────────
@app.get("/signal")
def manual_signal():
    macro_data  = format_macro(get_macro_data())
    news_data   = format_news(get_all_news())
    stock_data  = format_stocks()
    signal      = generate_signal(macro_data, news_data, stock_data)
    return signal


# ─── Trade log ──────────────────────────────────────────────────────────────
@app.get("/log")
def get_log():
    return {"total": len(trade_log), "trades": trade_log[-20:]}


# ─── Run ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n🚀 Webhook server starting on http://localhost:8000\n")
    print("Endpoints:")
    print("  GET  /         → health check")
    print("  POST /webhook  → TradingView alert receiver")
    print("  GET  /signal   → manual signal check")
    print("  GET  /log      → last 20 trade alerts\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
