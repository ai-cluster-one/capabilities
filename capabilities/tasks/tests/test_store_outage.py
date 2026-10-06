#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""A store that goes away for a while, a turn that dies, and a raise left open:
none of them ends a turn that could have gone on, or stalls a lane.

A command waits out a store that is away for the retry window and gives up
with exit 5 only once it is spent; a turn renews a short lease while its
process lives, so a turn that is killed is settled as cut off within that
lease while a store outage shorter than the window lapses nothing; and a claim
passes over a task an open raise still holds instead of failing on it.

The store going away is the relay the service tests own, between the CLI and
the store, so no test stops a server it did not start. Turns are real `tasks
run` children on the stand-in harness. The store-backed checks read
TASKS_TEST_DSN and skip when it is unset.

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
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402
import test_service as base  # noqa: E402
from test_service import lab, relay  # noqa: E402,F401  (fixtures)

mod = base.mod
needs_store = base.needs_store
DSN = base.DSN
cli, answer_of, poll_for = base.tasks_cli, base.answer_of, base.poll_for


def add(lab, key: str, kind: str = "alpha") -> None:
    answer_of(cli(lab, "add", "--type", kind, "--title", key, "--key", key,
                  "--status", "todo"))


def schema_of(lab) -> str:
    registry = json.loads((lab["project"] / "capabilities" / "tasks"
                           / "connections.json").read_text())
    return registry["connections"]["local"]["db_schema"]


def relayed(lab, relay, window: int) -> None:
    """Every command of the lab reaches the store through the relay, and waits
    a store that is away out for `window` seconds."""
    connections = lab["project"] / "capabilities" / "tasks" / "connections.json"
    registry = json.loads(connections.read_text())
    registry["connections"]["relayed"] = {**registry["connections"]["local"],
                                          "db_host": "127.0.0.1",
                                          "db_port": str(relay.port)}
    registry["default"] = "relayed"
    connections.write_text(json.dumps(registry))
    lab["env"]["TASKS_STORE_RETRY_SECONDS"] = str(window)


