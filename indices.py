"""
indices.py — Live index prices.
Primary: TradingView via tvdata.py (real-time, no delay).
Fallback: yfinance fast_info (15-min delayed for NSE).
"""

import yfinance as yf

# Global indices — yfinance tickers
_YF_GLOBAL = {
    "SPX":    "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW":    "^DJI",
    "DAX":    "^GDAXI",
    "FTSE":   "^FTSE",
    "NIKKEI": "^N225",
    "HSI":    "^HSI",
    "VIX":    "^VIX",
}

# NSE fallback via yfinance
_YF_NSE = {
    "NIFTY50":   "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX":    "^BSESN",
}


def _yf_price(sym: str) -> dict | None:
    try:
        fi   = yf.Ticker(sym).fast_info
        last = float(fi.last_price)
        prev = float(fi.previous_close)
        if last <= 0 or prev <= 0:
            return None
        chg = round((last - prev) / prev * 100, 2)
        return {"price": round(last, 2), "change": chg, "arrow": "▲" if chg > 0 else "▼"}
    except Exception:
        return None


def get_indices() -> dict:
    """
    Returns all indices — NSE (real-time via tvdatafeed) + global (yfinance).
    Format: {"NIFTY50": {price, change, arrow}, ...}
    """
    import gc
    data = {}

    # ── NSE: try tvdatafeed first ─────────────────────────────────────────────
    try:
        from tvdata import get_nse_indices
        nse = get_nse_indices()
        if nse:
            data.update(nse)
    except Exception as e:
        print(f"[indices] tvdata failed, using yfinance fallback: {e}", flush=True)

    # Fill any missing NSE indices via yfinance
    for label, ticker in _YF_NSE.items():
        if label not in data:
            result = _yf_price(ticker)
            if result:
                data[label] = result

    # ── Global: yfinance ──────────────────────────────────────────────────────
    for label, ticker in _YF_GLOBAL.items():
        result = _yf_price(ticker)
        if result:
            data[label] = result

    gc.collect()
    return data


def format_indices():
    data  = get_indices()
    text  = f"\n{'═'*44}\n  GLOBAL INDICES\n{'═'*44}\n"
    text += f"  {'INDEX':<12} {'PRICE':>12}   {'CHG':>7}\n"
    text += f"  {'─'*40}\n"
    for k, v in data.items():
        text += f"  {k:<12} {v['price']:>12,.2f}   {v['arrow']} {abs(v['change'])}%\n"
    text += f"{'═'*44}\n"
    return text


if __name__ == "__main__":
    print("📊 Fetching indices...\n")
    print(format_indices())
