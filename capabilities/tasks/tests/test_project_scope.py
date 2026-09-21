#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""Which project a task belongs to, and how far a command may reach.

Reads cross the project boundary and writes never do. The project is read from
the project's own identity file, which is checked here against a real file on
disk rather than against a value a test set, so what is proven is the path a
consuming project actually takes.

The store-backed half seeds two projects into one schema and reads
TASKS_TEST_DSN, skipping when it is unset; every run works in a schema of its
own and drops it.

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


# --- Identity: what the command stands in ------------------------------------

@pytest.fixture
def declares(tmp_path, monkeypatch):
    """A project on disk that declares itself, the way `capabilities init` leaves
    one. The file is real, so what is exercised is the reader and not a stub."""
    def write(body, *, envelope="capabilities"):
        root = tmp_path / envelope
        root.mkdir(parents=True, exist_ok=True)
        (root / "project.json").write_text(body if isinstance(body, str)
                                           else json.dumps(body))
        monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
        monkeypatch.setattr(mod, "_project_capabilities_dir",
                            lambda r: tmp_path / envelope)
        return tmp_path
    return write


def test_the_project_is_the_id_its_identity_file_declares(declares):
    declares({"id": HERE, "slug": "a-project", "schema": "capabilities.project.v1"})
    assert mod._declared_project() == HERE


def test_the_slug_is_never_the_key(declares):
    # The readable name has a home in the project; the ledger carries the id and
    # nothing else, so nothing here can match a project by two criteria.
    declares({"id": HERE, "slug": "a-project"})
    assert mod._declared_project() == HERE
    assert "slug" not in mod._LEAN


def test_reading_the_identity_asks_the_records_backend_nothing(declares, monkeypatch):
    """A project keeping its records on files declares itself the same way one
    keeping them in the store does, so the reader may not go near either."""
    def never(*a, **k):
        raise AssertionError("identity was read through the records backend")

    monkeypatch.setattr(mod, "_records", never)
    monkeypatch.setattr(mod, "open_records", never)
    monkeypatch.setattr(mod, "open_store", never)
    monkeypatch.setattr(mod, "records_mode", never)
    declares({"id": HERE, "slug": "a-project"})
    assert mod._declared_project() == HERE


def test_nowhere_and_nothing_declared_are_both_no_project(tmp_path, monkeypatch, declares):
    monkeypatch.setattr(mod, "_project_root", lambda: None)
    assert mod._declared_project() is None
    for body in ('{"slug": "no id here"}', '{"id": ""}', '{"id": "   "}',
                 "not json at all", "[]"):
        declares(body)
        assert mod._declared_project() is None
    # And an envelope with no identity file at all.
    monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda r: tmp_path / "empty")
    assert mod._declared_project() is None


# --- What each direction does with it ----------------------------------------

def _error(capsys) -> dict:
    return json.loads(capsys.readouterr().err)["error"]


def test_a_read_takes_the_project_it_stands_in_unless_it_names_one(monkeypatch):
    monkeypatch.setattr(mod, "PROJECT", HERE)
    assert mod._reading_project(None) == HERE
    assert mod._reading_project(THERE) == THERE


def test_a_read_outside_a_project_has_to_name_one(monkeypatch, capsys):
    monkeypatch.setattr(mod, "PROJECT", None)
    with pytest.raises(SystemExit) as exit_info:
        mod._reading_project(None)
    assert exit_info.value.code == 6
    error = _error(capsys)
    assert error["code"] == "no_project" and "--project" in error["hint"]
    # Naming one is all it takes; nothing else about being nowhere matters.
    assert mod._reading_project(THERE) == THERE


def test_a_write_outside_a_project_is_refused_and_names_what_settles_it(
        monkeypatch, capsys):
    monkeypatch.setattr(mod, "PROJECT", None)
    with pytest.raises(SystemExit) as exit_info:
        mod._writing_project()
    assert exit_info.value.code == 6
    error = _error(capsys)
    assert error["code"] == "no_project"
    assert "capabilities init" in error["hint"]
    monkeypatch.setattr(mod, "PROJECT", HERE)
    assert mod._writing_project() == HERE


