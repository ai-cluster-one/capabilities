"""Machine reads: outside any project, a declared read verb uses the machine's
own connections, read-only.

A capability declares them in `MACHINE_READS`, beside `WRITE_DEFAULT`. The
manager validates the declaration wherever it validates a capability, and the
contract honours it only outside a project, only for the verbs declared, and
only for reading. Inside a project the grant model alone decides, and the
policy gate is the same gate it always was. `connections` also reports the
capability's effective policy state.

The capability these tests drive is the manager's own scaffold, stamped with
the canonical contract and given three domain verbs: `look` (a read, which
`look push` turns into a write), `peek` (a read it does not declare) and
`push` (a write).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
NAME = "mrfix"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_machine_reads", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()

DOMAIN = '''    verb = " ".join(cleaned)
    if cleaned[:1] in (["look"], ["peek"], ["push"]):
        reg, _ = _connections_registry()
        cid, entry = _select_connection(reg, wanted)
        _write_gate(cid, entry["allow_write"], verb)
        _emit({"verb": verb, "connection": cid,
               "allow_write": entry["allow_write"]})
        return
    if cleaned == ["doctor"]:'''


def _fixture_text(machine_reads: str | None = "('connections', 'look')") -> str:
    text = M._capability_skeleton(NAME, False)
    text = text.replace(
        f"  {NAME} inventory [--network]\n",
        f"  {NAME} inventory [--network]\n  {NAME} look [push]\n"
        f"  {NAME} peek\n  {NAME} push\n", 1)
    text = text.replace("WRITE_VERBS = set()\n",
                        'WRITE_VERBS = {"push", "look push"}\n', 1)
    if machine_reads is not None:
        text = text.replace("WRITE_DEFAULT = True\n",
                            f"WRITE_DEFAULT = True\nMACHINE_READS = {machine_reads}\n", 1)
    text = text.replace("    if cleaned == [\"doctor\"]:", DOMAIN, 1)
    assert "def main" in text and DOMAIN.splitlines()[0] in text
    return text


def _write(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body) + "\n")


@pytest.fixture()
def lab(tmp_path):
    """A home, a config home, a project and a directory that is no project,
    all of them this test's own."""
    home = tmp_path / "home"
    nowhere = home / "nowhere"
    nowhere.mkdir(parents=True)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "capabilities").mkdir()
    config = tmp_path / "config"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CAPABILITIES_", "CLAUDE_"))}
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(config),
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"),
                "CAPABILITIES_BIN": str(tmp_path / "bin")})
    script = tmp_path / "cap" / NAME
    script.parent.mkdir()
    script.write_text(_fixture_text())
    script.chmod(0o755)
    lab = {"tmp": tmp_path, "home": home, "nowhere": nowhere, "project": project,
           "config": config, "env": env, "script": script}
    _policy(lab, glob=True)
    _declare_global(lab, {"default": "home",
                          "connections": {"home": {"url": "https://a.example"}}})
    return lab


def _policy(lab, glob=None, proj=None) -> None:
    if glob is not None:
        _write(lab["config"] / "capabilities" / "settings.json",
               {"capabilities": {NAME: {"enabled": glob}}})
    if proj is not None:
        _write(lab["project"] / "capabilities" / "settings.json",
               {"capabilities": {NAME: {"enabled": proj}}})


def _declare_global(lab, body) -> None:
    _write(lab["config"] / NAME / "connections.json", body)


def _declare_project(lab, body) -> None:
    _write(lab["project"] / "capabilities" / NAME / "connections.json", body)


def _quarantine(lab) -> None:
    _write(lab["config"] / "capabilities" / "machine.json",
           {"schema": "capabilities.machine.v1",
            "capabilities": {NAME: {"state": "quarantined", "at": "t", "by": "t"}}})


def _run(lab, *args, where="nowhere", extra=None):
    env = dict(lab["env"])
    env.update(extra or {})
    return subprocess.run([sys.executable, str(lab["script"]), *args],
                          cwd=lab[where], env=env, text=True,
                          capture_output=True, timeout=120)


def _error(result) -> dict:
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines, result.stdout
    return json.loads(lines[-1])["error"]


# --- the declaration, validated wherever a capability is ---------------------

INVALID = {
    "a list": "['connections', 'look']",
    "a string": "'look'",
    "not all strings": "('look', 7)",
    "computed": "tuple(['look'])",
    "an unknown verb": "('look', 'nosuchverb')",
    "a write verb": "('look', 'push')",
}


def _declared(tmp_path: Path, machine_reads: str | None) -> Path:
    script = tmp_path / "src" / "capabilities" / NAME / "bin" / NAME
    script.parent.mkdir(parents=True)
    script.write_text(_fixture_text(machine_reads))
    script.chmod(0o755)
    return script


def _manager(lab, *args):
    env = dict(lab["env"])
    env["CAPABILITIES_MANAGER_BIN"] = str(MANAGER)
    return subprocess.run([str(MANAGER), *args], cwd=lab["nowhere"], env=env,
                          text=True, capture_output=True, timeout=300)


