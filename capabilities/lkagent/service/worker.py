"""The line's LiveKit worker: GPT Live speaks, the agent's backend session works.

Started by `lkagent service run` in the agent's folder, with the LiveKit connection and the
OpenAI key in the environment and the line in LKAGENT_LINE (JSON).

The call itself - its configuration, waiting for an outbound call to be answered, hanging
up, and reporting a finished call to a webhook - is callva-livekit's: this module passes it
parameters and holds no lifecycle of its own. The configuration is a file this worker writes
as it starts and names in CONFIG_URL; `lkagent call` sends the same body with the call's
variables in the dispatch metadata.

The voice is GPT Live under client delegation: it has no tools, and each turn it hands over
arrives here as a `delegation_created` with an id. What was said since the last handover is
written at once into the call's backend - on Claude one process kept for the whole call, where
a message written while a turn runs joins that turn - as AgentKit's voice mode writes a
handover into a session it runs. Once the backend has nothing left to work on, its answer goes
to the voice as the result of the latest handover. A line that goes silent, with nobody
speaking and the backend idle, is a message of its own, told the agent's liveness
instruction; what it answers, the voice says.

A call is taken only on one of the line's numbers, and runs under that number's rights: an
inbound call on the number it called, an outbound one on the number it was placed from. A
call on any other number is hung up at once and recorded as failed. The worker takes one
call at a time, and each call is recorded when it ends (calls.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from callva.livekit import call as callva_call
from callva.livekit import config as callva_config
from callva.livekit import webhook as callva_webhook
from callva.livekit.core import envelope as callva_envelope
from callva.livekit.core import identity as callva_identity
from callva.livekit.core import state as callva_state
from callva.livekit.webhook import payload as callva_payload
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobExecutorType, cli
from livekit.plugins.openai.realtime import GPTLiveModel

import calls
import line

logger = logging.getLogger("lkagent")

AGENT_NAME = os.environ.get("LKAGENT_AGENT_NAME", "").strip() or "lkagent-line"
LINE: dict = json.loads(os.environ.get("LKAGENT_LINE") or "{}")
SETTINGS: dict = LINE.get("settings") or {}
NUMBERS: list = line.entries(SETTINGS.get("numbers"))
AGENT_ROOT = LINE.get("agent_root") or os.getcwd()
STATE_DIR = Path(LINE.get("state_dir") or (Path(AGENT_ROOT) / ".lkagent-state"))
CONNECTION = LINE.get("connection") or "default"

RECORDER = calls.Recorder(AGENT_ROOT, LINE.get("agent_id"))


def line_load(srv: AgentServer) -> float:
    """This line's load: full at one call in progress, never judged by the machine's CPU."""
    return min(1.0, float(len(srv.active_jobs)))


# One process: jobs run as threads, so the service's own modules need no spawn-safe layout.
server = AgentServer(job_executor_type=JobExecutorType.THREAD, load_fnc=line_load,
                     load_threshold=1.0)

callva_webhook.collect_errors()


# ── the backend ─────────────────────────────────────────────────────────────

class Backend:
    """The call's harness session: one turn at a time, created by the first and resumed by
    each later one, through callva-harness-runner."""

    def __init__(self, profile: dict, env: dict, cwd: str) -> None:
        self.profile, self.env, self.cwd = profile, env, cwd
        self.session_id: str | None = None
        self._lock = threading.Lock()

    def run(self, prompt: str) -> tuple[str | None, str | None, bool]:
        """(answer, error, whether it timed out). Blocking."""
        from callva import harness_runner as hr

        with self._lock:
            try:
                profile = hr.Profile.from_dict(self.profile)
                if self.session_id:
                    session = hr.Session.resume(self.session_id)
                elif profile.harness == "claude":
                    session = hr.Session.pinned(str(uuid.uuid4()))
                else:
                    session = hr.Session.fresh()
                outcome = hr.run(prompt, profile, self.cwd, session=session, environ=self.env)
            except Exception as exc:
                logger.exception("backend turn failed")
                return None, f"{type(exc).__name__}: {exc}", False
            if outcome.session_id:
                self.session_id = outcome.session_id
            failure = outcome.failure
            if failure is None:
                return outcome.answer, None, False
            kind = str(getattr(failure.kind, "value", failure.kind))
            return None, f"{kind}: {(failure.message or '')[-300:]}", kind == "timeout"


Ended = Callable[[str | None, str | None, bool], None]
"""What a backend calls once no message is left to answer: (answer, error, timed out)."""


class ClaudeLive:
    """The call's backend on Claude: one `claude` process kept for the whole call, driven
    over stream-json as AgentKit's voice mode drives a session it runs. Every message is written into it
    at once: it starts a turn, or joins the turn under way at its next tool boundary, and
    otherwise runs after it. Once no message is left, the last answer text is the turn's
    answer. A process that ended is started again on the next message, continuing the same
    session."""

    def __init__(self, profile: dict, env: dict, cwd: str) -> None:
        self.profile, self.env, self.cwd = profile, line.claude_environment(env), cwd
        self.ended: Ended = lambda *_: None
        self.progress: Callable[[str], None] = lambda _text: None
        """Called with each step of the work as it lands: thinking, text between steps, and
        each tool call as a short label."""
        self.block: str | None = None
        self.thought = ""
        self.told = False
        self.tool, self.tool_input = "", ""
        self.session_id = str(uuid.uuid4())
        self.begun = False
        self.process: asyncio.subprocess.Process | None = None
        self.pending: list[str] = []
        self.answer = ""
        self.timeout = float(profile.get("timeout_seconds") or 600)
        self.timer: asyncio.Task | None = None
        self.timed_out = False
        self.closed = False
        self.tasks: set[asyncio.Task] = set()
        self.errors: deque = deque(maxlen=20)

    @property
    def running(self) -> bool:
        return bool(self.pending)

    def spawn(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def send(self, text: str) -> bool:
        """Write `text` into the process as the person's message; whether it was written."""
        if self.closed:
            return False
        uid = str(uuid.uuid4())
        try:
            if self.process is None or self.process.returncode is not None:
                await self.launch()
            self.pending.append(uid)
            self.write(line.user_message(text, uid))
            await self.process.stdin.drain()
        except Exception:
            logger.exception("the message could not be written to the backend")
            if uid in self.pending:
                self.pending.remove(uid)
            return False
        if self.timer is None:
            self.timer = asyncio.get_running_loop().create_task(self.watch())
        return True

    async def launch(self) -> None:
        executable = line.claude_executable()
        if executable is None:
            raise RuntimeError("claude is not installed")
        arguments = line.claude_arguments(self.profile, session_id=self.session_id,
                                          resume=self.begun)
        process = await asyncio.create_subprocess_exec(
            executable, *arguments, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, limit=64 * 1024 * 1024)
        logger.info("backend process started (%s session %s)",
                    "continuing" if self.begun else "new", self.session_id)
        self.process, self.begun = process, True
        self.spawn(self.read(process))
        self.spawn(self.read_errors(process))

    def write(self, text: str) -> None:
        if self.process is not None and self.process.stdin is not None:
            self.process.stdin.write(text.encode())

    async def read(self, process: asyncio.subprocess.Process) -> None:
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            try:
                event = line.stream_event(json.loads(raw))
            except ValueError:
                continue
            self.take(event)
        await process.wait()
        if self.process is process and self.pending and not self.closed:
            self.pending = []
            reason = self.errors[-1] if self.errors else f"exit status {process.returncode}"
            logger.error("the backend process ended during a turn: %s", reason)
            self.finish(None, f"the backend process ended: {reason}")

    async def read_errors(self, process: asyncio.subprocess.Process) -> None:
        while raw := await process.stderr.readline():
            self.errors.append(raw.decode(errors="replace").strip()[-300:])

    def take(self, event: tuple) -> None:
        kind = event[0]
        if kind == "text_started":
            self.answer, self.block, self.told = "", "text", False
        elif kind == "text":
            self.answer += event[1]
        elif kind == "thinking_started":
            self.block, self.thought = "thinking", ""
        elif kind == "thinking":
            self.thought += event[1]
        elif kind == "tool_started":
            # Text written before a step is what the backend says between steps, not its
            # answer, which is the text that ends the turn.
            if self.answer.strip() and not self.told:
                self.told = True
                self.progress(self.answer)
            self.block, self.tool, self.tool_input = "tool", event[1], ""
        elif kind == "input":
            self.tool_input += event[1]
        elif kind == "block_stopped":
            block, self.block = self.block, None
            if block == "thinking" and self.thought.strip():
                self.progress(self.thought)
            elif block == "tool":
                try:
                    arguments = json.loads(self.tool_input or "{}")
                except ValueError:
                    arguments = {}
                self.progress(line.step_label(self.tool, arguments))
        elif kind == "command":
            _, uid, state = event
            if state in ("completed", "cancelled") and uid in self.pending:
                self.pending.remove(uid)
        elif kind == "request":
            self.write(line.unhandled_answer(event[1], event[2]))
        elif kind == "ended":
            _, interrupted, error, uuids = event
            # The messages the turn took in are done; a process that does not list them ran
            # one message per turn.
            if uuids is not None:
                self.pending = [u for u in self.pending if u not in uuids]
            elif self.pending:
                self.pending.pop(0)
            if not self.pending:
                answer, self.answer = self.answer, ""
                self.finish(answer, error or ("the turn was interrupted" if interrupted else None))

    def finish(self, answer: str | None, error: str | None) -> None:
        timed_out, self.timed_out = self.timed_out, False
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.ended(answer, error, timed_out)

    async def watch(self) -> None:
        """Stop the work once it has run backend_timeout seconds; the process stays."""
        await asyncio.sleep(self.timeout)
        self.timer = None
        if self.pending:
            logger.warning("the backend ran past %ss; interrupting it", self.timeout)
            self.timed_out = True
            self.write(line.interrupt(f"interrupt-{uuid.uuid4().hex}"))

    async def close(self) -> None:
        """End the process: the turn under way interrupted, then its input closed, then
        killed if it still runs after a grace."""
        self.closed = True
        if self.timer is not None:
            self.timer.cancel()
        process = self.process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(Exception):
            if self.pending:
                self.write(line.interrupt(f"stop-{uuid.uuid4().hex}"))
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 3.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()


