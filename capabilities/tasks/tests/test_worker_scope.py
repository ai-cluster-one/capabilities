#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""What a worker may write, and where the refusal falls.

The decision is pure and is checked without a store: the raise is a fake row
and the lookup a fake table, so every combination of held, not held, open and
over is reachable here rather than only against a live queue. The store-backed
checks then prove the same rules where they matter - through the verbs, on a
real claim - and read TASKS_TEST_DSN, skipping when it is unset; every run works
in a schema of its own and drops it.

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

HELD, OTHER = "task-held", "task-other"
HERE = "prj_worker_scope"
WORKER = {"TASKS_EXECUTION": "exec-1"}
OPEN = {"id": "exec-1", "task_id": HELD, "status": "running"}
CLOSED = {"id": "exec-1", "task_id": HELD, "status": "ok"}
SWEPT = {"id": "exec-1", "task_id": HELD, "status": "abandoned"}
OVER = (CLOSED, SWEPT, None)

ADDITIVE = ("tag", "meta-set", "activity")
HELD_ONLY = ("set", "untag", "meta-rm", "meta-overwrite")


def lookup_of(row):
    """A raise table holding at most one row, answering None for anything else."""
    table = {row["id"]: row} if row else {}
    return lambda execution_id: table.get(execution_id)


def never(execution_id):
    raise AssertionError("the raise was read when the answer did not depend on it")


def refusal(op: str, target: str, row=OPEN, env=WORKER):
    return mod._worker_refusal(op, target, lookup_of(row), env)


# --- The decision alone ------------------------------------------------------

def test_without_the_variable_nothing_is_a_worker():
    for op in ("status:todo", "status:in_progress", "type", *ADDITIVE, *HELD_ONLY):
        assert mod._worker_refusal(op, OTHER, never, {}) is None
        assert mod._worker_refusal(op, OTHER, never, {"TASKS_EXECUTION": "  "}) is None


def test_the_variable_is_read_in_one_place():
    assert mod._worker_execution({"TASKS_EXECUTION": " exec-7 "}) == "exec-7"
    assert mod._worker_execution({}) == ""
    # The same reading signs the write, so a worker's writes cannot be a worker's
    # by one rule and somebody else's by the other.
    assert mod._actor("person:flag", WORKER) == "execution:exec-1"


def test_a_worker_lands_its_own_task_on_the_five():
    assert mod.WORKER_STATUSES == ("draft", "todo", "waiting", "complete", "closed")
    for status in mod.WORKER_STATUSES:
        assert refusal(f"status:{status}", HELD) is None


def test_in_progress_is_not_a_landing():
    message = refusal("status:in_progress", HELD)
    assert message and "in_progress" in message
    for landing in mod.WORKER_STATUSES:
        assert landing in message


def test_a_worker_hands_its_own_task_over_and_nobody_elses():
    # Handing the work to somebody named is an ending a run reaches, so it is a
    # landing; it is still only a landing for the task the raise holds.
    assert "waiting" in mod.STATUSES and "waiting" in mod.WORKER_STATUSES
    assert refusal("status:waiting", HELD) is None
    assert refusal("status:waiting", OTHER) is not None


def test_another_task_is_not_a_workers_to_move():
    for status in mod.STATUSES:
        assert refusal(f"status:{status}", OTHER) is not None
    assert refusal("status:todo", OTHER) == (
        "a worker may not move another task to todo; it holds only the task its "
        "raise names")


def test_type_is_refused_everywhere_without_asking_the_raise():
    for target in (HELD, OTHER):
        message = mod._worker_refusal("type", target, never, WORKER)
        assert message and "type" in message


def test_adding_is_open_on_every_task_and_in_every_state():
    for op in ADDITIVE:
        for target in (HELD, OTHER):
            assert mod._worker_refusal(op, target, never, WORKER) is None


def test_taking_away_and_writing_over_belong_to_the_holder():
    for op in HELD_ONLY:
        assert refusal(op, HELD) is None
        message = refusal(op, OTHER)
        assert message and "does not hold" in message


@pytest.mark.parametrize("row", OVER, ids=("closed", "swept", "missing"))
def test_a_raise_that_is_over_holds_nothing(row):
    # Every status write, including on the task it used to hold.
    for target in (HELD, OTHER):
        for status in mod.WORKER_STATUSES:
            message = refusal(f"status:{status}", target, row)
            assert message and "no longer open" in message
    # Annotation falls back to what is open on any other task.
    for op in ADDITIVE:
        assert refusal(op, HELD, row) is None
    for op in HELD_ONLY:
        assert refusal(op, HELD, row) is not None


