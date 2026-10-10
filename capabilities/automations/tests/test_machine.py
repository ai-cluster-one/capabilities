"""Machine mode: one scheduler process serving every project that joined it.

The opt-in list, the machine settings, `join`'s refusals and what the machine
process reads of a project are checked with no store. The machine process
itself runs through the CLI, as a supervisor runs it, over fixture projects
written into a temp directory, each with an `.env` of its own; jobs are real
children. The store-backed checks read AUTOMATIONS_TEST_DSN and skip when it is
unset; every project works under an id of its own, so cases never see each
other's runs.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = next((path for path in (
    CAPABILITY / "bin" / "automations", CAPABILITY / "automations")
    if path.is_file()), CAPABILITY / "bin" / "automations")
RUNTIME_PATH = CAPABILITY / "service" / "runtime.py"
DSN = os.environ.get("AUTOMATIONS_TEST_DSN")
needs_store = pytest.mark.skipif(not DSN, reason="AUTOMATIONS_TEST_DSN is unset")

# What points a process at a project or a store; a fixture starts from none.
POINTERS = ("CLAUDE_PROJECT_DIR", "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
            "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
            "AUTOMATIONS_ENVIRONMENT", "AUTOMATIONS_NAMESPACE", "AUTOMATIONS_CONFIG",
            "AUTOMATIONS_STATE_DIR", "CAPABILITIES_STORE_URL", "CAPABILITIES_READ_ONLY")

PROBE = """import json, os, time
print(json.dumps({"secret": os.environ.get("PROJECT_SECRET"), "cwd": os.getcwd(),
                  "root": os.environ.get("AUTOMATION_PROJECT_ROOT"),
                  "trigger": os.environ.get("AUTOMATION_TRIGGER"),
                  "config": os.environ.get("AUTOMATIONS_CONFIG"),
                  "state_dir": os.environ.get("AUTOMATIONS_STATE_DIR")}))
"""
NAP = """import time
print(f"start {time.time()}", flush=True)
time.sleep(1.0)
print(f"end {time.time()}", flush=True)
"""
BEAT = """
[[automations]]
id = "beat"
script = "capabilities/automations/scripts/probe.py"
every_seconds = 2
timeout_seconds = 10
overlap = "skip"
"""
CONFIG = """version = 1
[engine]
tick_seconds = 0.1
max_parallel = 2
timezone = "UTC"
shutdown_grace_seconds = 1

[[automations]]
id = "probe"
script = "capabilities/automations/scripts/probe.py"
timeout_seconds = 10

