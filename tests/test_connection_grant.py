"""A connection a project inherited is not a connection a project may use.

Declaring a connection locally is the act of permission. An identity that
resolves at global scope is withheld from every project until that project's
own grant says `enabled: true`, and the refusal is a gate the agent reports
rather than lifts. These tests drive a real connection-bearing capability so
the enforcement is proven where every capability routes through it, not only
where the records tier decides it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
COOLIFY = REPO / "capabilities" / "coolify" / "bin" / "coolify"

GLOBAL_BOX = {"base_url": "https://coolify.global.example",
              "secret_env": "COOLIFY_TOKEN"}
LOCAL_BOX = {"base_url": "https://coolify.local.example",
             "secret_env": "COOLIFY_TOKEN"}


def _write(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body) + "\n")


@pytest.fixture()
def lab(tmp_path):
    """A project and a config home that exist only for this test.

    HOME is moved with the rest, because a capability that fell back to the
    machine's own config home would read the maintainer's real connections and
    the test would prove nothing about the project it built.
    """
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    _write(envelope / "settings.json",
           {"capabilities": {"coolify": {"enabled": True}}})
    config = tmp_path / "config"
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
    })
    for leaked in ("COOLIFY_BASE_URL", "COOLIFY_TOKEN", "VIRTUAL_ENV",
                   "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_STORE_URL",
                   "CAPABILITIES_STORE_MODE"):
        env.pop(leaked, None)
    env["COOLIFY_TOKEN"] = "test-token"
    return {"project": project, "envelope": envelope, "config": config, "env": env}


def _run(lab, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(COOLIFY), *args], cwd=lab["project"],
                          env=lab["env"], text=True, capture_output=True,
                          timeout=180)


def _error(result: subprocess.CompletedProcess) -> dict:
    """The error envelope, read off the last line the script wrote.

    The launcher warms its own cache on stderr the first time it runs a script,
    and the envelope is one line of JSON, so the last line is the answer."""
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines, result.stdout
    return json.loads(lines[-1])["error"]


def _declare_global(lab, body: dict) -> None:
    _write(lab["config"] / "coolify" / "connections.json", body)


def _declare_project(lab, body: dict) -> None:
    _write(lab["envelope"] / "coolify" / "connections.json", body)


# --- the gate ----------------------------------------------------------------

def test_a_global_connection_is_withheld_from_a_project_that_never_granted_it(lab):
    """The defect this rule exists for: a brand-new project used to resolve the
    owner's personal connection, for reads and writes alike."""
    _declare_global(lab, {"default": "personal",
                          "connections": {"personal": GLOBAL_BOX}})
    result = _run(lab, "connections")
    assert result.returncode == 4, result.stdout + result.stderr
    error = _error(result)
    assert error["code"] == "connection_not_granted"
    assert "personal" in error["message"]


def test_the_all_withheld_refusal_is_not_a_missing_registry(lab):
    """Exit 6 keeps meaning "nothing is declared anywhere". Something declared
    and withheld is a decision, and says so with its own code."""
    _declare_global(lab, {"connections": {"personal": GLOBAL_BOX}})
    withheld = _run(lab, "connections")
    assert withheld.returncode == 4
    assert _error(withheld)["code"] == "connection_not_granted"

    (lab["config"] / "coolify" / "connections.json").unlink()
    nothing = _run(lab, "connections")
    assert nothing.returncode == 6
    assert _error(nothing)["code"] == "connections_required"


def test_the_refusal_names_the_command_that_grants_it_here(lab):
    """The exit-4 wording convention: the agent is told to ask the human, and
    handed the exact command rather than a description of one."""
    _declare_global(lab, {"connections": {"personal": GLOBAL_BOX}})
    error = _error(_run(lab, "connections"))
    assert "ask the user" in error["hint"]
    assert "capabilities set coolify grant" in error["hint"]
    assert "run inside this project" in error["hint"]
    assert "capabilities init" not in error["hint"]
    assert "this project" in error["message"]


