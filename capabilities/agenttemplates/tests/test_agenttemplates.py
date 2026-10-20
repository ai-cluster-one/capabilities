"""agenttemplates lists and shows the templates its bundle carries.

The CLI runs as a child in a project that exists only for the test and
enables it, with a home of its own, so nothing reaches the real one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = next((path for path in (CAPABILITY / "bin" / "agenttemplates", CAPABILITY / "agenttemplates")
            if path.is_file()), CAPABILITY / "bin" / "agenttemplates")
TEMPLATES = CAPABILITY / "templates"
MANAGER = CAPABILITY.parents[1] / "bin" / "capabilities"


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    project = tmp_path / "project"
    if not project.is_dir():
        (project / ".git").mkdir(parents=True)
        (project / "capabilities").mkdir()
        (project / "capabilities" / "project.json").write_text(json.dumps(
            {"schema": "capabilities.project.v1", "slug": "lab", "id": "prj_" + "2c3d4e5f6a7b"}))
        (project / "capabilities" / "settings.json").write_text(json.dumps(
            {"capabilities": {"agenttemplates": {"enabled": True}}}))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENTKIT_DB_", "CAPABILITIES_", "CLAUDE"))}
    env.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_STATE_HOME": str(tmp_path / "state"),
                "XDG_DATA_HOME": str(tmp_path / "data"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"),
                "CAPABILITIES_MANAGER_BIN": str(MANAGER), "CLAUDE_PROJECT_DIR": str(project)})
    return subprocess.run([sys.executable, str(CLI), *args], cwd=project, env=env, text=True,
                          capture_output=True, timeout=120)


def _ok(tmp_path: Path, *args: str) -> dict:
    result = _run(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_list_names_the_starter_files_and_the_agent_templates(tmp_path):
    answer = _ok(tmp_path, "list")
    assert answer["source"] == str(TEMPLATES)
    assert [item["path"] for item in answer["starter"]] == sorted(
        path.relative_to(TEMPLATES).as_posix() for path in (TEMPLATES / "context").rglob("*.md"))
    assert all(item["title"] and item["description"] for item in answer["starter"])
    assert [(item["id"], item["path"]) for item in answer["agents"]] == [
        ("personal-agent", str(TEMPLATES / "agents" / "personal-agent"))]


def test_show_answers_an_agent_template_and_a_file(tmp_path):
    folder = TEMPLATES / "agents" / "personal-agent"
    template = _ok(tmp_path, "show", "personal-agent")
    assert template["manifest"] == tomllib.loads((folder / "template.toml").read_text())
    assert "routines/onboarding.md" in template["files"] and "icon.png" in template["files"]
    file = _ok(tmp_path, "show", "context/identity/AGENT.md")
    assert file == {"path": "context/identity/AGENT.md",
                    "text": (TEMPLATES / "context" / "identity" / "AGENT.md").read_text()}


@pytest.mark.parametrize("target", ["absent", "context/identity/ABSENT.md", "../bin/agenttemplates"])
def test_show_refuses_what_the_bundle_does_not_carry(tmp_path, target):
    result = _run(tmp_path, "show", target)
    assert result.returncode == 3, result.stderr
    assert json.loads(result.stderr)["error"]["code"] == "not_found"
