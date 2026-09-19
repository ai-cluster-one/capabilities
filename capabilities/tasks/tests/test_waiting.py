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
                   "it lands that task on draft, todo, waiting, complete or closed",
                   "A wait begins clean, because a hold from before the wait "
                   "does not say when the wait is over",
                   "Appoint the moment in the same call"):
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


# --- The hold a wait must not inherit ----------------------------------------

class Ended:
    """The one read a settlement makes of the store: which of the names a task
    is waiting on belong to a task that has already ended."""

    def __init__(self, ended: list[dict] | None = None) -> None:
        self.ended = [dict(r) for r in (ended or [])]
        self.asked_for: tuple | None = None
        self.answer: list[dict] = []

    def execute(self, sql, params=None):
        assert "status in ('complete','closed')" in " ".join(sql.split())
        self.asked_for = params
        self.answer = [dict(r) for r in self.ended]

    def fetchall(self):
        return self.answer


def test_a_wait_drops_a_pickup_written_before_it():
    # Whatever that moment was for, it was not this wait, and left in place the
    # next claim reads it as the wait being over.
    cur = Ended()
    assert mod._settle_the_wait(cur, "waiting", waits(pickup_at="then"), False) == (
        ["pickup_at = null"], [])
    # Nothing to drop is nothing written.
    assert mod._settle_the_wait(cur, "waiting", waits(), False) == ([], [])


def test_a_moment_appointed_in_the_same_call_stands():
    assert mod._settle_the_wait(Ended(), "waiting", waits(pickup_at="then"), True) == ([], [])


def test_no_other_landing_settles_anything():
    for status in mod.STATUSES:
        if status != "waiting":
            assert mod._settle_the_wait(
                Ended(), status, waits(pickup_at="then"), False) == ([], [])


def test_a_spent_blocked_by_is_dropped():
    task = waits(metadata={"blocked_by": ["k-9", "k-8"]})
    cur = Ended([{"id": "t-9", "unique_key": "k-9"}, {"id": "t-8", "unique_key": "k-8"}])
    assert mod._settle_the_wait(cur, "waiting", task, True) == (
        ["metadata = metadata - 'blocked_by'"], ["k-9", "k-8"])
    assert cur.asked_for == (["k-8", "k-9"], ["k-8", "k-9"])


def test_a_blocked_by_with_one_task_still_open_is_kept():
    task = waits(metadata={"blocked_by": ["k-9", "k-8"]})
    half = Ended([{"id": "t-9", "unique_key": "k-9"}])
    assert mod._settle_the_wait(half, "waiting", task, True) == ([], [])
    # And a name matching no task has not ended here either, exactly as in the
    # sweep: an unknown blocker holds the wait rather than clearing it away.
    assert mod._settle_the_wait(Ended(), "waiting", task, True) == ([], [])
    # A task naming no blockers asks the store nothing.
    bare = Ended()
    assert mod._settle_the_wait(bare, "waiting", waits(), True) == ([], [])
    assert bare.asked_for is None


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


def landed(cur, text: str) -> None:
    """Apply to the fake task whatever the write settled without a parameter.

    Both settlements are literals in the SQL rather than values, which is what
    lets a fake prove they were written at all."""
    if "pickup_at = null" in text:
        cur.task["pickup_at"] = None
    if "metadata - 'blocked_by'" in text:
        cur.task["metadata"] = {k: v for k, v in (cur.task.get("metadata") or {}).items()
                                if k != "blocked_by"}


class ReleaseCursor:
    """The reads and writes `release` makes, answered from memory."""

    def __init__(self, task: dict, ended: list[dict] | None = None) -> None:
        self.task = dict(task)
        self.ended = [dict(r) for r in (ended or [])]
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
        elif "status in ('complete','closed')" in text:
            self.answer = [dict(r) for r in self.ended]
        elif text.startswith("update") and "tasks set status" in text:
            self.task["status"] = params[0]
            landed(self, text)
            self.answer = [dict(self.task)]
        elif "task_changes" in text:
            self.changes.append(params)
            self.answer = []
        else:
            raise AssertionError(f"unexpected query: {text}")

    def fetchall(self):
        return self.answer


