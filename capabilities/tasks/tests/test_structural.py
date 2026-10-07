#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""Structural dead ends: a task that can never move as things stand is caught by
the claim scan and escalated at once, and a write that would leave a task there
is refused.

What `doctor` makes of the places a worker may land a task is checked with no
store. What the scan escalates - a task no enabled worker takes, one whose
worker cannot run, one whose blockers can never all end - and what a write
refuses are driven against a real store with the harness replaced. The
store-backed checks read TASKS_TEST_DSN and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_escalation as esc  # noqa: E402
import test_hooks as hooks  # noqa: E402
from test_hooks import project, store, turns  # noqa: E402,F401  (fixtures)

mod = hooks.mod
needs_store = hooks.needs_store
add, answer, shown, tell = hooks.add, hooks.answer, hooks.shown, hooks.tell
write_worker = hooks.write_worker


def refused(capsys, verb, entry, args) -> dict:
    """The error a verb exits 4 with, as JSON."""
    with pytest.raises(SystemExit) as stopped:
        verb(entry, args)
    assert stopped.value.code == 4, capsys.readouterr()
    return json.loads(capsys.readouterr().err)["error"]


def raw_task(conn, schema: str, key: str, *, kind: str = "alpha", status: str = "todo",
             assignee: str | None = None, blocked_by=()) -> str:
    """A task written straight into the store, as drift leaves one: the CLI
    refuses to write it. Its blockers are ids."""
    return str(conn.execute(
        f"""insert into {schema}.tasks
              (project_id, type, title, unique_key, status, assignee, blocked_by)
            values (%s, %s, %s, %s, %s, %s, %s::uuid[]) returning id""",
        (hooks.HERE, kind, f"a {kind}", key, status, assignee,
         list(blocked_by))).fetchone()[0])


NOWHERE = "00000000-0000-4000-8000-000000000000"


def entries(entry, capsys, key: str) -> list[str]:
    return [one["description"] for one in shown(entry, capsys, key)["activities"]]


# --- What doctor makes of a worker's landings --------------------------------

NARROW = "takes: {status: [todo], type: [alpha], assignee: [alpha]}\nprofile: plain"


def test_doctor_names_a_landing_no_worker_takes(project):
    esc.supervised(project)
    write_worker(project, "alpha", NARROW + "\n"
                 "writes: {held: {status: [todo, waiting], assignee: [supervisor, owner]}}")
    rows, _broken = mod._workers_report()
    warnings = mod._coverage(rows)
    [row] = [r for r in rows if r["worker"] == "alpha"]
    assert row["lands_untaken"] == [{"assignee": "supervisor", "status": "todo",
                                     "types": ["alpha"]}]
    [said] = warnings
    assert "worker alpha" in said and "todo on supervisor" in said
    # Waiting on the supervisor is taken; the owner is a person.
    assert "waiting" not in said and "owner" not in said


def test_doctor_reports_nothing_for_a_clean_envelope(project):
    esc.supervised(project)
    write_worker(project, "alpha", NARROW + "\n"
                 "writes: {held: {status: [waiting, complete], assignee: [supervisor]}}")
    rows, _broken = mod._workers_report()
    assert mod._coverage(rows) == []
    assert all("lands_untaken" not in r for r in rows)


def test_doctor_names_a_landing_on_a_worker_switched_off(project):
    esc.supervised(project)
    write_worker(project, "gamma", "enabled: false\n"
                 "takes: {status: [todo], type: [alpha], assignee: [gamma]}\nprofile: plain")
    write_worker(project, "supervisor", esc.SUPERVISOR + "\n"
                 "writes: {held: {status: [todo, waiting], assignee: [gamma]}}")
    rows, _broken = mod._workers_report()
    said = mod._coverage(rows)
    assert any("worker supervisor" in one and "todo on gamma" in one for one in said), said
    assert any("waiting on gamma" in one for one in said), said


