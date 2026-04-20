import json
import os

LOG_FILE = os.path.expanduser("~/ai-system/data/trades.json")


def analyze_performance():
    try:
        with open(LOG_FILE, "r") as f:
            trades = json.load(f)
    except:
        print("No trades yet")
        return

    total = len(trades)

    buy  = sum(1 for t in trades if t["decision"] == "BUY")
    sell = sum(1 for t in trades if t["decision"] == "SELL")

    print("\n=== PERFORMANCE ===")
    print("Total trades:", total)
    print("BUY:", buy)
    print("SELL:", sell)


if __name__ == "__main__":
    analyze_performance()
