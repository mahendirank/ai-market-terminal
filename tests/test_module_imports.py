"""Sprint-1 safety net: every *.py in core/ must import cleanly.

This test will catch broken modules (e.g. engine.py with the
build_system_prompt typo) before we even reach task #6.

Imports run in a SUBPROCESS so the test process is not polluted by
auth.init_auth_db() / alert_engine._init_db() side effects.
"""
import subprocess
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent

# Modules we explicitly KNOW are broken or orphan and shouldn't fail the build.
# Each entry must have a comment explaining why. Sprint 2/3 should empty this list.
KNOWN_BROKEN: dict[str, str] = {
    # Empty after Sprint 1 task #6:
    #   - engine.py: deleted (orphan + broken import)
    #   - run.py: wrapped in __main__ guard
    #   - claude_bridge.py: wrapped in __main__ guard
}

# Files that aren't application modules.
IGNORE_DIRS = {"__pycache__", "db", "reviews", "static", "templates", "tests", "scripts"}


def _python_modules():
    """Yield every top-level *.py under core/ that should be importable."""
    for p in CORE.glob("*.py"):
        if p.name == "__init__.py":
            continue
        yield p


@pytest.mark.smoke
@pytest.mark.parametrize("module_path", list(_python_modules()), ids=lambda p: p.name)
def test_module_imports(module_path, tmp_path):
    """Each top-level module must import in a fresh Python process."""
    if module_path.name in KNOWN_BROKEN:
        pytest.skip(f"Known broken: {KNOWN_BROKEN[module_path.name]}")

    module_name = module_path.stem
    # Use a tmpdir as cwd so any "db/" files written at import-time
    # land somewhere we control and clean up automatically.
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(CORE)!r}); import {module_name}"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Failed to import {module_name}:\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
