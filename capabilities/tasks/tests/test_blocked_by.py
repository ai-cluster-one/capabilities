#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.3.0",
#                 "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""`blocked_by` as a field of its own: a column of task ids, validated when it is
written, honoured on every open task by the claim, and moved out of the metadata
it used to live in.

What the schema declares is checked with no store. Writing, claiming, the scan,
the backfill and the compatibility kept for one release are driven against a
real store with the harness replaced. The store-backed checks read
TASKS_TEST_DSN and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'capabilities-contract==0.3.0' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402
import test_escalation as esc  # noqa: E402
import test_hooks as hooks  # noqa: E402
from test_hooks import project, store, turns  # noqa: E402,F401  (fixtures)

mod = hooks.mod
needs_store = hooks.needs_store
add, answer, shown = hooks.add, hooks.answer, hooks.shown
HERE, THERE = hooks.HERE, "prj_elsewhere"
NOWHERE = "00000000-0000-4000-8000-000000000000"


def refused(capsys, verb, entry, args, code: int = 4) -> dict:
    with pytest.raises(SystemExit) as stopped:
        verb(entry, args)
    assert stopped.value.code == code, capsys.readouterr()
    return json.loads(capsys.readouterr().err)["error"]


def set_(entry, capsys, *args) -> dict:
    mod.cmd_set(entry, list(args))
    return answer(capsys)


def claim(entry, capsys, *args) -> dict:
    mod.cmd_claim(entry, ["--worker", "a worker", *args])
    return answer(capsys)


def theirs(entry, capsys, monkeypatch, key: str, status: str = "todo") -> str:
    """A task of another project sharing the store, by its id."""
    monkeypatch.setattr(mod, "PROJECT", THERE)
    mod.cmd_add(entry, ["--type", "alpha", "--title", key, "--key", key, "--status", status])
    tid = answer(capsys)["created"]
    monkeypatch.setattr(mod, "PROJECT", HERE)
    return tid


# --- The typed home ----------------------------------------------------------

def test_the_tables_declare_the_column():
    assert "  blocked_by   uuid[]      not null default '{}'," in mod._TABLES_SHAPE


def test_the_help_states_the_field():
    said = " ".join(mod.__doc__.split())
    for needle in ("--blocked-by TASK[,TASK...]",
                   "Clearable: objective, description, assignee, pickup, unique-key, "
                   "blocked-by",
                   "No task is claimable while its `blocked_by` lists a task that has "
                   "not ended, in `todo` as much as in `waiting`",
                   "`blocked_by` is no metadata key",
                   "both stop taking the key in the next release"):
        assert needle in said, needle


# --- Writing it --------------------------------------------------------------

@needs_store
def test_blockers_are_written_by_id_from_keys_and_ids(store, capsys, monkeypatch):
    entry, schema, conn = store
    first = add(entry, capsys, "b-first")
    second = add(entry, capsys, "b-second")
    add(entry, capsys, "b-task")
    elsewhere = theirs(entry, capsys, monkeypatch, "b-there")
    # Keys resolve within the project, ids in any; each is kept once, in order.
    written = set_(entry, capsys, "b-task", "--blocked-by",
                   f"b-first, {second},b-first,{elsewhere}")
    assert written["task"]["blocked_by"] == [first, second, elsewhere]
    assert written["fields"] == ["blocked_by"]
    # `set` names the whole list.
    assert set_(entry, capsys, "b-task", "--blocked-by", "b-second")["task"]["blocked_by"] \
        == [second]
    assert set_(entry, capsys, "b-task", "--clear", "blocked-by")["task"]["blocked_by"] == []
    # `add` takes it too.
    mod.cmd_add(entry, ["--type", "alpha", "--title", "x", "--key", "b-new",
                        "--blocked-by", "b-first"])
    answer(capsys)
    assert shown(entry, capsys, "b-new")["task"]["blocked_by"] == [first]


