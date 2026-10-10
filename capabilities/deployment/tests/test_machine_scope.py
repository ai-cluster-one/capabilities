"""The machine scope: supervising the services that run once per machine.

A capability whose manifest declares `service.machine` runs one process for
every project that opted in to it, so its launchd agent belongs to the machine
rather than to any project. `deployment machine sync` compiles one agent per
such service, and a watchdog over them, from what the manager says is
installed and allowed here and from this machine's own settings, into this
machine's state home. These run the real manager against a scratch HOME, with
fixture capabilities built from the manager's own scaffold and installed by it.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest


CAP_ROOT = Path(__file__).resolve().parents[2]
REPO = CAP_ROOT.parent
MANAGER = REPO / "bin" / "capabilities"
DEPLOYMENT = next((path for path in (
    CAP_ROOT / "deployment" / "bin" / "deployment", CAP_ROOT / "deployment" / "deployment")
    if path.is_file()), CAP_ROOT / "deployment" / "bin" / "deployment")

SERVED = "mfix"        # declares a machine service, allowed
QUARANTINED = "mquar"  # declares one, never allowed here
PLAIN = "mplain"       # declares none
SWITCHED_OFF = "moff"  # declares one, allowed, disabled in deployment's machine.json
STATE_TEMPLATE = "$XDG_STATE_HOME/capabilities/machine/{name}"


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _machine(name: str) -> dict:
    return {
        "schema": "capabilities.service.machine.v1",
        "command": [name, "service", "run", "--machine"],
        "doctor": [name, "service", "doctor", "--machine"],
        "projects": f"$XDG_CONFIG_HOME/{name}/service/projects.toml",
        "state": STATE_TEMPLATE.format(name=name),
    }


def _fixture_script(manager, name: str, machine: bool) -> str:
    """The manager's core-only scaffold, declaring a service whose machine
    process sleeps and whose doctor answers ok."""
    text = manager._capability_skeleton(name, True).replace(
        "TODO: describe the capability's smallest useful surface.",
        "Fixture capability for the machine scope.")
    text = text.replace("Replace this scaffold check with a real readiness proof.",
                        "Fixture readiness.")
    text = re.sub(r"(?m)^INVENTORY = None.*$", 'INVENTORY = {"network": "none"}', text, count=1)
    text = text.replace(
        "\ndef main() -> None:\n",
        "\ndef _inventory(_network: bool) -> dict:\n"
        "    return {\"service\": {\"state\": \"stopped\"}}\n\n\n"
        "def main() -> None:\n", 1)
    if not machine:
        return text
    service = {"name": name, "summary": "Fixture service.",
               "verbs": ["run", "start", "stop", "status", "doctor", "join", "leave"],
               "machine": _machine(name)}
    text = text.replace("POST_INSTALL: list[dict] = []\n",
                        f"POST_INSTALL: list[dict] = []\nSERVICE = {service!r}\n", 1)
    dispatch = (
        "    if argv[:1] == [\"manifest\"]:\n"
        "        _emit({\"name\": NAME, \"summary\": SUMMARY,\n"
        "               \"credentials\": {\"scope\": SCOPE, \"keys\": CRED_KEYS},\n"
        "               \"docs\": {\"base\": DOCS_BASE, \"topics\": []},\n"
        "               \"state\": STATE, \"inventory\": _inventory_declaration(),\n"
        "               \"post_install\": POST_INSTALL, \"service\": SERVICE})\n"
        "        sys.exit(0)\n"
        "    if argv[:2] == [\"service\", \"run\"]:\n"
        "        import time\n"
        "        while True:\n"
        "            time.sleep(60)\n"
        "    if argv[:2] == [\"service\", \"doctor\"]:\n"
        "        _emit({\"ok\": True})\n"
        "        return\n"
        "    _contract(argv)\n")
    assert "    _contract(argv)\n" in text
    return text.replace("    _contract(argv)\n", dispatch, 1)


def _bundle(manager, root: Path, name: str, machine: bool) -> Path:
    bundle = root / name
    (bundle / "bin").mkdir(parents=True)
    if machine:
        (bundle / "service").mkdir()
        (bundle / "service" / "README.md").write_text("fixture service files\n")
    script = bundle / "bin" / name
    script.write_text(_fixture_script(manager, name, machine))
    script.chmod(0o755)
    return bundle


def _env(base: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("CAPABILITIES_READ_ONLY", "CLAUDE_PROJECT_DIR", "CAPABILITIES_AUTH_CONTEXT",
                "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
                "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
                "CAPABILITIES_MANAGER_BIN",
                "CAPABILITIES_DEV_SESSION", "CAPABILITIES_WORKSPACE"):
        env.pop(key, None)
    for key in [key for key in env if key.startswith("AGENTKIT_DB_")]:
        env.pop(key)
    env.update({
        "HOME": str(base / "home"),
        "XDG_CONFIG_HOME": str(base / "config"),
        "XDG_STATE_HOME": str(base / "state"),
        "XDG_CACHE_HOME": str(base / "cache"),
        "XDG_DATA_HOME": str(base / "data"),
        "CAPABILITIES_HOME": str(base / "registry"),
        "CAPABILITIES_BIN": str(base / "bin"),
        "PATH": os.pathsep.join([str(base / "bin"), env.get("PATH", "")]),
    })
    (base / "home").mkdir(parents=True, exist_ok=True)
    (base / "bin").mkdir(parents=True, exist_ok=True)
    return env


def _run(argv: list[str], env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True,
                          timeout=180)


@pytest.fixture(scope="module")
def machine(tmp_path_factory):
    """A scratch machine: four fixture capabilities installed by the manager,
    deployment enabled globally, and no deployment settings yet."""
    base = tmp_path_factory.mktemp("machine")
    env = _env(base)
    manager = _load(MANAGER, "capabilities_manager_for_machine_scope")
    sources = base / "sources"
    for name, declares, allow in ((SERVED, True, True), (QUARANTINED, True, False),
                                  (PLAIN, False, True), (SWITCHED_OFF, True, True)):
        bundle = _bundle(manager, sources, name, declares)
        args = [str(MANAGER), "install", name, "--from", str(bundle)]
        result = _run(args + (["--allow"] if allow else []), env, base / "home")
        assert result.returncode == 0, result.stdout + result.stderr
    listed = json.loads(_run([str(MANAGER), "list", "--json"], env, base / "home").stdout)
    states = {row["name"]: row["machine"] for row in listed["installed"]}
    assert states == {SERVED: "allowed", QUARANTINED: "quarantined",
                      PLAIN: "allowed", SWITCHED_OFF: "allowed"}
    gate = base / "config" / "capabilities" / "settings.json"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text(json.dumps({"capabilities": {"deployment": {"enabled": True}}}))
    return base, env


def _settings(base: Path, value: dict | None) -> Path:
    path = base / "config" / "deployment" / "machine.json"
    if value is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    return path


def _agents_dir(base: Path) -> Path:
    return base / "state" / "deployment" / "machine" / "launchd"


def _clear(base: Path) -> None:
    directory = _agents_dir(base)
    if directory.is_dir():
        for path in directory.iterdir():
            path.unlink()


def _deployment(env: dict, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return _run([str(DEPLOYMENT), *args], env, cwd)


def _sync(base: Path, env: dict, cwd: Path | None = None) -> dict:
    result = _deployment(env, cwd or base / "home", "machine", "sync")
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def scratch(machine):
    base, env = machine
    _clear(base)
    _settings(base, {"services": {SWITCHED_OFF: {"disabled": True}}})
    yield base, env
    _clear(base)
    _settings(base, None)


def test_a_declared_allowed_service_gets_a_launcher_and_an_agent(scratch):
    base, env = scratch
    result = _sync(base, env)
    assert result["ok"], result["findings"]
    agents_dir = _agents_dir(base)
    assert result["agents_dir"] == str(agents_dir)
    assert result["services"] == [SERVED]
    assert result["agents"] == ["capabilities.machine.mfix", "capabilities.machine.watchdog"]

    launcher = agents_dir / "capabilities-machine-mfix"
    assert os.access(launcher, os.X_OK)
    body = launcher.read_text()
    assert body.startswith("#!/bin/sh\n")
    assert body.rstrip().endswith(f'exec {base / "bin" / SERVED} service run --machine "$@"')

    plist_path = agents_dir / "capabilities.machine.mfix.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == "capabilities.machine.mfix"
    assert plist["ProgramArguments"] == [str(launcher)]
    assert plist["WorkingDirectory"] == str(base / "state" / "capabilities" / "machine" / SERVED)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["AbandonProcessGroup"] is True
    # The turns a machine service starts build and test; Background would hold
    # them to the efficiency cores at the lowest priority.
    assert plist["ProcessType"] == "Standard"
    path_entries = plist["EnvironmentVariables"]["PATH"].split(os.pathsep)
    assert str((base / "bin").resolve()) == path_entries[0]
    assert plist["StandardOutPath"] == str(agents_dir / "mfix.out.log")

    for path in agents_dir.glob("*.plist"):
        lint = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True)
        assert lint.returncode == 0, lint.stdout + lint.stderr


def test_the_watchdog_agent_is_scheduled_and_runs_the_machine_pass(scratch):
    base, env = scratch
    _sync(base, env)
    agents_dir = _agents_dir(base)
    plist = plistlib.loads((agents_dir / "capabilities.machine.watchdog.plist").read_bytes())
    assert plist["Label"] == "capabilities.machine.watchdog"
    assert plist["RunAtLoad"] is False
    assert "KeepAlive" not in plist
    assert plist["StartInterval"] == 60
    assert plist["ProcessType"] == "Background"
    launcher = Path(plist["ProgramArguments"][0])
    assert launcher == agents_dir / "capabilities-machine-watchdog"
    assert launcher.read_text().rstrip().endswith('machine watchdog "$@"')


def test_what_is_not_supervised_is_named_with_its_reason(scratch):
    base, env = scratch
    result = _sync(base, env)
    reasons = {entry["capability"]: entry["reason"] for entry in result["skipped"]}
    assert "quarantined" in reasons[QUARANTINED]
    assert reasons[PLAIN] == "declares no machine service"
    assert reasons[SWITCHED_OFF] == f"disabled in {base / 'config' / 'deployment' / 'machine.json'}"
    names = {path.name for path in _agents_dir(base).iterdir()}
    for name in (QUARANTINED, PLAIN, SWITCHED_OFF):
        assert not any(name in entry for entry in names), names


def test_an_agent_left_for_a_service_no_longer_supervised_is_reported(scratch):
    base, env = scratch
    _settings(base, None)
    _sync(base, env)
    _settings(base, {"services": {SWITCHED_OFF: {"disabled": True}}})
    result = _sync(base, env)
    stale = [f for f in result["findings"]
             if f["path"].endswith("capabilities.machine.moff.plist")]
    assert stale and "bootout" in stale[0]["message"]


def test_a_service_with_no_joined_project_is_still_compiled(scratch):
    base, env = scratch
    assert not (base / "config" / SERVED / "service" / "projects.toml").exists()
    assert SERVED in _sync(base, env)["services"]


def _tree(directory: Path) -> dict[str, str]:
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*")) if path.is_file()}


def test_the_same_files_land_in_the_same_place_from_anywhere(scratch):
    base, env = scratch
    project = base / "a-project"
    (project / ".git").mkdir(parents=True, exist_ok=True)
    (project / "capabilities").mkdir(exist_ok=True)
    (project / "capabilities" / "settings.json").write_text(
        json.dumps({"capabilities": {"deployment": {"enabled": True}}}))
    elsewhere = base / "elsewhere"
    elsewhere.mkdir(exist_ok=True)

    first = _sync(base, env, cwd=project)
    tree = _tree(_agents_dir(base))
    _clear(base)
    second = _sync(base, env, cwd=elsewhere)
    assert first["agents_dir"] == second["agents_dir"] == str(_agents_dir(base))
    assert _tree(_agents_dir(base)) == tree
    assert not (project / "deployment").exists() and not (elsewhere / "deployment").exists()


def test_settings_come_from_the_machine_config_home_and_default_when_absent(scratch):
    base, env = scratch
    path = _settings(base, {"watchdog": {"interval_seconds": 120},
                            "services": {SWITCHED_OFF: {"disabled": True}}})
    result = _sync(base, env)
    assert result["settings"] == {"path": str(path), "present": True}
    watchdog = _agents_dir(base) / "capabilities.machine.watchdog.plist"
    assert plistlib.loads(watchdog.read_bytes())["StartInterval"] == 120

    _settings(base, None)
    result = _sync(base, env)
    assert result["settings"] == {"path": str(path), "present": False}
    assert plistlib.loads(watchdog.read_bytes())["StartInterval"] == 60
    assert sorted(result["services"]) == [SERVED, SWITCHED_OFF]


def test_unreadable_settings_are_refused_rather_than_guessed(scratch):
    base, env = scratch
    path = _settings(base, {})
    path.write_text("{not json")
    result = _deployment(env, base / "home", "machine", "sync")
    assert result.returncode == 6
    assert "bad_machine_settings" in result.stderr


def test_next_hands_over_launchctl_and_changes_nothing(scratch):
    base, env = scratch
    _sync(base, env)
    before = _tree(base / "state"), _tree(base / "home")
    result = _deployment(env, base / "home", "machine", "next", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (_tree(base / "state"), _tree(base / "home")) == before
    library = base / "home" / "Library" / "LaunchAgents"
    plist = _agents_dir(base) / "capabilities.machine.mfix.plist"
    steps = "\n".join(payload["steps"])
    assert f"ln -sf {plist} {library / 'capabilities.machine.mfix.plist'}" in steps
    assert (f"launchctl bootstrap gui/$UID {library / 'capabilities.machine.mfix.plist'}"
            in steps)
    assert "capabilities.machine.watchdog.plist" in steps
    assert f"mkdir -p {base / 'state' / 'capabilities' / 'machine' / SERVED}" in steps
    text = _deployment(env, base / "home", "machine", "next")
    assert text.returncode == 0, text.stderr
    assert text.stdout.startswith("# Machine Agents Next Steps")
    assert f"launchctl bootstrap gui/$UID {library / 'capabilities.machine.mfix.plist'}" in text.stdout


def test_status_reports_each_compiled_agent_without_writing(scratch):
    base, env = scratch
    _sync(base, env)
    library = base / "home" / "Library" / "LaunchAgents"
    library.mkdir(parents=True, exist_ok=True)
    plist = _agents_dir(base) / "capabilities.machine.mfix.plist"
    (library / plist.name).symlink_to(plist)
    try:
        before = _tree(base / "state"), _tree(base / "home")
        result = _deployment(env, base / "home", "machine", "status")
        assert result.returncode == 0, result.stderr
        assert (_tree(base / "state"), _tree(base / "home")) == before
        agents = {agent["label"]: agent for agent in json.loads(result.stdout)["agents"]}
        assert sorted(agents) == ["capabilities.machine.mfix", "capabilities.machine.watchdog"]
        assert agents["capabilities.machine.mfix"]["compiled"] is True
        assert agents["capabilities.machine.mfix"]["linked"] is True
        assert agents["capabilities.machine.watchdog"]["linked"] is False
        assert all(agent["loaded"] is False for agent in agents.values())
        assert agents["capabilities.machine.mfix"]["process_type"] == "Standard"
        assert agents["capabilities.machine.watchdog"]["process_type"] == "Background"
        assert all(agent["spawn_type"] is None and agent["spawn_type_current"] is None
                   for agent in agents.values())
    finally:
        (library / plist.name).unlink()


def test_the_machine_verbs_pass_the_ordinary_gate(scratch):
    base, env = scratch
    gate = base / "config" / "capabilities" / "settings.json"
    gate.write_text(json.dumps({"capabilities": {"deployment": {"enabled": False}}}))
    try:
        for verb in ("sync", "next", "status", "watchdog"):
            result = _deployment(env, base / "home", "machine", verb)
            assert result.returncode == 4, (verb, result.stdout, result.stderr)
    finally:
        gate.write_text(json.dumps({"capabilities": {"deployment": {"enabled": True}}}))


def test_the_read_only_switch_stops_the_writing_verbs(scratch):
    base, env = scratch
    read_only = {**env, "CAPABILITIES_READ_ONLY": "1"}
    for verb in ("sync", "watchdog"):
        assert _deployment(read_only, base / "home", "machine", verb).returncode == 4
    assert not _agents_dir(base).is_dir() or not any(_agents_dir(base).iterdir())
    assert _deployment(read_only, base / "home", "machine", "status").returncode == 0


@pytest.mark.parametrize("spawn, current", [("daemon (3)", True), ("background (2)", False)])
def test_status_says_whether_launchd_runs_the_compiled_process_type(
        tmp_path, monkeypatch, capsys, spawn, current):
    """A loaded agent keeps the class it was bootstrapped with until it is
    handed to launchd again, so status compares the two."""
    module = _load(DEPLOYMENT, "deployment_machine_status_under_test")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(module.sys, "platform", "darwin")
    agents_dir = module._machine_agents_dir()
    agents_dir.mkdir(parents=True)
    (agents_dir / "capabilities.machine.mfix.plist").write_bytes(plistlib.dumps(
        {"Label": "capabilities.machine.mfix", "ProcessType": "Standard"}))
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    launchctl = stubs / "launchctl"
    launchctl.write_text(f"#!/bin/sh\nprintf '\\tpid = 7\\n\\tspawn type = {spawn}\\n'\n")
    launchctl.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(stubs), os.environ.get("PATH", "")]))
    module.cmd_machine_status(None)
    agent = json.loads(capsys.readouterr().out)["agents"][0]
    assert agent["loaded"] is True
    assert agent["process_type"] == "Standard"
    assert agent["spawn_type"] == spawn.split(" ")[0]
    assert agent["spawn_type_current"] is current


# --- one watchdog pass, in process, with launchctl recorded -------------------

@pytest.fixture
def machine_watchdog(tmp_path, monkeypatch):
    """The machine pass against stub doctors, with launchd's answers faked and
    `launchctl` on PATH replaced by a recorder, so nothing is really restarted."""
    module = _load(DEPLOYMENT, "deployment_machine_watchdog_under_test")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (tmp_path / "home").mkdir()
    calls = stubs / "launchctl.calls"
    for name, body in (("sick-doctor", "echo '{\"ok\": false}'; exit 1"),
                       ("well-doctor", "echo '{\"ok\": true}'"),
                       ("launchctl", f'echo "$@" >> {calls}')):
        script = stubs / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(stubs), os.environ.get("PATH", "")]))
    services = {
        "sick": {"command": ["sick", "service", "run", "--machine"],
                 "doctor": [str(stubs / "sick-doctor")], "working_directory": tmp_path},
        "well": {"command": ["well", "service", "run", "--machine"],
                 "doctor": [str(stubs / "well-doctor")], "working_directory": tmp_path},
    }
    monkeypatch.setattr(module, "_machine_services",
                        lambda: (services, [], module._machine_settings(), {}))
    monkeypatch.setattr(module, "_launchd_status",
                        lambda label: {"label": label, "loaded": True, "pid": "123"})
    settings = tmp_path / "config" / "deployment" / "machine.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"watchdog": {"failures_before_restart": 3,
                                                 "restart_cooldown_seconds": 600}}))
    module._test_calls = calls
    return module


def _actions(report: dict) -> dict[str, str]:
    return {entry["service"]: entry["action"] for entry in report["services"]}


def _kicks(module) -> list[str]:
    return (module._test_calls.read_text().splitlines()
            if module._test_calls.exists() else [])


def test_a_failing_machine_service_is_kickstarted_after_the_configured_count(machine_watchdog):
    module = machine_watchdog
    uid = os.getuid()
    assert _actions(module.cmd_machine_watchdog(False, now=1000.0)) == {
        "sick": "watching", "well": "none"}
    assert _actions(module.cmd_machine_watchdog(False, now=1060.0))["sick"] == "watching"
    assert _kicks(module) == []
    assert _actions(module.cmd_machine_watchdog(False, now=1120.0)) == {
        "sick": "restarted", "well": "none"}
    assert _kicks(module) == [f"kickstart -k gui/{uid}/capabilities.machine.sick"]
    state = json.loads((Path(os.environ["XDG_STATE_HOME"]) / "deployment" / "machine"
                        / "launchd" / ".watchdog-state.json").read_text())
    assert state == {"sick": {"failures": 0, "last_restart": 1120.0}}


def test_the_cooldown_holds_a_second_restart_back(machine_watchdog):
    module = machine_watchdog
    for moment in (1000.0, 1060.0, 1120.0):
        module.cmd_machine_watchdog(False, now=moment)
    actions = [_actions(module.cmd_machine_watchdog(False, now=moment))["sick"]
               for moment in (1180.0, 1240.0, 1300.0, 1360.0)]
    assert actions == ["watching", "watching", "cooling down", "cooling down"]
    assert len(_kicks(module)) == 1
    assert _actions(module.cmd_machine_watchdog(False, now=1720.0))["sick"] == "restarted"
    assert len(_kicks(module)) == 2


def test_a_dry_run_and_a_disabled_watchdog_restart_nothing(machine_watchdog):
    module = machine_watchdog
    for moment in (1000.0, 1060.0, 1120.0, 1180.0):
        module.cmd_machine_watchdog(True, now=moment)
    assert _kicks(module) == []
    settings = Path(os.environ["XDG_CONFIG_HOME"]) / "deployment" / "machine.json"
    settings.write_text(json.dumps({"watchdog": {"enabled": False}}))
    report = module.cmd_machine_watchdog(False, now=2000.0)
    assert report["services"] == [] and "disabled" in report["skipped"]
    assert _kicks(module) == []


def test_a_declared_state_expands_against_the_machine_homes(machine_watchdog):
    module = machine_watchdog
    assert module._expand_machine_path("$XDG_STATE_HOME/capabilities/machine/x") == (
        Path(os.environ["XDG_STATE_HOME"]) / "capabilities" / "machine" / "x")
    assert module._expand_machine_path("~/x") == Path(os.environ["HOME"]) / "x"
    assert module._expand_machine_path("$UNKNOWN/x") is None
    assert module._expand_machine_path("relative/x") is None


def _agent_without_pid(module, monkeypatch, last_exit: str | None) -> None:
    monkeypatch.setattr(module, "_launchd_status", lambda label: {
        "label": label, "loaded": True, "pid": None, "last_exit_status": last_exit})


def test_a_machine_agent_that_exited_cleanly_is_reported_stopped_not_restarted(
        machine_watchdog, monkeypatch):
    """KeepAlive restarts a machine agent only after a failure, so one with no
    pid after a clean exit is stopped, and stays left alone pass after pass."""
    module = machine_watchdog
    _agent_without_pid(module, monkeypatch, "0")
    for moment in (1000.0, 1060.0, 1120.0, 1180.0):
        report = module.cmd_machine_watchdog(False, now=moment)
    entries = {entry["service"]: entry for entry in report["services"]}
    for name in ("sick", "well"):
        assert entries[name]["action"] == "none"
        assert entries[name]["state"] == "stopped"
        assert entries[name]["last_exit_status"] == "0"
        assert "restarting" not in entries[name]["detail"]
        assert f"launchctl kickstart gui/$UID/capabilities.machine.{name}" in (
            entries[name]["detail"])
    assert _kicks(module) == []


def test_a_copy_running_outside_launchd_is_reported_with_its_pid_and_left_alone(
        machine_watchdog, monkeypatch, tmp_path):
    module = machine_watchdog
    _agent_without_pid(module, monkeypatch, "0")
    outside = tmp_path / "stubs" / "outside-doctor"
    outside.write_text("#!/bin/sh\necho '{\"ok\": true, \"mode\": \"machine\", "
                       "\"machine\": {\"running\": true, \"pid\": 4242}}'\n")
    outside.chmod(0o755)
    services = module._machine_services()[0]
    services["well"]["doctor"] = [str(outside)]
    for dry_run in (True, False):
        entries = {entry["service"]: entry
                   for entry in module.cmd_machine_watchdog(dry_run, now=1000.0)["services"]}
        well = entries["well"]
        assert (well["action"], well["state"], well["outside_pid"]) == (
            "none", "outside_launchd", 4242)
        assert "pid 4242" in well["detail"] and "restarting" not in well["detail"]
        assert "well service stop --machine" in well["detail"]
        assert "launchctl kickstart gui/$UID/capabilities.machine.well" in well["detail"]
        assert entries["sick"]["state"] == "stopped"
    assert _kicks(module) == []


def test_a_machine_agent_that_failed_is_still_left_to_launchd_to_restart(
        machine_watchdog, monkeypatch):
    module = machine_watchdog
    _agent_without_pid(module, monkeypatch, "1")
    entries = {entry["service"]: entry
               for entry in module.cmd_machine_watchdog(False, now=1000.0)["services"]}
    assert {name: (entry["action"], entry["state"]) for name, entry in entries.items()} == {
        "sick": ("none", "restarting"), "well": ("none", "restarting")}
    assert entries["sick"]["detail"] == "no pid; launchd is already restarting it"
    assert _kicks(module) == []
