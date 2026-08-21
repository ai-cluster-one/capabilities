#!/usr/bin/env python3
"""Focused regressions for the Telegram Gemini Live voice agent."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
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
                          "ntgcalls", "telethon", "telethon.tl",
                          "telethon.tl.types")}

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

    # The daemon imports the compiled binding at module scope; nothing in the
    # voice path reads a name out of it.
    ntgcalls = types.ModuleType("ntgcalls")

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
        "ntgcalls": ntgcalls,
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


def marker_pcm(rate, seconds, marker_from, marker_to, frequency, amplitude):
    """Silence for the whole span except one tone burst, so the burst is the
    only energy in the track and its position is measurable after encoding."""
    samples = array("h", bytes(2 * int(rate * seconds)))
    for n in range(int(rate * marker_from), int(rate * marker_to)):
        samples[n] = int(amplitude * math.sin(2 * math.pi * frequency * n / rate))
    return samples.tobytes()


class FakeClock:
    """A monotonic clock the test drives, so track alignment is arithmetic
    rather than a race with the scheduler."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# One call replayed on the fake clock: the media slot opens half a second after
# the session origin, both speakers carry a tone burst over exactly the same
# real second, and the call runs to 3.5 s. The agent track is generated live —
# a 10 ms frame per tick. The caller track is a real inbound stream: PyTgCalls
# dispatches nothing until the outbound slot is up at 2.5 s, then flushes the
# two seconds it buffered and continues live.
WINDOW = 4.0
SLOT_OPEN = 0.5
MARKER_FROM = 1.5
MARKER_TO = 2.5
AUDIO_END = 3.5
FRAME = 0.01


def replay_agent_track(writer, clock, rate):
    payload = marker_pcm(rate, AUDIO_END - SLOT_OPEN,
                         MARKER_FROM - SLOT_OPEN, MARKER_TO - SLOT_OPEN, 900, 6000)
    stride = int(rate * FRAME) * 2
    clock.now = SLOT_OPEN
    for start in range(0, len(payload), stride):
        writer.write(payload[start:start + stride])
        clock.advance(FRAME)


def replay_caller_track(writer, clock, rate):
    payload = marker_pcm(rate, AUDIO_END - SLOT_OPEN,
                         MARKER_FROM - SLOT_OPEN, MARKER_TO - SLOT_OPEN, 300, 9000)
    flush_at = MARKER_TO
    backlog = int(rate * (flush_at - SLOT_OPEN)) * 2
    stride = int(rate * FRAME) * 2
    clock.now = flush_at
    writer.write(payload[:backlog])
    clock.advance(FRAME)
    for start in range(backlog, len(payload), stride):
        writer.write(payload[start:start + stride])
        clock.advance(FRAME)


def build_offset_tracks(va, caller_pcm, agent_pcm):
    clock = FakeClock()
    caller = va._TrackWriter(caller_pcm, va.CALLER_RATE, 0.0, clock=clock)
    agent = va._TrackWriter(agent_pcm, va.AGENT_RATE, 0.0, clock=clock)
    replay_agent_track(agent, clock, va.AGENT_RATE)
    replay_caller_track(caller, clock, va.CALLER_RATE)
    caller.seal(WINDOW)
    agent.seal(WINDOW)
    return caller, agent


def first_sound_seconds(path, rate):
    """Where the tone burst sits in a raw mono track that is otherwise silent."""
    samples = array("h")
    samples.frombytes(path.read_bytes())
    return next(index for index, value in enumerate(samples) if value) / rate


def channel_onsets(path, rate=16000, block=0.01):
    """First moment each stereo channel carries real energy."""
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-f", "s16le", "-ar", str(rate), "-ac", "2", "-"],
        capture_output=True, check=True).stdout
    interleaved = array("h")
    interleaved.frombytes(decoded)
    span = int(rate * block)
    onsets = []
    for channel in (0, 1):
        samples = interleaved[channel::2]
        energies = [
            max(abs(value) for value in samples[start:start + span])
            for start in range(0, len(samples) - span, span)
        ]
        threshold = max(energies) * 0.25
        onsets.append(next(index for index, value in enumerate(energies)
                           if value >= threshold) * block)
    return onsets


@contextmanager
def fake_genai_types():
    """_handle_tool_call builds its reply with google.genai's FunctionResponse,
    and the sender wraps caller audio in a Blob."""
    names = ("google", "google.genai", "google.genai.types")
    saved = {name: sys.modules.get(name) for name in names}

    class FunctionResponse:
        def __init__(self, name=None, id=None, response=None):
            self.name = name
            self.id = id
            self.response = response

    google = types.ModuleType("google")
    google.__path__ = []
    genai = types.ModuleType("google.genai")
    genai.__path__ = []
    class Blob:
        def __init__(self, data=None, mime_type=None):
            self.data = data
            self.mime_type = mime_type

    class FakeModels:
        """`generate_content` as the phrase writer calls it: synchronously, off
        the loop in a thread. `answer` is what it replies with; a BaseException
        instance is raised instead, which is how the timeout is staged."""
        answer = ""

        def generate_content(self, **_kwargs):
            if isinstance(FakeModels.answer, BaseException):
                raise FakeModels.answer
            return types.SimpleNamespace(text=FakeModels.answer)

    class Client:
        def __init__(self, **_kwargs):
            self.models = FakeModels()

    genai.Client = Client
    genai.FakeModels = FakeModels
    genai_types = types.ModuleType("google.genai.types")
    genai_types.FunctionResponse = FunctionResponse
    genai_types.Blob = Blob
    genai.types = genai_types
    google.genai = genai
    sys.modules.update({"google": google, "google.genai": genai,
                        "google.genai.types": genai_types})
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class FakeLive:
    """The Live socket: keeps what the session sent it."""

    def __init__(self):
        self.tool_responses = []
        self.texts = []

    async def send_tool_response(self, function_responses):
        self.tool_responses.extend(function_responses)

    async def send_realtime_input(self, text=None, audio=None):
        self.texts.append(text)


