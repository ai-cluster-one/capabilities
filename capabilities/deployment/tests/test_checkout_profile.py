"""The checkout profile builds the same box and fills it differently.

What is proven here is the difference itself: the image must not carry the
project, the body must be a mount rather than a layer, and the initialization
that needs a checkout must move to boot. The baked profile is asserted
alongside each of these, because the whole point of a separate profile is that
it left the existing one alone.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


CAP_ROOT = Path(__file__).resolve().parents[2]
SERVICE_CAPABILITIES = ("telegram", "automations")


def _script(name: str) -> Path:
    root = CAP_ROOT / name
    return next((path for path in (root / "bin" / name, root / name)
                 if path.is_file()), root / "bin" / name)


DEPLOYMENT = _script("deployment")


def _manifest(name: str) -> dict:
    script = _script(name)
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
        text=True, timeout=60,
    )


def _setup(root: Path, env: dict[str, str], profile: str) -> dict:
    proc = _run(root, env, "setup", "--profile", profile, "--provider", "manual", "--force")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_image_carries_the_boot_path_and_not_the_project(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    dockerfile = (root / "Dockerfile").read_text()
    assert "COPY --chown=${USERNAME}:${USERNAME} . /app" not in dockerfile
    assert ("COPY --chown=${USERNAME}:${USERNAME} deployment/capabilities.lock "
            "entrypoint.sh supervisord.conf /opt/agent/") in dockerfile
    # The lock and the entrypoint are read from the boot directory: the volume
    # mounts over the project root and would hide anything left under it.
    assert 'ENTRYPOINT ["/opt/agent/entrypoint.sh"]' in dockerfile
    assert "/opt/agent/capabilities.lock" in dockerfile
    assert "/app/deployment/capabilities.lock" not in dockerfile


def test_baked_profile_still_copies_the_project(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box")
    dockerfile = (root / "Dockerfile").read_text()
    assert "COPY --chown=${USERNAME}:${USERNAME} . /app" in dockerfile
    assert "/opt/agent" not in dockerfile
    assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in dockerfile


def test_project_initialization_moves_from_build_to_boot(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    dockerfile = (root / "Dockerfile").read_text()
    entrypoint = (root / "entrypoint.sh").read_text()
    # Without a checkout at build time there is nothing to initialize against.
    assert "capabilities init" not in dockerfile
    assert "capabilities init --codex --claude" in entrypoint
    assert "capabilities doctor" in entrypoint


def test_body_is_a_mount_and_the_clone_is_declared(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    runtime = json.loads((root / "deployment" / "runtime.json").read_text())
    assert runtime["volumes"]["agent_body"]["mount"] == "/app"
    assert "agent_body" in runtime["services"]["agent"]["state"]
    assert "AGENT_REPO_URL" in runtime["services"]["agent"]["required_env"]
    assert runtime["services"]["agent"]["environment_defaults"]["AGENT_REPO_BRANCH"] == "main"
    compose = (root / "docker-compose.yaml").read_text()
    assert "- agent_body:/app" in compose
    # Compose passes only declared keys, so an undeclared one never reaches the
    # entrypoint and a fresh volume would have nothing to clone.
    assert 'AGENT_REPO_URL: "${AGENT_REPO_URL:-}"' in compose


def test_entrypoint_clones_only_into_an_empty_body(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    entrypoint = (root / "entrypoint.sh").read_text()
    assert 'if [ -d "$APP/.git" ]; then' in entrypoint
    # Emptiness is judged by files: a fresh named volume is seeded from the
    # image and inherits the empty mount directories it left behind.
    assert '-mindepth 1 -type f -print -quit' in entrypoint
    assert "refusing to touch it" in entrypoint
    assert "git clone --branch" in entrypoint
    assert subprocess.run(["bash", "-n", str(root / "entrypoint.sh")]).returncode == 0


def test_build_context_is_narrowed_to_the_copied_files(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    lines = [line for line in (root / ".dockerignore").read_text().splitlines()
             if line and not line.startswith("#")]
    assert lines[0] == "*"
    # Excluding a directory stops the walk into it, so each parent of a kept
    # file is re-admitted before its own contents are excluded again.
    assert lines.index("!deployment") < lines.index("deployment/*")
    assert lines.index("deployment/*") < lines.index("!deployment/capabilities.lock")
    assert "!entrypoint.sh" in lines
    assert "!supervisord.conf" in lines


def test_a_body_that_is_not_a_volume_is_refused(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    path = root / "deployment" / "runtime.json"
    runtime = json.loads(path.read_text())
    del runtime["volumes"]["agent_body"]
    path.write_text(json.dumps(runtime))
    payload = json.loads(_run(root, env, "doctor").stdout)
    assert [f for f in payload["findings"]
            if f["severity"] == "error" and "agent_body" in f["message"]]


def test_an_undeclared_repo_url_is_refused(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    path = root / "deployment" / "runtime.json"
    runtime = json.loads(path.read_text())
    agent = runtime["services"]["agent"]
    agent["required_env"] = [k for k in agent["required_env"] if k != "AGENT_REPO_URL"]
    path.write_text(json.dumps(runtime))
    payload = json.loads(_run(root, env, "doctor").stdout)
    assert [f for f in payload["findings"]
            if f["severity"] == "error" and "AGENT_REPO_URL" in f["message"]]


def test_next_names_the_repository_before_the_provider_steps(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    proc = _run(root, env, "next", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "AGENT_REPO_URL" in payload["provider_steps"][0]
    assert "GIT_DEPLOY_KEY_B64" in payload["provider_steps"][1]


def test_the_image_name_is_stated_once(tmp_path: Path) -> None:
    # A .env file lets the last assignment win, so a key restated lower down
    # silently overrides the value the heading above it explains.
    for profile in ("agent-box", "agent-box-checkout"):
        root, env = _project(tmp_path / profile, ("telegram",))
        _setup(root, env, profile)
        assignments = [line for line in (root / ".env.example").read_text().splitlines()
                       if line.startswith("AGENT_IMAGE=")]
        assert assignments == ["AGENT_IMAGE=agent-box"], (profile, assignments)