[[automations]]
id = "nap"
script = "capabilities/automations/scripts/nap.py"
timeout_seconds = 30
max_parallel = 2
max_pending = 10
overlap = "queue"
"""
LINE = re.compile(r"^\S+ automations service \[([^\]]+)\]: ")


def load_cli(name: str = "automations_cli_test"):
    loader = importlib.machinery.SourceFileLoader(name, str(CLI))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_runtime():
    spec = importlib.util.spec_from_file_location("automations_runtime_machine", RUNTIME_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def until(found, seconds: float = 30.0, what: str = "the condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = found()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail(f"{what} did not hold within {seconds:g}s")


class Farm:
    """Projects in a temp directory, the machine's homes beside them, and the
    CLI run in each as a person or a supervisor runs it."""

    def __init__(self, tmp: Path, *, store: bool = True):
        self.tmp = tmp
        self.env = {key: value for key, value in os.environ.items() if key not in POINTERS}
        self.env.update(XDG_CONFIG_HOME=str(tmp / "config"), XDG_STATE_HOME=str(tmp / "state"))
        if store and DSN:
            self.env["CAPABILITIES_STORE_URL"] = DSN
        self.projects: dict[str, dict] = {}

    @property
    def machine_state(self) -> Path:
        return self.tmp / "state" / "capabilities" / "machine" / "automations"

    def project(self, name: str, *, enabled: bool = True, beat: bool = True,
                env_text: str | None = None, config: str | None = None) -> Path:
        root = self.tmp / name
        envelope = root / "capabilities"
        (envelope / "automations" / "service").mkdir(parents=True)
        (envelope / "automations" / "scripts").mkdir(parents=True)
        policy = {"capabilities": {"automations": {"enabled": True}}} if enabled else \
            {"capabilities": {}}
        (envelope / "settings.json").write_text(json.dumps(policy) + "\n")
        project_id = str(uuid.uuid4())
        slug = f"{name}-{project_id[:6]}"
        (envelope / "project.json").write_text(json.dumps(
            {"schema": "capabilities.project.v1", "id": project_id, "slug": slug}) + "\n")
        (envelope / "automations" / "scripts" / "probe.py").write_text(PROBE)
        (envelope / "automations" / "scripts" / "nap.py").write_text(NAP)
        (envelope / "automations" / "service" / "config.toml").write_text(
            config if config is not None else CONFIG + (BEAT if beat else ""))
        (root / ".env").write_text(env_text if env_text is not None
                                   else f"PROJECT_SECRET={name}-secret\n")
        self.projects[name] = {"root": root, "id": project_id, "slug": slug}
        return root

    def state(self, name: str) -> Path:
        return (self.tmp / "state" / "capabilities" / "projects" / self.projects[name]["slug"]
                / "automations")

    def cli(self, name: str | None, *args: str, **env) -> subprocess.CompletedProcess:
        run_env = dict(self.env)
        cwd = self.tmp
        if name is not None:
            cwd = self.projects[name]["root"]
            run_env["CLAUDE_PROJECT_DIR"] = str(cwd)
        run_env.update(env)
        return subprocess.run([str(CLI), *args], cwd=cwd, env=run_env, capture_output=True,
                              text=True, timeout=90)

    def ok(self, name: str | None, *args: str, **env) -> dict:
        done = self.cli(name, *args, **env)
        assert done.returncode == 0, (args, done.returncode, done.stdout, done.stderr)
        return json.loads(done.stdout)

    def refused(self, name: str | None, *args: str, **env) -> tuple[int, dict]:
        done = self.cli(name, *args, **env)
        assert done.returncode != 0, (args, done.stdout)
        return done.returncode, json.loads(done.stderr.strip().splitlines()[-1])["error"]

    def machine(self) -> dict:
        return self.ok(None, "service", "status", "--machine")

    def entry(self, name: str) -> dict | None:
        return next((row for row in self.machine()["projects"]
                     if row["project_id"] == self.projects[name]["id"]), None)

    def until_state(self, name: str, state: str, seconds: float = 30.0) -> dict:
        return until(lambda: (row := self.entry(name)) and row.get("state") == state and row,
                     seconds, f"{name} {state}")

    def runs(self, name: str) -> list[dict]:
        runtime = load_runtime()
        with runtime.RunLedger(self.projects[name]["id"], "").open() as ledger:
            return ledger.list(limit=500)

    def finished(self, name: str, *, trigger: str, status: str = "succeeded") -> list[dict]:
        return [row for row in self.runs(name)
                if row["trigger"] == trigger and row["status"] == status]

    def output(self, row: dict) -> dict:
        return json.loads(Path(row["log_path"]).read_text().strip().splitlines()[-1])

    def stop_all(self) -> None:
        self.cli(None, "service", "stop", "--machine", "--force", "--timeout", "10")
        for name in self.projects:
            self.cli(name, "service", "stop", "--force", "--timeout", "5")


@pytest.fixture
def farm(tmp_path, monkeypatch):
    if not DSN:
        pytest.skip("AUTOMATIONS_TEST_DSN is unset")
    monkeypatch.setenv("CAPABILITIES_STORE_URL", DSN)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    built = Farm(tmp_path)
    yield built
    built.stop_all()


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """Projects and homes with no store at all."""
    monkeypatch.delenv("CAPABILITIES_STORE_URL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return Farm(tmp_path, store=False)


# --- What is declared, and what is read ---------------------------------------

def test_the_manifest_declares_the_machine_service(bare):
    service = bare.ok(None, "manifest", "--json")["service"]
    assert service["machine"] == {
        "schema": "capabilities.service.machine.v1",
        "command": ["automations", "service", "run", "--machine"],
        "doctor": ["automations", "service", "doctor", "--machine"],
        "projects": "$XDG_CONFIG_HOME/automations/service/projects.toml",
        "config": "$XDG_CONFIG_HOME/automations/service/config.toml",
        "state": "$XDG_STATE_HOME/capabilities/machine/automations",
    }
    assert {"run", "status", "doctor", "join", "leave", "pause", "resume"} <= set(service["verbs"])


def test_the_list_and_the_machine_settings_read_back_and_refuse_saying_why(bare, monkeypatch):
    cli = load_cli()
    monkeypatch.setattr(cli, "_CONFIG_HOME", bare.tmp / "config")
    listed = {"prj_one": {"root": "/srv/one", "slug": "one", "joined_at": "2026-10-08T10:00:00Z",
                          "joined_by": "ops@host"}}
    cli._write_machine_projects(listed)
    assert cli._read_machine_projects() == listed
    path = cli._machine_projects_path()
    for text, said in (("version = 2\n", "`version` is 1"),
                       ("version = 1\nextra = 1\n", "'extra'"),
                       ('version = 1\n[projects.p]\nroot = "rel"\nslug = "p"\n', "absolute path")):
        path.write_text(text)
        with pytest.raises(cli._Refusal) as refused:
            cli._read_machine_projects()
        assert said in refused.value.message
    assert cli._read_machine_settings() == {"version": 1, "max_parallel": None}
    initialized = bare.ok(None, "service", "init", "--machine")
    assert cli._read_machine_settings()["max_parallel"] == 8
    assert initialized["written"] == [str(cli._machine_settings_path())]
    cli._machine_settings_path().write_text("version = 1\nmax_parallel = 0\n")
    with pytest.raises(cli._Refusal):
        cli._read_machine_settings()
    cli._machine_settings_path().write_text("version = 1\ngrace = 1\n")
    with pytest.raises(cli._Refusal):
        cli._read_machine_settings()


def test_join_refuses_what_the_machine_cannot_serve_and_leave_takes_off(bare):
    bare.project("plain")
    bare.project("global-only", enabled=False)
    bare.project("own-store", env_text="CAPABILITIES_STORE_URL=postgresql://elsewhere/db\n")
    code, error = bare.refused("global-only", "service", "join")
    assert (code, error["code"]) == (4, "project_enable_required")
    code, error = bare.refused("own-store", "service", "join")
    assert (code, error["code"]) == (6, "project_store_set")
    code, error = bare.refused("plain", "service", "join", CAPABILITIES_READ_ONLY="1")
    assert (code, error["code"]) == (4, "read_only_switch")
    joined = bare.ok("plain", "service", "join")
    assert joined["joined"] is True
    assert joined["project"] == {"id": bare.projects["plain"]["id"],
                                 "slug": bare.projects["plain"]["slug"],
                                 "root": str(bare.projects["plain"]["root"])}
    assert bare.ok("plain", "service", "join")["joined"] is False
    # A second project carrying the same slug would share the state root.
    twin = bare.project("twin")
    identity = json.loads((twin / "capabilities" / "project.json").read_text())
    identity["slug"] = bare.projects["plain"]["slug"]
    (twin / "capabilities" / "project.json").write_text(json.dumps(identity))
    code, error = bare.refused("twin", "service", "join")
    assert (code, error["code"]) == (6, "slug_taken")
    assert bare.ok("plain", "service", "status")["mode"] == "machine"
    code, error = bare.refused("plain", "service", "start")
    assert (code, error["code"]) == (4, "joined_to_machine_service")
    code, error = bare.refused("plain", "service", "run")
    assert (code, error["code"]) == (4, "joined_to_machine_service")
    left = bare.ok(None, "service", "leave", "--machine", "--project",
                   bare.projects["plain"]["slug"])
    assert left["left"] is True
    assert bare.ok("plain", "service", "status")["mode"] == "project"
    assert bare.ok("plain", "service", "leave")["left"] is False


def test_the_machine_refuses_to_run_without_a_store(bare):
    code, error = bare.refused(None, "service", "run", "--machine")
    assert (code, error["code"]) == (6, "store_not_configured")
    code, error = bare.refused(None, "service", "start", "--machine")
    assert (code, error["code"]) == (6, "store_not_configured")


def test_the_machine_process_reads_no_project_environment(bare, monkeypatch):
    """Admitting a project, loading its declaration and checking it before an
    action read its selectors and the names its env files set, and never a
    value of the rest; the process environment is left as it was."""
    root = bare.project("quiet", env_text="PROJECT_SECRET=never-read\n"
                                          "AUTOMATIONS_ENVIRONMENT=staging\n")
    cli = load_cli()
    monkeypatch.setattr(cli, "_CONFIG_HOME", bare.tmp / "config")
    monkeypatch.setattr(cli, "_STATE_HOME", bare.tmp / "state")
    monkeypatch.chdir(bare.tmp)
    read = []
    original = cli._parse_env_file
    monkeypatch.setattr(cli, "_parse_env_file", lambda path: read.append(Path(path))
                        or original(path))
    before = dict(os.environ)
    entry = {"id": bare.projects["quiet"]["id"], "slug": bare.projects["quiet"]["slug"],
             "root": str(root)}
    host, refusal = cli._MachineHost(load_runtime()).admit(entry)
    assert refusal is None
    config = host.load()
    assert config["engine"]["environment"] == "staging"
    assert host.check() is None
    assert not [path for path in read if path.parent == root]
    assert dict(os.environ) == before
    assert host.launcher(["job"])[-4:] == ["service", "launch", "--", "job"]


# --- The machine process -------------------------------------------------------

@needs_store
def test_one_machine_process_serves_two_projects_and_a_third_keeps_its_own(farm):
    """Scheduled and manual runs of two joined projects, each in its own folder
    and environment, under the one ledger by project id and host; a project
    that did not join runs its own daemon as before."""
    for name in ("alpha", "beta", "gamma"):
        farm.project(name)
    for name in ("alpha", "beta"):
        assert farm.ok(name, "service", "join")["joined"] is True
    gamma = farm.ok("gamma", "service", "start")
    assert gamma["mode"] == "project"

    stopped = farm.machine()
    assert stopped["running"] is False
    assert {row["state"] for row in stopped["projects"]} == {None}
    assert all(row["present"] and row["enabled_explicitly"] for row in stopped["projects"])
    probe = farm.cli(None, "service", "doctor", "--machine")
    assert probe.returncode == 6
    assert json.loads(probe.stdout)["ok"] is False

    started = farm.ok(None, "service", "start", "--machine")
    assert started["started"] is True
    machine_pid = started["pid"]
    for name in ("alpha", "beta"):
        farm.until_state(name, "served")
    assert farm.ok(None, "service", "doctor", "--machine")["ok"] is True
    for name in ("alpha", "beta"):
        queued = farm.ok(name, "run", "probe")["run"]
        assert queued["project_id"] == farm.projects[name]["id"]

    for name in ("alpha", "beta", "gamma"):
        until(lambda: farm.finished(name, trigger="schedule"), 30, f"{name} scheduled run")
    for name in ("alpha", "beta"):
        manual = until(lambda: farm.finished(name, trigger="manual"), 30, f"{name} manual run")
        scheduled = farm.finished(name, trigger="schedule")
        for row in (*manual, *scheduled):
            assert row["project_id"] == farm.projects[name]["id"]
            assert row["host"] == socket.gethostname()
            said = farm.output(row)
            # The job ran as the project's own daemon runs it: in its folder,
            # with its .env, its config and its state root.
            assert said["secret"] == f"{name}-secret"
            assert Path(said["cwd"]).resolve() == farm.projects[name]["root"].resolve()
            assert Path(said["root"]).resolve() == farm.projects[name]["root"].resolve()
            assert Path(said["state_dir"]).resolve() == farm.state(name).resolve()
            assert said["config"].endswith("capabilities/automations/service/config.toml")
        status = farm.ok(name, "service", "status")
        assert status["mode"] == "machine" and status["running"] is True
        assert status["pid"] == machine_pid
        assert status["machine"]["state"] == "served"
    gamma_status = farm.ok("gamma", "service", "status")
    assert gamma_status["mode"] == "project" and gamma_status["pid"] == gamma["pid"] != machine_pid
    assert farm.output(farm.finished("gamma", trigger="schedule")[0])["secret"] == "gamma-secret"

    machine = farm.machine()
    assert machine["running"] is True and machine["pid"] == machine_pid
    assert sorted(row["project_id"] for row in machine["projects"]) == sorted(
        farm.projects[name]["id"] for name in ("alpha", "beta"))
    assert {row["state"] for row in machine["projects"]} == {"served"}

    slugs = {farm.projects[name]["slug"] for name in ("alpha", "beta")}
    lines = farm.ok(None, "service", "logs", "--machine", "--tail", "1000")["lines"]
    assert lines and all(LINE.match(line) for line in lines), lines
    assert {LINE.match(line).group(1) for line in lines} <= {"machine", *slugs}
    own = farm.ok("alpha", "service", "logs", "--tail", "1000")["lines"]
    assert own and {LINE.match(line).group(1) for line in own} == {farm.projects["alpha"]["slug"]}
    filtered = farm.ok(None, "service", "logs", "--machine", "--project",
                       farm.projects["beta"]["slug"])["lines"]
    assert filtered and all(f"[{farm.projects['beta']['slug']}]" in line for line in filtered)

    assert farm.ok(None, "service", "stop", "--machine")["stopped"] is True
    for name in ("alpha", "beta"):
        assert not (farm.state(name) / "daemon.pid").exists()
    assert farm.ok("gamma", "service", "status")["running"] is True


@needs_store
def test_a_project_that_does_not_load_is_paused_or_refused_leaves_the_others_served(farm):
    for name in ("alpha", "beta"):
        farm.project(name)
    farm.project("delta", config="version = 1\n[[automations]]\nid = \"Bad Id\"\n")
    for name in ("alpha", "beta", "delta"):
        assert farm.ok(name, "service", "join")["joined"] is True
    farm.ok(None, "service", "start", "--machine")
    delta = farm.until_state("delta", "error")
    assert "id must match" in delta["reason"]
    for name in ("alpha", "beta"):
        farm.until_state(name, "served")

    paused = farm.ok("alpha", "service", "pause", "--reason", "maintenance")
    assert paused["pause"]["reason"] == "maintenance"
    row = farm.until_state("alpha", "paused")
    assert "maintenance" in row["reason"]
    held = farm.ok("alpha", "run", "probe")["run"]
    seen_alpha = len(farm.runs("alpha"))
    seen_beta = len(farm.finished("beta", trigger="schedule"))
    time.sleep(4.5)
    assert len(farm.runs("alpha")) == seen_alpha
    assert any(r["id"] == held["id"] and r["status"] == "pending" for r in farm.runs("alpha"))
    assert len(farm.finished("beta", trigger="schedule")) > seen_beta
    assert farm.ok("alpha", "service", "resume")["resumed"] is True
    farm.until_state("alpha", "served")
    until(lambda: any(r["id"] == held["id"] and r["status"] == "succeeded"
                      for r in farm.runs("alpha")), 30, "the held run")

    # A project that stops enabling automations for itself is refused, alone.
    settings = farm.projects["beta"]["root"] / "capabilities" / "settings.json"
    settings.write_text(json.dumps({"capabilities": {"automations": {"enabled": False}}}))
    assert "project_enable_required" in farm.until_state("beta", "refused")["reason"]
    assert farm.entry("alpha")["state"] == "served"
    settings.write_text(json.dumps({"capabilities": {"automations": {"enabled": True}}}))
    farm.until_state("beta", "served")

    # Its own reload, in the project, hands the machine the repaired file.
    (farm.projects["delta"]["root"] / "capabilities" / "automations" / "service"
     / "config.toml").write_text(CONFIG)
    reloaded = farm.ok("delta", "service", "reload", "--timeout", "20")
    assert reloaded["reloaded"] is True
    farm.until_state("delta", "served")
    lines = farm.ok(None, "service", "logs", "--machine", "--tail", "1000")["lines"]
    assert all(LINE.match(line) for line in lines), lines


@needs_store
@pytest.mark.skipif(os.geteuid() == 0, reason="permissions do not bind root")
def test_a_project_whose_state_root_cannot_be_used_is_an_error_and_the_others_stay_served(farm):
    """One project whose state root cannot be made, and one whose state root
    takes its lock but not its pid, are each `error` with the reason; the
    others are served throughout, and each is served once its root is usable."""
    farm.project("alpha")
    sealed = farm.tmp / "sealed"
    sealed.mkdir()
    farm.project("delta", env_text=f"AUTOMATIONS_STATE_DIR={sealed / 'delta'}\n")
    shut = farm.tmp / "shut"
    shut.mkdir()
    (shut / "daemon.lock").touch()
    farm.project("omega", env_text=f"AUTOMATIONS_STATE_DIR={shut}\n")
    for name in ("alpha", "delta", "omega"):
        assert farm.ok(name, "service", "join")["joined"] is True
    sealed.chmod(0o555)
    shut.chmod(0o555)
    try:
        farm.ok(None, "service", "start", "--machine")
        for name in ("delta", "omega"):
            assert "cannot be used" in farm.until_state(name, "error")["reason"]
        farm.until_state("alpha", "served")
        queued = farm.ok("alpha", "run", "probe")["run"]
        until(lambda: any(r["id"] == queued["id"] and r["status"] == "succeeded"
                          for r in farm.runs("alpha")), 30, "alpha's manual run")
        assert farm.machine()["running"] is True
    finally:
        sealed.chmod(0o755)
        shut.chmod(0o755)
    for name in ("delta", "omega"):
        farm.until_state(name, "served")


@needs_store
def test_one_process_serves_a_project_at_a_time_in_both_directions(farm):
    farm.project("alpha", beat=False)
    own = farm.ok("alpha", "service", "start")
    joined = farm.ok("alpha", "service", "join")
    assert f"pid {own['pid']}" in joined["next"]
    machine_pid = farm.ok(None, "service", "start", "--machine")["pid"]
    refused = farm.until_state("alpha", "refused")
    assert f"a project-mode daemon, pid {own['pid']}, serves this project" in refused["reason"]
    code, error = farm.refused("alpha", "service", "start")
    assert (code, error["code"]) == (4, "joined_to_machine_service")
    code, error = farm.refused("alpha", "service", "run")
    assert (code, error["code"]) == (4, "joined_to_machine_service")

    # Its own daemon may still be stopped; the machine takes the project then.
    assert farm.ok("alpha", "service", "stop")["stopped"] is True
    farm.until_state("alpha", "served")
    assert int((farm.state("alpha") / "daemon.pid").read_text()) == machine_pid
    code, error = farm.refused("alpha", "service", "stop")
    assert (code, error["code"]) == (4, "joined_to_machine_service")
    assert farm.machine()["running"] is True

    # The lock itself keeps a project daemon out, however it is started.
    env = {**farm.env, "AUTOMATIONS_PROJECT_ROOT": str(farm.projects["alpha"]["root"]),
           "AUTOMATIONS_CONFIG": str(farm.projects["alpha"]["root"] / "capabilities"
                                     / "automations" / "service" / "config.toml"),
           "AUTOMATIONS_STATE_DIR": str(farm.state("alpha")),
           "CAPABILITIES_PROJECT_ID": farm.projects["alpha"]["id"],
           "CAPABILITIES_PROJECT_ID_ROOT": str(farm.projects["alpha"]["root"])}
    direct = subprocess.run([sys.executable, str(RUNTIME_PATH)], env=env, capture_output=True,
                            text=True, timeout=60)
    assert direct.returncode == 1
    assert "another automations daemon holds" in direct.stderr
    assert f"[{farm.projects['alpha']['slug']}]" in direct.stderr

    # Leaving hands it back: the machine lets go, and its own daemon starts.
    assert farm.ok("alpha", "service", "leave")["left"] is True
    until(lambda: not (farm.state("alpha") / "daemon.pid").exists(), 15, "the machine letting go")
    assert farm.machine()["projects"] == []
    again = farm.ok("alpha", "service", "start")
    assert again["running"] is True and again["pid"] != machine_pid
    assert farm.ok("alpha", "service", "status")["mode"] == "project"


@needs_store
def test_the_machine_cap_deals_starts_round_the_projects_in_turn(farm):
    farm.ok(None, "service", "init", "--machine")
    (farm.tmp / "config" / "automations" / "service" / "config.toml").write_text(
        "version = 1\nmax_parallel = 1\n")
    for name in ("alpha", "beta"):
        farm.project(name, beat=False)
        farm.ok(name, "service", "join")
        for _ in range(3):
            farm.ok(name, "run", "nap")
    status = farm.ok(None, "service", "start", "--machine")
    assert status["cap"]["max_parallel"] == 1
    for name in ("alpha", "beta"):
        until(lambda: len(farm.finished(name, trigger="manual")) == 3, 60, f"{name} naps")
    spans = []
    for name in ("alpha", "beta"):
        for row in farm.finished(name, trigger="manual"):
            said = Path(row["log_path"]).read_text().split()
            spans.append((float(said[said.index("start") + 1]),
                          float(said[said.index("end") + 1]), name))
    spans.sort()
    for earlier, later in zip(spans, spans[1:]):
        assert earlier[1] <= later[0], spans          # never two at once
        assert earlier[2] != later[2], spans          # dealt in turn


# --- Starting through launchd -------------------------------------------------
# launchd itself is stood in for by a `launchctl` on PATH that answers `print`
# from a file per job, records every call, and on `kickstart` writes what the
# machine process launchd starts would write: its pid and its status. No real
# job is asked anything.

FAKE_LAUNCHCTL = """#!/bin/sh
echo "$@" >> "$FAKE_LAUNCHD/calls"
case "$1" in
  print)
    label="${2##*/}"
    if [ -f "$FAKE_LAUNCHD/$label.print" ]; then cat "$FAKE_LAUNCHD/$label.print"; exit 0; fi
    echo "Could not find service \\"$label\\" in domain" >&2; exit 113 ;;
  kickstart)
    if [ -n "$FAKE_LAUNCHD_STATE" ]; then
      echo "$FAKE_LAUNCHD_PID" > "$FAKE_LAUNCHD_STATE/daemon.pid"
      echo "{\\"pid\\": $FAKE_LAUNCHD_PID}" > "$FAKE_LAUNCHD_STATE/daemon.json"
    fi
    exit 0 ;;