class CodexTurns:
    """The call's backend on Codex: each message a turn of its own through
    callva-harness-runner, resumed in one session and run one after another; a message
    written while a turn runs waits for it."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.ended: Ended = lambda *_: None
        self.progress: Callable[[str], None] = lambda _text: None
        """Never called: a turn through callva-harness-runner reports nothing until it ends."""
        self.queue: list[str] = []
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.task is not None

    async def send(self, text: str) -> bool:
        self.queue.append(text)
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self.drain())
        return True

    async def drain(self) -> None:
        answer, error, timed_out = None, None, False
        while self.queue:
            answer, error, timed_out = await asyncio.to_thread(self.backend.run,
                                                               self.queue.pop(0))
        self.task = None
        self.ended(answer, error, timed_out)

    async def close(self) -> None:
        self.queue.clear()


def make_backend(number: str | None, direction: str, room: str, variables: dict,
                 rights: dict) -> ClaudeLive | CodexTurns:
    """The call's backend, bound by the rights of the line's number the call is on: its
    capabilities, its read-only switch and its instructions."""
    auth = line.authority(rights, connection=CONNECTION, number=number, direction=direction)
    auth_file = line.write_authority(STATE_DIR, room, auth) if auth else None
    env = line.backend_env(dict(os.environ), authority_file=auth_file,
                           read_only=rights.get("read_only") is True)
    profile = line.backend_profile(SETTINGS, variables, rights.get("instructions"))
    if profile["harness"] == "claude":
        return ClaudeLive(profile, env, AGENT_ROOT)
    return CodexTurns(Backend(profile, env, AGENT_ROOT))


# ── answering delegations ───────────────────────────────────────────────────

OPEN, ANSWERED, DROPPED = "open", "answered", "dropped"


class AlreadyAnswered(Exception):
    pass


class Runner:
    """Every delegation one GPT Live session hands over, written into the backend at once.

    As AgentKit's voice mode does with a session it runs: a handover is written as the
    person's message whether or not the backend is working - on Claude it joins the turn
    under way - and the handovers not yet answered wait together. Once the backend has
    nothing left to work on, its answer goes to the voice as the result of the latest of
    them. A handover the backend did not take is answered as not delivered; one made over the
    goodbye after the backend ended the call is not written, and answered silently."""

    def __init__(self, live: Any, session: Any, dialogue: line.Dialogue,
                 backend: Any = None, *, progress: bool = True) -> None:
        self.live, self.session, self.dialogue = live, session, dialogue
        self.tells_progress = progress
        self.states: dict[str, str] = {}
        self.fell_back: set[str] = set()
        self.appends: dict[str, str] = {}
        self.waiting: list[str] = []
        """The handovers written and not yet answered, oldest first."""
        self.sending = 0
        self.held: Any = None
        self.ending = False
        self.closed = False
        self.started = time.monotonic()
        self.tasks: set[asyncio.Task] = set()
        self.hangup: asyncio.Task | None = None
        self.appended: dict | None = None
        self._backend: Any = None
        self.backend = backend
        self.listening = [
            (live, "delegation_created", self.on_delegation),
            (live, "session_reconnected", self.on_reconnected),
            (live, "openai_client_event_queued", self.on_sent),
            (live, "openai_server_event_received", self.on_received),
            (session, "close", self.on_close),
            (session, "agent_state_changed", self.on_agent_state),
        ]
        for emitter, event, handler in self.listening:
            emitter.on(event, handler)

    @property
    def backend(self) -> Any:
        return self._backend

    @backend.setter
    def backend(self, backend: Any) -> None:
        self._backend = backend
        if backend is not None:
            backend.ended = self.turn_ended
            if self.tells_progress:
                backend.progress = self.progress

    @property
    def busy(self) -> bool:
        """Whether a handover is being written or the backend works: the call is not quiet."""
        return self.held is not None

    def spawn(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # what arrives

    def on_delegation(self, delegation: Any) -> None:
        """A turn handed over. Spawned, never awaited: this runs inside the receive loop."""
        if self.closed:
            return
        delegation_id = delegation.id
        pending = getattr(delegation, "pending_transcript", "") or ""
        self.states[delegation_id] = OPEN
        logger.info("handover %s received: pending %d chars, backend %s", delegation_id,
                    len(pending), "working" if self.busy else "idle")
        if self.ending:
            # The backend ended the call; a request made over the goodbye is not written.
            self.think(delegation_id, line.SUPERSEDED)
            return
        said = self.dialogue.hand_over(pending)
        if not said:
            self.answer(delegation_id, line.NOTHING_TO_HAND_OVER)
            return
        self.hand(line.handover(said), delegation_id)

    def on_reconnected(self, _event: Any = None) -> None:
        """A new provider session, where every id handed out so far means nothing."""
        dead = [i for i, state in self.states.items() if state == OPEN]
        if dead:
            logger.warning("gpt live reconnected with %s delegation(s) in flight", len(dead))
        for delegation_id in dead:
            self.states[delegation_id] = DROPPED
        self.appends.clear()

    def on_close(self, _event: Any = None) -> None:
        if self.closed:
            return
        self.closed = True
        for emitter, event, handler in self.listening:
            with contextlib.suppress(Exception):
                emitter.off(event, handler)
        for task in list(self.tasks):
            task.cancel()
        self.release()
        if self.backend is not None:
            self.spawn(self.backend.close())

    def on_sent(self, raw: Any) -> None:
        if not isinstance(raw, dict) or raw.get("type") not in (
                "session.commentary.append", "session.thinking.append"):
            return
        delegation_id = raw.get("delegation_id")
        if delegation_id in self.states and (event_id := raw.get("event_id")):
            self.appends[event_id] = delegation_id

    def on_received(self, event: Any) -> None:
        """A refused answer is answered once more, silently."""
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind in ("session.commentary.appended", "session.thinking.appended"):
            self.appends.pop(event.get("client_event_id") or "", None)
        elif kind == "error":
            error = event.get("error") or {}
            delegation_id = self.appends.pop(error.get("client_event_id") or "", None)
            if delegation_id is None:
                return
            logger.error("the answer to delegation %s was refused: %s", delegation_id,
                         error.get("message") or error.get("code"))
            if delegation_id not in self.fell_back and self.states.get(delegation_id) != DROPPED:
                self.fell_back.add(delegation_id)
                self.live.append_thinking(line.REFUSED, delegation_id=delegation_id)

    def on_agent_state(self, event: Any) -> None:
        if getattr(event, "new_state", None) == "speaking" and self.appended is not None:
            logger.info("voice started speaking the answer to %s %.2fs after it was appended",
                        self.appended["id"], time.monotonic() - self.appended["at"])
            self.appended = None

    # a silent line

    def liveness(self, instruction: str) -> None:
        """Run a backend turn for a line that has gone quiet, told ``instruction``.

        The quiet watch only fires with nothing in flight, so a turn already running is the
        answer to the line and none is added. The turn is a delegation in all but its id: the
        same backend session and answering, with what was said since the last handover, then
        ``instruction`` under :data:`line.LIVENESS`. Its words go to the voice to speak,
        attached to nothing; a turn that says nothing speaks nothing."""
        if self.closed or self.ending:
            return
        if self.busy:
            logger.info("the line went quiet with the backend working; nothing added")
            return
        if self.backend is None:
            logger.warning("the line went quiet before the backend was attached; nothing sent")
            return
        logger.info("the line went quiet; the backend is told")
        self.hand(line.liveness(self.dialogue.hand_over(), instruction), None)

    # writing into the backend

    def hold(self) -> None:
        """The call is not quiet while the backend works, as with a tool in flight."""
        if self.held is None:
            held = callva_call.busy()
            held.__enter__()
            self.held = held
            self.started = time.monotonic()

    def release(self) -> None:
        if self.held is not None:
            held, self.held = self.held, None
            held.__exit__(None, None, None)

    def hand(self, text: str, delegation_id: str | None) -> None:
        """Write `text` into the backend; a handover's id waits for the answer once written."""
        self.hold()
        self.sending += 1
        self.spawn(self.deliver(text, delegation_id))

    async def deliver(self, text: str, delegation_id: str | None) -> None:
        try:
            written = self.backend is not None and await self.backend.send(text)
        finally:
            self.sending -= 1
        if self.closed:
            return
        if written:
            if delegation_id is not None:
                self.waiting.append(delegation_id)
            logger.info("%s written into the backend (%d waiting)",
                        delegation_id or "a silent line", len(self.waiting))
            return
        if delegation_id is not None:
            self.answer(delegation_id, line.not_delivered(line.NOT_TAKEN))
        if not self.sending and not (self.backend is not None and self.backend.running):
            self.release()

    def progress(self, text: str) -> None:
        """A step of the backend's work as it lands, for the voice to know and not to say:
        a silent note, attached to nothing, while the backend works."""
        if self.closed or self.ending or not self.busy or not text.strip():
            return
        pieces = line.chunks(line.progress(text))
        for piece in pieces:
            self.live.append_thinking(piece, delegation_id=None)
        logger.info("progress to the voice: %s", " ".join(text.split())[:120])

    def turn_ended(self, answer: str | None, error: str | None, timed_out: bool) -> None:
        """The backend has nothing left to work on: its answer is the result of the latest
        handover waiting, or, with none waiting, the voice's to say as it is."""
        if self.closed:
            return
        logger.info("backend done after %.1fs (%d chars%s)", time.monotonic() - self.started,
                    len(answer or ""), f", error {error[:120]}" if error else "")
        if not self.sending:
            self.release()
        waiting, self.waiting = self.waiting, []
        target = waiting[-1] if waiting else None
        if error:
            if target is not None:
                self.think(target, line.TIMED_OUT if timed_out else line.FAILED)
            return
        spoken, invoked, cut = line.speakable(answer or "")
        if cut:
            logger.warning("the backend wrote %s into its text; cut before the voice hears it",
                           ", ".join(repr(c[:120]) for c in cut))
        if line.END_CALL in invoked:
            self.ending = True
            logger.info("tool end_call: the call ends once the goodbye is spoken")
            goodbye = bool(spoken.strip())
            if goodbye:
                self.say(target, spoken)
            elif target is not None:
                self.think(target, line.ENDED)
            self.hangup = asyncio.get_running_loop().create_task(self.hang_up(goodbye))
            return
        self.say(target, spoken)

    # answering, exactly once

    def settle(self, delegation_id: str) -> str | None:
        state = self.states.get(delegation_id)
        if state == ANSWERED:
            raise AlreadyAnswered(delegation_id)
        if state == DROPPED:
            return None
        self.states[delegation_id] = ANSWERED
        return delegation_id

    def say(self, delegation_id: str | None, spoken: str) -> None:
        """The backend's answer, for the voice to say: a handover's as a result under its
        id; one no handover waits for - a silent line's - as it is, attached to nothing, and
        nothing where it is empty."""
        if delegation_id is None:
            pieces = line.chunks(spoken) if spoken.strip() else []
            for piece in pieces:
                self.live.append_commentary(piece, delegation_id=None)
            logger.info("the backend answered with no handover waiting: %d piece(s) to say",
                        len(pieces))
            return
        self.answer(delegation_id, line.result(spoken), late=spoken)

    def answer(self, delegation_id: str, text: str, late: str | None = None) -> None:
        """Something for the voice to say, under the delegation it answers."""
        try:
            target = self.settle(delegation_id)
        except AlreadyAnswered:
            logger.warning("delegation %s was already answered; not answering twice",
                           delegation_id)
            return
        if target is None:
            self.live.append_thinking(line.fit(line.LATE_RESULT.format(results=late or text)),
                                      delegation_id=None)
            return
        pieces = line.chunks(text)
        for piece in pieces:
            self.live.append_commentary(piece, delegation_id=target)
        self.appended = {"id": target, "at": time.monotonic()}
        logger.info("answer appended to the voice for %s: %d piece(s), voice %s", target,
                    len(pieces), getattr(self.session, "agent_state", "?"))

    def think(self, delegation_id: str, text: str) -> None:
        """Something for the voice to know without saying, answering the delegation."""
        try:
            target = self.settle(delegation_id)
        except AlreadyAnswered:
            logger.warning("delegation %s was already answered; not answering twice",
                           delegation_id)
            return
        self.live.append_thinking(line.fit(text), delegation_id=target)

    # hanging up

    async def hang_up(self, goodbye: bool) -> None:
        """End the call once the goodbye has started, and never before it could have; the
        hangup itself waits for the voice to finish."""
        if goodbye:
            with contextlib.suppress(Exception):
                await until_speaking(self.session, line.SPEAKING_WAIT)
        await callva_call.end(reason="the agent ended the call", session=self.session)


