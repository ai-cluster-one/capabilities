#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.3.0"]
# ///
"""The store is the machine's: tasks reaches it through the store setting and
keeps its tables in the setting's schema under its own ledger.

A store the previous release kept in a schema of its own, `tasks`, is moved by
the first step, every row as it was, into the bound schema and renamed there,
ending in the same tables a fresh store is created with; a store not at the
previous release's shape, or one that already holds the new tables beside the
old, is refused and left as found. A connection that still names a store is
refused, and so is every call with no store configured.

The store-backed checks read TASKS_TEST_DSN and skip when it is unset. The
schema the previous release used is one name, `tasks`, so the cases that build
it drop it before and after; each case binds a schema of its own.

    uv run --with pytest --with 'psycopg[binary]>=3.2' \\
        --with 'capabilities-contract==0.3.0' python -m pytest capabilities/tasks/tests -q
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

mod = _cli.load()
DSN = _cli.DSN
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")

# The schema file the previous release shipped and applied to schema `tasks`.
PREVIOUS = (Path(__file__).resolve().parent / "fakes"
            / "schema_of_the_previous_release.sql").read_text()
TABLES = {"tasks": "tasks_tasks", "task_activities": "tasks_activities",
          "task_changes": "tasks_changes", "task_executions": "tasks_executions"}


def _db():
    from capabilities_contract import db
    return db


def migrate(schema: str):
    """`tasks migrate` against `schema`, as the library runs it."""
    db = _db()
    steps, major, minor = mod._tables()
    conn = db.connect(application_name="tasks-test", setting=_cli.store_setting(schema))
    try:
        return db.migrate(conn, mod.NAME, steps, major=major, minor=minor)
    finally:
        conn.close()


def raw():
    import psycopg
    return psycopg.connect(DSN, autocommit=True)


@pytest.fixture
def old_store():
    """Schema `tasks` as the previous release left it, holding two projects'
    tasks with their trails, their moves and their raises - one still running -
    and a bound schema of the case's own to move it into."""
    bound = "tasks_test_move_" + secrets.token_hex(4)
    with raw() as conn:
        conn.execute("drop schema if exists tasks cascade")
        conn.execute(PREVIOUS)
        conn.execute("""
            insert into tasks.tasks (id, project_id, type, unique_key, title, status,
                                     assignee, tags, metadata, pickup_at, created_by,
                                     created_at, updated_at)
            values ('11111111-1111-4111-8111-111111111111', 'prj_one', 'change', 'k-1',
                    'gate', 'complete', null, '{a,b}', '{"stage": "done"}', null,
                    'person:a', now() - interval '3 days', now() - interval '2 days'),
                   ('22222222-2222-4222-8222-222222222222', 'prj_one', 'change', 'k-2',
                    'held', 'todo', 'alpha', '{}', '{}', now() - interval '1 hour',
                    'person:a', now() - interval '2 days', now() - interval '1 day'),
                   ('33333333-3333-4333-8333-333333333333', 'prj_two', 'defect', 'k-1',
                    'waits', 'waiting', 'the owner', '{x}', '{"n": 1}',
                    now() + interval '1 day', 'person:b', now() - interval '1 day',
                    now() - interval '1 hour')""")
        conn.execute("""update tasks.tasks set blocked_by = '{11111111-1111-4111-8111-111111111111}'
                         where unique_key = 'k-2' and project_id = 'prj_one'""")
        conn.execute("""
            insert into tasks.task_executions (id, task_id, attempt, worker, handler, status,
                                               lease_until, run_system, run_ref, detail,
                                               metrics, started_at, ended_at)
            values ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                    '11111111-1111-4111-8111-111111111111', 1, 'alpha', 'claude', 'ok',
                    null, 'claude', 'sess-1', 'done', '{"cost_usd": 0.5}',
                    now() - interval '2 days', now() - interval '2 days'),
                   ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
                    '22222222-2222-4222-8222-222222222222', 1, 'alpha', 'claude', 'running',
                    now() + interval '10 minutes', 'claude', 'sess-2', null, '{}',
                    now() - interval '5 minutes', null)""")
        conn.execute("""
            insert into tasks.task_activities (task_id, description, actor, origin_project)
            values ('11111111-1111-4111-8111-111111111111', 'shipped it', 'alpha', 'prj_one'),
                   ('33333333-3333-4333-8333-333333333333', 'asked', 'person:b', null)""")
        conn.execute("""
            insert into tasks.task_changes (task_id, field, old_value, new_value,
                                            execution_id, actor, origin_project)
            values ('11111111-1111-4111-8111-111111111111', 'status', 'todo', 'complete',
                    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'alpha', 'prj_one'),
                   ('33333333-3333-4333-8333-333333333333', 'status', 'draft', 'waiting',
                    null, 'person:b', 'prj_two')""")
    try:
        yield bound
    finally:
        with raw() as conn:
            conn.execute("drop schema if exists tasks cascade")
            conn.execute(f"drop schema if exists {bound} cascade")


