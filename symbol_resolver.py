"""
symbol_resolver.py — Universal symbol resolver for the Indicators panel.

Maps free-form user input (e.g. "GOLD", "reliance", "AAPL", "nifty", "eurusd",
"btc") to a canonical yfinance ticker + asset class metadata.

Coverage:
  - US stocks (AAPL, MSFT, ...)
  - Indian NSE/BSE (RELIANCE.NS, TCS.NS, ...)
  - Indian indices (^NSEI, ^BSESN, ...)
  - Forex majors (EURUSD=X, ...)
  - Commodities (GC=F gold, CL=F oil, ...)
  - Crypto (BTC-USD, ETH-USD, ...)

Resolution order:
  1. Direct hit in ALIAS_MAP (friendly names)
  2. Looks like a yfinance ticker already (contains =, -, ^, .) → trust it
  3. ALL-CAPS letters → try as US stock symbol verbatim
  4. Fall back to NSE suffix (.NS), then BSE (.BO) — only attempted when
     yfinance validation is requested.

Designed to be cheap: no network calls unless ``validate=True``.
"""
from __future__ import annotations

import re
from typing import Optional


# Asset classes (used for UI grouping and downstream sentiment routing)
US_STOCK   = "us_stock"
NSE        = "nse"
BSE        = "bse"
INDEX      = "index"
FOREX      = "forex"
COMMODITY  = "commodity"
CRYPTO     = "crypto"


# ─── Canonical alias map ─────────────────────────────────────────────────────
# Each entry: friendly_alias (uppercase) → (yf_ticker, display, asset_class, exchange)
ALIAS_MAP: dict[str, tuple[str, str, str, str]] = {
    # ── Commodities ──
    "GOLD":      ("GC=F",   "Gold (XAU/USD)",      COMMODITY, "COMEX"),
    "XAUUSD":    ("GC=F",   "Gold (XAU/USD)",      COMMODITY, "COMEX"),
    "XAU":       ("GC=F",   "Gold (XAU/USD)",      COMMODITY, "COMEX"),
    "SILVER":    ("SI=F",   "Silver (XAG/USD)",    COMMODITY, "COMEX"),
    "XAGUSD":    ("SI=F",   "Silver (XAG/USD)",    COMMODITY, "COMEX"),
    "OIL":       ("CL=F",   "Crude Oil (WTI)",     COMMODITY, "NYMEX"),
    "WTI":       ("CL=F",   "Crude Oil (WTI)",     COMMODITY, "NYMEX"),
    "BRENT":     ("BZ=F",   "Brent Crude",         COMMODITY, "ICE"),
    "NATGAS":    ("NG=F",   "Natural Gas",         COMMODITY, "NYMEX"),
    "COPPER":    ("HG=F",   "Copper",              COMMODITY, "COMEX"),
    "PLATINUM":  ("PL=F",   "Platinum",            COMMODITY, "NYMEX"),

    # ── FX majors ──
    "EURUSD":    ("EURUSD=X", "EUR/USD",  FOREX, "FX"),
    "GBPUSD":    ("GBPUSD=X", "GBP/USD",  FOREX, "FX"),
    "USDJPY":    ("USDJPY=X", "USD/JPY",  FOREX, "FX"),
    "USDCHF":    ("USDCHF=X", "USD/CHF",  FOREX, "FX"),
    "AUDUSD":    ("AUDUSD=X", "AUD/USD",  FOREX, "FX"),
    "NZDUSD":    ("NZDUSD=X", "NZD/USD",  FOREX, "FX"),
    "USDCAD":    ("USDCAD=X", "USD/CAD",  FOREX, "FX"),
    "USDINR":    ("INR=X",    "USD/INR",  FOREX, "FX"),
    "EURJPY":    ("EURJPY=X", "EUR/JPY",  FOREX, "FX"),
    "GBPJPY":    ("GBPJPY=X", "GBP/JPY",  FOREX, "FX"),
    "DXY":       ("DX-Y.NYB", "US Dollar Index",   FOREX, "ICE"),

    # ── US indices ──
    "SPX":       ("^GSPC",   "S&P 500",        INDEX, "US"),
    "S&P":       ("^GSPC",   "S&P 500",        INDEX, "US"),
    "S&P500":    ("^GSPC",   "S&P 500",        INDEX, "US"),
    "SP500":     ("^GSPC",   "S&P 500",        INDEX, "US"),
    "NASDAQ":    ("^IXIC",   "NASDAQ Composite", INDEX, "US"),
    "NDX":       ("^NDX",    "NASDAQ 100",     INDEX, "US"),
    "DOW":       ("^DJI",    "Dow Jones",      INDEX, "US"),
    "DJIA":      ("^DJI",    "Dow Jones",      INDEX, "US"),
    "RUSSELL":   ("^RUT",    "Russell 2000",   INDEX, "US"),
    "VIX":       ("^VIX",    "Volatility Index", INDEX, "US"),

    # ── Indian indices ──
    "NIFTY":     ("^NSEI",   "Nifty 50",       INDEX, "NSE"),
    "NIFTY50":   ("^NSEI",   "Nifty 50",       INDEX, "NSE"),
    "BANKNIFTY": ("^NSEBANK", "Bank Nifty",    INDEX, "NSE"),
    "SENSEX":    ("^BSESN",  "BSE Sensex",     INDEX, "BSE"),
    "NIFTYIT":   ("^CNXIT",  "Nifty IT",       INDEX, "NSE"),

    # ── Global indices ──
    "FTSE":      ("^FTSE",   "FTSE 100",       INDEX, "UK"),
    "DAX":       ("^GDAXI",  "DAX",            INDEX, "DE"),
    "NIKKEI":    ("^N225",   "Nikkei 225",     INDEX, "JP"),
    "HSI":       ("^HSI",    "Hang Seng",      INDEX, "HK"),
    "SHANGHAI":  ("000001.SS", "Shanghai Composite", INDEX, "CN"),

    # ── Crypto (top tier) ──
    "BTC":       ("BTC-USD", "Bitcoin",        CRYPTO, "CRYPTO"),
    "BITCOIN":   ("BTC-USD", "Bitcoin",        CRYPTO, "CRYPTO"),
    "ETH":       ("ETH-USD", "Ethereum",       CRYPTO, "CRYPTO"),
    "ETHEREUM":  ("ETH-USD", "Ethereum",       CRYPTO, "CRYPTO"),
    "SOL":       ("SOL-USD", "Solana",         CRYPTO, "CRYPTO"),
    "BNB":       ("BNB-USD", "BNB",            CRYPTO, "CRYPTO"),
    "XRP":       ("XRP-USD", "XRP",            CRYPTO, "CRYPTO"),
    "ADA":       ("ADA-USD", "Cardano",        CRYPTO, "CRYPTO"),
    "DOGE":      ("DOGE-USD","Dogecoin",       CRYPTO, "CRYPTO"),
    "MATIC":     ("MATIC-USD","Polygon",       CRYPTO, "CRYPTO"),
}


