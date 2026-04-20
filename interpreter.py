import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from surprise import calculate_surprise


def interpret_macro(macro_text, news_text, stock_text, econ_events=None):
    score    = 0
    insights = []

    text = (macro_text + news_text + stock_text).lower()

    # 🔹 Inflation logic (context-based)
    if "inflation" in text:
        if "higher than expected" in text:
            if "fed hawkish" in text or "rate hike" in text:
                score -= 2
                insights.append("High inflation + hawkish Fed → bearish gold")
            else:
                score += 1
                insights.append("Inflation high but no action → gold hedge demand")
        elif "cooling inflation" in text:
            score -= 1
            insights.append("Inflation cooling → bearish gold")

    # 🔹 GDP logic
    if "gdp" in text:
        if "strong" in text:
            score -= 1
            insights.append("Strong economy → risk-on → bearish gold")
        elif "weak" in text:
            score += 2
            insights.append("Weak GDP → recession fear → bullish gold")

    # 🔹 Unemployment
    if "unemployment" in text:
        if "rising" in text:
            score += 2
            insights.append("Job weakness → recession → bullish gold")
        elif "falling" in text:
            score -= 1
            insights.append("Strong jobs → bearish gold")

    # 🔹 Yields (real interpretation)
    if "yield" in text:
        if "rising" in text:
            if "inflation" in text:
                score -= 2
                insights.append("Real yields rising → bearish gold")
        elif "falling" in text:
            score += 2
            insights.append("Falling yields → bullish gold")

    # 🔹 Stocks reaction
    if "selloff" in text or "panic" in text:
        score += 2
        insights.append("Market fear → safe haven gold")
    elif "rally" in text:
        score -= 1
        insights.append("Risk-on sentiment → bearish gold")

    # 🔹 Geopolitics
    if "war" in text or "conflict" in text:
        score += 3
        insights.append("Geopolitical risk → strong gold bullish")

    # 🔹 Big Tech influence
    if "nvda" in text or "earnings" in text:
        if "beat" in text:
            score -= 1
            insights.append("Strong earnings → risk-on")
        elif "miss" in text:
            score += 1
            insights.append("Weak earnings → risk-off")

    # 🔹 Economic surprise layer
    if econ_events:
        econ = calculate_surprise(econ_events)
        score    += econ["score"]
        insights += econ["insights"]

    # 🔹 Noise filter
    if len(text) < 200:
        insights.append("Low data confidence")

    return {
        "score":    score,
        "insights": insights
    }