esac
"""
JOB = "test.supervisor.automations"


@pytest.fixture
def launchd(bare, monkeypatch):
    """A fake launchd that knows the job JOB, loaded and not running after a
    clean exit, a machine state root that recorded it, and the CLI loaded in
    process over the test's homes with a store that answers."""
    cli = load_cli("automations_cli_launchd_test")
    monkeypatch.setattr(cli, "_CONFIG_HOME", bare.tmp / "config")
    monkeypatch.setattr(cli, "_STATE_HOME", bare.tmp / "state")
    fake = bare.tmp / "launchd"
    fake.mkdir()
    script = fake / "launchctl"
    script.write_text(FAKE_LAUNCHCTL)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(fake), os.environ.get("PATH", "")]))
    monkeypatch.setenv("FAKE_LAUNCHD", str(fake))
    monkeypatch.setenv("FAKE_LAUNCHD_PID", str(os.getpid()))
    state = cli._machine_state_dir()
    state.mkdir(parents=True)
    monkeypatch.setenv("FAKE_LAUNCHD_STATE", str(state))
    (fake / f"{JOB}.print").write_text(
        f"gui/501/{JOB} = {{\n\tstate = not running\n\tlast exit code = 0\n}}\n")
    (state / "launchd.json").write_text(json.dumps({"label": JOB, "pid": 1}))
    runtime = load_runtime()

    class Link:
        def open(self):
            return self

        def close(self):
            pass

    monkeypatch.setattr(runtime, "StoreLink", Link)
    monkeypatch.setattr(runtime, "store_in_force", lambda: {"store": "test"})
    monkeypatch.setattr(cli, "_runtime_module", lambda: runtime)
    spawned = []
    real_popen = subprocess.Popen

    class HandCopy:
        """The detached copy `start --machine` spawns, standing in as itself."""

        def __init__(self, command):
            spawned.append(command)
            self.pid, self.returncode = os.getpid(), None
            (state / "daemon.pid").write_text(f"{self.pid}\n")
            (state / "daemon.json").write_text(json.dumps({"pid": self.pid}))

        def poll(self):
            return None

    def popen(command, *args, **kwargs):
        if isinstance(command, list) and command[-3:] == ["service", "run", "--machine"]:
            return HandCopy(command)
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return {"cli": cli, "fake": fake, "state": state, "spawned": spawned, "bare": bare}


