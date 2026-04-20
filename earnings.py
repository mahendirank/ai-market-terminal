import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yfinance as yf
import pandas as pd
from datetime import datetime, timezone

WATCH_LIST = {
    "MAG7":       ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"],
    "FINANCIALS": ["JPM", "GS", "BAC", "MS", "V", "MA", "C"],
    "ENERGY":     ["XOM", "CVX", "SLB", "BP"],
    "HEALTHCARE": ["JNJ", "PFE", "UNH", "MRK"],
    "SEMIS":      ["TSM", "AMD", "INTC", "QCOM", "AMAT", "MU"],
    "INDIA_ADR":  ["INFY", "WIT", "HDB", "IBN"],
}


def _fmt_b(v):
    """Format value in billions."""
    if v is None:
        return "—"
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:.0f}"


def _bps(old, new):
    """Margin change in basis points."""
    if old is None or new is None:
        return None
    return round((new - old) * 10000)


def _score(eps_surp, rev_surp, guidance):
    """Bloomberg-style score 0-100."""
    s = 50
    if eps_surp is not None:
        if   eps_surp > 15: s += 25
        elif eps_surp > 8:  s += 18
        elif eps_surp > 3:  s += 10
        elif eps_surp > 0:  s += 5
        elif eps_surp < -15: s -= 25
        elif eps_surp < -8:  s -= 18
        elif eps_surp < -3:  s -= 10
        else:                 s -= 5
    if rev_surp is not None:
        if   rev_surp > 8: s += 15
        elif rev_surp > 4: s += 10
        elif rev_surp > 0: s += 5
        elif rev_surp < -8: s -= 15
        elif rev_surp < -4: s -= 10
        else:                s -= 5
    if guidance == "raised":  s += 10
    if guidance == "lowered": s -= 10
    return max(0, min(100, s))


def _stars(score):
    """★★★★★ style rating."""
    n = round(score / 20)
    return "★" * n + "☆" * (5 - n)


def get_single(sym, group):
    try:
        t    = yf.Ticker(sym)
        info = t.info or {}

        name    = info.get("shortName", sym)[:22]
        sector  = info.get("sector", "")
        price   = info.get("currentPrice") or info.get("regularMarketPrice")

        # Earnings dates (EPS est / actual / surprise)
        eps_est = eps_act = surprise_pct = earn_date = None
        try:
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                now  = datetime.now(timezone.utc)
                idx  = pd.to_datetime(ed.index, utc=True)
                past = ed[idx < now]
                if not past.empty:
                    row          = past.iloc[0]
                    eps_est      = row.get("EPS Estimate")
                    eps_act      = row.get("Reported EPS")
                    surprise_pct = row.get("Surprise(%)")
                    earn_date    = str(past.index[0])[:7]   # YYYY-MM
        except:
            pass

        # Revenue & gross margin from quarterly financials
        rev_act = gross_margin_curr = gross_margin_prev = None
        try:
            qf = t.quarterly_financials
            if qf is not None and not qf.empty:
                if "Total Revenue" in qf.index:
                    cols = qf.loc["Total Revenue"].dropna()
                    if len(cols) >= 1:
                        rev_act = float(cols.iloc[0])
                if "Gross Profit" in qf.index and "Total Revenue" in qf.index:
                    rev_col = qf.loc["Total Revenue"].dropna()
                    gp_col  = qf.loc["Gross Profit"].dropna()
                    if len(rev_col) >= 1 and len(gp_col) >= 1 and float(rev_col.iloc[0]) > 0:
                        gross_margin_curr = float(gp_col.iloc[0]) / float(rev_col.iloc[0])
                    if len(rev_col) >= 2 and len(gp_col) >= 2 and float(rev_col.iloc[1]) > 0:
                        gross_margin_prev = float(gp_col.iloc[1]) / float(rev_col.iloc[1])
        except:
            pass

        margin_bps = _bps(gross_margin_prev, gross_margin_curr)

        # Forward guidance
        fwd_eps = info.get("forwardEps")
        fwd_pe  = info.get("forwardPE")
        rev_growth = info.get("revenueGrowth")
        earn_growth = info.get("earningsGrowth")
        rec    = info.get("recommendationKey", "").upper()
        target = info.get("targetMeanPrice")

        # Rough guidance signal
        if earn_growth and earn_growth > 0.3:
            guidance = "raised"
        elif earn_growth and earn_growth < -0.1:
            guidance = "lowered"
        else:
            guidance = "maintained"

        score = _score(
            float(surprise_pct) if surprise_pct else None,
            None,
            guidance,
        )

        return {
            "symbol":       sym,
            "name":         name,
            "group":        group,
            "sector":       sector,
            "price":        round(float(price), 2) if price else None,
            "earn_date":    earn_date or "—",
            "eps_est":      round(float(eps_est), 2) if eps_est is not None else None,
            "eps_act":      round(float(eps_act), 2) if eps_act is not None else None,
            "eps_beat":     round(float(surprise_pct), 1) if surprise_pct is not None else None,
            "revenue":      _fmt_b(rev_act),
            "rev_growth":   round(rev_growth * 100, 1) if rev_growth else None,
            "earn_growth":  round(earn_growth * 100, 1) if earn_growth else None,
            "gross_margin": round(gross_margin_curr * 100, 1) if gross_margin_curr else None,
            "margin_bps":   margin_bps,
            "fwd_eps":      round(float(fwd_eps), 2) if fwd_eps else None,
            "fwd_pe":       round(float(fwd_pe), 1) if fwd_pe else None,
            "analyst_rec":  rec,
            "price_target": round(float(target), 2) if target else None,
            "guidance":     guidance,
            "score":        score,
            "stars":        _stars(score),
        }
    except:
        return None


def get_earnings():
    results = []
    for group, syms in WATCH_LIST.items():
        for sym in syms:
            r = get_single(sym, group)
            if r:
                results.append(r)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


if __name__ == "__main__":
    data = get_earnings()
    for d in data[:5]:
        print(f"{d['symbol']:6} | {d['earn_date']} | EPS {d['eps_est']} → {d['eps_act']} ({d['eps_beat']}%) | Rev {d['revenue']} | {d['stars']} {d['score']}")
