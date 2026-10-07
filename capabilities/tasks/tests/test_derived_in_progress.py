#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""`in_progress` is derived from the open raise and never stored, and each raise
has one lease writer.

A claim opens a raise and writes nothing to the task: every reader shows it
`in_progress` while the raise holds it and the status it rests in after, and a
raise that ends without a move - failed, cut off, lapsed, handed back with
nothing named - leaves the task exactly where it rested. A turn the service
started renews nothing and only watches its lease; the service is its one
writer, and a run by hand keeps its own. A store still holding `in_progress`
rows from before is returned to where each rests by `migrate --apply`.

The store-backed checks read TASKS_TEST_DSN and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from callva import harness_runner
from callva.harness_runner import FailureKind, Result

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402
import test_hooks as hooks  # noqa: E402
import test_service as service  # noqa: E402
from test_hooks import project, store  # noqa: E402,F401  (fixtures)
from test_service import lab  # noqa: E402,F401  (fixture)

mod = hooks.mod
needs_store = hooks.needs_store
add, answer, shown, raises_of = hooks.add, hooks.answer, hooks.shown, hooks.raises_of


def stored(conn, schema: str, key: str) -> dict:
    """The task as the store holds it, read past every verb."""
    row = conn.execute(f"""select status, assignee, pickup_at, blocked_by, updated_at
                             from {schema}.tasks where unique_key = %s""", (key,)).fetchone()
    return dict(zip(("status", "assignee", "pickup_at", "blocked_by", "updated_at"), row))


def listed(entry, capsys, *args: str) -> list[str]:
    mod.cmd_list(entry, list(args))
    return sorted(row["unique_key"] for row in answer(capsys)["tasks"])


def claim(entry, capsys, key: str, lease: str = "600") -> str:
    mod.cmd_claim(entry, ["--key", key, "--worker", "alpha", "--lease", lease])
    return answer(capsys)["execution"]["id"]


def resting_pair(entry, capsys, conn, schema):
    """A todo task with an assignee, a pickup passed and a blocker that ended,
    and a waiting one on a person: each where a claim will find it."""
    add(entry, capsys, "t-done")
    mod.cmd_set(entry, ["t-done", "--status", "complete"])
    capsys.readouterr()
    add(entry, capsys, "t-todo")
    mod.cmd_set(entry, ["t-todo", "--assignee", "builder", "--pickup", "2020-01-01",
                        "--blocked-by", "t-done"])
    capsys.readouterr()
    add(entry, capsys, "t-wait")
    mod.cmd_set(entry, ["t-wait", "--status", "waiting", "--assignee", "the owner"])
    capsys.readouterr()
    return {key: stored(conn, schema, key) for key in ("t-todo", "t-wait")}


# --- The derivation ----------------------------------------------------------

@needs_store
def test_a_claim_writes_nothing_to_the_task_and_every_reader_shows_in_progress(
        store, capsys):
    entry, schema, conn = store
    rested = resting_pair(entry, capsys, conn, schema)
    add(entry, capsys, "t-free")
    changes_before = conn.execute(f"select count(*) from {schema}.task_changes").fetchone()[0]

    claim(entry, capsys, "t-todo")
    claim_wait = mod._claim(entry, {"key": "t-wait", "worker": "alpha", "lease": "600",
                                    "accept": [{"status": ["waiting"], "type": None}]})
    assert claim_wait["task"]["status"] == "in_progress"

    # Stored exactly where they rested, and no move recorded.
    for key in ("t-todo", "t-wait"):
        assert stored(conn, schema, key) == rested[key]
    assert conn.execute(f"select count(*) from {schema}.task_changes").fetchone()[0] \
        == changes_before

    assert shown(entry, capsys, "t-todo")["task"]["status"] == "in_progress"
    assert shown(entry, capsys, "t-wait")["task"]["status"] == "in_progress"
    assert listed(entry, capsys, "--status", "in_progress") == ["t-todo", "t-wait"]
    assert listed(entry, capsys, "--status", "todo") == ["t-free"]
    assert listed(entry, capsys, "--status", "waiting") == []
    mod.cmd_list(entry, ["--full"])
    full = {row["unique_key"]: row["status"] for row in answer(capsys)["tasks"]}
    assert full["t-todo"] == full["t-wait"] == "in_progress" and full["t-free"] == "todo"
    mod.cmd_list(entry, [])
    lean = answer(capsys)
    assert {r["unique_key"]: r["status"] for r in lean["tasks"]}["t-todo"] == "in_progress"
    assert lean["waiting_on"] == {}
    mod.cmd_search(entry, ["t-wa", "--status", "in_progress"])
    assert [r["unique_key"] for r in answer(capsys)["tasks"]] == ["t-wait"]
    mod.cmd_counts(entry, [])
    counted = answer(capsys)
    assert counted["counts"]["in_progress"] == 2 and counted["counts"]["todo"] == 1
    assert counted["counts"]["waiting"] == 0 and counted["total"] == 4
    mod.cmd_ready(entry, [])
    assert {r["unique_key"]: r["status"] for r in answer(capsys)["tasks"]} == {
        "t-todo": "in_progress", "t-wait": "in_progress", "t-free": "todo"}


