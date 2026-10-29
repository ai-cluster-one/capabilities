#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "capabilities-contract>=0.8,<1",
#                 "callva-harness-runner==0.10.1",
#                 "pyyaml>=6"]
# ///
"""The service: its declaration, its lanes, how it wakes, reloads and stops.

What the settings and the workers declare is checked with no store. The
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
        --with 'capabilities-contract>=0.8,<1' \\
        --with 'callva-harness-runner==0.10.1' python -m pytest capabilities/tasks/tests -q
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
    """A worker document of `project`, kept beside it as the store's version of
    that worker: served to this process by `serve_workers`, saved for a child
    by `tasks_cli`."""
    path = _cli.worker_docs(project) / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
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
    envelope.mkdir(parents=True)
    _cli.profile_docs(tmp_path).mkdir()
    (_cli.profile_docs(tmp_path) / "plain.toml").write_text(PROFILE)
    _cli.serve_workers(mod, monkeypatch, tmp_path)
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
                                "status", "logs", "pause", "resume", "harness"]
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
                                       "shutdown_grace_seconds": 60, "lanes": {}}


def test_every_enabled_worker_that_takes_something_is_a_lane(project):
    declaration = mod._service_declaration()
    assert [(lane["worker"], lane["max_parallel"]) for lane in declaration["lanes"]] == [
        ("alpha", 1), ("beta", 1)]
    idle = {row["worker"]: row["why"] for row in declaration["idle"]}
    assert set(idle) == {"default", "supervisor", "triage"}
    assert "on request" in idle["triage"] and "enabled: false" in idle["default"]
    assert "enabled: false" in idle["supervisor"]


def test_the_shipped_default_is_a_lane_taking_what_nobody_names(project):
    (_cli.worker_docs(project) / "default.md").unlink()
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
    worker = _cli.worker_docs(project) / "alpha.md"
    worker.write_text(worker.read_text() + "\nmore.\n")
    second = mod._service_fingerprint()
    assert second != first
    profile = _cli.profile_docs(project) / "plain.toml"
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


def test_inventory_reports_the_service_stopped_without_a_daemon(project, monkeypatch):
    monkeypatch.setattr(mod, "_service_connection", lambda wanted: ("main", {}))
    assert mod.INVENTORY == {"network": "none"}
    assert mod._inventory(False) == {"service": {
        "state": "stopped", "detail": "no daemon runs for this project", "ok": True}}
    (project / "capabilities" / "tasks" / "service" / "config.toml").unlink()
    service = mod._inventory(False)["service"]
    assert "not initialized" in service["detail"]
    # The verdict is `service doctor`'s, judged without the store.
    assert service["ok"] is False and "no service settings" in service["problem"]


def test_inventory_names_what_the_doctor_finds_wrong_with_what_serves_it(project, monkeypatch):
    monkeypatch.setattr(mod, "_service_connection", lambda wanted: ("main", {}))
    monkeypatch.setattr(mod, "_service_status", lambda module: {
        "running": False, "machine": {"running": False, "state": None}})
    assert mod._inventory_verdict(mod._service_module()) == {
        "ok": False, "problem": "this project is joined to the machine tasks service, and "
                                "no machine process runs; `tasks service start --machine`"}

    def no_store(*_a, **_k):
        raise AssertionError("the inventory verdict asked the store")

    monkeypatch.setattr(mod, "_connect", no_store)
    monkeypatch.setattr(mod, "_service_status", lambda module: {
        "running": False, "machine": {"running": True, "state": "served"}})
    assert mod._inventory_verdict(mod._service_module()) == {"ok": True}

    monkeypatch.setattr(mod, "_service_connection", lambda wanted: mod._die(
        6, "connections_required", "this project holds no connection"))
    assert mod._inventory_verdict(mod._service_module()) == {
        "ok": False, "problem": "connections_required: this project holds no connection"}


def test_the_notification_is_part_of_the_tables():
    ddl = mod._TABLES_SHAPE
    assert "create trigger tasks_notify_claimable after insert or update on tasks_tasks" in ddl
    assert "pg_notify('tasks_claimable'" in ddl
    assert "execute function tasks_notify_claimable()" in ddl


class _AwayListener:
    def notifies(self, timeout=None, stop_after=None):
        time.sleep(timeout or 0)
        return iter(())

    def close(self):
        pass


class _AwayConnection:
    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _AwayHost:
    """A host whose store refuses a listener until `away` is cleared."""

    channel, project, schema, slug = "tasks_claimable", "prj_away", "tasks", "away"

    def __init__(self, state_dir: Path):
        self.state_dir = self.root = state_dir
        self.away = True
        self.attempts: list[float] = []

    def scope(self, _environment):
        import contextlib
        return contextlib.nullcontext()

    def recheck(self, _machine=False):
        return None

    def store_key(self):
        return ("away:5432/tasks", "tasks", "agentkit")

    def store_name(self):
        return "away:5432/tasks"

    def listen(self):
        self.attempts.append(time.monotonic())
        if self.away:
            raise mod.Refusal(5, "unreachable", "cannot reach the store: refused")
        return _AwayListener()

    def open_query(self, _timeout):
        if self.away:
            raise mod.Refusal(5, "unreachable", "cannot reach the store: refused")
        return _AwayConnection()

    def lost(self, _conn, _exc):
        return False

    def ping(self, _conn):
        pass

    def bounded(self, _conn, _seconds):
        import contextlib
        return contextlib.nullcontext()

    def notification_installed(self, _conn):
        return True

    def next_moment(self, _conn, _cool_downs=None):
        return None

    def sweep_due(self, _conn):
        return False


def test_a_listener_the_store_refuses_is_asked_for_again_on_a_doubling_interval(tmp_path):
    service = mod._service_module()
    service.RELISTEN_FIRST_SECONDS, service.RELISTEN_LONGEST_SECONDS = 0.2, 0.8
    host = _AwayHost(tmp_path / "state")
    declaration = {"settings": {"poll_seconds": 3600, "max_parallel": 1,
                                "shutdown_grace_seconds": 0, "retry_delay_seconds": 60},
                   "lanes": [], "idle": [], "fingerprint": "f"}
    dispatcher = service.Dispatcher(tick=0.05)
    daemon = dispatcher.add(host, declaration)
    dispatcher.open()
    try:
        deadline = time.monotonic() + 3.3
        while time.monotonic() < deadline:
            dispatcher.step()
        gaps = [b - a for a, b in zip(host.attempts, host.attempts[1:])]
        # Asked at start, then after 0.2s, 0.4s, and 0.8s from then on.
        assert len(host.attempts) >= 5, gaps
        assert gaps[0] < gaps[1] < gaps[2], gaps
        assert all(0.7 < gap < 1.2 for gap in gaps[2:]), gaps
        assert daemon.listener is None
        assert daemon.status()["notification"]["listening"] is False
        log = (daemon.state_dir / "daemon.log").read_text()
        # Said once, however many times it was asked.
        assert log.count("cannot hold a connection to the store to follow its versions") == 1
        host.away = False
        deadline = time.monotonic() + 3
        while daemon.listener is None and time.monotonic() < deadline:
            dispatcher.step()
        assert daemon.listener is not None
        assert "relisten" in daemon.last_wake["reasons"]
        assert daemon.status()["wake_by"] == "notification"
        assert daemon.store.relisten_at is None
        assert daemon.store.relisten_delay == service.RELISTEN_FIRST_SECONDS
        assert "following tasks_claimable again" in (
            daemon.state_dir / "daemon.log").read_text()
    finally:
        dispatcher.close()


# --- Against a real store ----------------------------------------------------

HERE = "prj_service"
DSN = os.environ.get("TASKS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="TASKS_TEST_DSN is unset")


@pytest.fixture
def store(monkeypatch):
    import psycopg

    schema = "tasks_test_" + secrets.token_hex(4)
    entry = {"allow_write": True}
    _cli.bind_store(mod, monkeypatch, schema)
    monkeypatch.delenv("TASKS_EXECUTION", raising=False)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    monkeypatch.setattr(mod, "PROJECT", HERE)
    # The workers a case writes are saved in this store, so a raise names a
    # version it holds.
    _cli.keep_served_workers_in_store(mod, monkeypatch, schema)
    with psycopg.connect(DSN, autocommit=True) as conn:
        _cli.make_tables(mod, schema)
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


def serve(project: Path, entry: dict) -> None:
    """What the check before every action reads, as a project that may be served
    holds it: its id, tasks enabled for it, and the connection granted with
    writes. Written only where the test has not written its own; its slug is
    the state root the test's verbs already wrote to, which the id would
    otherwise stand in for."""
    envelope = project / "capabilities"
    key = mod._service_state_key(project)
    for path, value in (
            (envelope / "project.json",
             {"schema": "capabilities.project.v1", "id": HERE, "slug": key}),
            (envelope / "settings.json", {"capabilities": {"tasks": {"enabled": True}}}),
            (envelope / "tasks" / "connections.json",
             {"default": "local", "connections": {"local": entry}})):
        if not path.exists():
            path.write_text(json.dumps(value))


class Harness:
    """A dispatcher over one slot on the real host, with each turn replaced by
    FAKE_TURN. `daemon` is the slot."""

    def __init__(self, project: Path, entry: dict, setting=None):
        self.project = project
        self.claims = project / "claims"
        self.claims.mkdir(exist_ok=True)
        self.release = project / "release"
        script = project / "fake_turn.py"
        script.write_text(FAKE_TURN)
        serve(project, entry)
        service = mod._service_module()
        self.service = service
        self.host = mod._ServiceHost(entry, None)
        if setting is not None:
            # The store reached another way - through a relay - than the
            # verbs the test calls reach it.
            self.host.setting = setting
        self.host.turn_command = lambda worker: [sys.executable, str(script), worker,
                                                 str(self.claims), str(self.release)]
        self.dispatcher = service.Dispatcher(tick=0.2)
        self.daemon = self.dispatcher.add(self.host, mod._service_declaration())

    def __enter__(self):
        self.dispatcher.open()
        return self

    def __exit__(self, *exc):
        self.release.write_text("go")
        self.dispatcher.stopping = True
        deadline = time.monotonic() + 10
        while self.daemon.turns and time.monotonic() < deadline:
            self.daemon.reap()
            time.sleep(0.05)
        for turn in list(self.daemon.turns.values()):
            if turn.process is not None:
                turn.process.kill()
                turn.process.wait()
        self.daemon.turns.clear()
        self.dispatcher.close()

    def steps(self, until, seconds: float = 8.0) -> float:
        """Step until `until()` holds; how long it took."""
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            self.dispatcher.step()
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
            self.dispatcher.step()


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
        h.dispatcher.step()
        assert h.daemon.status()["wake_by"] == "notification"
        # It follows the store's change versions and listens for nothing.
        assert h.daemon.store.listener.execute(
            "select count(*) as n from pg_listening_channels()").fetchone()["n"] == 0
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
def test_a_running_daemon_is_seen_as_serving_its_project(project, store):
    """Another project reads whether a service serves this one, before it
    assigns a task here: the daemon says so by its row, which it renews, and
    by the lock an earlier release reads, and says nothing once it is gone."""
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")

    def serving() -> bool:
        with mod._connect(entry) as conn, conn.cursor() as cur:
            return mod._serving(cur, HERE)

    def rows() -> list:
        return conn.execute(f"select project_id, lock_pid, expires_at > now() as live "
                            f"from {schema}.tasks_presences").fetchall()

    def locks() -> int:
        return conn.execute("select count(*) from pg_locks where locktype = 'advisory' "
                            "and classid = %s::oid", (mod._PRESENCE_CLASS,)).fetchone()[0]

    assert serving() is False
    with Harness(project, entry) as h:
        h.dispatcher.step()
        assert serving() is True
        listener = h.daemon.store.listener
        pid = listener.execute("select pg_backend_pid() as p").fetchone()["p"]
        assert rows() == [(HERE, pid, True)] and locks() == 1
        # The row alone says it: an earlier release's lock is not what it rests on.
        listener.execute("select pg_advisory_unlock(%s, hashtext(%s))",
                         (mod._PRESENCE_CLASS, mod._presence_key(schema, HERE)))
        assert locks() == 0 and serving() is True
        # A row past its expiry says nothing, and the daemon renews it in time.
        conn.execute(f"update {schema}.tasks_presences set expires_at = now()")
        assert serving() is False
        h.dispatcher._ping(time.monotonic() + h.service.PRESENCE_RENEW_SECONDS)
        assert rows() == [(HERE, pid, True)] and serving() is True
    assert serving() is False and rows() == []


@needs_store
def test_on_a_transaction_only_store_the_daemon_serves_by_its_row_alone(
        project, store, capsys, monkeypatch):
    """A store reached through a transaction pooler alone keeps no session: the
    daemon holds a transaction connection, says it serves the project by a row
    naming no session and takes no lock, and still wakes by the versions."""
    from capabilities_contract import db
    entry, schema, conn = store
    setting = _cli.transaction_only(_cli.store_setting(schema))
    assert setting.has_session_address is False
    monkeypatch.setattr(mod, "_store_setting", lambda raising=False, root=None: setting)
    write_settings(project, "version = 1\npoll_seconds = 3600\n")

    def serving() -> bool:
        with mod._connect(entry) as conn, conn.cursor() as cur:
            return mod._serving(cur, HERE)

    def rows() -> list:
        return conn.execute(f"select project_id, lock_pid, expires_at > now() as live "
                            f"from {schema}.tasks_presences").fetchall()

    def locks() -> int:
        return conn.execute("select count(*) from pg_locks where locktype = 'advisory' "
                            "and classid = %s::oid", (mod._PRESENCE_CLASS,)).fetchone()[0]

    with Harness(project, entry, setting=setting) as h:
        h.dispatcher.step()
        listener = h.daemon.store.listener
        assert listener is not None and h.daemon.store.listen_error is None
        # A transaction connection, which refuses a session lock before sending it.
        with pytest.raises(db.DbError) as caught:
            listener.execute("select pg_advisory_lock(1)")
        assert caught.value.slug == "session_state"
        assert rows() == [(HERE, None, True)] and locks() == 0 and serving() is True
        assert h.daemon.status()["notification"]["listening"] is True
        assert h.daemon.status()["wake_by"] == "notification"
        add(entry, capsys, "alpha")
        took = h.steps(lambda: h.running("alpha") == 1, seconds=5)
        assert took < 3 and "notify" in h.daemon.last_wake["reasons"]
        conn.execute(f"update {schema}.tasks_presences set expires_at = now()")
        h.dispatcher._ping(time.monotonic() + h.service.PRESENCE_RENEW_SECONDS)
        assert rows() == [(HERE, None, True)]
    assert rows() == [] and serving() is False


@needs_store
def test_where_the_two_addresses_are_apart_the_daemon_wakes_on_the_notification(
        project, store, capsys, monkeypatch):
    """The held connection also listens, so a change ends the wait at once
    rather than at its end; the lock and the row are as before."""
    import dataclasses
    entry, schema, conn = store
    base = _cli.store_setting(schema)
    setting = dataclasses.replace(base, transaction_host=base.host,
                                  transaction_port=base.port)
    assert setting.has_transaction_address and setting.has_session_address
    monkeypatch.setattr(mod, "_store_setting", lambda raising=False, root=None: setting)
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    with Harness(project, entry, setting=setting) as h:
        h.dispatcher.step()
        listener = h.daemon.store.listener
        assert h.daemon.store.hears is True
        assert sorted(row["c"] for row in listener.execute(
            "select pg_listening_channels() as c").fetchall()) == sorted(
            [mod.CLAIMABLE_CHANNEL, mod.DECLARATION_CHANNEL, mod.LINES_CHANNEL])
        pid = listener.execute("select pg_backend_pid() as p").fetchone()["p"]
        assert conn.execute(f"select lock_pid from {schema}.tasks_presences").fetchall() == [
            (pid,)]
        for _ in range(3):
            h.dispatcher.wait(0.2)
        started = time.monotonic()
        h.dispatcher.wait(0.6)
        assert time.monotonic() - started >= 0.5
        add(entry, capsys, "alpha")
        started = time.monotonic()
        h.dispatcher.wait(5.0)
        assert time.monotonic() - started < 1.0
        assert "notify" in h.daemon.wakes


@needs_store
def test_a_transaction_only_store_is_migrated_on_transaction_connections(
        project, monkeypatch):
    """`migrate` brings the harness library's profile table up on a transaction
    connection, as every command brings this capability's tables up."""
    import psycopg
    from capabilities_contract import db
    schema = "tasks_test_" + secrets.token_hex(4)
    setting = _cli.transaction_only(_cli.store_setting(schema))
    monkeypatch.setattr(db, "resolve_setting", lambda root, **kwargs: setting)
    monkeypatch.setattr(mod, "_store_setting", lambda raising=False, root=None: setting)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    try:
        mod._migrate_profile_tables()
        with psycopg.connect(DSN, autocommit=True) as conn:
            assert conn.execute("select to_regclass(%s) is not null",
                                (f"{schema}.harness_profiles",)).fetchone()[0]
        with mod._connect({"allow_write": True}):
            pass
        with psycopg.connect(DSN, autocommit=True) as conn:
            assert conn.execute("select to_regclass(%s) is not null",
                                (f"{schema}.tasks_presences",)).fetchone()[0]
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


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


