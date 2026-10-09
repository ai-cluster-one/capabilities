"""A service row's verdict in `capabilities inventory`.

Each capability that ships a service answers for it in its own inventory
report: its state, and the verdict of the service's local checks. The manager
carries that verdict on the row as given, and reports `ok: null` wherever the
capability gave none, so a reader colours every service without knowing any
of them and never reads silence as health.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"



def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_inventory_under_test", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _install_fixture(registry: Path, name: str, service: dict | None) -> None:
    """A registry entry whose script answers `inventory` with the given
    service object, and declares a service so the survey asks it."""
    entry = registry / name
    entry.mkdir(parents=True)
    report = {"capability": name, "project": None, "network": False,
              "metrics": [], "items": [], "service": service, "deferred": []}
    script = entry / name
    script.write_text("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(report) + "\nEOF\n")
    script.chmod(0o755)
    (entry / "meta.json").write_text("{}")
    (entry / "manifest.json").write_text(json.dumps({
        "name": name, "summary": f"{name} fixture",
        "service": {"name": "fixture", "summary": "Fixture service.", "verbs": ["doctor"]},
        "inventory": {"network": "none"}}))


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


def test_inventory_carries_each_service_verdict_as_the_capability_gave_it(tmp_path):
    services = {
        "healthy": {"state": "running", "detail": "pid 1", "ok": True},
        "ailing": {"state": "running", "detail": "pid 2", "ok": False,
                   "problem": "config_stale: the daemon runs an older declaration"},
        "idle": {"state": "stopped", "ok": True},
        "silent": {"state": "running", "detail": "pid 3"},
    }
    root, env = _project(tmp_path, enabled=sorted(services))
    for name, service in services.items():
        _install_fixture(tmp_path / "registry", name, service)
    result = subprocess.run([str(MANAGER), "inventory"], cwd=root, env=env,
                            text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    rows = {row["name"]: row["service"]
            for row in json.loads(result.stdout)["capabilities"]}

    assert {k: rows["healthy"][k] for k in ("state", "ok", "problem")} == {
        "state": "running", "ok": True, "problem": None}
    assert {k: rows["ailing"][k] for k in ("state", "ok", "problem")} == {
        "state": "running", "ok": False,
        "problem": "config_stale: the daemon runs an older declaration"}
    assert {k: rows["idle"][k] for k in ("state", "ok")} == {"state": "stopped", "ok": True}
    # A capability that gives no verdict is reported as giving none.
    assert {k: rows["silent"][k] for k in ("state", "ok", "problem")} == {
        "state": "running", "ok": None, "problem": None}


def test_a_service_that_cannot_answer_has_no_verdict():
    row = M._inventory_service_state({"error": {"code": "x", "message": "it exited 1"}})
    assert row == {"state": "unknown", "detail": "it exited 1", "ok": None, "problem": None}


def test_the_audit_holds_a_given_verdict_to_its_shape():
    def problems(service):
        report = {"capability": "fix", "project": None, "network": False,
                  "metrics": [], "items": [], "service": service, "deferred": []}
        return M._validate_inventory("fix", json.dumps(report), {"service": {"name": "s"}})

    assert problems({"state": "running"}) == []
    assert problems({"state": "running", "ok": True}) == []
    assert problems({"state": "running", "ok": False, "problem": "it is stale"}) == []
    assert problems({"state": "running", "ok": "yes"}) != []
    assert problems({"state": "running", "ok": False}) != []
