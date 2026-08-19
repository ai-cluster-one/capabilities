from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "bin" / "capabilities"


def _audit(name: str, script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(MANAGER), "audit", name, "--from", str(script)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )


def test_manager_accepts_deploy_descriptor_and_non_service_capability() -> None:
    service = _audit("automations", ROOT / "capabilities" / "automations" / "bin" / "automations")
    assert service.returncode == 0, service.stdout + service.stderr
    non_service = _audit("deployment", ROOT / "capabilities" / "deployment" / "bin" / "deployment")
    assert non_service.returncode == 0, non_service.stdout + non_service.stderr


def test_manager_rejects_unknown_deploy_schema(tmp_path: Path) -> None:
    bundle = tmp_path / "automations"
    shutil.copytree(ROOT / "capabilities" / "automations", bundle)
    script = bundle / "bin" / "automations"
    script.write_text(
        script.read_text().replace(
            '"schema": "capabilities.service.deploy.v1"',
            '"schema": "capabilities.service.deploy.v999"',
            1,
        )
    )
    proc = _audit("automations", script)
    assert proc.returncode == 7
    payload = json.loads(proc.stdout)
    assert any("service.deploy.schema" in failure for failure in payload["failures"])


def test_telegram_descriptor_matches_runtime_credentials_and_uses_portable_mounts() -> None:
    script = ROOT / "capabilities" / "telegram" / "bin" / "telegram"
    proc = subprocess.run(
        [str(script), "manifest", "--json"], cwd=ROOT,
        capture_output=True, text=True, timeout=30, check=True,
    )
    deploy = json.loads(proc.stdout)["service"]["deploy"]
    assert deploy["environment"]["required"] == ["TELEGRAM_API_HASH"]
    optional = {item["key"]: item for item in deploy["environment"]["optional"]}
    assert "TELEGRAM_API_ID" in optional
    assert "default" not in optional["TG_WORKER"]
    assert all(item["target"].startswith(("{agent_home}", "{project_root}"))
               for item in deploy["mounts"])


def _service_project(tmp_path: Path, *, with_deployment: bool) -> tuple[Path, dict]:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text(json.dumps({
        "capabilities": {"telegram": {"enabled": True}},
    }))
    registry = tmp_path / "registry" / "telegram"
    registry.mkdir(parents=True)
    (registry / "telegram").write_text("#!/bin/sh\n")
    (registry / "stub").write_text("Telegram CLI over a personal account.\n")
    (registry / "manifest.json").write_text(json.dumps({
        "docs": {"topics": []},
        "service": {"name": "assistant", "summary": "Telegram assistant daemon.",
                    "verbs": ["run", "start", "stop"]},
    }))
    (registry / "service").mkdir()
    if with_deployment:
        other = tmp_path / "registry" / "deployment"
        other.mkdir(parents=True)
        (other / "deployment").write_text("#!/bin/sh\n")
        (other / "stub").write_text("Project deployment standard.\n")
        (other / "manifest.json").write_text(json.dumps({"docs": {"topics": []}}))
    import os
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
    })
    return project, env


def _fragment(project: Path, env: dict) -> str:
    proc = subprocess.run(
        [str(MANAGER), "context", "--fragment"],
        cwd=project, env=env, text=True, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_a_declared_service_is_told_where_persistence_is_answered(tmp_path: Path) -> None:
    project, env = _service_project(tmp_path, with_deployment=True)
    fragment = _fragment(project, env)
    assert "- Service (`telegram service ...`)" in fragment
    assert "`deployment` capability's domain" in fragment
    assert "start at `deployment help`" in fragment


def test_the_pointer_is_honest_when_deployment_is_absent(tmp_path: Path) -> None:
    project, env = _service_project(tmp_path, with_deployment=False)
    fragment = _fragment(project, env)
    assert "`deployment` capability's domain" in fragment
    assert "it is not installed here" in fragment
    assert "deployment help" not in fragment


def test_a_capability_without_a_service_is_told_nothing(tmp_path: Path) -> None:
    project, env = _service_project(tmp_path, with_deployment=True)
    manifest = tmp_path / "registry" / "telegram" / "manifest.json"
    manifest.write_text(json.dumps({"docs": {"topics": []}}))
    fragment = _fragment(project, env)
    assert "- Service (" not in fragment
    assert "deployment" not in fragment
