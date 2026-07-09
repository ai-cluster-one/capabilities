#!/usr/bin/env python3
"""Regression tests for manager bundle installation.

Run with: python3 tests/test_manager_bundle_install.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
TELEGRAM_SCRIPT = REPO / "capabilities" / "telegram" / "bin" / "telegram"
TELEGRAM_BUNDLE = TELEGRAM_SCRIPT.parent.parent
TELEGRAM_DAEMON = TELEGRAM_BUNDLE / "service" / "daemon.py"
TELEGRAM_WORKER = TELEGRAM_BUNDLE / "service" / "worker-bin" / "telegram"
MAILBOX_SCRIPT = REPO / "capabilities" / "mailbox" / "bin" / "mailbox"


def _env(tmp: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp / "home"
    cap_home = home / ".capabilities"
    bin_dir = tmp / "bin"
    for path in (home, cap_home, bin_dir):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "CAPABILITIES_HOME": str(cap_home),
        "CAPABILITIES_BIN": str(bin_dir),
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "XDG_DATA_HOME": str(tmp / "data"),
        "XDG_CACHE_HOME": str(tmp / "cache"),
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
    })
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env, cap_home, bin_dir


def _run(argv: list[str], env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        argv, cwd=str(cwd or REPO), env=env, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(
            f"{' '.join(argv)} exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def _run_manager(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return _run([str(MANAGER), *args], env)


def _run_service_init(bin_dir: Path, env: dict[str, str], project: Path) -> None:
    (project / ".capabilities").mkdir(parents=True, exist_ok=True)
    proc = _run(
        [str(bin_dir / "telegram"), "service", "init", "--connection", "marvin"],
        {**env, "CLAUDE_PROJECT_DIR": str(project)},
        cwd=project,
    )
    if "FileNotFoundError" in proc.stderr:
        raise AssertionError(proc.stderr)
    assert (project / ".capabilities" / "telegram" / "service" / "settings.json").is_file()


def test_install_from_source_script_installs_bundle() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env, cap_home, bin_dir = _env(tmp)

        _run_manager(["install", "telegram", "--from", str(TELEGRAM_SCRIPT)], env)

        assert (cap_home / "telegram" / "service" / "templates" / "settings.json").is_file()
        meta = json.loads((cap_home / "telegram" / "meta.json").read_text())
        assert meta["source_type"] == "directory"
        assert meta["source"] == str(TELEGRAM_BUNDLE)
        _run_service_init(bin_dir, env, tmp / "project-install")


def test_update_migrates_script_source_to_bundle() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env, cap_home, bin_dir = _env(tmp)
        reg = cap_home / "telegram"
        reg.mkdir(parents=True)
        installed = reg / "telegram"
        shutil.copy2(TELEGRAM_SCRIPT, installed)
        installed.chmod(installed.stat().st_mode | 0o755)
        (bin_dir / "telegram").symlink_to(installed)
        (reg / "meta.json").write_text(json.dumps({
            "name": "telegram",
            "source": str(TELEGRAM_SCRIPT),
            "source_type": "script",
        }) + "\n")

        _run_manager(["update", "telegram"], env)

        assert (cap_home / "telegram" / "service" / "templates" / "settings.json").is_file()
        meta = json.loads((cap_home / "telegram" / "meta.json").read_text())
        assert meta["source_type"] == "directory"
        assert meta["source"] == str(TELEGRAM_BUNDLE)
        _run_service_init(bin_dir, env, tmp / "project-update")


def test_telegram_daemon_sigterm_stops_without_traceback() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        service_dir = project / ".capabilities" / "telegram" / "service"
        fake_telethon = tmp / "fake" / "telethon"
        state_dir = tmp / "service-state"
        log_file = tmp / "daemon.log"
        for path in (service_dir, fake_telethon, state_dir):
            path.mkdir(parents=True, exist_ok=True)
        (service_dir / "settings.json").write_text(json.dumps({
            "connection": "marvin",
            "assistant_name": "Assistant",
            "direct_messages": {"mode": "allowed_users", "default_role": "direct_user"},
            "allowed_users": {},
            "allowed_groups": {},
            "defaults": {"worker": "stub"},
        }) + "\n")
        (service_dir / "context.md").write_text("test context\n")
        connections = tmp / "connections.json"
        connections.write_text(json.dumps({
            "connections": {
                "marvin": {
                    "api_id": 12345,
                    "allow_write": True,
                },
            },
        }) + "\n")
        (fake_telethon / "__init__.py").write_text("""
