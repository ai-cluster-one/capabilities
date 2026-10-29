"""An update moves a machine off deployment's runner before it replaces it.

`capabilities update` replaces deployment's payload with one that no longer
has `deployment runner` only once nothing here runs it: on a Mac whose node
manager agent is not loaded while launchd holds deployment's old agents, it
first runs `node install --apply`; otherwise, or where that does not move
everything, deployment keeps the payload it has and the update says why. The
manager is loaded as a module and the machine around it - launchd, `ps`, the
node install - is stood in for.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import plistlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
OLD = "USAGE\n  deployment runner <name> [--every SECONDS] -- <command...>\n"
NEW = "USAGE\n  deployment handover [--dry-run]\n"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_runner_move", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """An installed deployment that has the runner, a staged one that has not,
    and a machine whose launchd, processes and node install the test sets."""
    installed = tmp_path / "registry" / "deployment" / "deployment"
    installed.parent.mkdir(parents=True)
    installed.write_text(OLD)
    staged = tmp_path / "stage" / "deployment"
    staged.parent.mkdir()
    staged.write_text(NEW)
    world = {"mac": True, "node_loaded": False, "agents": [[{"label": "capabilities.machine.x",
                                                              "kind": "machine"}]],
             "processes": [[]], "installs": [], "install_answer": {"applied": True},
             "root": tmp_path / "agent"}
    monkeypatch.setattr(M, "_script_path", lambda name: installed)
    monkeypatch.setattr(M.shutil, "which",
                        lambda name: "/bin/launchctl" if name == "launchctl" and world["mac"]
                        else None)
    monkeypatch.setattr(M, "_launchd_loaded",
                        lambda label: label == M.NODE_AGENT_LABEL and world["node_loaded"])

    def agents():
        return world["agents"][0] if len(world["agents"]) == 1 else world["agents"].pop(0)

    def processes():
        return world["processes"][0] if len(world["processes"]) == 1 else \
            world["processes"].pop(0)

    def install(root, *argv):
        world["installs"].append((root, argv))
        answer = world["install_answer"]
        return (answer, "") if answer is not None else (None, "handover_incomplete")

    monkeypatch.setattr(M, "_runner_agents", agents)
    monkeypatch.setattr(M, "_runner_processes", processes)
    monkeypatch.setattr(M, "_node_install_manager", install)
    monkeypatch.setattr(M, "_project_root", lambda: world["root"])
    monkeypatch.setattr(M, "RUNNER_STOP_WAIT_SECONDS", 2)
    world["staged"] = staged
    world["installed"] = installed
    return world


def test_a_mac_with_old_agents_and_no_node_manager_is_moved_before_the_replace(machine):
    machine["agents"] = [[{"label": "capabilities.machine.x", "kind": "machine"}], []]
    kept, moved = M._runner_move(machine["staged"])
    assert kept is None
    assert moved == {"ok": True, "folder": str(machine["root"]),
                     "node_install": {"applied": True}}
    assert machine["installs"] == [(machine["root"], ("node", "install", "--apply"))]


def test_a_runner_its_agent_stopped_is_waited_for(machine):
    machine["agents"] = [[{"label": "lab.x", "kind": "host"}], []]
    machine["processes"] = [[{"pid": 7, "args": "deployment runner x"}], []]
    kept, moved = M._runner_move(machine["staged"])
    assert kept is None and moved["ok"] is True


def test_a_move_that_fails_keeps_the_old_deployment_and_says_what_moves_it(machine):
    machine["install_answer"] = None
    kept, moved = M._runner_move(machine["staged"])
    assert moved is None
    assert kept["move"]["ok"] is False
    assert kept["move"]["error"] == {"code": "node_install_failed",
                                     "message": "handover_incomplete"}
    assert "keeps the payload it has" in kept["reason"]
    assert "capabilities node install --apply" in kept["next"]


def test_agents_left_after_the_move_keep_the_old_deployment(machine):
    left = [{"label": "capabilities.machine.y", "kind": "machine"}]
    machine["agents"] = [[{"label": "capabilities.machine.x", "kind": "machine"}], left]
    kept, _moved = M._runner_move(machine["staged"])
    assert kept["agents"] == left and kept["move"]["ok"] is True


def test_with_the_node_manager_loaded_old_agents_are_not_moved_but_kept(machine):
    machine["node_loaded"] = True
    kept, moved = M._runner_move(machine["staged"])
    assert moved is None and machine["installs"] == []
    assert kept["agents"] == [{"label": "capabilities.machine.x", "kind": "machine"}]


def test_a_server_still_running_the_runner_keeps_the_old_deployment(machine):
    machine["mac"] = False
    machine["agents"] = [[]]
    machine["processes"] = [[{"pid": 7, "args": "/x/python /x/deployment runner tasks"}]]
    kept, moved = M._runner_move(machine["staged"])
    assert moved is None and machine["installs"] == []
    assert "Supervisor" in kept["reason"] or "supervisor" in kept["reason"]
    assert "agent-node image" in kept["next"]


def test_nothing_is_asked_where_the_runner_stays_or_was_gone_already(machine):
    machine["staged"].write_text(OLD)
    assert M._runner_move(machine["staged"]) == (None, None)
    machine["staged"].write_text(NEW)
    machine["installed"].write_text(NEW)
    assert M._runner_move(machine["staged"]) == (None, None)
    machine["installed"].write_text(OLD)
    machine["agents"], machine["processes"] = [[]], [[]]
    assert M._runner_move(machine["staged"]) == (None, None)
    assert machine["installs"] == []


def test_the_old_agents_are_found_by_what_they_run(tmp_path, monkeypatch):
    """A machine agent and an agent whose launcher runs `deployment runner`
    or a watchdog are deployment's; a body-sync agent and the node manager's
    own are not, and an agent launchd does not hold is not either."""
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def agent(label, launcher_text):
        launcher = tmp_path / f"{label}-launcher"
        launcher.write_text(launcher_text)
        (agents / f"{label}.plist").write_bytes(plistlib.dumps(
            {"Label": label, "ProgramArguments": [str(launcher)]}))

    agent("capabilities.machine.tasks", "#!/bin/sh\nexec /x/tasks service run --machine\n")
    agent("lab.tasks", "#!/bin/sh\nexec /x/deployment runner tasks --every 60 -- tasks\n")
    agent("lab.watchdog", "#!/bin/sh\nexec /x/deployment watchdog\n")
    agent("lab.node-sync-production", "#!/bin/sh\nexec deployment node sync --target p\n")
    agent("capabilities.node", "#!/bin/sh\nexec capabilities node run --launchd\n")
    agent("lab.unloaded", "#!/bin/sh\nexec /x/deployment runner coolify\n")
    monkeypatch.setattr(M.shutil, "which", lambda name: "/bin/launchctl")
    monkeypatch.setattr(M, "_launchd_loaded", lambda label: label != "lab.unloaded")
    found = {entry["label"]: entry["kind"] for entry in M._runner_agents()}
    assert found == {"capabilities.machine.tasks": "machine", "lab.tasks": "host",
                     "lab.watchdog": "host"}


def test_a_runner_process_is_found_by_its_command_line():
    pattern = M.RUNNER_PROCESS
    assert pattern.search("/u/.cache/uv/python /u/.capabilities/deployment/deployment "
                          "runner tasks --every 60 -- tasks service run")
    assert pattern.search("deployment runner tasks")
    assert not pattern.search("/u/deployment handover")
    assert not pattern.search("capabilities node supervise tasks --agent x")
    assert os.getpid() not in [entry["pid"] for entry in M._runner_processes()]
