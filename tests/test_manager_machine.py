"""This machine's ceiling over the policy gate, and the machine-service call.

Each installed capability is `allowed` or `quarantined` on the machine, in a
file only the manager writes. Quarantined is a ceiling above both scopes:
effective-disabled in every project whatever project or global policy says.
A service that declares `service.machine` takes `--machine` on its service
verbs, and such a call resolves no project and reads no policy row.

Everything runs against a scratch HOME and a fixture capability built from
the manager's own scaffold, so the gate under test is the stamped preamble.
"""

from __future__ import annotations

import copy
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
NAME = "machfix"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_machine_under_test", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()

MACHINE = {
    "schema": "capabilities.service.machine.v1",
    "command": [NAME, "service", "run", "--machine"],
    "doctor": [NAME, "service", "doctor", "--machine"],
    "projects": f"$XDG_CONFIG_HOME/{NAME}/service/projects.toml",
    "config": f"$XDG_CONFIG_HOME/{NAME}/service/config.toml",
    "state": f"$XDG_STATE_HOME/capabilities/machine/{NAME}",
}
VERBS = ["init", "run", "start", "stop", "reload", "status", "doctor", "join", "leave"]


def _service(machine: object = MACHINE, verbs: list[str] = VERBS) -> dict:
    service = {"name": "fixture", "summary": "Fixture service.", "verbs": list(verbs)}
    if machine is not None:
        service["machine"] = copy.deepcopy(machine)
    return service


def _script(service: dict | None) -> str:
    """The manager's own core-only scaffold, declaring `service` and echoing
    every service verb it is let through to, so the gate is all that decides."""
    text = M._capability_skeleton(NAME, True).replace(
        "TODO: describe the capability's smallest useful surface.",
        "Fixture capability for the machine ceiling.")
    text = text.replace(
        "Replace this scaffold check with a real readiness proof.",
        "Fixture readiness.")
    text = re.sub(r"(?m)^INVENTORY = None.*$", 'INVENTORY = {"network": "none"}', text, count=1)
    text = text.replace(
        "\ndef main() -> None:\n",
        "\ndef _inventory(_network: bool) -> dict:\n"
        "    return {\"service\": {\"state\": \"stopped\"}}\n\n\n"
        "def main() -> None:\n", 1)
    text = text.replace(
        "POST_INSTALL: list[dict] = []\n",
        "POST_INSTALL: list[dict] = []\n"
        + (f"SERVICE = {service!r}\n" if service is not None else ""), 1)
    dispatch = (
        "    if argv[:1] == [\"manifest\"] and \"SERVICE\" in globals():\n"
        "        _emit({\"name\": NAME, \"summary\": SUMMARY,\n"
        "               \"credentials\": {\"scope\": SCOPE, \"keys\": CRED_KEYS},\n"
        "               \"docs\": {\"base\": DOCS_BASE, \"topics\": []},\n"
        "               \"state\": STATE, \"inventory\": _inventory_declaration(),\n"
        "               \"post_install\": POST_INSTALL, \"service\": SERVICE})\n"
        "        sys.exit(0)\n"
        "    if argv[:1] == [\"service\"]:\n"
        "        _emit({\"service\": argv[1:]})\n"
        "        return\n"
        "    _contract(argv)\n")
    assert "    _contract(argv)\n" in text
    return text.replace("    _contract(argv)\n", dispatch, 1)


def _bundle(root: Path, service: dict | None) -> Path:
    bundle = root / NAME
    (bundle / "bin").mkdir(parents=True)
    (bundle / "service").mkdir()
    (bundle / "service" / "README.md").write_text("fixture service files\n")
    script = bundle / "bin" / NAME
    script.write_text(_script(service))
    script.chmod(0o755)
    return bundle


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("CAPABILITIES_READ_ONLY", "CLAUDE_PROJECT_DIR",
                "CAPABILITIES_AUTH_CONTEXT", "CAPABILITIES_PROJECT_ENVELOPE",
                "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CAPABILITIES_PROJECT_ID",
                "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_STORE_URL",
                "CAPABILITIES_DEV_SESSION", "CAPABILITIES_WORKSPACE"):
        env.pop(key, None)
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CAPABILITIES_BIN": str(tmp_path / "bin"),
    })
    (tmp_path / "home").mkdir(exist_ok=True)
    return env