# Common Indian stocks shortlist — autocomplete-friendly without hitting the network.
# Full universe is reachable via the .NS / .BO suffix fallback.
INDIAN_STOCKS: dict[str, str] = {
    "RELIANCE":  "Reliance Industries",
    "TCS":       "Tata Consultancy Services",
    "INFY":      "Infosys",
    "HDFCBANK":  "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "SBIN":      "State Bank of India",
    "WIPRO":     "Wipro",
    "ITC":       "ITC",
    "LT":        "Larsen & Toubro",
    "AXISBANK":  "Axis Bank",
    "KOTAKBANK": "Kotak Mahindra Bank",
    "MARUTI":    "Maruti Suzuki",
    "BAJFINANCE":"Bajaj Finance",
    "ADANIENT":  "Adani Enterprises",
    "TATAMOTORS":"Tata Motors",
    "HINDUNILVR":"Hindustan Unilever",
    "ASIANPAINT":"Asian Paints",
    "ULTRACEMCO":"UltraTech Cement",
    "TITAN":     "Titan Company",
    "NESTLEIND": "Nestlé India",
}


# Common US stocks shortlist — for autocomplete UX
US_STOCKS: dict[str, str] = {
    "AAPL":  "Apple",
    "MSFT":  "Microsoft",
    "GOOGL": "Alphabet (Class A)",
    "GOOG":  "Alphabet (Class C)",
    "AMZN":  "Amazon",
    "META":  "Meta Platforms",
    "TSLA":  "Tesla",
    "NVDA":  "NVIDIA",
    "AMD":   "Advanced Micro Devices",
    "INTC":  "Intel",
    "NFLX":  "Netflix",
    "JPM":   "JPMorgan Chase",
    "BAC":   "Bank of America",
    "WMT":   "Walmart",
    "DIS":   "Disney",
    "BA":    "Boeing",
    "XOM":   "ExxonMobil",
    "V":     "Visa",
    "MA":    "Mastercard",
    "PYPL":  "PayPal",
}


_YF_PATTERN = re.compile(r"^[\^A-Z0-9]+([.=\-][A-Z0-9]+)+$")


def _looks_like_yf_ticker(s: str) -> bool:
    """yfinance tickers usually contain '=', '-', '^', or '.' — trust those verbatim."""
    return bool(_YF_PATTERN.match(s))


