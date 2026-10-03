#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "psycopg[binary]>=3.2", "callva-harness-runner==0.8.0",
#                 "pyyaml>=6"]
# ///
"""The service as a dispatcher over project slots: the scope every question
about a project runs in, two projects served by one process with nothing
crossing between them, the check before every action, and the refusals only a
machine slot makes.

Projects are written into a temp directory and found the way the executable
finds one, from CLAUDE_PROJECT_DIR, so each scope really points the resolver at
its own project. One is identified by a UUID and one carries a slug that is not
its folder's name. Turns are replaced by a small process that records where it
ran and with what environment. The store-backed checks read TASKS_TEST_DSN and
skip when it is unset; every run works in schemas of its own and drops them.

    uv run --with pytest --with 'psycopg[binary]>=3.2' --with 'pyyaml>=6' \\
        --with 'callva-harness-runner==0.8.0' python -m pytest capabilities/tasks/tests -q
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_service as base  # noqa: E402

mod = base.mod
needs_store = base.needs_store
DSN = base.DSN

# A turn that records where it ran and what it was started with, and ends
# having claimed nothing, so its lane waits for the next poll.
RECORDING_TURN = """
import json, os, sys
from pathlib import Path
worker, record = sys.argv[1], Path(sys.argv[2])
(record / f"{worker}-{os.getpid()}.json").write_text(
    json.dumps({"worker": worker, "cwd": os.getcwd(), "env": dict(os.environ)}))
"""


def write_project(root: Path, *, project_id: str, slug: str, worker: str, takes: str,
                  connection: str, entry: dict) -> Path:
    """A project the executable resolves on its own: its identity, tasks enabled
    for it, one connection, one worker, and the service settings."""
    envelope = root / "capabilities"
    tasks = envelope / "tasks"
    (tasks / "workers").mkdir(parents=True)
    (tasks / "profiles").mkdir()
    (tasks / "profiles" / "plain.toml").write_text(base.PROFILE)
    (envelope / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "id": project_id, "slug": slug}))
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"tasks": {"enabled": True}}}))
    (tasks / "connections.json").write_text(json.dumps(
        {"default": connection, "connections": {connection: entry}}))
    base.write_worker(root, worker, f"takes: [{takes}]\nprofile: plain")
    base.write_worker(root, "default", "enabled: false")
    base.write_settings(root, "version = 1\npoll_seconds = 3600\n")
    return root


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """What every project here resolves against: a state home of the test's own,
    no machine-level configuration, and project envelopes where they stand."""
    monkeypatch.setattr(mod, "_project_capabilities_dir", lambda root: root / "capabilities")
    monkeypatch.setattr(mod, "_STATE_HOME", tmp_path / "state")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-machine"))
    for leaked in ("TASKS_EXECUTION", "CAPABILITIES_READ_ONLY", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_PROJECT_ENVELOPE",
                   "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CLAUDE_PROJECT_DIR",
                   "TASKS_ACTOR"):
        monkeypatch.delenv(leaked, raising=False)
    return tmp_path.resolve()


def entry_for(schema: str, secret_env: str, **over) -> dict:
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(DSN or "postgresql://nobody@127.0.0.1:5432/none")
    entry = {"db_host": info.get("host"), "db_port": str(info.get("port") or 5432),
             "db_user": info.get("user"), "db_name": info.get("dbname"),
             "db_sslmode": info.get("sslmode") or "prefer", "db_schema": schema,
             "secret_env": secret_env, "allow_write": True}
    entry.update(over)
    return {key: value for key, value in entry.items() if value is not None}


class Pair:
    """Two projects - an orchard identified by a UUID, and a kiln whose slug is
    not its folder's name - each with its own worker, schema and connection,
    served by one dispatcher."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.record = tmp / "turns-seen"
        self.record.mkdir()
        self.script = tmp / "recording_turn.py"
        self.script.write_text(RECORDING_TURN)
        suffix = secrets.token_hex(3)
        self.schemas = {"orchard": f"tasks_test_orchard_{suffix}",
                        "kiln": f"tasks_test_kiln_{suffix}"}
        self.ids = {"orchard": str(uuid.uuid4()), "kiln": "prj_" + uuid.uuid4().hex[:12]}
        self.entries = {
            "orchard": entry_for(self.schemas["orchard"], "TASKS_TEST_PASSWORD"),
            "kiln": entry_for(self.schemas["kiln"], "TASKS_TEST_PASSWORD_KILN"),
        }
        self.roots = {
            "orchard": write_project(tmp / "orchard", project_id=self.ids["orchard"],
                                     slug="orchard", worker="picker", takes="fruit",
                                     connection="orchard-store",
                                     entry=self.entries["orchard"]),
            "kiln": write_project(tmp / "pottery-shed", project_id=self.ids["kiln"],
                                  slug="kiln", worker="potter", takes="clay",
                                  connection="kiln-store", entry=self.entries["kiln"]),
        }

    def build(self, *, machine: bool = False, tick: float = 0.1):
        service = mod._service_module()
        self.service = service
        self.dispatcher = service.Dispatcher(tick=tick)
        self.slots = {}
        for name, root in self.roots.items():
            host = mod._service_host(root, None, self.dispatcher.environment)
            host.turn_command = lambda worker: [sys.executable, str(self.script), worker,
                                                str(self.record)]
            with host.scope(self.dispatcher.environment):
                declaration = host.load()
            self.slots[name] = self.dispatcher.add(host, declaration, machine=machine)
        return self

    def add(self, name: str, kind: str, *, project: str | None = None) -> None:
        """A task written into `name`'s schema, by `project` (its own by default)."""
        slot = self.slots[name]
        with slot.scope():
            mod.PROJECT = project or slot.host.project
            mod.cmd_add(self.entries[name], ["--type", kind, "--title", f"a {kind}",
                                             "--status", "todo"])

    def steps(self, until, seconds: float = 8.0) -> None:
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            self.dispatcher.step()
            if until():
                return
        raise AssertionError(f"not reached in {seconds}s")

    def settle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.dispatcher.step()

    def seen(self) -> list[dict]:
        return [json.loads(path.read_text()) for path in sorted(self.record.glob("*.json"))]


