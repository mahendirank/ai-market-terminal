"""
streaming.py — WebSocket streaming hub for live updates.

Architecture:
  - Single FastAPI WebSocket endpoint at /ws
  - Client subscribes to one or more channels: prices, alerts, macro, explainers
  - Background tasks publish events into the hub via hub.publish(channel, payload)
  - Hub fans out to all WebSockets subscribed to that channel
  - Server timestamps every message so the client can compute latency

Channels:
  - prices      → live price tile updates (debounced 1s, dedup'd)
  - alerts      → new alert just sent
  - macro       → macro regime commentary updated
  - explainers  → new "Why Did It Move?" explanation generated

Optimisations:
  - PriceDebouncer batches multiple asset updates into one message per second
  - Dedup: skip publish if value identical to last-sent
  - Heartbeat ping/pong every 15s keeps connection alive + measures latency
  - Dead connections auto-evicted on send failure

In-process today. Path to Redis pub/sub when worker container is split out
(see PRODUCTION.md "deferred"): replace publish() body with Redis PUBLISH.
"""
import os
import json
import time
import asyncio
import threading
from typing import Set, Dict
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

VALID_CHANNELS = {"prices", "alerts", "macro", "explainers", "system"}


# ─── Hub ─────────────────────────────────────────────────────────────────────

class StreamHub:
    def __init__(self):
        # ws -> set of subscribed channels
        self._conns: Dict[object, Set[str]] = {}
        self._lock = asyncio.Lock()
        # Per-channel last-sent payload (dedup)
        self._last: Dict[str, str] = {}

    async def connect(self, ws):
        await ws.accept()
        async with self._lock:
            self._conns[ws] = set()
        try:
            from production import log
            log("INFO", "ws", "client connected", total=len(self._conns))
        except Exception: pass

    async def disconnect(self, ws):
        async with self._lock:
            self._conns.pop(ws, None)

    async def subscribe(self, ws, channels: list):
        valid = [c for c in channels if c in VALID_CHANNELS]
        async with self._lock:
            if ws in self._conns:
                self._conns[ws] |= set(valid)
        return valid

    async def unsubscribe(self, ws, channels: list):
        async with self._lock:
            if ws in self._conns:
                self._conns[ws] -= set(channels)

    async def publish(self, channel: str, payload: dict, dedup_key: str = None):
        """Push a payload to every subscriber of this channel."""
        if channel not in VALID_CHANNELS:
            return
        # Dedup
        if dedup_key:
            sig = json.dumps({"k": dedup_key, "v": payload}, sort_keys=True, default=str)
            last = self._last.get(channel)
            if last == sig:
                return
            self._last[channel] = sig

        msg = {"channel": channel, "payload": payload, "server_ts_ms": int(time.time() * 1000)}
        text = json.dumps(msg, default=str)

        async with self._lock:
            targets = [ws for ws, ch in self._conns.items() if channel in ch]

        if not targets:
            return

        dead = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for d in dead:
                    self._conns.pop(d, None)

    def stats(self) -> dict:
        per_channel = {c: 0 for c in VALID_CHANNELS}
        for ch_set in self._conns.values():
            for ch in ch_set:
                if ch in per_channel:
                    per_channel[ch] += 1
        return {"connections": len(self._conns), "per_channel": per_channel}


hub = StreamHub()


# ─── Price publisher with debouncing + dedup ─────────────────────────────────

class PricePublisher:
    """Polls the live price cache every N seconds, publishes only the
    changed assets, batched into one message per cycle."""
    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self.last_sent: Dict[str, dict] = {}

    def _gather(self) -> dict:
        """Pull current macro-bar values + FX pairs into a single dict."""
        out = {}
        try:
            from live_prices import get_live_prices
            lp = get_live_prices() or {}
            # Tiles shown in the macro bar
            tiles = [
                ("GOLD",   "commodities", "GOLD"),
                ("DXY",    "fx", "DXY"),
                ("NASDAQ", "global", "NASDAQ"),
                ("US10Y",  "bonds", "US_10Y"),
                ("OIL",    "commodities", "CRUDE"),
                ("BTC",    "crypto", "BTC"),
                ("VIX",    "vix", "VIX"),
            ]
            for key, cat, sub in tiles:
                v = lp.get(cat, {}).get(sub) or {}
                price = v.get("price")
                change = v.get("change")
                if price is None: continue
                out[key] = {"price": float(price), "change": float(change or 0)}
        except Exception: pass

        try:
            from forex import get_forex_intel
            fxi = get_forex_intel() or {}
            for pair_name, p in (fxi.get("pairs") or {}).items():
                if not p.get("price"): continue
                key = pair_name.replace("/", "")  # EURUSD, USDJPY etc.
                out["FX_" + key] = {
                    "price":      float(p["price"]),
                    "change":     float(p.get("change_pct") or 0),
                    "direction":  p.get("direction"),
                    "confidence": p.get("confidence"),
                    "driver":     p.get("driver"),
                }
        except Exception: pass

        return out

    async def run(self):
        try:
            from production import log, heartbeat
            log("INFO", "ws.prices", "publisher started", interval=self.interval)
        except Exception: pass

        while True:
            try:
                cur = self._gather()
                # Build a delta — only assets that changed since last publish
                delta = {}
                for asset, data in cur.items():
                    last = self.last_sent.get(asset)
                    if last is None or last.get("price") != data["price"] or abs((last.get("change", 0)) - data["change"]) > 0.001:
                        delta[asset] = data
                        self.last_sent[asset] = data
                if delta:
                    await hub.publish("prices", delta)
                try:
                    from production import heartbeat
                    heartbeat("price_publisher")
                except Exception: pass
            except Exception as e:
                try:
                    from production import log
                    log("ERROR", "ws.prices", "publisher iter", err=type(e).__name__, msg=str(e)[:120])
                except Exception: pass
            await asyncio.sleep(self.interval)


# ─── Convenience publishers used by other background tasks ────────────────────

async def publish_macro_snapshot(view: dict):
    """Publish the latest macro_desk view (commentary + dimensions)."""
    payload = {
        "commentary":         view.get("commentary"),
        "dominant_driver":    view.get("dominant_driver"),
        "overall_confidence": view.get("overall_confidence"),
        "ts":                 view.get("generated_at"),
    }
    await hub.publish("macro", payload, dedup_key="macro_snap")


async def publish_alert(alert_event: dict):
    """Publish a freshly-sent alert."""
    payload = {
        "trigger_type":  alert_event.get("trigger_type"),
        "title":         alert_event.get("title"),
        "ts":            datetime.now(IST).strftime("%H:%M:%S IST"),
    }
    await hub.publish("alerts", payload)


async def publish_explainer(explanation: dict):
    """Publish a new 'Why Did It Move?' explanation."""
    payload = {
        "asset_key":     explanation.get("asset_key"),
        "asset_display": explanation.get("asset_display"),
        "change_pct":    explanation.get("change_pct"),
        "direction":     explanation.get("direction"),
        "confidence":    explanation.get("confidence"),
        "what_moved":    explanation.get("what_moved"),
        "tags":          explanation.get("tags", []),
        "ts":            explanation.get("ts_ist"),
    }
    await hub.publish("explainers", payload)


# ─── Public stats for /api/health ─────────────────────────────────────────────

def get_streaming_stats() -> dict:
    return {**hub.stats(), "ok": True}
