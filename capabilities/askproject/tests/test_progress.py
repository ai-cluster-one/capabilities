import importlib.util
import json
import os
import subprocess
import sys
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "askproject", CAPABILITY / "askproject")
    if path.is_file()), CAPABILITY / "bin" / "askproject")


def _load_cli():
    """Load the CLI as a module so defaults are asserted from their one home."""
    spec = importlib.util.spec_from_loader(
        "askproject_cli", SourceFileLoader("askproject_cli", str(SCRIPT)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = _load_cli()


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


def test_database_project_migrates_session_map_from_file(tmp_path, monkeypatch):
    root = tmp_path / "caller"
    envelope = root / "capabilities"
    state_file = envelope / "askproject" / "state" / "sessions.json"
    state_file.parent.mkdir(parents=True)
    project_id = str(uuid.uuid4())
    slug = "fixture-" + project_id[:8]
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": project_id,
        "slug": slug, "store": "db",
    }))
    original = {"/tmp/target": {"last_session_id": "thread-1"}}
    state_file.write_text(json.dumps(original))
    store_path = tmp_path / "store.db"
    with CLI.SQLiteStore.open(str(store_path)) as store:
        store.migrate()
        store.project_register(project_id, slug)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("CAPABILITIES_PROJECT_ENVELOPE", str(envelope))
    monkeypatch.setenv("CAPABILITIES_STORE_URL", str(store_path))

    assert CLI.load_state() == original
    updated = {**original, "/tmp/other": {"last_session_id": "thread-2"}}
    CLI.save_state(updated)
    with CLI.SQLiteStore.open(str(store_path)) as store:
        assert store.state_get("askproject", "sessions", ("project", slug)) == updated


CODEX_TIMEOUT_FAKE = r'''#!/usr/bin/env python3
import json
import time

print(json.dumps({
    "type": "thread.started", "thread_id": "codex-timeout-thread"
}), flush=True)
time.sleep(5)
'''


CODEX_TIMEOUT_WITHOUT_SESSION_FAKE = r'''#!/usr/bin/env python3
import time

time.sleep(5)
'''


CODEX_RESUME_ACT_FAKE = r'''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:3] != ["exec", "resume", "codex-timeout-thread"]:
    print(f"unexpected resume args: {args[:3]}", file=sys.stderr)
    raise SystemExit(20)
if "--dangerously-bypass-approvals-and-sandbox" not in args:
    print("resume lost act mode", file=sys.stderr)
    raise SystemExit(21)
if any("read-only" in value for value in args):
    print("resume unexpectedly became read-only", file=sys.stderr)
    raise SystemExit(22)

outfile = args[args.index("-o") + 1]
print(json.dumps({
    "type": "thread.started", "thread_id": "codex-timeout-thread"
}), flush=True)
print(json.dumps({
    "type": "turn.completed",
    "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}
}), flush=True)
Path(outfile).write_text("RESUMED ACT SESSION")
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

if "--effort" not in args:
    print("claude was not given a reasoning effort", file=sys.stderr)
    raise SystemExit(11)
if args[args.index("--model") + 1] != os.environ.get("EXPECT_MODEL", ""):
    print("unexpected model", file=sys.stderr)
    raise SystemExit(12)
if args[args.index("--effort") + 1] != os.environ.get("EXPECT_EFFORT", ""):
    print("unexpected effort", file=sys.stderr)
    raise SystemExit(13)

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
            open_stdin: bool = False,
            expect_model: str | None = None,
            expect_effort: str | None = None):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    _write_executable(fake_bin / engine, fake)

    caller = tmp_path / "caller"
    target = tmp_path / "target"
    caller.mkdir(exist_ok=True)
    target.mkdir(exist_ok=True)
    (caller / ".git").mkdir(exist_ok=True)
    capdir = caller / "capabilities"
    capdir.mkdir(exist_ok=True)
    (capdir / "settings.json").write_text(json.dumps({
        "capabilities": {"askproject": {"enabled": True}},
    }) + "\n")

    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["EXPECT_MODEL"] = expect_model or CLI.READ_MODEL
    env["EXPECT_EFFORT"] = expect_effort or CLI.DEFAULT_EFFORT
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


def test_claude_picks_sonnet_for_read_and_opus_for_act(tmp_path):
    for name, extra, expected in (("read", (), CLI.READ_MODEL),
                                  ("act", ("--act",), CLI.ACT_MODEL)):
        case = tmp_path / name
        case.mkdir()
        proc = _invoke(case, "claude", CLAUDE_FAKE, "--quiet", *extra,
                       expect_model=expected)

        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["model"] == expected
        assert result["effort"] == CLI.DEFAULT_EFFORT


def test_claude_effort_and_model_overrides_reach_the_peer(tmp_path):
    for name, extra in (("read", ()), ("act", ("--act",))):
        case = tmp_path / name
        case.mkdir()
        proc = _invoke(case, "claude", CLAUDE_FAKE, "--quiet", *extra,
                       "--effort", "low", "--model", "haiku",
                       expect_model=CLI.MODEL_ALIASES["haiku"], expect_effort="low")

        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["model"] == CLI.MODEL_ALIASES["haiku"]
        assert result["effort"] == "low"


def test_unknown_effort_fails_with_a_controlled_error(tmp_path):
    proc = _invoke(tmp_path, "claude", CLAUDE_FAKE, "--quiet", "--effort", "turbo")

    assert proc.returncode == 1
    result = json.loads(proc.stdout)
    assert result["ok"] is False
    assert "turbo" in result["error"]
    for level in CLI.EFFORT_LEVELS:
        assert level in result["error"]


def test_codex_reports_no_effort(tmp_path):
    proc = _invoke(tmp_path, "codex", CODEX_FAKE, "--quiet")

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["effort"] is None


def test_default_timeout_is_one_hour():
    assert CLI.DEFAULT_TIMEOUT == 3600
    assert "default 3600" in CLI.__doc__


def test_timed_out_act_session_can_resume_without_repeating_act(tmp_path):
    timed_out = _invoke(
        tmp_path, "codex", CODEX_TIMEOUT_FAKE, "--act", "--timeout", "1")

    assert timed_out.returncode == 1
    assert "resume it with -c" in json.loads(timed_out.stdout)["error"]

    resumed = _invoke(tmp_path, "codex", CODEX_RESUME_ACT_FAKE, "-c")

    assert resumed.returncode == 0, resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["mode"] == "act"
    assert result["resumed"] is True
    assert result["answer"] == "RESUMED ACT SESSION"


def test_timeout_without_session_id_does_not_fall_back_to_older_session(tmp_path):
    completed = _invoke(tmp_path, "codex", CODEX_FAKE, "--quiet")
    assert completed.returncode == 0, completed.stderr

    timed_out = _invoke(
        tmp_path, "codex", CODEX_TIMEOUT_WITHOUT_SESSION_FAKE,
        "--act", "--timeout", "1")
    assert timed_out.returncode == 1

    resumed = _invoke(tmp_path, "codex", CODEX_FAKE, "-c", "--quiet")

    assert resumed.returncode == 1
    error = json.loads(resumed.stdout)["error"]
    assert "timed out before its session id was observed" in error
    assert "without -c" in error
