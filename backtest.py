import pandas as pd
import yfinance as yf


def backtest_ema():
    data = yf.download("GC=F", period="1mo", interval="5m")

    data["EMA20"] = data["Close"].ewm(span=20).mean()
    data["EMA50"] = data["Close"].ewm(span=50).mean()

    data["Signal"] = 0
    data.loc[data["EMA20"] > data["EMA50"], "Signal"] = 1
    data.loc[data["EMA20"] < data["EMA50"], "Signal"] = -1

    trades = data["Signal"].diff()

    profit = trades.sum()

    return profit, data.tail(20)