async def until_speaking(session: Any, timeout: float) -> bool:
    if getattr(session, "agent_state", None) == "speaking":
        return True
    started = asyncio.Event()

    def on_state_changed(event: Any) -> None:
        if getattr(event, "new_state", None) == "speaking":
            started.set()

    session.on("agent_state_changed", on_state_changed)
    try:
        await asyncio.wait_for(started.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        logger.warning("the goodbye did not start within %ss; hanging up anyway", timeout)
        return False
    finally:
        with contextlib.suppress(Exception):
            session.off("agent_state_changed", on_state_changed)


# ── the call ────────────────────────────────────────────────────────────────

class LineAgent(Agent):
    """No tools: under client delegation the provider refuses a session whose agent carries
    any. The runner attaches as the agent enters, the first moment the provider's own
    session exists."""

    def __init__(self, *, instructions: str, dialogue: line.Dialogue) -> None:
        super().__init__(instructions=instructions)
        self.dialogue = dialogue
        self.backend: ClaudeLive | CodexTurns | None = None
        self.runner: Runner | None = None

    def set_backend(self, backend: ClaudeLive | CodexTurns) -> None:
        self.backend = backend
        if self.runner is not None:
            self.runner.backend = backend

    async def on_enter(self) -> None:
        try:
            self.runner = Runner(self.duplex_session, self.session, self.dialogue, self.backend,
                                 progress=SETTINGS.get("backend_progress") is not False)
        except Exception:
            logger.exception("the backend for this call could not be attached; ending the call")
            await callva_call.end(reason="the backend could not be attached", wait=False,
                                  ended_by=None)


# ── the call's record ───────────────────────────────────────────────────────

ENDED_BODY = "lkagent.ended_body"
CALL_RECORD = "lkagent.record"


def _keep_ended_body() -> None:
    """Keep the `call.ended` body callva-livekit builds on the call's state, so the call's
    row is read from the very body the webhook gets, whether or not a webhook is set."""
    build = callva_payload.build
    if getattr(build, "keeps_ended_body", False):
        return

    def kept(st: Any, *, event: str, **kwargs: Any) -> dict:
        body = build(st, event=event, **kwargs)
        if event == callva_payload.ENDED:
            st.extras[ENDED_BODY] = body
        return body

    kept.keeps_ended_body = True
    callva_payload.build = kept


_keep_ended_body()


def remember(ctx: Any, **facts: Any) -> None:
    """What only this line knows about the call, for its row: its backend, its task."""
    callva_state.state(ctx).extras.setdefault(CALL_RECORD, {}).update(facts)


def backend_session_id(backend: Any) -> str | None:
    """The harness session a call's backend ran in: the Claude process's session, or the
    Codex session its first turn made."""
    if backend is None:
        return None
    return getattr(backend, "session_id", None) or getattr(
        getattr(backend, "backend", None), "session_id", None)


async def on_session_end(ctx: JobContext) -> None:
    """Report the finished call as callva-livekit does, then record it."""
    await callva_webhook.on_session_end(ctx)
    extras = callva_state.state(ctx).extras
    body = extras.get(ENDED_BODY)
    if body is None:
        logger.warning("the call ended without a call.ended body; it is not recorded")
        return
    facts = extras.get(CALL_RECORD) or {}
    await asyncio.to_thread(RECORDER.record, body,
                            backend_session_id=backend_session_id(facts.get("backend")),
                            task=facts.get("task"))


# ── the call ────────────────────────────────────────────────────────────────

async def refuse(ctx: JobContext, participant: Any, direction: str, reason: str) -> None:
    """Hang up a call this line takes no call on, at once, and leave it failed with its
    reason for the call's record and webhook."""
    st = callva_state.state(ctx)
    st.failure = reason
    st.unanswered_reason = reason
    callva_state.ensure_identity(st, participant=participant, direction=direction)
    logger.warning("call refused: %s", reason)
    await callva_call.end(ctx, reason=reason, wait=False, ended_by=None)


def sip_participant(ctx: JobContext) -> Any | None:
    """The caller already in the room, if any: an inbound call's is there as the job starts."""
    for participant in (getattr(ctx.room, "remote_participants", None) or {}).values():
        if (getattr(participant, "attributes", None) or {}).get(line.CALLED_NUMBER):
            return participant
    return None


async def start_session(session: Any, agent: Any, room: Any) -> None:
    """Start the call's session; it records only where the line's `recording` is on."""
    await session.start(agent=agent, room=room, record=line.recording(SETTINGS))


@server.rtc_session(agent_name=AGENT_NAME, on_session_end=on_session_end)
@callva_call.hangs_up_on_failure
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    try:
        config = await callva_config.load()
    except callva_config.ConfigError:
        logger.warning("no configuration for this call; it has already been ended")
        return

    # Which of the line's numbers the call is on, before anything is started for it.
    envelope = callva_envelope.parse(ctx.job.metadata)
    direction = callva_identity.resolve_direction(envelope)
    caller = None
    if direction == callva_identity.INBOUND:
        caller = sip_participant(ctx) or await callva_call.await_pickup(ctx)
        if caller is None:
            await callva_call.end(ctx, reason="the call was not answered", wait=False)
            return
    on_number = line.line_number(direction, getattr(caller, "attributes", None),
                                 envelope.from_number)
    rights = line.number_entry(NUMBERS, on_number)
    if rights is None:
        await refuse(ctx, caller, direction, line.unconfigured(on_number))
        return

    session = AgentSession(llm=GPTLiveModel(voice=line.voice(SETTINGS.get("voice")),
                                            delegation="client"))
    # Deaf until someone is on the call: a ringing line is not a silent one.
    session.input.set_audio_enabled(False)
    callva_webhook.attach(session)

    dialogue = line.Dialogue()
    agent = LineAgent(instructions=config.prompt or line.DEFAULT_PROMPT, dialogue=dialogue)

    @session.on("conversation_item_added")
    def _heard(event: Any) -> None:
        item = event.item
        role = getattr(item, "role", None)
        if role in ("user", "assistant"):
            dialogue.hear(role, item.text_content)

    await start_session(session, agent, ctx.room)

    participant = await callva_call.await_pickup(ctx)
    if participant is None:
        await callva_call.end(ctx, reason="the call was not answered", wait=False)
        return

    number = (participant.attributes or {}).get("sip.phoneNumber") or participant.identity
    variables = dict(config.variables)
    backend = await asyncio.to_thread(make_backend, number, direction, ctx.room.name,
                                      variables, rights)
    agent.set_backend(backend)
    remember(ctx, backend=backend, task=variables.get("task") or None)

    session.input.set_audio_enabled(True)

    # How long the call may run and how a silent line is met are the agent's settings, passed
    # through untouched; a silent line is a backend turn, whatever phrases were written.
    when_quiet = line.liveness_instruction(SETTINGS.get("liveness_instruction"))

    async def utter(_phrase: str | None) -> None:
        if agent.runner is not None:
            agent.runner.liveness(when_quiet)

    callva_call.supervise(
        session,
        max_duration=config.agent.max_duration,
        silence_timeout=config.agent.user_silence_timeout,
        prompt_phrases=config.agent.prompt_phrases,
        max_prompt_attempts=config.agent.max_prompt_attempts,
        call_silence_timeout=config.agent.call_silence_timeout,
        utter=utter,
    )

    what = line.opening(config)
    instruction = line.opening_instruction(what)
    if instruction is None:
        logger.info("this call opens in silence")
    else:
        agent.duplex_session.append_instructions(instruction)


# ── the worker ──────────────────────────────────────────────────────────────

def agent_identity() -> tuple[str, str]:
    """The agent's name and description from its record, else the folder's name."""
    found = (Path(AGENT_ROOT).name, "")
    try:
        out = subprocess.run(["capabilities", "agent", "show"], cwd=AGENT_ROOT,
                             capture_output=True, text=True, timeout=20)
        data = json.loads(out.stdout or "{}")
        agent = data.get("agent", data)
        found = (agent.get("name") or found[0], agent.get("description") or "")
    except Exception as exc:
        logger.warning("agent identity not read: %s", exc)
    return found


def prepare() -> Path:
    """Write the line's call configuration and name it in CONFIG_URL, before any call."""
    name, description = agent_identity()
    path = line.write_config(STATE_DIR, line.call_config(SETTINGS, name=name,
                                                          description=description))
    os.environ["CONFIG_URL"] = str(path)
    return path


def run(mode: str = "start") -> None:
    """Register with the LiveKit server and serve calls until stopped."""
    import sys

    prepare()
    sys.argv = ["lkagent", mode]
    cli.run_app(server)
