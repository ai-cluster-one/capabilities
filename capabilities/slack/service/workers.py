"""Worker harnesses for the slack service daemon. Each turns a prompt into a
normalized {"reply", "meta"} dict. Subprocesses run in their own process group
so a timeout (or a control /stop) can kill the whole tree, not just the parent.

The behavioral boundary is the soft prompt (context.md) + the per-job authority
file; --dangerously-skip-permissions / --bypass mirrors the isolated-agent model
used by the telegram worker."""

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

STUB_TAIL = 200


class WorkerTimeout(Exception):
    pass


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_worker_proc(cmd, *, cwd, env, timeout, on_spawn=None):
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True,
                            cwd=str(cwd), env=env)
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


def worker_stub(prompt, *, cwd, env, timeout, model=None, effort=None, on_spawn=None):
    last = ""
    for line in reversed((prompt or "").splitlines()):
        if line.strip():
            last = line.strip()
            break
    return {"reply": f"[stub] {last[:STUB_TAIL]}", "meta": {"harness": "stub"}}


def worker_claude(prompt, *, cwd, env, timeout, model=None, effort=None, on_spawn=None):
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    rc, out, err = run_worker_proc(cmd, cwd=cwd, env=env, timeout=timeout, on_spawn=on_spawn)
    if rc != 0:
        raise RuntimeError(f"claude worker failed: "
                           f"{(err.strip() or out.strip() or f'exit {rc}')[:500]}")
    obj = json.loads(out)
    reply = (obj.get("result") or "").strip()
    if obj.get("is_error") or not reply:
        raise RuntimeError(f"claude worker error: "
                           f"{str(obj.get('subtype') or obj.get('result'))[:200]}")
    return {"reply": reply, "meta": {"harness": "claude", "model": model,
            "cost_usd": obj.get("total_cost_usd"), "session_id": obj.get("session_id")}}


def worker_codex(prompt, *, cwd, env, timeout, model=None, effort=None, on_spawn=None):
    fd, outpath = tempfile.mkstemp(prefix="slack-codex-", suffix=".txt")
    os.close(fd)
    try:
        cmd = ["codex", "exec", prompt, "--dangerously-bypass-approvals-and-sandbox",
               "--skip-git-repo-check", "--json", "--color", "never", "-o", outpath]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        rc, out, err = run_worker_proc(cmd, cwd=cwd, env=env, timeout=timeout, on_spawn=on_spawn)
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
