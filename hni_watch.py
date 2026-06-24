"""
hni_watch.py — Keyword/entity watchlist + pre-market scanner for HNI flow.

Watches the HNI/Telegram feed (WalterBloomberg, Unusual Whales, FinancialJuice,
DreamCatcher) for institutional-flow signals — big-name funds, analyst
initiations/price targets, stake changes, IPOs, and user-tracked names like
SpaceX — and fires an instant Telegram alert the moment one lands, so you catch
pre-US-open moves without having to query the archive.

Two priorities:
  HIGH   — a tracked name or a big institution is named (ARK, Cathie Wood,
           Berkshire, SpaceX, ...). Always alerts.
  MEDIUM — an action term (initiates, price target, raises stake, upgrade...)
           AND a ticker/cashtag is present (e.g. "$SPCX initiated Neutral $170").
           Always alerts.

The watchlist is config-driven: drop a hni_watchlist.json next to this file (or
point HNI_WATCHLIST_FILE at one) to override the defaults below without code
changes. Dedup + Telegram delivery are handled in notify.py.
"""
from __future__ import annotations

import os, json, re
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

_HERE = os.path.dirname(os.path.abspath(__file__))
_WATCHLIST_FILE = os.environ.get("HNI_WATCHLIST_FILE", os.path.join(_HERE, "hni_watchlist.json"))

# ── Default watchlist (lowercase; matched as substrings) ─────────────────────
DEFAULT_WATCH = {
    # Big institutions / star investors → HIGH priority on their own.
    "institutions": [
        "ark invest", "cathie wood", "berkshire", "buffett", "blackrock",
        "vanguard", "state street", "citadel", "bridgewater", "renaissance",
        "point72", "pershing square", "bill ackman", "ackman", "george soros",
        "david tepper", "carl icahn", "icahn", "third point", "elliott management",
        "sovereign wealth", "softbank", "tiger global", "sequoia", "andreessen",
        # Star investors & funds
        "michael burry", "burry", "scion", "stanley druckenmiller", "druckenmiller",
        "david einhorn", "einhorn", "greenlight capital", "coatue", "lone pine",
        "viking global", "baupost", "seth klarman", "klarman", "millennium",
        "marshall wace", "susquehanna", "jane street",
        # Gulf & sovereign funds (relevant to Dubai base + India flows)
        "adia", "abu dhabi investment", "mubadala", "adq", "pif",
        "public investment fund", "qia", "qatar investment", "norges",
        "norway wealth fund", "temasek", "gic",
        # Leading banks / sell-side houses (their US-index calls — S&P 500,
        # Nasdaq 100, Dow 30 — target lifts/cuts, upgrades, etc.)
        "jpmorgan", "jp morgan", "goldman sachs", "goldman", "morgan stanley",
        "citigroup", "bank of america", "bofa", "wells fargo", "ubs group",
        "barclays", "deutsche bank", "hsbc", "jefferies", "wedbush", "evercore",
        "raymond james", "piper sandler",
    ],
    # Names you specifically track → HIGH priority.
    "tracked": [
        "spacex", "starlink", "openai", "anthropic", "stripe", "x corp",
        "neuralink", "xai",
        # Hot private AI / tech names (pre-IPO, big rounds)
        "databricks", "perplexity", "mistral", "bytedance", "tiktok",
        "revolut", "figure ai", "canva", "epic games", "shein", "discord",
        # Specific mega-cap tickers (named — fire HIGH on any mention)
        "nvidia", "tesla", "palantir", "microstrategy", "micro strategy",
        "super micro", "supermicro", "broadcom", "eli lilly", "novo nordisk",
    ],
    # Action verbs → MEDIUM priority *when a ticker is present*.
    "actions": [
        "initiates", "initiated", "initiating coverage", "price target",
        "raises stake", "cuts stake", "ups stake", "new stake", "takes stake",
        "takes a stake", "boosts stake", "trims stake", "discloses stake",
        "acquires", "to acquire", "buys the dip", "buys dip", "sells stake",
        "upgrade", "upgrades", "downgrade", "downgrades", "reiterates",
        "overweight", "underweight", "outperform", "underperform",
        "activist", "files 13d", "files 13g", "13f",
    ],
    # Event terms → MEDIUM priority.
    "events": [
        "ipo", "spac", "files for ipo", "going public", "block trade",
        "secondary offering", "buyback", "tender offer", "merger", "to merge",
        "stake in", "in talks to", "bid for", "takeover",
    ],
    # Earnings / guidance terms → MEDIUM priority (earnings flow matters).
    "earnings": [
        "earnings today", "q1 earnings", "q2 earnings", "q3 earnings",
        "q4 earnings", "reports earnings", "earnings beat", "earnings miss",
        "earnings call", "profit warning", "cuts guidance", "raises guidance",
        "all eyes on", "ahead of earnings", "post earnings", "beats estimates",
        "misses estimates", "tops estimates",
    ],
}

