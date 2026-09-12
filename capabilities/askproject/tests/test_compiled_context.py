"""The result names the compiled project-context file the peer read, or nothing.

A target whose project context is build output leaves the peer reading whatever
snapshot is on disk, so the caller needs the artifact and its age to judge the
answer. It is a fact and never a verdict, and a target that compiles no such
file must produce no field, no warning, and no change in behaviour.

The artifact is declared by a file the target controls and reported into the
caller's result, so a declaration naming anything but a regular file inside the
target must be answered with that same silence rather than with the existence
and modification time of a path on the caller's machine.
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


# ── The declaration is the target's, the result is the caller's ──────────────
# A checked-in config in any cloned repository would otherwise turn a green
# askproject call into a probe of the caller's own filesystem, reporting the
# existence, resolved path and modification time of a file it names.


def _declares(tmp_path: Path, output: str) -> Path:
    """A bound target whose declaration is exactly `output`, verbatim."""
    return _bind(_project(tmp_path / "bound"),
                 f'[targets.claude]\noutput = "{output}"\n', None, "")


def test_a_declaration_climbing_out_of_the_target_reports_nothing(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("# not the target's\n")
    target = _declares(tmp_path, "../outside.md")

    result = _ask(tmp_path, target)

    assert result["ok"] is True
    assert result["compiled_context"] is None


def test_an_absolute_declaration_reports_nothing(tmp_path):
    outside = tmp_path / "elsewhere" / "absolute.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("# not the target's\n")
    target = _declares(tmp_path, str(outside))

    result = _ask(tmp_path, target)

    # An absolute right-hand side wins a pathlib join outright; nothing about
    # the target bounds it, so containment is what refuses it.
    assert result["ok"] is True
    assert result["compiled_context"] is None


def test_a_declaration_under_home_reports_nothing(tmp_path):
    target = _declares(tmp_path, "$HOME/.ssh")

    result = _ask(tmp_path, target)

    # No shell or user expansion is performed on the declaration, so the name
    # stays a literal segment of the target and no home path is ever stat-ed.
    assert result["ok"] is True
    assert result["compiled_context"] is None


def test_a_symlink_leaving_the_target_reports_nothing(tmp_path):
    outside = tmp_path / "outside" / "secret.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("# not the target's\n")
    target = _declares(tmp_path, "linked.md")
    (target / "linked.md").symlink_to(outside)

    result = _ask(tmp_path, target)

    # Containment is judged after every link is followed: reaching out through
    # one discloses the outside path exactly as naming it would.
    assert result["ok"] is True
    assert result["compiled_context"] is None


def test_a_symlink_staying_inside_the_target_is_still_named(tmp_path):
    target = _declares(tmp_path, "linked.md")
    real = target / "build" / "CONTEXT.md"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("# compiled\n")
    (target / "linked.md").symlink_to(real)

    reported = _ask(tmp_path, target)["compiled_context"]

    # The rule bounds where the artifact lives, not how it is reached.
    assert reported["path"] == str(real.resolve())


def test_a_declaration_naming_a_directory_reports_nothing(tmp_path):
    target = _declares(tmp_path, ".claude/rules")
    (target / ".claude" / "rules").mkdir(parents=True, exist_ok=True)

    result = _ask(tmp_path, target)

    # A directory is not the compiled artifact, and its mtime is not its age.
    assert result["ok"] is True
    assert result["compiled_context"] is None


def test_a_declaration_naming_the_target_itself_reports_nothing(tmp_path):
    target = _declares(tmp_path, ".")

    result = _ask(tmp_path, target)

    assert result["ok"] is True
    assert result["compiled_context"] is None