def _run(argv: list[str], env: dict, cwd: Path, extra: dict | None = None):
    return subprocess.run(argv, cwd=cwd, env={**env, **(extra or {})},
                          text=True, capture_output=True, timeout=180)


def _manager(env: dict, cwd: Path, *args: str, extra: dict | None = None):
    return _run([str(MANAGER), *args], env, cwd, extra)


def _ok(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _error(result: subprocess.CompletedProcess) -> dict:
    for line in reversed(result.stderr.splitlines()):
        try:
            return json.loads(line)["error"]
        except (ValueError, KeyError, TypeError):
            continue
    raise AssertionError(result.stdout + result.stderr)


def _outside(tmp_path: Path) -> Path:
    """A directory that resolves to no project."""
    place = tmp_path / "home" / "nowhere"
    place.mkdir(parents=True, exist_ok=True)
    return place


def _project(tmp_path: Path, policy: dict | str | None) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    envelope = root / "capabilities"
    envelope.mkdir(exist_ok=True)
    if policy is not None:
        (envelope / "settings.json").write_text(
            policy if isinstance(policy, str) else json.dumps(policy))
    return root


def _global(env: dict, policy: dict) -> None:
    gate = Path(env["XDG_CONFIG_HOME"]) / "capabilities" / "settings.json"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text(json.dumps(policy))


def _machine_file(env: dict) -> Path:
    return Path(env["XDG_CONFIG_HOME"]) / "capabilities" / "machine.json"


def _quarantine_file(env: dict, state: str = "quarantined") -> None:
    path = _machine_file(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "capabilities.machine.v1", "capabilities": {
        NAME: {"state": state, "at": "2026-01-01T00:00:00Z", "by": "test"}}}))


ENABLED = {"capabilities": {NAME: {"enabled": True}}}


# --- the manifest declaration ------------------------------------------------

INVALID = {
    "wrong schema": dict(MACHINE, schema="capabilities.service.machine.v2"),
    "command not starting with the name": dict(
        MACHINE, command=["other", "service", "run", "--machine"]),
    "doctor lacking service": dict(MACHINE, doctor=[NAME, "doctor", "--machine"]),
    "command lacking --machine": dict(MACHINE, command=[NAME, "service", "run"]),
    "projects outside the config home": dict(
        MACHINE, projects="$XDG_CONFIG_HOME/other/projects.toml"),
    "projects escaping the config home": dict(
        MACHINE, projects=f"$XDG_CONFIG_HOME/{NAME}/../other/projects.toml"),
    "an unknown key": dict(MACHINE, extra="rides in"),
}


def test_a_valid_machine_declaration_validates():
    manifest = {"name": NAME, "summary": "x", "credentials": {"scope": "project", "keys": []},
                "docs": {"base": "", "topics": []}, "state": False, "inventory": None,
                "post_install": [], "service": _service()}
    assert M._validate_manifest(NAME, manifest) == []
    for optional in ("config", "state"):
        trimmed = _service({k: v for k, v in MACHINE.items() if k != optional})
        assert M._validate_manifest(NAME, dict(manifest, service=trimmed)) == []


@pytest.mark.parametrize("shape", sorted(INVALID))
def test_each_invalid_machine_shape_is_refused(shape):
    problems = M._validate_service_machine(NAME, INVALID[shape], VERBS)
    assert problems and all("service.machine" in p for p in problems), problems


@pytest.mark.parametrize("verb", ["run", "status", "doctor", "join", "leave"])
def test_a_machine_service_must_list_each_required_verb(verb):
    problems = M._validate_service_machine(
        NAME, MACHINE, [v for v in VERBS if v != verb])
    assert any(verb in p and "service.verbs" in p for p in problems), problems


def test_a_service_without_machine_is_validated_as_before():
    assert M._validate_service_machine(NAME, MACHINE, VERBS) == []
    manifest = {"name": NAME, "summary": "x", "credentials": {"scope": "project", "keys": []},
                "docs": {"base": "", "topics": []}, "state": False, "inventory": None,
                "post_install": [], "service": _service(None, ["run"])}
    assert M._validate_manifest(NAME, manifest) == []


