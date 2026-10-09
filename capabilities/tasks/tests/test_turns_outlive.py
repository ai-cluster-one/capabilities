#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.3.0",
#                 "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""Running turns outlive the service: every way the daemon stops leaves them
running, the next daemon adopts them, and `service stop --end-turns` is the one
act that ends them.

Most checks run the real daemon through the CLI over a project the service
tests write, with real `tasks run` children on the stand-in harness held open
for several seconds, so a stop, a signal and a crash are observed against live
processes. What a new daemon makes of the turn records it finds is checked one
step at a time over records the test writes. The store-backed checks read
TASKS_TEST_DSN and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'capabilities-contract==0.3.0' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402
import test_service as base  # noqa: E402
from test_service import lab, project, store  # noqa: E402,F401  (fixtures)

mod = base.mod
needs_store = base.needs_store
cli, answer_of, poll_for = base.tasks_cli, base.answer_of, base.poll_for

# How long the stand-in holds each turn open before it finishes its task.
HELD = 10


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def lstart(pid: int) -> str:
    said = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                          text=True, env={**os.environ, "LC_ALL": "C", "LANG": "C"}).stdout
    return " ".join(said.split())


def held_lab(lab, *, grace: int = 5, held: int = HELD) -> dict:
    """The lab with its service initialised, polling once an hour, and each
    turn held open for `held` seconds."""
    lab["env"]["FAKE_ENGINE_SLEEP"] = str(held)
    lab["env"]["TASKS_ACTOR"] = "ops-person"
    assert answer_of(cli(lab, "service", "init"))["written"]
    base.write_settings(lab["project"], f"version = 1\npoll_seconds = 3600\n"
                                        f"shutdown_grace_seconds = {grace}\n")
    return lab


def add(lab, key: str) -> None:
    answer_of(cli(lab, "add", "--type", "alpha", "--title", key, "--key", key,
                  "--status", "todo"))


def working(lab, key: str) -> dict:
    """The status turn row once the turn holds `key`."""
    def found():
        status = answer_of(cli(lab, "service", "status"))
        return next((row for row in status.get("turns") or []
                     if row["task"] == key and row["phase"] == "working"), None)
    return poll_for(found, 30)


def status_of(lab, key: str) -> str:
    return answer_of(cli(lab, "show", key))["task"]["status"]


def complete(lab, key: str, seconds: float = 60) -> None:
    poll_for(lambda: status_of(lab, key) == "complete", seconds)


def runs(lab, key: str) -> list[dict]:
    return answer_of(cli(lab, "runs", key))["executions"]


def log_of(lab) -> str:
    return "\n".join(answer_of(cli(lab, "service", "logs", "--tail", "500"))["lines"])


def gone_within(pid: int, seconds: float) -> float:
    started = time.monotonic()
    while alive(pid):
        assert time.monotonic() - started < seconds, f"pid {pid} still alive"
        time.sleep(0.05)
    return time.monotonic() - started


def files_of(state: Path, turn_id: str) -> list[str]:
    return sorted(path.name for path in (state / "turns").iterdir() if turn_id in path.name)


# --- The turn record --------------------------------------------------------

@needs_store
def test_every_spawn_writes_a_record_a_new_daemon_keeps(lab):
    held_lab(lab)
    answer_of(cli(lab, "service", "start"))
    add(lab, "t-record")
    row = working(lab, "t-record")
    state = Path(answer_of(cli(lab, "service", "status"))["state_dir"])
    record_path = state / "turns" / f"{row['id']}.json"
    record = json.loads(record_path.read_text())
    slug = json.loads((lab["project"] / "capabilities" / "project.json").read_text())["slug"]
    assert set(record) == {"id", "project", "worker", "pid", "lstart", "receipt", "output",
                           "errors", "started_at"}
    assert (record["id"], record["project"], record["worker"], record["pid"],
            record["started_at"]) == (row["id"], slug, "alpha", row["pid"], row["started_at"])
    assert record["lstart"] == lstart(row["pid"]) and record["lstart"]
    assert record["receipt"] == str(state / "turns" / f"{row['id']}.claim.json")
    assert record["output"] == str(state / "turns" / f"{row['id']}.out")
    assert record["errors"] == str(state / "turns" / f"{row['id']}.err")
    assert all(Path(record[key]).is_file() for key in ("receipt", "output", "errors"))

    answer_of(cli(lab, "service", "stop"))
    assert alive(row["pid"]) and record_path.is_file()
    answer_of(cli(lab, "service", "start"))
    # A new daemon opening keeps the record of a turn still running.
    assert json.loads(record_path.read_text()) == record
    complete(lab, "t-record")
    poll_for(lambda: not record_path.exists(), 15)


