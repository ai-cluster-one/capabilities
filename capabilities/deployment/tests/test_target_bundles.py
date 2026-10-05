"""A container runtime compiles one bundle per target.

What is proven here is where the compiled files land and what reads them: each
target's artifacts sit in deployment/targets/<target>/ beside its declaration,
the project root gains nothing but the .gitattributes git can only read there,
the files of the root layout are no longer written, the Compose file reaches the
project root as its build context from its own folder, and the ignore file is
named for the Dockerfile it serves. A runtime that names its own paths, as every
runtime compiled before bundles does, keeps them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


CAP_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = next((path for path in (
    CAP_ROOT / "deployment" / "bin" / "deployment", CAP_ROOT / "deployment" / "deployment")
    if path.is_file()), CAP_ROOT / "deployment" / "bin" / "deployment")
PROFILES = ("agent-box", "agent-box-checkout")
# The files the root layout wrote by default; the suite re-declares them so a
# change to the compiler's list cannot pass silently.
ROOT_LAYOUT = ("Dockerfile", "docker-compose.yaml", ".dockerignore", ".env.example",
               "entrypoint.sh", "supervisord.conf", "body-sync.sh")
BUNDLE = {"docker-compose.yaml", "Dockerfile", "Dockerfile.dockerignore", ".env.example",
          "entrypoint.sh", "supervisord.conf", "target.json"}


def _project(tmp_path: Path, contextkit: bool = True) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "capabilities").mkdir()
    (root / "capabilities" / "settings.json").write_text(
        json.dumps({"capabilities": {"deployment": {"enabled": True}}}) + "\n")
    (root / "README.md").write_text("body\n")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
    }
    if contextkit:
        # The checkout profile asks ContextKit where memory lives to write the
        # union-merge attribute; a stand-in answers for the real one.
        (root / ".contextkit").mkdir()
        (root / ".contextkit" / "config.toml").write_text("[project]\n")
        (root / "memory").mkdir()
        fake = tmp_path / "bin" / "contextkit"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\n"
                        f'[ "$1 $2" = "path memory" ] && exec echo {root / "memory"}\n'
                        f'[ "$1 $2" = "path capabilities" ] && exec echo {root / "capabilities"}\n'
                        "exit 1\n")
        fake.chmod(0o755)
        env["PATH"] = str(fake.parent) + os.pathsep + env["PATH"]
    return root, env


def _run(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(DEPLOYMENT), *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=60)


def _root_entries(root: Path) -> set[str]:
    return {path.name for path in root.iterdir()}


@pytest.mark.parametrize("profile", PROFILES)
def test_sync_writes_each_target_into_its_own_folder_only(tmp_path: Path, profile: str) -> None:
    root, env = _project(tmp_path)
    before = _root_entries(root)
    setup = _run(root, env, "setup", "--profile", profile, "--target", "staging",
                 "--provider", "coolify")
    assert setup.returncode == 0, setup.stdout + setup.stderr
    added = _root_entries(root) - before
    # The root gains the deployment folder itself and, on the checkout profile,
    # the attribute file git reads only at the root.
    assert added == ({"deployment", ".gitattributes"} if profile == "agent-box-checkout"
                     else {"deployment"})
    assert not [name for name in ROOT_LAYOUT if (root / name).exists()]
    bundle = root / "deployment" / "targets" / "staging"
    expected = set(BUNDLE)
    if profile == "agent-box-checkout":
        expected.add("body-sync.sh")
    else:
        expected.discard("supervisord.conf")
    assert {path.name for path in bundle.iterdir()} == expected
    assert sorted(path.relative_to(root).as_posix() for path in (root / "deployment").iterdir()) == [
        "deployment/capabilities.lock", "deployment/runtime.json", "deployment/targets"]
    runtime = json.loads((root / "deployment" / "runtime.json").read_text())
    assert "compose_file" not in runtime and "artifacts" not in runtime["compiler"]

    # A second target compiles its own bundle beside the first, and a sync
    # writes nothing anywhere else.
    # init leaves the existing runtime and lock alone and adds the declaration.
    _run(root, env, "init", "--profile", profile, "--target", "edge", "--provider", "manual")
    assert (root / "deployment" / "targets" / "edge" / "target.json").is_file()
    before = _root_entries(root)
    sync = _run(root, env, "sync")
    assert sync.returncode == 0, sync.stdout + sync.stderr
    written = {item["path"] for item in json.loads(sync.stdout)["written"]
               if item["action"] != "unchanged"}
    assert written and all(path.startswith("deployment/targets/edge/") for path in written)
    assert _root_entries(root) == before
    assert not [name for name in ROOT_LAYOUT if (root / name).exists()]
    edge = json.loads((root / "deployment" / "targets" / "edge" / "target.json").read_text())
    assert edge["resource"]["compose_file"] == "deployment/targets/edge/docker-compose.yaml"
    check = _run(root, env, "sync", "--check")
    assert check.returncode == 0, check.stdout


@pytest.mark.parametrize("profile", PROFILES)
def test_the_bundle_builds_from_the_root_with_its_own_ignore_file(
        tmp_path: Path, profile: str) -> None:
    root, env = _project(tmp_path)
    assert _run(root, env, "setup", "--profile", profile, "--provider", "coolify").returncode == 0
    bundle = "deployment/targets/production"
    compose = (root / bundle / "docker-compose.yaml").read_text()
    # Relative to the Compose file's own folder, which is also the project
    # directory Coolify gives Compose when the base directory is that folder.
    assert 'context: "../../.."' in compose
    assert f'dockerfile: "{bundle}/Dockerfile"' in compose
    ignore = (root / bundle / "Dockerfile.dockerignore").read_text()
    assert not (root / ".dockerignore").exists()
    if profile == "agent-box":
        assert ".git" in ignore.splitlines() and ".env.local" in ignore.splitlines()
    else:
        lines = [line for line in ignore.splitlines() if line and not line.startswith("#")]
        assert lines[0] == "*"
        for kept in ("deployment/capabilities.lock", f"{bundle}/entrypoint.sh",
                     f"{bundle}/supervisord.conf", f"{bundle}/body-sync.sh"):
            assert "!" + kept in lines
            assert kept in (root / bundle / "Dockerfile").read_text()
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "compose", "version"], capture_output=True).returncode == 0:
        for project_directory in ([], ["--project-directory", bundle]):
            parsed = subprocess.run(
                [docker, "compose", *project_directory, "-f", f"{bundle}/docker-compose.yaml",
                 "config", "--format", "json"],
                cwd=root, capture_output=True, text=True, timeout=30,
                env={**env, "AGENT_REPO_URL": "fixture"})
            assert parsed.returncode == 0, parsed.stderr
            build = json.loads(parsed.stdout)["services"]["agent"]["build"]
            assert Path(build["context"]).resolve() == root.resolve()


def test_next_names_the_coolify_base_directory_and_compose_location(tmp_path: Path) -> None:
    root, env = _project(tmp_path, contextkit=False)
    assert _run(root, env, "setup", "--profile", "agent-box-checkout",
                "--provider", "coolify").returncode == 0
    payload = json.loads(_run(root, env, "next", "--json").stdout)
    steps = " ".join(payload["provider_steps"])
    assert "Base Directory to `/deployment/targets/production`" in steps
    assert "Docker Compose Location to `/docker-compose.yaml`" in steps
    assert all(path.startswith("deployment/targets/production/") for path in payload["artifacts"])


def test_a_coolify_target_deploys_into_production(tmp_path: Path) -> None:
    root, env = _project(tmp_path, contextkit=False)
    assert _run(root, env, "setup", "--profile", "agent-box-checkout", "--target", "staging",
                "--provider", "coolify").returncode == 0
    target = json.loads((root / "deployment" / "targets" / "staging" / "target.json").read_text())
    assert target["environment"] == "production"


def test_a_runtime_that_names_its_paths_keeps_the_root_layout(tmp_path: Path) -> None:
    root, env = _project(tmp_path, contextkit=False)
    assert _run(root, env, "init", "--profile", "agent-box-checkout",
                "--provider", "coolify").returncode == 0
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    # What every runtime compiled before bundles carries.
    runtime["compose_file"] = "docker-compose.yaml"
    runtime["compiler"]["artifacts"] = {"dockerfile": "Dockerfile", "entrypoint": "entrypoint.sh",
                                        "env_example": ".env.example", "dockerignore": ".dockerignore",
                                        "supervisor": "supervisord.conf"}
    runtime_path.write_text(json.dumps(runtime))
    sync = _run(root, env, "sync")
    assert sync.returncode == 0, sync.stdout + sync.stderr
    assert all((root / name).is_file() for name in ROOT_LAYOUT)
    assert {path.name for path in (root / "deployment" / "targets" / "production").iterdir()} == {
        "target.json"}
    compiled = json.loads(runtime_path.read_text())
    assert compiled["compose_file"] == "docker-compose.yaml"
    assert compiled["compiler"]["artifacts"]["dockerignore"] == ".dockerignore"


def test_moving_a_project_onto_bundles_reports_what_the_root_still_holds(tmp_path: Path) -> None:
    root, env = _project(tmp_path, contextkit=False)
    assert _run(root, env, "init", "--profile", "agent-box-checkout",
                "--provider", "coolify").returncode == 0
    runtime_path = root / "deployment" / "runtime.json"
    target_dir = root / "deployment" / "targets"
    # A project as it stood before bundles: root paths and a target file.
    runtime = json.loads(runtime_path.read_text())
    runtime["compose_file"] = "docker-compose.yaml"
    runtime["compiler"]["artifacts"] = {"dockerfile": "Dockerfile", "entrypoint": "entrypoint.sh",
                                        "env_example": ".env.example", "dockerignore": ".dockerignore"}
    runtime_path.write_text(json.dumps(runtime))
    (target_dir / "production" / "target.json").rename(target_dir / "production.json")
    (target_dir / "production").rmdir()
    assert _run(root, env, "sync").returncode == 0

    # The legacy declaration is read where it is, and its bundle compiles once
    # the runtime stops naming root paths.
    runtime = json.loads(runtime_path.read_text())
    del runtime["compose_file"]
    del runtime["compiler"]["artifacts"]
    runtime_path.write_text(json.dumps(runtime))
    sync = _run(root, env, "sync")
    assert sync.returncode == 0, sync.stdout + sync.stderr
    payload = json.loads(sync.stdout)
    assert (target_dir / "production" / "docker-compose.yaml").is_file()
    leftovers = sorted(f["path"] for f in payload["findings"]
                       if "no longer part of any build" in f["message"])
    assert leftovers == sorted(ROOT_LAYOUT)
    targets = json.loads(_run(root, env, "targets").stdout)
    assert [(t["name"], t["path"]) for t in targets] == [
        ("production", "deployment/targets/production.json")]

    # Once the declaration moves into the folder, a copy left beside it is an error.
    shutil.copy(target_dir / "production.json", target_dir / "production" / "target.json")
    doctor = json.loads(_run(root, env, "doctor").stdout)
    assert [f for f in doctor["findings"]
            if f["path"] == "deployment/targets/production.json" and "declared twice" in f["message"]]
    (target_dir / "production.json").unlink()
    for name in ROOT_LAYOUT:
        (root / name).unlink()
    doctor = json.loads(_run(root, env, "doctor").stdout)
    assert not [f for f in doctor["findings"]
                if "declared twice" in f["message"] or "no longer part of any build" in f["message"]]
    assert doctor["targets"] == ["deployment/targets/production/target.json"]


def test_a_bundled_runtime_without_a_target_says_so(tmp_path: Path) -> None:
    root, env = _project(tmp_path, contextkit=False)
    assert _run(root, env, "init", "--profile", "agent-box", "--provider", "manual").returncode == 0
    shutil.rmtree(root / "deployment" / "targets")
    sync = _run(root, env, "sync")
    assert sync.returncode == 6
    assert any("no target declared" in f["message"] for f in json.loads(sync.stdout)["findings"])