def versions(conn, schema: str) -> dict:
    return {(row[0], row[1]): row[2] for row in conn.execute(
        f"select channel, scope, version from {schema}.schema_channel").fetchall()}


@needs_store
def test_every_change_raises_its_projects_version_as_it_commits(project, store, capsys):
    """The claimable and watch channels move with the writes they announce, in
    the writer's own transaction, and a write rolled back moves nothing."""
    entry, schema, conn = store
    claimable, watch = (mod.CLAIMABLE_CHANNEL, HERE), (mod.WATCH_CHANNEL, HERE)
    before = versions(conn, schema)
    tid = add(entry, capsys, "alpha", status="draft")
    after_add = versions(conn, schema)
    assert after_add.get(claimable, 0) == before.get(claimable, 0)
    assert after_add[watch] > before.get(watch, 0)
    mod.cmd_set(entry, [tid, "--status", "todo"])
    capsys.readouterr()
    moved = versions(conn, schema)
    assert moved[claimable] == after_add.get(claimable, 0) + 1
    assert moved[watch] > after_add[watch]
    with conn.transaction():
        conn.execute(f"update {schema}.tasks_tasks set pickup_at = now() where id = %s", (tid,))
        conn.execute(f"insert into {schema}.tasks_activities (task_id, description, actor) "
                     "values (%s, 'kept', 'test')", (tid,))
    kept = versions(conn, schema)
    assert kept[claimable] == moved[claimable] + 1 and kept[watch] > moved[watch]
    try:
        with conn.transaction():
            conn.execute(f"update {schema}.tasks_tasks set pickup_at = null where id = %s",
                         (tid,))
            raise RuntimeError("roll back")
    except RuntimeError:
        pass
    assert versions(conn, schema) == kept