def test_a_project_grant_is_what_opens_a_global_connection(lab):
    _declare_global(lab, {"default": "personal",
                          "connections": {"personal": GLOBAL_BOX}})
    _declare_project(lab, {"connections": {"personal": {"enabled": True}}})
    result = _run(lab, "connections")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert set(report["connections"]) == {"personal"}


def test_a_grant_only_project_entry_keeps_the_identity_it_inherited(lab):
    """One file holds both records here, so a grant-only entry carries no
    identity field. It decides about the inherited connection; it must never
    replace it with a blank one."""
    _declare_global(lab, {"default": "personal",
                          "connections": {"personal": GLOBAL_BOX}})
    _declare_project(lab, {"connections": {"personal": {"enabled": True}}})
    report = json.loads(_run(lab, "connections").stdout)
    keys = {k["key"]: k for k in report["connections"]["personal"]["keys"]}
    assert keys["base_url"]["value"] == GLOBAL_BOX["base_url"]
    assert keys["COOLIFY_TOKEN"]["set"] is True


def test_a_global_grant_is_not_the_blessing(lab):
    """One line in the machine's own config would otherwise hand the connection
    back to every project on it."""
    _declare_global(lab, {"default": "personal",
                          "connections": {"personal": {**GLOBAL_BOX,
                                                       "enabled": True}}})
    result = _run(lab, "connections")
    assert result.returncode == 4, result.stdout + result.stderr
    assert _error(result)["code"] == "connection_not_granted"


def test_a_project_scope_connection_needs_no_grant(lab):
    """Declaring it locally is the act of permission."""
    _declare_project(lab, {"default": "local", "connections": {"local": LOCAL_BOX}})
    result = _run(lab, "connections")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert set(report["connections"]) == {"local"}


@pytest.fixture()
def nowhere(tmp_path):
    """A bare directory with no envelope anywhere above it.

    This is a supported, permanent state rather than an edge: there is no scope
    in which a grant could be written, so every inherited connection stays
    withheld and the capability cannot be used here at all."""
    bare = tmp_path / "bare"
    bare.mkdir()
    config = tmp_path / "config"
    _write(config / "capabilities" / "settings.json",
           {"capabilities": {"coolify": {"enabled": True}}})
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
    })
    for leaked in ("CLAUDE_PROJECT_DIR", "COOLIFY_BASE_URL", "COOLIFY_TOKEN",
                   "VIRTUAL_ENV", "CAPABILITIES_PROJECT_ENVELOPE",
                   "CAPABILITIES_STORE_URL", "CAPABILITIES_STORE_MODE"):
        env.pop(leaked, None)
    env["COOLIFY_TOKEN"] = "test-token"
    return {"project": bare, "envelope": bare / "capabilities",
            "config": config, "env": env}


def test_outside_any_project_the_refusal_says_so_and_names_the_first_step(nowhere):
    """The two refusals must not converge: an agent told it is in a project
    that has not granted goes looking for a grant to write, and there is
    nowhere to write one."""
    _declare_global(nowhere, {"default": "personal",
                              "connections": {"personal": GLOBAL_BOX}})
    result = _run(nowhere, "connections")
    assert result.returncode == 4, result.stdout + result.stderr
    error = _error(result)
    assert error["code"] == "connection_not_granted"
    assert "no project here" in error["message"]
    assert "ask the user" in error["hint"]
    assert "capabilities init" in error["hint"]
    assert "capabilities set coolify grant" in error["hint"]


def test_outside_any_project_naming_a_connection_is_refused_the_same_way(nowhere):
    """Selection reaches the same answer. Nothing is usable here, so the
    envelope refuses before a name can be looked up — and it has to refuse with
    the situation the caller is actually in, not a project's missing grant."""
    _declare_global(nowhere, {"connections": {"personal": GLOBAL_BOX,
                                              "other": LOCAL_BOX}})
    result = _run(nowhere, "--connection", "personal", "servers")
    assert result.returncode == 4, result.stdout + result.stderr
    error = _error(result)
    assert error["code"] == "connection_not_granted"
    assert "no project here" in error["message"]
    assert "capabilities init" in error["hint"]
    assert "run inside this project" not in error["hint"]


