#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.4.0",
#                 "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""What a worker file declares about itself: its description, what it takes and
what its turn may write.

The reading and the decisions are pure and are checked over a project written
into a temp directory, with no store. The store-backed checks prove selection,
binding and settlement through the verbs, read TASKS_TEST_DSN, and skip when it
is unset; every run works in a schema of its own and drops it.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'capabilities-contract==0.4.0' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
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

PROFILE = """harness = "claude"
model = "claude-opus-5"
timeout_seconds = 1800
"""

HERE = "prj_worker_declared"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with one profile and no workers of its own yet."""
    envelope = tmp_path / "capabilities" / "tasks"
    (envelope / "workers").mkdir(parents=True)
    (envelope / "profiles").mkdir()
    (envelope / "profiles" / "plain.toml").write_text(PROFILE)
    monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda root: root / "capabilities")
    monkeypatch.setattr(mod, "_project_env", dict)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-machine"))
    return tmp_path


def write_worker(project: Path, name: str, front: str, body: str = "the body.") -> Path:
    path = project / "capabilities" / "tasks" / "workers" / f"{name}.md"
    path.write_text(f"---\n{front.strip()}\n---\n\n{body}\n")
    return path


def problems_of(name: str) -> list[str]:
    _rows, broken = mod._workers_report()
    return [one for one in broken if one.startswith(f"{name}:")]


def row_of(name: str) -> dict:
    rows, _broken = mod._workers_report()
    return next(row for row in rows if row["worker"] == name)


IMPLEMENTATION = "takes: [defect, change]\nprofile: plain"
SUPERVISOR = """
takes:
  status: [waiting]
  assignee: [supervisor]
profile: plain
"""


# --- Description ----------------------------------------------------------------

def test_a_description_is_read_shown_and_heads_the_frame(project):
    write_worker(project, "probe", "description: Fixes what is reported broken.\n"
                 + IMPLEMENTATION, body="THE BODY")
    worker = mod._worker("probe")
    assert worker["description"] == "Fixes what is reported broken."
    assert row_of("probe")["description"] == "Fixes what is reported broken."
    prompt = mod._prompt(worker, {"id": "id-1", "type": "defect", "metadata": {}},
                         [], 1, None)
    assert prompt.startswith("Fixes what is reported broken.\n\n")
    assert prompt.index("Fixes what is reported broken.") < prompt.index("You are the worker")
    assert prompt.index("You are the worker") < prompt.index("THE BODY")


def test_a_worker_without_a_description_has_the_frame_it_always_had(project):
    write_worker(project, "probe", IMPLEMENTATION)
    worker = mod._worker("probe")
    assert worker["description"] is None and "description" not in row_of("probe")
    prompt = mod._prompt(worker, {"id": "id-1", "type": "defect", "metadata": {}},
                         [], 1, None)
    assert prompt.startswith("You are the worker `probe`")


def test_a_description_that_is_not_a_sentence_is_refused(project):
    write_worker(project, "probe", "description: [a, b]\n" + IMPLEMENTATION)
    assert any("`description` is one short sentence" in one for one in problems_of("probe"))


# --- What a worker takes ------------------------------------------------------

def test_the_list_of_types_is_one_filter_of_todo(project):
    write_worker(project, "probe", IMPLEMENTATION)
    worker = mod._worker("probe")
    assert worker["filters"] == [{"status": ["todo"], "type": ["defect", "change"],
                                  "assignee": None, "tags": []}]
    assert worker["types"] == ["defect", "change"] and worker["shorthand"] is True
    assert row_of("probe")["takes"] == ["defect", "change"]
    assert mod._asks(worker) == [{"cool_down": 60, "type": "defect"},
                                 {"cool_down": 60, "type": "change"}]


def test_one_filter_map_and_its_status_default(project):
    write_worker(project, "probe", "takes: {type: [chore], assignee: [ops]}\nprofile: plain")
    worker = mod._worker("probe")
    assert worker["filters"] == [{"status": ["todo"], "type": ["chore"],
                                  "assignee": ["ops"], "tags": []}]
    assert mod._asks(worker) == [{"assignees": ["ops"], "cool_down": 60, "type": "chore"}]
    assert row_of("probe")["takes"] == [{"status": ["todo"], "type": ["chore"],
                                         "assignee": ["ops"]}]


def test_a_list_of_filter_maps_is_asked_in_order(project):
    write_worker(project, "probe", """