def launchctl_calls(launchd) -> list[str]:
    calls = launchd["fake"] / "calls"
    return calls.read_text().splitlines() if calls.exists() else []


def test_start_hands_the_start_to_the_loaded_launchd_job_and_spawns_nothing(launchd):
    answer = launchd["cli"].cmd_service_start_machine()
    assert (answer["started"], answer["started_by"], answer["label"]) == (True, "launchd", JOB)
    assert (answer["running"], answer["pid"]) == (True, os.getpid())
    assert f"kickstart gui/{os.getuid()}/{JOB}" in launchctl_calls(launchd)
    assert launchd["spawned"] == []


def test_detached_starts_a_copy_by_hand_while_the_job_is_loaded(launchd):
    answer = launchd["cli"].cmd_service_start_machine(detached=True)
    assert answer["started"] is True and "started_by" not in answer
    assert len(launchd["spawned"]) == 1
    assert not [call for call in launchctl_calls(launchd) if call.startswith("kickstart")]


@pytest.mark.parametrize("where", ["no record", "job not loaded", "no launchctl"])
def test_with_no_loaded_job_start_starts_a_detached_copy_as_before(launchd, monkeypatch, where):
    if where == "no record":
        (launchd["state"] / "launchd.json").unlink()
    elif where == "job not loaded":
        (launchd["fake"] / f"{JOB}.print").unlink()
    else:
        monkeypatch.setenv("PATH", str(launchd["fake"] / "empty"))
    answer = launchd["cli"].cmd_service_start_machine()
    assert answer["started"] is True and "started_by" not in answer
    assert len(launchd["spawned"]) == 1
    assert not [call for call in launchctl_calls(launchd) if call.startswith("kickstart")]


