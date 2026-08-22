"""The project envelope location: asked of ContextKit, defaulted otherwise."""

import json
import os
import subprocess
import sys
from pathlib import Path


MANAGER = Path(__file__).parents[1] / "bin" / "capabilities"
YOUTRACK = Path(__file__).parents[1] / "capabilities" / "youtrack" / "bin" / "youtrack"


def _stderr_error(result: subprocess.CompletedProcess[str]) -> dict:
    line = next(line for line in reversed(result.stderr.splitlines())
                if line.lstrip().startswith("{"))
    return json.loads(line)["error"]


def _project(tmp_path: Path, contextkit: bool, body_root: str | None = None) -> Path:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    if contextkit:
        config = project / ".contextkit" / "config.toml"
        config.parent.mkdir(parents=True)
        body = 'version = 1\ntype = "agent-project"\n'
        if body_root:
            body += f'\n[body]\nroot = "{body_root}"\n'
        config.write_text(body)
    return project


def _fake_contextkit(tmp_path: Path, envelope: Path | None, exit_code: int = 0) -> Path:
    """A stand-in `contextkit` that logs each call. Returns the log path."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "contextkit-calls.log"
    script = bin_dir / "contextkit"
    body = f'#!/bin/sh\necho "$@" >> "{log}"\n'
    if envelope is not None:
        body += f'printf "%s\\n" "{envelope}"\n'
    body += f"exit {exit_code}\n"
    script.write_text(body)
    script.chmod(0o755)
    return log


def _env(tmp_path: Path, project: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "PATH": f"{tmp_path / 'fakebin'}{os.pathsep}{env.get('PATH', '')}",
    })
    env.pop("CAPABILITIES_PROJECT_ENVELOPE", None)
    return env


def _run(tmp_path: Path, project: Path, *args: str,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MANAGER), *args],
        cwd=project, env=env or _env(tmp_path, project),
        text=True, capture_output=True, timeout=60,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_contextkit_body_root_relocates_the_whole_envelope(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    envelope = project / "agent" / "capabilities"
    log = _fake_contextkit(tmp_path, envelope)

    init = _json(_run(tmp_path, project, "init"))
    enabled = _json(_run(tmp_path, project, "enable", "asana", "--project"))

    assert init["gate"] == str(envelope.resolve() / "settings.json")
    assert (envelope / "settings.json").is_file()
    assert "*/state/" in (envelope / ".gitignore").read_text()
    assert not (project / "capabilities").exists()
    gate = json.loads((envelope / "settings.json").read_text())
    assert gate["capabilities"]["asana"]["enabled"] is True
    assert enabled["gate"] == str(envelope.resolve() / "settings.json")
    assert log.read_text().splitlines() == ["path capabilities"] * 2

    listed = _json(_run(tmp_path, project, "list"))
    assert listed["project_envelope"] == str(envelope.resolve())


def test_capability_reads_the_gate_from_the_resolved_envelope(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    envelope = project / "agent" / "capabilities"
    envelope.mkdir(parents=True)
    (envelope / "settings.json").write_text(json.dumps({
        "capabilities": {"youtrack": {"enabled": False}},
    }))
    _fake_contextkit(tmp_path, envelope)

    result = subprocess.run(
        [str(YOUTRACK), "refs"], cwd=project,
        env=_env(tmp_path, project), text=True, capture_output=True, timeout=60,
    )

    # `disabled` (not `not_enabled`) proves the relocated project gate was read.
    assert result.returncode == 4
    assert _stderr_error(result)["code"] == "disabled"


def test_contextkit_without_body_root_keeps_the_root_envelope(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)
    log = _fake_contextkit(tmp_path, project / "capabilities")

    init = _json(_run(tmp_path, project, "init"))

    assert init["gate"] == str((project / "capabilities" / "settings.json").resolve())
    assert (project / "capabilities" / "settings.json").is_file()
    assert log.read_text().splitlines() == ["path capabilities"]


def test_project_without_contextkit_never_asks(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    log = _fake_contextkit(tmp_path, project / "agent" / "capabilities")

    init = _json(_run(tmp_path, project, "init"))

    assert init["gate"] == str((project / "capabilities" / "settings.json").resolve())
    assert (project / "capabilities" / "settings.json").is_file()
    assert not log.exists()


def test_contextkit_absent_from_path_is_a_structured_error(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    (tmp_path / "fakebin").mkdir()
    env = _env(tmp_path, project)
    env["PATH"] = str(tmp_path / "fakebin")

    result = _run(tmp_path, project, "init", env=env)

    assert result.returncode == 6
    assert json.loads(result.stderr)["error"]["code"] == "contextkit_unavailable"
    assert not (project / "capabilities").exists()
    assert not (project / "agent").exists()


def test_failing_contextkit_is_a_structured_error(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    log = _fake_contextkit(tmp_path, None, exit_code=1)

    result = _run(tmp_path, project, "init")

    assert log.exists()
    assert result.returncode == 6
    assert json.loads(result.stderr)["error"]["code"] == "contextkit_path_failed"
    assert not (project / "capabilities").exists()
    assert not (project / "agent").exists()


def test_answer_outside_the_project_is_a_structured_error(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)
    outside = tmp_path / "elsewhere" / "capabilities"
    _fake_contextkit(tmp_path, outside)

    result = _run(tmp_path, project, "init")

    assert result.returncode == 6
    assert json.loads(result.stderr)["error"]["code"] == "project_envelope_outside_project"
    assert not outside.exists()


def test_fragment_composed_for_a_context_owner_never_calls_it_back(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    envelope = project / "agent" / "capabilities"
    envelope.mkdir(parents=True)
    (envelope / "settings.json").write_text(json.dumps({"capabilities": {}}))
    log = _fake_contextkit(tmp_path, project / "capabilities")
    env = _env(tmp_path, project)
    env["CAPABILITIES_PROJECT_ENVELOPE"] = str(envelope)

    result = _run(tmp_path, project, "context", "--fragment", env=env)

    assert result.returncode == 0, result.stderr
    assert not log.exists()


def test_supplied_envelope_replaces_the_lookup(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    envelope = project / "agent" / "capabilities"
    log = _fake_contextkit(tmp_path, project / "capabilities")
    env = _env(tmp_path, project)
    env["CAPABILITIES_PROJECT_ENVELOPE"] = str(envelope)

    init = _json(_run(tmp_path, project, "init", env=env))

    assert init["gate"] == str(envelope.resolve() / "settings.json")
    assert not log.exists()


def test_path_json_reports_plain_project_provenance(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)

    answer = _json(_run(tmp_path, project, "path", "--json"))

    assert answer == {
        "project_root": str(project.resolve()),
        "project_envelope": str((project / "capabilities").resolve()),
        "provider": "default",
    }


def test_path_json_reports_contextkit_provenance(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    envelope = project / "agent" / "capabilities"
    _fake_contextkit(tmp_path, envelope)

    answer = _json(_run(tmp_path, project, "path", "--json"))

    assert answer["project_root"] == str(project.resolve())
    assert answer["project_envelope"] == str(envelope.resolve())
    assert answer["provider"] == "contextkit"


def test_path_json_reports_handoff_provenance_without_contextkit(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    envelope = project / "agent" / "capabilities"
    env = _env(tmp_path, project)
    env["CAPABILITIES_PROJECT_ENVELOPE"] = str(envelope)

    answer = _json(_run(tmp_path, project, "path", "--json", env=env))

    assert answer["project_envelope"] == str(envelope.resolve())
    assert answer["provider"] == "handoff"


def test_path_requires_json_output(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)

    result = _run(tmp_path, project, "path")

    assert result.returncode == 6
    assert json.loads(result.stderr)["error"]["code"] == "input"
