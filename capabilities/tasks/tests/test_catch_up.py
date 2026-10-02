#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""A store behind this version by additive changes only is brought up to it by
the first command that reaches it, once, under the schema's lock; anything that
is not additive is left to `migrate --apply`, and a connection that may not
create is served as before.

These run the CLI as a project would, against TASKS_TEST_DSN in a schema of
their own that they drop, and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")

# The check a store created before `waiting` existed carries.
OLD_CHECK = "check (status in ('draft','todo','in_progress','complete','closed'))"


# --- Without a store ---------------------------------------------------------

def test_only_what_fills_replaces_or_drops_is_not_additive():
    assert mod._NOT_ADDITIVE == {"tasks.project_id", "tasks.tasks_status_check",
                                 "tasks.tasks_project_unique_key_idx"}
    additive = ([f"{t}.{c}" for t, c in mod._COLUMNS if c != "project_id"]
                + [f"{t}.{g}" for t, g in (mod._NOTIFY_TRIGGER, *mod._WATCH_TRIGGERS)])
    assert not mod._NOT_ADDITIVE.intersection(additive)


def test_the_help_states_the_backup_rule_and_who_applies_what():
    store = " ".join(mod.__doc__.split("\nSTORE\n")[1].split("\nI/O\n")[0].split())
    assert "first command that reaches it" in store and "every poll" in store
    assert "No backup is taken" in store and "dropping what it added" in store
    assert "Only `migrate --apply` applies it" in store and "pg_dump" in store
    assert '{"migrated": {"schema": S' in store
    assert "read-only switch" in store and "allow_write" in store


# --- Against a store ---------------------------------------------------------

