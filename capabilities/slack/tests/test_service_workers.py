import json
import os
import stat
from pathlib import Path

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


def test_sanitized_worker_env_drops_slack_and_unrelated_secrets():
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
    assert "OPENAI_API_KEY" not in env
    assert "SLACK_BOT_TOKEN" not in env
    assert "SLACK_APP_TOKEN" not in env
    assert "DATABASE_URL" not in env


def test_claude_uses_permissions_and_explicit_capability_tools(monkeypatch):
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
        allowed_capabilities=["youtrack"],
        network_domains=["tenant.youtrack.cloud"],
        protected_home="/Users/operator",
        worker_bin="/opt/slack-worker-bin",
        workspace_mode="read_only",
    )
    command = captured["cmd"]
    assert "--dangerously-skip-permissions" not in command
    assert command[command.index("--permission-mode") + 1] == "default"
    assert "--safe-mode" in command
    assert command[command.index("--setting-sources") + 1] == ""
    settings = json.loads(command[command.index("--settings") + 1])
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["autoAllowBashIfSandboxed"] is False
    assert sandbox["network"]["allowedDomains"] == ["tenant.youtrack.cloud"]
    assert str(Path.cwd()) in sandbox["filesystem"]["denyWrite"]
    allowed = command[command.index("--allowedTools") + 1]
    assert "Bash(youtrack:*)" in allowed
    assert "Bash(slack:*)" in allowed
    assert "Edit" not in allowed
    assert out["reply"] == "ok"


def test_codex_uses_sandbox_ephemeral_mode_and_filtered_shell_env(
    tmp_path, monkeypatch
):
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
        workspace_mode="read_only",
    )
    command = captured["cmd"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command
    assert "shell_environment_policy.inherit=none" in command
    assert all("SLACK_BOT_TOKEN" not in part for part in command)
    assert out["reply"] == "ok"
