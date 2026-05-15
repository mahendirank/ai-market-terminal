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
from difflib import get_close_matches
from typing import Optional


def _normalize_key(s: str) -> str:
    """Uppercase, strip, remove internal whitespace + common separators.

    Preserves yf-special chars (``= - ^ .``) so raw tickers survive.
    'NIFTY 50' → 'NIFTY50',  'CRUDE_OIL' → 'CRUDEOIL',  'BTC-USD' → 'BTC-USD'.
    """
    if not s:
        return ""
    s = s.strip().upper()
    # Strip only whitespace and "soft" separators; keep yf tokens intact
    return re.sub(r"[\s_/\\]+", "", s)


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
    "SPX500":    ("^GSPC",   "S&P 500",        INDEX, "US"),
    "US500":     ("^GSPC",   "S&P 500",        INDEX, "US"),
    "NASDAQ":    ("^IXIC",   "NASDAQ Composite", INDEX, "US"),
    "NDX":       ("^NDX",    "NASDAQ 100",     INDEX, "US"),
    "US100":     ("^NDX",    "NASDAQ 100",     INDEX, "US"),
    "DOW":       ("^DJI",    "Dow Jones",      INDEX, "US"),
    "DJIA":      ("^DJI",    "Dow Jones",      INDEX, "US"),
    "US30":      ("^DJI",    "Dow Jones (US30)", INDEX, "US"),
    "RUSSELL":   ("^RUT",    "Russell 2000",   INDEX, "US"),
    "VIX":       ("^VIX",    "Volatility Index", INDEX, "US"),

    # ── Indian indices ──
    "NIFTY":     ("^NSEI",   "Nifty 50",       INDEX, "NSE"),
    "NIFTY50":   ("^NSEI",   "Nifty 50",       INDEX, "NSE"),
    "BANKNIFTY": ("^NSEBANK", "Bank Nifty",    INDEX, "NSE"),
    "NIFTYBANK": ("^NSEBANK", "Bank Nifty",    INDEX, "NSE"),
    "SENSEX":    ("^BSESN",  "BSE Sensex",     INDEX, "BSE"),
    "BSE":       ("^BSESN",  "BSE Sensex",     INDEX, "BSE"),
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


