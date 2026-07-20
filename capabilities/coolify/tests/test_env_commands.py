#!/usr/bin/env python3
"""Tests for coolify env commands, especially duplicate key handling.

Run with: uv run --with httpx python3 capabilities/coolify/tests/test_env_commands.py
(the coolify bin declares httpx in its PEP-723 header, so bare python3 cannot import it)
"""

import sys
from pathlib import Path
from unittest.mock import patch
import types

# Load the coolify script as a module by reading and exec'ing it
_coolify_path = Path(__file__).parent.parent / "bin" / "coolify"
_code = _coolify_path.read_text()
coolify_module = types.ModuleType("coolify")
exec(_code, coolify_module.__dict__)
sys.modules["coolify"] = coolify_module


def test_env_set_creates_when_key_absent():
    """env set creates new entry when key doesn't exist."""
    responses = iter([
        [],  # GET env list - empty
        {"created": True}  # POST create
    ])

    def mock_request(c, method, path, params=None, json_body=None):
        return next(responses)

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_env_set(None, "test-uuid", "NEW_KEY", "new_value")
        assert result == {"created": True}


def test_env_set_updates_all_matching_entries():
    """env set updates ALL entries (preview + non-preview) when key exists."""
    existing = [
        {"uuid": "env-uuid-1", "key": "SHARED_KEY", "value": "old_value", "is_preview": False},
        {"uuid": "env-uuid-2", "key": "SHARED_KEY", "value": "old_value", "is_preview": True}
    ]

    patch_calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        if method == "GET":
            return existing
        elif method == "PATCH":
            patch_calls.append(json_body)
            return {"patched": True}
        return {}

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_env_set(None, "test-uuid", "SHARED_KEY", "new_value")

        assert result["updated"] == 2
        assert result["key"] == "SHARED_KEY"
        assert len(patch_calls) == 2
        assert patch_calls[0]["is_preview"] is False
        assert patch_calls[1]["is_preview"] is True


def test_env_rm_deletes_all_matching_entries():
    """env rm deletes ALL entries with matching key (preview + non-preview)."""
    existing = [
        {"uuid": "env-uuid-1", "key": "DOOMED_KEY", "value": "value", "is_preview": False},
        {"uuid": "env-uuid-2", "key": "DOOMED_KEY", "value": "value", "is_preview": True},
        {"uuid": "env-uuid-3", "key": "OTHER_KEY", "value": "safe", "is_preview": False}
    ]

    deleted_uuids = []

    def mock_request(c, method, path, params=None, json_body=None):
        if method == "GET":
            return existing
        elif method == "DELETE":
            uuid = path.split("/")[-1]
            deleted_uuids.append(uuid)
            return {}
        return {}

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_env_rm(None, "test-uuid", "DOOMED_KEY")

        assert result["deleted"] == 2
        assert result["key"] == "DOOMED_KEY"
        assert len(result["entries"]) == 2
        assert "env-uuid-1" in deleted_uuids
        assert "env-uuid-2" in deleted_uuids
        assert "env-uuid-3" not in deleted_uuids


def test_env_rm_reports_preview_status():
    """env rm reports which entries were preview and which were not."""
    existing = [
        {"uuid": "env-uuid-1", "key": "TEST_KEY", "value": "v", "is_preview": False},
        {"uuid": "env-uuid-2", "key": "TEST_KEY", "value": "v", "is_preview": True}
    ]

    def mock_request(c, method, path, params=None, json_body=None):
        if method == "GET":
            return existing
        return {}

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_env_rm(None, "test-uuid", "TEST_KEY")

        assert result["entries"][0]["is_preview"] is False
        assert result["entries"][1]["is_preview"] is True


def test_env_rm_fails_when_key_not_found():
    """env rm exits with 'not_found' when key doesn't exist."""
    existing = [
        {"uuid": "env-uuid-1", "key": "OTHER_KEY", "value": "value", "is_preview": False}
    ]

    def mock_request(c, method, path, params=None, json_body=None):
        return existing

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        try:
            coolify_module.cmd_env_rm(None, "test-uuid", "MISSING_KEY")
            assert False, "Should have raised SystemExit"
        except SystemExit as e:
            assert e.code == 3


def test_normalize_env_list_handles_bare_list():
    """_normalize_env_list handles bare list response from Coolify 4.1.2."""
    bare_list = [{"uuid": "1", "key": "K"}]
    assert coolify_module._normalize_env_list(bare_list) == bare_list


def test_normalize_env_list_handles_data_wrapper():
    """_normalize_env_list handles dict-with-data wrapper from other Coolify versions."""
    wrapped = {"data": [{"uuid": "1", "key": "K"}]}
    assert coolify_module._normalize_env_list(wrapped) == [{"uuid": "1", "key": "K"}]


if __name__ == "__main__":
    import traceback

    tests = [
        ("env set creates when key absent", test_env_set_creates_when_key_absent),
        ("env set updates all matching entries", test_env_set_updates_all_matching_entries),
        ("env rm deletes all matching entries", test_env_rm_deletes_all_matching_entries),
        ("env rm reports preview status", test_env_rm_reports_preview_status),
        ("env rm fails when key not found", test_env_rm_fails_when_key_not_found),
        ("normalize_env_list handles bare list", test_normalize_env_list_handles_bare_list),
        ("normalize_env_list handles data wrapper", test_normalize_env_list_handles_data_wrapper),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_fn()
            print(f"✓ {name}")
            passed += 1
        except Exception:
            print(f"✗ {name}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
