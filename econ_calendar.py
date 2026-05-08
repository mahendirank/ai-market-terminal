"""
econ_calendar.py — Real economic calendar from ForexFactory JSON API.
Provides star-rated (1-2-3 star) events filterable by country.
No API key required. ForexFactory freely publishes JSON.
"""
import requests, time, threading
from datetime import datetime, timezone, timedelta, date
from concurrent.futures import ThreadPoolExecutor, as_completed

IST = timezone(timedelta(hours=5, minutes=30))
_cache_lock = threading.Lock()
_cache: dict = {}
CACHE_TTL   = 1800   # 30 min — FF updates hourly

FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

COUNTRY_META = {
    "USD": {"flag": "🇺🇸", "name": "US",     "label": "US"},
    "EUR": {"flag": "🇪🇺", "name": "EU",     "label": "EU"},
    "GBP": {"flag": "🇬🇧", "name": "UK",     "label": "UK"},
    "JPY": {"flag": "🇯🇵", "name": "JP",     "label": "JP"},
    "AUD": {"flag": "🇦🇺", "name": "AU",     "label": "AU"},
    "CAD": {"flag": "🇨🇦", "name": "CA",     "label": "CA"},
    "CHF": {"flag": "🇨🇭", "name": "CH",     "label": "CH"},
    "NZD": {"flag": "🇳🇿", "name": "NZ",     "label": "NZ"},
    "CNY": {"flag": "🇨🇳", "name": "CN",     "label": "CN"},
    "INR": {"flag": "🇮🇳", "name": "IN",     "label": "IN"},
}

IMPACT_META = {
    "High":    {"stars": 3, "icon": "🔴", "color": "#f87171", "bg": "#1a0a0a", "border": "#7f1d1d"},
    "Medium":  {"stars": 2, "icon": "🟡", "color": "#fbbf24", "bg": "#1a1200", "border": "#d97706"},
    "Low":     {"stars": 1, "icon": "⚪", "color": "#6b7280", "bg": "#0d1117", "border": "#1c2a3a"},
    "Holiday": {"stars": 0, "icon": "📅", "color": "#374151", "bg": "#0d1117", "border": "#1c2a3a"},
}


def _fetch_ff(url: str) -> list:
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code == 200:
            return r.json() or []
    except Exception as e:
        print(f"[econ_calendar] fetch failed {url}: {e}", flush=True)
    return []


def _parse_event(item: dict, today: date) -> dict | None:
    try:
        raw_date = item.get("date", "")
        # FF format: "2025-05-13T00:00:00-0500" or similar ISO
        try:
            dt_utc = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            ev_date = dt_utc.astimezone(IST).date()
        except Exception:
            ev_date = None

        if ev_date is None:
            return None

        days_away = (ev_date - today).days
        currency  = (item.get("country") or "").upper()
        meta_c    = COUNTRY_META.get(currency, {"flag": "🌐", "name": currency, "label": currency})
        impact    = item.get("impact") or "Low"
        meta_i    = IMPACT_META.get(impact, IMPACT_META["Low"])

        raw_time = item.get("time") or "All Day"
        # time is in Eastern US — just show as-is (users know FF uses ET)
        return {
            "date":        ev_date.strftime("%Y-%m-%d"),
            "date_label":  ev_date.strftime("%d %b"),
            "time":        raw_time,
            "country":     currency,
            "flag":        meta_c["flag"],
            "cname":       meta_c["name"],
            "event":       (item.get("title") or item.get("name") or "").strip(),
            "stars":       meta_i["stars"],
            "impact":      impact,
            "icon":        meta_i["icon"],
            "color":       meta_i["color"],
            "bg":          meta_i["bg"],
            "border":      meta_i["border"],
            "actual":      (item.get("actual")   or "").strip(),
            "forecast":    (item.get("forecast") or "").strip(),
            "previous":    (item.get("previous") or "").strip(),
            "days_away":   days_away,
            "is_today":    days_away == 0,
            "is_tomorrow": days_away == 1,
            "days_label":  ("TODAY" if days_away == 0 else
                            "TOMORROW" if days_away == 1 else
                            f"In {days_away}d" if days_away > 0 else
                            f"{abs(days_away)}d ago"),
            "source":      "forexfactory",
        }
    except Exception:
        return None


