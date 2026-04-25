"""
CFTC Commitment of Traders (COT) data — free, weekly (Friday release).
Shows what hedge funds (Large Specs) vs commercials are positioned in.
Extreme positioning = contrarian signal.
Assets: Gold, Crude Oil, DXY, S&P 500, Nifty (via USD/INR proxy)
"""
import os, re, io, csv, requests, json, time, sqlite3, threading
from datetime import datetime, timezone, timedelta

IST      = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 86400   # 24 hours (COT is weekly)
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "cot_cache.db")
_db_lock  = threading.Lock()

# CFTC report codes for assets we care about
COT_CODES = {
    "GOLD":   "088691",   # Gold futures
    "OIL":    "067651",   # Crude Oil WTI
    "DXY":    "098662",   # US Dollar Index
    "SPX":    "13874A",   # S&P 500 e-mini
    "SILVER": "084691",   # Silver
    "COPPER": "085692",   # Copper
}

COT_URL = "https://www.cftc.gov/dea/options/deaoptlns_txt.htm"
COT_CSV_URL = "https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip"


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS cot (
        key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL
    )""")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row  = conn.execute("SELECT data,ts FROM cot WHERE key=?", (key,)).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO cot(key,data,ts) VALUES(?,?,?)",
                         (key, json.dumps(data), time.time()))
            conn.commit()
            conn.close()
    except: pass


def _parse_zip(url, long_col, short_col, oi_col, keep_codes=None):
    """Download CFTC zip, parse CSV — only keeps rows for needed codes to save memory."""
    import zipfile, gc
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code != 200:
        return {}
    zf    = zipfile.ZipFile(io.BytesIO(resp.content))
    fname = [n for n in zf.namelist() if n.endswith(".txt") or n.endswith(".csv")]
    if not fname:
        return {}
    content = zf.read(fname[0]).decode("latin-1")
    zf.close()
    reader  = csv.DictReader(io.StringIO(content))
    latest  = {}
    needed_cols = {"CFTC_Contract_Market_Code", "As_of_Date_In_Form_YYMMDD",
                   long_col, short_col, oi_col}
    for row in reader:
        code = row.get("CFTC_Contract_Market_Code", "").strip()
        if keep_codes and code not in keep_codes:
            continue
        date_str = row.get("As_of_Date_In_Form_YYMMDD", "")
        if code not in latest or date_str > latest[code]["_date"]:
            slim = {k: row[k] for k in needed_cols if k in row}
            slim["_date"]  = date_str
            slim["_long"]  = long_col
            slim["_short"] = short_col
            slim["_oi"]    = oi_col
            latest[code]   = slim
    del content
    gc.collect()
    return latest


def _extract_position(row):
    """Extract net position from a COT row."""
    try:
        long_col  = row["_long"]
        short_col = row["_short"]
        oi_col    = row["_oi"]
        ls_long   = int(str(row.get(long_col,  "0")).replace(",",""))
        ls_short  = int(str(row.get(short_col, "0")).replace(",",""))
        oi        = int(str(row.get(oi_col,    "1")).replace(",",""))
        net       = ls_long - ls_short
        net_pct   = round(net / oi * 100, 1) if oi else 0
        date_raw  = row.get("As_of_Date_In_Form_YYMMDD", "")
        try:
            date_fmt = datetime.strptime(date_raw, "%y%m%d").strftime("%d %b %Y")
        except:
            date_fmt = date_raw
        if net_pct > 25:    signal = "EXTREME_LONG"
        elif net_pct > 10:  signal = "NET_LONG"
        elif net_pct < -25: signal = "EXTREME_SHORT"
        elif net_pct < -10: signal = "NET_SHORT"
        else:               signal = "NEUTRAL"
        return {
            "ls_long":   ls_long, "ls_short": ls_short,
            "net":       net,     "net_pct":  net_pct,   "oi": oi,
            "signal":    signal,
            "contrarian":"SELL" if signal=="EXTREME_LONG" else
                         "BUY"  if signal=="EXTREME_SHORT" else "HOLD",
            "date":      date_fmt,
        }
    except Exception:
        return None


def _fetch_cot_quandl_style():
    """
    Fetch COT from two CFTC reports:
    - Financial futures (fin): SPX, DXY, Treasuries
    - Disaggregated (disagg): Gold, Oil, Silver, Copper
    """
    year = datetime.now().year
    results = {}

    FIN_CODES   = {"DXY": "098662", "SPX": "13874A"}
    DISAGG_CODES = {"GOLD": "088691", "OIL": "067651", "SILVER": "084691", "COPPER": "085692"}

    # 1. Financial futures (SPX, DXY)
    try:
        fin = _parse_zip(
            f"https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip",
            "Lev_Money_Positions_Long_All",
            "Lev_Money_Positions_Short_All",
            "Open_Interest_All",
            keep_codes=set(FIN_CODES.values()),
        )
        for asset, code in FIN_CODES.items():
            row = fin.get(code)
            if row:
                pos = _extract_position(row)
                if pos:
                    results[asset] = {"asset": asset, **pos}
    except Exception:
        pass

    # 2. Disaggregated (Gold, Oil, Silver, Copper)
    try:
        disagg = _parse_zip(
            f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip",
            "M_Money_Positions_Long_All",
            "M_Money_Positions_Short_All",
            "Open_Interest_All",
            keep_codes=set(DISAGG_CODES.values()),
        )
        for asset, code in DISAGG_CODES.items():
            row = disagg.get(code)
            if row:
                pos = _extract_position(row)
                if pos:
                    results[asset] = {"asset": asset, **pos}
    except Exception:
        pass

    return results


def get_cot():
    """Main function — returns COT positioning with cache."""
    cached = _cache_get("cot_all")
    if cached:
        cached["cached"] = True
        return cached

    data = _fetch_cot_quandl_style()
    if data and not data.get("error"):
        data["cached"]    = False
        data["timestamp"] = datetime.now(IST).strftime("%d-%b-%Y %H:%M IST")
        _cache_set("cot_all", data)

    return data or {"error": "COT fetch failed", "cached": False}
