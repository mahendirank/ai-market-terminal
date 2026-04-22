"""
screener.in scraper for India NSE quarterly data.
- SQLite cache with 4-hour TTL (avoid rate limits)
- Single session with cookies
- Max 3 parallel workers with 0.3s delay between batches
"""
import re
import time
import json
import sqlite3
import threading
import requests
from concurrent.futures import ThreadPoolExecutor

import os as _os
DB_PATH    = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "screener_cache.db")
CACHE_TTL  = 4 * 3600  # 4 hours
SCREENER_SLUG = {
    "TATAMOTORS": "TMCV",
    "ZOMATO":     "ETERNAL",
    "LTIM":       "ALLTIME",
}

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.screener.in/",
}

_session     = None
_session_ts  = 0
_session_lk  = threading.Lock()
_slug_cache  = {}
_slug_lk     = threading.Lock()


# ── SQLite cache ──────────────────────────────────────────────────────────────
def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS screener_cache (
            symbol TEXT PRIMARY KEY,
            data   TEXT NOT NULL,
            ts     REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _cache_get(sym):
    try:
        conn = _db()
        row  = conn.execute(
            "SELECT data, ts FROM screener_cache WHERE symbol=?", (sym,)
        ).fetchone()
        conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _cache_set(sym, data):
    try:
        conn = _db()
        conn.execute(
            "INSERT OR REPLACE INTO screener_cache(symbol,data,ts) VALUES(?,?,?)",
            (sym, json.dumps(data), time.time())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Session management ────────────────────────────────────────────────────────
def _get_session():
    global _session, _session_ts
    with _session_lk:
        now = time.time()
        if _session and (now - _session_ts) < 600:
            return _session
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get("https://www.screener.in/", timeout=10)
            time.sleep(0.5)
        except Exception:
            pass
        _session    = s
        _session_ts = now
        return s


# ── HTML fetch with slug resolution ──────────────────────────────────────────
def _fetch_html(nse_sym):
    """Fetch screener.in HTML — 1-2 requests per call, uses slug cache."""
    with _slug_lk:
        cached_slug = _slug_cache.get(nse_sym)

    slug = SCREENER_SLUG.get(nse_sym, nse_sym)
    s    = _get_session()

    # If we know the working slug+suffix, use it directly
    if cached_slug:
        slug, suffix = cached_slug
        try:
            resp = s.get(f"https://www.screener.in/company/{slug}{suffix}", timeout=12)
            if resp.status_code == 200 and 'id="quarters"' in resp.text:
                return resp.text
        except Exception:
            pass

    # Try /consolidated/ then /standalone/
    for sfx in ["/consolidated/", "/"]:
        try:
            resp = s.get(f"https://www.screener.in/company/{slug}{sfx}", timeout=12)
            if resp.status_code == 200 and 'id="quarters"' in resp.text:
                with _slug_lk:
                    _slug_cache[nse_sym] = (slug, sfx)
                return resp.text
        except Exception:
            pass

    # Fallback: use screener search API
    try:
        r = s.get(
            f"https://www.screener.in/api/company/search/?q={nse_sym}&v=3&fts=1",
            timeout=8
        )
        if r.status_code == 200:
            items = r.json()
            if items and items[0].get("url") and "/company/" in items[0]["url"]:
                found = items[0]["url"].split("/company/")[1].split("/")[0]
                for sfx in ["/consolidated/", "/"]:
                    try:
                        resp = s.get(f"https://www.screener.in/company/{found}{sfx}", timeout=12)
                        if resp.status_code == 200 and 'id="quarters"' in resp.text:
                            with _slug_lk:
                                _slug_cache[nse_sym] = (found, sfx)
                            return resp.text
                    except Exception:
                        pass
    except Exception:
        pass
    return None


# ── HTML parsing ──────────────────────────────────────────────────────────────
def _clean(val):
    v = re.sub(r"<[^>]+>", "", val).strip().replace("\xa0", "").replace(",", "").replace("%", "").strip()
    if not v or v in ("-", "—", ""): return None
    try: return float(v)
    except ValueError: return None


def _parse_quarters(html):
    m = re.search(r'id="quarters"(.*?)</section>', html, re.DOTALL)
    if not m: return None, []
    txt      = m.group(1)
    qheaders = re.findall(r"<th[^>]*>\s*((?:Mar|Jun|Sep|Dec)\s+\d{4})\s*", txt)
    rows     = re.findall(r"<tr[^>]*>(.*?)</tr>", txt, re.DOTALL)
    data = {}
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        clean = [re.sub(r"<[^>]+>","",c).strip().replace("\xa0","").replace(",","") for c in cells]
        clean = [c for c in clean if c.strip()]
        if len(clean) > 1:
            label = re.sub(r"[+\s]+$","", clean[0].split("&")[0]).strip()
            data[label] = clean[1:]
    return data, qheaders


# ── Main public API ───────────────────────────────────────────────────────────
def get_screener_data(nse_sym):
    """Return latest quarterly data for an NSE symbol (without .NS suffix)."""
    # Check SQLite cache first
    cached = _cache_get(nse_sym)
    if cached:
        return cached

    html = _fetch_html(nse_sym)
    if not html:
        return {}

    data, qheaders = _parse_quarters(html)
    if not data or not qheaders:
        return {}

    def _last(key, idx=0):
        row = data.get(key, [])
        if len(row) < idx + 1: return None
        return _clean(row[-(idx + 1)])

    rev_curr      = _last("Revenue", 0)
    rev_prev      = _last("Revenue", 1)
    np_curr       = _last("Net Profit", 0)
    np_prev       = _last("Net Profit", 1)
    eps_curr      = _last("EPS in Rs", 0)
    eps_prev      = _last("EPS in Rs", 1)
    opm_curr      = _last("OPM %", 0)
    opm_prev      = _last("OPM %", 1)
    interest_curr = _last("Interest", 0)
    interest_prev = _last("Interest", 1)

    earn_date = qheaders[-1] if qheaders else "—"

    def _pct(old, new):
        if old and new and old != 0:
            return round((new - old) / abs(old) * 100, 1)
        return None

    result = {
        "earn_date":       earn_date,
        "revenue_cr":      rev_curr,
        "rev_prev_cr":     rev_prev,
        "rev_growth":      _pct(rev_prev, rev_curr),
        "net_profit_cr":   np_curr,
        "net_profit_prev": np_prev,
        "eps_act":         eps_curr,
        "eps_prev":        eps_prev,
        "eps_yoy":         _pct(eps_prev, eps_curr),
        "net_margin":      round(np_curr / rev_curr * 100, 1) if rev_curr and np_curr else None,
        "gross_margin":    opm_curr,
        "margin_bps":      round((opm_curr - opm_prev) * 100) if opm_curr is not None and opm_prev is not None else None,
        "nim_bps":         round(((interest_curr/rev_curr) - (interest_prev/rev_prev)) * 10000)
                           if interest_curr and rev_curr and interest_prev and rev_prev else None,
    }

    _cache_set(nse_sym, result)
    return result


def get_screener_batch(nse_syms, max_workers=3):
    """
    Fetch screener data for multiple symbols in parallel.
    max_workers=3 keeps screener.in happy (avoid rate limits).
    """
    _get_session()  # warm session before parallel fetch
    results = {}

    def _fetch_one(sym):
        d = get_screener_data(sym)
        time.sleep(0.2)  # small polite delay
        return sym, d

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_one, sym) for sym in nse_syms]
    for fut in futures:
        try:
            sym, d = fut.result(timeout=0.1)
            results[sym] = d
        except Exception:
            pass
    return results


if __name__ == "__main__":
    syms = ["HDFCBANK", "ICICIBANK", "TCS", "RELIANCE", "INFY", "TATAMOTORS", "BAJFINANCE", "ZOMATO"]
    t = time.time()
    for sym in syms:
        d = get_screener_data(sym)
        print(f"{sym:15} {d.get('earn_date','?'):10} EPS:{str(d.get('eps_act','?')):<8} PAT:{str(d.get('net_profit_cr','?'))} Cr")
    print(f"Time: {time.time()-t:.1f}s")