@needs_store
def test_a_name_that_is_no_task_here_is_refused(store, capsys, monkeypatch):
    entry, schema, conn = store
    add(entry, capsys, "b-task")
    theirs(entry, capsys, monkeypatch, "b-there")
    for name in ("b-nowhere", NOWHERE, "b-there"):
        error = refused(capsys, mod.cmd_set, entry, ["b-task", "--blocked-by", name])
        assert error["code"] == "policy"
        assert error["message"] == (f"a blocked_by may not name blockers that can never "
                                    f"all end: {name!r} names no task (a key names a task "
                                    f"only in {HERE})")
    error = refused(capsys, mod.cmd_add, entry, ["--type", "alpha", "--title", "x",
                                                 "--blocked-by", "b-nowhere"])
    assert "'b-nowhere' names no task" in error["message"]
    assert shown(entry, capsys, "b-task")["task"]["blocked_by"] == []


@needs_store
def test_itself_and_a_cycle_are_refused(store, capsys):
    entry, schema, conn = store
    add(entry, capsys, "b-a")
    add(entry, capsys, "b-b")
    add(entry, capsys, "b-c")
    error = refused(capsys, mod.cmd_set, entry, ["b-a", "--blocked-by", "b-a"])
    assert error["message"].endswith(": 'b-a' names itself")
    set_(entry, capsys, "b-a", "--blocked-by", "b-b")
    set_(entry, capsys, "b-b", "--blocked-by", "b-c")
    error = refused(capsys, mod.cmd_set, entry, ["b-c", "--blocked-by", "b-a"])
    assert error["code"] == "policy"
    assert error["message"].endswith("they wait on it in turn: b-c -> b-a -> b-b -> b-c")
    assert shown(entry, capsys, "b-c")["task"]["blocked_by"] == []
    # Through a blocker that has ended there is no cycle: it holds nothing.
    set_(entry, capsys, "b-b", "--status", "complete")
    assert set_(entry, capsys, "b-c", "--blocked-by", "b-a")["task"]["blocked_by"]


@needs_store
def test_the_flag_is_read_strictly(store, capsys):
    entry, schema, conn = store
    add(entry, capsys, "b-a")
    add(entry, capsys, "b-b")
    error = refused(capsys, mod.cmd_set, entry, ["b-a", "--blocked-by", " , "], 6)
    assert "--clear blocked-by" in error["hint"]
    error = refused(capsys, mod.cmd_set, entry, ["b-a", "--blocked-by", "b-b",
                                                 "--clear", "blocked-by"], 6)
    assert "cannot be given together" in error["message"]


# --- Honoured on every open task ---------------------------------------------

@needs_store
def test_a_todo_task_with_an_open_blocker_is_offered_to_no_claim(store, capsys, project,
                                                                 turns):
    entry, schema, conn = store
    add(entry, capsys, "b-blocker")
    set_(entry, capsys, "b-blocker", "--assignee", "the owner", "--status", "waiting")
    add(entry, capsys, "b-blocked")
    set_(entry, capsys, "b-blocked", "--blocked-by", "b-blocker")
    assert claim(entry, capsys)["claimed"] is None
    mod.cmd_run(entry, ["alpha"])
    assert answer(capsys)["would_claim"] is None
    # Named with --key it is refused, naming why.
    error = refused(capsys, mod.cmd_claim, entry, ["--worker", "a worker", "--key",
                                                   "b-blocked"], 6)
    assert "waits on the tasks its blocked_by names" in error["message"]
    # Nor is it in the queue a worker consumes.
    mod.cmd_ready(entry, [])
    assert [row["unique_key"] for row in answer(capsys)["tasks"]] == []
    # Once every blocker has ended it is taken, and the ended blocker stays listed.
    set_(entry, capsys, "b-blocker", "--status", "complete")
    mod.cmd_ready(entry, [])
    assert [row["unique_key"] for row in answer(capsys)["tasks"]] == ["b-blocked"]
    taken = claim(entry, capsys)
    assert taken["task"]["unique_key"] == "b-blocked"
    blocker = shown(entry, capsys, "b-blocker")["task"]["id"]
    assert shown(entry, capsys, "b-blocked")["task"]["blocked_by"] == [blocker]


