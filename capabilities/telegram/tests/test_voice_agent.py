#!/usr/bin/env python3
"""Focused regressions for the Telegram Gemini Live voice agent."""

from __future__ import annotations

import asyncio
import importlib.util
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path


TELEGRAM_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = TELEGRAM_DIR / "service"


class DummyType:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)


class NotInCallError(Exception):
    pass


@contextmanager
def fake_runtime_modules():
    """voice_agent only needs the pytgcalls names it sends frames with."""
    saved = {name: sys.modules.get(name)
             for name in ("pytgcalls", "pytgcalls.exceptions", "pytgcalls.types",
                          "telethon", "telethon.tl", "telethon.tl.types")}

    pytgcalls = types.ModuleType("pytgcalls")
    exceptions = types.ModuleType("pytgcalls.exceptions")
    exceptions.NotInCallError = NotInCallError
    pytgcalls_types = types.ModuleType("pytgcalls.types")

    class Device:
        MICROPHONE = "microphone"

    class Frame:
        class Info:
            def __init__(self, capture_time=0, width=0, height=0, rotation=0):
                self.capture_time = capture_time

        def __init__(self, ssrc=0, frame=b"", info=None):
            self.ssrc = ssrc
            self.frame = frame
            self.info = info

    pytgcalls_types.Device = Device
    pytgcalls_types.Frame = Frame

    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = DummyType
    telethon_tl = types.ModuleType("telethon.tl")
    telethon_tl_types = types.ModuleType("telethon.tl.types")
    telethon_tl_types.DocumentAttributeAudio = DummyType
    telethon_tl_types.DocumentAttributeFilename = DummyType

    sys.modules.update({
        "pytgcalls": pytgcalls,
        "pytgcalls.exceptions": exceptions,
        "pytgcalls.types": pytgcalls_types,
        "telethon": telethon,
        "telethon.tl": telethon_tl,
        "telethon.tl.types": telethon_tl_types,
    })
    try:
        yield Frame
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def import_voice_agent():
    sys.path.insert(0, str(SERVICE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "voice_agent_under_test", SERVICE_DIR / "voice_agent.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class RecordingCalls:
    """Stands in for PyTgCalls: keeps every outbound frame the pump sends."""

    def __init__(self):
        self.sent = []

    async def send_frame(self, chat_id, device, data, frame_data):
        self.sent.append(data)


def tone_pcm(rate, seconds, frequency, amplitude):
    samples = int(rate * seconds)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * frequency * n / rate)))
        for n in range(samples)
    )


class VoiceAgentMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_outbound_pump_sends_silence_when_nothing_is_buffered(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            calls = RecordingCalls()
            session = va.VoiceCallSession(
                calls, 42, api_key="k", model=None, voice=None,
                system_instruction="s", caller_name="Caller", log=lambda *_: None)
            session.start_pump()
            await asyncio.sleep(0.12)
            await session.stop()

        self.assertGreater(len(calls.sent), 3)
        self.assertTrue(all(len(frame) == va.AGENT_FRAME_BYTES for frame in calls.sent))
        self.assertTrue(all(frame == va.AGENT_SILENCE for frame in calls.sent))

    async def test_buffered_agent_audio_is_paced_out_ahead_of_silence(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            calls = RecordingCalls()
            session = va.VoiceCallSession(
                calls, 42, api_key="k", model=None, voice=None,
                system_instruction="s", caller_name="Caller", log=lambda *_: None)
            session._outbound.extend(b"\x11" * (va.AGENT_FRAME_BYTES * 3))
            session.start_pump()
            await asyncio.sleep(0.12)
            summary = await session.stop()

        self.assertEqual(calls.sent[:3], [b"\x11" * va.AGENT_FRAME_BYTES] * 3)
        self.assertEqual(summary["agent_voiced_seconds"], 0.03)

    async def test_interruption_drops_queued_agent_speech(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            session = va.VoiceCallSession(
                RecordingCalls(), 42, api_key="k", model=None, voice=None,
                system_instruction="s", caller_name="Caller", log=lambda *_: None)
            session._outbound.extend(b"\x22" * 4800)
            session._consume(types.SimpleNamespace(
                server_content=types.SimpleNamespace(
                    interrupted=True, input_transcription=None,
                    output_transcription=None, model_turn=None)))

        self.assertEqual(len(session._outbound), 0)
        self.assertEqual(session.interruptions, 1)

    async def test_transcription_fragments_join_into_speaker_turns(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            session = va.VoiceCallSession(
                RecordingCalls(), 42, api_key="k", model=None, voice=None,
                system_instruction="s", caller_name="Caller", log=lambda *_: None)
            for speaker, text in (("in", "приве"), ("in", "т как"), ("in", " дела"),
                                  ("out", "Всё"), ("out", " хорошо")):
                session._consume(types.SimpleNamespace(
                    server_content=types.SimpleNamespace(
                        interrupted=False,
                        input_transcription=(types.SimpleNamespace(text=text)
                                             if speaker == "in" else None),
                        output_transcription=(types.SimpleNamespace(text=text)
                                              if speaker == "out" else None),
                        model_turn=None)))

        self.assertEqual(
            [(t["speaker"], t["text"]) for t in session.transcript()],
            [("caller", "привет как дела"), ("agent", "Всё хорошо")])

    async def test_inbound_frames_reach_the_track_and_the_gemini_queue(self):
        with fake_runtime_modules() as Frame:
            va = import_voice_agent()
            with tempfile.TemporaryDirectory() as tmp:
                caller_pcm = Path(tmp) / "caller.pcm"
                session = va.VoiceCallSession(
                    RecordingCalls(), 42, api_key="k", model=None, voice=None,
                    system_instruction="s", caller_name="Caller",
                    caller_track=caller_pcm, log=lambda *_: None)
                payload = b"\x33" * va.CALLER_FRAME_BYTES
                session.on_incoming_frames([Frame(frame=payload) for _ in range(10)])
                await session.stop()
                written = caller_pcm.read_bytes()

        self.assertEqual(session.caller_bytes, va.CALLER_FRAME_BYTES * 10)
        self.assertEqual(session._input_queue.qsize(), 1)
        self.assertTrue(written.endswith(payload * 10))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg/ffprobe not available")
class StereoJoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_tracks_join_into_a_separated_stereo_opus_file(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                caller_pcm, agent_pcm = root / "caller.pcm", root / "agent.pcm"
                # Distinct tones per side so channel separation is measurable.
                caller_pcm.write_bytes(tone_pcm(va.CALLER_RATE, 2.0, 300, 9000))
                agent_pcm.write_bytes(tone_pcm(va.AGENT_RATE, 2.0, 900, 3000))
                output = root / "call.ogg"
                result = await va.join_tracks_to_stereo(caller_pcm, agent_pcm, output)

                self.assertEqual(result["status"], "complete", result["error"])
                self.assertGreater(result["output_bytes"], 1024)
                self.assertAlmostEqual(result["duration_seconds"], 2.0, delta=0.3)
                self.assertFalse(caller_pcm.exists())
                self.assertFalse(agent_pcm.exists())

                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "a:0",
                     "-show_entries", "stream=channels", "-of", "csv=p=0", str(output)],
                    capture_output=True, text=True, check=True)
                self.assertEqual(probe.stdout.strip(), "2")

                levels = subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", str(output),
                     "-filter_complex",
                     "[0:a]channelsplit=channel_layout=stereo[l][r];"
                     "[l]astats=metadata=1:reset=0[la];[r]astats=metadata=1:reset=0[ra];"
                     "[la][ra]amix=inputs=2",
                     "-f", "null", "-"],
                    capture_output=True, text=True)
                self.assertEqual(levels.returncode, 0, levels.stderr)

    async def test_an_empty_track_is_never_joined_or_delivered(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                caller_pcm, agent_pcm = root / "caller.pcm", root / "agent.pcm"
                caller_pcm.write_bytes(b"\x00" * 320)
                agent_pcm.write_bytes(b"")
                result = await va.join_tracks_to_stereo(
                    caller_pcm, agent_pcm, root / "call.ogg")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "voice_track_is_empty")


class PromptTests(unittest.TestCase):
    def test_prompt_layers_voice_rules_over_project_context_and_history(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            prompt = va.build_system_prompt(
                "Assistant", "Caller", "# Channel instructions\nReply naturally.",
                "Caller: привет\nAssistant: привет")

        self.assertTrue(prompt.startswith(va.VOICE_PREAMBLE))
        self.assertIn("You are Assistant. The caller is Caller.", prompt)
        self.assertLess(
            prompt.index("Reply naturally."), prompt.index("Caller: привет"))
        self.assertIn("the spoken rules above", prompt)

    def test_prompt_omits_absent_context_and_history_blocks(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            prompt = va.build_system_prompt("Assistant", "Caller", "", "")

        self.assertNotIn("--- Project assistant instructions ---", prompt)
        self.assertNotIn("--- Recent messages in this direct chat ---", prompt)


if __name__ == "__main__":
    unittest.main()
