#!/usr/bin/env python3
"""Focused regressions for the Telegram GPT Live voice agent.

Every case here is a defect this provider actually shipped into a live call
before it was written down. They are kept as tests because each was invisible
to reading and obvious the moment a phone rang: an un-awaited coroutine, a
contract the daemon reads by name, a prompt that points at material the prompt
builder drops. The provider talks to two things it cannot run in a test - a
websocket and a phone call - so what is checked here is everything on this side
of both.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import inspect
import sys
import tempfile
import types
import unittest
from unittest import mock
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

TELEGRAM_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = TELEGRAM_DIR / "service"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import CLI_PATH  # noqa: E402


class DummyType:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)


class NotInCallError(Exception):
    pass


@contextmanager
def fake_runtime_modules():
    """The provider needs the pytgcalls names it sends frames with, and the
    openai package only at the moment it opens a session, which no test does."""
    names = ("pytgcalls", "pytgcalls.exceptions", "pytgcalls.types", "ntgcalls",
             "telethon", "telethon.tl", "telethon.tl.types", "openai")
    saved = {name: sys.modules.get(name) for name in names}

    pytgcalls = types.ModuleType("pytgcalls")
    exceptions = types.ModuleType("pytgcalls.exceptions")
    exceptions.NotInCallError = NotInCallError
    pytgcalls_types = types.ModuleType("pytgcalls.types")

    class Device:
        MICROPHONE = "microphone"

    class Frame:
        class Info:
            def __init__(self, capture_time=0, **kwargs):
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

    openai = types.ModuleType("openai")
    openai.AsyncOpenAI = DummyType

    sys.modules.update({
        "pytgcalls": pytgcalls,
        "pytgcalls.exceptions": exceptions,
        "pytgcalls.types": pytgcalls_types,
        "ntgcalls": types.ModuleType("ntgcalls"),
        "telethon": telethon,
        "telethon.tl": telethon_tl,
        "telethon.tl.types": telethon_tl_types,
        "openai": openai,
    })
    try:
        yield Frame
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def import_module_at(name, path):
    sys.path.insert(0, str(SERVICE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def import_gptlive():
    """The provider, with the shared module under the name it imports it by."""
    import_module_at("voice_agent", SERVICE_DIR / "voice_agent.py")
    return import_module_at("gptlive_session_under_test",
                            SERVICE_DIR / "gptlive" / "session.py")


class RecordingCalls:
    """Stands in for PyTgCalls, keeping every outbound frame and its stamp.

    `send_frame` is a coroutine on the real object. It is one here too, because
    the first live call on this provider was silent: the pump built a frame every
    ten milliseconds, called this without awaiting it, and threw the coroutine
    away. Nothing raised, and nothing was heard."""

    def __init__(self):
        self.sent = []
        self.stamps = []

    async def send_frame(self, chat_id, device, data, frame_info):
        self.sent.append(data)
        self.stamps.append(getattr(frame_info, "capture_time", None))


def build_session(module, **overrides):
    kwargs = dict(
        api_key="test-key",
        model=None,
        voice=None,
        system_instruction="project prompt",
        caller_name="Caller",
        assistant_name="Assistant",
        log=lambda *_: None,
    )
    kwargs.update(overrides)
    return module.VoiceCallSession(RecordingCalls(), 42, **kwargs)


class MediaTests(unittest.TestCase):
    def test_pump_awaits_send_frame_so_audio_actually_leaves(self):
        with fake_runtime_modules():
            module = import_gptlive()

            async def run():
                session = build_session(module)
                session.start_pump()
                await asyncio.sleep(0.05)
                await session.stop()
                return session._calls

            calls = asyncio.run(run())
        self.assertTrue(calls.sent, "the pump sent no frame at all")
        self.assertTrue(all(isinstance(frame, bytes) for frame in calls.sent),
                        "a coroutine reached the call instead of audio bytes")

    def test_capture_time_is_whole_milliseconds(self):
        with fake_runtime_modules():
            module = import_gptlive()

            async def run():
                session = build_session(module)
                session.start_pump()
                await asyncio.sleep(0.05)
                await session.stop()
                return session._calls

            calls = asyncio.run(run())
        stamps = [stamp for stamp in calls.stamps if stamp is not None]
        self.assertTrue(stamps)
        for stamp in stamps:
            self.assertIsInstance(stamp, int)
            # Seconds-as-a-float would land around 1.7e9; milliseconds are 1e12.
            self.assertGreater(stamp, 10 ** 11)

    def test_a_late_tick_is_repaid_rather_than_lost(self):
        """Every GPT Live call sent its voice at 55-66% of real time: the pump
        restarted its schedule after any slot that ran over, so the time was
        never paid back, and a stream that never pauses piled up a backlog
        that reached a minute and a half by the end of a call."""

        class SlowCalls(RecordingCalls):
            async def send_frame(self, chat_id, device, data, frame_info):
                await super().send_frame(chat_id, device, data, frame_info)
                if len(self.sent) % 4 == 0:
                    await asyncio.sleep(0.025)

        with fake_runtime_modules():
            module = import_gptlive()

            async def run():
                session = module.VoiceCallSession(
                    SlowCalls(), 42, api_key="k", model=None, voice=None,
                    system_instruction="p", caller_name="C",
                    log=lambda *_: None)
                loop = asyncio.get_running_loop()
                started = loop.time()
                session.start_pump()
                await asyncio.sleep(0.6)
                elapsed = loop.time() - started
                await session.stop()
                return len(session._calls.sent), elapsed

            sent, elapsed = asyncio.run(run())
        self.assertGreaterEqual(sent, 0.9 * elapsed / module.FRAME_SECONDS)

    def test_a_backlog_is_paid_back_from_silence(self):
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module)
            frame = module.AGENT_FRAME_BYTES
            speech = (b"\x00\x40" * (frame // 2)) * 50
            session._outbound.extend(module.AGENT_SILENCE * 100)
            session._outbound.extend(speech)
            session._trim_outbound_silence()
        self.assertEqual(bytes(session._outbound), speech)
        self.assertEqual(session.trimmed_silence_frames, 100)

    def test_speech_is_never_trimmed(self):
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module)
            frame = module.AGENT_FRAME_BYTES
            held = (b"\x00\x40" * (frame // 2)) * 100 + module.AGENT_SILENCE * 100
            session._outbound.extend(held)
            session._trim_outbound_silence()
        self.assertEqual(bytes(session._outbound), held)
        self.assertEqual(session.trimmed_silence_frames, 0)

    def test_a_line_carrying_only_silence_is_quiet(self):
        """Every GPT Live hang-up waited out its 30 s cap: the stream never
        stops and the buffer is trimmed only to about 0.2 s of the model's own
        silence, so a line that waited for an empty buffer never saw one."""
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module)
            session._outbound.extend(module.AGENT_SILENCE * 20)
            self.assertTrue(session.line_quiet())

    def test_a_line_with_speech_still_queued_is_not_quiet(self):
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module)
            frame = module.AGENT_FRAME_BYTES
            session._outbound.extend(module.AGENT_SILENCE * 20)
            session._outbound.extend(b"\x00\x40" * (frame // 2))
            session._outbound.extend(module.AGENT_SILENCE * 5)
            self.assertFalse(session.line_quiet())

    def test_a_held_loop_is_measured_and_named(self):
        """A GPT Live call sounded metallic, stuttered and answered seconds late
        while its recording, written at the moment of sending, was clean. What
        the phone hears is the time between frames handed to ntgcalls, and a
        hold of the process puts a gap and then a burst there; the call has to
        say so, and say what held it."""
        import gc
        import time as clock
        lines = []

        def hold_the_loop():
            clock.sleep(0.25)

        with fake_runtime_modules():
            module = import_gptlive()

            async def run():
                session = build_session(module, log=lines.append)
                session.start_pump()
                await asyncio.sleep(0.1)
                hold_the_loop()
                await asyncio.sleep(0.1)
                summary = await session.stop()
                return session, summary

            session, summary = asyncio.run(run())
        media = summary["media"]
        self.assertEqual(media["loop_stalls"]["count"], 1)
        self.assertGreaterEqual(media["loop_stalls"]["max_ms"], 200)
        self.assertIn("hold_the_loop", media["loop_stalls"]["worst_in"])
        self.assertGreaterEqual(media["send"]["gaps_over_100ms"], 1)
        self.assertGreaterEqual(media["send"]["gap_max_ms"], 200)
        self.assertTrue(any("event loop held" in line and "hold_the_loop" in line
                            for line in lines), lines)
        self.assertTrue(any(line.startswith("voice: media at the ntgcalls boundary")
                            for line in lines), lines)
        self.assertNotIn(session._watch._on_gc, gc.callbacks)

    def test_a_hold_that_keeps_the_interpreter_still_says_what_held_it(self):
        """In the owner's call every hold was "not sampled": a thread held the
        interpreter, so nothing in Python could look. Each hold carries what
        the process did during it and the hand-over to the worker beside it.
        It takes no faulthandler dump: re-arming one on every beat waited on a
        dump that never finished, and the call's event loop stopped for good."""
        import faulthandler
        import re as regex
        import threading as threads
        lines = []
        pattern = regex.compile(r"(a+)+b")

        held, release = threads.Event(), threads.Event()

        def busy_in_c():
            # A backtracking match runs in C and never lets the interpreter go.
            # The thread then stays, as a pool's worker does.
            pattern.match("a" * 23)
            held.set()
            release.wait(5)

        with fake_runtime_modules():
            module = import_gptlive()

            async def run():
                session = build_session(module, log=lines.append)
                session.start_pump()
                await asyncio.sleep(0.1)
                session.note_handed_over()
                worker = threads.Thread(target=busy_in_c, name="worker-start")
                worker.start()
                await asyncio.to_thread(held.wait, 5)
                await asyncio.sleep(0.1)
                try:
                    return await session.stop()
                finally:
                    release.set()
                    worker.join()

            with mock.patch.object(faulthandler, "dump_traceback_later") as dump:
                summary = asyncio.run(run())
        dump.assert_not_called()
        stalls = summary["media"]["loop_stalls"]
        self.assertGreaterEqual(stalls["count"], 1, lines)
        event = max(stalls["events"], key=lambda item: item["held_ms"])
        self.assertGreaterEqual(event["held_ms"], 200)
        self.assertGreaterEqual(event["cpu_ms"], event["held_ms"] * 0.5)
        self.assertEqual(event["handover"], 1)
        self.assertGreater(event["after_handover_s"] * 1000, -event["held_ms"])
        self.assertNotIn("threads", event)
        self.assertEqual(len(summary["media"]["handovers_at_s"]), 1)
        self.assertTrue(any("event loop held" in line and "process cpu" in line
                            and "hand-over 1 to the worker" in line
                            for line in lines), lines)

    def test_stall_evidence_failing_breaks_neither_the_call_nor_its_stop(self):
        """The evidence is extra: when the process usage cannot be read, frames
        still go and the call stops."""
        import time as clock
        lines = []
        with fake_runtime_modules():
            module = import_gptlive()

            async def run():
                session = build_session(module, log=lines.append)
                with mock.patch.object(module, "_usage", lambda: None):
                    session.start_pump()
                    await asyncio.sleep(0.05)
                    clock.sleep(0.2)
                    await asyncio.sleep(0.05)
                    sent = len(session._calls.sent)
                    summary = await session.stop()
                return sent, summary

            sent, summary = asyncio.run(run())
        self.assertGreater(sent, 5)
        event = summary["media"]["loop_stalls"]["events"][0]
        self.assertNotIn("cpu_ms", event)
        self.assertTrue(any("event loop held" in line for line in lines), lines)

    def test_late_bursts_are_played_from_a_cushion_without_holes(self):
        """A call counted 85 underrun frames in 3 minutes: the pump played each
        burst of the model's audio the moment it came, so a burst more than ten
        milliseconds late put silence inside a word. It now plays from a cushion
        of OUTBOUND_PREFILL_BYTES, and fills it again after running dry.

        Time is virtual here - only the pump's sleeps advance it - so the
        jitter is exact and the result does not depend on the machine's load."""
        import base64
        chunk_seconds = 0.02
        first = b"\x00\x40"   # a voiced sample, one per utterance, told apart
        second = b"\x00\x30"
        with fake_runtime_modules():
            module = import_gptlive()
            chunk = int(module.AGENT_RATE * 2 * chunk_seconds) // 2
            arrivals, last = [], 0.0
            for k in range(60):  # 1.2 s of speech, each chunk up to 60 ms late
                last = max(last, 0.1 + k * chunk_seconds + (k * 37 % 61) / 1000)
                arrivals.append((last, first * chunk))
            resume = last + 0.3  # the model sends nothing, then speaks again
            arrivals += [(resume + k * chunk_seconds, second * chunk) for k in range(10)]
            now = [0.0]
            clock = types.SimpleNamespace(monotonic=lambda: now[0],
                                          time=lambda: 1.7e9 + now[0])
            real_sleep = asyncio.sleep

            async def virtual_sleep(delay, *args):
                now[0] += max(delay, 0)
                await real_sleep(0)

            async def run():
                session = build_session(module)
                session.start_pump()
                for at, payload in arrivals:
                    while now[0] < at:
                        await real_sleep(0)
                    session._on_output_audio(types.SimpleNamespace(
                        delta=base64.b64encode(payload).decode()))
                while now[0] < resume + 0.4:
                    await real_sleep(0)
                await session.stop()
                return session

            with mock.patch.object(module, "time", clock), \
                    mock.patch.object(module.asyncio, "sleep", virtual_sleep):
                session = asyncio.run(run())
        sent = session._calls.sent
        voice = [frame[:2] for frame in sent]
        for sample in (first, second):
            at = [n for n, head in enumerate(voice) if head == sample]
            self.assertEqual(at, list(range(at[0], at[-1] + 1)),
                             f"silence inside speech {sample!r}")
            self.assertEqual(len(at), sum(len(p) for _, p in arrivals
                                          if p[:2] == sample) // module.AGENT_FRAME_BYTES)
        # The second utterance waited, in silence, for its own cushion.
        start = int(round(resume / module.FRAME_SECONDS))
        played = voice.index(second)
        cushion = module.OUTBOUND_PREFILL_BYTES / (module.AGENT_RATE * 2)
        self.assertGreaterEqual(played - start, int(cushion / module.FRAME_SECONDS) - 3)
        self.assertTrue(all(sent[n] == module.AGENT_SILENCE for n in range(start, played)))

    def test_missing_and_zero_caller_frames_are_counted(self):
        with fake_runtime_modules() as Frame:
            module = import_gptlive()
            session = build_session(module)
            speech = b"\x00\x40" * (module.CALLER_FRAME_BYTES // 2)
            session.on_incoming_frames([Frame(frame=speech)])
            import time as clock
            clock.sleep(0.1)
            session.on_incoming_frames([Frame(frame=bytes(module.CALLER_FRAME_BYTES))])
            inbound = session.media_report()["inbound"]
        self.assertEqual(inbound["frames"], 2)
        self.assertEqual(inbound["zero_frames"], 1)
        self.assertGreaterEqual(inbound["missing_frames"], 8)
        self.assertGreaterEqual(inbound["gaps_over_50ms"], 1)

    def test_an_empty_first_batch_of_caller_frames_breaks_nothing(self):
        """ntgcalls can hand the frame callback an empty list; the caller's
        audio after it still reaches the call, and the call still stops."""
        with fake_runtime_modules() as Frame:
            module = import_gptlive()
            speech = b"\x00\x40" * (module.CALLER_FRAME_BYTES // 2)

            async def run():
                session = build_session(module)
                session.on_incoming_frames([])
                for _ in range(3):
                    session.on_incoming_frames([Frame(frame=speech)])
                return session, await session.stop()

            session, summary = asyncio.run(run())
        self.assertEqual(session.caller_bytes, 3 * len(speech))
        self.assertEqual(summary["media"]["inbound"]["frames"], 3)

    def test_one_rate_carries_the_whole_tract(self):
        with fake_runtime_modules():
            module = import_gptlive()
        self.assertEqual(module.CALLER_RATE, 24000)
        self.assertEqual(module.AGENT_RATE, 24000)
        self.assertEqual(module.AGENT_FRAME_BYTES, 480)
        self.assertEqual(module.INPUT_CHUNK_BYTES, 4800)



class FakeLive:
    """A Live connection that answers `session.start` and then streams the
    events a test puts on it. `starts` False makes the start fail the way a
    socket does when the caller hangs up while the phone still rings."""

    def __init__(self, starts=True):
        self.starts = starts
        self.said = []
        self.events = asyncio.Queue()

        class Channel:
            def __init__(channel, name):
                channel.name = name

            async def append(channel, **kwargs):
                self.said.append((channel.name, kwargs))

        self.session = types.SimpleNamespace(
            start=self._start,
            commentary=Channel("commentary"),
            instructions=Channel("instructions"),
            input_audio=Channel("input_audio"),
        )

    async def _start(self, session):
        self.said.append(("start", session))
        if not self.starts:
            raise ConnectionError("closed while ringing")
        await self.events.put(types.SimpleNamespace(
            type="session.started", session=types.SimpleNamespace(id="live-1")))

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return event

    def client(self):
        live = self

        @asynccontextmanager
        async def connect():
            yield live

        class Client:
            def __init__(client, **kwargs):
                client.live = types.SimpleNamespace(connect=connect)

        return Client


class PrepareBeforePickupTests(unittest.TestCase):
    """The pickup used to be followed by three or four seconds of silence: the
    Live session was opened, configured and asked to greet only after the call
    was answered. The daemon now does all of that while the phone rings, which
    needs the session opened apart from its greeting."""

    def test_the_session_is_configured_and_greeting_before_any_audio_leaves(self):
        live = FakeLive()
        with fake_runtime_modules():
            module = import_gptlive()
            sys.modules["openai"].AsyncOpenAI = live.client()

            async def run():
                session = build_session(module)
                await session.connect_agent()
                await session.greet()
                said = [name for name, _ in live.said]
                sent_before_pump = list(session._calls.sent)
                # The greeting's audio arrives while the phone still rings and
                # waits for the pump, which starts after the pickup.
                voiced = (b"\x00\x40" * (module.AGENT_FRAME_BYTES // 2)) * 10
                await live.events.put(types.SimpleNamespace(
                    type="session.output_audio.delta",
                    delta=base64.b64encode(voiced).decode("ascii")))
                await asyncio.sleep(0.02)
                buffered = len(session._outbound)
                session.start_pump()
                await asyncio.sleep(0.2)
                await live.events.put(None)
                await session.stop()
                return said, sent_before_pump, buffered, session._calls.sent

            said, sent_before_pump, buffered, sent = asyncio.run(run())
        self.assertEqual(said[:2], ["start", "commentary"])
        self.assertIn("project prompt", live.said[0][1]["instructions"])
        self.assertEqual(sent_before_pump, [])
        self.assertEqual(buffered, module.AGENT_FRAME_BYTES * 10)
        self.assertIn(b"\x00\x40" * (module.AGENT_FRAME_BYTES // 2), sent)

    def test_a_session_that_ends_before_it_is_ready_frees_the_line_at_once(self):
        """Seen live: the caller hung up while it rang, the Live session ended
        at once, and the old wait held the line for the whole connect timeout,
        so the next call was refused as one already in progress."""
        live = FakeLive(starts=False)
        with fake_runtime_modules():
            module = import_gptlive()
            sys.modules["openai"].AsyncOpenAI = live.client()

            async def run():
                session = build_session(module)
                started = asyncio.get_running_loop().time()
                try:
                    await asyncio.wait_for(session.connect_agent(), 2.0)
                except module.common.VoiceAgentError as exc:
                    error = str(exc)
                else:
                    error = None
                elapsed = asyncio.get_running_loop().time() - started
                await session.stop()
                return error, elapsed

            error, elapsed = asyncio.run(run())
        self.assertEqual(error, "gpt-live session ended before it was ready")
        self.assertLess(elapsed, 1.0)

    def test_waiting_for_the_phone_ends_at_its_first_audio_or_the_bound(self):
        """The greeting waits for the phone after the pickup: one spoken while
        the phone still plays nothing is lost."""
        with fake_runtime_modules() as Frame:
            module = import_gptlive()

            async def run():
                session = build_session(module)
                silent = await session.wait_for_caller_audio(0.1)
                session.on_incoming_frames([Frame(frame=b"\x00\x01" * 240)])
                started = asyncio.get_running_loop().time()
                heard = await session.wait_for_caller_audio(5.0)
                return silent, heard, asyncio.get_running_loop().time() - started

            silent, heard, waited = asyncio.run(run())
        self.assertFalse(silent)
        self.assertTrue(heard)
        self.assertLess(waited, 1.0)

    def test_start_agent_still_connects_then_greets(self):
        live = FakeLive()
        with fake_runtime_modules():
            module = import_gptlive()
            sys.modules["openai"].AsyncOpenAI = live.client()

            async def run():
                session = build_session(module)
                await session.start_agent()
                await live.events.put(None)
                await session.stop()

            asyncio.run(run())
        self.assertEqual([name for name, _ in live.said][:2], ["start", "commentary"])


class EndpointTests(unittest.TestCase):
    """OPENAI_BASE_URL names the endpoint a call is reached at, such as the EU
    one; unset, the client is built as it always was. The log names the host
    and never the key."""

    def _connect(self, **overrides):
        live = FakeLive()
        built, lines = [], []
        with fake_runtime_modules():
            module = import_gptlive()
            Client = live.client()

            class Recording(Client):
                def __init__(client, **kwargs):
                    built.append(kwargs)
                    super().__init__(**kwargs)

            sys.modules["openai"].AsyncOpenAI = Recording

            async def run():
                session = build_session(module, log=lambda line: lines.append(str(line)),
                                        **overrides)
                await session.connect_agent()
                await live.events.put(None)
                await session.stop()

            asyncio.run(run())
        return built, lines

    def test_unset_keeps_the_client_as_it_was(self):
        built, lines = self._connect()
        self.assertEqual(built, [{"api_key": "test-key"}])
        self.assertFalse([line for line in lines if "endpoint" in line])

    def test_a_named_endpoint_is_the_one_dialled_and_only_its_host_is_logged(self):
        built, lines = self._connect(base_url="https://eu.api.openai.com/v1")
        self.assertEqual(built, [{"api_key": "test-key",
                                  "base_url": "https://eu.api.openai.com/v1"}])
        self.assertIn("gpt-live: endpoint eu.api.openai.com", lines)
        self.assertFalse([line for line in lines if "test-key" in line])


class SessionContractTests(unittest.TestCase):
    """What the daemon reads off a session when a call ends.

    A missing key here surfaced only as an unhandled exception inside a Telegram
    update handler, with the traceback swallowed, and cost a call to find."""

    REQUIRED_SUMMARY_KEYS = {
        "transcript", "pump_error", "interruptions", "dropped_input_chunks",
        "caller_seconds", "agent_seconds", "agent_voiced_seconds",
        "messages_sent", "tasks",
    }

    def test_stop_answers_with_the_summary(self):
        with fake_runtime_modules():
            module = import_gptlive()
            summary = asyncio.run(build_session(module).stop())
        self.assertIsInstance(summary, dict)
        self.assertLessEqual(self.REQUIRED_SUMMARY_KEYS, set(summary))

    def test_tracks_is_a_property_not_a_method(self):
        with fake_runtime_modules():
            module = import_gptlive()
        self.assertIsInstance(
            inspect.getattr_static(module.VoiceCallSession, "tracks"), property)

    def test_stopping_seals_the_tracks_to_the_call(self):
        with fake_runtime_modules():
            module = import_gptlive()
            with tempfile.TemporaryDirectory() as tmp:
                caller = Path(tmp) / "caller.pcm"
                agent = Path(tmp) / "agent.pcm"

                async def run():
                    session = build_session(module, caller_track=caller,
                                            agent_track=agent)
                    session.start_pump()
                    await asyncio.sleep(0.05)
                    await session.stop()
                    return session

                session = asyncio.run(run())
                self.assertIsNotNone(
                    session.window_seconds,
                    "the window never closed, so neither track knows the "
                    "call's length and the stereo join cannot align them")
                self.assertEqual(len(session.tracks), 2)

    def test_stopping_hands_unfinished_work_to_the_chat(self):
        """The prompt promises the caller this, so the teardown has to keep it."""
        with fake_runtime_modules():
            module = import_gptlive()
            delivered = []

            async def run():
                runner = sys.modules["voice_agent"].VoiceTaskRunner(
                    lambda text: asyncio.sleep(0),
                    lambda completion: delivered.append(completion),
                    log=lambda *_: None)
                runner.completions.put_nowait({"job_id": "task-1", "ok": True,
                                               "result": "done"})
                session = build_session(module, task_runner=runner)
                await session.stop()

            asyncio.run(run())
        self.assertEqual([c["job_id"] for c in delivered], ["task-1"])


class ProviderDeclarationTests(unittest.TestCase):
    def test_delegation_is_how_this_provider_reaches_the_worker(self):
        """Not a tool, so not the tool set's decision.

        The provider declares no tools, and the daemon withheld the task runner
        because of it - leaving a call whose only possible answer was that it
        could not have anything done."""
        with fake_runtime_modules():
            import_module_at("voice_agent", SERVICE_DIR / "voice_agent.py")
            package = import_module_at("gptlive_package_under_test",
                                       SERVICE_DIR / "gptlive" / "__init__.py")
        self.assertEqual(package.TOOL_NAMES, ())
        self.assertTrue(package.DELEGATES_TO_WORKER)

    def test_the_other_provider_hands_the_turn_over_too(self):
        """Gemini reaches the worker through its one `delegate` tool, with the
        same frame, so it is given the runner whatever the tool set says."""
        with fake_runtime_modules():
            shared = import_module_at("voice_agent", SERVICE_DIR / "voice_agent.py")
            module = import_module_at("gptlive_session_under_test",
                                      SERVICE_DIR / "gptlive" / "session.py")
        self.assertTrue(shared.DELEGATES_TO_WORKER)
        self.assertEqual(shared.DELEGATE_TOOL["name"], "delegate")
        self.assertIs(module.HANDED_TURN_REQUEST, shared.HANDED_TURN_REQUEST)


class ForeignVocabularyTests(unittest.TestCase):
    """A voice or model named for the other provider ends the session at start,
    and the API's own types refuse neither: both are plain strings there."""

    def test_a_foreign_voice_is_replaced(self):
        with fake_runtime_modules():
            module = import_gptlive()
        self.assertEqual(module.resolve_voice("Algenib", log=lambda *_: None),
                         module.DEFAULT_VOICE)
        self.assertEqual(module.resolve_voice("Aoede", log=lambda *_: None),
                         module.DEFAULT_VOICE)

    def test_a_voice_this_stack_has_is_kept(self):
        with fake_runtime_modules():
            module = import_gptlive()
        for name in ("cedar", "stone", "marin", "CEDAR"):
            self.assertEqual(module.resolve_voice(name, log=lambda *_: None),
                             name.lower())

    def test_a_custom_voice_passes_through(self):
        with fake_runtime_modules():
            module = import_gptlive()
        voice = {"id": "voice_abc"}
        self.assertEqual(module.resolve_voice(voice, log=lambda *_: None), voice)

    def test_a_foreign_model_is_replaced(self):
        with fake_runtime_modules():
            module = import_gptlive()
        self.assertEqual(
            module.resolve_model("gemini-3.1-flash-live-preview", log=lambda *_: None),
            module.DEFAULT_MODEL)
        self.assertEqual(module.resolve_model("gpt-live-1", log=lambda *_: None),
                         "gpt-live-1")


class SessionConfigTests(unittest.TestCase):
    def test_the_config_opens_a_client_delegation_at_one_rate(self):
        with fake_runtime_modules():
            module = import_gptlive()
            config = build_session(module, voice="cedar")._session_config()
        self.assertEqual(config["delegation"], {"type": "client"})
        self.assertEqual(config["audio"]["format"],
                         {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(config["audio"]["output"]["voice"], "cedar")

    def test_the_call_facts_precede_the_project_prompt(self):
        with fake_runtime_modules():
            module = import_gptlive()
            config = build_session(module)._session_config()
        instructions = config["instructions"]
        self.assertIn("You are Assistant.", instructions)
        self.assertIn("Caller is calling you", instructions)
        self.assertLess(instructions.index("You are Assistant."),
                        instructions.index("project prompt"))


class AppendTests(unittest.TestCase):
    def test_a_long_answer_is_split_under_the_cap(self):
        with fake_runtime_modules():
            module = import_gptlive()
        body = ("Sentence number one is here. " * 200).strip()
        pieces = module.chunk_for_append(body)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece), module.APPEND_CHAR_BUDGET)
        self.assertEqual("".join(p.replace(" ", "") for p in pieces),
                         body.replace(" ", ""))

    def test_a_short_answer_is_one_piece(self):
        with fake_runtime_modules():
            module = import_gptlive()
        self.assertEqual(module.chunk_for_append("Short answer."),
                         ["Short answer."])
        self.assertEqual(module.chunk_for_append("   "), [])


class ProgressTests(unittest.TestCase):
    def test_notes_are_kept_in_the_shape_the_shared_digest_folds(self):
        """Bare strings here made the first progress flush of every call raise."""
        with fake_runtime_modules():
            module = import_gptlive()
            shared = sys.modules["voice_agent"]
            session = build_session(module)
            session.note_progress("reading files", "stream")
            session.note_progress("checked the register", "worker")
        self.assertTrue(all(isinstance(item, tuple) and len(item) == 2
                            for item in session._progress_window))
        self.assertEqual(shared.summarize_progress(session._progress_window),
                         "checked the register")

    def test_a_cadence_means_the_call_is_narrated(self):
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module, progress_interval=6)
        self.assertTrue(session._announces_progress)
        self.assertEqual(session._progress_interval, 6)

    def test_zero_keeps_the_assistant_told_and_quiet(self):
        """Off is not deaf: a caller who asks what is taking so long is owed a
        real answer, which is only possible if the assistant was still told."""
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module, progress_interval=0)
        self.assertFalse(session._announces_progress)
        self.assertEqual(session._progress_interval,
                         module.QUIET_PROGRESS_SECONDS)

    def test_an_unset_cadence_falls_back_to_the_shared_default(self):
        with fake_runtime_modules():
            module = import_gptlive()
            shared = sys.modules["voice_agent"]
            session = build_session(module, progress_interval=None)
        self.assertTrue(session._announces_progress)
        self.assertEqual(session._progress_interval,
                         shared.DEFAULT_PROGRESS_INTERVAL)


class ConversationTailTests(unittest.TestCase):
    def test_the_tail_names_both_speakers_for_the_worker_prompt(self):
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module)
            session._record_fragment("caller", "how many are there")
            session._record_fragment("agent", "one moment")
        tail = session.conversation_tail()
        self.assertEqual([row["is_assistant"] for row in tail], [False, True])
        self.assertEqual([row["sender"] for row in tail], ["Caller", "Assistant"])
        for row in tail:
            self.assertTrue(row["text"].strip())
            self.assertIn("id", row)

    def test_ids_count_from_the_start_of_the_call(self):
        """So a later hand-over can say which lines are new."""
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module)
            session._turns = [{"speaker": "caller" if n % 2 else "agent",
                               "text": f"line {n}", "at": 0.0} for n in range(1, 66)]
        tail = session.conversation_tail()
        self.assertEqual(len(tail), 60)
        self.assertEqual((tail[0]["id"], tail[0]["text"]), (6, "line 6"))
        self.assertEqual((tail[-1]["id"], tail[-1]["text"]), (65, "line 65"))

    def test_the_agent_speaking_closes_the_caller_s_open_turn(self):
        """Otherwise a reaction to the last answer rides along with the next ask."""
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module)
            session._on_input_transcript(types.SimpleNamespace(delta="nice, thanks"))
            session._record_fragment("agent", "glad to help")
            session._on_input_transcript(types.SimpleNamespace(delta="now check X"))
        self.assertEqual(session._ask_text(), "now check X")

    def test_fragments_join_edge_to_edge(self):
        """They are pieces of words; a space between them breaks every one."""
        with fake_runtime_modules():
            module = import_gptlive()
            session = build_session(module)
            for delta in ("Can ", "you ", "check", " it"):
                session._on_input_transcript(types.SimpleNamespace(delta=delta))
        self.assertEqual(session._ask_text(), "Can you check it")


class PromptOwnershipTests(unittest.TestCase):
    """Where the line runs between what a project may write and what it may not.

    The test of that line is not an opinion: strip every project-owned document
    and the machinery has to still be there. A project can then ruin the
    assistant's character, which is its own business, without reaching anything
    that decides whether work leaves the call at all."""

    MECHANISM = SERVICE_DIR / "gptlive" / "prompts" / "delegation.md"

    def test_the_mechanism_ships_in_the_bundle_not_the_envelope(self):
        self.assertTrue(self.MECHANISM.is_file())
        body = self.MECHANISM.read_text()
        for owed in ("Handing work over", "While it works", "When it comes back"):
            self.assertIn(owed, body)

    def test_gemini_ships_a_mechanism_of_its_own_for_its_one_tool(self):
        """Read the way GPT Live's is: `<provider>/prompts/delegation.md`."""
        mechanism = SERVICE_DIR / "gemini" / "prompts" / "delegation.md"
        self.assertTrue(mechanism.is_file())
        body = mechanism.read_text()
        for owed in ("Handing work over", "While it works", "When it comes back",
                     "`delegate`"):
            self.assertIn(owed, body)
        source = (SERVICE_DIR / "daemon.py").read_text()
        self.assertIn('HERE / provider / "prompts" / "delegation.md"', source)

    def test_nothing_seeds_the_mechanism_into_a_project(self):
        """A seeded copy is an editable copy, whatever the prose asks."""
        seeded = (SERVICE_DIR / "gptlive" / "templates").glob("*.md")
        for template in seeded:
            self.assertNotIn("Handing work over", template.read_text(),
                             f"{template.name} would put the mechanism where a "
                             "project can rewrite it")

    def test_a_project_owned_document_holds_no_mechanism(self):
        policy = (SERVICE_DIR / "gptlive" / "templates" / "delegation.md").read_text()
        for wire_fact in ("cannot see this call", "one at a time",
                          "not for you to say"):
            self.assertNotIn(wire_fact, policy)

    def test_the_mechanism_is_read_after_the_project_s_own_words(self):
        """Later prose wins where two pieces disagree, and this is the piece
        that cannot be allowed to lose."""
        source = (SERVICE_DIR / "daemon.py").read_text()
        start = source.index("def read_voice_context(")
        body = source[start:source.index("\ndef ", start + 10)]
        self.assertLess(body.index('read_voice_document("voice-agent"'),
                        body.index("read_voice_mechanism("))

    def test_the_provider_comes_first_in_a_key(self):
        """The key says who the instructions are addressed to."""
        schema_source = (SERVICE_DIR / "settings_schema.py").read_text()
        self.assertIn("VOICE_PROVIDER_DOCUMENTS", schema_source)
        cli = CLI_PATH.read_text()
        self.assertIn('key = f"{provider}-{kind}"', cli)


class TaskRunnerSignatureTests(unittest.TestCase):
    """This provider's request text is the same frame every time, so keying the
    one-at-a-time rule by it would refuse every delegation after the first."""

    def test_an_explicit_signature_separates_identical_requests(self):
        with fake_runtime_modules():
            shared = import_module_at("voice_agent", SERVICE_DIR / "voice_agent.py")

            async def run():
                runner = shared.VoiceTaskRunner(
                    lambda text: asyncio.sleep(3600),
                    lambda completion: None, log=lambda *_: None)
                first = runner.start("the same frame", signature="delegation-1")
                second = runner.start("the same frame", signature="delegation-2")
                for task in list(runner._jobs.values()):
                    task.cancel()
                return first, second

            first, second = asyncio.run(run())
        self.assertEqual(first["status"], "started")
        # Refused for being second, not for looking like the first.
        self.assertEqual(second["status"], "busy")

    def test_without_one_the_text_is_still_the_key(self):
        with fake_runtime_modules():
            shared = import_module_at("voice_agent", SERVICE_DIR / "voice_agent.py")

            async def run():
                runner = shared.VoiceTaskRunner(
                    lambda text: asyncio.sleep(3600),
                    lambda completion: None, log=lambda *_: None)
                first = runner.start("look at the register")
                second = runner.start("look at the register")
                for task in list(runner._jobs.values()):
                    task.cancel()
                return first, second

            first, second = asyncio.run(run())
        self.assertEqual(first["status"], "started")
        self.assertEqual(second["status"], "already_running")


if __name__ == "__main__":
    unittest.main(verbosity=1)