# --- Every way the daemon stops leaves its turns running --------------------

@needs_store
@pytest.mark.parametrize("how", ["stop", "sigterm"])
def test_stop_and_sigterm_leave_the_turn_running_to_settle_itself(lab, how):
    held_lab(lab)
    pid = answer_of(cli(lab, "service", "start"))["pid"]
    add(lab, "t-left")
    row = working(lab, "t-left")
    signalled = time.monotonic()
    if how == "stop":
        stopped = answer_of(cli(lab, "service", "stop"))
        assert stopped["stopped"] is True and stopped["waited_seconds"] < 2.5
    else:
        os.kill(pid, signal.SIGTERM)
    gone_within(pid, 3)
    assert time.monotonic() - signalled < 15
    assert answer_of(cli(lab, "service", "status"))["running"] is False
    assert alive(row["pid"]) and status_of(lab, "t-left") == "in_progress"
    complete(lab, "t-left")
    gone_within(row["pid"], 15)
    [raised] = runs(lab, "t-left")
    assert (raised["status"], raised["worker"]) == ("ok", "alpha")
    log = log_of(lab)
    assert "stopping: leaving 1 running turn(s) running" in log
    assert "cut off" not in log


@needs_store
@pytest.mark.parametrize("how", ["wrapper_sigterm", "foreground_sigint"])
def test_sigterm_to_the_wrapper_and_ctrl_c_leave_the_turn_running(lab, how):
    """`service run` as a supervisor runs it: the executable, which its shebang
    runs under `uv run --script`. SIGTERM goes to that wrapper, as launchd sends
    it; SIGINT goes to the whole foreground process group, as Ctrl-C does."""
    held_lab(lab)
    out = (lab["tmp"] / f"run-{how}.log").open("w")
    wrapper = subprocess.Popen([str(_cli.CLI_PATH), "service", "run"], cwd=lab["project"],
                               env=lab["env"], stdin=subprocess.DEVNULL, stdout=out,
                               stderr=out, start_new_session=True)
    try:
        status = poll_for(lambda: (lambda s: s if s["running"] else None)(
            answer_of(cli(lab, "service", "status"))), 60)
        daemon = status["pid"]
        said = subprocess.run(["ps", "-o", "command=", "-p", str(wrapper.pid)],
                              capture_output=True, text=True).stdout
        assert said.startswith("uv run --script"), said
        assert daemon != wrapper.pid
        add(lab, "t-signal")
        row = working(lab, "t-signal")
        if how == "wrapper_sigterm":
            os.kill(wrapper.pid, signal.SIGTERM)
        else:
            os.killpg(wrapper.pid, signal.SIGINT)
        gone_within(daemon, 3)
        assert wrapper.wait(timeout=10) == 0
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
        out.close()
    assert alive(row["pid"]) and status_of(lab, "t-signal") == "in_progress"
    complete(lab, "t-signal")
    [raised] = runs(lab, "t-signal")
    assert raised["status"] == "ok"
    assert "cut off" not in log_of(lab)


# --- A crash, and the daemon that adopts ------------------------------------