@pytest.fixture
def pair(machine, monkeypatch):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    password = conninfo_to_dict(DSN).get("password") or "" if DSN else ""
    monkeypatch.setenv("TASKS_TEST_PASSWORD", password)
    monkeypatch.setenv("TASKS_TEST_PASSWORD_KILN", password)
    found = Pair(machine)
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            for schema in found.schemas.values():
                conn.execute(mod._schema_ddl(schema))
    try:
        yield found
    finally:
        dispatcher = getattr(found, "dispatcher", None)
        if dispatcher is not None:
            dispatcher.stopping = True
            for slot in dispatcher.slots:
                for turn in list(slot.turns.values()):
                    turn.process.kill()
                    turn.process.wait()
                slot.turns.clear()
            dispatcher.close()
        if DSN:
            with psycopg.connect(DSN, autocommit=True) as conn:
                for schema in found.schemas.values():
                    conn.execute(f"drop schema if exists {schema} cascade")


# --- The scope ---------------------------------------------------------------

def test_a_scope_sets_the_project_and_puts_everything_back(machine, monkeypatch):
    root = machine / "orchard"
    write_project(root, project_id=str(uuid.uuid4()), slug="orchard", worker="picker",
                  takes="fruit", connection="orchard-store",
                  entry=entry_for("tasks_orchard", "TASKS_TEST_PASSWORD"))
    sentinel = object()
    monkeypatch.setattr(mod, "_RECORDS", sentinel)
    monkeypatch.setattr(mod, "CATCH_UP", True)
    monkeypatch.setattr(mod, "SCHEMA", "outer_schema")
    monkeypatch.setattr(mod, "PROJECT", "prj_outer")
    monkeypatch.setattr(mod, "PROJECT_IDENTITY", {"state": "outer"})
    monkeypatch.setenv("SCOPE_PROBE", "start")
    snapshot = dict(os.environ)
    with mod.ProjectScope(root, snapshot, schema="tasks_orchard", project="prj_inner",
                          identity={"state": "inner"}):
        assert os.environ["CLAUDE_PROJECT_DIR"] == str(root)
        assert mod._project_root() == root.resolve()
        assert (mod.SCHEMA, mod.PROJECT, mod.PROJECT_IDENTITY) == (
            "tasks_orchard", "prj_inner", {"state": "inner"})
        assert mod._RECORDS is None and mod.CATCH_UP is False
        # What resolving the project writes into the environment stays inside.
        mod._records()
        mod._declared_project()
        os.environ["SCOPE_PROBE"] = "changed"
        os.environ["SCOPE_ONLY"] = "inside"
        with pytest.raises(RuntimeError):
            with mod.ProjectScope(root, snapshot):
                pass
    assert dict(os.environ) == snapshot
    assert mod._RECORDS is sentinel and mod.CATCH_UP is True
    assert (mod.SCHEMA, mod.PROJECT, mod.PROJECT_IDENTITY) == (
        "outer_schema", "prj_outer", {"state": "outer"})
    assert mod.ProjectScope.current is None
    # An exit by exception puts everything back too.
    with pytest.raises(SystemExit):
        with mod.ProjectScope(root, snapshot):
            os.environ["SCOPE_ONLY"] = "inside"
            mod._die(6, "input", "refused inside")
    assert dict(os.environ) == snapshot and mod.SCHEMA == "outer_schema"


