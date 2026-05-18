"""Sprint-1 baseline: auth password hashing must be invertible and reject corrupt input.

These are pure-function tests of `_hash_password` and `_verify_password` in auth.py.
Importing auth.py runs init_auth_db() which opens core/db/auth.db — to keep
this test hermetic we re-exec under a tmpdir so any DB file lands there.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent


def _run_in_tmpdir(snippet: str, tmp_path):
    """Execute `snippet` under a clean tmpdir cwd; return (rc, stdout, stderr)."""
    full = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(CORE)!r})
        from auth import _hash_password, _verify_password
        {snippet}
    """).strip()
    result = subprocess.run(
        [sys.executable, "-c", full],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=15,
    )
    return result.returncode, result.stdout, result.stderr


@pytest.mark.smoke
def test_password_hash_verify_roundtrip(tmp_path):
    """A password hashed with _hash_password must verify with _verify_password."""
    rc, out, err = _run_in_tmpdir(
        """
        h = _hash_password("hunter2-correct-horse")
        assert _verify_password("hunter2-correct-horse", h), "roundtrip failed"
        assert not _verify_password("wrong-password", h), "wrong password verified"
        print("OK")
        """,
        tmp_path,
    )
    assert rc == 0, f"stderr: {err}"
    assert "OK" in out


@pytest.mark.smoke
def test_verify_returns_false_on_garbage_input(tmp_path):
    """_verify_password must not raise on garbage; just return False."""
    rc, out, err = _run_in_tmpdir(
        """
        assert _verify_password("any", "garbage")           is False
        assert _verify_password("any", "")                  is False
        assert _verify_password("any", "no$dollar$dollar")  is False
        print("OK")
        """,
        tmp_path,
    )
    assert rc == 0, f"stderr: {err}"
    assert "OK" in out
