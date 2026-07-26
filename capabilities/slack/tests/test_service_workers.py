import os
import stat

import pytest
import workers
from workers import (
    WORKERS,
    WorkerTimeout,
    run_worker_proc,
    sanitized_worker_env,
    worker_stub,
)


def test_stub_echoes_last_prompt_line():
    out = worker_stub(
        "ctx\n--- Conversation ---\nAlice: hello there", cwd=".", env={}, timeout=5
    )
    assert out["reply"].startswith("[stub]")
    assert "hello there" in out["reply"]
    assert out["meta"]["harness"] == "stub"


def test_workers_registry_has_all_three():
    assert set(WORKERS) == {"stub", "claude", "codex"}


def test_run_worker_proc_success():
    rc, out, _err = run_worker_proc(
        ["/bin/echo", "hi"], cwd=".", env=dict(os.environ), timeout=10
    )
    assert rc == 0
    assert out.strip() == "hi"


def test_run_worker_proc_times_out_and_raises():
    with pytest.raises(WorkerTimeout):
        run_worker_proc(["/bin/sleep", "5"], cwd=".", env=dict(os.environ), timeout=1)


def _fake_bin(tmp_path, name, script):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def test_worker_claude_raises_on_nonzero_exit(tmp_path, monkeypatch):
    from workers import worker_claude

    d = _fake_bin(tmp_path, "claude", "#!/bin/sh\necho boom >&2\nexit 1\n")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(RuntimeError):
        worker_claude("p", cwd=".", env=dict(os.environ), timeout=10)


def test_worker_claude_raises_on_is_error(tmp_path, monkeypatch):
    from workers import worker_claude

    d = _fake_bin(
        tmp_path, "claude", '#!/bin/sh\necho \'{"is_error": true, "result": ""}\'\n'
    )
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(RuntimeError):
        worker_claude("p", cwd=".", env=dict(os.environ), timeout=10)


def test_worker_claude_success_parses_result(tmp_path, monkeypatch):
    from workers import worker_claude

    d = _fake_bin(
        tmp_path,
        "claude",
        '#!/bin/sh\necho \'{"result": "hi there", "total_cost_usd": 0.01}\'\n',
    )
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    out = worker_claude("p", cwd=".", env=dict(os.environ), timeout=10)
    assert out["reply"] == "hi there"
    assert out["meta"]["harness"] == "claude"


def test_worker_codex_raises_on_nonzero_exit(tmp_path, monkeypatch):
    from workers import worker_codex

    d = _fake_bin(tmp_path, "codex", "#!/bin/sh\necho boom >&2\nexit 1\n")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(RuntimeError):
        worker_codex("p", cwd=".", env=dict(os.environ), timeout=10)


def test_worker_codex_raises_on_empty_output(tmp_path, monkeypatch):
    from workers import worker_codex

    d = _fake_bin(
        tmp_path, "codex", "#!/bin/sh\nexit 0\n"
    )  # exits 0 but writes nothing to -o
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(RuntimeError):
        worker_codex("p", cwd=".", env=dict(os.environ), timeout=10)


def test_sanitized_worker_env_only_drops_daemon_slack_tokens():
    env = sanitized_worker_env(
        {
            "PATH": "/bin",
            "HOME": "/home/test",
            "OPENAI_API_KEY": "provider-secret",
            "SLACK_BOT_TOKEN": "bot",
            "SLACK_APP_TOKEN": "app",
            "DATABASE_URL": "secret",
        },
        {"CAPABILITIES_AUTH_CONTEXT": "/authority.json"},
    )
    assert env["PATH"] == "/bin"
    assert env["CAPABILITIES_AUTH_CONTEXT"] == "/authority.json"
    assert env["OPENAI_API_KEY"] == "provider-secret"
    assert "SLACK_BOT_TOKEN" not in env
    assert "SLACK_APP_TOKEN" not in env
    assert env["DATABASE_URL"] == "secret"


def test_claude_uses_unrestricted_permissions(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return 0, '{"result":"ok"}', ""

    monkeypatch.setattr(workers, "run_worker_proc", fake_run)
    out = workers.worker_claude(
        "prompt",
        cwd=".",
        env={
            "PATH": "/bin",
            "SLACK_WORKER_OUTBOX": "/tmp/slack-outbox",
            "CAPABILITIES_AUTH_CONTEXT": "/tmp/slack-authority",
        },
        timeout=10,
        model="opus",
        effort="high",
    )
    command = captured["cmd"]
    assert "--dangerously-skip-permissions" in command
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "high"
    assert out["reply"] == "ok"


def test_codex_bypasses_approvals_and_sandbox(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        outpath = cmd[cmd.index("-o") + 1]
        with open(outpath, "w", encoding="utf-8") as fh:
            fh.write("ok")
        return 0, "", ""

    monkeypatch.setattr(workers, "run_worker_proc", fake_run)
    out = workers.worker_codex(
        "prompt",
        cwd=tmp_path,
        env={
            "PATH": "/bin",
            "CAPABILITIES_AUTH_CONTEXT": "/authority.json",
            "SLACK_BOT_TOKEN": "must-not-appear",
        },
        timeout=10,
        model="gpt-5.6",
        effort="high",
        service_tier="priority",
    )
    command = captured["cmd"]
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "--skip-git-repo-check" in command
    assert command[command.index("-m") + 1] == "gpt-5.6"
    assert 'model_reasoning_effort="high"' in command
    assert 'service_tier="priority"' in command
    assert all("SLACK_BOT_TOKEN" not in part for part in command)
    assert out["reply"] == "ok"
