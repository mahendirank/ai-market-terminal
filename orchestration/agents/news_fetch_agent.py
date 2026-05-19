"""Sprint 4 Stage 4.3 — NewsFetchAgent (shadow / dual-run mode).

Wraps the legacy `news.get_all_news()` function as a `TickAgent`. Runs
periodically in parallel with the existing news pipeline:

  - Existing pipeline KEEPS being authoritative (this agent doesn't
    route news anywhere production cares about).
  - Agent emits `news.raw` events to the bus for plumbing verification.
  - Per-tick structured log captures count, latency, sources, drift.
  - Legacy `news.py` is NOT modified.

Cache cooperation: `news.get_all_news()` has a 30s in-process cache.
Agent + legacy callers share that cache. Agent tick interval of 120s
means the agent's fetch is usually a cache MISS, contributing a real
fetch every 2 minutes (≈ +50% over the legacy 5-min digest rate).

Safety properties:
  - timeout=30s — hard ceiling per tick
  - retry_policy bounded to 3 attempts on EXTERNAL_API / TIMEOUT
  - Failures recorded but NEVER alter legacy outputs
  - DISABLED state after 5 consecutive failures (orchestrator policy)
  - No critic gating, no DLQ routing — Stage 4.4+ work

Forbidden in Stage 4.3 (by user spec):
  - replace legacy fetch path
  - signal generation
  - trade execution
  - critic enforcement
  - LangGraph reasoning
  - self-healing loops
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from logging_config import ErrorCategory
from orchestration import RetryPolicy, TickAgent


class NewsFetchAgent(TickAgent):
    """Shadow-mode wrapper for the legacy news.get_all_news() function.

    Subclass invariants (per AGENT_CONTRACT.md):
      - tick_interval default 120s — slower than the legacy 5-min digest so
        the dual-run window captures multiple legacy cycles per agent cycle
        without overloading external APIs (30s cache absorbs most overlap).
      - timeout 30s — hard cap; news fetch should never take longer.
      - retry_policy retries 3× with exponential backoff on transient errors.
        Failures classified as VALIDATION (e.g. parse errors) are NOT retried.
      - emit_event with metadata only (count, latency, source sample) — the
        full news list (~50KB) isn't shipped through the bus until a real
        consumer needs it (Sprint 5+).
    """

    name = "news.fetch"
    family = "news"
    version = "v1"

    # Default tick interval picked to be the LEAST disruptive value during
    # the dual-run observation window. Configurable via env so operator
    # can tune without redeploying code.
    tick_interval = float(os.environ.get("NEWS_FETCH_TICK_INTERVAL", "120"))
    timeout = float(os.environ.get("NEWS_FETCH_TIMEOUT", "30"))

    retry_policy = RetryPolicy(
        max_attempts=3,
        base_delay=2.0,
        max_delay=10.0,
        backoff_multiplier=2.0,
        jitter=0.2,
        retryable_categories=frozenset({
            ErrorCategory.EXTERNAL_API,
            ErrorCategory.TIMEOUT,
        }),
    )

    # Per-instance state for tick-to-tick drift detection.
    # Read-only after construction except by tick() itself (single-tick-at-a-time).
    _prev_tick_count: int | None = None
    _prev_tick_sources: frozenset[str] = frozenset()
    _prev_tick_latency_ms: float | None = None

    async def run_once(self) -> None:
        """Fetch news via the legacy function in a thread, then emit + log.

        This is intentionally a thin wrapper. The agent's value-add is:
          1. Async-safe invocation (asyncio.to_thread)
          2. Structured per-tick logging
          3. Drift detection vs previous tick
          4. Bus event emission (plumbing verification)
        """
        # Lazy import — keeps the orchestration package independently testable.
        from news import get_all_news

        t_start = time.perf_counter()
        try:
            news_list = await asyncio.to_thread(get_all_news)
        except Exception as e:
            # Re-raise with classification so the retry policy can decide.
            # The agent's tick() wrapper catches and records this as a failure.
            self.log.exception(
                "news_fetch_failed",
                extra={
                    "error_category": ErrorCategory.EXTERNAL_API,
                    "exc_type": type(e).__name__,
                },
            )
            raise

        latency_ms = (time.perf_counter() - t_start) * 1000.0

        # Defensive: get_all_news returns list[dict]. Normalize.
        if not isinstance(news_list, list):
            self.log.warning(
                "news_fetch_returned_non_list",
                extra={"got_type": type(news_list).__name__},
            )
            news_list = []

        count = len(news_list)
        sources = _extract_sources(news_list)
        sample_size = min(10, count)

        # ── Tick-to-tick drift detection ──
        drift_metrics = self._compute_drift(count, sources, latency_ms)

        # ── Structured per-tick log ──
        self.log.info(
            "news_agent_tick_complete",
            extra={
                "count": count,
                "latency_ms": round(latency_ms, 2),
                "source_count": len(sources),
                "sample_sources": sorted(sources)[:10],
                **drift_metrics,
            },
        )

        # ── Bus emission (metadata only — payload is bounded) ──
        await self.emit_event(
            event_type="news.raw",
            payload={
                "count": count,
                "latency_ms": round(latency_ms, 2),
                "source_count": len(sources),
                # Bound to top 20 source names to keep envelopes small.
                # Sprint 5+ may include full news data once a consumer needs it.
                "sources": sorted(sources)[:20],
                # No actual news content — Stage 4.3 plumbing only.
                "shadow_mode": True,
            },
        )

        # ── Update drift baseline for next tick ──
        self._prev_tick_count = count
        self._prev_tick_sources = sources
        self._prev_tick_latency_ms = latency_ms

    def _compute_drift(
        self,
        count: int,
        sources: frozenset[str],
        latency_ms: float,
    ) -> dict[str, Any]:
        """Tick-to-tick drift indicators. First tick returns empty dict."""
        if self._prev_tick_count is None:
            return {"first_tick": True}

        prev_count = self._prev_tick_count
        count_delta = count - prev_count
        count_drift_pct = (abs(count_delta) / max(prev_count, 1)) * 100.0

        sources_added = sources - self._prev_tick_sources
        sources_removed = self._prev_tick_sources - sources

        latency_delta_ms = (
            (latency_ms - self._prev_tick_latency_ms)
            if self._prev_tick_latency_ms is not None
            else 0.0
        )

        return {
            "first_tick": False,
            "count_delta": count_delta,
            "count_drift_pct": round(count_drift_pct, 2),
            "sources_added": sorted(sources_added)[:5],
            "sources_removed": sorted(sources_removed)[:5],
            "latency_delta_ms": round(latency_delta_ms, 2),
        }


def _extract_sources(news_list: list) -> frozenset[str]:
    """Collect unique source names from a news list. Defensive on shape."""
    out: set[str] = set()
    for item in news_list:
        if not isinstance(item, dict):
            continue
        s = item.get("source")
        if isinstance(s, str) and s:
            out.add(s)
    return frozenset(out)
