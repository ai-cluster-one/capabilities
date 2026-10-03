"""The core tier on the manager's capability rows, and its verification.

Which capabilities are core is declared once, by the manager. `list` and
`inventory` carry it on every row as `tier`, and it moves no gate: a core
capability is enabled, granted and gated exactly as a standard one is.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"

sys.path.insert(0, str(REPO / "contract"))
import store as S  # noqa: E402


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
        "schema": "capabilities.project.v1", "id": project_id,
        "slug": slug, "store": "db",
    }))
    store_path = tmp_path / "store.db"
    with S.SQLiteStore.open(str(store_path)) as store:
        store.migrate()
        store.project_register(project_id, slug)
        for name in enabled:
            store.config_set("capabilities", "policy", name,
                             {"enabled": True}, ("project", slug))
    env = dict(os.environ)
    env.update({
        "CLAUDE_PROJECT_DIR": str(root),
        "CAPABILITIES_PROJECT_ENVELOPE": str(envelope),
        "CAPABILITIES_STORE_URL": str(store_path),
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