def _surfaces(tmp_path: Path, service: dict) -> dict[str, subprocess.CompletedProcess]:
    """Audit, source check and install of one fixture declaration."""
    env = _env(tmp_path)
    here = _outside(tmp_path)
    out: dict[str, subprocess.CompletedProcess] = {}
    bundle = _bundle(tmp_path / "bundles", service)
    out["audit"] = _manager(env, here, "audit", NAME, "--from", str(bundle / "bin" / NAME))
    workspace = Path(_ok(_manager(env, here, "source", "init", "fixture"))["path"])
    shutil.copytree(bundle, workspace / "capabilities" / NAME)
    indexed = _manager(env, workspace, "source", "index", "fixture")
    out["source index"] = indexed
    out["source check"] = _manager(env, here, "source", "check", "fixture")
    out["install"] = _manager(env, here, "install", NAME, "--from", str(bundle))
    return out


def test_a_valid_declaration_passes_audit_source_check_and_install(tmp_path):
    for surface, result in _surfaces(tmp_path, _service()).items():
        assert result.returncode == 0, (surface, result.stdout + result.stderr)


@pytest.mark.parametrize("shape", sorted(INVALID) + ["missing join"])
def test_an_invalid_declaration_is_refused_at_audit_source_check_and_install(tmp_path, shape):
    service = (_service(MACHINE, [v for v in VERBS if v != "join"])
               if shape == "missing join" else _service(INVALID[shape]))
    results = _surfaces(tmp_path, service)
    audit = results["audit"]
    assert audit.returncode == 7, audit.stdout + audit.stderr
    assert any("service.machine" in f for f in json.loads(audit.stdout)["failures"])
    check = results["source check"]
    assert check.returncode == 7, check.stdout + check.stderr
    assert "service.machine" in json.dumps(json.loads(check.stdout)["failures"].get(NAME))
    install = results["install"]
    assert install.returncode == 6, install.stdout + install.stderr
    error = _error(install)
    assert error["code"] == "manifest_invalid" and "service.machine" in error["hint"]


# --- the gate branch -------------------------------------------------------------

@pytest.fixture
def machine_fixture(tmp_path):
    env = _env(tmp_path)
    script = _bundle(tmp_path / "bundles", _service()) / "bin" / NAME
    return env, script


@pytest.mark.parametrize("verb", ["run", "doctor"])
def test_a_supervisor_call_passes_outside_any_project_with_no_policy(tmp_path, machine_fixture, verb):
    env, script = machine_fixture
    result = _run([str(script), "service", verb, "--machine"], env, _outside(tmp_path))
    assert _ok(result) == {"service": [verb, "--machine"]}
    # The same call without --machine takes the ordinary path and is refused.
    plain = _run([str(script), "service", verb], env, _outside(tmp_path))
    assert plain.returncode == 4


def test_a_machine_call_reads_no_policy_row_and_resolves_no_project(tmp_path, machine_fixture):
    env, script = machine_fixture
    project = _project(tmp_path, "{ not json")
    ordinary = _run([str(script), "service", "status"], env, project)
    assert ordinary.returncode == 6 and _error(ordinary)["code"] == "bad_config"
    machine = _run([str(script), "service", "status", "--machine"], env, project)
    assert _ok(machine) == {"service": ["status", "--machine"]}


@pytest.mark.parametrize("verb", ["init", "start", "run", "reload", "leave"])
def test_the_read_only_switch_refuses_the_machine_activations(tmp_path, machine_fixture, verb):
    env, script = machine_fixture
    result = _run([str(script), "service", verb, "--machine"], env, _outside(tmp_path),
                  {"CAPABILITIES_READ_ONLY": "1"})
    assert result.returncode == 4 and _error(result)["code"] == "read_only_switch"


@pytest.mark.parametrize("verb", ["doctor", "status"])
def test_the_read_only_switch_leaves_machine_reads_open(tmp_path, machine_fixture, verb):
    env, script = machine_fixture
    result = _run([str(script), "service", verb, "--machine"], env, _outside(tmp_path),
                  {"CAPABILITIES_READ_ONLY": "1"})
    assert _ok(result) == {"service": [verb, "--machine"]}


def test_an_ingress_authority_still_refuses_first(tmp_path, machine_fixture):
    env, script = machine_fixture
    result = _run([str(script), "service", "run", "--machine"], env, _outside(tmp_path),
                  {"CAPABILITIES_AUTH_CONTEXT": json.dumps({"allowed_capabilities": []})})
    assert result.returncode == 4 and _error(result)["code"] == "capability_not_authorized"


@pytest.mark.parametrize("args", [["service", "join"], ["service", "join", "--machine"],
                                  ["service", "init"]])
