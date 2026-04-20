import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HIGH_KEYWORDS = {
    "fed": 10, "fomc": 10, "rate hike": 10, "rate cut": 10,
    "interest rate": 9, "powell": 9, "cpi": 9, "inflation": 9,
    "war": 9, "iran": 8, "russia": 8, "china": 8, "ukraine": 8,
    "hormuz": 9, "nuclear": 9, "sanctions": 8, "missile": 8,
    "gdp": 8, "unemployment": 8, "nfp": 8, "non-farm": 8,
    "recession": 8, "stagflation": 9, "default": 8, "debt ceiling": 9,
    "opec": 8, "crude": 7, "gold": 6, "oil": 7,
    "ecb": 8, "boj": 8, "rbi": 8, "central bank": 8,
    "conflict": 7, "attack": 7, "strike": 7, "invasion": 8,
}

MEDIUM_KEYWORDS = {
    "goldman": 6, "jp morgan": 6, "morgan stanley": 6, "blackrock": 6,
    "hedge fund": 6, "warren buffett": 6, "ray dalio": 6,
    "yield": 5, "bond": 5, "treasury": 5, "spread": 4,
    "dollar": 5, "dxy": 5, "euro": 4, "yen": 4, "rupee": 4,
    "nvidia": 5, "semiconductor": 5, "chip": 5, "tsmc": 5,
    "intel": 4, "amd": 4, "ai": 4, "artificial intelligence": 4,
    "bank": 4, "banking": 4, "credit": 4, "liquidity": 5,
    "nifty": 4, "sensex": 4, "india": 4, "fii": 5,
    "earnings": 4, "revenue": 4, "outlook": 4, "downgrade": 5,
    "tariff": 5, "trade war": 6, "trump": 5,
    "geopolit": 5, "hni": 4, "it service": 4,
    "copper": 4, "silver": 4, "commodity": 4,
    "ipo": 4, "merger": 4, "acquisition": 4,
}

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


def _parse_time(item):
    """Extract sortable time value from 'HH:MM IST' or similar."""
    try:
        t = item.get("time", "") if isinstance(item, dict) else ""
        if t and ":" in t:
            parts = t.replace(" IST", "").replace(" AM", "").replace(" PM", "").strip()
            h, m  = parts.split(":")[:2]
            return int(h) * 60 + int(m)
    except:
        pass
    return 0


def prioritize_news(news_list, summarize=False):
    # Summarization is OFF by default for speed (Ollama adds 2-3 min)
    if summarize:
        try:
            from summarizer import summarize_news
            news_list = summarize_news(news_list)
        except:
            pass

    scored = [(score_news(n), n) for n in news_list]

    # Sort purely by time (newest first) — priority shown as badge only
    scored.sort(key=lambda item: -_parse_time(item[1]))
    return scored[:40]


def format_priority_news(news_list):
    scored = prioritize_news(news_list)
    text   = "=== PRIORITY NEWS ===\n"
    for score, n in scored:
        label = "🔴 HIGH" if score >= 8 else "🟡 MED " if score >= 4 else "⚪ LOW "
        if isinstance(n, dict):
            source = n.get("source", "Unknown")
            t      = n.get("time", "")
            marker = " ✂" if n.get("summarized") else ""
            text  += f"  {label} [{score:>2}] [{source} | {t}]{marker}\n         {_text(n)}\n"
        else:
            text += f"  {label} [{score:>2}] {n[:150]}\n"
    return text


if __name__ == "__main__":
    from news import get_all_news
    news = get_all_news()
    print(format_priority_news(news))
