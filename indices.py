import yfinance as yf


SYMBOLS = {
    "SPX":    "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW":    "^DJI",
    "DAX":    "^GDAXI",
    "FTSE":   "^FTSE",
    "NIKKEI": "^N225",
    "HSI":    "^HSI",
    "NIFTY":  "^NSEI",
    "SENSEX": "^BSESN",
    "VIX":    "^VIX",
}


def _price_change(sym):
    """Use fast_info for correct live prices (yf.download returns stale/wrong data)."""
    try:
        fi = yf.Ticker(sym).fast_info
        last = float(fi.last_price)
        prev = float(fi.previous_close)
        if last <= 0 or prev <= 0:
            return None
        pct   = round((last - prev) / prev * 100, 2)
        arrow = "▲" if pct > 0 else "▼"
        return {"price": round(last, 2), "change": pct, "arrow": arrow}
    except Exception:
        return None


def get_indices():
    import gc
    data = {}
    for k, sym in SYMBOLS.items():
        result = _price_change(sym)
        if result:
            data[k] = result
    gc.collect()
    return data


def format_indices():
    data = get_indices()
    text  = f"\n{'═'*44}\n  GLOBAL INDICES\n{'═'*44}\n"
    text += f"  {'INDEX':<10} {'PRICE':>12}   {'CHG':>7}\n"
    text += f"  {'─'*40}\n"
    for k, v in data.items():
        text += f"  {k:<10} {v['price']:>12,.2f}   {v['arrow']} {abs(v['change'])}%\n"
    text += f"{'═'*44}\n"
    return text


if __name__ == "__main__":
    print("📊 Fetching global indices...\n")
    print(format_indices())
