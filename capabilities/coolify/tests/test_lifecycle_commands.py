#!/usr/bin/env python3
"""Tests for the in-place lifecycle verbs (start, stop, restart).

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_lifecycle_commands.py
(the coolify bin declares httpx in its PEP-723 header, so bare python3 cannot import it)
"""

import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
_code = _coolify_path.read_text()
coolify_module = types.ModuleType("coolify_lifecycle")
coolify_module.__file__ = str(_coolify_path)
exec(_code, coolify_module.__dict__)
sys.modules["coolify_lifecycle"] = coolify_module


def _record_calls():
    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path,
                      "params": params, "body": json_body})
        return {"message": "queued"}

    return calls, mock_request


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
@pytest.mark.parametrize("rtype,base", [
    ("application", "applications"),
    ("service", "services"),
    ("database", "databases"),
])
def test_lifecycle_posts_to_the_typed_endpoint(action, rtype, base):
    """Coolify's lifecycle endpoints answer GET with 405; the verbs send POST."""
    calls, mock_request = _record_calls()

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        result = coolify_module.cmd_lifecycle(None, action, "res-uuid", rtype)

    assert result == {"message": "queued"}
    assert calls == [{
        "method": "POST",
        "path": f"/{base}/res-uuid/{action}",
        "params": None,
        "body": None,
    }]


def test_deploy_keeps_its_post_with_query_parameters():
    calls, mock_request = _record_calls()

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_deploy(None, "app-uuid", True, "v1")

    assert calls == [{
        "method": "POST",
        "path": "/deploy",
        "params": {"uuid": "app-uuid", "force": "true", "tag": "v1"},
        "body": None,
    }]


def test_ability_probe_stays_on_the_non_mutating_get_route():
    """The probe reads the middleware verdict, so it must not send POST."""
    seen = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def get(self, path):
            seen.append(path)
            return FakeResponse()

    assert coolify_module._probe_effective_ability(FakeClient(), "/deploy") is True
    assert seen == ["/deploy"]


def test_help_does_not_claim_a_get_deployment():
    help_text = coolify_module.__doc__ or ""
    assert "(POST /deploy)" in help_text
    assert "(GET /deploy)" not in help_text
