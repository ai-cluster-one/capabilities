"""Which directory a manager command answers as its project.

The walk up from the cwd decides it, and two trees along the way must never win.
A home directory is not a project: the machine registry sits in one, and a
caller free to point $HOME anywhere would otherwise be free to make a home a
project. `.capabilities/` is both the legacy project envelope and the registry's
own name, so the name alone does not settle which of the two a tree is.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_project_root", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _env(tmp_path: Path) -> dict[str, str]:
    """A manager that answers out of the walk alone: no handoff, no host hint."""
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "lane-home"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    })
    for key in ("CLAUDE_PROJECT_DIR", "CAPABILITIES_PROJECT_ENVELOPE",
                "CAPABILITIES_PROJECT_ENVELOPE_ROOT"):
        env.pop(key, None)
    return env


def _path(tmp_path: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MANAGER), "path", "--json"],
        cwd=cwd, env=_env(tmp_path), text=True, capture_output=True, timeout=60,
    )


def _stderr_error(result: subprocess.CompletedProcess[str]) -> dict:
    line = next(line for line in reversed(result.stderr.splitlines())
                if line.lstrip().startswith("{"))
    return json.loads(line)["error"]


def test_a_legacy_envelope_names_a_project_and_a_machine_registry_does_not(
        tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inside = outer / "inside"
    inside.mkdir(parents=True)
    envelope = outer / ".capabilities"
    envelope.mkdir()

    as_envelope = _path(tmp_path, inside)
    (envelope / ".manager").mkdir()
    as_registry = _path(tmp_path, inside)

    # The first answer is what proves the walk reaches `outer` at all, so the
    # second one can only be the registry being passed over.
    assert as_envelope.returncode == 0, as_envelope.stderr
    assert json.loads(as_envelope.stdout)["project_root"] == str(outer)
    assert as_registry.returncode == 6, as_registry.stdout
    assert _stderr_error(as_registry)["code"] == "no_project"


def test_the_registry_a_manager_was_pointed_at_names_itself(
        tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inside = outer / "inside"
    inside.mkdir(parents=True)
    registry = outer / ".capabilities"
    registry.mkdir()
    env = _env(tmp_path)
    env["CAPABILITIES_HOME"] = str(registry)

    result = subprocess.run(
        [sys.executable, str(MANAGER), "path", "--json"],
        cwd=inside, env=env, text=True, capture_output=True, timeout=60,
    )

    assert result.returncode == 6, result.stdout
    assert _stderr_error(result)["code"] == "no_project"


def test_a_redirected_home_leaves_the_accounts_own_home_excluded(
        tmp_path: Path, monkeypatch) -> None:
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    lane_home = tmp_path / "lane-home"
    lane_home.mkdir()
    monkeypatch.setenv("HOME", str(lane_home))

    homes = M._home_dirs()

    assert lane_home.resolve() in homes
    assert account_home in homes


def test_a_home_field_that_names_no_directory_adds_no_home(
        tmp_path: Path, monkeypatch) -> None:
    """An empty or relative home field resolves against the cwd, an empty one to
    the cwd itself, so taken as a home it stops the walk wherever the process
    stands. Some service accounts carry an empty field; for them the guard is
    $HOME's alone and the walk finds the project it always found."""
    lane_home = tmp_path / "lane-home"
    lane_home.mkdir()
    project = tmp_path / "project"
    inside = project / "inside"
    inside.mkdir(parents=True)
    (project / ".git").mkdir()
    monkeypatch.setenv("HOME", str(lane_home))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(inside)

    for field in ("", ".", "..", "relative/home"):
        monkeypatch.setattr(
            M.pwd, "getpwuid",
            lambda _uid, field=field: SimpleNamespace(pw_dir=field))

        assert M._home_dirs() == {lane_home.resolve()}, field
        assert M._project_root() == project.resolve(), field
