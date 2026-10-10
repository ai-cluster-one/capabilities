#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract==0.4.0",
#                 "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""On a Mac a conveyor turn keeps the machine out of idle sleep for as long as
it runs: the daemon holds an assertion beside every turn it starts or adopts,
lets go of it when the turn ends, and holds none while no turn runs. Off a Mac
it takes none.

The holder is checked one step at a time with a stand-in command, on any
platform, and with the real `caffeinate` on a Mac. The daemon is checked through
the CLI with real `tasks run` children held open on the stand-in harness; those
checks run on a Mac only, read TASKS_TEST_DSN and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'capabilities-contract==0.4.0' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_service as base  # noqa: E402
import test_turns_outlive as outlive  # noqa: E402
from test_service import lab  # noqa: E402,F401  (fixture)

mod = base.mod
cli, answer_of, poll_for = base.tasks_cli, base.answer_of, base.poll_for
on_a_mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


@pytest.fixture
def service():
    return mod._service_module()


@pytest.fixture
def sleeper():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield process
    if process.poll() is None:
        process.kill()
    process.wait()


def holders_of(pid: int) -> list[int]:
    """The pids of every `caffeinate -i -w` watching `pid`."""
    said = subprocess.run(["ps", "-A", "-o", "pid=,command="], capture_output=True,
                          text=True).stdout
    wanted = f"/usr/bin/caffeinate -i -w {pid}"
    return [int(line.split(None, 1)[0]) for line in said.splitlines()
            if line.split(None, 1)[1:] == [wanted]]


def asserted_by(pid: int) -> bool:
    """Whether the process `pid` holds an idle-sleep assertion."""
    said = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True).stdout
    return any(line.strip().startswith(f"pid {pid}(") and "PreventUserIdleSystemSleep" in line
               for line in said.splitlines())


# --- The holder -------------------------------------------------------------

def test_off_a_mac_no_holder_is_started(service, sleeper, monkeypatch):
    monkeypatch.setattr(service, "HOLD_AWAKE", False)
    monkeypatch.setattr(service, "AWAKE_COMMAND", ("/nonexistent/should-not-run",))
    assert service.hold_awake(sleeper.pid) is None
    service.let_sleep(None)


def test_the_holder_watches_the_turn_in_a_session_of_its_own_and_is_let_go(
        service, sleeper, monkeypatch):
    monkeypatch.setattr(service, "HOLD_AWAKE", True)
    monkeypatch.setattr(service, "AWAKE_COMMAND", (
        sys.executable, "-c", "import sys, time; time.sleep(60)", "-w"))
    holder = service.hold_awake(sleeper.pid)
    try:
        assert holder.args[-2:] == ["-w", str(sleeper.pid)]
        assert holder.poll() is None
        assert os.getsid(holder.pid) == holder.pid != os.getsid(0)
        service.let_sleep(holder)
        assert holder.poll() is not None
    finally:
        if holder.poll() is None:
            holder.kill()


def test_a_holder_that_cannot_start_leaves_the_turn_alone(service, sleeper, monkeypatch):
    monkeypatch.setattr(service, "HOLD_AWAKE", True)
    monkeypatch.setattr(service, "AWAKE_COMMAND", ("/nonexistent/caffeinate",))
    assert service.hold_awake(sleeper.pid) is None
    assert sleeper.poll() is None


@on_a_mac
def test_on_a_mac_caffeinate_asserts_until_the_turn_ends(service, sleeper):
    holder = service.hold_awake(sleeper.pid)
    try:
        assert holders_of(sleeper.pid) == [holder.pid]
        poll_for(lambda: asserted_by(holder.pid), 5)
        sleeper.kill()
        sleeper.wait()
        # It lets go on its own once the turn is gone, whoever started it.
        assert holder.wait(timeout=5) is not None
        assert not asserted_by(holder.pid)
    finally:
        if holder.poll() is None:
            holder.kill()


# --- The daemon --------------------------------------------------------------

@on_a_mac
@base.needs_store
def test_the_daemon_holds_the_mac_awake_only_while_a_turn_runs(lab):
    outlive.held_lab(lab)
    pid = answer_of(cli(lab, "service", "start"))["pid"]
    try:
        # Serving, and no turn running: nothing is held.
        assert not [line for line in subprocess.run(
            ["ps", "-A", "-o", "ppid=,command="], capture_output=True, text=True
        ).stdout.splitlines() if line.split(None, 1)[0] == str(pid) and "caffeinate" in line]
        outlive.add(lab, "t-awake")
        row = outlive.working(lab, "t-awake")
        [holder] = holders_of(row["pid"])
        assert asserted_by(holder)
        outlive.complete(lab, "t-awake")
        outlive.gone_within(row["pid"], 15)
        outlive.gone_within(holder, 5)
    finally:
        answer_of(cli(lab, "service", "stop"))


@on_a_mac
@base.needs_store
def test_a_turn_adopted_after_a_crash_is_held_awake_too(lab):
    outlive.held_lab(lab, held=16)
    pid = answer_of(cli(lab, "service", "start"))["pid"]
    outlive.add(lab, "t-adopted")
    row = outlive.working(lab, "t-adopted")
    [first] = holders_of(row["pid"])
    os.kill(pid, signal.SIGKILL)
    outlive.gone_within(pid, 3)
    # The holder outlives the daemon that started it, as the turn does.
    assert outlive.alive(first) and asserted_by(first)
    try:
        answer_of(cli(lab, "service", "start"))
        held = poll_for(lambda: (lambda found: found if len(found) == 2 else None)(
            holders_of(row["pid"])), 10)
        assert first in held
        outlive.complete(lab, "t-adopted")
        outlive.gone_within(row["pid"], 15)
        for holder in held:
            outlive.gone_within(holder, 5)
    finally:
        answer_of(cli(lab, "service", "stop"))