def raise_of(schema: str, key: str) -> dict | None:
    """The newest raise of the task `key`, read straight from the store and
    never through the relay, with how long its lease has left and how long it
    was from the claim."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(
            f"""select e.id::text as id, e.status, e.detail, e.lease_until,
                       extract(epoch from e.lease_until - now())::float8 as left,
                       extract(epoch from e.lease_until - e.started_at)::float8 as span,
                       t.status as task_status
                  from {schema}.task_executions e
                  join {schema}.tasks t on t.id = e.task_id
                 where t.unique_key = %s
                 order by e.started_at desc limit 1""", (key,)).fetchone()


def start_run(lab, *args: str) -> subprocess.Popen:
    return subprocess.Popen([str(_cli.CLI_PATH), "run", "alpha", "--apply", *args],
                            cwd=lab["project"], env=lab["env"], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)


def descendants(pid: int) -> list[int]:
    listed = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True,
                            text=True).stdout.split()
    children: dict[int, list[int]] = {}
    for child, parent in zip(listed[::2], listed[1::2]):
        children.setdefault(int(parent), []).append(int(child))
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


# --- Without a store ---------------------------------------------------------

@pytest.mark.parametrize("said, away", [
    ('connection failed: connection to server at "127.0.0.1", port 5 failed: '
     'Connection refused', True),
    ("failed to resolve host 'db.example': nodename nor servname provided", True),
    ('connection failed: connection to server at "10.0.0.1", port 5432 failed: '
     'timeout expired', True),
    ('connection failed: FATAL:  the database system is starting up', True),
    ('connection failed: FATAL:  sorry, too many clients already', True),
    ('connection failed: FATAL:  password authentication failed for user "x"', False),
    ('connection failed: FATAL:  database "nope" does not exist', False),
    ('connection failed: FATAL:  role "nobody" does not exist', False),
    ("connection failed: fe_sendauth: no password supplied", False),
])
def test_only_a_store_that_is_away_is_asked_for_again(said, away):
    assert mod._store_away(Exception(said)) is away


@pytest.mark.parametrize("window, every, lease", [
    (None, 30, 270), ("180", 30, 270), ("0", 2, 6), ("6", 2, 12), ("60", 10, 90),
    ("600", 30, 690)])
def test_the_beat_and_its_lease_follow_the_retry_window(monkeypatch, window, every, lease):
    monkeypatch.setattr(mod, "_resolve_env_key",
                        lambda key: (window, "env", None) if key == mod._STORE_RETRY_ENV
                        else (None, None, None))
    assert mod._beat_lease() == (every, lease)
    # The lease outlasts the window by more than the slowest renewal after it.
    assert lease - (int(window) if window else 180) >= 3 * every


@pytest.mark.parametrize("window", ["-1", "soon", "1.5"])
def test_a_retry_window_that_is_not_whole_seconds_is_refused(monkeypatch, capsys, window):
    monkeypatch.setattr(mod, "_resolve_env_key", lambda key: (window, "env", None))
    with pytest.raises(SystemExit) as ended:
        mod._store_retry_seconds()
    assert ended.value.code == 6
    assert mod._STORE_RETRY_ENV in json.loads(capsys.readouterr().err)["error"]["message"]


def test_help_documents_the_window_the_beat_and_the_pass_over():
    said = " ".join(mod.__doc__.split())
    assert "TASKS_STORE_RETRY_SECONDS - 180 unless set" in said
    assert "A turn proves it is alive." in said
    assert "`verdict` `held`" in said


# --- A store that goes away --------------------------------------------------

@needs_store
def test_a_command_waits_out_a_store_that_is_away_and_gives_up_after_its_window(
        lab, relay):
    add(lab, "t-one")
    relayed(lab, relay, 60)

    relay.down()
    waiting = subprocess.Popen([str(_cli.CLI_PATH), "show", "t-one"], cwd=lab["project"],
                               env=lab["env"], text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    time.sleep(4)
    assert waiting.poll() is None, "gave up while the store was away"
    relay.up()
    out, err = waiting.communicate(timeout=90)
    assert waiting.returncode == 0, err
    assert json.loads(out)["task"]["unique_key"] == "t-one"
    said = [json.loads(line) for line in err.splitlines() if line.strip()]
    # Said once, however many times it asked.
    assert len(said) == 1 and said[0]["store_away"]["asking_again_for_seconds"] == 60
    assert "Connection refused" in said[0]["store_away"]["error"]

    # Once the window is spent the answer is what it always was.
    relay.down()
    lab["env"]["TASKS_STORE_RETRY_SECONDS"] = "3"
    started = time.monotonic()
    gone = cli(lab, "show", "t-one")
    assert gone.returncode == 5
    assert json.loads(gone.stderr.splitlines()[-1])["error"]["code"] == "unreachable"
    assert 2 <= time.monotonic() - started < 30

    # Zero asks once.
    lab["env"]["TASKS_STORE_RETRY_SECONDS"] = "0"
    at_once = cli(lab, "show", "t-one")
    assert at_once.returncode == 5
    assert [json.loads(line)["error"]["code"]
            for line in at_once.stderr.splitlines()] == ["unreachable"]


@needs_store
def test_a_store_that_answers_and_refuses_is_not_waited_for(lab):
    connections = lab["project"] / "capabilities" / "tasks" / "connections.json"
    registry = json.loads(connections.read_text())
    registry["connections"]["local"]["db_name"] = "no_such_database_here"
    connections.write_text(json.dumps(registry))
    lab["env"]["TASKS_STORE_RETRY_SECONDS"] = "120"
    started = time.monotonic()
    refused = cli(lab, "list")
    assert refused.returncode == 5
    assert time.monotonic() - started < 20
    assert [json.loads(line)["error"]["code"]
            for line in refused.stderr.splitlines()] == ["unreachable"]


@needs_store
def test_doctor_proves_the_store_as_it_is_now(lab, relay):
    relayed(lab, relay, 120)
    relay.down()
    started = time.monotonic()
    doctor = cli(lab, "doctor")
    assert doctor.returncode == 5
    assert time.monotonic() - started < 20


@needs_store
def test_a_turn_started_while_the_store_is_away_waits_for_it(lab, relay):
    lab["env"]["FAKE_ENGINE_SLEEP"] = "1"
    add(lab, "t-early")
    relayed(lab, relay, 60)
    relay.down()
    said = lab["tmp"] / "early.err"
    with said.open("w") as errors:
        turn = subprocess.Popen([str(_cli.CLI_PATH), "run", "alpha", "--apply"],
                                cwd=lab["project"], env=lab["env"], text=True,
                                stdout=subprocess.PIPE, stderr=errors)
    # The store is back only once the turn has said it is waiting for it.
    poll_for(lambda: "store_away" in said.read_text(), 60)
    time.sleep(2)
    assert turn.poll() is None, "the turn gave up while the store was away"
    relay.up()
    out, _ = turn.communicate(timeout=120)
    assert turn.returncode == 0, said.read_text()
    assert json.loads(out)["claimed"] == "t-early"
    settled = raise_of(schema_of(lab), "t-early")
    assert (settled["status"], settled["task_status"]) == ("ok", "complete")


@needs_store
def test_a_turn_outlives_a_store_outage_and_its_lease_never_lapses(lab, relay):
    """The store goes away for 8s in the middle of a turn, under a retry window
    of 10s: the worker's own `tasks` calls wait for it, the beat cannot reach
    it, and still the lease the turn holds never runs out and the turn settles
    as if nothing happened."""
    schema = schema_of(lab)
    lab["env"]["FAKE_ENGINE_SLEEP"] = "6"
    add(lab, "t-turn")
    relayed(lab, relay, 10)        # a beat every 2s, a lease of 16s
    turn = start_run(lab)
    first = poll_for(lambda: raise_of(schema, "t-turn"), 60)
    claimed = time.monotonic()
    # Held by the short lease from the claim, not by the profile's 1200s.
    assert first["status"] == "running" and first["span"] <= 17
    lifts = claimed + 4
    while time.monotonic() < lifts:
        time.sleep(0.2)
    before = raise_of(schema, "t-turn")
    assert before["lease_until"] > first["lease_until"], "the beat never renewed it"

    relay.down()
    left = []
    away_until = time.monotonic() + 8
    while time.monotonic() < away_until:
        row = raise_of(schema, "t-turn")
        if row["status"] == "running":
            left.append(row["left"])
        time.sleep(0.25)
    relay.up()

    out, err = turn.communicate(timeout=120)
    assert turn.returncode == 0, err
    assert left and min(left) > 0, left
    report = json.loads(out)
    assert report["claimed"] == "t-turn" and report["trail_grew_by"] >= 1
    settled = raise_of(schema, "t-turn")
    assert (settled["status"], settled["task_status"]) == ("ok", "complete")
    # Renewed again once the store was back, before it closed.
    assert settled["lease_until"] > before["lease_until"]


@needs_store
def test_a_killed_turn_is_cut_off_within_its_short_lease_and_its_task_returns(lab):
    schema = schema_of(lab)
    lab["env"]["FAKE_ENGINE_SLEEP"] = "120"
    lab["env"]["TASKS_STORE_RETRY_SECONDS"] = "3"   # a beat every 2s, a lease of 9s
    add(lab, "t-killed")
    turn = start_run(lab)
    try:
        first = poll_for(lambda: raise_of(schema, "t-killed"), 60)
        assert first["status"] == "running" and first["span"] <= 10
        renewed = poll_for(lambda: (lambda row: row if row["lease_until"]
                                    > first["lease_until"] else None)(
                                        raise_of(schema, "t-killed")), 15)
        assert renewed["left"] > 0 and renewed["status"] == "running"
    finally:
        below = descendants(turn.pid)
        os.kill(turn.pid, signal.SIGKILL)
        turn.wait(timeout=10)
        for pid in below:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # Nothing renews it now: it lapses within the lease the last beat wrote.
    lapsed = poll_for(lambda: (lambda row: row if row["left"] < 0 else None)(
        raise_of(schema, "t-killed")), 15)
    assert lapsed["status"] == "running" and lapsed["task_status"] == "in_progress"
    # And the next claim settles it the way any lapsed lease is settled.
    claim = answer_of(cli(lab, "claim", "--type", "nothing-of-this-type"))
    assert claim["claimed"] is None and claim["swept"] == [first["id"]]
    settled = raise_of(schema, "t-killed")
    assert (settled["status"], settled["task_status"]) == ("abandoned", "todo")
    assert settled["detail"] == "lease lapsed before release"
    runs = answer_of(cli(lab, "runs", "t-killed"))["executions"]
    assert mod._cut_off(runs[-1])


@needs_store
def test_a_renewal_cut_mid_flight_is_tried_again_and_the_lease_is_kept(lab, relay):
    """The connection three renewals in a row are sent on is cut after the query
    has passed and before its answer is back. Each is tried again on a new
    connection within a second, the lease never runs out, each failure is said,
    and the turn settles as if nothing happened."""
    schema = schema_of(lab)
    lab["env"]["FAKE_ENGINE_SLEEP"] = "10"
    add(lab, "t-cut")
    relayed(lab, relay, 6)         # a beat every 2s, a lease of 12s
    relay.cut_on(b"lease_until = least(", 3)
    turn = start_run(lab)
    first = poll_for(lambda: raise_of(schema, "t-cut"), 60)
    left = []
    while relay.cuts_left:
        row = raise_of(schema, "t-cut")
        assert row["status"] == "running", row
        left.append(row["left"])
        assert len(left) < 150, "the renewals were never cut"
        time.sleep(0.2)
    renewed = poll_for(lambda: (lambda row: row if row["lease_until"]
                                > first["lease_until"] else None)(
                                    raise_of(schema, "t-cut")), 15)
    assert renewed["status"] == "running" and renewed["left"] > 0
    assert min(left) > 0, left

    out, err = turn.communicate(timeout=120)
    assert turn.returncode == 0, err
    report = json.loads(out)
    assert report["claimed"] == "t-cut" and "lease_lost" not in report
    assert report["beat_failures"] >= 3
    assert err.count('"beat_failed"') >= 3
    settled = raise_of(schema, "t-cut")
    assert (settled["status"], settled["task_status"]) == ("ok", "complete")


def closed_under(schema: str, execution: str) -> None:
    """Close a running raise the way the sweep closes one whose lease lapsed,
    and put its task back."""
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"""update {schema}.task_executions
                            set status = 'abandoned', ended_at = now(),
                                detail = 'lease lapsed before release'
                          where id::text = %s""", (execution,))
        conn.execute(f"""update {schema}.tasks set status = 'todo'
                          where id = (select task_id from {schema}.task_executions
                                       where id::text = %s)""", (execution,))


def gone(pids: list[int]) -> bool:
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                               capture_output=True, text=True).stdout.strip()
        if state and not state.startswith("Z"):
            return False
    return True


@needs_store
def test_a_turn_whose_raise_is_closed_under_it_is_ended_at_once(lab, tmp_path):
    """The raise a live turn holds is closed by a sweep, as one is when a
    machine slept past its lease. The turn learns it at its next beat, is ended
    with what it started, says so, and the service's log line names it."""
    schema = schema_of(lab)
    lab["env"]["FAKE_ENGINE_SLEEP"] = "120"
    lab["env"]["TASKS_STORE_RETRY_SECONDS"] = "3"   # a beat every 2s, a lease of 9s
    add(lab, "t-swept")
    turn = start_run(lab)
    try:
        first = poll_for(lambda: raise_of(schema, "t-swept"), 60)
        below = poll_for(lambda: descendants(turn.pid) or None, 30)
        closed_under(schema, first["id"])
        out, err = turn.communicate(timeout=20)
    finally:
        if turn.poll() is None:
            os.killpg(turn.pid, signal.SIGKILL)
    report = json.loads(out)
    assert report["lease_lost"] == "its raise is abandoned, no longer running", report
    assert report["release_refused"] == "that raise is not open"
    assert '"lease_lost"' in err
    poll_for(lambda: gone(below), 10)
    settled = raise_of(schema, "t-swept")
    assert (settled["status"], settled["task_status"]) == ("abandoned", "todo")

    output, errors = tmp_path / "turn.out", tmp_path / "turn.err"
    output.write_text(out)
    errors.write_text(err)
    said = mod._service_module().ProjectSlot._said(
        SimpleNamespace(output=output, errors=errors))
    assert "lease_lost its raise is abandoned, no longer running" in said