# --- No taker ----------------------------------------------------------------

@needs_store
def test_an_orphan_the_wait_returned_is_escalated_by_any_claim(project, store, turns,
                                                               capsys):
    """The send-grammar case: a wait on the supervisor ends by its pickup, the
    sweep returns it to todo still on the supervisor, which takes only waiting
    tasks - and the same claim of another worker escalates it."""
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    past = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=5)).replace(microsecond=0).isoformat()
    mod.cmd_add(entry, ["--type", "beta", "--title", "t", "--key", "t-orphan",
                        "--status", "waiting", "--assignee", "supervisor", "--pickup", past])
    answer(capsys)
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] is None
    [moved] = report["escalated"]
    assert (moved["task"], moved["from"], moved["to"]) == ("t-orphan", "supervisor", "owner")
    assert moved["why"].startswith("no enabled worker takes it (status todo, type beta, "
                                   "assignee supervisor, tags none)")
    assert "worker 'supervisor' takes {status waiting" in moved["why"]
    task = shown(entry, capsys, "t-orphan")["task"]
    assert (task["status"], task["assignee"]) == ("waiting", "owner")
    said = entries(entry, capsys, "t-orphan")[-1]
    assert said.startswith("Escalated from supervisor to owner: no enabled worker takes it")


@needs_store
def test_a_task_on_a_worker_switched_off_is_escalated(project, store, turns, capsys):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    write_worker(project, "gamma", "enabled: false\n"
                 "takes: {status: [todo], type: [beta], assignee: [gamma]}\nprofile: plain")
    raw_task(conn, schema, "t-off", kind="beta", assignee="gamma")
    add(entry, capsys, "t-work")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-work" and turns == ["t-work"]
    [moved] = report["escalated"]
    assert (moved["task"], moved["from"], moved["to"]) == ("t-off", "gamma", "supervisor")
    assert "worker 'gamma' is switched off" in moved["why"]
    task = shown(entry, capsys, "t-off")["task"]
    assert (task["status"], task["assignee"]) == ("waiting", "supervisor")
    # The supervisor takes it there.
    mod.cmd_run(entry, ["supervisor"])
    assert answer(capsys)["would_claim"] == "t-off"


@needs_store
def test_a_task_on_an_unreadable_worker_is_escalated(project, store, turns, capsys):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    write_worker(project, "gamma", "takes: {status: [todo], type: [beta], assignee: [gamma]}\n"
                 "profile: plain\ncolour: blue")
    raw_task(conn, schema, "t-broken", kind="beta", assignee="gamma")
    mod.cmd_run(entry, ["alpha", "--apply"])
    [moved] = answer(capsys)["escalated"]
    assert moved["task"] == "t-broken" and "worker 'gamma' cannot be read" in moved["why"]


@needs_store
def test_without_a_chain_a_dead_end_is_reported_and_not_moved(project, store, turns,
                                                              capsys):
    entry, schema, conn = store
    esc.supervised(project)
    raw_task(conn, schema, "t-stuck", kind="beta", assignee="supervisor")
    with mod._connect(entry) as c:
        assert mod._service_sweep_due(c) is False
    mod.cmd_run(entry, ["alpha"])
    dry = answer(capsys)
    [said] = dry["dead_ends"]
    assert said["task"] == "t-stuck" and "no enabled worker takes it" in said["why"]
    assert "would_escalate" not in dry
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert [one["task"] for one in report["dead_ends"]] == ["t-stuck"]
    assert "escalated" not in report
    task = shown(entry, capsys, "t-stuck")["task"]
    assert (task["status"], task["assignee"]) == ("todo", "supervisor")