@pytest.fixture
def fresh_schema():
    schema = "tasks_test_fresh_" + secrets.token_hex(4)
    try:
        yield schema
    finally:
        with raw() as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def rows(conn, schema: str, table: str) -> list[tuple]:
    return conn.execute(f"select * from {schema}.{table} order by id").fetchall()


def tables_in(conn, schema: str) -> list[str]:
    return [row[0] for row in conn.execute(
        """select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s and c.relkind = 'r' order by 1""", (schema,)).fetchall()]


def catalog(conn, schema: str) -> dict:
    """Everything a store's tasks tables are made of, with the schema's name taken
    out, so two schemas holding the same shape read the same."""
    def plain(text):
        return None if text is None else text.replace(f"{schema}.", "S.")

    found: dict = {}
    found["columns"] = sorted(conn.execute(
        """select c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                  pg_get_expr(d.adbin, d.adrelid), col_description(c.oid, a.attnum)
             from pg_attribute a join pg_class c on c.oid = a.attrelid
             join pg_namespace n on n.oid = c.relnamespace
             left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
            where n.nspname = %s and c.relkind = 'r' and a.attnum > 0
              and not a.attisdropped and c.relname like 'tasks\\_%%'""",
        (schema,)).fetchall())
    found["tables"] = sorted(conn.execute(
        """select c.relname, obj_description(c.oid, 'pg_class') from pg_class c
             join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s and c.relname like 'tasks\\_%%'""", (schema,)).fetchall())
    found["indexes"] = sorted((name, plain(definition)) for name, definition in conn.execute(
        "select indexname, indexdef from pg_indexes where schemaname = %s", (schema,)
    ).fetchall() if name.startswith("tasks_"))
    found["constraints"] = sorted((table, name, kind, plain(definition))
                                  for table, name, kind, definition in conn.execute(
        """select t.relname, c.conname, c.contype::text, pg_get_constraintdef(c.oid)
             from pg_constraint c join pg_class t on t.oid = c.conrelid
             join pg_namespace n on n.oid = t.relnamespace
            where n.nspname = %s and t.relname like 'tasks\\_%%'""", (schema,)).fetchall())
    found["triggers"] = sorted((table, name, plain(definition))
                               for table, name, definition in conn.execute(
        """select t.relname, g.tgname, pg_get_triggerdef(g.oid) from pg_trigger g
             join pg_class t on t.oid = g.tgrelid
             join pg_namespace n on n.oid = t.relnamespace
            where n.nspname = %s and not g.tgisinternal""", (schema,)).fetchall())
    found["functions"] = sorted((name, plain(definition))
                                for name, definition in conn.execute(
        """select p.proname, pg_get_functiondef(p.oid) from pg_proc p
             join pg_namespace n on n.oid = p.pronamespace where n.nspname = %s""",
        (schema,)).fetchall())
    return found


# --- The steps -----------------------------------------------------------------