@needs_store
def test_a_closed_blocker_holds_nothing_and_stays_recorded(store, capsys):
    entry, schema, conn = store
    one = add(entry, capsys, "b-one")
    two = add(entry, capsys, "b-two")
    set_(entry, capsys, "b-one", "--assignee", "the owner", "--status", "waiting")
    set_(entry, capsys, "b-two", "--assignee", "the owner", "--status", "waiting")
    add(entry, capsys, "b-after")
    set_(entry, capsys, "b-after", "--blocked-by", "b-one,b-two")
    set_(entry, capsys, "b-one", "--status", "closed")
    # One open blocker still holds it.
    assert claim(entry, capsys)["claimed"] is None
    set_(entry, capsys, "b-two", "--status", "complete")
    assert claim(entry, capsys)["task"]["unique_key"] == "b-after"
    assert shown(entry, capsys, "b-after")["task"]["blocked_by"] == [one, two]


@needs_store
def test_a_waiting_task_returns_on_its_own_when_its_blockers_end(store, capsys,
                                                                 monkeypatch):
    entry, schema, conn = store
    here = add(entry, capsys, "b-here")
    elsewhere = theirs(entry, capsys, monkeypatch, "b-there")
    add(entry, capsys, "b-wait")
    set_(entry, capsys, "b-wait", "--blocked-by", f"b-here,{elsewhere}",
         "--status", "waiting", "--assignee", "the owner")
    set_(entry, capsys, "b-here", "--status", "complete")
    assert claim(entry, capsys)["returned"] == []
    # Another project's blocker, named by id, ends the wait as well.
    conn.execute(f"update {schema}.tasks_tasks set status = 'complete' where id = %s",
                 (elsewhere,))
    returned = claim(entry, capsys)
    assert returned["returned"] == ["b-wait"]
    task = shown(entry, capsys, "b-wait")["task"]
    assert task["blocked_by"] == [here, elsewhere]


# --- Dead ends read the column -----------------------------------------------

@needs_store
def test_a_todo_task_blocked_by_a_draft_is_escalated(project, store, turns, capsys):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    mod.cmd_add(entry, ["--type", "alpha", "--title", "d", "--key", "b-draft"])
    answer(capsys)
    add(entry, capsys, "b-held")
    set_(entry, capsys, "b-held", "--blocked-by", "b-draft")
    mod.cmd_run(entry, ["alpha", "--apply"])
    [moved] = answer(capsys)["escalated"]
    assert moved["task"] == "b-held"
    assert moved["why"] == "its blockers can never all end: 'b-draft' is a draft, which " \
                           "only a person releases"
    task = shown(entry, capsys, "b-held")["task"]
    assert (task["status"], task["blocked_by"]) == ("waiting", [])


# --- Compatibility for one release -------------------------------------------

@needs_store
def test_meta_set_writes_the_field_and_says_it_is_deprecated(store, capsys):
    entry, schema, conn = store
    first = add(entry, capsys, "c-first")
    add(entry, capsys, "c-task")
    mod.cmd_meta(entry, ["set", "c-task", "blocked_by", '["c-first"]', "note", "kept"])
    out = capsys.readouterr()
    said = json.loads(out.out)
    assert said["blocked_by"] == [first]
    assert said["metadata"] == {"note": "kept", "blocked_by": [first]}
    assert "set <task> --blocked-by" in said["deprecated"][0]
    assert json.loads(out.err)["deprecated"] == said["deprecated"]
    stored = conn.execute(f"select blocked_by, metadata from {schema}.tasks_tasks "
                          f"where unique_key = 'c-task'").fetchone()
    assert [str(one) for one in stored[0]] == [first] and stored[1] == {"note": "kept"}
    # The same refusals as `set --blocked-by`.
    error = refused(capsys, mod.cmd_meta, entry, ["set", "c-task", "blocked_by",
                                                  '["c-nowhere"]'])
    assert "'c-nowhere' names no task" in error["message"]
    error = refused(capsys, mod.cmd_meta, entry, ["set", "c-task", "blocked_by",
                                                  '"c-first"'], 6)
    assert "takes a list of tasks" in error["message"]
    # `meta rm` empties it.
    mod.cmd_meta(entry, ["rm", "c-task", "blocked_by"])
    removed = json.loads(capsys.readouterr().out)
    assert removed["blocked_by"] == [] and removed["deprecated"]
    assert shown(entry, capsys, "c-task")["task"]["blocked_by"] == []


