#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.5.0",
#                 "pyyaml>=6"]
# ///
"""The service: its declaration, its lanes, how it wakes, reloads and stops.

What the settings and the worker files declare is checked with no store. The
daemon itself is driven one step at a time over a project written into a temp
directory and a real store, with each turn replaced by a small process that
reports a claim and waits, so a lane's cap, a wake and a stop are observed
rather than inferred. One test then runs the real daemon through the CLI, whose
turns are real `tasks run` children on the stand-in harness. The store-backed
checks read TASKS_TEST_DSN and skip when it is unset; every run works in a
schema of its own and drops it. The store going away is a relay the test owns
between the daemon and the store being closed and opened again, so no test
stops a server it did not start.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.5.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

mod = _cli.load()
FAKE_CLAUDE = Path(__file__).resolve().parent / "fakes" / "claude"

PROFILE = """harness = "claude"
model = "claude-opus-5"
timeout_seconds = 600
permission_mode = "bypassPermissions"
"""

TEMPLATE = (_cli.CAPABILITY_DIR / "service" / "templates" / "config.toml")


def write_worker(project: Path, name: str, front: str, body: str = "the body.") -> Path:
    path = project / "capabilities" / "tasks" / "workers" / f"{name}.md"
    path.write_text(f"---\n{textwrap.dedent(front).strip()}\n---\n\n{body}\n")
    return path


def write_settings(project: Path, text: str) -> Path:
    path = project / "capabilities" / "tasks" / "service" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with two workers taking one type each, one that runs only on
    request, and the shipped `default` switched off."""
    envelope = tmp_path / "capabilities" / "tasks"
    (envelope / "workers").mkdir(parents=True)
    (envelope / "profiles").mkdir()
    (envelope / "profiles" / "plain.toml").write_text(PROFILE)
    write_worker(tmp_path, "alpha", "takes: [alpha]\nprofile: plain")
    write_worker(tmp_path, "beta", "takes: [beta]\nprofile: plain")
    write_worker(tmp_path, "triage", "on_request: [triage]\nprofile: plain")
    write_worker(tmp_path, "default", "enabled: false")
    monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda root: root / "capabilities")
    monkeypatch.setattr(mod, "_project_env", dict)
    monkeypatch.setattr(mod, "_STATE_HOME", tmp_path / "state")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-machine"))
    write_settings(tmp_path, TEMPLATE.read_text())
    return tmp_path


def refused(capsys=None) -> str:
    with pytest.raises(mod.Refusal) as caught:
        mod._service_declaration()
    return caught.value.message


# --- What is declared --------------------------------------------------------

def test_the_manifest_declares_the_service_the_contract_expects():
    service = mod.SERVICE
    assert service["verbs"] == ["init", "doctor", "run", "start", "reload", "stop",
                                "status", "logs"]
    assert service["config"] == "capabilities/tasks/service/config.toml"
    deploy = service["deploy"]
    assert deploy["schema"] == "capabilities.service.deploy.v1"
    assert deploy["command"] == ["tasks", "service", "run"]
    assert deploy["doctor"] == ["tasks", "service", "doctor"]
    assert deploy["default_policy"] in ("auto", "disabled")
    assert mod.STATE is True
    assert TEMPLATE.is_file() and (_cli.CAPABILITY_DIR / "service" / "daemon.py").is_file()


def test_the_template_is_the_defaults(project):
    declaration = mod._service_declaration()
    assert declaration["settings"] == {"version": 1, "poll_seconds": 60, "max_parallel": 2,
                                       "shutdown_grace_seconds": 60,
                                       "retry_delay_seconds": 60, "lanes": {}}


def test_every_enabled_worker_that_takes_something_is_a_lane(project):
    declaration = mod._service_declaration()
    assert [(lane["worker"], lane["max_parallel"]) for lane in declaration["lanes"]] == [
        ("alpha", 1), ("beta", 1)]
    idle = {row["worker"]: row["why"] for row in declaration["idle"]}
    assert set(idle) == {"default", "supervisor", "triage"}
    assert "on request" in idle["triage"] and "enabled: false" in idle["default"]
    assert "enabled: false" in idle["supervisor"]


def test_the_shipped_default_is_a_lane_taking_what_nobody_names(project):
    (project / "capabilities" / "tasks" / "workers" / "default.md").unlink()
    lanes = {lane["worker"]: lane for lane in mod._service_declaration()["lanes"]}
    assert set(lanes) == {"alpha", "beta", "default"}
    assert lanes["default"]["spec"]["not_types"] == ["alpha", "beta", "triage"]


def test_a_lane_takes_its_cap_from_its_table(project):
    write_settings(project, """
        version = 1
        max_parallel = 3
        [lanes.alpha]
        max_parallel = 2
    """)
    declaration = mod._service_declaration()
    assert declaration["settings"]["max_parallel"] == 3
    assert {lane["worker"]: lane["max_parallel"] for lane in declaration["lanes"]} == {
        "alpha": 2, "beta": 1}


