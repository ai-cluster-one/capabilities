import json
import os
import subprocess
import sys
from pathlib import Path


MANAGER = Path(__file__).parents[1] / "bin" / "capabilities"
YOUTRACK = Path(__file__).parents[1] / "capabilities" / "youtrack" / "bin" / "youtrack"


def _project(tmp_path: Path, contextkit: bool) -> Path:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    if contextkit:
        config = project / ".contextkit" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('version = 1\ntype = "agent-project"\n')
    return project


def _run(tmp_path: Path, project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
    })
    return subprocess.run(
        [sys.executable, str(MANAGER), *args],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_contextkit_init_skips_both_host_bindings(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)

    result = _json(_run(tmp_path, project, "init", "--claude", "--codex"))

    assert (project / "capabilities" / "settings.json").is_file()
    assert not (project / ".claude").exists()
    assert not (project / ".codex").exists()
    assert result["context"] == {
        "owner": "contextkit",
        "config": str(project / ".contextkit" / "config.toml"),
        "capabilities_host_wiring": "skipped",
        "targets": ["claude", "codex"],
        "refresh": "contextkit build --target all",
    }


def test_contextkit_init_and_context_preserve_owned_files(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)
    files = {
        project / ".codex" / "hooks" / "build-context.sh": "# ContextKit compiler\n",
        project / ".codex" / "hooks.json": '{"contextkit": true}\n',
        project / ".codex" / "generated" / "context.md": "CODEX CONTEXT\n",
        project / ".claude" / "settings.json": '{"contextkit": true}\n',
        project / ".claude" / "rules" / "CONTEXT.md": "CLAUDE CONTEXT\n",
    }
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    _json(_run(tmp_path, project, "init", "--claude", "--codex"))
    codex = _json(_run(tmp_path, project, "context", "--codex"))
    claude = _json(_run(tmp_path, project, "context", "--claude"))

    assert codex["skipped"] is True
    assert codex["refresh"] == "contextkit build --target codex"
    assert claude["skipped"] is True
    assert claude["refresh"] == "contextkit build --target claude"
    for path, body in files.items():
        assert path.read_text() == body
    assert not (project / ".codex" / "generated" / "capabilities.md").exists()
    assert not (project / ".claude" / "rules" / "CAPABILITIES.md").exists()


def test_contextkit_enable_updates_gate_without_generating_context(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)

    result = _json(_run(tmp_path, project, "enable", "asana"))

    gate = json.loads((project / "capabilities" / "settings.json").read_text())
    assert gate["capabilities"]["asana"]["enabled"] is True
    assert result["context"]["owner"] == "contextkit"
    assert result["context"]["refresh"] == "contextkit build --target all"
    assert not (project / ".claude").exists()
    assert not (project / ".codex").exists()


def test_contextkit_init_retires_only_legacy_capabilities_wiring(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    _json(_run(tmp_path, project, "init", "--claude", "--codex"))
    claude_settings = project / ".claude" / "settings.json"
    settings = json.loads(claude_settings.read_text())
    settings["hooks"]["SessionStart"].append({
        "hooks": [{"type": "command", "command": "keep-me"}],
    })
    claude_settings.write_text(json.dumps(settings))
    config = project / ".contextkit" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('version = 1\ntype = "agent-project"\n')

    result = _json(_run(tmp_path, project, "init", "--claude", "--codex"))

    retired = result["retired_capabilities_wiring"]
    assert "claude_hook" in retired
    assert "codex_hook" in retired
    assert not (project / ".claude" / "rules" / "CAPABILITIES.md").exists()
    assert not (project / ".codex" / "hooks" / "build-context.sh").exists()
    assert not (project / ".codex" / "generated" / "capabilities.md").exists()
    assert not (project / ".codex" / "generated" / "context.md").exists()
    next_settings = json.loads(claude_settings.read_text())
    commands = [
        hook["command"]
        for entry in next_settings["hooks"]["SessionStart"]
        for hook in entry["hooks"]
    ]
    assert commands == ["keep-me"]


def test_standalone_project_keeps_capabilities_owned_wiring(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)

    result = _json(_run(tmp_path, project, "init", "--claude"))

    assert result["hook_added"] is True
    assert (project / ".claude" / "settings.json").is_file()
    assert (project / ".claude" / "rules" / "CAPABILITIES.md").is_file()


def test_init_migrates_legacy_hidden_envelope(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)
    legacy = project / ".capabilities"
    (legacy / "asana").mkdir(parents=True)
    (legacy / "settings.json").write_text(json.dumps({
        "capabilities": {"asana": {"enabled": True}},
    }))
    (legacy / "asana" / "identifiers.json").write_text('{"project": "123"}\n')

    result = _json(_run(tmp_path, project, "init", "--claude", "--codex"))

    assert result["migrated_from"] == str(legacy)
    assert not legacy.exists()
    gate = json.loads((project / "capabilities" / "settings.json").read_text())
    assert gate["capabilities"]["asana"]["enabled"] is True
    assert (project / "capabilities" / "asana" / "identifiers.json").is_file()


def test_init_merges_contextkit_empty_gate_with_legacy_gate(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)
    current = project / "capabilities"
    current.mkdir()
    (current / "settings.json").write_text('{"capabilities": {}}\n')
    legacy = project / ".capabilities"
    legacy.mkdir()
    (legacy / "settings.json").write_text(json.dumps({
        "capabilities": {"telegram": {"enabled": True}},
    }))

    _json(_run(tmp_path, project, "init", "--claude", "--codex"))

    assert not legacy.exists()
    gate = json.loads((current / "settings.json").read_text())
    assert gate == {"capabilities": {"telegram": {"enabled": True}}}


def test_init_refuses_envelope_collision_without_partial_move(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    current = project / "capabilities"
    legacy = project / ".capabilities"
    (current / "asana").mkdir(parents=True)
    (legacy / "asana").mkdir(parents=True)
    (current / "settings.json").write_text('{"capabilities": {}}\n')
    (legacy / "settings.json").write_text('{"capabilities": {}}\n')
    (current / "asana" / "identifiers.json").write_text('{"project": "new"}\n')
    (legacy / "asana" / "identifiers.json").write_text('{"project": "old"}\n')

    result = _run(tmp_path, project, "init", "--claude")

    assert result.returncode == 6
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "envelope_conflict"
    assert json.loads((current / "asana" / "identifiers.json").read_text())["project"] == "new"
    assert json.loads((legacy / "asana" / "identifiers.json").read_text())["project"] == "old"


def test_capability_reads_legacy_gate_before_migration(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    legacy = project / ".capabilities"
    legacy.mkdir()
    (legacy / "settings.json").write_text(json.dumps({
        "capabilities": {"youtrack": {"enabled": False}},
    }))
    env = os.environ.copy()
    env.update({"HOME": str(tmp_path / "home"), "CLAUDE_PROJECT_DIR": str(project)})

    result = subprocess.run(
        [sys.executable, str(YOUTRACK), "stub"], cwd=project, env=env,
        text=True, capture_output=True, timeout=30,
    )

    assert result.returncode == 4
    assert json.loads(result.stderr)["error"]["code"] == "disabled"
