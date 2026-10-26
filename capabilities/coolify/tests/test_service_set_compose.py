#!/usr/bin/env python3
"""Tests for `service set-compose`: a service's whole Compose document
replaced through PATCH /services/<uuid>, without reading the stored one.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_service_set_compose.py
"""

import base64
import io
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
coolify_module = types.ModuleType("coolify_set_compose")
coolify_module.__file__ = str(_coolify_path)
exec(_coolify_path.read_text(), coolify_module.__dict__)
sys.modules["coolify_set_compose"] = coolify_module

COMPOSE = 'services:\n  agent:\n    image: "registry.example/agent:2"\n'


class Args:
    def __init__(self, compose, uuid="svc-uuid"):
        self.uuid, self.compose = uuid, compose


def _fake():
    calls = []

    def request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return {"uuid": "svc-uuid"}

    return calls, request


def test_set_compose_patches_the_document_without_reading_it(tmp_path):
    calls, request = _fake()
    document = tmp_path / "compose.yaml"
    document.write_text(COMPOSE)
    with patch.object(coolify_module, "_request", side_effect=request):
        answer = coolify_module.cmd_service_set_compose(None, Args(str(document)))
    assert answer == {"uuid": "svc-uuid", "written": True, "next": "coolify deploy svc-uuid"}
    assert [(c["method"], c["path"]) for c in calls] == [("PATCH", "/services/svc-uuid")]
    assert base64.b64decode(calls[0]["body"]["docker_compose_raw"]).decode() == COMPOSE


def test_set_compose_reads_the_document_from_stdin():
    calls, request = _fake()
    with patch.object(coolify_module, "_request", side_effect=request), \
            patch.object(sys, "stdin", io.StringIO(COMPOSE)):
        coolify_module.cmd_service_set_compose(None, Args("-"))
    assert base64.b64decode(calls[0]["body"]["docker_compose_raw"]).decode() == COMPOSE


def test_set_compose_refuses_an_empty_document_before_any_request():
    calls, request = _fake()
    with patch.object(coolify_module, "_request", side_effect=request), \
            patch.object(sys, "stdin", io.StringIO(" \n")):
        with pytest.raises(SystemExit) as exc:
            coolify_module.cmd_service_set_compose(None, Args("-"))
    assert exc.value.code == 6
    assert calls == []
