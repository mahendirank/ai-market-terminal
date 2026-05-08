"""
nse_earnings.py — Real quarterly results from NSE India API + BSE API.
Also fetches upcoming earnings dates for Nifty50/Nifty100 via yfinance.
NSE API needs a live session (2-step: homepage → API call with cookies).
"""
import requests, time, threading, json
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

IST = timezone(timedelta(hours=5, minutes=30))
_cache_lock = threading.Lock()
_cache: dict = {}

NSE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}

BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json, */*",
    "Referer":    "https://www.bseindia.com/",
}

# Nifty50 + key Nifty100 stocks for upcoming earnings scan
NIFTY50_YF = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "BHARTIARTL.NS", "ICICIBANK.NS",
    "SBIN.NS", "INFY.NS", "LT.NS", "KOTAKBANK.NS", "HCLTECH.NS",
    "WIPRO.NS", "AXISBANK.NS", "ITC.NS", "MARUTI.NS", "SUNPHARMA.NS",
    "TATAMOTORS.NS", "NTPC.NS", "POWERGRID.NS", "TITAN.NS", "BAJFINANCE.NS",
    "HINDUNILVR.NS", "ADANIPORTS.NS", "ONGC.NS", "NESTLEIND.NS", "DRREDDY.NS",
    "CIPLA.NS", "TECHM.NS", "BAJAJ-AUTO.NS", "EICHERMOT.NS", "JSWSTEEL.NS",
    "TATASTEEL.NS", "HINDALCO.NS", "DIVISLAB.NS", "ASIANPAINT.NS", "DMART.NS",
    "ULTRACEMCO.NS", "GRASIM.NS", "M&M.NS", "HEROMOTOCO.NS", "BPCL.NS",
    "COALINDIA.NS", "VEDL.NS", "ZOMATO.NS", "APOLLOHOSP.NS", "BRITANNIA.NS",
]

SYMBOL_NAMES = {
    "RELIANCE.NS": "Reliance Industries", "TCS.NS": "TCS",
    "HDFCBANK.NS": "HDFC Bank",     "BHARTIARTL.NS": "Bharti Airtel",
    "ICICIBANK.NS": "ICICI Bank",   "SBIN.NS": "State Bank of India",
    "INFY.NS": "Infosys",           "LT.NS": "Larsen & Toubro",
    "KOTAKBANK.NS": "Kotak Bank",   "HCLTECH.NS": "HCL Technologies",
    "WIPRO.NS": "Wipro",            "AXISBANK.NS": "Axis Bank",
    "ITC.NS": "ITC",                "MARUTI.NS": "Maruti Suzuki",
    "SUNPHARMA.NS": "Sun Pharma",   "TATAMOTORS.NS": "Tata Motors",
    "NTPC.NS": "NTPC",              "POWERGRID.NS": "Power Grid Corp",
    "TITAN.NS": "Titan Company",    "BAJFINANCE.NS": "Bajaj Finance",
    "HINDUNILVR.NS": "HUL",        "ADANIPORTS.NS": "Adani Ports",
    "ONGC.NS": "ONGC",             "NESTLEIND.NS": "Nestle India",
    "DRREDDY.NS": "Dr Reddy's",    "CIPLA.NS": "Cipla",
    "TECHM.NS": "Tech Mahindra",   "BAJAJ-AUTO.NS": "Bajaj Auto",
    "EICHERMOT.NS": "Eicher Motors","JSWSTEEL.NS": "JSW Steel",
    "TATASTEEL.NS": "Tata Steel",  "HINDALCO.NS": "Hindalco",
    "DIVISLAB.NS": "Divi's Labs",  "ASIANPAINT.NS": "Asian Paints",
    "DMART.NS": "DMart",            "ULTRACEMCO.NS": "UltraTech Cement",
    "GRASIM.NS": "Grasim Industries","M&M.NS": "Mahindra & Mahindra",
    "HEROMOTOCO.NS": "Hero MotoCorp","BPCL.NS": "BPCL",
    "COALINDIA.NS": "Coal India",   "VEDL.NS": "Vedanta",
    "ZOMATO.NS": "Zomato",         "APOLLOHOSP.NS": "Apollo Hospitals",
    "BRITANNIA.NS": "Britannia",
}

SECTOR_MAP = {
    "RELIANCE.NS": "Energy",    "ONGC.NS": "Energy",   "BPCL.NS": "Energy",
    "TCS.NS": "IT",             "INFY.NS": "IT",        "WIPRO.NS": "IT",
    "HCLTECH.NS": "IT",        "TECHM.NS": "IT",
    "HDFCBANK.NS": "Banking",  "ICICIBANK.NS": "Banking", "SBIN.NS": "Banking",
    "KOTAKBANK.NS": "Banking", "AXISBANK.NS": "Banking", "BAJFINANCE.NS": "Finance",
    "SUNPHARMA.NS": "Pharma",  "DRREDDY.NS": "Pharma",   "CIPLA.NS": "Pharma",
    "DIVISLAB.NS": "Pharma",   "APOLLOHOSP.NS": "Healthcare",
    "MARUTI.NS": "Auto",       "TATAMOTORS.NS": "Auto",  "HEROMOTOCO.NS": "Auto",
    "BAJAJ-AUTO.NS": "Auto",   "EICHERMOT.NS": "Auto",   "M&M.NS": "Auto",
    "ITC.NS": "FMCG",          "HINDUNILVR.NS": "FMCG",  "NESTLEIND.NS": "FMCG",
    "BRITANNIA.NS": "FMCG",    "ASIANPAINT.NS": "FMCG",  "TITAN.NS": "Consumer",
    "TATASTEEL.NS": "Metal",   "JSWSTEEL.NS": "Metal",   "HINDALCO.NS": "Metal",
    "VEDL.NS": "Metal",        "COALINDIA.NS": "Mining",
    "LT.NS": "Infra",          "ADANIPORTS.NS": "Infra", "NTPC.NS": "Energy",
    "POWERGRID.NS": "Energy",  "ULTRACEMCO.NS": "Cement","GRASIM.NS": "Cement",
    "BHARTIARTL.NS": "Telecom","DMART.NS": "Retail",     "ZOMATO.NS": "Consumer",
}


def _nse_session() -> requests.Session:
    """Create a requests session with NSE cookies by visiting the homepage first."""
    s = requests.Session()
    try:
        s.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=10)
        time.sleep(0.5)
    except Exception as e:
        print(f"[nse_earnings] session init failed: {e}", flush=True)
    return s


def _fetch_nse_results() -> list:
    """Fetch recent quarterly results from NSE API."""
    results = []
    try:
        session = _nse_session()
        url = "https://www.nseindia.com/api/corporate-results-new?index=equities"
        r = session.get(url, headers=NSE_HEADERS, timeout=15)
        if r.status_code == 200:
            raw = r.json()
            items = raw if isinstance(raw, list) else raw.get("data", raw.get("resultList", []))
            for item in (items or [])[:60]:
                sym     = item.get("symbol", "")
                name    = item.get("companyName") or item.get("comp_name") or sym
                period  = item.get("period") or item.get("xbrl_period") or ""
                xdt     = item.get("xDt")  or item.get("xdt")  or item.get("date") or ""
                # Try to extract basic financials if available
                rev_cr  = item.get("consolidatedRevenue") or item.get("revenue")
                pat_cr  = item.get("consolidatedPAT")     or item.get("pat")
                results.append({
                    "symbol":   sym,
                    "name":     name,
                    "sector":   SECTOR_MAP.get(sym + ".NS", "—"),
                    "period":   period,
                    "date":     xdt[:10] if xdt else "",
                    "revenue":  f"₹{float(rev_cr)/100:.0f} Cr" if rev_cr else "—",
                    "pat":      f"₹{float(pat_cr)/100:.0f} Cr" if pat_cr else "—",
                    "region":   "INDIA",
                    "source":   "NSE",
                    "exchange": "NSE",
                })
        else:
            print(f"[nse_earnings] NSE API {r.status_code}", flush=True)
    except Exception as e:
        print(f"[nse_earnings] NSE results error: {e}", flush=True)
    return results


def _fetch_bse_results() -> list:
    """Fetch recent results from BSE Bhav API."""
    results = []
    try:
        from datetime import date
        today = date.today()
        start = (today - timedelta(days=30)).strftime("%d/%m/%Y")
        end   = today.strftime("%d/%m/%Y")
        url   = (f"https://api.bseindia.com/BseIndiaAPI/api/Corpresults/w"
                 f"?catg=ET&scripcode=&quarterid=&Etype=C&scripname="
                 f"&fromdate={start}&todate={end}")
        r = requests.get(url, headers=BSE_HEADERS, timeout=12)
        if r.status_code == 200:
            raw = r.json()
            items = raw if isinstance(raw, list) else raw.get("Table", raw.get("data", []))
            for item in (items or [])[:60]:
                name   = item.get("SLONGNAME") or item.get("NAME") or item.get("scripname") or ""
                code   = str(item.get("SCRIP_CD") or item.get("scripcd") or "")
                period = item.get("PERIOD_END") or item.get("quarterid") or ""
                xdt    = item.get("SUBMISSIONDATE") or item.get("xdt") or ""
                results.append({
                    "symbol":   code,
                    "name":     name,
                    "sector":   "—",
                    "period":   period[:10] if period else "",
                    "date":     xdt[:10]    if xdt    else "",
                    "revenue":  "—",
                    "pat":      "—",
                    "region":   "INDIA",
                    "source":   "BSE",
                    "exchange": "BSE",
                })
    except Exception as e:
        print(f"[nse_earnings] BSE results error: {e}", flush=True)
    return results


def _fetch_upcoming_yf(symbols: list) -> list:
    """Upcoming earnings dates for NSE stocks from yfinance."""
    results = []
    try:
        import yfinance as yf
        for sym in symbols[:30]:
            try:
                ticker = yf.Ticker(sym)
                cal    = ticker.calendar
                if cal is None:
                    continue
                # calendar may be a dict (newer yfinance) or DataFrame
                if isinstance(cal, dict):
                    earn_dt = cal.get("Earnings Date")
                    if earn_dt:
                        if isinstance(earn_dt, (list, tuple)):
                            earn_dt = earn_dt[0]
                        earn_str = str(earn_dt)[:10]
                    else:
                        continue
                else:
                    # DataFrame
                    if not cal.empty:
                        earn_str = str(cal.columns[0])[:10]
                    else:
                        continue

                today = datetime.now(IST).date()
                try:
                    earn_date = datetime.strptime(earn_str[:10], "%Y-%m-%d").date()
                    days_away = (earn_date - today).days
                    if days_away < -5 or days_away > 45:
                        continue
                except Exception:
                    continue

                base_sym = sym.replace(".NS", "")
                results.append({
                    "symbol":     base_sym,
                    "name":       SYMBOL_NAMES.get(sym, base_sym),
                    "sector":     SECTOR_MAP.get(sym, "—"),
                    "period":     "Upcoming",
                    "date":       earn_str[:10],
                    "days_away":  days_away,
                    "days_label": ("TODAY" if days_away == 0 else
                                   "TOMORROW" if days_away == 1 else
                                   f"In {days_away}d" if days_away > 0 else
                                   f"{abs(days_away)}d ago"),
                    "revenue":    "—",
                    "pat":        "—",
                    "region":     "INDIA",
                    "source":     "upcoming",
                    "exchange":   "NSE",
                })
            except Exception:
                pass
    except Exception as e:
        print(f"[nse_earnings] yf upcoming error: {e}", flush=True)

    results.sort(key=lambda x: x.get("days_away", 999))
    return results


def get_nse_earnings(force: bool = False) -> dict:
    """
    Full NSE/BSE earnings: recent results + upcoming dates.
    Returns {"recent": [...], "upcoming": [...], "generated_at": "..."}
    Cached 30 min.
    """
    with _cache_lock:
        entry = _cache.get("nse_earnings")
        if entry and not force and (time.time() - entry["ts"]) < 1800:
            return entry["data"]

    recent: list   = []
    upcoming: list = []

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {
            pool.submit(_fetch_nse_results): "nse",
            pool.submit(_fetch_bse_results): "bse",
            pool.submit(_fetch_upcoming_yf, NIFTY50_YF): "upcoming",
        }
        for fut in as_completed(futs, timeout=25):
            tag = futs[fut]
            try:
                r = fut.result()
                if tag == "upcoming":
                    upcoming = r
                else:
                    recent.extend(r)
            except Exception as e:
                print(f"[nse_earnings] {tag} error: {e}", flush=True)

    # Deduplicate recent by (symbol, period)
    seen: set  = set()
    deduped: list = []
    for item in recent:
        k = (item.get("symbol", ""), item.get("period", ""), item.get("exchange", ""))
        if k not in seen:
            deduped.append(item)
            seen.add(k)

    result = {
        "recent":       deduped[:50],
        "upcoming":     upcoming[:30],
        "generated_at": datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        "nse_ok":       len(deduped) > 0,
    }
    with _cache_lock:
        _cache["nse_earnings"] = {"data": result, "ts": time.time()}
    return result
