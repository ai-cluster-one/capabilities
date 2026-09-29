#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""Which project a task belongs to, and how far a command may reach.

Reads cross the project boundary and writes never do, but for the two writes the
project a task is assigned to may make on it. The project is read from
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
    one. The file is real, so what is exercised is the reader and not a stub.

    Whether a context owner binds the project is the manager's answer, so that
    one answer is given here: unbound unless a test says what ContextKit holds."""
    def write(body, *, envelope="capabilities", contextkit=None):
        root = tmp_path / envelope
        root.mkdir(parents=True, exist_ok=True)
        (root / "project.json").write_text(body if isinstance(body, str)
                                           else json.dumps(body))
        monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
        monkeypatch.setattr(mod, "_project_capabilities_dir",
                            lambda r: tmp_path / envelope)
        monkeypatch.setattr(mod, "_context_identity",
                            lambda r: contextkit or {"bound": False, "id": None})
        # Resolving hands the id down in this process's environment; keep that
        # inside the test.
        monkeypatch.setenv("CAPABILITIES_PROJECT_ID", "")
        monkeypatch.setenv("CAPABILITIES_PROJECT_ID_ROOT", "")
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


# --- A ContextKit-bound project declares itself through ContextKit -----------

def _bound(contextkit_id):
    return {"bound": True, "id": contextkit_id}


def test_a_bound_project_is_the_id_contextkit_answers(declares):
    declares({"slug": "a-project"}, contextkit=_bound(HERE))
    assert mod._declared_project() == HERE
    assert mod._project_id_state()["state"] == "adopted"


def test_a_project_waiting_for_adoption_keeps_its_copy(declares):
    declares({"id": HERE, "slug": "a-project"}, contextkit=_bound(None))
    state = mod._project_id_state()
    assert (state["state"], state["id"]) == ("pending", HERE)
    assert mod._identity_refusal(state) is None


def test_a_mismatch_reads_as_contextkit_and_refuses_writes_naming_both(
        declares, monkeypatch, capsys):
    declares({"id": THERE, "slug": "a-project"}, contextkit=_bound(HERE))
    state = mod._project_id_state()
    assert (state["state"], mod._declared_project()) == ("mismatch", HERE)
    monkeypatch.setattr(mod, "PROJECT_IDENTITY", state)
    monkeypatch.setattr(mod, "PROJECT", state["id"])
    assert mod._reading_project(None) == HERE
    with pytest.raises(SystemExit) as exit_info:
        mod._writing_project()
    assert exit_info.value.code == 6
    error = _error(capsys)
    assert error["code"] == "project_id_mismatch"
    assert HERE in error["message"] and THERE in error["message"]


def test_a_bound_project_with_no_id_anywhere_refuses_writes(declares, monkeypatch, capsys):
    declares({"slug": "a-project"}, contextkit=_bound(None))
    state = mod._project_id_state()
    monkeypatch.setattr(mod, "PROJECT_IDENTITY", state)
    monkeypatch.setattr(mod, "PROJECT", state["id"])
    with pytest.raises(SystemExit) as exit_info:
        mod._writing_project()
    assert exit_info.value.code == 6
    error = _error(capsys)
    assert error["code"] == "project_id_unassigned"
    assert "contextkit identity adopt" in error["hint"]


def test_contextkit_that_cannot_answer_leaves_reads_and_refuses_writes(
        declares, monkeypatch, capsys):
    failed = {"error": {"code": "contextkit_identity_failed", "message": "no answer",
                        "hint": "repair it"}}
    declares({"id": HERE, "slug": "a-project"}, contextkit=failed)
    state = mod._project_id_state()
    assert (state["state"], mod._declared_project()) == ("unresolved", HERE)
    monkeypatch.setattr(mod, "PROJECT_IDENTITY", state)
    monkeypatch.setattr(mod, "PROJECT", state["id"])
    with pytest.raises(SystemExit) as exit_info:
        mod._writing_project()
    assert exit_info.value.code == 6
    assert _error(capsys)["code"] == "contextkit_identity_failed"


def test_a_handed_down_id_outranks_contextkit_and_is_handed_on(declares, monkeypatch):
    def never(root):
        raise AssertionError("ContextKit was asked despite a live hand-down")

    root = declares({"id": HERE, "slug": "a-project"})
    monkeypatch.setattr(mod, "_context_identity", never)
    monkeypatch.setenv("CAPABILITIES_PROJECT_ID", HERE)
    monkeypatch.setenv("CAPABILITIES_PROJECT_ID_ROOT", str(root))
    state = mod._project_id_state()
    assert (state["state"], state["id"]) == ("handed", HERE)
    assert mod._identity_refusal(state) is None


def test_a_resolved_id_a_write_may_stamp_is_handed_down(declares):
    root = declares({"id": HERE, "slug": "a-project"}, contextkit=_bound(HERE))
    mod._project_id_state()
    assert os.environ["CAPABILITIES_PROJECT_ID"] == HERE
    assert os.environ["CAPABILITIES_PROJECT_ID_ROOT"] == str(root)


def test_an_id_a_write_may_not_stamp_is_not_handed_down(declares):
    declares({"id": THERE, "slug": "a-project"}, contextkit=_bound(HERE))
    mod._project_id_state()
    assert os.environ["CAPABILITIES_PROJECT_ID"] == ""


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
    assert ("Reads cross the project boundary and writes never do, with one exception: "
            "the\n  project a task is assigned to may add to its trail and set its "
            "assignee.") in mod.__doc__
    filters = mod.__doc__.split("FILTERS  (list, ready, search, counts)")[1]
    assert "--project ID" in filters.split("PAGING")[0]
    assert "--all-projects" in filters.split("PAGING")[0]


def test_the_help_says_what_the_store_identity_is_for():
    store = mod.__doc__.split("\nSTORE\n")[1].split("\nI/O\n")[0]
    assert "`store`" in store and "asks each store once" in store


def test_the_help_says_a_key_is_unique_within_a_project():
    assert "unique within the project" in mod.__doc__
    assert "unique across the store" not in mod.__doc__
    addressing = mod.__doc__.split("ADDRESSING")[1].split("FIELDS")[0]
    assert "A key is unique within a project" in addressing


@pytest.mark.parametrize("verb", ("list", "search"))
def test_all_projects_is_refused_beside_a_project_naming_both(verb, monkeypatch,
                                                              capsys):
    monkeypatch.setattr(mod, "PROJECT", HERE)
    monkeypatch.setattr(mod, "_connect", lambda entry: pytest.fail(
        f"{verb} reached the store with a contradiction in its filters"))
    handler = {"list": mod.cmd_list, "search": mod.cmd_search}[verb]
    args = ["--all-projects", "--project", THERE] + (["q"] if verb == "search" else [])
    with pytest.raises(SystemExit) as exit_info:
        handler({"timezone": "UTC"}, args)
    assert exit_info.value.code == 6
    message = _error(capsys)["message"]
    assert "--all-projects" in message and "--project" in message


def test_the_queue_a_worker_consumes_takes_no_all_projects(monkeypatch, capsys):
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with pytest.raises(SystemExit) as exit_info:
        mod.cmd_ready({"timezone": "UTC"}, ["--all-projects"])
    assert exit_info.value.code == 6
    assert _error(capsys)["message"] == "unknown flag --all-projects"


class ControlCursor:
    """The reads the store identity makes, answered from memory: whether this
    role may read the cluster's identifier, and the identifier when it may."""

    def __init__(self, readable: bool) -> None:
        self.readable = readable
        self.asked: list[str] = []
        self.answer: dict | None = None

    def execute(self, sql, params=None):
        self.asked.append(sql)
        if "has_function_privilege" in sql:
            self.answer = {"ok": self.readable}
        elif "pg_control_system()" in sql:
            assert self.readable, "read the identifier without the right to"
            self.answer = {"cluster": "7000000000000000001", "db": "a_database"}
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self):
        return self.answer