@needs_store
def test_once_the_raise_ends_every_reader_shows_where_the_task_rests(store, capsys):
    entry, schema, conn = store
    resting_pair(entry, capsys, conn, schema)
    execution = claim(entry, capsys, "t-todo")
    mod.cmd_release(entry, [execution, "--outcome", "failed"])
    released = answer(capsys)
    assert released["task"]["status"] == "todo" and released["moved"] == []
    assert shown(entry, capsys, "t-todo")["task"]["status"] == "todo"
    assert listed(entry, capsys, "--status", "in_progress") == []
    assert "t-todo" in listed(entry, capsys, "--status", "todo")


@needs_store
def test_a_raise_whose_lease_ran_out_holds_nothing_before_it_is_swept(store, capsys):
    entry, schema, conn = store
    add(entry, capsys, "t-lapsed")
    execution = claim(entry, capsys, "t-lapsed")
    assert listed(entry, capsys, "--status", "in_progress") == ["t-lapsed"]
    conn.execute(f"""update {schema}.task_executions
                        set lease_until = now() - interval '1 second' where id = %s""",
                 (execution,))
    # Still running in the store, and no longer shown as holding the task.
    assert shown(entry, capsys, "t-lapsed")["task"]["status"] == "todo"
    assert listed(entry, capsys, "--status", "in_progress") == []


@needs_store
def test_in_progress_is_refused_to_every_writer(store, capsys):
    entry, schema, conn = store
    add(entry, capsys, "t-hand")
    with pytest.raises(SystemExit) as ended:
        mod.cmd_set(entry, ["t-hand", "--status", "in_progress"])
    assert ended.value.code == 4
    assert "in_progress is not stored" in capsys.readouterr().err
    assert stored(conn, schema, "t-hand")["status"] == "todo"


# --- Raises that end without a move -------------------------------------------

@needs_store
@pytest.mark.parametrize("ending", ["failed", "handback", "lapsed", "end-turns"])
def test_a_raise_ending_without_a_move_leaves_the_task_where_it_rested(
        store, capsys, ending):
    entry, schema, conn = store
    rested = resting_pair(entry, capsys, conn, schema)
    for key in ("t-todo", "t-wait"):
        if key == "t-wait":
            held = mod._claim(entry, {"key": key, "worker": "alpha", "lease": "600",
                                      "accept": [{"status": ["waiting"], "type": None}]})
            execution = str(held["execution"]["id"])
        else:
            execution = claim(entry, capsys, key)
        if ending in ("failed", "handback"):
            mod.cmd_release(entry, [execution, "--outcome", ending])
            assert answer(capsys)["moved"] == []
        elif ending == "lapsed":
            conn.execute(f"""update {schema}.task_executions
                                set lease_until = now() - interval '1 second'
                              where id = %s""", (execution,))
            assert mod._claim(entry, {"type": "nothing", "lease": "60"})["swept"] \
                == [execution]
        else:
            with mod._connect(entry, raising=True) as own:
                assert mod._settle_cut_off(own, execution) == [execution]
        assert stored(conn, schema, key) == rested[key], (ending, key)
        assert shown(entry, capsys, key)["task"]["status"] == rested[key]["status"]


