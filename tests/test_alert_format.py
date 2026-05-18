"""Sprint-1 baseline: alert_engine._fmt_alert must produce a stable structure.

Importing alert_engine triggers _init_redis() and _init_db() — we run the test
in a subprocess under tmpdir so any DB file lands somewhere we own.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent


@pytest.mark.smoke
def test_fmt_alert_contains_all_fields(tmp_path):
    snippet = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(CORE)!r})
        from alert_engine import _fmt_alert
        msg = _fmt_alert(
            emoji="⚠️",
            title="VIX SPIKE",
            what="VIX jumped 22% in 30 minutes",
            why="Risk-off regime emerging",
            assets="SPY, QQQ, GLD",
            position="Reduce equity exposure",
            risk="False alarm if Fed steps in",
        )
        # All field labels must appear
        for needle in ("VIX SPIKE", "What happened", "Why it matters",
                       "Affected assets", "Suggested positioning", "Risk",
                       "VIX jumped 22%", "Reduce equity exposure"):
            assert needle in msg, f"missing: {{needle}}"
        print("OK")
    """).strip()

    result = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "OK" in result.stdout