def test_a_scope_opened_to_resolve_a_project_starts_from_the_modules_own_values(
        machine, monkeypatch):
    root = machine / "pottery-shed"
    kiln = "prj_" + uuid.uuid4().hex[:12]
    write_project(root, project_id=kiln, slug="kiln", worker="potter", takes="clay",
                  connection="kiln-store", entry=entry_for("tasks_kiln", "TASKS_TEST_PASSWORD"))
    monkeypatch.setattr(mod, "SCHEMA", "outer_schema")
    monkeypatch.setattr(mod, "PROJECT", "prj_outer")
    host = mod._service_host(root, None, dict(os.environ))
    assert (host.root, host.slug, host.project, host.schema) == (
        root.resolve(), "kiln", kiln, "tasks_kiln")
    assert host.state_dir == machine / "state" / "capabilities" / "projects" / "kiln" / "tasks"
    assert host.identity["id"] == kiln
    assert (mod.SCHEMA, mod.PROJECT) == ("outer_schema", "prj_outer")


def test_a_turn_starts_from_the_start_environment_not_from_a_scope(machine, monkeypatch):
    root = machine / "orchard"
    write_project(root, project_id=str(uuid.uuid4()), slug="orchard", worker="picker",
                  takes="fruit", connection="orchard-store",
                  entry=entry_for("tasks_orchard", "TASKS_TEST_PASSWORD"))
    for key, value in (("TASKS_EXECUTION", "outer-raise"), ("TASKS_TURN_RECEIPT", "r"),
                       ("TASKS_TURN_EXCLUDE", "x"), ("CLAUDE_PROJECT_DIR", str(machine)),
                       ("KEPT_PROBE", "kept")):
        monkeypatch.setenv(key, value)
    snapshot = dict(os.environ)
    host = mod._service_host(root, None, snapshot)
    with host.scope(snapshot):
        os.environ["SCOPE_ONLY"] = "inside"
        env = host.turn_env(snapshot)
    assert env == {key: value for key, value in snapshot.items()
                   if key not in ("TASKS_EXECUTION", "TASKS_TURN_RECEIPT", "TASKS_TURN_EXCLUDE",
                                  "CLAUDE_PROJECT_DIR")}
    assert env["KEPT_PROBE"] == "kept" and "SCOPE_ONLY" not in env


