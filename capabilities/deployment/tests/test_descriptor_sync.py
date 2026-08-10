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


def test_embedded_service_merges_contract_into_agent(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram", "automations"))
    _setup(root, env)
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["service_policy"] = {
        "auto_include": False,
        "capabilities": {"telegram": "embedded", "automations": "disabled"},
    }
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")

    proc = _run(root, env, "sync")
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["services"] == []
    assert result["embedded_services"] == ["telegram"]

    compiled = json.loads(runtime_path.read_text())
    agent = compiled["services"]["agent"]
    assert agent["embedded_services"] == ["telegram"]
    assert "TELEGRAM_API_HASH" in agent["required_env"]
    assert "TELEGRAM_API_ID" in agent["optional_env"]
    assert set(agent["state"]) >= {"telegram_state", "claude_state", "codex_state"}
    assert "telegram" not in compiled["services"]

    compose = (root / "docker-compose.yaml").read_text()
    assert "project-telegram" not in compose
    assert 'TELEGRAM_API_HASH: "${TELEGRAM_API_HASH:-}"' in compose
    assert "telegram_state:/home/agent/.local/state/telegram" in compose


def test_runtime_capability_exclusions_only_trim_image_lock(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env)
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["service_policy"] = {
        "auto_include": False,
        "capabilities": {"telegram": "disabled"},
    }
    runtime["capabilities"]["exclude"] = ["deployment"]
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")

    proc = _run(root, env, "sync")
    assert proc.returncode == 0, proc.stderr
    lock = (root / "deployment" / "capabilities.lock").read_text().splitlines()
    assert "deployment" not in lock
    assert "telegram" in lock
    compiled = json.loads(runtime_path.read_text())
    assert compiled["capabilities"]["exclude"] == ["deployment"]


def test_runtime_cannot_exclude_an_embedded_service(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env)
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["service_policy"] = {
        "auto_include": False,
        "capabilities": {"telegram": "embedded"},
    }
    runtime["capabilities"]["exclude"] = ["telegram"]
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")

    proc = _run(root, env, "sync")
    assert proc.returncode == 6
    payload = json.loads(proc.stdout)
    assert any("cannot be both embedded and excluded" in item["message"]
               for item in payload["findings"])


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
        assert f'{key}: "${{{key}:-}}"' in compose
        assert f"{key}=" in env_example
    assert env_example.count("GIT_DEPLOY_KEY_B64=") == 1


def test_restart_no_is_rendered_as_a_yaml_string(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    manifest_path = Path(env["CAPABILITIES_HOME"]) / "telegram" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["service"]["deploy"]["restart"] = "no"
    manifest_path.write_text(json.dumps(manifest) + "\n")
    _setup(root, env)
    assert 'restart: "no"' in (root / "docker-compose.yaml").read_text()


def test_optional_defaults_are_scoped_to_each_service(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram", "automations"))
    for name, default in (("telegram", "telegram"), ("automations", "automations")):
        manifest_path = Path(env["CAPABILITIES_HOME"]) / name / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["service"]["deploy"]["environment"]["optional"].append(
            {"key": "SHARED_MODE", "default": default}
        )
        manifest_path.write_text(json.dumps(manifest) + "\n")

    result = _setup(root, env)
    assert result["services"] == ["automations", "telegram"]
    compose = (root / "docker-compose.yaml").read_text()
    assert compose.count('SHARED_MODE: "${SHARED_MODE:-automations}"') == 1
    assert compose.count('SHARED_MODE: "${SHARED_MODE:-telegram}"') == 1


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


def test_external_artifact_must_exist_and_is_not_created(tmp_path: Path) -> None:
    root, env = _project(tmp_path)
    init = _run(root, env, "init", "--provider", "manual", "--force")
    assert init.returncode == 0, init.stderr
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["compiler"]["artifacts"]["dockerfile"] = {
        "path": "deployment/docker/Dockerfile",
        "ownership": "external",
    }
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")

    proc = _run(root, env, "sync", "--adopt")
    assert proc.returncode == 6
    payload = json.loads(proc.stdout)
    assert any(finding["path"] == "deployment/docker/Dockerfile"
               and "does not exist" in finding["message"]
               for finding in payload["findings"])
    assert not (root / "deployment" / "docker" / "Dockerfile").exists()


def test_mixed_ownership_nested_layout_preserves_external_files_and_compose_semantics(
        tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram", "automations"))
    init = _run(root, env, "init", "--provider", "manual", "--force")
    assert init.returncode == 0, init.stderr

    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["compose_file"] = "deployment/compose.yaml"
    runtime["compiler"] = {
        "artifacts": {
            "dockerfile": {"path": "deployment/docker/Dockerfile", "ownership": "external"},
            "entrypoint": {"path": "deployment/docker/entrypoint.sh", "ownership": "external"},
            "env_example": {"path": ".env.example", "ownership": "external"},
            "dockerignore": {"path": ".dockerignore", "ownership": "external"},
        },
        "compose_overlays": ["deployment/compose.local.yaml"],
        "container": {"agent_home": "/home/jess", "project_root": "/app"},
    }
    runtime["services"]["agent"].update({
        "role": "project-worker",
        "description": "Project-owned worker declaration.",
        "required_env": ["WORKER_SESSION"],
        "optional_env": ["AGENT_IMAGE", "WORKER_MODE", "GOOGLE_API_KEY",
                         "MAILBOX_INTAKE_AGENT_RUNTIME", "MAILBOX_INTAKE_MAX_USD"],
        "environment_defaults": {
            "MAILBOX_INTAKE_AGENT_RUNTIME": "codex",
            "MAILBOX_INTAKE_MAX_USD": "10",
        },
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
        "environment_defaults": {
            "TELEGRAM_CHANNEL_ENABLED": "false",
            "TG_WORKER": "",
        },
        "state": ["worker_state"],
        "project_extension": {"routing": "project"},
    }
    runtime["services"]["automations"] = {
        "capability": "automations",
        "role": "project-scheduler",
        "compose_service": "automations",
        "description": "Project scheduler overrides.",
        "required_env": [],
        "optional_env": ["MAILBOX_INTAKE_AGENT_RUNTIME", "MAILBOX_INTAKE_MAX_USD"],
        "environment_defaults": {
            "AUTOMATIONS_NAMESPACE": "project",
            "MAILBOX_INTAKE_AGENT_RUNTIME": "codex",
            "MAILBOX_INTAKE_MAX_USD": "10",
        },
        "state": ["worker_state"],
    }
    runtime["volumes"] = {
        "claude_state": {"kind": "shared", "mount": "/home/jess/.claude"},
        "codex_state": {"kind": "shared", "mount": "/home/jess/.codex"},
        "telegram_state": {"kind": "state", "mount": "/home/jess/.local/state/telegram"},
        "automations_state": {"kind": "state", "mount": "/app/capabilities/automations/state"},
        "worker_state": {"kind": "shared", "mount": "/home/jess/.local/state/worker"},
    }
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    target_path = root / "deployment" / "targets" / "production.json"
    target = json.loads(target_path.read_text())
    target["resource"]["compose_file"] = "deployment/compose.yaml"
    target_path.write_text(json.dumps(target, indent=2) + "\n")

    owned = {
        root / "deployment" / "compose.yaml": "services: {}\n",
        root / "deployment" / "docker" / "Dockerfile": (
            "FROM debian:bookworm-slim\nARG CAPABILITIES_REF=1111111\n"
            "ARG CONTEXTKIT_REF=2222222\nARG CODEX_REF=rust-v0.139.0\n"
            "RUN mkdir -p /workspace/project capabilities/automations/state\n"
            "RUN telegram help >/dev/null\n"
        ),
        root / "deployment" / "docker" / "entrypoint.sh": (
            "#!/bin/sh\ngit config --global user.name 'Project Agent'\n"
            "git config --global user.email agent@example.invalid\nexec \"$@\"\n"
        ),
        root / ".env.example": (
            "# Project-owned documentation\nGOOGLE_API_KEY=\n"
            "MAILBOX_INTAKE_AGENT_RUNTIME=codex\nGIT_DEPLOY_KEY_B64=\n"
        ),
        root / ".dockerignore": (
            "expenses/\nmessages/\n*.log\n.claude/rules/generated/\n"
        ),
    }
    for path, content in owned.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    overlay = root / "deployment" / "compose.local.yaml"
    overlay_content = "services:\n  telegram:\n    environment:\n      LOCAL_ONLY: 'true'\n"
    overlay.write_text(overlay_content)
    (root / "capabilities" / "telegram" / "service").mkdir(parents=True)
    automation_config = root / "capabilities" / "automations" / "service" / "config.toml"
    automation_config.parent.mkdir(parents=True)
    automation_config.write_text("[automations]\n")
    (root / "capabilities" / "telegram" / "connections.json").write_text(
        json.dumps({"default": "main", "connections": {
            "main": {"api_id": 123456, "secret_env": "TELEGRAM_API_HASH",
                     "allow_write": True}
        }}) + "\n"
    )
    (root / ".env.local").write_text(
        "WORKER_SESSION=test\nPROJECT_CHANNEL_TOKEN=test\n"
        "TELEGRAM_API_HASH=test\n"
    )

    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    check = _run(root, env, "sync", "--check")
    assert check.returncode == 7
    check_payload = json.loads(check.stdout)
    unmanaged = {item["path"]: item for item in check_payload["drift"]
                 if item["kind"] == "unmanaged_conflict"}
    assert set(unmanaged) == {"deployment/compose.yaml"}
    assert all("sync --adopt" in item["hint"] for item in unmanaged.values())
    external_paths = {path.relative_to(root).as_posix() for path in owned
                      if path != root / "deployment" / "compose.yaml"}
    assert not any(item["path"] in external_paths
                   for item in check_payload["drift"])
    assert before == {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    adopt = _run(root, env, "sync", "--adopt")
    assert adopt.returncode == 0, adopt.stdout + adopt.stderr
    assert overlay.read_text() == overlay_content
    assert external_paths.isdisjoint({item["path"] for item in json.loads(adopt.stdout)["written"]})
    for path, content in owned.items():
        if path != root / "deployment" / "compose.yaml":
            assert path.read_text() == content
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
    assert set(telegram["required_env"]) == {"PROJECT_CHANNEL_TOKEN", "TELEGRAM_API_HASH"}
    assert set(telegram["optional_env"]) >= {"PROJECT_CHANNEL_MODE", "TELEGRAM_API_ID"}
    assert telegram["environment_defaults"] == {
        "TELEGRAM_CHANNEL_ENABLED": "false", "TG_WORKER": "",
    }
    assert set(telegram["state"]) >= {
        "worker_state", "telegram_state", "claude_state", "codex_state",
    }
    assert compiled["volumes"]["claude_state"]["mount"] == "/home/jess/.claude"
    assert compiled["volumes"]["codex_state"]["mount"] == "/home/jess/.codex"
    assert compiled["volumes"]["automations_state"]["mount"] == "/app/capabilities/automations/state"

    compose = (root / "deployment" / "compose.yaml").read_text()
    assert 'context: ".."' in compose
    assert 'dockerfile: "deployment/docker/Dockerfile"' in compose
    assert "      args:" not in compose
    assert "CAPABILITIES_REF" not in compose
    assert 'PROJECT_CHANNEL_TOKEN: "${PROJECT_CHANNEL_TOKEN:-}"' in compose
    assert 'TELEGRAM_API_ID: "${TELEGRAM_API_ID:-}"' in compose
    assert 'TELEGRAM_API_HASH: "${TELEGRAM_API_HASH:-}"' in compose
    assert 'TELEGRAM_CHANNEL_ENABLED: "${TELEGRAM_CHANNEL_ENABLED:-false}"' in compose
    assert 'TG_WORKER: "${TG_WORKER:-}"' in compose
    assert 'AUTOMATIONS_NAMESPACE: "${AUTOMATIONS_NAMESPACE:-project}"' in compose
    assert 'MAILBOX_INTAKE_AGENT_RUNTIME: "${MAILBOX_INTAKE_AGENT_RUNTIME:-codex}"' in compose
    assert 'MAILBOX_INTAKE_MAX_USD: "${MAILBOX_INTAKE_MAX_USD:-10}"' in compose
    assert 'GOOGLE_API_KEY: "${GOOGLE_API_KEY:-}"' in compose
    assert ":?" not in compose
    assert "worker_state:/home/jess/.local/state/worker" in compose
    assert "telegram_state:/home/jess/.local/state/telegram" in compose
    assert "automations_state:/app/capabilities/automations/state" in compose
    for volume_name in ("claude_state", "codex_state", "telegram_state",
                        "automations_state", "worker_state"):
        assert f"  {volume_name}:" in compose

    clean_check = _run(root, env, "sync", "--check")
    assert clean_check.returncode == 0, clean_check.stdout + clean_check.stderr
    doctor = _run(root, env, "doctor")
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    doctor_payload = json.loads(doctor.stdout)
    assert doctor_payload["drift"] == []
    assert not any("TELEGRAM_API_ID" in finding["message"]
                   for finding in doctor_payload["findings"])
    assert overlay.read_text() == overlay_content
