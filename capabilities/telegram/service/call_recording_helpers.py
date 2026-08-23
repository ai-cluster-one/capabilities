"""Shared call recording finalization and delivery helpers.

Used by both daemon.py (p2p call listener) and call_recorder.py (group watcher)
to avoid duplicate finalization implementations.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import sys
from array import array
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeFilename

# What a recording's own status has to say before its audio may be sent. A
# recovered one is finalized by the process that came after the crash rather
# than by the one that opened it, and is delivered on the same terms; its
# stop_reason goes on saying which of the two happened.
DELIVERABLE_STATUSES = ("complete", "recovered")


async def probe_audio_duration(path: Path) -> float | None:
    """Return the finalized media duration without making delivery depend on probing."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
    except OSError:
        return None
    if process.returncode != 0:
        return None
    try:
        duration = float(stdout.decode().strip())
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


async def trailing_silence_start(path: Path, floor: str = "-45dB",
                                 minimum: float = 10.0,
                                 tolerance: float = 1.0) -> float | None:
    """Where the run of silence that reaches the end of `path` begins.

    A capture closed by its own process ends where the call ended. One closed by
    the process dying ends wherever the process died, which is either mid-word
    or minutes into a room nobody left, and only the file can tell those apart.
    Returns None when the audio does not end in silence at all, so a recovery
    with nothing to trim trims nothing.

    The last silence is matched against the file's own length rather than read
    off an unclosed `silence_start`: ffmpeg closes the final stretch of silence
    at EOF like any other, so an unclosed one never appears and every silence
    would otherwise look like a pause in the middle.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i", str(path),
            "-map", "0:a:0",
            "-af", f"silencedetect=n={floor}:d={minimum:g}",
            "-f", "null",
            "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except OSError:
        return None
    if process.returncode != 0:
        return None
    started = ended = None
    for line in stderr.decode(errors="replace").splitlines():
        if "silence_start:" in line:
            try:
                started, ended = float(
                    line.rsplit("silence_start:", 1)[1].strip()), None
            except ValueError:
                started = ended = None
        elif "silence_end:" in line and started is not None:
            try:
                ended = float(line.rsplit("silence_end:", 1)[1]
                              .split("|", 1)[0].strip())
            except ValueError:
                ended = None
    if started is None:
        return None
    if ended is None:
        return started
    duration = await probe_audio_duration(path)
    if duration is None:
        return None
    return started if duration - ended <= tolerance else None


async def finalize_mp3_capture(capture: Path, output: Path,
                               keep_source: bool = False,
                               trim_to: float | None = None) -> dict:
    """Convert a closed built-in MP3 capture to the final OGG/Opus artifact.

    keep_source leaves the MP3 where it was written, so what the capture
    actually received can be told apart from what the conversion made of it.

    trim_to cuts the output at that many seconds. Nothing on the live path asks
    for it: it is how a recovered capture drops the silence it went on writing
    after everyone had gone."""
    result = {
        "status": "failed",
        "error": None,
        "source_path": str(capture),
        "source_bytes": capture.stat().st_size if capture.exists() else 0,
        "source_retained": capture.exists(),
        "output_bytes": 0,
        "duration_seconds": None,
    }
    if not result["source_bytes"]:
        result["error"] = "mp3_capture_is_empty"
        return result

    temporary = output.with_name(f".{output.stem}.{os.getpid()}.tmp.ogg")
    with contextlib.suppress(OSError):
        temporary.unlink()
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-i", str(capture),
            *(("-t", f"{trim_to:.3f}") if trim_to and trim_to > 0 else ()),
            "-map", "0:a:0",
            "-codec:a", "libopus",
            "-b:a", "96k",
            "-vbr", "on",
            "-application", "voip",
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
    if not keep_source:
        try:
            capture.unlink()
        except OSError as exc:
            result["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:500]
    result["source_retained"] = capture.exists()
    return result


def iso_utc(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def write_metadata(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    os.replace(temporary, path)


def display_duration(seconds: float | int | None) -> str:
    total = max(0, round(float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def recording_caption(metadata: dict) -> str:
    duration = display_duration(metadata.get("duration_seconds"))
    return f"Запись звонка · {duration}"


def pack_voice_waveform(values: list[int]) -> bytes:
    """Pack Telegram's 5-bit waveform samples into their wire representation."""
    packed = bytearray((len(values) * 5 + 7) // 8)
    bit_offset = 0
    for value in values:
        sample = max(0, min(31, int(value)))
        byte_offset, shift = divmod(bit_offset, 8)
        packed[byte_offset] |= (sample << shift) & 0xFF
        if shift > 3:
            packed[byte_offset + 1] |= sample >> (8 - shift)
        bit_offset += 5
    return bytes(packed)


def voice_waveform_from_pcm(pcm: bytes, bars: int = 100) -> bytes | None:
    """Build a normalized Telegram waveform from mono signed 16-bit PCM."""
    if bars <= 0 or len(pcm) < 2:
        return None
    amplitudes = array("h")
    amplitudes.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        amplitudes.byteswap()
    sample_count = len(amplitudes)
    count = min(bars, sample_count)
    levels: list[float] = []
    for index in range(count):
        start = index * sample_count // count
        end = max(start + 1, (index + 1) * sample_count // count)
        energy = sum(int(sample) * int(sample) for sample in amplitudes[start:end])
        levels.append(math.sqrt(energy / (end - start)))
    peak = max(levels, default=0)
    if peak <= 0:
        return pack_voice_waveform([0] * count)
    normalized = [round(31 * math.sqrt(level / peak)) for level in levels]
    return pack_voice_waveform(normalized)


async def build_voice_waveform(path: Path) -> bytes | None:
    """Decode a low-rate mono envelope for Telegram's voice-note waveform."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-v", "error",
            "-i", str(path),
            "-map", "0:a:0",
            "-ac", "1",
            "-ar", "800",
            "-f", "s16le",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pcm, _ = await process.communicate()
    except OSError:
        return None
    if process.returncode != 0:
        return None
    return voice_waveform_from_pcm(pcm)


def voice_document_attributes(
    duration_seconds: float | int | None,
    waveform: bytes | None,
) -> list:
    """Describe the finalized OGG as a downloadable Telegram voice note."""
    duration = max(1, round(float(duration_seconds or 0)))
    return [
        DocumentAttributeFilename(file_name="recording.ogg"),
        DocumentAttributeAudio(duration=duration, voice=True, waveform=waveform),
    ]


async def send_recording_to_chat(
    client: TelegramClient,
    chat_id: int,
    output: Path,
    metadata_path: Path,
    metadata: dict,
    emit_event_fn=None,
    caption: str | None = None,
) -> None:
    """Send finalized recording to a Telegram chat as a voice note.

    emit_event_fn: optional callable(event_name, **fields) for logging events.
    caption: the line the recording is announced with; the built-in one when
    the caller names none.
    """
    delivery = metadata["delivery"]
    if metadata.get("status") not in DELIVERABLE_STATUSES:
        delivery.update({"status": "skipped", "error": "recording_is_not_complete"})
        write_metadata(metadata_path, metadata)
        return
    if not metadata["audio"].get("settled"):
        delivery.update({"status": "failed", "error": "recording_file_not_finalized"})
        write_metadata(metadata_path, metadata)
        if emit_event_fn:
            emit_event_fn("recording_send_failed", chat_id=chat_id,
                          output=str(output), error=delivery["error"])
        return

    waveform = await build_voice_waveform(output)
    for attempt in range(1, 4):
        delivery.update({"status": "sending", "attempts": attempt, "error": None})
        write_metadata(metadata_path, metadata)
        try:
            if not delivery.get("notice_message_id"):
                notice = await client.send_message(
                    chat_id,
                    caption or recording_caption(metadata),
                )
                delivery["notice_message_id"] = getattr(notice, "id", None)
                write_metadata(metadata_path, metadata)
            message = await client.send_file(
                chat_id,
                file=str(output),
                mime_type="audio/ogg",
                attributes=voice_document_attributes(
                    metadata.get("duration_seconds"),
                    waveform,
                ),
                force_document=False,
                voice_note=True,
            )
        except Exception as exc:
            delivery.update({
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            })
            write_metadata(metadata_path, metadata)
            if emit_event_fn:
                emit_event_fn(
                    "recording_send_retry",
                    chat_id=chat_id,
                    output=str(output),
                    attempt=attempt,
                    error=delivery["error"],
                )
            if attempt < 3:
                delay = min(max(float(getattr(exc, "seconds", 0) or 0), 2 * attempt), 30)
                await asyncio.sleep(delay)
            continue
        delivery.update({
            "status": "sent",
            "message_id": getattr(message, "id", None),
            "sent_at": iso_utc(),
            "error": None,
        })
        write_metadata(metadata_path, metadata)
        if emit_event_fn:
            emit_event_fn(
                "recording_sent",
                chat_id=chat_id,
                output=str(output),
                message_id=delivery["message_id"],
                notice_message_id=delivery.get("notice_message_id"),
                attempts=attempt,
            )
        return