import asyncio
from types import SimpleNamespace


class _NewMessage:
    def __init__(self, *args, **kwargs):
        pass


class events:
    NewMessage = _NewMessage


class TelegramClient:
    def __init__(self, *args, **kwargs):
        pass

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(first_name="Stub", id=42)

    def on(self, _event):
        def decorate(fn):
            return fn
        return decorate

    async def run_until_disconnected(self):
        await asyncio.Event().wait()

    async def disconnect(self):
        pass
""".lstrip())
        env = dict(os.environ)
        env.update({
            "HOME": str(tmp / "home"),
            "XDG_CONFIG_HOME": str(tmp / "config"),
            "XDG_STATE_HOME": str(tmp / "state"),
            "PYTHONPATH": str(tmp / "fake") + os.pathsep + env.get("PYTHONPATH", ""),
            "TELEGRAM_API_HASH": "test-hash",
            "TELEGRAM_SERVICE_CONNECTION": "marvin",
            "TELEGRAM_SERVICE_CONNECTIONS_FILE": str(connections),
            "TELEGRAM_SERVICE_CONTEXT": str(service_dir / "context.md"),
            "TELEGRAM_SERVICE_PROJECT_ROOT": str(project),
            "TELEGRAM_SERVICE_SETTINGS": str(service_dir / "settings.json"),
            "TELEGRAM_SERVICE_STATE_DIR": str(state_dir),
        })
        with log_file.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, str(TELEGRAM_DAEMON)],
                cwd=str(project),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                text = log_file.read_text(errors="replace")
                if "live " in text and (state_dir / "daemon.pid").exists():
                    break
                time.sleep(0.1)
            text = log_file.read_text(errors="replace")
            if proc.poll() is not None:
                raise AssertionError(f"daemon exited early with {proc.returncode}\n{text}")
            assert "live " in text, text
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
        text = log_file.read_text(errors="replace")
        assert proc.returncode == 0, text
        assert "shutdown requested; stopping telegram daemon" in text
        assert "Traceback" not in text
        assert "CancelledError" not in text
        assert not (state_dir / "daemon.pid").exists()
        assert not (state_dir / "daemon.lock").exists()


def test_capability_auth_context_denies_unlisted_capability() -> None:
    env = dict(os.environ)
    env["CAPABILITIES_AUTH_CONTEXT"] = json.dumps({
        "source": "telegram",
        "chat_id": "-1001",
        "sender_role": "group_member",
        "allowed_capabilities": {
            "telegram": {"scope": "current_chat"},
            "routine": True,
        },
    })
    proc = subprocess.run(
        [str(MAILBOX_SCRIPT), "list"],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 4
    payload = json.loads(proc.stderr)
    assert payload["error"]["code"] == "capability_not_authorized"
    assert "mailbox" in payload["error"]["message"]


def test_telegram_worker_wrapper_limits_current_chat_scope() -> None:
    env = dict(os.environ)
    env.update({
        "CAPABILITIES_AUTH_CONTEXT": json.dumps({
            "source": "telegram",
            "chat_id": "-1001",
            "sender_role": "group_member",
            "allowed_capabilities": {
                "telegram": {"scope": "current_chat"},
            },
        }),
        "TELEGRAM_REAL_TELEGRAM": "/bin/echo",
    })
    ok = subprocess.run(
        [str(TELEGRAM_WORKER), "read", "-1001", "--limit", "1"],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ok.returncode == 0, ok.stderr
    assert "read -1001 --limit 1" in ok.stdout

    denied = subprocess.run(
        [str(TELEGRAM_WORKER), "read", "-1002", "--limit", "1"],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert denied.returncode == 4
    payload = json.loads(denied.stderr)
    assert payload["error"]["code"] == "chat_scope_denied"


if __name__ == "__main__":
    tests = [
        ("install from source script installs bundle", test_install_from_source_script_installs_bundle),
        ("update migrates script source to bundle", test_update_migrates_script_source_to_bundle),
        ("telegram daemon sigterm stops without traceback", test_telegram_daemon_sigterm_stops_without_traceback),
        ("auth context denies unlisted capability", test_capability_auth_context_denies_unlisted_capability),
        ("telegram worker wrapper limits current chat scope", test_telegram_worker_wrapper_limits_current_chat_scope),
    ]
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"ok - {name}")
        except Exception:
            failed += 1
            print(f"not ok - {name}")
            import traceback
            traceback.print_exc()
    sys.exit(1 if failed else 0)
