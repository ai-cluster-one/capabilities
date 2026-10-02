#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""The service's pause: no new turn on the lanes it holds, while the daemon
keeps running and running turns finish.

What `pause` and `resume` write, refuse and leave alone is checked with no
store, over the project the service tests write. The daemon is then driven one
step at a time over a real store with each turn the service tests' stand-in, and
once through the CLI with real `tasks run` children on the stand-in harness.
The store-backed checks read TASKS_TEST_DSN and skip when it is unset.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_service as base  # noqa: E402
from test_service import lab, project, store  # noqa: E402,F401  (fixtures)

mod = base.mod
needs_store = base.needs_store


@pytest.fixture(autouse=True)
def who(monkeypatch):
    monkeypatch.setenv("TASKS_ACTOR", "pause-tester")
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.delenv(mod.READ_ONLY_ENV, raising=False)


def pause(capsys, *args: str) -> dict:
    mod.cmd_service_pause(list(args))
    return json.loads(capsys.readouterr().out)


def resume(capsys, *args: str) -> dict:
    mod.cmd_service_resume(list(args))
    return json.loads(capsys.readouterr().out)


def refusal(capsys, verb, *args: str) -> dict:
    with pytest.raises(SystemExit) as ended:
        verb(list(args))
    assert ended.value.code == 6
    return json.loads(capsys.readouterr().err)["error"]


# --- What the verbs write ----------------------------------------------------

def test_a_pause_is_runtime_state_and_moves_no_fingerprint(project, capsys):
    before = mod._service_fingerprint()
    said = pause(capsys, "--reason", "maintenance window")
    assert said["paused"] == "every lane" and said["running"] is False
    assert (said["reason"], said["by"]) == ("maintenance window", "pause-tester")
    assert said["pause"]["holding"] == ["alpha", "beta"]
    assert mod._service_fingerprint() == before
    state = mod._service_state_dir()
    assert (state / "paused.json").is_file()
    assert not (project / "capabilities" / "tasks" / "service" / "config.toml").read_text() \
        .count("pause")
    status = mod._service_status(mod._service_module())
    assert status["running"] is False
    assert status["pause"]["paused"] is True
    assert status["pause"]["all"]["reason"] == "maintenance window"
    assert status["pause"]["all"]["by"] == "pause-tester"
    assert status["pause"]["all"]["at"]
    assert status["pause"]["lanes"] == {} and status["pause"]["holding"] == ["alpha", "beta"]
    log = (state / "daemon.log").read_text()
    assert "pause set by pause-tester on every lane: maintenance window" in log
    resumed = resume(capsys)
    assert resumed["changed"] is True and resumed["pause"]["paused"] is False
    assert not (state / "paused.json").exists()
    assert mod._service_fingerprint() == before
    assert "pause lifted by pause-tester on every lane" in (state / "daemon.log").read_text()
    # Nothing to lift is no change and no log line.
    assert resume(capsys)["changed"] is False
    assert (state / "daemon.log").read_text().count("pause lifted") == 1


def test_lanes_are_paused_and_resumed_by_name(project, capsys):
    held = pause(capsys, "beta")
    assert held["paused"] == ["beta"] and held["pause"]["holding"] == ["beta"]
    assert set(held["pause"]["lanes"]) == {"beta"} and held["pause"]["all"] is None
    assert pause(capsys, "alpha", "alpha")["pause"]["holding"] == ["alpha", "beta"]
    assert resume(capsys, "alpha")["pause"]["holding"] == ["beta"]
    assert resume(capsys, "beta")["pause"]["paused"] is False


def test_resuming_one_lane_under_a_pause_on_every_lane_keeps_the_others(project, capsys):
    pause(capsys, "--reason", "everything")
    said = resume(capsys, "alpha")
    assert said["pause"]["all"] is None
    assert said["pause"]["holding"] == ["beta"]
    assert said["pause"]["lanes"]["beta"]["reason"] == "everything"
    assert "beta stay paused by name" in (mod._service_state_dir() / "daemon.log").read_text()


@pytest.mark.parametrize("named", ["nobody", "triage", "default"])
def test_a_name_that_is_not_a_lane_is_refused(project, capsys, named):
    error = refusal(capsys, mod.cmd_service_pause, named)
    assert error["code"] == "lane_unknown" and repr(named) in error["message"]
    assert "alpha, beta" in error["hint"]
    assert not (mod._service_state_dir() / "paused.json").exists()
    assert refusal(capsys, mod.cmd_service_resume, named)["code"] == "lane_unknown"


