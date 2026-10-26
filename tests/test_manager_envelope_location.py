"""The project envelope location: read from a ContextKit binding, defaulted otherwise.

A stand-in `contextkit` that logs each call proves the manager never asks it."""

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


def _in_files_mode(envelope: Path) -> None:
    """An existing agent in files mode: a new one would start in the store."""
    envelope.mkdir(parents=True, exist_ok=True)
    (envelope / "settings.json").write_text('{"capabilities": {}}\n')


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


def _envelope_lookups(log: Path) -> list[str]:
    """The envelope questions asked of ContextKit, leaving out the identity
    question a bound project's init also asks."""
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines()
            if not line.startswith("identity ")]


def _env(tmp_path: Path, project: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
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
    _in_files_mode(envelope)

    init = _json(_run(tmp_path, project, "init"))
    enabled = _json(_run(tmp_path, project, "enable", "asana", "--project"))

    assert init["gate"] == str(envelope.resolve() / "settings.json")
    assert (envelope / "settings.json").is_file()
    assert "*/state/" in (envelope / ".gitignore").read_text()
    assert not (project / "capabilities").exists()
    gate = json.loads((envelope / "settings.json").read_text())
    assert gate["capabilities"]["asana"]["enabled"] is True
    assert enabled["gate"] == str(envelope.resolve() / "settings.json")
    assert _envelope_lookups(log) == []

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
    _in_files_mode(project / "capabilities")

    init = _json(_run(tmp_path, project, "init"))

    assert init["gate"] == str((project / "capabilities" / "settings.json").resolve())
    assert (project / "capabilities" / "settings.json").is_file()
    assert _envelope_lookups(log) == []


def test_project_without_contextkit_never_asks(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)
    log = _fake_contextkit(tmp_path, project / "agent" / "capabilities")
    _in_files_mode(project / "capabilities")

    init = _json(_run(tmp_path, project, "init"))

    assert init["gate"] == str((project / "capabilities" / "settings.json").resolve())
    assert (project / "capabilities" / "settings.json").is_file()
    assert not log.exists()


def test_a_bound_project_resolves_without_contextkit_on_path(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    (tmp_path / "fakebin").mkdir()
    env = _env(tmp_path, project)
    env["PATH"] = str(tmp_path / "fakebin")

    answer = _json(_run(tmp_path, project, "path", "--json", env=env))

    assert answer["project_envelope"] == str((project / "agent" / "capabilities").resolve())
    assert answer["body_root"] == "agent"


def test_a_body_root_outside_the_project_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="../elsewhere")

    result = _run(tmp_path, project, "init")

    assert result.returncode == 6
    assert json.loads(result.stderr)["error"]["code"] == "body_path_invalid"
    assert not (project / "capabilities").exists()
    assert not (tmp_path / "elsewhere").exists()


def test_a_hidden_body_root_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root=".agent")

    result = _run(tmp_path, project, "path", "--json")

    assert result.returncode == 6
    assert json.loads(result.stderr)["error"]["code"] == "body_path_invalid"


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
    _in_files_mode(envelope)

    init = _json(_run(tmp_path, project, "init", env=env))

    assert init["gate"] == str(envelope.resolve() / "settings.json")
    assert _envelope_lookups(log) == []


def test_path_json_reports_plain_project_provenance(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=False)

    answer = _json(_run(tmp_path, project, "path", "--json"))

    assert answer == {
        "project_root": str(project.resolve()),
        "project_envelope": str((project / "capabilities").resolve()),
        "provider": "default",
        "body_root": ".",
        "layers": {name: str((project / name).resolve())
                   for name in ("context", "assets", "routines", "capabilities")},
        "worktree_root": os.environ.get("XDG_DATA_HOME")
        or str(tmp_path / "home" / ".local" / "share"),
    }


