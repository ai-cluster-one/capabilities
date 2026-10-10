#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.4.0"]
# ///
"""A task assigned to another project's agent: who may write what on it, who
wrote each entry, and what handing it back does.

A task stays in the project that raised it. Assigned to another project by that
project's id, it may be answered from there - an entry on its trail, or its
assignee - and nothing else. The parsing half is checked without a store. The
store-backed half reads TASKS_TEST_DSN and skips when it is unset; every run
works in a schema of its own and drops it.

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

# The project that raises the task, the one it is assigned to, and a third.
OWNER, AGENT, OTHER = "prj_owner", "prj_agent", "prj_other"


def _error(capsys) -> dict:
    return json.loads(capsys.readouterr().err)["error"]


def _refused(capsys, call, *args) -> dict:
    with pytest.raises(SystemExit) as exit_info:
        call(*args)
    error = json.loads(capsys.readouterr().err)["error"]
    error["exit"] = exit_info.value.code
    return error


# --- Without a store ---------------------------------------------------------

def test_assigned_here_means_the_assignee_is_this_projects_id(monkeypatch):
    monkeypatch.setattr(mod, "PROJECT", AGENT)
    where, params = mod._where({"all-projects": True, "assigned-here": True})
    assert "assignee = %s" in where and params == [AGENT]


def test_assigned_here_is_refused_beside_an_assignee_and_outside_a_project(
        monkeypatch, capsys):
    monkeypatch.setattr(mod, "PROJECT", AGENT)
    with pytest.raises(SystemExit) as exit_info:
        mod._where({"assigned-here": True, "assignee": "someone"})
    assert exit_info.value.code == 6
    assert "--assigned-here" in _error(capsys)["message"]
    monkeypatch.setattr(mod, "PROJECT", None)
    with pytest.raises(SystemExit) as exit_info:
        mod._where({"all-projects": True, "assigned-here": True})
    assert exit_info.value.code == 6 and _error(capsys)["code"] == "no_project"


def test_the_help_states_the_rule_with_its_one_exception_where_it_lives():
    scope = mod.__doc__.split("\nPROJECT SCOPE\n")[1].split("\nWORKER SCOPE\n")[0]
    assert scope.startswith("  Reads cross the project boundary and writes never do, "
                            "with one exception")
    for said in ("exactly as that\n  project declares it",
                 "tasks list --all-projects --assigned-here",
                 'tasks activity <uuid> "..."', "--assignee <who>",
                 "returns it to `todo`", "`origin_project`"):
        assert said in scope, said
    filters = mod.__doc__.split("FILTERS  (")[1].split("PAGING")[0]
    assert "--assigned-here" in filters
    workers = mod.__doc__.split("\nWORKERS\n")[1].split("\nADDRESSING\n")[0]
    assert "written_by_another_project" in workers


def test_the_schema_comment_carries_the_same_exception():
    ddl = mod._TABLES_SHAPE
    assert "with one '\n  'exception: the project a task is assigned to" in ddl


# --- Store -------------------------------------------------------------------

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")


@pytest.fixture
def store(monkeypatch):
    import psycopg

    schema = "tasks_test_" + secrets.token_hex(4)
    entry = {"allow_write": True,
             "timezone": "UTC"}
    _cli.bind_store(mod, monkeypatch, schema)
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.delenv("TASKS_ACTOR", raising=False)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    monkeypatch.setattr(mod, "PROJECT", OWNER)
    with psycopg.connect(DSN, autocommit=True) as conn:
        _cli.make_tables(mod, schema)
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _answer(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def assigned(store, monkeypatch, capsys):
    """One task the owner raised and assigned to the agent's project, waiting on
    it, and the uuid that names it."""
    entry, schema, conn = store
    monkeypatch.setattr(mod, "PROJECT", OWNER)
    mod.cmd_add(entry, ["--type", "request", "--title", "please look", "--key", "ask-1",
                        "--objective", "answer the question", "--status", "waiting",
                        "--assignee", AGENT])
    tid = _answer(capsys)["created"]
    return entry, schema, conn, tid


def _as(monkeypatch, project):
    monkeypatch.setattr(mod, "PROJECT", project)


def _show(entry, capsys, tid, monkeypatch, project=OWNER) -> dict:
    _as(monkeypatch, project)
    mod.cmd_show(entry, [tid])
    return _answer(capsys)


@needs_store
def test_the_assigned_project_can_add_to_the_trail(assigned, capsys, monkeypatch):
    entry, _schema, _conn, tid = assigned
    _as(monkeypatch, AGENT)
    mod.cmd_activity(entry, [tid, "looked: the answer is 42"])
    written = _answer(capsys)
    assert written["activity"] and written["created_at"]
    shown = _show(entry, capsys, tid, monkeypatch)
    [entry_row] = shown["activities"]
    assert entry_row["description"] == "looked: the answer is 42"
    assert entry_row["origin_project"] == AGENT
    # Adding to the trail is not a handback: the task still waits on the agent.
    assert shown["task"]["status"] == "waiting" and shown["task"]["assignee"] == AGENT


@needs_store
def test_handing_a_waiting_task_back_returns_it_to_todo(assigned, capsys, monkeypatch):
    entry, _schema, _conn, tid = assigned
    _as(monkeypatch, AGENT)
    mod.cmd_set(entry, [tid, "--assignee", "the owner"])
    answer = _answer(capsys)
    assert answer["handed_back"] is True
    assert sorted(answer["moved"]) == ["assignee", "status"]
    assert (answer["task"]["status"], answer["task"]["assignee"]) == ("todo", "the owner")
    mod.cmd_history(entry, [tid])
    changes = _answer(capsys)["changes"]
    moved = {c["field"]: c for c in changes if c["origin_project"] == AGENT}
    assert moved["status"]["old_value"] == "waiting"
    assert moved["status"]["new_value"] == "todo"
    assert moved["assignee"]["new_value"] == "the owner"


@needs_store
def test_handing_on_and_clearing_are_the_same_write(assigned, capsys, monkeypatch):
    entry, _schema, _conn, tid = assigned
    _as(monkeypatch, AGENT)
    mod.cmd_set(entry, [tid, "--assignee", OTHER])
    assert _answer(capsys)["task"]["status"] == "todo"
    # Now the third project answers for it, and the agent has no write left.
    _as(monkeypatch, OTHER)
    mod.cmd_set(entry, [tid, "--clear", "assignee"])
    cleared = _answer(capsys)["task"]
    assert cleared["assignee"] is None and cleared["status"] == "todo"


@needs_store
def test_a_task_not_waiting_keeps_its_status(assigned, capsys, monkeypatch):
    entry, schema, conn, tid = assigned
    conn.execute(f"update {schema}.tasks_tasks set status = 'todo' where id = %s", (tid,))
    _as(monkeypatch, AGENT)
    mod.cmd_set(entry, [tid, "--assignee", "the owner"])
    answer = _answer(capsys)
    assert "handed_back" not in answer and answer["moved"] == ["assignee"]
    assert answer["task"]["status"] == "todo"


@needs_store
def test_naming_itself_again_is_no_handback(assigned, capsys, monkeypatch):
    entry, _schema, _conn, tid = assigned
    _as(monkeypatch, AGENT)
    mod.cmd_set(entry, [tid, "--assignee", AGENT])
    answer = _answer(capsys)
    assert "handed_back" not in answer and answer["task"]["status"] == "waiting"


@needs_store
def test_the_owners_own_handback_follows_the_ordinary_rules(assigned, capsys,
                                                           monkeypatch):
    entry, _schema, _conn, tid = assigned
    _as(monkeypatch, OWNER)
    mod.cmd_set(entry, [tid, "--assignee", "someone else"])
    answer = _answer(capsys)
    assert "handed_back" not in answer and answer["task"]["status"] == "waiting"


WRITES = {
    "status": (lambda e, t: mod.cmd_set(e, [t, "--status", "complete"])),
    "title": (lambda e, t: mod.cmd_set(e, [t, "--title", "mine now"])),
    "objective": (lambda e, t: mod.cmd_set(e, [t, "--objective", "something else"])),
    "description": (lambda e, t: mod.cmd_set(e, [t, "--description", "rewritten"])),
    "type": (lambda e, t: mod.cmd_set(e, [t, "--type", "defect"])),
    "pickup": (lambda e, t: mod.cmd_set(e, [t, "--pickup", "2030-01-01"])),
    "clear objective": (lambda e, t: mod.cmd_set(e, [t, "--clear", "objective"])),
    "assignee with a field": (lambda e, t: mod.cmd_set(
        e, [t, "--assignee", "the owner", "--status", "todo"])),
    "assignee with a title": (lambda e, t: mod.cmd_set(
        e, [t, "--assignee", "the owner", "--title", "x"])),
    "bare save": (lambda e, t: mod.cmd_set(e, [t])),
    "tag": (lambda e, t: mod.cmd_tag(e, [t, "mine"])),
    "untag": (lambda e, t: mod.cmd_tag(e, [t, "mine"], True)),
    "meta set": (lambda e, t: mod.cmd_meta(e, ["set", t, "k", "1"])),
    "meta rm": (lambda e, t: mod.cmd_meta(e, ["rm", t, "k"])),
    "claim": (lambda e, t: mod.cmd_claim(e, ["--key", t])),
}


@needs_store
@pytest.mark.parametrize("write", sorted(WRITES))
def test_every_other_write_stays_refused(assigned, capsys, monkeypatch, write):
    entry, schema, conn, tid = assigned
    conn.execute(f"update {schema}.tasks_tasks set status = 'todo', "
                 f"tags = '{{mine}}', metadata = '{{\"k\": 1}}' where id = %s", (tid,))
    before = _show(entry, capsys, tid, monkeypatch)["task"]
    _as(monkeypatch, AGENT)
    error = _refused(capsys, WRITES[write], entry, tid)
    assert error["exit"] == 4 and error["code"] == "policy", write
    assert "may only add to its trail or set its assignee" in error["message"]
    after = _show(entry, capsys, tid, monkeypatch)["task"]
    assert after == before, write


@needs_store
def test_a_release_of_the_owners_raise_stays_refused(assigned, capsys, monkeypatch):
    entry, schema, conn, tid = assigned
    conn.execute(f"update {schema}.tasks_tasks set status = 'todo' where id = %s", (tid,))
    _as(monkeypatch, OWNER)
    mod.cmd_claim(entry, ["--key", tid, "--worker", "owner worker"])
    execution = _answer(capsys)["execution"]["id"]
    _as(monkeypatch, AGENT)
    error = _refused(capsys, mod.cmd_release, entry, [execution, "--outcome", "ok"])
    assert error["exit"] == 4 and error["code"] == "policy"


@needs_store
def test_once_handed_on_the_agent_has_no_write_at_all(assigned, capsys, monkeypatch):
    entry, _schema, _conn, tid = assigned
    _as(monkeypatch, AGENT)
    mod.cmd_set(entry, [tid, "--assignee", "the owner"])
    capsys.readouterr()
    for call, args in ((mod.cmd_activity, [tid, "one more thing"]),
                       (mod.cmd_set, [tid, "--assignee", AGENT])):
        error = _refused(capsys, call, entry, args)
        assert error["exit"] == 4 and error["code"] == "policy"
        assert "a write reaches only the project it runs in" in error["message"]


@needs_store
def test_a_third_project_has_no_write(assigned, capsys, monkeypatch):
    entry, _schema, _conn, tid = assigned
    _as(monkeypatch, OTHER)
    for call, args in ((mod.cmd_activity, [tid, "not mine to answer"]),
                       (mod.cmd_set, [tid, "--assignee", OTHER])):
        error = _refused(capsys, call, entry, args)
        assert error["exit"] == 4 and error["code"] == "policy"


@needs_store
def test_a_key_still_names_nothing_outside_its_own_project(assigned, capsys,
                                                          monkeypatch):
    entry, _schema, _conn, _tid = assigned
    _as(monkeypatch, AGENT)
    for call, args in ((mod.cmd_activity, ["ask-1", "by key"]),
                       (mod.cmd_set, ["ask-1", "--assignee", "the owner"])):
        error = _refused(capsys, call, entry, args)
        assert error["exit"] == 3 and error["code"] == "not_found"


@needs_store
def test_the_agent_finds_what_is_assigned_to_it_in_one_read(assigned, capsys,
                                                            monkeypatch):
    entry, _schema, _conn, tid = assigned
    _as(monkeypatch, OTHER)
    mod.cmd_add(entry, ["--type", "request", "--title", "someone else's",
                        "--assignee", "a person"])
    capsys.readouterr()
    _as(monkeypatch, AGENT)
    mod.cmd_list(entry, ["--all-projects", "--assigned-here"])
    listed = _answer(capsys)
    assert [(t["id"], t["project_id"]) for t in listed["tasks"]] == [(tid, OWNER)]
    mod.cmd_list(entry, ["--all-projects", "--assignee", AGENT])
    assert [t["id"] for t in _answer(capsys)["tasks"]] == [tid]
    mod.cmd_counts(entry, ["--all-projects", "--assigned-here"])
    assert _answer(capsys)["counts"]["waiting"] == 1


@needs_store
def test_every_entry_and_move_says_which_project_wrote_it(assigned, capsys,
                                                         monkeypatch):
    entry, schema, conn, tid = assigned
    _as(monkeypatch, OWNER)
    mod.cmd_activity(entry, [tid, "asked the agent"])
    _as(monkeypatch, AGENT)
    mod.cmd_activity(entry, [tid, "answered"])
    mod.cmd_set(entry, [tid, "--assignee", "the owner"])
    _as(monkeypatch, OWNER)
    mod.cmd_set(entry, [tid, "--status", "complete"])
    capsys.readouterr()
    # A row from before origins were kept: no origin, and read as the task's own.
    conn.execute(f"insert into {schema}.tasks_activities (task_id, description) "
                 f"values (%s, 'from before')", (tid,))

    shown = _show(entry, capsys, tid, monkeypatch)
    assert [(a["description"], a["origin_project"]) for a in shown["activities"]] == [
        ("asked the agent", OWNER), ("answered", AGENT), ("from before", None)]
    mod.cmd_history(entry, [tid])
    origins = [(c["field"], c["new_value"], c["origin_project"])
               for c in _answer(capsys)["changes"]]
    assert ("assignee", "the owner", AGENT) in origins
    assert ("status", "todo", AGENT) in origins
    assert ("status", "complete", OWNER) in origins
    mod.cmd_list(entry, ["--status", "complete", "--activities"])
    [row] = _answer(capsys)["tasks"]
    assert [a["origin_project"] for a in row["activities"]] == [OWNER, AGENT, None]
    # The count and the last entry keep exactly their meaning.
    assert row["activities_count"] == 3
    # The origin comes from the project the command stands in and nowhere else:
    # the verb takes no flag that could name one.
    _as(monkeypatch, AGENT)
    error = _refused(capsys, mod.cmd_activity, entry,
                     [tid, "x", "--origin-project", OWNER])
    assert error["exit"] == 6


@needs_store
def test_a_page_costs_the_same_whatever_its_size(assigned, capsys, monkeypatch):
    """A page costs a fixed number of questions, however many rows it holds."""
    entry, schema, conn, _tid = assigned
    real = mod._connect
    counted = []

    def counting(e):
        c = real(e)
        original = c.cursor

        class Cur:
            def __init__(self, cur):
                self.cur = cur

            def execute(self, sql, *a, **k):
                counted.append(sql)
                return self.cur.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self.cur, name)

            def __enter__(self):
                self.cur.__enter__()
                return self

            def __exit__(self, *exc):
                return self.cur.__exit__(*exc)

        class Conn:
            def cursor(self, *a, **k):
                return Cur(original(*a, **k))

            def __enter__(self):
                c.__enter__()
                return self

            def __exit__(self, *exc):
                return c.__exit__(*exc)

        return Conn()

    monkeypatch.setattr(mod, "_connect", counting)
    sizes = []
    for per in ("1", "9"):
        counted.clear()
        mod.cmd_list(entry, ["--all-projects", "--activities", "--per-page", per])
        capsys.readouterr()
        sizes.append(len(counted))
    assert len(set(sizes)) == 1, sizes


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
