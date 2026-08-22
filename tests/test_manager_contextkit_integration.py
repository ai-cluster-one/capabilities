import json
import os
import subprocess
import sys
from pathlib import Path


MANAGER = Path(__file__).parents[1] / "bin" / "capabilities"
YOUTRACK = Path(__file__).parents[1] / "capabilities" / "youtrack" / "bin" / "youtrack"
TELEGRAM = Path(__file__).parents[1] / "capabilities" / "telegram" / "bin" / "telegram"
AUTOMATIONS = Path(__file__).parents[1] / "capabilities" / "automations" / "bin" / "automations"


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


def _stderr_error(result: subprocess.CompletedProcess[str]) -> dict:
    line = next(line for line in reversed(result.stderr.splitlines())
                if line.lstrip().startswith("{"))
    return json.loads(line)["error"]


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


def test_context_fragment_is_manager_owned_and_write_free(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)
    capability_dir = project / "capabilities" / "asana"
    capability_dir.mkdir(parents=True)
    (project / "capabilities" / "settings.json").write_text(json.dumps({
        "capabilities": {"asana": {"enabled": True}},
    }))
    (capability_dir / "identifiers.json").write_text(json.dumps({
        "workspace": {"value": "123", "note": "Asana workspace"},
    }))
    registry = tmp_path / "registry" / "asana"
    registry.mkdir(parents=True)
    (registry / "asana").write_text("#!/bin/sh\n")
    (registry / "stub").write_text("Create and update work in Asana.\n")
    (registry / "manifest.json").write_text(json.dumps({
        "docs": {"topics": ["tasks", "projects"]},
    }))

    result = _run(tmp_path, project, "context", "--fragment")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("# Capabilities\n")
    assert "managed by `capabilities`" in result.stdout
    assert "`capabilities list`" in result.stdout
    assert "`contextkit build` in a ContextKit-bound project" in result.stdout
    assert "## asana" in result.stdout
    assert "Create and update work in Asana." in result.stdout
    assert "Identifiers (1): run `capabilities ids asana`" in result.stdout
    assert "Guides (`asana guide <topic>`): projects, tasks" in result.stdout
    assert "<!-- capabilities:end -->" in result.stdout
    assert not (project / ".claude").exists()
    assert not (project / ".codex").exists()


def test_contextkit_enable_updates_gate_without_generating_context(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)

    result = _json(_run(tmp_path, project, "enable", "asana", "--project"))

    gate = json.loads((project / "capabilities" / "settings.json").read_text())
    assert gate["capabilities"]["asana"]["enabled"] is True
    assert result["context"]["owner"] == "contextkit"
    assert result["context"]["refresh"] == "contextkit build --target all"
    assert not (project / ".claude").exists()
    assert not (project / ".codex").exists()


def test_policy_change_requires_explicit_scope(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)

    result = _run(tmp_path, project, "enable", "asana")

    assert result.returncode == 6
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "scope_required"
    assert "--project or --global" in error["message"]
    assert not (project / "capabilities" / "settings.json").exists()
    assert not (tmp_path / "config" / "capabilities" / "settings.json").exists()


def test_global_policy_is_inherited_and_project_policy_overrides(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)

    global_result = _json(_run(tmp_path, project, "enable", "asana", "--global"))
    listing = _json(_run(tmp_path, project, "list"))
    asana = next(entry for entry in listing["installed"] if entry["name"] == "asana") \
        if listing["installed"] else None

    global_gate = tmp_path / "config" / "capabilities" / "settings.json"
    assert json.loads(global_gate.read_text())["capabilities"]["asana"]["enabled"] is True
    assert global_result["scope"] == "global"
    # This isolated test registry has no installed entries; effective enabled is
    # still reported as an enabled-not-installed policy declaration.
    assert "asana" in listing["enabled_not_installed"]
    assert asana is None

    _json(_run(tmp_path, project, "disable", "asana", "--project"))
    gate = json.loads((project / "capabilities" / "settings.json").read_text())
    assert gate["capabilities"]["asana"]["enabled"] is False


def test_doctor_validates_global_policy(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    global_gate = tmp_path / "config" / "capabilities" / "settings.json"
    global_gate.parent.mkdir(parents=True)
    global_gate.write_text(json.dumps({
        "capabilities": {"asana": {"enabled": "yes"}},
    }))

    result = _run(tmp_path, project, "doctor")

    assert result.returncode == 7
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert "global: policy entry asana needs boolean 'enabled'" in report["findings"]


def test_context_does_not_render_connection_menu(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)
    capdir = project / "capabilities" / "asana"
    capdir.mkdir(parents=True)
    (project / "capabilities" / "settings.json").write_text(json.dumps({
        "capabilities": {"asana": {"enabled": True}},
    }))
    (capdir / "connections.json").write_text(json.dumps({
        "default": "work",
        "connections": {"work": {"allow_write": False}},
    }))
    registry = tmp_path / "registry" / "asana"
    registry.mkdir(parents=True)
    (registry / "asana").write_text("#!/bin/sh\n")
    (registry / "stub").write_text("Create and update work in Asana.\n")
    (registry / "manifest.json").write_text(json.dumps({"docs": {"topics": []}}))

    result = _run(tmp_path, project, "context", "--fragment")

    assert result.returncode == 0, result.stderr
    assert "## asana" in result.stdout
    assert "Connections (`--connection" not in result.stdout
    assert "work (read-only)" not in result.stdout


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
        [str(YOUTRACK), "refs"], cwd=project, env=env,
        text=True, capture_output=True, timeout=30,
    )

    assert result.returncode == 4
    assert _stderr_error(result)["code"] == "disabled"