def test_a_store_it_cannot_prove_is_named_as_no_store(monkeypatch):
    """Without the cluster's identifier there is nothing every caller of one store
    reads the same way, so the answer is an explicit absence rather than a value
    that would differ between two callers of the same store."""
    monkeypatch.setattr(mod, "SCHEMA", "tasks")
    unprovable = ControlCursor(readable=False)
    assert mod._store_identity(unprovable) is None
    assert len(unprovable.asked) == 1
    proven = ControlCursor(readable=True)
    first = mod._store_identity(proven)
    assert first == mod._store_identity(ControlCursor(readable=True))
    assert len(first) == 16 and int(first, 16) >= 0
    monkeypatch.setattr(mod, "SCHEMA", "tasks_elsewhere")
    assert mod._store_identity(ControlCursor(readable=True)) != first


def test_the_help_says_the_store_can_be_absent():
    store = mod.__doc__.split("\nSTORE\n")[1].split("\nI/O\n")[0]
    assert "null when" in store


def test_all_projects_drops_the_project_clause_and_nothing_else(monkeypatch):
    monkeypatch.setattr(mod, "PROJECT", None)
    where, params = mod._where({"all-projects": True})
    assert where == "" and params == []
    where, params = mod._where({"all-projects": True, "status": "todo"})
    assert "project_id" not in where and params == ["todo"]


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


