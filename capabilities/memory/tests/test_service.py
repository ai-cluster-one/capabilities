"""The service: the loop's admission and its asks with a fake host and store,
the executable's admission rule, and the command line, in machine mode,
started for real against a store schema of the case's own."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _cli import CAPABILITY_DIR, CLI_PATH, World, cli_python, drop_schema, error, load, \
    needs_store, new_schema, project_id

LOOP_PATH = CAPABILITY_DIR / "service" / "engine.py"


def loop_module(monkeypatch):
    spec = importlib.util.spec_from_file_location("memory_service_under_test", LOOP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "FIRST_ASK_SECONDS", 0.0)
    monkeypatch.setattr(module, "SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(module, "RECHECK_SECONDS", 3600.0)
    monkeypatch.setattr(module, "CHECK_SECONDS", 3600.0)
    monkeypatch.setattr(module, "ADMIT_SECONDS", 0.1)
    return module


# A pass that marks the agent's file and lasts as long as the host says.
CHILD = ("import sys, time; open(sys.argv[1], 'a').write('ran\\n'); "
         "time.sleep(float(sys.argv[2]))")


class FakeSetting:
    schema = "fake"
    has_transaction_address = False

    def connect_kwargs(self):
        return {"host": "fake"}


class FakeConn:
    autocommit = False

    def close(self):
        pass


class FakeDb:
    """The store: its change channel is what the case puts in `moves`."""

    def __init__(self):
        self.moves: list[str] = []
        self.down = False
        db = self

        class Follower:
            def __init__(self, channels, **_kw):
                assert channels == ["memory_changed"]

            def poll(self, _conn):
                moved, db.moves[:] = list(db.moves), []
                return {("memory_changed", agent) for agent in moved}

            def close(self):
                pass

        self.Follower = Follower

    def connect(self, **_kw):
        if self.down:
            raise OSError("the store is away")
        return FakeConn()

    def migrate(self, *_a, **_kw):
        pass


class FakeHost:
    name = "memory"
    channel = "memory_changed"

    def __init__(self, tmp: Path, agents: list[str], seconds: float = 0.3):
        self.tmp = tmp
        self.fake_db = FakeDb()
        self.seconds = seconds
        self.refuse: dict[str, tuple] = {}
        self.admitted: list[str] = []
        self.listed = {}
        for agent in agents:
            root = tmp / agent
            root.mkdir()
            self.listed[agent] = {"root": str(root), "slug": agent}

    def db(self):
        return self.fake_db

    def ledger(self):
        return [], 2, 1

    def node(self):
        return "node-test"

    def entries(self):
        return dict(self.listed)

    def list_witness(self):
        return tuple(sorted(self.listed))

    def lock_path(self, slug):
        return self.tmp / "locks" / slug / "daemon.lock"

    def admit(self, agent_id, root, machine):
        self.admitted.append(agent_id)
        if agent_id in self.refuse:
            return self.refuse[agent_id], None
        return None, FakeSetting()

    def groom_command(self, root):
        return [sys.executable, "-c", CHILD, str(root / "ran"), str(self.seconds)], dict(os.environ)


def ran(host, agent) -> int:
    path = Path(host.listed[agent]["root"]) / "ran"
    return len(path.read_text().splitlines()) if path.exists() else 0


def drive(service, check, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        service.step()
        found = check()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError("the condition did not hold in time")


def make(module, tmp_path, agents, seconds, max_passes=None):
    host = FakeHost(tmp_path, agents, seconds)
    state = tmp_path / "state"
    state.mkdir()
    return host, module.Service(host, mode="machine", state_dir=state,
                                max_passes=max_passes or module.MAX_PASSES)


def test_an_admitted_agent_is_asked_soon_one_pass_at_a_time(monkeypatch, tmp_path):
    module = loop_module(monkeypatch)
    host, service = make(module, tmp_path, ["a"], seconds=0.5)
    try:
        drive(service, lambda: service.served["a"].child is not None)
        assert service.served["a"].state == "served"
        # A change while the pass runs starts no second one beside it.
        host.fake_db.moves.append("a")
        for _ in range(5):
            service.step()
            assert service.running() == 1
        # ... and is asked about once the pass ends.
        drive(service, lambda: ran(host, "a") == 2)
        drive(service, lambda: service.served["a"].child is None
              and service.served["a"].last is not None)
        assert service.served["a"].last["exit"] == 0
    finally:
        service.close()


def test_a_change_in_the_store_asks_again_and_the_timer_asks_on_its_own(monkeypatch, tmp_path):
    module = loop_module(monkeypatch)
    host, service = make(module, tmp_path, ["a", "b"], seconds=0.0)
    try:
        drive(service, lambda: ran(host, "a") == 1 and ran(host, "b") == 1
              and service.running() == 0)
        # Nothing asks while nothing changes.
        for _ in range(10):
            service.step()
            time.sleep(0.03)
        assert ran(host, "a") == 1 and ran(host, "b") == 1
        # A change asks for that agent alone.
        host.fake_db.moves.append("a")
        drive(service, lambda: ran(host, "a") == 2 and service.running() == 0)
        assert ran(host, "b") == 1
        # The timer asks with no change at all.
        monkeypatch.setattr(module, "RECHECK_SECONDS", 0.3)
        service.served["b"].timer_at = time.monotonic()
        drive(service, lambda: ran(host, "b") == 2)
    finally:
        service.close()


def test_the_machine_cap_holds_and_every_agent_is_served_in_turn(monkeypatch, tmp_path):
    module = loop_module(monkeypatch)
    host, service = make(module, tmp_path, ["a", "b", "c"], seconds=0.3, max_passes=1)
    try:
        deadline = time.monotonic() + 20
        while not all(ran(host, agent) for agent in "abc"):
            assert time.monotonic() < deadline, "not every agent was served"
            service.step()
            assert service.running() <= 1
            time.sleep(0.03)
    finally:
        service.close()


def test_admission_is_asked_before_every_pass_and_one_refusal_leaves_the_others(
        monkeypatch, tmp_path):
    module = loop_module(monkeypatch)
    host, service = make(module, tmp_path, ["a", "b"], seconds=0.0)
    host.refuse["b"] = ("refused", "memory is not enabled for this project (project)")
    try:
        drive(service, lambda: ran(host, "a") == 1 and service.running() == 0)
        assert ran(host, "b") == 0
        row = {r["project"]: r for r in service.status()["projects"]}["b"]
        assert row["state"] == "refused" and "not enabled" in row["reason"]
        # A served agent refused between its ask and its start gets no pass:
        # the periodic check is an hour away, so the refusal is the one asked
        # just before the start.
        host.refuse["a"] = ("refused", "memory is quarantined on this machine")
        host.fake_db.moves.append("a")
        drive(service, lambda: service.served["a"].state == "refused")
        assert ran(host, "a") == 1 and service.served["a"].child is None
        # Every line names its project.
        lines = (service.state_dir / module.LOG_FILE).read_text().splitlines()
        assert lines and all(" memory service [" in line for line in lines)
        assert any("[b]: not served: refused" in line for line in lines)
        assert any("[a]: no longer served: refused" in line for line in lines)
    finally:
        service.close()


def test_a_store_that_does_not_answer_holds_the_pass_back(monkeypatch, tmp_path):
    module = loop_module(monkeypatch)
    host, service = make(module, tmp_path, ["a"], seconds=0.0)
    host.fake_db.down = True
    try:
        for _ in range(10):
            service.step()
            time.sleep(0.03)
        assert ran(host, "a") == 0
        assert service.status()["stores"][0]["error"]
        host.fake_db.down = False
        service.links[next(iter(service.links))].retry_at = 0.0
        drive(service, lambda: ran(host, "a") == 1)
    finally:
        service.close()


def test_one_lock_per_project_keeps_a_second_process_off_it(monkeypatch, tmp_path):
    module = loop_module(monkeypatch)
    host, first = make(module, tmp_path, ["a"], seconds=5.0)
    second_state = tmp_path / "state2"
    second_state.mkdir()
    second = module.Service(host, mode="machine", state_dir=second_state)
    try:
        drive(first, lambda: first.served["a"].state == "served")
        second.step()
        assert second.served["a"].state == "refused"
        assert "another memory service process serves this project" in second.served["a"].reason
    finally:
        first.close()
        second.close()


# ── the executable's admission ───────────────────────────────────────────────

def admission(*, policy, machine_state="allowed", identity="prj_a",
              listed=("prj_a",)):
    cli = load()
    cli.__dict__["_machine_state"] = lambda: machine_state
    cli.__dict__["_policy_state"] = lambda: policy
    cli.__dict__["_identity_here"] = lambda: (identity, "a")
    cli.__dict__["_read_projects"] = lambda: {p: {} for p in listed}
    return cli


@pytest.mark.parametrize("source", ["project", "global"])
def test_memory_enabled_explicitly_or_inherited_is_admitted(monkeypatch, source):
    cli = admission(policy={"effective": "enabled", "source": source,
                                         "machine": "allowed"})
    assert cli._admission("prj_a", True) is None
    assert cli._admission("prj_a", False) is None


def test_what_admission_refuses(monkeypatch):
    on = {"effective": "enabled", "source": "global", "machine": "allowed"}
    cli = admission(policy={"effective": "disabled", "source": "project",
                                         "machine": "allowed"})
    assert cli._admission("prj_a", True)[0] == "refused"
    cli = admission(policy={"effective": "disabled", "source": "default",
                                         "machine": "allowed"})
    assert "not enabled" in cli._admission("prj_a", True)[1]
    cli = admission(policy=on, machine_state="quarantined")
    assert "quarantined" in cli._admission("prj_a", True)[1]
    cli = admission(policy=on, identity="prj_other")
    assert "prj_other" in cli._admission("prj_a", True)[1]
    cli = admission(policy=on, listed=())
    assert cli._admission("prj_a", True) == ("refused", "the project left the machine service")
    assert cli._admission("prj_a", False) is None
    cli = admission(policy={"error": {"code": "bad_policy", "message": "x"}})
    assert cli._admission("prj_a", True) == ("error", "bad_policy: x")


# ── the command line ─────────────────────────────────────────────────────────

def test_status_and_doctor_with_no_process(tmp_path):
    world = World(tmp_path, None)
    status = world.ok(tmp_path, "service", "status", "--machine")
    assert status["mode"] == "machine" and status["running"] is False
    assert status["list"]["path"] == str(world.config / "memory" / "service" / "projects.json")
    done = world.run(tmp_path, "service", "doctor", "--machine")
    assert done.returncode == 7, done.stderr
    assert json.loads(done.stdout)["process"]["error"] == "the machine service is not running"
    for path in (("service",), ("service", "run"), ("service", "status"), ("service", "doctor")):
        assert world.run(tmp_path, "help", *path).returncode == 0
    menu = world.run(tmp_path, "help").stdout
    assert "automation" not in menu and "memory service run" in menu
    assert "automations set" not in world.run(tmp_path, "help", "groom").stdout


@pytest.fixture
def world(tmp_path):
    schema = new_schema()
    made = World(tmp_path, schema)
    try:
        yield made
    finally:
        drop_schema(schema)


def put_on_list(world, *roots):
    """The machine list, as the node manager writes it for each agent on this
    node that has memory enabled."""
    path = world.config / "memory" / "service" / "projects.json"
    listed = {"version": 1, "projects": {project_id(root): {
        "root": str(root), "slug": root.name, "joined_at": "2026-11-26T00:00:00Z",
        "joined_by": "the node manager"} for root in roots}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(listed, indent=2) + "\n")


def until(check, seconds=120.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.5)
    raise AssertionError("the condition did not hold in time")


@needs_store
def test_the_machine_service_grooms_every_agent_that_has_memory_enabled(world):
    world.enable_globally()
    inherited = world.project("inherited", enabled=None)
    explicit = world.project("explicit")
    disabled = world.project("disabled", enabled=False)
    for root in (inherited, explicit):
        world.ok(root, "add", "x", "--title", "t")
    put_on_list(world, inherited, explicit, disabled)
    process = subprocess.Popen([cli_python(), str(CLI_PATH), "service", "run", "--machine"],
                               cwd=world.tmp,
                               env=world.env(), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        def rows():
            status = world.ok(world.tmp, "service", "status", "--machine")
            found = {r["project"]: r for r in status["projects"]}
            return found if all((found[n].get("last_groom") or {}).get("exit") == 0
                                for n in ("inherited", "explicit")) else None
        found = until(rows)
        assert found["inherited"]["state"] == found["explicit"]["state"] == "served"
        assert found["disabled"]["state"] == "refused"
        assert "not enabled" in found["disabled"]["reason"]
        state = world.state / "capabilities" / "machine" / "memory"
        assert '"due": false' in (state / "groom.log").read_text()
        doctor = world.ok(world.tmp, "service", "doctor", "--machine")
        assert doctor["ok"] and doctor["process"]["ok"]
        # A listed project's own process is refused.
        done = world.run(explicit, "service", "run")
        assert done.returncode == 4 and error(done)["code"] == "joined_to_machine_service"
        assert world.ok(explicit, "service", "status")["project"]["state"] == "served"
    finally:
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err
    log = (world.state / "capabilities" / "machine" / "memory" / "daemon.log").read_text()
    assert log and all(" memory service [" in line for line in log.splitlines())
    assert "[inherited]: served" in log and "[disabled]: not served: refused" in log