class SetCursor:
    """The reads and writes `set` makes, answered from memory."""

    def __init__(self, task: dict, ended: list[dict] | None = None) -> None:
        self.task = dict(task)
        self.ended = [dict(r) for r in (ended or [])]
        self.answer: list[dict] = []
        self.changes: list[tuple] = []
        self.written = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if "where id::text = %s or unique_key" in text:
            self.answer = [{"id": self.task["id"]}]
        elif "status in ('complete','closed')" in text:
            self.answer = [dict(r) for r in self.ended]
        elif text.startswith("select * from") and "tasks where id" in text:
            self.answer = [dict(self.task)]
        elif text.startswith("update") and "tasks set" in text:
            self.written = text
            body = text.split(" set ", 1)[1].split(" where ", 1)[0]
            named = [one.split(" = ")[0] for one in body.split(", ") if one.endswith("= %s")]
            for column, value in zip(named, params):
                self.task[column] = value
            landed(self, body)
            self.answer = [dict(self.task)]
        elif "task_executions" in text:
            self.answer = []
        elif "task_changes" in text:
            self.changes.append(params)
            self.answer = []
        else:
            raise AssertionError(f"unexpected query: {text}")

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
def releasing(monkeypatch):
    def run(task: dict, args: list[str], ended: list[dict] | None = None) -> ReleaseCursor:
        cur = ReleaseCursor(task, ended)
        monkeypatch.setattr(mod, "_connect", lambda entry: FakeConn(cur))
        mod.cmd_release({"timezone": "UTC"}, ["exec-1", *args])
        return cur
    return run


@pytest.fixture
def setting(monkeypatch):
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)

    def run(task: dict, args: list[str], ended: list[dict] | None = None) -> SetCursor:
        cur = SetCursor(task, ended)
        monkeypatch.setattr(mod, "_connect", lambda entry: FakeConn(cur))
        mod.cmd_set({"timezone": "UTC"}, [str(task["id"]), *args])
        return cur
    return run


def held(**fields) -> dict:
    return {"id": "t-1", "unique_key": "k-1", "status": "todo", "assignee": None,
            "type": "probe", "metadata": {}, "pickup_at": None, **fields}


THEN = "2026-09-18T09:00:00+00:00"


def test_set_drops_a_hold_from_before_the_wait(setting, capsys):
    cur = setting(held(pickup_at=THEN), ["--status", "waiting", "--assignee", "the owner"])
    answer = _answer(capsys)
    assert answer["task"]["status"] == "waiting" and answer["task"]["pickup_at"] is None
    # The drop is a field move like any other, so `history` carries it and a
    # reader sees what the wait let go of.
    assert answer["moved"] == ["status", "pickup", "assignee"]
    assert [c[1:4] for c in cur.changes if c[1] == "pickup"] == [("pickup", THEN, None)]


def test_set_keeps_a_moment_appointed_in_the_same_call(setting, capsys):
    cur = setting(held(pickup_at=THEN),
                  ["--status", "waiting", "--assignee", "the owner",
                   "--pickup", "2026-12-01T09:00:00+00:00"])
    answer = _answer(capsys)
    assert answer["task"]["pickup_at"].startswith("2026-12-01")
    assert "pickup_at = null" not in cur.written


def test_set_drops_a_spent_blocked_by_and_says_so(setting, capsys):
    cur = setting(held(metadata={"blocked_by": ["k-9"], "cost_total": "1.5"}),
                  ["--status", "waiting", "--assignee", "the owner"],
                  ended=[{"id": "t-9", "unique_key": "k-9"}])
    answer = _answer(capsys)
    assert answer["blocked_by_spent"] == ["k-9"]
    # That key and nothing else: the rest of the metadata is not this verb's.
    assert answer["task"]["metadata"] == {"cost_total": "1.5"}
    assert cur.changes and all(c[1] != "pickup" for c in cur.changes)