@needs_store
def test_the_service_starts_a_claim_for_a_dead_end_it_can_escalate(project, store, capsys):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    with mod._connect(entry) as c:
        assert mod._service_sweep_due(c) is False
    raw_task(conn, schema, "t-stuck", kind="beta", assignee="supervisor")
    with mod._connect(entry) as c:
        assert mod._service_sweep_due(c) is True
    # Dry, it is named and not moved.
    mod.cmd_run(entry, ["alpha"])
    [named] = answer(capsys)["would_escalate"]
    assert (named["task"], named["to"]) == ("t-stuck", "owner")
    assert shown(entry, capsys, "t-stuck")["task"]["status"] == "todo"


@needs_store
def test_a_task_on_a_person_or_on_nobody_is_no_dead_end(project, store, turns, capsys):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    raw_task(conn, schema, "t-person", kind="beta", assignee="the owner")
    raw_task(conn, schema, "t-nobody", kind="beta")
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert "escalated" not in report and "dead_ends" not in report


# --- Its worker cannot run ---------------------------------------------------

@needs_store
def test_a_task_whose_worker_cannot_run_is_escalated_not_parked(project, store, turns,
                                                                capsys):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    write_worker(project, "beta", "takes: [beta]\nprofile: plain\nroutines: [nowhere]")
    add(entry, capsys, "t-cannot", kind="beta")
    mod.cmd_run(entry, ["beta", "--apply"])
    report = answer(capsys)
    assert report["claimed"] is None and "parked" not in report and turns == []
    [moved] = report["escalated"]
    assert (moved["task"], moved["to"]) == ("t-cannot", "supervisor")
    assert moved["why"].startswith("its worker cannot run: worker 'beta': the routine "
                                   "'nowhere' is not at ")
    assert hooks.raises_of(entry, capsys, "t-cannot") == []


@needs_store
def test_a_worker_with_a_missing_profile_is_caught_by_another_workers_claim(
        project, store, turns, capsys):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    write_worker(project, "beta", "takes: [beta]\nprofile: nowhere-profile")
    add(entry, capsys, "t-noprofile", kind="beta")
    mod.cmd_run(entry, ["alpha", "--apply"])
    [moved] = answer(capsys)["escalated"]
    assert moved["task"] == "t-noprofile"
    assert "its worker cannot run: worker 'beta': the profile 'nowhere-profile' is not " \
           "there" in moved["why"]


# --- Blockers that can never all end ----------------------------------------

@needs_store
@pytest.mark.parametrize("case", ["unknown", "draft", "cycle"])
def test_blockers_that_can_never_all_end_are_escalated(project, store, turns, capsys,
                                                       case):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    if case == "unknown":
        raw_task(conn, schema, "t-blocked", status="waiting", assignee="supervisor",
                 blocked_by=[NOWHERE])
        said = f"'{NOWHERE}' names no task"
    elif case == "draft":
        mod.cmd_add(entry, ["--type", "alpha", "--title", "d", "--key", "t-draft"])
        draft = answer(capsys)["created"]
        raw_task(conn, schema, "t-blocked", status="waiting", assignee="supervisor",
                 blocked_by=[draft])
        said = "'t-draft' is a draft"
    else:
        blocked = raw_task(conn, schema, "t-blocked", status="waiting",
                           assignee="supervisor")
        other = raw_task(conn, schema, "t-other", status="waiting", assignee="the owner",
                         blocked_by=[blocked])
        conn.execute(f"update {schema}.tasks set blocked_by = %s::uuid[] where id = %s",
                     ([other], blocked))
        said = "t-blocked -> t-other -> t-blocked"
    mod.cmd_run(entry, ["alpha", "--apply"])
    [moved] = answer(capsys)["escalated"]
    assert (moved["task"], moved["from"], moved["to"]) == ("t-blocked", "supervisor", "owner")
    assert moved["why"].startswith("its blockers can never all end: ")
    assert said in moved["why"]
    task = shown(entry, capsys, "t-blocked")["task"]
    assert (task["status"], task["assignee"]) == ("waiting", "owner")
    assert task["blocked_by"] == [] and "blocked_by" not in task["metadata"]