def _machine_read_failures(failures) -> list[str]:
    return [f for f in failures if f.startswith("connections/machine-reads")]


@pytest.mark.parametrize("declaration", [None, "()", "('connections', 'look')",
                                         "('connections', 'doctor', 'peek')"])
def test_a_valid_declaration_is_accepted_at_audit_source_check_and_install(
        lab, tmp_path, declaration):
    script = _declared(tmp_path, declaration)
    assert M._machine_reads_problems(script) == []
    audit = _manager(lab, "audit", NAME, "--from", str(script))
    assert _machine_read_failures(json.loads(audit.stdout)["failures"]) == []
    _catalog, failures = M._catalog_payload(tmp_path / "src", audit=False)
    assert _machine_read_failures(failures.get(NAME, [])) == []
    install = _manager(lab, "install", NAME, "--from", str(script), "--allow")
    assert install.returncode == 0, install.stderr


@pytest.mark.parametrize("case", sorted(INVALID))
def test_an_invalid_declaration_is_refused_at_audit_source_check_and_install(
        lab, tmp_path, case):
    script = _declared(tmp_path, INVALID[case])
    problems = M._machine_reads_problems(script)
    assert problems, case

    audit = _manager(lab, "audit", NAME, "--from", str(script))
    assert audit.returncode != 0
    assert _machine_read_failures(json.loads(audit.stdout)["failures"]), case

    _catalog, failures = M._catalog_payload(tmp_path / "src", audit=False)
    assert _machine_read_failures(failures.get(NAME, [])), case
    _catalog, failures = M._catalog_payload_scoped(
        tmp_path / "src", {NAME}, {}, audit=False)
    assert _machine_read_failures(failures.get(NAME, [])), case

    install = _manager(lab, "install", NAME, "--from", str(script), "--allow")
    assert install.returncode == 6, install.stdout + install.stderr
    assert _error(install)["code"] == "machine_reads_invalid"
    assert not (Path(lab["env"]["CAPABILITIES_HOME"]) / NAME).exists()


def test_the_refusals_say_which_rule_the_declaration_broke(tmp_path):
    def problems(declaration, sub):
        target = tmp_path / sub
        target.mkdir()
        return " ".join(M._machine_reads_problems(_declared(target, declaration)))
    assert "tuple of verb names" in problems(INVALID["a list"], "a")
    assert "'nosuchverb'" in problems(INVALID["an unknown verb"], "b")
    assert "write verb 'push'" in problems(INVALID["a write verb"], "c")


# --- resolution outside any project -------------------------------------------

def test_a_declared_read_uses_the_machine_connection_read_only(lab):
    result = _run(lab, "look")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "verb": "look", "connection": "home", "allow_write": False}


def test_the_connection_flag_before_the_verb_still_names_the_verb(lab):
    for args in (("--connection", "home", "look"), ("--connection=home", "look"),
                 ("look", "--connection", "home")):
        result = _run(lab, *args)
        assert result.returncode == 0, (args, result.stderr)
        assert json.loads(result.stdout)["connection"] == "home"


def test_connections_names_the_machine_scope_and_reads_only(lab):
    result = _run(lab, "connections")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["connections"]["home"]["allow_write"] is False
    machine = report["machine_reads"]
    assert machine["verbs"] == ["connections", "look"]
    assert machine["connections"] == {"home": {
        "scope": "machine",
        "source": str(lab["config"] / NAME / "connections.json")}}
    assert "read-only" in machine["effect"]


def test_a_write_through_a_declared_read_is_refused_as_read_only(lab):
    _declare_global(lab, {"default": "home", "connections": {
        "home": {"url": "https://a.example", "allow_write": True}}})
    result = _run(lab, "look", "push")
    assert result.returncode == 4
    error = _error(result)
    assert error["code"] == "read_only"
    assert "ask the user" in error["hint"]


def test_a_write_verb_outside_a_project_is_refused_as_today(lab):
    result = _run(lab, "push")
    assert result.returncode == 4
    assert _error(result)["code"] == "connection_not_granted"


def test_an_undeclared_read_is_refused_as_today(lab):
    result = _run(lab, "peek")
    assert result.returncode == 4
    error = _error(result)
    assert error["code"] == "connection_not_granted"
    assert "no project here" in error["message"]


def test_a_grant_switched_off_on_the_machine_still_withholds_it(lab):
    _declare_global(lab, {"default": "home", "connections": {
        "home": {"url": "https://a.example", "enabled": False},
        "spare": {"url": "https://b.example"}}})
    result = _run(lab, "look")
    assert result.returncode == 4
    error = _error(result)
    assert error["code"] == "connection_not_granted"
    assert "switched off" in error["message"]
    spare = _run(lab, "--connection", "spare", "look")
    assert spare.returncode == 0, spare.stderr
    assert json.loads(spare.stdout)["allow_write"] is False

    _declare_global(lab, {"connections": {
        "home": {"url": "https://a.example", "enabled": False}}})
    every = _run(lab, "look")
    assert every.returncode == 4
    assert _error(every)["code"] == "connection_not_granted"


