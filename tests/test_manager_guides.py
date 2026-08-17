from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(MANAGER), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_manager_guide_menu_uses_the_capability_guide_shape() -> None:
    proc = _run("guide")
    assert proc.returncode == 0, proc.stderr
    menu = json.loads(proc.stdout)
    assert [entry["topic"] for entry in menu] == [
        "authoring",
        "conforming",
        "contract",
        "dev",
        "grooming",
        "publishing",
        "repositories",
        "sanitizing",
    ]
    for entry in menu:
        assert set(entry) == {"topic", "title", "preview", "command"}
        assert entry["title"]
        assert entry["preview"]
        assert entry["command"] == f"capabilities guide {entry['topic']}"


def test_manager_guide_body_comes_from_markdown() -> None:
    proc = _run("guide", "grooming")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (REPO / "guides" / "grooming.md").read_text()


def test_instruction_only_manager_verbs_are_removed() -> None:
    for verb in ("conform", "groom", "sanitize"):
        proc = _run(verb, "example")
        assert proc.returncode == 6
        assert json.loads(proc.stderr)["error"]["message"] == f"unknown command: {verb}"


def test_new_without_name_routes_to_the_authoring_guide() -> None:
    proc = _run("new")
    assert proc.returncode == 6
    error = json.loads(proc.stderr)["error"]
    assert error["hint"] == (
        "run `capabilities guide authoring` for the authoring workflow"
    )
