"""
hni_sentiment_provider.py — Real per-ticker news sentiment from hni_news.db.

Implements the SentimentProvider protocol declared in sentiment_provider.py.
Drop-in: at terminal startup do
    from hni_sentiment_provider import HNINewsSentimentProvider
    from sentiment_provider import set_provider
    set_provider(HNINewsSentimentProvider())
…and get_sentiment(ticker) — and therefore ripster_fusion.fuse() — instantly
return real, recency-weighted news sentiment instead of the neutral stub.

DESIGN
------
hni_news.db has no stored score, so we score the headline TEXT with a compact
finance lexicon (zero deps, deterministic, VPS-cheap) and aggregate per ticker
with exponential recency decay. Swap in FinBERT later by replacing
``_score_text`` — nothing else changes.

Output shape matches sentiment_provider.SentimentResult:
  score [-1,1], confidence [0,1], label, rationale, sources[], ts, provider
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hni_news.db")

# ─── Finance sentiment lexicon (headline-tuned). weight 2 = strong, 1 = mild ──
_POS = {
    # strong
    "surge": 2, "surges": 2, "soar": 2, "soars": 2, "skyrocket": 2, "rally": 2, "rallies": 2,
    "explosive": 2, "record": 2, "all-time high": 2, "breakout": 2, "blowout": 2, "smashes": 2,
    # mild
    "beat": 1, "beats": 1, "tops": 1, "exceeds": 1, "jumps": 1, "gains": 1, "rises": 1, "climbs": 1,
    "upgrade": 1, "upgrades": 1, "raises": 1, "raised": 1, "strong": 1, "outperform": 1, "bullish": 1,
    "buy": 1, "wins": 1, "growth": 1, "profit": 1, "expansion": 1, "boost": 1, "rebound": 1,
    "recovery": 1, "accelerate": 1, "milestone": 1, "partnership": 1, "approval": 1, "demand": 1,
    "optimistic": 1, "beats estimates": 2, "tops estimates": 2, "increases position": 1, "increases stake": 1,
}
_NEG = {
    # strong
    "plunge": 2, "plunges": 2, "crash": 2, "crashes": 2, "collapse": 2, "tanks": 2, "slump": 2,
    "bankruptcy": 2, "fraud": 2, "probe": 2, "investigation": 2, "lawsuit": 2, "recall": 2, "halt": 2,
    "selloff": 2, "guidance cut": 2, "slashes": 2, "plummets": 2,
    # mild
    "miss": 1, "misses": 1, "falls": 1, "drop": 1, "drops": 1, "declines": 1, "cut": 1, "cuts": 1,
    "downgrade": 1, "downgrades": 1, "lowers": 1, "weak": 1, "warns": 1, "warning": 1, "bearish": 1,
    "sell": 1, "layoffs": 1, "quits": 1, "resign": 1, "resigns": 1, "risk": 1, "concern": 1, "concerns": 1,
    "loss": 1, "losses": 1, "default": 1, "slowdown": 1, "disappoints": 1, "shortfall": 1, "pressure": 1,
}
_NEGATORS = {"not", "no", "without", "fails", "fail", "never", "isn't", "aren't", "won't"}

_RECENCY_HALFLIFE_H = 6.0     # a headline's weight halves every 6 hours
_LOOKBACK_H = 36.0           # ignore news older than this


def _score_text(text: str) -> float:
    """Return a per-headline sentiment in [-1, 1] from the finance lexicon.

    Negation flips the polarity of a hit within a small left-window. Replace
    this function with a FinBERT call to upgrade — the interface is just
    str -> float in [-1,1].
    """
    if not text:
        return 0.0
    lo = " " + text.lower() + " "
    net = 0.0
    # multi-word phrases first
    for phrase, w in list(_POS.items()) + [(p, -w) for p, w in _NEG.items()]:
        if " " in phrase and phrase in lo:
            net += w if phrase in _POS else w  # w already signed for NEG via comprehension
    # single tokens with negation window
    toks = re.findall(r"[a-z'\-]+", lo)
    for i, t in enumerate(toks):
        w = _POS.get(t, 0) or (-_NEG.get(t, 0))
        if w == 0:
            continue
        window = toks[max(0, i - 3):i]
        if any(n in window for n in _NEGATORS):
            w = -w
        net += w
    return max(-1.0, min(1.0, net / 3.0))


def _ticker_in_row(ticker: str, tickers_field: Optional[str], text: str) -> bool:
    t = ticker.upper()
    if tickers_field:
        if t in [x.strip().upper() for x in tickers_field.split(",") if x.strip()]:
            return True
    # also catch $CASHTAG in the headline body
    return re.search(rf"\${t}\b", text or "", re.IGNORECASE) is not None


class HNINewsSentimentProvider:
    name = "hni_news_lexicon"

    def __init__(self, db_path: str = _DB_PATH, lookback_h: float = _LOOKBACK_H):
        self.db_path = db_path
        self.lookback_h = lookback_h

    def _rows(self) -> list[sqlite3.Row]:
        if not os.path.exists(self.db_path):
            return []
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            return list(con.execute(
                "SELECT text, tickers, url, pub_utc FROM hni_news "
                "WHERE text IS NOT NULL AND text != '' ORDER BY pub_utc DESC LIMIT 1000"))
        finally:
            con.close()

    def get(self, ticker: str, asset_class: str = "us_stock") -> dict:
        now = datetime.now(timezone.utc)
        num = den = 0.0
        n = 0
        signs: list[float] = []
        sources: list[dict] = []
        for r in self._rows():
            if not _ticker_in_row(ticker, r["tickers"], r["text"]):
                continue
            # recency weight
            try:
                pub = datetime.fromisoformat(r["pub_utc"])
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            age_h = (now - pub).total_seconds() / 3600.0
            if age_h < 0 or age_h > self.lookback_h:
                continue
            rw = 0.5 ** (age_h / _RECENCY_HALFLIFE_H)
            s = _score_text(r["text"])
            num += s * rw
            den += rw
            n += 1
            signs.append(1.0 if s > 0.05 else -1.0 if s < -0.05 else 0.0)
            if len(sources) < 5 and abs(s) > 0.05:
                sources.append({"url": r["url"], "title": (r["text"] or "")[:90], "score": round(s, 2)})

        if n == 0 or den == 0:
            return dict(score=0.0, confidence=0.0, label="NEUTRAL",
                        rationale=f"No recent news for {ticker} in last {int(self.lookback_h)}h.",
                        sources=[], ts=int(time.time()), provider=self.name)

        score = max(-1.0, min(1.0, num / den))
        # confidence: more articles + more agreement = higher
        nonzero = [x for x in signs if x != 0]
        agreement = abs(sum(nonzero) / len(nonzero)) if nonzero else 0.0
        confidence = round(min(1.0, n / 4.0) * (0.5 + 0.5 * agreement), 3)
        label = "BULLISH" if score >= 0.15 else "BEARISH" if score <= -0.15 else "NEUTRAL"
        rationale = (f"{n} headline(s) /{self.lookback_h:.0f}h, recency-weighted "
                     f"score {score:+.2f}, agreement {agreement:.0%}.")
        return dict(score=round(score, 3), confidence=confidence, label=label,
                    rationale=rationale, sources=sources, ts=int(time.time()), provider=self.name)


def register() -> HNINewsSentimentProvider:
    """Install this provider as the live one (call at terminal startup)."""
    from sentiment_provider import set_provider
    p = HNINewsSentimentProvider()
    set_provider(p)
    return p


if __name__ == "__main__":
    import sys, json
    prov = HNINewsSentimentProvider()
    tickers = sys.argv[1:] or ["NVDA", "AAPL", "MU", "GOLD", "NDX"]
    for tk in tickers:
        res = prov.get(tk)
        print(f"\n=== {tk} ===")
        print(json.dumps({k: res[k] for k in ("score", "confidence", "label", "rationale")}, indent=2))
        for s in res["sources"]:
            print(f"   [{s['score']:+.2f}] {s['title']}")
