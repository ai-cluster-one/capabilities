"""Standalone host delivery: the manager's own Claude Code and Codex wiring in a
project that no context owner has bound."""
import json
import os
import subprocess
import sys
from pathlib import Path


MANAGER = Path(__file__).parents[1] / "bin" / "capabilities"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    return project


def _run(tmp_path: Path, project: Path, *args: str,
         stdin: str | None = None) -> subprocess.CompletedProcess[str]:
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
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True,
        timeout=30,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _payload(project: Path, source: str = "startup") -> str:
    return json.dumps({
        "session_id": "session", "transcript_path": "transcript",
        "cwd": str(project), "hook_event_name": "SessionStart", "source": source,
    })


def _hook(tmp_path: Path, project: Path, host: str) -> str:
    result = _run(tmp_path, project, "context", f"--{host}", stdin=_payload(project))
    assert result.returncode == 0, result.stderr
    return result.stdout


def _install_snapshot(tmp_path: Path, name: str, stub: str) -> None:
    registry = tmp_path / "registry" / name
    registry.mkdir(parents=True)
    (registry / name).write_text("#!/bin/sh\n")
    (registry / "stub").write_text(stub + "\n")
    (registry / "manifest.json").write_text(json.dumps({"docs": {"topics": []}}))


def _enable_by_hand(project: Path, *names: str) -> None:
    """Change the gate behind the manager's back, as a merge or checkout would."""
    (project / "capabilities" / "settings.json").write_text(json.dumps({
        "capabilities": {name: {"enabled": True} for name in names},
    }))


def test_claude_hook_speaks_only_when_the_loaded_copy_is_missing_or_stale(
        tmp_path: Path) -> None:
    project = _project(tmp_path)
    _json(_run(tmp_path, project, "init", "--claude"))
    context = project / ".claude" / "rules" / "CAPABILITIES.md"

    assert _hook(tmp_path, project, "claude") == ""

    context.unlink()
    notice = _hook(tmp_path, project, "claude")
    assert notice.startswith("# Capabilities Session Notice\n")
    assert "has none loaded" in notice
    assert context.is_file()

    _install_snapshot(tmp_path, "demo", "Create and update demo records.")
    _enable_by_hand(project, "demo")
    notice = _hook(tmp_path, project, "claude")
    assert "the loaded copy is stale" in notice
    assert "## demo" in context.read_text()

    assert _hook(tmp_path, project, "claude") == ""