def test_set_keeps_a_blocked_by_still_naming_an_open_task(setting, capsys):
    setting(held(metadata={"blocked_by": ["k-9"]}),
            ["--status", "waiting", "--assignee", "the owner"])
    answer = _answer(capsys)
    assert "blocked_by_spent" not in answer
    assert answer["task"]["metadata"] == {"blocked_by": ["k-9"]}


def test_set_settles_nothing_on_a_landing_that_is_not_a_wait(setting, capsys):
    cur = setting(held(pickup_at=THEN, metadata={"blocked_by": ["k-9"]}),
                  ["--assignee", "the owner"],
                  ended=[{"id": "t-9", "unique_key": "k-9"}])
    answer = _answer(capsys)
    assert answer["task"]["pickup_at"] == THEN
    assert answer["task"]["metadata"] == {"blocked_by": ["k-9"]}
    assert "pickup_at = null" not in cur.written


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


def test_a_handback_drops_a_hold_from_before_the_wait(releasing, capsys):
    cur = releasing({"id": "t-1", "status": "in_progress", "assignee": "the owner",
                     "pickup_at": THEN, "type": "defect", "metadata": {}},
                    ["--outcome", "handback"])
    answer = _answer(capsys)
    assert answer["task"]["status"] == "waiting" and answer["task"]["pickup_at"] is None
    assert answer["moved"] == ["status", "pickup"]
    assert [c[1:4] for c in cur.changes if c[1] == "pickup"] == [("pickup", THEN, None)]


def test_a_handback_drops_a_spent_blocked_by_and_keeps_a_live_one(releasing, capsys):
    releasing({"id": "t-1", "status": "in_progress", "assignee": "the owner",
               "pickup_at": None, "type": "defect", "metadata": {"blocked_by": ["k-9"]}},
              ["--outcome", "handback"], ended=[{"id": "t-9", "unique_key": "k-9"}])
    spent = _answer(capsys)
    assert spent["blocked_by_spent"] == ["k-9"] and spent["task"]["metadata"] == {}

    releasing({"id": "t-1", "status": "in_progress", "assignee": "the owner",
               "pickup_at": None, "type": "defect", "metadata": {"blocked_by": ["k-9"]}},
              ["--outcome", "handback"])
    kept = _answer(capsys)
    assert "blocked_by_spent" not in kept
    assert kept["task"]["metadata"] == {"blocked_by": ["k-9"]}


