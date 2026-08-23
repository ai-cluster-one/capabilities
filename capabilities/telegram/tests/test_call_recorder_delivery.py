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
HELPERS_PATH = TELEGRAM_DIR / "service" / "call_recording_helpers.py"


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
        "MessageService",
        "UpdateNewMessage",
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


def import_call_recording_helpers():
    with fake_runtime_modules():
        name = f"telegram_call_helpers_test_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, HELPERS_PATH)
        if spec is None or spec.loader is None:
            raise AssertionError("cannot import Telegram call recording helpers")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


class FakeClient:
    def __init__(self):
        self.file_calls = []
        self.message_calls = []

    async def send_message(self, chat_id, text):
        self.message_calls.append((chat_id, text))
        return types.SimpleNamespace(id=7000)

    async def send_file(self, chat_id, **kwargs):
        self.file_calls.append((chat_id, kwargs))
        return types.SimpleNamespace(id=7001)


class CallRecorderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_uses_voice_note_with_separate_notice(self):
        helpers = import_call_recording_helpers()
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
            original_waveform = helpers.build_voice_waveform

            async def fake_waveform(_path):
                return b"waveform"

            helpers.build_voice_waveform = fake_waveform
            try:
                await helpers.send_recording_to_chat(
                    client,
                    -1001,
                    output,
                    metadata_path,
                    metadata,
                )
            finally:
                helpers.build_voice_waveform = original_waveform

            self.assertEqual(
                client.message_calls,
                [(-1001, "Запись звонка · 10:38")],
            )
            self.assertEqual(len(client.file_calls), 1)
            chat_id, kwargs = client.file_calls[0]
            self.assertEqual(chat_id, -1001)
            self.assertEqual(kwargs["mime_type"], "audio/ogg")
            self.assertFalse(kwargs["force_document"])
            self.assertTrue(kwargs["voice_note"])
            filename, audio = kwargs["attributes"]
            self.assertIsInstance(filename, DocumentAttributeFilename)
            self.assertEqual(filename.file_name, "recording.ogg")
            self.assertIsInstance(audio, DocumentAttributeAudio)
            self.assertEqual(audio.duration, 638)
            self.assertTrue(audio.voice)
            self.assertEqual(audio.waveform, b"waveform")
            self.assertEqual(metadata["delivery"]["status"], "sent")
            self.assertEqual(metadata["delivery"]["notice_message_id"], 7000)
            self.assertEqual(metadata["delivery"]["message_id"], 7001)
            persisted = json.loads(metadata_path.read_text())
            self.assertEqual(persisted["delivery"]["notice_message_id"], 7000)
            self.assertEqual(persisted["delivery"]["message_id"], 7001)

    async def test_the_announcement_line_is_the_callers_when_it_gives_one(self):
        """The summary replies to this notice, so the project owns its wording;
        without one, the built-in line still announces the recording."""
        helpers = import_call_recording_helpers()
        original_waveform = helpers.build_voice_waveform

        async def fake_waveform(_path):
            return b"waveform"

        helpers.build_voice_waveform = fake_waveform
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                output = root / "recording.ogg"
                output.write_bytes(b"ogg")
                notices = []
                for caption in ("Here is the call:", None):
                    metadata = {
                        "status": "complete",
                        "duration_seconds": 61,
                        "audio": {"settled": True},
                        "delivery": {"enabled": True},
                    }
                    client = FakeClient()
                    await helpers.send_recording_to_chat(
                        client, -1001, output, root / "recording.json", metadata,
                        caption=caption)
                    notices.append(client.message_calls[0][1])
        finally:
            helpers.build_voice_waveform = original_waveform

        self.assertEqual(notices[0], "Here is the call:")
        self.assertEqual(notices[1], helpers.recording_caption({"duration_seconds": 61}))

    async def test_waveform_uses_telegram_five_bit_packing(self):
        helpers = import_call_recording_helpers()
        self.assertEqual(helpers.pack_voice_waveform([31, 31]), bytes([255, 3]))

        pcm = (
            int(1000).to_bytes(2, "little", signed=True) * 100
            + int(10000).to_bytes(2, "little", signed=True) * 100
        )
        waveform = helpers.voice_waveform_from_pcm(pcm, bars=2)
        self.assertIsNotNone(waveform)
        self.assertEqual(len(waveform), 2)

    async def test_probe_audio_duration_reads_ffprobe_value(self):
        helpers = import_call_recording_helpers()

        class Process:
            returncode = 0

            async def communicate(self):
                return b"638.299479\n", b""

        original = helpers.asyncio.create_subprocess_exec

        async def fake_exec(*_args, **_kwargs):
            return Process()

        helpers.asyncio.create_subprocess_exec = fake_exec
        try:
            duration = await helpers.probe_audio_duration(Path("recording.ogg"))
        finally:
            helpers.asyncio.create_subprocess_exec = original

        self.assertAlmostEqual(duration, 638.299479)


