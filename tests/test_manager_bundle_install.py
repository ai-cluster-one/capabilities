#!/usr/bin/env python3
"""Regression tests for manager bundle installation.

Run with: python3 tests/test_manager_bundle_install.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import importlib.machinery
import importlib.util
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
GEMINITALK_SCRIPT = REPO / "capabilities" / "geminitalk" / "bin" / "geminitalk"
GEMINITALK_BASE = REPO / "capabilities" / "geminitalk" / "prompts" / "base.md"


def _ensure_project_envelope(project: Path) -> Path:
    capdir = project / "capabilities"
    capdir.mkdir(parents=True, exist_ok=True)
    settings = capdir / "settings.json"
    if not settings.exists():
        settings.write_text('{"capabilities": {}}\n')
    return capdir


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


@contextlib.contextmanager
def _loaded_geminitalk(tmp: Path, project: Path):
    old_env = dict(os.environ)
    old_path = list(sys.path)
    module_name = f"geminitalk_test_{time.time_ns()}"
    try:
        _ensure_project_envelope(project)
        os.environ.update({
            "HOME": str(tmp / "home"),
            "XDG_CONFIG_HOME": str(tmp / "config"),
            "XDG_STATE_HOME": str(tmp / "state"),
            "XDG_DATA_HOME": str(tmp / "data"),
            "XDG_CACHE_HOME": str(tmp / "cache"),
            "CLAUDE_PROJECT_DIR": str(project),
        })
        loader = importlib.machinery.SourceFileLoader(module_name, str(GEMINITALK_SCRIPT))
        spec = importlib.util.spec_from_loader(module_name, loader)
        if spec is None or spec.loader is None:
            raise AssertionError("cannot load geminitalk module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        sys.modules.pop(module_name, None)


def _prompt_text(module, cfg: dict) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        module._emit(module._system_prompt(cfg))
    return buf.getvalue()


def _geminitalk_key_values(report: dict, cid: str = "default") -> dict:
    rows = ((report.get("connections") or {}).get(cid) or {}).get("keys") or []
    return {row.get("key"): row.get("value") for row in rows}


def _run_service_init(bin_dir: Path, env: dict[str, str], project: Path) -> None:
    _ensure_project_envelope(project)
    proc = _run(
        [str(bin_dir / "telegram"), "service", "init", "--connection", "marvin"],
        {**env, "CLAUDE_PROJECT_DIR": str(project)},
        cwd=project,
    )
    if "FileNotFoundError" in proc.stderr:
        raise AssertionError(proc.stderr)
    assert (project / "capabilities" / "telegram" / "service" / "settings.json").is_file()


def _write_fake_telethon(fake_telethon: Path) -> None:
    fake_telethon.mkdir(parents=True, exist_ok=True)
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


def _import_telegram_daemon(tmp: Path, settings: dict) -> object:
    project = tmp / "project"
    _ensure_project_envelope(project)
    service_dir = project / "capabilities" / "telegram" / "service"
    service_dir.mkdir(parents=True, exist_ok=True)
    (service_dir / "settings.json").write_text(json.dumps(settings) + "\n")
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
    fake_telethon = tmp / "fake" / "telethon"
    _write_fake_telethon(fake_telethon)
    old_env = dict(os.environ)
    old_path = list(sys.path)
    try:
        os.environ.update({
            "HOME": str(tmp / "home"),
            "XDG_CONFIG_HOME": str(tmp / "config"),
            "XDG_STATE_HOME": str(tmp / "state"),
            "PYTHONPATH": str(tmp / "fake") + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "TELEGRAM_API_HASH": "test-hash",
            "TELEGRAM_SERVICE_CONNECTION": "marvin",
            "TELEGRAM_SERVICE_CONNECTIONS_FILE": str(connections),
            "TELEGRAM_SERVICE_CONTEXT": str(service_dir / "context.md"),
            "TELEGRAM_SERVICE_PROJECT_ROOT": str(project),
            "TELEGRAM_SERVICE_SETTINGS": str(service_dir / "settings.json"),
            "TELEGRAM_SERVICE_STATE_DIR": str(tmp / "service-state"),
        })
        sys.path.insert(0, str(tmp / "fake"))
        module_name = f"telegram_daemon_test_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(module_name, TELEGRAM_DAEMON)
        if spec is None or spec.loader is None:
            raise AssertionError("cannot load telegram daemon module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path


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
        service_dir = project / "capabilities" / "telegram" / "service"
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


def test_telegram_service_authority_pins_connection_and_session() -> None:
    env = dict(os.environ)
    env["CAPABILITIES_AUTH_CONTEXT"] = json.dumps({
        "source": "telegram",
        "connection": "marvin",
        "chat_id": "-1001",
        "sender_role": "group_member",
        "allowed_capabilities": {
            "telegram": {"scope": "current_chat"},
        },
    })
    wrong_connection = subprocess.run(
        [str(TELEGRAM_SCRIPT), "chats", "--connection", "personal"],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert wrong_connection.returncode == 4
    payload = json.loads(wrong_connection.stderr)
    assert payload["error"]["code"] == "connection_scope_denied"

    wrong_session = subprocess.run(
        [str(TELEGRAM_SCRIPT), "chats", "--session", "/tmp/personal"],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert wrong_session.returncode == 4
    payload = json.loads(wrong_session.stderr)
    assert payload["error"]["code"] == "session_scope_denied"


def test_telegram_control_authority_limits_settings_commands() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        daemon = _import_telegram_daemon(tmp, {
            "connection": "marvin",
            "assistant_name": "Assistant",
            "direct_messages": {"mode": "allowed_users", "default_role": "direct_user"},
            "allowed_users": {
                "535123867": {"name": "KZ", "role": "supervisor"},
            },
            "allowed_groups": {
                "-1001": {"name": "Test Group", "member_role": "group_member"},
            },
            "control": {
                "roles": {
                    "supervisor": {"commands": ["status", "set", "stop", "help"]},
                    "group_member": {"commands": ["status", "help"]},
                },
            },
            "defaults": {"worker": "stub"},
        })
        group_policy = {"name": "Test Group", "member_role": "group_member"}
        supervisor = {"id": "535123867", "name": "KZ", "role": "supervisor"}
        member = {"id": "777", "name": "Member", "role": "group_member"}

        assert daemon._control_command_allowed("/status", member, group_policy)
        assert not daemon._control_command_allowed("/set", member, group_policy)
        assert not daemon._control_command_allowed("/stop", member, group_policy)
        assert daemon._control_command_allowed("/status", supervisor, group_policy)
        assert daemon._control_command_allowed("/set", supervisor, group_policy)
        assert daemon._control_command_allowed("/stop", supervisor, group_policy)


def test_telegram_group_chat_ref_never_falls_back_to_sender() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        daemon = _import_telegram_daemon(tmp, {
            "connection": "marvin",
            "assistant_name": "Assistant",
            "direct_messages": {"mode": "allowed_users", "default_role": "direct_user"},
            "allowed_users": {},
            "allowed_groups": {},
            "defaults": {"worker": "stub"},
        })

        class GroupEvent:
            chat_id = -770312767
            input_chat = None
            input_sender = "sender-peer"

            async def get_input_chat(self):
                return "chat-peer"

            async def get_input_sender(self):
                return "sender-peer-from-method"

        class FallbackGroupEvent:
            chat_id = -770312767
            input_chat = None
            input_sender = "sender-peer"

            async def get_input_chat(self):
                raise RuntimeError("no input chat")

            async def get_chat(self):
                raise RuntimeError("no chat entity")

            async def get_input_sender(self):
                return "sender-peer-from-method"

        assert asyncio.run(daemon._event_chat_ref(GroupEvent(), is_direct=False)) == "chat-peer"
        assert asyncio.run(daemon._event_chat_ref(FallbackGroupEvent(), is_direct=False)) == -770312767


def test_telegram_channel_context_overlay_is_added_to_prompt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        daemon = _import_telegram_daemon(tmp, {
            "connection": "marvin",
            "assistant_name": "Assistant",
            "direct_messages": {"mode": "allowed_users", "default_role": "direct_user"},
            "allowed_users": {},
            "allowed_groups": {},
            "defaults": {"worker": "stub"},
        })
        channel_context_dir = tmp / "project" / "capabilities" / "telegram" / "service" / "context"
        channel_context_dir.mkdir(parents=True)
        (channel_context_dir / "family.md").write_text("File overlay line.\n")

        policy = {
            "context": "Inline overlay line.",
            "context_file": "context/family.md",
        }
        overlay = daemon._channel_context_from_policy(policy)
        assert "Inline overlay line." in overlay
        assert "File overlay line." in overlay

        prompt = daemon.build_prompt(
            [{"id": 1, "sender": "KZ", "text": "Hello"}],
            {
                "chat_id": "-1001",
                "chat_type": "group",
                "connection": "marvin",
                "harness": "stub",
                "chat_name": "Family",
                "channel_context": overlay,
                "current_request": {"message_id": 1, "sender_name": "KZ", "sender_role": "supervisor", "kind": "text", "text": "Hello", "reply_to": 1},
                "settings": {"tail_size": 30, "debounce": 2, "worker": "stub"},
            },
        )
        assert "--- Channel-specific context ---" in prompt
        assert prompt.index("--- Channel-specific context ---") < prompt.index("--- Channel state ---")

        outside = daemon._channel_context_from_policy({"context_file": "../outside.md"})
        assert "ignored" in outside


def test_geminitalk_init_scaffolds_project_prompt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        (project / "capabilities").mkdir(parents=True)
        with _loaded_geminitalk(tmp, project) as geminitalk:
            result = geminitalk.cmd_init()
            prompt = project / "capabilities" / "geminitalk" / "base.md"
            assert prompt.is_file()
            assert prompt.read_text() == GEMINITALK_BASE.read_text()
            assert [Path(p).resolve() for p in result["written"]] == [prompt.resolve()]
            assert result["skipped"] == []
            assert result["prompt_files"] == ["capabilities/geminitalk/base.md"]


def test_geminitalk_effective_defaults_match_marvin_baseline() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        (project / "capabilities").mkdir(parents=True)
        with _loaded_geminitalk(tmp, project) as geminitalk:
            assert geminitalk.DEFAULT_VOICE == "Aoede"
            assert geminitalk.DEFAULT_AGENT_NAME == "Tessa"
            assert geminitalk.DEFAULT_LANGUAGE == "auto"
            assert geminitalk.DEFAULT_MAX_AGENT_SESSIONS == 3
            assert geminitalk.WRITE_DEFAULT is True

            cid, cfg = geminitalk._selected_cfg(None, None, require_key=False)
            assert cid == "default"
            assert cfg["voice"] == "Aoede"
            assert cfg["agent_name"] == "Tessa"
            assert cfg["language"] == "auto"
            assert cfg["max_agent_sessions"] == 3
            assert cfg["allow_capability_domain_commands"] is False
            assert cfg["allow_codex_tasks"] is True
            assert cfg["prompt_files"] == ["capabilities/geminitalk/base.md"]

            implicit = geminitalk._connections_report()
            assert implicit["connections"]["default"]["allow_write"] is True
            implicit_values = _geminitalk_key_values(implicit)
            assert implicit_values["voice"] == "Aoede"
            assert implicit_values["agent_name"] == "Tessa"
            assert implicit_values["language"] == "auto"
            assert implicit_values["max_agent_sessions"] == 3
            assert implicit_values["allow_capability_domain_commands"] is False
            assert implicit_values["allow_codex_tasks"] is True
            assert implicit_values["prompt_files"] == ["capabilities/geminitalk/base.md"]

            capdir = project / "capabilities" / "geminitalk"
            capdir.mkdir(parents=True)
            (capdir / "connections.json").write_text(json.dumps({
                "default": "marvin-like",
                "connections": {"marvin-like": {"secret_env": "GOOGLE_API_KEY"}},
            }) + "\n")
            registry = geminitalk._connections_report()
            assert registry["connections"]["marvin-like"]["allow_write"] is True
            registry_values = _geminitalk_key_values(registry, "marvin-like")
            assert registry_values["voice"] == "Aoede"
            assert registry_values["agent_name"] == "Tessa"
            assert registry_values["language"] == "auto"
            assert registry_values["max_agent_sessions"] == 3
            assert registry_values["allow_capability_domain_commands"] is False
            assert registry_values["allow_codex_tasks"] is True

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                try:
                    geminitalk._contract(["manifest", "--json"])
                except SystemExit as exc:
                    assert exc.code == 0
            manifest = json.loads(buf.getvalue())
            notes = {item["key"]: item.get("note", "") for item in manifest["credentials"]["keys"]}
            assert "default Aoede" in notes["GEMINITALK_VOICE"]
            assert "default Tessa" in notes["GEMINITALK_AGENT_NAME"]
            assert "default true" in notes["GEMINITALK_ALLOW_CODEX_TASKS"]
            assert "default true" in notes["allow_write"]


def test_geminitalk_default_prompt_adds_codex_context_when_present() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        (project / "capabilities").mkdir(parents=True)
        with _loaded_geminitalk(tmp, project) as geminitalk:
            geminitalk.cmd_init()
            _cid, cfg = geminitalk._selected_cfg(None, None, require_key=False)
            assert cfg["prompt_files"] == ["capabilities/geminitalk/base.md"]

            codex_context = project / ".codex" / "generated" / "context.md"
            codex_context.parent.mkdir(parents=True)
            codex_context.write_text("Codex generated context marker.\n")
            _cid, cfg = geminitalk._selected_cfg(None, None, require_key=False)
            assert cfg["prompt_files"] == [
                "capabilities/geminitalk/base.md",
                ".codex/generated/context.md",
            ]
            rendered = _prompt_text(geminitalk, cfg)
            assert "Codex generated context marker." in rendered


def test_geminitalk_explicit_prompt_files_are_ordered_and_authoritative() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        capdir = project / "capabilities" / "geminitalk"
        capdir.mkdir(parents=True)
        (project / ".codex" / "generated").mkdir(parents=True)
        (project / ".codex" / "generated" / "context.md").write_text("SHOULD NOT AUTO LOAD\n")
        (project / "first.md").write_text("FIRST PROMPT MARKER\n")
        (project / "second.md").write_text("SECOND PROMPT MARKER\n")
        (capdir / "base.md").write_text("BASE PROMPT MARKER\n")
        (capdir / "connections.json").write_text(json.dumps({
            "default": "ordered",
            "connections": {
                "ordered": {
                    "secret_env": "GOOGLE_API_KEY",
                    "prompt_files": [
                        "first.md",
                        "capabilities/geminitalk/base.md",
                        "second.md",
                    ],
                },
            },
        }) + "\n")
        with _loaded_geminitalk(tmp, project) as geminitalk:
            reg, _path = geminitalk._connections_registry()
            _cid, cfg = geminitalk._selected_cfg(reg, None, require_key=False)
            assert cfg["prompt_files"] == [
                "first.md",
                "capabilities/geminitalk/base.md",
                "second.md",
            ]
            rendered = _prompt_text(geminitalk, cfg)
            assert "SHOULD NOT AUTO LOAD" not in rendered
            assert rendered.index("FIRST PROMPT MARKER") < rendered.index("BASE PROMPT MARKER")
            assert rendered.index("BASE PROMPT MARKER") < rendered.index("SECOND PROMPT MARKER")


def test_geminitalk_legacy_prompt_file_migrates_after_project_base() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        capdir = project / "capabilities" / "geminitalk"
        capdir.mkdir(parents=True)
        (capdir / "base.md").write_text("BASE PROMPT MARKER\n")
        (project / "legacy-env.md").write_text("LEGACY ENV PROMPT MARKER\n")
        (project / "legacy-connection.md").write_text("LEGACY CONNECTION PROMPT MARKER\n")
        with _loaded_geminitalk(tmp, project) as geminitalk:
            os.environ["GEMINITALK_SYSTEM_PROMPT_FILE"] = "legacy-env.md"
            _cid, cfg = geminitalk._selected_cfg(None, None, require_key=False)
            assert cfg["prompt_files"] == [
                "capabilities/geminitalk/base.md",
                "legacy-env.md",
            ]
            rendered = _prompt_text(geminitalk, cfg)
            assert rendered.index("BASE PROMPT MARKER") < rendered.index("LEGACY ENV PROMPT MARKER")

            os.environ.pop("GEMINITALK_SYSTEM_PROMPT_FILE", None)
            (capdir / "connections.json").write_text(json.dumps({
                "connections": {
                    "legacy": {
                        "secret_env": "GOOGLE_API_KEY",
                        "prompt_file": "legacy-connection.md",
                    },
                },
            }) + "\n")
            reg, _path = geminitalk._connections_registry()
            _cid, cfg = geminitalk._selected_cfg(reg, None, require_key=False)
            assert cfg["prompt_files"] == [
                "capabilities/geminitalk/base.md",
                "legacy-connection.md",
            ]


def test_geminitalk_init_does_not_overwrite_edited_prompt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        (project / "capabilities").mkdir(parents=True)
        with _loaded_geminitalk(tmp, project) as geminitalk:
            geminitalk.cmd_init()
            prompt = project / "capabilities" / "geminitalk" / "base.md"
            prompt.write_text("USER EDITED PROMPT\n")
            result = geminitalk.cmd_init()
            assert prompt.read_text() == "USER EDITED PROMPT\n"
            assert result["written"] == []
            assert [Path(p).resolve() for p in result["skipped"]] == [prompt.resolve()]


def test_geminitalk_generation_complete_marks_turn_done() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        (project / "capabilities").mkdir(parents=True)
        with _loaded_geminitalk(tmp, project) as geminitalk:
            response = type("Response", (), {
                "server_content": type("Content", (), {
                    "input_transcription": None,
                    "output_transcription": None,
                    "turn_complete": False,
                    "generation_complete": True,
                })(),
            })()
            _inp, _out, done = geminitalk._transcription_texts(response)
            assert done is True


if __name__ == "__main__":
    tests = [
        ("install from source script installs bundle", test_install_from_source_script_installs_bundle),
        ("update migrates script source to bundle", test_update_migrates_script_source_to_bundle),
        ("telegram daemon sigterm stops without traceback", test_telegram_daemon_sigterm_stops_without_traceback),
        ("auth context denies unlisted capability", test_capability_auth_context_denies_unlisted_capability),
        ("telegram worker wrapper limits current chat scope", test_telegram_worker_wrapper_limits_current_chat_scope),
        ("telegram control authority limits settings commands", test_telegram_control_authority_limits_settings_commands),
        ("telegram group chat ref never falls back to sender", test_telegram_group_chat_ref_never_falls_back_to_sender),
        ("telegram channel context overlay is added to prompt", test_telegram_channel_context_overlay_is_added_to_prompt),
        ("geminitalk init scaffolds project prompt", test_geminitalk_init_scaffolds_project_prompt),
        ("geminitalk effective defaults match marvin baseline", test_geminitalk_effective_defaults_match_marvin_baseline),
        ("geminitalk default prompt adds codex context when present", test_geminitalk_default_prompt_adds_codex_context_when_present),
        ("geminitalk explicit prompt files are ordered and authoritative", test_geminitalk_explicit_prompt_files_are_ordered_and_authoritative),
        ("geminitalk legacy prompt_file migrates after project base", test_geminitalk_legacy_prompt_file_migrates_after_project_base),
        ("geminitalk init does not overwrite edited prompt", test_geminitalk_init_does_not_overwrite_edited_prompt),
        ("geminitalk generation_complete marks turn done", test_geminitalk_generation_complete_marks_turn_done),
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