@needs_store
def test_blockers_still_open_are_no_dead_end(project, store, turns, capsys):
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    first = add(entry, capsys, "t-first")
    raw_task(conn, schema, "t-second", status="waiting", assignee="supervisor",
             blocked_by=[first])
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = answer(capsys)
    assert report["claimed"] == "t-first" and "escalated" not in report


# --- An escalation with blockers still open ----------------------------------

@needs_store
def test_an_escalation_lets_go_of_open_blockers_so_its_target_takes_it(project, store,
                                                                       turns, capsys):
    """A task escalated into waiting while its blocked_by still lists an open
    task would be hidden from the name it went to, since no claim takes a task
    with open blockers: the escalation lets the blockers go and says so. A task
    with an open blocker is offered to no claim, so what escalates one is the
    scan - here, a worker switched off with the task left on it."""
    entry, schema, conn = store
    esc.supervised(project)
    esc.conveyor(project)
    write_worker(project, "gamma", "enabled: false\n"
                 "takes: {status: [todo], type: [beta], assignee: [gamma]}\nprofile: plain")
    still_open = add(entry, capsys, "t-open", kind="beta")
    mod.cmd_set(entry, ["t-open", "--assignee", "the owner"])
    answer(capsys)
    raw_task(conn, schema, "t-esc", kind="beta", assignee="gamma",
             blocked_by=[still_open])
    mod.cmd_run(entry, ["alpha", "--apply"])
    [moved] = answer(capsys)["escalated"]
    assert (moved["task"], moved["to"]) == ("t-esc", "supervisor")
    assert moved["blocked_by_let_go"] == [still_open]
    task = shown(entry, capsys, "t-esc")["task"]
    assert (task["status"], task["assignee"]) == ("waiting", "supervisor")
    assert task["blocked_by"] == [] and "blocked_by" not in task["metadata"]
    assert "Its blocked_by (t-open) is let go" in entries(entry, capsys, "t-esc")[-1]
    mod.cmd_run(entry, ["supervisor"])
    assert answer(capsys)["would_claim"] == "t-esc"


# --- Writes that would land in a dead end ------------------------------------

@needs_store
def test_a_write_leaving_a_task_on_a_worker_nobody_takes_it_from_is_refused(project, store,
                                                                            capsys):
    entry, schema, conn = store
    esc.supervised(project)
    write_worker(project, "alpha", NARROW)
    add(entry, capsys, "t-one")
    error = refused(capsys, mod.cmd_set, entry, ["t-one", "--assignee", "supervisor"])
    assert error["code"] == "policy"
    assert error["message"].startswith("a write may not leave a task where no enabled "
                                       "worker takes it: no enabled worker takes it "
                                       "(status todo, type alpha, assignee supervisor")
    task = shown(entry, capsys, "t-one")["task"]
    assert task["assignee"] is None  # nothing was written
    error = refused(capsys, mod.cmd_add, entry, ["--type", "alpha", "--title", "x",
                                                 "--status", "todo",
                                                 "--assignee", "supervisor"])
    assert "no enabled worker takes it" in error["message"]
    # A type nobody takes on a worker's name.
    write_worker(project, "beta", "takes: {status: [todo], type: [beta], assignee: [beta]}\n"
                 "profile: plain")
    error = refused(capsys, mod.cmd_add, entry, ["--type", "gamma", "--title", "x",
                                                 "--status", "todo", "--assignee", "beta"])
    assert "type gamma, assignee beta" in error["message"]
    # Into waiting on the supervisor it is taken, and so is a person's name.
    mod.cmd_set(entry, ["t-one", "--status", "waiting", "--assignee", "supervisor"])
    assert answer(capsys)["task"]["assignee"] == "supervisor"
    mod.cmd_set(entry, ["t-one", "--status", "todo", "--assignee", "the owner"])
    assert answer(capsys)["task"]["assignee"] == "the owner"
    # A wait that ends on its own returns to a worker that takes todo.
    error = refused(capsys, mod.cmd_set, entry, ["t-one", "--status", "waiting",
                                                 "--assignee", "alpha"])
    assert "status waiting" in error["message"]
    later = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(hours=1)).replace(microsecond=0).isoformat()
    mod.cmd_set(entry, ["t-one", "--status", "waiting", "--assignee", "alpha",
                        "--pickup", later])
    assert answer(capsys)["task"]["status"] == "waiting"