# --- Two projects, one process -----------------------------------------------

@needs_store
def test_two_projects_in_one_dispatcher_share_nothing(pair, monkeypatch):
    """Two slots with different workers, schemas and connections: each asks only
    its own workers on its own schema, each notification wakes only the slot of
    its schema and project, each turn runs in its own project with its own
    environment, and after every scope the environment and the globals are what
    they were. No secret is resolved inside a scope."""
    sentinel = object()
    monkeypatch.setattr(mod, "_RECORDS", sentinel)
    # Disarmed outside, as the service runs: the dispatcher's own connections
    # are opened outside every scope, and an armed CATCH_UP would bring the
    # outer schema up to date on the first of them.
    monkeypatch.setattr(mod, "CATCH_UP", False)
    monkeypatch.setattr(mod, "SCHEMA", "outer_schema")
    monkeypatch.setattr(mod, "PROJECT", "prj_outer")
    monkeypatch.setattr(mod, "PROJECT_IDENTITY", None)
    outer = ("outer_schema", "prj_outer", None, False, sentinel)
    pair.build()
    dispatcher, slots = pair.dispatcher, pair.slots
    snapshot = dict(dispatcher.environment)
    assert len(dispatcher.stores()) == 2  # two connections, two stores held

    # What each scope saw inside, and what was left once it closed.
    inside, after, secrets_resolved = [], [], []
    enter, leave = mod.ProjectScope.__enter__, mod.ProjectScope.__exit__

    def entering(scope):
        found = enter(scope)
        inside.append((str(scope.root), os.environ["CLAUDE_PROJECT_DIR"], mod.SCHEMA,
                       mod.PROJECT, mod.CATCH_UP, mod._RECORDS))
        return found

    def leaving(scope, *exc):
        found = leave(scope, *exc)
        after.append((dict(os.environ) == snapshot,
                      (mod.SCHEMA, mod.PROJECT, mod.PROJECT_IDENTITY, mod.CATCH_UP,
                       mod._RECORDS) == outer))
        return found

    monkeypatch.setattr(mod.ProjectScope, "__enter__", entering)
    monkeypatch.setattr(mod.ProjectScope, "__exit__", leaving)
    tracking = {"on": True}
    resolve = mod._resolve_env_key
    secret_keys = {"TASKS_TEST_PASSWORD", "TASKS_TEST_PASSWORD_KILN"}

    def resolving(key):
        if tracking["on"] and key in secret_keys:
            secrets_resolved.append((key, mod.ProjectScope.current is not None))
        return resolve(key)

    monkeypatch.setattr(mod, "_resolve_env_key", resolving)
    asked = {name: [] for name in slots}
    for name, slot in slots.items():
        own = slot.host.lane_has_work
        slot.host.lane_has_work = (lambda conn, worker, exclude=(), name=name, own=own: (
            asked[name].append((worker["name"], mod.SCHEMA, mod.PROJECT,
                                os.environ["CLAUDE_PROJECT_DIR"]))
            or own(conn, worker, exclude)))
    woken = {name: 0 for name in slots}
    for name, slot in slots.items():
        heard = slot.notified
        slot.notified = (lambda payload, name=name, heard=heard: (
            woken.__setitem__(name, woken[name] + 1) or heard(payload)))

    dispatcher.open()
    pair.settle(0.5)
    assert all(slot.state() == "served" for slot in slots.values())
    assert all(slot.status()["wake_by"] == "notification" for slot in slots.values())

    # A task in the orchard's schema but another project's: no slot's.
    tracking["on"] = False
    pair.add("orchard", "fruit", project=pair.ids["kiln"])
    tracking["on"] = True
    pair.settle(1.0)
    assert woken == {"orchard": 0, "kiln": 0} and pair.seen() == []

    tracking["on"] = False
    pair.add("orchard", "fruit")
    tracking["on"] = True
    pair.steps(lambda: len(pair.seen()) == 1)
    assert woken == {"orchard": 1, "kiln": 0}
    tracking["on"] = False
    pair.add("kiln", "clay")
    tracking["on"] = True
    pair.steps(lambda: len(pair.seen()) == 2)
    assert woken == {"orchard": 1, "kiln": 1}
    pair.settle(0.5)

    # Each asked only its own worker, on its own schema, as its own project.
    for name, slot in slots.items():
        worker = {"orchard": "picker", "kiln": "potter"}[name]
        assert asked[name] and set(asked[name]) == {
            (worker, pair.schemas[name], pair.ids[name], str(pair.roots[name]))}
    # Each turn ran in its own project, from the start environment.
    by_worker = {turn["worker"]: turn for turn in pair.seen()}
    assert set(by_worker) == {"picker", "potter"}
    for name, worker in (("orchard", "picker"), ("kiln", "potter")):
        turn = by_worker[worker]
        assert Path(turn["cwd"]).resolve() == pair.roots[name].resolve()
        env = {key: value for key, value in turn["env"].items()
               if key not in ("TASKS_TURN_RECEIPT", "TASKS_TURN_EXCLUDE")}
        expected = slots[name].host.turn_env(snapshot)
        assert {key: env.get(key) for key in expected} == expected
        assert set(env) - set(expected) <= {"__CF_USER_TEXT_ENCODING"}, set(env) - set(expected)
        assert "CLAUDE_PROJECT_DIR" not in env
        assert turn["env"]["TASKS_TURN_RECEIPT"].startswith(str(slots[name].turns_dir))
    # Every scope opened for its own project and closed back onto the snapshot.
    roots = {str(root) for root in pair.roots.values()}
    assert len(inside) > 10 and len(after) == len(inside)
    assert all(seen[0] in roots and seen[1] == seen[0] for seen in inside)
    assert all(seen[4] is False and seen[5] is not sentinel for seen in inside)
    for root, _dir, schema, project, _c, _r in inside:
        name = next(n for n, r in pair.roots.items() if str(r) == root)
        assert (schema, project) == (pair.schemas[name], pair.ids[name])
    assert all(environment and globals_ for environment, globals_ in after)
    assert dict(os.environ) == snapshot
    # The secrets were resolved, and never inside a scope.
    assert len(secrets_resolved) >= 4
    assert not [key for key, scoped in secrets_resolved if scoped]


