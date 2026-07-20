#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "py-tgcalls==2.3.3",
#     "telethon>=1.36",
# ]
# ///
"""Record an existing Telegram group voice chat through a full MTProto account.

This is a bundled engine helper, not a public capability executable.  The
telegram CLI owns the eventual command surface; this helper owns the long-lived
PyTgCalls media process and writes recordings under connection-scoped state.

The recorder deliberately refuses to create a voice chat.  An administrator
starts the chat in Telegram first, then this process joins as the selected user
account and records the mixed incoming audio until SIGINT/SIGTERM or until the
voice chat closes.  Watch mode is supervised by the assistant daemon: automatic
groups are probed for active calls, while on-request groups are activated by
small request files written by the daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ntgcalls import MediaSource
from pytgcalls import PyTgCalls, filters
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError
from pytgcalls.types import CallConfig, ChatUpdate, GroupCallConfig
from pytgcalls.types.raw import AudioParameters, AudioStream, Stream
from telethon import TelegramClient, events
from telethon.errors import AuthKeyError, RPCError
from telethon.errors.common import TypeNotFoundError
from telethon.tl.types import (
    InputGroupCallInviteMessage,
    InputGroupCallSlug,
    MessageActionConferenceCall,
    MessageActionGroupCall,
    MessageActionInviteToGroupCall,
)


CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
USER_CONNECTIONS = CONFIG_HOME / "telegram" / "connections.json"
USER_CREDENTIALS = CONFIG_HOME / "telegram" / "credentials.env"


class RecorderError(RuntimeError):
    def __init__(self, exit_code: int, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code
        self.message = message
        self.hint = hint


def emit(value) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def emit_event(event: str, **fields) -> None:
    payload = {"event": event, **fields}
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def die(error: RecorderError) -> None:
    payload = {"code": error.code, "message": error.message}
    if error.hint:
        payload["hint"] = error.hint
    sys.stderr.write(json.dumps({"error": payload}, ensure_ascii=False) + "\n")
    raise SystemExit(error.exit_code)


def find_project_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    start = os.environ.get("TELEGRAM_SERVICE_PROJECT_ROOT") or os.getcwd()
    here = Path(start).expanduser().resolve()
    home = Path.home().resolve()
    for candidate in (here, *here.parents):
        if candidate == home:
            break
        if ((candidate / "capabilities" / "settings.json").is_file()
                or (candidate / ".capabilities").is_dir()
                or (candidate / ".env.local").is_file()
                or (candidate / ".env").is_file()
                or (candidate / ".git").is_dir()):
            return candidate
    return here


def project_capabilities_dir(root: Path) -> Path:
    current = root / "capabilities"
    legacy = root / ".capabilities"
    if (current / "settings.json").is_file() or not legacy.is_dir():
        return current
    return legacy


def parse_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        values[key] = value.strip().strip('"').strip("'")
    return values


def env_value(root: Path, key: str) -> str | None:
    for path in (root / ".env.local", root / ".env", USER_CREDENTIALS):
        value = parse_env_file(path).get(key)
        if value:
            return value
    return os.environ.get(key) or None


def connections_registry(root: Path) -> tuple[dict | None, Path | None]:
    candidates = [project_capabilities_dir(root) / "telegram" / "connections.json",
                  USER_CONNECTIONS]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise RecorderError(6, "bad_config", f"cannot read {path}: {exc}") from exc
        if not isinstance(data.get("connections"), dict) or not data["connections"]:
            raise RecorderError(6, "bad_config", f"{path} is not a connections envelope")
        return data, path
    return None, None


def select_connection(registry: dict | None, wanted: str | None) -> tuple[str, dict | None]:
    if registry is None:
        if wanted and wanted != "default":
            raise RecorderError(6, "unknown_connection",
                                f"no Telegram connection matches {wanted!r}")
        return "default", None
    connections = registry["connections"]
    selected = wanted or registry.get("default")
    if selected:
        if selected not in connections:
            raise RecorderError(6, "unknown_connection",
                                f"no Telegram connection matches {selected!r}",
                                f"known: {', '.join(connections)}")
        return selected, connections[selected]
    if len(connections) == 1:
        selected = next(iter(connections))
        return selected, connections[selected]
    raise RecorderError(6, "ambiguous_connection",
                        "multiple Telegram connections are configured; pass --connection")


def resolve_connection(root: Path, wanted: str | None) -> dict:
    registry, registry_path = connections_registry(root)
    connection_id, entry = select_connection(registry, wanted)
    entry = entry or {}
    api_id = entry.get("api_id") or env_value(root, "TELEGRAM_API_ID")
    secret_key = entry.get("secret_env") or "TELEGRAM_API_HASH"
    api_hash = env_value(root, secret_key)
    if not api_id or not api_hash:
        missing = "api_id" if not api_id else secret_key
        raise RecorderError(2, "missing_credentials",
                            f"Telegram connection {connection_id!r} is missing {missing}")
    session_value = entry.get("session")
    if session_value:
        session = Path(session_value).expanduser()
        if not session.is_absolute():
            session = root / session
    else:
        session = STATE_HOME / "telegram" / connection_id / "session"
    return {
        "id": connection_id,
        "api_id": int(api_id),
        "api_hash": api_hash,
        "session": session,
        "registry": registry_path,
    }


def safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "chat"


def hot_copy_session(source_stem: Path, runtime_dir: Path) -> Path:
    source = Path(str(source_stem) + ".session")
    if not source.is_file():
        raise RecorderError(2, "session_missing", f"Telegram session not found: {source}",
                            "run `telegram login --connection <id>` first")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / f"session-{os.getpid()}.session"
    for suffix in ("", "-journal", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(str(target) + suffix).unlink()
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=3) as src:
            with sqlite3.connect(target, timeout=3) as dst:
                src.backup(dst)
    except sqlite3.Error as exc:
        raise RecorderError(5, "session_copy_failed",
                            f"cannot snapshot active Telegram session: {exc}") from exc
    return target.with_suffix("")


def cleanup_session(stem: Path) -> None:
    for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
        with contextlib.suppress(OSError):
            Path(str(stem) + suffix).unlink()


def default_output(connection_id: str, chat_ref: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (STATE_HOME / "telegram" / connection_id / "calls" / "recordings"
            / f"{timestamp}-{safe_part(chat_ref)}.ogg")


def watch_output(connection_id: str, chat_id: int, call_id: int) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (STATE_HOME / "telegram" / connection_id / "calls" / "recordings"
            / f"{timestamp}-{safe_part(str(chat_id))}-call-{call_id}.ogg")


def ogg_opus_record_stream(output: Path) -> Stream:
    """Write playback PCM directly to an OGG container with the Opus codec.

    PyTgCalls' convenience ``RecordStream(path)`` always invokes libmp3lame for
    48 kHz audio, regardless of the filename extension.  Supplying a raw shell
    stream lets the media engine encode the final OGG/Opus artifact while the
    call is in progress, without creating or converting an intermediate MP3.
    """
    if output.suffix.lower() != ".ogg":
        raise RecorderError(6, "ogg_output_required", "recording output must end in .ogg")
    command = shlex.join([
        "ffmpeg",
        "-y",
        "-loglevel", "quiet",
        "-f", "s16le",
        "-ar", "48000",
        "-ac", "2",
        "-i", "pipe:0",
        "-codec:a", "libopus",
        "-b:a", "96k",
        "-vbr", "on",
        "-application", "voip",
        "-f", "ogg",
        "-flush_packets", "1",
        str(output),
    ])
    return Stream(
        microphone=AudioStream(
            media_source=MediaSource.SHELL,
            path=command,
            parameters=AudioParameters(bitrate=48000, channels=2),
        ),
    )


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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "chat",
        nargs="?",
        help="group/channel id, @username, or exact dialog title with an active voice chat",
    )
    result.add_argument("--connection", help="Telegram connection id")
    result.add_argument("--project-root", help="consuming project root")
    result.add_argument("--output", help=".ogg recording path; defaults to connection state")
    result.add_argument("--probe", action="store_true",
                        help="prove account/chat readiness without joining the call")
    result.add_argument(
        "--listen",
        action="store_true",
        help="wait for an incoming private call, accept it, and record automatically",
    )
    result.add_argument(
        "--caller",
        action="append",
        default=[],
        help="allowed incoming caller user id; repeatable (default: any caller)",
    )
    result.add_argument(
        "--primary-session",
        action="store_true",
        help="use the connection session directly; its regular daemon must be stopped",
    )
    result.add_argument(
        "--invite-link",
        help="join an incoming conference directly using its https://t.me/call/... link",
    )
    result.add_argument(
        "--watch-groups",
        action="store_true",
        help="stay alive and record configured group calls for the assistant daemon",
    )
    result.add_argument(
        "--auto-group",
        action="append",
        default=[],
        help="group id whose active calls are recorded automatically; repeatable",
    )
    result.add_argument(
        "--request-group",
        action="append",
        default=[],
        help="group id recorded only after a daemon request; repeatable",
    )
    result.add_argument(
        "--send-to-chat-group",
        action="append",
        default=[],
        help="recorded group whose finalized OGG is sent back to the chat; repeatable",
    )
    result.add_argument(
        "--request-dir",
        help="directory containing daemon recording-request JSON files",
    )
    result.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="active-call/request probe interval in watch mode (default: 2)",
    )
    return result


def call_invite_slug(value: str) -> str:
    match = re.fullmatch(
        r"https://t\.me/call/([A-Za-z0-9_-]+)(?:[?#].*)?",
        value.strip(),
    )
    if not match:
        raise RecorderError(6, "bad_invite_link", "expected an https://t.me/call/... link")
    return match.group(1)


async def resolve_chat_id(calls: PyTgCalls, client: TelegramClient, reference: str) -> int:
    try:
        return await calls.resolve_chat_id(reference)
    except (TypeError, ValueError):
        pass

    wanted = reference.strip().casefold()
    matches = []
    async for dialog in client.iter_dialogs():
        if (dialog.name or "").strip().casefold() == wanted:
            matches.append(dialog)
    if not matches:
        raise RecorderError(
            6,
            "chat_not_found",
            f"no Telegram dialog matches {reference!r}",
            "open the group with this account, or pass its numeric id or @username",
        )
    if len(matches) > 1:
        ids = ", ".join(str(dialog.id) for dialog in matches)
        raise RecorderError(
            6,
            "ambiguous_chat",
            f"multiple Telegram dialogs are titled {reference!r}: {ids}",
            "pass the numeric chat id",
        )
    return await calls.resolve_chat_id(matches[0].id)


async def inspect_group_call(calls: PyTgCalls, chat_id: int) -> dict:
    bridge = getattr(getattr(calls, "_app", None), "_bind_client", None)
    get_call = getattr(bridge, "get_call", None)
    if get_call is None:
        raise RecorderError(5, "probe_unsupported", "PyTgCalls call inspection is unavailable")
    active = await get_call(chat_id) is not None
    participants = await calls.get_participants(chat_id) if active else []
    return {
        "active": active,
        "participants": len(participants),
        "unmuted": sum(
            not participant.muted and not participant.muted_by_admin
            for participant in participants
        ),
        "video": sum(participant.video for participant in participants),
        "camera": sum(participant.video_camera for participant in participants),
        "screen_sharing": sum(participant.screen_sharing for participant in participants),
    }


async def run(args: argparse.Namespace, config: dict, runtime_session: Path) -> dict:
    client = TelegramClient(str(runtime_session), config["api_id"], config["api_hash"])
    calls: PyTgCalls | None = None
    joined = False
    chat_id: int | None = None
    output: Path | None = None
    started_at: float | None = None
    stop_reason = "signal"
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RecorderError(2, "session_unauthorized",
                                f"Telegram session for {config['id']!r} is not authorized")
        me = await client.get_me()
        calls = PyTgCalls(client)
        # PyTgCalls prints a version banner; keep stdout reserved for our JSON result.
        with contextlib.redirect_stdout(sys.stderr):
            await calls.start()
        chat_id = await resolve_chat_id(calls, client, args.chat)
        if chat_id >= 0:
            raise RecorderError(6, "group_required",
                                "the first recorder prototype accepts group/channel voice chats only")
        entity = await client.get_entity(chat_id)
        title = getattr(entity, "title", None) or str(args.chat)
        if args.probe:
            group_call = await inspect_group_call(calls, chat_id)
            return {
                "ok": True,
                "mode": "probe",
                "connection": config["id"],
                "account": getattr(me, "username", None) or str(me.id),
                "chat_id": chat_id,
                "chat": title,
                "group_call": group_call,
            }

        output = (Path(args.output).expanduser().resolve() if args.output
                  else default_output(config["id"], str(chat_id)))
        if output.exists():
            raise RecorderError(6, "output_exists", f"recording already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)

        stop_event = asyncio.Event()

        @calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def call_closed(_client: PyTgCalls, update: ChatUpdate):
            nonlocal stop_reason
            if update.chat_id == chat_id:
                stop_reason = "voice_chat_closed"
                stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)

        await calls.record(
            chat_id,
            ogg_opus_record_stream(output),
            config=GroupCallConfig(auto_start=False),
        )
        await calls.mute(chat_id)
        joined = True
        started_at = time.time()
        emit_event("recording_started", connection=config["id"], account=me.username,
                   chat_id=chat_id, chat=title, output=str(output))
        await stop_event.wait()
        with contextlib.suppress(NotInCallError, NoActiveGroupCall, RPCError):
            await calls.leave_call(chat_id)
            joined = False
        # The recording subprocess flushes its container as the call source closes.
        await asyncio.sleep(0.25)
        return {
            "ok": True,
            "mode": "record",
            "connection": config["id"],
            "account": getattr(me, "username", None) or str(me.id),
            "chat_id": chat_id,
            "chat": title,
            "output": str(output),
            "bytes": output.stat().st_size if output.exists() else 0,
            "duration_seconds": round(time.time() - started_at, 3),
            "stopped": stop_reason,
        }
    except NoActiveGroupCall as exc:
        raise RecorderError(3, "no_active_voice_chat",
                            "the Telegram group has no active voice chat",
                            "start the voice chat in Telegram, then retry") from exc
    except (AuthKeyError, RPCError) as exc:
        raise RecorderError(5, "telegram_error", str(exc)) from exc
    finally:
        if calls is not None and joined and chat_id is not None:
            with contextlib.suppress(NotInCallError, NoActiveGroupCall, RPCError):
                await calls.leave_call(chat_id)
        if client.is_connected():
            await client.disconnect()


def participant_row(participant) -> dict:
    return {
        "user_id": str(participant.user_id),
        "source": participant.source,
        "muted": bool(participant.muted),
        "muted_by_admin": bool(participant.muted_by_admin),
        "video": bool(participant.video),
        "screen_sharing": bool(participant.screen_sharing),
    }


async def participant_snapshot(calls: PyTgCalls, chat_id: int) -> list[dict]:
    try:
        participants = await calls.get_participants(chat_id) or []
    except (NotInCallError, NoActiveGroupCall, RPCError):
        return []
    return sorted(
        (participant_row(participant) for participant in participants),
        key=lambda item: int(item["user_id"]),
    )


async def group_call_started_at(
    client: TelegramClient,
    chat_id: int,
    call_id: int,
) -> str | None:
    """Best-effort Telegram start time from the matching group-call service message."""
    try:
        async for message in client.iter_messages(chat_id, limit=100):
            action = getattr(message, "action", None)
            if not isinstance(action, MessageActionGroupCall):
                continue
            if getattr(getattr(action, "call", None), "id", None) != call_id:
                continue
            if getattr(action, "duration", None) is None:
                return iso_utc(getattr(message, "date", None))
    except RPCError:
        return None
    return None


def load_request(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        emit_event("recording_request_invalid", path=str(path), error=str(exc))
        return None
    return value if isinstance(value, dict) else None


async def settled_file_size(path: Path, attempts: int = 20) -> tuple[int, bool]:
    """Wait until the encoder has stopped growing the finalized container."""
    previous = -1
    stable_reads = 0
    size = 0
    for _ in range(attempts):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > 0 and size == previous:
            stable_reads += 1
            if stable_reads >= 2:
                return size, True
        else:
            stable_reads = 0
        previous = size
        await asyncio.sleep(0.25)
    return size, False


def display_duration(seconds: float | int | None) -> str:
    total = max(0, round(float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def recording_caption(metadata: dict) -> str:
    duration = display_duration(metadata.get("duration_seconds"))
    return f"Запись звонка · {duration}"


async def send_recording_to_chat(
    client: TelegramClient,
    chat_id: int,
    output: Path,
    metadata_path: Path,
    metadata: dict,
) -> None:
    delivery = metadata["delivery"]
    if metadata.get("status") != "complete":
        delivery.update({"status": "skipped", "error": "recording_is_not_complete"})
        write_metadata(metadata_path, metadata)
        return
    if not metadata["audio"].get("settled"):
        delivery.update({"status": "failed", "error": "recording_file_did_not_settle"})
        write_metadata(metadata_path, metadata)
        emit_event("recording_send_failed", chat_id=chat_id,
                   output=str(output), error=delivery["error"])
        return

    for attempt in range(1, 4):
        delivery.update({"status": "sending", "attempts": attempt, "error": None})
        write_metadata(metadata_path, metadata)
        try:
            message = await client.send_file(
                chat_id,
                file=str(output),
                caption=recording_caption(metadata),
            )
        except Exception as exc:
            delivery.update({
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            })
            write_metadata(metadata_path, metadata)
            emit_event(
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
        emit_event(
            "recording_sent",
            chat_id=chat_id,
            output=str(output),
            message_id=delivery["message_id"],
            attempts=attempt,
        )
        return


async def watch_groups(
    args: argparse.Namespace,
    config: dict,
    runtime_session: Path,
) -> dict:
    auto_groups = {int(value) for value in args.auto_group}
    request_groups = {int(value) for value in args.request_group}
    send_to_chat_groups = {int(value) for value in args.send_to_chat_group}
    overlap = auto_groups & request_groups
    if overlap:
        raise RecorderError(
            6,
            "duplicate_group_mode",
            f"groups have both auto and on-request modes: {sorted(overlap)}",
        )
    all_groups = auto_groups | request_groups
    if not all_groups:
        raise RecorderError(6, "groups_required", "watch mode requires at least one group")
    if any(chat_id >= 0 for chat_id in all_groups):
        raise RecorderError(6, "group_required", "watch mode accepts negative group ids only")
    unknown_delivery_groups = send_to_chat_groups - all_groups
    if unknown_delivery_groups:
        raise RecorderError(
            6,
            "delivery_group_not_recorded",
            f"send-to-chat groups are not configured for recording: {sorted(unknown_delivery_groups)}",
        )
    if args.poll_seconds < 0.25:
        raise RecorderError(6, "bad_poll_interval", "--poll-seconds must be at least 0.25")

    request_dir = (
        Path(args.request_dir).expanduser().resolve()
        if args.request_dir
        else STATE_HOME / "telegram" / config["id"] / "service" / "call-recording-requests"
    )
    if request_groups:
        request_dir.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(runtime_session), config["api_id"], config["api_hash"])
    calls = PyTgCalls(client)
    stop_event = asyncio.Event()
    call_closed_event = asyncio.Event()
    active: dict | None = None
    recorded_call_ids: dict[int, int] = {}
    schema_fallback_groups: set[int] = set()
    recordings = 0

    bridge = getattr(getattr(calls, "_app", None), "_bind_client", None)

    async def active_call_from_history(chat_id: int):
        """Recover the current call without decoding ChannelFull.

        Telegram can introduce a ChannelFull constructor before Telethon ships
        its schema. Group-call service messages still carry an InputGroupCall,
        so the newest start/end action is a safe fallback and lets PyTgCalls
        join through its cache without repeating the unsupported full-chat read.
        """
        try:
            async for message in client.iter_messages(chat_id, limit=100):
                action = getattr(message, "action", None)
                if not isinstance(action, MessageActionGroupCall):
                    continue
                if getattr(action, "duration", None) is not None:
                    return None
                call_ref = getattr(action, "call", None)
                if call_ref is None:
                    return None
                cache = getattr(bridge, "_cache", None)
                if cache is not None:
                    cache.set_cache(chat_id, call_ref)
                emit_event(
                    "group_probe_fallback",
                    chat_id=chat_id,
                    call_id=str(getattr(call_ref, "id", "")),
                    source_message_id=message.id,
                )
                return call_ref
        except (RPCError, TypeNotFoundError) as exc:
            emit_event(
                "group_probe_failed",
                chat_id=chat_id,
                method="service_message_fallback",
                error=type(exc).__name__,
            )
        return None

    async def active_call(chat_id: int):
        get_call = getattr(bridge, "get_call", None)
        if get_call is None:
            raise RecorderError(5, "watch_unsupported", "PyTgCalls call inspection is unavailable")
        try:
            call_ref = await get_call(chat_id)
            schema_fallback_groups.discard(chat_id)
            cache = getattr(bridge, "_cache", None)
            if call_ref is not None and cache is not None:
                cache.set_cache(chat_id, call_ref)
            return call_ref
        except TypeNotFoundError:
            if chat_id not in schema_fallback_groups:
                emit_event(
                    "group_probe_schema_fallback",
                    chat_id=chat_id,
                    reason="unknown_full_chat_constructor",
                )
                schema_fallback_groups.add(chat_id)
            return await active_call_from_history(chat_id)
        except RPCError as exc:
            emit_event("group_probe_failed", chat_id=chat_id, error=str(exc))
            return None

    async def start_recording(chat_id: int, trigger: str, request: dict | None = None) -> bool:
        nonlocal active
        call_ref = await active_call(chat_id)
        if call_ref is None:
            recorded_call_ids.pop(chat_id, None)
            emit_event("recording_not_started", chat_id=chat_id, trigger=trigger,
                       reason="no_active_voice_chat")
            return False
        call_id = int(call_ref.id)
        if recorded_call_ids.get(chat_id) == call_id:
            emit_event("recording_not_started", chat_id=chat_id, call_id=str(call_id),
                       trigger=trigger, reason="call_already_recorded")
            return False

        entity = await client.get_entity(chat_id)
        title = getattr(entity, "title", None) or str(chat_id)
        output = watch_output(config["id"], chat_id, call_id)
        metadata_path = output.with_suffix(".json")
        output.parent.mkdir(parents=True, exist_ok=True)
        detected_at = iso_utc()
        metadata = {
            "schema_version": 1,
            "status": "joining",
            "connection": config["id"],
            "account_id": str((await client.get_me()).id),
            "chat_id": str(chat_id),
            "chat_title": title,
            "telegram_call_id": str(call_id),
            "telegram_call_started_at": await group_call_started_at(client, chat_id, call_id),
            "detected_at": detected_at,
            "recording_started_at": None,
            "recording_ended_at": None,
            "duration_seconds": None,
            "trigger": trigger,
            "request": request,
            "stop_reason": None,
            "audio": {
                "path": str(output),
                "format": output.suffix.lstrip("."),
                "codec": "opus",
                "bytes": 0,
                "tracks": [{"kind": "mixed", "participants_separated": False}],
            },
            "participants_at_start": [],
            "participants_at_end": [],
            "delivery": {
                "enabled": chat_id in send_to_chat_groups,
                "status": "pending" if chat_id in send_to_chat_groups else "disabled",
                "attempts": 0,
                "message_id": None,
                "sent_at": None,
                "error": None,
            },
        }
        write_metadata(metadata_path, metadata)
        try:
            await calls.record(
                chat_id,
                ogg_opus_record_stream(output),
                config=GroupCallConfig(auto_start=False),
            )
            await calls.mute(chat_id)
        except NoActiveGroupCall:
            metadata.update({"status": "not_started", "stop_reason": "no_active_voice_chat"})
            write_metadata(metadata_path, metadata)
            return False

        started_monotonic = time.monotonic()
        recorded_call_ids[chat_id] = call_id
        active = {
            "chat_id": chat_id,
            "call_id": call_id,
            "output": output,
            "metadata_path": metadata_path,
            "metadata": metadata,
            "started_monotonic": started_monotonic,
            "stop_reason": None,
        }
        await asyncio.sleep(0.25)
        metadata.update({
            "status": "recording",
            "recording_started_at": iso_utc(),
            "participants_at_start": await participant_snapshot(calls, chat_id),
        })
        write_metadata(metadata_path, metadata)
        emit_event(
            "recording_started",
            connection=config["id"],
            chat_id=chat_id,
            chat=title,
            call_id=str(call_id),
            trigger=trigger,
            output=str(output),
            metadata=str(metadata_path),
        )
        return True

    async def finish_recording(reason: str) -> None:
        nonlocal active, recordings
        if active is None:
            return
        current = active
        chat_id = current["chat_id"]
        metadata = current["metadata"]
        metadata["participants_at_end"] = await participant_snapshot(calls, chat_id)
        with contextlib.suppress(NotInCallError, NoActiveGroupCall, RPCError):
            await calls.leave_call(chat_id)
        output = current["output"]
        duration = round(time.monotonic() - current["started_monotonic"], 3)
        recording_ended_at = iso_utc()
        size, settled = await settled_file_size(output)
        metadata.update({
            "status": "complete" if size else "empty",
            "recording_ended_at": recording_ended_at,
            "duration_seconds": duration,
            "stop_reason": reason,
        })
        metadata["audio"]["bytes"] = size
        metadata["audio"]["settled"] = settled
        write_metadata(current["metadata_path"], metadata)
        emit_event(
            "recording_finished",
            chat_id=chat_id,
            call_id=str(current["call_id"]),
            output=str(output),
            metadata=str(current["metadata_path"]),
            bytes=size,
            duration_seconds=duration,
            stop_reason=reason,
        )
        if metadata["delivery"]["enabled"]:
            await send_recording_to_chat(
                client,
                chat_id,
                output,
                current["metadata_path"],
                metadata,
            )
        recordings += 1
        active = None
        call_closed_event.clear()

    @calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
    async def call_closed(_client: PyTgCalls, update: ChatUpdate):
        if active is not None and update.chat_id == active["chat_id"]:
            active["stop_reason"] = "voice_chat_closed"
            call_closed_event.set()

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RecorderError(2, "session_unauthorized",
                                f"Telegram session for {config['id']!r} is not authorized")
        me = await client.get_me()
        with contextlib.redirect_stdout(sys.stderr):
            await calls.start()
        # calls.start() creates the MTProto bridge used by active_call().
        bridge = getattr(getattr(calls, "_app", None), "_bind_client", None)
        emit_event(
            "group_watcher_started",
            connection=config["id"],
            account=getattr(me, "username", None) or str(me.id),
            auto_groups=sorted(auto_groups),
            request_groups=sorted(request_groups),
            send_to_chat_groups=sorted(send_to_chat_groups),
            request_dir=str(request_dir),
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)

        while not stop_event.is_set():
            if active is not None:
                try:
                    await asyncio.wait_for(call_closed_event.wait(), timeout=args.poll_seconds)
                except asyncio.TimeoutError:
                    call_ref = await active_call(active["chat_id"])
                    if call_ref is None or int(call_ref.id) != active["call_id"]:
                        active["stop_reason"] = "voice_chat_closed"
                        call_closed_event.set()
                if call_closed_event.is_set():
                    await finish_recording(active["stop_reason"] or "voice_chat_closed")
                continue

            for path in sorted(request_dir.glob("*.json")) if request_groups else ():
                request = load_request(path)
                with contextlib.suppress(OSError):
                    path.unlink()
                if request is None:
                    continue
                try:
                    chat_id = int(request.get("chat_id"))
                except (TypeError, ValueError):
                    emit_event("recording_request_invalid", path=str(path),
                               error="chat_id is missing or invalid")
                    continue
                if chat_id not in request_groups and chat_id not in auto_groups:
                    emit_event("recording_request_ignored", chat_id=chat_id,
                               reason="group_not_configured")
                    continue
                if await start_recording(chat_id, "text_request", request):
                    break

            if active is None:
                for chat_id in sorted(auto_groups):
                    call_ref = await active_call(chat_id)
                    if call_ref is None:
                        recorded_call_ids.pop(chat_id, None)
                        continue
                    if recorded_call_ids.get(chat_id) == int(call_ref.id):
                        continue
                    if await start_recording(chat_id, "automatic"):
                        break

            if active is None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=args.poll_seconds)
                except asyncio.TimeoutError:
                    pass

        if active is not None:
            await finish_recording("daemon_shutdown")
        return {
            "ok": True,
            "mode": "watch_groups",
            "connection": config["id"],
            "recordings": recordings,
            "stopped": "signal",
        }
    except (AuthKeyError, RPCError) as exc:
        raise RecorderError(5, "telegram_error", str(exc)) from exc
    finally:
        if active is not None:
            with contextlib.suppress(NotInCallError, NoActiveGroupCall, RPCError):
                await calls.leave_call(active["chat_id"])
        if client.is_connected():
            await client.disconnect()


async def listen(args: argparse.Namespace, config: dict, runtime_session: Path) -> dict:
    client = TelegramClient(str(runtime_session), config["api_id"], config["api_hash"])
    calls = PyTgCalls(client)
    stop_event = asyncio.Event()
    allowed_callers = {int(value) for value in args.caller}
    active: dict | None = None
    failure: str | None = None
    stop_reason = "signal"
    conference_poll_task: asyncio.Task | None = None

    async def start_recording(
        call_client: PyTgCalls,
        caller_id: int,
        mode: str,
        call_ref=None,
        source_message_id: int | None = None,
    ) -> None:
        nonlocal active, failure
        if allowed_callers and caller_id not in allowed_callers:
            emit_event("incoming_call_ignored", caller_id=caller_id, reason="caller_not_allowed")
            return
        if active is not None:
            emit_event("incoming_call_ignored", caller_id=caller_id, reason="recorder_busy")
            return
        output = (Path(args.output).expanduser().resolve() if args.output
                  else default_output(config["id"], f"{mode}-{caller_id}"))
        if output.exists():
            failure = f"recording already exists: {output}"
            stop_event.set()
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        if call_ref is not None:
            bridge = getattr(getattr(call_client, "_app", None), "_bind_client", None)
            cache = getattr(bridge, "_cache", None)
            if cache is None:
                failure = "PyTgCalls conference-call cache is unavailable"
                stop_event.set()
                return
            cache.set_cache(caller_id, call_ref)
        active = {
            "caller_id": caller_id,
            "mode": mode,
            "source_message_id": source_message_id,
            "output": output,
            "started_at": time.time(),
        }
        emit_event(
            "incoming_call",
            caller_id=caller_id,
            mode=mode,
            source_message_id=source_message_id,
        )
        try:
            call_config = (
                GroupCallConfig(auto_start=False)
                if call_ref is not None
                else CallConfig(timeout=60)
            )
            await call_client.record(
                caller_id,
                ogg_opus_record_stream(output),
                config=call_config,
            )
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            emit_event("recording_failed", caller_id=caller_id, mode=mode, error=failure)
            stop_event.set()
            return
        emit_event(
            "recording_started",
            connection=config["id"],
            caller_id=caller_id,
            mode=mode,
            output=str(output),
        )

    async def handle_conference_message(call_client: PyTgCalls, message) -> bool:
        action = getattr(message, "action", None)
        caller_id = getattr(message, "sender_id", None)
        if caller_id is None or (allowed_callers and caller_id not in allowed_callers):
            return False
        if isinstance(action, MessageActionConferenceCall) and action.active:
            await start_recording(
                call_client,
                caller_id,
                "conference",
                call_ref=InputGroupCallInviteMessage(message.id),
                source_message_id=message.id,
            )
            return True
        if isinstance(action, MessageActionInviteToGroupCall):
            await start_recording(
                call_client,
                caller_id,
                "conference",
                call_ref=action.call,
                source_message_id=message.id,
            )
            return True
        return False

    @client.on(events.NewMessage(incoming=True))
    async def conference_invite(event):
        await handle_conference_message(calls, event.message)

    @calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
    async def incoming_call(call_client: PyTgCalls, update: ChatUpdate):
        await start_recording(call_client, update.chat_id, "p2p")

    @calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
    async def call_closed(_client: PyTgCalls, update: ChatUpdate):
        nonlocal stop_reason
        if active is not None and update.chat_id == active["caller_id"]:
            stop_reason = "call_closed"
            stop_event.set()

    async def poll_conference_cache() -> None:
        bridge = getattr(getattr(calls, "_app", None), "_bind_client", None)
        cache = getattr(bridge, "_cache", None)
        if cache is None:
            return
        while not stop_event.is_set():
            if active is None:
                for caller_id in sorted(allowed_callers):
                    call_ref = await cache.get_input_call(caller_id)
                    if type(call_ref).__name__ not in {
                        "InputGroupCall",
                        "InputGroupCallInviteMessage",
                        "InputGroupCallSlug",
                    }:
                        continue
                    emit_event(
                        "conference_call_cached",
                        caller_id=caller_id,
                        call_ref_type=type(call_ref).__name__,
                    )
                    await start_recording(
                        calls,
                        caller_id,
                        "conference",
                        call_ref=call_ref,
                    )
                    break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RecorderError(2, "session_unauthorized",
                                f"Telegram session for {config['id']!r} is not authorized")
        me = await client.get_me()
        with contextlib.redirect_stdout(sys.stderr):
            await calls.start()
        emit_event(
            "listening",
            connection=config["id"],
            account=getattr(me, "username", None) or str(me.id),
            callers=sorted(allowed_callers),
        )
        conference_poll_task = asyncio.create_task(poll_conference_cache())
        if args.invite_link:
            if len(allowed_callers) != 1:
                raise RecorderError(
                    6,
                    "caller_required",
                    "--invite-link requires exactly one --caller user id",
                )
            caller_id = next(iter(allowed_callers))
            await start_recording(
                calls,
                caller_id,
                "conference-link",
                call_ref=InputGroupCallSlug(call_invite_slug(args.invite_link)),
            )
        for caller_id in sorted(allowed_callers):
            messages = await client.get_messages(caller_id, limit=30)
            for message in messages:
                age = time.time() - message.date.timestamp() if message.date else 0
                if age > 15 * 60:
                    break
                action = getattr(message, "action", None)
                if action is not None:
                    emit_event(
                        "service_message",
                        message_id=message.id,
                        sender_id=message.sender_id,
                        action=type(action).__name__,
                        active=getattr(action, "active", None),
                        missed=getattr(action, "missed", None),
                        video=getattr(action, "video", None),
                        duration=getattr(action, "duration", None),
                    )
                if await handle_conference_message(calls, message):
                    break
            if active is not None:
                break
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
        if active is None:
            return {"ok": True, "mode": "listen", "stopped": stop_reason, "recording": None}
        caller_id = active["caller_id"]
        with contextlib.suppress(NotInCallError, NoActiveGroupCall, RPCError):
            await calls.leave_call(caller_id)
        await asyncio.sleep(0.25)
        output = active["output"]
        result = {
            "ok": failure is None,
            "mode": "listen",
            "connection": config["id"],
            "caller_id": caller_id,
            "output": str(output),
            "bytes": output.stat().st_size if output.exists() else 0,
            "duration_seconds": round(time.time() - active["started_at"], 3),
            "stopped": stop_reason,
        }
        if failure:
            result["error"] = failure
        return result
    finally:
        if conference_poll_task is not None:
            conference_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await conference_poll_task
        if client.is_connected():
            await client.disconnect()


def main() -> None:
    args = parser().parse_args()
    selected_modes = sum(bool(value) for value in (args.listen, args.watch_groups))
    if selected_modes > 1 or (args.probe and selected_modes):
        die(RecorderError(6, "bad_arguments",
                          "--listen, --watch-groups, and --probe are mutually exclusive"))
    if not args.listen and not args.watch_groups and not args.chat:
        die(RecorderError(6, "chat_required",
                          "chat is required unless --listen or --watch-groups is used"))
    project_root = find_project_root(args.project_root)
    try:
        config = resolve_connection(project_root, args.connection)
        runtime_dir = STATE_HOME / "telegram" / config["id"] / "calls" / "runtime"
        runtime_session = (
            config["session"]
            if args.primary_session
            else hot_copy_session(config["session"], runtime_dir)
        )
        try:
            target = watch_groups if args.watch_groups else listen if args.listen else run
            emit(asyncio.run(target(args, config, runtime_session)))
        finally:
            if not args.primary_session:
                cleanup_session(runtime_session)
    except RecorderError as error:
        die(error)


if __name__ == "__main__":
    main()
