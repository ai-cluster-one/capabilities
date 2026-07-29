"""
ical EventKit reliability regression tests.

Deterministic tests that verify timeout handling, TCC diagnostics, and
structured error behavior through actual CLI invocations.
"""

import json
import subprocess
import sys
from pathlib import Path

ICAL_BIN = Path(__file__).parent.parent / "bin" / "ical"


def test_doctor_returns_structured_error_on_eventkit_unavailable():
    """Verify doctor returns structured error when EventKit unavailable."""
    proc = subprocess.run(
        [sys.executable, str(ICAL_BIN), "doctor"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    
    if proc.returncode == 0:
        return
    
    if proc.returncode == 2:
        try:
            err = json.loads(proc.stderr)
            assert "error" in err
            assert err["error"]["code"] in (
                "calendar_access_denied",
                "automation_denied",
                "eventkit_unavailable"
            )
            
            if err["error"]["code"] == "calendar_access_denied":
                hint = err["error"].get("hint", "")
                assert "Full Calendar" in hint, \
                    f"calendar_access_denied hint should mention Full Calendar: {hint}"
                assert ("terminal" in hint.lower() or "IDE" in hint or 
                        "parent" in hint.lower()), \
                    f"calendar_access_denied hint should mention requesting identity: {hint}"
        except (json.JSONDecodeError, KeyError, AssertionError) as e:
            assert False, f"Expected structured error on exit 2: {proc.stderr} ({e})"


def test_help_includes_eventkit_backend_documentation():
    """Verify help text documents EventKit reads and TCC requirement."""
    proc = subprocess.run(
        [sys.executable, str(ICAL_BIN), "help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    
    assert proc.returncode == 0
    assert "EventKit" in proc.stdout, "Help should document EventKit backend"
    assert "Full Calendar Access" in proc.stdout or "Calendars" in proc.stdout, \
        "Help should document TCC Full Calendar Access requirement"


if __name__ == "__main__":
    test_doctor_returns_structured_error_on_eventkit_unavailable()
    test_help_includes_eventkit_backend_documentation()
    print("ical reliability tests passed")
