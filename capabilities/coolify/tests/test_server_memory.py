#!/usr/bin/env python3
"""Tests for `memory` on server reads, read over the connection's SSH access.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_server_memory.py
"""

import io
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
coolify_module = types.ModuleType("coolify_server_memory")
coolify_module.__file__ = str(_coolify_path)
exec(_coolify_path.read_text(), coolify_module.__dict__)
sys.modules["coolify_server_memory"] = coolify_module

BASE = "https://coolify.example.com"
SSH = {"user": "root", "key_path": "/keys/coolify"}
API = {
    "/servers": [
        {"uuid": "srv-1", "name": "localhost", "ip": "host.docker.internal", "port": 22},
        {"uuid": "srv-2", "name": "worker", "ip": "203.0.113.7", "port": 2222},
    ],
    "/servers/srv-2": {"uuid": "srv-2", "name": "worker", "ip": "203.0.113.7", "port": 2222},
}
MEMINFO = "MemTotal:        8000000 kB\nMemFree:  100 kB\nMemAvailable:    3000000 kB\n"


def _run(argv, ssh, ssh_result):
    ssh_calls = []

    def fake_ssh(cmd, **kwargs):
        ssh_calls.append(cmd)
        return ssh_result(cmd)

    out, err = io.StringIO(), io.StringIO()
    out.isatty = lambda: False
    code = 0
    with (
        patch.object(sys, "argv", ["coolify", *argv]),
        patch.object(sys, "stdout", out),
        patch.object(sys, "stderr", err),
        patch.object(coolify_module, "_gate", lambda: None),
        patch.object(coolify_module, "_resolve_conn", return_value={
            "id": "exp", "allow_write": False, "base_url": BASE, "token": "1|t", "ssh": ssh}),
        patch.object(coolify_module, "_request", side_effect=lambda c, m, path, **k: API[path]),
        patch.object(coolify_module.subprocess, "run", side_effect=fake_ssh),
    ):
        try:
            coolify_module.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    assert code == 0, err.getvalue()
    return json.loads(out.getvalue()), ssh_calls


def _ok(cmd):
    return subprocess.CompletedProcess(cmd, 0, MEMINFO, "")


def test_each_listed_server_carries_its_memory_in_bytes_and_human_units():
    value, calls = _run(["servers"], SSH, _ok)

    assert [r["uuid"] for r in value] == ["srv-1", "srv-2"]
    for record in value:
        assert record["memory"] == {"total_bytes": 8192000000, "available_bytes": 3072000000,
                                    "total": "7.6 GiB", "available": "2.9 GiB"}
        assert record["dashboard_url"] == f"{BASE}/server/{record['uuid']}"
    # the instance's own host is reached at the base_url host; others at their ip and port
    assert calls[0][-2:] == ["root@coolify.example.com", "cat /proc/meminfo"]
    assert calls[1][-2:] == ["root@203.0.113.7", "cat /proc/meminfo"]
    assert calls[1][calls[1].index("-p") + 1] == "2222"
    assert calls[1][calls[1].index("-i") + 1] == "/keys/coolify"
    assert "BatchMode=yes" in calls[1]


def test_a_server_read_by_uuid_carries_its_memory():
    value, _ = _run(["servers", "srv-2"], SSH, _ok)

    assert value["memory"]["available_bytes"] == 3072000000


def test_a_server_that_cannot_be_read_shows_the_reason_and_the_list_succeeds():
    def one_refuses(cmd):
        if cmd[-2] == "root@203.0.113.7":
            return subprocess.CompletedProcess(
                cmd, 255, "", "Warning: x\nroot@203.0.113.7: Permission denied (publickey).\n")
        return _ok(cmd)

    value, _ = _run(["servers"], SSH, one_refuses)

    assert value[0]["memory"]["total_bytes"] == 8192000000
    assert value[1]["memory"] == {
        "error": "ssh to 203.0.113.7 failed: root@203.0.113.7: Permission denied (publickey)."}


def test_an_unreachable_server_times_out_into_a_reason():
    def hangs(cmd):
        raise subprocess.TimeoutExpired(cmd, 30)

    value, _ = _run(["servers", "srv-2"], SSH, hangs)

    assert value["memory"] == {"error": "ssh to 203.0.113.7 timed out"}


def test_without_ssh_access_no_server_is_dialled_and_each_says_why():
    value, calls = _run(["servers"], None, _ok)

    assert calls == []
    assert all("coolify connect --ssh-key" in r["memory"]["error"] for r in value)
    assert all(set(r["memory"]) == {"error"} for r in value)