takes:
  - {type: [defect], status: [todo, waiting]}
  - {status: [waiting], assignee: [supervisor], tags: [urgent]}
profile: plain
""")
    worker = mod._worker("probe")
    assert worker["types"] == ["defect"]
    # Every ask carries the cool-down it is read against.
    assert mod._asks(worker) == [
        {"statuses": ["todo", "waiting"], "cool_down": 60, "type": "defect"},
        {"tag": ["urgent"], "assignees": ["supervisor"], "statuses": ["waiting"],
         "cool_down": 60}]


def test_top_level_tags_and_assignee_mean_the_same_inside_every_filter(project):
    write_worker(project, "probe", "takes: [chore]\ntags: [inside]\nassignee: ops\n"
                 "profile: plain")
    [one] = mod._worker("probe")["filters"]
    assert one == {"status": ["todo"], "type": ["chore"], "assignee": ["ops"],
                   "tags": ["inside"]}
    write_worker(project, "probe", "takes: {type: [chore]}\ntags: [inside]\nprofile: plain")
    assert mod._worker("probe")["filters"][0]["tags"] == ["inside"]
    write_worker(project, "probe", "takes: {type: [chore], tags: [x]}\ntags: [inside]\n"
                 "profile: plain")
    assert any("`tags` both at the top and inside `takes`" in one
               for one in problems_of("probe"))


@pytest.mark.parametrize("takes, said", [
    ("{type: [chore], priority: [high]}", "'priority', which nothing reads"),
    ("{status: [draft]}", "takes a task in todo or waiting, not draft"),
    ("{type: chore}", "its `type` is a list of one or more values"),
    ("[{type: [a]}, b]", "a list of names, one filter map, or a list of filter maps"),
])
def test_a_filter_that_cannot_be_read_is_refused(project, takes, said):
    write_worker(project, "probe", f"takes: {takes}\nprofile: plain")
    assert any(said in one for one in problems_of("probe")), problems_of("probe")


def test_a_filter_without_a_type_names_no_type_to_the_default(project):
    write_worker(project, "probe", SUPERVISOR)
    assert mod._types_named(project / "capabilities/tasks/workers/probe.md") == set()
    write_worker(project, "probe", "takes: [{type: [a]}, {type: [b], status: [waiting]}]")
    assert mod._types_named(project / "capabilities/tasks/workers/probe.md") == {"a", "b"}


def test_a_named_task_is_accepted_by_status_and_type(project):
    write_worker(project, "probe", SUPERVISOR)
    opts = {"key": "k", "worker": "probe", **mod._key_types(mod._worker("probe"))}
    waiting = {"type": "chore", "status": "waiting", "metadata": {}, "pickup_at": None}
    assert mod._key_refusal(waiting, True, opts) is None
    said, _hint = mod._key_refusal({**waiting, "status": "todo"}, True, opts)
    assert said == "k is todo, not waiting"
    said, _hint = mod._key_refusal(waiting, True, opts, None, ["t-1"])
    assert "waits on the tasks its blocked_by names" in said
    write_worker(project, "probe", IMPLEMENTATION)
    opts = {"key": "k", "worker": "probe", **mod._key_types(mod._worker("probe"))}
    said, _hint = mod._key_refusal(waiting, True, opts)
    assert said == "k is waiting, not todo"


# --- One task, one worker -----------------------------------------------------

def test_an_implementation_and_a_supervisor_do_not_overlap(project):
    write_worker(project, "implementation",
                 "takes: {type: [defect, change], status: [todo]}\nprofile: plain")
    write_worker(project, "supervisor", SUPERVISOR)
    assert mod._workers_report()[1] == []


def test_two_filters_that_can_select_one_task_are_refused(project):
    write_worker(project, "one", "takes: {status: [waiting]}\nprofile: plain")
    write_worker(project, "two", SUPERVISOR)
    [said] = mod._workers_report()[1]
    assert "one and two could both take the same task" in said


def test_one_type_with_waiting_in_both_is_refused_in_the_words_it_always_had(project):
    write_worker(project, "one", "takes: {type: [defect], status: [todo, waiting]}\n"
                 "profile: plain")
    write_worker(project, "two", "takes: {type: [defect, change], status: [waiting]}\n"
                 "profile: plain")
    assert any("one and two both take 'defect'" in said for said in mod._workers_report()[1])


@pytest.mark.parametrize("one, two", [
    ("takes: {type: [defect], assignee: [ann]}", "takes: {type: [defect], assignee: [bob]}"),
    ("takes: {type: [defect], status: [todo]}", "takes: {type: [defect], status: [waiting]}"),
    ("takes: [defect]\ntags: [inside]", "takes: [defect]\ntags: [outside]"),
])
def test_filters_split_by_status_assignee_or_tags_are_accepted(project, one, two):
    write_worker(project, "one", f"{one}\nprofile: plain")
    write_worker(project, "two", f"{two}\nprofile: plain")
    assert mod._workers_report()[1] == []


def test_a_todo_filter_naming_no_type_overlaps_the_default(project):
    write_worker(project, "probe", "takes: {assignee: [ops]}\nprofile: plain")
    assert any("default and probe could both take the same task" in said
               for said in mod._workers_report()[1])
    write_worker(project, "default", "enabled: false")
    assert mod._workers_report()[1] == []


def test_a_disabled_worker_overlaps_nothing(project):
    write_worker(project, "one", "enabled: false\ntakes: {status: [waiting]}\nprofile: plain")
    write_worker(project, "two", SUPERVISOR)
    assert mod._workers_report()[1] == []


# --- The shipped supervisor -----------------------------------------------------

SHIPPED = mod._shipped_workers_dir() / "supervisor.md"


def test_the_shipped_supervisor_is_there_switched_off(project):
    row = row_of("supervisor")
    assert row["source"] == "shipped" and row["enabled"] is False and row["ok"] is True
    assert row["description"].startswith("Acts on behalf of this project's owner")
    assert row["takes"] == [{"status": ["waiting"], "assignee": ["supervisor"]}]
    assert row["writes"] == {
        "held": {"status": ["todo", "waiting", "complete", "closed"], "assignee": "*"},
        "other": {"status": ["todo", "waiting"], "assignee": "*", "metadata": ["blocked_by"]},
        "new": {"status": ["todo", "waiting"], "type": ["defect", "change"],
                "assignee": "*"},
    }


def test_the_shipped_supervisor_switched_on_beside_the_default_is_clean(project):
    path = project / "capabilities" / "tasks" / "workers" / "supervisor.md"
    path.write_text(SHIPPED.read_text().replace("enabled: false", "enabled: true"))
    rows, broken = mod._workers_report()
    assert broken == []
    by_name = {row["worker"]: row for row in rows}
    assert by_name["supervisor"]["enabled"] is True and by_name["default"]["enabled"] is True
    assert mod._worker("supervisor")["writes"]["held"]["assignee"] == "*"



# --- What a turn may write ------------------------------------------------------

HELD, OTHER = "task-held", "task-other"
WORKER = {"TASKS_EXECUTION": "exec-1"}
OPEN = {"id": "exec-1", "task_id": HELD, "status": "running"}

DECLARED = {
    "held": {"status": ["todo", "complete"], "type": ["change"], "assignee": ["ann"],
             "pickup": "*", "metadata": ["stage"], "tags": ["hot"]},
    "other": {"status": ["waiting"], "type": "*", "assignee": "*",
              "metadata": "*", "tags": "*"},
    "new": {"status": ["todo"], "type": ["defect"]},
}


def judged(op: str, target: str, writes=DECLARED):
    return mod._worker_refusal(op, target, lambda execution: OPEN, WORKER, writes)


@pytest.mark.parametrize("op, allowed, refused", [
    ("status", "todo", "waiting"),
    ("type", "change", "defect"),
    ("assignee", "ann", "bob"),
    ("untag", "hot", "cold"),
    ("meta-rm", "stage", "origin"),
    ("meta-overwrite", "stage", "origin"),
])
def test_a_listed_value_is_allowed_on_the_held_task_and_another_refused(op, allowed, refused):
    assert judged(f"{op}:{allowed}", HELD) is None
    said = judged(f"{op}:{refused}", HELD)
    assert said and said.startswith("a worker may not ")
    assert "its worker file's `writes.held." in said and refused in said


@pytest.mark.parametrize("op", ["status:todo", "type:x", "assignee:x", "pickup",
                                "untag:x", "meta-rm:x", "meta-overwrite:x"])
def test_star_allows_any_value(op):
    star = {"held": {field: "*" for field in mod._WRITE_FIELDS}, "other": {}, "new": {}}
    assert judged(op, HELD, star) is None
    listed = {"held": {field: ["a", "*"] for field in mod._WRITE_FIELDS if field != "pickup"},
              "other": {}, "new": {}}
    if op != "pickup":
        assert judged(op, HELD, listed) is None


@pytest.mark.parametrize("op", ["status:waiting", "type:x", "assignee:x", "pickup",
                                "untag:x", "meta-rm:x", "meta-overwrite:x"])
def test_an_unlisted_field_is_refused(op):
    nothing = {"held": {}, "other": {}, "new": {}}
    said = judged(op, HELD, nothing)
    assert said and ("does not list" in said or "type" in said)
    said = judged(op, OTHER, nothing)
    assert said and ("does not list" in said or "type" in said)


def test_the_other_scope_answers_for_every_other_task():
    assert judged("status:waiting", OTHER) is None
    assert "`writes.other.status` allows only waiting" in judged("status:todo", OTHER)
    for op in ("type:any", "assignee:anyone", "untag:x", "meta-rm:x", "meta-overwrite:x"):
        assert judged(op, OTHER) is None
    assert "`writes.other` does not list pickup" in judged("pickup", OTHER)
    # Fields `writes` does not name stay the holder's alone.
    assert judged("set", OTHER) == mod._WORKER_HELD_ONLY["set"]
    assert judged("set", HELD) is None


def test_clearing_a_field_needs_star():
    assert "clear the assignee" in judged("assignee:", HELD)
    assert judged("assignee:", OTHER) is None


def test_adding_is_allowed_on_every_task_under_any_writes():
    nothing = {"held": {}, "other": {}, "new": {}}
    for op in mod._WORKER_ADDITIVE:
        for target in (HELD, OTHER):
            assert mod._worker_refusal(op, target, lambda e: pytest.fail("asked"),
                                       WORKER, nothing) is None


def test_a_raise_that_is_over_holds_nothing_whatever_it_was_bound_to():
    closed = {**OPEN, "status": "ok"}
    lookup = lambda execution: closed  # noqa: E731
    assert "no longer open" in mod._worker_refusal("status:waiting", OTHER, lookup,
                                                   WORKER, DECLARED)
    assert "does not hold" in mod._worker_refusal("assignee:x", OTHER, lookup,
                                                  WORKER, DECLARED)


def test_the_gate_reads_the_writes_bound_to_the_raise(capsys, monkeypatch):
    monkeypatch.setenv("TASKS_EXECUTION", "exec-1")
    bound = {**OPEN, "metrics": {mod._BOUND_WRITES: DECLARED}}
    mod._worker_gate("status:waiting", OTHER, lambda e: bound)  # permitted: no exit
    with pytest.raises(SystemExit) as exit_info:
        mod._worker_gate("status:closed", OTHER, lambda e: bound)
    assert exit_info.value.code == 4
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "policy" and "`writes.other.status`" in error["message"]
    # Unbound, the default fence answers in its own words.
    with pytest.raises(SystemExit):
        mod._worker_gate("status:waiting", OTHER, lambda e: OPEN)
    assert "move another task to waiting" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_the_default_writes_are_the_fence_spelled_out():
    assert mod.DEFAULT_WRITES == {
        "held": {"status": ["draft", "todo", "waiting", "complete", "closed"],
                 "assignee": "*", "pickup": "*", "metadata": "*", "tags": "*"},
        "other": {},
        "new": {"status": ["draft"], "type": "*", "assignee": "*", "pickup": "*"},
    }
    # Declared exactly, it permits exactly what the unbound fence permits.
    for op in ("status:draft", "status:in_progress", "status:waiting", "type:x",
               "assignee:x", "assignee:", "pickup", "untag:x", "meta-rm:x",
               "meta-overwrite:x", "set"):
        for target in (HELD, OTHER):
            assert ((judged(op, target, mod.DEFAULT_WRITES) is None)
                    == (judged(op, target, None) is None)), (op, target)


@pytest.mark.parametrize("front, said", [
    ("writes: true", "`writes` is a mapping"),
    ("writes: {mine: {}}", "'mine', which nothing reads"),
    ("writes: {held: {stage: [a]}}", "`writes.held` names 'stage'"),
    ("writes: {new: {tags: [a]}}", "`writes.new` names 'tags'"),
    ("writes: {held: {status: true}}", "is a list of values"),
    ("writes: {held: {status: [in_progress]}}", "names in_progress"),
    ("writes: {held: {pickup: [tomorrow]}}", "pickup` is \"*\""),
])
def test_writes_that_cannot_be_read_are_refused(project, front, said):
    write_worker(project, "probe", f"{IMPLEMENTATION}\n{front}")
    assert any(said in one for one in problems_of("probe")), problems_of("probe")


def test_the_frame_says_what_the_declared_writes_allow(project):
    write_worker(project, "probe", IMPLEMENTATION + """