# --- granting reach is not granting write ------------------------------------
#
# coolify's WRITE_DEFAULT is True, which is what would make this invisible:
# a machine-wide `allow_write: false` washed away by a project's `enabled: true`
# does not read as a missing decision, it reads as permission to write.

def test_granting_a_connection_does_not_hand_out_write_the_owner_withheld(lab):
    """The project runs exactly the command the refusal's hint prints, and the
    machine's own read-only decision has to survive it."""
    _declare_global(lab, {"connections": {"personal": {**GLOBAL_BOX,
                                                       "allow_write": False}}})
    _declare_project(lab, {"connections": {"personal": {"enabled": True}}})
    report = json.loads(_run(lab, "connections").stdout)
    assert report["connections"]["personal"]["allow_write"] is False

    writing = _run(lab, "--connection", "personal", "projects", "u", "create",
                   "--name", "probe")
    assert writing.returncode == 4, writing.stdout + writing.stderr
    assert _error(writing)["code"] == "read_only"


def test_a_project_that_asks_for_write_in_its_own_grant_still_gets_it(lab):
    """The decision is the project's to make; what it may not do is inherit one
    it never made."""
    _declare_global(lab, {"connections": {"personal": {**GLOBAL_BOX,
                                                       "allow_write": False}}})
    _declare_project(lab, {"connections": {"personal": {"enabled": True,
                                                        "allow_write": True}}})
    report = json.loads(_run(lab, "connections").stdout)
    assert report["connections"]["personal"]["allow_write"] is True


def test_writability_nobody_decided_is_still_the_capability_default(lab):
    _declare_global(lab, {"connections": {"personal": GLOBAL_BOX}})
    _declare_project(lab, {"connections": {"personal": {"enabled": True}}})
    report = json.loads(_run(lab, "connections").stdout)
    assert report["connections"]["personal"]["allow_write"] is True   # WRITE_DEFAULT


# --- what the report shows, and what selection refuses -----------------------

def test_a_withheld_connection_stays_out_of_the_report(lab):
    _declare_global(lab, {"connections": {"personal": GLOBAL_BOX}})
    _declare_project(lab, {"default": "local", "connections": {"local": LOCAL_BOX}})
    report = json.loads(_run(lab, "connections").stdout)
    assert set(report["connections"]) == {"local"}


def test_asking_for_a_withheld_connection_by_name_is_refused(lab):
    _declare_global(lab, {"connections": {"personal": GLOBAL_BOX}})
    _declare_project(lab, {"default": "local", "connections": {"local": LOCAL_BOX}})
    result = _run(lab, "--connection", "personal", "servers")
    assert result.returncode == 4, result.stdout + result.stderr
    error = _error(result)
    assert error["code"] == "connection_not_granted"
    assert "capabilities set coolify grant personal" in error["hint"]


def test_a_connection_this_project_switched_off_is_refused_as_such(lab):
    """The same gate, told apart in the message: nothing to grant from
    elsewhere, just a decision this project already made."""
    _declare_project(lab, {"default": "local",
                           "connections": {"local": LOCAL_BOX,
                                           "old": {**LOCAL_BOX, "enabled": False}}})
    result = _run(lab, "--connection", "old", "servers")
    assert result.returncode == 4, result.stdout + result.stderr
    error = _error(result)
    assert error["code"] == "connection_not_granted"
    assert "disabled in this project" in error["message"]


def test_a_default_pointing_at_a_withheld_connection_is_refused(lab):
    """Not "unknown connection": the pointer is right and the permission is
    missing, and an agent told the wrong one will go and invent a connection."""
    _declare_global(lab, {"default": "personal",
                          "connections": {"personal": GLOBAL_BOX}})
    _declare_project(lab, {"connections": {"local": LOCAL_BOX}})
    result = _run(lab, "servers")
    assert result.returncode == 4, result.stdout + result.stderr
    assert _error(result)["code"] == "connection_not_granted"
