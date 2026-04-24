"""
NSE India free data — no API key required.
Covers: FII/DII flows, India VIX, Nifty PCR, Bulk/Block deals.
"""
import requests
import json
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

NSE_HEADERS = {
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Referer":          "https://www.nseindia.com/",
    "X-Requested-With": "XMLHttpRequest",
}

def _nse_session():
    """NSE requires a cookie from homepage before API calls work."""
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=8)
    except:
        pass
    return s


# ── FII / DII Daily Flow ───────────────────────────────────────────────────────
def get_fii_dii():
    """Returns today's FII and DII net buy/sell in ₹ crore."""
    try:
        s    = _nse_session()
        resp = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=10)
        data = resp.json()
        result = {}
        for row in data:
            cat = row.get("category", "").strip()
            # Net value: buy - sell in crores
            try:
                net = float(str(row.get("netValue", "0")).replace(",", ""))
            except:
                net = 0
            if "FII" in cat.upper() or "FPI" in cat.upper():
                result["FII_net"] = round(net, 2)
                result["FII_buy"] = row.get("buyValue", "—")
                result["FII_sell"]= row.get("sellValue", "—")
            elif "DII" in cat.upper():
                result["DII_net"] = round(net, 2)
                result["DII_buy"] = row.get("buyValue", "—")
                result["DII_sell"]= row.get("sellValue", "—")
        result["date"] = datetime.now(IST).strftime("%d-%b-%Y")
        return result
    except Exception as e:
        return {"error": str(e)}


# ── India VIX ─────────────────────────────────────────────────────────────────
def get_india_vix():
    """India VIX — fear gauge for Nifty options market."""
    try:
        s    = _nse_session()
        resp = s.get("https://www.nseindia.com/api/allIndices", timeout=10)
        data = resp.json().get("data", [])
        for idx in data:
            if "VIX" in idx.get("index", "").upper():
                return {
                    "vix":    round(float(idx.get("last", 0)), 2),
                    "change": round(float(idx.get("change", 0)), 2),
                    "pct":    round(float(idx.get("percentChange", 0)), 2),
                    "level":  "HIGH" if float(idx.get("last", 0)) > 20
                              else "ELEVATED" if float(idx.get("last", 0)) > 14
                              else "LOW",
                }
    except:
        pass
    return {}


# ── Nifty PCR (Put-Call Ratio) + Max Pain ────────────────────────────────────
def _parse_option_chain(symbol="NIFTY"):
    """Parse NSE option chain — returns PCR, proper Max Pain, top OI strikes."""
    try:
        s    = _nse_session()
        resp = s.get(
            f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}",
            timeout=12
        )
        data = resp.json()
        oc   = data.get("filtered", {}).get("data", [])
        spot = float(data.get("records", {}).get("underlyingValue", 0))

        total_pe_oi = sum(r.get("PE", {}).get("openInterest", 0) for r in oc)
        total_ce_oi = sum(r.get("CE", {}).get("openInterest", 0) for r in oc)
        pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else None

        # Proper Max Pain calculation:
        # For each strike, calculate total loss if price expires at that strike
        strikes_data = {}
        for row in oc:
            strike = row.get("strikePrice", 0)
            ce_oi  = row.get("CE", {}).get("openInterest", 0)
            pe_oi  = row.get("PE", {}).get("openInterest", 0)
            ce_iv  = row.get("CE", {}).get("impliedVolatility", 0)
            pe_iv  = row.get("PE", {}).get("impliedVolatility", 0)
            strikes_data[strike] = {"ce_oi": ce_oi, "pe_oi": pe_oi,
                                     "ce_iv": ce_iv, "pe_iv": pe_iv}

        all_strikes = sorted(strikes_data.keys())
        pain = {}
        for test_strike in all_strikes:
            loss = 0
            for s_price, d in strikes_data.items():
                # Call writers lose when price > strike
                if test_strike > s_price:
                    loss += (test_strike - s_price) * d["ce_oi"]
                # Put writers lose when price < strike
                if test_strike < s_price:
                    loss += (s_price - test_strike) * d["pe_oi"]
            pain[test_strike] = loss
        max_pain = min(pain, key=pain.get) if pain else None

        # Top OI concentrations near spot (key support/resistance)
        near_strikes = sorted(strikes_data.keys(),
                              key=lambda x: abs(x - spot))[:10] if spot else []
        oi_levels = sorted(
            [{"strike": s, "ce_oi": strikes_data[s]["ce_oi"],
              "pe_oi": strikes_data[s]["pe_oi"],
              "total": strikes_data[s]["ce_oi"] + strikes_data[s]["pe_oi"]}
             for s in near_strikes],
            key=lambda x: -x["total"]
        )[:5]

        bias = "BULLISH" if pcr and pcr > 1.2 else "BEARISH" if pcr and pcr < 0.8 else "NEUTRAL"
        return {
            "symbol":       symbol,
            "pcr":          pcr,
            "max_pain":     max_pain,
            "spot":         round(spot, 2),
            "bias":         bias,
            "total_ce_oi":  total_ce_oi,
            "total_pe_oi":  total_pe_oi,
            "oi_levels":    oi_levels,
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


def get_nifty_pcr():
    return _parse_option_chain("NIFTY")


def get_banknifty_pcr():
    return _parse_option_chain("BANKNIFTY")


# ── FII Cumulative 5-day flow ─────────────────────────────────────────────────
def get_fii_cumulative():
    """Last 5 days FII/DII flows — trend is more important than single day."""
    try:
        s    = _nse_session()
        resp = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=10)
        data = resp.json()
        rows = []
        for row in data[:10]:
            try:
                net = float(str(row.get("netValue", "0")).replace(",", ""))
                rows.append({
                    "category": row.get("category", ""),
                    "net":      round(net, 2),
                    "date":     row.get("date", ""),
                })
            except: pass
        fii_rows = [r for r in rows if "FII" in r["category"].upper() or "FPI" in r["category"].upper()]
        dii_rows = [r for r in rows if "DII" in r["category"].upper()]
        fii_5d = sum(r["net"] for r in fii_rows[:5])
        dii_5d = sum(r["net"] for r in dii_rows[:5])
        return {
            "fii_5d_net": round(fii_5d, 2),
            "dii_5d_net": round(dii_5d, 2),
            "trend":      "BUYING" if fii_5d > 3000 else "SELLING" if fii_5d < -3000 else "MIXED",
            "rows":       rows[:10],
        }
    except Exception as e:
        return {"error": str(e)}


