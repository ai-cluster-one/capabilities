"""Test deployment capability target build lifecycle ordering and failure visibility."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


DEPLOYMENT = Path(__file__).parents[1] / "capabilities" / "deployment" / "bin" / "deployment"


def _project(tmp_path: Path, has_contextkit: bool = False) -> Path:
    """Create a minimal project structure."""
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    if has_contextkit:
        config = project / ".contextkit" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('version = 1\ntype = "agent-project"\n')
    return project


def _run(tmp_path: Path, project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run deployment command with isolated environment."""
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
    })
    return subprocess.run(
        [sys.executable, str(DEPLOYMENT), *args],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_dockerfile_has_capabilities_doctor_after_init(tmp_path: Path) -> None:
    """Verify the generated Dockerfile runs capabilities doctor after capabilities init."""
    project = _project(tmp_path, has_contextkit=False)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text('{"capabilities": {}}\n')

    result = _run(tmp_path, project, "setup", "--profile", "agent-box", "--force")

    assert result.returncode == 0, result.stderr
    dockerfile = (project / "Dockerfile").read_text()

    # Find the capabilities installation section
    install_idx = dockerfile.find("capabilities install")
    assert install_idx > 0, "capabilities install not found in Dockerfile"

    # Verify capabilities init comes after install
    init_idx = dockerfile.find("capabilities init", install_idx)
    assert init_idx > install_idx, "capabilities init should come after install"

    # Verify capabilities doctor comes after init
    doctor_idx = dockerfile.find("capabilities doctor", init_idx)
    assert doctor_idx > init_idx, "capabilities doctor should come after capabilities init"

    # Verify doctor has error handling
    assert "capabilities doctor ||" in dockerfile, "capabilities doctor should have error handling"
    assert "capabilities readiness check failed" in dockerfile


def test_dockerfile_contextkit_has_doctor_after_capabilities_init(tmp_path: Path) -> None:
    """Verify ContextKit projects run capabilities doctor after capabilities init."""
    project = _project(tmp_path, has_contextkit=True)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text('{"capabilities": {}}\n')

    result = _run(tmp_path, project, "setup", "--profile", "agent-box", "--force")

    assert result.returncode == 0, result.stderr
    dockerfile = (project / "Dockerfile").read_text()

    # Verify capabilities doctor comes after capabilities init
    cap_init_idx = dockerfile.find("capabilities init")
    cap_doctor_idx = dockerfile.find("capabilities doctor")
    assert cap_init_idx > 0, "capabilities init not found"
    assert cap_doctor_idx > 0, "capabilities doctor not found"
    assert cap_init_idx < cap_doctor_idx, (
        "capabilities init should run before capabilities doctor"
    )

    # Verify capabilities doctor comes before contextkit install-hooks
    hooks_idx = dockerfile.find("contextkit install-hooks")
    assert hooks_idx > 0, "contextkit install-hooks not found"
    assert cap_doctor_idx < hooks_idx, (
        "capabilities doctor should run before contextkit install-hooks"
    )


def test_dockerfile_contextkit_does_not_duplicate_capabilities_init(tmp_path: Path) -> None:
    """Verify ContextKit projects don't call capabilities init twice."""
    project = _project(tmp_path, has_contextkit=True)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text('{"capabilities": {}}\n')

    result = _run(tmp_path, project, "setup", "--profile", "agent-box", "--force")

    assert result.returncode == 0, result.stderr
    dockerfile = (project / "Dockerfile").read_text()

    # Count occurrences of "capabilities init"
    init_count = dockerfile.count("capabilities init")
    assert init_count == 1, (
        f"capabilities init should appear exactly once, found {init_count} times"
    )


def test_dockerfile_ordering_capabilities_to_contextkit(tmp_path: Path) -> None:
    """Verify the complete ordering for ContextKit projects."""
    project = _project(tmp_path, has_contextkit=True)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text('{"capabilities": {}}\n')

    result = _run(tmp_path, project, "setup", "--profile", "agent-box", "--force")

    assert result.returncode == 0, result.stderr
    dockerfile = (project / "Dockerfile").read_text()

    # Extract the relevant sections and verify ordering
    steps = [
        "capabilities install",
        "contextkit init",
        "capabilities init",
        "capabilities doctor",
        "contextkit install-hooks",
        "contextkit doctor",
        "contextkit build",
        "contextkit audit",
    ]

    indices = []
    for step in steps:
        idx = dockerfile.find(step)
        assert idx > 0, f"{step} not found in Dockerfile"
        indices.append((step, idx))

    # Verify the steps appear in order
    for i in range(len(indices) - 1):
        current_step, current_idx = indices[i]
        next_step, next_idx = indices[i + 1]
        assert current_idx < next_idx, (
            f"{current_step} should come before {next_step}"
        )


def test_freeze_includes_deployment_in_lock(tmp_path: Path) -> None:
    """Verify deployment freeze includes deployment itself in capabilities.lock."""
    project = _project(tmp_path, has_contextkit=False)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text(json.dumps({
        "capabilities": {"telegram": {"enabled": True}},
    }))

    result = _run(tmp_path, project, "init", "--profile", "agent-box")
    assert result.returncode == 0, result.stderr

    result = _run(tmp_path, project, "freeze")
    assert result.returncode == 0, result.stderr

    lock = (project / "deployment" / "capabilities.lock").read_text()
    entries = [
        line.strip()
        for line in lock.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    # Verify deployment is in the lock
    assert "deployment" in entries, "deployment should be in capabilities.lock"
    # Verify enabled capabilities are in the lock
    assert "telegram" in entries, "enabled capabilities should be in lock"


def test_init_does_not_create_reference_files(tmp_path: Path) -> None:
    """Verify deployment init does not create pointer files in capabilities/deployment/reference/."""
    project = _project(tmp_path, has_contextkit=False)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text('{"capabilities": {}}\n')

    result = _run(tmp_path, project, "init", "--profile", "agent-box", "--target", "production")
    assert result.returncode == 0, result.stderr

    # Verify the reference directory was not created
    refdir = project / "capabilities" / "deployment" / "reference"
    assert not refdir.exists(), "reference directory should not be created by init"


def test_setup_does_not_create_reference_files(tmp_path: Path) -> None:
    """Verify deployment setup does not create pointer files in capabilities/deployment/reference/."""
    project = _project(tmp_path, has_contextkit=False)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text('{"capabilities": {}}\n')

    result = _run(tmp_path, project, "setup", "--profile", "agent-box", "--target", "production", "--force")
    assert result.returncode == 0, result.stderr

    # Verify the reference directory was not created
    refdir = project / "capabilities" / "deployment" / "reference"
    assert not refdir.exists(), "reference directory should not be created by setup"


def test_doctor_clean_without_reference_directory(tmp_path: Path) -> None:
    """Verify deployment doctor is clean when reference directory does not exist."""
    project = _project(tmp_path, has_contextkit=False)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text('{"capabilities": {}}\n')

    result = _run(tmp_path, project, "init", "--profile", "agent-box")
    assert result.returncode == 0, result.stderr

    result = _run(tmp_path, project, "doctor")
    assert result.returncode == 0, result.stderr

    # Parse the JSON output
    output = json.loads(result.stdout)
    assert output.get("ok") is True, "doctor should be ok without reference directory"

    # Verify no warning about missing references
    findings = output.get("findings", [])
    ref_warnings = [
        f for f in findings
        if "reference" in f.get("message", "").lower()
    ]
    assert not ref_warnings, "doctor should not warn about missing references"


def test_doctor_lists_genuine_reference_files(tmp_path: Path) -> None:
    """Verify deployment doctor validates and lists genuine reference files when present."""
    project = _project(tmp_path, has_contextkit=False)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "settings.json").write_text('{"capabilities": {}}\n')

    result = _run(tmp_path, project, "init", "--profile", "agent-box")
    assert result.returncode == 0, result.stderr

    # Create a genuine project-specific reference file
    refdir = project / "capabilities" / "deployment" / "reference"
    refdir.mkdir(parents=True, exist_ok=True)
    (refdir / "custom-workflow.md").write_text("# Custom Deployment Workflow\n\nProject-specific steps.\n")

    result = _run(tmp_path, project, "doctor")
    assert result.returncode == 0, result.stderr

    # Parse the JSON output
    output = json.loads(result.stdout)
    assert output.get("ok") is True, "doctor should be ok with valid reference file"

    # Verify the reference file is listed
    refs = output.get("references", [])
    assert any("custom-workflow.md" in ref for ref in refs), "custom reference should be listed"
