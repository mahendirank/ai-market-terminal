import json
import os

LOG_FILE = os.environ.get(
    "TRADES_LOG",
    os.path.expanduser("~/ai-system/data/trades.json")
)


def adjust_strategy():
    try:
        with open(LOG_FILE, "r") as f:
            trades = json.load(f)
    except:
        return {"bias": 0}

    score_sum = sum(t["score"] for t in trades)
    avg_score = score_sum / len(trades) if trades else 0

    bias = 0

    if avg_score > 2:
        bias = 1   # bullish bias
    elif avg_score < -2:
        bias = -1  # bearish bias

    return {"bias": bias}


# 🔹 Learn which score threshold works best
def best_score_threshold():
    try:
        with open(LOG_FILE, "r") as f:
            trades = json.load(f)
    except:
        return

    print("\n=== SCORE THRESHOLD ANALYSIS ===\n")

    for t in [2, 3, 4, 5]:
        filtered = [x for x in trades if abs(x.get("score", 0)) >= t]
        wins     = sum(1 for x in filtered if x.get("result") == "win")
        total    = len(filtered)
        wr       = round((wins / total) * 100, 1) if total > 0 else 0
        print(f"  Score >= {t} : {total} trades | {wins} wins | WR: {wr}%")


# 🔹 Learn which session performs best
def best_session():
    try:
        with open(LOG_FILE, "r") as f:
            trades = json.load(f)
    except:
        return

    sessions = {}

    for t in trades:
        s = t.get("session", "Unknown")
        if s not in sessions:
            sessions[s] = {"wins": 0, "total": 0}
        sessions[s]["total"] += 1
        if t.get("result") == "win":
            sessions[s]["wins"] += 1

    print("\n=== SESSION PERFORMANCE ===\n")

    for s, v in sessions.items():
        wr = round((v["wins"] / v["total"]) * 100, 1) if v["total"] > 0 else 0
        print(f"  {s:<12}: {v['total']} trades | WR: {wr}%")


if __name__ == "__main__":
    result = adjust_strategy()
    print("Strategy Bias:", result["bias"])

    best_score_threshold()
    best_session()