def test_a_landing_that_is_not_a_wait_keeps_the_hold(releasing, capsys):
    # Completed, the task is over and nothing about it is a wait.
    releasing({"id": "t-1", "status": "in_progress", "assignee": "the owner",
               "pickup_at": THEN, "type": "defect", "metadata": {"blocked_by": ["k-9"]}},
              ["--outcome", "ok"], ended=[{"id": "t-9", "unique_key": "k-9"}])
    done = _answer(capsys)
    assert done["task"]["pickup_at"] == THEN
    assert done["task"]["metadata"] == {"blocked_by": ["k-9"]}

    # And a handback the store could hand to nobody lands in `draft`, which is
    # backlog carrying a hold rather than a wait beginning.
    releasing({"id": "t-1", "status": "in_progress", "assignee": None,
               "pickup_at": THEN, "type": "defect", "metadata": {}},
              ["--outcome", "handback"])
    drafted = _answer(capsys)
    assert drafted["task"]["status"] == "draft" and drafted["task"]["pickup_at"] == THEN


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
def test_a_gate_stop_survives_the_claim_that_follows_it(store, capsys):
    entry, schema, conn = store
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    seed(entry, capsys, "g-1", status="todo", pickup=past)
    seed(entry, capsys, "g-2", status="todo")
    seed(entry, capsys, "g-blocker", status="todo")
    mod.cmd_meta(entry, ["set", "g-2", "blocked_by", '["g-blocker"]'])
    mod.cmd_set(entry, ["g-blocker", "--status", "complete"])
    capsys.readouterr()

    # A hold from before the wait is dropped on the way in, and the drop is a
    # field move like any other.
    mod.cmd_set(entry, ["g-1", "--status", "waiting", "--assignee", "the owner"])
    stopped = _answer(capsys)
    assert stopped["task"]["status"] == "waiting" and stopped["task"]["pickup_at"] is None
    assert "pickup" in stopped["moved"]
    mod.cmd_history(entry, ["g-1", "--field", "pickup"])
    [dropped] = _answer(capsys)["changes"]
    assert dropped["new_value"] is None and dropped["old_value"]

    # A blocked_by with nothing left to wait on goes with it, said out loud.
    mod.cmd_set(entry, ["g-2", "--status", "waiting", "--assignee", "the owner"])
    spent = _answer(capsys)
    assert spent["blocked_by_spent"] == ["g-blocker"]
    assert "blocked_by" not in spent["task"]["metadata"]

    # The next claim is where a stop used to evaporate. Both are still waiting.
    mod.cmd_claim(entry, ["--type", "nothing"])
    assert _answer(capsys)["returned"] == []
    for key in ("g-1", "g-2"):
        mod.cmd_show(entry, [key])
        assert _answer(capsys)["task"]["status"] == "waiting"


@needs_store
def test_a_handback_hands_over_a_wait_that_begins_clean(store, capsys):
    entry, schema, conn = store
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    seed(entry, capsys, "g-3", status="todo", assignee="the owner", pickup=past)
    mod.cmd_claim(entry, ["--key", "g-3", "--worker", "a worker"])
    mod.cmd_release(entry, [_answer(capsys)["execution"]["id"], "--outcome", "handback"])
    handed = _answer(capsys)
    assert handed["task"]["status"] == "waiting" and handed["task"]["pickup_at"] is None
    assert "pickup" in handed["moved"]

    mod.cmd_claim(entry, ["--type", "nothing"])
    assert _answer(capsys)["returned"] == []
    mod.cmd_show(entry, ["g-3"])
    assert _answer(capsys)["task"]["status"] == "waiting"


@needs_store
def test_a_wait_keeps_what_was_chosen_for_it(store, capsys):
    entry, schema, conn = store
    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    seed(entry, capsys, "g-4", status="todo")
    seed(entry, capsys, "g-5", status="todo")
    seed(entry, capsys, "g-open", status="todo")
    mod.cmd_meta(entry, ["set", "g-5", "blocked_by", '["g-open"]'])
    capsys.readouterr()

    # A moment appointed in the same call was chosen for this wait, so it stands.
    mod.cmd_set(entry, ["g-4", "--status", "waiting", "--assignee", "the owner",
                        "--pickup", soon])
    appointed = _answer(capsys)
    assert appointed["task"]["pickup_at"] is not None

    # A blocker still open is a live reason to wait, and the key is untouched.
    mod.cmd_set(entry, ["g-5", "--status", "waiting", "--assignee", "the owner"])
    kept = _answer(capsys)
    assert kept["task"]["metadata"]["blocked_by"] == ["g-open"]
    assert "blocked_by_spent" not in kept

    mod.cmd_claim(entry, ["--type", "nothing"])
    assert _answer(capsys)["returned"] == []

    # And the wait still ends the way it did: when the task it named is over.
    mod.cmd_set(entry, ["g-open", "--status", "complete"])
    capsys.readouterr()
    mod.cmd_claim(entry, ["--type", "nothing"])
    assert _answer(capsys)["returned"] == ["g-5"]


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