writes:
  held: {status: [todo], assignee: "*"}
  other: {metadata: [blocked_by]}
""")
    prompt = mod._prompt(mod._worker("probe"), {"id": "id-1", "metadata": {}}, [], 1, None)
    assert "`writes.held` allows: status `todo`; assignee any value." in prompt
    assert ("`writes.other` allows: writing over or removing the metadata keys "
            "`blocked_by`.") in prompt
    assert "`writes.new` allows: nothing." in prompt
    assert "you land it on `draft`, `todo`" not in prompt


# --- Store: selection, binding and settlement through the verbs -----------------

DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")


@pytest.fixture
def store(project, monkeypatch):
    import psycopg

    schema = "tasks_test_" + secrets.token_hex(4)
    entry = {"allow_write": True}
    _cli.bind_store(mod, monkeypatch, schema)
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.delenv("TASKS_ACTOR", raising=False)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        _cli.make_tables(mod, schema)
        try:
            yield entry
        finally:
            conn.execute(f"drop schema {schema} cascade")


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def add(entry, capsys, key, kind="defect", status="todo", **fields):
    args = ["--type", kind, "--title", key, "--key", key, "--status", status]
    for flag, value in fields.items():
        args += [f"--{flag}", value]
    mod.cmd_add(entry, args)
    return _out(capsys)["created"]


def refused(capsys, call, *args) -> str:
    with pytest.raises(SystemExit) as exit_info:
        call(*args)
    assert exit_info.value.code == 4
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "policy"
    return error["message"]


def taken(entry, name: str) -> str | None:
    answer = mod._take(entry, mod._worker(name), None)
    return answer["task"]["unique_key"] if answer.get("claimed") else None


@needs_store
def test_each_form_selects_exactly_what_it_describes(project, store, capsys):
    entry = store
    # The workers first: a task is written only where a worker that is on takes it.
    write_worker(project, "default", "enabled: false")
    write_worker(project, "implementation", IMPLEMENTATION)
    write_worker(project, "supervisor", SUPERVISOR)
    write_worker(project, "ops", "takes: [{type: [chore], assignee: [ops]}]\nprofile: plain")
    add(entry, capsys, "d-todo")
    add(entry, capsys, "c-todo", kind="change")
    add(entry, capsys, "chore-ops", kind="chore", assignee="ops")
    add(entry, capsys, "w-owner", status="waiting", assignee="the owner")
    add(entry, capsys, "w-later", status="waiting", assignee="supervisor",
        pickup="2999-01-01")
    add(entry, capsys, "w-blocked", status="waiting", assignee="supervisor")
    mod.cmd_meta(entry, ["set", "w-blocked", "blocked_by", '["d-todo"]'])
    capsys.readouterr()
    add(entry, capsys, "w-super", kind="change", status="waiting", assignee="supervisor")

    # The supervisor takes the one task waiting on its name and nothing else.
    assert taken(entry, "supervisor") == "w-super"
    assert taken(entry, "supervisor") is None
    # The list of types takes todo only, defects first; waiting is never its.
    assert taken(entry, "implementation") == "d-todo"
    assert taken(entry, "implementation") == "c-todo"
    assert taken(entry, "implementation") is None
    assert taken(entry, "ops") == "chore-ops"
    mod.cmd_list(entry, ["--status", "waiting"])
    left = sorted(row["unique_key"] for row in _out(capsys)["tasks"])
    assert left == ["w-blocked", "w-later", "w-owner"]


@needs_store
def test_writes_are_fixed_at_the_claim(project, store, capsys, monkeypatch):
    entry = store
    path = write_worker(project, "supervisor", SUPERVISOR + """
