import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HIGH_KEYWORDS = {
    "fed":           10,
    "fomc":          10,
    "rate hike":     10,
    "rate cut":      10,
    "cpi":            9,
    "inflation":      9,
    "interest rate":  9,
    "war":            9,
    "gdp":            8,
    "unemployment":   8,
    "nfp":            8,
    "recession":      8,
}

MEDIUM_KEYWORDS = {
    "goldman":       6,
    "jp morgan":     6,
    "blackrock":     6,
    "hedge fund":    6,
    "yield":         5,
    "oil":           5,
    "dollar":        5,
    "geopolitical":  5,
    "earnings":      4,
    "hni":           4,
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
    # Summarize long articles before scoring
    news_list = summarize_news(news_list)

    scored = []
    for n in news_list:
        s = score_news(n)
        if s > 0:
            scored.append((s, n))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:10]


# 🔹 Format with score + priority label + source + IST time
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
            time       = n.get("time",       "")
            summarized = n.get("summarized", False)
            headline   = _text(n)
            marker     = " ✂" if summarized else ""
            text += f"  {label} [{score:>2}] [{source} | {time}]{marker}\n         {headline}\n"
        else:
            text += f"  {label} [{score:>2}] {n[:150]}\n"
    return text


if __name__ == "__main__":
    from news import get_all_news
    news = get_all_news()
    print(format_priority_news(news))
