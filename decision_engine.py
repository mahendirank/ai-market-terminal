"""
Trade Decision Engine — combines AI news sentiment + technical signals.
Output: bias (BUY/SELL/WAIT), confidence %, scalp vs swing, per-asset decisions.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


# ── Technical signal fetcher ──────────────────────────────────
def _get_technicals(symbol):
    """Returns dict with ema, rsi, vwap signals for a symbol."""
    try:
        from trade_signal import ema_signal, rsi_signal, vwap_signal, _download
        df1h = _download(symbol, period="5d",  interval="1h")
        df5m = _download(symbol, period="2d",  interval="5m")
        return {
            "ema":  ema_signal(df1h),
            "rsi":  rsi_signal(df1h),
            "vwap": vwap_signal(df5m),
        }
    except:
        return {"ema": "NEUTRAL", "rsi": "NEUTRAL", "vwap": "NEUTRAL"}


def _tech_score(signals):
    """Convert signals dict to numeric score: +1 buy, -1 sell, 0 neutral."""
    mapping = {"BUY": 1, "SELL": -1, "NEUTRAL": 0}
    vals = [mapping.get(v, 0) for v in signals.values()]
    return sum(vals) / len(vals) if vals else 0


# ── Asset symbol map ──────────────────────────────────────────
ASSET_SYMBOLS = {
    "GOLD":  "GC=F",
    "OIL":   "CL=F",
    "BTC":   "BTC-USD",
    "SPX":   "^GSPC",
    "NDX":   "^NDX",
    "DJIA":  "^DJI",
    "DXY":   "DX-Y.NYB",
    "NIFTY": "^NSEI",
    "SILVER":"SI=F",
}

ASSET_LABELS = {
    "GOLD":  "Gold (XAUUSD)",
    "OIL":   "Crude Oil",
    "BTC":   "Bitcoin",
    "SPX":   "S&P 500",
    "NDX":   "NASDAQ 100",
    "DJIA":  "Dow Jones",
    "DXY":   "US Dollar Index",
    "NIFTY": "Nifty 50",
    "SILVER":"Silver",
}


# ── Core decision logic ───────────────────────────────────────
def _make_decision(news_score, tech_score, news_conf):
    """
    news_score: -1 to +1 (from sentiment)
    tech_score: -1 to +1 (from EMA/RSI/VWAP)
    news_conf:  0-100 (confidence from AI)
    Returns: bias, confidence, trade_type
    """
    # Weight: news 40%, technicals 60%
    combined = (news_score * 0.40) + (tech_score * 0.60)

    if combined > 0.25:    bias = "BUY"
    elif combined < -0.25: bias = "SELL"
    else:                  bias = "WAIT"

    # Confidence: higher when news + tech agree
    agreement = abs(news_score - tech_score) < 0.3
    base_conf = abs(combined) * 100
    confidence = min(95, int(base_conf * (1.2 if agreement else 0.8) + 40))

    # Scalp vs Swing based on news impact + tech alignment
    if agreement and abs(combined) > 0.5:
        trade_type = "SWING"
    elif abs(combined) > 0.3:
        trade_type = "SCALP"
    else:
        trade_type = "WAIT"

    return bias, confidence, trade_type


def generate_decisions(enriched_news=None, assets=None):
    """
    Main entry. Returns trade decisions for key assets.
    enriched_news: output from ai_layer.enrich_news()
    assets: list of asset names to analyze (default: top 6)
    """
    if assets is None:
        assets = ["GOLD", "OIL", "BTC", "SPX", "NDX", "NIFTY"]

    # Get overall news sentiment
    news_sentiment = {}
    news_confidence = 50
    if enriched_news:
        try:
            from ai_layer import get_market_sentiment
            mkt = get_market_sentiment(enriched_news)
            news_confidence = mkt.get("confidence", 50)
            # Overall sentiment
            overall_bias = mkt.get("bias", "NEU")
            overall_score = 0.6 if overall_bias=="BULL" else (-0.6 if overall_bias=="BEAR" else 0)
            # Per-asset sentiment from news
            for asset, bias in mkt.get("assets", {}).items():
                news_sentiment[asset] = 0.8 if bias=="BULL" else (-0.8 if bias=="BEAR" else 0)
        except:
            overall_score = 0

    decisions = []
    now_ist = datetime.now(IST).strftime("%H:%M IST")

    for asset in assets:
        symbol = ASSET_SYMBOLS.get(asset)
        if not symbol:
            continue

        # Technical signals
        tech = _get_technicals(symbol)
        t_score = _tech_score(tech)

        # News score (asset-specific or overall)
        n_score = news_sentiment.get(asset, overall_score if enriched_news else 0)

        bias, confidence, trade_type = _make_decision(n_score, t_score, news_confidence)

        # Reasoning string
        tech_bias = "bullish" if t_score > 0 else ("bearish" if t_score < 0 else "neutral")
        news_bias = "bullish" if n_score > 0 else ("bearish" if n_score < 0 else "neutral")
        reason = f"Technicals {tech_bias} (EMA:{tech['ema']}, RSI:{tech['rsi']}), news {news_bias}"

        decisions.append({
            "asset":       asset,
            "label":       ASSET_LABELS.get(asset, asset),
            "bias":        bias,
            "confidence":  confidence,
            "trade_type":  trade_type,
            "tech":        tech,
            "reason":      reason,
            "timestamp":   now_ist,
        })

    # Sort by confidence descending
    decisions.sort(key=lambda x: -x["confidence"])
    return decisions


def get_overall_bias(decisions):
    """Summarise all asset decisions into one market-wide bias."""
    if not decisions:
        return {"bias": "WAIT", "confidence": 50, "summary": "Insufficient data"}

    scores = {"BUY": 0, "SELL": 0, "WAIT": 0}
    total_conf = 0
    for d in decisions:
        scores[d["bias"]] += d["confidence"]
        total_conf += d["confidence"]

    dominant = max(scores, key=scores.get)
    confidence = int(scores[dominant] / total_conf * 100) if total_conf else 50

    buy_count  = sum(1 for d in decisions if d["bias"]=="BUY")
    sell_count = sum(1 for d in decisions if d["bias"]=="SELL")

    summary = (
        f"{buy_count}/{len(decisions)} assets bullish, "
        f"{sell_count}/{len(decisions)} bearish"
    )

    return {
        "bias":       dominant,
        "confidence": confidence,
        "summary":    summary,
        "breakdown":  scores,
    }