@needs_store
def test_a_turn_that_moves_nothing_leaves_the_task_where_it_rested(project, store,
                                                                  capsys, monkeypatch):
    """Cut off: the turn answered and moved nothing, so its raise fails, marked
    `cut_off`, and no landing puts the task anywhere."""
    entry, schema, conn = store
    rested = resting_pair(entry, capsys, conn, schema)

    class Idle:
        Profile, Session, FailureKind = (harness_runner.Profile, harness_runner.Session,
                                         FailureKind)
        find_profile_file = staticmethod(harness_runner.find_profile_file)
        ProfileNotFound = harness_runner.ProfileNotFound

        def run(self, prompt, profile, cwd, *, session=None, environ=None, **kw):
            assert stored(conn, schema, "t-todo") == rested["t-todo"]
            return Result(ok=True, harness="claude", answer="looked", session_id=session.id,
                          model="m", cost_usd=0.0, duration_ms=1, num_turns=1)

    monkeypatch.setattr(mod, "_harness_runner", Idle)
    mod.cmd_run(entry, ["alpha", "--apply", "--key", "t-todo"])
    report = answer(capsys)
    assert report["claimed"] == "t-todo"
    [raised] = raises_of(entry, capsys, "t-todo")
    assert raised["status"] == "failed" and raised["metrics"]["cut_off"] is True
    # Where it stands is untouched; what the turn cost is still added to its
    # metadata, which moves `updated_at`.
    place = ("status", "assignee", "pickup_at", "blocked_by")
    after = stored(conn, schema, "t-todo")
    assert {k: after[k] for k in place} == {k: rested["t-todo"][k] for k in place}


@needs_store
def test_ok_alone_still_completes_a_claim_worked_by_hand(store, capsys):
    entry, schema, conn = store
    add(entry, capsys, "t-ok")
    execution = claim(entry, capsys, "t-ok")
    mod.cmd_release(entry, [execution, "--outcome", "ok"])
    assert answer(capsys)["task"]["status"] == "complete"


@needs_store
def test_a_waiting_task_a_raise_holds_is_not_returned_by_the_sweep(store, capsys):
    """Its wait ended while the raise held it: the turn decides, not the sweep."""
    entry, schema, conn = store
    add(entry, capsys, "t-wait")
    mod.cmd_set(entry, ["t-wait", "--status", "waiting", "--assignee", "the owner"])
    capsys.readouterr()
    held = mod._claim(entry, {"key": "t-wait", "worker": "alpha", "lease": "600",
                              "accept": [{"status": ["waiting"], "type": None}]})
    conn.execute(f"update {schema}.tasks set pickup_at = now() - interval '1 minute' "
                 "where unique_key = 't-wait'")
    assert mod._claim(entry, {"type": "nothing", "lease": "60"})["returned"] == []
    assert stored(conn, schema, "t-wait")["status"] == "waiting"
    mod.cmd_release(entry, [str(held["execution"]["id"]), "--outcome", "failed"])
    capsys.readouterr()
    assert mod._claim(entry, {"type": "nothing", "lease": "60"})["returned"] == ["t-wait"]


# --- One lease writer per raise -----------------------------------------------

def lease_of(conn, schema: str, execution: str):
    return conn.execute(f"select lease_until from {schema}.task_executions where id = %s",
                        (execution,)).fetchone()[0]


@needs_store
@pytest.mark.parametrize("renews", [True, False])
def test_a_beat_writes_the_lease_only_for_a_run_started_by_hand(store, capsys, renews):
    entry, schema, conn = store
    add(entry, capsys, "t-beat")
    execution = claim(entry, capsys, "t-beat", lease="30")
    first = lease_of(conn, schema, execution)
    beat = mod._Beat(entry, execution, cap=600, lease=30, every=1, renews=renews)
    try:
        time.sleep(3.5)
        assert beat.lost is None and beat.failures == 0
        later = lease_of(conn, schema, execution)
        assert (later > first) is renews
    finally:
        beat.stop()


@needs_store
def test_a_watched_lease_that_runs_out_ends_the_turn(store, capsys):
    """Whoever keeps it: a turn the service started loses its raise once the
    lease the service last wrote has run out."""
    entry, schema, conn = store
    add(entry, capsys, "t-watched")
    execution = claim(entry, capsys, "t-watched", lease="2")
    beat = mod._Beat(entry, execution, cap=600, lease=2, every=1, renews=False)
    try:
        assert beat.ended.wait(10)
        assert beat.lost == ("its lease ran out: the service that started this turn "
                             "renews it no longer")
        assert lease_of(conn, schema, execution) < conn.execute("select now()").fetchone()[0]
    finally:
        beat.stop()