def test_status_and_doctor_name_the_supervisor_and_how_to_start(launchd):
    cli = launchd["cli"]
    status = cli._machine_status(cli._runtime_module())
    assert status["running"] is False
    assert status["supervisor"] == {"launchd": JOB, "pid": None, "last_exit_code": "0"}
    assert f"launchctl kickstart gui/$UID/{JOB}" in status["hint"]
    assert "service start --machine" in status["hint"]
    doctor = cli.cmd_service_doctor_machine()
    assert doctor["ok"] is False and doctor["machine"]["supervisor"]["launchd"] == JOB
    assert any(status["hint"] in problem for problem in doctor["problems"])
    (launchd["state"] / "launchd.json").unlink()
    bare = cli._machine_status(cli._runtime_module())
    assert bare["supervisor"] is None
    assert bare["hint"] == ("`automations service start --machine`, or `automations service "
                            "run --machine` under a supervisor")
    cli.cmd_service_start_machine(detached=True)
    assert "hint" not in cli._machine_status(cli._runtime_module())


def test_the_machine_process_records_the_job_launchd_started_it_as(launchd, monkeypatch):
    cli = launchd["cli"]
    record = launchd["state"] / "launchd.json"
    record.unlink()
    print_file = launchd["fake"] / f"{JOB}.print"
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    cli._record_launchd_supervisor(launchd["state"])
    assert not record.exists() and launchctl_calls(launchd) == []
    monkeypatch.setenv("XPC_SERVICE_NAME", JOB)
    print_file.write_text(f"gui/501/{JOB} = {{\n\tpid = 999999\n}}\n")
    cli._record_launchd_supervisor(launchd["state"])
    assert not record.exists()
    print_file.write_text(f"gui/501/{JOB} = {{\n\tpid = {os.getppid()}\n}}\n")
    cli._record_launchd_supervisor(launchd["state"])
    written = json.loads(record.read_text())
    assert (written["label"], written["pid"]) == (JOB, os.getppid())


def test_detached_without_machine_is_refused(bare):
    bare.project("plain")
    code, error = bare.refused("plain", "service", "start", "--detached")
    assert (code, error["code"]) == (6, "input")