def test_every_refusal_is_one_sentence_a_person_can_read():
    seen = [refusal("status:in_progress", HELD), refusal("status:todo", OTHER),
            refusal("status:todo", HELD, CLOSED), refusal("type", HELD),
            *[refusal(op, OTHER) for op in HELD_ONLY]]
    for message in seen:
        assert message.startswith("a worker ")
        assert message.count(".") == 0 and len(message) < 200


def test_the_gate_exits_with_the_policy_code(capsys, monkeypatch):
    # The gate reads the process environment, which is where a runner puts it.
    monkeypatch.setenv("TASKS_EXECUTION", "exec-1")
    with pytest.raises(SystemExit) as exit_info:
        mod._worker_gate("status:todo", OTHER, lookup_of(OPEN))
    assert exit_info.value.code == 4
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "policy"
    assert error["message"] == refusal("status:todo", OTHER)
    assert "activity" in error["hint"]
    mod._worker_gate("status:todo", HELD, lookup_of(OPEN))  # permitted: no exit


def test_an_unknown_operation_is_a_programming_error():
    with pytest.raises(ValueError):
        refusal("delete", HELD)


def test_the_raise_is_read_once_however_often_it_is_asked_for():
    class Cur:
        def __init__(self):
            self.queries: list[tuple] = []

        def execute(self, sql, params=None):
            self.queries.append((sql, params))

        def fetchall(self):
            return [dict(OPEN)]

    cur = Cur()
    lookup = mod._execution_lookup(cur)
    assert lookup("exec-1")["task_id"] == HELD
    assert lookup("exec-1")["task_id"] == HELD
    assert len(cur.queries) == 1
    assert cur.queries[0][1] == ("exec-1",)


def test_help_names_the_scope():
    assert "WORKER SCOPE" in mod.__doc__
    for needle in ("TASKS_EXECUTION", "coerced", "exit 4",
                   "draft, todo, waiting, complete or closed"):
        assert needle in mod.__doc__


# --- The verb, with the two reads faked and no store -------------------------

# `set` decides on the verb and not on the field, so the proof has to name every
# field the verb accepts: one left out of the gate is exactly the hole this
# closes. The values are only shaped well enough to parse.
SET_VALUES = {"type": "change", "title": "a title", "objective": "why",
              "description": "what was seen", "status": "todo",
              "assignee": "somebody", "pickup": "2026-09-10", "unique-key": "k-1"}
PLAIN_FIELDS = tuple(f for f in mod._SCALARS + ("unique-key",)
                     if f not in ("status", "type"))
ENTRY = {"timezone": "UTC"}


class Reached(Exception):
    """A write got past the gate and addressed the store."""


class FakeCursor:
    """The two reads `set` makes before it writes - the task by reference and the
    raise by id - answered from memory. Every other query is a write, and a write
    arriving here means the gate let it through."""

    def __init__(self, row):
        self.row, self.answer = row, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "task_executions" in sql:
            self.answer = [dict(self.row)] if self.row else []
        elif sql.strip().startswith("select id, project_id from"):
            # Every task the fake holds is this project's, so what is proven
            # below is worker scope and never the project boundary above it.
            self.answer = [{"id": params[0], "project_id": HERE}]
        else:
            raise Reached(sql.strip().split("\n")[0])

    def fetchall(self):
        return self.answer


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.cur

    def commit(self):
        pass


@pytest.fixture
def setting(monkeypatch):
    """`set` run as a worker against the fake reads; the argument is the raise."""
    monkeypatch.setenv("TASKS_EXECUTION", "exec-1")
    monkeypatch.setattr(mod, "PROJECT", HERE)

    def run(args: list[str], row=OPEN):
        monkeypatch.setattr(mod, "_connect", lambda entry: FakeConn(FakeCursor(row)))
        mod.cmd_set(ENTRY, args)

    return run


def _set_refused(setting, capsys, args, row=OPEN) -> str:
    with pytest.raises(SystemExit) as exit_info:
        setting(args, row)
    assert exit_info.value.code == 4
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "policy"
    return error["message"]


@pytest.mark.parametrize("field", PLAIN_FIELDS)
def test_no_field_of_another_task_is_a_workers_to_write(field, setting, capsys):
    assert _set_refused(setting, capsys, [OTHER, f"--{field}", SET_VALUES[field]]) == \
        refusal("set", OTHER)


@pytest.mark.parametrize("field", mod._CLEARABLE)
def test_no_field_of_another_task_is_a_workers_to_clear(field, setting, capsys):
    assert _set_refused(setting, capsys, [OTHER, "--clear", field]) == refusal("set", OTHER)


def test_a_bare_save_on_another_task_is_a_write_too(setting, capsys):
    # It names no field and still moves the task's moment, so it is the holder's.
    assert _set_refused(setting, capsys, [OTHER]) == refusal("set", OTHER)


