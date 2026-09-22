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
import importlib.util
import inspect
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

    def test_one_rate_carries_the_whole_tract(self):
        with fake_runtime_modules():
            module = import_gptlive()
        self.assertEqual(module.CALLER_RATE, 24000)
        self.assertEqual(module.AGENT_RATE, 24000)
        self.assertEqual(module.AGENT_FRAME_BYTES, 480)
        self.assertEqual(module.INPUT_CHUNK_BYTES, 4800)


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

    def test_the_other_provider_reaches_it_by_tool(self):
        with fake_runtime_modules():
            shared = import_module_at("voice_agent", SERVICE_DIR / "voice_agent.py")
        self.assertFalse(shared.DELEGATES_TO_WORKER)
        self.assertIn("agent_task", shared.TOOL_NAMES)


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
        cli = (SERVICE_DIR.parent / "bin" / "telegram").read_text()
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
