"""Progress on stderr, a clean JSON stdout, and the session map across calls.

The fake engines under fakes/ report a tool call, a command, a file change and
commentary beside the final answer, so what reaches stderr is read against
what the engine said. See _peer.py for how to run the suite.
"""

import importlib.util
import json
import os
import uuid
from importlib.machinery import SourceFileLoader

import pytest

from _peer import SCRIPT, Lab


def _load_cli():
    """Load the CLI as a module so its helpers are exercised from their one home."""
    spec = importlib.util.spec_from_loader(
        "askproject_cli", SourceFileLoader("askproject_cli", str(SCRIPT)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = _load_cli()


@pytest.fixture()
def lab(tmp_path):
    return Lab(tmp_path)


def _open_stdin_ask(lab, *extra):
    """An ask whose own stdin is a pipe nobody writes to or closes."""
    import subprocess
    import sys
    read_fd, write_fd = os.pipe()
    try:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(lab.target), "do the task", *extra],
            cwd=lab.caller, env=lab.env, text=True, capture_output=True,
            stdin=read_fd, timeout=60)
    finally:
        os.close(read_fd)
        os.close(write_fd)


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


def test_codex_progress_is_concise_and_stdout_stays_json(lab):
    proc, result, _ = lab.ask("--engine", "codex", quiet=False,
                              env={"FAKE_ANSWER": "FINAL ANSWER MUST NOT BE PROGRESS"})

    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True
    assert result["answer"] == "FINAL ANSWER MUST NOT BE PROGRESS"
    assert result["session_id"] == "codex-thread"

    assert "askproject[codex] starting: Launching codex peer with profile codex-read (shipped)" in proc.stderr
    assert "askproject[codex] started: Codex peer started" in proc.stderr
    assert "askproject[codex] update: I will run the focused checks now." in proc.stderr
    assert "askproject[codex] verify: Running tests" in proc.stderr
    assert "askproject[codex] edit: Updated project files" in proc.stderr
    assert "askproject[codex] completed: Codex peer finished" in proc.stderr
    assert "FINAL ANSWER MUST NOT BE PROGRESS" not in proc.stderr
    assert "/private/project/test_secret.py" not in proc.stderr
    assert "/private/changed.py" not in proc.stderr
    assert "sensitive command output" not in proc.stderr


def test_claude_progress_uses_stream_events_without_echoing_answer(lab):
    proc, result, _ = lab.ask(quiet=False,
                              env={"FAKE_ANSWER": "CLAUDE FINAL MUST NOT BE PROGRESS"})

    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True
    assert result["answer"] == "CLAUDE FINAL MUST NOT BE PROGRESS"

    assert "askproject[claude] started: Claude peer started" in proc.stderr
    assert "askproject[claude] update: I will inspect the relevant module." in proc.stderr
    assert "askproject[claude] inspect: Inspecting the project" in proc.stderr
    assert "askproject[claude] completed: Claude peer finished" in proc.stderr
    assert "CLAUDE FINAL MUST NOT BE PROGRESS" not in proc.stderr
    assert "/private/code.py" not in proc.stderr


def test_quiet_keeps_stderr_silent(lab):
    for engine in ("claude", "codex"):
        proc, result, _ = lab.ask("--engine", engine)
        assert proc.returncode == 0, proc.stderr
        assert result["answer"] == "PEER ANSWER"
        assert proc.stderr == ""


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("extra", [(), ("--quiet",)])
def test_an_inherited_open_stdin_does_not_hold_the_peer(lab, engine, extra):
    proc = _open_stdin_ask(lab, "--engine", engine, *extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["answer"] == "PEER ANSWER"


def test_unknown_effort_fails_with_a_controlled_error(lab):
    proc, result, launch = lab.ask("--effort", "turbo")

    assert proc.returncode == 1
    assert result["ok"] is False
    assert "turbo" in result["error"]
    for level in CLI.EFFORT_LEVELS:
        assert level in result["error"]
    assert launch is None


def test_claude_aliases_resolve_to_full_ids(lab):
    proc, result, _ = lab.ask("--model", "haiku", "--effort", "low")
    assert proc.returncode == 0, proc.stderr
    assert (result["model"], result["effort"]) == (CLI.MODEL_ALIASES["haiku"], "low")


def test_an_act_thread_resumes_only_when_the_follow_up_names_its_mode(lab):
    timed_out, result, _ = lab.ask("--engine", "codex", "--act", "--timeout", "1",
                                   env={"PEER_SLEEP": "5",
                                        "FAKE_THREAD_ID": "codex-timeout-thread"})
    assert timed_out.returncode == 1
    assert "resume it with -c" in result["error"]

    bare, result, launch = lab.ask("-c")
    assert bare.returncode == 1
    assert "the last session used act" in result["error"]
    assert "--act" in result["error"] and "--read" in result["error"]
    assert launch is None

    resumed, result, launch = lab.ask("-c", "--act")
    assert resumed.returncode == 0, resumed.stderr
    assert (result["mode"], result["resumed"]) == ("act", True)
    resume = next(r for r in launch["requests"] if r["method"] == "thread/resume")
    assert resume["params"]["threadId"] == "codex-timeout-thread"


def test_read_resumes_an_act_thread_in_read_mode(lab):
    timed_out, _, _ = lab.ask("--engine", "codex", "--act", "--timeout", "1",
                              env={"PEER_SLEEP": "5", "FAKE_THREAD_ID": "codex-timeout-thread"})
    assert timed_out.returncode == 1

    resumed, result, launch = lab.ask("-c", "--read")
    assert resumed.returncode == 0, resumed.stderr
    assert (result["mode"], result["resumed"], result["profile"]["name"]) == (
        "read", True, "codex-read")
    assert launch["env"]["CAPABILITIES_READ_ONLY"] == "1"


def test_act_and_read_together_are_refused(lab):
    proc, result, launch = lab.ask("--act", "--read")
    assert proc.returncode == 1
    assert "contradictory" in result["error"]
    assert launch is None


def test_timeout_without_session_id_does_not_fall_back_to_older_session(lab):
    completed, _, _ = lab.ask("--engine", "codex")
    assert completed.returncode == 0, completed.stderr

    timed_out, _, _ = lab.ask("--engine", "codex", "--act", "--timeout", "1",
                              env={"PEER_SLEEP_BEFORE": "5"})
    assert timed_out.returncode == 1

    resumed, result, _ = lab.ask("-c")
    assert resumed.returncode == 1
    assert "timed out before its session id was observed" in result["error"]
    assert "without -c" in result["error"]
