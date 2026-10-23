"""The shell profile builds the same box with no body at all.

What is proven here is what the box leaves out and what its boot does instead:
the image carries no project and no ContextKit, nothing arrives from Git, and
the entrypoint binds the empty project root to the store's agent by its id and
composes its context with the manager's own build.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


CAP_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = CAP_ROOT / "deployment" / "bin" / "deployment"
BUNDLE = Path("deployment/targets/production")


def _project(tmp_path: Path, contextkit: bool = False) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "capabilities").mkdir()
    (root / "capabilities" / "settings.json").write_text(
        json.dumps({"capabilities": {"deployment": {"enabled": True}}}) + "\n")
    if contextkit:
        (root / ".contextkit").mkdir()
        (root / ".contextkit" / "config.toml").write_text('version = 1\n')
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
    }
    env.pop("CAPABILITIES_PROJECT_ID", None)
    env.pop("CAPABILITIES_PROJECT_ID_ROOT", None)
    return root, env


def _setup(root: Path, env: dict[str, str]) -> None:
    proc = subprocess.run(
        [str(DEPLOYMENT), "setup", "--profile", "agent-box-shell", "--provider", "manual",
         "--force"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_image_carries_no_body_and_no_contextkit(tmp_path: Path) -> None:
    root, env = _project(tmp_path, contextkit=True)
    _setup(root, env)
    dockerfile = (root / BUNDLE / "Dockerfile").read_text()
    assert "COPY --chown=${USERNAME}:${USERNAME} . /app" not in dockerfile
    assert f"COPY --chown=${{USERNAME}}:${{USERNAME}} {BUNDLE}/entrypoint.sh /opt/agent/" in dockerfile
    assert "chown ${USERNAME}:${USERNAME} /opt/agent /app" in dockerfile
    assert 'ENTRYPOINT ["/opt/agent/entrypoint.sh"]' in dockerfile
    assert "contextkit" not in dockerfile.lower()
    assert "capabilities init" not in dockerfile
    ignore = (root / BUNDLE / "Dockerfile.dockerignore").read_text()
    assert "*" in ignore.splitlines()


def test_boot_binds_the_agent_by_id_and_builds_from_the_store(tmp_path: Path) -> None:
    root, env = _project(tmp_path, contextkit=True)
    _setup(root, env)
    entrypoint = (root / BUNDLE / "entrypoint.sh").read_text()
    init = entrypoint.index('capabilities init --id "$CAPABILITIES_PROJECT_ID"')
    hooks = entrypoint.index("capabilities hooks install --claude --codex")
    build = entrypoint.index("capabilities build")
    assert init < hooks < build
    # The id alone binds the folder: no ContextKit binding is laid for the manager.
    assert ".contextkit" not in entrypoint
    assert "contextkit build" not in entrypoint
    assert "contextkit init" not in entrypoint
    assert "git clone" not in entrypoint
    assert "AGENT_REPO_URL" not in entrypoint
    assert "AGENT_BODY_REBUILD_COMMAND" not in entrypoint


def test_compose_has_no_body_and_passes_the_id_through(tmp_path: Path) -> None:
    root, env = _project(tmp_path)
    _setup(root, env)
    compose = (root / BUNDLE / "docker-compose.yaml").read_text()
    assert "agent_body" not in compose
    assert "AGENT_REPO_URL" not in compose
    assert "CAPABILITIES_PROJECT_ID: ${CAPABILITIES_PROJECT_ID:-}" in compose
    assert 'CAPABILITIES_PROJECT_ID_ROOT: "/app"' in compose
    assert not (root / BUNDLE / "body-sync.sh").exists()
