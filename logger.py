import json
import os
from datetime import datetime

LOG_FILE = os.path.expanduser("~/ai-system/data/trades.json")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)


def log_trade(signal, price, result="open"):
    trade = {
        "time":     str(datetime.utcnow()),
        "decision": signal["decision"],
        "score":    signal["score"],
        "session":  signal["session"],
        "price":    price,
        "result":   result
    }

    try:
        with open(LOG_FILE, "r") as f:
            data = json.load(f)
    except:
        data = []

    data.append(trade)

    with open(LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print("✅ Trade logged")


def get_logs(n=20):
    try:
        with open(LOG_FILE, "r") as f:
            data = json.load(f)
        return data[-n:]
    except:
        return []


def print_summary(n=10):
    logs = get_logs(n)
    print(f"\n=== TRADE LOG (last {n}) ===\n")
    for t in logs:
        print(f"[{t['time']}] {t['decision']} | Score:{t['score']} | Session:{t['session']} | Price:{t['price']} | {t['result']}")
    print()


if __name__ == "__main__":
    print_summary()
