"""
test_stage2.py — Unit tests for macro_reasoning_engine Stage 2 analyzers.

Runs without pytest. Invoke directly:
    python -m tests.test_stage2
    # or from container:
    docker exec market-terminal python /app/tests/test_stage2.py

Each test builds a synthetic snapshot and asserts on the analyzer output.
Functions are pure → tests are pure: same snapshot in, same dict out.
"""
import os
import sys
import time
import traceback

# Path setup so the file runs from any directory
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from macro_reasoning_engine import (
    analyze_yields, analyze_usd, analyze_volatility,
    analyze_sentiment, analyze_events, analyze_stage2,
    DIR_RISING, DIR_FALLING, DIR_FLAT,
    DIR_STRONG, DIR_WEAK, DIR_RANGE,
    BIAS_HAWKISH, BIAS_DOVISH, BIAS_NEUTRAL,
    VOL_COMPRESSED, VOL_NORMAL, VOL_HIGH, VOL_EXTREME,
    SENT_BULLISH, SENT_BEARISH, SENT_NEUTRAL,
    MOVER_FIRST, MOVER_SECOND, MOVER_STALE, MOVER_NONE,
)


# ─── Tiny test runner (no pytest dep) ──────────────────────────────────────
PASS, FAIL = 0, 0
FAILURES: list[str] = []


def _check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  ({detail})")


# ─── Snapshot builders ─────────────────────────────────────────────────────
def snap_with_us10y(level: float, change_pct: float = None) -> dict:
    m = {"price": level} if change_pct is None else {"price": level, "change_pct": change_pct}
    return {"macro_snapshot": {"us10y": m}}


def snap_with_dxy(level: float, change_pct: float = None) -> dict:
    m = {"price": level} if change_pct is None else {"price": level, "change_pct": change_pct}
    return {"macro_snapshot": {"dxy": m}}


def snap_with_vix(level: float) -> dict:
    return {"macro_snapshot": {"vix": {"price": level}}}


def snap_with_sentiment(tilt: float, sample: int = 20) -> dict:
    return {"sentiment": {"tilt_score": tilt, "sample_size": sample, "macro_tilt": "?"}}


def cluster(topic: str, category: str, severity: int, age_hours: float, direction: str = "BEAR_RISK"):
    ts = time.time() - age_hours * 3600
    return {
        "topic": topic,
        "event": {"category": category, "severity": severity, "direction": direction},
        "headlines": [{"ts": ts, "source": "Reuters", "text": topic}],
        "size": 1,
        "tickers": [],
        "first_mover": "Reuters",
    }


# ═══════════════════════════════════════════════════════════════════════════
# YIELDS
# ═══════════════════════════════════════════════════════════════════════════
def test_yields():
    print("\n═══ analyze_yields ═══")

    # Rising — 4.46 with +2% (~9bp at 4.46 level)
    r = analyze_yields(snap_with_us10y(4.46, change_pct=2.0))
    _check("yields_rising_above_threshold", r["direction"] == DIR_RISING,
           f"got {r['direction']} delta={r['us10y_delta_bp']}")

    # Falling — 4.40 with -3% (~13bp drop)
    r = analyze_yields(snap_with_us10y(4.40, change_pct=-3.0))
    _check("yields_falling_below_threshold", r["direction"] == DIR_FALLING,
           f"got {r['direction']} delta={r['us10y_delta_bp']}")

    # Flat — tiny move
    r = analyze_yields(snap_with_us10y(4.40, change_pct=0.05))
    _check("yields_flat", r["direction"] == DIR_FLAT,
           f"got {r['direction']} delta={r['us10y_delta_bp']}")

    # No change_pct → FLAT
    r = analyze_yields(snap_with_us10y(4.40))
    _check("yields_no_change_returns_flat", r["direction"] == DIR_FLAT,
           f"got {r['direction']}")

    # Fed bias HAWKISH — rising yields alone (≥+0.3 threshold)
    r = analyze_yields(snap_with_us10y(4.50, change_pct=2.0))
    _check("yields_fed_bias_hawkish_from_yields_only",
           r["fed_bias"] == BIAS_HAWKISH,
           f"got {r['fed_bias']} score={r['fed_score']}")

    # Fed bias DOVISH — falling yields alone
    r = analyze_yields(snap_with_us10y(4.30, change_pct=-2.5))
    _check("yields_fed_bias_dovish_from_yields_only",
           r["fed_bias"] == BIAS_DOVISH,
           f"got {r['fed_bias']} score={r['fed_score']}")

    # Fed bias HAWKISH with monetary news amplification
    s = snap_with_us10y(4.45, change_pct=1.5)
    s["events_classified"] = {
        "by_category": {"MONETARY": {"count": 3, "max_sev": 9, "avg_sev": 7}},
        "directional":  {"bull_count": 0, "bear_count": 3,
                          "bull_weighted": 0, "bear_weighted": 27},
        "total_classified": 3,
    }
    r = analyze_yields(s)
    _check("yields_fed_bias_amplified_by_hawkish_news",
           r["fed_bias"] == BIAS_HAWKISH,
           f"got {r['fed_bias']} score={r['fed_score']}")

    # Term premium signal expands when delta > 8bp
    r = analyze_yields(snap_with_us10y(4.50, change_pct=3.0))   # ~13bp
    _check("yields_term_premium_expanding",
           r["term_premium_signal"] == "expanding",
           f"got {r['term_premium_signal']}")


