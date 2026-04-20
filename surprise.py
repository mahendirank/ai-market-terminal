from econ import get_economic_data


def parse_number(value):
    try:
        return float(value.replace("%", "").replace("K", "").replace(",", ""))
    except:
        return None


def calculate_surprise(events):
    score    = 0
    insights = []

    for e in events:
        actual   = parse_number(e["actual"])
        forecast = parse_number(e["forecast"])

        if actual is None or forecast is None:
            continue

        diff = actual - forecast
        name = e["event"].lower()

        # 🔹 CPI / Inflation
        if "cpi" in name or "inflation" in name:
            if diff > 0:
                score -= 2
                insights.append("Inflation above expectation → bearish gold")
            else:
                score += 2
                insights.append("Inflation below expectation → bullish gold")

        # 🔹 Jobs
        elif "unemployment" in name or "nfp" in name:
            if diff > 0:
                score += 2
                insights.append("Weak jobs → bullish gold")
            else:
                score -= 2
                insights.append("Strong jobs → bearish gold")

        # 🔹 GDP
        elif "gdp" in name:
            if diff > 0:
                score -= 1
                insights.append("Strong growth → bearish gold")
            else:
                score += 1
                insights.append("Weak growth → bullish gold")

    return {
        "score":    score,
        "insights": insights
    }


# 🔹 Format for AI
def format_surprises(data):
    text = "ECONOMIC SURPRISES:\n"

    for r in data["results"]:
        emoji = "✅" if r["impact"] > 0 else "❌" if r["impact"] < 0 else "➖"
        text += f"  {emoji} {r['event']}: {r['actual']} vs {r['forecast']} → {r['surprise']} (gold impact: {r['impact']:+})\n"

    text += f"\nSURPRISE SCORE (gold): {data['score']:+}\n"
    text += "Positive = bullish gold | Negative = bearish gold\n"

    return text


# 🔹 Full run
def get_surprise_score():
    events = get_economic_data()
    if not events:
        return {"score": 0, "insights": []}
    return calculate_surprise(events)


if __name__ == "__main__":
    print("📊 Analyzing economic surprises...\n")
    data = get_surprise_score()
    print(format_surprises(data))
