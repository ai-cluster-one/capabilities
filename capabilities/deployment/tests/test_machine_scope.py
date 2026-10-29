"""The machine scope: handing an older deployment's machine agents over.

A capability whose manifest declares `service.machine` runs one process for
every agent placed on the node to share it, under the capabilities manager's
node manager. An older deployment compiled a launchd agent per such service,
and a watchdog over them, into this machine's state home; `deployment machine
handover` takes them down. These run the real manager against a scratch HOME,
with fixture capabilities built from the manager's own scaffold and installed
by it.
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


@pytest.fixture
def scratch(machine):
    base, env = machine
    _clear(base)
    _settings(base, {"services": {SWITCHED_OFF: {"disabled": True}}})
    yield base, env
    _clear(base)
    _settings(base, None)


def test_the_machine_verbs_pass_the_ordinary_gate(scratch):
    base, env = scratch
    gate = base / "config" / "capabilities" / "settings.json"
    gate.write_text(json.dumps({"capabilities": {"deployment": {"enabled": False}}}))
    try:
        for verb in ("handover",):
            result = _deployment(env, base / "home", "machine", verb)
            assert result.returncode == 4, (verb, result.stdout, result.stderr)
    finally:
        gate.write_text(json.dumps({"capabilities": {"deployment": {"enabled": True}}}))


def test_the_read_only_switch_stops_the_writing_verbs(scratch):
    base, env = scratch
    read_only = {**env, "CAPABILITIES_READ_ONLY": "1"}
    assert _deployment(read_only, base / "home", "machine", "handover").returncode == 4
    assert not _agents_dir(base).is_dir() or not any(_agents_dir(base).iterdir())



def test_handover_takes_the_machine_agents_down_and_the_watchdog_with_the_last(
        tmp_path, monkeypatch, capsys):
    module = _load(DEPLOYMENT, "deployment_machine_handover_under_test")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    agents_dir = module._machine_agents_dir()
    agents_dir.mkdir(parents=True)
    library = tmp_path / "home" / "Library" / "LaunchAgents"
    library.mkdir(parents=True)
    labels = ["capabilities.machine.mfix", "capabilities.machine.other",
              "capabilities.machine.watchdog"]
    for label in labels:
        (agents_dir / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label}))
        (library / f"{label}.plist").symlink_to(agents_dir / f"{label}.plist")
    loaded = set(labels)

    def launchctl(*argv):
        label = argv[1].rsplit("/", 1)[-1]
        if argv[0] == "print":
            return (0, "") if label in loaded else (113, "not found")
        if argv[0] == "bootout":
            loaded.discard(label)
        return 0, ""

    monkeypatch.setattr(module, "_launchctl", launchctl)

    def handover(*names, dry_run=False):
        module.cmd_machine_handover(type("Args", (), {"names": list(names),
                                                      "dry_run": dry_run})())
        report = json.loads(capsys.readouterr().out)
        return [(a["label"], a["kind"], a["action"]) for a in report["agents"]]

    # Named, only that one goes; the watchdog stays while another agent runs.
    assert handover("mfix", dry_run=True) == [
        ("capabilities.machine.mfix", "service", "would_hand_over")]
    assert loaded == set(labels)
    assert handover("mfix") == [("capabilities.machine.mfix", "service", "handed_over")]
    assert loaded == {"capabilities.machine.other", "capabilities.machine.watchdog"}
    assert not (library / "capabilities.machine.mfix.plist").exists()
    assert (agents_dir / "capabilities.machine.mfix.plist").is_file()

    # Unnamed, every compiled one goes, and the watchdog with the last.
    assert handover() == [
        ("capabilities.machine.mfix", "service", "absent"),
        ("capabilities.machine.other", "service", "handed_over"),
        ("capabilities.machine.watchdog", "watchdog", "handed_over")]
    assert loaded == set() and not any(library.iterdir())
    assert [action for _l, _k, action in handover()] == ["absent"] * 3