def task_uuid(conn, schema: str, project: str, key: str) -> str:
    """A task's uuid, which names it wherever it is; a key names it only in its
    own project."""
    row = conn.execute(f"select id from {schema}.tasks where project_id = %s "
                       f"and unique_key = %s", (project, key)).fetchone()
    return str(row[0])


@pytest.fixture
def two_projects(store, monkeypatch, capsys):
    entry, schema, conn = store
    seed(monkeypatch, entry, capsys, HERE, "h-1", status="todo")
    seed(monkeypatch, entry, capsys, HERE, "h-2", status="draft")
    seed(monkeypatch, entry, capsys, THERE, "t-1", status="todo")
    return entry, schema, conn


@needs_store
def test_a_task_is_created_in_the_project_that_created_it(two_projects, capsys):
    entry, schema, conn = two_projects
    mod.cmd_show(entry, ["h-1"])
    assert _answer(capsys)["task"]["project_id"] == HERE
    mod.cmd_show(entry, [task_uuid(conn, schema, THERE, "t-1")])
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
def test_all_projects_reads_the_whole_store_from_inside_a_project(two_projects,
                                                                  capsys):
    entry, _schema, _conn = two_projects
    mod.cmd_list(entry, ["--all-projects"])
    listed = _answer(capsys)
    assert sorted((t["unique_key"], t["project_id"]) for t in listed["tasks"]) == [
        ("h-1", HERE), ("h-2", HERE), ("t-1", THERE)]
    assert listed["pagination"]["total"] == 3
    mod.cmd_list(entry, ["--all-projects", "--full"])
    assert all(t["project_id"] for t in _answer(capsys)["tasks"])
    mod.cmd_search(entry, ["-1", "--all-projects"])
    searched = _answer(capsys)
    assert sorted((t["unique_key"], t["project_id"]) for t in searched["tasks"]) == [
        ("h-1", HERE), ("t-1", THERE)]
    # The other filters still narrow it.
    mod.cmd_list(entry, ["--all-projects", "--status", "todo"])
    assert sorted(t["unique_key"] for t in _answer(capsys)["tasks"]) == ["h-1", "t-1"]
    # And without it, the scan still answers for where it stands.
    mod.cmd_list(entry, [])
    assert {t["project_id"] for t in _answer(capsys)["tasks"]} == {HERE}


@needs_store
def test_all_projects_is_a_read_that_works_outside_a_project(two_projects, capsys,
                                                             monkeypatch):
    entry, _schema, _conn = two_projects
    monkeypatch.setattr(mod, "PROJECT", None)
    mod.cmd_list(entry, ["--all-projects"])
    assert len(_answer(capsys)["tasks"]) == 3
    mod.cmd_search(entry, ["t-", "--all-projects"])
    assert [t["unique_key"] for t in _answer(capsys)["tasks"]] == ["t-1"]
    error = _refused(capsys, mod.cmd_list, entry, ["--all-projects", "--project", HERE])
    assert error["exit"] == 6
    assert "--all-projects" in error["message"] and "--project" in error["message"]


@needs_store
def test_the_store_is_named_the_same_for_the_same_store_and_not_for_another(
        store, capsys, monkeypatch):
    import psycopg
    entry, schema, conn = store
    mod.cmd_list(entry, [])
    first = _answer(capsys)["store"]
    mod.cmd_search(entry, ["anything", "--all-projects"])
    assert _answer(capsys)["store"] == first
    # Another caller, standing elsewhere, reaching the same schema.
    monkeypatch.setattr(mod, "PROJECT", THERE)
    mod.cmd_list(entry, ["--all-projects"])
    assert _answer(capsys)["store"] == first
    # Another schema in the same database is another store.
    other = schema + "_other"
    conn.execute(mod._schema_ddl(other))
    try:
        monkeypatch.setattr(mod, "SCHEMA", other)
        mod.cmd_list({**entry, "db_schema": other}, ["--all-projects"])
        second = _answer(capsys)["store"]
    finally:
        conn.execute(f"drop schema {other} cascade")
    assert second != first
    # Nothing secret is in it: it is a digest, and the password is not.
    password = os.environ.get("TASKS_TEST_PASSWORD") or ""
    for identity in (first, second):
        assert len(identity) == 16 and int(identity, 16) >= 0
        assert not password or password not in identity


