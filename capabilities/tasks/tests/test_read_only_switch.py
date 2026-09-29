#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""Under CAPABILITIES_READ_ONLY the task tracker reads and records nothing.

Every write verb reaches the contract's write gate, which the switch closes
whatever the connection grants, so the queue can be read by a process that may
not move it. These run the CLI as a project would, against TASKS_TEST_DSN in a
schema of their own that they drop, and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

DSN = os.environ.get("TASKS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")
SWITCH = "CAPABILITIES_READ_ONLY"


@pytest.fixture()
def lab(tmp_path):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    schema = "tasks_test_" + secrets.token_hex(4)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    (envelope / "tasks").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1",
        "id": "prj_" + uuid.uuid4().hex[:12], "slug": "lab"}))
    (envelope / "tasks" / "connections.json").write_text(json.dumps({
        "default": "local",
        "connections": {"local": {
            "db_host": info.get("host"), "db_port": str(info.get("port") or 5432),
            "db_user": info.get("user"), "db_name": info.get("dbname"),
            "db_sslmode": info.get("sslmode") or "prefer", "db_schema": schema,
            "secret_env": "TASKS_TEST_PASSWORD", "allow_write": True}}}))
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "TASKS_TEST_PASSWORD": info.get("password") or "",
    })
    for leaked in (SWITCH, "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_STORE_URL", "CAPABILITIES_STORE_MODE"):
        env.pop(leaked, None)
    lab = {"project": project, "env": env}
    assert _tasks(lab, "migrate", "--apply").returncode == 0
    try:
        yield lab
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def _tasks(lab, *args: str, switch: str | None = None) -> subprocess.CompletedProcess:
    env = dict(lab["env"])
    if switch is not None:
        env[SWITCH] = switch
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=lab["project"], env=env,
                          text=True, capture_output=True, timeout=180)


def _total(lab, switch: str | None = None) -> int:
    counted = _tasks(lab, "counts", switch=switch)
    assert counted.returncode == 0, counted.stdout + counted.stderr
    return json.loads(counted.stdout)["total"]


@pytest.mark.parametrize("value", ["1", "true"])
def test_reads_work_and_every_write_verb_is_refused(lab, value):
    made = _tasks(lab, "add", "--type", "proposal", "--title", "before")
    assert made.returncode == 0, made.stdout + made.stderr
    task = json.loads(made.stdout)["created"]

    assert _total(lab, switch=value) == 1
    shown = _tasks(lab, "show", task, switch=value)
    assert shown.returncode == 0, shown.stderr

    for args in (("add", "--type", "proposal", "--title", "under the switch"),
                 ("set", task, "--status", "todo"),
                 ("activity", task, "did something"),
                 ("tag", task, "t"),
                 ("claim", "--key", "none"),
                 ("migrate", "--apply")):
        refused = _tasks(lab, *args, switch=value)
        assert refused.returncode == 4, (args, refused.stdout, refused.stderr)
        error = json.loads(refused.stderr.strip().splitlines()[-1])["error"]
        assert error["code"] == "read_only_switch"
        assert SWITCH in error["message"]

    after = json.loads(_tasks(lab, "show", task).stdout)
    assert after["task"]["status"] == "draft"
    assert after["activities"] == []
    assert _total(lab) == 1


def test_connections_names_the_switch(lab):
    report = json.loads(_tasks(lab, "connections", switch="1").stdout)
    assert report["connections"]["local"]["allow_write"] is False
    assert report["read_only_switch"]["variable"] == SWITCH


@pytest.mark.parametrize("value", [None, "0", "false"])
def test_with_the_switch_off_writes_land_as_before(lab, value):
    made = _tasks(lab, "add", "--type", "proposal", "--title", "off", switch=value)
    assert made.returncode == 0, made.stdout + made.stderr
    assert _total(lab, switch=value) == 1
    assert "read_only_switch" not in json.loads(
        _tasks(lab, "connections", switch=value).stdout)
