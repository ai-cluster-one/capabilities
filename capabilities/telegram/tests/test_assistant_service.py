#!/usr/bin/env python3
"""Focused regressions for the bundled Telegram assistant service."""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


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
class _Handler:
    def __init__(self, *args, **kwargs):
        pass

class events:
    NewMessage = _Handler
    Raw = _Handler

class TelegramClient:
    pass

__version__ = "1.43.2-test"
""".lstrip()
    )
    tl = package / "tl"
    tl.mkdir()
    (tl / "__init__.py").write_text("")
    (tl / "types.py").write_text(
        """
class _Placeholder:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)

class DocumentAttributeAudio(_Placeholder): pass
class DocumentAttributeFilename(_Placeholder): pass
class InputGroupCallInviteMessage(_Placeholder): pass
class MessageActionConferenceCall(_Placeholder): pass
class MessageActionInviteToGroupCall(_Placeholder): pass
class MessageService(_Placeholder): pass
class UpdateNewMessage(_Placeholder): pass
""".lstrip()
    )
    functions = tl / "functions"
    functions.mkdir()
    (functions / "__init__.py").write_text("")
    (functions / "phone.py").write_text(
        """
class GetGroupCallChainBlocksRequest:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)


class GetGroupCallRequest:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)
""".lstrip()
    )
    errors = package / "errors"
    errors.mkdir()
    (errors / "__init__.py").write_text(
        "from .common import TypeNotFoundError\n"
        "class RPCError(Exception): pass\n"
        "class AuthKeyError(Exception): pass\n"
    )
    (errors / "common.py").write_text("class TypeNotFoundError(Exception): pass\n")


def write_fake_pytgcalls(root: Path) -> None:
    """The daemon imports the media stack at module scope; these stand-ins let the
    non-media behaviour be exercised without the compiled ntgcalls binding."""
    package = root / "pytgcalls"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        """
from . import filters

class PyTgCalls:
    def __init__(self, *args, **kwargs):
        pass

__version__ = "2.3.3-test"
""".lstrip()
    )
    (package / "filters.py").write_text(
        """
def chat_update(*args, **kwargs):
    return lambda *a, **k: False

def stream_frame(*args, **kwargs):
    return lambda *a, **k: False
""".lstrip()
    )
    (package / "exceptions.py").write_text(
        "class NoActiveGroupCall(Exception): pass\n"
        "class NotInCallError(Exception): pass\n"
    )
    types_pkg = package / "types"
    types_pkg.mkdir()
    (types_pkg / "__init__.py").write_text(
        """
class _Placeholder:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)

class CallConfig(_Placeholder): pass
class ChatUpdate(_Placeholder):
    class Status:
        INCOMING_CALL = "incoming_call"
        LEFT_CALL = "left_call"
class Device:
    MICROPHONE = "microphone"
class Direction:
    INCOMING = "incoming"
class ExternalMedia:
    AUDIO = "audio"
class Frame(_Placeholder):
    class Info(_Placeholder): pass
class GroupCallConfig(_Placeholder): pass
class MediaStream(_Placeholder):
    class Flags:
        REQUIRED = 1
        IGNORE = 2
class RecordStream(_Placeholder): pass
class UpdatedGroupCallParticipant(_Placeholder): pass
""".lstrip()
    )
    (types_pkg / "raw.py").write_text(
        """
class AudioParameters:
    def __init__(self, bitrate=48000, channels=1):
        self.bitrate = bitrate
        self.channels = channels