@pytest.mark.parametrize("text, said", [
    ("version = 1\ntick = 2\n", "'tick', which nothing reads"),
    ("version = 1\n[lanes.alpha]\nmax_parallel = 1\npriority = 2\n",
     "`lanes.alpha` names 'priority', which nothing reads"),
    ("version = 2\n", "`version` is 1"),
    ("poll_seconds = 60\n", "`version` is 1"),
    ("version = 1\npoll_seconds = 0\n", "`poll_seconds` is a whole number of at least 1"),
    ("version = 1\nmax_parallel = true\n", "`max_parallel` is a whole number"),
    ("version = 1\nshutdown_grace_seconds = -1\n", "`shutdown_grace_seconds`"),
    ("version = 1\n[lanes.alpha]\nmax_parallel = 0\n", "`lanes.alpha.max_parallel`"),
    ("version = 1\nretry_delay_seconds = 0\n",
     "`retry_delay_seconds` is a whole number of at least 1"),
    ("version = 1\nretry_delay_seconds = 1.5\n", "`retry_delay_seconds`"),
    ("version = 1\nlanes = 3\n", "`lanes` is one table per worker"),
    ("version = 1\n[lanes.nobody]\n", "no worker of that name is declared here"),
    ("version = \n", "could not be read"),
])
def test_what_the_settings_do_not_say_is_refused(project, text, said):
    write_settings(project, text)
    assert said in refused()


def test_without_settings_the_service_is_not_initialized(project):
    (project / "capabilities" / "tasks" / "service" / "config.toml").unlink()
    with pytest.raises(mod.Refusal) as caught:
        mod._service_declaration()
    assert caught.value.code == "service_not_initialized"
    assert "service init" in caught.value.hint


def test_a_worker_that_cannot_be_read_refuses_the_declaration(project):
    write_worker(project, "beta", "takes: [beta]\nprofile: plain\ncadence: hourly")
    assert "'cadence', which nothing reads" in refused()


def test_init_writes_the_template_and_keeps_an_edited_file(project, capsys):
    target = project / "capabilities" / "tasks" / "service" / "config.toml"
    target.unlink()
    mod.cmd_service_init([])
    first = json.loads(capsys.readouterr().out)
    assert first["written"] == [str(target)] and target.read_text() == TEMPLATE.read_text()
    target.write_text(target.read_text() + "\n[lanes.alpha]\nmax_parallel = 2\n")
    mod.cmd_service_init([])
    assert json.loads(capsys.readouterr().out)["skipped"] == [str(target)]
    assert "[lanes.alpha]" in target.read_text()
    mod.cmd_service_init(["--force"])
    capsys.readouterr()
    assert target.read_text() == TEMPLATE.read_text()


def test_the_fingerprint_moves_with_settings_workers_and_profiles(project):
    first = mod._service_fingerprint()
    assert mod._service_fingerprint() == first
    worker = project / "capabilities" / "tasks" / "workers" / "alpha.md"
    worker.write_text(worker.read_text() + "\nmore.\n")
    second = mod._service_fingerprint()
    assert second != first
    profile = project / "capabilities" / "tasks" / "profiles" / "plain.toml"
    profile.write_text(PROFILE.replace("600", "900"))
    third = mod._service_fingerprint()
    assert third != second
    write_worker(project, "gamma", "takes: [gamma]\nprofile: plain")
    fourth = mod._service_fingerprint()
    assert fourth != third
    write_settings(project, "version = 1\npoll_seconds = 5\n")
    assert mod._service_fingerprint() != fourth


def test_the_state_lives_under_the_projects_capability_state(project):
    (project / "capabilities" / "project.json").write_text(json.dumps({"slug": "lab"}))
    assert mod._service_state_dir() == (project / "state" / "capabilities" / "projects"
                                        / "lab" / "tasks")


def test_inventory_reports_the_service_stopped_without_a_daemon(project):
    assert mod.INVENTORY == {"network": "none"}
    assert mod._inventory(False) == {"service": {
        "state": "stopped", "detail": "no daemon runs for this project"}}
    (project / "capabilities" / "tasks" / "service" / "config.toml").unlink()
    assert "not initialized" in mod._inventory(False)["service"]["detail"]


def test_the_notification_is_part_of_the_schema():
    ddl = mod._schema_ddl("elsewhere")
    assert "create trigger tasks_notify_claimable after insert or update on elsewhere.tasks" in ddl
    assert "pg_notify('tasks_claimable'" in ddl
    assert "elsewhere.notify_claimable()" in ddl


class _AwayListener:
    def notifies(self, timeout=None, stop_after=None):
        time.sleep(timeout or 0)
        return iter(())

    def close(self):
        pass


class _AwayHost:
    """A host whose store refuses a listener until `away` is cleared."""

    channel, project, schema = "tasks_claimable", "prj_away", "tasks"

    def __init__(self, state_dir: Path):
        self.state_dir = self.root = state_dir
        self.away = True
        self.attempts: list[float] = []

    def listen(self):
        self.attempts.append(time.monotonic())
        if self.away:
            raise mod.Refusal(5, "unreachable", "cannot reach the store: refused")
        return _AwayListener()

    def notification_installed(self):
        return True

    def next_moment(self):
        return None

    def sweep_due(self):
        return False