writes:
  held: {status: [todo, waiting, complete, closed], assignee: "*"}
  other: {status: [todo, waiting], assignee: "*", metadata: [blocked_by]}
""")
    add(entry, capsys, "held", status="waiting", assignee="supervisor")
    add(entry, capsys, "other", status="waiting", assignee="someone")
    answer = mod._take(entry, mod._worker("supervisor"), None)
    execution = answer["execution"]
    assert execution["metrics"][mod._BOUND_WRITES]["other"]["status"] == ["todo", "waiting"]
    assert execution["metrics"][mod._CLAIMED_FROM] == "waiting"
    # The file loses its `writes` while the turn runs: the raise keeps them.
    path.write_text(path.read_text().split("writes:")[0] + "---\n\nthe body.\n")
    assert mod._worker("supervisor")["writes"] is None
    monkeypatch.setenv("TASKS_EXECUTION", str(execution["id"]))
    mod.cmd_set(entry, ["other", "--status", "todo", "--assignee", "ops"])
    assert _out(capsys)["task"]["status"] == "todo"
    assert "`writes.other.status` allows only todo, waiting" in refused(
        capsys, mod.cmd_set, entry, ["other", "--status", "complete"])
    assert "`writes.held` does not list pickup" in refused(
        capsys, mod.cmd_set, entry, ["held", "--pickup", "2999-01-01"])

    # And the other way round: a raise claimed without writes keeps the default
    # fence after the file gains them.
    monkeypatch.delenv("TASKS_EXECUTION")
    add(entry, capsys, "plain", kind="chore")
    add(entry, capsys, "plain-other", kind="chore", status="waiting", assignee="x")
    write_worker(project, "chores", "takes: [chore]\nprofile: plain")
    answer = mod._take(entry, mod._worker("chores"), None)
    assert mod._BOUND_WRITES not in (answer["execution"]["metrics"] or {})
    write_worker(project, "chores", "takes: [chore]\nprofile: plain\n"
                 "writes: {other: {status: '*'}}")
    monkeypatch.setenv("TASKS_EXECUTION", str(answer["execution"]["id"]))
    assert "move another task to todo" in refused(
        capsys, mod.cmd_set, entry, ["plain-other", "--status", "todo"])


@needs_store
def test_new_tasks_are_held_to_the_new_scope(project, store, capsys, monkeypatch):
    entry = store
    write_worker(project, "supervisor", SUPERVISOR + """
