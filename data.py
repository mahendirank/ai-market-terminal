import yfinance as yf


def get_gold_data():
    data = yf.download("GC=F", period="5d", interval="5m")
    return data.tail(50)


def format_data(data):
    return data.to_string()