@needs_store
def test_the_state_root_lock_and_log_tag_come_from_the_slug_and_the_identity_from_the_id(
        pair, monkeypatch):
    pair.build()
    pair.dispatcher.open()
    pair.add("orchard", "fruit")
    pair.add("kiln", "clay")
    pair.steps(lambda: len(pair.seen()) == 2)
    pair.settle(0.3)
    projects = pair.tmp / "state" / "capabilities" / "projects"
    for name, slug in (("orchard", "orchard"), ("kiln", "kiln")):
        slot = pair.slots[name]
        assert slot.state_dir == projects / slug / "tasks"
        assert pair.roots[name].name == {"orchard": "orchard", "kiln": "pottery-shed"}[name]
        # The lock is held, in the slug's state root.
        with (slot.state_dir / "daemon.lock").open("a+") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert (slot.state_dir / "daemon.pid").read_text().strip() == str(os.getpid())
        lines = (slot.state_dir / "daemon.log").read_text().splitlines()
        assert lines and all(f" tasks service [{slug}]: " in line for line in lines), lines
        status = json.loads((slot.state_dir / "daemon.json").read_text())
        assert status["project"] == pair.ids[name] and slot.host.project == pair.ids[name]
        assert all(row["project"] == slug for row in status["lanes"])
    assert not (projects / "pottery-shed").exists()
    uuid.UUID(pair.ids["orchard"])
    # A turn row names its project by the slug.
    pair.dispatcher.stopping = True
    slot = pair.slots["kiln"]
    turn = slot.spawn("potter", "work") if slot.served() else None
    assert turn is not None and turn.row()["project"] == "kiln"
    turn.process.wait()


