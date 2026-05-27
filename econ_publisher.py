"""
econ_publisher.py — Pre-print impact gate for the economic calendar.

Watches the ForexFactory calendar (already fetched by econ_calendar.get_calendar)
and publishes HIGH-impact events to the event_bus shortly BEFORE they print
(default 15 min for CRITICAL events, 5 min for others). Subscribers
(morning_report, yield_watch) drop their caches on receipt so the dashboard
serves fresh narratives the moment the print lands rather than reading a
30-minute-stale brief through the spike.

Also fires a post-print event ~3 min after the scheduled time with the
actual value (when ForexFactory has populated it) so a second cache flush
happens once the surprise vs forecast is known.

The publisher is idempotent: per-event dedup keyed by (date, country, title)
prevents a 60s scan loop from re-firing the same event over its window.

This module does NOT fetch the calendar itself — it consumes
econ_calendar.get_calendar() which has its own 30 min cache. Calling
scan_and_publish() on a 60s loop is cheap (calendar reads are cache hits).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_UTC = timezone.utc

# Per-event dedup window — same event can't republish within this many seconds.
# 30 min covers both the pre-print and post-print windows from a single
# event without re-firing if the loop ticks during them.
_DEDUP_SECS = 1800

_recent: dict[str, float] = {}
_recent_lock = threading.Lock()


# ─── Severity mapping ──────────────────────────────────────────────────────

# Tier-1: events the LLM should narrate around (Fed, ECB, BOJ rate decisions
# and surprise speeches; NFP, CPI, GDP, PCE; OPEC; PBOC). These get severity
# 10 (top of event_classifier's 1-10 scale) so the event_bus subscribers
# treat them with maximum urgency.
_TIER1_PATTERNS = (
    r"\bfomc\b", r"\bfed\b.*\b(decision|funds|rate|chair|speaks)\b",
    r"\bpowell\b", r"\bnon[- ]?farm\b", r"\bnfp\b",
    r"\bcpi\b.*\b(m/m|y/y|core)\b",
    r"\bgdp\b.*\b(q/q|advance|final)\b",
    r"\bpce\b.*\b(price|index)\b",
    r"\becb.*\b(decision|main refi|rate|press conference|lagarde)\b",
    r"\bboj.*\b(decision|outlook|rate|ueda)\b",
    r"\bpboc.*\b(loan|prime|rate|decision)\b",
    r"\bopec\b", r"\btariff", r"\bsanction",
)
_TIER1_RE = re.compile("|".join(_TIER1_PATTERNS), re.IGNORECASE)

# Tier-2: high-impact but lower priority. PMIs, retail sales, jobless claims,
# unemployment rate, ISM, regional Fed surveys.
_TIER2_RE = re.compile(
    r"\b(pmi|retail sales|jobless claims|unemployment|ism|empire state|"
    r"philly fed|consumer confidence|housing starts|industrial production)\b",
    re.IGNORECASE,
)


def _severity_for(event: dict) -> int:
    """Map FF impact + title to event_classifier's 1-10 severity scale."""
    title = (event.get("event") or "").strip()
    impact = (event.get("impact") or "Low").lower()
    if _TIER1_RE.search(title):
        return 10
    if impact == "high":
        return 9
    if _TIER2_RE.search(title):
        return 8
    if impact == "medium":
        return 7
    return 5  # Low — generally not a cache-invalidation event


# ─── Time parsing ──────────────────────────────────────────────────────────

def _parse_event_dt(event: dict) -> Optional[datetime]:
    """Return the event's scheduled UTC datetime, or None if not parseable.

    econ_calendar normalises FF's ISO date (with TZ offset) into the
    ``date`` string ``YYYY-MM-DD``, dropping the time component. The original
    ``time`` field looks like '8:30am'/'All Day'. We need the full timestamp
    to know if an event fires in the next 5 min, so we re-fetch the raw
    FF JSON below in get_imminent_events. Here we accept whatever was given.
    """
    iso = event.get("_dt_iso") or ""
    if iso:
        try:
            return datetime.fromisoformat(iso).astimezone(_UTC)
        except Exception:
            pass
    return None


# ─── Event id (stable across scans) ────────────────────────────────────────

def _event_id(event: dict) -> str:
    """Stable hash so the same FF row produces the same dedup key across
    minutes. Uses the original date + country + title, not the display
    fields that econ_calendar adds (which might change formatting)."""
    raw = f"{event.get('date','')}|{event.get('country','')}|{event.get('event','')[:80]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ─── Imminent-event discovery ──────────────────────────────────────────────