# ── Bulk & Block Deals ────────────────────────────────────────────────────────
def get_bulk_deals():
    """NSE bulk deals — institutional buy/sell activity today."""
    try:
        s    = _nse_session()
        resp = s.get("https://www.nseindia.com/api/bulk-deals", timeout=10)
        data = resp.json().get("data", [])
        deals = []
        for d in data[:20]:
            deals.append({
                "symbol":   d.get("symbol", ""),
                "client":   d.get("clientName", ""),
                "type":     d.get("buySell", ""),
                "qty":      d.get("quantityTraded", 0),
                "price":    d.get("tradePrice", 0),
            })
        return deals
    except:
        return []


# ── Fear & Greed Index (CNN Money) ────────────────────────────────────────────
def get_fear_greed():
    """Fear & Greed Index via alternative.me (0=extreme fear, 100=extreme greed)."""
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=2",
            timeout=8, headers={"User-Agent": "Mozilla/5.0"}
        )
        data = resp.json().get("data", [])
        if not data:
            return {}
        curr = data[0]
        prev = data[1] if len(data) > 1 else {}
        val  = int(curr.get("value", 0))
        prev_val = int(prev.get("value", val)) if prev else val
        return {
            "score":    val,
            "label":    curr.get("value_classification", ""),
            "previous": prev_val,
            "change":   val - prev_val,
        }
    except:
        pass
    return {}


# ── Combined NSE snapshot ─────────────────────────────────────────────────────
def get_nse_snapshot():
    from concurrent.futures import ThreadPoolExecutor
    results = {}
    def _run(key, fn):
        try: results[key] = fn()
        except: results[key] = {}

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [
            pool.submit(_run, "fii_dii",      get_fii_dii),
            pool.submit(_run, "fii_cumul",    get_fii_cumulative),
            pool.submit(_run, "vix",          get_india_vix),
            pool.submit(_run, "pcr",          get_nifty_pcr),
            pool.submit(_run, "banknifty_pcr",get_banknifty_pcr),
            pool.submit(_run, "fear_greed",   get_fear_greed),
        ]
        for f in futs: f.result(timeout=25)

    return results


if __name__ == "__main__":
    snap = get_nse_snapshot()
    print("FII/DII:", snap.get("fii_dii"))
    print("VIX:",     snap.get("vix"))
    print("PCR:",     snap.get("pcr"))
    print("F&G:",     snap.get("fear_greed"))
    print("Bulk:",    get_bulk_deals()[:3])
