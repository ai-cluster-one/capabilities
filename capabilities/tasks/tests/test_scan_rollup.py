#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.4.0"]
# ///
"""A scan row carries its task's runs, what they cost, and what blocks it, so a
consumer judges a row without a second call.

The store-backed half seeds tasks, raises and blockers into a schema of its own,
reads TASKS_TEST_DSN, skips when it is unset, and drops the schema.

    uv run --with pytest --with 'psycopg[binary]>=3.2' \\
        --with 'capabilities-contract==0.4.0' python -m pytest capabilities/tasks/tests -q
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


def test_the_help_states_the_fields():
    said = " ".join(mod.__doc__.split("\nCOLLECTIONS\n")[1].split())
    for field in ("`runs`", "{count, by_worker, cost_usd, without_cost}",
                  "`blocked_by`", "`blocked_by_status`", "never one query per row"):
        assert field in said


def test_every_scan_row_carries_the_rollup():
    for opts in ({}, {"full": True}):
        for column in ("as runs", "as blocked_by", "as blocked_by_status"):
            assert column in mod._scan_columns(opts)


# --- Store -------------------------------------------------------------------

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")


@pytest.fixture
def store(monkeypatch):
    import psycopg

    schema = "tasks_test_" + secrets.token_hex(4)
    entry = {"allow_write": False}
    _cli.bind_store(mod, monkeypatch, schema)
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        _cli.make_tables(mod, schema)
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _task(conn, schema, key, status="todo", project=HERE, metadata=None,
          blocked_by=()) -> str:
    return str(conn.execute(
        f"""insert into {schema}.tasks_tasks (project_id, type, unique_key, title, status,
                                        metadata, blocked_by)
            values (%s, 'change', %s, %s, %s, %s::jsonb, %s::uuid[]) returning id""",
        (project, key, f"title of {key}", status,
         json.dumps(metadata or {}), list(blocked_by))).fetchone()[0])


NOBODY = "00000000-0000-4000-8000-000000000000"


def _raise(conn, schema, task, attempt, worker, metrics=None) -> None:
    conn.execute(
        f"""insert into {schema}.tasks_executions (task_id, attempt, worker, status,
                                                  metrics)
            values (%s, %s, %s, 'ok', %s::jsonb)""",
        (task, attempt, worker, json.dumps(metrics or {})))


@pytest.fixture
def seeded(store):
    entry, schema, conn = store
    idle = _task(conn, schema, "idle")
    ran = _task(conn, schema, "ran")
    _raise(conn, schema, ran, 1, "dispatcher", {"cost_usd": 0.25, "turns": 3})
    _raise(conn, schema, ran, 2, "executor", {"cost_usd": 1.5})
    _raise(conn, schema, ran, 3, "executor", {"turns": 9})
    _raise(conn, schema, ran, 4, None, {"cost_usd": "unknown"})
    uncosted = _task(conn, schema, "uncosted")
    _raise(conn, schema, uncosted, 1, "executor")
    ended = _task(conn, schema, "ended", status="complete")
    elsewhere = _task(conn, schema, "elsewhere", project=THERE, status="draft")
    _task(conn, schema, "blocked", blocked_by=[ran, ended, elsewhere, NOBODY])
    return entry, {"idle": idle, "ran": ran, "ended": ended, "elsewhere": elsewhere}


def _scan(capsys, verb, entry, *args) -> dict:
    verb(entry, list(args))
    return {t["unique_key"]: t for t in json.loads(capsys.readouterr().out)["tasks"]}


@needs_store
@pytest.mark.parametrize("full", [False, True])
def test_a_scan_row_carries_its_runs_and_their_cost(seeded, capsys, full):
    entry, _ids = seeded
    flags = ["--full"] if full else []
    for verb, args in ((mod.cmd_list, flags), (mod.cmd_search, ["", *flags])):
        rows = _scan(capsys, verb, entry, *args)
        assert rows["idle"]["runs"] == {"count": 0, "by_worker": {},
                                        "cost_usd": None, "without_cost": 0}
        ran = rows["ran"]["runs"]
        assert ran["count"] == 4 and ran["without_cost"] == 2
        assert ran["by_worker"] == {"dispatcher": 1, "executor": 2, "": 1}
        assert ran["cost_usd"] == pytest.approx(1.75)
        assert rows["uncosted"]["runs"] == {"count": 1, "by_worker": {"executor": 1},
                                            "cost_usd": None, "without_cost": 1}


@needs_store
@pytest.mark.parametrize("full", [False, True])
def test_a_scan_row_carries_its_blockers_and_their_status(seeded, capsys, full):
    entry, ids = seeded
    flags = ["--full"] if full else []
    for verb, args in ((mod.cmd_list, flags), (mod.cmd_search, ["blocked", *flags])):
        rows = _scan(capsys, verb, entry, *args)
        blocked = rows["blocked"]
        listed = [ids["ran"], ids["ended"], ids["elsewhere"], NOBODY]
        assert blocked["blocked_by"] == listed
        # Open and ended are told apart, another project's task is read by its
        # id, and an id that names no task answers null.
        assert blocked["blocked_by_status"] == {ids["ran"]: "todo",
                                                ids["ended"]: "complete",
                                                ids["elsewhere"]: "draft", NOBODY: None}
        if full:
            # The metadata mirrors the field for one release.
            assert blocked["metadata"] == {"blocked_by": listed}
    rows = _scan(capsys, mod.cmd_list, entry, *flags)
    assert rows["idle"]["blocked_by"] == []
    assert rows["idle"]["blocked_by_status"] == {}
    if full:
        assert rows["idle"]["metadata"] == {}
