#!/usr/bin/env python3
"""Tests for `dashboard_url` on resource and server reads.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_dashboard_url.py
"""

import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
coolify_module = types.ModuleType("coolify_dashboard_url")
coolify_module.__file__ = str(_coolify_path)
exec(_coolify_path.read_text(), coolify_module.__dict__)
sys.modules["coolify_dashboard_url"] = coolify_module

BASE = "https://coolify.example.com"
CONN = {"id": "exp", "allow_write": False, "base_url": BASE, "token": "1|test-token"}

API = {
    "/resources": [
        {"uuid": "app-1", "name": "web", "type": "application", "environment_id": 3},
        {"uuid": "svc-1", "name": "cache", "type": "service", "environment_id": 4},
        {"uuid": "db-1", "name": "pg", "type": "standalone-postgresql", "environment_id": 3},
        {"uuid": "lost-1", "name": "orphan", "type": "application", "environment_id": 99},
    ],
    "/applications/app-1": {"uuid": "app-1", "name": "web", "environment_id": 3,
                            "environment": {"id": 3, "name": "production", "project_id": 1}},
    "/projects": [{"id": 1, "uuid": "proj-a", "name": "a"},
                  {"id": 2, "uuid": "proj-b", "name": "b"}],
    "/projects/proj-a": {"uuid": "proj-a", "environments": [
        {"id": 3, "uuid": "env-prod", "name": "production", "project_id": 1}]},
    "/projects/proj-b": {"uuid": "proj-b", "environments": [
        {"id": 4, "uuid": "env-stage", "name": "staging", "project_id": 2}]},
    "/servers": [{"uuid": "srv-1", "name": "localhost"}],
    "/servers/srv-1": {"uuid": "srv-1", "name": "localhost", "ip": "host.docker.internal"},
}


def _run(argv):
    calls = []

    def fake_request(c, method, path, params=None, json_body=None):
        calls.append(path)
        return API[path]

    out, err = io.StringIO(), io.StringIO()
    out.isatty = lambda: False
    code = 0
    with (
        patch.object(sys, "argv", ["coolify", *argv]),
        patch.object(sys, "stdout", out),
        patch.object(sys, "stderr", err),
        patch.object(coolify_module, "_gate", lambda: None),
        patch.object(coolify_module, "_resolve_conn", return_value=CONN),
        patch.object(coolify_module, "_request", side_effect=fake_request),
    ):
        try:
            coolify_module.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    assert code == 0, err.getvalue()
    return json.loads(out.getvalue()), calls


def test_resources_carry_their_dashboard_page():
    records, _ = _run(["resources"])
    urls = {r["uuid"]: r.get("dashboard_url") for r in records}

    assert urls == {
        "app-1": f"{BASE}/project/proj-a/environment/env-prod/application/app-1",
        "svc-1": f"{BASE}/project/proj-b/environment/env-stage/service/svc-1",
        "db-1": f"{BASE}/project/proj-a/environment/env-prod/database/db-1",
        "lost-1": None,
    }
    assert records[0] == {"uuid": "app-1", "name": "web", "type": "application",
                          "dashboard_url": urls["app-1"]}


def test_a_resource_read_by_uuid_keeps_its_detail_and_adds_the_page():
    record, _ = _run(["applications", "app-1"])

    assert record == {**API["/applications/app-1"], "dashboard_url":
                      f"{BASE}/project/proj-a/environment/env-prod/application/app-1"}


@pytest.mark.parametrize("argv,path", [(["servers"], "/servers"),
                                       (["servers", "srv-1"], "/servers/srv-1")])
def test_server_reads_carry_their_page_without_another_request(argv, path):
    value, calls = _run(argv)
    record = value[0] if isinstance(value, list) else value

    assert record["dashboard_url"] == f"{BASE}/server/srv-1"
    assert calls == [path]