@needs_store
def test_a_turn_whose_lease_runs_out_unrenewed_is_ended(lab, relay, tmp_path):
    """The store stays away past the lease the last renewal wrote. The turn
    does not work on unclaimed: it is ended once that lease has run out, with
    every failed renewal counted."""
    schema = schema_of(lab)
    lab["env"]["FAKE_ENGINE_SLEEP"] = "120"
    add(lab, "t-away")
    relayed(lab, relay, 6)         # a beat every 2s, a lease of 12s
    said = tmp_path / "away.err"
    with said.open("w") as errors:
        turn = subprocess.Popen([str(_cli.CLI_PATH), "run", "alpha", "--apply"],
                                cwd=lab["project"], env=lab["env"], text=True,
                                stdout=subprocess.PIPE, stderr=errors,
                                start_new_session=True)
    try:
        poll_for(lambda: raise_of(schema, "t-away"), 60)
        below = poll_for(lambda: descendants(turn.pid) or None, 30)
        relay.down()
        away = time.monotonic()
        poll_for(lambda: '"lease_lost"' in said.read_text(), 30)
        assert time.monotonic() - away < 20
        # Back at once, so the run can settle what it held once the turn is gone.
        relay.up()
        poll_for(lambda: gone(below), 10)
        out, _ = turn.communicate(timeout=60)
    finally:
        if turn.poll() is None:
            os.killpg(turn.pid, signal.SIGKILL)
    report = json.loads(out)
    assert report["lease_lost"].startswith(
        "its lease ran out with no renewal reaching the store"), report
    assert report["beat_failures"] >= 3
    assert '"beat_failed"' in said.read_text()


