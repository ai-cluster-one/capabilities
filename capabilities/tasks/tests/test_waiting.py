#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2"]
# ///
"""A task that is over to somebody, and what brings it back.

`waiting` is the one status that names a person: the task is with its assignee,
it leaves the queue while it is, and it returns on its own when the thing it was
waiting for is over. The rules that decide all of that are pure, so they are
checked here without a store - the wait, the name a wait needs, and the landing a
handback reaches. The verbs are then driven against fake reads, and the
store-backed checks prove the same rules where they matter: they read
TASKS_TEST_DSN and skip when it is unset, and every run works in a schema of its
own and drops it.

    uv run --with pytest --with 'psycopg[binary]>=3.2' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()
SCHEMA_SQL = (Path(_cli.CAPABILITY_DIR) / "schema.sql").read_text()


def _answer(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _refused(capsys, call, *args) -> dict:
    with pytest.raises(SystemExit) as exit_info:
        call(*args)
    error = json.loads(capsys.readouterr().err)["error"]
    error["exit"] = exit_info.value.code
    return error


# --- The status itself -------------------------------------------------------

def test_waiting_is_declared_everywhere_the_set_is():
    assert "waiting" in mod.STATUSES
    # The store keeps its own copy of the set twice: once for a store being
    # created, and once for one being brought forward. A declaration left behind
    # is a store that refuses a status the CLI writes.
    assert SCHEMA_SQL.count(
        "check (status in ('draft','todo','in_progress','waiting','complete','closed'))") == 2
    assert mod._CONSTRAINTS == (("tasks", "tasks_status_check", "waiting"),)


def test_the_help_says_what_waiting_means():
    # Read with the wrapping taken out: the help is written to a column, and a
    # sentence that says the right thing must not fail for breaking in a new
    # place.
    said = " ".join(mod.__doc__.split())
    for needle in ("`waiting` means the task is over to its assignee",
                   "a task that waits on nobody is a stall with a name missing",
                   "once its pickup moment has passed",
                   "`blocked_by` names has ended",
                   "it lands that task on draft, todo, waiting, complete or closed"):
        assert needle in said
    # And `draft` keeps the one meaning it had.
    assert "`draft` means one thing and only one: nobody has released it yet" in said


# --- The name a wait needs ---------------------------------------------------

def test_a_wait_names_somebody(capsys):
    assert mod._wait_has_a_name("waiting", "the owner") is None
    for empty in (None, "", "   "):
        error = _refused(capsys, mod._wait_has_a_name, "waiting", empty)
        assert error["exit"] == 6 and error["code"] == "input"
        assert error["message"] == (
            "a task that waits on nobody is a stall with a name missing")


def test_every_other_status_needs_no_name():
    for status in mod.STATUSES:
        if status != "waiting":
            assert mod._wait_has_a_name(status, None) is None


# --- What ends a wait --------------------------------------------------------

def waits(**fields) -> dict:
    return {"id": "t-1", "unique_key": "k-1", "status": "waiting",
            "assignee": "the owner", "metadata": {}, "pickup_at": None,
            "due": False, **fields}


def test_a_moment_that_has_passed_ends_it():
    assert mod._wait_over(waits(due=True), set()) == "its pickup moment has passed"


def test_the_last_blocker_ending_ends_it():
    task = waits(metadata={"blocked_by": ["a", "b"]})
    assert mod._wait_over(task, {"a", "b"}) == "every task it was waiting on has ended"
    assert mod._wait_over(task, {"a"}) is None
    assert mod._wait_over(task, set()) is None


def test_waiting_on_neither_waits_on_the_person():
    assert mod._wait_over(waits(), {"a", "b"}) is None
    # A blocked_by that is not a list of names is no list of blockers at all,
    # and an empty one names nothing to have ended.
    for named in (None, "a", {}, [], ["", "  "]):
        assert mod._blocked_by(waits(metadata={"blocked_by": named})) == []
        assert mod._wait_over(waits(metadata={"blocked_by": named}), {"a"}) is None


def test_a_blocker_that_names_no_task_holds_it():
    # Nothing ended it, so it did not end. The conservative answer is the only
    # safe one: the alternative frees a task because a name was mistyped.
    assert mod._wait_over(waits(metadata={"blocked_by": ["gone"]}), {"a"}) is None


# --- The sweep, against fake reads -------------------------------------------

class SweepCursor:
    """The reads and writes `_sweep_waiting` makes, answered from memory."""

    def __init__(self, waiting: list[dict], ended: list[dict] | None = None) -> None:
        self.waiting = [dict(t) for t in waiting]
        self.ended = [dict(r) for r in (ended or [])]
        self.answer: list[dict] = []
        self.asked_for: tuple | None = None
        self.changes: list[tuple] = []
        self.activities: list[tuple] = []

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if "status = 'waiting'" in text:
            assert "for update skip locked" in text
            self.answer = [dict(t) for t in self.waiting]
        elif "status in ('complete','closed')" in text:
            self.asked_for = params
            self.answer = [dict(r) for r in self.ended]
        elif text.startswith("update") and "set status = 'todo'" in text:
            row = next(t for t in self.waiting if str(t["id"]) == params[0])
            moved = {k: v for k, v in row.items() if k != "due"}
            self.answer = [{**moved, "status": "todo"}]
        elif "task_changes" in text:
            self.changes.append(params)
            self.answer = []
        elif "task_activities" in text:
            self.activities.append(params)
            self.answer = []
        else:
            raise AssertionError(f"unexpected query: {text}")

    def fetchall(self):
        return self.answer


def test_the_sweep_returns_a_wait_that_is_over():
    cur = SweepCursor([waits(due=True)])
    [returned] = mod._sweep_waiting(cur)
    assert returned["status"] == "todo"
    # The move says who made it, because nobody did: a change row with neither a
    # raise nor an actor reads as a person's.
    assert cur.changes == [("t-1", "status", "waiting", "todo", None, mod.SWEEP_ACTOR)]
    [(task_id, description, actor)] = cur.activities
    assert (task_id, actor) == ("t-1", mod.SWEEP_ACTOR)
    assert description == "Returned to the queue from waiting: its pickup moment has passed."


def test_the_sweep_returns_a_task_whose_blockers_have_ended():
    cur = SweepCursor([waits(metadata={"blocked_by": ["k-9", "k-8"]})],
                      ended=[{"id": "t-9", "unique_key": "k-9"},
                             {"id": "t-8", "unique_key": "k-8"}])
    [returned] = mod._sweep_waiting(cur)
    assert returned["status"] == "todo"
    assert cur.asked_for == (["k-8", "k-9"], ["k-8", "k-9"])
    assert cur.activities[0][1].endswith("every task it was waiting on has ended.")


def test_the_sweep_leaves_a_task_waiting_on_a_person():
    cur = SweepCursor([waits(), waits(id="t-2", metadata={"blocked_by": ["k-9"]})])
    assert mod._sweep_waiting(cur) == []
    assert cur.changes == [] and cur.activities == []


def test_the_sweep_asks_nothing_when_nothing_waits():
    cur = SweepCursor([])
    assert mod._sweep_waiting(cur) == []
    assert cur.asked_for is None


# --- Release, against fake reads ---------------------------------------------

EXECUTION = {"id": "exec-1", "task_id": "t-1", "status": "running", "worker": "a worker"}


class ReleaseCursor:
    """The reads and writes `release` makes, answered from memory."""

    def __init__(self, task: dict) -> None:
        self.task = dict(task)
        self.answer: list[dict] = []
        self.changes: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if "task_executions where id::text" in text:
            self.answer = [dict(EXECUTION)]
        elif text.startswith("update") and "task_executions" in text:
            self.answer = [{**EXECUTION, "status": params[0], "ended_at": "now"}]
        elif text.startswith("select * from") and "tasks where id" in text:
            self.answer = [dict(self.task)]
        elif text.startswith("update") and "tasks set status" in text:
            self.task["status"] = params[0]
            self.answer = [dict(self.task)]
        elif "task_changes" in text:
            self.changes.append(params)
            self.answer = []
        else:
            raise AssertionError(f"unexpected query: {text}")

    def fetchall(self):
        return self.answer


class ReleaseConn:
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
def releasing(monkeypatch):
    def run(task: dict, args: list[str]) -> dict:
        cur = ReleaseCursor(task)
        monkeypatch.setattr(mod, "_connect", lambda entry: ReleaseConn(cur))
        mod.cmd_release({"timezone": "UTC"}, ["exec-1", *args])
        return cur
    return run


def test_a_handback_lands_the_task_with_its_assignee(releasing, capsys):
    cur = releasing({"id": "t-1", "status": "in_progress", "assignee": "the owner",
                     "pickup_at": None, "type": "defect"}, ["--outcome", "handback"])
    answer = _answer(capsys)
    assert mod._OUTCOMES["handback"] == "waiting"
    assert answer["task"]["status"] == "waiting" and answer["moved"] == ["status"]
    assert "instead_of_waiting" not in answer
    assert cur.changes[0][1:4] == ("status", "in_progress", "waiting")


def test_a_handback_on_a_task_naming_nobody_lands_in_draft(releasing, capsys):
    for nobody in (None, "  "):
        cur = releasing({"id": "t-1", "status": "in_progress", "assignee": nobody,
                         "pickup_at": None, "type": "defect"}, ["--outcome", "handback"])
        answer = _answer(capsys)
        assert answer["task"]["status"] == "draft"
        # Said out loud: a runner cannot invent whom the task waits for, and a
        # caller told `handback` would otherwise believe it landed in waiting.
        assert "names no assignee" in answer["instead_of_waiting"]


def test_status_still_overrides_the_landing(releasing, capsys):
    releasing({"id": "t-1", "status": "in_progress", "assignee": "the owner",
               "pickup_at": None, "type": "defect"},
              ["--outcome", "handback", "--status", "todo"])
    assert _answer(capsys)["task"]["status"] == "todo"
    # And an override naming `waiting` answers to the same rule as the outcome.
    releasing({"id": "t-1", "status": "in_progress", "assignee": None,
               "pickup_at": None, "type": "defect"},
              ["--outcome", "ok", "--status", "waiting"])
    landed = _answer(capsys)
    assert landed["task"]["status"] == "draft" and landed["instead_of_waiting"]


# --- What a scan says about who is waited on ---------------------------------

def test_list_groups_the_waiting_tasks_it_carries():
    rows = [{"id": "t-1", "unique_key": "k-1", "status": "waiting", "assignee": "the owner"},
            {"id": "t-2", "unique_key": None, "status": "waiting", "assignee": "a reporter"},
            {"id": "t-3", "unique_key": "k-3", "status": "waiting", "assignee": "the owner"},
            {"id": "t-4", "unique_key": "k-4", "status": "todo", "assignee": "the owner"}]
    assert mod._waiting_on(rows) == {"the owner": ["k-1", "k-3"], "a reporter": ["t-2"]}
    assert mod._waiting_on([rows[3]]) == {}


# --- Store: the same rules through the verbs ---------------------------------

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")

OLD_CHECK = "check (status in ('draft','todo','in_progress','complete','closed'))"
NEW_CHECK = "check (status in ('draft','todo','in_progress','waiting','complete','closed'))"


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
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def seed(entry, capsys, key: str, **fields) -> None:
    args = ["--type", "probe", "--title", key, "--key", key]
    for flag, value in fields.items():
        args += [f"--{flag}", value]
    mod.cmd_add(entry, args)
    capsys.readouterr()


@needs_store
def test_the_store_takes_a_waiting_task_and_the_verbs_move_it(store, capsys):
    entry, schema, conn = store
    seed(entry, capsys, "w-1", status="todo")
    mod.cmd_set(entry, ["w-1", "--status", "waiting", "--assignee", "the owner"])
    moved = _answer(capsys)
    assert moved["task"]["status"] == "waiting"
    assert set(moved["moved"]) == {"status", "assignee"}
    mod.cmd_set(entry, ["w-1", "--status", "todo"])
    assert _answer(capsys)["task"]["status"] == "todo"


@needs_store
def test_a_wait_with_no_name_never_reaches_the_store(store, capsys):
    entry, schema, conn = store
    seed(entry, capsys, "w-2", status="todo")
    error = _refused(capsys, mod.cmd_set, entry, ["w-2", "--status", "waiting"])
    assert error["exit"] == 6 and "stall with a name missing" in error["message"]
    mod.cmd_show(entry, ["w-2"])
    assert _answer(capsys)["task"]["status"] == "todo"

    # Named in the same call, it is permitted; emptied afterwards, it is not.
    mod.cmd_set(entry, ["w-2", "--status", "waiting", "--assignee", "the owner"])
    capsys.readouterr()
    error = _refused(capsys, mod.cmd_set, entry, ["w-2", "--clear", "assignee"])
    assert "stall with a name missing" in error["message"]
    mod.cmd_show(entry, ["w-2"])
    assert _answer(capsys)["task"]["assignee"] == "the owner"

    # And `add` cannot create one either.
    error = _refused(capsys, mod.cmd_add, entry,
                     ["--type", "probe", "--title", "x", "--status", "waiting"])
    assert "stall with a name missing" in error["message"]


@needs_store
def test_ready_and_claim_both_pass_a_waiting_task_over(store, capsys):
    entry, schema, conn = store
    seed(entry, capsys, "w-3", status="waiting", assignee="the owner")
    seed(entry, capsys, "w-4", status="todo")

    mod.cmd_ready(entry, [])
    assert [t["unique_key"] for t in _answer(capsys)["tasks"]] == ["w-4"]

    # By status and not by any moment: it has no pickup at all.
    error = _refused(capsys, mod.cmd_claim, entry, ["--key", "w-3"])
    assert error["exit"] == 6 and "is waiting, not todo" in error["message"]
    assert "over to its assignee" in error["hint"]
    mod.cmd_claim(entry, ["--worker", "a worker"])
    assert _answer(capsys)["task"]["unique_key"] == "w-4"
    mod.cmd_claim(entry, ["--worker", "a worker"])
    assert _answer(capsys)["claimed"] is None


@needs_store
def test_a_handback_hands_the_task_to_its_assignee(store, capsys):
    entry, schema, conn = store
    seed(entry, capsys, "w-5", status="todo", assignee="the owner")
    seed(entry, capsys, "w-6", status="todo")

    mod.cmd_claim(entry, ["--key", "w-5", "--worker", "a worker"])
    mod.cmd_release(entry, [_answer(capsys)["execution"]["id"], "--outcome", "handback"])
    assert _answer(capsys)["task"]["status"] == "waiting"

    mod.cmd_claim(entry, ["--key", "w-6", "--worker", "a worker"])
    mod.cmd_release(entry, [_answer(capsys)["execution"]["id"], "--outcome", "handback"])
    unnamed = _answer(capsys)
    assert unnamed["task"]["status"] == "draft" and unnamed["instead_of_waiting"]


@needs_store
def test_the_sweep_returns_what_the_store_says_is_over(store, capsys):
    entry, schema, conn = store
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    seed(entry, capsys, "s-1", status="waiting", assignee="the owner", pickup=past)
    seed(entry, capsys, "s-2", status="waiting", assignee="the owner")
    seed(entry, capsys, "s-3", status="waiting", assignee="the owner")
    seed(entry, capsys, "s-blocker", status="todo")
    mod.cmd_meta(entry, ["set", "s-3", "blocked_by", '["s-blocker"]'])
    capsys.readouterr()

    # Nothing is claimable yet, and the claim that finds nothing still returns
    # the one whose moment has passed.
    mod.cmd_claim(entry, ["--type", "nothing"])
    assert _answer(capsys)["returned"] == ["s-1"]
    mod.cmd_show(entry, ["s-1"])
    shown = _answer(capsys)
    assert shown["task"]["status"] == "todo"
    assert shown["activities"][-1]["description"] == (
        "Returned to the queue from waiting: its pickup moment has passed.")
    assert shown["activities"][-1]["actor"] == mod.SWEEP_ACTOR
    mod.cmd_history(entry, ["s-1", "--field", "status"])
    [move] = _answer(capsys)["changes"]
    assert (move["old_value"], move["new_value"], move["actor"]) == (
        "waiting", "todo", mod.SWEEP_ACTOR)

    # The blocked one comes back when what it named is over, and not before.
    mod.cmd_claim(entry, ["--type", "nothing"])
    assert _answer(capsys)["returned"] == []
    mod.cmd_set(entry, ["s-blocker", "--status", "complete"])
    capsys.readouterr()
    mod.cmd_claim(entry, ["--type", "nothing"])
    assert _answer(capsys)["returned"] == ["s-3"]

    # And the one waiting on a person is still waiting on that person.
    mod.cmd_show(entry, ["s-2"])
    still = _answer(capsys)
    assert still["task"]["status"] == "waiting" and still["activities"] == []


@needs_store
def test_a_returned_task_is_taken_by_the_same_claim(store, capsys):
    entry, schema, conn = store
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    seed(entry, capsys, "s-4", status="waiting", assignee="the owner", pickup=past)
    mod.cmd_claim(entry, ["--worker", "a worker"])
    claimed = _answer(capsys)
    assert claimed["returned"] == ["s-4"]
    assert claimed["task"]["unique_key"] == "s-4"
    assert claimed["task"]["status"] == "in_progress"


@needs_store
def test_list_reports_who_is_waited_on(store, capsys):
    entry, schema, conn = store
    seed(entry, capsys, "l-1", status="waiting", assignee="the owner")
    seed(entry, capsys, "l-2", status="waiting", assignee="a reporter")
    seed(entry, capsys, "l-3", status="todo")
    mod.cmd_list(entry, [])
    answer = _answer(capsys)
    assert answer["waiting_on"] == {"the owner": ["l-1"], "a reporter": ["l-2"]}
    # Every key a caller already reads is still there.
    assert set(answer) >= {"pagination", "tasks"}
    assert len(answer["tasks"]) == 3


@needs_store
def test_migrate_reports_the_constraint_a_store_is_behind_on(store, capsys, monkeypatch):
    from psycopg.rows import dict_row
    entry, schema, conn = store
    # A store created before the status existed: the check it was created with.
    conn.execute(f"alter table {schema}.tasks drop constraint tasks_status_check")
    conn.execute(f"alter table {schema}.tasks add constraint tasks_status_check "
                 f"{OLD_CHECK}")
    with conn.cursor(row_factory=dict_row) as cur:
        tables = mod._tables_present(cur, schema)
        assert mod._constraints_behind(cur, schema, tables) == ["tasks.tasks_status_check"]
        # A table that is absent is reported as a table, not as its constraints.
        assert mod._constraints_behind(cur, schema, []) == []

    mod.cmd_migrate(entry, [])
    reported = _answer(capsys)
    assert reported["would_add"] == ["tasks.tasks_status_check"]
    assert reported["applied"] is False

    mod.cmd_migrate(entry, ["--apply"])
    applied = _answer(capsys)
    assert applied["added"] == ["tasks.tasks_status_check"]
    assert applied["applied"] is True

    # Applied, the store takes the status; and running it again reports nothing.
    seed(entry, capsys, "m-1", status="waiting", assignee="the owner")
    mod.cmd_migrate(entry, [])
    assert _answer(capsys)["would_add"] == []


@needs_store
def test_a_store_that_predates_the_status_refuses_it_legibly(store, capsys):
    entry, schema, conn = store
    conn.execute(f"alter table {schema}.tasks drop constraint tasks_status_check")
    conn.execute(f"alter table {schema}.tasks add constraint tasks_status_check {OLD_CHECK}")
    seed(entry, capsys, "m-2", status="todo")
    error = _refused(capsys, mod._guarded, mod.cmd_set, entry,
                     ["m-2", "--status", "waiting", "--assignee", "the owner"])
    assert error["exit"] == 6 and error["code"] == "input"
    assert "waiting" in error["hint"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