def test_a_listener_the_store_refuses_is_asked_for_again_on_a_doubling_interval(tmp_path):
    service = mod._service_module()
    service.RELISTEN_FIRST_SECONDS, service.RELISTEN_LONGEST_SECONDS = 0.2, 0.8
    host = _AwayHost(tmp_path / "state")
    declaration = {"settings": {"poll_seconds": 3600, "max_parallel": 1,
                                "shutdown_grace_seconds": 0, "retry_delay_seconds": 60},
                   "lanes": [], "idle": [], "fingerprint": "f"}
    daemon = service.Daemon(host, declaration, tick=0.05)
    daemon.open()
    try:
        deadline = time.monotonic() + 3.3
        while time.monotonic() < deadline:
            daemon.step()
        gaps = [b - a for a, b in zip(host.attempts, host.attempts[1:])]
        # Asked at start, then after 0.2s, 0.4s, and 0.8s from then on.
        assert len(host.attempts) >= 5, gaps
        assert gaps[0] < gaps[1] < gaps[2], gaps
        assert all(0.7 < gap < 1.2 for gap in gaps[2:]), gaps
        assert daemon.listener is None
        assert daemon.status()["notification"]["listening"] is False
        log = (daemon.state_dir / "daemon.log").read_text()
        # Said once, however many times it was asked.
        assert log.count("cannot listen for the store's notification") == 1
        host.away = False
        deadline = time.monotonic() + 3
        while daemon.listener is None and time.monotonic() < deadline:
            daemon.step()
        assert daemon.listener is not None
        assert "relisten" in daemon.last_wake["reasons"]
        assert daemon.status()["wake_by"] == "notification"
        assert daemon.relisten_at is None
        assert daemon.relisten_delay == service.RELISTEN_FIRST_SECONDS
        assert "listening on tasks_claimable again" in (
            daemon.state_dir / "daemon.log").read_text()
    finally:
        daemon.close()


# --- Against a real store ----------------------------------------------------

HERE = "prj_service"
DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")


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
    monkeypatch.setattr(mod, "SCHEMA", schema)
    monkeypatch.setattr(mod, "PROJECT", HERE)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(mod._schema_ddl(schema))
        try:
            yield entry, schema, conn
        finally:
            conn.execute(f"drop schema {schema} cascade")


# A turn that does no work: it says it claimed, then holds until told to go or
# until it is ended. What it claims is what the test hands it, so a stop can be
# shown settling a raise the test opened.
FAKE_TURN = """
import json, os, sys, time
from pathlib import Path
worker = sys.argv[1]
claims = Path(sys.argv[2])
release = Path(sys.argv[3])
named = claims / f"{worker}.json"
claim = json.loads(named.read_text()) if named.is_file() else {"task": f"{worker}-task"}
receipt = Path(os.environ["TASKS_TURN_RECEIPT"])
receipt.write_text(json.dumps(claim))
(claims / f"started-{os.getpid()}").write_text(worker)
while not release.exists():
    time.sleep(0.05)
"""