def test_status_and_type_keep_naming_their_own_rule(setting, capsys):
    assert _set_refused(setting, capsys, [OTHER, "--status", "todo"]) == \
        refusal("status:todo", OTHER)
    assert _set_refused(setting, capsys, [OTHER, "--type", "change"]) == \
        refusal("type", OTHER)


@pytest.mark.parametrize("field", PLAIN_FIELDS)
def test_the_task_it_holds_keeps_every_field_open(field, setting):
    with pytest.raises(Reached):
        setting([HELD, f"--{field}", SET_VALUES[field]])


@pytest.mark.parametrize("field", mod._CLEARABLE)
def test_the_task_it_holds_can_still_be_emptied(field, setting):
    with pytest.raises(Reached):
        setting([HELD, "--clear", field])


@pytest.mark.parametrize("row", OVER, ids=("closed", "swept", "missing"))
def test_a_raise_that_is_over_writes_no_field_on_its_own_former_task(row, setting, capsys):
    assert _set_refused(setting, capsys, [HELD, "--assignee", "x"], row) == \
        refusal("set", HELD, row)
    assert "does not hold" in refusal("set", HELD, row)


def test_nothing_is_a_worker_without_the_variable(setting, monkeypatch):
    monkeypatch.delenv("TASKS_EXECUTION")
    with pytest.raises(Reached):
        setting([OTHER, "--assignee", "x"])