@needs_store
def test_a_release_landing_where_nobody_takes_it_is_refused(project, store, capsys):
    entry, schema, conn = store
    esc.supervised(project)
    add(entry, capsys, "t-rel", kind="beta")
    mod.cmd_set(entry, ["t-rel", "--assignee", "the owner"])
    answer(capsys)
    mod.cmd_claim(entry, ["--key", "t-rel"])
    execution = answer(capsys)["execution"]["id"]
    # A release moves the task only where it is told, and that move is judged.
    error = refused(capsys, mod.cmd_release, entry, [execution, "--outcome", "failed",
                                                     "--assignee", "supervisor"])
    assert "no enabled worker takes it (status todo" in error["message"]
    [raised] = hooks.raises_of(entry, capsys, "t-rel")
    assert raised["status"] == "running"  # nothing was closed
    mod.cmd_release(entry, [execution, "--outcome", "handback", "--status", "waiting",
                            "--assignee", "supervisor"])
    assert answer(capsys)["task"]["status"] == "waiting"


@needs_store
def test_in_progress_is_never_written(project, store, capsys):
    entry, schema, conn = store
    add(entry, capsys, "t-hand")
    error = refused(capsys, mod.cmd_set, entry, ["t-hand", "--status", "in_progress"])
    assert error["code"] == "policy" and "in_progress is not stored" in error["message"]
    error = refused(capsys, mod.cmd_add, entry, ["--type", "alpha", "--title", "x",
                                                 "--status", "in_progress"])
    assert "in_progress is not stored" in error["message"]
    mod.cmd_claim(entry, ["--key", "t-hand"])
    execution = answer(capsys)["execution"]["id"]
    error = refused(capsys, mod.cmd_release, entry, [execution, "--outcome", "ok",
                                                     "--status", "in_progress"])
    assert "in_progress is not stored" in error["message"]
    assert shown(entry, capsys, "t-hand")["task"]["status"] == "in_progress"


@needs_store
def test_a_blocked_by_that_can_never_end_is_refused(project, store, capsys):
    entry, schema, conn = store
    add(entry, capsys, "t-a")
    add(entry, capsys, "t-b")
    tid_b = shown(entry, capsys, "t-b")["task"]["id"]
    error = refused(capsys, mod.cmd_meta, entry, ["set", "t-a", "blocked_by", '["t-zz"]'])
    assert error["code"] == "policy" and "'t-zz' names no task" in error["message"]
    error = refused(capsys, mod.cmd_meta, entry, ["set", "t-a", "blocked_by", '["t-a"]'])
    assert "'t-a' names itself" in error["message"]
    mod.cmd_meta(entry, ["set", "t-a", "blocked_by", json.dumps([tid_b])])
    answer(capsys)
    error = refused(capsys, mod.cmd_meta, entry, ["set", "t-b", "blocked_by", '["t-a"]'])
    assert "t-b -> t-a -> " in error["message"]
    assert "blocked_by" not in shown(entry, capsys, "t-b")["task"]["metadata"]
    # A blocker that has ended holds nothing, so it closes no cycle.
    mod.cmd_set(entry, ["t-a", "--status", "complete"])
    answer(capsys)
    tid_a = shown(entry, capsys, "t-a")["task"]["id"]
    mod.cmd_meta(entry, ["set", "t-b", "blocked_by", '["t-a"]'])
    assert answer(capsys)["metadata"]["blocked_by"] == [tid_a]