@needs_store
def test_the_service_renews_by_the_cap_its_turn_reported(project, store, capsys):
    """The receipt carries the turn's `limits.lease_seconds`, so the service
    renews it without reading the worker file, and never past that cap."""
    entry, schema, conn = store
    tid = add(entry, capsys, "t-cap")
    execution = claim(entry, capsys, "t-cap", lease="5")
    receipt = {"task": "t-cap", "task_id": tid, "execution": execution, "attempt": 1,
               "lease_seconds": 3}
    with mod._connect(entry, raising=True) as own:
        renewed = mod._service_renew_leases(own, [("no-such-worker", receipt)])
        own.commit()
    assert renewed["renewed"] == [execution]
    span = conn.execute(f"""select extract(epoch from lease_until - started_at)::float8
                              from {schema}.task_executions where id = %s""",
                        (execution,)).fetchone()[0]
    # Never shorter than what is written: the claim's 5s stands over the 3s cap.
    assert 4 < span <= 5.5


@needs_store
def test_a_run_the_service_started_renews_nothing_and_ends_when_unrenewed(lab, tmp_path):
    """A run handed a receipt - the mark of a turn the service started - writes
    no lease of its own. With no service renewing it, the lease the claim wrote
    runs out and the turn ends itself, its task where it rested."""
    import psycopg
    schema = json.loads((lab["project"] / "capabilities" / "tasks" / "connections.json")
                        .read_text())["connections"]["local"]["db_schema"]
    lab["env"]["FAKE_ENGINE_SLEEP"] = "120"
    lab["env"]["TASKS_STORE_RETRY_SECONDS"] = "3"    # a beat every 2s, a lease of 9s
    service.answer_of(service.tasks_cli(lab, "add", "--type", "alpha", "--title", "t",
                                        "--key", "t-service", "--status", "todo"))
    receipt = tmp_path / "receipt.json"
    env = {**lab["env"], "TASKS_TURN_RECEIPT": str(receipt)}
    turn = subprocess.Popen([str(_cli.CLI_PATH), "run", "alpha", "--apply"],
                            cwd=lab["project"], env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        got = service.poll_for(lambda: receipt.is_file() and json.loads(receipt.read_text()), 60)
        assert got["lease_seconds"] > 0
        with psycopg.connect(service.DSN, autocommit=True) as conn:
            first = lease_of(conn, schema, got["execution"])
            time.sleep(5)
            assert lease_of(conn, schema, got["execution"]) == first, "the run renewed it"
        out, err = turn.communicate(timeout=60)
    finally:
        if turn.poll() is None:
            os.killpg(turn.pid, signal.SIGKILL)
    report = json.loads(out)
    assert report["lease_lost"] == ("its lease ran out: the service that started this "
                                    "turn renews it no longer"), report
    assert '"lease_lost"' in err
    task = service.answer_of(service.tasks_cli(lab, "show", "t-service"))["task"]
    assert task["status"] == "todo"


# --- The migration ------------------------------------------------------------

def stored_in_progress(conn, schema: str, key: str, *, came_from: str | None,
                       by: str | None, execution: bool = False) -> None:
    """Put the task in `in_progress` the way a version that stored it did: a
    claim's move stamped with its raise, or a person's move by hand."""
    tid = conn.execute(f"select id from {schema}.tasks where unique_key = %s",
                       (key,)).fetchone()[0]
    raise_id = None
    if execution:
        raise_id = conn.execute(f"""insert into {schema}.task_executions
                                      (task_id, attempt, worker, lease_until)
                                    values (%s, 1, 'alpha', now() + interval '1 hour')
                                    returning id""", (tid,)).fetchone()[0]
    conn.execute(f"alter table {schema}.tasks disable trigger tasks_touch_updated_at")
    conn.execute(f"update {schema}.tasks set status = 'in_progress' where id = %s", (tid,))
    conn.execute(f"alter table {schema}.tasks enable trigger tasks_touch_updated_at")
    if came_from is not None:
        conn.execute(f"""insert into {schema}.task_changes
                           (task_id, field, old_value, new_value, execution_id, actor)
                         values (%s, 'status', %s, 'in_progress', %s, %s)""",
                     (tid, came_from, raise_id, by))


@needs_store
def test_migrate_returns_every_stored_in_progress_to_where_it_rests(project, store, capsys,
                                                                   monkeypatch):
    entry, schema, conn = store
    monkeypatch.setattr(mod, "_schema", lambda entry: schema)
    monkeypatch.setattr(mod, "_writing_project", lambda: hooks.HERE)
    for key in ("m-claimed", "m-from-wait", "m-hand", "m-none", "m-nobody", "m-other"):
        add(entry, capsys, key)
    mod.cmd_set(entry, ["m-from-wait", "--status", "waiting", "--assignee", "decider"])
    capsys.readouterr()
    stored_in_progress(conn, schema, "m-claimed", came_from="todo", by="alpha", execution=True)
    stored_in_progress(conn, schema, "m-from-wait", came_from="waiting", by="decider",
                       execution=True)
    # A person put it there by hand from todo, with no raise.
    stored_in_progress(conn, schema, "m-hand", came_from="todo", by="a-person")
    stored_in_progress(conn, schema, "m-none", came_from=None, by=None)
    stored_in_progress(conn, schema, "m-nobody", came_from="waiting", by="a-person")
    conn.execute(f"update {schema}.tasks set project_id = 'prj_other' "
                 "where unique_key = 'm-other'")
    stored_in_progress(conn, schema, "m-other", came_from="todo", by="a-person")
    before = {key: stored(conn, schema, key)["updated_at"]
              for key in ("m-claimed", "m-hand")}

    mod.cmd_migrate(entry, [])
    dry = answer(capsys)["would_rest"]
    assert dry["tasks"] == 6
    mine = dry["by_project"][hooks.HERE]
    assert mine["tasks"] == 5 and mine["to"] == {"todo": 3, "waiting": 1, "draft": 1}
    assert {one["task"]: one["by"] for one in mine["by_hand"]} == {"m-hand": "a-person"}
    assert {one["task"]: one["to"] for one in mine["without_record"]} == {
        "m-none": "todo", "m-nobody": "draft"}
    assert dry["by_project"]["prj_other"]["tasks"] == 1
    # A dry run writes nothing.
    assert stored(conn, schema, "m-claimed")["status"] == "in_progress"

    mod.cmd_migrate(entry, ["--apply"])
    rested = answer(capsys)["rested"]
    assert rested["tasks"] == 6
    assert {key: stored(conn, schema, key)["status"] for key in
            ("m-claimed", "m-from-wait", "m-hand", "m-none", "m-nobody", "m-other")} == {
        "m-claimed": "todo", "m-from-wait": "waiting", "m-hand": "todo",
        "m-none": "todo", "m-nobody": "draft", "m-other": "todo"}
    # Nobody moved them: updated_at stands, and the move reads as the migration's.
    for key, moment in before.items():
        assert stored(conn, schema, key)["updated_at"] == moment
    row = conn.execute(f"""select c.old_value, c.new_value, c.actor, c.execution_id
                             from {schema}.task_changes c join {schema}.tasks t
                               on t.id = c.task_id
                            where t.unique_key = 'm-hand' order by c.changed_at desc
                            limit 1""").fetchone()
    assert row == ("in_progress", "todo", mod.MIGRATE_ACTOR, None)
    # A raise still open shows it in progress, as the open raise says.
    assert shown(entry, capsys, "m-claimed")["task"]["status"] == "in_progress"
    assert shown(entry, capsys, "m-hand")["task"]["status"] == "todo"

    mod.cmd_migrate(entry, ["--apply"])
    assert answer(capsys)["rested"] == {"tasks": 0, "by_project": {}}


# --- The contract --------------------------------------------------------------

def test_help_states_the_derivation_the_one_lease_writer_and_the_migration():
    said = " ".join(mod.__doc__.split())
    for needle in ("`in_progress` is never stored, and no verb writes it.",
                   "Each raise has exactly one lease writer:",
                   "the turn's `run` writes nothing and reads the lease on the same beat",
                   "The daemon is the one writer of its turns' leases",
                   "a claim announces `run_started` with the task shown `in_progress`",
                   "`migrate --apply` returns each, in the same transaction and "
                   "store-wide, to the status it rests in",
                   "under `would_rest`",
                   "`--outcome ok` alone, the one landing kept for a claim worked by "
                   "hand, completes the task"):
        assert needle in said, needle
    assert "second beat" not in said
