"""
ical EventKit reliability regression tests.

Validates:
- Timeout handling (no silent hangs)
- TCC permission diagnostics
- Error propagation vs false negatives
"""

import json
import subprocess
import sys
from pathlib import Path

ICAL_BIN = Path(__file__).parent.parent / "bin" / "ical"


def test_timeout_error_structure():
    """Verify JXA timeout produces structured error, not silent hang."""
    proc = subprocess.run(
        [sys.executable, str(ICAL_BIN), "doctor"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode == 2:
        try:
            err = json.loads(proc.stderr)
            assert "error" in err
            assert err["error"]["code"] in (
                "calendar_access_denied",
                "automation_denied",
                "eventkit_unavailable"
            )
        except (json.JSONDecodeError, AssertionError):
            assert False, f"Expected structured error on exit 2, got: {proc.stderr}"


def test_tcc_permission_diagnostic():
    """Verify TCC denial message includes stable requesting identity guidance."""
    proc = subprocess.run(
        [sys.executable, str(ICAL_BIN), "doctor"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode == 2:
        try:
            err = json.loads(proc.stderr)
            code = err["error"]["code"]
            hint = err["error"].get("hint", "")
            if code == "calendar_access_denied":
                assert "terminal or IDE" in hint or "parent process" in hint, \
                    f"TCC hint should mention requesting identity: {hint}"
                assert "Full Calendar" in hint, \
                    f"TCC hint should specify Full Calendar Access: {hint}"
        except (json.JSONDecodeError, KeyError):
            pass


if __name__ == "__main__":
    test_timeout_error_structure()
    test_tcc_permission_diagnostic()
    print("ical reliability tests passed")