def test_join_requires_explicit_project_enable_like_init(tmp_path, machine_fixture, args):
    env, script = machine_fixture
    _global(env, ENABLED)
    project = _project(tmp_path, {"capabilities": {}})
    inherited = _run([str(script), *args], env, project)
    assert inherited.returncode == 4
    assert _error(inherited)["code"] == "project_enable_required"
    _project(tmp_path, ENABLED)
    assert _ok(_run([str(script), *args], env, project))["service"] == args[1:]


def test_join_is_refused_under_the_read_only_switch(tmp_path, machine_fixture):
    env, script = machine_fixture
    project = _project(tmp_path, ENABLED)
    result = _run([str(script), "service", "join"], env, project,
                  {"CAPABILITIES_READ_ONLY": "1"})
    assert result.returncode == 4 and _error(result)["code"] == "read_only_switch"


def test_without_service_machine_the_flag_is_an_ordinary_argument(tmp_path):
    env = _env(tmp_path)
    script = _bundle(tmp_path / "bundles", _service(None, ["run", "join"])) / "bin" / NAME
    here = _outside(tmp_path)
    refused = _run([str(script), "service", "status", "--machine"], env, here)
    assert refused.returncode == 4 and _error(refused)["code"] == "not_enabled"
    activation = _run([str(script), "service", "run", "--machine"], env, here)
    assert activation.returncode == 4
    assert _error(activation)["code"] == "project_enable_required"
    _global(env, ENABLED)
    assert _ok(_run([str(script), "service", "status", "--machine"], env, here))
    inherited = _run([str(script), "service", "run", "--machine"], env, here)
    assert inherited.returncode == 4
    assert _error(inherited)["code"] == "project_enable_required"
    read_only = _run([str(script), "service", "status", "--machine"], env, here,
                     {"CAPABILITIES_READ_ONLY": "1"})
    assert _ok(read_only)
    # `join` and `leave` are verbs like any other to a service without a machine mode.
    project = _project(tmp_path, {"capabilities": {}})
    assert _ok(_run([str(script), "service", "join", "--machine"], env, project))
    assert _ok(_run([str(script), "service", "leave"], env, project,
                    {"CAPABILITIES_READ_ONLY": "1"}))


# --- the ceiling -------------------------------------------------------------------

def _installed(tmp_path: Path, *install_flags: str) -> tuple[dict, Path]:
    env = _env(tmp_path)
    bundle = _bundle(tmp_path / "bundles", _service())
    _ok(_manager(env, _outside(tmp_path), "install", NAME, "--from", str(bundle),
                 *install_flags))
    return env, Path(env["CAPABILITIES_HOME"]) / NAME / NAME


def _machine_entries(env: dict) -> dict:
    path = _machine_file(env)
    return json.loads(path.read_text())["capabilities"] if path.exists() else {}


def test_a_quarantined_capability_is_refused_at_both_scopes_and_allow_restores_it(tmp_path):
    env, script = _installed(tmp_path)
    _global(env, ENABLED)
    project = _project(tmp_path, ENABLED)
    for args in (["doctor"], ["refs"], ["ids", "list"], ["service", "status"],
                 ["service", "run", "--machine"], ["service", "doctor", "--machine"]):
        refused = _run([str(script), *args], env, project)
        assert refused.returncode == 4, (args, refused.stdout + refused.stderr)
        error = _error(refused)
        assert error["code"] == "quarantined", args
        assert f"capabilities allow {NAME}" in error["hint"]
        assert "never" in error["hint"]
    outside = _run([str(script), "service", "run", "--machine"], env, _outside(tmp_path))
    assert outside.returncode == 4 and _error(outside)["code"] == "quarantined"
    for args in (["help"], ["stub"], ["manifest", "--json"], ["connections"]):
        assert _run([str(script), *args], env, project).returncode == 0, args

    _ok(_manager(env, _outside(tmp_path), "allow", NAME))
    assert _ok(_run([str(script), "doctor"], env, project))["ok"] is True
    assert _ok(_run([str(script), "service", "run", "--machine"], env, project))
    _ok(_manager(env, _outside(tmp_path), "quarantine", NAME))
    assert _error(_run([str(script), "doctor"], env, project))["code"] == "quarantined"


def test_an_allowed_capability_leaves_both_scopes_deciding(tmp_path):
    env, script = _installed(tmp_path, "--allow")
    project = _project(tmp_path, {"capabilities": {NAME: {"enabled": False}}})
    assert _error(_run([str(script), "doctor"], env, project))["code"] == "disabled"
    _project(tmp_path, {"capabilities": {}})
    assert _error(_run([str(script), "doctor"], env, project))["code"] == "not_enabled"
    _global(env, ENABLED)
    assert _ok(_run([str(script), "doctor"], env, project))["ok"] is True