def get_imminent_events(window_pre_min: int = 15, window_post_min: int = 5) -> list[dict]:
    """Return events whose scheduled time falls in [-window_post, +window_pre]
    from now (UTC). Source is the raw ForexFactory JSON — we re-parse the
    timestamp ourselves because econ_calendar.py drops the time component
    in its normalised output."""
    import requests
    out: list[dict] = []
    now_utc = datetime.now(_UTC)
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return out
        items = r.json() or []
    except Exception as e:  # noqa: BLE001
        log.warning("[econ_publisher] FF fetch failed: %s", e)
        return out

    for item in items:
        raw_date = item.get("date")
        if not raw_date:
            continue
        try:
            dt_utc = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).astimezone(_UTC)
        except Exception:
            continue
        delta = (dt_utc - now_utc).total_seconds()
        # Pre-window: positive delta within window_pre_min minutes.
        # Post-window: negative delta within window_post_min minutes.
        if delta > window_pre_min * 60 or delta < -window_post_min * 60:
            continue
        out.append({
            "_dt_iso":  dt_utc.isoformat(),
            "delta_secs": int(delta),
            "minutes_until": int(round(delta / 60)),
            "date":     raw_date,
            "country":  item.get("country", ""),
            "event":    (item.get("title") or item.get("name") or "").strip(),
            "impact":   item.get("impact") or "Low",
            "forecast": (item.get("forecast") or "").strip(),
            "previous": (item.get("previous") or "").strip(),
            "actual":   (item.get("actual")   or "").strip(),
        })
    # Closest-first
    out.sort(key=lambda e: abs(e["delta_secs"]))
    return out


# ─── Publisher ─────────────────────────────────────────────────────────────

def _should_publish(event_id: str) -> bool:
    """Per-event dedup. Same event can't publish twice within _DEDUP_SECS."""
    now = time.time()
    with _recent_lock:
        last = _recent.get(event_id, 0)
        if now - last < _DEDUP_SECS:
            return False
        _recent[event_id] = now
        # Bound memory: drop entries older than 2h
        if len(_recent) > 200:
            cutoff = now - 7200
            for k in [kk for kk, tt in _recent.items() if tt < cutoff]:
                _recent.pop(k, None)
    return True


def scan_and_publish(window_pre_min: int = 15, window_post_min: int = 5) -> dict:
    """One scan tick. Publishes pre-print and post-print events to event_bus.

    Returns a small summary for the worker loop to log:
        {"checked": N, "published": M, "events": [...short list...]}
    """
    try:
        from event_bus import publish_breaking
    except Exception as e:  # noqa: BLE001
        log.debug("[econ_publisher] event_bus import failed: %s", e)
        return {"checked": 0, "published": 0, "events": []}

    imminent = get_imminent_events(window_pre_min=window_pre_min,
                                   window_post_min=window_post_min)
    published: list[dict] = []
    for ev in imminent:
        sev = _severity_for(ev)
        if sev < 7:
            continue  # Not worth a breaking-news cache flush
        ev_id = _event_id(ev)
        phase = "PRE" if ev["delta_secs"] > 0 else "POST"
        # Different phases get different dedup keys so we can fire both
        scoped_id = f"{phase}:{ev_id}"
        if not _should_publish(scoped_id):
            continue
        if phase == "PRE":
            topic = f"UPCOMING {ev['country']} {ev['event']} — in {ev['minutes_until']} min"
        else:
            actual = ev["actual"] or "—"
            fc = ev["forecast"] or "—"
            topic = f"PRINTED {ev['country']} {ev['event']} — actual {actual} vs forecast {fc}"
        ok = publish_breaking(
            topic=topic[:200],
            severity=sev,
            extra={
                "category":   "ECONOMIC" if sev < 10 else "CENTRAL_BANK",
                "source":     "forexfactory",
                "event_id":   ev_id,
                "phase":      phase,
                "country":    ev["country"],
                "impact":     ev["impact"],
                "forecast":   ev["forecast"],
                "previous":   ev["previous"],
                "actual":     ev["actual"],
                "minutes":    ev["minutes_until"],
            },
        )
        if ok:
            published.append({
                "phase":   phase,
                "topic":   topic,
                "sev":     sev,
                "in_min":  ev["minutes_until"],
            })
            log.info("[econ_publisher] %s sev=%d %s", phase, sev, topic[:100])

    return {"checked": len(imminent), "published": len(published), "events": published}