# --- Store: the same rules through the verbs ---------------------------------

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
    # The ledger is scoped by project, so a verb called straight has to stand
    # somewhere the way `main` makes it stand somewhere.
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(mod._schema_ddl(schema))
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _answer(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _refused(capsys, call, *args):
    with pytest.raises(SystemExit) as exit_info:
        call(*args)
    assert exit_info.value.code == 4
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "policy"
    return error["message"]


def _claim(mod_entry, capsys, key: str) -> str:
    mod.cmd_claim(mod_entry, ["--key", key, "--worker", "a worker"])
    return _answer(capsys)["execution"]["id"]


@needs_store
def test_a_worker_writes_inside_its_raise(store, capsys, monkeypatch):
    entry, schema, conn = store
    for key in ("w-held", "w-other"):
        mod.cmd_add(entry, ["--type", "probe", "--title", key, "--key", key,
                            "--status", "todo"])
        assert _answer(capsys)["status"] == "todo"
    execution = _claim(entry, capsys, "w-held")
    monkeypatch.setenv("TASKS_EXECUTION", execution)

    # add lands in draft whatever it was asked for, and says so.
    mod.cmd_add(entry, ["--type", "defect", "--title", "found", "--key", "w-new",
                        "--status", "todo"])
    created = _answer(capsys)
    assert created["status"] == "draft" and created["coerced"] == {"status": "draft"}
    mod.cmd_show(entry, ["w-new"])
    assert _answer(capsys)["task"]["created_by"] == f"execution:{execution}"

    # The task it holds: the four landings, and the other scalars as before.
    for status in ("complete", "draft", "todo"):
        mod.cmd_set(entry, ["w-held", "--status", status])
        assert _answer(capsys)["task"]["status"] == status
    mod.cmd_set(entry, ["w-held", "--assignee", "the owner", "--objective", "why"])
    assert set(_answer(capsys)["fields"]) == {"assignee", "objective"}

    # And the two it does not write there.
    assert "in_progress" in _refused(capsys, mod.cmd_set,
                                     entry, ["w-held", "--status", "in_progress"])
    assert "type" in _refused(capsys, mod.cmd_set, entry, ["w-held", "--type", "change"])
    assert "another task" in _refused(capsys, mod.cmd_set,
                                      entry, ["w-other", "--status", "todo"])
    mod.cmd_show(entry, ["w-other"])
    assert _answer(capsys)["task"]["status"] == "todo"  # the refusal wrote nothing


@needs_store
def test_a_worker_writes_no_field_on_a_task_it_does_not_hold(store, capsys, monkeypatch):
    entry, schema, conn = store
    for key in ("f-held", "f-other"):
        mod.cmd_add(entry, ["--type", "probe", "--title", key, "--key", key,
                            "--status", "todo", "--objective", "as raised",
                            "--assignee", "the owner"])
        capsys.readouterr()
    execution = _claim(entry, capsys, "f-held")
    monkeypatch.setenv("TASKS_EXECUTION", execution)

    for args in (["f-other", "--assignee", "somebody else"],
                 ["f-other", "--objective", "mine now"],
                 ["f-other", "--clear", "assignee"],
                 ["f-other"]):
        assert "does not hold" in _refused(capsys, mod.cmd_set, entry, args)
    mod.cmd_show(entry, ["f-other"])
    other = _answer(capsys)["task"]
    assert other["assignee"] == "the owner" and other["objective"] == "as raised"

    # On the task it holds, the whole verb is open.
    mod.cmd_set(entry, ["f-held", "--assignee", "me", "--clear", "objective"])
    assert set(_answer(capsys)["fields"]) == {"assignee", "objective"}

    # And once the raise is over, its own former task is another task too.
    mod.cmd_release(entry, [execution, "--outcome", "ok"])
    capsys.readouterr()
    assert "does not hold" in _refused(capsys, mod.cmd_set,
                                       entry, ["f-held", "--assignee", "nobody"])


@needs_store
def test_a_worker_annotates_another_task_but_takes_nothing_from_it(store, capsys, monkeypatch):
    entry, schema, conn = store
    for key in ("a-held", "a-other"):
        mod.cmd_add(entry, ["--type", "probe", "--title", key, "--key", key,
                            "--status", "todo"])
        capsys.readouterr()
    mod.cmd_tag(entry, ["a-other", "kept"])
    mod.cmd_meta(entry, ["set", "a-other", "origin", '"a report"'])
    capsys.readouterr()
    execution = _claim(entry, capsys, "a-held")
    monkeypatch.setenv("TASKS_EXECUTION", execution)

    mod.cmd_activity(entry, ["a-other", "looked at this while working the other one"])
    assert _answer(capsys)["task"]
    mod.cmd_tag(entry, ["a-other", "seen"])
    assert _answer(capsys)["tags"] == ["kept", "seen"]
    mod.cmd_meta(entry, ["set", "a-other", "note", "1"])
    assert _answer(capsys)["metadata"] == {"origin": "a report", "note": 1}

    assert "write over" in _refused(capsys, mod.cmd_meta,
                                    entry, ["set", "a-other", "origin", '"mine"'])
    assert "remove metadata" in _refused(capsys, mod.cmd_meta, entry, ["rm", "a-other", "origin"])
    assert "remove a tag" in _refused(capsys, mod.cmd_tag, entry, ["a-other", "kept"], True)
    mod.cmd_meta(entry, ["show", "a-other"])
    assert _answer(capsys)["metadata"] == {"origin": "a report", "note": 1}

    # On the task it holds, all of it is open.
    mod.cmd_tag(entry, ["a-held", "mine"])
    assert _answer(capsys)["tags"] == ["mine"]
    mod.cmd_tag(entry, ["a-held", "mine"], remove=True)
    assert _answer(capsys)["tags"] == []
    mod.cmd_meta(entry, ["set", "a-held", "n", "1"])
    assert _answer(capsys)["metadata"] == {"n": 1}
    mod.cmd_meta(entry, ["set", "a-held", "n", "2"])
    assert _answer(capsys)["metadata"] == {"n": 2}
    mod.cmd_meta(entry, ["rm", "a-held", "n"])
    assert _answer(capsys)["metadata"] == {}


@needs_store
def test_a_raise_that_is_over_leaves_a_worker_no_status_at_all(store, capsys, monkeypatch):
    entry, schema, conn = store
    mod.cmd_add(entry, ["--type", "probe", "--title", "r", "--key", "r-held",
                        "--status", "todo"])
    capsys.readouterr()
    execution = _claim(entry, capsys, "r-held")
    monkeypatch.setenv("TASKS_EXECUTION", execution)
    mod.cmd_release(entry, [execution, "--outcome", "ok"])
    assert _answer(capsys)["task"]["status"] == "complete"

    assert "no longer open" in _refused(capsys, mod.cmd_set,
                                        entry, ["r-held", "--status", "todo"])
    # What is still open is what is open on anybody's task.
    mod.cmd_activity(entry, ["r-held", "wrote this after the raise closed"])
    assert _answer(capsys)["activity"]
    assert "remove a tag" in _refused(capsys, mod.cmd_tag, entry, ["r-held", "x"], True)

    # An id that never named a raise is the same answer.
    monkeypatch.setenv("TASKS_EXECUTION", "00000000-0000-0000-0000-000000000000")
    assert "no longer open" in _refused(capsys, mod.cmd_set,
                                        entry, ["r-held", "--status", "draft"])


@needs_store
def test_claim_and_release_are_outside_the_scope(store, capsys, monkeypatch):
    """A runner sets the variable for the worker it starts, never for itself -
    but a turn that inherited it must still be able to close its own raise."""
    entry, schema, conn = store
    mod.cmd_add(entry, ["--type", "probe", "--title", "c", "--key", "c-one",
                        "--status", "todo"])
    capsys.readouterr()
    execution = _claim(entry, capsys, "c-one")
    monkeypatch.setenv("TASKS_EXECUTION", execution)
    mod.cmd_release(entry, [execution, "--outcome", "failed"])
    released = _answer(capsys)
    assert released["task"]["status"] == "todo" and released["moved"] == ["status"]
    # And claiming again under a stale variable still works.
    again = _claim(entry, capsys, "c-one")
    assert again != execution


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