class Harness:
    """A daemon over the real host, with each turn replaced by FAKE_TURN."""

    def __init__(self, project: Path, entry: dict):
        self.project = project
        self.claims = project / "claims"
        self.claims.mkdir(exist_ok=True)
        self.release = project / "release"
        script = project / "fake_turn.py"
        script.write_text(FAKE_TURN)
        service = mod._service_module()
        self.service = service
        self.host = mod._ServiceHost(entry, None)
        self.host.turn_command = lambda worker: [sys.executable, str(script), worker,
                                                 str(self.claims), str(self.release)]
        self.daemon = service.Daemon(self.host, mod._service_declaration(), tick=0.2)

    def __enter__(self):
        self.daemon.open()
        return self

    def __exit__(self, *exc):
        self.release.write_text("go")
        self.daemon.stopping = True
        deadline = time.monotonic() + 10
        while self.daemon.turns and time.monotonic() < deadline:
            self.daemon.reap()
            time.sleep(0.05)
        for turn in list(self.daemon.turns.values()):
            turn.process.kill()
            turn.process.wait()
        self.daemon.turns.clear()
        self.daemon.close()

    def steps(self, until, seconds: float = 8.0) -> float:
        """Step until `until()` holds; how long it took."""
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            self.daemon.step()
            if until():
                return time.monotonic() - started
        raise AssertionError(f"not reached in {seconds}s; last wake {self.daemon.last_wake}")

    def running(self, worker: str | None = None) -> int:
        return sum(1 for turn in self.daemon.turns.values()
                   if worker is None or turn.worker == worker)

    def settle(self, seconds: float = 1.5) -> None:
        """Step long enough for every turn started to report its claim."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.daemon.step()


def add(entry, capsys, kind: str, **fields) -> str:
    args = ["--type", kind, "--title", f"a {kind}", "--status", fields.pop("status", "todo")]
    for key, value in fields.items():
        args += [f"--{key}", value]
    mod.cmd_add(entry, args)
    return json.loads(capsys.readouterr().out)["created"]


@needs_store
def test_the_store_notifies_and_the_daemon_wakes_for_its_own_project_only(
        project, store, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    with Harness(project, entry) as h:
        h.daemon.step()
        assert h.daemon.status()["wake_by"] == "notification"
        assert h.running() == 0
        reported = mod._inventory(False)["service"]
        assert reported["state"] == "running"
        assert "lanes alpha, beta" in reported["detail"]
        assert "woken by notification" in reported["detail"]
        # Another project's task on the same schema is not a wake for this one.
        mod.PROJECT = "prj_elsewhere"
        add(entry, capsys, "alpha")
        mod.PROJECT = HERE
        h.settle(1.0)
        assert h.running() == 0 and "notify" not in h.daemon.last_wake["reasons"]
        add(entry, capsys, "alpha")
        took = h.steps(lambda: h.running("alpha") == 1, seconds=5)
        assert took < 3
        assert "notify" in h.daemon.last_wake["reasons"]
        assert "poll" not in h.daemon.last_wake["reasons"]


@needs_store
def test_a_task_moved_to_todo_or_given_a_new_pickup_is_announced(project, store, capsys):
    entry, schema, conn = store
    tid = add(entry, capsys, "alpha", status="draft")
    conn.execute(f"listen {mod.CLAIMABLE_CHANNEL}")
    heard = lambda: [json.loads(n.payload) for n in conn.notifies(timeout=0.5)]  # noqa: E731
    assert heard() == []
    mod.cmd_set(entry, [tid, "--status", "todo"])
    capsys.readouterr()
    [moved] = heard()
    assert (moved["schema"], moved["project"], moved["task"]) == (schema, HERE, tid)
    mod.cmd_set(entry, [tid, "--pickup", "2099-01-01"])
    capsys.readouterr()
    [held] = heard()
    assert held["pickup_at"].startswith("2099-01-01")
    mod.cmd_set(entry, [tid, "--title", "renamed"])
    capsys.readouterr()
    assert heard() == []


@needs_store
def test_without_the_notification_the_daemon_wakes_by_the_poll(project, store, capsys):
    entry, schema, conn = store
    conn.execute(f"drop trigger tasks_notify_claimable on {schema}.tasks")
    write_settings(project, "version = 1\npoll_seconds = 1\n")
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        assert mod._notify_behind(cur, schema, mod._tables_present(cur, schema)) == [
            "tasks.tasks_notify_claimable"]
    mod.cmd_migrate(entry, [])
    assert "tasks.tasks_notify_claimable" in json.loads(capsys.readouterr().out)["would_add"]
    with Harness(project, entry) as h:
        h.daemon.step()
        assert h.daemon.status()["wake_by"] == "poll"
        assert h.daemon.status()["notification"]["installed"] is False
        add(entry, capsys, "beta")
        h.steps(lambda: h.running("beta") == 1, seconds=4)
        assert "poll" in h.daemon.last_wake["reasons"]
        assert "notify" not in h.daemon.last_wake["reasons"]
    mod.cmd_migrate(entry, ["--apply"])
    assert "tasks.tasks_notify_claimable" in json.loads(capsys.readouterr().out)["added"]


@needs_store
def test_the_daemon_wakes_at_the_earliest_pickup_still_ahead(project, store, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    tid = add(entry, capsys, "alpha")
    conn.execute(f"update {schema}.tasks set pickup_at = now() + interval '2 seconds' "
                 "where id = %s", (tid,))
    with Harness(project, entry) as h:
        h.daemon.step()
        assert h.running() == 0
        assert h.daemon.status()["next_wake"]["reason"] == "pickup"
        took = h.steps(lambda: h.running("alpha") == 1, seconds=6)
        assert 1 < took < 5
        assert "pickup" in h.daemon.last_wake["reasons"]


@needs_store
@pytest.mark.parametrize("across, alpha_cap, expected", [
    (3, 2, {"alpha": 2, "beta": 1}),
    (2, 2, {"alpha": 1, "beta": 1}),
    (4, 1, {"alpha": 1, "beta": 1}),
])
def test_each_lane_runs_at_most_its_cap_and_all_at_most_theirs(
        project, store, capsys, across, alpha_cap, expected):
    entry, schema, conn = store
    write_settings(project, f"""
        version = 1
        poll_seconds = 3600
        max_parallel = {across}
        [lanes.alpha]
        max_parallel = {alpha_cap}
    """)
    for kind in ("alpha", "alpha", "alpha", "beta", "beta", "triage"):
        add(entry, capsys, kind)
    with Harness(project, entry) as h:
        h.settle(2.0)
        assert {w: h.running(w) for w in ("alpha", "beta")} == expected
        assert h.running("triage") == 0
        status = h.daemon.status()
        assert {lane["worker"]: lane["running"] for lane in status["lanes"]} == expected
        assert all(turn["phase"] == "working" for turn in status["turns"])
        assert sum(expected.values()) <= across


@needs_store
def test_reload_publishes_the_new_fingerprint_and_leaves_running_turns(
        project, store, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    add(entry, capsys, "alpha")
    with Harness(project, entry) as h:
        state = h.daemon.state_dir
        h.steps(lambda: h.running("alpha") == 1)
        [turn] = h.daemon.turns.values()
        loaded = h.service.read_fingerprint(state)
        assert loaded == mod._service_fingerprint()
        # An edit on disk: the daemon no longer holds what is declared.
        write_settings(project, "version = 1\npoll_seconds = 3600\n[lanes.alpha]\n"
                                "max_parallel = 2\n")
        current = mod._service_fingerprint()
        assert h.service.read_fingerprint(state) != current
        h.daemon.reload_requested = True
        h.daemon.step()
        assert h.service.read_fingerprint(state) == current
        assert h.daemon.lanes()[0]["max_parallel"] == 2
        assert turn.process.poll() is None and h.daemon.turns[turn.id] is turn
        # An edit that does not load is refused and the daemon keeps what it had.
        write_settings(project, "version = 1\nticks = 3\n")
        h.daemon.reload_requested = True
        h.daemon.step()
        assert h.service.read_fingerprint(state) == current
        assert "'ticks', which nothing reads" in h.daemon.reload_error
        assert h.daemon.lanes()[0]["max_parallel"] == 2
        assert turn.process.poll() is None


@needs_store
def test_stop_waits_for_turns_then_ends_them_and_settles_like_a_lapsed_lease(
        project, store, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\nshutdown_grace_seconds = 1\n")
    tid = add(entry, capsys, "alpha", key="t-held")
    # The claim a real turn would make, so the stop has a raise to settle.
    held = mod._claim(entry, {"type": "alpha", "worker": "alpha", "lease": "3600"})
    execution = str(held["execution"]["id"])
    h = Harness(project, entry)
    (h.claims / "alpha.json").write_text(json.dumps(
        {"task": "t-held", "task_id": tid, "execution": execution, "attempt": 1}))
    add(entry, capsys, "alpha")  # work the lane would take, so a turn starts
    with h:
        h.steps(lambda: h.running("alpha") == 1 and all(
            t.phase == "working" for t in h.daemon.turns.values()))
        [turn] = h.daemon.turns.values()
        pid = turn.process.pid
        started = time.monotonic()
        h.daemon.stop_requested = True
        h.daemon.shutdown()
        waited = time.monotonic() - started
        assert 1 <= waited < 8
        assert h.daemon.turns == {} and turn.process.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    row = conn.execute(f"select status, detail from {schema}.task_executions where id = %s",
                       (execution,)).fetchone()
    assert row == ("abandoned", "cut off when the service stopped")
    status = conn.execute(f"select status from {schema}.tasks where id = %s",
                          (tid,)).fetchone()[0]
    assert status == "todo"
    log = (h.daemon.state_dir / "daemon.log").read_text()
    assert "cut off after 1s" in log and "settled as a lapsed lease" in log


@needs_store
def test_a_turn_that_finishes_within_the_grace_is_left_to_finish(project, store, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\nshutdown_grace_seconds = 20\n")
    add(entry, capsys, "beta")
    with Harness(project, entry) as h:
        h.steps(lambda: h.running("beta") == 1)
        [turn] = h.daemon.turns.values()
        h.daemon.stop_requested = True
        h.release.write_text("go")
        started = time.monotonic()
        h.daemon.shutdown()
        assert time.monotonic() - started < 10
        assert turn.process.returncode == 0
        assert "cut off" not in (h.daemon.state_dir / "daemon.log").read_text()


@needs_store
def test_a_lane_whose_turn_takes_nothing_waits_for_the_poll(project, store, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    add(entry, capsys, "alpha")
    with Harness(project, entry) as h:
        # A command that fails before it claims anything.
        h.host.turn_command = lambda worker: [sys.executable, "-c", "raise SystemExit(6)"]
        h.steps(lambda: "alpha" in h.daemon.held)
        h.settle(1.0)
        assert h.running() == 0 and "alpha" in h.daemon.held
        assert h.daemon.status()["lanes"][0]["held_until_poll"] is True


@needs_store
def test_run_writes_the_receipt_the_service_reads(project, store, capsys, monkeypatch):
    from callva import harness_runner
    from callva.harness_runner import FailureKind, Result
    entry, schema, conn = store
    tid = add(entry, capsys, "alpha", key="t-receipt")
    receipt = project / "receipt.json"
    monkeypatch.setenv(mod._RECEIPT_ENV, str(receipt))
    seen = {}

    class Recorder:
        def run(self, prompt, profile, cwd, *, session=None, environ=None, **kw):
            seen["env"] = dict(environ or {})
            seen["receipt"] = json.loads(receipt.read_text())
            return Result(ok=True, harness="claude", answer="done", session_id=session.id,
                          model="m", cost_usd=0.0, duration_ms=1, num_turns=1)

    recorder = Recorder()
    recorder.Profile, recorder.Session, recorder.FailureKind = (
        harness_runner.Profile, harness_runner.Session, FailureKind)
    recorder.find_profile_file = harness_runner.find_profile_file
    recorder.ProfileNotFound = harness_runner.ProfileNotFound
    monkeypatch.setattr(mod, "_harness_runner", lambda: recorder)
    mod.cmd_run(entry, ["alpha", "--apply"])
    report = json.loads(capsys.readouterr().out)
    assert seen["receipt"] == {"task": "t-receipt", "task_id": tid,
                               "execution": report["execution"], "attempt": 1}
    # Written before the turn, and never handed to it.
    assert mod._RECEIPT_ENV not in seen["env"]


# --- The store going away ----------------------------------------------------

class Relay:
    """A TCP relay to the store on a port of its own. `down()` cuts every
    connection through it and refuses new ones, as a store that went away does;
    `up()` opens the same port again."""

    def __init__(self, host: str, port: int):
        self.target = (host, port)
        self.pairs: list[tuple[socket.socket, socket.socket]] = []
        self.lock = threading.Lock()
        self.listener: socket.socket | None = None
        self.port = 0
        self.up()

    def up(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(16)
        self.port = listener.getsockname()[1]
        self.listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()

    def down(self) -> None:
        listener, self.listener = self.listener, None
        if listener is not None:
            listener.close()
        with self.lock:
            pairs, self.pairs = self.pairs, []
        for pair in pairs:
            for end in pair:
                try:
                    end.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                end.close()

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self.target)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.pairs.append((client, upstream))
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        for end in (source, sink):
            try:
                end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


@pytest.fixture
def relay():
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    found = Relay(info.get("host") or "127.0.0.1", int(info.get("port") or 5432))
    try:
        yield found
    finally:
        found.down()


@needs_store
def test_the_daemon_outlives_the_store_going_away_and_listens_again(
        project, store, relay, capsys):
    """The store goes away under a daemon running a turn: the daemon keeps
    running and keeps the turn, and when the store is back it listens again
    without a restart, takes the work it could not hear about, and then takes a
    task by the store's notification."""
    entry, schema, conn = store
    write_worker(project, "gamma", "takes: [gamma]\nprofile: plain")
    write_settings(project, "version = 1\npoll_seconds = 3600\nmax_parallel = 3\n")
    # The daemon reaches the store through the relay; the test writes directly.
    through = {**entry, "db_host": "127.0.0.1", "db_port": str(relay.port)}
    add(entry, capsys, "alpha")
    with Harness(project, through) as h:
        h.steps(lambda: h.running("alpha") == 1 and all(
            t.phase == "working" for t in h.daemon.turns.values()))
        assert h.daemon.status()["wake_by"] == "notification"
        [turn] = h.daemon.turns.values()

        relay.down()
        h.steps(lambda: h.daemon.listener is None, seconds=5)
        # Work written while the daemon cannot hear the store.
        add(entry, capsys, "beta")
        h.settle(2.5)
        status = h.daemon.status()
        assert status["notification"]["listening"] is False
        assert "cannot reach the store" in status["notification"]["error"]
        assert h.running("beta") == 0
        assert turn.process.poll() is None and h.daemon.turns[turn.id] is turn

        relay.up()
        h.steps(lambda: h.daemon.listener is not None, seconds=8)
        assert "relisten" in h.daemon.last_wake["reasons"]
        h.steps(lambda: h.running("beta") == 1 and all(
            t.phase == "working" for t in h.daemon.turns.values()))
        assert h.daemon.status()["wake_by"] == "notification"
        assert turn.process.poll() is None

        # A task written now is heard, not polled for.
        add(entry, capsys, "gamma")
        took = h.steps(lambda: h.running("gamma") == 1, seconds=5)
        assert took < 3
        assert "notify" in h.daemon.last_wake["reasons"]
        assert "poll" not in h.daemon.last_wake["reasons"]
        log = (h.daemon.state_dir / "daemon.log").read_text()
        assert "lost the store's notification" in log
        assert "listening on tasks_claimable again" in log


