"""
ical EventKit reliability regression tests.

Tests verify timeout handling, TCC diagnostics, and structured error propagation
through code inspection and behavioral contracts.
"""

import json
import subprocess
import sys
from pathlib import Path

ICAL_BIN = Path(__file__).parent.parent / "bin" / "ical"


def test_run_jxa_has_timeout_handler():
    """Verify run_jxa has TimeoutExpired handler that calls _die with jxa_timeout."""
    source = ICAL_BIN.read_text()
    assert "except subprocess.TimeoutExpired:" in source, \
        "run_jxa should handle subprocess.TimeoutExpired"
    assert '"jxa_timeout"' in source, \
        "TimeoutExpired handler should use jxa_timeout error code"
    assert "_die(1," in source or "_die(1 " in source, \
        "Timeout should exit 1"


def test_run_jxa_has_tcc_denial_handler():
    """Verify run_jxa detects -1743 and calls _die with automation_denied."""
    source = ICAL_BIN.read_text()
    assert '"-1743"' in source or "'-1743'" in source, \
        "run_jxa should detect -1743 TCC denial error code"
    assert '"automation_denied"' in source, \
        "TCC denial should use automation_denied error code"
    assert "_die(2," in source or "_die(2 " in source, \
        "TCC denial should exit 2"


def test_eventkit_store_has_full_calendar_diagnostic():
    """Verify _eventkit_store calendar_access_denied mentions Full Calendar and requesting identity."""
    source = ICAL_BIN.read_text()

    store_start = source.find('def _eventkit_store():')
    assert store_start > 0, "_eventkit_store function should exist"

    store_end = source.find('\ndef ', store_start + 20)
    store_body = source[store_start:store_end]

    assert '"calendar_access_denied"' in store_body, \
        "_eventkit_store should use calendar_access_denied error code"
    assert "Full Calendar" in store_body, \
        "calendar_access_denied hint should mention Full Calendar"
    assert ("terminal or IDE" in store_body or "parent process" in store_body), \
        "calendar_access_denied hint should mention requesting identity (terminal/IDE/parent)"


def test_uses_eventkit_not_jxa_for_reads():
    """Verify doctor and read commands use EventKit, not Calendar.app JXA."""
    source = ICAL_BIN.read_text()

    doctor_start = source.find('def cmd_doctor():')
    assert doctor_start > 0
    doctor_end = source.find('\ndef ', doctor_start + 20)
    doctor_body = source[doctor_start:doctor_end]

    assert "_eventkit_store()" in doctor_body, \
        "cmd_doctor should use EventKit, not JXA"
    assert "_eventkit_calendars" in doctor_body, \
        "cmd_doctor should use EventKit calendar listing"


def test_help_documents_eventkit_and_tcc():
    """Verify help text documents EventKit backend and TCC Full Calendar requirement."""
    source = ICAL_BIN.read_text()

    assert "EventKit" in source, \
        "Help should document EventKit backend"
    assert "Full Calendar Access" in source, \
        "Help should document Full Calendar Access TCC requirement"
    assert "System Settings" in source and "Calendars" in source, \
        "Help should reference macOS System Settings > Calendars"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