def test_the_tables_are_declared_for_the_contracts_migrate():
    assert mod.TABLES is True and mod._tables_declared()
    steps, major, minor = mod._tables()
    assert [step_id for step_id, _sql in steps] == ["0001-move-from-schema-tasks",
                                                    "0002-tables"]
    assert (major, minor) == (mod.STORE_MAJOR, mod.STORE_MINOR)
    names = [key["key"] for key in mod.CRED_KEYS]
    assert names == ["timezone"]
    environment = mod.SERVICE["deploy"]["environment"]
    assert "TASKS_DB_PASSWORD" not in json.dumps(environment)


@needs_store
def test_a_fresh_store_gets_every_table_under_the_tasks_prefix(fresh_schema):
    done = migrate(fresh_schema)
    assert done.applied == ["0001-move-from-schema-tasks", "0002-tables"]
    with raw() as conn:
        assert tables_in(conn, fresh_schema) == [
            "schema_ledger", "schema_version", "tasks_activities", "tasks_changes",
            "tasks_executions", "tasks_tasks"]
        assert conn.execute("select to_regnamespace('tasks')").fetchone()[0] is None
    assert migrate(fresh_schema).applied == []


# --- The move --------------------------------------------------------------------

@needs_store
def test_the_previous_store_is_moved_whole_into_the_bound_schema(old_store):
    with raw() as conn:
        before = {table: rows(conn, "tasks", table) for table in TABLES}
    assert len(before["tasks"]) == 3 and len(before["task_executions"]) == 2

    done = migrate(old_store)
    assert done.applied == ["0001-move-from-schema-tasks", "0002-tables"]

    with raw() as conn:
        for old, new in TABLES.items():
            assert rows(conn, old_store, new) == before[old], new
        # The running raise holds its task as it did, its lease untouched.
        [held] = conn.execute(f"""select status, lease_until > now() from
                                  {old_store}.tasks_executions where ended_at is null""").fetchall()
        assert held == ("running", True)
        # Schema `tasks` holds no table any more; emptied, it is gone.
        assert conn.execute("""select count(*) from pg_class c join pg_namespace n
                                 on n.oid = c.relnamespace
                                where n.nspname = 'tasks'""").fetchone()[0] == 0
        assert conn.execute("select to_regnamespace('tasks')").fetchone()[0] is None
        # Rows move under every trigger as they did: the touch and both notices.
        conn.execute(f"""update {old_store}.tasks_tasks set title = 'renamed'
                          where id = '22222222-2222-4222-8222-222222222222'""")
        assert conn.execute(f"""select updated_at > now() - interval '1 minute'
                                  from {old_store}.tasks_tasks
                                 where id = '22222222-2222-4222-8222-222222222222'"""
                            ).fetchone()[0] is True
    # Moved once: a second migrate applies nothing.
    assert migrate(old_store).applied == []


@needs_store
def test_a_moved_store_and_a_fresh_one_are_the_same_store(old_store, fresh_schema):
    migrate(old_store)
    migrate(fresh_schema)
    with raw() as conn:
        moved, fresh = catalog(conn, old_store), catalog(conn, fresh_schema)
    for part in ("tables", "columns", "indexes", "constraints", "triggers", "functions"):
        assert moved[part] == fresh[part], part
    assert {name for name, _def in moved["functions"]} == {
        "tasks_touch_updated_at", "tasks_notify_claimable", "tasks_notify_watch"}
    assert all(name.startswith("tasks_") for _table, name, _kind, _def in moved["constraints"])
    assert all(name.startswith("tasks_") for _table, name, _def in moved["triggers"])


@needs_store
def test_the_watch_and_claimable_notices_name_the_bound_schema(old_store):
    import psycopg
    migrate(old_store)
    with psycopg.connect(DSN, autocommit=True) as listener, raw() as conn:
        listener.execute("listen tasks_watch")
        listener.execute("listen tasks_claimable")
        conn.execute(f"""update {old_store}.tasks_tasks set pickup_at = now()
                          where id = '22222222-2222-4222-8222-222222222222'""")
        conn.execute(f"""insert into {old_store}.tasks_activities (task_id, description)
                         values ('33333333-3333-4333-8333-333333333333', 'more')""")
        heard = [(note.channel, json.loads(note.payload))
                 for note in listener.notifies(timeout=2, stop_after=3)]
    assert ("tasks_claimable", {"schema": old_store, "project": "prj_one",
                                "task": "22222222-2222-4222-8222-222222222222",
                                "pickup_at": heard[[c for c, _p in heard].index(
                                    "tasks_claimable")][1]["pickup_at"]}) in heard
    assert ("tasks_watch", {"schema": old_store, "project": "prj_one",
                            "task": "22222222-2222-4222-8222-222222222222",
                            "kind": "task_changed"}) in heard
    assert ("tasks_watch", {"schema": old_store, "project": "prj_two",
                            "task": "33333333-3333-4333-8333-333333333333",
                            "kind": "activity_added"}) in heard


