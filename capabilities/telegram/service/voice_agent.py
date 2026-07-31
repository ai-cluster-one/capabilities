"""Gemini Live voice agent for direct Telegram calls.

Answering a direct call by talking runs on the daemon's own Telethon client and
PyTgCalls instance. The two media slots of one p2p call are independent and
coexist: `record()` owns the PLAYBACK slot (caller audio in), `play()` owns the
CAPTURE slot (agent audio out). Both honour the requested `AudioParameters`
exactly, so 16 kHz mono in and 24 kHz mono out match the Live API's own formats
and no resampling happens anywhere.

Recording a voice-agent call therefore writes two separate PCM tracks on one
shared time origin — caller left, agent right — which ffmpeg joins into a single
stereo Opus file with real speaker separation.

A caller can also have work done while they stay on the line: `agent_task` hands
one task to the project's worker and returns at once, so speaking is never
blocked on it. The result is spoken when it lands, or delivered to the caller's
chat if the call ended first. While it runs, a digest of the worker's own event
stream is offered to the conversation, so the line is not silent. `send_to_chat`
covers what speech carries badly — a link, a spelling, a number.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pytgcalls.exceptions import NotInCallError
from pytgcalls.types import Device, Frame

from call_recording_helpers import iso_utc, probe_audio_duration

DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Aoede"
DEFAULT_HISTORY_MESSAGES = 30
# One task per call, always. Several workers on one call race each other's
# progress and their answers into the conversation, and the caller cannot tell
# which reply belongs to which question. Not a setting: a project raising it
# would only be re-creating that defect.
TASKS_PER_CALL = 1
# How long a finished task waits for a turn boundary before it is spoken anyway.
ANNOUNCE_IDLE_TIMEOUT = 20.0
# A pause longer than this starts a new transcript turn, even for one speaker.
TURN_JOIN_GAP_SECONDS = 4.0
# How often a long-running task may report in. Nothing about it reaches the
# caller more often than this, however talkative the worker is, and never while
# anyone is mid-sentence. A project may set its own pace.
DEFAULT_PROGRESS_INTERVAL = 10.0
PROGRESS_CALLER_QUIET = 3.0

CALLER_RATE = 16000
AGENT_RATE = 24000
FRAME_SECONDS = 0.01
CALLER_FRAME_BYTES = int(CALLER_RATE * 2 * FRAME_SECONDS)          # 320
AGENT_FRAME_BYTES = int(AGENT_RATE * 2 * FRAME_SECONDS)            # 480
AGENT_SILENCE = b"\x00" * AGENT_FRAME_BYTES
GEMINI_INPUT_CHUNK_BYTES = CALLER_FRAME_BYTES * 10                 # ~100 ms
INPUT_QUEUE_FRAMES = 50                                            # ~5 s of backlog


AGENT_TASK_TOOL = {
    "name": "agent_task",
    "description": (
        "LAST RESORT, not first. Hand one task to the project's worker, which "
        "can reach files, other systems and tools you cannot. Returns "
        "immediately; the result is announced in the conversation when it lands."
        "\n\n"
        "Before calling this, check whether you can already answer. The recent "
        "chat messages in your instructions are YOURS — reading, summarising, "
        "quoting or drawing conclusions from them needs no tool at all, and "
        "delegating that will simply fail, because the worker cannot see this "
        "conversation."
        "\n\n"
        "Call it only when the answer genuinely is not in front of you: current "
        "state of a system, a file's contents, a check against something "
        "external, or an action with an effect such as writing something down "
        "or filing something. If you are unsure, answer from what you have and "
        "say what you could not check."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": (
                    "The task in a sentence or two, self-contained. The worker "
                    "sees only this text — not the call, not the chat history "
                    "in your instructions — so anything it needs must be "
                    "written out here in full."
                ),
            },
        },
        "required": ["task"],
    },
}


SEND_TO_CHAT_TOOL = {
    "name": "send_to_chat",
    "description": (
        "Write a message into this caller's Telegram chat, where they can read "
        "it during or after the call. Use it for anything speech carries badly "
        "— links, addresses, exact names and spellings, numbers, code, or a "
        "list they will want to keep. Say aloud that you have sent it; do not "
        "read the contents out."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "text": {
                "type": "STRING",
                "description": "The message exactly as it should appear in the chat.",
            },
        },
        "required": ["text"],
    },
}


class VoiceAgentError(RuntimeError):
    pass


def summarize_progress(window):
    """Turn a window of worker events into a few plain facts.

    Deliberately not "the last thing that happened": a worker's last step is
    often something that did not work, which it then routes around. Reporting
    that would tell the caller a failure that is not one. So the whole window is
    folded into counts plus where the work stands now, and nothing is inferred.
    """
    narrated = [text for kind, text in window if kind == "worker"]
    if narrated:
        # The worker's own words beat anything derived from its command stream.
        return narrated[-1]
    stages = [text for kind, text in window if kind == "stream"]
    if not stages:
        return None
    counts = {}
    for stage in stages:
        counts[stage] = counts.get(stage, 0) + 1
    current = stages[-1]
    earlier = [f"{stage} ({count}x)" if count > 1 else stage
               for stage, count in counts.items() if stage != current]
    if not earlier:
        return current
    return f"{'; '.join(earlier[:3])}; now {current}"


def progress_prompt(note):
    return (
        "Internal status note about the task you are running. The caller has "
        "not said this and it is not a request.\n\n"
        "It is a rough digest of the last few seconds of work, not a result and "
        "not a verdict. Do not read it out, do not quote it, and do not treat "
        "anything in it as finished or failed — the work is still going. Turn it "
        "into at most one short clause in the language of the conversation, of "
        "the kind a person drops while they are looking something up.\n\n"
        "If the caller is in the middle of something else, or you have nothing "
        "new to add, say nothing at all — silence is the right answer more often "
        "than not.\n\n" + str(note)[:600]
    )


def completion_prompt(completion):
    return (
        "Internal background event: a task you started has completed. This is "
        "not a new user request. Briefly tell the caller the result in one or "
        "two short spoken sentences, in the language of the conversation. Do "
        "not start the same task again.\n"
        + json.dumps(completion, ensure_ascii=False)[:8000]
    )


class VoiceTaskRunner:
    """Work started from a call, bounded and deduplicated.

    Starting a task never blocks the conversation: `start` returns a decision the
    model can speak straight away, and the task runs on its own. Where the result
    goes is decided when it lands, not when it was asked for — spoken while the
    call is up, delivered to the caller's chat once it is not, so an answer the
    caller asked for is never dropped because they hung up first.
    """

    def __init__(self, run_task, deliver, *, log=print):
        self._run_task = run_task
        self._deliver = deliver
        self.limit = TASKS_PER_CALL
        self.completions = asyncio.Queue()
        self._jobs = {}
        self._signatures = {}
        self._sequence = 0
        self._live = True
        self._log = log
        # One record per task, so a call's own metadata says what was asked for
        # and what came back — not only the log.
        self.history = []

    @property
    def running(self):
        return len(self._jobs)

    def start(self, task):
        text = " ".join(str(task or "").split())
        if not text:
            return {"ok": False, "status": "empty_task",
                    "instruction": "Ask the caller what they want done, then call again."}
        signature = text.lower()
        running = self._signatures.get(signature)
        if running is not None:
            return {"ok": True, "status": "already_running", "job_id": running,
                    "running": self.running, "limit": self.limit,
                    "instruction": "This exact task is already running; do not start it twice."}
        if self.running >= self.limit:
            return {"ok": False, "status": "busy",
                    "running": self.running, "limit": self.limit,
                    "instruction": "A task is already running and only one runs at a "
                                   "time. Do NOT start this one. Tell the caller you "
                                   "will do it once the current one is done, and wait "
                                   "for that result before calling this tool again."}
        self._sequence += 1
        job_id = f"task-{self._sequence}"
        self._signatures[signature] = job_id
        self.history.append({"job_id": job_id, "task": text, "at": iso_utc(),
                             "status": "running", "seconds": None, "result": None})
        self._jobs[job_id] = asyncio.create_task(self._run(job_id, signature, text))
        return {"ok": True, "status": "started", "job_id": job_id,
                "running": self.running, "limit": self.limit,
                "instruction": "Say in one short sentence that you are on it, then carry on "
                               "talking. The result arrives by itself; do not wait for it."}

    async def _run(self, job_id, signature, text):
        completion = {"job_id": job_id, "task": text}
        started = time.monotonic()
        try:
            completion.update({"ok": True, "result": await self._run_task(text)})
            reply = str(completion.get("result") or "")
            self._log(f"voice: task {job_id} finished ok in "
                      f"{time.monotonic() - started:.1f}s, {len(reply)} chars")
        except asyncio.CancelledError:
            self._log(f"voice: task {job_id} cancelled after "
                      f"{time.monotonic() - started:.1f}s")
            raise
        except Exception as exc:
            completion.update({"ok": False,
                               "result": f"{type(exc).__name__}: {exc}"[:800]})
            self._log(f"voice: task {job_id} failed after "
                      f"{time.monotonic() - started:.1f}s — {completion['result']}")
        self._jobs.pop(job_id, None)
        self._signatures.pop(signature, None)
        self._close_record(job_id, completion, round(time.monotonic() - started, 1))
        # Decided without an await in between, so a call ending mid-decision
        # cannot route a result to a conversation that is already gone.
        if self._live:
            self.completions.put_nowait(completion)
            self._log(f"voice: task {job_id} queued to be spoken "
                      f"(queue depth {self.completions.qsize()})")
        else:
            self._log(f"voice: task {job_id} finished after the call — "
                      "delivering to the chat")
            await self.deliver(completion)

    def _close_record(self, job_id, completion, seconds):
        for row in self.history:
            if row["job_id"] == job_id:
                row.update({
                    "status": "ok" if completion.get("ok") else "failed",
                    "seconds": seconds,
                    "result": str(completion.get("result") or "")[:2000],
                })
                return

    async def deliver(self, completion):
        job_id = completion.get("job_id")
        try:
            await self._deliver(completion)
            self._log(f"voice: task {job_id} result delivered to the chat")
        except Exception as exc:
            self._log("voice: cannot deliver task result to the chat — "
                      f"{type(exc).__name__}: {exc}")

    async def detach(self):
        """The call is over: what is already finished goes to the chat, and work
        still running follows it there when it lands."""
        self._live = False
        self._log(f"voice: runner detached; {self.running} task(s) still running, "
                  f"{self.completions.qsize()} finished result(s) to hand to the chat")
        while True:
            try:
                completion = self.completions.get_nowait()
            except asyncio.QueueEmpty:
                return
            await self.deliver(completion)


def build_system_prompt(voice_context, history, now_line=None):
    """The project's own voice prompt, when the call is happening, then the
    recent direct-chat tail. The prompt text belongs to the project; nothing is
    added to it here beyond those two runtime facts."""
    blocks = [(voice_context or "").strip()]
    if now_line:
        blocks.append("--- Right now ---\n\n" + now_line.strip())
    tail = (history or "").strip()
    if tail:
        blocks.append(
            "--- Recent messages in this direct chat ---\n\n"
            "Older first. Each line is timestamped in the same zone as the "
            "current time above.\n\n" + tail)
    return "\n\n".join(block for block in blocks if block)


def resolve_timezone(name):
    """The zone a call states its times in, as (tzinfo, label).

    An IANA name; UTC when unset or unknown, never the host's own zone — a
    daemon moves between hosts and the caller's clock does not.
    """
    label = str(name or "").strip() or "UTC"
    if label.upper() == "UTC":
        return timezone.utc, "UTC"
    try:
        return ZoneInfo(label), label
    except Exception:
        return timezone.utc, "UTC"


def current_time_line(zone, label):
    """What the model is told about the present moment. Without it a call has no
    'now' at all, so anything the caller says about time is unanchored."""
    stamp = datetime.now(zone)
    return (f"The call is happening now: {stamp:%Y-%m-%d %H:%M} "
            f"({stamp:%A}), timezone {label}.")


def greeting_prompt(caller_name):
    return (
        f"{caller_name} has just picked up and the call is connected. "
        "Greet them briefly in one short spoken sentence and let them speak."
    )


def live_config(system_instruction, voice, tools=None):
    config = {
        "response_modalities": ["AUDIO"],
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "speech_config": {
            "voice_config": {"prebuilt_voice_config": {"voice_name": voice}}
        },
        "input_audio_transcription": {},
        "output_audio_transcription": {},
    }
    if tools:
        config["tools"] = [{"function_declarations": list(tools)}]
    return config


def _sample_bytes(seconds, rate):
    """Byte offset of a moment in a mono s16 track, on a sample boundary."""
    return max(0, int(max(0.0, seconds) * rate)) * 2


def _write_silence(handle, size):
    while size > 0:
        block = min(size, 1 << 20)
        handle.write(b"\x00" * block)
        size -= block


class _TrackWriter:
    """One speaker's PCM track, laid on the session's shared time origin.

    Leading silence is resolved when the track is sealed, never when its first
    payload lands. A live inbound stream can hand over audio that already covers
    part of the interval since the origin — PyTgCalls dispatches no incoming
    frame until the outbound slot is up, then flushes everything it buffered
    meanwhile — so padding by the delay measured at the first write counts that
    interval twice and drags the speaker later than they spoke. The audio's own
    length places it instead: a track can have begun no later than its first
    write, and no later than its last write minus everything it carries.
    """

    def __init__(self, path, rate, origin, clock=time.monotonic):
        self.path = path
        self.rate = rate
        self.origin = origin
        self.bytes = 0
        self.payload_bytes = 0
        self.lead_bytes = 0
        self._clock = clock
        self._first_write = None
        self._last_write = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("wb")

    def write(self, payload):
        if self._handle is None:
            return
        self._last_write = self._clock() - self.origin
        if self._first_write is None:
            self._first_write = self._last_write
        self._handle.write(payload)
        self.payload_bytes += len(payload)

    def close(self):
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def seal(self, window_seconds):
        """Close the track and place it in the call's window: leading silence up
        to its first real sample, trailing silence to the end of the call. Both
        tracks of a call seal to the same window, so joining them at t=0 aligns
        the two speakers instead of shifting one against the other. A track that
        never received audio stays empty, so the empty-track guard still sees it.
        """
        self.close()
        if not self.payload_bytes:
            self.bytes = 0
            return
        window = _sample_bytes(window_seconds, self.rate)
        payload = min(self.payload_bytes, window)
        lead = min(_sample_bytes(self._first_write, self.rate),
                   _sample_bytes(self._last_write, self.rate) - payload)
        lead = max(0, min(lead, window - payload))
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with self.path.open("rb") as source, temporary.open("wb") as target:
            _write_silence(target, lead)
            # Audio beyond the window can only be over-delivery at the head; the
            # stream's last sample is current, so the newest audio is the kept one.
            source.seek(self.payload_bytes - payload)
            remaining = payload
            while remaining > 0:
                chunk = source.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                target.write(chunk)
                remaining -= len(chunk)
            _write_silence(target, window - lead - payload)
        os.replace(temporary, self.path)
        self.lead_bytes = lead
        self.bytes = self.path.stat().st_size

    @property
    def duration_seconds(self):
        return self.bytes / (self.rate * 2)

    @property
    def lead_seconds(self):
        return self.lead_bytes / (self.rate * 2)


class VoiceCallSession:
    """One answered call bridged to one Gemini Live session."""

    def __init__(self, calls, chat_id, *, api_key, model, voice,
                 system_instruction, caller_name, caller_track=None,
                 agent_track=None, task_runner=None, send_to_chat=None,
                 progress_interval=DEFAULT_PROGRESS_INTERVAL, log=print):
        self._calls = calls
        self._chat_id = chat_id
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._voice = voice or DEFAULT_VOICE
        self._system_instruction = system_instruction
        self._caller_name = caller_name
        self._task_runner = task_runner
        self._send_to_chat = send_to_chat
        self._log = log

        # A completion is injected only between turns: cutting into speech the
        # caller is listening to would be heard as the agent talking over itself.
        self._model_idle = asyncio.Event()
        self._model_idle.set()
        self._live = None
        self._stack = None
        self._tasks = []
        self._pending_input = bytearray()
        self._input_queue = asyncio.Queue(maxsize=INPUT_QUEUE_FRAMES)
        self._outbound = bytearray()
        self._turns = []
        self._pump_error = None

        # One time origin for both tracks, fixed before the call is answered, so
        # caller audio arriving the instant record() lands is already on it.
        self.origin = time.monotonic()
        self.window_seconds = None
        self._caller_writer = (
            _TrackWriter(caller_track, CALLER_RATE, self.origin)
            if caller_track is not None else None)
        self._agent_writer = (
            _TrackWriter(agent_track, AGENT_RATE, self.origin)
            if agent_track is not None else None)
        self.caller_bytes = 0
        self.agent_frames = 0
        self.agent_voiced_frames = 0
        self.interruptions = 0
        self.dropped_input_chunks = 0
        self.messages_sent = 0
        # A finished result taken off the queue but not yet handed to the model.
        self._announcing = None
        self._last_fragment_at = 0.0
        self._progress_window = []
        self._progress_offered_at = 0.0
        self._progress_last_offered = None
        self._progress_interval = max(1.0, float(
            progress_interval or DEFAULT_PROGRESS_INTERVAL))
        self._caller_spoke_at = 0.0

    # --- media -------------------------------------------------------------
    def start_pump(self):
        """Start the paced capture pump, before the Live session is up so the
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
        while len(self._pending_input) >= GEMINI_INPUT_CHUNK_BYTES:
            chunk = bytes(self._pending_input[:GEMINI_INPUT_CHUNK_BYTES])
            del self._pending_input[:GEMINI_INPUT_CHUNK_BYTES]
            if self._input_queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._input_queue.get_nowait()
                self.dropped_input_chunks += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._input_queue.put_nowait(chunk)

    async def _outbound_pump(self):
        next_tick = time.monotonic()
        while True:
            next_tick += FRAME_SECONDS
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -1.0:
                next_tick = time.monotonic()
            if len(self._outbound) >= AGENT_FRAME_BYTES:
                payload = bytes(self._outbound[:AGENT_FRAME_BYTES])
                del self._outbound[:AGENT_FRAME_BYTES]
                self.agent_voiced_frames += 1
            else:
                payload = AGENT_SILENCE
            self.agent_frames += 1
            if self._agent_writer is not None:
                self._agent_writer.write(payload)
            try:
                await self._calls.send_frame(
                    self._chat_id,
                    Device.MICROPHONE,
                    payload,
                    Frame.Info(capture_time=int(time.time() * 1000)),
                )
            except NotInCallError:
                return
            except Exception as exc:
                self._pump_error = f"{type(exc).__name__}: {exc}"[:300]
                self._log(f"voice: outbound pump stopped — {self._pump_error}")
                return

    # --- Gemini ------------------------------------------------------------
    def set_system_instruction(self, system_instruction):
        """Set after the call is answered, not before.

        Answering has a ring window: anything slow done before `record()` — and
        reading a long chat tail is slow — lets the call expire, after which
        pytgcalls tries to place a new call instead of accepting the offered one.
        """
        self._system_instruction = system_instruction

    def _declared_tools(self):
        """Only what this call can actually do is declared, so the model is never
        holding a tool that would fail if it reached for it."""
        tools = []
        if self._task_runner is not None:
            tools.append(AGENT_TASK_TOOL)
        if self._send_to_chat is not None:
            tools.append(SEND_TO_CHAT_TOOL)
        return tools or None

    async def start_agent(self):
        from google import genai

        self._stack = contextlib.AsyncExitStack()
        try:
            client = genai.Client(api_key=self._api_key)
            self._live = await self._stack.enter_async_context(
                client.aio.live.connect(
                    model=self._model,
                    config=live_config(
                        self._system_instruction, self._voice,
                        tools=self._declared_tools()),
                )
            )
        except Exception as exc:
            await self._stack.aclose()
            self._stack = None
            raise VoiceAgentError(f"{type(exc).__name__}: {exc}") from exc
        self._tasks.append(asyncio.create_task(self._gemini_sender()))
        self._tasks.append(asyncio.create_task(self._gemini_receiver()))
        if self._task_runner is not None:
            self._tasks.append(asyncio.create_task(self._announce_completions()))
            self._tasks.append(asyncio.create_task(self._announce_progress()))
        await self._live.send_realtime_input(text=greeting_prompt(self._caller_name))

    async def _gemini_sender(self):
        from google.genai import types

        while True:
            chunk = await self._input_queue.get()
            try:
                await self._live.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type=f"audio/pcm;rate={CALLER_RATE}",
                    )
                )
            except Exception as exc:
                self._log(f"voice: Gemini input stopped — {type(exc).__name__}: {exc}")
                return

    async def _gemini_receiver(self):
        while True:
            received = False
            try:
                async for response in self._live.receive():
                    received = True
                    self._consume(response)
                    await self._handle_tool_call(response)
            except Exception as exc:
                self._log(f"voice: Gemini stream ended — {type(exc).__name__}: {exc}")
                return
            if not received:
                return

    def _consume(self, response):
        content = getattr(response, "server_content", None)
        if content is None:
            return
        if getattr(content, "interrupted", False):
            self.interruptions += 1
            self._outbound.clear()
            self._model_idle.set()
        if getattr(content, "turn_complete", False) or getattr(
                content, "generation_complete", False):
            self._model_idle.set()
        elif getattr(content, "model_turn", None) is not None:
            self._model_idle.clear()
        transcription = getattr(content, "input_transcription", None)
        if transcription is not None:
            if getattr(transcription, "text", None):
                self._caller_spoke_at = time.monotonic()
            self._record_fragment("caller", getattr(transcription, "text", None))
        transcription = getattr(content, "output_transcription", None)
        if transcription is not None:
            self._record_fragment("agent", getattr(transcription, "text", None))
        turn = getattr(content, "model_turn", None)
        for part in (getattr(turn, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if data:
                self._outbound.extend(data)

    async def _handle_tool_call(self, response):
        """Answer the tool call in the same turn it arrived in: the runner's
        decision is what the model speaks, and it is already made."""
        tool_call = getattr(response, "tool_call", None)
        calls = getattr(tool_call, "function_calls", None) if tool_call else None
        if not calls:
            return
        from google.genai import types

        answers = []
        for call in calls:
            name = getattr(call, "name", None)
            args = getattr(call, "args", None) or {}
            if name == AGENT_TASK_TOOL["name"] and self._task_runner is not None:
                result = self._task_runner.start(args.get("task"))
                self._log(f"voice: agent_task {result.get('status')} "
                          f"({result.get('job_id') or '-'})")
            elif name == SEND_TO_CHAT_TOOL["name"] and self._send_to_chat is not None:
                result = await self._write_to_chat(args.get("text"))
            else:
                result = {"ok": False, "status": "unknown_tool",
                          "instruction": f"There is no tool named {name}."}
            answers.append(types.FunctionResponse(
                name=name, id=getattr(call, "id", None), response={"result": result}))
        await self._live.send_tool_response(function_responses=answers)

    async def _write_to_chat(self, text):
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "status": "empty_message",
                    "instruction": "Nothing to send; write the message text and call again."}
        try:
            await self._send_to_chat(body)
        except Exception as exc:
            self._log(f"voice: cannot write to the chat — {type(exc).__name__}: {exc}")
            return {"ok": False, "status": "send_failed",
                    "instruction": "The message did not go through. Tell the caller "
                                   "you will send it after the call."}
        self.messages_sent += 1
        self._log(f"voice: wrote {len(body)} chars to the chat")
        return {"ok": True, "status": "sent",
                "instruction": "Say in one short sentence that you have sent it. "
                               "Do not read the contents out."}

    def note_progress(self, note, source="stream"):
        """Collect one event. What the caller eventually hears is a digest of the
        whole window, decided at tick time — never a single raw line."""
        text = str(note or "").strip()
        if not text:
            return
        self._progress_window.append((source, text))
        del self._progress_window[:-40]

    async def _announce_progress(self):
        """Offer the latest progress at most once per interval, and only into a
        genuine lull: the model silent, the caller silent, something new to say."""
        while True:
            await asyncio.sleep(1.0)
            if not self._task_runner.running:
                # Nothing is running, so nothing in the window is still current.
                self._progress_window.clear()
                continue
            note = self._due_progress()
            if note is None:
                continue
            try:
                await self._live.send_realtime_input(text=progress_prompt(note))
                self._log(f"voice: progress offered to the model — {note[:80]!r}")
            except Exception as exc:
                self._log("voice: cannot offer progress — "
                          f"{type(exc).__name__}: {exc}")
                return

    def _due_progress(self):
        """The digest to offer now, or None while any of the conditions that keep
        progress off the line still holds."""
        if not self._progress_window:
            return None
        now = time.monotonic()
        if now - self._progress_offered_at < self._progress_interval:
            return None
        if not self._model_idle.is_set():
            return None
        if now - self._caller_spoke_at < PROGRESS_CALLER_QUIET:
            return None
        note = summarize_progress(self._progress_window)
        self._progress_window.clear()
        if note is None or note == self._progress_last_offered:
            # Nothing has changed since the last time; saying it again is noise.
            return None
        self._progress_last_offered = note
        self._progress_offered_at = now
        return note

    async def _announce_completions(self):
        """Speak finished tasks between turns.

        A result taken off the queue is held in `_announcing` until it has
        actually been handed to the model, so that a call ending while we wait
        for a pause cannot swallow it: `stop()` puts anything still in hand back
        on the queue, and it reaches the caller's chat instead.
        """
        while True:
            completion = await self._task_runner.completions.get()
            self._announcing = completion
            job_id = completion.get("job_id")
            waited = time.monotonic()
            if not self._model_idle.is_set():
                self._log(f"voice: task {job_id} ready, waiting for the model "
                          "to stop speaking")
                try:
                    await asyncio.wait_for(self._model_idle.wait(),
                                           timeout=ANNOUNCE_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    # Never sit on a finished result because a turn boundary
                    # failed to arrive; interrupting is better than losing it.
                    self._log(f"voice: task {job_id} still waiting after "
                              f"{ANNOUNCE_IDLE_TIMEOUT}s — speaking it anyway")
            try:
                await self._live.send_realtime_input(
                    text=completion_prompt(completion))
                self._announcing = None
                self._log(f"voice: task {job_id} handed to the model to speak "
                          f"(waited {time.monotonic() - waited:.1f}s)")
            except Exception as exc:
                self._announcing = None
                self._log("voice: cannot announce a finished task — "
                          f"{type(exc).__name__}: {exc}")
                await self._task_runner.deliver(completion)
                return

    def _record_fragment(self, speaker, text):
        """Transcriptions arrive as one-to-three-word fragments; join
        consecutive fragments from the same speaker into a turn."""
        if not text:
            return
        now = time.monotonic()
        # Same speaker is not enough: a task answered fifteen seconds later is a
        # new turn, not a continuation, and joining them makes the timestamp lie.
        if (self._turns and self._turns[-1]["speaker"] == speaker
                and now - self._last_fragment_at <= TURN_JOIN_GAP_SECONDS):
            self._turns[-1]["text"] += text
            self._last_fragment_at = now
            return
        self._turns.append({"speaker": speaker, "at": iso_utc(), "text": text})
        self._last_fragment_at = now

    # --- teardown ----------------------------------------------------------
    async def stop(self):
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        # Tasks the caller started outlive the call; nothing can be spoken any
        # more, so their results are handed to the chat instead of dropped.
        if self._task_runner is not None:
            if self._announcing is not None:
                # Cancelled mid-announcement: this result was already off the
                # queue, so put it back before draining or it is lost silently.
                self._log(f"voice: task {self._announcing.get('job_id')} was "
                          "waiting to be spoken when the call ended")
                self._task_runner.completions.put_nowait(self._announcing)
                self._announcing = None
            await self._task_runner.detach()
        # The window closes once nothing can write any more, before the slower
        # Gemini teardown, so both tracks are sealed to the call's own length.
        self.window_seconds = time.monotonic() - self.origin
        for writer in (self._caller_writer, self._agent_writer):
            if writer is not None:
                writer.seal(self.window_seconds)
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            self._stack = None
        self._live = None
        return self.summary()

    def transcript(self):
        return [
            {"speaker": turn["speaker"], "at": turn["at"], "text": turn["text"].strip()}
            for turn in self._turns
            if turn["text"].strip()
        ]

    def summary(self):
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


def transcript_text(turns, assistant_name, caller_name):
    labels = {"caller": caller_name, "agent": assistant_name}
    return "\n".join(
        f"{labels.get(turn['speaker'], turn['speaker'])}: {turn['text']}"
        for turn in turns
    )


async def join_tracks_to_stereo(caller_pcm, agent_pcm, output):
    """Join the two raw tracks into one stereo Opus file — caller left, agent
    right — so the delivered recording carries real speaker separation."""
    result = {
        "status": "failed",
        "error": None,
        "output_bytes": 0,
        "duration_seconds": None,
        "sources_retained": True,
    }
    sizes = {path: (path.stat().st_size if path.exists() else 0)
             for path in (caller_pcm, agent_pcm)}
    if not all(sizes.values()):
        result["error"] = "voice_track_is_empty"
        return result

    temporary = output.with_name(f".{output.stem}.{os.getpid()}.tmp.ogg")
    with contextlib.suppress(OSError):
        temporary.unlink()
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-f", "s16le", "-ar", str(CALLER_RATE), "-ac", "1", "-i", str(caller_pcm),
            "-f", "s16le", "-ar", str(AGENT_RATE), "-ac", "1", "-i", str(agent_pcm),
            "-filter_complex",
            "[0:a]aresample=48000[l];[1:a]aresample=48000[r];"
            "[l][r]join=inputs=2:channel_layout=stereo[a]",
            "-map", "[a]",
            "-c:a", "libopus",
            "-b:a", "64k",
            "-f", "ogg",
            str(temporary),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except OSError as exc:
        result["error"] = f"ffmpeg_unavailable: {exc}"[:500]
        return result

    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        result["error"] = f"ffmpeg_exit_{process.returncode}: {detail}"[:500]
        with contextlib.suppress(OSError):
            temporary.unlink()
        return result
    if not temporary.exists() or temporary.stat().st_size == 0:
        result["error"] = "ogg_output_is_empty"
        with contextlib.suppress(OSError):
            temporary.unlink()
        return result

    os.replace(temporary, output)
    result.update({
        "status": "complete",
        "output_bytes": output.stat().st_size,
        "duration_seconds": await probe_audio_duration(output),
    })
    for path in (caller_pcm, agent_pcm):
        with contextlib.suppress(OSError):
            path.unlink()
    result["sources_retained"] = any(
        path.exists() for path in (caller_pcm, agent_pcm))
    return result
