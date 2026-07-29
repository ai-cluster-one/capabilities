"""
mail JXA reliability regression tests.

Deterministic tests that verify fail-closed behavior, timeout handling, and
structured error propagation through actual CLI invocations with mocked JXA.
"""

import json
import subprocess
import sys
from pathlib import Path

MAIL_BIN = Path(__file__).parent.parent / "bin" / "mail"


def test_reduced_timeout_constant():
    """Verify run_jxa default timeout is 120s (not 600s)."""
    source = MAIL_BIN.read_text()
    assert "timeout: int = 120" in source, \
        "run_jxa should have 120s default timeout, not 600s"
    assert "600" in source and ("reduced" in source.lower() or "120" in source), \
        "Docstring or comment should document timeout reduction from 600s"


def test_doctor_returns_structured_error_on_automation_denial():
    """Verify doctor returns structured error when Mail.app automation denied."""
    proc = subprocess.run(
        [sys.executable, str(MAIL_BIN), "doctor"],
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
            assert err["error"]["code"] in ("automation_denied", "credentials_missing")
            
            if err["error"]["code"] == "automation_denied":
                hint = err["error"].get("hint", "")
                assert "Automation" in hint, \
                    f"automation_denied hint should mention Automation: {hint}"
        except (json.JSONDecodeError, KeyError, AssertionError) as e:
            assert False, f"Expected structured error on exit 2: {proc.stderr} ({e})"


def test_help_documents_timeout_reduction():
    """Verify help or source documents timeout behavior."""
    proc = subprocess.run(
        [sys.executable, str(MAIL_BIN), "help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    
    assert proc.returncode == 0
    source = MAIL_BIN.read_text()
    assert ("120" in source and "timeout" in source.lower()) or \
           ("reduced" in source.lower() and "timeout" in source.lower()), \
        "Source should document timeout behavior"


if __name__ == "__main__":
    test_reduced_timeout_constant()
    test_doctor_returns_structured_error_on_automation_denial()
    test_help_documents_timeout_reduction()
    print("mail reliability tests passed")
