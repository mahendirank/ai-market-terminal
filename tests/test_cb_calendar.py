"""
test_cb_calendar.py — Unit tests for the central-bank action tilt.

Covers get_action_tilt() — the cb_action feed that aggregates the
news-inferred per-CB hawk/dove stances into one tilt in [-1, +1].

Runs without pytest:
    docker exec market-terminal python /app/tests/test_cb_calendar.py
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import cb_calendar as cbc


PASS, FAIL = 0, 0
FAILURES: list[str] = []

# News strings that trip the Fed's hawk / dove keyword sets.
HAWKISH_NEWS = ("powell hawkish — rate hike likely, higher for longer, "
                "hawkish fed stance into year end")
DOVISH_NEWS  = ("powell dovish — rate cut coming, fed pivot in play, "
                "dovish fed, fed easing ahead")


def _check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  ({detail})")


def test_output_shape():
    print("\n═══ get_action_tilt: output shape ═══")
    out = cbc.get_action_tilt("")
    _check("has_tilt", "tilt" in out, f"got {set(out)}")
    _check("has_label", "label" in out, f"got {set(out)}")
    _check("has_per_cb", "per_cb" in out, f"got {set(out)}")
    _check("per_cb_all_six_banks",
           set(out["per_cb"]) == set(cbc._ACTION_WEIGHTS),
           f"got {set(out['per_cb'])}")


def test_hawkish_news_negative_tilt():
    print("\n═══ get_action_tilt: hawkish news → negative tilt ═══")
    out = cbc.get_action_tilt(HAWKISH_NEWS)
    _check("hawkish_tilt_negative", out["tilt"] < 0, f"got {out['tilt']}")
    _check("hawkish_label", out["label"] == "hawkish", f"got {out['label']}")
    _check("fed_read_hawkish", out["per_cb"]["FED"]["bias"] == "HAWKISH",
           f"got {out['per_cb']['FED']}")


def test_dovish_news_positive_tilt():
    print("\n═══ get_action_tilt: dovish news → positive tilt ═══")
    out = cbc.get_action_tilt(DOVISH_NEWS)
    _check("dovish_tilt_positive", out["tilt"] > 0, f"got {out['tilt']}")
    _check("dovish_label", out["label"] == "dovish", f"got {out['label']}")


def test_neutral_news_zero_tilt():
    print("\n═══ get_action_tilt: no CB news → neutral ═══")
    out = cbc.get_action_tilt("markets drifted sideways on light volume")
    _check("neutral_tilt_zero", abs(out["tilt"]) < 1e-9, f"got {out['tilt']}")
    _check("neutral_label", out["label"] == "neutral", f"got {out['label']}")


def test_tilt_always_in_range():
    print("\n═══ get_action_tilt: tilt clamped to [-1, +1] ═══")
    for txt in ("", HAWKISH_NEWS, DOVISH_NEWS, HAWKISH_NEWS * 5):
        t = cbc.get_action_tilt(txt)["tilt"]
        _check(f"in_range ({txt[:18]!r})", -1.0 <= t <= 1.0, f"got {t}")


def test_case_insensitive():
    print("\n═══ get_action_tilt: case-insensitive keyword scan ═══")
    lower = cbc.get_action_tilt(HAWKISH_NEWS)["tilt"]
    upper = cbc.get_action_tilt(HAWKISH_NEWS.upper())["tilt"]
    _check("upper_equals_lower", lower == upper, f"lower={lower} upper={upper}")


def test_deterministic():
    print("\n═══ get_action_tilt: deterministic ═══")
    a = cbc.get_action_tilt(DOVISH_NEWS)
    b = cbc.get_action_tilt(DOVISH_NEWS)
    _check("same_input_same_output", a == b, "non-deterministic!")


# ─── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    print("═" * 60)
    print("cb_calendar — central-bank action tilt tests")
    print("═" * 60)
    tests = (
        test_output_shape, test_hawkish_news_negative_tilt,
        test_dovish_news_positive_tilt, test_neutral_news_zero_tilt,
        test_tilt_always_in_range, test_case_insensitive, test_deterministic,
    )
    for test in tests:
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
