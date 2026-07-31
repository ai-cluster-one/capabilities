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
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

from pytgcalls.exceptions import NotInCallError
from pytgcalls.types import Device, Frame

from call_recording_helpers import iso_utc, probe_audio_duration

DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Aoede"
DEFAULT_HISTORY_MESSAGES = 30

CALLER_RATE = 16000
AGENT_RATE = 24000
FRAME_SECONDS = 0.01
CALLER_FRAME_BYTES = int(CALLER_RATE * 2 * FRAME_SECONDS)          # 320
AGENT_FRAME_BYTES = int(AGENT_RATE * 2 * FRAME_SECONDS)            # 480
AGENT_SILENCE = b"\x00" * AGENT_FRAME_BYTES
GEMINI_INPUT_CHUNK_BYTES = CALLER_FRAME_BYTES * 10                 # ~100 ms
INPUT_QUEUE_FRAMES = 50                                            # ~5 s of backlog


class VoiceAgentError(RuntimeError):
    pass


def build_system_prompt(voice_context, history):
    """The project's own voice prompt, then the recent direct-chat tail. The
    prompt text belongs to the project; nothing is added to it here."""
    blocks = [(voice_context or "").strip()]
    tail = (history or "").strip()
    if tail:
        blocks.append("--- Recent messages in this direct chat ---\n\n" + tail)
    return "\n\n".join(block for block in blocks if block)


def greeting_prompt(caller_name):
    return (
        f"{caller_name} has just picked up and the call is connected. "
        "Greet them briefly in one short spoken sentence and let them speak."
    )


def live_config(system_instruction, voice):
    return {
        "response_modalities": ["AUDIO"],
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "speech_config": {
            "voice_config": {"prebuilt_voice_config": {"voice_name": voice}}
        },
        "input_audio_transcription": {},
        "output_audio_transcription": {},
    }


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
                 agent_track=None, log=print):
        self._calls = calls
        self._chat_id = chat_id
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._voice = voice or DEFAULT_VOICE
        self._system_instruction = system_instruction
        self._caller_name = caller_name
        self._log = log

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
    async def start_agent(self):
        from google import genai

        self._stack = contextlib.AsyncExitStack()
        try:
            client = genai.Client(api_key=self._api_key)
            self._live = await self._stack.enter_async_context(
                client.aio.live.connect(
                    model=self._model,
                    config=live_config(self._system_instruction, self._voice),
                )
            )
        except Exception as exc:
            await self._stack.aclose()
            self._stack = None
            raise VoiceAgentError(f"{type(exc).__name__}: {exc}") from exc
        self._tasks.append(asyncio.create_task(self._gemini_sender()))
        self._tasks.append(asyncio.create_task(self._gemini_receiver()))
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
        transcription = getattr(content, "input_transcription", None)
        if transcription is not None:
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

    def _record_fragment(self, speaker, text):
        """Transcriptions arrive as one-to-three-word fragments; join
        consecutive fragments from the same speaker into a turn."""
        if not text:
            return
        if self._turns and self._turns[-1]["speaker"] == speaker:
            self._turns[-1]["text"] += text
            return
        self._turns.append({"speaker": speaker, "at": iso_utc(), "text": text})

    # --- teardown ----------------------------------------------------------
    async def stop(self):
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
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