# --- The check before every action -------------------------------------------

def counted(slot) -> dict:
    """Count every question the slot puts to the store about its project."""
    counts = {"questions": 0}
    for name in ("lane_has_work", "sweep_due", "next_moment", "catch_up",
                 "notification_installed"):
        own = getattr(slot.host, name)

        def asking(*args, own=own, **kwargs):
            counts["questions"] += 1
            return own(*args, **kwargs)

        setattr(slot.host, name, asking)
    return counts


@needs_store
def test_a_project_that_fails_the_check_is_asked_nothing_and_served_again_when_it_passes(
        pair):
    """On a one-second poll, with work waiting in the store: each failure makes
    the slot refused - or error, for a folder that is gone - with its reason, no
    question is put to the store for it and no turn starts; putting it right
    serves the project again from the next wake, with no restart."""
    root = pair.roots["orchard"]
    base.write_settings(root, "version = 1\npoll_seconds = 1\n")
    del pair.roots["kiln"]
    pair.build()
    slot = pair.slots["orchard"]
    counts = counted(slot)
    pair.dispatcher.open()
    pair.add("orchard", "fruit")
    pair.steps(lambda: len(pair.seen()) >= 1)
    settings = root / "capabilities" / "settings.json"
    connections = root / "capabilities" / "tasks" / "connections.json"
    registry = json.loads(connections.read_text())

    def granted(**fields):
        changed = json.loads(json.dumps(registry))
        changed["connections"]["orchard-store"].update(fields)
        connections.write_text(json.dumps(changed))

    moved = pair.tmp / "orchard-moved"
    cases = [
        ("not enabled", "refused", "project_enable_required",
         lambda: settings.write_text(json.dumps({"capabilities": {}})),
         lambda: settings.write_text(json.dumps({"capabilities": {"tasks": {"enabled": True}}}))),
        ("withheld", "refused", "connection_not_granted",
         lambda: granted(enabled=False), lambda: connections.write_text(json.dumps(registry))),
        ("read-only", "refused", "read_only:",
         lambda: granted(allow_write=False), lambda: connections.write_text(json.dumps(registry))),
        ("read-only switch", "refused", "read_only_switch",
         lambda: pair.dispatcher.environment.__setitem__("CAPABILITIES_READ_ONLY", "1"),
         lambda: pair.dispatcher.environment.pop("CAPABILITIES_READ_ONLY")),
        ("folder gone", "error", "is gone",
         lambda: root.rename(moved), lambda: moved.rename(root)),
    ]
    log = slot.state_dir / "daemon.log"
    for what, state, said, fail, restore in cases:
        # Served first, turns starting while the work is there.
        pair.steps(lambda: slot.state() == "served", seconds=4)
        pair.settle(1.2)
        while slot.turns:
            pair.dispatcher.step()
        fail()
        pair.steps(lambda: slot.state() == state, seconds=4)
        while slot.turns:
            pair.dispatcher.step()
        time.sleep(0.3)
        asked, started = counts["questions"], len(pair.seen())
        pair.settle(2.5)  # past two polls
        assert slot.state() == state, what
        assert said in slot.status()["reason"], (what, slot.status()["reason"])
        assert json.loads((slot.state_dir / "daemon.json").read_text())["state"] == state
        assert counts["questions"] == asked, what
        assert len(pair.seen()) == started and not slot.turns, what
        assert log.read_text().count(slot.status()["reason"]) == 1, what
        restore()
        pair.steps(lambda: len(pair.seen()) > started, seconds=4)
        assert slot.state() == "served" and slot.status()["reason"] is None, what
    assert log.read_text().count("served again: the check before every action passes") == 5