def test_capability_inherits_global_policy_with_default_deny(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    config = tmp_path / "config"
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config),
        "CLAUDE_PROJECT_DIR": str(project),
    })

    denied = subprocess.run(
        [str(YOUTRACK), "refs"], cwd=project, env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert denied.returncode == 4
    assert _stderr_error(denied)["code"] == "not_enabled"

    global_gate = config / "capabilities" / "settings.json"
    global_gate.parent.mkdir(parents=True)
    global_gate.write_text(json.dumps({
        "capabilities": {"youtrack": {"enabled": True}},
    }))
    inherited = subprocess.run(
        [str(YOUTRACK), "refs"], cwd=project, env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert inherited.returncode == 0, inherited.stderr

    project_gate = project / "capabilities" / "settings.json"
    project_gate.parent.mkdir(parents=True)
    project_gate.write_text(json.dumps({
        "capabilities": {"youtrack": {"enabled": False}},
    }))
    overridden = subprocess.run(
        [str(YOUTRACK), "refs"], cwd=project, env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert overridden.returncode == 4
    assert json.loads(overridden.stderr)["error"]["code"] == "disabled"


def test_connection_bearing_capability_requires_registry(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    config = tmp_path / "config"
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config),
        "CLAUDE_PROJECT_DIR": str(project),
    })

    missing = subprocess.run(
        [str(YOUTRACK), "connections"], cwd=project, env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert missing.returncode == 6
    assert _stderr_error(missing)["code"] == "connections_required"

    registry = config / "youtrack" / "connections.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({
        "default": "work",
        "connections": {"work": {}},
    }))
    declared = subprocess.run(
        [str(YOUTRACK), "connections"], cwd=project, env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert declared.returncode == 0, declared.stderr
    report = json.loads(declared.stdout)
    assert report["default"] == "work"
    assert set(report["connections"]) == {"work"}


def test_service_init_requires_explicit_project_enable(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    capdir = project / "capabilities"
    capdir.mkdir()
    (capdir / "settings.json").write_text('{"capabilities": {}}\n')
    config = tmp_path / "config"
    global_gate = config / "capabilities" / "settings.json"
    global_gate.parent.mkdir(parents=True)
    global_gate.write_text(json.dumps({
        "capabilities": {"telegram": {"enabled": True}},
    }))
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CLAUDE_PROJECT_DIR": str(project),
    })

    inherited = subprocess.run(
        [str(TELEGRAM), "service", "init"], cwd=project, env=env,
        text=True, capture_output=True, timeout=60,
    )
    assert inherited.returncode == 4
    error_line = inherited.stderr.strip().splitlines()[-1]
    assert json.loads(error_line)["error"]["code"] == "project_enable_required"

    (capdir / "settings.json").write_text(json.dumps({
        "capabilities": {"telegram": {"enabled": True}},
    }))
    local = subprocess.run(
        [str(TELEGRAM), "service", "init"], cwd=project, env=env,
        text=True, capture_output=True, timeout=60,
    )
    assert local.returncode == 0, local.stderr
    assert (capdir / "telegram" / "service" / "settings.json").is_file()


def test_automations_service_init_requires_explicit_project_enable(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    capdir = project / "capabilities"
    capdir.mkdir()
    (capdir / "settings.json").write_text('{"capabilities": {}}\n')
    config = tmp_path / "config"
    global_gate = config / "capabilities" / "settings.json"
    global_gate.parent.mkdir(parents=True)
    global_gate.write_text(json.dumps({
        "capabilities": {"automations": {"enabled": True}},
    }))
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CLAUDE_PROJECT_DIR": str(project),
    })

    inherited = subprocess.run(
        [str(AUTOMATIONS), "service", "init"], cwd=project, env=env,
        text=True, capture_output=True, timeout=60,
    )
    assert inherited.returncode == 4
    assert json.loads(inherited.stderr)["error"]["code"] == "project_enable_required"

    (capdir / "settings.json").write_text(json.dumps({
        "capabilities": {"automations": {"enabled": True}},
    }))
    local = subprocess.run(
        [str(AUTOMATIONS), "service", "init"], cwd=project, env=env,
        text=True, capture_output=True, timeout=60,
    )
    assert local.returncode == 0, local.stderr
    assert (capdir / "automations" / "service" / "config.toml").is_file()
