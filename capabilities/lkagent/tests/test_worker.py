"""The worker's wiring, with no server, no provider and no harness.

    uv run --no-project --python 3.12 --with pytest --with pytest-asyncio \
        --with "livekit-agents>=1.8.5,<2" --with "livekit-plugins-openai>=1.8.5,<2" \
        --with "callva-livekit>=0.1.72" --with "callva-harness-runner==0.13.1" \
        pytest capabilities/lkagent/tests -q -o asyncio_mode=auto
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("livekit.agents")
pytest.importorskip("callva.livekit.call")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-placeholder")
    monkeypatch.setenv("LKAGENT_AGENT_NAME", "lkagent-test")
    monkeypatch.setenv("LKAGENT_LINE", '{"settings": {"voice": "cedar"}}')
    sys.modules.pop("worker", None)
    import worker as module

    return module


class Emitter:
    def __init__(self):
        self.handlers = {}
        self.agent_state = "listening"

    def on(self, event, handler=None):
        self.handlers.setdefault(event, []).append(handler)
        return handler

    def off(self, event, handler):
        self.handlers.get(event, []).remove(handler)

    def emit(self, event, *args):
        for handler in list(self.handlers.get(event, [])):
            handler(*args)


class Live(Emitter):
    def __init__(self):
        super().__init__()
        self.sent = []

    def append_commentary(self, text, *, delegation_id=None):
        self.sent.append(("say", delegation_id, text))

    def append_thinking(self, text, *, delegation_id=None):
        self.sent.append(("know", delegation_id, text))


class FakeLive:
    """A backend that takes every message at once and ends when the test says so."""

    def __init__(self, take=True):
        self.ended = lambda *_: None
        self.progress = lambda _text: None
        self.messages, self.take, self.working = [], take, False
        self.closed = False

    @property
    def running(self):
        return self.working

    async def send(self, text):
        if not self.take:
            return False
        self.messages.append(text)
        self.working = True
        return True

    def finish(self, answer, error=None, timed_out=False):
        self.working = False
        self.ended(answer, error, timed_out)

    async def close(self):
        self.closed = True


def _spoken(text):
    """An append carries its text with its whitespace collapsed."""
    return " ".join(text.split())


async def _written(runner):
    while runner.sending:
        await asyncio.sleep(0.005)


def _runner(worker, backend, said=()):
    import line

    live, session, dialogue = Live(), Emitter(), line.Dialogue()
    for role, text in said:
        dialogue.hear(role, text)
    return worker.Runner(live, session, dialogue, backend), live, dialogue


def _delegate(live, delegation_id, pending=""):
    live.emit("delegation_created", SimpleNamespace(id=delegation_id, pending_transcript=pending))


def test_the_worker_is_full_at_one_call_not_by_cpu(worker):
    srv = SimpleNamespace(active_jobs=[])
    assert worker.line_load(srv) == 0.0
    srv.active_jobs = [object()]
    assert worker.line_load(srv) == 1.0
    srv.active_jobs = [object(), object()]
    assert worker.line_load(srv) == 1.0
    assert worker.server._load_threshold == 1.0
    assert not hasattr(worker, "MAX_CALLS")


async def test_a_handover_is_answered_with_the_voice_modes_result(worker):
    import line

    backend = FakeLive()
    runner, live, _ = _runner(worker, backend, [("user", "What's on tomorrow?")])
    _delegate(live, "d1")
    await _written(runner)
    assert backend.messages == ["*Voice mode messages*\n\n**User:** What's on tomorrow?"]
    assert runner.busy
    backend.finish("Two meetings tomorrow.")
    assert not runner.busy
    assert live.sent == [("say", "d1", _spoken(line.result("Two meetings tomorrow.")))]


async def test_nothing_said_is_not_handed_over(worker):
    import line

    backend = FakeLive()
    runner, live, _ = _runner(worker, backend)
    _delegate(live, "d1")
    await _written(runner)
    assert backend.messages == [] and live.sent == [("say", "d1", line.NOTHING_TO_HAND_OVER)]


async def test_a_handover_while_the_backend_works_joins_it_and_the_latest_gets_the_answer(worker):
    import line

    backend = FakeLive()
    runner, live, dialogue = _runner(worker, backend, [("user", "is it ready for release?")])
    _delegate(live, "d1")
    await _written(runner)
    dialogue.hear("user", "no, just check, don't release")
    _delegate(live, "d2")
    await _written(runner)
    assert len(backend.messages) == 2 and "don't release" in backend.messages[1]
    assert live.sent == []
    backend.finish("Ready; nothing released.")
    assert live.sent == [("say", "d2", _spoken(line.result("Ready; nothing released.")))]


async def test_failures_are_silent_lines(worker):
    import line

    backend = FakeLive()
    runner, live, dialogue = _runner(worker, backend, [("user", "a")])
    _delegate(live, "d1")
    await _written(runner)
    backend.finish(None, "interrupted", True)
    dialogue.hear("user", "b")
    _delegate(live, "d2")
    await _written(runner)
    backend.finish(None, "crash", False)
    assert live.sent == [("know", "d1", line.TIMED_OUT), ("know", "d2", line.FAILED)]


async def test_a_handover_the_backend_does_not_take_is_not_delivered(worker):
    import line

    backend = FakeLive(take=False)
    runner, live, _ = _runner(worker, backend, [("user", "hi")])
    _delegate(live, "d1")
    await _written(runner)
    assert live.sent == [("say", "d1", _spoken(line.not_delivered(line.NOT_TAKEN)))]
    assert not runner.busy


async def test_end_call_says_the_goodbye_then_hangs_up_and_takes_nothing_more(worker,
                                                                              monkeypatch):
    import line

    ended = []

    async def end(**kwargs):
        ended.append(kwargs["reason"])

    monkeypatch.setattr(worker.callva_call, "end", end)
    monkeypatch.setattr(line, "SPEAKING_WAIT", 0.01)
    backend = FakeLive()
    runner, live, dialogue = _runner(worker, backend, [("user", "bye")])
    _delegate(live, "d1")
    await _written(runner)
    backend.finish('Bye! {"action": "end_call"}')
    dialogue.hear("user", "wait")
    _delegate(live, "d2")
    await runner.hangup
    assert live.sent == [("say", "d1", _spoken(line.result("Bye!"))),
                         ("know", "d2", line.SUPERSEDED)]
    assert len(backend.messages) == 1 and ended == ["the agent ended the call"]


async def test_an_answer_for_an_id_a_reconnect_killed_goes_without_it(worker):
    backend = FakeLive()
    runner, live, _ = _runner(worker, backend, [("user", "find it")])
    _delegate(live, "d1")
    await _written(runner)
    live.emit("session_reconnected")
    backend.finish("Found it.")
    assert live.sent[-1][0:2] == ("know", None) and "Found it." in live.sent[-1][2]


async def test_a_refused_answer_is_answered_once_more_silently(worker):
    import line

    backend = FakeLive()
    runner, live, _ = _runner(worker, backend, [("user", "q")])
    _delegate(live, "d1")
    await _written(runner)
    backend.finish("Yes.")
    live.emit("openai_client_event_queued",
              {"type": "session.commentary.append", "delegation_id": "d1", "event_id": "e1"})
    live.emit("openai_server_event_received",
              {"type": "error", "error": {"client_event_id": "e1", "message": "too long"}})
    assert live.sent[-1] == ("know", "d1", line.REFUSED)


async def test_a_silent_line_hands_over_what_was_said_and_the_voice_says_the_answer(worker):
    import line

    backend = FakeLive()
    runner, live, dialogue = _runner(worker, backend, [("user", "Book it"),
                                                       ("assistant", "Let me check")])
    runner.liveness(line.LIVENESS_INSTRUCTION)
    await _written(runner)
    assert backend.messages == [line.liveness([("person", "Book it"), ("voice", "Let me check")],
                                              line.LIVENESS_INSTRUCTION)]
    assert dialogue.utterances == []
    backend.finish("Booked for nine.")
    assert live.sent == [("say", None, "Booked for nine.")]


async def test_a_silent_line_with_nothing_to_say_or_a_failure_says_nothing(worker):
    import line

    backend = FakeLive()
    runner, live, _ = _runner(worker, backend)
    runner.liveness(line.LIVENESS_INSTRUCTION)
    await _written(runner)
    backend.finish("   ")
    runner.liveness(line.LIVENESS_INSTRUCTION)
    await _written(runner)
    backend.finish(None, "crash")
    assert len(backend.messages) == 2 and live.sent == []


async def test_a_silent_line_adds_nothing_while_the_backend_works(worker):
    import line

    backend = FakeLive()
    runner, live, _ = _runner(worker, backend, [("user", "a")])
    _delegate(live, "d1")
    await _written(runner)
    runner.liveness(line.LIVENESS_INSTRUCTION)
    await _written(runner)
    assert len(backend.messages) == 1


async def test_closing_the_call_closes_the_backend(worker):
    backend = FakeLive()
    runner, _, _ = _runner(worker, backend)
    runner.on_close()
    await asyncio.sleep(0.01)
    assert backend.closed


# ── the live claude process, from its stream ────────────────────────────────

def _claude(worker):
    import line

    claude = worker.ClaudeLive(line.backend_profile({}), {}, "/tmp")
    got = []
    claude.ended = lambda *args: got.append(args)
    return claude, got


def test_claude_answers_once_every_message_is_done_with_the_last_text(worker):
    import line

    claude, got = _claude(worker)
    claude.pending = ["m1", "m2"]
    for obj in (
            {"type": "stream_event", "event": {"type": "content_block_start",
                                               "content_block": {"type": "text"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "text_delta",
                                                         "text": "Checking."}}},
            {"type": "stream_event", "parent_tool_use_id": "t1",
             "event": {"type": "content_block_delta",
                       "delta": {"type": "text_delta", "text": "subagent"}}},
            {"type": "stream_event", "event": {"type": "content_block_start",
                                               "content_block": {"type": "text"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "text_delta",
                                                         "text": "Ready."}}},
            {"type": "result", "subtype": "success", "user_message_uuids": ["m1"]}):
        claude.take(line.stream_event(obj))
    assert got == [] and claude.pending == ["m2"]
    claude.take(line.stream_event({"type": "result", "subtype": "success",
                                   "user_message_uuids": ["m2"]}))
    assert got == [("Ready.", None, False)] and not claude.running


def _block(kind, **extra):
    return {"type": "stream_event",
            "event": {"type": "content_block_start", "content_block": {"type": kind, **extra}}}


def _delta(kind, key, value):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": kind, key: value}}}


STOP = {"type": "stream_event", "event": {"type": "content_block_stop"}}


def test_claude_tells_each_step_as_it_lands_but_not_its_answer(worker):
    import line

    claude, got = _claude(worker)
    told = []
    claude.progress = told.append
    claude.pending = ["m1"]
    for obj in (_block("thinking"), _delta("thinking_delta", "thinking", "Is a release asked?"),
                STOP,
                _block("text"), _delta("text_delta", "text", "Checking the release state."),
                STOP,
                _block("tool_use", name="Bash"),
                _delta("input_json_delta", "partial_json", '{"command": "git log -1", '),
                _delta("input_json_delta", "partial_json", '"description": "the last commit"}'),
                STOP,
                _block("tool_use", name="Read"),
                _delta("input_json_delta", "partial_json", '{"file_path": "/a/b/notes.md"}'),
                STOP,
                _block("text"), _delta("text_delta", "text", "Ready, nothing released."), STOP,
                {"type": "result", "subtype": "success", "user_message_uuids": ["m1"]}):
        claude.take(line.stream_event(obj))
    assert told == ["Is a release asked?", "Checking the release state.",
                    "Ran the last commit", "Read notes.md"]
    assert got == [("Ready, nothing released.", None, False)]


async def test_progress_reaches_the_voice_as_a_silent_note_only_while_the_backend_works(worker):
    import line

    backend = FakeLive()
    runner, live, _ = _runner(worker, backend, [("user", "release?")])
    backend.progress("before")
    assert live.sent == []
    _delegate(live, "d1")
    await _written(runner)
    backend.progress("Ran the tests")
    assert live.sent == [("know", None, _spoken(line.progress("Ran the tests")))]
    backend.finish("Done.")
    backend.progress("after")
    assert len(live.sent) == 2


def test_progress_can_be_switched_off(worker):
    import line

    live, session, dialogue = Live(), Emitter(), line.Dialogue()
    backend = FakeLive()
    backend.progress = None
    worker.Runner(live, session, dialogue, backend, progress=False)
    assert backend.progress is None


def test_claude_reports_an_interrupted_or_failed_turn(worker):
    import line

    claude, got = _claude(worker)
    claude.pending = ["m1"]
    claude.take(line.stream_event({"type": "result", "subtype": "error_during_execution"}))
    claude.pending = ["m2"]
    claude.take(line.stream_event({"type": "result", "subtype": "error_max_turns",
                                   "is_error": True, "user_message_uuids": ["m2"]}))
    assert got[0] == ("", "the turn was interrupted", False)
    assert got[1][1] == "the turn ended with error_max_turns"