def test_path_json_reports_contextkit_provenance(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    envelope = project / "agent" / "capabilities"
    _fake_contextkit(tmp_path, envelope)

    answer = _json(_run(tmp_path, project, "path", "--json"))

    assert answer["project_root"] == str(project.resolve())
    assert answer["project_envelope"] == str(envelope.resolve())
    assert answer["provider"] == "contextkit"
    assert answer["body_root"] == "agent"
    assert answer["layers"]["context"] == str((project / "agent" / "context").resolve())


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


def _records(tmp_path: Path) -> list[Path]:
    root = tmp_path / "cache" / "capabilities" / "envelope"
    return sorted(root.glob("*.json")) if root.is_dir() else []


def _record(tmp_path: Path) -> dict:
    entries = _records(tmp_path)
    assert len(entries) == 1, entries
    return json.loads(entries[0].read_text())


def test_a_second_process_reads_the_recorded_answer(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    envelope = project / "agent" / "capabilities"
    log = _fake_contextkit(tmp_path, envelope)

    first = _json(_run(tmp_path, project, "path", "--json"))
    second = _json(_run(tmp_path, project, "path", "--json"))

    assert first == second
    assert second["project_envelope"] == str(envelope.resolve())
    assert not log.exists()
    assert _record(tmp_path)["root"] == str(project.resolve())


def test_a_capability_reads_the_record_without_starting_the_manager(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    envelope = project / "agent" / "capabilities"
    envelope.mkdir(parents=True)
    (envelope / "settings.json").write_text(json.dumps({
        "capabilities": {"youtrack": {"enabled": False}},
    }))
    _fake_contextkit(tmp_path, envelope)
    _json(_run(tmp_path, project, "path", "--json"))

    # A manager that refuses to answer proves the record carried the resolution.
    refuser = tmp_path / "fakebin" / "refusing-manager"
    refuser.write_text("#!/bin/sh\nexit 9\n")
    refuser.chmod(0o755)
    env = _env(tmp_path, project)
    env["CAPABILITIES_MANAGER_BIN"] = str(refuser)

    result = subprocess.run(
        [str(YOUTRACK), "refs"], cwd=project,
        env=env, text=True, capture_output=True, timeout=60,
    )

    # `disabled` (not a manager failure) proves the relocated gate was read.
    assert result.returncode == 4, result.stderr
    assert _stderr_error(result)["code"] == "disabled"


def test_a_changed_context_binding_is_resolved_again(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    first = _json(_run(tmp_path, project, "path", "--json"))
    assert first["project_envelope"] == str((project / "agent" / "capabilities").resolve())

    (project / ".contextkit" / "config.toml").write_text(
        'version = 1\ntype = "agent-project"\n\n[body]\nroot = "workbench"\n')

    answer = _json(_run(tmp_path, project, "path", "--json"))

    assert answer["project_envelope"] == str((project / "workbench" / "capabilities").resolve())


def test_a_damaged_record_costs_a_reading_rather_than_the_answer(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    envelope = project / "agent" / "capabilities"
    log = _fake_contextkit(tmp_path, envelope)
    _json(_run(tmp_path, project, "path", "--json"))
    _records(tmp_path)[0].write_text("{ this is not json")

    answer = _json(_run(tmp_path, project, "path", "--json"))

    assert answer["project_envelope"] == str(envelope.resolve())
    assert not log.exists()


def test_each_project_root_keeps_its_own_record(tmp_path: Path) -> None:
    first = _project(tmp_path, contextkit=True, body_root="agent")
    second = tmp_path / "second"
    (second / ".git").mkdir(parents=True)
    _fake_contextkit(tmp_path, first / "agent" / "capabilities")

    first_answer = _json(_run(tmp_path, first, "path", "--json"))
    second_answer = _json(_run(tmp_path, second, "path", "--json",
                               env={**_env(tmp_path, second)}))

    assert first_answer["project_envelope"] == str((first / "agent" / "capabilities").resolve())
    assert second_answer["project_envelope"] == str((second / "capabilities").resolve())
    assert len(_records(tmp_path)) == 2


def test_a_handed_off_answer_is_never_recorded(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    log = _fake_contextkit(tmp_path, project / "capabilities")
    env = _env(tmp_path, project)
    env["CAPABILITIES_PROJECT_ENVELOPE"] = str(project / "agent" / "capabilities")

    answer = _json(_run(tmp_path, project, "path", "--json", env=env))

    assert answer["provider"] == "handoff"
    assert not log.exists()
    assert _records(tmp_path) == []


def test_a_layer_naming_the_body_itself_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True)
    config = project / ".contextkit" / "config.toml"
    config.write_text(config.read_text() + '\n[sources]\ncapabilities = "."\n')

    result = _run(tmp_path, project, "path", "--json")

    assert result.returncode == 6
    assert json.loads(result.stderr)["error"]["code"] == "body_path_invalid"


def test_the_legacy_envelope_is_named_in_any_spelling(tmp_path: Path) -> None:
    project = _project(tmp_path, contextkit=True, body_root="agent")
    config = project / ".contextkit" / "config.toml"
    config.write_text(config.read_text() + '\n[sources]\ncapabilities = "./.capabilities/"\n')

    answer = _json(_run(tmp_path, project, "path", "--json"))

    assert answer["project_envelope"] == str((project / ".capabilities").resolve())
