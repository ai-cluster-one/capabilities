"""One answered Telegram call bridged to one OpenAI GPT Live session.

The media path is the daemon's own PyTgCalls instance, exactly as the Gemini
provider uses it: `record()` owns the inbound slot, `play()` the outbound one.
GPT Live speaks PCM16 mono and its `audio.format` governs both directions at
once, so the whole tract runs at 24 kHz and nothing is resampled anywhere.

Where this differs from the Gemini provider, and why:

**The model owns turn taking and never reports it.** There is no turn-detection
setting, no interruption event, no way to cancel a response and no way to clear
what is already queued to speak. The output stream is continuous and carries the
model's own silence, so "is it speaking" is answered here by a level gate rather
than by anything the service says.

**Delegation replaces tools.** The model asks for help by emitting
`session.delegation.created`, which carries an id and nothing else — no task
text. What the caller asked for is whatever was said, so this module keeps the
open user turn and hands that over. The answer goes back as
`session.commentary.append`, which the model paraphrases aloud in its own voice.

**Progress is free of the silence window.** The Gemini provider has to smuggle a
progress line into a gap between turns, because the only way to tell that model
anything is to hand it something to say. Here `session.thinking.append` is
silent by construction: the model is told, and decides for itself whether any of
it is worth saying. So the quiet-window machinery has no counterpart here, and
progress is merely batched.

**Every delegation is answered, always.** The service tracks unanswered work at
session scope, so one delegation left hanging is capable of holding up
everything after it. A refusal, a timeout and a crash therefore each end in an
append, and never in silence.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from array import array

from pytgcalls.exceptions import NotInCallError
from pytgcalls.types import Device, Frame

import voice_agent as common

DEFAULT_MODEL = "gpt-live-1"
DEFAULT_VOICE = "marin"

# The voices this stack has. The setting that names one is shared with the other
# provider, whose vocabulary is different and whose names the API's own type
# accepts as plain strings — a `voice` field typed `str` refuses nothing. A name
# from elsewhere reaching the service kills the session at start, which reads as
# a broken call rather than as a misconfigured voice, so it is caught here.
BUILT_IN_VOICES = frozenset({
    "alloy", "ash", "ballad", "beacon", "bossa", "cedar", "cinder", "coral",
    "delta", "echo", "gleam", "marin", "meridian", "quartz", "ripple", "sage",
    "shimmer", "stone", "tempo", "verse", "vesper", "willow",
})


def resolve_model(model, log=print):
    """The model to open with. `model` is shared with the other provider, whose
    names mean nothing here, so a name from elsewhere is replaced rather than
    sent."""
    name = str(model or "").strip()
    if not name:
        return DEFAULT_MODEL
    if name.lower().startswith("gpt-live"):
        return name
    log(f"voice: {name!r} is not a gpt-live model; opening with {DEFAULT_MODEL!r}")
    return DEFAULT_MODEL


def resolve_voice(voice, log=print):
    """The voice to open with. A custom voice arrives as a mapping carrying its
    id and is passed through; anything else has to be a name this stack knows."""
    if isinstance(voice, dict):
        return voice
    name = str(voice or "").strip()
    if not name:
        return DEFAULT_VOICE
    if name.lower() in BUILT_IN_VOICES:
        return name.lower()
    log(f"voice: {name!r} is not a gpt-live voice; opening with {DEFAULT_VOICE!r}")
    return DEFAULT_VOICE

# One format field governs both directions, so a lower input rate would also
# lower the voice. `audio/pcm` accepts 16000 and 24000; 24000 is the rate the
# voice is worth.
CALLER_RATE = 24000
AGENT_RATE = 24000
FRAME_SECONDS = common.FRAME_SECONDS
CALLER_FRAME_BYTES = int(CALLER_RATE * 2 * FRAME_SECONDS)
AGENT_FRAME_BYTES = int(AGENT_RATE * 2 * FRAME_SECONDS)
AGENT_SILENCE = b"\x00" * AGENT_FRAME_BYTES
# ~100 ms per append, the granularity the protocol is built around.
INPUT_CHUNK_BYTES = CALLER_FRAME_BYTES * 10
INPUT_QUEUE_FRAMES = common.INPUT_QUEUE_FRAMES

# The service caps one append at 500 tokens and enforces it; there is no
# tokenizer here, so the budget is held in characters with room to spare.
APPEND_CHAR_BUDGET = 1500

# How often the worker's completion is checked for, which also bounds how
# promptly progress is noticed. The cadence progress actually reaches the model
# at is the project's `progress_interval`: every append is permanent context, so
# how chatty a call's background narration should be is a project's call, not a
# constant's.
PROGRESS_POLL_SECONDS = 1.0

# The output stream carries the model's own silence, so speech is told from it
# by level. Opening high and closing low keeps a breath between words from
# reading as the end of a turn.
SPEECH_RMS_OPEN = 600
SPEECH_RMS_CLOSE = 300

# What the worker is handed instead of a request. The delegation carries no
# task text, so inventing one means scraping the caller's speech and hoping the
# scrape is right — which is a fiction with its own failure modes. The turn was
# handed over mid-conversation, so the conversation is where the work is, and
# saying so is both true and more robust than any ask we could assemble.
#
# The last line of that conversation is usually the speaking model acknowledging
# the hand-off rather than stating the work: the delegation is raised before it
# speaks, and its account of what it is doing arrives seconds later. So the
# frame says to read the acknowledgement as the commitment it is, and to take
# the work itself from what the caller asked for.
HANDED_TURN_REQUEST = (
    "You have just been handed the turn from a live phone call. This is not a "
    "message someone sent you: nobody wrote this request, and there is no task "
    "text to read. What has to be done is in the conversation below.\n\n"
    "The voice on that call is your own speaking half. It hears the caller and "
    "speaks for you, it cannot do anything itself, and it hands the turn over "
    "the moment something has to actually be done. A line marked as yours is "
    "therefore you, not another participant — when it says it will go and look "
    "at something, that is a promise you are now expected to keep.\n\n"
    "Read the conversation to its end. The last thing your speaking half said "
    "is usually a short acknowledgement rather than a statement of the work, so "
    "take the work from what the caller actually asked for, and read the "
    "acknowledgement only as a sign of which of their requests is the live one. "
    "Where the caller refers to something by 'it' or 'they', resolve it from "
    "what was said earlier on the call.\n\n"
    "Do that one thing, and answer it.")

CONNECT_TIMEOUT = 20.0
CLOSE_DRAIN_TIMEOUT = 5.0


def _rms(payload: bytes) -> float:
    """Level of one PCM16 frame, used only to tell speech from the silence the
    output stream carries between turns."""
    if len(payload) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(payload[:len(payload) // 2 * 2])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def chunk_for_append(text: str, budget: int = APPEND_CHAR_BUDGET) -> list[str]:
    """One long answer as the several appends the cap allows, split where a
    sentence ends so each piece is speakable on its own."""
    body = " ".join(str(text or "").split())
    if not body:
        return []
    if len(body) <= budget:
        return [body]
    pieces, rest = [], body
    while len(rest) > budget:
        window = rest[:budget]
        cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
        if cut < budget // 3:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = budget
        else:
            cut += 1
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


class VoiceCallSession:
    """One answered call bridged to one GPT Live session."""

    def __init__(self, calls, chat_id, *, api_key, model, voice,
                 system_instruction, caller_name, caller_track=None,
                 agent_track=None, task_runner=None, send_to_chat=None,
                 capability_runner=None, file_reader=None, on_stream_end=None,
                 reload_service=None, greeting=None, assistant_name=None,
                 progress_interval=common.DEFAULT_PROGRESS_INTERVAL, log=print):
        self._calls = calls
        self._chat_id = chat_id
        self._api_key = api_key
        self._model = resolve_model(model, log)
        self._voice = resolve_voice(voice, log)
        self._system_instruction = system_instruction
        self._caller_name = caller_name
        self._assistant_name = assistant_name or "Assistant"
        self._task_runner = task_runner
        # Carried for interface parity with the Gemini provider. Client
        # delegation has no tool channel, so the model cannot reach these; the
        # worker brings its own. Kept so the daemon constructs both the same way.
        self._send_to_chat = send_to_chat
        self._capability_runner = capability_runner
        self._file_reader = file_reader
        self._reload_service = reload_service
        self._greeting = greeting
        self._on_stream_end = on_stream_end
        self._stream_ended = False
        self._log = log

        self._client = None
        self._conn = None
        self._live_ready = asyncio.Event()
        self._closing = False
        self._session_id = None
        self._close_reason = None
        self._tasks = []
        self._pending_input = bytearray()
        self._input_queue = asyncio.Queue(maxsize=INPUT_QUEUE_FRAMES)
        self._outbound = bytearray()
        self._turns = []
        self._pump_error = None

        # One time origin for both tracks, fixed before the call is answered.
        self.origin = time.monotonic()
        self.window_seconds = None
        self._caller_writer = (
            common._TrackWriter(caller_track, CALLER_RATE, self.origin)
            if caller_track is not None else None)
        self._agent_writer = (
            common._TrackWriter(agent_track, AGENT_RATE, self.origin)
            if agent_track is not None else None)
        self.caller_bytes = 0
        self.agent_frames = 0
        self.agent_voiced_frames = 0
        self.interruptions = 0
        self.dropped_input_chunks = 0
        self.messages_sent = 0

        # The open user turn. The delegation event carries no task text, so this
        # is what the backend is actually given.
        self._user_turn = []
        self._last_fragment_at = 0.0
        self._traced_at = 0.0
        self._traced_turn = None

        # Asked for while the one worker was busy, and not lost because of it.
        # One worker at a time is about not having two of them racing their
        # progress and their answers into one conversation; it was never about
        # dropping the second question on the floor.
        self._deferred = []
        # Delegations in flight, by id, and the job each became.
        self._delegations = {}
        self._jobs_to_delegation = {}
        self.delegations_seen = 0
        self.delegations_answered = 0

        # Progress, coalesced per delegation.
        self._progress_window = []
        self._progress_flushed_at = 0.0
        self._progress_interval = max(1.0, float(
            progress_interval or common.DEFAULT_PROGRESS_INTERVAL))

        self._speaking = False
        self._usage_seconds = 0.0
        self._context_ratio = None
        # What this call actually costs the model's context, per channel, so the
        # question "are the background notes cluttering it" has a number rather
        # than an opinion.
        self._append_chars = {}

    # --- media -------------------------------------------------------------

    def start_pump(self):
        """Start the paced capture pump before the session is up, so the
        outbound slot never stalls: a tick with nothing buffered sends
        silence."""
        self._tasks.append(asyncio.create_task(self._outbound_pump()))

    def on_incoming_frames(self, frames):
        """Called from the PyTgCalls stream-frame update; must not block."""
        for frame in frames or ():
            payload = getattr(frame, "frame", None)
            if not payload:
                continue
            self.caller_bytes += len(payload)
            if self._caller_writer is not None:
                self._caller_writer.write(payload)
            self._pending_input.extend(payload)
            while len(self._pending_input) >= INPUT_CHUNK_BYTES:
                chunk = bytes(self._pending_input[:INPUT_CHUNK_BYTES])
                del self._pending_input[:INPUT_CHUNK_BYTES]
                try:
                    self._input_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    # Backlog means the uplink is behind; the newest audio is
                    # the audio worth keeping.
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self._input_queue.get_nowait()
                    self.dropped_input_chunks += 1
                    with contextlib.suppress(asyncio.QueueFull):
                        self._input_queue.put_nowait(chunk)

    async def _outbound_pump(self):
        """Feed the capture slot at real time. GPT Live streams continuously, so
        the buffer normally holds about one tick; silence covers a gap."""
        next_tick = time.monotonic()
        while True:
            try:
                if len(self._outbound) >= AGENT_FRAME_BYTES:
                    payload = bytes(self._outbound[:AGENT_FRAME_BYTES])
                    del self._outbound[:AGENT_FRAME_BYTES]
                else:
                    payload = AGENT_SILENCE
                self.agent_frames += 1
                if self._agent_writer is not None:
                    self._agent_writer.write(payload)
                try:
                    await self._calls.send_frame(
                        self._chat_id, Device.MICROPHONE, payload,
                        Frame.Info(capture_time=int(time.time() * 1000)))
                except NotInCallError:
                    await self._note_stream_end("not_in_call")
                    return
                next_tick += FRAME_SECONDS
                delay = next_tick - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_tick = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._pump_error = f"{type(exc).__name__}: {exc}"[:300]
                self._log(f"voice: outbound pump stopped — {self._pump_error}")
                await self._note_stream_end("pump_error")
                return

    async def _note_stream_end(self, reason):
        if self._stream_ended:
            return
        self._stream_ended = True
        self._log(f"voice: media stream ended ({reason})")
        if self._on_stream_end is None:
            return
        try:
            await self._on_stream_end()
        except Exception as exc:
            self._log(f"voice: could not end the call after the stream stopped — "
                      f"{type(exc).__name__}: {exc}")

    # --- prompt ------------------------------------------------------------

    def set_system_instruction(self, system_instruction):
        """Set before the session opens. GPT Live freezes `instructions` at
        start; anything later has to go through an instructions append."""
        self._system_instruction = system_instruction

    def _call_facts(self):
        """Who is on this call, stated rather than left to be inferred.

        The project's prompt says who the assistant is to everyone; the chat tail
        shows names in passing. Neither says plainly that this is a phone call,
        who picked it up, or which of the names in the tail is the person now
        speaking — and a voice model that has to work that out from context gets
        it wrong exactly when a call opens, before there is any context to work
        from."""
        lines = [f"You are {self._assistant_name}."]
        if self._caller_name:
            lines.append(f"{self._caller_name} is calling you, and is on the line now.")
        lines.append("This is a Telegram voice call: everything here is spoken, "
                     "both ways, in real time.")
        return "--- This call ---\n\n" + "\n".join(lines)

    def _session_config(self):
        instructions = "\n\n".join(
            part for part in (self._call_facts(), self._system_instruction) if part)
        return {
            "model": self._model,
            "instructions": instructions,
            "audio": {
                "format": {"type": "audio/pcm", "rate": CALLER_RATE},
                "output": {"voice": self._voice},
            },
            "delegation": {"type": "client"},
        }

    # --- session -----------------------------------------------------------

    async def start_agent(self):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=self._api_key)
        self._tasks.append(asyncio.create_task(self._run_session()))
        try:
            await asyncio.wait_for(self._live_ready.wait(), CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise common.VoiceAgentError(
                f"gpt-live session did not start within {CONNECT_TIMEOUT}s") from exc
        self._tasks.append(asyncio.create_task(self._sender()))
        # Commentary, not instructions. An instructions append is a standing
        # rule: the model reads it, keeps it, and goes on waiting for the caller,
        # which is a call that opens in silence. Commentary is something to say,
        # and saying it now is what the last two sentences are for. Always sent,
        # not only where the project wrote a line — the shared helper carries a
        # default for exactly this moment.
        await self._append("commentary", (
            common.greeting_prompt(self._caller_name, self._greeting)
            + " Say this now, in your own words and in the language of this "
            "instruction. Do not wait for the caller to speak first. "
            "After that, pause and listen."))

    async def _run_session(self):
        try:
            async with self._client.live.connect() as conn:
                self._conn = conn
                await conn.session.start(session=self._session_config())
                async for event in conn:
                    await self._handle(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(f"voice: gpt-live session ended — "
                      f"{type(exc).__name__}: {exc}"[:300])
        finally:
            self._conn = None
            self._live_ready.clear()
            await self._note_stream_end(self._close_reason or "session_ended")

    async def _sender(self):
        while True:
            chunk = await self._input_queue.get()
            conn = self._conn
            if conn is None:
                continue
            try:
                await conn.session.input_audio.append(
                    audio=base64.b64encode(chunk).decode("ascii"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(f"voice: cannot send audio — "
                          f"{type(exc).__name__}: {exc}"[:200])
                return

    async def _append(self, channel, content, delegation_id=None):
        """One append on one of the three context channels. Never raises into a
        caller: a delegation that cannot be answered must still not take the
        receive loop down with it."""
        conn = self._conn
        if conn is None or not content:
            return False
        resource = getattr(conn.session, channel)
        try:
            await resource.append(content=content, delegation_id=delegation_id)
            self.messages_sent += 1
            self._append_chars[channel] = (
                self._append_chars.get(channel, 0) + len(content))
            return True
        except Exception as exc:
            self._log(f"voice: {channel} append failed — "
                      f"{type(exc).__name__}: {exc}"[:200])
            return False

    # --- events ------------------------------------------------------------

    async def _handle(self, event):
        kind = getattr(event, "type", "")
        if kind == "session.started":
            self._session_id = getattr(getattr(event, "session", None), "id", None)
            self._live_ready.set()
            self._log(f"voice: gpt-live session started ({self._session_id}) "
                      f"model={self._model} voice={self._voice}")
        elif kind == "session.output_audio.delta":
            self._on_output_audio(event)
        elif kind == "session.input_transcript.delta":
            self._on_input_transcript(event)
        elif kind == "session.output_transcript.delta":
            self._record_fragment("agent", getattr(event, "delta", ""))
        elif kind == "session.delegation.created":
            self._on_delegation(event)
        elif kind == "session.usage.updated":
            usage = getattr(event, "usage", None)
            self._usage_seconds = float(getattr(usage, "seconds", 0) or 0)
            window = getattr(event, "context_window", None)
            ratio = getattr(window, "usage_ratio", None)
            if ratio is not None:
                was = self._context_ratio
                self._context_ratio = float(ratio)
                # At 0.9 the service silently swaps in a replacement engine that
                # keeps only the instructions and 8192 tokens of history, so the
                # approach to it is worth seeing rather than discovering.
                if was is None or int(ratio * 20) != int(was * 20):
                    appended = ", ".join(
                        f"{channel}={chars}c"
                        for channel, chars in sorted(self._append_chars.items()))
                    self._log(f"voice: context {ratio:.1%} used, "
                              f"{self._usage_seconds:.0f}s billed"
                              + (f", appended {appended}" if appended else ""))
        elif kind == "session.closed":
            self._close_reason = str(getattr(event, "reason", "") or "closed")
            self._log(f"voice: gpt-live closed ({self._close_reason}), "
                      f"{self._usage_seconds:.0f}s billed")
        elif kind == "error":
            body = getattr(event, "error", None)
            self._log("voice: gpt-live error — "
                      f"{getattr(body, 'code', None) or getattr(body, 'type', None)}: "
                      f"{getattr(body, 'message', '')}"[:300])

    def _on_output_audio(self, event):
        payload = base64.b64decode(getattr(event, "delta", "") or "")
        if not payload:
            return
        level = _rms(payload)
        if self._speaking:
            if level < SPEECH_RMS_CLOSE:
                self._speaking = False
        elif level > SPEECH_RMS_OPEN:
            self._speaking = True
        if self._speaking:
            self.agent_voiced_frames += 1
        self._outbound.extend(payload)

    def _on_input_transcript(self, event):
        delta = getattr(event, "delta", "") or ""
        if not delta.strip():
            return
        now = time.monotonic()
        if now - self._last_fragment_at > common.TURN_JOIN_GAP_SECONDS:
            self._user_turn = []
        self._last_fragment_at = now
        self._user_turn.append(delta)
        self._record_fragment("caller", delta)

    def _record_fragment(self, speaker, text):
        text = str(text or "")
        if not text.strip():
            return
        now = time.monotonic()
        last = self._turns[-1] if self._turns else None
        if (last is not None and last["speaker"] == speaker
                and now - last["at"] <= common.TURN_JOIN_GAP_SECONDS):
            last["text"] = (last["text"] + text).strip()
            last["at"] = now
            self._trace_turn(last)
            return
        if speaker == "agent":
            # The exchange has moved on: what the caller said before this answer
            # is answered, and appending the next question to it would hand the
            # backend a reaction to the last one as part of the new ask.
            self._user_turn = []
        turn = {"speaker": speaker, "text": text.strip(), "at": now}
        self._turns.append(turn)
        self._trace_turn(turn)

    def _trace_turn(self, turn):
        """The call as it happens, in the log. Transcripts otherwise only land in
        the recording's metadata once the call is over, which is too late to see
        why a call went the way it did."""
        now = time.monotonic()
        if now - self._traced_at < 1.5 and turn is self._traced_turn:
            return
        self._traced_at, self._traced_turn = now, turn
        self._log(f"voice[{turn['speaker']}]: {turn['text'][-300:]}")

    # --- delegation --------------------------------------------------------

    def _on_delegation(self, event):
        """The model wants the backend. Dispatch and return at once: this runs
        on the receive loop's own stack, and blocking it stops the audio."""
        info = getattr(event, "delegation", None)
        delegation_id = getattr(info, "id", None)
        if not delegation_id:
            self._log("voice: delegation arrived with no id; nothing can answer it")
            return
        self.delegations_seen += 1
        self._delegations[delegation_id] = time.monotonic()
        self._log(f"voice: delegation {delegation_id} raised by the model")
        self._tasks.append(asyncio.create_task(self._serve(delegation_id)))

    def _ask_text(self):
        """What the caller asked for. The delegation carries no task text, so
        the open turn is the ask; where the model delegates after speaking, the
        last thing the caller said is the best there is."""
        if self._user_turn:
            # The deltas are fragments of words, carrying their own spacing;
            # joining them with a space breaks every word they split.
            return " ".join("".join(self._user_turn).split())
        for turn in reversed(self._turns):
            if turn["speaker"] == "caller" and turn["text"].strip():
                return turn["text"].strip()
        return ""

    async def _serve(self, delegation_id):
        """Answer one delegation. Every path through this ends in an append:
        unanswered work is held at session scope and would stall what follows."""
        try:
            # Nothing is assembled into an ask: the conversation is the ask, and
            # it travels with the task rather than being summarised into it.
            heard = self._ask_text()
            self._user_turn = []
            if not self._turns:
                await self._append(
                    "thinking",
                    "Nothing has been said on this call yet that could be acted on. "
                    "Ask the caller what they want done.", delegation_id)
                return
            if self._task_runner is None:
                await self._append(
                    "thinking",
                    "The backend is not available on this call. Say plainly that you "
                    "cannot have that done right now.", delegation_id)
                return

            # Keyed by the delegation, because the request text is the same
            # frame every time and would otherwise read as the same task twice.
            decision = self._task_runner.start(
                HANDED_TURN_REQUEST, signature=delegation_id)
            job_id = decision.get("job_id")
            if not decision.get("ok") or not job_id:
                # A refusal has no structured channel here, so it goes as silent
                # context and the prompt decides what the caller hears.
                # The runner's own wording is written for the tool the other
                # provider calls, and tells the model not to call it again.
                # There is no tool here, so the situation is stated plainly.
                busy = decision.get("status") in ("busy", "busy_from_earlier_call")
                if busy:
                    self._deferred.append(heard)
                    self._log(f"voice: delegation {delegation_id} deferred, "
                              f"one already running; queued: {heard[-160:]}")
                await self._append(
                    "thinking",
                    ("Two separate things are true and the caller needs both. "
                     "One: what they asked for a moment ago is still running and "
                     "has not come back. Two: this new request of theirs has NOT "
                     "been started, because only one runs at a time — it is held "
                     "and will be started by itself the moment the first finishes. "
                     "Say both plainly and do not report on the new one as though "
                     "it were under way."
                     if busy else
                     f"The backend did not take this on: {decision.get('instruction') or ''}".strip()),
                    delegation_id)
                return
            if decision.get("status") == "already_running":
                await self._append(
                    "thinking",
                    "That exact work is already running; it was not started twice.",
                    delegation_id)
                return

            self._jobs_to_delegation[job_id] = delegation_id
            self._log(f"voice: delegation {delegation_id} -> {job_id}; "
                      f"{len(self._turns)} turn(s) of call handed over; "
                      f"caller last said: {heard[-160:]}")
            await self._append(
                "thinking",
                "Background, not something to say: the work is running and has not "
                "come back yet. Do not tell the caller this unless they ask.",
                delegation_id)
            await self._await_completion(job_id, delegation_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(f"voice: delegation {delegation_id} failed — "
                      f"{type(exc).__name__}: {exc}"[:300])
            await self._append(
                "thinking",
                "The backend failed before it could answer. Say that it did not go "
                "through and ask how they want to proceed.", delegation_id)
        finally:
            self._delegations.pop(delegation_id, None)
            self.delegations_answered += 1

    async def _await_completion(self, job_id, delegation_id):
        """Wait for this job, flushing progress as it accrues. The runner's queue
        is shared, so a completion for another job is put back rather than
        eaten."""
        runner = self._task_runner
        others = []
        try:
            while True:
                await self._flush_progress(delegation_id)
                try:
                    completion = await asyncio.wait_for(
                        runner.completions.get(), PROGRESS_POLL_SECONDS)
                except asyncio.TimeoutError:
                    continue
                if completion.get("job_id") != job_id:
                    others.append(completion)
                    continue
                await self._speak_completion(completion, delegation_id)
                await self._start_deferred()
                return
        finally:
            for completion in others:
                runner.completions.put_nowait(completion)

    async def _speak_completion(self, completion, delegation_id):
        result = str(completion.get("result") or "").strip()
        if not completion.get("ok"):
            await self._append(
                "commentary",
                f"That did not go through. {result[:600]}", delegation_id)
            return
        if not result:
            await self._append(
                "thinking",
                "The backend came back with nothing to say. Tell the caller it "
                "returned nothing rather than inventing a result.", delegation_id)
            return
        for piece in chunk_for_append(result):
            await self._append("commentary", piece, delegation_id)

    async def _start_deferred(self):
        """Run what was held back, now that the one worker is free.

        Not a queue in any interesting sense: it holds what the caller asked for
        while the slot was taken, so a second question is answered rather than
        forgotten. The worker is still one at a time, which is the whole of what
        the limit was ever protecting."""
        if not self._deferred or self._task_runner is None:
            return
        held = self._deferred.pop(0)
        request = (
            "Earlier in this call the caller asked for something while you were "
            "already busy, so it was never started. They asked, in their words: "
            f"\"{held}\"\n\n"
            + HANDED_TURN_REQUEST)
        decision = self._task_runner.start(request, signature=f"deferred::{held[:200]}")
        job_id = decision.get("job_id")
        if not decision.get("ok") or not job_id:
            self._deferred.insert(0, held)
            return
        self._log(f"voice: deferred request started as {job_id}: {held[-160:]}")
        # Unattached: the delegation that asked for this is long closed, and an
        # id the service no longer knows is not a safe thing to quote back.
        await self._append(
            "thinking",
            "The request they made while you were busy has now been started. "
            "Tell them you are on it, then say nothing further about it until "
            "the answer arrives.", None)
        await self._await_completion(job_id, None)

    # --- progress ----------------------------------------------------------

    def note_progress(self, note, source="stream"):
        """A line from the worker's own event stream. Kept, not sent: appends are
        permanent context, so they are coalesced and flushed on a beat."""
        text = " ".join(str(note or "").split())
        if text:
            # `(kind, text)`, the shape the shared digest folds: a worker's own
            # words outrank anything derived from its command stream.
            self._progress_window.append((source, text))
            del self._progress_window[:-40]

    async def _flush_progress(self, delegation_id):
        if not self._progress_window:
            return
        now = time.monotonic()
        if now - self._progress_flushed_at < self._progress_interval:
            return
        window, self._progress_window = self._progress_window, []
        self._progress_flushed_at = now
        digest = (common.summarize_progress(window)
                  or " · ".join(text for _, text in window[-4:]))
        await self._append(
            "thinking",
            ("Background, not something to say. Where the work stands: "
             f"{digest} — answer from this only if the caller asks how it is "
             "going.")[:APPEND_CHAR_BUDGET],
            delegation_id)

    # --- teardown and reporting -------------------------------------------

    async def stop(self):
        """Close the call down and answer with what it was.

        The daemon takes the summary from here and the tracks from the property
        beside it, so both have to survive the teardown they describe."""
        self._closing = True
        conn = self._conn
        if conn is not None:
            # `session.closed` carries the final billable usage and the service
            # drains before sending it, so it is worth the short wait.
            with contextlib.suppress(Exception):
                await conn.session.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._drain_close(), CLOSE_DRAIN_TIMEOUT)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        # Work the caller started outlives the call: nothing can be spoken any
        # more, so the results go to the chat rather than being dropped. The
        # prompt promises the caller exactly this.
        if self._task_runner is not None:
            await self._task_runner.detach()
        # The window closes once nothing can write any more, so both tracks are
        # sealed to the call's own length rather than to whenever teardown ended.
        self.window_seconds = time.monotonic() - self.origin
        for writer in (self._caller_writer, self._agent_writer):
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.seal(self.window_seconds)
        self._conn = None
        return self.summary()

    async def _drain_close(self):
        """`session.closed` carries the final billable usage; the service drains
        before sending it."""
        while self._close_reason is None and self._conn is not None:
            await asyncio.sleep(0.05)

    def conversation_tail(self, limit=60):
        """The call so far, shaped like the message tail a worker prompt renders.

        The worker is otherwise handed one sentence and nothing about the
        exchange it came out of, so a question that refers to the answer before
        it cannot be resolved at all. A call is minutes long, so the whole of it
        costs little and there is no telling in advance which part mattered."""
        rows = []
        for index, turn in enumerate(self._turns[-limit:], start=1):
            text = turn["text"].strip()
            if not text:
                continue
            rows.append({
                "id": index,
                "sender": (self._assistant_name if turn["speaker"] == "agent"
                           else self._caller_name),
                "is_assistant": turn["speaker"] == "agent",
                "text": text,
            })
        return rows

    def transcript(self):
        return [{"speaker": t["speaker"], "text": t["text"]} for t in self._turns
                if t["text"].strip()]

    def summary(self):
        """What the call was, in the shape the daemon's own record expects.

        The first block is that record's contract — the metadata written beside
        every recording reads these by name, and a provider that renames one
        breaks the record rather than its own reporting. The rest is this
        stack's, added beside it."""
        return {
            "model": self._model,
            "voice": self._voice,
            "caller_seconds": round(self.caller_bytes / (CALLER_RATE * 2), 3),
            "agent_seconds": round(self.agent_frames * FRAME_SECONDS, 3),
            "agent_voiced_seconds": round(self.agent_voiced_frames * FRAME_SECONDS, 3),
            "interruptions": self.interruptions,
            "dropped_input_chunks": self.dropped_input_chunks,
            "pump_error": self._pump_error,
            "messages_sent": self.messages_sent,
            "tasks": (self._task_runner.history
                      if self._task_runner is not None else []),
            "transcript": self.transcript(),
            # This stack's own account of the call.
            "provider": "gptlive",
            "session_id": self._session_id,
            "delegations_seen": self.delegations_seen,
            "delegations_answered": self.delegations_answered,
            "delegations_deferred": len(self._deferred),
            "append_chars": dict(sorted(self._append_chars.items())),
            "usage_seconds": round(self._usage_seconds, 1),
            "context_usage_ratio": self._context_ratio,
            "close_reason": self._close_reason,
        }

    @property
    def tracks(self):
        rows = []
        for kind, channel, writer in (
            ("caller", "left", self._caller_writer),
            ("agent", "right", self._agent_writer),
        ):
            if writer is None:
                continue
            rows.append({
                "kind": kind,
                "channel": channel,
                "sample_rate": writer.rate,
                "path": str(writer.path),
                "bytes": writer.bytes,
                "duration_seconds": round(writer.duration_seconds, 3),
                "lead_seconds": round(writer.lead_seconds, 3),
            })
        return rows