def _connections(env: dict) -> None:
    registry = Path(env["XDG_CONFIG_HOME"]) / NAME / "connections.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"default": "first", "connections": {
        "first": {"url": "https://one.example"},
        "second": {"url": "https://two.example", "allow_write": False},
        "third": {"url": "https://three.example", "enabled": True},
        "withheld": {"url": "https://four.example", "enabled": False},
    }}))


def test_list_reports_the_ceiling_and_the_machines_connections(tmp_path):
    env, _script_path = _installed(tmp_path, "--allow")
    _connections(env)
    _global(env, ENABLED)
    project = _project(tmp_path, ENABLED)
    row = {r["name"]: r for r in _ok(_manager(env, project, "list", "--json"))["installed"]}[NAME]
    assert row["machine"] == "allowed"
    assert row["machine_connections"] == 3
    assert (row["effective"], row["source"]) == ("enabled", "project")

    _ok(_manager(env, project, "quarantine", NAME))
    for where in (project, _outside(tmp_path)):
        row = {r["name"]: r for r in _ok(_manager(env, where, "list", "--json"))["installed"]}[NAME]
        assert row["machine"] == "quarantined"
        assert row["machine_connections"] == 3
        assert (row["effective"], row["source"]) == ("disabled", "machine")
        assert row["global_gate"] == "enabled"


def test_inventory_and_context_leave_a_quarantined_capability_out(tmp_path):
    env, _script_path = _installed(tmp_path, "--allow")
    project = _project(tmp_path, ENABLED)
    rows = _ok(_manager(env, project, "inventory"))["capabilities"]
    assert [r["name"] for r in rows] == [NAME]
    fragment = _manager(env, project, "context", "--fragment")
    assert fragment.returncode == 0 and f"## {NAME}" in fragment.stdout

    _ok(_manager(env, project, "quarantine", NAME))
    assert _ok(_manager(env, project, "inventory"))["capabilities"] == []
    # The manager caches each project's envelope answer; the gate is read fresh.
    fragment = _manager(env, project, "context", "--fragment")
    assert fragment.returncode == 0 and f"## {NAME}" not in fragment.stdout


def test_allow_and_quarantine_answer_what_changed(tmp_path):
    env, _script_path = _installed(tmp_path, "--allow")
    here = _outside(tmp_path)
    assert _ok(_manager(env, here, "allow", NAME)) == {
        "capability": NAME, "machine": "allowed", "changed": False}
    assert _ok(_manager(env, here, "quarantine", NAME)) == {
        "capability": NAME, "machine": "quarantined", "changed": True}
    assert _ok(_manager(env, here, "quarantine", NAME)) == {
        "capability": NAME, "machine": "quarantined", "changed": False}
    assert _ok(_manager(env, here, "allow", NAME)) == {
        "capability": NAME, "machine": "allowed", "changed": True}
    entry = _machine_entries(env)[NAME]
    assert entry["state"] == "allowed" and entry["at"] and entry["by"]
    body = json.loads(_machine_file(env).read_text())
    assert body["schema"] == "capabilities.machine.v1"
    assert not [p for p in _machine_file(env).parent.iterdir() if p.name.endswith(".tmp")]


def test_allow_and_quarantine_refuse_what_is_not_installed(tmp_path):
    env = _env(tmp_path)
    for verb in ("allow", "quarantine"):
        result = _manager(env, _outside(tmp_path), verb, NAME)
        assert result.returncode == 3 and _error(result)["code"] == "not_found"
    assert not _machine_file(env).exists()


def test_allow_and_quarantine_refuse_under_the_read_only_switch(tmp_path):
    env, _script_path = _installed(tmp_path)
    before = _machine_file(env).read_bytes()
    for verb in ("allow", "quarantine"):
        result = _manager(env, _outside(tmp_path), verb, NAME,
                          extra={"CAPABILITIES_READ_ONLY": "1"})
        assert result.returncode == 4 and _error(result)["code"] == "read_only_switch"
    assert _machine_file(env).read_bytes() == before


