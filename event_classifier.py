"""
event_classifier.py — Structured event taxonomy for the intel layer.

Classifies a news headline or event into a fixed taxonomy with severity,
expected direction, time-to-impact, and affected assets. Replaces the raw
keyword scoring in news.py with something AI tabs can reason from.

Categories (CAT_*):
  MONETARY      — Fed/ECB/BOJ/RBI rate decisions, dovish/hawkish surprises
  INFLATION     — CPI/PCE/PPI prints, wage data, inflation expectations
  EMPLOYMENT    — NFP, JOLTS, jobless claims
  GROWTH        — GDP, retail sales, PMI, industrial production
  GEOPOLITICAL  — war, sanctions, trade tensions, OPEC, regulatory
  EARNINGS      — corporate earnings beats/misses, guidance
  CORPORATE     — M&A, bankruptcy, executive moves, lawsuits
  LIQUIDITY     — repo, BOJ intervention, central bank balance sheet, QT/QE
  COMMODITIES   — oil supply, OPEC+ decisions, metals, ag, weather
  CRYPTO        — major crypto regulatory / ETF / hack / fork events
  RISK_EVENT    — flash crashes, circuit breakers, bank failures
  TECHNICAL     — price-action milestones (52w high, gap, break)

Severity (0-10):
  10 — market-moving in next 24h (Fed decision, war, bank failure)
   7-9 — significant for affected assets
   4-6 — moderate / sector-level
   1-3 — minor / noise

Direction tags:
  BULL_*  — supports buying the affected assets
  BEAR_*  — supports selling
  TWO_WAY — depends on the print vs expectation
  NEUTRAL — informational

Pure-Python, rule-based first pass. An optional LLM second-pass hook is left
for future enhancement; the rule pass is the fast path that runs on every
news item.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ─── Category constants ─────────────────────────────────────────────────────
CAT_MONETARY     = "MONETARY"
CAT_INFLATION    = "INFLATION"
CAT_EMPLOYMENT   = "EMPLOYMENT"
CAT_GROWTH       = "GROWTH"
CAT_GEOPOLITICAL = "GEOPOLITICAL"
CAT_EARNINGS     = "EARNINGS"
CAT_CORPORATE    = "CORPORATE"
CAT_LIQUIDITY    = "LIQUIDITY"
CAT_COMMODITIES  = "COMMODITIES"
CAT_CRYPTO       = "CRYPTO"
CAT_RISK_EVENT   = "RISK_EVENT"
CAT_TECHNICAL    = "TECHNICAL"
CAT_UNKNOWN      = "UNKNOWN"


# ─── Pattern → (category, base_severity, direction, asset_tags) rules ───────
# Direction tags use coarse "BULL_*" / "BEAR_*" / "TWO_WAY" / "NEUTRAL" so the
# sentiment_weighting module can roll them up. Asset tags map to keys the
# rest of the system recognises (matches sentiment_weighting._KNOWN_ASSETS).
_RULES: list[tuple[re.Pattern, str, int, str, list[str]]] = [
    # ── Monetary policy ──
    (re.compile(r"\b(fed|federal reserve|fomc)\b.*(rate|decision|minutes|meeting|hike|cut)", re.I),
        CAT_MONETARY, 9, "TWO_WAY", ["DXY", "SPX", "GOLD", "US10Y"]),
    (re.compile(r"\b(powell|jpow|chair powell)\b", re.I),
        CAT_MONETARY, 8, "TWO_WAY", ["DXY", "SPX", "GOLD"]),
    (re.compile(r"\b(ecb|lagarde)\b.*(rate|decision|hike|cut)", re.I),
        CAT_MONETARY, 7, "TWO_WAY", ["EUR", "EURUSD", "DXY"]),
    (re.compile(r"\b(boj|bank of japan|ueda)\b.*(rate|policy|intervention|yc[ \-]?c|yield curve)", re.I),
        CAT_MONETARY, 8, "TWO_WAY", ["JPY", "USDJPY", "DXY"]),
    (re.compile(r"\b(rbi|reserve bank of india|das)\b.*(rate|policy)", re.I),
        CAT_MONETARY, 7, "TWO_WAY", ["NIFTY", "INR", "USDINR"]),
    (re.compile(r"\b(rate (hike|hikes|increase|raised))\b", re.I),
        CAT_MONETARY, 7, "BEAR_RISK", ["SPX", "NIFTY", "GOLD"]),
    (re.compile(r"\b(rate (cut|cuts|reduction|lowered)|easing|dovish|pivot)\b", re.I),
        CAT_MONETARY, 7, "BULL_RISK", ["SPX", "NIFTY", "GOLD", "BTC"]),
    (re.compile(r"\b(hawkish|tightening|higher for longer)\b", re.I),
        CAT_MONETARY, 6, "BEAR_RISK", ["SPX", "GOLD"]),

    # ── Inflation ──
    (re.compile(r"\b(cpi|inflation)\b.*(rises|jumps|surges|hot|above expectations|beat)", re.I),
        CAT_INFLATION, 8, "BEAR_RISK", ["SPX", "GOLD", "DXY"]),
    (re.compile(r"\b(cpi|inflation)\b.*(cools|eases|drops|falls|below expectations|miss)", re.I),
        CAT_INFLATION, 8, "BULL_RISK", ["SPX", "NIFTY", "BTC"]),
    (re.compile(r"\b(pce|core pce|pcepi)\b", re.I),
        CAT_INFLATION, 7, "TWO_WAY", ["SPX", "GOLD", "DXY"]),
    (re.compile(r"\b(ppi|producer price)\b", re.I),
        CAT_INFLATION, 5, "TWO_WAY", ["SPX"]),
    (re.compile(r"\b(wage|wages|average hourly earnings)\b", re.I),
        CAT_INFLATION, 5, "TWO_WAY", ["SPX", "DXY"]),

    # ── Employment ──
    (re.compile(r"\b(nfp|non[ -]?farm|payrolls?|jobs report)\b", re.I),
        CAT_EMPLOYMENT, 9, "TWO_WAY", ["DXY", "SPX", "GOLD"]),
    (re.compile(r"\b(jobless claims|initial claims|unemployment)\b", re.I),
        CAT_EMPLOYMENT, 6, "TWO_WAY", ["SPX", "DXY"]),
    (re.compile(r"\b(jolts|job openings)\b", re.I),
        CAT_EMPLOYMENT, 5, "TWO_WAY", ["SPX"]),

    # ── Growth ──
    (re.compile(r"\b(gdp|gross domestic)\b", re.I),
        CAT_GROWTH, 7, "TWO_WAY", ["SPX", "DXY"]),
    (re.compile(r"\b(retail sales|consumer spending)\b", re.I),
        CAT_GROWTH, 6, "TWO_WAY", ["SPX", "DXY"]),
    (re.compile(r"\b(pmi|ism)\b.*(manufacturing|services)", re.I),
        CAT_GROWTH, 6, "TWO_WAY", ["SPX"]),
    (re.compile(r"\b(industrial production|manufacturing output)\b", re.I),
        CAT_GROWTH, 4, "TWO_WAY", ["SPX"]),

    # ── Geopolitical ──
    (re.compile(r"\b(war|invasion|missile|attack|strike|airstrike)\b", re.I),
        CAT_GEOPOLITICAL, 9, "BEAR_RISK", ["SPX", "GOLD", "OIL", "DXY"]),
    (re.compile(r"\b(sanction|sanctions|embargo)\b", re.I),
        CAT_GEOPOLITICAL, 7, "BEAR_RISK", ["OIL", "GOLD"]),
    (re.compile(r"\b(tariff|tariffs|trade war|trade tension)\b", re.I),
        CAT_GEOPOLITICAL, 8, "BEAR_RISK", ["SPX", "GOLD"]),
    (re.compile(r"\b(opec|opec\+|saudi.*oil|cartel)\b", re.I),
        CAT_GEOPOLITICAL, 7, "TWO_WAY", ["OIL"]),
    (re.compile(r"\b(ceasefire|peace deal|de-?escalation)\b", re.I),
        CAT_GEOPOLITICAL, 7, "BULL_RISK", ["SPX", "OIL"]),

    # ── Earnings ──
    (re.compile(r"\b(beats? estimates|beats? expectations|beats? forecast|earnings beat)\b", re.I),
        CAT_EARNINGS, 6, "BULL_NAME", []),
    (re.compile(r"\b(miss(?:es)? estimates|miss(?:es)? expectations|earnings miss|disappointing)\b", re.I),
        CAT_EARNINGS, 6, "BEAR_NAME", []),
    (re.compile(r"\b(raises? guidance|upgrades? outlook|guides? higher)\b", re.I),
        CAT_EARNINGS, 7, "BULL_NAME", []),
    (re.compile(r"\b(cuts? guidance|lowers? outlook|guides? lower|warns)\b", re.I),
        CAT_EARNINGS, 7, "BEAR_NAME", []),
    (re.compile(r"\b(earnings|q[1-4] results|quarterly results)\b", re.I),
        CAT_EARNINGS, 4, "TWO_WAY", []),

    # ── Corporate ──
    (re.compile(r"\b(acquire|acquisition|merger|takeover)\b", re.I),
        CAT_CORPORATE, 6, "BULL_NAME", []),
    (re.compile(r"\b(bankruptcy|chapter 11|insolvency|files for protection)\b", re.I),
        CAT_CORPORATE, 8, "BEAR_NAME", []),
    (re.compile(r"\b(stock split|split-?adjusted|share buyback|dividend (raise|increase))\b", re.I),
        CAT_CORPORATE, 5, "BULL_NAME", []),
    (re.compile(r"\b(sec (charges?|fines?|investigation)|fraud|lawsuit|class action)\b", re.I),
        CAT_CORPORATE, 6, "BEAR_NAME", []),
    (re.compile(r"\b(ceo (steps down|resigns|fired|departed)|executive (departure|change))\b", re.I),
        CAT_CORPORATE, 5, "BEAR_NAME", []),

    # ── Liquidity ──
    (re.compile(r"\b(quantitative easing|qe|balance sheet expansion|liquidity injection)\b", re.I),
        CAT_LIQUIDITY, 8, "BULL_RISK", ["SPX", "GOLD", "BTC"]),
    (re.compile(r"\b(quantitative tightening|qt|balance sheet runoff|drains liquidity)\b", re.I),
        CAT_LIQUIDITY, 7, "BEAR_RISK", ["SPX", "BTC"]),
    (re.compile(r"\b(intervention|moF intervention|fx intervention|currency intervention)\b", re.I),
        CAT_LIQUIDITY, 7, "TWO_WAY", ["USDJPY", "DXY"]),
    (re.compile(r"\b(repo (operation|rates?)|reverse repo)\b", re.I),
        CAT_LIQUIDITY, 5, "TWO_WAY", ["SPX"]),

    # ── Commodities-specific ──
    (re.compile(r"\b(opec\+? (cut|reduction|extension))\b", re.I),
        CAT_COMMODITIES, 8, "BULL_NAME", ["OIL"]),
    (re.compile(r"\b(strategic petroleum reserve|spr (release|drawdown))\b", re.I),
        CAT_COMMODITIES, 6, "BEAR_NAME", ["OIL"]),
    (re.compile(r"\b(api (inventory|crude)|eia (inventory|crude))\b", re.I),
        CAT_COMMODITIES, 5, "TWO_WAY", ["OIL"]),
    (re.compile(r"\b(weather|drought|hurricane|frost).*(crop|wheat|corn|soy)", re.I),
        CAT_COMMODITIES, 5, "TWO_WAY", []),

    # ── Crypto ──
    (re.compile(r"\b(bitcoin etf|btc etf|spot etf approval|sec approves.*etf)\b", re.I),
        CAT_CRYPTO, 8, "BULL_NAME", ["BTC", "ETH"]),
    (re.compile(r"\b(crypto (ban|crackdown|regulation)|exchange (hack|seizure))\b", re.I),
        CAT_CRYPTO, 7, "BEAR_NAME", ["BTC", "ETH"]),
    (re.compile(r"\b(halving|merge|hard fork)\b", re.I),
        CAT_CRYPTO, 6, "BULL_NAME", ["BTC", "ETH"]),

    # ── Risk events ──
    (re.compile(r"\b(flash crash|circuit breaker|trading halt|market halt)\b", re.I),
        CAT_RISK_EVENT, 10, "BEAR_RISK", ["SPX", "NIFTY", "BTC"]),
    (re.compile(r"\b(bank (failure|run|collapse|seized|fdic))\b", re.I),
        CAT_RISK_EVENT, 10, "BEAR_RISK", ["SPX", "GOLD"]),
    (re.compile(r"\b(default|credit event|debt ceiling|government shutdown)\b", re.I),
        CAT_RISK_EVENT, 9, "BEAR_RISK", ["SPX", "DXY"]),

    # ── Technical / price action ──
    (re.compile(r"\b(52[ \-]?week (high|low)|all[ \-]?time high|record high)\b", re.I),
        CAT_TECHNICAL, 4, "BULL_NAME", []),
    (re.compile(r"\b(gap (up|down)|breakout|breakdown)\b", re.I),
        CAT_TECHNICAL, 3, "TWO_WAY", []),
]


# ─── Source credibility — used by news_deduper + sentiment_weighting ─────────
# Higher = more trusted. Tier 0 reserved for "unknown source".
SOURCE_CREDIBILITY: dict[str, int] = {
    # Tier 5 — wire services and regulator-tier
    "Reuters": 5, "Bloomberg": 5, "WSJ": 5, "Financial Times": 5, "FT": 5,
    "AP": 5, "Associated Press": 5, "Dow Jones": 5,
    # Tier 4 — major outlets
    "CNBC": 4, "Barron's": 4, "MarketWatch": 4, "Yahoo Finance": 4,
    "Forbes": 4, "Business Insider": 4, "Bloomberg Terminal": 4,
    # Tier 3 — sector specialists
    "Coindesk": 3, "The Block": 3, "Decrypt": 3, "Investing.com": 3,
    "Benzinga": 3, "Seeking Alpha": 3, "Zacks": 3,
    # Tier 3 — Indian outlets
    "PTI": 3, "Moneycontrol": 3, "Economic Times": 3, "Mint": 3,
    "Business Standard": 3, "LiveMint": 3, "ET Markets": 3,
    # Tier 2 — second-tier
    "Walter Bloomberg": 2, "WalterBloomberg": 2, "Zerohedge": 2,
    "Trading Economics": 2, "ForexLive": 2,
    # Tier 1 — aggregators + social
    "Finviz": 1, "Twitter": 1, "X": 1, "Reddit": 1,
}


# ─── Result type ─────────────────────────────────────────────────────────────
@dataclass
class EventClassification:
    """Output of :func:`classify`. JSON-serialisable via ``asdict``."""
    category:        str                = CAT_UNKNOWN
    subcategory:     Optional[str]      = None
    severity:        int                = 1
    direction:       str                = "NEUTRAL"      # BULL_*, BEAR_*, TWO_WAY, NEUTRAL
    affected_assets: list[str]          = field(default_factory=list)
    source_tier:     int                = 0
    matched_rules:   list[str]          = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "category":        self.category,
            "subcategory":     self.subcategory,
            "severity":        self.severity,
            "direction":       self.direction,
            "affected_assets": self.affected_assets,
            "source_tier":     self.source_tier,
            "matched_rules":   self.matched_rules,
        }


def classify(text: str, *, source: Optional[str] = None) -> EventClassification:
    """Classify a single headline / event.

    The first matching rule sets category + base severity. Subsequent matches
    only boost severity and accumulate affected_assets. This avoids one
    headline being "MONETARY+INFLATION+GROWTH" simultaneously — we pick the
    most-specific match and add context.
    """
    if not text:
        return EventClassification()

    result = EventClassification()
    result.source_tier = SOURCE_CREDIBILITY.get(source or "", 0)
    matched_assets: set[str] = set()

    for idx, (pat, cat, sev, direction, assets) in enumerate(_RULES):
        if pat.search(text):
            result.matched_rules.append(pat.pattern[:60])
            if result.category == CAT_UNKNOWN:
                # First match — set primary category
                result.category  = cat
                result.severity  = sev
                result.direction = direction
            else:
                # Subsequent matches — boost severity by 1 per extra rule,
                # cap at 10. Direction stays from primary.
                result.severity = min(10, result.severity + 1)
            for a in assets:
                matched_assets.add(a)

    # Add asset tickers visible in the text itself
    for tk in _extract_simple_tickers(text):
        matched_assets.add(tk)

    result.affected_assets = sorted(matched_assets)
    return result


_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")
_ASSET_ALIASES = {
    "FED": "DXY", "FOMC": "DXY", "POWELL": "DXY",
    "ECB": "EUR", "BOJ": "JPY", "RBI": "NIFTY",
    "NIFTY50": "NIFTY", "NIFTY 50": "NIFTY",
    "SPX": "SPX", "S&P": "SPX", "SP500": "SPX",
    "BITCOIN": "BTC", "ETHEREUM": "ETH",
}


def _extract_simple_tickers(text: str) -> list[str]:
    """Pull bare uppercase tokens 2-5 chars that look like tickers.
    Drops obvious noise tokens (common ALL-CAPS words like THE, AND, FOR)."""
    NOISE = {"THE", "AND", "FOR", "WITH", "FROM", "BY", "AT", "ON", "IN",
             "AS", "TO", "OF", "OR", "BUT", "NOT", "NEW", "OLD", "TOP", "MORE",
             "NEXT", "WHAT", "WHY", "HOW", "WHO", "ALL", "ANY", "MAY", "WAS",
             "BE", "BEEN", "AM", "IS", "ARE", "WERE", "DO", "DID", "HAS"}
    out: list[str] = []
    for tok in _TICKER_RE.findall(text):
        if tok in NOISE:
            continue
        out.append(_ASSET_ALIASES.get(tok, tok))
    return out


# ─── Batch helper ───────────────────────────────────────────────────────────
def classify_batch(items: list[dict], *, text_key: str = "text",
                   source_key: str = "source") -> list[dict]:
    """Classify a list of news dicts. Each gets a new ``event`` field with
    the classification. Returns the same list (mutated in place + returned)."""
    for it in items:
        if not isinstance(it, dict):
            continue
        c = classify(it.get(text_key, ""), source=it.get(source_key))
        it["event"] = c.to_dict()
    return items


def summarize_distribution(classified: list[dict]) -> dict:
    """Aggregate counts + avg severity by category — useful for the intel
    snapshot's events_classified summary."""
    by_cat: dict[str, dict] = {}
    bull_sum = bear_sum = 0
    bull_w = bear_w = 0.0
    for it in classified:
        ev = it.get("event") or {}
        cat = ev.get("category", CAT_UNKNOWN)
        sev = ev.get("severity", 0)
        d   = ev.get("direction", "NEUTRAL")
        slot = by_cat.setdefault(cat, {"count": 0, "total_sev": 0, "max_sev": 0})
        slot["count"]     += 1
        slot["total_sev"] += sev
        slot["max_sev"]    = max(slot["max_sev"], sev)
        if d.startswith("BULL"):
            bull_sum += 1
            bull_w   += sev
        elif d.startswith("BEAR"):
            bear_sum += 1
            bear_w   += sev
    for cat, s in by_cat.items():
        s["avg_sev"] = round(s["total_sev"] / s["count"], 1) if s["count"] else 0
        del s["total_sev"]
    return {
        "by_category":     by_cat,
        "directional":     {"bull_count": bull_sum, "bear_count": bear_sum,
                            "bull_weighted": bull_w, "bear_weighted": bear_w,
                            "tilt": round((bull_w - bear_w) / max(bull_w + bear_w, 1), 3)},
        "total_classified": len(classified),
    }
