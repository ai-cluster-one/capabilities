#!/usr/bin/env python3
"""Test suite for ContextKit public installer integration.

Validates that deployment capability:
- Never vendors ContextKit or generated host wiring from consuming repos
- Uses public installer for ContextKit projects
- Generates correct build order: install ContextKit, copy project, install capabilities,
  install hooks, build context, verify with doctor
- Excludes generated files from Docker build context
- Preserves non-ContextKit behavior
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Use local deployment binary from the repo
DEPLOYMENT_BIN = Path(__file__).parent.parent / "bin" / "deployment"


def run_cmd(cmd: list[str], cwd: Path) -> dict[str, Any]:
    """Run command and return parsed JSON output or text."""
    # Replace 'deployment' with local binary path
    if cmd[0] == "deployment":
        cmd = [str(DEPLOYMENT_BIN)] + cmd[1:]

    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"Command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"stderr: {result.stderr}", file=sys.stderr)
        print(f"stdout: {result.stdout}", file=sys.stderr)
        sys.exit(1)

    # Try to parse as JSON first
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"text": result.stdout}


def setup_test_project(tmpdir: Path, with_contextkit: bool = True, name: str = "test-project") -> Path:
    """Create a minimal test project."""
    project = tmpdir / name
    if project.exists():
        shutil.rmtree(project)
    project.mkdir()

    # Initialize git
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=project, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.local"],
        cwd=project, check=True, capture_output=True
    )

    # Create capabilities envelope
    caps_dir = project / "capabilities"
    caps_dir.mkdir()
    settings = {
        "capabilities": {
            "deployment": {"enabled": True}
        }
    }
    (caps_dir / "settings.json").write_text(json.dumps(settings, indent=2))

    # Create ContextKit config if requested
    if with_contextkit:
        contextkit_dir = project / ".contextkit"
        contextkit_dir.mkdir()
        (contextkit_dir / "config.toml").write_text('[project]\nname = "test"\n')

    return project


def test_dry_run_no_contextkit_copy(tmpdir: Path) -> None:
    """Verify dry-run output shows no .contextkit/manager/contextkit copy."""
    project = setup_test_project(tmpdir, with_contextkit=True, name="test-dry-run")

    result = run_cmd(
        ["deployment", "setup", "--dry-run"],
        cwd=project
    )

    assert result["ok"], "dry-run should succeed"
    assert result["dry_run"], "should be a dry-run"

    would_write = result.get("would_write", [])

    # Should NOT write .contextkit/manager/contextkit
    assert ".contextkit/manager/contextkit" not in would_write, \
        "Should not vendor ContextKit manager binary"

    print("✓ Dry-run shows no ContextKit manager copy")


def test_dockerignore_excludes_contextkit(tmpdir: Path) -> None:
    """Verify .dockerignore excludes ContextKit manager and generated files."""
    project = setup_test_project(tmpdir, with_contextkit=True, name="test-dockerignore")

    run_cmd(["deployment", "setup", "--force"], cwd=project)

    dockerignore = (project / ".dockerignore").read_text()

    # Should exclude ContextKit product paths
    assert ".contextkit/manager/" in dockerignore, \
        "Should exclude .contextkit/manager/"
    assert ".contextkit/.manager/" in dockerignore, \
        "Should exclude .contextkit/.manager/"
    assert ".contextkit/bundle/" in dockerignore, \
        "Should exclude .contextkit/bundle/"
    assert ".contextkit/guides/" in dockerignore, \
        "Should exclude .contextkit/guides/"
    assert ".contextkit/templates/" in dockerignore, \
        "Should exclude .contextkit/templates/"
    assert ".contextkit/install.json" in dockerignore, \
        "Should exclude .contextkit/install.json"
    assert ".contextkit/release.json" in dockerignore, \
        "Should exclude .contextkit/release.json"

    # Should exclude generated host bindings and hooks
    assert ".codex/generated/" in dockerignore, \
        "Should exclude .codex/generated/"
    assert ".codex/hooks/build-context.sh" in dockerignore, \
        "Should exclude .codex/hooks/build-context.sh"
    assert ".claude/rules/CONTEXT.md" in dockerignore, \
        "Should exclude .claude/rules/CONTEXT.md"

    # Should exclude machine-local bindings
    assert ".env.local" in dockerignore, \
        "Should exclude .env.local (machine-local binding)"

    # Should have explanatory comment
    assert "public installer" in dockerignore.lower(), \
        "Should explain ContextKit is installed via public installer"

    print("✓ .dockerignore excludes ContextKit product paths and generated files")


def test_dockerfile_uses_public_installer(tmpdir: Path) -> None:
    """Verify Dockerfile uses public installer instead of local copy."""
    project = setup_test_project(tmpdir, with_contextkit=True, name="test-dockerfile")

    run_cmd(["deployment", "setup", "--force"], cwd=project)

    dockerfile = (project / "Dockerfile").read_text()

    # Should use public installer
    assert "https://raw.githubusercontent.com/ai-cluster-one/context-kit/" in dockerfile, \
        "Should use public ContextKit installer URL"
    assert "CONTEXTKIT_REF" in dockerfile, \
        "Should use CONTEXTKIT_REF build arg"
    assert "contextkit doctor" in dockerfile, \
        "Should verify project with contextkit doctor"

    # Should NOT copy local contextkit binary
    assert ".contextkit/manager/contextkit" not in dockerfile, \
        "Should not reference local ContextKit manager binary"

    # Should initialize, install hooks, and build context
    assert "contextkit init" in dockerfile, \
        "Should initialize ContextKit target-local bindings"
    assert "contextkit install-hooks" in dockerfile, \
        "Should install ContextKit hooks"
    assert "contextkit build --target all" in dockerfile, \
        "Should build ContextKit context"
    assert "contextkit audit" in dockerfile, \
        "Should audit built ContextKit context"

    # Should install both target hooks
    assert "contextkit install-hooks --target codex --target claude" in dockerfile, \
        "Should install hooks for both codex and claude targets"

    # Should initialize capabilities for both targets
    assert "capabilities init --codex --claude" in dockerfile, \
        "Should initialize capabilities for both codex and claude"

    # Verify build order
    lines = dockerfile.split('\n')
    contextkit_install_idx = None
    contextkit_verify_idx = None
    copy_idx = None
    capabilities_install_idx = None
    contextkit_init_idx = None
    capabilities_init_idx = None
    hooks_idx = None
    doctor_idx = None
    build_idx = None
    audit_idx = None

    for i, line in enumerate(lines):
        if "context-kit" in line and "install.sh" in line:
            contextkit_install_idx = i
        elif "contextkit help" in line and contextkit_verify_idx is None:
            contextkit_verify_idx = i
        elif "COPY" in line and "/app" in line and "entrypoint" not in line:
            copy_idx = i
        elif "capabilities install" in line:
            capabilities_install_idx = i
        elif "contextkit init" in line and contextkit_init_idx is None:
            contextkit_init_idx = i
        elif "capabilities init" in line and capabilities_init_idx is None:
            capabilities_init_idx = i
        elif "contextkit install-hooks" in line and hooks_idx is None:
            hooks_idx = i
        elif "contextkit doctor" in line and doctor_idx is None:
            doctor_idx = i
        elif "contextkit build --target all" in line and build_idx is None:
            build_idx = i
        elif "contextkit audit" in line and audit_idx is None:
            audit_idx = i

    # Verify order: install ContextKit -> verify product -> copy project -> install capabilities -> contextkit init -> capabilities init -> hooks -> doctor -> build -> audit
    assert contextkit_install_idx is not None, "Should install ContextKit"
    assert contextkit_verify_idx is not None, "Should verify ContextKit product installation"
    assert copy_idx is not None, "Should copy project"
    assert capabilities_install_idx is not None, "Should install capabilities"
    assert contextkit_init_idx is not None, "Should initialize ContextKit target-local bindings"
    assert capabilities_init_idx is not None, "Should initialize capabilities contexts"
    assert hooks_idx is not None, "Should install hooks"
    assert doctor_idx is not None, "Should verify project with doctor"
    assert build_idx is not None, "Should build context"
    assert audit_idx is not None, "Should audit built context"

    assert contextkit_install_idx < contextkit_verify_idx, \
        "Should verify ContextKit installation immediately after install"
    assert contextkit_verify_idx < copy_idx, \
        "Should verify product before copying project"
    assert copy_idx < capabilities_install_idx, \
        "Should copy project before installing capabilities"
    assert capabilities_install_idx < contextkit_init_idx, \
        "Should install capabilities before initializing ContextKit bindings"
    assert contextkit_init_idx < capabilities_init_idx, \
        "Should initialize ContextKit bindings before initializing capability contexts"
    assert capabilities_init_idx < hooks_idx, \
        "Should initialize capability contexts before installing hooks"
    assert hooks_idx < doctor_idx, \
        "Should install hooks before verifying with doctor"
    assert doctor_idx < build_idx, \
        "Should verify with doctor before building context"
    assert build_idx < audit_idx, \
        "Should build context before auditing"

    # CRITICAL: No project-dependent contextkit commands should occur before COPY
    for i in range(copy_idx if copy_idx is not None else len(lines)):
        line = lines[i]
        if "contextkit init" in line:
            assert False, f"contextkit init (project-dependent) found before COPY at line {i}"
        if "contextkit doctor" in line:
            assert False, f"contextkit doctor (project-dependent) found before COPY at line {i}"
        if "contextkit build" in line:
            assert False, f"contextkit build (project-dependent) found before COPY at line {i}"
        if "contextkit audit" in line:
            assert False, f"contextkit audit (project-dependent) found before COPY at line {i}"
        if "contextkit install-hooks" in line:
            assert False, f"contextkit install-hooks (project-dependent) found before COPY at line {i}"

    print("✓ Dockerfile uses public installer with correct build order")


def test_compose_includes_contextkit_ref(tmpdir: Path) -> None:
    """Verify docker-compose.yaml includes CONTEXTKIT_REF build arg."""
    project = setup_test_project(tmpdir, with_contextkit=True, name="test-compose")

    run_cmd(["deployment", "setup", "--force"], cwd=project)

    compose = (project / "docker-compose.yaml").read_text()

    # Should include CONTEXTKIT_REF build arg
    assert "CONTEXTKIT_REF:" in compose, \
        "Should include CONTEXTKIT_REF build arg"
    assert "${CONTEXTKIT_REF:-main}" in compose, \
        "Should default CONTEXTKIT_REF to main"

    print("✓ docker-compose.yaml includes CONTEXTKIT_REF")


def test_env_example_includes_contextkit_ref(tmpdir: Path) -> None:
    """Verify .env.example includes CONTEXTKIT_REF for ContextKit projects."""
    project = setup_test_project(tmpdir, with_contextkit=True, name="test-env")

    run_cmd(["deployment", "setup", "--force"], cwd=project)

    env_example = (project / ".env.example").read_text()

    # Should include CONTEXTKIT_REF
    assert "CONTEXTKIT_REF=" in env_example, \
        "Should include CONTEXTKIT_REF in .env.example"

    print("✓ .env.example includes CONTEXTKIT_REF")


def test_non_contextkit_behavior_preserved(tmpdir: Path) -> None:
    """Verify non-ContextKit projects work as before."""
    project = setup_test_project(tmpdir, with_contextkit=False, name="test-no-contextkit")

    run_cmd(["deployment", "setup", "--force"], cwd=project)

    dockerfile = (project / "Dockerfile").read_text()
    dockerignore = (project / ".dockerignore").read_text()
    compose = (project / "docker-compose.yaml").read_text()
    env_example = (project / ".env.example").read_text()

    # Should NOT include ContextKit setup
    assert "contextkit" not in dockerfile.lower(), \
        "Should not reference ContextKit in Dockerfile"
    assert ".contextkit" not in dockerignore, \
        "Should not reference ContextKit in .dockerignore"
    assert "CONTEXTKIT_REF" not in compose, \
        "Should not include CONTEXTKIT_REF in compose"
    assert "CONTEXTKIT_REF" not in env_example, \
        "Should not include CONTEXTKIT_REF in .env.example"

    print("✓ Non-ContextKit behavior preserved")


def test_build_fails_on_missing_steps(tmpdir: Path) -> None:
    """Verify build includes failure checks for installer, hooks, and doctor."""
    project = setup_test_project(tmpdir, with_contextkit=True, name="test-failure-checks")

    run_cmd(["deployment", "setup", "--force"], cwd=project)

    dockerfile = (project / "Dockerfile").read_text()

    # Should fail on ContextKit installation failure
    assert "ContextKit installation failed" in dockerfile, \
        "Should fail on ContextKit installation failure"

    # Should fail on project setup failure
    assert "ContextKit project setup failed" in dockerfile, \
        "Should fail on ContextKit project setup failure"

    # Check for exit 1
    assert "exit 1" in dockerfile, \
        "Should exit with code 1 on failure"

    print("✓ Build includes failure checks")


def main() -> None:
    """Run all tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        print("Running ContextKit installer integration tests...\n")

        test_dry_run_no_contextkit_copy(tmppath)
        test_dockerignore_excludes_contextkit(tmppath)
        test_dockerfile_uses_public_installer(tmppath)
        test_compose_includes_contextkit_ref(tmppath)
        test_env_example_includes_contextkit_ref(tmppath)
        test_non_contextkit_behavior_preserved(tmppath)
        test_build_fails_on_missing_steps(tmppath)

        print("\n✅ All tests passed!")


if __name__ == "__main__":
    main()
