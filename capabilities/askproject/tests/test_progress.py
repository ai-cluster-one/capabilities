import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "askproject"


CODEX_FAKE = r'''#!/usr/bin/env python3
import json
import os
import select
import sys
from pathlib import Path

ready, _, _ = select.select([0], [], [], 0.5)
if not ready:
    print("codex inherited an open stdin", file=sys.stderr)
    raise SystemExit(8)
if os.read(0, 1) != b"":
    print("codex received unexpected stdin data", file=sys.stderr)
    raise SystemExit(9)

args = sys.argv[1:]
outfile = args[args.index("-o") + 1]

def emit(value):
    print(json.dumps(value), flush=True)

emit({"type": "thread.started", "thread_id": "codex-thread"})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {
    "type": "agent_message", "text": "I will run the focused checks now."
}})
emit({"type": "item.started", "item": {
    "type": "command_execution",
    "command": "/bin/zsh -lc 'pytest /private/project/test_secret.py'",
    "status": "in_progress"
}})
emit({"type": "item.completed", "item": {
    "type": "command_execution",
    "command": "/bin/zsh -lc 'pytest /private/project/test_secret.py'",
    "aggregated_output": "sensitive command output",
    "exit_code": 0,
    "status": "completed"
}})
emit({"type": "item.completed", "item": {
    "type": "file_change", "changes": [{"path": "/private/changed.py"}]
}})
emit({"type": "item.completed", "item": {
    "type": "agent_message", "text": "FINAL ANSWER MUST NOT BE PROGRESS"
}})
emit({"type": "turn.completed", "usage": {
    "input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 4
}})
Path(outfile).write_text("FINAL ANSWER MUST NOT BE PROGRESS")
'''


CLAUDE_FAKE = r'''#!/usr/bin/env python3
import json
import os
import select
import sys

ready, _, _ = select.select([0], [], [], 0.5)
if not ready:
    print("claude inherited an open stdin", file=sys.stderr)
    raise SystemExit(8)
if os.read(0, 1) != b"":
    print("claude received unexpected stdin data", file=sys.stderr)
    raise SystemExit(9)

args = sys.argv[1:]
streaming = "stream-json" in args
if streaming != ("--verbose" in args):
    print("expected streaming flags", file=sys.stderr)
    raise SystemExit(10)

def emit(value):
    print(json.dumps(value), flush=True)

if streaming:
    emit({
        "type": "system", "subtype": "init", "session_id": "claude-session"
    })
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "I will inspect the relevant module."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/private/code.py"}}
    ]}})
emit({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "CLAUDE FINAL MUST NOT BE PROGRESS",
    "session_id": "claude-session",
    "duration_ms": 12,
    "num_turns": 2,
    "total_cost_usd": 0.01,
    "usage": {
        "input_tokens": 8,
        "output_tokens": 5,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 1
    }
})
'''


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _invoke(tmp_path: Path, engine: str, fake: str, *extra: str,
            open_stdin: bool = False):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / engine, fake)

    caller = tmp_path / "caller"
    target = tmp_path / "target"
    caller.mkdir()
    target.mkdir()
    (caller / ".git").mkdir()

    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    read_fd = write_fd = None
    if open_stdin:
        read_fd, write_fd = os.pipe()
    try:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(target), "do the task",
             "--engine", engine, *extra],
            cwd=caller,
            env=env,
            text=True,
            capture_output=True,
            stdin=read_fd,
            timeout=10,
        )
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)


def test_codex_progress_is_concise_and_stdout_stays_json(tmp_path):
    proc = _invoke(tmp_path, "codex", CODEX_FAKE)

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    assert result["answer"] == "FINAL ANSWER MUST NOT BE PROGRESS"
    assert result["session_id"] == "codex-thread"

    assert "askproject[codex] starting: Launching codex peer" in proc.stderr
    assert "askproject[codex] update: I will run the focused checks now." in proc.stderr
    assert "askproject[codex] verify: Running tests" in proc.stderr
    assert "askproject[codex] edit: Updated project files" in proc.stderr
    assert "askproject[codex] completed: Codex peer finished" in proc.stderr
    assert "FINAL ANSWER MUST NOT BE PROGRESS" not in proc.stderr
    assert "/private/project/test_secret.py" not in proc.stderr
    assert "/private/changed.py" not in proc.stderr
    assert "sensitive command output" not in proc.stderr


def test_quiet_keeps_legacy_silent_stderr(tmp_path):
    proc = _invoke(tmp_path, "codex", CODEX_FAKE, "--quiet")

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["answer"] == "FINAL ANSWER MUST NOT BE PROGRESS"
    assert proc.stderr == ""


def test_codex_closes_inherited_open_stdin(tmp_path):
    for name, extra in (("stream", ()), ("quiet", ("--quiet",))):
        case = tmp_path / name
        case.mkdir()
        proc = _invoke(case, "codex", CODEX_FAKE, *extra, open_stdin=True)

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["answer"] == "FINAL ANSWER MUST NOT BE PROGRESS"


def test_claude_closes_inherited_open_stdin(tmp_path):
    for name, extra in (("stream", ()), ("quiet", ("--quiet",))):
        case = tmp_path / name
        case.mkdir()
        proc = _invoke(case, "claude", CLAUDE_FAKE, *extra, open_stdin=True)

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["answer"] == "CLAUDE FINAL MUST NOT BE PROGRESS"


def test_claude_progress_uses_stream_events_without_echoing_answer(tmp_path):
    proc = _invoke(tmp_path, "claude", CLAUDE_FAKE)

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    assert result["answer"] == "CLAUDE FINAL MUST NOT BE PROGRESS"
    assert result["session_id"] == "claude-session"

    assert "askproject[claude] started: Claude peer started" in proc.stderr
    assert "askproject[claude] update: I will inspect the relevant module." in proc.stderr
    assert "askproject[claude] inspect: Inspecting the project" in proc.stderr
    assert "askproject[claude] completed: Claude peer finished" in proc.stderr
    assert "CLAUDE FINAL MUST NOT BE PROGRESS" not in proc.stderr
    assert "/private/code.py" not in proc.stderr