# ═══════════════════════════════════════════════════════════════════════════
# USD
# ═══════════════════════════════════════════════════════════════════════════
def test_usd():
    print("\n═══ analyze_usd ═══")

    # STRONG — +0.45%
    r = analyze_usd(snap_with_dxy(99.27, change_pct=0.45))
    _check("usd_strong_on_positive_move",
           r["direction"] == DIR_STRONG and r["liquidity_stance"] == "tightening",
           f"dir={r['direction']} liq={r['liquidity_stance']}")

    # WEAK — -0.50%
    r = analyze_usd(snap_with_dxy(102.10, change_pct=-0.50))
    _check("usd_weak_on_negative_move",
           r["direction"] == DIR_WEAK and r["liquidity_stance"] == "easing",
           f"dir={r['direction']} liq={r['liquidity_stance']}")

    # RANGE — drift < 0.15%
    r = analyze_usd(snap_with_dxy(99.50, change_pct=0.08))
    _check("usd_range_on_small_drift",
           r["direction"] == DIR_RANGE and r["liquidity_stance"] == "neutral",
           f"dir={r['direction']} liq={r['liquidity_stance']}")

    # No change_pct field → RANGE (neutral fallback)
    r = analyze_usd(snap_with_dxy(99.50))
    _check("usd_range_when_no_delta_available",
           r["direction"] == DIR_RANGE,
           f"dir={r['direction']}")


# ═══════════════════════════════════════════════════════════════════════════
# VOLATILITY
# ═══════════════════════════════════════════════════════════════════════════
def test_volatility():
    print("\n═══ analyze_volatility ═══")

    r = analyze_volatility(snap_with_vix(11.5))
    _check("vol_compressed_below_13", r["regime"] == VOL_COMPRESSED, f"got {r['regime']}")

    r = analyze_volatility(snap_with_vix(15.0))
    _check("vol_normal_13_to_17", r["regime"] == VOL_NORMAL, f"got {r['regime']}")

    r = analyze_volatility(snap_with_vix(20.0))
    _check("vol_high_17_to_25", r["regime"] == VOL_HIGH, f"got {r['regime']}")

    r = analyze_volatility(snap_with_vix(32.5))
    _check("vol_extreme_above_25", r["regime"] == VOL_EXTREME, f"got {r['regime']}")

    # Fallback to regime_state when VIX missing
    r = analyze_volatility({
        "macro_snapshot": {},
        "regime_state": {"dimensions": {"VOLATILITY": {"score": 80}}},
    })
    _check("vol_fallback_to_regime_state", r["regime"] == VOL_EXTREME, f"got {r['regime']}")


# ═══════════════════════════════════════════════════════════════════════════
# SENTIMENT
# ═══════════════════════════════════════════════════════════════════════════
def test_sentiment():
    print("\n═══ analyze_sentiment ═══")

    r = analyze_sentiment(snap_with_sentiment(0.30))
    _check("sentiment_bullish_above_0.15",
           r["label"] == SENT_BULLISH and r["tilt"] == 0.30,
           f"got {r['label']}")

    r = analyze_sentiment(snap_with_sentiment(-0.30))
    _check("sentiment_bearish_below_-0.15",
           r["label"] == SENT_BEARISH and r["tilt"] == -0.30,
           f"got {r['label']}")

    r = analyze_sentiment(snap_with_sentiment(0.05))
    _check("sentiment_neutral_in_band", r["label"] == SENT_NEUTRAL, f"got {r['label']}")

    # Zero sample size always neutral even with non-zero tilt
    r = analyze_sentiment(snap_with_sentiment(0.50, sample=0))
    _check("sentiment_neutral_when_no_samples",
           r["label"] == SENT_NEUTRAL,
           f"got {r['label']} samples={r['sample_size']}")


