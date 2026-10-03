#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""From a run a harness knows by its own handle to the raise it was and the task
it ran for, for many runs in one call.

The store-backed half seeds raises naming runs into two projects in one schema
and reads TASKS_TEST_DSN, skipping when it is unset; every run works in a schema
of its own and drops it.

    uv run --with pytest --with 'psycopg[binary]>=3.2' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()

HERE, THERE = "prj_here", "prj_there"
ROW = {"execution", "task_id", "project_id", "unique_key", "title", "worker",
       "status", "started_at", "ended_at", "run_system", "run_ref"}


def _refused(capsys, call, *args) -> dict:
    with pytest.raises(SystemExit) as exit_info:
        call(*args)
    error = json.loads(capsys.readouterr().err)["error"]
    error["exit"] = exit_info.value.code
    return error


# --- Without a store ---------------------------------------------------------

@pytest.mark.parametrize("args", [
    ["some-task", "--run", "claude:abc"],
    ["some-task", "--since", "2026-10-01"],
    ["--run", "claude"],
    ["--run", ":abc"],
    ["some-task", "--all-projects"],
    [],
])
def test_a_call_that_cannot_be_read_is_refused_before_the_store(args, capsys):
    error = _refused(capsys, mod.cmd_runs, {}, args)
    assert error["exit"] == 6 and error["code"] == "input"


def test_the_run_index_is_additive_and_a_store_without_it_is_behind():
    named = [f"{t}.{i}" for t, i in mod._INDEXES]
    assert named == ["task_executions.task_executions_run_idx"]
    assert not mod._NOT_ADDITIVE.intersection(named)
    catalog = {"tables": ["task_executions"], "indexes": set()}
    assert mod._indexes_absent(catalog) == named
    assert mod._indexes_absent({**catalog, "indexes": set(mod._INDEXES)}) == []
    assert mod._indexes_absent({"tables": [], "indexes": set()}) == []


def test_the_help_states_the_contract():
    runs = " ".join(mod.__doc__.split("\nRUNS\n")[1].split("\nWATCH\n")[0].split())
    for said in ("--run SYSTEM:REF", "--since WHEN", "--all-projects",
                 "--run claude:<session id>", "answers no row and is not an error",
                 "one query on one connection"):
        assert said in runs
    for column in ROW:
        assert column in runs


# --- Store: raises in two projects -------------------------------------------

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")


@pytest.fixture
def store(monkeypatch):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    schema = "tasks_test_" + secrets.token_hex(4)
    entry = {"db_host": info.get("host"), "db_port": str(info.get("port") or 5432),
             "db_user": info.get("user"), "db_name": info.get("dbname"),
             "db_sslmode": info.get("sslmode") or "prefer", "db_schema": schema,
             "secret_env": "TASKS_TEST_PASSWORD", "allow_write": False}
    monkeypatch.setenv("TASKS_TEST_PASSWORD", info.get("password") or "")
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(mod._schema_ddl(schema))
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _task(conn, schema, project, key) -> str:
    return str(conn.execute(
        f"""insert into {schema}.tasks (project_id, type, unique_key, title, status)
            values (%s, 'change', %s, %s, 'todo') returning id""",
        (project, key, f"title of {key}")).fetchone()[0])


def _raise(conn, schema, task, attempt, started, system=None, ref=None,
           status="ok", worker="executor") -> None:
    conn.execute(
        f"""insert into {schema}.task_executions
              (task_id, attempt, worker, status, run_system, run_ref, started_at,
               ended_at)
            values (%s, %s, %s, %s, %s, %s, %s::timestamptz,
                    %s::timestamptz + interval '5 minutes')""",
        (task, attempt, worker, status, system, ref, started, started))


@pytest.fixture
def raises(store):
    entry, schema, conn = store
    here = _task(conn, schema, HERE, "h-1")
    there = _task(conn, schema, THERE, "t-1")
    _raise(conn, schema, here, 1, "2026-10-01T10:00:00Z", "claude", "sess-a")
    _raise(conn, schema, here, 2, "2026-10-02T10:00:00Z", "claude", "sess-b",
           worker="dispatcher")
    _raise(conn, schema, there, 1, "2026-10-02T11:00:00Z", "claude", "sess-c")
    _raise(conn, schema, there, 2, "2026-10-03T09:00:00Z", "codex", "sess-a")
    _raise(conn, schema, there, 3, "2026-10-03T10:00:00Z")
    return entry, schema, conn, here, there


def _runs(capsys, entry, *args) -> list[dict]:
    mod.cmd_runs(entry, list(args))
    return json.loads(capsys.readouterr().out)["runs"]


