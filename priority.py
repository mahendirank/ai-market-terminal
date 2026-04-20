import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── HIGH impact — direct market movers ────────────────────
HIGH_KEYWORDS = {
    # Fed / Central Banks
    "fed":              10,
    "fomc":             10,
    "rate hike":        10,
    "rate cut":         10,
    "interest rate":     9,
    "powell":            9,
    "ecb":               8,
    "boj":               8,
    "rbi":               8,
    "central bank":      8,
    # Inflation / Economy
    "cpi":               9,
    "inflation":         9,
    "gdp":               8,
    "unemployment":      8,
    "nfp":               8,
    "non-farm":          8,
    "recession":         8,
    "stagflation":       9,
    # War / Geopolitics
    "war":               9,
    "iran":              8,
    "russia":            8,
    "china":             8,
    "ukraine":           8,
    "hormuz":            9,
    "nuclear":           9,
    "sanctions":         8,
    "missile":           8,
    "conflict":          7,
    # Gold / Commodities
    "gold":              7,
    "oil":               7,
    "crude":             7,
    "opec":              8,
}

# ── MEDIUM impact — sector / institutional ─────────────────
MEDIUM_KEYWORDS = {
    # HNI / Institutions
    "goldman":           6,
    "jp morgan":         6,
    "morgan stanley":    6,
    "blackrock":         6,
    "hedge fund":        6,
    "warren buffett":    6,
    "ray dalio":         6,
    "bill ackman":       5,
    # Macro / Markets
    "yield":             5,
    "dollar":            5,
    "dxy":               5,
    "treasury":          5,
    "bond":              5,
    "debt ceiling":      6,
    "geopolit":          5,
    "tariff":            5,
    "trade war":         6,
    "trump":             5,
    # Semiconductors / Tech
    "nvidia":            5,
    "semiconductor":     5,
    "chip":              5,
    "tsmc":              5,
    "intel":             4,
    "amd":               4,
    "ai":                4,
    "artificial intelligence": 4,
    # Banking / Finance
    "bank":              4,
    "banking":           4,
    "credit":            4,
    "liquidity":         5,
    "svb":               6,
    "lehman":            6,
    # India
    "nifty":             4,
    "sensex":            4,
    "india":             4,
    "rupee":             4,
    "fii":               5,
    # Events
    "earnings":          4,
    "revenue":           4,
    "outlook":           4,
    "guidance":          4,
    "downgrade":         5,
    "upgrade":           4,
    "hni":               4,
    "it service":        4,
}


from summarizer import summarize_news


def _text(item):
    return item["text"] if isinstance(item, dict) else item


def score_news(item):
    text  = _text(item).lower()
    score = 0
    for k, v in HIGH_KEYWORDS.items():
        if k in text:
            score += v
    for k, v in MEDIUM_KEYWORDS.items():
        if k in text:
            score += v
    return score


def prioritize_news(news_list):
    news_list = summarize_news(news_list)
    scored = []
    for n in news_list:
        s = score_news(n)
        if s > 0:
            scored.append((s, n))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:25]


def format_priority_news(news_list):
    scored = prioritize_news(news_list)
    text   = "=== PRIORITY NEWS ===\n"
    for score, n in scored:
        if score >= 8:
            label = "🔴 HIGH"
        elif score >= 4:
            label = "🟡 MED "
        else:
            label = "⚪ LOW "

        if isinstance(n, dict):
            source     = n.get("source",     "Unknown")
            time_      = n.get("time",       "")
            summarized = n.get("summarized", False)
            headline   = _text(n)
            marker     = " ✂" if summarized else ""
            text += f"  {label} [{score:>2}] [{source} | {time_}]{marker}\n         {headline}\n"
        else:
            text += f"  {label} [{score:>2}] {n[:150]}\n"
    return text


if __name__ == "__main__":
    from news import get_all_news
    news = get_all_news()
    print(format_priority_news(news))
