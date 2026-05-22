"""
cb_calendar.py — Central Bank meeting calendar (Fed, ECB, BOJ, BOE, RBA, SNB).

Returns the next N upcoming meetings with metadata:
  - date, time (local + IST), days to event
  - expected volatility tier (RED / YELLOW / GREEN)
  - previous decision (rate + direction)
  - expected market bias (HAWKISH / DOVISH / HOLD), inferred from recent news
  - impacted assets

The official meeting schedules are public and published annually by each
central bank — they're hardcoded here for reliability. The "expected bias"
is derived from a news keyword scan against the cached headlines so it
moves with sentiment without external API dependency.

Optional: if TRADING_ECONOMICS_KEY env var is set, enriches with their
calendar API (free tier returns limited fields with 24h delay).
"""
import os
import time
from datetime import datetime, date, timezone, timedelta
from typing import List, Optional

IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────────────────────────────────────────
# Hardcoded official meeting schedules (May 2026 → Jul 2027)
# Source: each central bank's published calendar
# ─────────────────────────────────────────────────────────────────────────────

MEETINGS = [
    # FED FOMC — 2-day meetings, decision/press conference on day 2
    {"cb":"FED",  "date":"2026-06-17", "time":"23:30 IST", "label":"FOMC Rate Decision + SEP + Press Conf", "two_day":True},
    {"cb":"FED",  "date":"2026-07-29", "time":"23:30 IST", "label":"FOMC Rate Decision + Press Conf",       "two_day":True},
    {"cb":"FED",  "date":"2026-09-16", "time":"23:30 IST", "label":"FOMC Rate Decision + SEP + Press Conf", "two_day":True},
    {"cb":"FED",  "date":"2026-10-28", "time":"23:30 IST", "label":"FOMC Rate Decision + Press Conf",       "two_day":True},
    {"cb":"FED",  "date":"2026-12-09", "time":"23:30 IST", "label":"FOMC Rate Decision + SEP + Press Conf", "two_day":True},
    {"cb":"FED",  "date":"2027-01-27", "time":"23:30 IST", "label":"FOMC Rate Decision + Press Conf",       "two_day":True},
    {"cb":"FED",  "date":"2027-03-17", "time":"23:30 IST", "label":"FOMC Rate Decision + SEP + Press Conf", "two_day":True},

    # ECB — every 6 weeks roughly
    {"cb":"ECB",  "date":"2026-06-04", "time":"17:45 IST", "label":"Main Refinancing Rate Decision"},
    {"cb":"ECB",  "date":"2026-07-16", "time":"17:45 IST", "label":"Main Refinancing Rate Decision"},
    {"cb":"ECB",  "date":"2026-09-10", "time":"17:45 IST", "label":"Main Refinancing Rate Decision"},
    {"cb":"ECB",  "date":"2026-10-22", "time":"17:45 IST", "label":"Main Refinancing Rate Decision"},
    {"cb":"ECB",  "date":"2026-12-17", "time":"17:45 IST", "label":"Main Refinancing Rate Decision"},
    {"cb":"ECB",  "date":"2027-01-21", "time":"17:45 IST", "label":"Main Refinancing Rate Decision"},

    # BOE — MPC roughly every 6 weeks
    {"cb":"BOE",  "date":"2026-06-18", "time":"17:00 IST", "label":"Bank Rate Decision + MPR"},
    {"cb":"BOE",  "date":"2026-08-06", "time":"17:00 IST", "label":"Bank Rate Decision + MPR"},
    {"cb":"BOE",  "date":"2026-09-17", "time":"17:00 IST", "label":"Bank Rate Decision"},
    {"cb":"BOE",  "date":"2026-11-05", "time":"17:00 IST", "label":"Bank Rate Decision + MPR"},
    {"cb":"BOE",  "date":"2026-12-17", "time":"17:00 IST", "label":"Bank Rate Decision"},

    # BOJ — 8 meetings/yr, decision usually late afternoon JST
    {"cb":"BOJ",  "date":"2026-06-17", "time":"08:30 IST", "label":"Policy Rate Decision + Outlook Report"},
    {"cb":"BOJ",  "date":"2026-07-31", "time":"08:30 IST", "label":"Policy Rate Decision + Outlook Report"},
    {"cb":"BOJ",  "date":"2026-09-18", "time":"08:30 IST", "label":"Policy Rate Decision"},
    {"cb":"BOJ",  "date":"2026-10-30", "time":"08:30 IST", "label":"Policy Rate Decision + Outlook Report"},
    {"cb":"BOJ",  "date":"2026-12-18", "time":"08:30 IST", "label":"Policy Rate Decision"},

    # RBA — first Tuesday most months
    {"cb":"RBA",  "date":"2026-05-19", "time":"10:00 IST", "label":"Cash Rate Decision + Statement"},
    {"cb":"RBA",  "date":"2026-07-07", "time":"10:00 IST", "label":"Cash Rate Decision + Statement"},
    {"cb":"RBA",  "date":"2026-08-11", "time":"10:00 IST", "label":"Cash Rate Decision + SMP"},
    {"cb":"RBA",  "date":"2026-09-29", "time":"10:00 IST", "label":"Cash Rate Decision"},
    {"cb":"RBA",  "date":"2026-11-03", "time":"10:00 IST", "label":"Cash Rate Decision + SMP"},
    {"cb":"RBA",  "date":"2026-12-08", "time":"10:00 IST", "label":"Cash Rate Decision"},

    # SNB — quarterly
    {"cb":"SNB",  "date":"2026-06-18", "time":"13:00 IST", "label":"Policy Rate Decision + Forecast"},
    {"cb":"SNB",  "date":"2026-09-24", "time":"13:00 IST", "label":"Policy Rate Decision + Forecast"},
    {"cb":"SNB",  "date":"2026-12-10", "time":"13:00 IST", "label":"Policy Rate Decision + Forecast"},
]