# ═══════════════════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════════════════
def test_events():
    print("\n═══ analyze_events ═══")

    snap = {
        "news": {"clusters": [
            cluster("Apple Q3 beats", "EARNINGS", 6, age_hours=10),
            cluster("Fed pauses rate hikes",  "MONETARY", 9, age_hours=2.0, direction="BULL_RISK"),
            cluster("Random market chatter",  "UNKNOWN",  3, age_hours=5),
        ]}
    }
    r = analyze_events(snap)
    _check("events_dominant_picks_highest_severity",
           r["dominant_event"]["category"] == "MONETARY"
           and r["dominant_event"]["severity"] == 9,
           f"got {r['dominant_event']}")
    _check("events_first_mover_for_recent_high_severity",
           r["first_or_second_mover"] == MOVER_FIRST,
           f"got {r['first_or_second_mover']}")
    _check("events_catalyst_window_72h_for_severity_9",
           r["catalyst_window_hours"] == 72,
           f"got {r['catalyst_window_hours']}")

    # Second-mover: high severity but older
    snap2 = {"news": {"clusters": [
        cluster("Old news still relevant", "GROWTH", 8, age_hours=12),
    ]}}
    r = analyze_events(snap2)
    _check("events_second_mover_for_aged_event",
           r["first_or_second_mover"] == MOVER_SECOND,
           f"got {r['first_or_second_mover']}")

    # Stale
    snap3 = {"news": {"clusters": [
        cluster("Yesterday's news", "GROWTH", 7, age_hours=30),
    ]}}
    r = analyze_events(snap3)
    _check("events_stale_for_over_24h",
           r["first_or_second_mover"] == MOVER_STALE,
           f"got {r['first_or_second_mover']}")

    # Empty cluster list
    r = analyze_events({"news": {"clusters": []}})
    _check("events_none_when_no_clusters",
           r["dominant_event"] is None
           and r["first_or_second_mover"] == MOVER_NONE,
           f"got {r}")


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 2 COMPOSER + LATENCY
# ═══════════════════════════════════════════════════════════════════════════
def test_stage2_composer_and_latency():
    print("\n═══ analyze_stage2 (composer + latency) ═══")
    snap = {
        "macro_snapshot": {
            "us10y": {"price": 4.46, "change_pct": 1.5},
            "dxy":   {"price": 99.27, "change_pct": 0.45},
            "vix":   {"price": 14.2},
        },
        "sentiment": {"tilt_score": -0.38, "sample_size": 43, "macro_tilt": "BEARISH"},
        "events_classified": {
            "by_category": {"MONETARY": {"count": 1, "max_sev": 9, "avg_sev": 9}},
            "directional":  {"bull_count": 0, "bear_count": 1,
                              "bull_weighted": 0, "bear_weighted": 9},
            "total_classified": 1,
        },
        "news": {"clusters": [cluster("Fed minutes hawkish", "MONETARY", 9, 1.5)]},
    }

    t0 = time.time()
    out = analyze_stage2(snap)
    elapsed_ms = (time.time() - t0) * 1000
    _check("stage2_returns_all_five_analyzers",
           set(out.keys()) >= {"yields","usd","volatility","sentiment","events"},
           f"got keys={list(out.keys())}")
    _check("stage2_latency_under_100ms_cold",
           elapsed_ms < 100,
           f"elapsed_ms={elapsed_ms:.1f}")
    _check("stage2_yields_rising_on_composite_input",
           out["yields"]["direction"] == DIR_RISING,
           f"got {out['yields']['direction']}")
    _check("stage2_usd_strong_on_composite_input",
           out["usd"]["direction"] == DIR_STRONG,
           f"got {out['usd']['direction']}")
    _check("stage2_vol_normal_on_vix_14",
           out["volatility"]["regime"] == VOL_NORMAL,
           f"got {out['volatility']['regime']}")
    _check("stage2_sentiment_bearish_on_neg_tilt",
           out["sentiment"]["label"] == SENT_BEARISH,
           f"got {out['sentiment']['label']}")
    _check("stage2_event_first_mover_on_recent_severity9",
           out["events"]["first_or_second_mover"] == MOVER_FIRST,
           f"got {out['events']['first_or_second_mover']}")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("Stage 2 analyzer unit tests")
    print("═" * 60)

    for test in (test_yields, test_usd, test_volatility,
                  test_sentiment, test_events,
                  test_stage2_composer_and_latency):
        try:
            test()
        except Exception:
            print(f"  EXCEPTION in {test.__name__}:")
            print(traceback.format_exc())
            global FAIL
            FAIL += 1
            FAILURES.append(f"{test.__name__}: EXCEPTION")

    print()
    print("═" * 60)
    print(f"  {PASS} passed   {FAIL} failed")
    if FAILURES:
        print("  failures:")
        for f in FAILURES:
            print(f"    - {f}")
    print("═" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