def resolve(query: str) -> Optional[dict]:
    """Resolve a free-form query to a canonical symbol record.

    Returns ``{ticker, display, asset_class, exchange, source}`` or ``None``.

    ``source`` indicates how the match was made (alias, raw_yf, us_guess,
    nse_suffix, bse_suffix) so the caller can decide whether to trust it
    without further validation.
    """
    if not query:
        return None
    q = query.strip().upper()
    if not q:
        return None

    # 1) Direct alias
    if q in ALIAS_MAP:
        yf_t, display, klass, exch = ALIAS_MAP[q]
        return {
            "ticker": yf_t, "display": display,
            "asset_class": klass, "exchange": exch, "source": "alias",
        }

    # 2) Looks like a yf ticker already (e.g. "RELIANCE.NS", "BTC-USD")
    if _looks_like_yf_ticker(q):
        klass = _infer_class_from_ticker(q)
        return {
            "ticker": q, "display": q,
            "asset_class": klass, "exchange": _exchange_from_ticker(q),
            "source": "raw_yf",
        }

    # 3) Known US stock shortlist
    if q in US_STOCKS:
        return {
            "ticker": q, "display": f"{q} — {US_STOCKS[q]}",
            "asset_class": US_STOCK, "exchange": "US", "source": "us_known",
        }

    # 4) Known Indian stock shortlist → default to NSE
    if q in INDIAN_STOCKS:
        return {
            "ticker": f"{q}.NS", "display": f"{q} — {INDIAN_STOCKS[q]}",
            "asset_class": NSE, "exchange": "NSE", "source": "nse_known",
        }

    # 5) Plain uppercase alphanumerics → ambiguous. Best guess: US first.
    # Caller can retry with .NS / .BO via :func:`resolve_with_fallbacks`.
    if re.fullmatch(r"[A-Z0-9]{1,12}", q):
        return {
            "ticker": q, "display": q,
            "asset_class": US_STOCK, "exchange": "US", "source": "us_guess",
        }

    return None


def resolve_with_fallbacks(query: str) -> list[dict]:
    """Return ordered candidate resolutions for ambiguous input.

    Useful when you want to validate (e.g. via yfinance) which ticker actually
    has data. First-candidate-first ordering matches typical user intent.
    """
    primary = resolve(query)
    if not primary:
        return []
    out = [primary]
    if primary["source"] == "us_guess":
        # Same root might also be a valid NSE/BSE symbol
        root = query.strip().upper()
        out.append({
            "ticker": f"{root}.NS", "display": f"{root} (NSE)",
            "asset_class": NSE, "exchange": "NSE", "source": "nse_suffix",
        })
        out.append({
            "ticker": f"{root}.BO", "display": f"{root} (BSE)",
            "asset_class": BSE, "exchange": "BSE", "source": "bse_suffix",
        })
    return out


def _infer_class_from_ticker(t: str) -> str:
    if t.endswith(".NS"): return NSE
    if t.endswith(".BO"): return BSE
    if t.endswith("=X"):  return FOREX
    if t.endswith("=F"):  return COMMODITY
    if t.endswith("-USD") or t.endswith("-USDT"): return CRYPTO
    if t.startswith("^"): return INDEX
    return US_STOCK


def _exchange_from_ticker(t: str) -> str:
    m = _infer_class_from_ticker(t)
    return {
        NSE: "NSE", BSE: "BSE", FOREX: "FX",
        COMMODITY: "FUT", CRYPTO: "CRYPTO", INDEX: "IDX",
        US_STOCK: "US",
    }.get(m, "US")


def search(query: str, limit: int = 10) -> list[dict]:
    """Autocomplete-style search across all known aliases.

    Substring + prefix match, prefix wins. Returns a ranked list of resolution
    candidates the UI can show in a dropdown.
    """
    if not query:
        return []
    q = query.strip().upper()
    if not q:
        return []

    results: list[tuple[int, dict]] = []

    def _push(score: int, ticker: str, display: str, klass: str, exch: str):
        results.append((score, {
            "ticker": ticker, "display": display,
            "asset_class": klass, "exchange": exch,
        }))

    # Alias map
    for alias, (yf_t, display, klass, exch) in ALIAS_MAP.items():
        if alias == q:                  _push(0, yf_t, display, klass, exch)
        elif alias.startswith(q):       _push(1, yf_t, display, klass, exch)
        elif q in alias:                _push(2, yf_t, display, klass, exch)
        elif q in display.upper():      _push(3, yf_t, display, klass, exch)

    # US shortlist
    for sym, name in US_STOCKS.items():
        if sym == q:                    _push(0, sym, f"{sym} — {name}", US_STOCK, "US")
        elif sym.startswith(q):         _push(1, sym, f"{sym} — {name}", US_STOCK, "US")
        elif q in name.upper():         _push(3, sym, f"{sym} — {name}", US_STOCK, "US")

    # Indian shortlist
    for sym, name in INDIAN_STOCKS.items():
        if sym == q:                    _push(0, f"{sym}.NS", f"{sym} — {name}", NSE, "NSE")
        elif sym.startswith(q):         _push(1, f"{sym}.NS", f"{sym} — {name}", NSE, "NSE")
        elif q in name.upper():         _push(3, f"{sym}.NS", f"{sym} — {name}", NSE, "NSE")

    # Sort by score, dedupe by ticker
    seen: set[str] = set()
    ordered: list[dict] = []
    for _, item in sorted(results, key=lambda x: x[0]):
        if item["ticker"] in seen:
            continue
        seen.add(item["ticker"])
        ordered.append(item)
        if len(ordered) >= limit:
            break
    return ordered


def list_asset_classes() -> list[str]:
    return [US_STOCK, NSE, BSE, INDEX, FOREX, COMMODITY, CRYPTO]