@needs_store
def test_several_runs_across_projects_in_one_call(raises, capsys):
    entry, _schema, _conn, here, there = raises
    rows = _runs(capsys, entry, "--all-projects", "--run", "claude:sess-a",
                 "--run", "claude:sess-c", "--run", "codex:sess-a")
    assert [(r["run_system"], r["run_ref"], r["project_id"]) for r in rows] == [
        ("claude", "sess-a", HERE), ("claude", "sess-c", THERE),
        ("codex", "sess-a", THERE)]
    assert all(set(r) == ROW for r in rows)
    first = rows[0]
    assert first["task_id"] == here and first["unique_key"] == "h-1"
    assert first["title"] == "title of h-1" and first["worker"] == "executor"
    assert first["status"] == "ok" and first["started_at"] and first["ended_at"]
    assert rows[1]["task_id"] == there


@needs_store
def test_an_unknown_run_answers_no_row_and_no_error(raises, capsys):
    entry, *_ = raises
    assert _runs(capsys, entry, "--all-projects", "--run", "claude:nobody") == []
    rows = _runs(capsys, entry, "--all-projects", "--run", "claude:nobody",
                 "--run", "claude:sess-b")
    assert [r["run_ref"] for r in rows] == ["sess-b"]
    # The system is part of the handle: the same id under another system is not it.
    assert _runs(capsys, entry, "--all-projects", "--run", "codex:sess-b") == []


@needs_store
def test_since_lists_every_raise_naming_a_run_from_that_moment(raises, capsys):
    entry, *_ = raises
    rows = _runs(capsys, entry, "--all-projects", "--since", "2026-10-02T10:30:00Z")
    # The raise that names no run is not one a caller can look up, so it is out.
    assert [(r["run_system"], r["run_ref"]) for r in rows] == [
        ("claude", "sess-c"), ("codex", "sess-a")]
    rows = _runs(capsys, entry, "--all-projects", "--since", "2026-10-02")
    assert [r["run_ref"] for r in rows] == ["sess-b", "sess-c", "sess-a"]
    # Given together, both narrow.
    rows = _runs(capsys, entry, "--all-projects", "--since", "2026-10-02",
                 "--run", "claude:sess-a", "--run", "claude:sess-b")
    assert [r["run_ref"] for r in rows] == ["sess-b"]


@needs_store
def test_the_scope_is_the_one_list_reads(raises, capsys, monkeypatch):
    entry, *_ = raises
    every = ("--run", "claude:sess-a", "--run", "claude:sess-c")
    assert [r["project_id"] for r in _runs(capsys, entry, *every)] == [HERE]
    assert [r["project_id"] for r in _runs(capsys, entry, "--project", THERE,
                                           *every)] == [THERE]
    assert [r["project_id"] for r in _runs(capsys, entry, "--all-projects",
                                           *every)] == [HERE, THERE]
    error = _refused(capsys, mod.cmd_runs, entry,
                     ["--all-projects", "--project", THERE, *every])
    assert error["exit"] == 6
    # Outside a project the whole store is still a read; one project has to be named.
    monkeypatch.setattr(mod, "PROJECT", None)
    assert len(_runs(capsys, entry, "--all-projects", *every)) == 2
    assert _refused(capsys, mod.cmd_runs, entry, list(every))["exit"] == 6


class _Counted:
    """A connection or cursor that counts what is asked of it and passes the rest
    through."""

    def __init__(self, inner, statements: list) -> None:
        self._inner, self._statements = inner, statements

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def cursor(self, *a, **k):
        return _Counted(self._inner.cursor(*a, **k), self._statements)

    def execute(self, sql, *a, **k):
        self._statements.append(sql)
        return self._inner.execute(sql, *a, **k)


@needs_store
def test_one_query_however_many_runs_are_named(raises, capsys, monkeypatch):
    entry, *_ = raises
    connects, statements = [], []
    real_connect = mod._connect

    def counting(*a, **k):
        connects.append(1)
        return _Counted(real_connect(*a, **k), statements)

    monkeypatch.setattr(mod, "_connect", counting)
    many = [arg for i in range(40) for arg in ("--run", f"claude:sess-{i}")]
    rows = _runs(capsys, entry, "--all-projects", "--run", "claude:sess-a", *many)
    assert [r["run_ref"] for r in rows] == ["sess-a"]
    assert len(connects) == 1 and len(statements) == 1


@needs_store
def test_a_read_only_connection_answers_it(raises, capsys):
    entry, *_ = raises
    assert entry["allow_write"] is False
    assert len(_runs(capsys, entry, "--all-projects", "--run", "claude:sess-a")) == 1


@needs_store
def test_a_store_without_the_run_index_catches_up_to_it(store):
    import psycopg
    _entry, schema, conn = store
    conn.execute(f"drop index {schema}.task_executions_run_idx")
    with psycopg.connect(DSN, row_factory=psycopg.rows.dict_row) as fresh:
        applied = mod._catch_up(fresh, schema, HERE)
    assert applied == {"schema": schema, "created": [],
                       "added": ["task_executions.task_executions_run_idx"]}
    assert conn.execute("select 1 from pg_indexes where schemaname = %s and "
                        "indexname = 'task_executions_run_idx'",
                        (schema,)).fetchone()