# Tracked tickers (cashtags) → these matter even when only the SYMBOL appears,
# not the company name (e.g. a "$NVDA $SPCX" trending post). Matched against the
# item's detected `tickers` list, so cashtag-only mentions still get captured.
TRACKED_TICKERS = {
    "SPCX", "NVDA", "TSLA", "PLTR", "MSTR", "AVGO", "LLY", "NVO",
}


def _load_watch() -> dict:
    base = {k: list(v) for k, v in DEFAULT_WATCH.items()}
    try:
        if os.path.exists(_WATCHLIST_FILE):
            with open(_WATCHLIST_FILE) as f:
                override = json.load(f)
            # Merge: override lists replace defaults per-key; extra keys ignored.
            for k in base:
                if isinstance(override.get(k), list):
                    base[k] = [str(t).lower() for t in override[k]]
            # Allow a free "extra" bucket of high-priority terms.
            if isinstance(override.get("tracked_extra"), list):
                base["tracked"] += [str(t).lower() for t in override["tracked_extra"]]
    except Exception as e:
        print(f"[hni_watch] watchlist load failed, using defaults: {e}", flush=True)
    return base


WATCH = _load_watch()


def _boundary_re(terms):
    """Compile a regex matching any term on word boundaries (no substring hits
    like 'spac' inside 'airspace' or 'gic' inside 'strategic')."""
    terms = sorted({str(t).lower() for t in terms if t}, key=len, reverse=True)
    if not terms:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(t) for t in terms) + r")(?![a-z0-9])")


_HIGH_RE = _boundary_re(WATCH["institutions"] + WATCH["tracked"])
_CTX_RE  = _boundary_re(WATCH["actions"] + WATCH["events"] + WATCH.get("earnings", []))