@needs_store
def test_the_metadata_mirrors_the_field_for_one_release(store, capsys):
    entry, schema, conn = store
    first = add(entry, capsys, "c-first")
    add(entry, capsys, "c-task")
    set_(entry, capsys, "c-task", "--blocked-by", "c-first")
    task = shown(entry, capsys, "c-task")["task"]
    assert task["blocked_by"] == [first] and task["metadata"] == {"blocked_by": [first]}
    mod.cmd_meta(entry, ["show", "c-task"])
    assert answer(capsys)["metadata"] == {"blocked_by": [first]}
    mod.cmd_list(entry, ["--full"])
    rows = {row["unique_key"]: row for row in answer(capsys)["tasks"]}
    assert rows["c-task"]["metadata"] == {"blocked_by": [first]}
    assert rows["c-task"]["blocked_by_status"] == {first: "todo"}
    assert rows["c-first"]["metadata"] == {}
    # Names a backfill could not move are shown as stored, not hidden.
    conn.execute(f"""update {schema}.tasks_tasks set metadata = '{{"blocked_by": ["c-gone"]}}'
                      where unique_key = 'c-task'""")
    assert shown(entry, capsys, "c-task")["task"]["metadata"] == {"blocked_by": ["c-gone"]}


@needs_store
def test_a_worker_writes_blockers_under_the_metadata_scope(store, capsys, project,
                                                           monkeypatch):
    """A worker file that lets a turn write `blocked_by` on another task names it
    under `metadata`, as it did when blockers were a metadata key."""
    entry, schema, conn = store
    add(entry, capsys, "w-held")
    add(entry, capsys, "w-other")
    add(entry, capsys, "w-gate")
    add(entry, capsys, "w-gate-2")
    hooks.write_worker(project, "alpha", "takes: [alpha]\nprofile: plain\n"
                       "writes: {held: {status: [todo]}, other: {metadata: [blocked_by]}}")
    mod.cmd_claim(entry, ["--worker", "alpha", "--key", "w-held"])
    execution = answer(capsys)["execution"]["id"]
    conn.execute(f"""update {schema}.tasks_executions
                        set metrics = jsonb_build_object('worker_writes', %s::jsonb)
                      where id = %s""",
                 (json.dumps({"held": {"status": ["todo"]},
                              "other": {"metadata": ["blocked_by"]}}), execution))
    monkeypatch.setenv("TASKS_EXECUTION", execution)
    assert set_(entry, capsys, "w-other", "--blocked-by", "w-gate")["task"]["blocked_by"]
    assert set_(entry, capsys, "w-other", "--blocked-by", "w-gate-2")["task"]["blocked_by"]
    assert set_(entry, capsys, "w-other", "--clear", "blocked-by")["task"]["blocked_by"] == []
    # Without it, a worker may add blockers to a task that has none, and no more.
    conn.execute(f"""update {schema}.tasks_executions set metrics = '{{}}'::jsonb
                      where id = %s""", (execution,))
    assert set_(entry, capsys, "w-other", "--blocked-by", "w-gate")["task"]["blocked_by"]
    error = refused(capsys, mod.cmd_set, entry, ["w-other", "--blocked-by", "w-gate-2"])
    assert "write over a metadata key" in error["message"]
    error = refused(capsys, mod.cmd_set, entry, ["w-other", "--clear", "blocked-by"])
    assert "remove metadata" in error["message"]


def test_a_frame_that_lets_a_turn_write_blockers_says_how(project):
    hooks.write_worker(project, "probe", "takes: [alpha]\nprofile: plain\n"
                       "writes: {held: {status: [todo]}, other: {metadata: [blocked_by]}}")
    prompt = mod._prompt(mod._worker("probe"), {"id": "id-1", "metadata": {}}, [], 1, None)
    assert "`tasks set <task> --blocked-by <task>[,<task>...]`" in prompt
    hooks.write_worker(project, "probe", "takes: [alpha]\nprofile: plain\n"
                       "writes: {held: {status: [todo]}}")
    prompt = mod._prompt(mod._worker("probe"), {"id": "id-1", "metadata": {}}, [], 1, None)
    assert "--blocked-by" not in prompt
