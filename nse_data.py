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


# ── Nifty PCR (Put-Call Ratio) ────────────────────────────────────────────────
def get_nifty_pcr():
    """
    PCR < 0.8  → Bearish (too many calls, complacency)
    PCR 0.8–1.2 → Neutral
    PCR > 1.2  → Bullish squeeze (hedged, wall of worry)
    """
    try:
        s    = _nse_session()
        resp = s.get(
            "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
            timeout=12
        )
        oc   = resp.json().get("filtered", {}).get("data", [])
        total_pe_oi = sum(row.get("PE", {}).get("openInterest", 0) for row in oc)
        total_ce_oi = sum(row.get("CE", {}).get("openInterest", 0) for row in oc)
        pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else None

        # Max pain — strike with maximum OI on both sides
        pain = {}
        for row in oc:
            strike = row.get("strikePrice", 0)
            ce_oi  = row.get("CE", {}).get("openInterest", 0)
            pe_oi  = row.get("PE", {}).get("openInterest", 0)
            pain[strike] = ce_oi + pe_oi
        max_pain = max(pain, key=pain.get) if pain else None

        bias = "BULLISH" if pcr and pcr > 1.2 else "BEARISH" if pcr and pcr < 0.8 else "NEUTRAL"
        return {"pcr": pcr, "max_pain": max_pain, "bias": bias,
                "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi}
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

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [
            pool.submit(_run, "fii_dii",    get_fii_dii),
            pool.submit(_run, "vix",        get_india_vix),
            pool.submit(_run, "pcr",        get_nifty_pcr),
            pool.submit(_run, "fear_greed", get_fear_greed),
        ]
        for f in futs: f.result(timeout=20)

    return results


if __name__ == "__main__":
    snap = get_nse_snapshot()
    print("FII/DII:", snap.get("fii_dii"))
    print("VIX:",     snap.get("vix"))
    print("PCR:",     snap.get("pcr"))
    print("F&G:",     snap.get("fear_greed"))
    print("Bulk:",    get_bulk_deals()[:3])
