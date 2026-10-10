#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.4.0"]
# ///
"""Under CAPABILITIES_READ_ONLY the task tracker reads and records nothing.

Every write reaches the contract's write gate, which the switch closes whatever
the connection grants, so the queue can be read by a process that may not move
it. `meta show`, and `migrate` and `run` without `--apply`, write nothing and
answer under the switch and under a read-only grant alike. These run the CLI as
a project would, against TASKS_TEST_DSN in a schema of their own that they
drop, and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' \\
        --with 'capabilities-contract==0.4.0' python -m pytest capabilities/tasks/tests -q
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
    store: dict = {}
    (envelope / "tasks" / "connections.json").write_text(json.dumps({
        "default": "local",
        "connections": {"local": {**store, "allow_write": True},
                        "reader": {**store, "allow_write": False}}}))
    _cli.write_store_setting(tmp_path / "config", schema)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
    })
    for leaked in (SWITCH, "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_STORE_MODE", *_cli.STORE_OVERRIDES):
        env.pop(leaked, None)
    lab = {"project": project, "env": env, "schema": schema, "tmp": tmp_path}
    assert _tasks(lab, "migrate").returncode == 0
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
                 ("migrate",)):
        refused = _tasks(lab, *args, switch=value)
        assert refused.returncode == 4, (args, refused.stdout, refused.stderr)
        error = json.loads(refused.stderr.strip().splitlines()[-1])["error"]
        assert error["code"] == "read_only_switch"
        assert SWITCH in error["message"]

    after = json.loads(_tasks(lab, "show", task).stdout)
    assert after["task"]["status"] == "draft"
    assert after["activities"] == []
    assert _total(lab) == 1


@pytest.mark.parametrize("gate", ["1", "true", "reader"])
def test_the_forms_that_write_nothing_answer_and_their_writes_stay_refused(lab, gate):
    """`meta show`, `migrate status`, and `run` without `--apply`, answer behind
    the gate exactly as they do in front of it, while every form of those verbs
    that writes - however its flags are placed - is still refused before the
    store."""
    made = _tasks(lab, "add", "--type", "proposal", "--title", "forms", "--key", "f-1")
    assert made.returncode == 0, made.stdout + made.stderr
    assert _tasks(lab, "meta", "set", "f-1", "a", "1").returncode == 0
    if gate == "reader":
        switch, connection, code = None, ("--connection", "reader"), "read_only"
    else:
        switch, connection, code = gate, (), "read_only_switch"

    for args in (("meta", "show", "f-1"), ("migrate", "status"), ("run", "default")):
        open_ = _tasks(lab, *args)
        # `migrate` acts on the machine's store, through no connection.
        gated = _tasks(lab, *(connection if args[0] != "migrate" else ()), *args,
                       switch=switch)
        assert gated.returncode == 0, (args, gated.stdout, gated.stderr)
        assert gated.stdout == open_.stdout, args

    before = _tasks(lab, "show", "f-1").stdout
    for args in (("meta", "set", "f-1", "a", "2"),
                 ("meta", "rm", "f-1", "a"),
                 ("meta", "--actor", "show", "set", "f-1", "a", "2"),
                 ("run", "default", "--apply"),
                 ("run", "--apply", "default")):
        refused = _tasks(lab, *connection, *args, switch=switch)
        assert refused.returncode == 4, (args, refused.stdout, refused.stderr)
        error = json.loads(refused.stderr.strip().splitlines()[-1])["error"]
        assert error["code"] == code, args
    assert _tasks(lab, "show", "f-1").stdout == before


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