writes:
  new: {status: [todo, waiting], type: [defect, change]}
""")
    add(entry, capsys, "held", status="waiting", assignee="supervisor")
    execution = mod._take(entry, mod._worker("supervisor"), None)["execution"]["id"]
    monkeypatch.setenv("TASKS_EXECUTION", str(execution))
    mod.cmd_add(entry, ["--type", "defect", "--title", "t", "--status", "todo"])
    created = _out(capsys)
    assert created["status"] == "todo" and "coerced" not in created
    mod.cmd_add(entry, ["--type", "change", "--title", "t", "--status", "complete"])
    created = _out(capsys)
    assert created["status"] == "draft" and created["coerced"] == {"status": "draft"}
    assert "`writes.new.type` allows only defect, change" in refused(
        capsys, mod.cmd_add, entry, ["--type", "chore", "--title", "t"])
    assert "`writes.new` does not list assignee" in refused(
        capsys, mod.cmd_add, entry, ["--type", "defect", "--title", "t", "--assignee", "a"])


@needs_store
def test_a_task_taken_from_waiting_goes_back_there_when_nothing_settles_it(
        project, store, capsys):
    entry = store
    write_worker(project, "supervisor", SUPERVISOR + "limits: {cool_down_seconds: 1}\n")
    add(entry, capsys, "w-one", status="waiting", assignee="supervisor")
    worker = mod._worker("supervisor")
    lapsing = mod._claim(entry, {"key": "w-one", "lease": "1", "worker": "supervisor",
                                 **mod._key_types(worker)})
    assert lapsing["task"]["status"] == "in_progress"
    import time
    time.sleep(1.2)
    # Any claim sweeps the lapsed lease, and the task goes back to its wait.
    mod.cmd_claim(entry, ["--type", "nothing-of-this-type"])
    assert str(lapsing["execution"]["id"]) in _out(capsys)["swept"]
    mod.cmd_show(entry, ["w-one"])
    assert _out(capsys)["task"]["status"] == "waiting"
    # The lapsed raise cools the task for the worker's cool-down first.
    time.sleep(1.1)
    failing = mod._take(entry, worker, None)
    assert failing["task"]["unique_key"] == "w-one"
    mod.cmd_release(entry, [str(failing["execution"]["id"]), "--outcome", "failed",
                            "--metrics", '{"cost_usd": 1}'])
    released = _out(capsys)
    assert released["task"]["status"] == "waiting"
    assert released["execution"]["metrics"] == {"cost_usd": 1,
                                                mod._CLAIMED_FROM: "waiting"}


def test_settlement_of_a_task_taken_from_waiting():
    """The claim left it waiting, so nothing is put back: a turn that moved
    nothing leaves it in the wait, and moving it to todo released it."""
    rested = {"status": "waiting", "assignee": "decider", "pickup_at": None, "metadata": {}}
    settle = lambda unspent=False, **moved: mod._settle(  # noqa: E731
        rested, {**rested, **moved}, unspent)
    assert settle() == ("failed", {"cut_off": True})
    assert settle(unspent=True) == ("failed", {"exhausted": True, "cut_off": True})
    assert settle(status="todo") == ("ok", {})
    assert settle(assignee="the owner") == ("handback", {})
    assert settle(status="complete") == ("ok", {})


def test_attempts_from_waiting_and_from_the_queue_are_counted_apart():
    rows = [{"metrics": {"cost_usd": 1}}, {"metrics": {"cost_usd": 1}},
            {"metrics": {mod._CLAIMED_FROM: "waiting"}}]
    assert mod._spent(rows) == 2
    assert mod._spent(rows, from_waiting=True) == 1



@needs_store
def test_the_shipped_supervisor_creates_a_task_waiting_on_a_name(project, store, capsys,
                                                                 monkeypatch):
    entry = store
    path = project / "capabilities" / "tasks" / "workers" / "supervisor.md"
    path.write_text(SHIPPED.read_text().replace("enabled: false", "enabled: true"))
    add(entry, capsys, "held", status="waiting", assignee="supervisor")
    execution = mod._take(entry, mod._worker("supervisor"), None)["execution"]["id"]
    monkeypatch.setenv("TASKS_EXECUTION", str(execution))
    mod.cmd_add(entry, ["--type", "defect", "--title", "t", "--status", "waiting",
                        "--assignee", "the owner"])
    created = _out(capsys)
    assert created["status"] == "waiting" and "coerced" not in created
    assert "`writes.new.type` allows only defect, change" in refused(
        capsys, mod.cmd_add, entry, ["--type", "chore", "--title", "t"])

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