@needs_store
def test_a_crashed_daemons_turn_is_adopted_counted_and_let_go_when_it_ends(lab):
    held_lab(lab, held=16)
    pid = answer_of(cli(lab, "service", "start"))["pid"]
    add(lab, "t-first")
    row = working(lab, "t-first")
    add(lab, "t-second")
    os.kill(pid, signal.SIGKILL)
    gone_within(pid, 3)
    assert alive(row["pid"])
    again = answer_of(cli(lab, "service", "start"))
    assert again["pid"] != pid
    state = Path(again["state_dir"])
    # The lane takes one turn at a time, and the adopted one is it.
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        status = answer_of(cli(lab, "service", "status"))
        [adopted] = status["turns"]
        assert (adopted["id"], adopted["pid"], adopted["adopted"], adopted["task"]) == (
            row["id"], row["pid"], True, "t-first")
        assert status["lanes"][0]["running"] == 1
        assert status_of(lab, "t-second") == "todo"
        time.sleep(0.5)
    complete(lab, "t-first")
    gone_within(row["pid"], 15)
    log = poll_for(lambda: (lambda text: text if f"turn {row['id']} ended" in text else None)(
        log_of(lab)), 15)
    assert f"turn {row['id']} adopted: worker alpha, pid {row['pid']}" in log
    assert f"turn {row['id']} ended: worker alpha, exit unknown, adopted, claimed t-first" in log
    assert files_of(state, row["id"]) == []
    # The lane is free again, and the second task gets its own turn.
    complete(lab, "t-second", 90)
    assert [r["status"] for r in runs(lab, "t-first")] == ["ok"]


# --- Records whose process is gone ------------------------------------------

def _record(turns: Path, turn_id: str, pid: int, started: str, answer: dict | None) -> None:
    for suffix, text in ((".claim.json", json.dumps({"task": f"t-{turn_id}"})),
                         (".out", json.dumps(answer) if answer else ""), (".err", "")):
        (turns / f"{turn_id}{suffix}").write_text(text)
    (turns / f"{turn_id}.json").write_text(json.dumps({
        "id": turn_id, "project": "lab", "worker": "alpha", "pid": pid, "lstart": started,
        "receipt": str(turns / f"{turn_id}.claim.json"), "output": str(turns / f"{turn_id}.out"),
        "errors": str(turns / f"{turn_id}.err"), "started_at": "2026-01-01T00:00:00+00:00"}))


@needs_store
def test_a_gone_or_reused_pid_is_logged_as_ended_unwatched_and_never_adopted(
        project, store, capsys):
    entry, schema, conn = store
    base.write_settings(project, "version = 1\npoll_seconds = 3600\n")
    h = base.Harness(project, entry)
    turns = h.daemon.turns_dir
    turns.mkdir(parents=True, exist_ok=True)
    ended = subprocess.Popen(["true"])
    ended.wait()
    _record(turns, "aaaaaaaaaaaa", ended.pid, "Thu Jan  1 00:00:00 2026",
            {"claimed": "t-aaaaaaaaaaaa", "execution": "e1"})
    reused = subprocess.Popen(["sleep", "30"])
    live = subprocess.Popen(["sleep", "30"])
    try:
        _record(turns, "bbbbbbbbbbbb", reused.pid, "Thu Jan  1 00:00:00 2026", None)
        _record(turns, "cccccccccccc", live.pid, lstart(live.pid), None)
        (turns / "stray.out").write_text("left by a daemon before records")
        with h:
            log = (h.daemon.state_dir / "daemon.log").read_text()
            assert (f"turn aaaaaaaaaaaa ended while unwatched: worker alpha, "
                    f"pid {ended.pid}, exit unknown, claimed t-aaaaaaaaaaaa") in log
            assert (f"turn bbbbbbbbbbbb ended while unwatched: worker alpha, "
                    f"pid {reused.pid}, exit unknown") in log
            assert "adopted 1 running turn(s)" in log
            assert "cleared 1 file(s) in turns/ that no turn record names" in log
            assert list(h.daemon.turns) == ["cccccccccccc"]
            [row] = h.daemon.status()["turns"]
            assert (row["pid"], row["adopted"], row["task"]) == (live.pid, True,
                                                                 "t-cccccccccccc")
            # A pid that is another process now is left alone: never adopted,
            # never ended.
            assert reused.poll() is None
            assert files_of(turns.parent, "aaaaaaaaaaaa") == []
            assert files_of(turns.parent, "bbbbbbbbbbbb") == []
            assert not (turns / "stray.out").exists()
            live.kill()
            live.wait()
            h.steps(lambda: not h.daemon.turns, seconds=5)
            log = (h.daemon.state_dir / "daemon.log").read_text()
            assert "turn cccccccccccc ended: worker alpha, exit unknown, adopted" in log
            assert files_of(turns.parent, "cccccccccccc") == []
    finally:
        for process in (reused, live):
            process.kill()
            process.wait()


