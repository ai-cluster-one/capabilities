#!/usr/bin/env python3
"""A manager and a capability never stay below each other's minimum.

The manager declares its level (`MANAGER_LEVEL`) and the minimum level it
requires of each capability (`MANAGER_REQUIRES`); a capability declares its
own `LEVEL` and the manager level it requires (`REQUIRES_MANAGER`), module
literals read without running the script, 0 where absent. `source index`
stamps them into the release manifest (level, requires) and the catalogue
(level, requires_manager). `install` and `update` refuse a payload the running
manager and it would leave below each other's minimum; `self-update` brings an
installed capability below a requirement of the release along, or refuses
before switching, or switches back. `self-update --check` and `doctor` answer
`compat`. A manifest or catalogue that names no levels behaves as before.

Levels are simulated: the managers are this one with its two literals
rewritten, and the capability is resend with literals appended; the sources
are trees served over HTTP from the test, and no store is configured.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import runpy
import shutil
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
MANIFEST_REL = ".capability-source/manager-release.json"
CATALOG_REL = ".capability-source/catalog.json"
BUNDLE = REPO / "capabilities" / "resend"
M = runpy.run_path(str(MANAGER), run_name="manager_compat_levels_test")
LEVEL_LINE = "MANAGER_LEVEL = 1\n"
REQUIRES_LINE = 'MANAGER_REQUIRES: dict = {"teams": 1}\n'
COMPAT_KEYS = {"manager_level", "unmet", "update_needed", "next"}
UNMET_KEYS = {"name", "needs", "installed", "required", "required_by", "command"}


# --- the machine, the sources and the managers -----------------------------------

def _env(tmp: Path) -> dict[str, str]:
    home = tmp / "home"
    bin_dir = tmp / "bin"
    for path in (home, bin_dir):
        path.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AGENTKIT_DB_")}
    env.update({
        "HOME": str(home),
        "CAPABILITIES_HOME": str(home / ".capabilities"),
        "CAPABILITIES_BIN": str(bin_dir),
        "CAPABILITIES_SOURCE": "http://127.0.0.1:9/main",
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "XDG_DATA_HOME": str(tmp / "data"),
        "XDG_CACHE_HOME": str(tmp / "cache"),
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
    })
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def _call(env: dict[str, str], cwd: Path, *args: str, manager: Path = MANAGER
          ) -> subprocess.CompletedProcess:
    return subprocess.run([str(manager), *args], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=600)


def _run(env: dict[str, str], cwd: Path, *args: str, manager: Path = MANAGER) -> dict:
    proc = _call(env, cwd, *args, manager=manager)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _error(proc: subprocess.CompletedProcess) -> dict:
    line = next(line for line in reversed(proc.stderr.splitlines())
                if line.lstrip().startswith("{"))
    return json.loads(line)["error"]


@contextmanager
def _serve(root: Path):
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


def _manager_text(tag: str, level: int | None, requires: dict | None) -> bytes:
    """This manager at `level` requiring `requires`; for None, one whose
    literals are the ones it ships with."""
    text = MANAGER.read_text()
    assert text.count(LEVEL_LINE) == 1 and text.count(REQUIRES_LINE) == 1
    if level is not None:
        text = text.replace(LEVEL_LINE, f"MANAGER_LEVEL = {level}\n")
    if requires is not None:
        text = text.replace(REQUIRES_LINE, f"MANAGER_REQUIRES: dict = {requires!r}\n")
    return (text + f"\n# the {tag} manager\n").encode()


def _script(level: int | None = None, needs: int | None = None, tag: str = "") -> bytes:
    """resend declaring `LEVEL` and `REQUIRES_MANAGER` where given."""
    text = (BUNDLE / "bin" / "resend").read_text()
    if level is not None:
        text += f"\nLEVEL = {level}\n"
    if needs is not None:
        text += f"\nREQUIRES_MANAGER = {needs}\n"
    return (text + f"\n# {tag}\n").encode()


def _bundle(root: Path, script: bytes) -> Path:
    """A resend bundle at `root` carrying `script`."""
    shutil.copytree(BUNDLE, root)
    (root / "bin" / "resend").write_bytes(script)
    return root


def _tree(root: Path, tag: str, *, level: int | None = None, requires: dict | None = None,
          stamped: bool = True, capability: bytes | None = None,
          offered: tuple[int, int] | None = None) -> Path:
    """A source tree at `root`: a manager release at `level` requiring
    `requires` (its manifest naming neither where not `stamped`, as an older
    line's does), and, for a `capability` script, resend in its catalogue at
    the (level, requires_manager) `offered` - unstamped, no level at all.
    Returns the tree's manager, which runs with the tree's assets."""
    manager = _manager_text(tag, level, requires)
    files = {"bin/capabilities": manager}
    for rel in M["_MANAGER_RELEASE_ASSETS"]:
        files[rel] = (REPO / rel).read_bytes()
    files["manager/CHANGELOG.md"] = f"# capabilities - change log\n\n## {tag}\n".encode()
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body)
    (root / "bin" / "capabilities").chmod(0o755)
    manifest = {"schema": "capabilities.manager-release.v1",
                "manager": {"path": "bin/capabilities",
                            "sha256": hashlib.sha256(manager).hexdigest()},
                "assets": {rel: hashlib.sha256(body).hexdigest()
                           for rel, body in files.items() if rel != "bin/capabilities"}}
    if stamped:
        manifest["level"] = level if level is not None else M["MANAGER_LEVEL"]
        manifest["requires"] = requires or {}
    assert not M["_validate_manager_release_manifest"](manifest)
    (root / MANIFEST_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_REL).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if capability is not None:
        target = root / "capabilities" / "resend" / "bin" / "resend"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(capability)
        entry: dict = {"path": "capabilities/resend", "payload_sha256": "0" * 64}
        if offered is not None:
            entry.update({"level": offered[0], "requires_manager": offered[1]})
        (root / CATALOG_REL).write_text(json.dumps({"capabilities": {"resend": entry}}))
    return root / "bin" / "capabilities"


def _outgoing(tmp: Path, env: dict[str, str]) -> Path:
    """This manager, at the level it ships with, installed as a release with
    its assets and linked: the manager a self-update replaces."""
    tree = _tree(tmp / "outgoing", "outgoing")
    _run(env, tmp, "self-update", "--from", str(tree))
    link = Path(env["CAPABILITIES_BIN"]) / "capabilities"
    assert link.resolve().read_text().rstrip().endswith("# the outgoing manager")
    return link


def _install_canonical(tmp: Path, env: dict[str, str], base: str, script: bytes,
                       manager: Path = MANAGER) -> None:
    """resend carrying `script`, installed as from the canonical source."""
    _run(env, tmp, "install", "resend", "--from",
         str(_bundle(tmp / "installed-bundle", script)), manager=manager)
    meta_path = Path(env["CAPABILITIES_HOME"]) / "resend" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["source"] = f"{base}/main/capabilities/resend/bin/resend"
    meta_path.write_text(json.dumps(meta))


def _installed(env: dict[str, str]) -> Path:
    return Path(env["CAPABILITIES_HOME"]) / "resend" / "resend"


def _installed_levels(env: dict[str, str]) -> dict:
    return M["_levels_declared"](_installed(env).read_text())[0]


def _compat(env: dict[str, str], cwd: Path, manager: Path) -> dict:
    """The compat `doctor` answers, which exits 7 where it names a pair."""
    proc = _call(env, cwd, "doctor", manager=manager)
    report = json.loads(proc.stdout)
    compat = report["compat"]
    assert set(compat) == COMPAT_KEYS
    assert all(set(row) == UNMET_KEYS for row in compat["unmet"])
    level_findings = [f for f in report["findings"] if "level" in f]
    assert len(level_findings) == len(compat["unmet"])
    if compat["unmet"]:
        assert proc.returncode == 7 and report["ok"] is False
    return compat


# --- declarations ------------------------------------------------------------------

def test_levels_are_read_from_the_script_without_running_it() -> None:
    declared = M["_levels_declared"]
    assert declared("x = 1\n") == ({"LEVEL": 0, "REQUIRES_MANAGER": 0}, [])
    assert declared("LEVEL = 3\nREQUIRES_MANAGER: int = 2\n") == (
        {"LEVEL": 3, "REQUIRES_MANAGER": 2}, [])
    for bad in ("LEVEL = -1\n", "LEVEL = True\n", "LEVEL = '2'\n", "LEVEL = 1 + 1\n"):
        levels, problems = declared(bad)
        assert levels["LEVEL"] == 0
        assert problems == ["LEVEL must be a literal non-negative integer"]
    assert M["MANAGER_LEVEL"] == 1 and M["MANAGER_REQUIRES"] == {"teams": 1}


def test_install_refuses_a_level_that_is_not_a_literal(tmp_path: Path) -> None:
    env = _env(tmp_path)
    bundle = _bundle(tmp_path / "bundle", _script() + b"\nLEVEL = int('2')\n")
    proc = _call(env, tmp_path, "install", "resend", "--from", str(bundle))
    assert proc.returncode == 7
    error = _error(proc)
    assert error["code"] == "install_validation_failed"
    assert "LEVEL must be a literal non-negative integer" in error["hint"]
    assert not _installed(env).exists()


def test_the_manifest_and_the_catalogue_carry_levels() -> None:
    manifest = json.loads((REPO / MANIFEST_REL).read_text())
    catalog = json.loads((REPO / CATALOG_REL).read_text())
    assert manifest["level"] == M["MANAGER_LEVEL"]
    assert manifest["requires"] == M["MANAGER_REQUIRES"]
    assert catalog["capabilities"]
    for name, entry in catalog["capabilities"].items():
        script = REPO / "capabilities" / name / "bin" / name
        levels = M["_levels_declared"](script.read_text())[0]
        assert (entry["level"], entry["requires_manager"]) == (
            levels["LEVEL"], levels["REQUIRES_MANAGER"])


def test_a_tree_whose_manager_and_catalogue_do_not_meet_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "bin").mkdir(parents=True)
    manager = root / "bin" / "capabilities"
    capdir = root / "capabilities" / "x"
    (capdir / "bin").mkdir(parents=True)
    (capdir / "bin" / "x").write_text("LEVEL = 1\nREQUIRES_MANAGER = 2\n")
    entry = M["_catalog_entry_of"](capdir, "x")
    assert (entry["level"], entry["requires_manager"]) == (1, 2)
    catalog = {"capabilities": {"x": entry}}
    failures = M["_level_declaration_failures"]

    manager.write_text("MANAGER_LEVEL = 2\nMANAGER_REQUIRES: dict = {'x': 1}\n")
    assert failures(root, catalog) == []
    assert M["_tree_manager_levels"](M["_tree_declared"](root)) == {
        "level": 2, "requires": {"x": 1}}

    manager.write_text("MANAGER_LEVEL = 1\nMANAGER_REQUIRES: dict = {'x': 2, 'y': 1}\n")
    assert failures(root, catalog) == [
        "MANAGER_REQUIRES names x at level 2, and this catalogue offers level 1",
        "MANAGER_REQUIRES names y at level 1, which this catalogue does not carry",
        "x requires manager level 2, above this tree's MANAGER_LEVEL 1"]
    built, found = M["_stamp_catalog_levels"](root, {"x": entry}, {})
    assert built["capabilities"]["x"]["level"] == 1 and len(found["levels"]) == 3

    # A manager from before levels is level 0 and names nothing to stamp.
    manager.write_text("STORE_MAJOR = 4\n")
    assert M["_tree_manager_levels"](M["_tree_declared"](root)) == {}
    assert failures(root, catalog) == [
        "x requires manager level 2, above this tree's MANAGER_LEVEL 0"]
    (capdir / "bin" / "x").write_text("pass\n")
    assert failures(root, {"capabilities": {"x": M["_catalog_entry_of"](capdir, "x")}}) == []


def test_a_manifest_and_a_catalogue_without_levels_read_as_level_zero() -> None:
    assert M["_manifest_levels"]({}) == (0, {})
    assert M["_manifest_levels"]({"level": 2, "requires": {"x": 1}}) == (2, {"x": 1})
    assert M["_catalogue_levels"]({"capabilities": {"x": {}}}, "x") == {
        "LEVEL": 0, "REQUIRES_MANAGER": 0}
    assert M["_catalogue_levels"](None, "x") == {"LEVEL": 0, "REQUIRES_MANAGER": 0}
    base = {"schema": "capabilities.manager-release.v1",
            "manager": {"path": "bin/capabilities", "sha256": "0" * 64},
            "assets": {"SHEBANG.md": "0" * 64}}
    assert M["_validate_manager_release_manifest"](base) == []
    assert M["_validate_manager_release_manifest"]({**base, "level": 0, "requires": {"x": -1}}) \
        == ["release manifest level must be a positive integer",
            "release manifest requires must map capability names to non-negative "
            "integer levels"]


# --- install and update ------------------------------------------------------------

def test_an_older_manager_refuses_a_capability_needing_a_newer_one(tmp_path: Path) -> None:
    env = _env(tmp_path)
    proc = _call(env, tmp_path, "install", "resend", "--from",
                 str(_bundle(tmp_path / "needs-2", _script(1, 2, "needs-2"))))
    assert proc.returncode == 6
    error = _error(proc)
    assert error["code"] == "manager_too_old"
    assert "resend" in error["message"] and "level 2" in error["message"] \
        and "level 1" in error["message"]
    assert "capabilities self-update" in error["hint"]
    assert not _installed(env).exists()

    root = tmp_path / "src"
    _tree(root / "main", "main", capability=_script(1, 2, "main"), offered=(1, 2))
    with _serve(root) as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        _install_canonical(tmp_path, env, base, _script(tag="installed"))
        before = _installed(env).read_bytes()
        checked = _run(env, tmp_path, "update", "--check", "resend")
        named = _call(env, tmp_path, "update", "resend")
        every = _run(env, tmp_path, "update")
    row = checked["checked"][0]
    assert row["update_available"] is False
    assert row["refused"]["code"] == "manager_too_old"
    assert named.returncode == 6 and _error(named)["code"] == "manager_too_old"
    assert every["updated"] == [{"name": "resend", "changed": False,
                                 "refused": row["refused"]}]
    assert _installed(env).read_bytes() == before
    # Nothing installed is below anything: the refused payload never landed.
    assert _compat(env, tmp_path, MANAGER)["unmet"] == []


def test_a_newer_manager_refuses_a_capability_below_its_requirement(tmp_path: Path) -> None:
    env = _env(tmp_path)
    manager = _tree(tmp_path / "newer", "newer", level=2, requires={"resend": 1})
    old = _bundle(tmp_path / "old", _script(tag="old"))
    proc = _call(env, tmp_path, "install", "resend", "--from", str(old), manager=manager)
    assert proc.returncode == 6
    error = _error(proc)
    assert error["code"] == "capability_below_manager_requirement"
    assert "resend" in error["message"] and "level 1" in error["message"] \
        and "level 0" in error["message"]
    assert not _installed(env).exists()
    _run(env, tmp_path, "install", "resend", "--from",
         str(_bundle(tmp_path / "met", _script(1, 2, "met"))), manager=manager)
    assert _compat(env, tmp_path, manager) == {
        "manager_level": 2, "unmet": [], "update_needed": False, "next": []}


def test_compat_names_each_pair_installed_below_the_others_minimum(tmp_path: Path) -> None:
    """A capability installed by a newer manager, read by an older one; and an
    old capability read by a newer manager that requires more of it."""
    env = _env(tmp_path)
    newer = _tree(tmp_path / "newer", "newer", level=2)
    _run(env, tmp_path, "install", "resend", "--from",
         str(_bundle(tmp_path / "b", _script(0, 2, "b"))), manager=newer)
    assert _compat(env, tmp_path, MANAGER) == {
        "manager_level": 1,
        "unmet": [{"name": "resend", "needs": "manager", "installed": 1, "required": 2,
                   "required_by": "resend", "command": "capabilities self-update"}],
        "update_needed": True, "next": ["capabilities self-update"]}
    demanding = _tree(tmp_path / "demanding", "demanding", level=2, requires={"resend": 1})
    assert _compat(env, tmp_path, demanding) == {
        "manager_level": 2,
        "unmet": [{"name": "resend", "needs": "capability", "installed": 0, "required": 1,
                   "required_by": "capabilities", "command": "capabilities update resend"}],
        "update_needed": True, "next": ["capabilities update resend"]}


# --- self-update -------------------------------------------------------------------

@pytest.mark.parametrize("order", ["self-update-then-update", "update-then-self-update"])
def test_either_order_ends_with_no_unmet_pair(tmp_path: Path, order: str) -> None:
    env = _env(tmp_path)
    link = _outgoing(tmp_path, env)
    root = tmp_path / "src"
    _tree(root / "main", "main", level=2, requires={"resend": 1},
          capability=_script(1, 2, "main"), offered=(1, 2))
    with _serve(root) as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        _install_canonical(tmp_path, env, base, _script(tag="installed"))
        if order == "update-then-self-update":
            # The older manager refuses what needs the newer one, and keeps going.
            updated = _run(env, tmp_path, "update", manager=link)
            assert updated["updated"][0]["refused"]["code"] == "manager_too_old"
            assert _installed_levels(env)["LEVEL"] == 0
        checked = _run(env, tmp_path, "self-update", "--check", manager=link)
        assert set(checked["compat"]) == COMPAT_KEYS | {"available"}
        assert checked["compat"]["manager_level"] == 1
        assert checked["compat"]["unmet"] == []
        assert checked["compat"]["available"] == {"level": 2, "requires": {"resend": 1},
                                                  "brings": ["resend"]}
        result = _run(env, tmp_path, "self-update", manager=link)
        assert result["self_update"] == "updated"
        assert result["brought_capabilities"] == [
            {"name": "resend", "installed": 0, "required": 1, "level": 1}]
        assert link.resolve().read_text().rstrip().endswith("# the main manager")
        assert _installed_levels(env) == {"LEVEL": 1, "REQUIRES_MANAGER": 2}
        if order == "self-update-then-update":
            updated = _run(env, tmp_path, "update", manager=link)
            assert "refused" not in updated["updated"][0]
    assert _compat(env, tmp_path, link) == {
        "manager_level": 2, "unmet": [], "update_needed": False, "next": []}


@pytest.mark.parametrize("case", ["brings", "refuses", "rolls-back"])
def test_a_release_requiring_a_level_never_leaves_it_unmet(tmp_path: Path, case: str) -> None:
    """A release that drops a fallback declares `requires {resend: 2}`, and the
    machine has resend at level 1: self-update brings it, refuses before
    switching where the catalogue cannot meet it, or switches back where the
    catalogue promised a level the payload does not carry."""
    env = _env(tmp_path)
    link = _outgoing(tmp_path, env)
    before_manager = link.resolve()
    releases = Path(env["CAPABILITIES_HOME"]) / ".manager" / "releases"
    staged = sorted(releases.iterdir())
    offered = {"brings": (2, 0), "refuses": (1, 0), "rolls-back": (2, 0)}[case]
    served = _script(1, 0, "served") if case == "rolls-back" else _script(*offered, "served")
    root = tmp_path / "src"
    _tree(root / "main", "main", level=2, requires={"resend": 2},
          capability=served, offered=offered)
    with _serve(root) as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        _install_canonical(tmp_path, env, base, _script(1, 0, "installed"))
        before = _installed(env).read_bytes()
        checked = _run(env, tmp_path, "self-update", "--check", manager=link)
        proc = _call(env, tmp_path, "self-update", manager=link)
    available = checked["compat"]["available"]
    if case == "refuses":
        assert "brings" not in available
        assert "resend is installed at level 1" in available["refused"]
        assert proc.returncode == 6
        error = _error(proc)
        assert error["code"] == "manager_requirement_unmet"
        assert "resend is installed at level 1 and the release requires level 2" \
            in error["message"]
        assert sorted(releases.iterdir()) == staged
    elif case == "rolls-back":
        assert available["brings"] == ["resend"]
        assert proc.returncode == 6
        error = _error(proc)
        assert error["code"] == "required_capability_failed"
        assert "resend" in error["message"] and "level 2" in error["message"]
    if case == "brings":
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["brought_capabilities"] == [
            {"name": "resend", "installed": 1, "required": 2, "level": 2}]
        assert link.resolve() != before_manager
        assert _installed_levels(env)["LEVEL"] == 2
    else:
        assert link.resolve() == before_manager
        assert _installed(env).read_bytes() == before
    # Whichever manager is active, nothing installed is below its minimum.
    assert _compat(env, tmp_path, link)["unmet"] == []


def test_a_release_and_a_catalogue_without_levels_behave_as_before(tmp_path: Path) -> None:
    """An older line's manifest names no level and its catalogue none, and no
    catalogue is read at all where a release requires nothing."""
    env = _env(tmp_path)
    link = _outgoing(tmp_path, env)
    root = tmp_path / "src"
    _tree(root / "main", "main", stamped=False)
    with _serve(root) as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        _install_canonical(tmp_path, env, base, _script(tag="installed"))
        checked = _run(env, tmp_path, "self-update", "--check", manager=link)
        result = _run(env, tmp_path, "self-update", manager=link)
    assert checked["update_available"] is True
    assert checked["compat"]["available"] == {"level": 0, "requires": {}, "brings": []}
    assert result["self_update"] == "updated"
    assert "brought_capabilities" not in result
    assert link.resolve().read_text().rstrip().endswith("# the main manager")

    _tree(root / "main", "main", stamped=False, capability=_script(tag="newer"))
    with _serve(root) as base:
        env["CAPABILITIES_SOURCE"] = f"{base}/main"
        meta_path = Path(env["CAPABILITIES_HOME"]) / "resend" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["source"] = f"{base}/main/capabilities/resend/bin/resend"
        meta_path.write_text(json.dumps(meta))
        checked = _run(env, tmp_path, "update", "--check", "resend", manager=link)
        updated = _run(env, tmp_path, "update", "resend", manager=link)
    assert checked["checked"][0]["update_available"] is True
    assert "refused" not in checked["checked"][0]
    assert [(row["name"], row["changed"], "refused" in row) for row in updated["updated"]] \
        == [("resend", True, False)]
    assert _installed(env).read_text().rstrip().endswith("# newer")