@needs_store
def test_the_check_is_taken_again_just_before_a_turn_starts(pair):
    root = pair.roots["orchard"]
    del pair.roots["kiln"]
    pair.build()
    slot = pair.slots["orchard"]
    settings = root / "capabilities" / "settings.json"
    own = slot.host.lane_has_work
    answered = []

    def and_then_disabled(conn, worker, exclude=()):
        # The project stops being enabled between the question and the turn.
        found = own(conn, worker, exclude)
        answered.append(found)
        if found:
            settings.write_text(json.dumps({"capabilities": {}}))
        return found

    slot.host.lane_has_work = and_then_disabled
    pair.add("orchard", "fruit")
    pair.dispatcher.open()
    pair.steps(lambda: slot.state() == "refused", seconds=4)
    pair.settle(0.5)
    assert answered == [True]
    assert not slot.turns and pair.seen() == []
    assert "project_enable_required" in slot.status()["reason"]


# --- Refusals only a machine slot makes ---------------------------------------

def machine_case(machine, *, entry_over=None, env_files=None, secret="TASKS_TEST_PASSWORD"):
    root = machine / f"case-{secrets.token_hex(3)}"
    entry = entry_for("tasks_case", secret, **(entry_over or {}))
    write_project(root, project_id="prj_" + uuid.uuid4().hex[:12], slug=root.name,
                  worker="picker", takes="fruit", connection="case-store", entry=entry)
    for name, text in (env_files or {}).items():
        (root / name).write_text(text)
    environment = dict(os.environ)
    host = mod._service_host(root, None, environment)
    return host, environment


@pytest.mark.parametrize("env_files", [
    {".env": "TASKS_TEST_PASSWORD=from-the-project-dotenv\n"},
    {".env.local": "export TASKS_TEST_PASSWORD='from-the-project-local'\n"},
    {".env": "TASKS_TEST_PASSWORD=\n"},
])
def test_a_machine_slot_refuses_a_project_whose_env_files_set_the_store_secret(
        machine, env_files):
    host, environment = machine_case(machine, env_files=env_files)
    with host.scope(environment):
        state, reason = host.recheck(machine=True)
        assert host.recheck(machine=False) is None
    assert state == "refused" and reason.startswith("project_secret_set:")
    assert "TASKS_TEST_PASSWORD" in reason and "from-the-project" not in reason
    # The slot carries it only in machine mode.
    dispatcher = mod._service_module().Dispatcher(environment=environment)
    with host.scope(environment):
        declaration = host.load()
    on_machine = dispatcher.add(host, declaration, machine=True)
    in_project = dispatcher.add(host, declaration)
    assert on_machine.served() is False and on_machine.state() == "refused"
    assert in_project.served() is True and in_project.state() == "served"


@pytest.mark.parametrize("left, named", [
    ({"db_port": None}, "db_port"),
    ({"db_host": None, "db_schema": None}, "db_host, db_schema"),
    ({"db_sslmode": ""}, "db_sslmode"),
])
def test_a_machine_slot_refuses_a_connection_that_leaves_a_value_to_the_environment(
        machine, left, named):
    host, environment = machine_case(machine, entry_over=left)
    with host.scope(environment):
        state, reason = host.recheck(machine=True)
        assert host.recheck(machine=False) is None
    assert state == "refused" and reason.startswith("connection_incomplete:")
    assert f"leaves {named} to the environment" in reason


def test_a_machine_slot_serves_a_project_with_a_full_entry_and_no_secret_of_its_own(machine):
    host, environment = machine_case(machine, env_files={".env": "OTHER_KEY=1\n"})
    with host.scope(environment):
        assert host.recheck(machine=True) is None


def test_every_line_of_the_log_carries_its_project(tmp_path):
    service = mod._service_module()
    service.log_line(tmp_path, "kiln", "lost the store: server closed the connection\n"
                                       "\tThis probably means the server terminated\n")
    service.log_line(tmp_path, "kiln", "stopped")
    lines = (tmp_path / service.LOG_FILE).read_text().splitlines()
    assert len(lines) == 2 and all(" tasks service [kiln]: " in line for line in lines)
    assert lines[0].endswith("server closed the connection This probably means the server "
                             "terminated")
