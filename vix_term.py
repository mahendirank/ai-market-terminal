"""
VIX Term Structure + Fed Rate Cut Probability + CBOE Put/Call Ratio.
- VIX (spot), VIX3M (3-month), VVIX (vol of vol) — contango vs backwardation signal
- ZQ=F (Fed Funds futures) → implied rate → cut probability vs current Fed rate
- CBOE equity put/call ratio — fear/greed signal
"""
import os, json, time, sqlite3, threading
from datetime import datetime, timezone, timedelta

IST       = timezone(timedelta(hours=5, minutes=30))
CACHE_TTL = 300  # 5 minutes
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "vix_cache.db")
_db_lock  = threading.Lock()

CURRENT_FED_RATE = 4.33  # Fed Funds effective rate — update when Fed changes rates


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS vt (key TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL)")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row  = conn.execute("SELECT data,ts FROM vt WHERE key=?", (key,)).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute("INSERT OR REPLACE INTO vt(key,data,ts) VALUES(?,?,?)",
                         (key, json.dumps(data), time.time()))
            conn.commit(); conn.close()
    except: pass


def _get_vix_structure():
    """Fetch VIX spot, VIX3M, VVIX via yfinance fast_info."""
    import yfinance as yf
    result = {}
    for sym, label in [("^VIX", "VIX"), ("^VIX3M", "VIX3M"), ("^VVIX", "VVIX")]:
        try:
            fi   = yf.Ticker(sym).fast_info
            last = float(fi.last_price)
            prev = float(fi.previous_close)
            if last > 0:
                chg = round((last - prev) / prev * 100, 2) if prev > 0 else 0
                result[label] = {"value": round(last, 2), "prev": round(prev, 2), "chg": chg}
        except Exception:
            pass
    return result


def _get_fed_futures():
    """ZQ=F = near-month Fed Funds futures. Implied rate = 100 - price."""
    import yfinance as yf
    try:
        fi    = yf.Ticker("ZQ=F").fast_info
        price = float(fi.last_price)
        if 90 < price < 100:
            implied_rate = round(100 - price, 3)
            diff         = round(implied_rate - CURRENT_FED_RATE, 3)
            if diff < -0.20:
                signal = "CUT EXPECTED"
                signal_cls = "bull"
            elif diff > 0.20:
                signal = "HIKE EXPECTED"
                signal_cls = "bear"
            else:
                signal = "HOLD EXPECTED"
                signal_cls = "neu"
            return {
                "futures_price": round(price, 3),
                "implied_rate":  implied_rate,
                "current_rate":  CURRENT_FED_RATE,
                "diff":          diff,
                "signal":        signal,
                "signal_cls":    signal_cls,
            }
    except Exception:
        pass
    return None


def _get_spy_pcr():
    """Calculate SPY put/call ratio from nearest expiry options chain via yfinance."""
    import yfinance as yf
    try:
        spy  = yf.Ticker("SPY")
        exp  = spy.options[0]  # nearest expiry
        chain = spy.option_chain(exp)
        put_vol  = chain.puts["volume"].sum()
        call_vol = chain.calls["volume"].sum()
        if call_vol > 0 and put_vol > 0:
            pcr = round(put_vol / call_vol, 3)
            if 0.2 < pcr < 5.0:
                if pcr > 1.2:
                    sentiment = "FEAR"
                    cls = "bear"
                elif pcr < 0.7:
                    sentiment = "GREED"
                    cls = "bull"
                else:
                    sentiment = "NEUTRAL"
                    cls = "neu"
                return {"pcr": pcr, "sentiment": sentiment, "cls": cls, "source": "SPY options"}
    except Exception:
        pass
    return None


def get_vix_signals():
    cached = _cache_get("vix")
    if cached: return cached

    import gc
    vix  = _get_vix_structure()
    fed  = _get_fed_futures()
    pcr  = _get_spy_pcr()
    gc.collect()

    # VIX term structure interpretation
    term_signal     = "N/A"
    term_signal_cls = "neu"
    if "VIX" in vix and "VIX3M" in vix:
        spot    = vix["VIX"]["value"]
        three_m = vix["VIX3M"]["value"]
        if spot < three_m * 0.95:
            term_signal     = "CONTANGO"   # normal — future vol > spot vol
            term_signal_cls = "bull"
        elif spot > three_m * 1.05:
            term_signal     = "BACKWARDATION"  # stress — spot vol > future vol
            term_signal_cls = "bear"
        else:
            term_signal     = "FLAT"
            term_signal_cls = "neu"

    result = {
        "vix":           vix,
        "term_signal":   term_signal,
        "term_signal_cls": term_signal_cls,
        "fed":           fed,
        "pcr":           pcr,
        "timestamp":     datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
    }
    if vix:
        _cache_set("vix", result)
    return result
