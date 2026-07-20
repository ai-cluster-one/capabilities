#!/usr/bin/env python3
"""Focused regressions for Telegram call-recording delivery metadata."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path


TELEGRAM_DIR = Path(__file__).resolve().parents[1]
CALL_RECORDER_PATH = TELEGRAM_DIR / "service" / "call_recorder.py"


class DummyType:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)


class DummyError(Exception):
    pass


class DocumentAttributeAudio(DummyType):
    pass


class DocumentAttributeFilename(DummyType):
    pass


@contextmanager
def fake_runtime_modules():
    modules: dict[str, types.ModuleType] = {}

    pytgcalls = types.ModuleType("pytgcalls")
    pytgcalls.PyTgCalls = DummyType
    pytgcalls.filters = types.SimpleNamespace()
    modules["pytgcalls"] = pytgcalls

    pytgcalls_exceptions = types.ModuleType("pytgcalls.exceptions")
    pytgcalls_exceptions.NoActiveGroupCall = DummyError
    pytgcalls_exceptions.NotInCallError = DummyError
    modules["pytgcalls.exceptions"] = pytgcalls_exceptions

    pytgcalls_types = types.ModuleType("pytgcalls.types")
    for name in ("CallConfig", "ChatUpdate", "GroupCallConfig", "RecordStream"):
        setattr(pytgcalls_types, name, DummyType)
    modules["pytgcalls.types"] = pytgcalls_types

    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = DummyType
    telethon.events = types.SimpleNamespace()
    modules["telethon"] = telethon

    telethon_errors = types.ModuleType("telethon.errors")
    telethon_errors.AuthKeyError = DummyError
    telethon_errors.RPCError = DummyError
    modules["telethon.errors"] = telethon_errors

    telethon_common = types.ModuleType("telethon.errors.common")
    telethon_common.TypeNotFoundError = DummyError
    modules["telethon.errors.common"] = telethon_common

    telethon_sessions = types.ModuleType("telethon.sessions")
    telethon_sessions.StringSession = DummyType
    modules["telethon.sessions"] = telethon_sessions

    modules["telethon.tl"] = types.ModuleType("telethon.tl")
    modules["telethon.tl.functions"] = types.ModuleType("telethon.tl.functions")
    telethon_messages = types.ModuleType("telethon.tl.functions.messages")
    telethon_messages.GetHistoryRequest = DummyType
    modules["telethon.tl.functions.messages"] = telethon_messages

    telethon_types = types.ModuleType("telethon.tl.types")
    telethon_types.DocumentAttributeAudio = DocumentAttributeAudio
    telethon_types.DocumentAttributeFilename = DocumentAttributeFilename
    for name in (
        "InputGroupCallInviteMessage",
        "InputGroupCallSlug",
        "MessageActionConferenceCall",
        "MessageActionGroupCall",
        "MessageActionInviteToGroupCall",
    ):
        setattr(telethon_types, name, DummyType)
    modules["telethon.tl.types"] = telethon_types

    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def import_call_recorder():
    with fake_runtime_modules():
        name = f"telegram_call_recorder_test_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, CALL_RECORDER_PATH)
        if spec is None or spec.loader is None:
            raise AssertionError("cannot import Telegram call recorder")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


class FakeClient:
    def __init__(self):
        self.calls = []

    async def send_file(self, chat_id, **kwargs):
        self.calls.append((chat_id, kwargs))
        return types.SimpleNamespace(id=7001)


class CallRecorderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_uses_seekable_audio_attributes(self):
        recorder = import_call_recorder()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "recording.ogg"
            output.write_bytes(b"ogg")
            metadata_path = root / "recording.json"
            metadata = {
                "status": "complete",
                "duration_seconds": 638.299,
                "audio": {"settled": True},
                "delivery": {"enabled": True},
            }
            client = FakeClient()

            await recorder.send_recording_to_chat(
                client,
                -1001,
                output,
                metadata_path,
                metadata,
            )

            self.assertEqual(len(client.calls), 1)
            chat_id, kwargs = client.calls[0]
            self.assertEqual(chat_id, -1001)
            self.assertEqual(kwargs["mime_type"], "audio/ogg")
            self.assertFalse(kwargs["force_document"])
            self.assertFalse(kwargs["voice_note"])
            filename, audio = kwargs["attributes"]
            self.assertIsInstance(filename, DocumentAttributeFilename)
            self.assertEqual(filename.file_name, "recording.ogg")
            self.assertIsInstance(audio, DocumentAttributeAudio)
            self.assertEqual(audio.duration, 638)
            self.assertFalse(audio.voice)
            self.assertEqual(metadata["delivery"]["status"], "sent")
            self.assertEqual(metadata["delivery"]["message_id"], 7001)
            persisted = json.loads(metadata_path.read_text())
            self.assertEqual(persisted["delivery"]["message_id"], 7001)

    async def test_probe_audio_duration_reads_ffprobe_value(self):
        recorder = import_call_recorder()

        class Process:
            returncode = 0

            async def communicate(self):
                return b"638.299479\n", b""

        original = recorder.asyncio.create_subprocess_exec

        async def fake_exec(*_args, **_kwargs):
            return Process()

        recorder.asyncio.create_subprocess_exec = fake_exec
        try:
            duration = await recorder.probe_audio_duration(Path("recording.ogg"))
        finally:
            recorder.asyncio.create_subprocess_exec = original

        self.assertAlmostEqual(duration, 638.299479)


if __name__ == "__main__":
    unittest.main()
