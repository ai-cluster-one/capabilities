#!/usr/bin/env python3
"""Focused regressions for the bundled Telegram assistant service."""

from __future__ import annotations

import asyncio
import importlib.util
import json
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
class MessageActionConferenceCall(_Placeholder): pass
class MessageActionInviteToGroupCall(_Placeholder): pass
class MessageService(_Placeholder): pass
class UpdateNewMessage(_Placeholder): pass
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


def import_daemon(tmp: Path, service_settings: dict, *,
                  connection_extra: dict | None = None,
                  voice_context: str | None = None,
                  project_env: dict | None = None):
    project = tmp / "project"
    service_dir = project / "capabilities" / "telegram" / "service"
    service_dir.mkdir(parents=True)
    settings_file = service_dir / "settings.json"
    context_file = service_dir / "context.md"
    voice_context_file = service_dir / "voice-agent.md"
    settings_file.write_text(json.dumps(service_settings) + "\n")
    context_file.write_text("test context\n")
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

    stubbed = ("telethon", "telethon.tl", "telethon.tl.types", "telethon.errors",
               "telethon.errors.common", "pytgcalls", "pytgcalls.filters",
               "pytgcalls.exceptions", "pytgcalls.types", "pytgcalls.types.raw",
               "call_recording_helpers", "voice_agent")

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
            updated["allowed_groups"] = {"-200": {"project": "/tmp/other"}}
            daemon.SETTINGS_FILE.write_text(json.dumps(updated) + "\n")

            result = daemon.reload_runtime_settings()

            self.assertFalse(result["ok"])
            self.assertIn(
                "settings.allowed_groups.-200.project: unsupported property",
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
            TELEGRAM_DIR / "bin" / "telegram",
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

    async def test_ambient_voice_catch_up_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            service_settings = settings()
            service_settings["allowed_groups"] = {
                "-200": {"voice_transcription": {"mode": "auto"}}
            }
            daemon = import_daemon(Path(td), service_settings)
            daemon.save_register({"-200": {"last_processed_message_id": 0}})

            message = Message(404, voice=True)
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


if __name__ == "__main__":
    unittest.main()