def test_the_project_clause_is_on_every_question_the_store_is_asked(monkeypatch):
    monkeypatch.setattr(mod, "PROJECT", HERE)
    where, params = mod._where({})
    assert where.startswith(" where project_id = %s") and params == [HERE]
    where, params = mod._where({"project": THERE, "status": "todo"})
    assert params == [THERE, "todo"]


def test_a_write_reaching_another_project_is_refused_the_way_scope_refuses(
        monkeypatch, capsys):
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with pytest.raises(SystemExit) as exit_info:
        mod._project_gate("k-1", THERE)
    assert exit_info.value.code == 4
    error = _error(capsys)
    assert error["code"] == "policy"
    assert THERE in error["message"] and HERE in error["message"]
    mod._project_gate("k-1", HERE)  # its own: no exit


WRITE_VERBS = {
    "add": (mod.cmd_add, ["--type", "probe", "--title", "t"]),
    "set": (mod.cmd_set, ["k-1", "--title", "t"]),
    "tag": (mod.cmd_tag, ["k-1", "a-tag"]),
    "meta": (mod.cmd_meta, ["set", "k-1", "a", "1"]),
    "activity": (mod.cmd_activity, ["k-1", "what happened"]),
    "claim": (mod.cmd_claim, []),
    "release": (mod.cmd_release, ["exec-1", "--outcome", "ok"]),
    "run": (mod.cmd_run, ["a-worker"]),
    "migrate": (mod.cmd_migrate, []),
}


@pytest.mark.parametrize("verb", sorted(WRITE_VERBS))
def test_no_write_verb_takes_a_project(verb, monkeypatch, capsys):
    """A write never names its project, so the flag is not merely ignored there -
    it is refused, and the refusal lists what the verb does take."""
    monkeypatch.setattr(mod, "PROJECT", HERE)
    monkeypatch.setattr(mod, "_connect", lambda entry: pytest.fail(
        f"{verb} reached the store with a project named"))
    handler, args = WRITE_VERBS[verb]
    with pytest.raises(SystemExit) as exit_info:
        handler({"timezone": "UTC"}, ["--project", THERE, *args])
    assert exit_info.value.code == 6
    assert _error(capsys)["message"] == "unknown flag --project"


@pytest.mark.parametrize("verb", ("list", "ready", "search"))
def test_every_scan_takes_one(verb):
    assert "project" in mod._LIST_FLAGS


def test_the_help_states_the_rule_and_files_the_filter_with_the_others():
    assert "Reads cross the project boundary and writes never do." in mod.__doc__
    filters = mod.__doc__.split("FILTERS  (list, ready, search)")[1]
    assert "--project ID" in filters.split("PAGING")[0]


# --- The migration -----------------------------------------------------------

def test_the_store_can_be_behind_on_the_column():
    assert ("tasks", "project_id") in mod._COLUMNS


def test_the_schema_adds_then_fills_then_tightens():
    ddl = mod._schema_ddl("tasks")
    steps = [ddl.index("add column if not exists project_id"),
             ddl.index("set project_id = current_setting"),
             ddl.index("alter column project_id set not null")]
    assert steps == sorted(steps)
    assert "create index if not exists tasks_project_idx" in ddl
    # Nothing is dropped or emptied to make the column fit. Read past the
    # commentary, which says so in prose and would answer for itself.
    statements = "\n".join(line for line in ddl.splitlines()
                           if not line.lstrip().startswith("--"))
    assert "drop column" not in statements
    assert "truncate" not in statements.lower()
    assert "delete from" not in statements.lower()


def test_the_schema_names_no_project_of_its_own():
    """The value existing rows are filled with arrives from the project running
    the migration. A project id written into the file would be right for exactly
    one store, and would be one consumer's identity shipped to every other."""
    ddl = mod._schema_ddl("tasks")
    assert "prj_" not in ddl


def test_the_schema_rewrite_leaves_the_migration_setting_alone():
    """The whole file is re-addressed to whichever schema the connection names,
    and the setting the backfill reads is not a schema."""
    ddl = mod._schema_ddl("tasks_elsewhere")
    assert "current_setting('tasks_migration.project_id')" in ddl
    assert "tasks_elsewhere.tasks" in ddl