""".lstrip()
    )
    (root / "ntgcalls.py").write_text("")


def import_daemon(tmp: Path, service_settings: dict, *,
                  connection_extra: dict | None = None,
                  voice_context: str | None = None,
                  worker_context: str | None = None,
                  jobs_prompt: str | None = None,
                  project_env: dict | None = None,
                  store: bool = False):
    """Import one daemon against a throwaway project.

    `store` gives that project the identity and the store the job register
    needs. Configuration stays in files: where a project keeps its settings and
    where its queue lives are different questions, and the register answers the
    second one the same way whatever the first says. Without it the register
    cannot open, and the daemon keeps answering with the job class off — the
    degradation every other test here exercises.
    """
    project = tmp / "project"
    service_dir = project / "capabilities" / "telegram" / "service"
    service_dir.mkdir(parents=True)
    if store:
        (project / "capabilities" / "project.json").write_text(json.dumps({
            "id": "11111111-1111-4111-8111-111111111111",
            "slug": "testproject",
            "store": "files",
        }) + "\n")
    settings_file = service_dir / "settings.json"
    context_file = service_dir / "context.md"
    worker_context_file = service_dir / "worker.md"
    voice_context_file = service_dir / "voice-agent.md"
    settings_file.write_text(json.dumps(service_settings) + "\n")
    context_file.write_text("test context\n")
    if worker_context is not None:
        worker_context_file.write_text(worker_context)
    if jobs_prompt is not None:
        (service_dir / "jobs.md").write_text(jobs_prompt)
    if voice_context is not None:
        voice_context_file.write_text(voice_context)
    if project_env:
        (project / ".env.local").write_text(
            "".join(f"{key}={value}\n" for key, value in project_env.items()))
    connections_file = tmp / "connections.json"
    connections_file.write_text(json.dumps({
        "connections": {"test": {"api_id": 12345, "allow_write": True,
                                 **(connection_extra or {})}},
    }) + "\n")
    fake_root = tmp / "fake"
    write_fake_telethon(fake_root)
    write_fake_pytgcalls(fake_root)

    stubbed = ("telethon", "telethon.tl", "telethon.tl.types",
               "telethon.tl.functions", "telethon.tl.functions.phone",
               "telethon.errors", "telethon.errors.common", "pytgcalls",
               "pytgcalls.filters", "pytgcalls.exceptions", "pytgcalls.types",
               "pytgcalls.types.raw", "ntgcalls", "call_recording_helpers",
               "voice_agent")

    old_env = dict(os.environ)
    old_path = list(sys.path)
    old_modules = {name: sys.modules.pop(name, None) for name in stubbed}
    project_layout = {
        "project_root": str(project),
        "capabilities": str(project / "capabilities"),
        "context": str(project / "context"),
        "routines": str(project / "routines"),
        "assets": str(project / "assets"),
        "memory": str(project / "memory"),
        "deployment": str(project / "deployment"),
        "provider": "test",
    }
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
            "TELEGRAM_SERVICE_PROJECT_ENVELOPE": str(project / "capabilities"),
            "TELEGRAM_SERVICE_PROJECT_LAYOUT": json.dumps(project_layout),
            "TELEGRAM_SERVICE_SETTINGS": str(settings_file),
            "TELEGRAM_SERVICE_STATE_DIR": str(tmp / "service-state"),
        })
        if store:
            os.environ["CAPABILITIES_STORE_URL"] = str(tmp / "store.sqlite3")
        else:
            os.environ.pop("CAPABILITIES_STORE_URL", None)
        os.environ.pop("TELEGRAM_SERVICE_VOICE_CONTEXT", None)
        if voice_context is not None:
            os.environ["TELEGRAM_SERVICE_VOICE_CONTEXT"] = str(voice_context_file)
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
        for name, module in old_modules.items():
            sys.modules.pop(name, None)
            if module is not None:
                sys.modules[name] = module


class Message:
    def __init__(self, message_id: int, *, text: str = "hello", voice: bool = False,
                 video_note: bool = False):
        self.id = message_id
        self.sender_id = 777
        self.text = "" if voice or video_note else text
        self.raw_text = self.text
        self.message = self.text
        self.voice = voice
        self.audio = False
        self.video_note = video_note
        self.photo = False
        self.file = (SimpleNamespace(
            mime_type="audio/ogg" if voice else "video/mp4", name=None)
            if voice or video_note else None)
        self.out = False
        self.downloads = 0
        self.is_reply = False
        self._reply_message = None

    async def get_sender(self):
        return SimpleNamespace(first_name="Test", last_name="User", username=None)

    async def download_media(self, file=None):
        self.downloads += 1
        await asyncio.sleep(0)
        return b"voice"

    async def get_reply_message(self):
        return self._reply_message


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
        self.catch_up_calls = 0
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
        # Return both initial messages and sent messages (echoes)
        all_messages = list(self.messages)
        for sent in self.sent:
            # Model Telethon's behavior: messages sent with parse_mode='html' are stored
            # as plain text (HTML tags are converted to entity markers, not preserved)
            text = sent.get("text", "")
            if sent.get("parse_mode") == "html":
                text = strip_html_tags(text)
            # Create a Message-like object for sent messages
            msg = SimpleNamespace(
                id=sent.get("id", 1000 + len(all_messages)),
                text=text,
                out=True,
                voice=False,
                photo=False,
                file=None,
                is_reply=sent.get("reply_to") is not None,
            )
            # If this is a reply, link it to the original message
            if sent.get("reply_to"):
                original = next((m for m in all_messages if m.id == sent["reply_to"]), None)
                async def get_reply(orig=original):
                    return orig
                msg.get_reply_message = get_reply
            all_messages.append(msg)
        return all_messages[-limit:] if limit else all_messages

    async def catch_up(self):
        self.catch_up_calls += 1
        await asyncio.sleep(0)

    @asynccontextmanager
    async def action(self, _chat, _kind):
        yield

    async def send_message(self, chat, text, **kwargs):
        self.send_attempts += 1
        if self.fail_sends:
            self.fail_sends -= 1
            raise RuntimeError("simulated outbound failure")
        msg_id = 1000 + len(self.sent)
        item = {"id": msg_id, "chat": chat, "text": text, **kwargs}
        self.sent.append(item)
        return SimpleNamespace(id=msg_id)

    async def run_until_disconnected(self):
        self.started.set()
        await self.disconnected.wait()

    async def disconnect(self):
        self.disconnected.set()


class ForumClient(FakeClient):
    def __init__(self, messages=(), *, fail_sends=0):
        super().__init__(messages, fail_sends=fail_sends)
        self.fetch_topics = []

    async def get_messages(self, chat, limit=None, reply_to=None):
        self.fetch_topics.append(reply_to)
        messages = await super().get_messages(chat, limit=None)
        if reply_to is not None:
            messages = [
                message for message in messages
                if getattr(
                    getattr(message, "reply_to", None),
                    "reply_to_top_id",
                    getattr(message, "reply_to_top_id", None),
                ) == reply_to
            ]
        return messages[-limit:] if limit else messages


def strip_html_tags(text):
    """Strip HTML tags to model Telethon's behavior: HTML sent with parse_mode='html'
    is stored as plain text with entity markers, not HTML tags."""
    import re
    return re.sub(r'<[^>]+>', '', text)


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
    async def test_register_replace_failure_keeps_previous_complete_json(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            original = {"test": {"last_processed_message_id": 7}}
            daemon.save_register(original)
            with mock.patch.object(daemon.os, "replace", side_effect=OSError("power loss")):
                with self.assertRaisesRegex(OSError, "power loss"):
                    daemon.save_register({"test": {"last_processed_message_id": 8}})
            self.assertEqual(daemon.load_register(), original)

    async def test_hardened_state_adopts_only_version_one_and_requires_cutover(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(),
                connection_extra={"expected_account_id": 123456})
            daemon.LAUNCH_NONCE = "test-launch-nonce"
            daemon._atomic_json(daemon.OWNERSHIP_FILE, {
                "schema": "telegram.ownership.v1",
                "protocol_version": 1,
                "account_id": daemon.EXPECTED_ACCOUNT_ID,
            })
            daemon._require_hardened_state()
            marker = json.loads(daemon.STATE_SCHEMA_FILE.read_text())
            self.assertEqual(marker["version"], 1)
            marker["version"] = 2
            daemon._atomic_json(daemon.STATE_SCHEMA_FILE, marker)
            with self.assertRaisesRegex(SystemExit, "version mismatch"):
                daemon._require_hardened_state()

    async def stop_session(self, client, task):
        client.disconnected.set()
        await asyncio.wait_for(task, timeout=5)

    async def test_codex_completed_empty_final_is_a_silent_success(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps({"type": "turn.completed", "usage": {
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "cached_input_tokens": 4,
                }}),
            ])
            with mock.patch.object(
                    daemon, "run_worker_proc", return_value=(0, stdout, "")):
                result = daemon.worker_codex("123", [], {}, {})

            self.assertTrue(result["silent"])
            self.assertEqual(result["reply"], "")
            self.assertEqual(result["meta"]["session_id"], "thread-1")
            self.assertEqual(result["meta"]["tokens"]["output"], 0)

    async def test_codex_is_told_to_run_the_hooks_no_daemon_can_have_trusted(self):
        """Hook trust is granted interactively, so a daemon never holds any, and
        a project's session hook is skipped in silence without the bypass."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ])
            seen = {}

            def capture(_key, cmd, *_args, **_kwargs):
                seen["cmd"] = cmd
                return (0, stdout, "")

            with mock.patch.object(
                    daemon.subprocess, "run",
                    return_value=SimpleNamespace(
                        stdout="  --dangerously-bypass-hook-trust\n")):
                with mock.patch.object(daemon, "run_worker_proc", side_effect=capture):
                    daemon.worker_codex("123", [], {}, {})

            self.assertIn("--dangerously-bypass-hook-trust", seen["cmd"])

    async def test_an_engine_that_never_heard_of_the_bypass_is_not_given_it(self):
        """The flag reaches the model only on engines that know it; an older one
        exits on an unknown argument and would take every turn with it."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ])
            seen = {}

            def capture(_key, cmd, *_args, **_kwargs):
                seen["cmd"] = cmd
                return (0, stdout, "")

            with mock.patch.object(
                    daemon.subprocess, "run",
                    return_value=SimpleNamespace(stdout="  --skip-git-repo-check\n")):
                with mock.patch.object(daemon, "run_worker_proc", side_effect=capture):
                    daemon.worker_codex("123", [], {}, {})

            self.assertNotIn("--dangerously-bypass-hook-trust", seen["cmd"])

    async def test_the_reported_codex_failure_is_the_one_codex_gave(self):
        """codex prints a routine line to stderr on every run and reports its
        actual refusal as a protocol event. Reading the first stderr line named
        stdin as the cause of every failure, including an exhausted quota."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            api_error = json.dumps({
                "type": "error", "status": 429,
                "error": {"type": "usage_limit_reached",
                          "message": "You've hit your usage limit."}})
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps({"type": "item.completed", "item": {
                    "id": "item_0", "type": "error",
                    "message": "Model metadata not found; using fallback."}}),
                json.dumps({"type": "error", "message": api_error}),
                json.dumps({"type": "turn.failed", "error": {"message": api_error}}),
            ])
            stderr = ("Reading additional input from stdin...\n"
                      "2026-08-30T16:03:12Z ERROR codex_models_manager: cache miss\n")
            with mock.patch.object(
                    daemon, "run_worker_proc", return_value=(1, stdout, stderr)):
                with self.assertRaisesRegex(RuntimeError, "hit your usage limit"):
                    daemon.worker_codex("123", [], {}, {})
            reason = daemon.codex_failure_reason(stdout, stderr, 1)
            self.assertNotIn("stdin", reason)
            self.assertTrue(daemon.is_quota_exhausted(reason))

    async def test_a_codex_failure_with_no_events_still_skips_the_routine_line(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            reason = daemon.codex_failure_reason(
                "", "Reading additional input from stdin...\nauth token expired\n", 1)
            self.assertEqual(reason, "auth token expired")
            self.assertEqual(
                daemon.codex_failure_reason(
                    "", "Reading additional input from stdin...\n", 7),
                "exit 7")
            self.assertFalse(daemon.is_quota_exhausted("auth token expired"))

    async def test_codex_empty_final_without_completion_remains_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            stdout = json.dumps({"type": "thread.started", "thread_id": "thread-1"})
            with mock.patch.object(
                    daemon, "run_worker_proc", return_value=(0, stdout, "")):
                with self.assertRaisesRegex(RuntimeError, "produced no final message"):
                    daemon.worker_codex("123", [], {}, {})

    async def test_silent_worker_completion_marks_job_done_without_telegram_error(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            def silent_worker(*_args):
                result = successful_result("")
                result["silent"] = True
                return result

            daemon.WORKERS["stub"] = silent_worker
            message = Message(90, text="Assistant, no acknowledgement needed")
            client = FakeClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(message))
            await wait_until(
                lambda: daemon.load_register()["123"]["last_processed_message_id"] == 90)

            self.assertEqual(client.sent, [])
            self.assertTrue(any("completed silently job msg=90" in line
                                for line in daemon._test_logs))
            await self.stop_session(client, task)

    async def test_final_reply_is_suppressed_when_progress_already_delivered_it(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            def echoing_worker(_chat, _tail, state=None, _procs=None):
                Path(state["progress_outbox"]).write_text(json.dumps({
                    "text": "The answer is ready."
                }) + "\n")
                return successful_result("The answer is ready.")

            daemon.WORKERS["stub"] = echoing_worker
            message = Message(91, text="Assistant, answer this")
            client = FakeClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(message))
            await wait_until(
                lambda: daemon.load_register()["123"]["last_processed_message_id"] == 91)

            self.assertEqual(len(client.sent), 1)
            self.assertIn("The answer is ready.", client.sent[0]["text"])
            self.assertTrue(any("suppressed reply already sent as progress" in line
                                for line in daemon._test_logs))
            await self.stop_session(client, task)

    async def test_registered_job_hard_handoff_ends_a_duplicate_dialogue_turn(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(worker_timeout=2), store=True)
            daemon.DIALOGUE_HANDOFF_GRACE_SECONDS = 0.02
            message = Message(92, text="Do a long count")
            client = FakeClient([message])

            def worker(_chat, _tail, state=None, _procs=None):
                if state["current_request"]["kind"] == "registered job":
                    return successful_result("count complete")
                store, register = daemon.jobs.open_register(
                    daemon._records_module(), daemon.PROJECT_CAPABILITIES_DIR,
                    daemon.ENVIRONMENT, url=daemon.STORE_URL)
                try:
                    row = register.register(
                        channel_key=state["channel_key"],
                        requested_by=state["current_request"]["sender_id"],
                        origin_message_id=state["current_request"]["message_id"],
                        description="Count every topic",
                        engine="stub",
                    )
                    # The handoff is the submit, so the turn does both before
                    # it signals: a draft would never reach the runner.
                    row = register.submit(row["id"])
                finally:
                    store.close()
                Path(state["progress_outbox"]).write_text(
                    json.dumps({
                        "event": "job_submitted",
                        "job_id": row["id"],
                        "description": row["description"],
                    }) + "\n" + json.dumps({"text": "Counting every topic."}) + "\n")
                while not state["cancel_event"].wait(0.005):
                    pass
                raise RuntimeError("dialogue worker was handed off")

            daemon.WORKERS["stub"] = worker
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(message))
            await wait_until(
                lambda: daemon.load_register()["123"]["last_processed_message_id"] == 92)

            def durable_job_succeeded():
                rows = daemon.job_register().list(limit=1)
                return bool(rows and rows[0]["outcome"] == "succeeded")

            await wait_until(durable_job_succeeded)
            await wait_until(
                lambda: any(item["text"] == "count complete"
                            for item in client.sent))

            texts = [item["text"] for item in client.sent]
            self.assertIn("Counting every topic.", texts)
            self.assertIn("count complete", texts)
            self.assertFalse(any("Worker error" in text for text in texts))
            self.assertTrue(any("handed off to durable job" in line
                                for line in daemon._test_logs))
            await self.stop_session(client, task)

    async def test_prompt_uses_compact_timestamps_and_reply_topology(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            tail = [
                {"id": 100, "date": "2026-08-05", "time": "20:41",
                 "sender": "Юрий", "text": "Первое сообщение"},
                {"id": 101, "date": "2026-08-05", "time": "20:42",
                 "sender": "Константин", "text": "Марвин, посмотри",
                 "in_reply_to": {"id": 100, "sender": "Юрий", "is_assistant": False}},
            ]
            prompt = daemon.build_prompt(tail, {"current_request": {
                "message_id": 101,
                "sender_name": "Константин",
                "sender_role": "supervisor",
                "kind": "text",
                "text": "Марвин, посмотри",
                "in_reply_to": tail[1]["in_reply_to"],
            }})

            self.assertEqual(prompt.count("--- 2026-08-05 ---"), 1)
            self.assertIn("[20:41 #100] Юрий: Первое сообщение", prompt)
            self.assertIn(
                "[20:42 #101 | reply to #100 by Юрий] Константин: Марвин, посмотри",
                prompt,
            )
            self.assertIn("Reply to: #100 by Юрий", prompt)
            self.assertIn("Message proximity alone", prompt)

    async def test_live_tail_resolves_reply_author_only_inside_window(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {"aliases": ["Марвин"]}}
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            captured = {}

            def capture_worker(_chat, tail, state=None, _procs=None):
                captured["tail"] = tail
                captured["state"] = state
                return successful_result("Готово")

            daemon.WORKERS["stub"] = capture_worker
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            original = Message(100, text="Что думаешь?")
            original.sender_id = 888
            original.date = datetime(2026, 8, 5, 17, 41, tzinfo=timezone.utc)

            async def original_sender():
                return SimpleNamespace(first_name="Юрий", last_name="", username=None)

            original.get_sender = original_sender
            request = Message(101, text="Марвин, посмотри")
            request.sender_id = 777
            request.mentioned = False
            request.is_reply = True
            request.reply_to_msg_id = 100
            request._reply_message = original
            request.date = datetime(2026, 8, 5, 17, 42, tzinfo=timezone.utc)
            client.messages.extend([original, request])

            event = Event(request, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: "state" in captured)

            current = captured["state"]["current_request"]
            self.assertEqual(current["in_reply_to"], {
                "id": 100,
                "sender": "Юрий",
                "is_assistant": False,
            })
            request_tail = next(row for row in captured["tail"] if row["id"] == 101)
            self.assertEqual(request_tail["date"], "2026-08-05")
            self.assertEqual(request_tail["time"], "20:42")
            self.assertEqual(request_tail["in_reply_to"]["sender"], "Юрий")
            await self.stop_session(client, task)

    async def test_daemon_refuses_a_session_owned_by_another_account(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            daemon.EXPECTED_ACCOUNT_ID = 8200881535
            client = FakeClient([])  # the fixture is account id 42

            with self.assertRaisesRegex(
                    daemon.IdentityMismatch, r"expected 8200881535, got 42"):
                await daemon.run_session(client)

    async def test_forum_topics_have_independent_history_and_channel_state(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {"aliases": ["Assistant"]}}
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({})

            def in_topic(message_id, topic_id, text, reply_to=None):
                message = Message(message_id, text=text)
                message.mentioned = "Assistant" in text
                message.reply_to = SimpleNamespace(
                    forum_topic=True,
                    reply_to_top_id=topic_id,
                    reply_to_msg_id=reply_to,
                )
                message.reply_to_msg_id = reply_to
                return message

            one = in_topic(101, 10, "topic one context")
            two = in_topic(102, 20, "topic two context")
            request_one = in_topic(103, 10, "Assistant, answer one", reply_to=101)
            request_two = in_topic(104, 20, "Assistant, answer two", reply_to=102)
            client = FakeClient([one, two, request_one, request_two])
            captured = {}

            def capture(_chat, tail, state=None, _procs=None):
                captured[state["topic_id"]] = {"tail": tail, "state": state}
                return successful_result(f"reply topic {state['topic_id']}")

            daemon.WORKERS["stub"] = capture
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            for message in (request_one, request_two):
                event = Event(message, chat_id=-200)
                event.is_private = False
                await client.handler(event)
            await wait_until(lambda: set(captured) == {10, 20})

            first_text = [row["text"] for row in captured[10]["tail"]]
            second_text = [row["text"] for row in captured[20]["tail"]]
            self.assertIn("topic one context", first_text)
            self.assertNotIn("topic two context", first_text)
            self.assertIn("topic two context", second_text)
            self.assertNotIn("topic one context", second_text)
            self.assertEqual(captured[10]["state"]["channel_key"], "-200#topic:10")
            self.assertEqual(captured[20]["state"]["channel_key"], "-200#topic:20")
            current = next(row for row in captured[10]["tail"] if row["id"] == 103)
            self.assertEqual(current["in_reply_to"]["id"], 101)
            await self.stop_session(client, task)

    async def test_general_topic_reply_chain_stays_on_the_base_channel(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {"aliases": ["Assistant"]}}
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({})

            # A forum's general topic leaves forum_topic unset and carries
            # reply_to_top_id with the root of a plain reply chain.
            def in_general(message_id, text, reply_to=None, chain_root=None):
                message = Message(message_id, text=text)
                message.mentioned = "Assistant" in text
                if reply_to is not None:
                    message.reply_to = SimpleNamespace(
                        forum_topic=False,
                        reply_to_top_id=chain_root,
                        reply_to_msg_id=reply_to,
                    )
                message.reply_to_msg_id = reply_to
                return message

            root = in_general(201, "general context")
            answer = in_general(202, "an earlier answer", reply_to=201)
            request = in_general(
                203, "Assistant, answer here", reply_to=202, chain_root=201)
            client = FakeClient([root, answer, request])
            captured = {}

            def capture(_chat, tail, state=None, _procs=None):
                captured["state"] = state
                return successful_result("answered in general")

            daemon.WORKERS["stub"] = capture
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(request, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: "state" in captured)

            self.assertIsNone(captured["state"]["topic_id"])
            self.assertEqual(captured["state"]["channel_key"], "-200")
            await self.stop_session(client, task)

    async def test_forum_topic_progress_uses_the_prompted_wrapper_and_reply(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"aliases": ["Assistant"]},
            }
            daemon = import_daemon(Path(td), service_settings)
            context_template = (
                TELEGRAM_DIR / "service" / "templates" / "context.md").read_text()
            daemon.CONTEXT_FILE.write_text(context_template.replace(
                "{{TELEGRAM_PROGRESS_COMMAND}}", "telegram send <chat_id> <text>"))
            daemon.save_register({})
            captured = {}

            def worker(_chat, tail, state=None, _procs=None):
                captured["tail"] = tail
                captured["state"] = state
                captured["prompt"] = daemon.build_prompt(tail, state)
                captured["env"] = daemon.worker_env(state)
                sent = daemon.subprocess.run(
                    [str(daemon.WORKER_BIN / "telegram"), "send", "-200",
                     "checking this topic"],
                    env=captured["env"], capture_output=True, text=True, check=False)
                if sent.returncode:
                    raise AssertionError(sent.stderr)
                return successful_result("topic result")

            daemon.WORKERS["stub"] = worker
            request = Message(105, text="Assistant, check this topic")
            request.mentioned = True
            request.reply_to = SimpleNamespace(
                forum_topic=True,
                reply_to_top_id=10,
                reply_to_msg_id=None,
            )
            client = ForumClient([request])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            event = Event(request, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: len(client.sent) == 2)

            command = (
                f'{daemon.WORKER_BIN / "telegram"} send -200 "<one short line>"')
            self.assertIn(f"`{command}`", captured["prompt"])
            self.assertNotIn("`telegram send <chat_id> <text>`", captured["prompt"])
            daemon.CONTEXT_FILE.write_text(context_template)
            self.assertIn(
                f"`{command}`",
                daemon.build_prompt(captured["tail"], captured["state"]),
            )
            self.assertEqual(captured["env"]["TELEGRAM_AUTHORIZED_TOPIC_ID"], "10")
            self.assertCountEqual(
                [(item["text"], item.get("reply_to")) for item in client.sent],
                [("checking this topic", 105), ("topic result", 105)],
            )
            await self.stop_session(client, task)

    async def test_base_chat_catch_up_uses_topic_key_and_dedupes_later_passes(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=0.02, sync_stale_after=0.2)
            service_settings["allowed_groups"] = {
                "-200": {"aliases": ["Assistant"]},
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({
                "-200": {"last_processed_message_id": 999},
                "-200#topic:10": {"last_processed_message_id": 0},
            })

            message = Message(105, text="Assistant, recover this")
            message.mentioned = True
            message.reply_to = SimpleNamespace(
                forum_topic=True,
                reply_to_top_id=10,
                reply_to_msg_id=None,
            )
            entered = threading.Event()
            release = threading.Event()
            dispatches = []

            def capture(_chat, _tail, state=None, _procs=None):
                dispatches.append(state)
                entered.set()
                release.wait(timeout=2)
                return successful_result("recovered")

            daemon.WORKERS["stub"] = capture
            client = ForumClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(entered.is_set)

            reg = daemon.load_register()
            self.assertNotIn("105", reg["-200"].get("jobs", {}))
            self.assertEqual(
                reg["-200#topic:10"]["jobs"]["105"]["channel_key"],
                "-200#topic:10",
            )
            self.assertEqual(dispatches[0]["channel_key"], "-200#topic:10")

            release.set()
            await wait_until(
                lambda: daemon.load_register()["-200#topic:10"][
                    "last_processed_message_id"] == 105)
            fetches_after_dispatch = client.get_messages_calls
            await wait_until(
                lambda: client.get_messages_calls >= fetches_after_dispatch + 2)
            self.assertEqual(len(dispatches), 1)
            self.assertEqual(client.send_attempts, 1)
            await self.stop_session(client, task)

    async def test_base_chat_catch_up_honors_legacy_watermark(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=60)
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "agent_dialogue": {
                        "max_turns": 2,
                        "reset_on_human_message": True,
                    },
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 105}})

            message = Message(105, text="Assistant, already handled before upgrade")
            message.mentioned = True
            message.reply_to = SimpleNamespace(
                forum_topic=True,
                reply_to_top_id=10,
                reply_to_msg_id=None,
            )
            dispatches = []
            daemon.WORKERS["stub"] = lambda *_args, **_kwargs: dispatches.append(True)
            client = ForumClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            self.assertEqual(dispatches, [])
            self.assertEqual(client.send_attempts, 0)
            self.assertNotIn("-200#topic:10", daemon.load_register())
            await self.stop_session(client, task)

    async def test_base_chat_catch_up_honors_legacy_queued_and_running_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=60)
            service_settings["allowed_groups"] = {
                "-200": {"aliases": ["Assistant"]},
            }
            daemon = import_daemon(Path(td), service_settings)
            initial_register = {
                "-200": {
                    "last_processed_message_id": 0,
                    "jobs": {
                        "106": {"message_id": 106, "status": "queued"},
                        "107": {"message_id": 107, "status": "running"},
                    },
                },
                "-200#topic:10": {"last_processed_message_id": 0},
                "-200#topic:20": {"last_processed_message_id": 0},
            }
            daemon.save_register(initial_register)
            before_check = json.loads(json.dumps(initial_register))
            self.assertTrue(daemon._catch_up_message_is_known(
                initial_register, "-200#topic:10", "-200", 106))
            self.assertTrue(daemon._catch_up_message_is_known(
                initial_register, "-200#topic:20", "-200", 107))
            self.assertEqual(initial_register, before_check)
            daemon._has_pending_jobs = lambda _reg, _key: False

            messages = []
            for message_id, topic_id in ((106, 10), (107, 20)):
                message = Message(message_id, text="Assistant, already queued")
                message.mentioned = True
                message.reply_to = SimpleNamespace(
                    forum_topic=True,
                    reply_to_top_id=topic_id,
                    reply_to_msg_id=None,
                )
                messages.append(message)
            client = ForumClient(messages)
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            register = daemon.load_register()
            self.assertEqual(
                register["-200#topic:10"]["last_processed_message_id"], 0)
            self.assertEqual(
                register["-200#topic:20"]["last_processed_message_id"], 0)
            self.assertNotIn("106", register["-200#topic:10"].get("jobs", {}))
            self.assertNotIn("107", register["-200#topic:20"].get("jobs", {}))
            self.assertEqual(client.send_attempts, 0)
            await self.stop_session(client, task)

    async def test_periodic_known_human_replay_does_not_reset_newer_dialogue(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=0.02, sync_stale_after=0.2)
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "agent_dialogue": {
                        "max_turns": 3,
                        "reset_on_human_message": True,
                    },
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            dialogue = {
                "turns": 2,
                "reset_message_id": 110,
                "reset_at": "2026-08-11T12:00:00+00:00",
            }
            daemon.save_register({
                "-200": {"last_processed_message_id": 0},
                "-200#topic:10": {
                    "last_processed_message_id": 105,
                    "agent_dialogue": dialogue,
                },
            })
            client = ForumClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            replay = Message(105, text="older human conversation")
            replay.reply_to = SimpleNamespace(
                forum_topic=True,
                reply_to_top_id=10,
                reply_to_msg_id=None,
            )
            client.messages.append(replay)
            fetches = client.get_messages_calls
            await wait_until(lambda: client.get_messages_calls >= fetches + 2)

            current = daemon.load_register()["-200#topic:10"]["agent_dialogue"]
            self.assertEqual(current, dialogue)
            self.assertEqual(client.send_attempts, 0)
            await self.stop_session(client, task)

    async def test_topic_call_recording_catch_up_uses_base_chat_for_watcher(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=60)
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "call_recording": {"mode": "on_request"},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            daemon.configured_call_recording_groups = lambda: {
                "auto": {}, "on_request": {},
            }

            message = Message(108, text="/record")
            message.reply_to = SimpleNamespace(
                forum_topic=True,
                reply_to_top_id=10,
                reply_to_msg_id=None,
            )
            client = ForumClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            requests = list(daemon.CALL_RECORDING_REQUEST_DIR.glob("*.json"))
            self.assertEqual(len(requests), 1)
            payload = json.loads(requests[0].read_text())
            self.assertEqual(payload["chat_id"], "-200")
            self.assertNotIn("topic", requests[0].name)
            register = daemon.load_register()
            self.assertEqual(
                register["-200#topic:10"]["last_processed_message_id"], 108)
            self.assertEqual(register["-200"]["last_processed_message_id"], 0)
            self.assertEqual(client.send_attempts, 1)
            await self.stop_session(client, task)

    async def test_live_and_catch_up_share_topic_call_recording_reservation(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=60)
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "call_recording": {"mode": "on_request"},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            daemon.configured_call_recording_groups = lambda: {
                "auto": {}, "on_request": {},
            }

            message = Message(109, text="/record")
            message.reply_to = SimpleNamespace(
                forum_topic=True,
                reply_to_top_id=10,
                reply_to_msg_id=None,
            )
            profile_started = asyncio.Event()
            release_profile = asyncio.Event()
            profile_calls = 0

            async def delayed_sender():
                nonlocal profile_calls
                profile_calls += 1
                if profile_calls == 1:
                    profile_started.set()
                    await release_profile.wait()
                return SimpleNamespace(
                    first_name="Test", last_name="User", username=None)

            message.get_sender = delayed_sender
            client = ForumClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await profile_started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            release_profile.set()
            await client.started.wait()

            requests = list(daemon.CALL_RECORDING_REQUEST_DIR.glob("*.json"))
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                json.loads(requests[0].read_text())["chat_id"], "-200")
            self.assertEqual(profile_calls, 1)
            self.assertEqual(client.send_attempts, 1)
            register = daemon.load_register()
            topic = register["-200#topic:10"]
            self.assertEqual(topic["last_processed_message_id"], 109)
            self.assertEqual(topic.get("jobs", {}), {})
            self.assertEqual(register["-200"]["last_processed_message_id"], 0)
            await self.stop_session(client, task)

    async def test_call_recording_ack_failure_does_not_leave_worker_job(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=60)
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "call_recording": {"mode": "on_request"},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(110, text="/record")
            message.reply_to = SimpleNamespace(
                forum_topic=True,
                reply_to_top_id=10,
                reply_to_msg_id=None,
            )
            client = ForumClient([message], fail_sends=1)

            with self.assertRaisesRegex(RuntimeError, "simulated outbound failure"):
                await daemon.run_session(client)

            topic = daemon.load_register()["-200#topic:10"]
            self.assertEqual(topic["last_processed_message_id"], 110)
            self.assertEqual(topic.get("jobs", {}), {})
            self.assertEqual(
                len(list(daemon.CALL_RECORDING_REQUEST_DIR.glob("*.json"))), 1)

    async def test_unaddressed_topic_catch_up_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=60)
            service_settings["allowed_groups"] = {
                "-200": {"aliases": ["Assistant"]},
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(109, text="conversation for another participant")
            message.reply_to = SimpleNamespace(
                forum_topic=True,
                reply_to_top_id=10,
                reply_to_msg_id=None,
            )
            client = ForumClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            self.assertEqual(
                daemon.load_register(),
                {"-200": {"last_processed_message_id": 0}},
            )
            self.assertEqual(client.send_attempts, 0)
            await self.stop_session(client, task)

    async def test_periodic_base_chat_catch_up_arms_every_discovered_topic(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=0.02, sync_stale_after=0.2)
            service_settings["allowed_groups"] = {
                "-200": {"aliases": ["Assistant"]},
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            client = ForumClient([])
            dispatched = []

            def capture(_chat, _tail, state=None, _procs=None):
                dispatched.append(state["channel_key"])
                return successful_result(f"reply for {state['channel_key']}")

            daemon.WORKERS["stub"] = capture
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            for message_id, topic_id in ((106, 10), (107, 20)):
                message = Message(message_id, text="Assistant, recover this topic")
                message.mentioned = True
                message.reply_to = SimpleNamespace(
                    forum_topic=True,
                    reply_to_top_id=topic_id,
                    reply_to_msg_id=None,
                )
                client.messages.append(message)

            await wait_until(lambda: len(dispatched) == 2)
            await wait_until(
                lambda: all(
                    daemon.load_register().get(key, {}).get(
                        "last_processed_message_id") == message_id
                    for key, message_id in (
                        ("-200#topic:10", 106),
                        ("-200#topic:20", 107),
                    )
                )
            )
            self.assertEqual(set(dispatched), {
                "-200#topic:10",
                "-200#topic:20",
            })
            reg = daemon.load_register()
            self.assertEqual(reg["-200#topic:10"]["last_processed_message_id"], 106)
            self.assertEqual(reg["-200#topic:20"]["last_processed_message_id"], 107)
            self.assertEqual(client.send_attempts, 2)
            await self.stop_session(client, task)

    async def test_agent_member_uses_explicit_address_and_top_level_responses(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            group_policy = {
                "members": {
                    "5509911365": {
                        "name": "Solomon",
                        "kind": "agent",
                        "address_aliases": ["Соломон", "Solomon"],
                    },
                },
            }

            self.assertIsNone(
                daemon._reply_target(100, "5509911365", group_policy, False))
            self.assertEqual(
                daemon._reply_target(100, "777", group_policy, False), 100)
            self.assertIsNone(
                daemon._reply_target(100, "5509911365", group_policy, True))
            self.assertIn(
                "top-level group message",
                daemon._delivery_description(None, False),
            )
            self.assertEqual(daemon._agent_peers(group_policy), [{
                "id": "5509911365",
                "name": "Solomon",
                "aliases": ["Соломон", "Solomon"],
            }])

            me = SimpleNamespace(id=99, username="marvin")
            replied = SimpleNamespace(out=True, sender_id=99)

            async def get_reply_message():
                return replied

            reply_only = SimpleNamespace(
                sender_id=5509911365,
                raw_text="Понял.",
                mentioned=False,
                is_reply=True,
                get_reply_message=get_reply_message,
            )
            named = SimpleNamespace(
                sender_id=5509911365,
                raw_text="Марвин, продолжим?",
                mentioned=False,
                is_reply=True,
                get_reply_message=get_reply_message,
            )
            policy = {**group_policy, "aliases": ["Марвин"]}
            self.assertFalse(await daemon._message_addresses_me(reply_only, me, policy))
            self.assertTrue(await daemon._message_addresses_me(named, me, policy))

    async def test_agent_dialogue_has_finite_turns_and_human_reset(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            policy = {
                "members": {"5509911365": {"name": "Solomon", "kind": "agent"}},
                "agent_dialogue": {"max_turns": 2, "reset_on_human_message": True},
            }
            reg = {}
            first = SimpleNamespace(id=10, sender_id=5509911365)
            second = SimpleNamespace(id=11, sender_id=5509911365)
            third = SimpleNamespace(id=12, sender_id=5509911365)

            self.assertEqual(daemon._admit_agent_turn(reg, "-1", first, policy),
                             (True, {"turns": 1, "max_turns": 2}))
            self.assertEqual(daemon._admit_agent_turn(reg, "-1", second, policy),
                             (True, {"turns": 2, "max_turns": 2}))
            self.assertEqual(daemon._admit_agent_turn(reg, "-1", third, policy),
                             (False, {"turns": 2, "max_turns": 2}))

            human = SimpleNamespace(id=13, sender_id=777)
            self.assertTrue(daemon._reset_agent_dialogue_for_human(
                reg, "-1", human, policy))
            self.assertEqual(daemon._agent_dialogue_snapshot(reg, "-1", policy)["turns"], 0)
            self.assertEqual(daemon._admit_agent_turn(reg, "-1", third, policy),
                             (True, {"turns": 1, "max_turns": 2}))

    async def test_agent_loop_cap_is_enforced_by_live_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Марвин"],
                    "require_reference": False,
                    "members": {
                        "5509911365": {"name": "Solomon", "kind": "agent"},
                    },
                    "agent_dialogue": {
                        "max_turns": 1,
                        "reset_on_human_message": True,
                    },
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            daemon.WORKERS["stub"] = lambda *_args: successful_result("Ответ")
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            reply_only = Message(1, text="Продолжаю")
            reply_only.sender_id = 5509911365
            reply_only.mentioned = False
            reply_only.is_reply = True
            reply_only._reply_message = SimpleNamespace(out=True, sender_id=42)
            reply_event = Event(reply_only, chat_id=-200)
            reply_event.is_private = False
            await client.handler(reply_event)
            await asyncio.sleep(0.02)
            self.assertEqual(client.send_attempts, 0)

            first = Message(2, text="Марвин, продолжим")
            first.sender_id = 5509911365
            first.mentioned = False
            first_event = Event(first, chat_id=-200)
            first_event.is_private = False
            await client.handler(first_event)
            await wait_until(lambda: len(client.sent) == 1)
            self.assertIsNone(client.sent[0].get("reply_to"))

            capped = Message(3, text="Марвин, ещё ход")
            capped.sender_id = 5509911365
            capped.mentioned = False
            capped_event = Event(capped, chat_id=-200)
            capped_event.is_private = False
            await client.handler(capped_event)
            await asyncio.sleep(0.02)
            self.assertEqual(len(client.sent), 1)

            human = Message(4, text="Новая человеческая реплика")
            human.sender_id = 777
            human.mentioned = False
            human_event = Event(human, chat_id=-200)
            human_event.is_private = False
            await client.handler(human_event)
            await wait_until(lambda: len(client.sent) == 2)

            after_reset = Message(5, text="Марвин, теперь можно")
            after_reset.sender_id = 5509911365
            after_reset.mentioned = False
            after_reset_event = Event(after_reset, chat_id=-200)
            after_reset_event.is_private = False
            await client.handler(after_reset_event)
            await wait_until(lambda: len(client.sent) == 3)
            self.assertIsNone(client.sent[2].get("reply_to"))
            await self.stop_session(client, task)

    async def test_may_address_is_a_boolean_on_users_and_members(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_users"] = {
                "888": {"name": "Muted", "may_address": False},
            }
            service_settings["allowed_groups"] = {
                "-200": {"members": {"888": {"name": "Muted", "may_address": True}}},
            }
            daemon = import_daemon(Path(td), service_settings)

            self.assertIs(daemon.ALLOWED["888"]["may_address"], False)

            invalid = settings()
            invalid["allowed_groups"] = {
                "-200": {"members": {"888": {"may_address": "no"}}},
            }
            daemon.SETTINGS_FILE.write_text(json.dumps(invalid) + "\n")
            result = daemon.reload_runtime_settings()

            self.assertFalse(result["ok"])
            self.assertIn(
                "settings.allowed_groups.-200.members.888.may_address: must be a boolean",
                result["error"],
            )

    async def test_muted_member_mention_dispatches_nothing_but_stays_in_the_tail(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "members": {"888": {"name": "Muted", "may_address": False}},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            muted = Message(201, text="Assistant, answer me")
            muted.sender_id = 888
            muted.mentioned = True
            caller = Message(202, text="Assistant, answer me")
            caller.mentioned = True
            client = FakeClient([muted])
            captured = {}

            def capture(_chat, tail, state=None, _procs=None):
                captured["tail"] = tail
                captured["state"] = state
                return successful_result("ok")

            daemon.WORKERS["stub"] = capture
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            muted_event = Event(muted, chat_id=-200)
            muted_event.is_private = False
            await client.handler(muted_event)
            await asyncio.sleep(0.02)

            self.assertEqual(client.send_attempts, 0)
            self.assertEqual(daemon.load_register()["-200"],
                             {"last_processed_message_id": 0})

            client.messages.append(caller)
            caller_event = Event(caller, chat_id=-200)
            caller_event.is_private = False
            await client.handler(caller_event)
            await wait_until(lambda: "tail" in captured)

            self.assertIn(201, [row["id"] for row in captured["tail"]])
            self.assertIn("Muted",
                          [p["name"] for p in captured["state"]["participants"]])
            await self.stop_session(client, task)

    async def test_muted_agent_peer_cannot_address_the_assistant(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "members": {
                        "4242": {
                            "name": "Peer",
                            "kind": "agent",
                            "may_address": False,
                        },
                    },
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            daemon.WORKERS["stub"] = lambda *_args: successful_result("ok")
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            message = Message(203, text="Assistant, continue")
            message.sender_id = 4242
            message.mentioned = True
            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await asyncio.sleep(0.02)

            self.assertEqual(client.send_attempts, 0)
            await self.stop_session(client, task)

    async def test_muted_member_stays_silent_where_no_reference_is_required(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {
                    "require_reference": False,
                    "members": {"888": {"name": "Muted", "may_address": False}},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            daemon.WORKERS["stub"] = lambda *_args: successful_result("ok")
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            muted = Message(204, text="just talking")
            muted.sender_id = 888
            muted.mentioned = False
            muted_event = Event(muted, chat_id=-200)
            muted_event.is_private = False
            await client.handler(muted_event)
            await asyncio.sleep(0.02)
            self.assertEqual(client.send_attempts, 0)

            other = Message(205, text="just talking")
            other.mentioned = False
            other_event = Event(other, chat_id=-200)
            other_event.is_private = False
            await client.handler(other_event)
            await wait_until(lambda: len(client.sent) == 1)
            await self.stop_session(client, task)

    async def test_muted_member_record_request_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=60)
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "call_recording": {"mode": "on_request"},
                    "members": {"888": {"name": "Muted", "may_address": False}},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            daemon.configured_call_recording_groups = lambda: {
                "auto": {}, "on_request": {},
            }
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            message = Message(206, text="/record")
            message.sender_id = 888
            message.mentioned = False
            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await asyncio.sleep(0.02)

            self.assertEqual(
                list(daemon.CALL_RECORDING_REQUEST_DIR.glob("*.json")), [])
            self.assertEqual(client.send_attempts, 0)
            self.assertEqual(daemon.load_register()["-200"],
                             {"last_processed_message_id": 0})
            await self.stop_session(client, task)

    async def test_muted_member_control_command_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "members": {"888": {"name": "Muted", "may_address": False}},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            muted = Message(207, text="/status")
            muted.sender_id = 888
            muted.mentioned = True
            muted_event = Event(muted, chat_id=-200)
            muted_event.is_private = False
            await client.handler(muted_event)
            await asyncio.sleep(0.02)
            self.assertEqual(client.send_attempts, 0)

            other = Message(208, text="/status")
            other.mentioned = True
            other_event = Event(other, chat_id=-200)
            other_event.is_private = False
            await client.handler(other_event)
            await wait_until(lambda: len(client.sent) == 1)
            await self.stop_session(client, task)

    async def test_muted_member_ambient_voice_is_transcribed_without_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {
                    "voice_transcription": {"mode": "auto"},
                    "members": {"888": {"name": "Muted", "may_address": False}},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(209, voice=True)
            message.sender_id = 888
            message.mentioned = False
            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = (
                lambda audio, mime: transcriptions.append((audio, mime))
                or "Hey Assistant, what is the plan?")
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(
                lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 209)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(client.send_attempts, 1)
            self.assertIn("Hey Assistant", client.sent[0]["text"])
            self.assertEqual(daemon.load_register()["-200"]["jobs"], {})
            await self.stop_session(client, task)

    async def test_group_member_entry_restores_a_globally_muted_sender(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_users"] = {
                "888": {"name": "Muted", "may_address": False},
            }
            service_settings["allowed_groups"] = {
                "-200": {"aliases": ["Assistant"]},
                "-300": {
                    "aliases": ["Assistant"],
                    "members": {"888": {"name": "Muted", "may_address": True}},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({
                "-200": {"last_processed_message_id": 0},
                "-300": {"last_processed_message_id": 0},
            })
            daemon.WORKERS["stub"] = lambda *_args: successful_result("ok")
            client = FakeClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            silenced = Message(210, text="Assistant, answer me")
            silenced.sender_id = 888
            silenced.mentioned = True
            silenced_event = Event(silenced, chat_id=-200)
            silenced_event.is_private = False
            await client.handler(silenced_event)
            await asyncio.sleep(0.02)
            self.assertEqual(client.send_attempts, 0)

            restored = Message(211, text="Assistant, answer me")
            restored.sender_id = 888
            restored.mentioned = True
            restored_event = Event(restored, chat_id=-300)
            restored_event.is_private = False
            await client.handler(restored_event)
            await wait_until(lambda: len(client.sent) == 1)
            await self.stop_session(client, task)

    async def test_catch_up_replay_dispatches_only_for_a_sender_who_may_address(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=60)
            service_settings["allowed_groups"] = {
                "-200": {
                    "aliases": ["Assistant"],
                    "members": {"888": {"name": "Muted", "may_address": False}},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 300}})

            muted = Message(302, text="Assistant, answer me")
            muted.sender_id = 888
            muted.mentioned = True
            caller = Message(301, text="Assistant, answer me")
            caller.mentioned = True
            dispatched = []

            def capture(_chat, _tail, state=None, _procs=None):
                dispatched.append(state["current_request"]["message_id"])
                return successful_result("ok")

            daemon.WORKERS["stub"] = capture
            # The replay walks the fetched tail in reverse, so listing the muted
            # note last has catch-up weigh it before any watermark moves.
            client = FakeClient([caller, muted])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(
                lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 301)

            self.assertEqual(dispatched, [301])
            self.assertEqual(client.send_attempts, 1)
            self.assertEqual(daemon.load_register()["-200"]["jobs"], {})
            await self.stop_session(client, task)

    async def test_catch_up_ambient_voice_from_a_muted_sender_echoes_without_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=60)
            service_settings["allowed_groups"] = {
                "-200": {
                    "voice_transcription": {"mode": "auto"},
                    "members": {"888": {"name": "Muted", "may_address": False}},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(303, voice=True)
            message.sender_id = 888
            message.mentioned = False
            dispatched = []
            daemon.WORKERS["stub"] = (
                lambda *_args: dispatched.append(True) or successful_result("ok"))
            transcriptions = []
            daemon.deepgram_transcribe = (
                lambda audio, mime: transcriptions.append((audio, mime))
                or "Hey Assistant, what is the plan?")
            client = FakeClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await wait_until(
                lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 303)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(client.send_attempts, 1)
            self.assertIn("Hey Assistant", client.sent[0]["text"])
            self.assertEqual(dispatched, [])
            self.assertEqual(daemon.load_register()["-200"]["jobs"], {})
            await self.stop_session(client, task)

    async def test_reload_replaces_live_policy_without_reimporting_the_daemon(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=20)
            service_settings["assistant_name"] = "Before"
            daemon = import_daemon(Path(td), service_settings)
            original_module = id(daemon)

            updated = settings(sync_interval=7)
            updated["assistant_name"] = "After"
            updated["allowed_users"] = {"42": {"name": "New caller"}}
            daemon.SETTINGS_FILE.write_text(json.dumps(updated) + "\n")
            result = daemon.reload_runtime_settings()

            self.assertTrue(result["ok"])
            self.assertEqual(id(daemon), original_module)
            self.assertEqual(daemon.ASSISTANT_NAME, "After")
            self.assertEqual(daemon.SYNC_INTERVAL, 7)
            self.assertIn("42", daemon.ALLOWED)
            self.assertEqual(daemon.SETTINGS_GENERATION, 2)
            health = json.loads(daemon.HEALTH_FILE.read_text())
            self.assertEqual(health["settings_generation"], 2)

    async def test_invalid_reload_keeps_the_previous_settings(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["assistant_name"] = "Still here"
            daemon = import_daemon(Path(td), service_settings)
            daemon.SETTINGS_FILE.write_text("{not-json\n")

            result = daemon.reload_runtime_settings()

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "reload_refused")
            self.assertEqual(daemon.ASSISTANT_NAME, "Still here")
            self.assertEqual(daemon.SETTINGS_GENERATION, 1)

    async def test_unknown_nested_setting_is_rejected_without_partial_reload(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(sync_interval=20)
            service_settings["assistant_name"] = "Stable"
            daemon = import_daemon(Path(td), service_settings)
            updated = settings(sync_interval=7)
            updated["assistant_name"] = "Must not publish"
            updated["allowed_groups"] = {"-200": {"purpose": "not a property"}}
            daemon.SETTINGS_FILE.write_text(json.dumps(updated) + "\n")

            result = daemon.reload_runtime_settings()

            self.assertFalse(result["ok"])
            self.assertIn(
                "settings.allowed_groups.-200.purpose: unsupported property",
                result["error"],
            )
            self.assertEqual(daemon.ASSISTANT_NAME, "Stable")
            self.assertEqual(daemon.SYNC_INTERVAL, 20)
            self.assertEqual(daemon.SETTINGS_GENERATION, 1)

    async def test_unsafe_context_overlay_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"context_file": "../../outside.md"},
            }
            with self.assertRaisesRegex(
                    SystemExit, r"settings\.allowed_groups\.-200\.context_file"):
                import_daemon(Path(td), service_settings)

    async def test_reload_refuses_a_connection_change(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            updated = settings()
            updated["connection"] = "some-other-session"
            daemon.SETTINGS_FILE.write_text(json.dumps(updated) + "\n")

            result = daemon.reload_runtime_settings()

            self.assertFalse(result["ok"])
            self.assertIn("restart is required", result["error"])
            self.assertEqual(daemon.SETTINGS["connection"], "test")

    async def test_call_recording_modes_are_read_from_group_policy(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-1001": {
                    "call_recording": {"mode": "auto", "send_to_chat": True},
                },
                "-1002": {"call_recording": {"mode": "on_request"}},
                "-1003": {"call_recording": {"mode": "disabled"}},
            }
            daemon = import_daemon(Path(td), service_settings)

            self.assertEqual(
                daemon.configured_call_recording_groups(),
                {"auto": [-1001], "on_request": [-1002], "send_to_chat": [-1001]},
            )
            # Group watching runs on the daemon's own client: a second
            # update-consuming connection would swallow p2p INCOMING_CALL.
            self.assertIsNone(daemon.call_recorder_command())

    async def test_voice_agent_and_call_recording_are_independent_switches(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_users"] = {
                "1": {"name": "Both",
                      "call_recording": {"mode": "auto"},
                      "voice_agent": {"mode": "auto"}},
                "2": {"name": "Record only", "call_recording": {"mode": "auto"}},
                "3": {"name": "Voice only", "voice_agent": {"mode": "enabled"}},
                "4": {"name": "Neither"},
                "5": {"name": "Voice disabled", "voice_agent": {"mode": "disabled"}},
            }
            daemon = import_daemon(Path(td), service_settings)

            self.assertEqual(
                daemon.configured_call_recording_users()["allowed_callers"], [1, 2])
            self.assertEqual(sorted(daemon.configured_voice_agent_users()), [1, 3])

    async def test_voice_agent_policy_defaults_and_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_users"] = {
                "1": {"name": "Default", "voice_agent": {"mode": "auto"}},
                "2": {"name": "Tuned", "voice_agent": {
                    "mode": "auto", "model": "other-live", "voice": "Puck",
                    "history": 5}},
            }
            daemon = import_daemon(Path(td), service_settings)
            users = daemon.configured_voice_agent_users()

            self.assertEqual(users[1], {
                "name": "Default",
                "model": daemon.voice_agent.DEFAULT_MODEL,
                "voice": daemon.voice_agent.DEFAULT_VOICE,
                # Unset by default: the shipped greeting is the engine's own.
                "greeting": None,
                "history": daemon.voice_agent.DEFAULT_HISTORY_MESSAGES,
                # Every tool off until a project names it: one the model is
                # holding is one it will reach for.
                "tools": {name: False
                          for name in daemon.voice_agent.TOOL_NAMES},
            })
            self.assertEqual(users[2]["model"], "other-live")
            self.assertEqual(users[2]["voice"], "Puck")
            self.assertEqual(users[2]["history"], 5)

    async def test_voice_tools_layer_rather_than_replace(self):
        """A project names the set its calls run on; a caller turns one on or
        off without restating the rest. Anything unnamed stays off."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["defaults"]["voice_agent"] = {
                "tools": {"agent_task": True, "send_to_chat": True}}
            service_settings["allowed_users"] = {
                "1": {"name": "Project set", "voice_agent": {"mode": "auto"}},
                "2": {"name": "One overridden", "voice_agent": {
                    "mode": "auto", "tools": {"send_to_chat": False,
                                              "read_project_file": True}}},
            }
            daemon = import_daemon(Path(td), service_settings)
            users = daemon.configured_voice_agent_users()

            self.assertEqual(users[1]["tools"], {
                "agent_task": True, "send_to_chat": True,
                "run_capability": False, "read_project_file": False,
                "reload_service": False})
            self.assertEqual(users[2]["tools"], {
                "agent_task": True, "send_to_chat": False,
                "run_capability": False, "read_project_file": True,
                "reload_service": False})

    async def test_invalid_voice_history_is_rejected_with_full_json_path(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_users"] = {
                "3": {"voice_agent": {"mode": "auto", "history": "not-a-number"}},
            }
            with self.assertRaisesRegex(
                    SystemExit, r"settings\.allowed_users\.3\.voice_agent\.history"):
                import_daemon(Path(td), service_settings)

    async def test_a_call_runs_under_the_authority_the_callers_messages_resolve(self):
        """The one thing a voice channel must not do: widen what a caller can
        reach. The authority handed to a worker launched from a call is compared
        against the authority the same user's message pipeline actually handed
        its worker — captured from the live path, not restated here."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_users"] = {
                "777": {"name": "Caller", "role": "direct_user"}}
            service_settings["authority"] = {
                "roles": {
                    "supervisor": {"allowed_capabilities": {"*": True}},
                    "direct_user": {"allowed_capabilities": {"routine": True}},
                },
            }
            daemon = import_daemon(Path(td), service_settings)
            seen = {}

            def capture(chat, tail, state, procs):
                seen["authority"] = state["authority"]
                return successful_result("done")

            daemon.WORKERS["stub"] = capture
            message = Message(401)
            client = FakeClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await client.handler(Event(message))
            await wait_until(
                lambda: daemon.load_register()["123"]["last_processed_message_id"] == 401)
            await self.stop_session(client, task)

            from_call = daemon.voice_task_authority(777, "Caller", "check the invoice")
            stranger = daemon.voice_task_authority(999, "Stranger", "check the invoice")

        from_message = seen["authority"]
        self.assertEqual(from_message["sender_role"], "direct_user")
        self.assertEqual(from_call["sender_role"], from_message["sender_role"])
        self.assertEqual(from_call["allowed_capabilities"],
                         from_message["allowed_capabilities"])
        self.assertEqual(from_call["allowed_capabilities"], {"routine": True})
        # No role is assumed for a caller the settings never named.
        self.assertEqual(stranger["sender_role"], "direct_user")
        self.assertEqual(stranger["allowed_capabilities"], {"routine": True})

    async def test_connection_authority_accepts_read_and_write_grants(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            root = Path(td)
            base = {"connection": "test", "assistant_name": "Assistant",
                    "direct_messages": {"mode": "anyone",
                                        "default_role": "direct_user"},
                    "allowed_users": {}, "allowed_groups": {}}

            def validated(connections):
                return daemon.validate_settings({
                    **base,
                    "authority": {"roles": {"supervisor": {
                        "allowed_capabilities": {"telegram": {
                            "connections": connections}}}}},
                }, root, root)

            validated(["personal"])
            validated({"personal": {}})
            validated({"personal": {"allow_write": True}})
            with self.assertRaisesRegex(Exception, "allow_write.*must be a boolean"):
                validated({"personal": {"allow_write": "yes"}})
            with self.assertRaisesRegex(Exception, "unsupported property"):
                validated({"personal": {"write": True}})

    async def test_connection_write_grant_stays_with_the_requesters_role(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["authority"] = {"roles": {
                "supervisor": {"allowed_capabilities": {"telegram": {
                    "connections": {"personal": {"allow_write": True}}}}},
                "group_member": {"allowed_capabilities": {"telegram": {
                    "connections": ["personal"]}}},
            }}
            daemon = import_daemon(Path(td), service_settings)

            supervisor = daemon._authority_policy_for(
                {"sender_role": "supervisor", "sender_id": 1}, None, True)
            member = daemon._authority_policy_for(
                {"sender_role": "group_member", "sender_id": 2}, None, True)

            self.assertEqual(
                supervisor["allowed_capabilities"]["telegram"]["connections"],
                {"personal": {"allow_write": True}})
            self.assertEqual(
                member["allowed_capabilities"]["telegram"]["connections"],
                ["personal"])

    async def test_voice_task_worker_policy_falls_back_to_the_projects_own(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings(
                worker="claude", workers={"claude": {"model": "text-model",
                                                     "effort": "high"}}))
            inherited = daemon.voice_agent_settings()

        self.assertEqual(inherited["worker"], "claude")
        self.assertEqual(inherited["model"], "text-model")
        self.assertEqual(inherited["effort"], "high")
        self.assertEqual(inherited["worker_timeout"], 2)
        # How many tasks a call may run is not part of the settings surface:
        # the bound is fixed at one in code.
        self.assertNotIn("max_parallel_jobs", inherited)

        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings(
                worker="claude",
                workers={"claude": {"model": "text-model", "effort": "high"}},
                voice_agent={"worker": "claude",
                             "workers": {"claude": {"model": "fast-model"}}}))
            tuned = daemon.voice_agent_settings()

        self.assertEqual(tuned["model"], "fast-model")
        self.assertEqual(tuned["effort"], "high")
        self.assertEqual(tuned["worker_timeout"], 2)

    async def test_voice_defaults_resolve_per_user_then_project_then_built_in(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(
                voice_agent={"voice": "ProjectVoice", "history": 120})
            service_settings["allowed_users"] = {
                "1": {"voice_agent": {"mode": "auto", "voice": "UserVoice"}},
                "2": {"voice_agent": {"mode": "auto"}},
            }
            daemon = import_daemon(Path(td), service_settings)
            resolved = daemon.configured_voice_agent_users()

        # The user's own choice wins; otherwise the project's; otherwise built in.
        self.assertEqual(resolved[1]["voice"], "UserVoice")
        self.assertEqual(resolved[2]["voice"], "ProjectVoice")
        self.assertEqual(resolved[1]["history"], 120)
        self.assertEqual(resolved[2]["history"], 120)

        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_users"] = {"2": {"voice_agent": {"mode": "auto"}}}
            daemon = import_daemon(Path(td), service_settings)
            bare = daemon.configured_voice_agent_users()

        self.assertEqual(bare[2]["voice"], daemon.voice_agent.DEFAULT_VOICE)
        self.assertEqual(bare[2]["history"],
                         daemon.voice_agent.DEFAULT_HISTORY_MESSAGES)

    async def test_a_call_states_its_times_in_the_configured_zone(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(voice_agent={"timezone": "Asia/Tokyo"}))
            self.assertEqual(daemon.VOICE_TIMEZONE_NAME, "Asia/Tokyo")

        with tempfile.TemporaryDirectory() as td:
            # Unset means UTC, never whatever zone the host happens to be in.
            daemon = import_daemon(Path(td), settings())
            self.assertEqual(daemon.VOICE_TIMEZONE_NAME, "UTC")
            self.assertEqual(daemon.VOICE_RECORDING_CAPTION, "")
            self.assertEqual(daemon.VOICE_PROGRESS_INTERVAL, 10.0)

    async def test_a_worker_step_is_named_past_the_shell_wrapper(self):
        """Codex wraps every command in `<shell> -lc "…"`, so the first word is
        the shell and says nothing a waiting caller could use."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            def stage(command):
                return daemon.codex_event_stage(json.dumps(
                    {"type": "item.started",
                     "item": {"type": "command_execution", "command": command}}))

            self.assertEqual(stage(["/bin/zsh", "-lc", "rg needle ."]),
                             "searching for it")
            self.assertEqual(stage("/bin/bash -lc 'git status'"),
                             "checking the repository")
            # A capability the project happens to have is recognised without
            # this file ever naming one.
            self.assertEqual(stage("/bin/zsh -lc 'mailbox list --unread'"),
                             "asking mailbox to list")
            self.assertEqual(stage("/bin/zsh -lc 'someunknowntool'"),
                             "running someunknowntool")
            # The worker reporting its own progress is not progress.
            self.assertIsNone(stage("/bin/zsh -lc 'telegram send 42 \"looking\"'"))

            self.assertEqual(
                daemon.codex_event_stage(json.dumps({"type": "thread.started"})),
                "starting")
            self.assertIsNone(daemon.codex_event_stage("not json"))
            self.assertIsNone(daemon.codex_event_stage(json.dumps(
                {"type": "item.started", "item": {"type": "agent_message"}})))

    async def test_a_voice_task_reports_to_the_assistant_not_the_caller(self):
        """Worker progress reaching the caller's chat leaves the agent guessing
        about messages it cannot see, and it starts inventing."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            preamble = daemon.voice_task_preamble(4242, 10)

        self.assertIn("telegram send 4242", preamble)
        self.assertIn("every 10 seconds", preamble)
        self.assertIn("go to the assistant on the call, not to the caller",
                      preamble)
        self.assertIn("do not address them", preamble)
        self.assertIn("The answer you return at the end is the result",
                      preamble)
        # The delivery note the worker is given is about the spoken reply only;
        # everything about progress lives in the preamble and nowhere else.
        self.assertNotIn("progress", daemon.VOICE_TASK_DELIVERY)

    async def test_the_call_is_answered_before_the_chat_tail_is_read(self):
        """Reading the tail first expires the ring window, after which pytgcalls
        tries to *place* a call instead of accepting the offered one. The handler
        only runs on a real call, so the ordering itself is the guard."""
        source = DAEMON_PATH.read_text()
        body = source.split("async def start_voice_call", 1)[1]
        body = body.split("def begin_voice_call", 1)[0]
        order = [body.index(marker) for marker in (
            "await calls.record(",
            "await calls.play(",
            "session.start_pump()",
            "await voice_chat_history(",
            "session.set_system_instruction(",
            "await session.start_agent()",
        )]
        self.assertEqual(order, sorted(order))

    async def test_the_call_summary_does_not_depend_on_a_recording(self):
        """The summary is started before the recording is joined, so it is ready
        to reply to it — and a call with no recording still gets one."""
        source = DAEMON_PATH.read_text()
        body = source.split("async def finish_voice_call", 1)[1]
        body = body.split("@calls.on_update", 1)[0]
        self.assertLess(body.index("generate_call_summary("),
                        body.index("if not record:"))
        self.assertLess(body.index("generate_call_summary("),
                        body.index("join_tracks_to_stereo("))
        # Posted after the recording is delivered, replying to that message.
        flush = source.split("async def flush_call_summary", 1)[1].split("\n\n        async def", 1)[0]
        self.assertIn("force_reply=True", flush)
        self.assertIn('(metadata.get("delivery") or {}).get("message_id")', flush)

    async def test_stream_loss_and_session_close_own_finalisation(self):
        source = DAEMON_PATH.read_text()
        stream_loss = source.split("async def end_call_after_stream_loss", 1)[1]
        stream_loss = stream_loss.split("async def read_voice_project_file", 1)[0]
        self.assertIn("finally:", stream_loss)
        self.assertIn("await flush_call_summary()", stream_loss)

        shutdown = source.split("closing = True", 1)[1]
        shutdown = shutdown.split("cleanup_tasks =", 1)[0]
        self.assertIn("await asyncio.gather(*finalisers", shutdown)
        self.assertIn('await finish_voice_call("session_closing")', shutdown)
        self.assertIn("await flush_call_summary()", shutdown)

    async def test_voice_prompt_file_override_comes_from_settings(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(voice_agent={"prompt_file": "call.md"}))
            self.assertEqual(daemon.VOICE_CONTEXT_FILE.name, "call.md")
            self.assertEqual(daemon.VOICE_CONTEXT_FILE.parent.name, "service")

    async def test_gemini_key_env_name_comes_from_the_connection(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.assertEqual(daemon.GEMINI_SECRET_ENV, "GOOGLE_API_KEY")

        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(),
                connection_extra={"gemini_secret_env": "PROJECT_GEMINI_KEY"},
                voice_context="Speak briefly.\n",
                project_env={"PROJECT_GEMINI_KEY": "live-key",
                             "GOOGLE_API_KEY": "wrong-key"})

            self.assertEqual(daemon.GEMINI_SECRET_ENV, "PROJECT_GEMINI_KEY")
            api_key, voice_context, blocked = daemon.voice_call_readiness()
            self.assertEqual(api_key, "live-key")
            self.assertEqual(voice_context, "Speak briefly.")
            self.assertIsNone(blocked)

    async def test_voice_call_is_blocked_when_the_named_key_is_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(),
                connection_extra={"gemini_secret_env": "PROJECT_GEMINI_KEY"},
                voice_context="Speak briefly.\n")

            api_key, voice_context, blocked = daemon.voice_call_readiness()
            self.assertIsNone(api_key)
            self.assertIsNone(voice_context)
            self.assertIn("PROJECT_GEMINI_KEY", blocked)

    async def test_missing_voice_prompt_blocks_the_call_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(),
                project_env={"GOOGLE_API_KEY": "live-key"})

            self.assertEqual(daemon.read_voice_context(), "")
            api_key, voice_context, blocked = daemon.voice_call_readiness()
            self.assertIsNone(api_key)
            self.assertIsNone(voice_context)
            self.assertIn(str(daemon.VOICE_CONTEXT_FILE), blocked)

    async def test_empty_voice_prompt_is_not_a_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(), voice_context="   \n",
                project_env={"GOOGLE_API_KEY": "live-key"})

            _, _, blocked = daemon.voice_call_readiness()
            self.assertIn(str(daemon.VOICE_CONTEXT_FILE), blocked)

    async def test_call_recording_request_is_explicit_and_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.assertTrue(daemon._is_call_recording_request("/record"))
            self.assertTrue(daemon._is_call_recording_request("Марвин, запиши звонок"))
            self.assertTrue(daemon._is_call_recording_request("Marvin, record the call"))
            self.assertFalse(daemon._is_call_recording_request("Марвин, привет"))

            path = daemon.queue_call_recording_request(
                -1002,
                77,
                {"id": "42", "name": "Test User", "role": "group_member"},
            )
            payload = json.loads(path.read_text())
            self.assertEqual(payload["chat_id"], "-1002")
            self.assertEqual(payload["message_id"], 77)
            self.assertEqual(payload["requested_by"]["user_id"], "42")

    async def test_record_slash_command_addresses_assistant_in_on_request_group(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            message = SimpleNamespace(
                raw_text="/record",
                text="/record",
                message="/record",
                mentioned=False,
                is_reply=False,
            )
            policy = {"call_recording": {"mode": "on_request"}}
            self.assertTrue(await daemon._message_addresses_me(
                message,
                SimpleNamespace(username="assistant", id=42),
                policy,
            ))

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

    async def test_long_final_reply_is_delivered_in_telegram_sized_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            reply = "A" * (daemon.TELEGRAM_MESSAGE_LIMIT + 1)
            daemon.WORKERS["stub"] = lambda *_args: successful_result(reply)
            message = Message(324)
            client = FakeClient([message])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            await client.handler(Event(message))
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 324)

            self.assertEqual([item["text"] for item in client.sent], [
                "A" * daemon.TELEGRAM_MESSAGE_LIMIT,
                "A",
            ])
            self.assertTrue(all(len(item["text"]) <= daemon.TELEGRAM_MESSAGE_LIMIT
                                for item in client.sent))
            await self.stop_session(client, task)

    async def test_periodic_sync_recovers_message_missed_by_live_updates(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td),
                settings(sync_interval=0.02, sync_stale_after=0.2),
            )
            daemon.save_register({"123": {"last_processed_message_id": 320}})
            message = Message(323, voice=True)
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append(message.id) or "recovered"

            first = FakeClient([])
            first_task = asyncio.create_task(daemon.run_session(first))
            await first.started.wait()
            first.messages.append(message)
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 323)
            self.assertEqual(transcriptions, [323])
            self.assertEqual(first.send_attempts, 2)
            self.assertGreaterEqual(first.get_messages_calls, 2)
            self.assertGreaterEqual(first.catch_up_calls, 1)
            health = json.loads(daemon.HEALTH_FILE.read_text())
            self.assertEqual(health["state"], "healthy")
            self.assertEqual(health["last_catch_up_reason"], "periodic")
            self.assertIsNotNone(health["last_sync_at"])
            await self.stop_session(first, first_task)

            second = FakeClient([message])
            second_task = asyncio.create_task(daemon.run_session(second))
            await second.started.wait()
            await asyncio.sleep(0.05)
            self.assertEqual(transcriptions, [323])
            self.assertEqual(second.send_attempts, 0)
            await self.stop_session(second, second_task)

            source = DAEMON_PATH.read_text()
            self.assertIn('catch_up_known("periodic")', source)

    async def test_periodic_tl_decode_failure_marks_health_and_reconnects(self):
        class TypeNotFoundError(Exception):
            pass

        class BrokenClient(FakeClient):
            async def get_messages(self, chat, limit=None):
                self.get_messages_calls += 1
                if self.get_messages_calls > 1:
                    raise TypeNotFoundError("matching Constructor ID")
                return await super().get_messages(chat, limit=limit)

        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td),
                settings(sync_interval=0.02, sync_stale_after=0.2),
            )
            daemon.save_register({"123": {"last_processed_message_id": 320}})
            client = BrokenClient([])
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            with self.assertRaises(daemon.SessionUnhealthy):
                await asyncio.wait_for(task, timeout=3)
            health = json.loads(daemon.HEALTH_FILE.read_text())
            self.assertEqual(health["state"], "unhealthy")
            self.assertIn("TL decode failed", health["last_error"])

    async def test_telethon_compatibility_version_is_pinned_across_bundle(self):
        expected = '"telethon==1.43.2"'
        for path in (
            next((candidate for candidate in (
                TELEGRAM_DIR / "bin" / "telegram", TELEGRAM_DIR / "telegram")
                if candidate.is_file()), TELEGRAM_DIR / "bin" / "telegram"),
            TELEGRAM_DIR / "service" / "daemon.py",
            TELEGRAM_DIR / "service" / "call_recorder.py",
        ):
            self.assertIn(expected, path.read_text(), path)

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

    async def test_ambient_voice_disabled_by_default_ignores_unaddressed_voice(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {}}
            daemon = import_daemon(Path(td), service_settings)

            message = Message(400, voice=True)
            message.sender_id = 888
            message.mentioned = False
            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "ambient speech"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await asyncio.sleep(0.05)

            self.assertEqual(len(transcriptions), 0)
            self.assertEqual(client.send_attempts, 0)
            self.assertEqual(daemon.load_register().get("-200", {}), {})
            await self.stop_session(client, task)

    async def test_ambient_voice_auto_transcribes_unaddressed_voice_no_worker(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(401, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Ambient", last_name="User", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "ambient speech"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 401)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(message.downloads, 1)
            self.assertEqual(client.send_attempts, 1)
            self.assertEqual(len(client.sent), 1)
            sent_text = client.sent[0]["text"]
            self.assertNotIn("сказал:", sent_text)
            self.assertNotIn("Ambient User", sent_text)
            self.assertIn("ambient speech", sent_text)
            self.assertIn("<blockquote>", sent_text)
            reg = daemon.load_register()
            self.assertEqual(reg["-200"]["jobs"], {})
            await self.stop_session(client, task)

    async def test_ambient_video_note_transcript_dispatches_through_voice_path(self):
        """A video note naming the assistant becomes the worker's text request."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(4011, video_note=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Ambient", last_name="Video", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            requests = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "Assistant, handle this video note"
            daemon.WORKERS["stub"] = lambda _chat, _tail, state, _procs: (
                requests.append(state["current_request"]["text"])
                or successful_result())
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 4011)

            self.assertEqual(transcriptions, [(b"voice", "video/mp4")])
            self.assertEqual(message.downloads, 1)
            self.assertEqual(client.send_attempts, 2)
            self.assertIn("Assistant, handle this video note", client.sent[0]["text"])
            self.assertEqual(requests, ["Assistant, handle this video note"])
            self.assertEqual(daemon.load_register()["-200"]["jobs"], {})
            await self.stop_session(client, task)

    async def test_addressed_voice_in_group_uses_sender_attributed_format(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {}}
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(402, voice=True)
            message.sender_id = 888
            message.mentioned = True

            async def fake_get_sender():
                return SimpleNamespace(first_name="Addressed", last_name="User", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "addressed speech"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 402)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(client.send_attempts, 2)
            echo = client.sent[0]["text"]
            self.assertNotIn("сказал:", echo)
            self.assertNotIn("Addressed User", echo)
            self.assertIn("addressed speech", echo)
            self.assertIn("<blockquote>", echo)
            reg = daemon.load_register()
            self.assertEqual(len(reg["-200"]["jobs"]), 0)
            await self.stop_session(client, task)

    async def test_direct_voice_keeps_current_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            message = Message(403, voice=True)
            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "direct speech"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            await client.handler(Event(message))
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 403)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(client.send_attempts, 2)
            echo = client.sent[0]["text"]
            self.assertIn("Твоё сообщение:", echo)
            self.assertIn("direct speech", echo)
            reg = daemon.load_register()
            self.assertEqual(len(reg["123"]["jobs"]), 0)
            await self.stop_session(client, task)

    async def test_ambient_voice_concurrent_duplicates_transcribe_once(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(405, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Concurrent", last_name="Test", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "concurrent speech"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False

            await asyncio.gather(client.handler(event), client.handler(event))
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 405)

            self.assertEqual(len(transcriptions), 1, "Should transcribe exactly once")
            self.assertEqual(message.downloads, 1, "Should download exactly once")
            self.assertEqual(client.send_attempts, 1, "Should echo exactly once")
            self.assertEqual(len(client.sent), 1, "Should send exactly one message")
            sent_text = client.sent[0]["text"]
            self.assertNotIn("сказал:", sent_text)
            self.assertNotIn("Concurrent Test", sent_text)
            self.assertIn("concurrent speech", sent_text)
            self.assertIn("<blockquote>", sent_text)
            reg = daemon.load_register()
            self.assertEqual(reg["-200"]["jobs"], {}, "Ambient voice should not leave jobs")
            await self.stop_session(client, task)

    async def test_ambient_voice_transcription_failure_uses_sender_attributed_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(406, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Failed", last_name="Transcription", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])

            def failing_transcribe(audio, mime):
                raise RuntimeError("Deepgram API failure")
            daemon.deepgram_transcribe = failing_transcribe

            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 406)

            self.assertEqual(client.send_attempts, 1)
            self.assertEqual(len(client.sent), 1)
            sent_text = client.sent[0]["text"]
            self.assertNotIn("сказал:", sent_text)
            self.assertNotIn("Failed Transcription", sent_text)
            self.assertIn("[голосовое — не удалось расшифровать]", sent_text)
            self.assertIn("<blockquote>", sent_text)
            reg = daemon.load_register()
            self.assertEqual(reg["-200"]["jobs"], {})
            await self.stop_session(client, task)

    async def test_ambient_video_note_catch_up_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(404, video_note=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Catch", last_name="Up", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append(message.id) or "catch up speech"
            first = FakeClient([message])
            first_task = asyncio.create_task(daemon.run_session(first))
            await first.started.wait()
            await asyncio.sleep(0.05)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(transcriptions, [404])
            self.assertEqual(first.send_attempts, 1)
            await self.stop_session(first, first_task)

            second = FakeClient([message])
            second_task = asyncio.create_task(daemon.run_session(second))
            await second.started.wait()
            await asyncio.sleep(0.05)

            self.assertEqual(transcriptions, [404])
            self.assertEqual(second.send_attempts, 0)
            await self.stop_session(second, second_task)

    async def test_set_voice_transcription_auto_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {}}
            daemon = import_daemon(Path(td), service_settings)
            reg = daemon.load_register()

            result = daemon.set_channel_setting(reg, "-200", "voice-transcription", "auto")
            self.assertIn("auto", result)
            daemon.save_register(reg)
            reg = daemon.load_register()
            self.assertEqual(reg["-200"]["settings"]["voice_transcription"], "auto")

            status = daemon._status(reg, "-200")
            self.assertIn("voice-transcription = auto", status)

            result = daemon.set_channel_setting(reg, "-200", "voice-transcription", "disabled")
            self.assertIn("disabled", result)
            daemon.save_register(reg)
            reg = daemon.load_register()
            self.assertEqual(reg["-200"]["settings"]["voice_transcription"], "disabled")

            status = daemon._status(reg, "-200")
            self.assertIn("voice-transcription = disabled", status)

    async def test_set_voice_transcription_aliases(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {}}
            daemon = import_daemon(Path(td), service_settings)
            reg = daemon.load_register()

            daemon.set_channel_setting(reg, "-200", "voice-transcription", "on")
            self.assertEqual(reg["-200"]["settings"]["voice_transcription"], "auto")

            daemon.set_channel_setting(reg, "-200", "voice-transcription", "off")
            self.assertEqual(reg["-200"]["settings"]["voice_transcription"], "disabled")

    async def test_set_voice_transcription_only_in_groups(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            reg = daemon.load_register()

            with self.assertRaises(ValueError) as ctx:
                daemon.set_channel_setting(reg, "123", "voice-transcription", "auto")
            self.assertIn("only available in groups", str(ctx.exception))

    async def test_set_voice_transcription_help(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {}}
            daemon = import_daemon(Path(td), service_settings)
            reg = daemon.load_register()

            help_text = daemon._set_help(reg, "-200", "voice-transcription")
            self.assertIn("auto|disabled", help_text)
            self.assertIn("current:", help_text)

            help_text = daemon._set_help(reg, "123", "voice-transcription")
            self.assertIn("only available in groups", help_text)

    async def test_group_worker_timeout_and_runtime_override(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings(worker_timeout=120)
            service_settings["allowed_groups"] = {
                "-200": {"worker_timeout": 600}
            }
            daemon = import_daemon(Path(td), service_settings)
            reg = daemon.load_register()

            self.assertEqual(daemon.channel_settings(reg, "-200")["worker_timeout"], 600)

            result = daemon.set_channel_setting(reg, "-200", "worker-timeout", "300")
            self.assertEqual(result, "worker-timeout = 300s")
            self.assertEqual(daemon.channel_settings(reg, "-200")["worker_timeout"], 300)
            self.assertIn("worker-timeout = 300s", daemon._status(reg, "-200"))

            result = daemon.set_channel_setting(reg, "-200", "worker-timeout", "default")
            self.assertEqual(result, "worker-timeout = default (600s effective)")
            self.assertEqual(daemon.channel_settings(reg, "-200")["worker_timeout"], 600)
            self.assertNotIn("worker_timeout", reg["-200"]["settings"])

    async def test_set_worker_timeout_validation_and_help(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            reg = daemon.load_register()

            help_text = daemon._set_help(reg, "123", "worker-timeout")
            self.assertIn("1..3600|default", help_text)
            with self.assertRaisesRegex(ValueError, "1..3600"):
                daemon.set_channel_setting(reg, "123", "timeout", "0")

    async def test_ambient_voice_transcript_without_alias_echo_only(self):
        """Ambient voice with transcript that doesn't name assistant => echo only, no worker."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(501, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Speaker", last_name="One", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "just some regular speech without addressing anyone"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 501)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(message.downloads, 1)
            self.assertEqual(client.send_attempts, 1, "Should send exactly one echo")
            sent_text = client.sent[0]["text"]
            self.assertNotIn("сказал:", sent_text)
            self.assertNotIn("Speaker One", sent_text)
            self.assertIn("just some regular speech", sent_text)
            self.assertIn("<blockquote>", sent_text)
            reg = daemon.load_register()
            self.assertEqual(reg["-200"]["jobs"], {}, "Should not create worker job")
            await self.stop_session(client, task)

    async def test_ambient_voice_transcript_with_english_alias_dispatches_worker(self):
        """Ambient voice with transcript naming 'Assistant' => echo + worker dispatch."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(502, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Speaker", last_name="Two", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "Hey Assistant, what's the weather like?"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 502)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(message.downloads, 1)
            self.assertEqual(client.send_attempts, 2, "Should send echo + worker response")
            echo = client.sent[0]["text"]
            self.assertNotIn("сказал:", echo)
            self.assertNotIn("Speaker Two", echo)
            self.assertIn("Hey Assistant", echo)
            self.assertIn("<blockquote>", echo)
            reg = daemon.load_register()
            self.assertEqual(len(reg["-200"]["jobs"]), 0, "Worker job should complete")
            await self.stop_session(client, task)

    async def test_ambient_voice_transcript_with_russian_alias_dispatches_worker(self):
        """Ambient voice with transcript naming 'Марвин' => echo + worker dispatch."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {
                    "voice_transcription": {"mode": "auto"},
                    "aliases": ["Марвин"]
                }
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(503, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Speaker", last_name="Three", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "Марвин, покажи мне погоду"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 503)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(message.downloads, 1)
            self.assertEqual(client.send_attempts, 2, "Should send echo + worker response")
            echo = client.sent[0]["text"]
            self.assertNotIn("сказал:", echo)
            self.assertNotIn("Speaker Three", echo)
            self.assertIn("Марвин, покажи мне погоду", echo)
            self.assertIn("<blockquote>", echo)
            reg = daemon.load_register()
            self.assertEqual(len(reg["-200"]["jobs"]), 0, "Worker job should complete")
            await self.stop_session(client, task)

    async def test_ambient_voice_custom_group_alias_honored(self):
        """Custom per-group alias is checked in transcript."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {
                    "voice_transcription": {"mode": "auto"},
                    "aliases": ["CustomBot"]
                }
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(504, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Speaker", last_name="Four", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "CustomBot, help me with this task"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 504)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(client.send_attempts, 2, "Should send echo + worker response")
            reg = daemon.load_register()
            self.assertEqual(len(reg["-200"]["jobs"]), 0, "Worker job should complete")
            await self.stop_session(client, task)

    async def test_ambient_voice_concurrent_duplicates_with_alias_dispatch_once(self):
        """Concurrent delivery of ambient voice with spoken alias => one download/echo/worker."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(505, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Speaker", last_name="Five", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "Assistant, run the tests"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False

            await asyncio.gather(client.handler(event), client.handler(event))
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 505)

            self.assertEqual(len(transcriptions), 1, "Should transcribe exactly once")
            self.assertEqual(message.downloads, 1, "Should download exactly once")
            self.assertEqual(client.send_attempts, 2, "Should send echo + worker response exactly once")
            reg = daemon.load_register()
            self.assertEqual(len(reg["-200"]["jobs"]), 0, "Worker job should complete")
            await self.stop_session(client, task)

    async def test_ambient_voice_catchup_with_alias_dispatches_once(self):
        """Catch-up with ambient voice containing spoken alias dispatches worker exactly once."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(506, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Speaker", last_name="Six", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append(message.id) or "Assistant, tell me a joke"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()
            await asyncio.sleep(0.05)

            self.assertEqual(len(transcriptions), 1)
            self.assertEqual(transcriptions, [506])
            self.assertEqual(message.downloads, 1)
            self.assertEqual(client.send_attempts, 2, "Should send echo + worker response")
            reg = daemon.load_register()
            self.assertEqual(len(reg["-200"]["jobs"]), 0, "Worker job should complete")
            await self.stop_session(client, task)

    async def test_ambient_voice_failed_transcription_never_dispatches(self):
        """Failed transcription produces fallback marker and does not dispatch worker."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(507, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Speaker", last_name="Seven", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])

            def failing_transcribe(audio, mime):
                raise RuntimeError("Deepgram failure")
            daemon.deepgram_transcribe = failing_transcribe

            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 507)

            self.assertEqual(client.send_attempts, 1, "Should send only echo with fallback")
            sent_text = client.sent[0]["text"]
            self.assertIn("[голосовое — не удалось расшифровать]", sent_text)
            reg = daemon.load_register()
            self.assertEqual(reg["-200"]["jobs"], {}, "Failed transcription should not dispatch worker")
            await self.stop_session(client, task)

    async def test_ambient_voice_disabled_group_no_transcription(self):
        """Group with disabled transcription does not transcribe or check for spoken alias."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "disabled"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(508, voice=True)
            message.sender_id = 888
            message.mentioned = False

            async def fake_get_sender():
                return SimpleNamespace(first_name="Speaker", last_name="Eight", username=None)
            message.get_sender = fake_get_sender

            client = FakeClient([message])
            transcriptions = []
            daemon.deepgram_transcribe = lambda audio, mime: transcriptions.append((audio, mime)) or "Assistant, this should be ignored"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await asyncio.sleep(0.05)  # Let handler complete

            self.assertEqual(len(transcriptions), 0, "Should not transcribe when disabled")
            self.assertEqual(message.downloads, 0, "Should not download when disabled")
            self.assertEqual(client.send_attempts, 0, "Should not send anything")
            reg = daemon.load_register()
            self.assertEqual(reg["-200"].get("last_processed_message_id", 0), 0, "Should not process")
            await self.stop_session(client, task)

    async def test_voice_echo_attributed_to_sender_in_worker_history(self):
        """Voice echo attribution via explicit register mapping, not content heuristics."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            # Voice message from Alice
            voice_msg = Message(601, voice=True)
            voice_msg.sender_id = 999
            voice_msg.mentioned = False

            async def fake_get_voice_sender():
                return SimpleNamespace(first_name="Alice", last_name="Smith", username=None)
            voice_msg.get_sender = fake_get_voice_sender

            # Later text message from Bob, addressed to assistant
            text_msg = Message(602, text="Assistant, summarize what Alice said")
            text_msg.sender_id = 888
            text_msg.mentioned = True

            async def fake_get_text_sender():
                return SimpleNamespace(first_name="Bob", last_name="Jones", username=None)
            text_msg.get_sender = fake_get_text_sender

            client = FakeClient([voice_msg, text_msg])
            daemon.deepgram_transcribe = lambda audio, mime: "this is what I wanted to say"

            # Capture the tail passed to the worker
            captured_tail = []
            def capture_worker(chat, tail, state=None, procs=None):
                captured_tail.append(tail)
                return {"reply": "summary here", "meta": {"harness": "stub", "model": None,
                                                          "is_error": False, "tokens": {},
                                                          "cost_usd": None, "duration_ms": None,
                                                          "session_id": None}}
            daemon.WORKERS["stub"] = capture_worker

            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            # Send voice message
            event = Event(voice_msg, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 601)

            # Verify echo ID is recorded in register
            reg = daemon.load_register()
            echo_senders = reg["-200"].get("voice_echo_senders", {})
            self.assertEqual(len(echo_senders), 1, "Echo ID should be recorded")
            # The echo will have ID 1000 (first sent message)
            self.assertEqual(echo_senders.get("1000"), "Alice Smith")

            # Send addressed text message
            event = Event(text_msg, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 602)

            # Verify worker was called and captured the tail
            self.assertEqual(len(captured_tail), 1, "Worker should be called for addressed message")
            tail = captured_tail[0]

            # Find the voice echo in the tail
            echo_entry = None
            for entry in tail:
                if "this is what I wanted to say" in entry["text"]:
                    echo_entry = entry
                    break

            self.assertIsNotNone(echo_entry, "Voice echo should appear in conversation history")
            self.assertEqual(echo_entry["sender"], "Alice Smith",
                             "Voice echo must be attributed to Alice via register mapping")

            # Verify the echo message itself has no visible sender prefix
            echo_msg = client.sent[0]["text"]
            self.assertNotIn("Alice", echo_msg, "Echo should not have visible sender name")
            self.assertNotIn("сказал:", echo_msg, "Echo should not have 'сказал:' prefix")

            await self.stop_session(client, task)

    async def test_voice_echo_attribution_survives_restart(self):
        """Echo attribution mapping persists across daemon save/load cycles."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {"voice_transcription": {"mode": "auto"}}}
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            voice_msg = Message(701, voice=True)
            voice_msg.sender_id = 999
            async def fake_get_sender():
                return SimpleNamespace(first_name="Alice", last_name="", username=None)
            voice_msg.get_sender = fake_get_sender

            client = FakeClient([voice_msg])
            daemon.deepgram_transcribe = lambda audio, mime: "restart test"
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(voice_msg, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 701)
            await self.stop_session(client, task)

            # Simulate restart: reload register from disk
            saved_reg = daemon.load_register()
            self.assertIn("voice_echo_senders", saved_reg["-200"])
            self.assertEqual(saved_reg["-200"]["voice_echo_senders"]["1000"], "Alice")

            # Verify persistence: clear in-memory state and reload from disk
            daemon.reg = {}  # Clear in-memory register
            loaded_reg = daemon.load_register()
            self.assertEqual(loaded_reg["-200"]["voice_echo_senders"]["1000"], "Alice",
                             "Attribution mapping must survive reload from disk")

    async def test_normal_assistant_reply_remains_assistant_attributed(self):
        """Ordinary assistant replies, even with blockquote formatting, stay attributed to Assistant."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {}}
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            # Voice message that will be addressed and get a normal reply
            voice_msg = Message(702, voice=True)
            voice_msg.sender_id = 999
            voice_msg.mentioned = True
            async def fake_get_sender():
                return SimpleNamespace(first_name="Alice", last_name="", username=None)
            voice_msg.get_sender = fake_get_sender

            # Follow-up text request
            text_msg = Message(703, text="Assistant, what did I say?")
            text_msg.sender_id = 888
            text_msg.mentioned = True
            async def fake_get_text_sender():
                return SimpleNamespace(first_name="Bob", last_name="", username=None)
            text_msg.get_sender = fake_get_text_sender

            client = FakeClient([voice_msg, text_msg])
            daemon.deepgram_transcribe = lambda audio, mime: "voice content"

            captured_tail = []
            def capture_worker(chat, tail, state=None, procs=None):
                captured_tail.append(tail)
                # Return a reply with blockquote formatting
                return {"reply": "You said:\n<blockquote>voice content</blockquote>",
                        "meta": {"harness": "stub", "model": None, "is_error": False,
                                 "tokens": {}, "cost_usd": None, "duration_ms": None, "session_id": None}}
            daemon.WORKERS["stub"] = capture_worker

            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            # Process voice (addressed, so it dispatches worker)
            event = Event(voice_msg, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: len(captured_tail) >= 1)

            # Process text request
            event = Event(text_msg, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: len(captured_tail) >= 2 and daemon.load_register()["-200"]["last_processed_message_id"] == 703)

            # Second worker call should see:
            # - Echo (1000) attributed to Alice
            # - Normal assistant reply (1001) attributed to Assistant (even though it has blockquote)
            self.assertGreaterEqual(len(captured_tail), 2)
            tail = captured_tail[1]

            assistant_reply = next((e for e in tail if "You said" in e["text"]), None)
            self.assertIsNotNone(assistant_reply, "Assistant's reply should be in tail")
            self.assertEqual(assistant_reply["sender"], "Assistant",
                             "Normal assistant reply must stay attributed to Assistant, not falsely to Alice")

            await self.stop_session(client, task)

    async def test_multi_chunk_voice_echo_attribution(self):
        """Long transcripts split into multiple messages are all attributed correctly."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {"voice_transcription": {"mode": "auto"}}}
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            voice_msg = Message(704, voice=True)
            voice_msg.sender_id = 999
            async def fake_get_sender():
                return SimpleNamespace(first_name="Verbose", last_name="Speaker", username=None)
            voice_msg.get_sender = fake_get_sender

            # Long transcript that will be chunked
            long_transcript = "word " * 2000  # Exceeds TELEGRAM_MESSAGE_LIMIT

            client = FakeClient([voice_msg])
            daemon.deepgram_transcribe = lambda audio, mime: long_transcript
            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(voice_msg, chat_id=-200)
            event.is_private = False
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 704)

            # Multiple chunks should have been sent
            self.assertGreater(len(client.sent), 1, "Long transcript should be chunked")

            # All chunk IDs should be recorded
            reg = daemon.load_register()
            echo_senders = reg["-200"]["voice_echo_senders"]
            for sent in client.sent:
                msg_id = str(sent["id"])
                self.assertIn(msg_id, echo_senders,
                              f"Chunk {msg_id} must be recorded in echo mapping")
                self.assertEqual(echo_senders[msg_id], "Verbose Speaker")

            await self.stop_session(client, task)

    async def test_voice_echo_sender_mapping_is_bounded(self):
        """Echo sender mapping is pruned to prevent unbounded growth."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["defaults"]["tail_size"] = 5  # Small tail for testing
            daemon = import_daemon(Path(td), service_settings)

            # Directly test the helper functions without going through the full daemon
            reg = {"-200": {"last_processed_message_id": 0}}
            key = "-200"

            # Record 15 echo IDs with monotonically increasing message IDs
            for i in range(15):
                echo_id = 1000 + i
                sender_name = f"Speaker{i}"
                daemon._record_voice_echo_sender(reg, key, [echo_id], sender_name)

            # Verify mapping is pruned to exactly the limit (2 × tail_size = 10)
            echo_senders = reg["-200"]["voice_echo_senders"]
            self.assertEqual(len(echo_senders), 10,
                             "Mapping must be pruned to exactly 2 × tail_size")

            # Verify oldest IDs (1000-1004) are removed and newest IDs (1005-1014) remain
            for i in range(5):  # First 5 should be pruned
                self.assertNotIn(str(1000 + i), echo_senders,
                                 f"Oldest echo ID {1000 + i} should be pruned")
            for i in range(5, 15):  # Last 10 should remain
                self.assertIn(str(1000 + i), echo_senders,
                              f"Recent echo ID {1000 + i} should remain after pruning")
                self.assertEqual(echo_senders[str(1000 + i)], f"Speaker{i}",
                                 f"Echo {1000 + i} should map to correct sender")

    async def test_normal_reply_delivery_preserves_message_id_in_job_metadata(self):
        """Normal text replies record their Telegram message ID in job.reply_message_id."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            daemon = import_daemon(Path(td), service_settings)

            message = Message(901, text="Assistant, hello")
            message.mentioned = True
            client = FakeClient([message])

            # Worker returns a normal text reply
            def stub_worker(chat, tail, state=None, procs=None):
                return {"reply": "Hello there!", "meta": {"harness": "stub", "model": None,
                                                          "is_error": False, "tokens": {},
                                                          "cost_usd": None, "duration_ms": None,
                                                          "session_id": None}}
            daemon.WORKERS["stub"] = stub_worker

            task = asyncio.create_task(daemon.run_session(client))
            await client.started.wait()

            event = Event(message)
            await client.handler(event)
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 901)

            # Verify the reply was sent (DM doesn't have voice echo, just the reply)
            self.assertEqual(len(client.sent), 1, "Should send reply")

            # Verify the reply has a valid Telegram message ID
            reply_msg = client.sent[0]
            self.assertIsNotNone(reply_msg.get("id"), "Reply message must have an ID")
            self.assertGreaterEqual(reply_msg["id"], 1000, "Reply ID should be from FakeClient sequence")

            await self.stop_session(client, task)

    async def test_direct_voice_and_addressed_voice_unchanged(self):
        """Direct message voice and already-addressed group voice keep existing behavior."""
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {}}
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            # Direct message voice
            dm_message = Message(509, voice=True)
            dm_client = FakeClient([dm_message])
            daemon.deepgram_transcribe = lambda audio, mime: "direct voice message"
            dm_task = asyncio.create_task(daemon.run_session(dm_client))
            await dm_client.started.wait()

            await dm_client.handler(Event(dm_message))
            await wait_until(lambda: daemon.load_register()["123"]["last_processed_message_id"] == 509)

            self.assertEqual(dm_client.send_attempts, 2, "Direct voice: echo + worker")
            echo = dm_client.sent[0]["text"]
            self.assertIn("Твоё сообщение:", echo)
            self.assertIn("direct voice message", echo)
            await self.stop_session(dm_client, dm_task)

            # Addressed group voice (mentioned=True)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})
            addressed_message = Message(510, voice=True)
            addressed_message.sender_id = 888
            addressed_message.mentioned = True

            async def fake_get_sender():
                return SimpleNamespace(first_name="Addressed", last_name="Speaker", username=None)
            addressed_message.get_sender = fake_get_sender

            group_client = FakeClient([addressed_message])
            daemon.deepgram_transcribe = lambda audio, mime: "addressed group voice"
            group_task = asyncio.create_task(daemon.run_session(group_client))
            await group_client.started.wait()

            event = Event(addressed_message, chat_id=-200)
            event.is_private = False
            await group_client.handler(event)
            await wait_until(lambda: daemon.load_register()["-200"]["last_processed_message_id"] == 510)

            self.assertEqual(group_client.send_attempts, 2, "Addressed group voice: echo + worker")
            echo = group_client.sent[0]["text"]
            self.assertNotIn("сказал:", echo)
            self.assertNotIn("Addressed Speaker", echo)
            self.assertIn("addressed group voice", echo)
            self.assertIn("<blockquote>", echo)
            await self.stop_session(group_client, group_task)


class TestCodexImageDiscovery(unittest.IsolatedAsyncioTestCase):
    """Test Codex image artifact discovery and path validation."""

    async def test_discover_codex_images_finds_valid_images_in_thread_directory(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            
            # Create fake Codex image directory structure
            codex_home = Path(td) / "fake-home" / ".codex" / "generated_images"
            thread_id = "019ed6d8-test-thread-id"
            thread_dir = codex_home / thread_id
            thread_dir.mkdir(parents=True)
            
            # Create test images
            (thread_dir / "image1.png").write_bytes(b"fake-png-data")
            (thread_dir / "image2.jpg").write_bytes(b"fake-jpg-data")
            (thread_dir / "image3.gif").write_bytes(b"fake-gif-data")
            (thread_dir / "ignored.txt").write_text("not an image")
            (thread_dir / "empty.png").write_bytes(b"")  # Should be skipped
            
            # Monkey-patch Path.home() for this test
            original_home = Path.home
            Path.home = lambda: Path(td) / "fake-home"
            try:
                images = daemon.discover_codex_images(thread_id)
                self.assertEqual(len(images), 3)
                names = {img.name for img in images}
                self.assertEqual(names, {"image1.png", "image2.jpg", "image3.gif"})
            finally:
                Path.home = original_home

    async def test_discover_codex_images_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            
            # Create structure outside the valid generated_images tree
            codex_home = Path(td) / "fake-home" / ".codex" / "generated_images"
            codex_home.mkdir(parents=True)
            outside = Path(td) / "outside"
            outside.mkdir()
            (outside / "malicious.png").write_bytes(b"data")
            
            original_home = Path.home
            Path.home = lambda: Path(td) / "fake-home"
            try:
                # Thread ID that would escape the safe directory
                images = daemon.discover_codex_images("../../outside")
                self.assertEqual(images, [])
            finally:
                Path.home = original_home

    async def test_discover_codex_images_caps_at_telegram_album_limit(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            
            codex_home = Path(td) / "fake-home" / ".codex" / "generated_images"
            thread_id = "test-many-images"
            thread_dir = codex_home / thread_id
            thread_dir.mkdir(parents=True)
            
            # Create 15 images
            for i in range(15):
                (thread_dir / f"img{i:02d}.png").write_bytes(b"data")
            
            original_home = Path.home
            Path.home = lambda: Path(td) / "fake-home"
            try:
                images = daemon.discover_codex_images(thread_id)
                self.assertEqual(len(images), 10)  # Telegram album limit
            finally:
                Path.home = original_home

    async def test_discover_codex_images_returns_empty_for_missing_thread(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            
            codex_home = Path(td) / "fake-home" / ".codex" / "generated_images"
            codex_home.mkdir(parents=True)
            
            original_home = Path.home
            Path.home = lambda: Path(td) / "fake-home"
            try:
                images = daemon.discover_codex_images("nonexistent-thread")
                self.assertEqual(images, [])
                
                images = daemon.discover_codex_images(None)
                self.assertEqual(images, [])
                
                images = daemon.discover_codex_images("")
                self.assertEqual(images, [])
            finally:
                Path.home = original_home


class VoiceCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """The capability read a call answers from: who may run what, what comes
    back, and what happens when it does not come back in time."""

    def daemon(self, td):
        return import_daemon(Path(td), settings())

    async def test_authority_decides_which_capabilities_a_call_reaches(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            everything = {"allowed_capabilities": {"*": True}}
            self.assertTrue(daemon.capability_allowed(everything, "simplbooks"))

            named = {"allowed_capabilities": {"clickup": True,
                                              "simplbooks": {"allow": False},
                                              "telegram": {"scope": "current_chat"}}}
            self.assertTrue(daemon.capability_allowed(named, "clickup"))
            # A scoped rule still allows the capability; the scope is the CLI's
            # own business, not a reason to refuse the call.
            self.assertTrue(daemon.capability_allowed(named, "telegram"))
            self.assertFalse(daemon.capability_allowed(named, "simplbooks"))
            # Absent is refused, not allowed: a capability nobody granted is one
            # nobody thought about.
            self.assertFalse(daemon.capability_allowed(named, "coolify"))

            # No authority declared at all leaves the CLI's own gate as the only
            # one — the same answer the worker path gives.
            self.assertTrue(daemon.capability_allowed(None, "simplbooks"))

    async def test_only_help_stands_in_for_help(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            helped = set()

            # A real command at a tool nobody has read is answered with what the
            # tool takes, rather than run on a guessed flag.
            self.assertEqual(
                daemon.voice_capability_step(["list", "--unread"], "mailbox", helped),
                "prime")

            # The other contract verbs pass through — asking what a tool is, is
            # the behaviour worth having — but none of them says how it is
            # called, so the primer is still owed afterwards.
            for verb in ("guide", "refs", "ids", "connections", "manifest"):
                self.assertEqual(
                    daemon.voice_capability_step([verb], "telegram", helped),
                    "contract", verb)
            self.assertEqual(
                daemon.voice_capability_step(["search", "-100"], "telegram", helped),
                "prime")

            # Help is what the primer is made of, so asking for it settles the
            # debt and the next command runs.
            self.assertEqual(
                daemon.voice_capability_step(["help"], "coolify", helped), "help")
            helped.add("coolify")
            self.assertEqual(
                daemon.voice_capability_step(["logs", "abc"], "coolify", helped), "run")

            # No arguments at all is a real call, not a contract verb.
            self.assertEqual(
                daemon.voice_capability_step([], "fathom", helped), "prime")

    async def test_output_is_bounded_and_says_where_it_was_cut(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            body, cut = daemon.truncate_capability_output("short", 100)
            self.assertEqual(body, "short")
            self.assertFalse(cut)

            body, cut = daemon.truncate_capability_output("x" * 500, 100)
            self.assertTrue(cut)
            self.assertTrue(body.endswith("…[cut]"))
            self.assertEqual(body[:100], "x" * 100)

    async def test_a_command_that_outlives_the_call_is_abandoned(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            started = time.monotonic()
            code, out, err = await daemon.run_capability_process(
                sys.executable, ["-c", "import time; time.sleep(30)"],
                dict(os.environ), timeout=0.5)
            # Nothing to report and nothing left running: the caller is told to
            # hand the work to a worker instead.
            self.assertIsNone(code)
            self.assertIsNone(out)
            self.assertLess(time.monotonic() - started, 10)

    async def test_a_command_that_answers_returns_its_output_and_code(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            code, out, _ = await daemon.run_capability_process(
                sys.executable, ["-c", "print('ok')"], dict(os.environ), timeout=5)
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "ok")

            code, _, err = await daemon.run_capability_process(
                sys.executable, ["-c", "import sys; sys.stderr.write('no'); sys.exit(4)"],
                dict(os.environ), timeout=5)
            self.assertEqual(code, 4)
            self.assertIn("no", err)

    async def test_command_output_is_bounded_while_the_pipe_is_drained(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            code, out, err = await daemon.run_capability_process(
                sys.executable,
                ["-c", "import sys; print('x' * 100000); "
                 "sys.stderr.write('y' * 100000)"],
                dict(os.environ), timeout=5)
            self.assertEqual(code, 0)
            self.assertEqual(len(out), daemon.voice_agent.CAPABILITY_OUTPUT_LIMIT + 1)
            self.assertEqual(len(err), 4096)

    async def test_authority_context_is_unique_and_owner_only(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = self.daemon(td)
            first = Path(daemon.write_authority_context(
                {"allowed_capabilities": {"routine": True}}, "caller"))
            second = Path(daemon.write_authority_context(
                {"allowed_capabilities": {"routine": True}}, "caller"))
            try:
                self.assertNotEqual(first, second)
                self.assertEqual(first.stat().st_mode & 0o777, 0o600)
                self.assertEqual(second.stat().st_mode & 0o777, 0o600)
            finally:
                first.unlink(missing_ok=True)
                second.unlink(missing_ok=True)


class VoiceProjectFileTests(unittest.IsolatedAsyncioTestCase):
    """What a call may open. The project holds credentials, sessions and bank
    material, so this is the boundary that matters most in the call path."""

    async def test_only_the_named_roots_can_be_opened(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            path, refusal = daemon.resolve_project_file(
                "capabilities/simplbooks/reference/vat-regimes.md")
            self.assertIsNone(refusal)
            self.assertTrue(str(path).endswith("vat-regimes.md"))

            for allowed in ("context/identity/SOUL.md", "routines/second-opinion.md",
                            "assets/sessions/note.md", "deployment/runtime.json"):
                _, refusal = daemon.resolve_project_file(allowed)
                self.assertIsNone(refusal, allowed)

            # Anything not named is refused, including the repository root itself.
            for outside in ("README.md", ".git/config", ".claude/settings.json",
                            "src/main.py"):
                _, refusal = daemon.resolve_project_file(outside)
                self.assertEqual(refusal, "not_readable_here", outside)

    async def test_a_path_that_climbs_out_is_judged_by_where_it_lands(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            for escape in ("../../etc/passwd",
                           "context/../../etc/passwd",
                           "/etc/passwd",
                           "capabilities/../../../.ssh/id_rsa"):
                _, refusal = daemon.resolve_project_file(escape)
                self.assertIn(refusal, ("outside_project", "not_readable_here"), escape)

    async def test_an_absolute_path_inside_the_project_names_the_same_file(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            root = Path(td) / "project"
            rel = "capabilities/simplbooks/reference/vat-regimes.md"

            # `refs` hands out absolute paths and the project body lists relative
            # ones; both name one file and both have to open it.
            by_relative, refusal = daemon.resolve_project_file(rel)
            self.assertIsNone(refusal)
            by_absolute, refusal = daemon.resolve_project_file(str(root / rel))
            self.assertIsNone(refusal)
            self.assertEqual(by_relative, by_absolute)

    async def test_nested_resolved_layer_is_readable_without_root_conventions(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            root = Path(td) / "project"
            nested = root / "agent" / "context"
            note = nested / "identity.md"
            note.parent.mkdir(parents=True)
            note.write_text("nested\n")
            daemon.PROJECT_LAYOUT["context"] = str(nested)

            path, refusal = daemon.resolve_project_file("agent/context/identity.md")

            self.assertIsNone(refusal)
            self.assertEqual(path, note.resolve())

    async def test_symlink_escape_from_resolved_layer_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            root = Path(td) / "project"
            context = root / "context"
            outside = Path(td) / "outside.txt"
            context.mkdir(parents=True)
            outside.write_text("secret\n")
            (context / "escape.md").symlink_to(outside)

            _, refusal = daemon.resolve_project_file("context/escape.md")

            self.assertEqual(refusal, "outside_project")

    async def test_secrets_are_refused_inside_the_allowed_roots(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            for secret in ("capabilities/telegram/.env",
                           "capabilities/telegram/.env.local",
                           "capabilities/telegram/.env/backup.txt",
                           "capabilities/simplbooks/connections.json",
                           "capabilities/simplbooks/state/session.json",
                           "capabilities/telegram/credentials/token.txt",
                           "capabilities/telegram/secrets/api.txt",
                           "capabilities/telegram/service/jess.session",
                           "deployment/deploy.key",
                           "capabilities/automations/runs.sqlite"):
                _, refusal = daemon.resolve_project_file(secret)
                self.assertEqual(refusal, "not_readable_here", secret)

    async def test_an_empty_path_is_answered_rather_than_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path, refusal = daemon.resolve_project_file("   ")
            self.assertIsNone(path)
            self.assertEqual(refusal, "no_path")

    async def test_a_readable_file_comes_back_as_its_contents(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            root = Path(td) / "project"
            note = root / "context" / "identity" / "COMPANY.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("# Company\n\nVAT number: EE101369226\n")

            result = daemon.read_project_file_result("context/identity/COMPANY.md")
            self.assertTrue(result["ok"], result)
            self.assertIn("EE101369226", result["text"])
            self.assertFalse(result["truncated"])
            self.assertEqual(result["path"], "context/identity/COMPANY.md")

    async def test_a_long_file_is_bounded_and_says_so(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            root = Path(td) / "project"
            big = root / "context" / "long.md"
            big.parent.mkdir(parents=True, exist_ok=True)
            big.write_text("x" * 60000)

            result = daemon.read_project_file_result("context/long.md")
            self.assertTrue(result["ok"])
            self.assertTrue(result["truncated"])
            self.assertTrue(result["text"].endswith("…[cut]"))
            self.assertIn("agent_task", result["instruction"])

    async def test_a_file_that_is_not_there_is_said_to_be_missing(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            result = daemon.read_project_file_result("context/nothing-here.md")
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "not_found")


class VoicePromptIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_daemon_call_path_does_not_read_project_context(self):
        source = DAEMON_PATH.read_text()
        call_path = source.split("async def start_voice_call", 1)[1]
        call_path = call_path.split("async def finish_voice_call", 1)[0]

        self.assertNotIn("project_context", call_path)
        self.assertNotIn("compiled_context", source)


class WorkerResultParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_answer_survives_hook_documents_on_both_sides(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            answer = {"result": "finished", "usage": {}, "session_id": "abc"}
            stdout = "\n".join((
                json.dumps({"hook": "session-start"}),
                json.dumps(answer),
                json.dumps({"metrics": {"fire_index": 7}}),
            ))

            with mock.patch.object(
                    daemon, "run_worker_proc", return_value=(0, stdout, "")):
                result = daemon.worker_claude("123", [], {}, {})

            self.assertEqual(result["reply"], "finished")
            self.assertTrue(any("foreign output" in line
                                for line in daemon._test_logs))

    async def test_hook_output_without_an_answer_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            with self.assertRaisesRegex(RuntimeError, r"no answer document.*metrics"):
                daemon._first_json_document(
                    json.dumps({"metrics": {"fire_index": 7}}),
                    ("result", "is_error", "subtype"))

    async def test_usage_summary_names_every_class_and_keeps_unknown_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            self.assertEqual(
                daemon._usage_summary({
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 7,
                }),
                "in=2 out=3 cache_r=5 cache_w=7")
            self.assertEqual(
                daemon._usage_summary({"input": 2, "output": 3}),
                "in=2 out=3 cache_r=? cache_w=?")

    async def test_claude_error_logs_the_usage_that_was_still_billed(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            stdout = json.dumps({
                "result": "", "is_error": True, "subtype": "failed",
                "usage": {"input_tokens": 2, "output_tokens": 3,
                          "cache_read_input_tokens": 5,
                          "cache_creation_input_tokens": 7},
            })

            with mock.patch.object(
                    daemon, "run_worker_proc", return_value=(0, stdout, "")):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    daemon.worker_claude("123", [], {}, {})

            self.assertTrue(any("in=2 out=3 cache_r=5 cache_w=7" in line
                                for line in daemon._test_logs))

    async def test_previous_usage_in_the_prompt_includes_cache_classes(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            prompt = daemon.build_prompt([], {"prev_usage": {
                "input": 2, "output": 3, "cache_read": 5, "cache_write": 7}})

            self.assertIn("in=2 out=3 cache_r=5 cache_w=7", prompt)


class ProgressEchoTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_final_reply_that_repeats_progress_is_suppressed(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            self.assertTrue(daemon._progress_already_delivered(
                [daemon._normalize_delivered("Answer is ready.\nDone.")],
                "Answer is ready. Done."))
            self.assertFalse(daemon._progress_already_delivered(
                [daemon._normalize_delivered("Checking the logs")],
                "The service was restarted."))
            long_progress = "A" * 180
            self.assertTrue(daemon._progress_already_delivered(
                [long_progress], long_progress + " Additional detail."))


class WorkerSessionContinuityTests(unittest.IsolatedAsyncioTestCase):
    """A call keeps the worker session its last finished task ran in, so the
    next task builds on what that one found instead of rediscovering it."""

    def captured_codex(self, daemon, state, stdout=None):
        """Run the codex worker against a recorded command line."""
        seen = {}
        stdout = stdout or "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ])

        def record(proc_key, cmd, procs, **kwargs):
            seen["cmd"] = cmd
            return (0, stdout, "")

        with mock.patch.object(daemon, "run_worker_proc", side_effect=record):
            result = daemon.worker_codex("123", [], state, {})
        return seen["cmd"], result

    async def test_a_resumed_run_continues_the_thread_without_color(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            cmd, _ = self.captured_codex(daemon, {"resume_session": "thread-1"})

            self.assertEqual(cmd[:4], ["codex", "exec", "resume", "thread-1"])
            # `resume` has no --color at all: passing it exits before the model
            # is ever reached, and the run would look like a session that could
            # not be opened.
            self.assertNotIn("--color", cmd)
            for flag in ("--json", "-o", "--skip-git-repo-check",
                         "--dangerously-bypass-approvals-and-sandbox"):
                self.assertIn(flag, cmd)

    async def test_a_fresh_run_is_left_exactly_as_it_was(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            cmd, _ = self.captured_codex(daemon, {})

            self.assertEqual(cmd[:2], ["codex", "exec"])
            self.assertNotIn("resume", cmd)
            self.assertEqual(cmd[cmd.index("--color") + 1], "never")

    async def test_claude_is_told_which_session_to_resume(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            seen = {}

            def record(proc_key, cmd, procs, **kwargs):
                seen["cmd"] = cmd
                return (0, json.dumps({"result": "done", "usage": {},
                                       "session_id": "abc"}), "")

            with mock.patch.object(daemon, "run_worker_proc", side_effect=record):
                daemon.worker_claude("123", [], {"resume_session": "abc"}, {})

            self.assertEqual(seen["cmd"][seen["cmd"].index("--resume") + 1], "abc")

    async def test_a_resumed_prompt_carries_the_request_and_nothing_settled(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            state = {"resume_session": "thread-1", "now": "Monday",
                     "chat_id": "123", "harness": "codex",
                     "settings": {"worker": "codex"},
                     "participants": [{"name": "Owner", "role": "owner"}],
                     "current_request": {"text": "and now file it",
                                         "delivery": "spoken"}}
            prompt = daemon.build_prompt([], state)

            self.assertIn("and now file it", prompt)
            self.assertIn("Monday", prompt)
            # The thread already holds all of this; restating it every turn is
            # what resuming was meant to stop paying for.
            self.assertNotIn("Channel state", prompt)
            self.assertNotIn("Owner", prompt)
            self.assertNotIn("Conversation", prompt)

    async def test_only_a_thread_start_event_yields_a_thread_id(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            self.assertEqual(daemon.codex_thread_started(json.dumps(
                {"type": "thread.started", "thread_id": "thread-9"})), "thread-9")
            self.assertIsNone(daemon.codex_thread_started(json.dumps(
                {"type": "turn.completed"})))
            self.assertIsNone(daemon.codex_thread_started("not json at all"))

    async def test_a_lane_adopts_a_session_and_counts_its_turns(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            lane = daemon.WorkerLane()

            self.assertIsNone(lane.session_id)
            lane.pin("thread-1")
            self.assertEqual(lane.session_id, "thread-1")
            lane.pin("thread-1")
            self.assertEqual(lane.turns, 2)
            # A turn that never finished reports nothing, and nothing is adopted.
            lane.pin(None)
            self.assertEqual(lane.turns, 2)
            self.assertEqual(len(lane.sessions), 1)

    async def test_a_session_outlives_the_call_that_opened_it(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            lane = daemon.load_lane(777)
            self.assertIsNone(lane.session_id)
            lane.pin("thread-1")
            lane.pin("thread-1")
            daemon.save_lane(777, lane)

            # A later call — a later daemon, even — picks it back up.
            carried = daemon.load_lane(777)
            self.assertEqual(carried.session_id, "thread-1")
            self.assertEqual(carried.turns, 2)
            # Seeded as the call's first row, so the metadata reads continuously.
            self.assertEqual(carried.sessions[0]["session_id"], "thread-1")
            # Somebody else's calls are their own.
            self.assertIsNone(daemon.load_lane(999).session_id)

    async def test_a_session_pinned_after_the_next_call_started_is_still_picked_up(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            # The call is answered with nothing on record yet.
            lane = daemon.load_lane(777)
            self.assertIsNone(lane.session_id)

            # The previous call's task finishes a moment later and pins its own.
            straggler = daemon.WorkerLane()
            straggler.pin("thread-1")
            straggler.pin("thread-1")
            daemon.save_lane(777, straggler)

            # This call's next task reads the file again and carries on there,
            # instead of opening a thread that knows nothing.
            lane.refresh(daemon._read_lanes().get("777"))
            self.assertEqual(lane.session_id, "thread-1")
            self.assertEqual(lane.turns, 2)
            self.assertEqual(lane.sessions[-1]["session_id"], "thread-1")

    async def test_a_session_is_on_record_from_the_moment_it_opens(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            lane = daemon.load_lane(777)

            # The worker has only just reported opening the thread; the work
            # itself will take another half minute.
            lane.open("thread-1")
            daemon.save_lane(777, lane)

            # A call arriving inside that window already finds it.
            self.assertEqual(daemon.load_lane(777).session_id, "thread-1")
            self.assertEqual(lane.turns, 0)

            # Completing the turn counts it without opening anything new.
            lane.pin("thread-1")
            self.assertEqual(lane.turns, 1)
            self.assertEqual(len(lane.sessions), 1)

            # A turn that fails instead takes the record back down with it.
            lane.clear("task_failed")
            daemon.save_lane(777, lane)
            self.assertIsNone(daemon.load_lane(777).session_id)

    async def test_a_lost_session_stops_being_carried(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            lane = daemon.load_lane(777)
            lane.pin("thread-1")
            daemon.save_lane(777, lane)
            lane.clear("session_lost")
            daemon.save_lane(777, lane)

            self.assertIsNone(daemon.load_lane(777).session_id)

    async def test_reload_is_the_one_way_to_a_new_session(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            lane = daemon.load_lane(777)
            lane.pin("thread-1")
            daemon.save_lane(777, lane)

            self.assertEqual(daemon.forget_lanes(), 1)
            self.assertIsNone(daemon.load_lane(777).session_id)
            # Nothing carried is not an error, it is just nothing to drop.
            self.assertEqual(daemon.forget_lanes(), 0)

    async def test_a_compacted_thread_restates_its_context_next_turn(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            self.assertTrue(daemon.codex_context_compacted(json.dumps(
                {"type": "item.completed",
                 "item": {"type": "context_compaction"}})))
            self.assertFalse(daemon.codex_context_compacted(json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message"}})))

            # Re-anchoring survives the call it happened in, so a thread that
            # summarized itself at hangup still restates itself when rung back.
            lane = daemon.load_lane(777)
            lane.pin("thread-1")
            lane.needs_reanchor = True
            daemon.save_lane(777, lane)
            self.assertTrue(daemon.load_lane(777).needs_reanchor)

            state = {"resume_session": "thread-1", "resume_reanchor": True,
                     "now": "Monday", "chat_id": "123", "harness": "codex",
                     "settings": {"worker": "codex"},
                     "participants": [{"name": "Owner", "role": "owner"}],
                     "current_request": {"text": "carry on", "delivery": "spoken"}}
            self.assertIn("Channel state", daemon.build_prompt([], state))

    async def test_only_a_resume_that_opened_nothing_may_be_run_again(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            lane = daemon.WorkerLane()

            self.assertEqual(
                lane.classify("codex", resumed=True, thread_started=False),
                "session_lost")
            # The thread opened, so the task may already have done half of what
            # it was asked. Running it again would do that half twice.
            self.assertEqual(
                lane.classify("codex", resumed=True, thread_started=True),
                "task_failed")
            self.assertEqual(
                lane.classify("codex", resumed=False, thread_started=False),
                "task_failed")
            # Claude reports no thread event ever, so it can never be told apart.
            self.assertEqual(
                lane.classify("claude", resumed=True, thread_started=False),
                "task_failed")

    async def test_nothing_but_a_failure_ever_ends_a_session(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            lane = daemon.WorkerLane()

            # No ceiling, no clock, no generation to drift out of: a hundred
            # turns later it is still the same thread. Codex summarizes it for
            # itself when the context window fills.
            for _ in range(100):
                lane.pin("thread-1")

            self.assertEqual(lane.session_id, "thread-1")
            self.assertEqual(lane.turns, 100)
            self.assertEqual(len(lane.sessions), 1)

    async def test_each_session_a_call_used_is_kept_with_its_ending(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            lane = daemon.WorkerLane()

            lane.pin("thread-1")
            lane.clear("session_lost")
            lane.pin("thread-2")
            lane.clear("call_ended")

            self.assertEqual(
                [(row["session_id"], row["turns"], row["closed"])
                 for row in lane.sessions],
                [("thread-1", 1, "session_lost"), ("thread-2", 1, "call_ended")])

    async def test_the_schema_admits_only_the_two_session_modes(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            base = {"connection": "test", "assistant_name": "Assistant",
                    "direct_messages": {"mode": "anyone",
                                        "default_role": "direct_user"},
                    "allowed_users": {}, "allowed_groups": {}}
            root = Path(td)

            def validated(session):
                return daemon.validate_settings(
                    {**base, "defaults": {"voice_agent": {"session": session}}},
                    root, root)

            validated({"mode": "carry"})
            validated({"mode": "fresh"})
            with self.assertRaisesRegex(Exception, "must be one of"):
                validated({"mode": "sometimes"})
            with self.assertRaisesRegex(Exception, "unsupported property"):
                validated({"lifetime": 5})

    async def test_fresh_mode_is_the_way_back_to_one_session_per_task(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.assertEqual(daemon.voice_agent_settings()["session_mode"],
                             "carry")

        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td),
                settings(voice_agent={"session": {"mode": "fresh"}}))
            self.assertEqual(daemon.voice_agent_settings()["session_mode"],
                             "fresh")


class ChatProjectRoutingTests(unittest.IsolatedAsyncioTestCase):
    """A chat, a forum topic or a direct sender can name the project its worker
    runs in. The daemon does not resolve that project; it stops asserting its
    own, because every capability CLI reads CLAUDE_PROJECT_DIR before cwd."""

    def target(self, td, name="projectB", marker=".git"):
        """A route target inside the test home, carrying a project marker."""
        root = Path(td) / "home" / name
        root.mkdir(parents=True)
        (root / marker).mkdir()
        return root

    def routed_settings(self, target, **groups):
        service_settings = settings()
        service_settings["allowed_groups"] = groups
        return service_settings

    async def test_the_schema_accepts_a_route_on_every_tier(self):
        with tempfile.TemporaryDirectory() as td:
            target = self.target(td)
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"project": str(target),
                         "topics": {"7": {"project": str(target),
                                          "context": "topic prose"}}}}
            service_settings["allowed_users"] = {"42": {"project": str(target)}}

            daemon = import_daemon(Path(td), service_settings)

            self.assertEqual(daemon.ALLOWED_GROUPS["-200"]["project"], str(target))
            self.assertEqual(daemon.ALLOWED["42"]["project"], str(target))

    async def test_a_route_is_refused_at_load_by_its_shape(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            base = {"connection": "test", "assistant_name": "Assistant",
                    "direct_messages": {"mode": "anyone",
                                        "default_role": "direct_user"},
                    "allowed_users": {}, "allowed_groups": {}}
            root = Path(td)

            def validated(project):
                with mock.patch.dict(os.environ, {"HOME": str(root)}):
                    return daemon.validate_settings(
                        {**base, "allowed_groups": {"-200": {"project": project}}},
                        root, root)

            validated(str(root / "somewhere"))
            with self.assertRaisesRegex(Exception, "must be an absolute path"):
                validated("../elsewhere")
            with self.assertRaisesRegex(Exception, "must be a path inside"):
                validated("/opt/elsewhere")

    async def test_a_topic_entry_carries_a_route_and_prose_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            root = Path(td)
            base = {"connection": "test", "assistant_name": "Assistant",
                    "direct_messages": {"mode": "anyone",
                                        "default_role": "direct_user"},
                    "allowed_users": {}, "allowed_groups": {}}

            def validated(topics):
                with mock.patch.dict(os.environ, {"HOME": str(root)}):
                    return daemon.validate_settings(
                        {**base, "allowed_groups": {"-200": {"topics": topics}}},
                        root, root)

            validated({"7": {"project": str(root / "p"), "context": "prose"}})
            with self.assertRaisesRegex(Exception, "unsupported property"):
                validated({"7": {"authority": {}}})
            with self.assertRaisesRegex(Exception, "positive forum topic ID"):
                validated({"0": {"context": "prose"}})

    async def test_route_resolution_reads_topic_then_chat_then_direct_sender(self):
        with tempfile.TemporaryDirectory() as td:
            room = self.target(td, "room")
            lane = self.target(td, "lane")
            mine = self.target(td, "mine")
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"project": str(room),
                         "topics": {"7": {"project": str(lane)}}}}
            service_settings["allowed_users"] = {"42": {"project": str(mine)}}
            daemon = import_daemon(Path(td), service_settings)
            _, group_policy = daemon._group_policy(-200)

            topic_job = {"chat_id": -200, "topic_id": 7, "sender_id": 42}
            chat_job = {"chat_id": -200, "topic_id": 9, "sender_id": 42}
            direct_job = {"chat_id": 42, "sender_id": 42}

            self.assertEqual(daemon._route_for(topic_job, group_policy, False)[0],
                             str(lane))
            self.assertEqual(daemon._route_for(chat_job, group_policy, False)[0],
                             str(room))
            self.assertEqual(daemon._route_for(direct_job, None, True)[0],
                             str(mine))
            self.assertEqual(daemon._route_for({"chat_id": -1}, None, False),
                             (None, None))

    async def test_a_route_target_is_judged_at_dispatch_not_at_load(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            good = self.target(td)
            bare = Path(td) / "home" / "bare"
            bare.mkdir(parents=True)

            # Resolved before it is judged, so a link is judged by where it
            # lands rather than by how it was spelled.
            self.assertEqual(daemon._route_target(str(good), "label"),
                             good.resolve())
            with self.assertRaisesRegex(daemon.RouteUnavailable, "does not exist"):
                daemon._route_target(str(Path(td) / "home" / "gone"), "label")
            with self.assertRaisesRegex(daemon.RouteUnavailable, "no project marker"):
                daemon._route_target(str(bare), "label")

    async def test_a_routed_worker_is_told_where_it_is_and_loses_the_pin(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            target = self.target(td)
            pinned = {
                "CAPABILITIES_PROJECT_ENVELOPE": "/home/project/capabilities",
                "TELEGRAM_SERVICE_PROJECT_ROOT": "/home/project",
                "TELEGRAM_SERVICE_PROJECT_ENVELOPE": "/home/project/capabilities",
                "TELEGRAM_SERVICE_PROJECT_LAYOUT": "{}",
            }

            with mock.patch.dict(os.environ, pinned):
                routed = daemon.worker_env({"project_dir": target})
                home = daemon.worker_env({"project_dir": daemon.PROJECT_ROOT})

            self.assertEqual(routed["CLAUDE_PROJECT_DIR"], str(target))
            for name in pinned:
                self.assertNotIn(name, routed)
            # The home route is the row that results when nothing matched, and
            # it must stay exactly as the launcher handed it over.
            self.assertEqual(home["CAPABILITIES_PROJECT_ENVELOPE"],
                             pinned["CAPABILITIES_PROJECT_ENVELOPE"])
            self.assertNotIn("CLAUDE_PROJECT_DIR", home)

    async def test_a_worker_never_inherits_the_nonce_or_an_agent_channel(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            leaked = {
                "TELEGRAM_SERVICE_LAUNCH_NONCE": "ownership-proof",
                "SSH_AUTH_SOCK": "/private/tmp/ssh",
                "CLAUDE_CODE_MESSAGING_TOKEN": "token",
                "CLAUDECODE": "1",
                "VSCODE_IPC_HOOK": "/private/tmp/vscode",
            }

            with mock.patch.dict(os.environ, leaked):
                env = daemon.worker_env({})

            for name in leaked:
                self.assertNotIn(name, env)

    async def test_topic_prose_follows_room_prose(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"context": "room prose",
                         "topics": {"7": {"context": "topic prose"}}}}
            daemon = import_daemon(Path(td), service_settings)
            _, group_policy = daemon._group_policy(-200)

            context, exclusive = daemon._channel_context(
                [group_policy, daemon._topic_policy(group_policy, 7)])

            self.assertEqual(context, "room prose\n\ntopic prose")
            self.assertFalse(exclusive)
            self.assertEqual(
                daemon._channel_context(
                    [group_policy, daemon._topic_policy(group_policy, 9)]),
                ("room prose", False))

    async def test_a_direct_sender_receives_the_prose_written_for_them(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_users"] = {"42": {"context": "direct prose"}}
            daemon = import_daemon(Path(td), service_settings)

            self.assertEqual(
                daemon._channel_context([daemon.ALLOWED.get("42"), {}]),
                ("direct prose", False))

    async def test_the_route_map_names_every_configured_scope(self):
        with tempfile.TemporaryDirectory() as td:
            room = self.target(td, "room")
            lane = self.target(td, "lane")
            mine = self.target(td, "mine")
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"project": str(room),
                         "topics": {"7": {"project": str(lane)}}},
                "-300": True}
            service_settings["allowed_users"] = {"42": {"project": str(mine)}}
            daemon = import_daemon(Path(td), service_settings)

            self.assertEqual(daemon._route_map(), [
                {"scope": "group -200", "project": str(room)},
                {"scope": "group -200 topic 7", "project": str(lane)},
                {"scope": "direct 42", "project": str(mine)},
            ])


class ChannelPromptTests(unittest.IsolatedAsyncioTestCase):
    """A room and a topic each own their prose. A level that declares itself
    exclusive answers with that prose alone, and the daemon-resolved state it
    needs to answer at all is never part of the bargain."""

    def group(self, **policy):
        service_settings = settings()
        service_settings["allowed_groups"] = {"-200": policy}
        return service_settings

    async def test_a_topic_that_claims_its_lane_drops_the_room(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), self.group(
                context="room prose",
                topics={"7": {"context": "lane prose", "context_mode": "exclusive"},
                        "9": {"context": "quiet lane"}}))
            _, policy = daemon._group_policy(-200)

            claimed = daemon._channel_context(
                [policy, daemon._topic_policy(policy, 7)])
            inherited = daemon._channel_context(
                [policy, daemon._topic_policy(policy, 9)])

            self.assertEqual(claimed, ("lane prose", True))
            self.assertEqual(inherited, ("room prose\n\nquiet lane", False))

    async def test_an_existing_legacy_context_path_is_not_reported_missing(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            topic = daemon.SERVICE_DIR / "topics" / "hq-briefings.md"
            topic.parent.mkdir(parents=True, exist_ok=True)
            topic.write_text("briefing prose\n")

            context = daemon._channel_context_from_policy(
                {"context_file": "topics/hq-briefings.md"})

            self.assertEqual(context, "briefing prose")
            self.assertNotIn("missing", context)

    async def test_an_exclusive_room_still_lets_its_topics_speak(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), self.group(
                context="room prose", context_mode="exclusive",
                topics={"7": {"context": "lane prose"}}))
            _, policy = daemon._group_policy(-200)

            # Exclusivity cuts what is above the level that declared it, never
            # what is below: the room drops the service context, the topic still
            # adds its own line.
            self.assertEqual(
                daemon._channel_context([policy, daemon._topic_policy(policy, 7)]),
                ("room prose\n\nlane prose", True))

    async def test_the_shipped_jobs_prompt_is_served_when_a_project_has_none(self):
        """A project inherits the wording until it decides to own it."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            prompt = daemon.build_prompt(
                [], {"chat_id": 5, "jobs_available": True})
            self.assertIn("--- Registered jobs ---", prompt)
            self.assertIn("the choice is yours", prompt)
            self.assertIn("jobs submit", prompt)

    async def test_a_project_jobs_prompt_replaces_the_shipped_one(self):
        """The wording is prose a project iterates on without a release, so its
        own file is the prompt rather than an addition to it."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(),
                jobs_prompt="ask me about work with {{TELEGRAM_JOBS_COMMAND}} active\n")
            prompt = daemon.build_prompt(
                [], {"chat_id": 5, "jobs_available": True})
            self.assertIn("ask me about work with", prompt)
            self.assertIn("jobs active", prompt)
            self.assertNotIn("the choice is yours", prompt)

    async def test_a_channel_without_delegation_is_told_nothing_about_jobs(self):
        """A channel reaches the register only where something says so: silence
        offers no verbs, and nothing about work it cannot start."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.assertFalse(daemon._delegation_allowed(None))
            self.assertFalse(daemon._delegation_allowed(
                {"delegation": {"mode": "disabled"}}))
            self.assertTrue(daemon._delegation_allowed(
                {"delegation": {"mode": "allowed"}}))
            prompt = daemon.build_prompt(
                [], {"chat_id": 5, "jobs_available": False})
            self.assertNotIn("--- Registered jobs ---", prompt)
            self.assertNotIn("jobs submit", prompt)

    async def test_a_channel_default_can_open_delegation_for_every_room(self):
        """`defaults` opens every channel at once, and a room closes itself
        again under it — the nearer setting is the one that decides."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(delegation={"mode": "allowed"}))
            self.assertTrue(daemon._delegation_allowed(None))
            self.assertFalse(daemon._delegation_allowed(
                {"delegation": {"mode": "disabled"}}))

    async def test_a_channel_default_can_close_delegation_for_every_room(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(delegation={"mode": "disabled"}))
            self.assertFalse(daemon._delegation_allowed(None))

    async def test_an_exclusive_prompt_answers_without_the_service_context(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(
                Path(td), settings(), worker_context="project worker prose\n")
            state = {"chat_id": 5, "channel_context": "lane prose"}

            extend = daemon.build_prompt([], state)
            exclusive = daemon.build_prompt([], {**state, "context_exclusive": True})

            self.assertIn("test context", extend)
            self.assertIn("--- Project worker context ---", extend)
            self.assertIn("project worker prose", extend)
            self.assertLess(extend.index("test context"),
                            extend.index("project worker prose"))
            self.assertLess(extend.index("project worker prose"),
                            extend.index("lane prose"))
            self.assertNotIn("test context", exclusive)
            self.assertNotIn("project worker prose", exclusive)
            self.assertIn("lane prose", exclusive)

    async def test_the_progress_channel_survives_an_exclusive_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            command = f'{daemon.WORKER_BIN / "telegram"} send 5'

            for state in ({"chat_id": 5},
                          {"chat_id": 5, "context_exclusive": True,
                           "channel_context": "lane prose"}):
                # The command names the channel this request answers on, so it
                # is a fact the daemon resolves rather than prose a channel can
                # forget to carry.
                self.assertIn(command, daemon.build_prompt([], state))

    async def test_prose_naming_the_progress_command_is_rewritten_wherever_it_sits(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            shim = str(daemon.WORKER_BIN / "telegram")

            prompt = daemon.build_prompt([], {
                "chat_id": 5, "context_exclusive": True,
                "channel_context": "Report with telegram send <chat_id> <text> first."})

            self.assertIn(f"Report with {shim} send 5", prompt)
            self.assertNotIn("<chat_id>", prompt)

    async def test_the_schema_admits_only_the_two_context_modes(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            root = Path(td)
            base = {"connection": "test", "assistant_name": "Assistant",
                    "direct_messages": {"mode": "anyone",
                                        "default_role": "direct_user"},
                    "allowed_users": {}, "allowed_groups": {}}

            def validated(policy):
                return daemon.validate_settings(
                    {**base, "allowed_groups": {"-200": policy}}, root, root)

            validated({"context_mode": "extend"})
            validated({"topics": {"7": {"context_mode": "exclusive"}}})
            with self.assertRaisesRegex(Exception, "must be one of"):
                validated({"context_mode": "only"})
            with self.assertRaisesRegex(Exception, "must be one of"):
                validated({"topics": {"7": {"context_mode": "only"}}})


class GeneralTopicTests(unittest.IsolatedAsyncioTestCase):
    """Telegram lists General as topic 1, but marks nothing on the wire. A chat
    that named its rooms gets General among them; a chat that named none keeps
    it where it has always been."""

    def message(self, chat_id):
        return SimpleNamespace(id=500, chat_id=chat_id, reply_to=None)

    async def test_general_is_addressable_once_a_chat_names_its_rooms(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"topics": {"1": {"context": "general prose"}}},
                "-300": {"name": "no map"}}
            daemon = import_daemon(Path(td), service_settings)

            self.assertEqual(
                daemon._message_topic_id(self.message(-200)), 1)
            # Without a map General stays on the bare chat key, which is where a
            # plain group's messages belong and where a reply chain must not
            # become a topic of its own.
            self.assertIsNone(daemon._message_topic_id(self.message(-300)))

    async def test_a_real_topic_still_wins_over_the_general_synthesis(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"topics": {"1": {}, "7": {}}}}
            daemon = import_daemon(Path(td), service_settings)
            inside = SimpleNamespace(
                id=500, chat_id=-200, forum_topic=True,
                reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=7))

            self.assertEqual(daemon._message_topic_id(inside), 7)

    async def test_generals_watermark_follows_it_onto_the_topic_key(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {"-200": {"topics": {"1": {}}}}
            daemon = import_daemon(Path(td), service_settings)
            reg = {"-200": {"last_processed_message_id": 4242}}

            self.assertTrue(daemon.migrate_register(reg))

            general = daemon._channel_key(-200, 1)
            self.assertEqual(reg[general]["last_processed_message_id"], 4242)
            self.assertNotIn("-200", reg)

    async def test_a_chat_without_a_map_keeps_its_register_row(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            reg = {"-200": {"last_processed_message_id": 4242}}

            daemon.migrate_register(reg)

            self.assertEqual(reg["-200"]["last_processed_message_id"], 4242)


class AgentMarkerTests(unittest.IsolatedAsyncioTestCase):
    """Two daemons share a room. A request tagged #external says its answer is
    consumed by a live session, and the daemon — not the model — stamps
    #noreply on everything it sends for that request."""

    def sender(self, text):
        return SimpleNamespace(id=500, chat_id=-200, sender_id=777, text=text,
                               raw_text=text, message=text, mentioned=False,
                               reply_to=None)

    async def policy(self, td, **group):
        service_settings = settings()
        service_settings["allowed_groups"] = {"-200": group or {"name": "room"}}
        return import_daemon(Path(td), service_settings)

    async def test_a_tagged_answer_is_never_an_invitation(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = await self.policy(td)
            _, policy = daemon._group_policy(-200)
            me = SimpleNamespace(id=1, username="assistant", first_name="Assistant")

            named = self.sender("Assistant, here is what I found")
            tagged = self.sender("Assistant, here is what I found\n\n#noreply")

            self.assertTrue(await daemon._message_addresses_me(named, me, policy))
            self.assertFalse(await daemon._message_addresses_me(tagged, me, policy))

    async def test_the_tag_counts_only_as_the_last_line_alone(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = await self.policy(td)
            _, policy = daemon._group_policy(-200)
            me = SimpleNamespace(id=1, username="assistant", first_name="Assistant")

            # Agents discuss this protocol, and a message about the tag must not
            # be silenced by the tag it is about.
            about = self.sender("Assistant, the #noreply tag goes on its own line")
            trailing = self.sender("Assistant, see below\n#noreply extra words")

            self.assertTrue(await daemon._message_addresses_me(about, me, policy))
            self.assertTrue(await daemon._message_addresses_me(trailing, me, policy))

    async def test_a_request_records_what_its_answer_owes(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = await self.policy(td)

            self.assertTrue(daemon._marker_line("ask\n\n#external", "#external"))
            self.assertFalse(daemon._marker_line("about #external tags", "#external"))
            self.assertEqual(daemon._with_marker("body", "#noreply"),
                             "body\n\n#noreply")

    async def test_every_chunk_carries_the_tag(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = await self.policy(td)
            limit = daemon.TELEGRAM_MESSAGE_LIMIT

            # A peer daemon reads each message on its own, so an untagged tail
            # chunk would wake it.
            reserve = len("#noreply") + 2
            chunks = [daemon._with_marker(chunk, "#noreply")
                      for chunk in _chunks("x " * limit, limit - reserve)]

            self.assertGreater(len(chunks), 1)
            for chunk in chunks:
                self.assertTrue(chunk.endswith("#noreply"))
                self.assertLessEqual(len(chunk), limit)


def _chunks(text, limit):
    """The daemon's split rule, mirrored for a test that cannot reach into the
    client closure where it lives."""
    out = []
    while len(text) > limit:
        boundary = max(text.rfind("\n", 0, limit + 1), text.rfind(" ", 0, limit + 1))
        boundary = limit if boundary <= 0 else boundary + 1
        out.append(text[:boundary])
        text = text[boundary:]
    out.append(text)
    return out


class MediaStackLogTests(unittest.IsolatedAsyncioTestCase):
    """What the media stack says about a call is routed into the daemon's log,
    at a level the daemon chooses rather than the one the import leaves."""

    async def test_the_media_logger_is_routed_and_claimed(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            media = logging.getLogger("ntgcalls")

            self.assertTrue(any(isinstance(h, daemon._MediaStackLog)
                                for h in media.handlers))
            # Importing pytgcalls leaves this at CRITICAL, which is silence for
            # everything worth reading. Claiming it is what the ntgcalls pin
            # buys: the stack's log thread enters the interpreter on every line,
            # and only from b19 does it do so without a native lock held.
            self.assertEqual(media.level, logging.INFO)

    async def test_a_repeated_condition_is_collapsed_not_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            handler = daemon._MediaStackLog()

            def record(message):
                return logging.LogRecord("ntgcalls", logging.DEBUG, __file__, 0,
                                         message, None, None)

            for _ in range(3):
                handler.emit(record("connection lost"))
            handler.emit(record("something else"))

            lines = [line for line in daemon._test_logs if "call-media" in line]
            self.assertEqual(len(lines), 3)
            self.assertIn("connection lost", lines[0])
            self.assertIn("repeated 2x", lines[1])
            self.assertIn("something else", lines[2])


class AudioPeerTrackingTests(unittest.IsolatedAsyncioTestCase):
    """Nothing else surfaces whether anyone is still sending audio into a
    conference, so the media stack's own channel lines are followed."""

    async def test_channels_are_followed_by_ssrc(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            daemon.MEDIA_AUDIO_PEERS.clear()

            daemon._track_audio_peer(
                "native_network_interface.cpp:161 Adding incoming audio "
                "channel with ssrc 1932033436")
            daemon._track_audio_peer(
                "native_network_interface.cpp:161 Adding incoming audio "
                "channel with ssrc 42")
            self.assertEqual(daemon.MEDIA_AUDIO_PEERS, {1932033436, 42})

            daemon._track_audio_peer(
                "native_network_interface.cpp:214 Removing incoming audio "
                "channel with ssrc 1932033436")
            self.assertEqual(daemon.MEDIA_AUDIO_PEERS, {42})

    async def test_unrelated_lines_leave_the_set_alone(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            daemon.MEDIA_AUDIO_PEERS.clear()
            daemon.MEDIA_AUDIO_PEERS.add(7)

            for line in ("ntgcalls.cpp:186 Migrating P2P call to conference call",
                         "group_call.cpp:43 Data channel opened",
                         "Adding incoming video channel with ssrc 99"):
                daemon._track_audio_peer(line)

            self.assertEqual(daemon.MEDIA_AUDIO_PEERS, {7})

    async def test_churn_inside_a_live_call_does_not_read_as_empty(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            daemon.MEDIA_AUDIO_PEERS.clear()

            def add(ssrc):
                daemon._track_audio_peer(
                    f"Adding incoming audio channel with ssrc {ssrc}")

            def remove(ssrc):
                daemon._track_audio_peer(
                    f"Removing incoming audio channel with ssrc {ssrc}")

            # The 2026-08-21 conference that recorded cleanly: two participants,
            # and the same ssrc dropped and restored three times in nineteen
            # seconds while the call was alive throughout.
            add(946429944)
            remove(946429944); add(946429944)
            add(1425171273)
            remove(946429944); add(946429944)
            remove(1425171273); add(1425171273)

            self.assertEqual(daemon.MEDIA_AUDIO_PEERS, {946429944, 1425171273})
            # Quiet only decides when to ask Telegram who is still there; the
            # answer, not the quiet, ends a recording.
            self.assertGreaterEqual(daemon.CONFERENCE_QUIET_BEFORE_CHECK, 10.0)
            self.assertGreaterEqual(daemon.CONFERENCE_EMPTY_ANSWERS, 2)
            # Measured from the join: the 2026-08-21 conference that hung
            # produced no incoming audio channel at all, so a grace that waits
            # for a first channel would never start counting on the very case
            # the watchdog exists for.
            self.assertGreaterEqual(daemon.CONFERENCE_JOIN_GRACE, 20.0)

    async def test_the_removal_that_ended_a_real_call_is_recognised(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            daemon.MEDIA_AUDIO_PEERS.clear()
            handler = daemon._MediaStackLog()

            def record(message):
                return logging.LogRecord("ntgcalls", logging.INFO, __file__, 0,
                                         message, None, None)

            # Verbatim from the 2026-08-21 conference that hung for an hour.
            handler.emit(record("native_network_interface.cpp:161 Adding "
                                "incoming audio channel with ssrc 1932033436"))
            self.assertTrue(daemon.MEDIA_AUDIO_PEERS)
            handler.emit(record("native_network_interface.cpp:214 Removing "
                                "incoming audio channel with ssrc 1932033436"))
            self.assertFalse(daemon.MEDIA_AUDIO_PEERS)


class MediaLogLevelTests(unittest.TestCase):
    """The media stack is read at the level the settings ask for."""

    def test_the_default_reads_the_media_stack_at_info(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            self.assertEqual(daemon._media_log.level, logging.INFO)

    def test_a_call_under_investigation_can_be_read_at_debug(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td),
                                   settings(media_log_level="debug"))

            self.assertEqual(daemon._media_log.level, logging.DEBUG)

    def test_a_level_the_sink_does_not_know_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            root = Path(td)

            # Refused, not guessed at: an unreadable level would otherwise
            # decide silently how much of a failing call is recorded.
            self.assertIsNone(daemon.media_log_level("verbose"))
            with self.assertRaisesRegex(Exception, "must be one of"):
                daemon.validate_settings(
                    {"connection": "test", "assistant_name": "Assistant",
                     "direct_messages": {"mode": "anyone",
                                         "default_role": "direct_user"},
                     "allowed_users": {}, "allowed_groups": {},
                     "defaults": {"media_log_level": "verbose"}},
                    root, root)


class ConferenceChainSettleTests(unittest.IsolatedAsyncioTestCase):
    """A conference still being built is waited out, not refused outright."""

    def prepare(self, daemon):
        daemon.CONFERENCE_CHAIN_SETTLE_STEP = 0.01
        daemon.CONFERENCE_CHAIN_SETTLE_MIN = 0.04
        daemon.CONFERENCE_CHAIN_SETTLE_BUDGET = 0.4

    def reader(self, blocks):
        """Answer each read from a list, holding the last answer afterwards."""
        answers = list(blocks)
        reads = []

        async def read(invite_msg_id):
            reads.append(invite_msg_id)
            return answers.pop(0) if len(answers) > 1 else answers[0]

        return read, reads

    async def test_a_still_chain_is_joined_once_its_block_has_aged(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.prepare(daemon)
            read, reads = self.reader([b"block-one"])

            block = await daemon.settle_conference_chain(4242, b"block-one",
                                                         6.0, read)

            self.assertEqual(block, b"block-one")
            self.assertTrue(reads, "the chain is re-read while it settles")
            self.assertTrue(any("still being built" in line
                                for line in daemon._test_logs))
            # The join is announced as one made on an unready invite, because
            # whether it works is what the wait exists to find out.
            self.assertTrue(any("settled" in line and "joining" in line
                                for line in daemon._test_logs))

    async def test_growth_restarts_the_wait_and_joins_the_newest_block(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.prepare(daemon)
            read, _ = self.reader([b"block-two", b"block-three"])

            block = await daemon.settle_conference_chain(4242, b"block-one",
                                                         6.0, read)

            self.assertEqual(block, b"block-three")
            self.assertEqual(
                2, sum("still growing" in line for line in daemon._test_logs))

    async def test_a_chain_that_never_stops_growing_is_declined(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.prepare(daemon)
            counter = itertools.count()

            async def read(invite_msg_id):
                return f"block-{next(counter)}".encode()

            with self.assertRaises(RuntimeError) as caught:
                await daemon.settle_conference_chain(4242, b"block-one",
                                                     6.0, read)

            self.assertIn("declined", str(caught.exception))

    async def test_a_chain_that_empties_while_settling_is_declined(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.prepare(daemon)

            async def read(invite_msg_id):
                return None

            with self.assertRaises(RuntimeError) as caught:
                await daemon.settle_conference_chain(4242, b"block-one",
                                                     6.0, read)

            self.assertIn("went empty", str(caught.exception))


class ConferenceJoinChainRetryTests(unittest.IsolatedAsyncioTestCase):
    """A join Telegram refuses because the chain moved past the block it was
    offered is given the block that chain ends on now."""

    REFUSAL = ("BadRequestError: RPCError 400: CONF_WRITE_CHAIN_INVALID "
               "(caused by JoinGroupCallRequest)")

    def parts(self, daemon, refusals, other=None):
        """A join refused `refusals` times, and the counters to prove it."""
        daemon.CONFERENCE_JOIN_CHAIN_RETRY_DELAY = 0.0
        counts = {"prepare": 0, "join": 0, "leave": 0}

        async def prepare():
            counts["prepare"] += 1

        async def join():
            counts["join"] += 1
            if other is not None:
                raise RuntimeError(other)
            if counts["join"] <= refusals:
                raise RuntimeError(self.REFUSAL)
            return "joined"

        async def leave():
            counts["leave"] += 1

        return prepare, join, leave, counts

    async def test_a_refused_chain_is_read_again_and_joined(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            prepare, join, leave, counts = self.parts(daemon, 1)

            result = await daemon.join_conference_on_a_current_chain(
                prepare, join, leave)

            self.assertEqual("joined", result)
            # The chain is read again before the second attempt, which is the
            # whole point: the refused block is not offered twice.
            self.assertEqual(2, counts["prepare"])
            self.assertEqual(2, counts["join"])
            self.assertEqual(1, counts["leave"])
            self.assertTrue(any("moved past the block" in line
                                for line in daemon._test_logs))

    async def test_a_chain_that_keeps_refusing_is_given_up_on(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            prepare, join, leave, counts = self.parts(daemon, 99)

            with self.assertRaises(RuntimeError) as caught:
                await daemon.join_conference_on_a_current_chain(
                    prepare, join, leave)

            self.assertIn("CONF_WRITE_CHAIN_INVALID", str(caught.exception))
            self.assertEqual(daemon.CONFERENCE_JOIN_CHAIN_RETRIES + 1,
                             counts["join"])

    async def test_any_other_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            prepare, join, leave, counts = self.parts(
                daemon, 0, other="conference chain empty for invite 2367")

            with self.assertRaises(RuntimeError):
                await daemon.join_conference_on_a_current_chain(
                    prepare, join, leave)

            # A caller is waiting through every one of these attempts, so a
            # failure the retry cannot address costs them nothing extra.
            self.assertEqual(1, counts["join"])
            self.assertEqual(0, counts["leave"])


class OrphanedRecordingTests(unittest.IsolatedAsyncioTestCase):
    """A recording is closed by the process that opened it, so one that outlives
    its process is not still running."""

    async def test_a_preempted_recording_task_cannot_keep_running(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            cancelled = asyncio.Event()

            async def recorder():
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

            task = asyncio.create_task(recorder())
            await asyncio.sleep(0)
            await daemon._cancel_recording_task(task)

            self.assertTrue(task.cancelled())
            self.assertTrue(cancelled.is_set())

    def write(self, daemon, name, **fields):
        folder = daemon.CONNECTION_STATE_DIR / "calls" / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        record = {"status": "recording", "stop_reason": None,
                  "delivery": {"status": "pending", "error": None}}
        record.update(fields)
        path.write_text(json.dumps(record) + "\n")
        return path

    async def test_an_abandoned_recording_stops_claiming_to_run(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            stuck = self.write(daemon, "a.json")

            daemon.reconcile_orphaned_recordings()

            record = json.loads(stuck.read_text())
            self.assertEqual(record["status"], "interrupted")
            self.assertEqual(record["stop_reason"], "process_ended")
            self.assertEqual(record["delivery"]["status"], "skipped")

    async def test_a_settled_recording_is_left_exactly_as_it_was(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            done = self.write(daemon, "b.json", status="complete",
                              stop_reason="call_closed",
                              delivery={"status": "sent", "message_id": 7})

            daemon.reconcile_orphaned_recordings()

            record = json.loads(done.read_text())
            self.assertEqual(record["status"], "complete")
            self.assertEqual(record["stop_reason"], "call_closed")
            self.assertEqual(record["delivery"]["message_id"], 7)

    async def test_unreadable_metadata_does_not_stop_the_sweep(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            folder = daemon.CONNECTION_STATE_DIR / "calls" / "recordings"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "broken.json").write_text("{not json\n")
            stuck = self.write(daemon, "c.json")

            daemon.reconcile_orphaned_recordings()

            self.assertEqual(json.loads(stuck.read_text())["status"], "interrupted")


class InterruptedRecordingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """A process killed mid-call leaves audio behind. What the next process does
    with it is the difference between losing a call and delivering it late."""

    def write(self, daemon, name, *, capture_bytes=None, capture_age=0.0,
              **fields):
        folder = daemon.CONNECTION_STATE_DIR / "calls" / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.json"
        output = folder / f"{name}.ogg"
        capture = folder / f"{name}.mp3"
        if capture_bytes is not None:
            capture.write_bytes(b"x" * capture_bytes)
            if capture_age:
                stamp = time.time() - capture_age
                os.utime(capture, (stamp, stamp))
        record = {
            "status": "recording",
            "stop_reason": None,
            "mode": "conference",
            "caller_id": "4242",
            "conference_invite_msg_id": 99,
            "audio": {
                "path": str(output),
                "bytes": 0,
                "settled": False,
                "source": {"path": str(capture), "bytes": 0, "retained": False},
                "conversion": {"status": "pending", "error": None},
            },
            "delivery": {"enabled": True, "status": "pending", "error": None},
        }
        record.update(fields)
        path.write_text(json.dumps(record) + "\n")
        return path

    def stub_conversion(self, daemon, *, duration=90.0, silence_from=None):
        async def trailing_silence_start(_capture, **_kwargs):
            return silence_from

        async def finalize_mp3_capture(_capture, output, **_kwargs):
            output.write_bytes(b"ogg")
            return {"status": "complete", "error": None, "output_bytes": 3,
                    "source_bytes": 1024, "source_retained": True,
                    "duration_seconds": duration}

        daemon.trailing_silence_start = trailing_silence_start
        daemon.finalize_mp3_capture = finalize_mp3_capture

    def stub_delivery(self, daemon):
        sent = []

        async def send_recording_to_chat(_client, chat_id, output, _path,
                                         metadata, **kwargs):
            metadata["delivery"].update({"status": "sent", "message_id": 1})
            sent.append((chat_id, output, kwargs.get("caption")))

        daemon.send_recording_to_chat = send_recording_to_chat
        return sent

    def client(self, participants):
        class Client:
            def __init__(self):
                self.messages = []

            async def __call__(self, _request):
                if participants is None:
                    raise RuntimeError("no answer")
                return SimpleNamespace(
                    call=SimpleNamespace(participants_count=participants))

            async def send_message(self, chat_id, text):
                self.messages.append((chat_id, text))
                return SimpleNamespace(id=1)

        return Client()

    async def test_a_capture_with_audio_is_held_for_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path = self.write(daemon, "held", capture_bytes=2048)

            held = daemon.reconcile_orphaned_recordings()

            record = json.loads(path.read_text())
            self.assertEqual(record["status"], "interrupted")
            self.assertEqual(record["stop_reason"], "process_ended")
            self.assertEqual(record["delivery"]["status"], "pending_recovery")
            self.assertEqual(held, [path])

    async def test_a_capture_with_no_audio_is_still_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path = self.write(daemon, "empty", capture_bytes=0)

            held = daemon.reconcile_orphaned_recordings()

            record = json.loads(path.read_text())
            self.assertEqual(record["delivery"]["status"], "skipped")
            self.assertEqual(record["delivery"]["error"], "interrupted")
            self.assertEqual(held, [])

    async def test_a_recovered_recording_is_converted_and_delivered(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path = self.write(daemon, "call", capture_bytes=4096)
            held = daemon.reconcile_orphaned_recordings()
            self.stub_conversion(daemon, duration=1595.8)
            sent = self.stub_delivery(daemon)

            await daemon.recover_interrupted_recordings(
                self.client(0), held, rejoin=None)

            record = json.loads(path.read_text())
            self.assertEqual(record["status"], "recovered")
            self.assertEqual(record["stop_reason"], "process_ended")
            self.assertEqual(record["duration_seconds"], 1595.8)
            self.assertTrue(record["audio"]["settled"])
            self.assertEqual(len(sent), 1)
            self.assertIn("26:36", sent[0][2])
            self.assertIn("оборвана", sent[0][2])

    async def test_trailing_silence_is_cut_off_what_is_sent(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path = self.write(daemon, "quiet", capture_bytes=4096)
            held = daemon.reconcile_orphaned_recordings()
            self.stub_conversion(daemon, duration=120.0, silence_from=120.0)
            self.stub_delivery(daemon)

            await daemon.recover_interrupted_recordings(
                self.client(0), held, rejoin=None)

            record = json.loads(path.read_text())
            self.assertEqual(
                record["audio"]["trimmed_tail_silence_from_seconds"], 120.0)

    async def test_a_capture_that_is_only_silence_is_never_converted(self):
        """The shape the old throw-it-away rule was written for: a conference
        that outlived its call and encoded an empty room."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path = self.write(daemon, "silent", capture_bytes=4096)
            held = daemon.reconcile_orphaned_recordings()
            converted = []

            async def trailing_silence_start(_capture, **_kwargs):
                return 0.4

            async def finalize_mp3_capture(*args, **kwargs):
                converted.append(args)
                raise AssertionError("nothing here is worth converting")

            daemon.trailing_silence_start = trailing_silence_start
            daemon.finalize_mp3_capture = finalize_mp3_capture
            sent = self.stub_delivery(daemon)

            await daemon.recover_interrupted_recordings(
                self.client(0), held, rejoin=None)

            record = json.loads(path.read_text())
            self.assertEqual(record["audio"]["conversion"]["status"], "skipped")
            self.assertEqual(record["audio"]["conversion"]["error"], "only_silence")
            self.assertEqual(record["delivery"]["status"], "skipped")
            self.assertEqual(record["delivery"]["error"], "audio_not_received")
            self.assertEqual(converted, [])
            self.assertEqual(sent, [])

    async def test_a_recording_shorter_than_a_call_is_not_sent(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path = self.write(daemon, "blip", capture_bytes=4096)
            held = daemon.reconcile_orphaned_recordings()
            self.stub_conversion(daemon, duration=1.2)
            sent = self.stub_delivery(daemon)

            await daemon.recover_interrupted_recordings(
                self.client(0), held, rejoin=None)

            record = json.loads(path.read_text())
            self.assertEqual(record["delivery"]["status"], "skipped")
            self.assertEqual(record["delivery"]["error"], "audio_not_received")
            self.assertEqual(sent, [])

    async def test_a_conference_that_still_has_people_is_rejoined(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write(daemon, "live", capture_bytes=4096, capture_age=12.0)
            held = daemon.reconcile_orphaned_recordings()
            self.stub_conversion(daemon)
            sent = self.stub_delivery(daemon)
            rejoined = []

            async def rejoin(caller_id, invite_msg_id, continues=None):
                rejoined.append((caller_id, invite_msg_id))
                return True

            client = self.client(2)
            await daemon.recover_interrupted_recordings(client, held, rejoin)

            self.assertEqual(rejoined, [(4242, 99)])
            self.assertEqual(len(client.messages), 1)
            self.assertIn("во время звонка и вернулся", client.messages[0][1])
            self.assertEqual(sent, [], "a rejoined part is not sent on its own")

    async def test_a_crash_and_a_restart_are_not_described_the_same_way(self):
        """Both leave `process_ended` on the record; only the health file the
        previous process left behind says which one it was."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write(daemon, "call", capture_bytes=4096)
            held = daemon.reconcile_orphaned_recordings()
            self.stub_conversion(daemon, duration=90.0)
            sent = self.stub_delivery(daemon)

            daemon.PREVIOUS_EXIT["unclean"] = True
            await daemon.recover_interrupted_recordings(
                self.client(0), held, rejoin=None)
            self.assertIn("упал во время звонка", sent[-1][2])

            self.write(daemon, "call2", capture_bytes=4096)
            held = daemon.reconcile_orphaned_recordings()
            daemon.PREVIOUS_EXIT["unclean"] = False
            await daemon.recover_interrupted_recordings(
                self.client(0), held, rejoin=None)
            self.assertIn("перезапустился во время звонка", sent[-1][2])

    async def test_an_empty_conference_is_not_rejoined(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write(daemon, "over", capture_bytes=4096, capture_age=12.0)
            held = daemon.reconcile_orphaned_recordings()
            self.stub_conversion(daemon)
            self.stub_delivery(daemon)
            rejoined = []

            async def rejoin(caller_id, invite_msg_id, continues=None):
                rejoined.append((caller_id, invite_msg_id))
                return True

            await daemon.recover_interrupted_recordings(
                self.client(0), held, rejoin)

            self.assertEqual(rejoined, [])

    async def test_a_call_that_stopped_long_ago_is_not_rejoined(self):
        """Coming back hours later would walk into a call that has since been
        held, finished and forgotten."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write(daemon, "old", capture_bytes=4096,
                       capture_age=daemon.RECOVERY_REJOIN_WINDOW + 60)
            held = daemon.reconcile_orphaned_recordings()
            self.stub_conversion(daemon)
            self.stub_delivery(daemon)
            rejoined = []

            async def rejoin(caller_id, invite_msg_id, continues=None):
                rejoined.append((caller_id, invite_msg_id))
                return True

            client = self.client(5)
            await daemon.recover_interrupted_recordings(client, held, rejoin)

            self.assertEqual(rejoined, [])

    async def test_one_broken_recovery_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write(daemon, "a-broken", capture_bytes=4096)
            self.write(daemon, "b-fine", capture_bytes=4096)
            held = daemon.reconcile_orphaned_recordings()
            self.stub_conversion(daemon)
            sent = self.stub_delivery(daemon)
            calls = itertools.count()
            original = daemon.finalize_mp3_capture

            async def finalize(capture, output, **kwargs):
                if next(calls) == 0:
                    raise RuntimeError("ffmpeg went missing")
                return await original(capture, output, **kwargs)

            daemon.finalize_mp3_capture = finalize

            await daemon.recover_interrupted_recordings(
                self.client(0), held, rejoin=None)

            self.assertEqual(len(sent), 1)


class DeferredRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Converting and uploading beside a live media session wedged the daemon
    on 2026-08-24, so a recovery that walks back into a call waits for it."""

    def write(self, daemon, name, **fields):
        folder = daemon.CONNECTION_STATE_DIR / "calls" / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.json"
        output = folder / f"{name}.ogg"
        capture = folder / f"{name}.mp3"
        capture.write_bytes(b"x" * 4096)
        record = {
            "status": "recording", "stop_reason": None, "mode": "conference",
            "caller_id": "4242", "conference_invite_msg_id": 99,
            "audio": {
                "path": str(output), "bytes": 0, "settled": False,
                "source": {"path": str(capture), "bytes": 0, "retained": False},
                "conversion": {"status": "pending", "error": None},
            },
            "delivery": {"enabled": True, "status": "pending", "error": None},
        }
        record.update(fields)
        path.write_text(json.dumps(record) + "\n")
        return path

    def client(self, participants=3):
        class Client:
            def __init__(self):
                self.messages = []

            async def __call__(self, _request):
                return SimpleNamespace(
                    call=SimpleNamespace(participants_count=participants))

            async def send_message(self, chat_id, text):
                self.messages.append((chat_id, text))
                return SimpleNamespace(id=1)

        return Client()

    def stubs(self, daemon):
        events = []

        async def trailing_silence_start(_capture, **_kwargs):
            events.append("measured")
            return None

        async def finalize_mp3_capture(_capture, output, **_kwargs):
            events.append("converted")
            output.write_bytes(b"ogg")
            return {"status": "complete", "error": None, "output_bytes": 3,
                    "source_bytes": 4096, "source_retained": True,
                    "duration_seconds": 90.0}

        async def send_recording_to_chat(_client, _chat, _out, _path, metadata,
                                         **_kwargs):
            events.append("sent")
            metadata["delivery"].update({"status": "sent", "message_id": 5})

        daemon.trailing_silence_start = trailing_silence_start
        daemon.finalize_mp3_capture = finalize_mp3_capture
        daemon.send_recording_to_chat = send_recording_to_chat
        return events

    async def test_a_rejoined_part_is_handed_on_and_never_sent_on_its_own(self):
        """It is half a conversation. The recording that carries on from it
        renders both and sends one file when the call is actually over."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path = self.write(daemon, "live")
            held = daemon.reconcile_orphaned_recordings()
            events = self.stubs(daemon)
            handed = []

            async def rejoin(_caller_id, _invite, continues=None):
                handed.append(continues)
                return True

            await asyncio.wait_for(daemon.recover_interrupted_recordings(
                self.client(), held, rejoin, lambda: True), timeout=5)

            self.assertEqual(events, [])
            self.assertEqual(handed, [path])
            record = json.loads(path.read_text())
            self.assertEqual(record["status"], "continued")
            self.assertEqual(record["delivery"]["status"], "continued")

    async def test_a_handed_on_part_is_not_swept_again(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path = self.write(daemon, "live")
            held = daemon.reconcile_orphaned_recordings()
            self.stubs(daemon)

            async def rejoin(_caller_id, _invite, continues=None):
                return True

            await asyncio.wait_for(daemon.recover_interrupted_recordings(
                self.client(), held, rejoin, lambda: True), timeout=5)

            self.assertEqual(daemon.reconcile_orphaned_recordings(), [])
            self.assertTrue(path.exists())

    async def test_a_recovery_beside_someone_elses_call_still_waits(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            daemon.RECOVERY_IDLE_POLL = 0.01
            self.write(daemon, "other")
            held = daemon.reconcile_orphaned_recordings()
            events = self.stubs(daemon)
            busy = {"value": True}

            task = asyncio.create_task(daemon.recover_interrupted_recordings(
                self.client(participants=0), held, None,
                lambda: busy["value"]))
            for _ in range(20):
                await asyncio.sleep(0.01)
                if events:
                    break
            self.assertEqual(events, [], "worked beside a live call")

            busy["value"] = False
            await asyncio.wait_for(task, timeout=5)

            self.assertEqual(events, ["measured", "converted", "sent"])

    async def test_a_recovery_with_nothing_running_does_not_wait(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write(daemon, "over")
            held = daemon.reconcile_orphaned_recordings()
            events = self.stubs(daemon)

            async def rejoin(_caller_id, _invite, continues=None):
                raise AssertionError("an empty conference is not rejoined")

            await asyncio.wait_for(daemon.recover_interrupted_recordings(
                self.client(participants=0), held, rejoin, lambda: False),
                timeout=5)

            self.assertEqual(events, ["measured", "converted", "sent"])


class UnfinishedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """A delivery is not finished when the process running it dies, and until
    2026-08-24 nothing ever looked at one again."""

    def write(self, daemon, name, delivery, **audio):
        folder = daemon.CONNECTION_STATE_DIR / "calls" / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.json"
        output = folder / f"{name}.ogg"
        capture = folder / f"{name}.mp3"
        record = {
            "status": "recovered", "stop_reason": "process_ended",
            "mode": "conference", "caller_id": "4242",
            "duration_seconds": 54.0,
            "audio": {
                "path": str(output), "bytes": 0, "settled": False,
                "source": {"path": str(capture), "bytes": 0, "retained": False},
                "conversion": {"status": "complete", "error": None},
            },
            "delivery": dict({"enabled": True}, **delivery),
        }
        record["audio"].update(audio)
        if record["audio"]["settled"]:
            output.write_bytes(b"ogg")
        else:
            capture.write_bytes(b"x" * 4096)
        path.write_text(json.dumps(record) + "\n")
        return path

    async def test_a_delivery_stuck_at_sending_is_carried_into_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            path = self.write(daemon, "stuck",
                              {"status": "sending", "message_id": None},
                              settled=True)

            held = daemon.reconcile_orphaned_recordings()

            self.assertEqual(held, [path])

    async def test_an_already_converted_recording_is_resent_not_reconverted(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write(daemon, "stuck",
                       {"status": "sending", "message_id": None}, settled=True)
            held = daemon.reconcile_orphaned_recordings()
            sent = []

            async def finalize_mp3_capture(*_args, **_kwargs):
                raise AssertionError("the audio was already converted")

            async def send_recording_to_chat(_client, chat_id, _out, _path,
                                             metadata, **kwargs):
                sent.append((chat_id, kwargs.get("caption")))
                metadata["delivery"].update({"status": "sent", "message_id": 9})

            daemon.finalize_mp3_capture = finalize_mp3_capture
            daemon.send_recording_to_chat = send_recording_to_chat

            await daemon.recover_interrupted_recordings(
                None, held, rejoin=None, call_active=None)

            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], 4242)
            self.assertIn("0:54", sent[0][1])

    async def test_a_finished_delivery_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write(daemon, "done",
                       {"status": "sent", "message_id": 11}, settled=True)

            self.assertEqual(daemon.reconcile_orphaned_recordings(), [])


class StitchedRecordingTests(unittest.IsolatedAsyncioTestCase):
    """A call split by a crash is one conversation, and is delivered as one."""

    def part(self, daemon, name, *, started, stopped, continues=None):
        folder = daemon.CONNECTION_STATE_DIR / "calls" / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.json"
        capture = folder / f"{name}.mp3"
        capture.write_bytes(b"x" * 4096)
        os.utime(capture, (stopped, stopped))
        record = {
            "status": "recording", "mode": "conference", "caller_id": "4242",
            "recording_started_at": datetime.fromtimestamp(
                started, tz=timezone.utc).isoformat(timespec="seconds"),
            "continues": str(continues) if continues else None,
            "audio": {
                "path": str(folder / f"{name}.ogg"), "bytes": 0,
                "settled": False,
                "source": {"path": str(capture), "bytes": 0, "retained": True},
                "conversion": {"status": "pending", "error": None},
            },
            "delivery": {"enabled": True, "status": "pending", "error": None},
        }
        path.write_text(json.dumps(record) + "\n")
        return path, record

    async def test_the_chain_is_gathered_oldest_first_with_the_gap_between(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            base = 1_700_000_000
            first, _ = self.part(daemon, "one", started=base, stopped=base + 60)
            second, record = self.part(daemon, "two", started=base + 74,
                                       stopped=base + 130, continues=first)

            parts = daemon.gather_recording_parts(record, second)

            self.assertEqual([p["path"].stem for p in parts], ["one", "two"])
            self.assertEqual(parts[0]["gap_before"], 0.0)
            self.assertAlmostEqual(parts[1]["gap_before"], 14.0, places=1)

    async def test_three_lives_of_one_call_all_land_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            base = 1_700_000_000
            one, _ = self.part(daemon, "one", started=base, stopped=base + 30)
            two, _ = self.part(daemon, "two", started=base + 40,
                               stopped=base + 70, continues=one)
            three, record = self.part(daemon, "three", started=base + 80,
                                      stopped=base + 110, continues=two)

            parts = daemon.gather_recording_parts(record, three)

            self.assertEqual([p["path"].stem for p in parts],
                             ["one", "two", "three"])

    async def test_an_absurd_gap_is_not_written_back_as_silence(self):
        """A machine that was off for a day is not a pause in the call."""
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            base = 1_700_000_000
            first, _ = self.part(daemon, "one", started=base, stopped=base + 60)
            second, record = self.part(daemon, "two", started=base + 90_000,
                                       stopped=base + 90_060, continues=first)

            parts = daemon.gather_recording_parts(record, second)

            self.assertEqual(parts[1]["gap_before"], daemon.RECOVERY_MAX_GAP)

    async def test_a_part_whose_capture_is_gone_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            base = 1_700_000_000
            first, first_record = self.part(daemon, "one", started=base,
                                            stopped=base + 60)
            second, record = self.part(daemon, "two", started=base + 74,
                                       stopped=base + 130, continues=first)
            Path(first_record["audio"]["source"]["path"]).unlink()

            parts = daemon.gather_recording_parts(record, second)

            self.assertEqual([p["path"].stem for p in parts], ["two"])

    async def test_a_chain_that_points_at_itself_still_terminates(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            base = 1_700_000_000
            path, record = self.part(daemon, "loop", started=base,
                                     stopped=base + 60)
            record["continues"] = str(path)
            path.write_text(json.dumps(record) + "\n")

            parts = await asyncio.wait_for(
                asyncio.to_thread(daemon.gather_recording_parts, record, path),
                timeout=5)

            self.assertEqual(len(parts), 1)

    async def test_the_caption_names_the_crash_and_what_it_cost(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            daemon.PREVIOUS_EXIT["unclean"] = True

            plain = daemon.stitched_caption({}, 95.0)
            stitched = daemon.stitched_caption(
                {"stitched_from_parts": 2, "gap_seconds": 14.0}, 140.0)

            self.assertEqual(plain, "Запись звонка · 1:35")
            self.assertIn("склеена из 2 частей", stitched)
            self.assertIn("упал во время звонка", stitched)
            self.assertIn("0:14", stitched)


class UncleanExitTests(unittest.IsolatedAsyncioTestCase):
    """A crash and a restart look the same from the outside unless the daemon
    says which one it was."""

    def write_health(self, daemon, **fields):
        daemon.HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        daemon.HEALTH_FILE.write_text(json.dumps(fields) + "\n")

    async def test_a_process_that_was_killed_is_named_as_killed(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write_health(daemon, state="healthy", pid=4242,
                              updated_at="2026-08-23T18:17:57+00:00")

            daemon.report_unclean_exit()

            self.assertTrue(any("killed, not stopped" in line
                                for line in daemon._test_logs))
            self.assertTrue(daemon.PREVIOUS_EXIT["unclean"])

    async def test_a_clean_stop_is_not_reported_as_a_crash(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())
            self.write_health(daemon, state="stopped", pid=4242)

            daemon.report_unclean_exit()

            self.assertEqual(daemon._test_logs, [])
            self.assertFalse(daemon.PREVIOUS_EXIT["unclean"])

    async def test_a_first_run_has_nothing_to_report(self):
        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), settings())

            daemon.report_unclean_exit()

            self.assertEqual(daemon._test_logs, [])


if __name__ == "__main__":
    unittest.main()
