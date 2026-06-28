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

# Plausibility bounds (min, max). The no-login TradingView feed on the VPS
# sometimes returns garbage (e.g. NIFTY50 as 4.7, or FINNIFTY echoing SENSEX's
# ~77k). Any index outside its band is treated as missing so the yfinance
# fallback refills it with a real value.
_NSE_BOUNDS = {
    "NIFTY50":    (5_000, 60_000),
    "BANKNIFTY":  (10_000, 120_000),
    "SENSEX":     (20_000, 200_000),
    "FINNIFTY":   (5_000, 50_000),
    "MIDCPNIFTY": (2_000, 50_000),
}


def _implausible(label: str, entry) -> bool:
    bounds = _NSE_BOUNDS.get(label)
    if not bounds:
        return False
    price = entry.get("price") if isinstance(entry, dict) else None
    lo, hi = bounds
    return not isinstance(price, (int, float)) or price < lo or price > hi


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

    # Drop any implausible tvdata values (e.g. NIFTY50=4.7) so yfinance refills.
    for label in list(data.keys()):
        if _implausible(label, data[label]):
            print(f"[indices] dropping implausible {label}={data[label].get('price')}", flush=True)
            del data[label]

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