def test_a_lane_no_longer_declared_can_still_be_resumed(project, capsys):
    pause(capsys, "beta")
    (project / "capabilities" / "tasks" / "workers" / "beta.md").unlink()
    assert refusal(capsys, mod.cmd_service_pause, "beta")["code"] == "lane_unknown"
    assert resume(capsys, "beta")["pause"]["paused"] is False


def test_the_whole_service_pauses_even_when_the_declaration_does_not_load(project, capsys):
    base.write_settings(project, "version = 1\nbogus = 1\n")
    said = pause(capsys)
    assert said["pause"]["all"]["by"] == "pause-tester" and said["pause"]["holding"] is None
    status = mod._service_status(mod._service_module())
    assert "bogus" in status["declaration_error"] and status["pause"]["paused"] is True
    with pytest.raises(SystemExit) as ended:
        mod.cmd_service_pause(["alpha"])
    assert ended.value.code == 6
    capsys.readouterr()


def test_both_verbs_are_refused_under_the_read_only_switch(project, capsys, monkeypatch):
    monkeypatch.setenv(mod.READ_ONLY_ENV, "1")
    for verb in (mod.cmd_service_pause, mod.cmd_service_resume):
        with pytest.raises(SystemExit) as ended:
            verb([])
        assert ended.value.code == 4
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "read_only_switch"
    assert not (mod._service_state_dir() / "paused.json").exists()


# --- The daemon, step by step ------------------------------------------------

def started(h) -> int:
    return len(list(h.claims.glob("started-*")))


@pytest.fixture
def quick(monkeypatch):
    """The daemon in these tests runs in this process and is stepped by it, so
    the verbs do not wait for it to take the pause up; each test steps it."""
    monkeypatch.setattr(mod, "_pause_wait", lambda *args: None)


@needs_store
def test_a_pause_starts_no_new_turn_and_leaves_the_running_one_to_finish(
        project, store, capsys, quick):
    entry, schema, conn = store
    base.write_settings(project, "version = 1\npoll_seconds = 1\nmax_parallel = 4\n")
    base.add(entry, capsys, "alpha")
    with base.Harness(project, entry) as h:
        h.steps(lambda: h.running("alpha") == 1 and all(
            t.phase == "working" for t in h.daemon.turns.values()))
        [turn] = h.daemon.turns.values()
        pause(capsys, "--reason", "hold")
        h.daemon.step()
        assert h.daemon.status()["pause"]["holding"] == ["alpha", "beta"]
        assert h.daemon.status()["pause"]["all"]["reason"] == "hold"
        base.add(entry, capsys, "beta")
        count = started(h)
        h.settle(2.5)  # past a poll and a notification
        assert started(h) == count and h.running("beta") == 0
        assert turn.process.poll() is None and h.daemon.turns[turn.id] is turn
        # The running turn finishes on its own, and nothing replaces it.
        h.release.write_text("go")
        h.steps(lambda: not h.daemon.turns)
        assert turn.process.returncode == 0
        h.settle(1.5)
        assert started(h) == count and h.running() == 0
        resume(capsys)
        h.steps(lambda: started(h) > count)
        assert "resume" in h.daemon.last_wake["reasons"]
    log = (h.daemon.state_dir / "daemon.log").read_text()
    assert "paused: starting no turn on alpha, beta; 1 running turn(s) left to finish" in log
    assert "resumed: alpha, beta start turns again" in log


@needs_store
def test_a_paused_lane_waits_while_the_others_run(project, store, capsys, quick):
    entry, schema, conn = store
    base.write_settings(project, "version = 1\npoll_seconds = 1\nmax_parallel = 4\n")
    pause(capsys, "beta")
    base.add(entry, capsys, "alpha")
    base.add(entry, capsys, "beta")
    with base.Harness(project, entry) as h:
        h.steps(lambda: h.running("alpha") == 1)
        h.settle(2.5)
        assert h.running("beta") == 0 and h.running("alpha") == 1
        status = h.daemon.status()
        assert status["pause"]["holding"] == ["beta"]
        assert status["pause"]["lanes"]["beta"]["by"] == "pause-tester"
        resume(capsys, "beta")
        h.steps(lambda: h.running("beta") == 1, seconds=3)