@needs_store
def test_naming_one_task_by_uuid_answers_about_it_whichever_project_it_is_in(
        two_projects, capsys):
    entry, schema, conn = two_projects
    theirs = task_uuid(conn, schema, THERE, "t-1")
    mod.cmd_show(entry, [theirs])
    assert _answer(capsys)["task"]["unique_key"] == "t-1"
    mod.cmd_runs(entry, [theirs])
    assert _answer(capsys)["attempts"] == 0
    mod.cmd_history(entry, [theirs])
    assert _answer(capsys)["changes"] == []


@needs_store
def test_a_key_never_reaches_another_projects_task(two_projects, capsys,
                                                   monkeypatch):
    """A key is unique within its project, so it names a task only there: from
    here, their key is no task at all, for a read and for a write alike."""
    entry, schema, conn = two_projects
    for call, args in ((mod.cmd_show, ["t-1"]), (mod.cmd_runs, ["t-1"]),
                       (mod.cmd_history, ["t-1"]), (mod.cmd_meta, ["show", "t-1"]),
                       (mod.cmd_set, ["t-1", "--title", "mine now"]),
                       (mod.cmd_activity, ["t-1", "reached across"]),
                       (mod.cmd_claim, ["--key", "t-1"])):
        error = _refused(capsys, call, entry, args)
        assert error["exit"] == 3 and error["code"] == "not_found", call
    # Nothing moved on their task.
    mod.cmd_show(entry, [task_uuid(conn, schema, THERE, "t-1")])
    other = _answer(capsys)
    assert other["task"]["title"] == "t-1" and other["task"]["status"] == "todo"
    assert other["activities"] == []
    # Standing in their project, the same key is theirs.
    monkeypatch.setattr(mod, "PROJECT", THERE)
    mod.cmd_show(entry, ["t-1"])
    assert _answer(capsys)["task"]["project_id"] == THERE


@needs_store
def test_two_projects_each_own_the_same_key(two_projects, capsys, monkeypatch):
    entry, schema, conn = two_projects
    # Their project already holds t-1; here it is free.
    mod.cmd_add(entry, ["--type", "probe", "--title", "ours", "--key", "t-1"])
    created = _answer(capsys)
    assert "created" in created
    ours = created["created"]
    assert ours != task_uuid(conn, schema, THERE, "t-1")
    # Each project's key names its own task.
    mod.cmd_show(entry, ["t-1"])
    assert _answer(capsys)["task"]["title"] == "ours"
    monkeypatch.setattr(mod, "PROJECT", THERE)
    mod.cmd_show(entry, ["t-1"])
    assert _answer(capsys)["task"]["title"] == "t-1"
    # Idempotent within a project: a second add creates nothing on either side.
    for project, holder in ((HERE, ours), (THERE, task_uuid(conn, schema, THERE, "t-1"))):
        monkeypatch.setattr(mod, "PROJECT", project)
        mod.cmd_add(entry, ["--type", "probe", "--title", "again", "--key", "t-1"])
        assert _answer(capsys) == {"exists": holder, "unique_key": "t-1"}
    count = conn.execute(f"select count(*) from {schema}.tasks "
                         f"where unique_key = 't-1'").fetchone()[0]
    assert count == 2
    # The store itself refuses a second holder inside one project.
    monkeypatch.setattr(mod, "PROJECT", HERE)
    error = _refused(capsys, mod._guarded, mod.cmd_set, entry,
                     ["h-1", "--unique-key", "t-1"])
    assert error["exit"] == 6 and error["code"] == "conflict"


@needs_store
def test_a_wait_is_freed_only_by_its_own_projects_key(two_projects, capsys,
                                                      monkeypatch):
    """A blocker named by key is read in the waiting task's project. Another
    project's task of the same key ending frees nothing here."""
    entry, schema, conn = two_projects
    monkeypatch.setattr(mod, "PROJECT", THERE)
    mod.cmd_add(entry, ["--type", "probe", "--title", "theirs", "--key", "gate",
                        "--status", "complete"])
    capsys.readouterr()
    monkeypatch.setattr(mod, "PROJECT", HERE)
    mod.cmd_add(entry, ["--type", "probe", "--title", "ours", "--key", "gate",
                        "--status", "todo"])
    capsys.readouterr()
    mod.cmd_meta(entry, ["set", "h-2", "blocked_by", '["gate"]'])
    mod.cmd_set(entry, ["h-2", "--status", "waiting", "--assignee", "someone"])
    capsys.readouterr()
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        assert mod._ended_among(cur, ["gate"], HERE) == set()
    mod.cmd_claim(entry, ["--worker", "a worker"])
    assert _answer(capsys)["returned"] == []
    mod.cmd_show(entry, ["h-2"])
    assert _answer(capsys)["task"]["status"] == "waiting"
    # Our own gate ending is what frees it.
    mod.cmd_set(entry, ["gate", "--status", "complete"])
    capsys.readouterr()
    mod.cmd_claim(entry, ["--worker", "a worker"])
    assert _answer(capsys)["returned"] == ["h-2"]