def test_the_ceiling_is_written_under_the_manager_lock(tmp_path):
    env, _script_path = _installed(tmp_path, "--allow")
    lock = Path(env["XDG_STATE_HOME"]) / "capabilities" / "manager-mutation.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        waiting = subprocess.Popen(
            [str(MANAGER), "quarantine", NAME], cwd=_outside(tmp_path), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(2.0)
        assert waiting.poll() is None
        assert _machine_entries(env)[NAME]["state"] == "allowed"
    out, err = waiting.communicate(timeout=60)
    assert waiting.returncode == 0, out + err
    assert json.loads(out)["changed"] is True
    assert _machine_entries(env)[NAME]["state"] == "quarantined"


def test_a_capability_never_writes_the_ceiling(tmp_path):
    env = _env(tmp_path)
    script = _bundle(tmp_path / "bundles", _service()) / "bin" / NAME
    project = _project(tmp_path, ENABLED)
    for args in (["doctor"], ["service", "run", "--machine"], ["service", "join"]):
        _run([str(script), *args], env, project)
    assert not _machine_file(env).exists()
    _quarantine_file(env, "allowed")
    before = _machine_file(env).read_bytes()
    for args in (["doctor"], ["service", "run", "--machine"], ["ids", "set", "k", "v"]):
        _run([str(script), *args], env, project)
    assert _machine_file(env).read_bytes() == before


def test_a_fresh_install_arrives_quarantined_and_keeps_its_state(tmp_path):
    env, _script_path = _installed(tmp_path)
    here = _outside(tmp_path)
    assert _machine_entries(env)[NAME]["state"] == "quarantined"
    bundle = tmp_path / "bundles" / NAME
    reinstalled = _ok(_manager(env, here, "install", NAME, "--from", str(bundle)))
    assert reinstalled["machine"] == "quarantined"
    _ok(_manager(env, here, "update", NAME))
    assert _machine_entries(env)[NAME]["state"] == "quarantined"
    _ok(_manager(env, here, "allow", NAME))
    assert _ok(_manager(env, here, "install", NAME, "--from", str(bundle)))["machine"] == "allowed"
    _ok(_manager(env, here, "update", NAME))
    assert _machine_entries(env)[NAME]["state"] == "allowed"
    _ok(_manager(env, here, "uninstall", NAME))
    assert NAME not in _machine_entries(env)
    again = _ok(_manager(env, here, "install", NAME, "--from", str(bundle)))
    assert again["machine"] == "quarantined"
    assert f"capabilities allow {NAME}" in again["machine_hint"]


def test_install_with_allow_arrives_allowed(tmp_path):
    env, _script_path = _installed(tmp_path, "--allow")
    assert _machine_entries(env)[NAME]["state"] == "allowed"


def test_without_a_machine_file_every_capability_is_allowed(tmp_path):
    env = _env(tmp_path)
    registry = Path(env["CAPABILITIES_HOME"]) / NAME
    registry.mkdir(parents=True)
    (registry / NAME).write_text("#!/bin/sh\nexit 1\n")
    (registry / "meta.json").write_text("{}")
    (registry / "manifest.json").write_text(json.dumps({"name": NAME, "summary": "x"}))
    _global(env, ENABLED)
    row = _ok(_manager(env, _outside(tmp_path), "list", "--json"))["installed"][0]
    assert (row["machine"], row["effective"], row["source"]) == ("allowed", "enabled", "global")
    assert row["machine_connections"] == 0
    assert not _machine_file(env).exists()


@pytest.mark.parametrize("scope", ["--project", "--global"])
def test_enabling_a_quarantined_capability_writes_and_warns(tmp_path, scope):
    env, _script_path = _installed(tmp_path)
    project = _project(tmp_path, {"capabilities": {}})
    result = _manager(env, project, "enable", NAME, scope)
    payload = _ok(result)
    assert payload["enabled"] == NAME and payload["machine"] == "quarantined"
    assert "quarantined" in payload["warning"]
    assert any("quarantined" in json.loads(line).get("warning", "")
               for line in result.stderr.splitlines() if line.startswith("{"))
    gate = (project / "capabilities" / "settings.json" if scope == "--project"
            else Path(env["XDG_CONFIG_HOME"]) / "capabilities" / "settings.json")
    assert json.loads(gate.read_text())["capabilities"][NAME] == {"enabled": True}
    row = {r["name"]: r for r in _ok(_manager(env, project, "list", "--json"))["installed"]}[NAME]
    assert (row["effective"], row["source"]) == ("disabled", "machine")