# --- Ending running turns is one explicit act --------------------------------

@needs_store
def test_stop_end_turns_writes_the_intent_then_ends_and_settles_the_turn(lab):
    held_lab(lab, grace=2, held=40)
    pid = answer_of(cli(lab, "service", "start"))["pid"]
    add(lab, "t-ended")
    row = working(lab, "t-ended")
    state = Path(answer_of(cli(lab, "service", "status"))["state_dir"])
    intent_path = state / "end-turns.json"
    stopping = subprocess.Popen([str(_cli.CLI_PATH), "service", "stop", "--end-turns"],
                                cwd=lab["project"], env=lab["env"], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def written():
        try:
            return json.loads(intent_path.read_text())
        except (OSError, ValueError):
            return None
    intent = poll_for(written, 30)
    assert intent["by"] == "ops-person"
    moment = datetime.datetime.fromisoformat(intent["at"])
    assert abs((datetime.datetime.now(datetime.timezone.utc) - moment).total_seconds()) < 30
    out, err = stopping.communicate(timeout=60)
    assert stopping.returncode == 0, err
    stopped = json.loads(out)
    assert stopped["stopped"] is True and stopped["end_turns"] == {"by": "ops-person",
                                                                   "at": intent["at"]}
    assert 2 <= stopped["waited_seconds"] < 15
    assert not alive(pid) and not alive(row["pid"])
    assert not intent_path.exists()
    # Settled exactly as a stop settled it before: the raise closed as
    # abandoned by the cut-off, the task back in todo.
    [raised] = runs(lab, "t-ended")
    assert raised["status"] == "abandoned"
    assert status_of(lab, "t-ended") == "todo"
    log = log_of(lab)
    asked = log.index("end-turns asked by ops-person")
    taken = log.index(f"ending running turns, as ops-person asked at {intent['at']}")
    assert asked < taken
    assert f"turn {row['id']} cut off after 2s: worker alpha, task t-ended, settled as a " \
           "lapsed lease" in log
    assert files_of(state, row["id"]) == []


@needs_store
def test_an_intent_older_than_the_stop_timeout_is_ignored(lab):
    held_lab(lab, grace=2)
    pid = answer_of(cli(lab, "service", "start"))["pid"]
    add(lab, "t-stale")
    row = working(lab, "t-stale")
    state = Path(answer_of(cli(lab, "service", "status"))["state_dir"])
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(seconds=120)).isoformat(timespec="seconds")
    (state / "end-turns.json").write_text(json.dumps(
        {"by": "someone-earlier", "at": old, "timeout_seconds": 32}))
    os.kill(pid, signal.SIGTERM)
    gone_within(pid, 3)
    assert alive(row["pid"])
    assert not (state / "end-turns.json").exists()
    complete(lab, "t-stale")
    assert [r["status"] for r in runs(lab, "t-stale")] == ["ok"]
    log = log_of(lab)
    assert f"ignored the end-turns intent someone-earlier wrote at {old}" in log
    assert "cut off" not in log


# --- Never claimed twice ----------------------------------------------------

@needs_store
def test_a_turn_left_running_or_adopted_is_never_claimed_twice(lab):
    held_lab(lab, held=14)
    answer_of(cli(lab, "service", "start"))
    add(lab, "t-once")
    row = working(lab, "t-once")
    answer_of(cli(lab, "service", "stop"))
    # No service at all: a run by hand finds nothing to claim.
    assert answer_of(cli(lab, "run", "alpha", "--apply"))["claimed"] is None
    answer_of(cli(lab, "service", "start"))
    [adopted] = answer_of(cli(lab, "service", "status"))["turns"]
    assert (adopted["id"], adopted["adopted"]) == (row["id"], True)
    # Adopted: neither the daemon nor a run by hand claims it.
    assert answer_of(cli(lab, "run", "alpha", "--apply"))["claimed"] is None
    time.sleep(2)
    assert [t["id"] for t in answer_of(cli(lab, "service", "status"))["turns"]] == [row["id"]]
    [raised] = runs(lab, "t-once")
    assert raised["status"] == "running"
    complete(lab, "t-once")
    assert [r["status"] for r in runs(lab, "t-once")] == ["ok"]
