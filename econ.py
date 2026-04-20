import requests
import feedparser
from datetime import datetime
from bs4 import BeautifulSoup


# 🔹 FRED API (free, no key needed for basic data)
# Key economic indicators via Yahoo Finance / public sources

import yfinance as yf


def _last(symbol, period="1mo", interval="1d"):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if not df.empty:
            val = df["Close"].iloc[-1]
            return float(val.item() if hasattr(val, "item") else val)
    except:
        pass
    return None


# 🔹 US Economic proxies via ETFs / indices
def get_us_economy():
    symbols = {
        "SP500":      ("^GSPC",  2),   # broad economy proxy
        "US_2Y":      ("^IRX",   3),   # short-term rate expectations
        "US_10Y":     ("^TNX",   3),   # long-term growth/inflation
        "US_30Y":     ("^TYX",   3),   # long bond
        "TIP":        ("TIP",    2),   # TIPS ETF — real yields / inflation
        "HYG":        ("HYG",    2),   # High yield bonds — credit risk appetite
        "LQD":        ("LQD",    2),   # Investment grade bonds
        "VIX":        ("^VIX",   2),   # Fear index
    }

    data = {}
    for name, (ticker, dec) in symbols.items():
        val = _last(ticker)
        if val:
            data[name] = round(val, dec)

    return data


# 🔹 Inflation proxies
def get_inflation_proxies():
    symbols = {
        "BREAKEVEN_10Y": ("RINF",   3),  # ProShares Inflation Expectations ETF proxy
        "GOLD":          ("GC=F",   2),  # Gold — inflation hedge
        "SILVER":        ("SI=F",   2),  # Silver
        "OIL_WTI":       ("CL=F",   2),  # WTI crude
        "OIL_BRENT":     ("BZ=F",   2),  # Brent crude
        "WHEAT":         ("ZW=F",   2),  # Wheat — food inflation
        "COPPER":        ("HG=F",   3),  # Copper — industrial growth
    }

    data = {}
    for name, (ticker, dec) in symbols.items():
        val = _last(ticker, period="5d", interval="1h")
        if val:
            data[name] = round(val, dec)

    return data


# 🔹 Global growth proxies
def get_global_growth():
    symbols = {
        "EEM":   ("EEM",   2),   # Emerging markets ETF
        "FXI":   ("FXI",   2),   # China large cap
        "EWJ":   ("EWJ",   2),   # Japan ETF
        "EWG":   ("EWG",   2),   # Germany ETF
        "EWZ":   ("EWZ",   2),   # Brazil ETF
        "INDA":  ("INDA",  2),   # India ETF
    }

    data = {}
    for name, (ticker, dec) in symbols.items():
        val = _last(ticker)
        if val:
            data[name] = round(val, dec)

    return data


# 🔹 Yield curve analysis
def get_yield_curve():
    us_2y  = _last("^IRX")
    us_10y = _last("^TNX")
    us_30y = _last("^TYX")

    if us_2y and us_10y:
        spread_2_10 = round(us_10y - us_2y, 3)
        inverted    = spread_2_10 < 0
    else:
        spread_2_10 = None
        inverted    = None

    return {
        "US_2Y":       round(us_2y,  3) if us_2y  else None,
        "US_10Y":      round(us_10y, 3) if us_10y else None,
        "US_30Y":      round(us_30y, 3) if us_30y else None,
        "spread_2_10": spread_2_10,
        "inverted":    inverted,
        "signal":      "RECESSION RISK" if inverted else "NORMAL"
    }


# 🔹 ForexFactory economic calendar (actual vs forecast)
def get_economic_data():
    url = "https://www.forexfactory.com/calendar"
    headers = {"User-Agent": "Mozilla/5.0"}

    res = requests.get(url, headers=headers)
    soup = BeautifulSoup(res.text, "html.parser")

    events = []

    rows = soup.select("tr.calendar__row")[:15]

    for row in rows:
        try:
            event    = row.select_one(".calendar__event-title").text.strip()
            actual   = row.select_one(".calendar__actual").text.strip()
            forecast = row.select_one(".calendar__forecast").text.strip()

            if actual and forecast:
                events.append({
                    "event":    event,
                    "actual":   actual,
                    "forecast": forecast
                })
        except:
            pass

    return events


def format_economic_data(events):
    if not events:
        return "No economic calendar data available.\n"
    text = "ECONOMIC CALENDAR (Actual vs Forecast):\n"
    for e in events:
        beat = "✅" if e["actual"] >= e["forecast"] else "❌"
        text += f"  {beat} {e['event']}: actual={e['actual']} forecast={e['forecast']}\n"
    return text


# 🔹 Combine all economic data
def get_econ_data():
    return {
        "US_ECONOMY":     get_us_economy(),
        "INFLATION":      get_inflation_proxies(),
        "GLOBAL_GROWTH":  get_global_growth(),
        "YIELD_CURVE":    get_yield_curve(),
    }


# 🔹 Format for AI
def format_econ(data):
    text = "ECONOMIC DATA:\n\n"

    for category, values in data.items():
        text += f"{category}:\n"
        if isinstance(values, dict):
            for k, v in values.items():
                text += f"- {k}: {v}\n"
        else:
            text += f"- {values}\n"
        text += "\n"

    try:
        events = get_economic_data()
        text += format_economic_data(events)
    except:
        pass

    return text


# 🔹 Interpret yield curve for gold
def interpret_yield_curve(curve):
    signal  = curve.get("signal", "")
    spread  = curve.get("spread_2_10")
    insight = []

    if signal == "RECESSION RISK":
        insight.append("⚠️ Yield curve INVERTED — recession risk → bullish gold")
    elif spread is not None:
        if spread < 0.5:
            insight.append("Yield curve flat — growth slowing → mild gold support")
        else:
            insight.append("Yield curve normal — economy healthy → neutral gold")

    return "\n".join(insight)


if __name__ == "__main__":
    print("📊 Fetching economic data...\n")
    data = get_econ_data()
    print(format_econ(data))

    curve = data["YIELD_CURVE"]
    print("YIELD CURVE ANALYSIS:")
    print(interpret_yield_curve(curve))
