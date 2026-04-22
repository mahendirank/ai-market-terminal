"""
Earnings module — 120+ large-cap stocks across India, USA, Europe, Japan, China.
Data sources:
  - India: screener.in (real-time, Mar 2026 data)  → yfinance fallback
  - USA/Europe/Japan/China: yfinance (quarterly_income_stmt / quarterly_financials)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yfinance as yf
import math
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

WATCH_LIST = {
    # ── USA: Mega-cap + Large-cap ─────────────────────────────
    "MAG7":        ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"],
    "US_FINANCE":  ["JPM", "GS", "BAC", "MS", "V", "MA", "C", "AXP", "BLK"],
    "US_ENERGY":   ["XOM", "CVX", "COP", "SLB", "OXY"],
    "US_HEALTH":   ["JNJ", "LLY", "UNH", "ABBV", "MRK", "PFE", "BMY"],
    "US_SEMIS":    ["TSM", "AMD", "ASML", "AVGO", "QCOM", "INTC", "MU"],
    "US_CONSUM":   ["WMT", "HD", "COST", "NKE", "TGT", "LOW", "SBUX"],
    "US_INDUS":    ["CAT", "GE", "BA", "HON", "RTX", "LMT", "UPS", "FDX"],
    "US_TECH2":    ["NFLX", "ORCL", "CRM", "CSCO", "IBM", "ADBE", "PYPL"],
    "US_CONSUM2":  ["KO", "PEP", "MCD", "DIS", "F", "GM"],

    # ── INDIA (NSE) — Nifty 100 large-caps ───────────────────
    "IN_BANKS":    ["HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS",
                    "AXISBANK.NS", "SBIN.NS", "INDUSINDBK.NS",
                    "HDFCLIFE.NS", "SBILIFE.NS", "BAJAJFINSV.NS"],
    "IN_IT":       ["TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS",
                    "TECHM.NS", "LTIM.NS", "PERSISTENT.NS"],
    "IN_FMCG":     ["HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS",
                    "DABUR.NS", "COLPAL.NS", "BRITANNIA.NS", "GODREJCP.NS"],
    "IN_AUTO":     ["MARUTI.NS", "TATAMOTORS.NS", "HEROMOTOCO.NS",
                    "BAJAJ-AUTO.NS", "EICHERMOT.NS", "MOTHERSON.NS"],
    "IN_PHARMA":   ["SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS",
                    "DIVISLAB.NS", "TORNTPHARM.NS", "LUPIN.NS"],
    "IN_INFRA":    ["LT.NS", "ADANIPORTS.NS", "NTPC.NS",
                    "POWERGRID.NS", "BHEL.NS"],
    "IN_METALS":   ["TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "VEDL.NS"],
    "IN_ENERGY2":  ["RELIANCE.NS", "ONGC.NS", "BPCL.NS"],
    "IN_MISC":     ["TITAN.NS", "ASIANPAINT.NS", "BAJFINANCE.NS",
                    "APOLLOHOSP.NS", "DMART.NS", "ZOMATO.NS"],

    # ── EUROPE ───────────────────────────────────────────────
    "EU_TECH":     ["SAP", "ASML", "SIEGY", "NXPI"],
    "EU_FINANCE":  ["HSBC", "UBS", "AZN", "ALIZY", "DB"],
    "EU_AUTOS":    ["VWAGY", "BMWYY", "STLA"],
    "EU_CONSUM":   ["NSRGY", "LVMUY", "IDEXY"],

    # ── JAPAN ────────────────────────────────────────────────
    "JP_AUTOS":    ["TM", "HMC", "NSANY"],
    "JP_TECH":     ["SONY", "NTDOY", "KYCCF", "FANUY"],
    "JP_FINANCE":  ["MUFG", "SMFG", "NTT"],

    # ── CHINA (US-listed ADRs) ────────────────────────────────
    "CN_TECH":     ["BABA", "JD", "BIDU", "PDD", "NTES"],
    "CN_EV":       ["NIO", "LI", "XPEV"],
    "CN_FINANCE":  ["TCEHY"],
}

REGION_MAP = {
    "MAG7":"USA","US_FINANCE":"USA","US_ENERGY":"USA","US_HEALTH":"USA",
    "US_SEMIS":"USA","US_CONSUM":"USA","US_INDUS":"USA","US_TECH2":"USA","US_CONSUM2":"USA",
    "IN_BANKS":"INDIA","IN_IT":"INDIA","IN_FMCG":"INDIA","IN_AUTO":"INDIA",
    "IN_PHARMA":"INDIA","IN_INFRA":"INDIA","IN_METALS":"INDIA",
    "IN_ENERGY2":"INDIA","IN_MISC":"INDIA",
    "EU_TECH":"EUROPE","EU_FINANCE":"EUROPE","EU_AUTOS":"EUROPE","EU_CONSUM":"EUROPE",
    "JP_AUTOS":"JAPAN","JP_TECH":"JAPAN","JP_FINANCE":"JAPAN",
    "CN_TECH":"CHINA","CN_EV":"CHINA","CN_FINANCE":"CHINA",
}

CURRENCY_MAP = {
    "IN_BANKS":"INR","IN_IT":"INR","IN_FMCG":"INR","IN_AUTO":"INR",
    "IN_PHARMA":"INR","IN_INFRA":"INR","IN_METALS":"INR",
    "IN_ENERGY2":"INR","IN_MISC":"INR",
}

NAMES = {
    # USA
    "AAPL":"Apple","MSFT":"Microsoft","GOOGL":"Alphabet","AMZN":"Amazon",
    "NVDA":"NVIDIA","META":"Meta","TSLA":"Tesla",
    "JPM":"JPMorgan","GS":"Goldman Sachs","BAC":"Bank of America",
    "MS":"Morgan Stanley","V":"Visa","MA":"Mastercard","C":"Citigroup",
    "AXP":"American Express","BLK":"BlackRock",
    "XOM":"ExxonMobil","CVX":"Chevron","COP":"ConocoPhillips","SLB":"Schlumberger","OXY":"Occidental",
    "JNJ":"J&J","LLY":"Eli Lilly","UNH":"UnitedHealth","ABBV":"AbbVie",
    "MRK":"Merck","PFE":"Pfizer","BMY":"Bristol Myers",
    "TSM":"TSMC","AMD":"AMD","ASML":"ASML","AVGO":"Broadcom","QCOM":"Qualcomm",
    "INTC":"Intel","MU":"Micron",
    "WMT":"Walmart","HD":"Home Depot","COST":"Costco","NKE":"Nike",
    "TGT":"Target","LOW":"Lowe's","SBUX":"Starbucks",
    "CAT":"Caterpillar","GE":"GE","BA":"Boeing","HON":"Honeywell",
    "RTX":"Raytheon","LMT":"Lockheed","UPS":"UPS","FDX":"FedEx",
    "NFLX":"Netflix","ORCL":"Oracle","CRM":"Salesforce","CSCO":"Cisco",
    "IBM":"IBM","ADBE":"Adobe","PYPL":"PayPal",
    "KO":"Coca-Cola","PEP":"PepsiCo","MCD":"McDonald's","DIS":"Disney",
    "F":"Ford","GM":"General Motors",
    # India
    "HDFCBANK.NS":"HDFC Bank","ICICIBANK.NS":"ICICI Bank",
    "KOTAKBANK.NS":"Kotak Bank","AXISBANK.NS":"Axis Bank",
    "SBIN.NS":"SBI","INDUSINDBK.NS":"IndusInd Bank",
    "HDFCLIFE.NS":"HDFC Life","SBILIFE.NS":"SBI Life","BAJAJFINSV.NS":"Bajaj Finserv",
    "TCS.NS":"TCS","INFY.NS":"Infosys","WIPRO.NS":"Wipro",
    "HCLTECH.NS":"HCL Tech","TECHM.NS":"Tech Mahindra",
    "LTIM.NS":"LTIMindtree","PERSISTENT.NS":"Persistent",
    "HINDUNILVR.NS":"HUL","ITC.NS":"ITC",
    "NESTLEIND.NS":"Nestle India","DABUR.NS":"Dabur",
    "COLPAL.NS":"Colgate","BRITANNIA.NS":"Britannia","GODREJCP.NS":"Godrej CP",
    "MARUTI.NS":"Maruti","TATAMOTORS.NS":"Tata Motors",
    "HEROMOTOCO.NS":"Hero MotoCorp","BAJAJ-AUTO.NS":"Bajaj Auto",
    "EICHERMOT.NS":"Eicher Motors","MOTHERSON.NS":"Motherson",
    "SUNPHARMA.NS":"Sun Pharma","DRREDDY.NS":"Dr Reddy",
    "CIPLA.NS":"Cipla","DIVISLAB.NS":"Divi's Labs",
    "TORNTPHARM.NS":"Torrent Pharma","LUPIN.NS":"Lupin",
    "LT.NS":"L&T","ADANIPORTS.NS":"Adani Ports","NTPC.NS":"NTPC",
    "POWERGRID.NS":"Power Grid","BHEL.NS":"BHEL",
    "TATASTEEL.NS":"Tata Steel","JSWSTEEL.NS":"JSW Steel",
    "HINDALCO.NS":"Hindalco","VEDL.NS":"Vedanta",
    "RELIANCE.NS":"Reliance","ONGC.NS":"ONGC","BPCL.NS":"BPCL",
    "TITAN.NS":"Titan","ASIANPAINT.NS":"Asian Paints",
    "BAJFINANCE.NS":"Bajaj Finance","APOLLOHOSP.NS":"Apollo Hospitals",
    "DMART.NS":"DMart","ZOMATO.NS":"Zomato",
    # Europe
    "SAP":"SAP","ASML":"ASML","SIEGY":"Siemens","NXPI":"NXP Semi",
    "HSBC":"HSBC","UBS":"UBS","AZN":"AstraZeneca","ALIZY":"Allianz","DB":"Deutsche Bank",
    "VWAGY":"Volkswagen","BMWYY":"BMW","STLA":"Stellantis",
    "NSRGY":"Nestle","LVMUY":"LVMH","IDEXY":"Industria de Diseño",
    # Japan
    "TM":"Toyota","HMC":"Honda","NSANY":"Nissan",
    "SONY":"Sony","NTDOY":"Nintendo","KYCCF":"Keyence","FANUY":"Fanuc",
    "MUFG":"Mitsubishi UFJ","SMFG":"Sumitomo Mitsui","NTT":"NTT",
    # China
    "BABA":"Alibaba","JD":"JD.com","BIDU":"Baidu","PDD":"PDD Holdings",
    "NTES":"NetEase","TCEHY":"Tencent",
    "NIO":"NIO","LI":"Li Auto","XPEV":"XPeng",
}

SECTOR_MAP = {
    "IN_BANKS":"Banking","US_FINANCE":"Banking","JP_FINANCE":"Banking",
    "IN_IT":"IT Services","US_SEMIS":"Semiconductors","EU_TECH":"Technology",
    "US_TECH2":"Technology","MAG7":"Technology",
    "IN_AUTO":"Automotive","EU_AUTOS":"Automotive","JP_AUTOS":"Automotive",
    "IN_PHARMA":"Pharma","US_HEALTH":"Healthcare",
    "IN_FMCG":"FMCG","US_CONSUM":"Consumer","US_CONSUM2":"Consumer","EU_CONSUM":"Consumer",
    "IN_ENERGY2":"Energy","US_ENERGY":"Energy",
    "IN_METALS":"Metals","IN_INFRA":"Infrastructure","US_INDUS":"Industrials",
    "CN_TECH":"Technology","CN_EV":"EV","CN_FINANCE":"Finance",
    "EU_FINANCE":"Finance",
}


def _safe(v):
    try:
        if v is None: return None
        if isinstance(v, float) and math.isnan(v): return None
        return v
    except: return None

def _rnd(v, d=2):
    v = _safe(v)
    if v is None: return None
    try: return round(float(v), d)
    except: return None

def _fmt_b(v, currency="USD"):
    if v is None: return "—"
    sym = "₹" if currency == "INR" else "$"
    v = float(v)
    if abs(v) >= 1e12: return f"{sym}{v/1e12:.2f}T"
    if abs(v) >= 1e9:  return f"{sym}{v/1e9:.2f}B"
    if abs(v) >= 1e6:  return f"{sym}{v/1e6:.0f}M"
    return f"{sym}{v:.0f}"

def _fmt_cr(v):
    """Format crore value for India stocks."""
    if v is None: return "—"
    v = float(v)
    if abs(v) >= 1e5: return f"₹{v/1e5:.2f}L Cr"
    if abs(v) >= 1e3: return f"₹{v/1e3:.1f}K Cr"
    return f"₹{v:.0f} Cr"

def _bps(old, new):
    if old is None or new is None: return None
    return round((new - old) * 10000)

def _pct_chg(old, new):
    if old is None or new is None or old == 0: return None
    return round((new - old) / abs(old) * 100, 1)

def _get_row(df, *candidates):
    if df is None or df.empty: return None
    for c in candidates:
        if c in df.index:
            s = df.loc[c].dropna()
            return s if not s.empty else None
    return None

def _col(series, i=0):
    try:
        v = _safe(float(series.iloc[i]))
        return v
    except: return None


# ── Screener.in data for India ────────────────────────────────────────────────
def _get_screener_sym(full_sym):
    """Convert HDFCBANK.NS → HDFCBANK for screener lookup."""
    return full_sym.replace(".NS", "")


def _build_from_screener(sym, group, scr):
    """Build result dict from screener.in data."""
    currency = "INR"
    region   = "INDIA"
    name     = NAMES.get(sym, sym.replace(".NS", ""))
    is_bank  = group in ("IN_BANKS",)

    eps_act  = scr.get("eps_act")
    eps_prev = scr.get("eps_prev")
    eps_yoy  = scr.get("eps_yoy")
    rev_cr   = scr.get("revenue_cr")
    rp_cr    = scr.get("rev_prev_cr")
    np_cr    = scr.get("net_profit_cr")

    revenue   = _fmt_cr(rev_cr)   if rev_cr  else "—"
    rev_prev  = _fmt_cr(rp_cr)    if rp_cr   else "—"

    # Score
    score = 50
    if eps_yoy:
        if eps_yoy > 20: score += 25
        elif eps_yoy > 10: score += 15
        elif eps_yoy > 0: score += 8
        elif eps_yoy < -20: score -= 25
        elif eps_yoy < -10: score -= 15
        else: score -= 8
    rev_g = scr.get("rev_growth")
    if rev_g:
        if rev_g > 10: score += 15
        elif rev_g > 5: score += 8
        elif rev_g < -5: score -= 10
    mbps = scr.get("margin_bps")
    if mbps and mbps > 50: score += 5
    if mbps and mbps < -50: score -= 5
    score = max(0, min(100, score))

    # Guidance
    guidance = "—"
    if rev_g is not None and eps_yoy is not None:
        if rev_g > 10 and eps_yoy > 15: guidance = "RAISED"
        elif rev_g > 5 or eps_yoy > 8:  guidance = "IN LINE"
        elif rev_g < -5 or eps_yoy < -10: guidance = "LOWERED"
        else: guidance = "MAINTAINED"

    # Commentary
    parts = []
    if eps_yoy is not None:
        parts.append(f"EPS {'▲' if eps_yoy>0 else '▼'}{abs(eps_yoy):.1f}% YoY")
    if np_cr:
        parts.append(f"PAT {_fmt_cr(np_cr)}")
    if rev_g is not None:
        parts.append(f"Rev {'▲' if rev_g>0 else '▼'}{abs(rev_g):.1f}% QoQ")
    if mbps is not None:
        parts.append(f"OPM {'+' if mbps>0 else ''}{mbps}bps")
    if scr.get("nim_bps") is not None:
        parts.append(f"NIM {'+' if scr['nim_bps']>0 else ''}{scr['nim_bps']}bps")
    if scr.get("net_margin"):
        parts.append(f"Net margin {scr['net_margin']}%")

    n = round(score / 20)

    return {
        "symbol":       sym.replace(".NS", ""),
        "name":         name,
        "group":        group,
        "region":       region,
        "currency":     currency,
        "sector":       SECTOR_MAP.get(group, ""),
        "price":        None,
        "price_chg_pct":None,
        "earn_date":    scr.get("earn_date", "—"),
        "eps_act":      _rnd(eps_act, 2),
        "eps_prev":     _rnd(eps_prev, 2),
        "eps_yoy":      eps_yoy,
        "revenue":      revenue,
        "rev_prev":     rev_prev,
        "rev_growth":   rev_g,
        "gross_margin": scr.get("gross_margin"),
        "margin_bps":   mbps,
        "net_margin":   scr.get("net_margin"),
        "nim_bps":      scr.get("nim_bps"),
        "net_interest_income": "—",
        "earn_growth":  None,
        "guidance":     guidance,
        "commentary":   " · ".join(parts) if parts else "Awaiting report",
        "analyst_rec":  "—",
        "score":        score,
        "stars":        "★"*n + "☆"*(5-n),
        "data_source":  "screener.in",
    }


# ── yfinance fallback ─────────────────────────────────────────────────────────
def get_single_yf(sym, group):
    currency = CURRENCY_MAP.get(group, "USD")
    region   = REGION_MAP.get(group, "USA")
    name     = NAMES.get(sym, sym.replace(".NS", ""))
    is_bank  = group in ("IN_BANKS", "US_FINANCE", "JP_FINANCE")

    result = {
        "symbol": sym.replace(".NS", ""),
        "name": name,
        "group": group,
        "region": region,
        "currency": currency,
        "sector": SECTOR_MAP.get(group, ""),
        "price": None,
        "price_chg_pct": None,
        "earn_date": "—",
        "eps_act": None, "eps_prev": None, "eps_yoy": None,
        "revenue": "—", "rev_prev": "—", "rev_growth": None,
        "gross_margin": None, "margin_bps": None, "net_margin": None,
        "nim_bps": None, "net_interest_income": "—",
        "earn_growth": None, "guidance": "—", "commentary": "—",
        "analyst_rec": "—", "score": 50, "stars": "★★★☆☆",
        "data_source": "yfinance",
    }

    try:
        t = yf.Ticker(sym)

        try:
            qi = t.quarterly_income_stmt
            eps_row = _get_row(qi, "Diluted EPS", "Basic EPS")
            rev_row_i = _get_row(qi, "Total Revenue", "Operating Revenue")
            if eps_row is not None:
                result["eps_act"]  = _rnd(_col(eps_row, 0), 2)
                result["eps_prev"] = _rnd(_col(eps_row, 1), 2)
                result["eps_yoy"]  = _pct_chg(_col(eps_row, 1), _col(eps_row, 0))
            if rev_row_i is not None:
                rev_curr = _col(rev_row_i, 0)
                rev_prev = _col(rev_row_i, 1)
                result["revenue"]    = _fmt_b(rev_curr, currency)
                result["rev_prev"]   = _fmt_b(rev_prev, currency)
                result["rev_growth"] = _pct_chg(rev_prev, rev_curr)
        except: pass

        try:
            qf = t.quarterly_financials
            rev_row  = _get_row(qf, "Total Revenue", "Operating Revenue")
            gp_row   = _get_row(qf, "Gross Profit")
            ni_row_f = _get_row(qf, "Net Income From Continuing Operation Net Minority Interest", "Net Income")
            nii_row  = _get_row(qf, "Net Interest Income", "Interest Income Net")

            if rev_row is not None and gp_row is not None:
                r0, r1 = _col(rev_row, 0), _col(rev_row, 1)
                g0, g1 = _col(gp_row,  0), _col(gp_row,  1)
                gm_curr = (g0/r0) if r0 and g0 else None
                gm_prev = (g1/r1) if r1 and g1 else None
                result["gross_margin"] = _rnd(gm_curr * 100, 1) if gm_curr else None
                result["margin_bps"]   = _bps(gm_prev, gm_curr)

            if rev_row is not None and ni_row_f is not None:
                r0 = _col(rev_row,  0)
                n0 = _col(ni_row_f, 0)
                result["net_margin"] = _rnd((n0/r0)*100, 1) if r0 and n0 else None

            if is_bank and nii_row is not None and rev_row is not None:
                r0, r1 = _col(rev_row,  0), _col(rev_row,  1)
                n0, n1 = _col(nii_row, 0), _col(nii_row, 1)
                nim_c = (n0/r0) if r0 and n0 else None
                nim_p = (n1/r1) if r1 and n1 else None
                result["nim_bps"]             = _bps(nim_p, nim_c)
                result["net_interest_income"] = _fmt_b(n0, currency)
        except: pass

        try:
            qi = t.quarterly_income_stmt
            if qi is not None and not qi.empty:
                result["earn_date"] = str(qi.columns[0])[:7]
        except: pass

        rev_g = result.get("rev_growth")
        eps_y = result.get("eps_yoy")
        mbps  = result.get("margin_bps")
        nbps  = result.get("nim_bps")

        if rev_g is not None and eps_y is not None:
            if rev_g > 10 and eps_y > 15:   result["guidance"] = "RAISED"
            elif rev_g > 5 or eps_y > 8:    result["guidance"] = "IN LINE"
            elif rev_g < -5 or eps_y < -10: result["guidance"] = "LOWERED"
            else:                            result["guidance"] = "MAINTAINED"

        parts = []
        if eps_y is not None:
            parts.append(f"EPS {'▲' if eps_y>0 else '▼'}{abs(eps_y):.1f}% YoY")
        if rev_g is not None:
            parts.append(f"Rev {'▲' if rev_g>0 else '▼'}{abs(rev_g):.1f}% QoQ")
        if mbps is not None:
            parts.append(f"Margin {'+' if mbps>0 else ''}{mbps}bps")
        if nbps is not None:
            parts.append(f"NIM {'+' if nbps>0 else ''}{nbps}bps")
        if result.get("net_margin"):
            parts.append(f"Net margin {result['net_margin']}%")
        result["commentary"] = " · ".join(parts) if parts else "Awaiting next report"

        score = 50
        if eps_y:
            if eps_y > 20: score += 25
            elif eps_y > 10: score += 15
            elif eps_y > 0: score += 8
            elif eps_y < -20: score -= 25
            elif eps_y < -10: score -= 15
            else: score -= 8
        if rev_g:
            if rev_g > 10: score += 15
            elif rev_g > 5: score += 8
            elif rev_g < -5: score -= 10
        if mbps and mbps > 50: score += 5
        if mbps and mbps < -50: score -= 5
        score = max(0, min(100, score))
        result["score"] = score
        n = round(score / 20)
        result["stars"] = "★"*n + "☆"*(5-n)

    except Exception:
        result["commentary"] = "Data unavailable"

    return result


def get_single(sym, group):
    """
    Main per-stock entry point.
    India stocks → screener.in first, yfinance fallback.
    All others → yfinance.
    """
    is_india = group.startswith("IN_")

    if is_india:
        try:
            from screener_data import get_screener_data
            scr_sym = sym.replace(".NS", "")
            scr = get_screener_data(scr_sym)
            if scr and scr.get("eps_act") is not None:
                return _build_from_screener(sym, group, scr)
        except Exception:
            pass

    return get_single_yf(sym, group)


def get_earnings():
    """Fetch all stocks. Prices via batch download, fundamentals per-stock."""
    tasks    = [(sym, group) for group, syms in WATCH_LIST.items() for sym in syms]
    all_syms = [s for s, _ in tasks]

    # Batch prices
    prices     = {}
    price_chgs = {}
    try:
        df = yf.download(all_syms, period="5d", progress=False, auto_adjust=True)
        if "Close" in df.columns:
            closes = df["Close"].dropna(how="all")
            if len(closes) >= 2:
                for sym in all_syms:
                    try:
                        p  = float(closes[sym].dropna().iloc[-1])
                        p0 = float(closes[sym].dropna().iloc[-2])
                        prices[sym]     = round(p, 2)
                        price_chgs[sym] = round((p - p0) / p0 * 100, 2) if p0 else None
                    except: pass
            elif len(closes) >= 1:
                for sym in all_syms:
                    try: prices[sym] = round(float(closes[sym].dropna().iloc[-1]), 2)
                    except: pass
    except: pass

    # Pre-warm screener.in session (avoids cold-start timeout for India stocks)
    india_syms = list({sym.replace(".NS","") for sym, grp in tasks if grp.startswith("IN_")})
    screener_cache = {}
    try:
        from screener_data import get_screener_batch, _get_session
        _get_session()
        screener_cache = get_screener_batch(india_syms, max_workers=2)
    except Exception:
        pass

    # Fetch live Telegram earnings (Ollama-parsed, 10-min cache)
    tg_earnings = {}
    try:
        from earnings_telegram import get_telegram_earnings
        tg_earnings = get_telegram_earnings()
    except Exception:
        pass

    def _get_single_with_cache(sym, grp):
        if grp.startswith("IN_"):
            scr_sym = sym.replace(".NS", "")
            scr = screener_cache.get(scr_sym, {})
            if scr and scr.get("eps_act") is not None:
                r = _build_from_screener(sym, grp, scr)
            else:
                r = get_single_yf(sym, grp)
        else:
            r = get_single_yf(sym, grp)

        # Overlay live Telegram data if available (always most fresh)
        bare = sym.replace(".NS", "")
        tg = tg_earnings.get(bare) or tg_earnings.get(sym)
        if tg:
            from earnings_telegram import build_earnings_row
            r = build_earnings_row(bare, tg, r)
        return r

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_get_single_with_cache, sym, grp): (sym, grp) for sym, grp in tasks}
        for fut in futures:
            try:
                r = fut.result(timeout=45)
                if not r:
                    continue
                # Attach price
                for key in [
                    r["symbol"] + ".NS",
                    r["symbol"],
                    next((s for s, g in tasks if g == r["group"] and s.replace(".NS","") == r["symbol"]), None)
                ]:
                    if key and key in prices:
                        r["price"] = prices[key]
                        r["price_chg_pct"] = price_chgs.get(key)
                        break
                results.append(r)
            except: pass

    # Deduplicate — same symbol in multiple groups: keep highest score
    seen = {}
    for r in results:
        sym = r["symbol"]
        if sym not in seen or r["score"] > seen[sym]["score"]:
            seen[sym] = r
    results = list(seen.values())

    region_order = {"INDIA": 0, "USA": 1, "EUROPE": 2, "JAPAN": 3, "CHINA": 4}
    results.sort(key=lambda x: (region_order.get(x["region"], 9), -x["score"]))
    return results