# ─── Synonym redirects (multi-word / colloquial → canonical ALIAS_MAP key) ───
# These are applied AFTER whitespace normalization. Example chain:
#   "Crude Oil" → strip+upper → "CRUDEOIL" → SYNONYMS → "OIL" → ALIAS_MAP entry
SYNONYMS: dict[str, str] = {
    # Commodities
    "CRUDEOIL":      "OIL",
    "WTICRUDE":      "WTI",
    "BRENTCRUDE":    "BRENT",
    "NATURALGAS":    "NATGAS",
    "GOLDFUTURES":   "GOLD",
    "SILVERFUTURES": "SILVER",
    "COMEXGOLD":     "GOLD",
    "SPOTGOLD":      "GOLD",
    # FX / Dollar
    "USDOLLAR":      "DXY",
    "DOLLARINDEX":   "DXY",
    "DOLLARIDX":     "DXY",
    # US indices (human variants)
    "DOWJONES":      "DOW",
    "DOWJONESINDUSTRIALAVERAGE": "DOW",
    "NASDAQ100":     "NDX",
    "NASDAQCOMPOSITE": "NASDAQ",
    "SP500INDEX":    "SPX",
    "RUSSELL2000":   "RUSSELL",
    "VOLATILITYINDEX": "VIX",
    # Indian indices (colloquial)
    "NIFTYFIFTY":    "NIFTY",
    "NIFTYBANKINDEX":"BANKNIFTY",
    "BSESENSEX":     "SENSEX",
    "NIFTYITINDEX":  "NIFTYIT",
    # Crypto
    "BITCOINUSD":    "BTC",
    "ETHEREUMUSD":   "ETH",
    "SOLANA":        "SOL",
    "POLYGONMATIC":  "MATIC",
    "BINANCECOIN":   "BNB",
    "CARDANO":       "ADA",
    "DOGECOIN":      "DOGE",
    # Global indices
    "FTSE100":       "FTSE",
    "NIKKEI225":     "NIKKEI",
    "HANGSENG":      "HSI",
    "SHANGHAICOMPOSITE": "SHANGHAI",
    # FX pair variants
    "EUR/USD":       "EURUSD",  # rare; usually hits raw lookup, harmless backup
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


def _from_alias(key: str) -> dict:
    yf_t, display, klass, exch = ALIAS_MAP[key]
    return {"ticker": yf_t, "display": display,
            "asset_class": klass, "exchange": exch, "source": "alias"}


def resolve(query: str) -> Optional[dict]:
    """Resolve a free-form query to a canonical symbol record.

    Lookup order (each step is cheap, no network):
      1. Raw upper (preserves yf chars) → ALIAS_MAP
      2. Looks like a yf ticker already (``BTC-USD``, ``RELIANCE.NS``)
      3. Normalized (whitespace + separators stripped) → ALIAS_MAP
      4. Normalized → SYNONYMS → ALIAS_MAP
      5. Normalized in US_STOCKS / INDIAN_STOCKS shortlists
      6. Pure ticker-ish ``[A-Z0-9]+`` → US-guess (autocomplete will fallback)

    Returns ``None`` if nothing matches — callers should fall back to
    :func:`suggest` to give the user something to click.
    """
    if not query:
        return None
    raw = query.strip()
    if not raw:
        return None

    upper = raw.upper()

    # 1) Direct alias with original punctuation preserved
    if upper in ALIAS_MAP:
        return _from_alias(upper)

    # 2) Looks like a yf ticker (e.g. RELIANCE.NS, BTC-USD, ^GSPC)
    if _looks_like_yf_ticker(upper):
        klass = _infer_class_from_ticker(upper)
        return {"ticker": upper, "display": upper,
                "asset_class": klass, "exchange": _exchange_from_ticker(upper),
                "source": "raw_yf"}

    # 3) Whitespace/separator-stripped lookup
    norm = _normalize_key(raw)
    if not norm:
        return None
    if norm in ALIAS_MAP:
        return _from_alias(norm)

    # 4) Synonym redirect → canonical alias
    if norm in SYNONYMS:
        canon = SYNONYMS[norm]
        if canon in ALIAS_MAP:
            r = _from_alias(canon)
            r["source"] = "synonym"
            return r

    # 5) Stock shortlists
    if norm in US_STOCKS:
        return {"ticker": norm, "display": f"{norm} — {US_STOCKS[norm]}",
                "asset_class": US_STOCK, "exchange": "US", "source": "us_known"}
    if norm in INDIAN_STOCKS:
        return {"ticker": f"{norm}.NS",
                "display": f"{norm} — {INDIAN_STOCKS[norm]}",
                "asset_class": NSE, "exchange": "NSE", "source": "nse_known"}

    # 6) Plain alnum → ambiguous US-guess. Caller may try .NS/.BO via
    # :func:`resolve_with_fallbacks`. For multi-word inputs that don't match
    # anything above, return None so the API can show suggestions.
    if re.fullmatch(r"[A-Z0-9]{1,12}", norm):
        return {"ticker": norm, "display": norm,
                "asset_class": US_STOCK, "exchange": "US", "source": "us_guess"}

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


def _all_searchable() -> list[tuple[str, str, str, str, str]]:
    """One unified pool of (lookup_key, ticker, display, asset_class, exchange).

    Indexes both the canonical key AND the human display text so users can
    type either "DXY" or "dollar index". Also includes SYNONYMS so colloquial
    inputs like "crude oil" hit even when the substring isn't in the alias.
    """
    pool: list[tuple[str, str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()  # (lookup_key, ticker) — dedupe pool

    def _add(key: str, yf_t: str, display: str, klass: str, exch: str):
        norm = _normalize_key(key)
        if not norm:
            return
        k = (norm, yf_t)
        if k in seen:
            return
        seen.add(k)
        pool.append((norm, yf_t, display, klass, exch))

    for alias, (yf_t, display, klass, exch) in ALIAS_MAP.items():
        _add(alias,   yf_t, display, klass, exch)
        _add(display, yf_t, display, klass, exch)
    for syn_key, canon in SYNONYMS.items():
        if canon in ALIAS_MAP:
            yf_t, display, klass, exch = ALIAS_MAP[canon]
            _add(syn_key, yf_t, display, klass, exch)
    for sym, name in US_STOCKS.items():
        _add(sym,  sym, f"{sym} — {name}", US_STOCK, "US")
        _add(name, sym, f"{sym} — {name}", US_STOCK, "US")
    for sym, name in INDIAN_STOCKS.items():
        _add(sym,  f"{sym}.NS", f"{sym} — {name}", NSE, "NSE")
        _add(name, f"{sym}.NS", f"{sym} — {name}", NSE, "NSE")
    return pool


def search(query: str, limit: int = 10) -> list[dict]:
    """Autocomplete-style search with normalization + fuzzy fallback.

    Ranking:
      0 — exact normalized match
      1 — prefix match
      2 — substring match
      3 — fuzzy match (difflib, cutoff 0.6) when nothing else hit
    """
    if not query:
        return []
    norm = _normalize_key(query)
    if not norm:
        return []

    pool = _all_searchable()
    scored: list[tuple[int, str, str, str, str]] = []

    for key, ticker, display, klass, exch in pool:
        if key == norm:
            scored.append((0, ticker, display, klass, exch))
        elif key.startswith(norm):
            scored.append((1, ticker, display, klass, exch))
        elif norm in key:
            scored.append((2, ticker, display, klass, exch))

    # Also consider SYNONYMS — surface the canonical entry when user typed a
    # known synonym keyword
    if norm in SYNONYMS and SYNONYMS[norm] in ALIAS_MAP:
        yf_t, display, klass, exch = ALIAS_MAP[SYNONYMS[norm]]
        scored.append((0, yf_t, display, klass, exch))

    # Fuzzy fallback when nothing direct matched
    if not scored:
        keys = list({k for k, *_ in pool})
        matches = get_close_matches(norm, keys, n=limit, cutoff=0.6)
        for m in matches:
            for key, ticker, display, klass, exch in pool:
                if key == m:
                    scored.append((3, ticker, display, klass, exch))
                    break

    seen: set[str] = set()
    ordered: list[dict] = []
    for _, ticker, display, klass, exch in sorted(scored, key=lambda x: x[0]):
        if ticker in seen:
            continue
        seen.add(ticker)
        ordered.append({"ticker": ticker, "display": display,
                        "asset_class": klass, "exchange": exch})
        if len(ordered) >= limit:
            break
    return ordered


def suggest(query: str, limit: int = 6) -> list[dict]:
    """Shorter alias around :func:`search` for the 'did you mean?' UX path."""
    return search(query, limit=limit)


def list_asset_classes() -> list[str]:
    return [US_STOCK, NSE, BSE, INDEX, FOREX, COMMODITY, CRYPTO]
