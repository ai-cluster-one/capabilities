"""Worker harnesses for the slack service daemon. Each turns a prompt into a
normalized {"reply", "meta"} dict. Subprocesses run in their own process group
so a timeout (or a control /stop) can kill the whole tree, not just the parent.

Workers run with the harness' normal permission system and a bounded environment.
The daemon supplies explicit capability names and a read-only/workspace-write
mode; no worker receives Slack tokens."""

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

STUB_TAIL = 200
WORKSPACE_MODES = {"read_only", "workspace_write"}
SAFE_PROCESS_ENV = {
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TERM",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}
SAFE_SHELL_ENV = set(SAFE_PROCESS_ENV)


class WorkerTimeout(Exception):
    pass


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_worker_proc(cmd, *, cwd, env, timeout, on_spawn=None):
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=str(cwd),
        env=env,
    )
    if on_spawn is not None:
        on_spawn(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise WorkerTimeout(f"worker exceeded {timeout}s") from None


def sanitized_worker_env(base_env, extra=None):
    env = {
        key: value
        for key, value in (base_env or {}).items()
        if key in SAFE_PROCESS_ENV and value
    }
    env.update(
        {key: str(value) for key, value in (extra or {}).items() if value is not None}
    )
    return env


def _workspace_mode(value):
    mode = str(value or "read_only").strip().lower()
    if mode not in WORKSPACE_MODES:
        raise ValueError(f"workspace_mode must be one of {sorted(WORKSPACE_MODES)}")
    return mode


def _codex_shell_config(env):
    args = ["-c", "shell_environment_policy.inherit=none"]
    for key in sorted(
        SAFE_SHELL_ENV
        | {
            "CAPABILITIES_AUTH_CONTEXT",
            "SLACK_WORKER_CONVERSATION",
            "SLACK_WORKER_OUTBOX",
        }
    ):
        value = env.get(key)
        if value:
            args += [
                "-c",
                f"shell_environment_policy.set.{key}={json.dumps(str(value))}",
            ]
    return args


def worker_stub(
    prompt,
    *,
    cwd,
    env,
    timeout,
    model=None,
    effort=None,
    on_spawn=None,
    allowed_capabilities=None,
    network_domains=None,
    protected_home=None,
    worker_bin=None,
    capability_roots=None,
    workspace_mode="read_only",
):
    last = ""
    for line in reversed((prompt or "").splitlines()):
        if line.strip():
            last = line.strip()
            break
    return {"reply": f"[stub] {last[:STUB_TAIL]}", "meta": {"harness": "stub"}}


def worker_claude(
    prompt,
    *,
    cwd,
    env,
    timeout,
    model=None,
    effort=None,
    on_spawn=None,
    allowed_capabilities=None,
    network_domains=None,
    protected_home=None,
    worker_bin=None,
    capability_roots=None,
    workspace_mode="read_only",
):
    mode = _workspace_mode(workspace_mode)
    available_tools = ["Read", "Glob", "Grep", "Bash"]
    allowed_tools = []
    if env.get("SLACK_WORKER_OUTBOX"):
        allowed_tools.append("Bash(slack:*)")
    allowed_tools.extend(
        f"Bash({name}:*)" for name in sorted(allowed_capabilities or [])
    )
    if mode == "workspace_write":
        available_tools.extend(["Edit", "Write"])
    project = str(Path(cwd).resolve())
    allow_read = [project]
    allow_write = []
    if env.get("CAPABILITIES_AUTH_CONTEXT"):
        allow_read.append(str(Path(env["CAPABILITIES_AUTH_CONTEXT"]).resolve()))
    if env.get("SLACK_WORKER_OUTBOX"):
        allow_write.append(str(Path(env["SLACK_WORKER_OUTBOX"]).resolve()))
    if worker_bin:
        allow_read.append(str(Path(worker_bin).resolve()))
    allow_read.extend(str(Path(path).resolve()) for path in capability_roots or [])
    filesystem = {
        "allowRead": sorted(set(allow_read)),
        "allowWrite": sorted(set(allow_write)),
    }
    if protected_home:
        filesystem["denyRead"] = [str(Path(protected_home).resolve())]
    if mode == "read_only":
        filesystem["denyWrite"] = [project]
    strict_settings = {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": False,
            "filesystem": filesystem,
            "network": {"allowedDomains": sorted(set(network_domains or []))},
        }
    }
    denied_tools = [
        "WebFetch",
        "WebSearch",
        "Edit" if mode == "read_only" else None,
        "Write" if mode == "read_only" else None,
        "NotebookEdit" if mode == "read_only" else None,
        "Read(**/.env)",
        "Read(**/.env.*)",
        "Read(**/credentials.env)",
    ]
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        "default",
        "--safe-mode",
        "--setting-sources",
        "",
        "--tools",
        ",".join(available_tools),
        "--settings",
        json.dumps(strict_settings, separators=(",", ":")),
        "--allowedTools",
        ",".join(allowed_tools),
        "--disallowedTools",
        ",".join(tool for tool in denied_tools if tool),
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    rc, out, err = run_worker_proc(
        cmd, cwd=cwd, env=env, timeout=timeout, on_spawn=on_spawn
    )
    if rc != 0:
        raise RuntimeError(
            f"claude worker failed: "
            f"{(err.strip() or out.strip() or f'exit {rc}')[:500]}"
        )
    obj = json.loads(out)
    reply = (obj.get("result") or "").strip()
    if obj.get("is_error") or not reply:
        raise RuntimeError(
            f"claude worker error: {str(obj.get('subtype') or obj.get('result'))[:200]}"
        )
    return {
        "reply": reply,
        "meta": {
            "harness": "claude",
            "model": model,
            "cost_usd": obj.get("total_cost_usd"),
            "session_id": obj.get("session_id"),
        },
    }


def worker_codex(
    prompt,
    *,
    cwd,
    env,
    timeout,
    model=None,
    effort=None,
    on_spawn=None,
    allowed_capabilities=None,
    network_domains=None,
    protected_home=None,
    worker_bin=None,
    capability_roots=None,
    workspace_mode="read_only",
):
    fd, outpath = tempfile.mkstemp(prefix="slack-codex-", suffix=".txt")
    os.close(fd)
    try:
        sandbox = (
            "read-only"
            if _workspace_mode(workspace_mode) == "read_only"
            else "workspace-write"
        )
        cmd = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            prompt,
            "--sandbox",
            sandbox,
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--color",
            "never",
            "-o",
            outpath,
            *_codex_shell_config(env),
        ]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        rc, _out, err = run_worker_proc(
            cmd, cwd=cwd, env=env, timeout=timeout, on_spawn=on_spawn
        )
        if rc != 0:
            raise RuntimeError(f"codex worker failed: {err.strip()[:200]}")
        reply = Path(outpath).read_text().strip()
        if not reply:
            raise RuntimeError("codex worker produced no final message")
        return {"reply": reply, "meta": {"harness": "codex", "model": model}}
    finally:
        try:
            os.unlink(outpath)
        except OSError:
            pass


WORKERS = {"stub": worker_stub, "claude": worker_claude, "codex": worker_codex}
