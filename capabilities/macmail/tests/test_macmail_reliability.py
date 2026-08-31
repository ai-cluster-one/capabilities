"""
macmail JXA reliability regression tests.

Tests verify fail-closed behavior, timeout handling, and structured error
propagation through code inspection and behavioral contracts.
"""

import json
import subprocess
import sys
from pathlib import Path

CAPABILITY = Path(__file__).resolve().parents[1]
MAIL_BIN = next((path for path in (
    CAPABILITY / "bin" / "macmail", CAPABILITY / "macmail")
    if path.is_file()), CAPABILITY / "bin" / "macmail")


def test_run_jxa_has_timeout_handler():
    """Verify run_jxa has TimeoutExpired handler that calls _die with jxa_timeout."""
    source = MAIL_BIN.read_text()
    assert "except subprocess.TimeoutExpired:" in source, \
        "run_jxa should handle subprocess.TimeoutExpired"
    assert '"jxa_timeout"' in source, \
        "TimeoutExpired handler should use jxa_timeout error code"
    assert "_die(1," in source or "_die(1 " in source, \
        "Timeout should exit 1"


def test_cmd_search_fails_closed_on_mailbox_errors():
    """Verify cmd_search exits 1 with incomplete_search when errors array non-empty."""
    source = MAIL_BIN.read_text()
    assert "if errors:" in source, \
        "cmd_search should check for errors array"
    assert '"incomplete_search"' in source, \
        "cmd_search should use incomplete_search error code for mailbox failures"
    assert "Cannot distinguish missing messages from search failures" in source or \
           "cannot determine" in source.lower(), \
        "Error message should explain fail-closed rationale"


def test_cmd_search_success_returns_json_array():
    """Verify cmd_search prints msgs array on success (not object with errors key)."""
    source = MAIL_BIN.read_text()

    search_start = source.find('def cmd_search(')
    assert search_start > 0
    search_end = source.find('\ndef ', search_start + 20)
    search_body = source[search_start:search_end]

    assert 'print(json.dumps(msgs' in search_body, \
        "cmd_search should print msgs array for --json"

    json_print_idx = search_body.find('if args.json:')
    assert json_print_idx > 0
    json_section = search_body[json_print_idx:json_print_idx+200]

    assert 'print(json.dumps(msgs' in json_section, \
        "--json branch should print msgs"
    assert '"errors"' not in json_section or 'if errors:' in json_section, \
        "Successful --json should not include errors (only checked before this point)"


def test_source_fetch_fails_closed_on_mailbox_errors():
    """Verify SOURCE_FETCH JXA fails before checking message found when errors exist."""
    source = MAIL_BIN.read_text()

    source_fetch_start = source.find('SOURCE_FETCH = r"""')
    assert source_fetch_start > 0, "SOURCE_FETCH constant should exist"

    source_fetch_end = source.find('"""', source_fetch_start + 20)
    source_fetch_body = source[source_fetch_start:source_fetch_end]

    assert 'if (errors.length > 0)' in source_fetch_body, \
        "SOURCE_FETCH should check errors.length before checking if message found"
    assert "Cannot determine if message exists" in source_fetch_body, \
        "SOURCE_FETCH should explain it cannot determine if message exists when errors occur"


def test_cmd_show_fails_closed_on_mailbox_errors():
    """Verify cmd_show script fails before checking message found when errors exist."""
    source = MAIL_BIN.read_text()

    show_start = source.find('def cmd_show(')
    assert show_start > 0

    show_end = source.find('\ndef ', show_start + 20)
    show_body = source[show_start:show_end]

    assert 'if (errors.length > 0)' in show_body or \
           'if errors.length > 0' in show_body, \
        "cmd_show JXA should check errors before checking if message found"


def test_reduced_timeout_constant():
    """Verify run_jxa default timeout is 120s (not 600s)."""
    source = MAIL_BIN.read_text()
    assert "timeout: int = 120" in source, \
        "run_jxa should have 120s default timeout, not 600s"
    assert "600" in source, \
        "Source should document timeout reduction from 600s"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