# --- A raise left open -------------------------------------------------------

@needs_store
def test_a_claim_passes_over_a_task_an_open_raise_holds(lab, tmp_path):
    add(lab, "t-held")
    add(lab, "t-next")
    taken = answer_of(cli(lab, "claim", "--type", "alpha", "--worker", "alpha"))
    assert taken["task"]["unique_key"] == "t-held"
    execution = str(taken["execution"]["id"])
    # The turn moved the task on and was never released.
    answer_of(cli(lab, "set", "t-held", "--status", "todo"))

    took = answer_of(cli(lab, "claim", "--type", "alpha"))
    assert took["task"]["unique_key"] == "t-next"
    [passed] = took["passed_over"]
    assert (passed["task"], passed["verdict"], passed["execution"]) == (
        "t-held", "held", execution)
    assert f"its raise {execution} of worker 'alpha' is still open" in passed["why"]

    nothing = answer_of(cli(lab, "claim", "--type", "alpha"))
    assert nothing["claimed"] is None
    assert [one["task"] for one in nothing["passed_over"]] == ["t-held"]

    for args in (["claim", "--key", "t-held"], ["run", "alpha", "--key", "t-held", "--apply"]):
        refused = cli(lab, *args)
        assert refused.returncode == 6, refused.stdout + refused.stderr
        error = json.loads(refused.stderr.splitlines()[-1])["error"]
        assert error["code"] == "conflict"
        assert f"t-held is still held by its raise {execution}" in error["message"]
        assert "runs t-held" in error["hint"]

    ran = cli(lab, "run", "alpha", "--apply")
    report = answer_of(ran)
    assert report["claimed"] is None
    assert [one["task"] for one in report["passed_over"]] == ["t-held"]
    # The service's log line for that turn says so.
    output, errors = tmp_path / "turn.out", tmp_path / "turn.err"
    output.write_text(ran.stdout)
    errors.write_text("")
    said = mod._service_module().ProjectSlot._said(
        SimpleNamespace(output=output, errors=errors))
    assert "claimed nothing" in said
    assert any(line.startswith("passed over t-held: held (its raise ") for line in said)

    # Once the raise is released, the task is taken again.
    answer_of(cli(lab, "release", execution, "--outcome", "failed"))
    again = answer_of(cli(lab, "claim", "--key", "t-held"))
    assert again["task"]["unique_key"] == "t-held" and "passed_over" not in again