@needs_store
def test_two_writers_of_one_project_both_commit(project, store, capsys):
    """Each writer takes the project's version rows at its commit, the watch row
    before the claimable one, so a writer that first adds a trail entry and one
    that first makes a task claimable never wait on each other in a cycle."""
    import psycopg
    entry, schema, _conn = store
    one = add(entry, capsys, "alpha", status="draft")
    two = add(entry, capsys, "alpha", status="draft")
    a, b = (psycopg.connect(DSN) for _ in range(2))
    try:
        a.execute(f"insert into {schema}.tasks_activities (task_id, description, actor) "
                  "values (%s, 'a', 'test')", (one,))
        b.execute(f"update {schema}.tasks_tasks set status = 'todo' where id = %s", (two,))
        a.execute(f"update {schema}.tasks_tasks set status = 'todo' where id = %s", (one,))
        done: dict = {}

        def commit(name, conn):
            try:
                conn.commit()
                done[name] = "committed"
            except Exception as exc:
                done[name] = type(exc).__name__

        threads = [threading.Thread(target=commit, args=pair) for pair in (("a", a), ("b", b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert done == {"a": "committed", "b": "committed"}
    finally:
        a.close()
        b.close()


@needs_store
def test_the_daemon_wakes_at_the_earliest_pickup_still_ahead(project, store, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    tid = add(entry, capsys, "alpha")
    conn.execute(f"update {schema}.tasks_tasks set pickup_at = now() + interval '2 seconds' "
                 "where id = %s", (tid,))
    with Harness(project, entry) as h:
        h.dispatcher.step()
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
        h.dispatcher.reload_requested = True
        h.dispatcher.step()
        assert h.service.read_fingerprint(state) == current
        assert h.daemon.lanes()[0]["max_parallel"] == 2
        assert turn.process.poll() is None and h.daemon.turns[turn.id] is turn
        # An edit that does not load is refused and the daemon keeps what it had.
        write_settings(project, "version = 1\nticks = 3\n")
        h.dispatcher.reload_requested = True
        h.dispatcher.step()
        assert h.service.read_fingerprint(state) == current
        assert "'ticks', which nothing reads" in h.daemon.reload_error
        assert h.daemon.lanes()[0]["max_parallel"] == 2
        assert turn.process.poll() is None


@needs_store
def test_a_stop_ending_turns_waits_then_ends_them_and_settles_like_a_lapsed_lease(
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
        h.service.write_end_turns(h.daemon.state_dir, "a-stopper", 90)
        started = time.monotonic()
        h.dispatcher.stop_requested = True
        h.dispatcher.shutdown()
        waited = time.monotonic() - started
        assert 1 <= waited < 8
        assert not (h.daemon.state_dir / h.service.END_TURNS_FILE).exists()
        assert h.daemon.turns == {} and turn.process.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    row = conn.execute(f"select status, detail from {schema}.tasks_executions where id = %s",
                       (execution,)).fetchone()
    assert row == ("abandoned", "cut off when the service stopped")
    status = conn.execute(f"select status from {schema}.tasks_tasks where id = %s",
                          (tid,)).fetchone()[0]
    assert status == "todo"
    log = (h.daemon.state_dir / "daemon.log").read_text()
    assert "cut off after 1s" in log and "settled as a lapsed lease" in log


def held_raise(entry, schema, conn, capsys, h: Harness, lease: int) -> tuple[str, callable]:
    """A raise claimed by `lease` seconds, which the next stand-in turn reports
    as its own claim and every one after it does not; the raise's execution,
    and how many seconds its lease has left."""
    tid = add(entry, capsys, "alpha", key="t-held")
    held = mod._claim(entry, {"type": "alpha", "worker": "alpha", "lease": str(lease)})
    execution = str(held["execution"]["id"])
    (h.claims / "alpha.json").write_text(json.dumps(
        {"task": "t-held", "task_id": tid, "execution": execution, "attempt": 1}))

    def left() -> float:
        return conn.execute(f"""select extract(epoch from lease_until - now())::float8
                                  from {schema}.tasks_executions where id = %s""",
                            (execution,)).fetchone()[0]
    return execution, left


def swept_by_a_claim(entry) -> list[str]:
    """What the sweep every claim runs closes; there is no beta work to take."""
    return mod._claim(entry, {"type": "beta", "worker": "beta", "lease": "60"})["swept"]


@needs_store
def test_the_service_keeps_a_live_turns_lease_and_lets_a_gone_ones_lapse(
        project, store, capsys, monkeypatch):
    entry, schema, conn = store
    monkeypatch.setattr(mod, "_beat_lease", lambda: (1, 3))
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    h = Harness(project, entry)
    execution, left = held_raise(entry, schema, conn, capsys, h, 3)
    with h:
        # The stand-in turn has no beat of its own: only the service renews.
        turn = h.daemon.spawn("alpha", "work")
        h.steps(lambda: turn.phase == "working")
        (h.claims / "alpha.json").unlink()
        h.settle(7)
        assert 0 < left() <= 3.5
        assert execution not in swept_by_a_claim(entry)
        status = conn.execute(f"select status from {schema}.tasks_executions where id = %s",
                              (execution,)).fetchone()[0]
        assert status == "running"
        turn.process.kill()
        turn.process.wait()
        h.steps(lambda: h.running() == 0)
        poll_for(lambda: left() < 0, 6)
        assert execution in swept_by_a_claim(entry)
    status = conn.execute(f"select status from {schema}.tasks_executions where id = %s",
                          (execution,)).fetchone()[0]
    assert status == "abandoned"


@needs_store
def test_the_service_renews_before_it_starts_a_turn_whose_claim_would_sweep(
        project, store, capsys, monkeypatch):
    entry, schema, conn = store
    # A beat far off, so only the renewal before a turn starts can reach it.
    monkeypatch.setattr(mod, "_beat_lease", lambda: (3600, 3))
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    h = Harness(project, entry)
    execution, left = held_raise(entry, schema, conn, capsys, h, 3)
    with h:
        turn = h.daemon.spawn("alpha", "work")
        h.steps(lambda: turn.phase == "working")
        (h.claims / "alpha.json").unlink()
        h.settle(0.5)
        time.sleep(max(0.0, left()) + 0.5)
        assert left() < 0
        h.daemon.spawn("beta", "sweep")
        assert 0 < left() <= 3.5
        assert execution not in swept_by_a_claim(entry)


@needs_store
def test_a_closed_raise_is_never_revived(project, store, capsys):
    entry, schema, conn = store
    tid = add(entry, capsys, "alpha", key="t-closed")
    held = mod._claim(entry, {"type": "alpha", "worker": "alpha", "lease": "60"})
    execution = str(held["execution"]["id"])
    serve(project, entry)
    host = mod._ServiceHost(entry, None)
    receipt = {"task": "t-closed", "task_id": tid, "execution": execution, "attempt": 1}
    conn.execute(f"""update {schema}.tasks_executions
                        set status = 'abandoned', ended_at = now(),
                            lease_until = now() - interval '1 second' where id = %s""",
                 (execution,))
    with mod._connect(entry, raising=True) as own:
        assert host.renew_leases(own, [("alpha", receipt)])["renewed"] == []
        # A receipt naming another task than the raise's is not that raise's.
        other = add(entry, capsys, "alpha", key="t-other")
        # Named: the closed raise left t-closed resting in todo, as every
        # claim leaves its task, so asking for the next would take it again.
        again = mod._claim(entry, {"key": "t-other", "worker": "alpha", "lease": "60"})
        assert host.renew_leases(own, [("alpha", {**receipt, "execution": str(
            again["execution"]["id"])})])["renewed"] == []
        assert host.renew_leases(own, [("alpha", {**receipt, "task_id": other, "execution": str(
            again["execution"]["id"])})])["renewed"] == [str(again["execution"]["id"])]
    row = conn.execute(f"select status, lease_until < now() from {schema}.tasks_executions "
                       "where id = %s", (execution,)).fetchone()
    assert row == ("abandoned", True)


@needs_store
def test_a_turn_that_finishes_within_the_grace_is_left_to_finish(project, store, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\nshutdown_grace_seconds = 20\n")
    add(entry, capsys, "beta")
    with Harness(project, entry) as h:
        h.steps(lambda: h.running("beta") == 1)
        [turn] = h.daemon.turns.values()
        h.service.write_end_turns(h.daemon.state_dir, "a-stopper", 90)
        h.dispatcher.stop_requested = True
        h.release.write_text("go")
        started = time.monotonic()
        h.dispatcher.shutdown()
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
    # With the cap the service renews its lease to, since the service is its
    # one writer.
    assert seen["receipt"] == {"task": "t-receipt", "task_id": tid,
                               "execution": report["execution"], "attempt": 1,
                               "lease_seconds": mod._worker("alpha")["limits"]["lease_seconds"]}
    # Written before the turn, and never handed to it.
    assert mod._RECEIPT_ENV not in seen["env"]


# --- The store going away ----------------------------------------------------

class Relay:
    """A TCP relay to the store on a port of its own. `down()` cuts every
    connection through it and refuses new ones, as a store that went away does;
    `up()` opens the same port again. `stall()` keeps every connection through
    it open and passes nothing more on them, as a path that dropped under them
    does. `cut_on(words, times)` cuts the connection a query carrying `words`
    is sent on, once the query has passed and before its answer comes back,
    for the next `times` such queries."""

    def __init__(self, host: str, port: int):
        self.target = (host, port)
        self.pairs: list[tuple[socket.socket, socket.socket]] = []
        self.stalled: set[socket.socket] = set()
        self.clients: set[socket.socket] = set()
        self.cut_words, self.cuts_left = b"", 0
        self.lock = threading.Lock()
        self.listener: socket.socket | None = None
        self.port = 0
        self.up()

    def refuse(self) -> None:
        """Refuse new connections and leave the ones through it open, as a store
        that stops accepting does."""
        self._stop_listening()

    def stall(self) -> None:
        """Pass nothing more either way on the connections through it, holding
        them open; new connections pass."""
        with self.lock:
            self.stalled.update(end for pair in self.pairs for end in pair)

    def cut_on(self, words: bytes, times: int) -> None:
        with self.lock:
            self.cut_words, self.cuts_left = words, times

    def up(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(16)
        self.port = listener.getsockname()[1]
        self.listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()

    def down(self) -> None:
        self._stop_listening()
        with self.lock:
            pairs, self.pairs = self.pairs, []
        for pair in pairs:
            for end in pair:
                try:
                    end.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                end.close()

    def _stop_listening(self) -> None:
        """Close the listener. On Linux one closed while a thread waits in its
        accept() goes on accepting, so it is shut down first, which wakes it."""
        listener, self.listener = self.listener, None
        if listener is not None:
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            listener.close()

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
                self.clients.add(client)
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                if source not in self.stalled:
                    sink.sendall(data)
                if source in self.clients and self._cutting(data):
                    break
        except OSError:
            pass
        for end in (source, sink):
            try:
                end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _cutting(self, data: bytes) -> bool:
        with self.lock:
            if self.cuts_left > 0 and self.cut_words and self.cut_words in data:
                self.cuts_left -= 1
                return True
        return False


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
    through = _cli.store_setting(schema, host="127.0.0.1", port=relay.port)
    add(entry, capsys, "alpha")
    with Harness(project, entry, through) as h:
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
        assert "lost the connection the store's versions are followed on" in log
        assert "following tasks_claimable again" in log


# --- The connections it holds -----------------------------------------------

def quick(h: Harness, **constants) -> Harness:
    """Shorten the daemon's intervals, before it opens anything."""
    first = constants.pop("first", 0.2)
    h.service.RELISTEN_FIRST_SECONDS = first
    h.service.RELISTEN_LONGEST_SECONDS = constants.pop("longest", 0.8)
    for name, value in constants.items():
        setattr(h.service, name, value)
    h.daemon.store.relisten_delay = h.daemon.store.requery_delay = first
    return h


def gaps_of(moments: list[float]) -> list[float]:
    return [round(b - a, 2) for a, b in zip(moments, moments[1:])]


def backend(conn) -> int:
    return conn.info.backend_pid


@needs_store
def test_the_questions_of_every_pass_go_on_the_one_working_connection(
        project, store, capsys, monkeypatch):
    """Idle on a one-second poll with two lanes, the daemon holds its listener
    and one working connection: each pass borrows the process's working
    connection for all its questions and gives it back at its end, so every
    pass asks on the same connection, kept between passes."""
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 1\n")
    opened = []
    real = mod._connect

    def counted(*args, **kwargs):
        opened.append(time.monotonic())
        return real(*args, **kwargs)

    # Loading the declaration reads the project's workers from the store; what
    # is counted is what the daemon opens once it holds that declaration.
    h = Harness(project, entry)
    monkeypatch.setattr(mod, "_connect", counted)
    asked = []
    lane_has_work = h.host.lane_has_work
    h.host.lane_has_work = lambda c, worker, exclude=(): (
        asked.append(backend(c)) or lane_has_work(c, worker, exclude))
    with h:
        h.settle(3.6)
        store_ = h.daemon.store
        assert store_.query is None
        listener = backend(store_.listener)
        passes = set(asked)
        assert listener not in passes
        # Start and three polls, each asking both lanes, all on one connection.
        assert len(asked) >= 6 and len(passes) == 1
        assert len(opened) >= 4
        assert "poll" in h.daemon.last_wake["reasons"]
        [row] = h.daemon.status()["stores"]
        from psycopg.conninfo import conninfo_to_dict
        info = conninfo_to_dict(DSN)
        assert row["store"] == f"{info['host']}:{info['port']}/{info['dbname']}"
        assert (row["listening"], row["connections_opened"], row["error"]) == \
            (True, len(opened), None)
        assert row["last_opened_at"]
        live = conn.execute("select pid from pg_stat_activity where pid = any(%s)",
                            (sorted(passes | {listener}),)).fetchall()
        assert sorted(live) == sorted((pid,) for pid in passes | {listener})


@needs_store
def test_a_question_connection_the_store_refuses_is_asked_for_again_on_the_doubling_interval(
        project, store, relay, capsys, monkeypatch):
    """The store takes no new connection and the working connection the
    questions go on is lost: the daemon keeps its listener, and the pass a
    notification wakes asks for a connection for its questions when the
    library dials the store again - its one doubling pause - and the moment it
    has one asks for the work it missed, having said the refusal once."""
    from capabilities_contract.db import _connect as library
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    through = _cli.store_setting(schema, host="127.0.0.1", port=relay.port)
    h = quick(Harness(project, entry, through))
    monkeypatch.setattr(library, "RECONNECT_FIRST_SECONDS", 0.2)
    monkeypatch.setattr(library, "RECONNECT_LONGEST_SECONDS", 0.8)
    # The working connection is proven as it is lent, as one idle for longer
    # than the library's ping interval is, so its loss is met as it is lent.
    monkeypatch.setattr(library, "PING_AFTER_SECONDS", 0)
    attempts, opened, refused = [], [], []
    open_query = h.host.open_query

    def timed(seconds):
        # The wait between attempts is the library's: from the end of one to the
        # start of the next, whatever each spent trying to connect.
        began = time.monotonic()
        try:
            question = open_query(seconds)
            opened.append(backend(question))
            return question
        except Exception as exc:
            refused.append((began, time.monotonic(), getattr(exc, "dials_again_at", None),
                            getattr(exc, "message", "")))
            raise
        finally:
            attempts.append((began, time.monotonic()))

    h.host.open_query = timed
    with h:
        h.dispatcher.step()
        store_ = h.daemon.store
        assert store_.connections_opened == 2 and len(attempts) == 1
        assert store_.query is None
        listening = backend(store_.listener)
        relay.refuse()
        conn.execute("select pg_terminate_backend(%s)", (opened[0],))
        add(entry, capsys, "alpha")
        h.steps(lambda: store_.query_error is not None, seconds=5)
        assert "notify" in h.daemon.last_wake["reasons"]
        h.settle(4.0)
        assert h.running() == 0
        [row] = h.daemon.status()["stores"]
        assert row["listening"] is True and row["error"]
        # Asked again the moment the library dials again, so every ask dials:
        # after half to all of 0.2s, then of 0.4s, then of 0.8s from then on.
        assert len(attempts) == 1 + len(refused)
        assert not any("not dialled again" in said for *_t, said in refused), refused
        for (_b, _e, dials, _s), (began, *_rest) in zip(refused, refused[1:]):
            assert 0 <= began - dials < 0.1, refused
        gaps = [round(nxt[0] - prev[1], 2) for prev, nxt in zip(refused, refused[1:])]
        assert len(gaps) >= 4, gaps
        assert 0.08 < gaps[0] < 0.3 and 0.18 < gaps[1] < 0.5, gaps
        assert all(0.38 < gap < 0.95 for gap in gaps[2:]), gaps

        relay.up()
        h.steps(lambda: h.running("alpha") == 1, seconds=5)
        assert "reconnect" in h.daemon.last_wake["reasons"]
        assert store_.connections_opened >= 3
        assert backend(store_.listener) == listening
        [row] = h.daemon.status()["stores"]
        assert row["error"] is None
        log = (h.daemon.state_dir / "daemon.log").read_text()
        assert log.count("cannot connect to the store to ask it for work") == 1
        assert "connected to the store again for its questions" in log


@needs_store
def test_connections_the_path_dropped_are_severed_and_asked_for_again(
        project, store, relay, capsys):
    """Every connection through the relay stops carrying anything while staying
    open: the listener's round trip is severed within the bound rather than
    waited on, no pass of the loop holds longer, the questions go on connections
    of their own opened for each pass, and the work is taken."""
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 1\n")
    through = _cli.store_setting(schema, host="127.0.0.1", port=relay.port)
    h = quick(Harness(project, entry, through), ROUND_TRIP_SECONDS=1.0, PING_SECONDS=0.5)
    with h:
        h.dispatcher.step()
        store_ = h.daemon.store
        assert store_.connections_opened == 2
        relay.stall()
        add(entry, capsys, "alpha")
        passes = []

        def taken() -> bool:
            passes.append(time.monotonic())
            return h.running("alpha") == 1

        h.steps(taken, seconds=10)
        assert max(gaps_of(passes)) < 3.0, gaps_of(passes)
        assert store_.connections_opened >= 3
        h.steps(lambda: store_.listener is not None, seconds=5)
        log = (h.daemon.state_dir / "daemon.log").read_text()
        assert "lost the connection the store's versions are followed on" in log


@needs_store
def test_a_lost_listener_is_asked_for_again_on_the_doubling_interval(
        project, store, relay, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    through = _cli.store_setting(schema, host="127.0.0.1", port=relay.port)
    h = quick(Harness(project, entry, through))
    attempts = []
    listen = h.host.listen

    def timed():
        # The wait between attempts is the daemon's: from the end of one to the
        # start of the next, whatever each spent trying to connect.
        began = time.monotonic()
        try:
            return listen()
        finally:
            attempts.append((began, time.monotonic()))

    h.host.listen = timed
    with h:
        h.dispatcher.step()
        store_ = h.daemon.store
        assert store_.query is None
        relay.refuse()
        conn.execute("select pg_terminate_backend(%s)", (backend(store_.listener),))
        h.steps(lambda: h.daemon.listener is None, seconds=5)
        h.settle(5.0)
        tried = attempts[1:]
        gaps = [round(nxt[0] - prev[1], 2) for prev, nxt in zip(tried, tried[1:])]
        assert len(gaps) >= 3 and gaps[0] < gaps[1], gaps
        assert 0.3 < gaps[0] < 0.6 and all(0.6 < gap < 1.2 for gap in gaps[1:]), gaps
        assert h.daemon.status()["stores"][0]["listening"] is False

        relay.up()
        h.steps(lambda: h.daemon.listener is not None, seconds=5)
        assert "relisten" in h.daemon.last_wake["reasons"]
        add(entry, capsys, "beta")
        took = h.steps(lambda: h.running("beta") == 1, seconds=5)
        assert took < 3 and "notify" in h.daemon.last_wake["reasons"]
        assert store_.connections_opened >= 4
        assert store_.query is None
        [row] = h.daemon.status()["stores"]
        assert (row["listening"], row["error"]) == (True, None)


@needs_store
def test_a_question_that_hangs_is_cut_off_and_the_daemon_carries_on(project, store, capsys):
    """A lock held on the tasks table holds every question about them: the
    store cuts the question off at its timeout, the daemon's pass ends rather
    than waiting for the lock, and once the lock is gone it is asking again on
    a new connection and taking work."""
    import psycopg
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    h = quick(Harness(project, entry), QUESTION_TIMEOUT_SECONDS=1)
    with h:
        h.dispatcher.step()
        store_ = h.daemon.store
        blocker = psycopg.connect(DSN)
        try:
            blocker.execute(f"lock table {schema}.tasks_tasks in access exclusive mode")
            h.daemon.wakes.add("notify")
            started = time.monotonic()
            h.dispatcher.step(wait=False)
            took = time.monotonic() - started
            assert 0.9 < took < 5, took
            assert store_.query is None and h.running() == 0
            log = (h.daemon.state_dir / "daemon.log").read_text()
            assert "canceling statement due to statement timeout" in log
            assert "'s work: QueryCanceled" in log
        finally:
            blocker.rollback()
            blocker.close()
        add(entry, capsys, "alpha")
        h.steps(lambda: h.running("alpha") == 1, seconds=5)
        assert store_.connections_opened >= 4


@needs_store
def test_the_listener_carries_a_round_trip_on_the_ping_interval(
        project, store, capsys):
    entry, schema, conn = store
    write_settings(project, "version = 1\npoll_seconds = 3600\n")
    h = quick(Harness(project, entry), PING_SECONDS=0.5)
    pings = []
    ping = h.host.ping
    h.host.ping = lambda c: pings.append((backend(c), time.monotonic())) or ping(c)
    with h:
        h.settle(2.7)
        store_ = h.daemon.store
        listener = [at for pid, at in pings if pid == backend(store_.listener)]
        assert 4 <= len(listener) <= 6, listener
        assert all(0.4 < gap < 0.8 for gap in gaps_of(listener)), gaps_of(listener)
        # No question connection is held between passes, so nothing else is pinged.
        assert store_.query is None
        assert {pid for pid, _at in pings} == {backend(store_.listener)}
        # Its last statement is one of its own round trips: the ping, or the
        # read of the change versions it makes at every wait.
        last = conn.execute("select query from pg_stat_activity where pid = %s",
                            (backend(store_.listener),)).fetchone()[0]
        assert last == "select 1" or last.startswith("SELECT channel, scope, version FROM"), last
        assert store_.connections_opened == 2


# --- The daemon, through the CLI ---------------------------------------------

@pytest.fixture()
def lab(tmp_path):
    """A project the CLI runs in as a consuming project would, with the envelope
    handed down so no manager is asked."""
    import psycopg

    schema = "tasks_test_" + secrets.token_hex(4)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    tasks = envelope / "tasks"
    tasks.mkdir(parents=True)
    _cli.profile_docs(project).mkdir()
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1",
        "id": "prj_" + uuid.uuid4().hex[:12], "slug": "lab-" + secrets.token_hex(3)}))
    (tasks / "connections.json").write_text(json.dumps({
        "default": "local", "connections": {"local": {"allow_write": True}}}))
    _cli.write_store_setting(tmp_path / "config", schema)
    (_cli.profile_docs(project) / "plain.toml").write_text(
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
        "FAKE_ENGINE_TASKS": str(_cli.CLI_PATH),
    })
    for leaked in ("CAPABILITIES_READ_ONLY", "TASKS_EXECUTION", "TASKS_ACTOR",
                   "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_PROJECT_ID_ROOT",
                   "CAPABILITIES_STORE_MODE", *_cli.STORE_OVERRIDES):
        env.pop(leaked, None)
    lab = {"project": project, "env": env, "tmp": tmp_path, "schema": schema}
    assert tasks_cli(lab, "migrate").returncode == 0
    try:
        yield lab
    finally:
        tasks_cli(lab, "service", "stop", "--end-turns", "--timeout", "30", "--force")
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"drop schema if exists {schema} cascade")


def tasks_cli(lab, *args: str) -> subprocess.CompletedProcess:
    if args[:1] != ("migrate",):
        # The lab's worker documents as the store's workers of its project,
        # for the child about to read them.
        _cli.save_declared_workers(mod, lab["project"], lab["schema"])
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
    slug = json.loads((project / "capabilities" / "project.json").read_text())["slug"]
    assert status["lanes"] == [{"project": slug, "worker": "alpha", "max_parallel": 1,
                                "running": 0, "held_until_poll": False}]
    assert (status["state"], status["reason"]) == ("served", None)
    assert status["next_wake"]["reason"] == "poll" and status["current"] is True
    [held] = status["stores"]
    assert (held["listening"], held["connections_opened"], held["error"]) == (True, 2, None)
    assert held["store"] and held["last_opened_at"]

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
    # Every line names the project by its slug.
    assert log and all(f" tasks service [{slug}]: " in line for line in log), log
    assert made["created"]

    # A worker saved: the store announces it, and the daemon takes it up with
    # no reload.
    worker = _cli.worker_docs(project) / "alpha.md"
    worker.write_text(worker.read_text() + "\nedited.\n")
    poll_for(lambda: answer_of(tasks_cli(lab, "service", "status"))["current"] is True, 30)
    assert answer_of(tasks_cli(lab, "service", "doctor"))["ok"]
    log = answer_of(tasks_cli(lab, "service", "logs"))["lines"]
    assert any("the store announced a change to the declaration" in line for line in log)
    # The settings edited: doctor says the daemon is behind, reload catches it up.
    settings.write_text(settings.read_text().replace("shutdown_grace_seconds = 5",
                                                     "shutdown_grace_seconds = 6"))
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
    assert not mod._service_module().pid_alive(pid)
    assert answer_of(tasks_cli(lab, "service", "status"))["running"] is False


@needs_store
def test_start_with_no_settings_writes_them_and_runs_a_turn_with_no_init(lab):
    """A fresh agent's service starts with no `service init` by hand: `start`
    writes the shipped template, the daemon it runs takes work, and settings
    already there are left as they are."""
    settings = lab["project"] / "capabilities" / "tasks" / "service" / "config.toml"
    assert not settings.exists()
    started = answer_of(tasks_cli(lab, "service", "start"))
    assert started["started"] and started["initialized"] is True
    assert settings.read_text() == TEMPLATE.read_text()
    answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "probe",
                        "--key", "t-probe", "--status", "todo"))
    poll_for(lambda: answer_of(tasks_cli(lab, "show", "t-probe"))["task"]["status"]
             == "complete", 60)
    assert answer_of(tasks_cli(lab, "service", "stop"))["stopped"] is True

    settings.write_text(settings.read_text() + "\n[lanes.alpha]\nmax_parallel = 2\n")
    edited = settings.read_text()
    assert answer_of(tasks_cli(lab, "service", "start"))["started"]
    assert settings.read_text() == edited


@needs_store
def test_service_harness_answers_the_harness_of_every_lanes_profile(lab):
    assert mod.SERVICE["sign_ins"] == {"tools": ["gh"],
                                       "harness": ["tasks", "service", "harness", "--json"]}
    project = lab["project"]
    (_cli.profile_docs(project) / "coder.toml").write_text(
        'harness = "codex"\nmodel = "gpt-6-sol"\ntimeout_seconds = 600\n')
    write_worker(project, "beta", "takes: [beta]\nprofile: coder")
    write_worker(project, "gamma", "takes: [gamma]\nprofile: nowhere")
    write_worker(project, "triage", "on_request: [triage]\nprofile: coder")
    answer = answer_of(tasks_cli(lab, "service", "harness", "--json"))
    assert answer == {"harness": ["claude", "codex"],
                      "lanes": [{"worker": "alpha", "profile": "plain", "harness": "claude"},
                                {"worker": "beta", "profile": "coder", "harness": "codex"},
                                {"worker": "gamma", "profile": "nowhere", "harness": None}],
                      "unresolved": ["nowhere"]}


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
def test_a_task_whose_turns_fail_at_once_is_retried_no_faster_than_the_cool_down(
        lab, failing):
    """A turn that fails in an instant puts its task straight back in todo, and
    the store announces it. No claim takes it again until the worker's
    cool-down has passed since that raise ended, whether the failure spent an
    attempt or not, and the task keeps coming back rather than being lost. The
    service wakes when the cool-down ends; a run by hand is held to it too."""
    project = lab["project"]
    lab["env"]["FAKE_ENGINE_FAIL"] = failing
    write_worker(project, "alpha", """
        takes: [alpha]
        profile: plain
        limits:
          attempts: 50
          cool_down_seconds: 2
    """)
    assert answer_of(tasks_cli(lab, "service", "init"))["written"]
    write_settings(project, """
        version = 1
        poll_seconds = 3600
        shutdown_grace_seconds = 10
    """)
    answer_of(tasks_cli(lab, "service", "start"))
    answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", "fails",
                        "--key", "t-fail", "--status", "todo"))
    runs = lambda: answer_of(tasks_cli(lab, "runs", "t-fail"))["executions"]  # noqa: E731
    poll_for(runs, 30)
    time.sleep(5.5)
    answer_of(tasks_cli(lab, "service", "stop", "--end-turns"))

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
    assert shown["status"] == "todo" and shown["pickup_at"] is None

    # A run by hand reads the same cool-down from the last raise.
    last = max(moment(row["ended_at"]) for row in runs())
    wait = (last + datetime.timedelta(seconds=2)
            - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if wait > 0.5:
        assert answer_of(tasks_cli(lab, "run", "alpha"))["would_claim"] is None
    time.sleep(max(0.0, wait) + 0.3)
    by_hand = answer_of(tasks_cli(lab, "run", "alpha", "--apply"))
    assert by_hand["claimed"] == "t-fail"


@needs_store
def test_service_doctor_fails_a_daemon_whose_planned_wake_is_long_overdue(lab):
    """A daemon held past its planned wake by more than a round trip may hold it
    fails the probe; one that wakes as planned passes it."""
    import datetime
    import signal

    project = lab["project"]
    assert answer_of(tasks_cli(lab, "service", "init"))["written"]
    write_settings(project, "version = 1\npoll_seconds = 3600\nshutdown_grace_seconds = 5\n")
    pid = answer_of(tasks_cli(lab, "service", "start"))["pid"]
    try:
        assert answer_of(tasks_cli(lab, "service", "doctor"))["ok"]
        published = Path(answer_of(tasks_cli(lab, "service", "status"))["state_dir"]) / "daemon.json"
        os.kill(pid, signal.SIGSTOP)
        status = json.loads(published.read_text())
        late = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
        status["next_wake"]["at"] = late.isoformat(timespec="seconds")
        published.write_text(json.dumps(status))
        hung = tasks_cli(lab, "service", "doctor")
        assert hung.returncode == 6
        assert any("overdue; it may hang" in problem
                   for problem in json.loads(hung.stdout)["problems"])
    finally:
        os.kill(pid, signal.SIGCONT)
        answer_of(tasks_cli(lab, "service", "stop"))


@needs_store
def test_service_doctor_warns_while_the_daemon_waits_out_the_store(lab, relay):
    """The probe a supervisor runs: with a daemon running, the store being away
    is a warning and exit 0, and a daemon behind the settings on disk still
    fails it; with no daemon it fails. The daemon lives through it and works
    when the store is back, and a stale declaration still fails the probe until
    a reload."""
    project = lab["project"]
    # The machine's store is reached through the relay.
    _cli.write_store_setting(lab["tmp"] / "config", lab["schema"], host="127.0.0.1",
                             port=relay.port)
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
    assert away["settings"]["poll_seconds"] == 3600
    # A daemon behind the files on disk fails the probe, store or no store.
    settings = project / "capabilities" / "tasks" / "service" / "config.toml"
    kept = settings.read_text()
    settings.write_text(kept.replace("poll_seconds = 3600", "poll_seconds = 1800"))
    stale = tasks_cli(lab, "service", "doctor")
    assert stale.returncode == 6 and "declaration_stale" in json.loads(stale.stdout)
    assert answer_of(tasks_cli(lab, "service", "status"))["current"] is False
    settings.write_text(kept)
    assert answer_of(tasks_cli(lab, "service", "status"))["current"] is None
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
    assert "following tasks_claimable again" in log
    # A worker saved since the daemon loaded is announced by the store, and the
    # daemon takes it up with no reload.
    worker = _cli.worker_docs(project) / "alpha.md"
    worker.write_text(worker.read_text() + "\nedited.\n")
    _cli.save_declared_workers(mod, project, lab["schema"])
    poll_for(lambda: answer_of(tasks_cli(lab, "service", "status"))["current"] is True, 30)
    assert answer_of(tasks_cli(lab, "service", "doctor"))["ok"]
    log = "\n".join(answer_of(tasks_cli(lab, "service", "logs", "--tail", "200"))["lines"])
    assert "the store announced a change to the declaration; reloading" in log
    assert answer_of(tasks_cli(lab, "service", "reload"))["reloaded"] is False
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
        tasks_cli(lab, "service", "stop", "--end-turns", "--timeout", "30", "--force")
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


@needs_store
def test_each_turn_reads_the_env_files_as_they_are_when_it_starts(lab):
    """The daemon reads no env file of the project: each turn reads .env and
    .env.local itself, so an edit reaches the next turn of the same daemon with
    no restart."""
    project = lab["project"]
    record = lab["tmp"] / "engine.jsonl"
    (project / ".env.local").write_text(f"FAKE_ENGINE_RECORD={record}\n")

    def dotenv(probe: str) -> None:
        (project / ".env").write_text(
            f"FAKE_ENGINE_TASKS={lab['env']['FAKE_ENGINE_TASKS']}\n"
            f"TURN_PROBE_EDITED={probe}\n")

    dotenv("first")
    assert answer_of(tasks_cli(lab, "service", "init"))["written"]
    write_settings(project, "version = 1\npoll_seconds = 3600\nshutdown_grace_seconds = 5\n")
    minimal = {key: lab["env"][key] for key in (
        "PATH", "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "UV_CACHE_DIR",
        "CAPABILITIES_HOME", "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_MANAGER_BIN",
        "TMPDIR") if key in lab["env"]}
    log = (lab["tmp"] / "supervised.log").open("w")
    daemon = subprocess.Popen([str(_cli.CLI_PATH), "service", "run"], cwd=project, env=minimal,
                              stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    try:
        poll_for(lambda: answer_of(tasks_cli(lab, "service", "status"))["running"], 60)
        pid = answer_of(tasks_cli(lab, "service", "status"))["pid"]
        for key in ("t-first", "t-second"):
            if key == "t-second":
                dotenv("second")
            answer_of(tasks_cli(lab, "add", "--type", "alpha", "--title", key, "--key", key,
                                "--status", "todo"))
            poll_for(lambda key=key: answer_of(tasks_cli(lab, "show", key))["task"]["status"]
                     == "complete", 60)
        status = answer_of(tasks_cli(lab, "service", "status"))
        # The same daemon throughout: nothing was restarted for the edit.
        assert status["pid"] == pid and status["state"] == "served"
    finally:
        tasks_cli(lab, "service", "stop", "--end-turns", "--timeout", "30", "--force")
        daemon.wait(timeout=60)
        log.close()
    turns = [json.loads(line) for line in record.read_text().splitlines()]
    assert [turn["env"]["TURN_PROBE_EDITED"] for turn in turns] == ["first", "second"]
    assert all("CLAUDE_PROJECT_DIR" not in turn["env"] for turn in turns)