# --- Store: two projects in one schema ---------------------------------------

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
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(mod._schema_ddl(schema))
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _answer(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _refused(capsys, call, *args) -> dict:
    with pytest.raises(SystemExit) as exit_info:
        call(*args)
    error = json.loads(capsys.readouterr().err)["error"]
    error["exit"] = exit_info.value.code
    return error


def seed(monkeypatch, entry, capsys, project: str, key: str, **fields) -> None:
    """One task, created standing in the project it is to belong to - which is
    the only way a task is ever created."""
    monkeypatch.setattr(mod, "PROJECT", project)
    args = ["--type", "probe", "--title", key, "--key", key]
    for flag, value in fields.items():
        args += [f"--{flag}", value]
    mod.cmd_add(entry, args)
    capsys.readouterr()
    monkeypatch.setattr(mod, "PROJECT", HERE)


@pytest.fixture
def two_projects(store, monkeypatch, capsys):
    entry, schema, conn = store
    seed(monkeypatch, entry, capsys, HERE, "h-1", status="todo")
    seed(monkeypatch, entry, capsys, HERE, "h-2", status="draft")
    seed(monkeypatch, entry, capsys, THERE, "t-1", status="todo")
    return entry, schema, conn


@needs_store
def test_a_task_is_created_in_the_project_that_created_it(two_projects, capsys):
    entry, _schema, _conn = two_projects
    mod.cmd_show(entry, ["h-1"])
    assert _answer(capsys)["task"]["project_id"] == HERE
    mod.cmd_show(entry, ["t-1"])
    assert _answer(capsys)["task"]["project_id"] == THERE


@needs_store
def test_a_scan_unsaid_answers_for_the_project_it_stands_in(two_projects, capsys):
    entry, _schema, _conn = two_projects
    mod.cmd_list(entry, [])
    listed = _answer(capsys)
    assert sorted(t["unique_key"] for t in listed["tasks"]) == ["h-1", "h-2"]
    assert listed["pagination"]["total"] == 2
    mod.cmd_ready(entry, [])
    assert [t["unique_key"] for t in _answer(capsys)["tasks"]] == ["h-1"]
    # `search` reads what a person writes, so it is asked for one of those.
    mod.cmd_search(entry, ["h-"])
    assert sorted(t["unique_key"] for t in _answer(capsys)["tasks"]) == ["h-1", "h-2"]


@needs_store
def test_a_scan_that_names_a_project_crosses_to_it(two_projects, capsys):
    entry, _schema, _conn = two_projects
    mod.cmd_list(entry, ["--project", THERE])
    listed = _answer(capsys)
    assert [t["unique_key"] for t in listed["tasks"]] == ["t-1"]
    assert listed["tasks"][0]["project_id"] == THERE
    mod.cmd_ready(entry, ["--project", THERE])
    assert [t["unique_key"] for t in _answer(capsys)["tasks"]] == ["t-1"]
    mod.cmd_search(entry, ["t-", "--project", THERE])
    assert [t["unique_key"] for t in _answer(capsys)["tasks"]] == ["t-1"]
    # And the same question, unsaid, still answers for where it stands.
    mod.cmd_search(entry, ["t-"])
    assert _answer(capsys)["tasks"] == []
    # A project with nothing in it answers with nothing, not with everything.
    mod.cmd_list(entry, ["--project", "prj_nobody"])
    assert _answer(capsys)["tasks"] == []


@needs_store
def test_naming_one_task_answers_about_it_whichever_project_it_is_in(
        two_projects, capsys):
    entry, _schema, _conn = two_projects
    mod.cmd_show(entry, ["t-1"])
    assert _answer(capsys)["task"]["unique_key"] == "t-1"
    mod.cmd_runs(entry, ["t-1"])
    assert _answer(capsys)["attempts"] == 0
    mod.cmd_history(entry, ["t-1"])
    assert _answer(capsys)["changes"] == []


@needs_store
def test_no_write_reaches_another_projects_task(two_projects, capsys):
    entry, _schema, _conn = two_projects
    for call, args in ((mod.cmd_set, [["t-1", "--title", "mine now"]]),
                       (mod.cmd_set, [["t-1", "--clear", "objective"]]),
                       (mod.cmd_tag, [["t-1", "mine"]]),
                       (mod.cmd_meta, [["set", "t-1", "mine", "1"]]),
                       (mod.cmd_meta, [["rm", "t-1", "mine"]]),
                       (mod.cmd_activity, [["t-1", "reached across"]])):
        error = _refused(capsys, call, entry, *args)
        assert error["exit"] == 4 and error["code"] == "policy"
        assert THERE in error["message"] and HERE in error["message"]
    error = _refused(capsys, mod.cmd_tag, entry, ["t-1", "mine"], True)
    assert error["exit"] == 4
    # The refusals wrote nothing.
    mod.cmd_show(entry, ["t-1"])
    other = _answer(capsys)
    assert other["task"]["title"] == "t-1" and other["task"]["tags"] == []
    assert other["task"]["metadata"] == {} and other["activities"] == []
    # Reading the other project's metadata is still a read.
    mod.cmd_meta(entry, ["show", "t-1"])
    assert _answer(capsys)["metadata"] == {}


@needs_store
def test_a_claim_cannot_take_another_projects_task(two_projects, capsys):
    entry, _schema, _conn = two_projects
    error = _refused(capsys, mod.cmd_claim, entry, ["--key", "t-1"])
    assert error["exit"] == 4 and error["code"] == "policy"
    assert THERE in error["message"]
    mod.cmd_show(entry, ["t-1"])
    assert _answer(capsys)["task"]["status"] == "todo"  # untouched


@needs_store
def test_a_claim_takes_only_what_its_own_project_holds(two_projects, capsys):
    entry, _schema, _conn = two_projects
    mod.cmd_claim(entry, ["--worker", "a worker"])
    first = _answer(capsys)
    assert first["task"]["unique_key"] == "h-1"
    # h-1 taken, h-2 is a draft and t-1 is another project's: nothing is left.
    mod.cmd_claim(entry, ["--worker", "a worker"])
    assert _answer(capsys)["claimed"] is None


@needs_store
def test_a_release_cannot_close_another_projects_raise(two_projects, capsys,
                                                       monkeypatch):
    entry, _schema, _conn = two_projects
    monkeypatch.setattr(mod, "PROJECT", THERE)
    mod.cmd_claim(entry, ["--key", "t-1", "--worker", "their worker"])
    execution = _answer(capsys)["execution"]["id"]
    monkeypatch.setattr(mod, "PROJECT", HERE)
    error = _refused(capsys, mod.cmd_release, entry, [execution, "--outcome", "ok"])
    assert error["exit"] == 4 and error["code"] == "policy"
    assert THERE in error["message"]
    mod.cmd_show(entry, ["t-1"])
    assert _answer(capsys)["task"]["status"] == "in_progress"  # still held


@needs_store
def test_a_claim_sweeps_nothing_of_another_projects(two_projects, capsys,
                                                    monkeypatch):
    """Both sweeps inside `claim` are writes, so a claim looking for its own work
    never closes another project's raise or returns another project's wait."""
    entry, schema, conn = two_projects
    monkeypatch.setattr(mod, "PROJECT", THERE)
    seed(monkeypatch, entry, capsys, THERE, "t-2", status="todo")
    monkeypatch.setattr(mod, "PROJECT", THERE)
    mod.cmd_claim(entry, ["--key", "t-1", "--worker", "their worker"])
    execution = _answer(capsys)["execution"]["id"]
    mod.cmd_set(entry, ["t-2", "--status", "waiting", "--assignee", "them",
                        "--pickup", "2020-01-01"])
    capsys.readouterr()
    # Their lease has already passed and their wait is already over.
    conn.execute(f"update {schema}.task_executions set lease_until = now() - "
                 f"interval '1 hour' where id = %s", (execution,))

    monkeypatch.setattr(mod, "PROJECT", HERE)
    mod.cmd_claim(entry, ["--worker", "a worker"])
    answer = _answer(capsys)
    assert answer["task"]["unique_key"] == "h-1"
    assert answer["swept"] == [] and answer["returned"] == []
    mod.cmd_show(entry, ["t-1"])
    assert _answer(capsys)["task"]["status"] == "in_progress"
    mod.cmd_show(entry, ["t-2"])
    assert _answer(capsys)["task"]["status"] == "waiting"

    # Their own claim sweeps both, so what was proven is the boundary and not
    # that the sweeps stopped working.
    monkeypatch.setattr(mod, "PROJECT", THERE)
    mod.cmd_claim(entry, ["--worker", "their worker"])
    theirs = _answer(capsys)
    assert theirs["swept"] == [execution] and theirs["returned"] == ["t-2"]


@needs_store
def test_outside_a_project_a_read_names_one_and_a_write_is_refused(
        two_projects, capsys, monkeypatch):
    entry, _schema, _conn = two_projects
    monkeypatch.setattr(mod, "PROJECT", None)

    error = _refused(capsys, mod.cmd_list, entry, [])
    assert error["exit"] == 6 and error["code"] == "no_project"
    mod.cmd_list(entry, ["--project", HERE])
    assert sorted(t["unique_key"] for t in _answer(capsys)["tasks"]) == ["h-1", "h-2"]
    # Naming one task needs no project at all.
    mod.cmd_show(entry, ["h-1"])
    assert _answer(capsys)["task"]["unique_key"] == "h-1"

    for call, args in ((mod.cmd_add, ["--type", "probe", "--title", "orphan"]),
                       (mod.cmd_set, ["h-1", "--title", "renamed"]),
                       (mod.cmd_activity, ["h-1", "from nowhere"]),
                       (mod.cmd_claim, ["--worker", "a worker"])):
        error = _refused(capsys, call, entry, args)
        assert error["exit"] == 6 and error["code"] == "no_project"
        assert "capabilities init" in error["hint"]

    # Nothing was written, and above all no row belonging to nobody.
    with _conn.cursor() as cur:
        cur.execute(f"select count(*) from {_schema}.tasks "
                    f"where project_id is null or project_id = ''")
        assert cur.fetchone()[0] == 0
        cur.execute(f"select count(*) from {_schema}.tasks")
        assert cur.fetchone()[0] == 3


@needs_store
def test_the_migration_fills_a_store_that_predates_the_column(
        two_projects, capsys, monkeypatch):
    """A store from before the column holds one project's tasks, and the project
    running the migration is that project - so every row it finds is filled with
    the id that project declares, and only then is the column tightened."""
    from psycopg.rows import dict_row
    entry, schema, conn = two_projects
    conn.execute(f"alter table {schema}.tasks drop column project_id")

    with conn.cursor(row_factory=dict_row) as cur:
        tables = mod._tables_present(cur, schema)
        assert mod._columns_absent(cur, schema, tables) == ["tasks.project_id"]

    mod.cmd_migrate(entry, [])
    reported = _answer(capsys)
    assert reported["would_add"] == ["tasks.project_id"]
    assert reported["project"] == HERE and reported["applied"] is False

    # What each task's moment was before the column arrived. Filling a column is
    # not the task moving, and a migration that says otherwise leaves a ledger
    # where everything last happened at once.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"select unique_key, updated_at from {schema}.tasks")
        before = {r["unique_key"]: r["updated_at"] for r in cur.fetchall()}
    assert len(before) == 3

    mod.cmd_migrate(entry, ["--apply"])
    applied = _answer(capsys)
    assert applied["added"] == ["tasks.project_id"] and applied["applied"] is True

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"select project_id, count(*) as n from {schema}.tasks "
                    f"group by project_id")
        assert _cli and [dict(r) for r in cur.fetchall()] == [{"project_id": HERE,
                                                              "n": 3}]
        cur.execute("""select is_nullable from information_schema.columns
                        where table_schema = %s and table_name = 'tasks'
                          and column_name = 'project_id'""", (schema,))
        assert cur.fetchone()["is_nullable"] == "NO"
        cur.execute("""select indexname from pg_indexes
                        where schemaname = %s and indexname = 'tasks_project_idx'""",
                    (schema,))
        assert cur.fetchone() is not None

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"select unique_key, updated_at from {schema}.tasks")
        after = {r["unique_key"]: r["updated_at"] for r in cur.fetchall()}
    assert after == before

    # Repeatable: running it again reports nothing and moves no row.
    mod.cmd_migrate(entry, [])
    assert _answer(capsys)["would_add"] == []
    mod.cmd_migrate(entry, ["--apply"])
    assert _answer(capsys)["added"] == []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"select count(*) as n from {schema}.tasks "
                    f"where project_id = %s", (HERE,))
        assert cur.fetchone()["n"] == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
