#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""Who a write is attributed to, and where that lands.

The resolver is pure and is checked without a store. The store-backed checks
read TASKS_TEST_DSN, a libpq URL for a database the run may create a schema in,
and skip when it is unset; every run works in a schema of its own and drops it.

    uv run --with pytest --with 'psycopg[binary]>=3.2' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import pwd
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()
OS_USER = pwd.getpwuid(os.getuid()).pw_name


# --- Identity: the resolver alone -------------------------------------------

def test_execution_wins_over_everything():
    env = {"TASKS_EXECUTION": "exec-1", "TASKS_ACTOR": "shell"}
    assert mod._actor("person:flag", env) == "execution:exec-1"


def test_flag_wins_over_shell():
    assert mod._actor("person:flag", {"TASKS_ACTOR": "shell"}) == "person:flag"


def test_shell_when_nothing_named():
    assert mod._actor(None, {"TASKS_ACTOR": "shell"}) == "shell"


def test_os_user_when_nothing_set():
    assert mod._actor(None, {}) == OS_USER


def test_empty_values_are_unset():
    assert mod._actor("  ", {"TASKS_EXECUTION": "", "TASKS_ACTOR": " "}) == OS_USER


def test_env_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("TASKS_EXECUTION", "exec-2")
    assert mod._actor("person:flag") == "execution:exec-2"
    monkeypatch.delenv("TASKS_EXECUTION")
    monkeypatch.setenv("TASKS_ACTOR", "shell")
    assert mod._actor() == "shell"


def test_tracked_fields_are_exactly_these():
    assert [field for field, _column in mod._TRACKED] == ["status", "pickup", "type", "assignee"]


def test_help_names_the_rule():
    for needle in ("--actor", "TASKS_EXECUTION", "TASKS_ACTOR", "created_by"):
        assert needle in mod.__doc__


def test_every_named_write_verb_accepts_the_flag():
    # The flag parser refuses an unknown flag with exit 6, so parsing it under
    # each verb's own declaration is the check that the verb takes it.
    assert mod._flags(["t", "--actor", "x"], ("actor",), takes=1)["actor"] == "x"
    with pytest.raises(SystemExit):
        mod._flags(["t", "--actor", "x"], (), takes=1)


# --- Store: where the identity lands ----------------------------------------

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
             "secret_env": "TASKS_TEST_PASSWORD", "allow_write": True}
    monkeypatch.setenv("TASKS_TEST_PASSWORD", info.get("password") or "")
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.delenv("TASKS_ACTOR", raising=False)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(mod._schema_ddl(schema))
        conn.execute(mod._schema_ddl(schema))  # additive, so twice is once
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _answer(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


@needs_store
def test_migrate_adds_the_columns_a_store_lacks(store):
    from psycopg.rows import dict_row
    entry, schema, conn = store
    with conn.cursor(row_factory=dict_row) as cur:
        tables = mod._tables_present(cur, schema)
        assert tables == sorted(mod._TABLES)
        assert mod._columns_absent(cur, schema, tables) == []
        conn.execute(f"alter table {schema}.tasks drop column created_by")
        conn.execute(f"alter table {schema}.task_activities drop column actor")
        assert mod._columns_absent(cur, schema, tables) == ["tasks.created_by",
                                                           "task_activities.actor"]
        conn.execute(mod._schema_ddl(schema))
        assert mod._columns_absent(cur, schema, tables) == []


@needs_store
def test_writes_carry_their_actor(store, capsys, monkeypatch):
    entry, schema, conn = store
    monkeypatch.setenv("TASKS_EXECUTION", "exec-9")
    mod.cmd_add(entry, ["--type", "probe", "--title", "A probe", "--key", "k1"])
    created = _answer(capsys)["created"]
    monkeypatch.delenv("TASKS_EXECUTION")

    mod.cmd_set(entry, ["k1", "--assignee", "someone", "--type", "other",
                        "--title", "renamed", "--actor", "person:test"])
    moved = _answer(capsys)
    assert moved["moved"] == ["type", "assignee"]
    assert set(moved["fields"]) == {"assignee", "type", "title"}

    mod.cmd_set(entry, ["k1", "--assignee", "someone"])  # unchanged: no row
    assert _answer(capsys)["moved"] == []

    mod.cmd_activity(entry, ["k1", "written by the os user"])
    capsys.readouterr()

    mod.cmd_show(entry, ["k1"])
    shown = _answer(capsys)
    assert shown["task"]["id"] == created
    assert shown["task"]["created_by"] == "execution:exec-9"
    assert {"id", "type", "unique_key", "title", "status", "assignee", "tags",
            "metadata", "pickup_at", "created_at", "updated_at"} <= set(shown["task"])
    [entry_row] = shown["activities"]
    assert entry_row["actor"] == OS_USER
    assert {"id", "description", "created_at", "actor"} <= set(entry_row)

    mod.cmd_history(entry, ["k1"])
    changes = _answer(capsys)["changes"]
    assert {(c["field"], c["old_value"], c["new_value"], c["actor"]) for c in changes} == {
        ("type", "probe", "other", "person:test"),
        ("assignee", None, "someone", "person:test"),
    }


@needs_store
def test_tags_and_metadata_take_the_flag_and_keep_no_trail(store, capsys):
    entry, schema, conn = store
    mod.cmd_add(entry, ["--type", "probe", "--title", "A probe", "--key", "k2"])
    capsys.readouterr()
    mod.cmd_tag(entry, ["k2", "one", "--actor", "person:test"])
    assert _answer(capsys)["tags"] == ["one"]
    mod.cmd_meta(entry, ["set", "k2", "n", "1", "--actor", "person:test"])
    assert _answer(capsys)["metadata"] == {"n": 1}
    mod.cmd_history(entry, ["k2"])
    assert _answer(capsys)["changes"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