def test_terminal_run_keeps_its_json_result(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _json(_run(tmp_path, project, "init", "--claude", "--codex"))

    for host, rel in (("claude", ".claude/rules/CAPABILITIES.md"),
                      ("codex", ".codex/generated/capabilities.md")):
        for stdin in (None, "", json.dumps({"hook_event_name": "Stop"}), "not json"):
            result = _json(_run(tmp_path, project, "context", f"--{host}", stdin=stdin))
            assert result == {"written": str(project / rel), "enabled": []}


def test_hook_failure_is_a_notice_and_never_breaks_session_start(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _json(_run(tmp_path, project, "init", "--claude"))
    (project / "capabilities" / "settings.json").write_text(json.dumps({
        "capabilities": {"demo": {"enabled": "yes"}},
    }))

    notice = _hook(tmp_path, project, "claude")

    assert "could not rebuild the capability context" in notice
    assert "invalid capability policy entry 'demo'" in notice
    assert "{" not in notice


def test_root_agents_file_is_a_codex_delivery_blocker(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _json(_run(tmp_path, project, "init", "--claude", "--codex"))
    assert _json(_run(tmp_path, project, "doctor"))["ok"] is True
    assert _hook(tmp_path, project, "codex") == ""

    for name in ("AGENTS.md", "AGENTS.override.md"):
        agents = project / name
        agents.write_text("# Project notes\n")

        notice = _hook(tmp_path, project, "codex")
        assert "did not load in this session" in notice
        assert f"root `{name}`" in notice

        doctor = _run(tmp_path, project, "doctor")
        assert doctor.returncode == 7
        findings = json.loads(doctor.stdout)["findings"]
        assert any(f"root `{name}`" in finding for finding in findings)

        assert agents.read_text() == "# Project notes\n"
        agents.unlink()

    (project / "AGENTS.md").write_text("# Project notes\n")
    assert _hook(tmp_path, project, "claude") == ""


def test_generated_context_is_replaced_whole(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _json(_run(tmp_path, project, "init", "--claude", "--codex"))
    _install_snapshot(tmp_path, "demo", "Create and update demo records.")
    files = [project / ".claude" / "rules" / "CAPABILITIES.md",
             project / ".codex" / "generated" / "capabilities.md"]
    before = {path: path.stat().st_ino for path in files}

    _enable_by_hand(project, "demo")
    _json(_run(tmp_path, project, "context", "--claude"))
    _json(_run(tmp_path, project, "context", "--codex"))

    for path in files:
        assert path.stat().st_ino != before[path]
        assert "## demo" in path.read_text()
        assert oct(path.stat().st_mode & 0o777) == oct(0o644)
        assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_codex_hook_matcher_is_written_new_and_never_rewritten(tmp_path: Path) -> None:
    project = _project(tmp_path)
    hooks_file = project / ".codex" / "hooks.json"

    _json(_run(tmp_path, project, "init", "--codex"))
    [entry] = json.loads(hooks_file.read_text())["hooks"]["SessionStart"]
    assert entry["matcher"] == "startup|resume|clear"

    hooks = json.loads(hooks_file.read_text())
    hooks["hooks"]["SessionStart"][0]["matcher"] = "startup|resume|clear|compact"
    hooks_file.write_text(json.dumps(hooks))
    before = hooks_file.read_text()

    result = _json(_run(tmp_path, project, "init", "--codex"))

    assert result["codex"]["hook_added"] is False
    assert hooks_file.read_text() == before
    assert _json(_run(tmp_path, project, "doctor"))["ok"] is True


def test_codex_loads_the_generated_file_through_a_root_link(tmp_path: Path) -> None:
    project = _project(tmp_path)
    link = project / ".capabilities.md"

    result = _json(_run(tmp_path, project, "init", "--codex"))

    assert result["codex"]["root_link"] == "linked"
    assert link.is_symlink()
    assert os.readlink(link) == ".codex/generated/capabilities.md"
    assert link.read_text() == (project / ".codex" / "generated" / "capabilities.md").read_text()
    assert _json(_run(tmp_path, project, "doctor"))["ok"] is True

    link.unlink()
    link.symlink_to("gone.md")
    doctor = _run(tmp_path, project, "doctor")
    assert doctor.returncode == 7
    assert any("`.capabilities.md` is missing or does not link" in finding
               for finding in json.loads(doctor.stdout)["findings"])

    notice = _hook(tmp_path, project, "codex")
    assert "has none loaded" in notice
    assert os.readlink(link) == ".codex/generated/capabilities.md"
    assert _json(_run(tmp_path, project, "doctor"))["ok"] is True


def test_a_root_file_the_manager_does_not_own_is_left_and_reported(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _json(_run(tmp_path, project, "init", "--codex"))
    link = project / ".capabilities.md"
    link.unlink()
    link.write_text("# Mine\n")

    notice = _hook(tmp_path, project, "codex")

    assert "did not load in this session" in notice
    assert "`.capabilities.md` exists and is not manager-generated context" in notice
    assert link.read_text() == "# Mine\n"
    assert _run(tmp_path, project, "doctor").returncode == 7


def test_wired_project_converts_on_its_next_session_without_init(tmp_path: Path) -> None:
    for stdin in (None, "hook"):
        project = _project(tmp_path / (stdin or "terminal"))
        _json(_run(tmp_path, project, "init", "--codex"))
        # The layout an earlier release left behind: the generated file itself
        # named as the fallback, beside a name the person added.
        (project / ".capabilities.md").unlink()
        config = project / ".codex" / "config.toml"
        config.write_text(
            'project_doc_fallback_filenames = ["NOTES.md", '
            '".codex/generated/capabilities.md", ".codex/generated/context.md"]\n'
            "project_doc_max_bytes = 131072\n")
        assert _run(tmp_path, project, "doctor").returncode == 7

        if stdin:
            assert "has none loaded" in _hook(tmp_path, project, "codex")
        else:
            _json(_run(tmp_path, project, "context", "--codex"))

        assert config.read_text() == (
            'project_doc_fallback_filenames = ["NOTES.md", ".capabilities.md"]\n'
            "project_doc_max_bytes = 131072\n")
        assert os.readlink(project / ".capabilities.md") == ".codex/generated/capabilities.md"
        assert _json(_run(tmp_path, project, "doctor"))["ok"] is True
        assert _hook(tmp_path, project, "codex") == ""


def test_context_does_not_wire_codex_into_an_unwired_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _json(_run(tmp_path, project, "init", "--claude"))

    _json(_run(tmp_path, project, "context", "--codex"))

    assert not (project / ".codex" / "config.toml").exists()
    assert not (project / ".capabilities.md").exists()


def test_one_unreadable_capability_does_not_cost_the_others_their_block(
        tmp_path: Path) -> None:
    project = _project(tmp_path)
    _json(_run(tmp_path, project, "init", "--claude"))
    _install_snapshot(tmp_path, "demo", "Create and update demo records.")
    _install_snapshot(tmp_path, "other", "Read and update other records.")
    _enable_by_hand(project, "demo", "other")
    (project / "capabilities" / "demo").mkdir()
    (project / "capabilities" / "demo" / "identifiers.json").write_text("{broken")
    (project / "capabilities" / "other").mkdir()
    (project / "capabilities" / "other" / "identifiers.json").write_text(json.dumps({
        "board": {"value": "7", "note": "team board"},
    }))

    result = _run(tmp_path, project, "context", "--fragment")

    assert result.returncode == 0, result.stderr
    demo, other = result.stdout.split("## demo", 1)[1].split("## other", 1)
    assert "Create and update demo records." in demo
    assert "Project records for `demo` are unavailable" in demo
    assert "Read and update other records." in other
    assert "Identifiers (1): run `capabilities ids other`" in other
    assert result.stdout.rstrip().endswith("<!-- capabilities:end -->")


def test_codex_config_keys_land_at_the_toml_root(tmp_path: Path) -> None:
    project = _project(tmp_path)
    config = project / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "m"\n\n[mcp_servers.docs]\ncommand = "docs"\n')

    _json(_run(tmp_path, project, "init", "--codex"))

    assert config.read_text() == (
        'model = "m"\n'
        'project_doc_fallback_filenames = [".capabilities.md"]\n'
        "project_doc_max_bytes = 131072\n"
        "\n"
        "[mcp_servers.docs]\n"
        'command = "docs"\n'
    )
    assert _json(_run(tmp_path, project, "init", "--codex"))["codex"]["config_changed"] is False


def test_init_lifts_keys_earlier_wiring_filed_under_a_table(tmp_path: Path) -> None:
    project = _project(tmp_path)
    config = project / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.docs]\ncommand = "docs"\n'
        'project_doc_fallback_filenames = [".codex/generated/capabilities.md"]\n'
        "project_doc_max_bytes = 131072\n")

    result = _json(_run(tmp_path, project, "init", "--codex"))

    assert result["codex"]["config_changed"] is True
    assert config.read_text() == (
        'project_doc_fallback_filenames = [".capabilities.md"]\n'
        "project_doc_max_bytes = 131072\n"
        "\n"
        "[mcp_servers.docs]\n"
        'command = "docs"\n'
    )


def test_init_refuses_host_settings_it_cannot_parse(tmp_path: Path) -> None:
    for broken in (Path(".claude") / "settings.json", Path(".codex") / "hooks.json"):
        project = _project(tmp_path / broken.parent.name)
        path = project / broken
        path.parent.mkdir(parents=True)
        path.write_text("{not json\n")

        result = _run(tmp_path, project, "init", "--claude", "--codex")

        assert result.returncode == 6
        assert "host_settings_unreadable" in result.stderr
        assert path.read_text() == "{not json\n"
        assert not (project / ".claude" / "rules" / "CAPABILITIES.md").exists()
        assert not (project / ".codex" / "config.toml").exists()


def test_init_fills_a_blank_host_settings_file(tmp_path: Path) -> None:
    for blank in (Path(".claude") / "settings.json", Path(".codex") / "hooks.json"):
        project = _project(tmp_path / blank.parent.name)
        path = project / blank
        path.parent.mkdir(parents=True)
        path.write_text("")

        result = _run(tmp_path, project, "init", "--claude", "--codex")

        assert result.returncode == 0, result.stderr
        assert "SessionStart" in json.loads(path.read_text())["hooks"]