# ── Country / market tagging (extensible) ────────────────────────────────────
# Each item is tagged with the countries whose market terms or major companies
# it mentions. To add a country, just add an entry here (or a "countries" block
# in hni_watchlist.json). Terms are matched on word boundaries (so "sap" won't
# hit "sapphire", "fed" won't hit "federal-funds-noise", etc.).
DEFAULT_COUNTRIES = {
    "IN": {"name": "India", "flag": "🇮🇳", "terms": [
        "nifty", "sensex", "bank nifty", "banknifty", "nse", "bse", "sebi",
        "rbi", "rupee", "inr", "dalal street", "mumbai", "india", "indian",
        "reliance", "tata", "tcs", "infosys", "hdfc", "icici", "adani",
        "wipro", "sbi", "bajaj", "zomato", "paytm",
    ]},
    "US": {"name": "USA", "flag": "🇺🇸", "terms": [
        "s&p 500", "s&p500", "s&p", "nasdaq", "dow jones", "dow 30", "dow",
        "wall street", "nyse", "federal reserve", "fed", "fomc", "sec",
        "u.s.", "united states", "new york", "california", "white house",
        "apple", "nvidia", "tesla", "microsoft", "amazon", "alphabet",
        "google", "meta", "jpmorgan", "goldman", "palantir", "broadcom",
        "microstrategy", "qualcomm", "paramount", "oracle", "intel", "amd",
        "boeing", "ford", "disney", "netflix", "coinbase", "openai",
        "anthropic",
    ]},
    "DE": {"name": "Germany", "flag": "🇩🇪", "terms": [
        "dax", "frankfurt", "bundesbank", "germany", "german", "sap",
        "siemens", "volkswagen", "bmw", "mercedes", "allianz", "bayer",
        "adidas", "deutsche bank", "deutsche telekom", "deutsche borse",
        "porsche", "infineon", "rheinmetall",
    ]},
    "JP": {"name": "Japan", "flag": "🇯🇵", "terms": [
        "nikkei", "topix", "boj", "bank of japan", "yen", "tokyo", "japan",
        "japanese", "toyota", "sony", "softbank", "nintendo", "honda",
        "mitsubishi", "keyence", "fast retailing", "hitachi",
    ]},
    "IT": {"name": "Italy", "flag": "🇮🇹", "terms": [
        "ftse mib", "mib", "milan", "borsa italiana", "italy", "italian",
        "ferrari", "eni", "enel", "stellantis", "unicredit", "intesa",
        "generali", "leonardo", "prada", "moncler",
    ]},
    "GB": {"name": "UK", "flag": "🇬🇧", "terms": [
        "ftse 100", "ftse", "london stock exchange", "london", "boe",
        "bank of england", "gilt", "gilts", "pound", "sterling", "gbp",
        "hsbc", "bp", "astrazeneca", "unilever", "barclays", "glencore",
        "rio tinto", "glaxosmithkline", "diageo", "rolls-royce", "vodafone",
        "lloyds", "natwest", "bae systems", "national grid",
    ]},
    "FR": {"name": "France", "flag": "🇫🇷", "terms": [
        "cac 40", "cac40", "cac", "paris", "euronext", "french", "france",
        "lvmh", "hermes", "l'oreal", "loreal", "totalenergies", "airbus",
        "sanofi", "schneider electric", "kering", "bnp paribas", "axa",
        "danone", "michelin", "thales", "capgemini", "saint-gobain",
    ]},
    "CN": {"name": "China", "flag": "🇨🇳", "terms": [
        "shanghai", "shenzhen", "csi 300", "csi300", "hang seng", "hong kong",
        "hsi", "pboc", "yuan", "renminbi", "rmb", "china", "chinese",
        "beijing", "alibaba", "tencent", "baidu", "jd.com", "pinduoduo",
        "byd", "nio", "xpeng", "li auto", "xiaomi", "meituan", "catl",
        "ping an", "icbc",
    ]},
}


def _load_countries() -> dict:
    base = {k: {"name": v["name"], "flag": v["flag"], "terms": list(v["terms"])}
            for k, v in DEFAULT_COUNTRIES.items()}
    try:
        if os.path.exists(_WATCHLIST_FILE):
            with open(_WATCHLIST_FILE) as f:
                override = json.load(f)
            cc = override.get("countries")
            if isinstance(cc, dict):
                for code, v in cc.items():
                    if not isinstance(v, dict):
                        continue
                    entry = base.get(code, {"name": code, "flag": "🏳", "terms": []})
                    entry["name"] = v.get("name", entry["name"])
                    entry["flag"] = v.get("flag", entry.get("flag", "🏳"))
                    if isinstance(v.get("terms"), list):
                        entry["terms"] = [str(t).lower() for t in v["terms"]]
                    base[code] = entry
    except Exception as e:
        print(f"[hni_watch] country config load failed, using defaults: {e}", flush=True)
    # Precompile a boundary regex per country for safe substring-free matching.
    for code, v in base.items():
        terms = sorted({t.lower() for t in v["terms"] if t}, key=len, reverse=True)
        v["_re"] = re.compile(
            r"(?<![a-z0-9])(?:" + "|".join(re.escape(t) for t in terms) + r")(?![a-z0-9])"
        ) if terms else None
    return base


COUNTRIES = _load_countries()


def detect_countries(item: dict) -> list:
    """Return list of country codes this item relates to (by market terms /
    major companies). An item can map to several (e.g. 'Fed cuts, Nikkei pops')."""
    text = (item.get("text") or "").lower()
    out = []
    for code, v in COUNTRIES.items():
        rx = v.get("_re")
        if rx and text and rx.search(text):
            out.append(code)
    # Ticker-based tagging — exchange suffix tells the listing country.
    tickers = [str(t).upper() for t in (item.get("tickers") or [])]
    SUFFIX = {".NS": "IN", ".BO": "IN", ".DE": "DE", ".F": "DE",
              ".T": "JP", ".MI": "IT", ".L": "GB", ".PA": "FR",
              ".SS": "CN", ".SZ": "CN", ".HK": "CN"}
    for t in tickers:
        for suf, code in SUFFIX.items():
            if t.endswith(suf) and code not in out:
                out.append(code)
    # Fallback: a plain cashtag with no exchange suffix ($AVGO, $QCOM) is
    # almost always a US listing in this feed — tag US, but only when no other
    # country was found (so an India item carrying a cashtag isn't double-tagged).
    if not out and any("." not in t and t.isalpha() and 1 <= len(t) <= 5 for t in tickers):
        out.append("US")
    return out


