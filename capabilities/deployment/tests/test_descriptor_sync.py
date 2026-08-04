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


def test_nested_layout_adoption_preserves_project_service_extensions(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    init = _run(root, env, "init", "--provider", "manual", "--force")
    assert init.returncode == 0, init.stderr

    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["compose_file"] = "deployment/compose.yaml"
    runtime["compiler"] = {
        "artifacts": {
            "dockerfile": "deployment/docker/Dockerfile",
            "entrypoint": "deployment/docker/entrypoint.sh",
            "env_example": ".env.example",
            "dockerignore": ".dockerignore",
        },
        "compose_overlays": ["deployment/compose.local.yaml"],
        "container": {"agent_home": "/home/jess", "project_root": "/app"},
    }
    runtime["services"]["agent"].update({
        "role": "project-worker",
        "description": "Project-owned worker declaration.",
        "required_env": ["WORKER_SESSION"],
        "optional_env": ["AGENT_IMAGE", "WORKER_MODE"],
        "state": ["claude_state", "codex_state", "worker_state"],
        "project_extension": {"queue": "primary"},
    })
    runtime["services"]["telegram"] = {
        "capability": "telegram",
        "role": "project-channel",
        "compose_service": "telegram",
        "description": "Project-owned Telegram worker overrides.",
        "restart": "always",
        "required_env": ["PROJECT_CHANNEL_TOKEN"],
        "optional_env": ["PROJECT_CHANNEL_MODE"],
        "state": ["worker_state"],
        "project_extension": {"routing": "project"},
    }
    runtime["volumes"] = {
        "claude_state": {"kind": "shared", "mount": "/home/jess/.claude"},
        "codex_state": {"kind": "shared", "mount": "/home/jess/.codex"},
        "telegram_state": {"kind": "state", "mount": "/home/jess/.local/state/telegram"},
        "worker_state": {"kind": "shared", "mount": "/home/jess/.local/state/worker"},
    }
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    target_path = root / "deployment" / "targets" / "production.json"
    target = json.loads(target_path.read_text())
    target["resource"]["compose_file"] = "deployment/compose.yaml"
    target_path.write_text(json.dumps(target, indent=2) + "\n")

    owned = {
        root / "deployment" / "compose.yaml": "services: {}\n",
        root / "deployment" / "docker" / "Dockerfile": "FROM scratch\n",
        root / "deployment" / "docker" / "entrypoint.sh": "#!/bin/sh\n",
        root / ".env.example": "PROJECT_OWNED=1\n",
        root / ".dockerignore": "project-owned\n",
    }
    for path, content in owned.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    overlay = root / "deployment" / "compose.local.yaml"
    overlay_content = "services:\n  telegram:\n    environment:\n      LOCAL_ONLY: 'true'\n"
    overlay.write_text(overlay_content)
    (root / "capabilities" / "telegram" / "service").mkdir(parents=True)
    (root / ".env.local").write_text(
        "WORKER_SESSION=test\nPROJECT_CHANNEL_TOKEN=test\n"
        "TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=test\n"
    )

    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    check = _run(root, env, "sync", "--check")
    assert check.returncode == 7
    check_payload = json.loads(check.stdout)
    unmanaged = {item["path"]: item for item in check_payload["drift"]
                 if item["kind"] == "unmanaged_conflict"}
    assert set(unmanaged) == {
        ".dockerignore", ".env.example", "deployment/compose.yaml",
        "deployment/docker/Dockerfile", "deployment/docker/entrypoint.sh",
    }
    assert all("sync --adopt" in item["hint"] for item in unmanaged.values())
    assert not any(item["path"] in {"Dockerfile", "docker-compose.yaml", "entrypoint.sh"}
                   for item in check_payload["drift"])
    assert before == {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    adopt = _run(root, env, "sync", "--adopt")
    assert adopt.returncode == 0, adopt.stdout + adopt.stderr
    assert overlay.read_text() == overlay_content
    compiled = json.loads(runtime_path.read_text())
    agent = compiled["services"]["agent"]
    telegram = compiled["services"]["telegram"]
    assert agent["role"] == "project-worker"
    assert agent["project_extension"] == {"queue": "primary"}
    assert agent["required_env"] == ["WORKER_SESSION"]
    assert "worker_state" in agent["state"]
    assert telegram["role"] == "project-channel"
    assert telegram["description"] == "Project-owned Telegram worker overrides."
    assert telegram["restart"] == "always"
    assert telegram["project_extension"] == {"routing": "project"}
    assert set(telegram["required_env"]) == {
        "PROJECT_CHANNEL_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
    }
    assert "PROJECT_CHANNEL_MODE" in telegram["optional_env"]
    assert set(telegram["state"]) >= {
        "worker_state", "telegram_state", "claude_state", "codex_state",
    }
    assert all(volume["mount"].startswith("/home/jess")
               for volume in compiled["volumes"].values())

    compose = (root / "deployment" / "compose.yaml").read_text()
    dockerfile = (root / "deployment" / "docker" / "Dockerfile").read_text()
    assert 'context: ".."' in compose
    assert 'dockerfile: "deployment/docker/Dockerfile"' in compose
    assert "PROJECT_CHANNEL_TOKEN: ${PROJECT_CHANNEL_TOKEN:?PROJECT_CHANNEL_TOKEN is required}" in compose
    assert "TELEGRAM_API_ID: ${TELEGRAM_API_ID:?TELEGRAM_API_ID is required}" in compose
    assert "worker_state:/home/jess/.local/state/worker" in compose
    assert "ARG USERNAME=jess" in dockerfile
    assert "WORKDIR /app" in dockerfile
    assert "ENTRYPOINT [\"/app/deployment/docker/entrypoint.sh\"]" in dockerfile

    clean_check = _run(root, env, "sync", "--check")
    assert clean_check.returncode == 0, clean_check.stdout + clean_check.stderr
    doctor = _run(root, env, "doctor")
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    doctor_payload = json.loads(doctor.stdout)
    assert doctor_payload["drift"] == []
    assert overlay.read_text() == overlay_content