@needs_store
def test_no_write_reaches_another_projects_task(two_projects, capsys):
    entry, schema, conn = two_projects
    theirs = task_uuid(conn, schema, THERE, "t-1")
    for call, args in ((mod.cmd_set, [[theirs, "--title", "mine now"]]),
                       (mod.cmd_set, [[theirs, "--clear", "objective"]]),
                       (mod.cmd_tag, [[theirs, "mine"]]),
                       (mod.cmd_meta, [["set", theirs, "mine", "1"]]),
                       (mod.cmd_meta, [["rm", theirs, "mine"]]),
                       (mod.cmd_activity, [[theirs, "reached across"]])):
        error = _refused(capsys, call, entry, *args)
        assert error["exit"] == 4 and error["code"] == "policy"
        assert THERE in error["message"] and HERE in error["message"]
    error = _refused(capsys, mod.cmd_tag, entry, [theirs, "mine"], True)
    assert error["exit"] == 4
    # The refusals wrote nothing.
    mod.cmd_show(entry, [theirs])
    other = _answer(capsys)
    assert other["task"]["title"] == "t-1" and other["task"]["tags"] == []
    assert other["task"]["metadata"] == {} and other["activities"] == []
    # Reading the other project's metadata is still a read.
    mod.cmd_meta(entry, ["show", theirs])
    assert _answer(capsys)["metadata"] == {}


@needs_store
def test_a_claim_cannot_take_another_projects_task(two_projects, capsys):
    entry, schema, conn = two_projects
    theirs = task_uuid(conn, schema, THERE, "t-1")
    error = _refused(capsys, mod.cmd_claim, entry, ["--key", theirs])
    assert error["exit"] == 4 and error["code"] == "policy"
    assert THERE in error["message"]
    mod.cmd_show(entry, [theirs])
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
    mod.cmd_show(entry, [task_uuid(_conn, _schema, THERE, "t-1")])
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
    mod.cmd_show(entry, [task_uuid(conn, schema, THERE, "t-1")])
    assert _answer(capsys)["task"]["status"] == "in_progress"
    mod.cmd_show(entry, [task_uuid(conn, schema, THERE, "t-2")])
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
    # Naming one task by its uuid needs no project at all.
    mod.cmd_show(entry, [task_uuid(_conn, _schema, HERE, "h-1")])
    assert _answer(capsys)["task"]["unique_key"] == "h-1"
    # A key is unique only within a project, so outside one it names nothing.
    error = _refused(capsys, mod.cmd_show, entry, ["h-1"])
    assert error["exit"] == 6 and error["code"] == "no_project"
    assert "uuid" in error["hint"]

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
    # A store from before the column also keyed its tasks across the whole store.
    conn.execute(f"alter table {schema}.tasks drop column project_id")
    conn.execute(f"alter table {schema}.tasks add constraint tasks_unique_key_key "
                 f"unique (unique_key)")

    with conn.cursor(row_factory=dict_row) as cur:
        tables = mod._tables_present(cur, schema)
        assert mod._columns_absent(cur, schema, tables) == ["tasks.project_id"]

    mod.cmd_migrate(entry, [])
    reported = _answer(capsys)
    assert reported["would_add"] == ["tasks.project_id",
                                     "tasks.tasks_project_unique_key_idx"]
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
    assert applied["added"] == ["tasks.project_id",
                                "tasks.tasks_project_unique_key_idx"]
    assert applied["applied"] is True

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

    # The migrated store keys per project: the store-wide constraint is gone, and
    # another project may now hold a key this one already holds.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select count(*) as n from pg_constraint c
                         join pg_namespace n on n.oid = c.connamespace
                        where n.nspname = %s and c.conname = 'tasks_unique_key_key'""",
                    (schema,))
        assert cur.fetchone()["n"] == 0
    seed(monkeypatch, entry, capsys, THERE, "h-1")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"select project_id from {schema}.tasks where unique_key = 'h-1' "
                    f"order by project_id")
        assert [r["project_id"] for r in cur.fetchall()] == [HERE, THERE]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