def tool_call(task, call_id="call-1", name="agent_task"):
    return types.SimpleNamespace(
        tool_call=types.SimpleNamespace(function_calls=[
            types.SimpleNamespace(name=name, id=call_id, args={"task": task})]))


def turn_complete():
    return types.SimpleNamespace(server_content=types.SimpleNamespace(
        interrupted=False, turn_complete=True, generation_complete=True,
        input_transcription=None, output_transcription=None, model_turn=None))


def task_session(va, runner):
    session = va.VoiceCallSession(
        RecordingCalls(), 42, api_key="k", model=None, voice=None,
        system_instruction="s", caller_name="Caller", task_runner=runner,
        log=lambda *_: None)
    # A session handed a live socket is a connected one: the readiness the real
    # connect sets is part of that, and without it these tests would be sitting
    # in the reconnect gap without saying so.
    session._live = FakeLive()
    session._live_ready.set()
    return session


class AgentTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_tool_answers_before_the_work_is_done(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            release = asyncio.Event()

            async def work(text):
                await release.wait()
                return f"answer to {text}"

            runner = va.VoiceTaskRunner(work, self.fail_delivery, log=lambda *_: None)
            session = task_session(va, runner)
            await session._handle_tool_call(tool_call("look the thing up"))
            answered = session._live.tool_responses[0].response["result"]
            still_running = runner.running

            job = next(iter(runner._jobs.values()))
            release.set()
            await job
            completion = runner.completions.get_nowait()

        self.assertEqual(answered["status"], "started")
        self.assertEqual(still_running, 1)
        self.assertEqual(completion["result"], "answer to look the thing up")
        self.assertEqual(runner.running, 0)

    async def test_the_tool_is_declared_only_when_a_runner_is_present(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            with_tools = va.live_config("s", "v", tools=[va.AGENT_TASK_TOOL])
            without = va.live_config("s", "v")

        self.assertEqual(with_tools["tools"],
                         [{"function_declarations": [va.AGENT_TASK_TOOL]}])
        self.assertNotIn("tools", without)
        self.assertEqual(va.AGENT_TASK_TOOL["name"], "agent_task")

    async def test_work_still_running_from_an_earlier_call_holds_the_next_one(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            busy = {"elsewhere": True}
            runner = va.VoiceTaskRunner(
                lambda text: self.answer("done"), self.fail_delivery,
                log=lambda *_: None, elsewhere=lambda: busy["elsewhere"])

            refused = runner.start("look the thing up")
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["status"], "busy_from_earlier_call")
            self.assertEqual(runner.running, 0)

            # Two workers on one carried session would be two processes writing
            # one codex rollout, so holding is the point — once the earlier work
            # is done, this starts normally.
            busy["elsewhere"] = False
            self.assertTrue(runner.start("look the thing up")["ok"])

    async def test_a_finished_task_is_announced_only_between_turns(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner = va.VoiceTaskRunner(
                lambda text: self.answer("the answer is ready"),
                self.fail_delivery, log=lambda *_: None)
            session = task_session(va, runner)
            notifier = asyncio.create_task(session._announce_completions())

            session._model_idle.clear()          # the agent is speaking
            await session._handle_tool_call(tool_call("look the thing up"))
            await asyncio.sleep(0.05)
            spoken_over = list(session._live.texts)

            session._consume(turn_complete())    # the turn ends
            await asyncio.sleep(0.05)
            announced = list(session._live.texts)
            notifier.cancel()

        self.assertEqual(spoken_over, [])
        self.assertEqual(len(announced), 1)
        self.assertIn("Internal background event", announced[0])
        self.assertIn("not a new user request", announced[0])
        self.assertIn("the answer is ready", announced[0])

    async def test_a_second_task_is_refused_while_one_runs(self):
        """One task per call, and the refusal tells the model to wait rather than
        retry: several workers on one call race their answers into the call."""
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            self.assertEqual(va.TASKS_PER_CALL, 1)
            release = asyncio.Event()

            async def work(text):
                await release.wait()
                return text

            runner = va.VoiceTaskRunner(work, self.fail_delivery,
                                        log=lambda *_: None)
            session = task_session(va, runner)
            for task in ("first task", "second task", "third task"):
                await session._handle_tool_call(tool_call(task))
            answers = [row.response["result"] for row in session._live.tool_responses]
            running = runner.running

            jobs = list(runner._jobs.values())
            release.set()
            await asyncio.gather(*jobs)

        self.assertEqual([answer["status"] for answer in answers],
                         ["started", "busy", "busy"])
        self.assertEqual(running, 1)
        self.assertEqual(answers[-1]["limit"], 1)
        self.assertFalse(answers[-1]["ok"])
        self.assertIn("Do NOT start this one", answers[-1]["instruction"])

    async def test_the_task_bound_is_not_a_setting(self):
        """A project raising the bound would only re-create the defect it fixes,
        so the runner takes no limit at all."""
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            with self.assertRaises(TypeError):
                va.VoiceTaskRunner(self.fail_delivery, self.fail_delivery, limit=4)

    async def test_the_same_task_is_never_started_twice(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            release = asyncio.Event()

            async def work(text):
                await release.wait()
                return text

            runner = va.VoiceTaskRunner(work, self.fail_delivery, log=lambda *_: None)
            session = task_session(va, runner)
            await session._handle_tool_call(tool_call("Look   The Thing Up"))
            await session._handle_tool_call(tool_call("look the thing up"))
            answers = [row.response["result"] for row in session._live.tool_responses]
            running = runner.running

            jobs = list(runner._jobs.values())
            release.set()
            await asyncio.gather(*jobs)

        self.assertEqual([answer["status"] for answer in answers],
                         ["started", "already_running"])
        self.assertEqual(answers[0]["job_id"], answers[1]["job_id"])
        self.assertEqual(running, 1)

    async def test_a_result_that_lands_after_the_call_goes_to_the_chat(self):
        delivered = []
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            release = asyncio.Event()

            async def work(text):
                await release.wait()
                return f"answer to {text}"

            runner = va.VoiceTaskRunner(
                work, lambda completion: self.collect(delivered, completion),
                log=lambda *_: None)
            session = task_session(va, runner)
            await session._handle_tool_call(tool_call("look the thing up"))
            job = next(iter(runner._jobs.values()))

            await session.stop()                 # the caller hangs up
            self.assertEqual(delivered, [])      # the work is still running
            release.set()
            await job

        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["result"], "answer to look the thing up")
        self.assertTrue(delivered[0]["ok"])

    async def test_a_result_waiting_to_be_spoken_at_hangup_goes_to_the_chat(self):
        delivered = []
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner = va.VoiceTaskRunner(
                lambda text: self.answer("the answer is ready"),
                lambda completion: self.collect(delivered, completion),
                log=lambda *_: None)
            session = task_session(va, runner)
            session._model_idle.clear()          # nothing can be injected yet
            await session._handle_tool_call(tool_call("look the thing up"))
            await asyncio.sleep(0.05)
            queued = runner.completions.qsize()

            await session.stop()

        self.assertEqual(queued, 1)
        self.assertEqual([row["result"] for row in delivered], ["the answer is ready"])

    async def test_a_result_in_hand_when_the_call_ends_still_reaches_the_chat(self):
        """The announcer has taken the result off the queue and is waiting for the
        model to stop speaking when the caller hangs up. Held in hand, it must be
        put back before the queue is drained, or it is lost silently."""
        delivered = []
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner = va.VoiceTaskRunner(
                lambda text: self.answer("the answer is ready"),
                lambda completion: self.collect(delivered, completion),
                log=lambda *_: None)
            session = task_session(va, runner)
            session._model_idle.clear()          # the agent is mid-sentence
            notifier = asyncio.create_task(session._announce_completions())
            session._tasks.append(notifier)
            await session._handle_tool_call(tool_call("look the thing up"))
            await asyncio.sleep(0.05)
            in_hand = session._announcing
            spoken = list(session._live.texts)

            await session.stop()                 # the caller hangs up

        self.assertIsNotNone(in_hand)            # off the queue, not yet spoken
        self.assertEqual(spoken, [])
        self.assertEqual([row["result"] for row in delivered], ["the answer is ready"])

    async def test_waiting_for_a_pause_is_bounded_rather_than_endless(self):
        """A turn boundary that never arrives must not sit on a finished result:
        interrupting the agent is better than losing what the caller asked for."""
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            self.assertEqual(va.ANNOUNCE_IDLE_TIMEOUT, 20.0)
            va.ANNOUNCE_IDLE_TIMEOUT = 0.05
            runner = va.VoiceTaskRunner(
                lambda text: self.answer("the answer is ready"),
                self.fail_delivery, log=lambda *_: None)
            session = task_session(va, runner)
            session._model_idle.clear()          # and it never becomes idle
            notifier = asyncio.create_task(session._announce_completions())
            await session._handle_tool_call(tool_call("look the thing up"))
            await asyncio.sleep(0.2)
            announced = list(session._live.texts)
            notifier.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notifier

        self.assertEqual(len(announced), 1)
        self.assertIn("the answer is ready", announced[0])
        self.assertIsNone(session._announcing)

    async def test_a_failed_task_is_reported_rather_than_lost(self):
        delivered = []
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()

            async def work(text):
                raise RuntimeError("worker timed out")

            runner = va.VoiceTaskRunner(
                work, lambda completion: self.collect(delivered, completion),
                log=lambda *_: None)
            session = task_session(va, runner)
            await session._handle_tool_call(tool_call("look the thing up"))
            job = next(iter(runner._jobs.values()))
            await job
            completion = runner.completions.get_nowait()

        self.assertFalse(completion["ok"])
        self.assertIn("worker timed out", completion["result"])

    async def answer(self, text):
        return text

    async def collect(self, sink, completion):
        sink.append(completion)

    async def fail_delivery(self, completion):
        raise AssertionError("the call was still up; nothing should reach the chat")


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
                await asyncio.sleep(0.15)
                await session.stop()
                written = caller_pcm.read_bytes()

        self.assertEqual(session.caller_bytes, va.CALLER_FRAME_BYTES * 10)
        self.assertEqual(session._input_queue.qsize(), 1)
        self.assertTrue(written.startswith(payload * 10))

    async def test_both_tracks_seal_to_the_same_call_window(self):
        with fake_runtime_modules() as Frame:
            va = import_voice_agent()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                session = va.VoiceCallSession(
                    RecordingCalls(), 42, api_key="k", model=None, voice=None,
                    system_instruction="s", caller_name="Caller",
                    caller_track=root / "caller.pcm", agent_track=root / "agent.pcm",
                    log=lambda *_: None)
                session.start_pump()
                await asyncio.sleep(0.1)
                session.on_incoming_frames(
                    [Frame(frame=b"\x33" * va.CALLER_FRAME_BYTES) for _ in range(5)])
                await asyncio.sleep(0.1)
                await session.stop()
                tracks = session.tracks

        durations = {track["kind"]: track["duration_seconds"] for track in tracks}
        self.assertAlmostEqual(durations["caller"], durations["agent"], delta=0.001)
        for duration in durations.values():
            self.assertLessEqual(duration, session.window_seconds + 0.001)


class TrackAlignmentTests(unittest.TestCase):
    def test_leading_silence_counts_the_delay_to_the_first_sample_once(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                caller, agent = build_offset_tracks(
                    va, root / "caller.pcm", root / "agent.pcm")
                marks = (first_sound_seconds(caller.path, va.CALLER_RATE),
                         first_sound_seconds(agent.path, va.AGENT_RATE))

        # The caller's stream was dispatched two seconds late but carried those
        # two seconds with it: its silence is the real delay, not the delay plus
        # the audio that already covered it.
        self.assertAlmostEqual(caller.lead_seconds, SLOT_OPEN, delta=2 * FRAME)
        self.assertAlmostEqual(agent.lead_seconds, SLOT_OPEN, delta=2 * FRAME)
        for mark in marks:
            self.assertAlmostEqual(mark, MARKER_FROM, delta=2 * FRAME)

    def test_neither_track_outlives_the_call_window(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                caller, agent = build_offset_tracks(
                    va, root / "caller.pcm", root / "agent.pcm")

        for writer in (caller, agent):
            self.assertLessEqual(writer.duration_seconds, WINDOW)
        self.assertAlmostEqual(
            caller.duration_seconds, agent.duration_seconds, delta=0.001)

    def test_a_track_that_never_received_audio_stays_empty(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "caller.pcm"
                writer = va._TrackWriter(path, va.CALLER_RATE, 0.0, clock=FakeClock())
                writer.seal(WINDOW)
                size = path.stat().st_size

        self.assertEqual(size, 0)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg/ffprobe not available")
class StereoJoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracks_that_start_at_different_offsets_stay_aligned(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                caller_pcm, agent_pcm = root / "caller.pcm", root / "agent.pcm"
                build_offset_tracks(va, caller_pcm, agent_pcm)
                output = root / "call.ogg"
                result = await va.join_tracks_to_stereo(caller_pcm, agent_pcm, output)

                self.assertEqual(result["status"], "complete", result["error"])
                self.assertAlmostEqual(result["duration_seconds"], WINDOW, delta=0.1)
                caller_onset, agent_onset = channel_onsets(output)

        # Both speakers marked the same real second of the call, so the mix must
        # put both markers in the same place.
        self.assertAlmostEqual(caller_onset, agent_onset, delta=0.05)
        self.assertAlmostEqual(caller_onset, MARKER_FROM, delta=0.1)

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
    def test_prompt_is_voice_file_then_time_then_history(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            prompt = va.build_system_prompt(
                "# Voice instructions\nSpeak briefly.\n",
                "Caller: hello\nAssistant: hi",
                now_line="The call is happening now: 2026-08-11 14:30 (Tuesday), "
                         "timezone Europe/Tallinn.")

        self.assertTrue(prompt.startswith("# Voice instructions\nSpeak briefly."))
        self.assertLess(prompt.index("Speak briefly."), prompt.index("Caller: hello"))
        self.assertEqual(
            prompt,
            "# Voice instructions\nSpeak briefly.\n\n"
            "--- Right now ---\n\n"
            "The call is happening now: 2026-08-11 14:30 (Tuesday), "
            "timezone Europe/Tallinn.\n\n"
            "--- Recent messages in this direct chat ---\n\n"
            "Older first. Each line is timestamped in the same zone as the "
            "current time above.\n\n"
            "Caller: hello\nAssistant: hi")

    def test_prompt_carries_no_built_in_text_of_its_own(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            self.assertEqual(va.build_system_prompt("Project prompt.", ""),
                             "Project prompt.")
            self.assertEqual(va.build_system_prompt("", ""), "")
            self.assertFalse(hasattr(va, "VOICE_PREAMBLE"))

    def test_prompt_cannot_accept_project_context(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            with self.assertRaisesRegex(TypeError, "project_context"):
                va.build_system_prompt(
                    "Project prompt.", "Caller: hello",
                    project_context="compiled ContextKit body")

    def test_the_prompt_can_be_set_after_the_call_is_answered(self):
        """Building it means reading the chat tail, which must happen after the
        call is picked up — so the session starts without one and is told later."""
        with fake_runtime_modules():
            va = import_voice_agent()
            session = va.VoiceCallSession(
                RecordingCalls(), 42, api_key="k", model=None, voice="v",
                system_instruction="", caller_name="Caller", log=lambda *_: None)
            session.set_system_instruction("read from the chat")
            config = va.live_config(session._system_instruction, "v")

        self.assertEqual(config["system_instruction"],
                         {"parts": [{"text": "read from the chat"}]})

    def test_the_call_states_a_time_in_a_named_zone_never_the_hosts(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            zone, label = va.resolve_timezone("Europe/Berlin")
            self.assertEqual(label, "Europe/Berlin")
            line = va.current_time_line(zone, label)
            self.assertIn("timezone Europe/Berlin", line)
            self.assertIn(f"{datetime.now(zone):%Y-%m-%d}", line)
            # Unset, unknown, or nonsense all mean UTC — never the host's zone,
            # which would make a relocated daemon quietly report the wrong time.
            for name in (None, "", "Mars/Olympus", "not a zone"):
                self.assertEqual(va.resolve_timezone(name),
                                 (timezone.utc, "UTC"))


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_the_digest_is_the_whole_window_not_its_last_step(self):
        """A worker's last step is often something that did not work, which it
        then routes around; reporting it tells the caller a failure that is not
        one. So the window is folded into counts plus where the work stands."""
        with fake_runtime_modules():
            va = import_voice_agent()
            note = va.summarize_progress([
                ("stream", "searching for it"),
                ("stream", "searching for it"),
                ("stream", "reading what it found"),
                ("stream", "changing files"),
            ])
            single = va.summarize_progress([("stream", "starting")])
            nothing = va.summarize_progress([])

        self.assertIn("searching for it (2x)", note)
        self.assertIn("reading what it found", note)
        self.assertTrue(note.endswith("now changing files"))
        self.assertEqual(single, "starting")
        self.assertIsNone(nothing)

    def test_a_line_the_worker_wrote_outranks_a_derived_one(self):
        with fake_runtime_modules():
            va = import_voice_agent()
            note = va.summarize_progress([
                ("stream", "searching for it"),
                ("worker", "got the list, counting the failed ones"),
                ("stream", "changing files"),
            ])

        self.assertEqual(note, "got the list, counting the failed ones")

    async def test_the_same_note_is_never_offered_twice(self):
        """Saying again what was just said is worse than silence, whether the
        phrase was written by the model or folded by rule."""
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner = va.VoiceTaskRunner(lambda text: self.never(),
                                        self.never, log=lambda *_: None)
            session = task_session(va, runner)

            # No usable model here, so every phrase is the rule-built digest —
            # which is what makes this deterministic.
            first = await session._phrase_progress([("stream", "searching for it")])
            session._progress_last_offered = first
            repeated = await session._phrase_progress([("stream", "searching for it")])
            changed = await session._phrase_progress([("stream", "changing files")])

        self.assertEqual(first, "searching for it")
        self.assertIsNone(repeated)
        self.assertEqual(changed, "changing files")

    async def test_a_window_is_composed_by_the_work_not_by_a_clock(self):
        """Enough having happened is what asks for a phrase, and it arrives
        irregularly because the work does. A line the worker wrote itself never
        waits behind a count of machine events."""
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner = va.VoiceTaskRunner(lambda text: self.never(),
                                        self.never, log=lambda *_: None)
            session = task_session(va, runner)

            nothing_yet = session._due_progress()
            for n in range(va.PROGRESS_EVENT_BURST):
                session.note_progress(f"step {n}")
            burst = session._due_progress()
            emptied = session._due_progress()

            # Composing has a floor of its own, tested separately; step past it
            # so this stays about what asks for a phrase.
            session._progress_composed_at = 0.0
            session.note_progress("stage", source="stream")
            session.note_progress("got the list", source="worker")
            with_a_worker_line = session._due_progress()

        self.assertIsNone(nothing_yet)
        self.assertEqual(len(burst), va.PROGRESS_EVENT_BURST)
        # The window has had its turn whether or not anything is said from it.
        self.assertIsNone(emptied)
        self.assertIn(("worker", "got the list"), with_a_worker_line)

    async def test_the_written_phrase_is_used_and_every_way_it_can_fail_falls_back(self):
        """The phrase writer sits on the call's own path, so none of its failures
        may reach the tick loop: what it cannot answer, the rule-built digest
        answers instead. Its one non-fallback answer is silence, which it asks
        for with a hyphen — and that must not be overruled by the digest, since
        overruling it is how "still working" gets said out loud."""
        with fake_runtime_modules(), fake_genai_types():
            import google.genai as genai

            va = import_voice_agent()
            runner = va.VoiceTaskRunner(lambda text: self.never(),
                                        self.never, log=lambda *_: None)
            session = task_session(va, runner)
            events = [("stream", "searching for it")]

            async def phrase_when(answer):
                genai.FakeModels.answer = answer
                session._phrase_client = None
                session._progress_last_offered = None
                return await session._phrase_progress(events)

            written = await phrase_when("looking through yesterday's calls")
            silent = await phrase_when("-")
            empty = await phrase_when("")
            timed_out = await phrase_when(asyncio.TimeoutError())
            raised = await phrase_when(RuntimeError("upstream said no"))

        self.assertEqual(written, "looking through yesterday's calls")
        self.assertIsNone(silent)
        self.assertEqual(empty, "searching for it")
        self.assertEqual(timed_out, "searching for it")
        self.assertEqual(raised, "searching for it")

    async def test_composing_a_phrase_has_a_floor_of_its_own(self):
        """A phrase that finds no gap is dropped while the work carries on
        producing events. Without this, a caller who talks through a busy task
        has one composed and thrown away every tick."""
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner = va.VoiceTaskRunner(lambda text: self.never(),
                                        self.never, log=lambda *_: None)
            session = task_session(va, runner)

            session.note_progress("searching for it")
            first = session._due_progress()
            session.note_progress("changing files")
            too_soon = session._due_progress()
            session._progress_composed_at = (time.monotonic()
                                             - session._progress_interval - 1)
            later = session._due_progress()

        self.assertIsNotNone(first)
        self.assertIsNone(too_soon)
        self.assertIsNotNone(later)

    async def test_a_phrase_that_finds_no_gap_is_dropped_rather_than_said_late(self):
        """It describes the last few seconds, so it goes stale on the line."""
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner = va.VoiceTaskRunner(lambda text: self.never(),
                                        self.never, log=lambda *_: None)
            session = task_session(va, runner)
            session._progress_interval = 0.0
            session._caller_spoke_at = time.monotonic()   # the caller is talking
            session._progress_pending = ("got the list", time.monotonic() + 5.0)

            held = session._can_speak_progress()
            session._caller_spoke_at = time.monotonic() - va.PROGRESS_CALLER_QUIET - 1
            session._agent_voiced_at = time.monotonic() - va.PROGRESS_AGENT_QUIET - 1
            freed = session._can_speak_progress()

        self.assertFalse(held)
        self.assertTrue(freed)

    async def test_progress_stays_off_the_line_until_there_is_a_lull(self):
        """Composing is the work's business; saying is the conversation's. A
        phrase waits while either party still holds the line — including while
        the caller is still hearing audio the model finished generating."""
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner = va.VoiceTaskRunner(lambda text: self.never(),
                                        self.never, log=lambda *_: None)
            session = task_session(va, runner)
            session._progress_interval = 0.0

            session._model_idle.clear()               # the agent is speaking
            while_speaking = session._can_speak_progress()
            session._model_idle.set()
            session._caller_spoke_at = time.monotonic()   # the caller just spoke
            while_caller_talks = session._can_speak_progress()
            session._caller_spoke_at = time.monotonic() - va.PROGRESS_CALLER_QUIET - 1
            session._agent_voiced_at = time.monotonic()   # its audio still playing
            while_audio_drains = session._can_speak_progress()
            session._agent_voiced_at = time.monotonic() - va.PROGRESS_AGENT_QUIET - 1
            in_the_lull = session._can_speak_progress()

        self.assertFalse(while_speaking)
        self.assertFalse(while_caller_talks)
        self.assertFalse(while_audio_drains)
        self.assertTrue(in_the_lull)

    async def test_two_phrases_never_land_in_the_same_breath(self):
        """The work sets the rhythm, but a burst of it cannot fire twice over:
        the interval survives as the floor between two spoken phrases."""
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner = va.VoiceTaskRunner(lambda text: self.never(),
                                        self.never, log=lambda *_: None)
            session = task_session(va, runner)
            session._caller_spoke_at = time.monotonic() - va.PROGRESS_CALLER_QUIET - 1
            session._agent_voiced_at = time.monotonic() - va.PROGRESS_AGENT_QUIET - 1

            session._progress_offered_at = time.monotonic()      # just spoke
            too_soon = session._can_speak_progress()
            session._progress_offered_at = (time.monotonic()
                                            - session._progress_interval - 1)
            far_enough = session._can_speak_progress()

        self.assertFalse(too_soon)
        self.assertTrue(far_enough)

    async def never(self, *_args):
        raise AssertionError("nothing should run in a progress-only test")


class TranscriptTurnTests(unittest.TestCase):
    def test_a_pause_starts_a_new_turn_even_for_the_same_speaker(self):
        """A task answered fifteen seconds later is a new turn, not a
        continuation: joining them makes the turn's own timestamp lie."""
        with fake_runtime_modules():
            va = import_voice_agent()
            session = va.VoiceCallSession(
                RecordingCalls(), 42, api_key="k", model=None, voice=None,
                system_instruction="", caller_name="Caller", log=lambda *_: None)
            session._record_fragment("agent", "one moment")
            session._record_fragment("agent", ", looking")
            joined = len(session._turns)
            session._last_fragment_at -= va.TURN_JOIN_GAP_SECONDS + 1
            session._record_fragment("agent", "here it is")

        self.assertEqual(joined, 1)
        self.assertEqual([turn["text"] for turn in session._turns],
                         ["one moment, looking", "here it is"])


class RunCapabilityToolTests(unittest.IsolatedAsyncioTestCase):
    """The tool is declared only where it works, and a malformed call comes
    back as something the model can act on rather than an unanswered turn."""

    def session(self, va, runner=None):
        session = va.VoiceCallSession(
            RecordingCalls(), 42, api_key="k", model=None, voice=None,
            system_instruction="s", caller_name="Caller",
            capability_runner=runner, log=lambda *_: None)
        session._live = FakeLive()
        return session

    async def test_it_is_declared_only_when_a_runner_backs_it(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()

            async def runner(capability, args):
                return {"ok": True, "status": "ok"}

            names = [t["name"] for t in self.session(va, runner)._declared_tools()]
            self.assertIn("run_capability", names)

            # No runner, no declaration: the model is never handed a tool that
            # would fail the moment it reached for it.
            self.assertIsNone(self.session(va, None)._declared_tools())

    async def test_arguments_packed_into_one_string_are_refused_not_split(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            seen = []

            async def runner(capability, args):
                seen.append((capability, args))
                return {"ok": True, "status": "ok"}

            session = self.session(va, runner)
            result = await session._run_capability("clickup", "tasks --list Accounting")
            # Splitting that string is what puts shell metacharacters back into
            # play, so it is refused and the shape is explained instead.
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "args_not_a_list")
            self.assertIn("separate items", result["instruction"])
            self.assertEqual(seen, [])

    async def test_a_missing_name_is_answered_rather_than_run(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()

            async def runner(capability, args):
                raise AssertionError("must not run without a name")

            result = await self.session(va, runner)._run_capability("  ", [])
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "no_capability")

    async def test_the_greeting_is_the_projects_when_it_supplies_one(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            # The last thing said before the model speaks decides the language
            # the call opens in, whatever the prompt above asked for.
            self.assertEqual(
                va.greeting_prompt("KZ", "{caller} снял трубку. Поздоровайся по-русски."),
                "KZ снял трубку. Поздоровайся по-русски.")
            # A template with nothing to fill in is used as written.
            self.assertEqual(va.greeting_prompt("KZ", "Поздоровайся."), "Поздоровайся.")
            # A malformed one is still spoken rather than crashing the pickup.
            self.assertEqual(va.greeting_prompt("KZ", "{nope}"), "{nope}")
            # Unset falls back to the shipped English line.
            self.assertIn("has just picked up", va.greeting_prompt("KZ"))

    async def test_a_lost_speech_session_hangs_up_once(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            hangups = []

            async def on_stream_end():
                hangups.append(True)

            session = va.VoiceCallSession(
                RecordingCalls(), 42, api_key="k", model=None, voice=None,
                system_instruction="s", caller_name="Caller",
                on_stream_end=on_stream_end, log=lambda *_: None)
            session._live = FakeLive()

            await session._speech_is_over()
            # The receiver can reach this twice — the stream raising and then the
            # loop ending — and the caller must not be hung up on twice.
            await session._speech_is_over()
            self.assertEqual(len(hangups), 1)

    async def test_a_hang_up_that_fails_does_not_escape(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()

            async def on_stream_end():
                raise RuntimeError("the call was already gone")

            session = va.VoiceCallSession(
                RecordingCalls(), 42, api_key="k", model=None, voice=None,
                system_instruction="s", caller_name="Caller",
                on_stream_end=on_stream_end, log=lambda *_: None)
            session._live = FakeLive()
            # Already-ended calls are the normal case here, not an exception to
            # propagate into the receiver.
            await session._speech_is_over()

    async def test_a_tool_that_breaks_costs_its_answer_not_the_call(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()

            async def runner(capability, args):
                raise AttributeError("something in the handler is wrong")

            session = self.session(va, runner)
            call = types.SimpleNamespace(
                tool_call=types.SimpleNamespace(function_calls=[
                    types.SimpleNamespace(name="run_capability", id="1",
                                          args={"capability": "clickup",
                                                "args": ["help"]})]))
            # The stream must survive a broken handler: an exception here reaches
            # the Live session instead of the model and the caller holds a dead
            # line.
            await session._handle_tool_call(call)
            self.assertEqual(len(session._live.tool_responses), 1)

    async def test_arguments_reach_the_runner_as_given(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            seen = []

            async def runner(capability, args):
                seen.append((capability, args))
                return {"ok": True, "status": "ok", "stdout": "[]"}

            session = self.session(va, runner)
            result = await session._run_capability("clickup", ["tasks", "--list", "A B"])
            self.assertTrue(result["ok"])
            self.assertEqual(seen, [("clickup", ["tasks", "--list", "A B"])])

            # No arguments at all is a legitimate call, not a malformed one.
            await session._run_capability("clickup", None)
            self.assertEqual(seen[-1], ("clickup", []))


class ScriptedLive(FakeLive):
    """A Live socket that ends the way a real one does: some messages, then the
    connection going away under the session."""

    def __init__(self, messages, fail=None):
        super().__init__()
        self.messages = list(messages)
        self.fail = fail
        self.audio = []

    async def send_realtime_input(self, text=None, audio=None):
        if audio is not None:
            self.audio.append(audio)
        else:
            self.texts.append(text)

    async def receive(self):
        for message in self.messages:
            yield message
        if self.fail is not None:
            raise self.fail


def resumption_update(handle, resumable=True):
    return types.SimpleNamespace(
        session_resumption_update=types.SimpleNamespace(
            resumable=resumable, new_handle=handle),
        go_away=None, server_content=None, tool_call=None)


class SessionResumptionTests(unittest.IsolatedAsyncioTestCase):
    """A connection to the speech model is retired long before a talkative
    caller is finished. What must survive that is the conversation."""

    def session(self, va, on_stream_end=None):
        return va.VoiceCallSession(
            RecordingCalls(), 42, api_key="k", model=None, voice=None,
            system_instruction="s", caller_name="Caller",
            on_stream_end=on_stream_end, log=lambda *_: None)

    def reconnects_through(self, session, lives):
        """Stand in for the real connect, recording the handle each one is
        opened with — that handle is the whole difference between continuing a
        conversation and starting a stranger's."""
        opened = []

        async def _open_live(resume_handle=None):
            opened.append(resume_handle)
            if not lives:
                raise ConnectionError("nothing left to connect to")
            session._live = lives.pop(0)
            session._live_ready.set()

        session._open_live = _open_live
        return opened

    async def test_a_retired_connection_comes_back_with_the_conversation(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            hangups = []

            async def on_stream_end():
                hangups.append(True)

            session = self.session(va, on_stream_end)
            second = ScriptedLive([], fail=None)
            opened = self.reconnects_through(session, [second])
            session._live = ScriptedLive(
                [resumption_update("handle-1")],
                fail=ConnectionError("1008 go away"))
            session._live_ready.set()

            await session._gemini_receiver()

            # The newest handle went back on the wire, so the model still knows
            # what was said before the drop.
            self.assertEqual(opened[0], "handle-1")
            # A resumed call is not a new one: greeting it again would have the
            # assistant introduce itself in the middle of a conversation.
            self.assertEqual(second.texts, [])
            # And the caller was hung up on only after the second connection
            # ended too, with nothing left to come back with.
            self.assertEqual(len(hangups), 1)
            self.assertEqual(session._resumes, 2)

    async def test_a_connection_lost_before_any_handle_ends_the_call(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            hangups = []

            async def on_stream_end():
                hangups.append(True)

            session = self.session(va, on_stream_end)
            opened = self.reconnects_through(session, [])
            session._live = ScriptedLive([], fail=ConnectionError("gone"))
            session._live_ready.set()

            await session._gemini_receiver()

            # Reconnecting without a handle would silently swap the caller onto a
            # session that has forgotten them. Hanging up is the honest end.
            self.assertEqual(opened, [])
            self.assertEqual(len(hangups), 1)

    async def test_only_a_resumable_handle_is_kept(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            session = self.session(va)
            session._live = FakeLive()

            session._consume(resumption_update("h1"))
            self.assertEqual(session._resume_handle, "h1")
            # The server reissues as the conversation grows; the newest wins.
            session._consume(resumption_update("h2"))
            self.assertEqual(session._resume_handle, "h2")
            # A handle the server says cannot be resumed with is not one.
            session._consume(resumption_update("h3", resumable=False))
            self.assertEqual(session._resume_handle, "h2")

    async def test_coming_back_is_bounded(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            session = self.session(va)
            session._live = FakeLive()
            session._resume_handle = "h"
            session._resumes = va.MAX_RESUMES
            # A server dropping every connection the instant it opens must not
            # be reconnected to forever.
            self.assertFalse(await session._resume(ConnectionError("again")))

    async def never(self, *_args, **_kwargs):
        raise AssertionError("this call should not have been made")

    def gap_session(self, va):
        """A session mid-resume: a task running, and no connection to speak
        into. This is not an edge of the resume scenario — progress ticks once a
        second into exactly the lull a dead connection produces."""
        runner = va.VoiceTaskRunner(lambda text: self.never(), self.never,
                                    log=lambda *_: None)
        session = task_session(va, runner)
        session._live = None            # mid-resume: there is nothing to send to
        session._live_ready.clear()
        return runner, session

    async def test_progress_survives_a_reconnect_it_ticks_into(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner, session = self.gap_session(va)
            runner._jobs["j"] = object()          # something is running
            session._progress_interval = 0.0
            session.note_progress("searching for it")
            session._caller_spoke_at = time.monotonic() - va.PROGRESS_CALLER_QUIET - 1

            progress = asyncio.create_task(session._announce_progress())
            await asyncio.sleep(1.3)
            # Alive, and the window is untouched: what happened during the gap
            # is still there to be said once the conversation is back.
            self.assertFalse(progress.done())
            self.assertTrue(session._progress_window)

            session._live = FakeLive()
            session._live_ready.set()
            await asyncio.sleep(1.3)
            self.assertTrue(session._live.texts)

            progress.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress

    async def test_a_finished_task_waits_for_the_connection_to_come_back(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            runner, session = self.gap_session(va)

            announcer = asyncio.create_task(session._announce_completions())
            runner.completions.put_nowait({"job_id": "task-1", "result": "done"})
            await asyncio.sleep(0.1)
            # Held rather than spoken into nothing — and never dropped.
            self.assertIsNotNone(session._announcing)

            session._live = FakeLive()
            session._live_ready.set()
            await asyncio.sleep(0.2)
            self.assertTrue(session._live.texts)
            self.assertIsNone(session._announcing)

            announcer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await announcer

    async def test_audio_spoken_between_connections_is_dropped(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            session = self.session(va)
            live = ScriptedLive([])
            session._live = live

            session._live_ready.clear()
            session._input_queue.put_nowait(b"\x00\x01")
            sender = asyncio.create_task(session._gemini_sender())
            await asyncio.sleep(0.05)
            # Held audio would be played into the resumed conversation seconds
            # late, on top of whatever is being said by then.
            self.assertEqual(live.audio, [])

            session._live_ready.set()
            session._input_queue.put_nowait(b"\x02\x03")
            await asyncio.sleep(0.05)
            self.assertEqual(len(live.audio), 1)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
class ReloadServiceToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_is_declared_only_for_an_authorized_call(self):
        with fake_runtime_modules():
            va = import_voice_agent()

            with_reload = va.VoiceCallSession(
                RecordingCalls(), 42, api_key="k", model=None, voice=None,
                system_instruction="s", caller_name="Caller",
                reload_service=lambda: {"ok": True}, log=lambda *_: None)
            without_reload = va.VoiceCallSession(
                RecordingCalls(), 42, api_key="k", model=None, voice=None,
                system_instruction="s", caller_name="Caller", log=lambda *_: None)

            self.assertIn("reload_service",
                          [tool["name"] for tool in with_reload._declared_tools()])
            self.assertIsNone(without_reload._declared_tools())

    async def test_reload_runs_inside_the_live_call_and_returns_its_result(self):
        with fake_runtime_modules(), fake_genai_types():
            va = import_voice_agent()
            called = []

            def reload_service():
                called.append(True)
                return {"ok": True, "status": "reloaded", "generation": 2}

            session = va.VoiceCallSession(
                RecordingCalls(), 42, api_key="k", model=None, voice=None,
                system_instruction="s", caller_name="Caller",
                reload_service=reload_service, log=lambda *_: None)
            session._live = FakeLive()
            call = types.SimpleNamespace(
                tool_call=types.SimpleNamespace(function_calls=[
                    types.SimpleNamespace(
                        name="reload_service", id="reload-1", args={})]))

            await session._handle_tool_call(call)

            result = session._live.tool_responses[0].response["result"]
            self.assertEqual(called, [True])
            self.assertEqual(result["status"], "reloaded")


if __name__ == "__main__":
    unittest.main()