class TrailingSilenceTests(unittest.IsolatedAsyncioTestCase):
    """What a recovered capture ends with decides how much of it is worth
    sending, and only the audio itself can say."""

    @contextmanager
    def ffmpeg_saying(self, helpers, stderr, returncode=0, duration=None):
        class Process:
            def __init__(self):
                self.returncode = returncode

            async def communicate(self):
                return b"", stderr.encode()

        seen = []

        async def fake_exec(*args, **_kwargs):
            seen.append(args)
            return Process()

        async def fake_duration(_path):
            return duration

        original_exec = helpers.asyncio.create_subprocess_exec
        original_probe = helpers.probe_audio_duration
        helpers.asyncio.create_subprocess_exec = fake_exec
        helpers.probe_audio_duration = fake_duration
        try:
            yield seen
        finally:
            helpers.asyncio.create_subprocess_exec = original_exec
            helpers.probe_audio_duration = original_probe

    async def test_silence_that_reaches_the_end_reports_where_it_began(self):
        """ffmpeg closes the last stretch of silence at EOF like any other, so
        what marks it as trailing is that it ends where the file does."""
        helpers = import_call_recording_helpers()
        stderr = (
            "[silencedetect @ 0x1] silence_start: 12.5\n"
            "[silencedetect @ 0x1] silence_end: 40.1 | silence_duration: 27.6\n"
            "[silencedetect @ 0x1] silence_start: 3011.75\n"
            "[silencedetect @ 0x1] silence_end: 3600 | silence_duration: 588.25\n"
        )
        with self.ffmpeg_saying(helpers, stderr, duration=3600.0):
            start = await helpers.trailing_silence_start(Path("capture.mp3"))
        self.assertAlmostEqual(start, 3011.75)

    async def test_silence_the_call_came_back_from_is_left_alone(self):
        helpers = import_call_recording_helpers()
        stderr = (
            "[silencedetect @ 0x1] silence_start: 12.5\n"
            "[silencedetect @ 0x1] silence_end: 40.1 | silence_duration: 27.6\n"
        )
        with self.ffmpeg_saying(helpers, stderr, duration=3600.0):
            start = await helpers.trailing_silence_start(Path("capture.mp3"))
        self.assertIsNone(start)

    async def test_a_capture_cut_off_mid_speech_has_no_trailing_silence(self):
        """The 2026-08-23 segfault ended a conference in mid-sentence: nothing
        to trim, and trimming anything would take conversation off the end."""
        helpers = import_call_recording_helpers()
        with self.ffmpeg_saying(helpers, "", duration=1595.8):
            start = await helpers.trailing_silence_start(Path("capture.mp3"))
        self.assertIsNone(start)

    async def test_ffmpeg_failing_is_not_read_as_a_silent_tail(self):
        helpers = import_call_recording_helpers()
        with self.ffmpeg_saying(helpers, "", returncode=1):
            start = await helpers.trailing_silence_start(Path("capture.mp3"))
        self.assertIsNone(start)

    async def test_an_unmeasurable_length_is_not_read_as_a_silent_tail(self):
        helpers = import_call_recording_helpers()
        stderr = (
            "[silencedetect @ 0x1] silence_start: 3011.75\n"
            "[silencedetect @ 0x1] silence_end: 3600 | silence_duration: 588.25\n"
        )
        with self.ffmpeg_saying(helpers, stderr, duration=None):
            start = await helpers.trailing_silence_start(Path("capture.mp3"))
        self.assertIsNone(start)

    async def test_a_trim_bounds_the_conversion(self):
        helpers = import_call_recording_helpers()
        with tempfile.TemporaryDirectory() as td:
            capture = Path(td) / "capture.mp3"
            capture.write_bytes(b"mp3")
            with self.ffmpeg_saying(helpers, "") as seen:
                await helpers.finalize_mp3_capture(
                    capture, Path(td) / "out.ogg", trim_to=3011.75)
            self.assertIn("-t", seen[0])
            self.assertEqual(seen[0][seen[0].index("-t") + 1], "3011.750")

    async def test_no_trim_leaves_the_conversion_unbounded(self):
        helpers = import_call_recording_helpers()
        with tempfile.TemporaryDirectory() as td:
            capture = Path(td) / "capture.mp3"
            capture.write_bytes(b"mp3")
            with self.ffmpeg_saying(helpers, "") as seen:
                await helpers.finalize_mp3_capture(
                    capture, Path(td) / "out.ogg")
            self.assertNotIn("-t", seen[0])


class RecoveredDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_recovered_recording_is_deliverable(self):
        """It was finalized by the process that came after the crash rather
        than by the one that opened it, which changes who converted it and not
        whether it is worth having."""
        helpers = import_call_recording_helpers()
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "recording.ogg"
            output.write_bytes(b"ogg")
            metadata = {
                "status": "recovered",
                "duration_seconds": 1595.8,
                "audio": {"settled": True},
                "delivery": {"enabled": True},
            }
            client = FakeClient()
            original = helpers.build_voice_waveform

            async def fake_waveform(_path):
                return b"waveform"

            helpers.build_voice_waveform = fake_waveform
            try:
                await helpers.send_recording_to_chat(
                    client, 4242, output, Path(td) / "recording.json", metadata,
                    caption="Запись звонка · 26:36 · оборвана")
            finally:
                helpers.build_voice_waveform = original

            self.assertEqual(metadata["delivery"]["status"], "sent")
            self.assertEqual(client.message_calls,
                             [(4242, "Запись звонка · 26:36 · оборвана")])

    async def test_an_interrupted_recording_is_still_not_deliverable(self):
        helpers = import_call_recording_helpers()
        with tempfile.TemporaryDirectory() as td:
            metadata = {
                "status": "interrupted",
                "audio": {"settled": False},
                "delivery": {"enabled": True},
            }
            client = FakeClient()
            await helpers.send_recording_to_chat(
                client, 4242, Path(td) / "recording.ogg",
                Path(td) / "recording.json", metadata)

            self.assertEqual(metadata["delivery"]["status"], "skipped")
            self.assertEqual(metadata["delivery"]["error"],
                             "recording_is_not_complete")
            self.assertEqual(client.file_calls, [])


if __name__ == "__main__":
    unittest.main()
