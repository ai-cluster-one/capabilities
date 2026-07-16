#!/usr/bin/env python3
"""Focused regressions for the bundled Telegram assistant service."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace


TELEGRAM_DIR = Path(__file__).resolve().parents[1]
DAEMON_PATH = TELEGRAM_DIR / "service" / "daemon.py"


def settings(**default_overrides):
    defaults = {
        "worker": "stub",
        "debounce": 0,
        "worker_timeout": 2,
        "progress_after": 60,
        "max_parallel_jobs": 1,
        "max_attempts": 3,
        **default_overrides,
    }
    return {
        "connection": "test",
        "assistant_name": "Assistant",
        "direct_messages": {"mode": "anyone", "default_role": "direct_user"},
        "allowed_users": {},
        "allowed_groups": {},
        "defaults": defaults,
    }


def write_fake_telethon(root: Path) -> None:
    package = root / "telethon"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        """
class _NewMessage:
    def __init__(self, *args, **kwargs):
        pass

class events:
    NewMessage = _NewMessage

class TelegramClient:
    pass
""".lstrip()
    )


def import_daemon(tmp: Path, service_settings: dict):
    project = tmp / "project"
    service_dir = project / "capabilities" / "telegram" / "service"
    service_dir.mkdir(parents=True)
    settings_file = service_dir / "settings.json"
    context_file = service_dir / "context.md"
    settings_file.write_text(json.dumps(service_settings) + "\n")
    context_file.write_text("test context\n")
    connections_file = tmp / "connections.json"
    connections_file.write_text(json.dumps({
        "connections": {"test": {"api_id": 12345, "allow_write": True}},
    }) + "\n")
    fake_root = tmp / "fake"
    write_fake_telethon(fake_root)

    old_env = dict(os.environ)
    old_path = list(sys.path)
    old_telethon = sys.modules.pop("telethon", None)
    try:
        os.environ.update({
            "HOME": str(tmp / "home"),
            "XDG_CONFIG_HOME": str(tmp / "config"),
            "XDG_STATE_HOME": str(tmp / "state"),
            "TELEGRAM_API_HASH": "test-hash",
            "TELEGRAM_SERVICE_CONNECTION": "test",
            "TELEGRAM_SERVICE_CONNECTIONS_FILE": str(connections_file),
            "TELEGRAM_SERVICE_CONTEXT": str(context_file),
            "TELEGRAM_SERVICE_PROJECT_ROOT": str(project),
            "TELEGRAM_SERVICE_SETTINGS": str(settings_file),
            "TELEGRAM_SERVICE_STATE_DIR": str(tmp / "service-state"),
        })
        sys.path.insert(0, str(fake_root))
        name = f"telegram_assistant_test_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, DAEMON_PATH)
        if spec is None or spec.loader is None:
            raise AssertionError("cannot import Telegram assistant daemon")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module._test_logs = []
        module.log = module._test_logs.append
        return module
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        sys.modules.pop("telethon", None)
        if old_telethon is not None:
            sys.modules["telethon"] = old_telethon


class Message:
    def __init__(self, message_id: int, *, text: str = "hello", voice: bool = False):
        self.id = message_id
        self.sender_id = 777
        self.text = "" if voice else text
        self.raw_text = self.text
        self.message = self.text
        self.voice = voice
        self.audio = False
        self.video_note = False
        self.photo = False
        self.file = SimpleNamespace(mime_type="audio/ogg", name=None) if voice else None
        self.out = False
        self.downloads = 0

    async def get_sender(self):
        return SimpleNamespace(first_name="Test", last_name="User", username=None)

    async def download_media(self, file=None):
        self.downloads += 1
        await asyncio.sleep(0)
        return b"voice"


class Event:
    def __init__(self, message: Message, chat_id: int = 123):
        self.message = message
        self.chat_id = chat_id
        self.sender_id = message.sender_id
        self.is_private = True
        self.out = False
        self.input_chat = chat_id


class FakeClient:
    def __init__(self, messages=(), *, fail_sends=0):
        self.messages = list(messages)
        self.fail_sends = fail_sends
        self.send_attempts = 0
        self.sent = []
        self.get_messages_calls = 0
        self.handler = None
        self.started = asyncio.Event()
        self.disconnected = asyncio.Event()

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(first_name="Assistant", id=42, username="assistant")

    def on(self, _event):
        def decorate(fn):
            self.handler = fn
            return fn
        return decorate

    async def get_input_entity(self, chat_id):
        return chat_id

    async def get_messages(self, _chat, limit=None):
        self.get_messages_calls += 1
        return self.messages[-limit:] if limit else list(self.messages)

    @asynccontextmanager
    async def action(self, _chat, _kind):
        yield

    async def send_message(self, chat, text, **kwargs):
        self.send_attempts += 1
        if self.fail_sends:
            self.fail_sends -= 1
            raise RuntimeError("simulated outbound failure")
        item = {"chat": chat, "text": text, **kwargs}
        self.sent.append(item)
        return SimpleNamespace(id=1000 + len(self.sent))

    async def run_until_disconnected(self):
        self.started.set()
        await self.disconnected.wait()

    async def disconnect(self):
        self.disconnected.set()


async def wait_until(predicate, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


def successful_result(reply="done"):
    return {
        "reply": reply,
        "meta": {
            "harness": "stub",
            "model": None,
            "is_error": False,
            "tokens": {},
            "cost_usd": None,
            "duration_ms": None,
            "session_id": None,
        },
    }


class AssistantServiceTests(unittest.IsolatedAsyncioTestCase):
    async def stop_session(self, client, task):
        client.disconnected.set()
        await asyncio.wait_for(task, timeout=5)

    async def test_worker_process_stdin_is_closed(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            captured = {}

            class FakeProcess:
                pid = 123
                returncode = 0

                def communicate(self):
                    return "", ""

            def fake_popen(*args, **kwargs):
                captured.update(kwargs)
                return FakeProcess()

            original_popen = daemon.subprocess.Popen
            try:
                daemon.subprocess.Popen = fake_popen
                rc, out, err = daemon.run_worker_proc("worker", ["worker"], {})
            finally:
                daemon.subprocess.Popen = original_popen

            self.assertEqual((rc, out, err), (0, "", ""))
            self.assertEqual(captured["stdin"], daemon.subprocess.DEVNULL)

    async def test_voice_is_reserved_before_transcription_and_live_duplicate_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            message = Message(323, voice=True)
            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "spoken"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            await asyncio.gather(client.handler(Event(message)), client.handler(Event(message)))
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 323)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(message.downloads, 1)
            self.assertEqual(client.send_attempts, 2)  # one voice echo + one final reply
            self.assertEqual(len(client.sent), 2)
            self.assertEqual(daemon.load_register()["123"]["jobs"], {})
            await self.stop_session(client, task)

    async def test_startup_catch_up_runs_once_per_connection_without_periodic_polling(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            daemon.save_register({"123": {"last_processed_message_id": 320}})
            message = Message(323, voice=True)
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append(message.id) or "recovered"

            first = FakeClient([message])
            first_task = asyncio.create_task(daemon.run_session(first))
            await first.started.wait()
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 323)
            self.assertEqual(transcriptions, [323])
            self.assertEqual(first.send_attempts, 2)
            calls_after_recovery = first.get_messages_calls
            await asyncio.sleep(0.05)
            self.assertEqual(first.get_messages_calls, calls_after_recovery)
            await self.stop_session(first, first_task)

            second = FakeClient([message])
            second_task = asyncio.create_task(daemon.run_session(second))
            await second.started.wait()
            await asyncio.sleep(0.05)
            self.assertEqual(transcriptions, [323])
            self.assertEqual(second.send_attempts, 0)
            await self.stop_session(second, second_task)

            source = DAEMON_PATH.read_text()
            self.assertNotIn("periodic_catch_up", source)
            self.assertNotIn('catch_up_known("periodic")', source)

    async def test_cancelled_worker_is_killed_and_persisted_for_startup_retry(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings(worker_timeout=30))
            client = FakeClient([Message(10)])
            processes = []
            original_popen = daemon.subprocess.Popen

            def recording_popen(*args, **kwargs):
                proc = original_popen(*args, **kwargs)
                processes.append(proc)
                return proc

            daemon.subprocess.Popen = recording_popen

            def blocking_worker(chat, tail, state, procs):
                rc, _out, err = daemon.run_worker_proc(
                    state["proc_key"],
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    procs,
                    cancel_event=state["cancel_event"],
                )
                if rc:
                    raise RuntimeError(err or f"worker exit {rc}")
                return successful_result()

            daemon.WORKERS["stub"] = blocking_worker
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(client.messages[0]))
            await wait_until(lambda: processes and processes[0].poll() is None)
            await wait_until(lambda: daemon.load_register()["123"]["jobs"]["10"]["status"] == "running")

            await self.stop_session(client, task)
            daemon.subprocess.Popen = original_popen

            await wait_until(lambda: processes[0].poll() is not None)
            job = daemon.load_register()["123"]["jobs"]["10"]
            self.assertEqual(job["status"], "queued")
            self.assertNotIn("started_at", job)
            self.assertIn("cancel", job["last_error"])
            self.assertEqual(client.send_attempts, 0)

    async def test_timeout_kills_process_group_and_cleans_job(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings(worker_timeout=0.05))
            client = FakeClient([Message(11)])
            processes = []
            original_popen = daemon.subprocess.Popen

            def recording_popen(*args, **kwargs):
                proc = original_popen(*args, **kwargs)
                processes.append(proc)
                return proc

            daemon.subprocess.Popen = recording_popen

            def slow_worker(chat, tail, state, procs):
                rc, _out, err = daemon.run_worker_proc(
                    state["proc_key"],
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    procs,
                    cancel_event=state["cancel_event"],
                )
                if rc:
                    raise RuntimeError(err or f"worker exit {rc}")
                return successful_result()

            daemon.WORKERS["stub"] = slow_worker
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(client.messages[0]))
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 11)
            await wait_until(lambda: processes and processes[0].poll() is not None)

            row = daemon.load_register()["123"]
            self.assertEqual(row["jobs"], {})
            self.assertEqual(client.send_attempts, 1)
            self.assertTrue(any("timed out" in line for line in daemon._test_logs))
            await self.stop_session(client, task)
            daemon.subprocess.Popen = original_popen

    async def test_disappeared_process_and_failed_final_send_cannot_orphan_running_job(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            def vanished_worker(chat, tail, state, procs):
                rc, _out, err = daemon.run_worker_proc(
                    state["proc_key"],
                    [sys.executable, "-c", "raise SystemExit(7)"],
                    procs,
                    cancel_event=state["cancel_event"],
                )
                if rc:
                    raise RuntimeError(err or f"worker process disappeared with exit {rc}")
                return successful_result()

            daemon.WORKERS["stub"] = vanished_worker
            vanished_client = FakeClient([Message(12)])
            vanished_task = asyncio.create_task(daemon.run_session(vanished_client))
            await vanished_client.started.wait()
            await vanished_client.handler(Event(vanished_client.messages[0]))
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 12)
            self.assertEqual(daemon.load_register()["123"]["jobs"], {})
            self.assertEqual(vanished_client.send_attempts, 1)
            await self.stop_session(vanished_client, vanished_task)

        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            client = FakeClient([Message(13)], fail_sends=1)
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(client.messages[0]))
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 13)

            row = daemon.load_register()["123"]
            self.assertEqual(row["jobs"], {})
            self.assertEqual(client.send_attempts, 2)  # failed final send + one error notice
            self.assertEqual(len(client.sent), 1)
            self.assertTrue(any("simulated outbound failure" in line for line in daemon._test_logs))
            await self.stop_session(client, task)

    async def test_recovery_and_watermark_dedupe_preserve_cleanup_invariants(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            register = {
                "123": {
                    "last_processed_message_id": 20,
                    "jobs": {
                        "19": {"message_id": 19, "status": "running", "started_at": "then"},
                        "21": {"message_id": 21, "status": "preparing"},
                        "18": {"message_id": 18, "status": "done"},
                    },
                },
            }
            self.assertTrue(daemon._recover_incomplete_jobs(register))
            self.assertEqual(register["123"]["jobs"]["19"]["status"], "queued")
            self.assertEqual(register["123"]["jobs"]["21"]["status"], "queued")
            self.assertNotIn("started_at", register["123"]["jobs"]["19"])
            self.assertTrue(daemon._message_is_known(register, "123", 19))
            self.assertTrue(daemon._message_is_known(register, "123", 20))
            self.assertFalse(daemon._message_is_known(register, "123", 22))
            self.assertTrue(daemon._prune_jobs(register, "123"))
            self.assertNotIn("18", register["123"]["jobs"])
            self.assertIn("19", register["123"]["jobs"])
            self.assertIn("21", register["123"]["jobs"])


if __name__ == "__main__":
    unittest.main()
