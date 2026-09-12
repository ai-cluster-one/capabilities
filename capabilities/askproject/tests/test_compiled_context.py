"""The result names the compiled project-context file the peer read, or nothing.

A target whose project context is build output leaves the peer reading whatever
snapshot is on disk, so the caller needs the artifact and its age to judge the
answer. It is a fact and never a verdict, and a target that compiles no such
file must produce no field, no warning, and no change in behaviour.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "askproject", CAPABILITY / "askproject")
    if path.is_file()), CAPABILITY / "bin" / "askproject")


CLAUDE_FAKE = r'''#!/usr/bin/env python3
import json
import sys

print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "PEER ANSWER", "session_id": "claude-session",
    "duration_ms": 1, "num_turns": 1, "total_cost_usd": 0.01, "usage": {},
}), flush=True)
'''


def _project(path: Path) -> Path:
    envelope = path / "capabilities"
    envelope.mkdir(parents=True, exist_ok=True)
    (envelope / "settings.json").write_text(json.dumps({
        "capabilities": {"askproject": {"enabled": True}},
    }) + "\n")
    return path


def _bind(target: Path, body: str, output: str | None, text: str) -> Path:
    binding = target / ".contextkit"
    binding.mkdir(parents=True, exist_ok=True)
    (binding / "config.toml").write_text(body)
    if output is None:
        return target
    artifact = target / output
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(text)
    return target


def _ask(tmp_path: Path, target: Path) -> dict:
    caller = _project(tmp_path / "caller")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake = fake_bin / "claude"
    fake.write_text(CLAUDE_FAKE)
    fake.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env.pop("CLAUDE_PROJECT_DIR", None)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(target), "what is here?", "--quiet"],
        cwd=caller, env=env, text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def test_a_compiled_context_is_named_with_its_age(tmp_path):
    target = _bind(
        _project(tmp_path / "bound"),
        '[targets.claude]\noutput = ".claude/rules/CONTEXT.md"\n',
        ".claude/rules/CONTEXT.md", "# compiled\n")
    os.utime(target / ".claude" / "rules" / "CONTEXT.md",
             (time.time() - 3600, time.time() - 3600))

    reported = _ask(tmp_path, target)["compiled_context"]

    assert reported["path"] == str((target / ".claude/rules/CONTEXT.md").resolve())
    assert reported["age_seconds"] >= 3600
    assert reported["modified_at"].endswith("+00:00")
    # A fact, never a judgement about it.
    assert set(reported) == {"path", "modified_at", "age_seconds"}


def test_an_undeclared_target_falls_back_to_the_engine_default(tmp_path):
    target = _bind(_project(tmp_path / "bound"), 'version = 1\n',
                   ".claude/rules/CONTEXT.md", "# compiled\n")

    reported = _ask(tmp_path, target)["compiled_context"]

    assert reported["path"] == str((target / ".claude/rules/CONTEXT.md").resolve())


def test_a_target_that_compiles_nothing_reports_nothing(tmp_path):
    target = _project(tmp_path / "plain")

    result = _ask(tmp_path, target)

    assert result["ok"] is True
    assert result["compiled_context"] is None


def test_a_bound_target_with_no_artifact_yet_reports_nothing(tmp_path):
    target = _bind(_project(tmp_path / "bound"), 'version = 1\n', None, "")

    result = _ask(tmp_path, target)

    assert result["ok"] is True
    assert result["compiled_context"] is None


def test_an_unreadable_binding_never_fails_the_call(tmp_path):
    target = _bind(_project(tmp_path / "broken"), 'this is not = = toml\n',
                   ".claude/rules/CONTEXT.md", "# compiled\n")

    result = _ask(tmp_path, target)

    # The declaration is unreadable, so the engine default answers instead —
    # and under no circumstance does the call itself stop working.
    assert result["ok"] is True
    assert result["compiled_context"]["path"] == str(
        (target / ".claude/rules/CONTEXT.md").resolve())