@needs_store
def test_the_pause_holds_across_a_daemon_restart(project, store, capsys, quick):
    entry, schema, conn = store
    base.write_settings(project, "version = 1\npoll_seconds = 1\n")
    base.add(entry, capsys, "alpha")
    pause(capsys, "--reason", "over a restart")
    with base.Harness(project, entry) as h:
        h.settle(2.5)
        assert h.running() == 0
    # A second daemon on the same state directory, as a supervisor restart is.
    with base.Harness(project, entry) as h:
        h.release.unlink(missing_ok=True)
        h.settle(2.5)
        assert h.running() == 0 and started(h) == 0
        assert h.daemon.status()["pause"]["all"]["reason"] == "over a restart"
        resume(capsys)
        h.steps(lambda: h.running("alpha") == 1, seconds=3)
    log = (h.daemon.state_dir / "daemon.log").read_text()
    assert log.count("; paused, starting no turn on alpha, beta") == 2


# --- Through the CLI ---------------------------------------------------------

@needs_store
def test_a_running_daemon_is_paused_and_resumed_through_the_cli(lab):
    cli, answer_of = base.tasks_cli, base.answer_of
    project = lab["project"]
    lab["env"]["TASKS_ACTOR"] = "ops-person"
    assert answer_of(cli(lab, "service", "init"))["written"]
    base.write_settings(project, "version = 1\npoll_seconds = 3600\n"
                                 "shutdown_grace_seconds = 5\n")
    pid = answer_of(cli(lab, "service", "start"))["pid"]
    assert answer_of(cli(lab, "service", "doctor"))["service"]["current"] is True

    unknown = cli(lab, "service", "pause", "nobody")
    assert unknown.returncode == 6
    assert json.loads(unknown.stderr)["error"]["code"] == "lane_unknown"

    held = answer_of(cli(lab, "service", "pause", "--reason", "upgrade"))
    assert held["running"] is True and held["pause"]["taken_up"] is True
    status = answer_of(cli(lab, "service", "status"))
    assert status["pid"] == pid and status["current"] is True
    assert status["pause"]["holding"] == ["alpha"]
    assert (status["pause"]["all"]["reason"], status["pause"]["all"]["by"]) == (
        "upgrade", "ops-person")
    assert status["pause"]["all"]["at"] and status["pause"]["taken_up"] is True
    doctor = cli(lab, "service", "doctor")
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    reported = json.loads(doctor.stdout)
    assert reported["ok"] is True and "declaration_stale" not in reported
    assert reported["pause"]["paused"] is True

    answer_of(cli(lab, "add", "--type", "alpha", "--title", "held", "--key", "t-held",
                  "--status", "todo"))
    time.sleep(3)
    assert answer_of(cli(lab, "show", "t-held"))["task"]["status"] == "todo"
    assert answer_of(cli(lab, "service", "status"))["turns"] == []

    resumed = answer_of(cli(lab, "service", "resume"))
    assert resumed["changed"] is True and resumed["pause"]["taken_up"] is True
    base.poll_for(lambda: answer_of(cli(lab, "show", "t-held"))["task"]["status"]
                  == "complete", 60)

    # A run by hand is not the service, and the pause does not hold it.
    answer_of(cli(lab, "service", "pause"))
    answer_of(cli(lab, "add", "--type", "alpha", "--title", "by hand", "--key", "t-hand",
                  "--status", "todo"))
    assert answer_of(cli(lab, "run", "alpha", "--apply"))["claimed"] == "t-hand"

    log = "\n".join(answer_of(cli(lab, "service", "logs", "--tail", "200"))["lines"])
    assert "pause set by ops-person on every lane: upgrade" in log
    assert "paused: starting no turn on alpha" in log
    assert "pause lifted by ops-person on every lane" in log
    assert "resumed: alpha start turns again" in log
    assert answer_of(cli(lab, "service", "stop"))["stopped"] is True
    # Stopped, it is still paused, and the next daemon starts paused.
    assert answer_of(cli(lab, "service", "status"))["pause"]["paused"] is True
    assert answer_of(cli(lab, "service", "resume"))["running"] is False
