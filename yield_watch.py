"""
yield_watch.py — Sovereign 10Y yield monitor with AI narrative.

Reads US10Y, JGB10Y, Bund10Y, Gilt10Y, India 10Y from live_prices, computes
1-day deltas in basis points, and (when any |Δ| ≥ 5bp) generates a one-paragraph
LLM narrative explaining what the moves mean for cross-asset positioning.

This is the layer that turns "US10Y +8bp" from a number on the Bonds tab into
"AI-led risk-on fighting geopolitical inflation shock — sell rallies on Nasdaq,
gold longs highest conviction" — the kind of analyst paragraph that competing
products (ChatGPT, Bloomberg analysts) write for free but the terminal
previously did not produce despite having the data.

Public API:
    get_yield_watch(force=False) -> dict
        {
          "yields": {
              "US_10Y": {"value": 4.55, "delta_bp": -3, "arrow": "▼", ...},
              ...
          },
          "narrative": "...",   # may be None if LLM disabled or all moves tiny
          "any_breaking": True,  # any yield moved >= 10bp
          "generated_at": int(time.time()),
        }
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

# Bps threshold for "this move matters" — 5bp on a 10Y is roughly a 1% price
# move on a 10-year bond, enough to ripple into equity/FX positioning.
_DELTA_THRESHOLD_BP   = 5
_BREAKING_BP          = 10   # any yield moving 10bp+ is "breaking"
_CACHE_TTL            = 300  # 5 min
_CACHE_TTL_BREAKING   = 60   # 1 min when any |Δ| >= breaking threshold

_YIELDS_WATCHED = ["US_10Y", "JP_10Y", "DE_10Y", "UK_10Y", "IN_10Y"]

# Display labels for the narrative prompt (LLM sees these, not the raw keys).
_LABEL = {
    "US_10Y": "US 10Y",
    "JP_10Y": "Japan 10Y (JGB)",
    "DE_10Y": "Germany 10Y (Bund)",
    "UK_10Y": "UK 10Y (Gilt)",
    "IN_10Y": "India 10Y",
}


# ─── Redis cache (in-process fallback) ──────────────────────────────────────
_redis_client = None
_redis_ok = False


def _init_redis() -> None:
    global _redis_client, _redis_ok
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return
    try:
        import redis
        c = redis.from_url(url, socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
        c.ping()
        _redis_client, _redis_ok = c, True
    except Exception:  # noqa: BLE001
        _redis_ok = False


_init_redis()

_INPROC: dict[str, tuple[float, dict]] = {}


# ─── Event-bus subscriber: drop yield_watch cache on breaking news ──────────
def _on_breaking_event(ev: dict) -> None:
    """Invalidate yield_watch cache so the next read recomputes (and the LLM
    narration sees the latest bond moves alongside the new catalyst)."""
    try:
        if _redis_ok and _redis_client:
            try:
                _redis_client.delete("yield_watch:v1")
            except Exception:  # noqa: BLE001
                pass
        _INPROC.pop("yield_watch:v1", None)
        log.info("[yield_watch] dropped cache on breaking event: sev=%s",
                 ev.get("severity"))
    except Exception as e:  # noqa: BLE001
        log.warning("[yield_watch] _on_breaking_event failed: %s", e)


try:
    from event_bus import subscribe as _bus_subscribe, start_listener as _bus_start
    _bus_subscribe(_on_breaking_event)
    _bus_start()
except Exception as _e:  # noqa: BLE001
    log.debug("[yield_watch] event_bus wiring skipped: %s", _e)


def _cache_get(key: str) -> Optional[dict]:
    if _redis_ok and _redis_client:
        try:
            raw = _redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
    entry = _INPROC.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    return None


def _cache_put(key: str, value: dict, ttl: int) -> None:
    if _redis_ok and _redis_client:
        try:
            _redis_client.setex(key, ttl, json.dumps(value, default=str))
        except Exception:  # noqa: BLE001
            pass
    _INPROC[key] = (time.time() + ttl, value)


# ─── Core: read yields + compute deltas ─────────────────────────────────────
def _read_yields() -> dict:
    """Pull current yields from live_prices and compute bp deltas.

    Returns ``{key: {value, prev, delta_bp, delta_pct, arrow, source}}``.
    Missing keys (e.g. tvdata cold-start failure) are skipped silently rather
    than emitting zero-rows that would confuse the narrative.
    """
    try:
        from live_prices import get_live_prices
        snap = get_live_prices()
    except Exception as e:  # noqa: BLE001
        log.warning("[yield_watch] live_prices read failed: %s", e)
        return {}

    bonds = snap.get("bonds") or {}
    out: dict = {}
    for k in _YIELDS_WATCHED:
        row = bonds.get(k)
        if not row:
            continue
        try:
            value = float(row.get("price", 0))
            prev  = float(row.get("prev", value))
            delta = round((value - prev) * 100, 1)  # 4.55 - 4.58 = -0.03 → -3bp
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        out[k] = {
            "label":     _LABEL.get(k, k),
            "value":     round(value, 3),
            "prev":      round(prev, 3),
            "delta_bp":  delta,
            "delta_pct": row.get("change", 0),
            "arrow":     row.get("arrow", "─"),
            "source":    row.get("source", ""),
        }
    return out


# ─── LLM narrative ──────────────────────────────────────────────────────────
def _generate_narrative(yields: dict, big_movers: list[str]) -> Optional[str]:
    """One-paragraph cross-asset read of today's yield moves. Gated by an env
    flag so the module can ship with zero LLM cost if desired."""
    if os.environ.get("ENABLE_YIELD_NARRATION", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    if not big_movers:
        return None

    try:
        from ai_router import chat
    except Exception:
        return None

    lines = []
    for k, row in yields.items():
        marker = " ← MOVER" if k in big_movers else ""
        lines.append(f"  {row['label']:<22} {row['value']:>6.3f}%   Δ {row['delta_bp']:+.1f}bp{marker}")
    table = "\n".join(lines)

    prompt = (
        "Today's sovereign 10-year yields:\n\n"
        f"{table}\n\n"
        "Write ONE concise paragraph (3-4 sentences) explaining what these moves "
        "mean for cross-asset positioning today. Cover:\n"
        "  1. Which yields are doing the heavy lifting and why (oil, AI, "
        "geopolitics, central-bank divergence — pick the most likely driver).\n"
        "  2. Implication for equities (which indices are pressured, which are not).\n"
        "  3. Implication for gold/USD/yen carry trade if relevant.\n\n"
        "No bullet points. No hedge words like 'might' or 'could'. State the read."
    )
    messages = [
        {"role": "system", "content": (
            "You are a fixed-income desk analyst writing a one-paragraph note "
            "for traders. Be specific and concrete. Cross-asset linkages only.")},
        {"role": "user", "content": prompt},
    ]
    try:
        result = chat(task="fast_summary", messages=messages,
                      temperature=0.25, max_tokens=220, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.warning("[yield_watch] LLM call failed: %s", e)
        return None
    if not result.ok or not result.content:
        return None
    return result.content.strip()


# ─── Public API ─────────────────────────────────────────────────────────────
def get_yield_watch(force: bool = False) -> dict:
    """Main entry. Cached. Set ``force=True`` to bypass cache."""
    cache_key = "yield_watch:v1"
    if not force:
        cached = _cache_get(cache_key)
        if cached:
            cached["_cache_hit"] = True
            return cached

    yields = _read_yields()
    big_movers = [k for k, v in yields.items() if abs(v.get("delta_bp", 0)) >= _DELTA_THRESHOLD_BP]
    any_breaking = any(abs(v.get("delta_bp", 0)) >= _BREAKING_BP for v in yields.values())
    narrative = _generate_narrative(yields, big_movers) if big_movers else None

    out = {
        "yields":      yields,
        "narrative":   narrative,
        "big_movers":  big_movers,
        "any_breaking": any_breaking,
        "generated_at": int(time.time()),
        "_cache_hit":   False,
    }

    ttl = _CACHE_TTL_BREAKING if any_breaking else _CACHE_TTL
    _cache_put(cache_key, out, ttl)
    return out
