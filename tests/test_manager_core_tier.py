"""The core tier on the manager's capability rows, and its verification.

Which capabilities are core is declared once, by the manager. `list` and
`inventory` carry it on every row as `tier`. The tier acts once, at a core
capability's first install from the official catalogue, which leaves it
allowed on the machine and enabled at global scope; from then on it is
enabled, granted and gated exactly as a standard one is.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import functools
import http.server
import json
import os
import subprocess
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"



def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_under_test", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()
CORE = sorted(M.CORE_CAPABILITIES)[0]
STANDARD = "slack"
assert STANDARD not in M.CORE_CAPABILITIES


def _install_fixture(registry: Path, name: str) -> None:
    """A registry entry as `list` and `inventory` read it: the script, its
    meta, and its manifest snapshot. Neither verb runs a capability that
    ships no service, so the script is never executed."""
    entry = registry / name
    entry.mkdir(parents=True)
    (entry / name).write_text("#!/bin/sh\nexit 1\n")
    (entry / "meta.json").write_text("{}")
    (entry / "manifest.json").write_text(json.dumps({
        "name": name, "summary": f"{name} fixture", "service": None,
        "inventory": None}))


def _project(tmp_path: Path, enabled: list[str]) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "consumer"
    envelope = root / "capabilities"
    envelope.mkdir(parents=True)
    project_id = str(uuid.uuid4())
    slug = "fixture-" + project_id[:8]
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": project_id, "slug": slug,
    }))
    (envelope / "settings.json").write_text(json.dumps(
        {"capabilities": {name: {"enabled": True} for name in enabled}}))
    env = dict(os.environ)
    env.update({
        "CLAUDE_PROJECT_DIR": str(root),
        "CAPABILITIES_PROJECT_ENVELOPE": str(envelope),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    })
    return root, env


def _manager(root: Path, env: dict[str, str], *args: str) -> dict:
    result = subprocess.run(
        [str(MANAGER), *args], cwd=root, env=env, text=True,
        capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_list_and_inventory_rows_carry_the_tier_and_nothing_else_moves(tmp_path):
    idle_core = sorted(M.CORE_CAPABILITIES)[-1]
    assert idle_core != CORE
    root, env = _project(tmp_path, enabled=[CORE, STANDARD])
    for name in (CORE, STANDARD, idle_core):
        _install_fixture(tmp_path / "registry", name)

    listed = {row["name"]: row for row in _manager(root, env, "list")["installed"]}
    assert listed[CORE]["tier"] == "core"
    assert listed[STANDARD]["tier"] == "standard"
    assert listed[idle_core]["tier"] == "core"
    for row in listed.values():
        assert set(row) == {"name", "summary", "tier", "project_gate",
                            "global_gate", "effective", "source", "machine",
                            "machine_connections"}
    # Core is a mark, not a grant: an unenabled core capability stays exactly
    # as gated as a standard one would be.
    assert listed[idle_core]["effective"] != "enabled"
    assert listed[idle_core]["project_gate"] == "absent"

    rows = {row["name"]: row
            for row in _manager(root, env, "inventory")["capabilities"]}
    assert set(rows) == {CORE, STANDARD}
    assert rows[CORE]["tier"] == listed[CORE]["tier"] == "core"
    assert rows[STANDARD]["tier"] == listed[STANDARD]["tier"] == "standard"
    for row in rows.values():
        assert set(row) == {"name", "summary", "tier", "gate", "connections",
                            "service", "inventory"}
        assert row["gate"] == {"effective": "enabled", "source": "project"}


def test_this_catalogue_carries_every_declared_core_name():
    assert M._core_declaration_failures(REPO) == []


def _tree(tmp_path: Path, declaration: str | None) -> Path:
    root = tmp_path / "tree"
    (root / "capabilities" / "demo" / "bin").mkdir(parents=True)
    (root / "capabilities" / "demo" / "bin" / "demo").write_text("#!/bin/sh\n")
    manager = MANAGER.read_text()
    if declaration is not None:
        lines = [line for line in manager.splitlines(keepends=True)
                 if not line.startswith("CORE_CAPABILITIES = ")]
        manager = "".join(lines) + f"\nCORE_CAPABILITIES = {declaration}\n"
    (root / "bin").mkdir()
    (root / "bin" / "capabilities").write_text(manager)
    return root


def test_verification_refuses_a_core_name_the_catalogue_does_not_carry(tmp_path):
    root = _tree(tmp_path, '("demo", "nosuch")')
    assert M._core_declaration_failures(root) == [
        "CORE_CAPABILITIES names 'nosuch', which is not in this catalogue"]

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test",
                    "-c", "user.email=test@example.com",
                    "commit", "-qm", "tree"], check=True)
    report = M._source_snapshot_report(root, "HEAD", audit=False)
    assert report["ok"] is False
    assert report["failures"]["core"] == [
        "CORE_CAPABILITIES names 'nosuch', which is not in this catalogue"]


def test_verification_reads_the_verified_tree_not_the_running_manager(tmp_path):
    assert M._core_declaration_failures(_tree(tmp_path / "a", '("demo",)')) == []
    assert M._core_declaration_failures(_tree(tmp_path / "b", '"demo"')) == [
        "CORE_CAPABILITIES must be a literal tuple of capability names"]
    bare = tmp_path / "c"
    (bare / "capabilities").mkdir(parents=True)
    assert M._core_declaration_failures(bare) == []


# --- arrival ----------------------------------------------------------------------

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


def _publish(root: Path, name: str) -> Path:
    bundle = root / "capabilities" / name
    script = bundle / "bin" / name
    script.parent.mkdir(parents=True)
    script.write_text(M._capability_skeleton(name, True))
    script.chmod(0o755)
    return bundle


@contextmanager
def _catalogue(root: Path, *names: str):
    """A stand-in for the official catalogue: CAPABILITIES_SOURCE pointed at a
    local server carrying capabilities/<name>/bin/<name> for each name."""
    for name in names:
        _publish(root, name)
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _machine_env(tmp_path: Path, source: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("CAPABILITIES_READ_ONLY", "CLAUDE_PROJECT_DIR",
                "CAPABILITIES_AUTH_CONTEXT", "CAPABILITIES_PROJECT_ENVELOPE",
                "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CAPABILITIES_PROJECT_ID",
                "CAPABILITIES_PROJECT_ID_ROOT", "AGENTKIT_DB_URL",
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
        "CAPABILITIES_SOURCE": source,
    })
    return env


def _outside(tmp_path: Path) -> Path:
    place = tmp_path / "home" / "nowhere"
    place.mkdir(parents=True, exist_ok=True)
    return place


def _machine(env: dict[str, str]) -> dict:
    path = Path(env["XDG_CONFIG_HOME"]) / "capabilities" / "machine.json"
    return json.loads(path.read_text())["capabilities"] if path.exists() else {}


def _global_policy(env: dict[str, str]) -> dict:
    path = Path(env["XDG_CONFIG_HOME"]) / "capabilities" / "settings.json"
    return json.loads(path.read_text())["capabilities"] if path.exists() else {}


def test_a_core_capability_arrives_allowed_and_enabled_globally(tmp_path):
    with _catalogue(tmp_path / "catalogue", CORE) as source:
        env = _machine_env(tmp_path, source)
        here = _outside(tmp_path)
        installed = _manager(here, env, "install", CORE)
    gate = str(tmp_path / "config" / "capabilities" / "settings.json")
    assert installed["machine"] == "allowed"
    assert "machine_hint" not in installed
    assert installed["global_policy"] == {"enabled": True, "gate": gate}
    assert _machine(env)[CORE]["state"] == "allowed"
    assert _global_policy(env) == {CORE: {"enabled": True}}
    row = {r["name"]: r for r in _manager(here, env, "list")["installed"]}[CORE]
    assert (row["tier"], row["machine"], row["global_gate"], row["effective"]) == (
        "core", "allowed", "enabled", "enabled")


def test_a_standard_capability_still_arrives_quarantined_with_no_policy(tmp_path):
    with _catalogue(tmp_path / "catalogue", STANDARD) as source:
        env = _machine_env(tmp_path, source)
        installed = _manager(_outside(tmp_path), env, "install", STANDARD)
    assert installed["machine"] == "quarantined"
    assert "global_policy" not in installed
    assert _machine(env)[STANDARD]["state"] == "quarantined"
    assert _global_policy(env) == {}


def test_a_core_name_from_another_source_arrives_as_any_capability(tmp_path):
    env = _machine_env(tmp_path, "http://127.0.0.1:9")
    bundle = _publish(tmp_path / "elsewhere", CORE)
    installed = _manager(_outside(tmp_path), env, "install", CORE, "--from", str(bundle))
    assert installed["machine"] == "quarantined"
    assert "global_policy" not in installed
    assert _global_policy(env) == {}


def test_arrival_writes_the_global_record_where_enable_global_would(tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "capabilities").mkdir()
    with _catalogue(tmp_path / "catalogue", CORE) as source:
        env = _machine_env(tmp_path, source)
        installed = _manager(project, env, "install", CORE)
    assert installed["global_policy"]["enabled"] is True
    assert _global_policy(env) == {CORE: {"enabled": True}}
    row = {r["name"]: r for r in _manager(project, env, "list")["installed"]}[CORE]
    assert (row["project_gate"], row["global_gate"], row["effective"]) == (
        "absent", "enabled", "enabled")


def test_the_users_later_choices_survive_reinstall_update_and_a_second_arrival(tmp_path):
    with _catalogue(tmp_path / "catalogue", CORE) as source:
        env = _machine_env(tmp_path, source)
        here = _outside(tmp_path)
        _manager(here, env, "install", CORE)
        _manager(here, env, "quarantine", CORE)
        _manager(here, env, "disable", CORE, "--global")
        reinstalled = _manager(here, env, "install", CORE)
        assert reinstalled["machine"] == "quarantined"
        assert "global_policy" not in reinstalled
        _manager(here, env, "update", CORE)
        assert _machine(env)[CORE]["state"] == "quarantined"
        assert _global_policy(env) == {CORE: {"enabled": False}}
        # Uninstalling drops the machine state but not the user's global
        # entry, so a second arrival is allowed and the disable is kept.
        _manager(here, env, "uninstall", CORE)
        again = _manager(here, env, "install", CORE)
    assert again["machine"] == "allowed"
    assert "global_policy" not in again
    assert _global_policy(env) == {CORE: {"enabled": False}}


def test_under_the_read_only_switch_a_core_capability_arrives_as_any_other(tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "capabilities").mkdir()
    with _catalogue(tmp_path / "catalogue", CORE) as source:
        env = _machine_env(tmp_path, source)
        env["CAPABILITIES_READ_ONLY"] = "1"
        for place in (_outside(tmp_path), project):
            installed = _manager(place, env, "install", CORE)
            assert installed["machine"] == "quarantined"
            assert "global_policy" not in installed
            _manager(place, {k: v for k, v in env.items()
                             if k != "CAPABILITIES_READ_ONLY"}, "uninstall", CORE)
    assert _global_policy(env) == {}


def test_an_arrival_that_cannot_write_the_global_record_leaves_nothing(tmp_path):
    with _catalogue(tmp_path / "catalogue", CORE) as source:
        env = _machine_env(tmp_path, source)
        (tmp_path / "config" / "capabilities" / "settings.json").mkdir(parents=True)
        result = subprocess.run(
            [str(MANAGER), "install", CORE], cwd=_outside(tmp_path), env=env,
            text=True, capture_output=True, timeout=120)
    assert result.returncode != 0
    assert CORE not in _machine(env)
    registry = tmp_path / "registry"
    assert not registry.exists() or not any(registry.iterdir())
