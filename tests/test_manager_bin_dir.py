"""The link directory is the machine's, not the call's.

A call without `CAPABILITIES_BIN` - a service whose environment lacks the
shell's export, say - links where the manager's own link already sits, so
every install and self-update on one machine lands where `doctor` looks,
whatever environment each runs in. A machine an older manager left with the
manager linked in two directories gets every link in both, and a self-update
relinks both. Only a machine whose manager has no link falls back to the PATH
choice it always made.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
NAME = "bindirfix"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_bin_dir", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _lab(tmp_path: Path, manager_link_in: str | tuple[str, ...] | None) -> dict:
    """A home holding both default directories, with only `~/.local/bin` on
    PATH - the environment of a service the shell's export never reached."""
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    home_bin = home / "bin"
    for path in (local_bin, home_bin):
        path.mkdir(parents=True)
    registry = tmp_path / "registry"
    manager = registry / ".manager" / "capabilities"
    manager.parent.mkdir(parents=True)
    shutil.copy2(MANAGER, manager)
    if isinstance(manager_link_in, str):
        manager_link_in = (manager_link_in,)
    for directory in manager_link_in or ():
        (home / directory / "capabilities").symlink_to(manager)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CAPABILITIES_", "CLAUDE_"))}
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "CAPABILITIES_HOME": str(registry),
        "CAPABILITIES_MANAGER_BIN": str(MANAGER),
        "PATH": str(local_bin) + os.pathsep + os.environ.get("PATH", ""),
    })
    script = tmp_path / "cap" / NAME
    script.parent.mkdir()
    script.write_text(M._capability_skeleton(NAME, False))
    script.chmod(0o755)
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    return {"env": env, "local_bin": local_bin, "home_bin": home_bin,
            "script": script, "nowhere": nowhere, "registry": registry}


def _manager(lab: dict, *args: str, env: dict | None = None):
    return subprocess.run([str(MANAGER), *args], cwd=lab["nowhere"],
                          env=env or lab["env"], text=True,
                          capture_output=True, timeout=300)


def _symlink_findings(result) -> list[str]:
    return [f for f in json.loads(result.stdout)["findings"]
            if f.startswith(f"{NAME}:") and "symlink" in f]


def test_an_install_without_capabilities_bin_links_beside_the_manager(tmp_path):
    lab = _lab(tmp_path, manager_link_in="bin")

    install = _manager(lab, "install", NAME, "--from", str(lab["script"]), "--allow")

    assert install.returncode == 0, install.stderr
    assert (lab["home_bin"] / NAME).is_symlink()
    assert not (lab["local_bin"] / NAME).exists()
    shell = dict(lab["env"], CAPABILITIES_BIN=str(lab["home_bin"]))
    assert _symlink_findings(_manager(lab, "doctor", env=shell)) == []
    assert _symlink_findings(_manager(lab, "doctor")) == []

    uninstall = _manager(lab, "uninstall", NAME)
    assert uninstall.returncode == 0, uninstall.stderr
    assert json.loads(uninstall.stdout)["symlink_removed"] is True
    assert not (lab["home_bin"] / NAME).exists()


def test_a_self_update_without_capabilities_bin_relinks_the_manager_in_place(tmp_path):
    lab = _lab(tmp_path, manager_link_in="bin")
    release = _release(tmp_path)

    updated = _manager(lab, "self-update", "--from", str(release))

    assert updated.returncode == 0, updated.stderr
    link = lab["home_bin"] / "capabilities"
    assert link.resolve() == Path(json.loads(updated.stdout)["manager"])
    assert not (lab["local_bin"] / "capabilities").exists()


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "incoming"
    shutil.copytree(REPO / "contract", release / "contract")
    (release / "bin").mkdir()
    shutil.copy2(MANAGER, release / "bin" / "capabilities")
    assets = {rel: hashlib.sha256((release / rel).read_bytes()).hexdigest()
              for rel in ("contract/CHANGELOG.md",)}
    manifest = release / ".capability-source" / "manager-release.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({
        "schema": "capabilities.manager-release.v1",
        "manager": {"path": "bin/capabilities", "sha256": hashlib.sha256(
            (release / "bin" / "capabilities").read_bytes()).hexdigest()},
        "assets": assets,
    }, indent=2, sort_keys=True) + "\n")
    return release


def test_a_manager_linked_in_two_directories_gets_every_link_in_both(tmp_path):
    lab = _lab(tmp_path, manager_link_in=("bin", ".local/bin"))

    install = _manager(lab, "install", NAME, "--from", str(lab["script"]), "--allow")

    assert install.returncode == 0, install.stderr
    assert (lab["home_bin"] / NAME).is_symlink()
    assert (lab["local_bin"] / NAME).is_symlink()
    shell = dict(lab["env"], CAPABILITIES_BIN=str(lab["home_bin"]))
    assert _symlink_findings(_manager(lab, "doctor", env=shell)) == []
    assert _symlink_findings(_manager(lab, "doctor")) == []

    uninstall = _manager(lab, "uninstall", NAME)
    assert uninstall.returncode == 0, uninstall.stderr
    assert not (lab["home_bin"] / NAME).exists()
    assert not (lab["local_bin"] / NAME).exists()


def test_a_self_update_relinks_a_second_manager_link_left_behind(tmp_path):
    lab = _lab(tmp_path, manager_link_in="bin")
    older = lab["registry"] / ".manager" / "releases" / "older" / "bin" / "capabilities"
    older.parent.mkdir(parents=True)
    shutil.copy2(MANAGER, older)
    (lab["local_bin"] / "capabilities").symlink_to(older)
    release = _release(tmp_path)
    shell = dict(lab["env"], CAPABILITIES_BIN=str(lab["home_bin"]))

    updated = _manager(lab, "self-update", "--from", str(release), env=shell)

    assert updated.returncode == 0, updated.stderr
    report = json.loads(updated.stdout)
    manager = Path(report["manager"])
    assert (lab["home_bin"] / "capabilities").resolve() == manager
    assert (lab["local_bin"] / "capabilities").resolve() == manager
    assert report["also_relinked"] == [str(lab["local_bin"] / "capabilities")]

    install = _manager(lab, "install", NAME, "--from", str(lab["script"]), "--allow")
    assert install.returncode == 0, install.stderr
    assert _symlink_findings(_manager(lab, "doctor", env=shell)) == []


def test_a_manager_without_a_link_keeps_the_path_choice(tmp_path):
    lab = _lab(tmp_path, manager_link_in=None)

    install = _manager(lab, "install", NAME, "--from", str(lab["script"]), "--allow")

    assert install.returncode == 0, install.stderr
    assert (lab["local_bin"] / NAME).is_symlink()
    assert not (lab["home_bin"] / NAME).exists()


def test_capabilities_bin_still_decides_when_it_is_set(tmp_path):
    lab = _lab(tmp_path, manager_link_in="bin")
    chosen = tmp_path / "chosen"
    env = dict(lab["env"], CAPABILITIES_BIN=str(chosen),
               PATH=str(chosen) + os.pathsep + lab["env"]["PATH"])

    install = _manager(lab, "install", NAME, "--from", str(lab["script"]),
                       "--allow", env=env)

    assert install.returncode == 0, install.stderr
    assert (chosen / NAME).is_symlink()
    assert not (lab["home_bin"] / NAME).exists()
