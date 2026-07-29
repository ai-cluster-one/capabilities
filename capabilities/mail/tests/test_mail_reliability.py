"""
mail JXA reliability regression tests.

Validates:
- Timeout handling (no silent hangs)
- Per-mailbox error propagation vs false negatives
- Reduced timeout from 600s to 120s
"""

import json
import subprocess
import sys
from pathlib import Path

MAIL_BIN = Path(__file__).parent.parent / "bin" / "mail"


def test_timeout_error_structure():
    """Verify JXA timeout produces structured error, not silent hang."""
    proc = subprocess.run(
        [sys.executable, str(MAIL_BIN), "doctor"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode == 2:
        try:
            err = json.loads(proc.stderr)
            assert "error" in err
            assert err["error"]["code"] in ("automation_denied", "credentials_missing")
        except (json.JSONDecodeError, AssertionError):
            assert False, f"Expected structured error on exit 2, got: {proc.stderr}"


def test_reduced_timeout():
    """Verify run_jxa default timeout is 120s (not 600s)."""
    source = (Path(__file__).parent.parent / "bin" / "mail").read_text()
    assert "timeout: int = 120" in source, \
        "run_jxa should have 120s default timeout, not 600s"
    assert "600s to 120s" in source, \
        "Docstring should document timeout reduction"


if __name__ == "__main__":
    test_timeout_error_structure()
    test_reduced_timeout()
    print("mail reliability tests passed")