def countries_meta() -> list:
    """Lightweight list for the UI filter buttons (order preserved)."""
    return [{"code": k, "name": v["name"], "flag": v["flag"]} for k, v in COUNTRIES.items()]


def classify(item: dict):
    """Return (matched_terms, priority). priority ∈ {'high','medium',None}."""
    text = (item.get("text") or "").lower()
    if not text:
        return [], None
    # Word-boundary matching (not substring) so "spac" never hits "airspace",
    # "gic" never hits "strategic", "fed" never hits "feedback", etc.
    high_hits = list(dict.fromkeys(_HIGH_RE.findall(text))) if _HIGH_RE else []
    ctx_hits  = list(dict.fromkeys(_CTX_RE.findall(text)))  if _CTX_RE else []

    # Tracked tickers present as cashtags/detected symbols (e.g. "$NVDA").
    item_tickers = [str(t).upper() for t in (item.get("tickers") or [])]
    tk_hits = [t for t in item_tickers if t in TRACKED_TICKERS]

    if high_hits:
        return high_hits + [c for c in ctx_hits if c not in high_hits], "high"

    # MEDIUM (WATCH): any analyst/event/earnings context, OR a tracked ticker.
    # Pure geopolitics (no context term, no tracked ticker) stays excluded.
    if ctx_hits or tk_hits:
        return ctx_hits + [f"${t}" for t in tk_hits], "medium"
    return [], None


# ── US pre-market window (robust to DST via zoneinfo) ────────────────────────

def et_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: assume EDT (UTC-4). Off by 1h in winter — acceptable.
        return datetime.now(timezone.utc) - timedelta(hours=4)


def is_premarket() -> bool:
    """True during US pre-open: 04:00–09:30 ET on a weekday."""
    et = et_now()
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return 4 * 60 <= mins < 9 * 60 + 30


# ── Scanner ──────────────────────────────────────────────────────────────────

def _hni_items() -> list:
    """Pull current HNI items from the warm news cache (already ticker-tagged)."""
    try:
        from news import get_all_news
        return [n for n in get_all_news()
                if isinstance(n, dict) and n.get("category") == "HNI"]
    except Exception as e:
        print(f"[hni_watch] feed read failed: {e}", flush=True)
        return []


def scan_and_alert() -> dict:
    """Scan HNI feed; fire a Telegram alert for each fresh watchlist hit.

    Dedup (persistent, restart-safe) lives in notify.alert_hni_watch, so this
    is safe to call on a tight loop and from multiple processes.
    """
    items = _hni_items()
    premarket = is_premarket()
    checked = matched = sent = 0
    try:
        from notify import alert_hni_watch
    except Exception as e:
        return {"checked": 0, "matched": 0, "sent": 0, "error": str(e)}
    for it in items:
        checked += 1
        terms, prio = classify(it)
        if not prio:
            continue
        matched += 1
        if alert_hni_watch(it, terms, prio, premarket=premarket):
            sent += 1
    return {"checked": checked, "matched": matched, "sent": sent,
            "premarket": premarket}


if __name__ == "__main__":
    print("ET now:", et_now().strftime("%Y-%m-%d %H:%M %Z"), "| pre-market:", is_premarket())
    print("Watch terms loaded:",
          {k: len(v) for k, v in WATCH.items()})
    # quick classify smoke test
    for t in [
        {"text": "$SPCX - CATHIE WOOD BUYS SPACEX DIP. ARK bought 210,000 shares.", "tickers": ["SPCX"]},
        {"text": "$SPCX - SUSQUEHANNA initiates SpaceX Neutral, $170 price target.", "tickers": ["SPCX"]},
        {"text": "Random macro headline about oil inventories.", "tickers": []},
    ]:
        print(classify(t), "|", t["text"][:55])
