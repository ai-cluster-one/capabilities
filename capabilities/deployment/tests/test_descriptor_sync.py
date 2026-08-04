from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


CAP_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = CAP_ROOT / "deployment" / "bin" / "deployment"
SERVICE_CAPABILITIES = ("telegram", "automations", "slack")


def _manifest(name: str) -> dict:
    script = CAP_ROOT / name / "bin" / name
    proc = subprocess.run(
        [str(script), "manifest", "--json"], capture_output=True,
        text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout)


def _project(tmp_path: Path, enabled: tuple[str, ...] = ()) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "capabilities").mkdir()
    caps = {"deployment": {"enabled": True}}
    caps.update({name: {"enabled": True} for name in enabled})
    (root / "capabilities" / "settings.json").write_text(
        json.dumps({"capabilities": caps}) + "\n"
    )
    registry = tmp_path / "registry"
    for name in SERVICE_CAPABILITIES:
        path = registry / name / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(_manifest(name)) + "\n")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(registry),
    }
    return root, env


def _run(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DEPLOYMENT), *args], cwd=root, env=env, capture_output=True,
        text=True, timeout=30,
    )


def _setup(root: Path, env: dict[str, str]) -> dict:
    proc = _run(root, env, "setup", "--provider", "manual", "--force")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_auto_discovers_only_explicit_project_services(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram", "automations"))
    result = _setup(root, env)
    assert result["services"] == ["automations", "telegram"]
    runtime = json.loads((root / "deployment" / "runtime.json").read_text())
    assert runtime["services"]["telegram"]["command"] == ["telegram", "service", "run"]
    assert runtime["services"]["automations"]["restart"] == "unless-stopped"


def test_explicit_disable_and_enable_overrides(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram", "automations"))
    _setup(root, env)
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["service_policy"] = {
        "auto_include": False,
        "capabilities": {"telegram": "enabled", "automations": "disabled"},
    }
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    proc = _run(root, env, "sync")
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["services"] == ["telegram"]
    compose = (root / "docker-compose.yaml").read_text()
    assert "project-telegram" in compose
    assert "project-automations" not in compose


def test_global_enable_is_not_service_authorization(tmp_path: Path) -> None:
    root, env = _project(tmp_path)
    global_gate = Path(env["XDG_CONFIG_HOME"]) / "capabilities" / "settings.json"
    global_gate.parent.mkdir(parents=True)
    global_gate.write_text(json.dumps({"capabilities": {"telegram": {"enabled": True}}}))
    result = _setup(root, env)
    assert "telegram" not in result["services"]


def test_slack_requires_bot_and_app_tokens(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("slack",))
    result = _setup(root, env)
    assert result["services"] == ["slack"]
    compose = (root / "docker-compose.yaml").read_text()
    env_example = (root / ".env.example").read_text()
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        assert f"{key}: ${{{key}:?{key} is required}}" in compose
        assert f"{key}=" in env_example


def test_restart_no_is_rendered_as_a_yaml_string(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    manifest_path = Path(env["CAPABILITIES_HOME"]) / "telegram" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["service"]["deploy"]["restart"] = "no"
    manifest_path.write_text(json.dumps(manifest) + "\n")
    _setup(root, env)
    assert 'restart: "no"' in (root / "docker-compose.yaml").read_text()


def test_stale_service_directory_is_ignored(tmp_path: Path) -> None:
    root, env = _project(tmp_path)
    (root / "capabilities" / "telegram" / "service").mkdir(parents=True)
    result = _setup(root, env)
    assert "telegram" not in result["services"]
    doctor = _run(root, env, "doctor")
    payload = json.loads(doctor.stdout)
    assert any("stale service directory" in f["message"] for f in payload["findings"])


@pytest.mark.parametrize("path", [
    "deployment/capabilities.lock",
    "deployment/runtime.json",
    "docker-compose.yaml",
])
def test_check_reports_closure_drift_without_writing(tmp_path: Path, path: str) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env)
    target = root / path
    if path.endswith("runtime.json"):
        data = json.loads(target.read_text())
        data["services"]["telegram"]["command"] = ["wrong"]
        target.write_text(json.dumps(data, indent=2) + "\n")
    else:
        target.write_text(target.read_text() + "\n# drift\n")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    proc = _run(root, env, "sync", "--check")
    assert proc.returncode == 7
    payload = json.loads(proc.stdout)
    assert any(item["path"] == path for item in payload["drift"])
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after


def test_legacy_runtime_preserves_existing_service_on_migration(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env)
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime.pop("service_policy")
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    proc = _run(root, env, "sync")
    assert proc.returncode == 0, proc.stderr
    migrated = json.loads(runtime_path.read_text())
    assert migrated["service_policy"]["capabilities"] == {"telegram": "enabled"}
    assert "telegram" in migrated["services"]


def test_local_compose_overlay_is_preserved(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("automations",))
    _setup(root, env)
    overlay = root / "docker-compose.override.yaml"
    content = "services:\n  agent:\n    environment:\n      LOCAL_ONLY: yes\n"
    overlay.write_text(content)
    proc = _run(root, env, "sync")
    assert proc.returncode == 0, proc.stderr
    assert overlay.read_text() == content


def test_enabled_override_cannot_bypass_project_gate(tmp_path: Path) -> None:
    root, env = _project(tmp_path)
    _setup(root, env)
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["service_policy"]["capabilities"]["telegram"] = "enabled"
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    proc = _run(root, env, "sync", "--check")
    assert proc.returncode == 7
    payload = json.loads(proc.stdout)
    assert any("not explicitly project-enabled" in f["message"] for f in payload["findings"])


def test_unmarked_owned_path_is_preserved(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    custom = root / "docker-compose.yaml"
    custom.write_text("services:\n  local:\n    image: local\n")
    before = custom.read_text()
    proc = _run(root, env, "setup", "--provider", "manual")
    assert proc.returncode == 6
    assert custom.read_text() == before
    assert not (root / "deployment" / "runtime.json").exists()
