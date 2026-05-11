"""
market_sentiment.py — sentiment gauges for US equities and crypto.

CNN Fear & Greed Index (US stocks) is computed from 7 sub-indicators:
stock price strength, breadth, junk bond demand, market volatility,
put/call ratio, market momentum, safe haven demand.

Crypto Fear & Greed already lives in nse_data.get_fear_greed (using
alternative.me) — we re-export it here so callers have one entry point.
"""
import requests
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

CNN_FNG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_CNN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/121.0.0.0 Safari/537.36",
    "Accept":     "application/json, text/plain, */*",
    "Origin":     "https://edition.cnn.com",
    "Referer":    "https://edition.cnn.com/",
}


def _classify(score: float) -> str:
    if score >= 75: return "Extreme Greed"
    if score >= 55: return "Greed"
    if score >= 45: return "Neutral"
    if score >= 25: return "Fear"
    return "Extreme Fear"


def get_cnn_fng() -> dict:
    """CNN Fear & Greed Index for US stocks. 0=extreme fear, 100=extreme greed."""
    try:
        resp = requests.get(CNN_FNG_URL, timeout=10, headers=_CNN_HEADERS)
        if resp.status_code != 200:
            return {"error": f"http {resp.status_code}"}
        data = resp.json()
        fng  = data.get("fear_and_greed", {}) or {}
        score   = float(fng.get("score", 0) or 0)
        prev_cl = float(fng.get("previous_close", score) or score)
        prev_1w = float(fng.get("previous_1_week", score) or score)
        prev_1m = float(fng.get("previous_1_month", score) or score)
        prev_1y = float(fng.get("previous_1_year", score) or score)
        return {
            "score":             round(score, 1),
            "label":             _classify(score),
            "previous_close":    round(prev_cl, 1),
            "previous_1_week":   round(prev_1w, 1),
            "previous_1_month":  round(prev_1m, 1),
            "previous_1_year":   round(prev_1y, 1),
            "change_1d":         round(score - prev_cl, 1),
            "change_1w":         round(score - prev_1w, 1),
            "change_1m":         round(score - prev_1m, 1),
            "rating":            fng.get("rating", "").replace("_", " ").title(),
        }
    except Exception as e:
        return {"error": str(e)}


def get_combined_sentiment() -> dict:
    """Returns both US-equity (CNN) and crypto (alternative.me) Fear & Greed gauges."""
    from nse_data import get_fear_greed
    return {
        "us_stocks":    get_cnn_fng(),
        "crypto":       get_fear_greed(),
        "generated_at": datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
    }
