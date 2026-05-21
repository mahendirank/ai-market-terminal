"""test_health_probes.py — the /api/health data probes must be cache-only.

A data probe must never call a synchronous network getter (get_live_prices,
get_all_news, detect_market_regime, get_forex_intel): on a cold cache that
fetches over the network and blocks — which is what made /api/health take
~13 s right after a restart. These tests cover the cache-peek helper, the
shared verdict, and that every registered data probe returns fast without
raising even when the underlying caches are cold.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import production as p


# ─── _cache_verdict ──────────────────────────────────────────────────────────
def test_cache_verdict_cold():
    v = p._cache_verdict(None, None, stale_after=60, what="prices")
    assert v["ok"] is False
    assert v["cache"] == "cold"


def test_cache_verdict_warm_fresh():
    v = p._cache_verdict({"x": 1}, age=5.0, stale_after=60, what="prices")
    assert v["ok"] is True
    assert v["cache"] == "warm"
    assert v["cache_age_s"] == 5.0


def test_cache_verdict_stale():
    v = p._cache_verdict({"x": 1}, age=999.0, stale_after=60, what="prices")
    assert v["ok"] is False
    assert v["cache"] == "stale"


# ─── _peek_cache ─────────────────────────────────────────────────────────────
def test_peek_cache_missing_module():
    data, age = p._peek_cache("module_that_does_not_exist_xyz", "_cache")
    assert data is None and age is None


def test_peek_cache_missing_attr():
    data, age = p._peek_cache("production", "_attr_that_does_not_exist")
    assert data is None and age is None


def test_peek_cache_reads_populated_cache():
    # production itself is a loaded module — stash a fake cache on it.
    p._fake_probe_cache = {"data": {"k": "v"}, "ts": time.time() - 3}
    try:
        data, age = p._peek_cache("production", "_fake_probe_cache")
        assert data == {"k": "v"}
        assert 0 <= age < 60
    finally:
        del p._fake_probe_cache


def test_peek_cache_cold_entry_is_none():
    p._fake_cold_cache = {"data": None, "ts": 0.0}
    try:
        data, age = p._peek_cache("production", "_fake_cold_cache")
        assert data is None and age is None
    finally:
        del p._fake_cold_cache


def test_peek_cache_nested_sub_key():
    p._fake_nested = {"regime": {"data": {"label": "RISK_ON"}, "ts": time.time()}}
    try:
        data, age = p._peek_cache("production", "_fake_nested", sub="regime")
        assert data == {"label": "RISK_ON"}
        assert age is not None
    finally:
        del p._fake_nested


# ─── the registered data probes ──────────────────────────────────────────────
def test_data_probes_return_fast_without_raising():
    """Every data probe must return a dict with an `ok` key, fast, and must
    not raise — even when the underlying caches are cold. A probe that still
    called a network getter would blow the time budget here."""
    for name in ("live_data", "news", "regime", "forex"):
        probe = p._HEALTH_PROBES[name]
        t0 = time.time()
        result = probe()
        elapsed = time.time() - t0
        assert isinstance(result, dict), f"{name}: returned {type(result)}"
        assert "ok" in result, f"{name}: missing 'ok' key"
        assert elapsed < 1.0, f"{name}: took {elapsed:.2f}s — not cache-only?"


def test_get_health_is_fast():
    """The full aggregator must complete quickly — no probe may block on a
    cold-cache network fetch."""
    t0 = time.time()
    h = p.get_health()
    elapsed = time.time() - t0
    assert "checks" in h and "status" in h
    assert elapsed < 5.0, f"get_health() took {elapsed:.2f}s"
