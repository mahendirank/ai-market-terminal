"""
sentiment_provider.py — Pluggable sentiment hook for the Indicators panel.

Today this returns a neutral stub; tomorrow a real provider (Groq + RSS,
your existing ``explainer.py``, or a third-party news API) drops in by
implementing :class:`SentimentProvider` and registering with
:func:`set_provider`.

Composite score in ``indicators.py`` already reads ``get_sentiment(ticker)`` —
swapping providers does NOT require touching the indicators code.
"""
from __future__ import annotations

from typing import Protocol, Optional
import time


class SentimentResult(dict):
    """Typed-ish dict so callers can rely on shape.

    Keys:
      - score:       float in [-1.0, 1.0]   (−1 very bearish, +1 very bullish)
      - confidence:  float in [0.0, 1.0]    (provider's self-rated certainty)
      - label:       "BULLISH" | "BEARISH" | "NEUTRAL"
      - rationale:   short human-readable string
      - sources:     list of {url, title} (may be empty)
      - ts:          unix timestamp
    """
    pass


class SentimentProvider(Protocol):
    name: str
    def get(self, ticker: str, asset_class: str) -> SentimentResult: ...


# ─── Default neutral provider (stub) ─────────────────────────────────────────
class _NeutralStub:
    name = "neutral_stub"

    def get(self, ticker: str, asset_class: str) -> SentimentResult:
        return SentimentResult(
            score=0.0,
            confidence=0.0,
            label="NEUTRAL",
            rationale="Sentiment provider not yet wired — neutral placeholder.",
            sources=[],
            ts=int(time.time()),
            provider=self.name,
        )


_provider: SentimentProvider = _NeutralStub()


def set_provider(p: SentimentProvider) -> None:
    """Install a real provider at runtime (e.g. from dashboard_api startup)."""
    global _provider
    _provider = p


def get_provider() -> SentimentProvider:
    return _provider


def get_sentiment(ticker: str, asset_class: str = "us_stock") -> SentimentResult:
    """Front door for the indicators module.

    Wraps in try/except so a misbehaving provider can never crash the
    indicators pipeline — composite signal stays computable from price alone.
    """
    try:
        return _provider.get(ticker, asset_class)
    except Exception as e:  # noqa: BLE001 — defensive boundary
        return SentimentResult(
            score=0.0, confidence=0.0, label="NEUTRAL",
            rationale=f"Sentiment unavailable ({e.__class__.__name__})",
            sources=[], ts=int(time.time()), provider="error",
        )