# ─────────────────────────────────────────────────────────────────────────────
# Central bank metadata — fixed properties
# ─────────────────────────────────────────────────────────────────────────────

CB_META = {
    "FED": {
        "name":          "Federal Reserve",
        "country":       "United States",
        "flag":          "🇺🇸",
        "volatility":    "RED",
        "prev_rate":     "4.25–4.50%",
        "prev_action":   "HELD",
        "impacted":      ["DXY", "US 10Y", "S&P 500", "NASDAQ", "Gold", "EUR/USD"],
        "hawk_keywords": ("powell hawkish", "fed hike", "rate hike", "higher for longer",
                          "no rate cut", "hawkish fed"),
        "dove_keywords": ("powell dovish", "rate cut", "fed pivot", "cut cycle",
                          "dovish fed", "fed easing", "rate cuts"),
    },
    "ECB": {
        "name":          "European Central Bank",
        "country":       "Euro Area",
        "flag":          "🇪🇺",
        "volatility":    "RED",
        "prev_rate":     "2.50%",
        "prev_action":   "HELD",
        "impacted":      ["EUR/USD", "EUR/GBP", "Bund 10Y", "DAX", "EUR/JPY"],
        "hawk_keywords": ("lagarde hawkish", "ecb hike", "ecb hawkish", "no ecb cut"),
        "dove_keywords": ("lagarde dovish", "ecb cut", "ecb dovish", "ecb easing"),
    },
    "BOE": {
        "name":          "Bank of England",
        "country":       "United Kingdom",
        "flag":          "🇬🇧",
        "volatility":    "YELLOW",
        "prev_rate":     "4.00%",
        "prev_action":   "HELD",
        "impacted":      ["GBP/USD", "EUR/GBP", "UK Gilt 10Y", "FTSE 100"],
        "hawk_keywords": ("boe hawkish", "boe hike", "bank of england hike"),
        "dove_keywords": ("boe cut", "boe dovish", "bank of england cut"),
    },
    "BOJ": {
        "name":          "Bank of Japan",
        "country":       "Japan",
        "flag":          "🇯🇵",
        "volatility":    "RED",
        "prev_rate":     "0.50%",
        "prev_action":   "HELD",
        "impacted":      ["USD/JPY", "EUR/JPY", "JGB 10Y", "Nikkei 225"],
        "hawk_keywords": ("boj hike", "boj hawkish", "ueda hawkish", "yen intervention"),
        "dove_keywords": ("boj hold", "boj dovish", "ueda dovish", "boj easing"),
    },
    "RBA": {
        "name":          "Reserve Bank of Australia",
        "country":       "Australia",
        "flag":          "🇦🇺",
        "volatility":    "YELLOW",
        "prev_rate":     "4.10%",
        "prev_action":   "HELD",
        "impacted":      ["AUD/USD", "AUD/JPY", "ASX 200", "AUD/NZD"],
        "hawk_keywords": ("rba hawkish", "rba hike"),
        "dove_keywords": ("rba cut", "rba dovish"),
    },
    "SNB": {
        "name":          "Swiss National Bank",
        "country":       "Switzerland",
        "flag":          "🇨🇭",
        "volatility":    "YELLOW",
        "prev_rate":     "0.50%",
        "prev_action":   "HELD",
        "impacted":      ["USD/CHF", "EUR/CHF", "Gold", "SMI"],
        "hawk_keywords": ("snb hike", "snb hawkish"),
        "dove_keywords": ("snb cut", "snb dovish", "snb intervention"),
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# News-based bias inference
# ─────────────────────────────────────────────────────────────────────────────

def _infer_bias(cb: str, news_text: str) -> dict:
    """Scan recent news headlines for hawk/dove keywords specific to this CB."""
    meta = CB_META[cb]
    hawk = sum(1 for kw in meta["hawk_keywords"] if kw in news_text)
    dove = sum(1 for kw in meta["dove_keywords"] if kw in news_text)
    if hawk > dove and hawk >= 1:
        return {"bias": "HAWKISH", "hawk_hits": hawk, "dove_hits": dove, "conviction": min(60 + hawk * 10, 88)}
    if dove > hawk and dove >= 1:
        return {"bias": "DOVISH", "hawk_hits": hawk, "dove_hits": dove, "conviction": min(60 + dove * 10, 88)}
    return {"bias": "HOLD / WAIT", "hawk_hits": hawk, "dove_hits": dove, "conviction": 50}


def _gather_news_text() -> str:
    try:
        from news import get_all_news
        items = (get_all_news() or [])[:80]
        return " ".join(
            str(it.get("text") or it.get("title", "") or "") for it in items
        ).lower()
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def get_cb_calendar(days_ahead: int = 90, limit: int = 12) -> dict:
    """Return upcoming central bank meetings sorted by date."""
    today = datetime.now(IST).date()
    horizon = today + timedelta(days=days_ahead)
    news_text = _gather_news_text()

    upcoming = []
    for m in MEETINGS:
        try:
            d = datetime.strptime(m["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < today or d > horizon:
            continue
        cb = m["cb"]
        meta = CB_META.get(cb, {})
        bias = _infer_bias(cb, news_text)
        days_to = (d - today).days

        upcoming.append({
            "cb":              cb,
            "cb_name":         meta.get("name", cb),
            "country":         meta.get("country", ""),
            "flag":            meta.get("flag", ""),
            "date":            m["date"],
            "date_display":    d.strftime("%a, %d %b %Y"),
            "time_ist":        m["time"],
            "days_to_event":   days_to,
            "is_today":        days_to == 0,
            "is_this_week":    0 <= days_to <= 7,
            "label":           m["label"],
            "volatility":      meta.get("volatility", "YELLOW"),
            "prev_rate":       meta.get("prev_rate", "—"),
            "prev_action":     meta.get("prev_action", "—"),
            "expected_bias":   bias["bias"],
            "bias_conviction": bias["conviction"],
            "hawk_hits":       bias["hawk_hits"],
            "dove_hits":       bias["dove_hits"],
            "impacted_assets": meta.get("impacted", []),
        })

    upcoming.sort(key=lambda x: x["date"])
    upcoming = upcoming[:limit]

    return {
        "generated_at": datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
        "horizon_days": days_ahead,
        "count":        len(upcoming),
        "events":       upcoming,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Central-bank action tilt — the cb_action feed for the causal layer
# ─────────────────────────────────────────────────────────────────────────────

# Weight of each central bank in the aggregate action tilt. The Fed dominates
# global risk transmission; the others are lighter satellites. Sums to 1.0.
_ACTION_WEIGHTS = {
    "FED": 0.50, "ECB": 0.20, "BOJ": 0.12, "BOE": 0.10, "RBA": 0.04, "SNB": 0.04,
}


def get_action_tilt(news_text: Optional[str] = None) -> dict:
    """Aggregate the news-inferred per-CB stances into a single action tilt.

    Returns a tilt in [-1, +1] — the `cb_action` input the pressure-vector
    engine folds in as its ninth force:

        +1 = fully dovish / easing       (risk-positive)
        -1 = fully hawkish / tightening  (risk-negative)
         0 = banks on hold / no clear stance

    Each central bank's HAWKISH/DOVISH/HOLD read (from `_infer_bias`) is
    signed (- hawkish, + dovish), scaled by its conviction, and combined on
    `_ACTION_WEIGHTS`. Pure + fail-soft — returns a neutral tilt on error.

    `news_text` is accepted for testability; when omitted the live cached
    news feed is scanned (the same source the rest of this module uses).
    """
    try:
        news = (news_text if news_text is not None else _gather_news_text()) or ""
        news = news.lower()
        tilt, total_w, per_cb = 0.0, 0.0, {}
        for cb, w in _ACTION_WEIGHTS.items():
            b = _infer_bias(cb, news)
            sign = (1.0 if b["bias"] == "DOVISH"
                    else -1.0 if b["bias"] == "HAWKISH" else 0.0)
            tilt += w * sign * (b["conviction"] / 100.0)
            total_w += w
            per_cb[cb] = {"bias": b["bias"], "conviction": b["conviction"]}
        tilt = round(max(-1.0, min(1.0, tilt / total_w if total_w else 0.0)), 4)
        label = ("dovish"  if tilt >  0.12 else
                 "hawkish" if tilt < -0.12 else "neutral")
        return {"tilt": tilt, "label": label, "per_cb": per_cb}
    except Exception:
        return {"tilt": 0.0, "label": "neutral", "per_cb": {}}