def left_as_found(conn, bound: str, before: dict) -> None:
    """The refused move changed nothing: schema `tasks` holds what it held, and
    the ledger records no step of tasks."""
    for table in TABLES:
        assert rows(conn, "tasks", table) == before[table], table
    applied = conn.execute(f"""select count(*) from {bound}.schema_ledger
                                where owner = 'tasks'""").fetchone()[0]
    assert applied == 0


@needs_store
def test_a_store_holding_the_new_tables_beside_the_old_is_refused_and_left_as_found(
        old_store):
    with raw() as conn:
        before = {table: rows(conn, "tasks", table) for table in TABLES}
        conn.execute(f"create schema {old_store}")
        conn.execute(f"create table {old_store}.tasks_tasks (id uuid primary key)")
    with pytest.raises(_db().DbError) as refused:
        migrate(old_store)
    assert refused.value.slug == "step_failed"
    assert "already holds tasks_tasks beside them" in refused.value.message
    with raw() as conn:
        left_as_found(conn, old_store, before)
        assert tables_in(conn, old_store) == ["schema_ledger", "schema_version",
                                              "tasks_tasks"]


@pytest.mark.parametrize("behind, named", [
    ("alter table tasks.tasks drop column created_by", "tasks.created_by"),
    ("alter table tasks.task_changes drop column origin_project",
     "task_changes.origin_project"),
    ("drop index tasks.task_executions_run_idx", "task_executions_run_idx"),
    ("""update tasks.tasks set status = 'todo' where status = 'waiting';
        alter table tasks.tasks drop constraint tasks_status_check;
        alter table tasks.tasks add constraint tasks_status_check
          check (status in ('draft','todo','in_progress','complete','closed'))""",
     "tasks.tasks_status_check"),
    ("update tasks.tasks set status = 'in_progress' where unique_key = 'k-2'",
     "1 task(s) stored as in_progress"),
    ("""update tasks.tasks set metadata = '{"blocked_by": ["k-1"]}'
         where project_id = 'prj_two'""",
     "1 task(s) keeping blocked_by in their metadata"),
])
@needs_store
def test_a_store_not_at_the_previous_releases_shape_is_refused_and_left_as_found(
        old_store, behind, named):
    with raw() as conn:
        conn.execute(behind)
        before = {table: rows(conn, "tasks", table) for table in TABLES}
    with pytest.raises(_db().DbError) as refused:
        migrate(old_store)
    assert refused.value.slug == "step_failed"
    message = refused.value.message
    assert "not at the shape of the previous release" in message and named in message
    assert "previous release's `tasks migrate --apply`" in message
    with raw() as conn:
        left_as_found(conn, old_store, before)
        assert tables_in(conn, old_store) == ["schema_ledger", "schema_version"]


# --- Through the CLI -------------------------------------------------------------

def project_at(tmp_path: Path, *, entry: dict | None = None, schema: str | None = None,
               ) -> dict:
    """A project the CLI runs in, with the store setting naming `schema` - or no
    store configured at all."""
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    (envelope / "tasks").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": "prj_" + uuid.uuid4().hex[:12],
        "slug": "lab"}))
    (envelope / "tasks" / "connections.json").write_text(json.dumps({
        "default": "local", "connections": {"local": entry or {"allow_write": True}}}))
    if schema is not None:
        _cli.write_store_setting(tmp_path / "config", schema)
    env = _cli.child_env(os.environ.copy())
    env.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"),
                "CLAUDE_PROJECT_DIR": str(project)})
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "CAPABILITIES_PROJECT_ENVELOPE",
                   "CAPABILITIES_PROJECT_ID", "CAPABILITIES_STORE_MODE"):
        env.pop(leaked, None)
    return {"project": project, "env": env}