def test_a_capability_declaring_nothing_has_no_machine_reads(lab):
    lab["script"].write_text(_fixture_text(None))
    for verb in ("look", "connections"):
        result = _run(lab, verb)
        assert result.returncode == 4, verb
        assert _error(result)["code"] == "connection_not_granted"


# --- inside a project the grant model alone decides ---------------------------

def test_inside_a_project_a_declared_read_needs_the_project_grant(lab):
    for verb in ("look", "connections", "peek"):
        result = _run(lab, verb, where="project")
        assert result.returncode == 4, verb
        error = _error(result)
        assert error["code"] == "connection_not_granted"
        assert "this project has not granted it" in error["message"] or \
            "usable in this project" in error["message"]


def test_inside_a_project_a_granted_connection_resolves_as_today(lab):
    _declare_project(lab, {"connections": {"home": {"enabled": True}}})
    look = _run(lab, "look", where="project")
    assert look.returncode == 0, look.stderr
    assert json.loads(look.stdout)["allow_write"] is True
    push = _run(lab, "look", "push", where="project")
    assert push.returncode == 0, push.stderr
    report = json.loads(_run(lab, "connections", where="project").stdout)
    assert "machine_reads" not in report
    assert report["connections"]["home"]["allow_write"] is True


def test_inside_a_project_a_project_switch_off_stands(lab):
    _declare_project(lab, {"connections": {"home": {"enabled": False}}})
    result = _run(lab, "look", where="project")
    assert result.returncode == 4
    assert _error(result)["code"] == "connection_not_granted"


# --- the gate is the gate it was ----------------------------------------------

def test_outside_a_project_a_declared_read_of_a_capability_not_enabled_is_refused(lab):
    (lab["config"] / "capabilities" / "settings.json").unlink()
    result = _run(lab, "look")
    assert result.returncode == 4
    assert _error(result)["code"] == "not_enabled"
    _policy(lab, glob=False)
    result = _run(lab, "look")
    assert result.returncode == 4
    assert _error(result)["code"] == "not_enabled"


def test_outside_a_project_a_declared_read_of_a_quarantined_capability_is_refused(lab):
    _quarantine(lab)
    result = _run(lab, "look")
    assert result.returncode == 4
    assert _error(result)["code"] == "quarantined"


def test_under_the_read_only_switch_writes_stay_refused(lab):
    switch = {"CAPABILITIES_READ_ONLY": "1"}
    look = _run(lab, "look", extra=switch)
    assert look.returncode == 0, look.stderr
    assert json.loads(look.stdout)["allow_write"] is False
    push = _run(lab, "look", "push", extra=switch)
    assert push.returncode == 4
    assert _error(push)["code"] == "read_only_switch"
    report = json.loads(_run(lab, "connections", extra=switch).stdout)
    assert report["read_only_switch"]["on"] is True
    assert report["machine_reads"]["connections"]["home"]["scope"] == "machine"


# --- connections reports the policy state -------------------------------------

def _report(lab, where):
    result = _run(lab, "connections", where=where)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("policy, where, expected", [
    ({"proj": True}, "project", {"effective": "enabled", "source": "project"}),
    ({"proj": False}, "project", {"effective": "disabled", "source": "project"}),
    ({}, "project", {"effective": "enabled", "source": "global"}),
    ({"glob": False}, "project", {"effective": "disabled", "source": "global"}),
    ({}, "nowhere", {"effective": "enabled", "source": "global"}),
])
def test_connections_reports_the_effective_policy(lab, policy, where, expected):
    _declare_project(lab, {"connections": {"home": {"enabled": True}}})
    _policy(lab, **policy)
    report = _report(lab, where)
    assert report["policy"] == {**expected, "machine": "allowed"}


def test_connections_reports_no_policy_as_the_default_deny(lab):
    (lab["config"] / "capabilities" / "settings.json").unlink()
    _declare_project(lab, {"connections": {"home": {"enabled": True}}})
    assert _report(lab, "project")["policy"] == {
        "effective": "disabled", "source": "default", "machine": "allowed"}


def test_connections_reports_a_quarantine_as_the_machine_deciding(lab):
    _declare_project(lab, {"connections": {"home": {"enabled": True}}})
    _policy(lab, proj=True)
    _quarantine(lab)
    assert _report(lab, "project")["policy"] == {
        "effective": "disabled", "source": "machine", "machine": "quarantined"}


def test_the_policy_field_is_the_only_field_added(lab):
    _declare_project(lab, {"default": "home",
                           "connections": {"home": {"enabled": True}}})
    lab["script"].write_text(_fixture_text(None))
    report = _report(lab, "project")
    assert set(report) == {"default", "connections", "policy"}
    assert report["default"] == "home"
    assert report["connections"] == {"home": {"allow_write": True, "keys": []}}


def test_an_unreadable_policy_is_reported_and_connections_still_answers(lab):
    _declare_project(lab, {"connections": {"home": {"enabled": True}}})
    _write(lab["project"] / "capabilities" / "settings.json",
           {"capabilities": {NAME: {"enabled": "yes"}}})
    report = _report(lab, "project")
    assert report["policy"]["error"]["code"] == "bad_policy"
    assert set(report["connections"]) == {"home"}
