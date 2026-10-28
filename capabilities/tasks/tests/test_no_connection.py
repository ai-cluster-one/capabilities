#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract>=0.8,<1"]
# ///
"""A project needs no tasks connection, and the time zone is a setting.

The store is the project's store setting, so a project with no tasks connection
usable reads, writes and proves its store with none selected, its writes gated
by the read-only switch alone. A connection usable, or one named, is selected
as before. The zone a bare date is read in is the `timezone` setting - the
project's, then the machine's - then the selected connection's `timezone`, then
the system's. These run the CLI as a project would, against TASKS_TEST_DSN in a
schema of their own that they drop, and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' \\
        --with 'capabilities-contract>=0.8,<1' python -m pytest capabilities/tasks/tests -q
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


def _lab(tmp_path, connections: dict | None):
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
    if connections is not None:
        (envelope / "tasks" / "connections.json").write_text(json.dumps(connections))
    _cli.write_store_setting(tmp_path / "config", schema)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "TZ": "America/New_York",
    })
    for leaked in (SWITCH, "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_STORE_MODE", *_cli.STORE_OVERRIDES):
        env.pop(leaked, None)
    return {"project": project, "env": env, "schema": schema, "tmp": tmp_path}


@pytest.fixture()
def labs(tmp_path):
    import psycopg
    made: list[dict] = []

    def make(connections: dict | None = None) -> dict:
        lab = _lab(tmp_path / str(len(made)), connections)
        made.append(lab)
        return lab

    try:
        yield make
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            for lab in made:
                conn.execute(f"drop schema if exists {lab['schema']} cascade")


def _tasks(lab, *args: str, switch: str | None = None) -> subprocess.CompletedProcess:
    env = dict(lab["env"])
    if switch is not None:
        env[SWITCH] = switch
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=lab["project"], env=env,
                          text=True, capture_output=True, timeout=180)


def _ok(lab, *args: str) -> dict:
    done = _tasks(lab, *args)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return json.loads(done.stdout)


def _refused(done: subprocess.CompletedProcess) -> dict:
    return json.loads(done.stderr.strip().splitlines()[-1])["error"]


def _pickup(lab, title: str) -> str:
    """The moment a task added with a bare pickup date of 2026-11-01 holds."""
    task = _ok(lab, "add", "--type", "change", "--title", title,
               "--pickup", "2026-11-01")["created"]
    return _ok(lab, "show", task)["task"]["pickup_at"]


def test_a_project_with_no_connection_reads_writes_and_proves_its_store(labs):
    lab = labs(None)
    doctor = _ok(lab, "doctor")
    assert doctor["ok"] is True and doctor["connection"] is None
    task = _ok(lab, "add", "--type", "change", "--title", "x")["created"]
    assert [row["id"] for row in _ok(lab, "list")["tasks"]] == [task]
    _ok(lab, "activity", task, "did something")
    assert _ok(lab, "show", task)["activities"]
    # `connections` reports connections alone, and refuses with none declared
    # as the structural audit requires of a capability with connections.
    listed = _tasks(lab, "connections")
    assert listed.returncode == 6 and _refused(listed)["code"] == "connections_required"

    refused = _tasks(lab, "add", "--type", "change", "--title", "y", switch="1")
    assert refused.returncode == 4, refused.stderr
    assert _refused(refused)["code"] == "read_only_switch"


def test_a_project_whose_every_connection_is_withheld_runs_on_its_store(labs):
    lab = labs({"default": "theirs", "connections": {
        "theirs": {"timezone": "Asia/Tokyo", "enabled": False, "allow_write": True}}})
    _ok(lab, "add", "--type", "change", "--title", "x")
    assert _ok(lab, "doctor")["connection"] is None
    named = _tasks(lab, "--connection", "theirs", "list")
    assert named.returncode == 4, named.stderr
    assert _refused(named)["code"] == "connection_not_granted"


def test_a_granted_connection_is_selected_and_gates_writes_as_before(labs):
    lab = labs({"default": "local", "connections": {
        "local": {"timezone": "Europe/Moscow", "allow_write": True},
        "reader": {"allow_write": False}}})
    assert _ok(lab, "doctor")["connection"] == "local"
    # Its timezone is read while no setting is set: midnight in Moscow.
    assert _pickup(lab, "connection zone") == "2026-10-31T21:00:00+00:00"
    refused = _tasks(lab, "--connection", "reader", "add", "--type", "change",
                     "--title", "no")
    assert refused.returncode == 4, refused.stderr
    assert _refused(refused)["code"] == "read_only"
    unknown = _tasks(lab, "--connection", "nope", "list")
    assert unknown.returncode == 6 and _refused(unknown)["code"] == "unknown_connection"


def test_the_zone_is_the_project_setting_then_the_machines_then_the_connections_then_the_systems(
        labs):
    lab = labs({"default": "local", "connections": {
        "local": {"timezone": "Europe/Moscow", "allow_write": True},
        "plain": {"allow_write": True}}})
    _ok(lab, "settings", "set", "timezone", "Asia/Tokyo", "--scope", "machine")
    assert _pickup(lab, "machine over connection") == "2026-10-31T15:00:00+00:00"
    _ok(lab, "settings", "set", "timezone", "UTC")
    assert _pickup(lab, "project over machine") == "2026-11-01T00:00:00+00:00"
    _ok(lab, "settings", "unset", "timezone")
    _ok(lab, "settings", "unset", "timezone", "--scope", "machine")
    assert _pickup(lab, "connection alone") == "2026-10-31T21:00:00+00:00"
    # No setting and a connection without one: the system's zone, here TZ's.
    task = _ok(lab, "--connection", "plain", "add", "--type", "change",
               "--title", "system", "--pickup", "2026-11-01")["created"]
    assert _ok(lab, "show", task)["task"]["pickup_at"] == "2026-11-01T04:00:00+00:00"


def test_a_timezone_setting_reads_bare_dates_without_a_connection(labs):
    lab = labs(None)
    _ok(lab, "settings", "set", "timezone", "Europe/Moscow")
    assert _pickup(lab, "moscow") == "2026-10-31T21:00:00+00:00"
    ready = _tasks(lab, "ready")
    assert ready.returncode == 0, ready.stderr


def test_an_unknown_zone_is_refused_wherever_it_was_read(labs):
    lab = labs(None)
    written = _tasks(lab, "settings", "set", "timezone", "Mars/Olympus")
    assert written.returncode == 6, written.stderr
    assert _refused(written)["code"] == "settings_invalid"
    assert "timezone" not in json.dumps(_ok(lab, "settings", "export", "--scope", "project"))

    settings = lab["project"] / "capabilities" / "tasks" / "service" / "config.toml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('version = 1\ntimezone = "Mars/Olympus"\n')
    read = _tasks(lab, "add", "--type", "change", "--title", "x", "--pickup", "2026-11-01")
    assert read.returncode == 2, read.stderr
    assert _refused(read)["code"] == "bad_timezone"

    held = labs({"default": "local", "connections": {
        "local": {"timezone": "Mars/Olympus", "allow_write": True}}})
    read = _tasks(held, "list", "--created-before", "2026-11-01")
    assert read.returncode == 2 and _refused(read)["code"] == "bad_timezone"