def cli(lab: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=lab["project"], env=lab["env"],
                          text=True, capture_output=True, timeout=180)


def error_of(done: subprocess.CompletedProcess) -> dict:
    return json.loads(done.stderr.strip().splitlines()[-1])["error"]


def test_the_manifest_declares_tables(tmp_path):
    manifest = json.loads(cli(project_at(tmp_path), "manifest").stdout)
    assert manifest["tables"] is True and "service" in manifest


def test_with_no_store_configured_every_call_is_refused(tmp_path):
    lab = project_at(tmp_path)
    assert cli(lab, "service", "init").returncode == 0
    for args in (("list",), ("add", "--type", "change", "--title", "t"), ("doctor",),
                 ("migrate", "status"), ("service", "doctor"), ("service", "run")):
        done = cli(lab, *args)
        assert done.returncode == 6, (args, done.stdout, done.stderr)
        error = error_of(done)
        assert error["code"] == "store_not_configured", args
        assert "capabilities store set" in error["hint"], args


@pytest.mark.parametrize("carried", [
    {"db_host": "db.example", "db_port": "5432", "db_name": "tasks", "db_user": "tasks"},
    {"db_schema": "tasks"},
    {"secret_env": "TASKS_DB_PASSWORD"},
    {"db_sslmode": "require"},
])
def test_a_connection_that_still_names_a_store_is_refused(tmp_path, carried):
    lab = project_at(tmp_path, entry={"allow_write": True, **carried},
                     schema="tasks_test_unused")
    for args in (("list",), ("doctor",), ("service", "doctor")):
        done = cli(lab, *args)
        assert done.returncode == 6, (args, done.stdout, done.stderr)
        error = error_of(done)
        assert error["code"] == "store_in_connection", args
        assert all(key in error["message"] for key in carried), args
        assert "capabilities store set" in error["hint"]
        assert all(key in error["hint"] for key in carried)


@needs_store
def test_migrate_status_then_migrate_then_status(tmp_path, fresh_schema):
    lab = project_at(tmp_path, schema=fresh_schema)
    first = json.loads(cli(lab, "migrate", "status").stdout)
    assert (first["owner"], first["schema"], first["state"]) == ("tasks", fresh_schema,
                                                                 "pending")
    assert first["pending"] == ["0001-move-from-schema-tasks", "0002-tables"]
    with raw() as conn:
        assert tables_in(conn, fresh_schema) == []
    done = cli(lab, "migrate")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["applied"] == ["0001-move-from-schema-tasks",
                                                  "0002-tables"]
    then = json.loads(cli(lab, "migrate", "status").stdout)
    assert then["state"] == "current" and then["pending"] == []
    assert then["store"] == {"major": mod.STORE_MAJOR, "minor": mod.STORE_MINOR}
    # And the store answers the domain verbs, in the bound schema.
    made = cli(lab, "add", "--type", "change", "--title", "t", "--key", "k-1")
    assert made.returncode == 0, made.stderr
    doctor = json.loads(cli(lab, "doctor").stdout)
    assert doctor["store"]["schema"] == fresh_schema
    assert doctor["tables"]["state"] == "current"


@needs_store
def test_an_ordinary_call_brings_missing_tables_up_once(tmp_path, fresh_schema):
    lab = project_at(tmp_path, schema=fresh_schema)
    done = cli(lab, "list")
    assert done.returncode == 0, done.stderr
    said = [json.loads(line) for line in done.stderr.splitlines() if line.strip()]
    assert said == [{"migrated": {"schema": fresh_schema,
                                  "applied": ["0001-move-from-schema-tasks", "0002-tables"]}}]
    again = cli(lab, "list")
    assert again.returncode == 0 and again.stderr == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