@pytest.fixture()
def lab(tmp_path):
    """A project over a schema of its own that nothing has created yet, with a
    connection that may write, one that does not allow writes, and one for a
    role the store lets read but not create."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    schema = "tasks_test_" + secrets.token_hex(4)
    reader = "tasks_reader_" + secrets.token_hex(4)
    reader_password = secrets.token_hex(16)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    (envelope / "tasks").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    here = "prj_" + uuid.uuid4().hex[:12]
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": here, "slug": "lab"}))
    store = {"db_host": info.get("host"), "db_port": str(info.get("port") or 5432),
             "db_user": info.get("user"), "db_name": info.get("dbname"),
             "db_sslmode": info.get("sslmode") or "prefer", "db_schema": schema,
             "secret_env": "TASKS_TEST_PASSWORD"}
    (envelope / "tasks" / "connections.json").write_text(json.dumps({
        "default": "local",
        "connections": {
            "local": {**store, "allow_write": True},
            "locked": {**store, "allow_write": False},
            "reader": {**store, "db_user": reader,
                       "secret_env": "TASKS_READER_PASSWORD", "allow_write": True}}}))
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        # Not empty under trust, where it is not asked for, because `doctor`
        # reads an empty credential as one that is missing.
        "TASKS_TEST_PASSWORD": info.get("password") or "unused-under-trust",
        "TASKS_READER_PASSWORD": reader_password,
    })
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_STORE_URL", "CAPABILITIES_STORE_MODE"):
        env.pop(leaked, None)
    lab = {"project": project, "env": env, "schema": schema, "here": here,
           "reader": reader, "reader_password": reader_password}
    try:
        yield lab
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")
            conn.execute(f"drop role if exists {reader}")


def _tasks(lab, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=lab["project"],
                          env=env or lab["env"], text=True, capture_output=True,
                          timeout=180)


def _answer(proc: subprocess.CompletedProcess):
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def _migrated(proc: subprocess.CompletedProcess) -> list[dict]:
    """The catch-up lines a command wrote to stderr."""
    return [json.loads(line)["migrated"] for line in proc.stderr.splitlines()
            if line.startswith('{"migrated"')]


def _sql(statement: str, params=()) -> list[tuple]:
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as conn:
        cur = conn.execute(statement, params)
        return cur.fetchall() if cur.description else []


def _has_trigger(lab, table: str, trigger: str) -> bool:
    return bool(_sql("""select 1 from pg_trigger g join pg_class t on t.oid = g.tgrelid
                         join pg_namespace n on n.oid = t.relnamespace
                        where n.nspname = %s and t.relname = %s and g.tgname = %s""",
                     (lab["schema"], table, trigger)))


def _current(lab) -> None:
    proc = _tasks(lab, "migrate", "--apply")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@needs_store
def test_a_fresh_store_is_created_by_the_first_command_and_reported_once(lab):
    first = _tasks(lab, "list")
    assert _answer(first)["tasks"] == []
    assert _migrated(first) == [{"schema": lab["schema"],
                                 "created": list(mod._TABLES), "added": []}]
    second = _tasks(lab, "list")
    assert second.returncode == 0 and _migrated(second) == [] and second.stderr == ""
    assert _answer(_tasks(lab, "migrate"))["would_create"] == []


@needs_store
def test_a_store_behind_by_one_trigger_is_brought_up_to_date_before_the_answer(lab):
    _current(lab)
    _sql(f"drop trigger tasks_notify_claimable on {lab['schema']}.tasks")
    added = _tasks(lab, "add", "--type", "probe", "--title", "a probe")
    assert added.returncode == 0, added.stderr
    assert _migrated(added) == [{"schema": lab["schema"], "created": [],
                                 "added": ["tasks.tasks_notify_claimable"]}]
    assert _has_trigger(lab, "tasks", "tasks_notify_claimable")
    assert _answer(_tasks(lab, "migrate"))["would_add"] == []


@needs_store
def test_two_clients_racing_apply_it_exactly_once(lab):
    """Both find the store behind and wait on the schema's lock, held here; let
    go, the first applies it and the second finds it current."""
    import psycopg
    _current(lab)
    _sql(f"drop trigger task_activities_notify_watch on {lab['schema']}.task_activities")
    key = f"tasks.schema:{lab['schema']}"
    with psycopg.connect(DSN, autocommit=True) as holder:
        holder.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (key,))
        racers = [subprocess.Popen([str(_cli.CLI_PATH), "counts"], cwd=lab["project"],
                                   env=lab["env"], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                  for _ in range(2)]
        deadline = time.monotonic() + 8
        waiting = 0
        while time.monotonic() < deadline:
            waiting = holder.execute("""select count(*) from pg_locks
                                         where locktype = 'advisory' and not granted""").fetchone()[0]
            if waiting == 2:
                break
            time.sleep(0.05)
        holder.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
    assert waiting == 2, "both clients should have been waiting on the schema's lock"
    finished = []
    for racer in racers:
        out, err = racer.communicate(timeout=120)
        finished.append(subprocess.CompletedProcess(racer.args, racer.returncode, out, err))
    assert [proc.returncode for proc in finished] == [0, 0], [p.stderr for p in finished]
    reports = [report for proc in finished for report in _migrated(proc)]
    assert reports == [{"schema": lab["schema"], "created": [],
                        "added": ["task_activities.task_activities_notify_watch"]}]
    assert _has_trigger(lab, "task_activities", "task_activities_notify_watch")


@needs_store
def test_a_difference_that_is_not_additive_is_left_to_migrate_apply(lab):
    _current(lab)
    schema = lab["schema"]
    _sql(f"drop trigger tasks_notify_claimable on {schema}.tasks")
    _sql(f"alter table {schema}.tasks drop constraint tasks_status_check")
    _sql(f"alter table {schema}.tasks add constraint tasks_status_check {OLD_CHECK}")
    listed = _tasks(lab, "list")
    assert listed.returncode == 0 and _migrated(listed) == [], listed.stderr
    assert not _has_trigger(lab, "tasks", "tasks_notify_claimable")
    [(definition,)] = _sql("""select pg_get_constraintdef(c.oid) from pg_constraint c
                               join pg_namespace n on n.oid = c.connamespace
                              where n.nspname = %s and c.conname = 'tasks_status_check'""",
                           (schema,))
    assert "'waiting'" not in definition
    doctor = _answer(_tasks(lab, "doctor"))
    assert set(doctor["behind"]) == {"tasks.tasks_status_check",
                                     "tasks.tasks_notify_claimable"}
    assert "nothing else does" in doctor["warning"] and "backup" in doctor["warning"]
    applied = _answer(_tasks(lab, "migrate", "--apply"))
    assert set(applied["added"]) == {"tasks.tasks_status_check",
                                     "tasks.tasks_notify_claimable"}
    assert _has_trigger(lab, "tasks", "tasks_notify_claimable")


@needs_store
def test_a_connection_that_may_not_create_reads_as_before_and_reports_behind(lab):
    import psycopg
    _current(lab)
    schema, reader = lab["schema"], lab["reader"]
    try:
        _sql(f"create role {reader} login password '{lab['reader_password']}'")
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("the test store's role may not create a role")
    _sql(f"grant usage on schema {schema} to {reader}")
    _sql(f"grant select on all tables in schema {schema} to {reader}")
    _answer(_tasks(lab, "add", "--type", "probe", "--title", "a probe", "--key", "p-1"))
    _sql(f"drop trigger tasks_notify_claimable on {schema}.tasks")

    # A role the store lets read but not create.
    read = _tasks(lab, "list", "--connection", "reader")
    assert [row["unique_key"] for row in _answer(read)["tasks"]] == ["p-1"]
    assert _migrated(read) == [] and read.stderr == ""
    assert not _has_trigger(lab, "tasks", "tasks_notify_claimable")
    doctor = _answer(_tasks(lab, "doctor", "--connection", "reader"))
    assert doctor["behind"] == ["tasks.tasks_notify_claimable"]

    # A connection without allow_write, and the read-only switch, over a role
    # that could create.
    switched = {**lab["env"], "CAPABILITIES_READ_ONLY": "1"}
    for proc in (_tasks(lab, "list", "--connection", "locked"),
                 _tasks(lab, "list", env=switched)):
        assert [row["unique_key"] for row in _answer(proc)["tasks"]] == ["p-1"]
        assert _migrated(proc) == [] and proc.stderr == ""
    assert not _has_trigger(lab, "tasks", "tasks_notify_claimable")