# --- The daemon, through the CLI ---------------------------------------------

@pytest.fixture()
def lab(tmp_path):
    """A project the CLI runs in as a consuming project would, with the envelope
    handed down so no manager is asked."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN)
    schema = "tasks_test_" + secrets.token_hex(4)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    tasks = envelope / "tasks"
    (tasks / "workers").mkdir(parents=True)
    (tasks / "profiles").mkdir()
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1",
        "id": "prj_" + uuid.uuid4().hex[:12], "slug": "lab-" + secrets.token_hex(3)}))
    (tasks / "connections.json").write_text(json.dumps({
        "default": "local",
        "connections": {"local": {
            "db_host": info.get("host"), "db_port": str(info.get("port") or 5432),
            "db_user": info.get("user"), "db_name": info.get("dbname"),
            "db_sslmode": info.get("sslmode") or "prefer", "db_schema": schema,
            "secret_env": "TASKS_TEST_PASSWORD", "allow_write": True}}}))
    (tasks / "profiles" / "plain.toml").write_text(
        PROFILE + f'cli_path = "{FAKE_CLAUDE}"\n')
    write_worker(project, "alpha", "takes: [alpha]\nprofile: plain")
    write_worker(project, "default", "enabled: false")
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "CAPABILITIES_PROJECT_ENVELOPE": str(envelope),
        "TASKS_TEST_PASSWORD": info.get("password") or "",
        "FAKE_ENGINE_TASKS": str(_cli.CLI_PATH),
    })
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_STORE_URL",
                   "CAPABILITIES_STORE_MODE"):
        env.pop(leaked, None)
    lab = {"project": project, "env": env, "tmp": tmp_path}
    assert tasks_cli(lab, "migrate", "--apply").returncode == 0
    try:
        yield lab
    finally:
        tasks_cli(lab, "service", "stop", "--timeout", "30", "--force")
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def tasks_cli(lab, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(_cli.CLI_PATH), *args], cwd=lab["project"], env=lab["env"],
                          text=True, capture_output=True, timeout=180)


def answer_of(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def poll_for(check, seconds: float):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.2)
    raise AssertionError(f"not reached within {seconds}s")


@needs_store
def test_the_daemon_end_to_end_through_the_cli(lab):
    project = lab["project"]
    assert answer_of(tasks_cli(lab, "service", "init"))["written"]
    settings = project / "capabilities" / "tasks" / "service" / "config.toml"
    settings.write_text(settings.read_text().replace("poll_seconds = 60", "poll_seconds = 3600")
                        .replace("shutdown_grace_seconds = 60", "shutdown_grace_seconds = 5"))
    doctor = answer_of(tasks_cli(lab, "service", "doctor"))
    assert doctor["ok"] and doctor["notification"]["installed"] is True
    assert doctor["lanes"] == [{"worker": "alpha", "max_parallel": 1}]

    started = answer_of(tasks_cli(lab, "service", "start"))
    assert started["started"] and started["running"] and started["wake_by"] == "notification"
    pid = started["pid"]
    status = answer_of(tasks_cli(lab, "service", "status"))
    assert status["lanes"] == [{"worker": "alpha", "max_parallel": 1, "running": 0,
                                "held_until_poll": False}]
    assert status["next_wake"]["reason"] == "poll" and status["current"] is True

    made = answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "probe",
                               "--key", "t-probe", "--status", "todo"))
    at = time.monotonic()
    done = poll_for(lambda: (lambda shown: shown if shown["task"]["status"] == "complete"
                             else None)(answer_of(tasks_cli(lab, "show", "t-probe"))), 60)
    assert time.monotonic() - at < 60
    assert [a["description"] for a in done["activities"]] == ["done by the stand-in"]
    [raised] = answer_of(tasks_cli(lab, "runs", "t-probe"))["executions"]
    assert (raised["status"], raised["worker"]) == ("ok", "alpha")
    log = answer_of(tasks_cli(lab, "service", "logs"))["lines"]
    assert any(f"claimed t-probe" in line for line in log)
    assert made["created"]

    # A worker file edited: doctor says the daemon is behind, reload catches it up.
    worker = project / "capabilities" / "tasks" / "workers" / "alpha.md"
    worker.write_text(worker.read_text() + "\nedited.\n")
    stale = tasks_cli(lab, "service", "doctor")
    assert stale.returncode == 6 and "declaration_stale" in json.loads(stale.stdout)
    reloaded = answer_of(tasks_cli(lab, "service", "reload"))
    assert reloaded["reloaded"] is True and reloaded["pid"] == pid
    assert answer_of(tasks_cli(lab, "service", "doctor"))["ok"]
    again = answer_of(tasks_cli(lab, "service", "reload"))
    assert again["reloaded"] is False
    settings.write_text(settings.read_text() + "\nbogus = 1\n")
    refused_reload = tasks_cli(lab, "service", "reload")
    assert refused_reload.returncode == 6 and "bogus" in refused_reload.stderr

    stopped = answer_of(tasks_cli(lab, "service", "stop"))
    assert stopped["stopped"] is True
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert answer_of(tasks_cli(lab, "service", "status"))["running"] is False


@needs_store
def test_init_start_and_run_need_the_project_itself_to_enable_tasks(lab):
    envelope = lab["project"] / "capabilities"
    (envelope / "settings.json").write_text(json.dumps({"capabilities": {}}))
    global_gate = lab["tmp"] / "config" / "capabilities" / "settings.json"
    global_gate.parent.mkdir(parents=True, exist_ok=True)
    global_gate.write_text(json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    for verb in ("init", "start", "run"):
        refused_verb = tasks_cli(lab, "service", verb)
        assert refused_verb.returncode == 4, (verb, refused_verb.stderr)
        assert json.loads(refused_verb.stderr)["error"]["code"] == "project_enable_required"
    assert tasks_cli(lab, "service", "status").returncode == 0


@needs_store
@pytest.mark.parametrize("failing", ["idle", "ordinary"])
def test_a_task_whose_turns_fail_at_once_is_retried_no_faster_than_the_delay(lab, failing):
    """A turn that fails in an instant puts its task straight back in todo, and
    the store announces it. The service still waits `retry_delay_seconds` after
    that turn ended before it starts another on the task, whether the failure
    spent an attempt or not, and the task keeps coming back rather than being
    lost. The task's own hold is shorter than the delay here, so the delay is
    what is measured."""
    project = lab["project"]
    lab["env"]["FAKE_ENGINE_FAIL"] = failing
    write_worker(project, "alpha", """
        takes: [alpha]
        profile: plain
        limits:
          attempts: 50
          hold_seconds_on_exhaustion: 1
    """)
    assert answer_of(tasks_cli(lab, "service", "init"))["written"]
    write_settings(project, """
        version = 1
        poll_seconds = 3600
        shutdown_grace_seconds = 10
        retry_delay_seconds = 2
    """)
    answer_of(tasks_cli(lab, "service", "start"))
    answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "fails",
                        "--key", "t-fail", "--status", "todo"))
    runs = lambda: answer_of(tasks_cli(lab, "runs", "t-fail"))["executions"]  # noqa: E731
    poll_for(runs, 30)
    time.sleep(5.5)
    status = answer_of(tasks_cli(lab, "service", "status"))
    assert status["retry_delay_seconds"] == 2
    answer_of(tasks_cli(lab, "service", "stop"))

    import datetime
    moment = datetime.datetime.fromisoformat
    raised = sorted(runs(), key=lambda row: row["started_at"])
    first = moment(raised[0]["started_at"])
    window = [row for row in raised
              if moment(row["started_at"]) < first + datetime.timedelta(seconds=5)]
    # One turn, then one more at most every two seconds: never more than three
    # in five seconds, and the task was not dropped after the first.
    assert 2 <= len(window) <= 3, [(r["started_at"], r["ended_at"]) for r in raised]
    for before, after in zip(raised, raised[1:]):
        gap = moment(after["started_at"]) - moment(before["ended_at"])
        assert gap >= datetime.timedelta(seconds=2), (before, after)
    for row in raised:
        assert row["status"] == "failed"
        assert bool((row["metrics"] or {}).get("exhausted")) is (failing == "idle")
    shown = answer_of(tasks_cli(lab, "show", "t-fail"))["task"]
    assert shown["status"] == "todo"
    log = "\n".join(answer_of(tasks_cli(lab, "service", "logs", "--tail", "200"))["lines"])
    assert "t-fail not run again before" in log

    # A run by hand is not the service and selects as it always has.
    time.sleep(1.2)
    by_hand = answer_of(tasks_cli(lab, "run", "alpha", "--apply"))
    assert by_hand["claimed"] == "t-fail"


@needs_store
def test_service_doctor_warns_while_the_daemon_waits_out_the_store(lab, relay):
    """The probe a supervisor runs: with a daemon running, the store being away
    is a warning and exit 0, and a stale declaration still fails; with no
    daemon it fails. The daemon lives through it and works when the store is
    back."""
    project = lab["project"]
    connections = project / "capabilities" / "tasks" / "connections.json"
    registry = json.loads(connections.read_text())
    registry["connections"]["relayed"] = {**registry["connections"]["local"],
                                          "db_host": "127.0.0.1", "db_port": str(relay.port)}
    registry["default"] = "relayed"
    connections.write_text(json.dumps(registry))
    assert answer_of(tasks_cli(lab, "service", "init"))["written"]
    write_settings(project, "version = 1\npoll_seconds = 3600\nshutdown_grace_seconds = 5\n")

    relay.down()
    without = tasks_cli(lab, "service", "doctor")
    assert without.returncode == 5
    assert json.loads(without.stderr)["error"]["code"] == "unreachable"

    relay.up()
    doctor = answer_of(tasks_cli(lab, "service", "doctor"))
    assert doctor["ok"] and doctor["store"] == {"reachable": True}
    started = answer_of(tasks_cli(lab, "service", "start"))
    pid = started["pid"]
    assert started["wake_by"] == "notification"

    relay.down()
    away = answer_of(tasks_cli(lab, "service", "doctor"))
    assert away["ok"] is True and "problems" not in away
    assert away["store"]["reachable"] is False
    assert "cannot reach the store" in away["store"]["error"]
    assert "unreachable" in away["warning"]
    assert away["service"]["running"] is True
    # A daemon behind the files on disk fails the probe, store or no store.
    worker = project / "capabilities" / "tasks" / "workers" / "alpha.md"
    kept = worker.read_text()
    worker.write_text(kept + "\nedited.\n")
    stale = tasks_cli(lab, "service", "doctor")
    assert stale.returncode == 6 and "declaration_stale" in json.loads(stale.stdout)
    worker.write_text(kept)
    time.sleep(3)
    os.kill(pid, 0)
    assert answer_of(tasks_cli(lab, "service", "status"))["notification"]["listening"] is False

    relay.up()
    poll_for(lambda: answer_of(tasks_cli(lab, "service", "status"))["notification"]["listening"],
             40)
    answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "after", "--key", "t-after",
                        "--status", "todo"))
    poll_for(lambda: answer_of(tasks_cli(lab, "show", "t-after"))["task"]["status"] == "complete",
             60)
    back = answer_of(tasks_cli(lab, "service", "doctor"))
    assert back["ok"] and back["store"] == {"reachable": True}
    assert back["service"]["pid"] == pid
    log = "\n".join(answer_of(tasks_cli(lab, "service", "logs", "--tail", "200"))["lines"])
    assert "listening on tasks_claimable again" in log
    assert answer_of(tasks_cli(lab, "service", "stop"))["stopped"] is True


@needs_store
def test_a_turn_starts_with_the_projects_env_files_over_the_daemons_environment(lab):
    """A supervisor starts the daemon with next to nothing in its environment.
    Each turn still gets the project's .env and .env.local over it, .env.local
    winning over .env and both over the process, parsed as every capability
    parses them - the environment an automations job starts with."""
    project = lab["project"]
    record = lab["tmp"] / "engine.jsonl"
    (project / ".env").write_text(
        f"TASKS_TEST_PASSWORD={lab['env']['TASKS_TEST_PASSWORD']}\n"
        f"FAKE_ENGINE_TASKS={lab['env']['FAKE_ENGINE_TASKS']}\n"
        "TURN_PROBE_OVER_PROCESS=dotenv\n"
        "TURN_PROBE_LAYERED=dotenv\n"
        "TURN_PROBE_DOTENV=dotenv\n")
    (project / ".env.local").write_text(
        "# a comment, and a blank line, are not variables\n\n"
        f"export FAKE_ENGINE_RECORD='{record}'\n"
        "TURN_PROBE_LAYERED=local\n"
        'export TURN_PROBE_QUOTED="a quoted value"\n')
    assert answer_of(tasks_cli(lab, "service", "init"))["written"]
    write_settings(project, "version = 1\npoll_seconds = 3600\nshutdown_grace_seconds = 5\n")
    minimal = {key: lab["env"][key] for key in (
        "PATH", "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "UV_CACHE_DIR",
        "CAPABILITIES_HOME", "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_MANAGER_BIN",
        "TMPDIR") if key in lab["env"]}
    minimal.update({"TURN_PROBE_OVER_PROCESS": "process", "TURN_PROBE_PROCESS": "process"})
    log = (lab["tmp"] / "supervised.log").open("w")
    daemon = subprocess.Popen([str(_cli.CLI_PATH), "service", "run"], cwd=project, env=minimal,
                              stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    try:
        poll_for(lambda: answer_of(tasks_cli(lab, "service", "status"))["running"], 60)
        answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "probe", "--key",
                            "t-env", "--status", "todo"))
        poll_for(lambda: answer_of(tasks_cli(lab, "show", "t-env"))["task"]["status"]
                 == "complete", 60)
    finally:
        tasks_cli(lab, "service", "stop", "--timeout", "30", "--force")
        daemon.wait(timeout=60)
        log.close()
    [turn] = [json.loads(line) for line in record.read_text().splitlines()]
    env = turn["env"]
    assert env["TURN_PROBE_PROCESS"] == "process"
    assert env["TURN_PROBE_OVER_PROCESS"] == "dotenv"
    assert env["TURN_PROBE_DOTENV"] == "dotenv"
    assert env["TURN_PROBE_LAYERED"] == "local"
    assert env["TURN_PROBE_QUOTED"] == "a quoted value"
    assert "TASKS_EXECUTION" in env
    # What the files hold is handed to the turn, never written to the log.
    said = "\n".join(answer_of(tasks_cli(lab, "service", "logs", "--tail", "500"))["lines"])
    said += (lab["tmp"] / "supervised.log").read_text()
    assert "a quoted value" not in said
    if lab["env"]["TASKS_TEST_PASSWORD"]:
        assert lab["env"]["TASKS_TEST_PASSWORD"] not in said