def _india_fixed(today: date, days_ahead: int) -> list:
    """NSE/RBI/India-specific events not always on ForexFactory."""
    raw = [
        # RBI MPC 2026
        ("2026-06-04", "08:45", "RBI MPC Rate Decision",   "INR", "High",   "Bi-monthly MPC — rate path for CY2026 H2"),
        ("2026-08-06", "08:45", "RBI MPC Rate Decision",   "INR", "High",   "Mid-year policy review"),
        ("2026-10-07", "08:45", "RBI MPC Rate Decision",   "INR", "High",   "Pre-festive season policy"),
        ("2026-12-04", "08:45", "RBI MPC Rate Decision",   "INR", "High",   "Year-end monetary stance"),
        # NSE F&O Expiry (last Thu of each month)
        ("2026-05-28", "15:30", "NSE F&O Monthly Expiry",  "INR", "Medium", "Nifty/BankNifty/Fin monthly settlement"),
        ("2026-06-25", "15:30", "NSE F&O Monthly Expiry",  "INR", "Medium", "Nifty/BankNifty/Fin monthly settlement"),
        ("2026-07-30", "15:30", "NSE F&O Monthly Expiry",  "INR", "Medium", "Nifty/BankNifty/Fin monthly settlement"),
        ("2026-08-27", "15:30", "NSE F&O Monthly Expiry",  "INR", "Medium", "Nifty/BankNifty/Fin monthly settlement"),
        ("2026-09-24", "15:30", "NSE F&O Monthly Expiry",  "INR", "Medium", "Nifty/BankNifty/Fin monthly settlement"),
        ("2026-10-29", "15:30", "NSE F&O Monthly Expiry",  "INR", "Medium", "Nifty/BankNifty/Fin monthly settlement"),
        # India macro data releases
        ("2026-05-12", "17:30", "India CPI Inflation (Apr)","INR", "High",  "Key for RBI rate path; target 4%"),
        ("2026-05-13", "12:00", "US CPI Inflation (Apr)",  "USD", "High",   "Apr CPI — Fed rate expectations"),
        ("2026-05-15", "17:30", "India WPI (Apr)",         "INR", "Medium", "Wholesale Price Index release"),
        ("2026-05-30", "17:30", "India GDP Q4 FY26",       "INR", "High",   "Full-year FY26 GDP estimate"),
        ("2026-06-04", "14:30", "India S&P PMI Composite", "INR", "Medium", "Business activity indicator"),
        ("2026-06-11", "18:30", "US CPI Inflation (May)",  "USD", "High",   "May CPI — Fed rate expectations"),
        ("2026-06-12", "17:30", "India CPI Inflation (May)","INR","High",   "May CPI release"),
        ("2026-06-17", "23:30", "US Fed FOMC Decision",    "USD", "High",   "Fed rate decision — impacts FII/INR"),
        ("2026-06-25", "17:30", "India F&O Expiry",        "INR", "Medium", "Monthly options settlement day"),
        ("2026-07-14", "17:30", "India CPI Inflation (Jun)","INR","High",   "June CPI data"),
        ("2026-07-29", "23:30", "US Fed FOMC Decision",    "USD", "High",   "Fed rate decision"),
        ("2026-09-16", "23:30", "US Fed FOMC Decision",    "USD", "High",   "Fed rate decision"),
        ("2026-11-04", "23:30", "US Fed FOMC Decision",    "USD", "High",   "Fed rate decision"),
    ]
    result = []
    for date_str, t, name, cur, impact, note in raw:
        try:
            ev_date   = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_away = (ev_date - today).days
            if days_away < -1 or days_away > days_ahead:
                continue
            meta_c = COUNTRY_META.get(cur, {"flag": "🇮🇳", "name": "IN", "label": "IN"})
            meta_i = IMPACT_META.get(impact, IMPACT_META["Low"])
            result.append({
                "date":        date_str,
                "date_label":  ev_date.strftime("%d %b"),
                "time":        t,
                "country":     cur,
                "flag":        meta_c["flag"],
                "cname":       meta_c["name"],
                "event":       name,
                "stars":       meta_i["stars"],
                "impact":      impact,
                "icon":        meta_i["icon"],
                "color":       meta_i["color"],
                "bg":          meta_i["bg"],
                "border":      meta_i["border"],
                "actual":      "",
                "forecast":    note,
                "previous":    "",
                "days_away":   days_away,
                "is_today":    days_away == 0,
                "is_tomorrow": days_away == 1,
                "days_label":  ("TODAY" if days_away == 0 else
                                "TOMORROW" if days_away == 1 else
                                f"In {days_away}d" if days_away > 0 else
                                f"{abs(days_away)}d ago"),
                "source": "fixed",
            })
        except Exception:
            pass
    return result


def get_calendar(days_ahead: int = 14) -> dict:
    """
    Returns economic calendar from ForexFactory + India fixed events.
    Response: {"events": [...], "total": n, "generated_at": "...", "source": "forexfactory"}
    Each event has: date, time, country, flag, cname, event, stars, icon, color,
                    actual, forecast, previous, days_away, days_label, is_today
    """
    with _cache_lock:
        entry = _cache.get("calendar")
        if entry and (time.time() - entry["ts"]) < CACHE_TTL:
            return entry["data"]

    today = datetime.now(IST).date()
    events: list = []

    # Fetch FF weeks concurrently
    ff_ok = False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(_fetch_ff, url) for url in FF_URLS]
            for fut in as_completed(futs, timeout=15):
                try:
                    for item in (fut.result() or []):
                        ev = _parse_event(item, today)
                        if ev and -1 <= ev["days_away"] <= days_ahead:
                            events.append(ev)
                            ff_ok = True
                except Exception:
                    pass
    except Exception as e:
        print(f"[econ_calendar] FF pool error: {e}", flush=True)

    # Always merge India-specific fixed events
    india_fixed = _india_fixed(today, days_ahead)
    seen_keys = {(e["date"], e["event"][:30].lower()) for e in events}
    for ev in india_fixed:
        k = (ev["date"], ev["event"][:30].lower())
        if k not in seen_keys:
            events.append(ev)
            seen_keys.add(k)

    # Sort: by date asc, then stars desc
    events.sort(key=lambda x: (x["days_away"] if x["days_away"] is not None else 999, -x["stars"]))

    # Remove duplicates by (date + event name)
    seen2: set = set()
    deduped: list = []
    for ev in events:
        k2 = (ev["date"], ev["event"][:40].lower())
        if k2 not in seen2:
            deduped.append(ev)
            seen2.add(k2)

    result = {
        "events":       deduped,
        "total":        len(deduped),
        "source":       "forexfactory" if ff_ok else "fixed_only",
        "generated_at": datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"),
    }
    with _cache_lock:
        _cache["calendar"] = {"data": result, "ts": time.time()}
    return result
