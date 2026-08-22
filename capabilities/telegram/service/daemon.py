#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "telethon==1.43.2",
#     "py-tgcalls==3.0.0.dev5",
#     "ntgcalls==3.0.0b16",
#     "google-genai>=1.36.0",
# ]
# ///
# The 3.x line is what carries conference calls: joining one needs
# CallConfig(conference=<invite message id>), which 2.x does not accept.
# ntgcalls is pinned explicitly because inbound audio arrives only from b15 on
# (ntgcalls#52). dev5 is the first release carrying the three defects this
# capability used to work around: pytgcalls/pytgcalls#334, #335 and #336.
"""
Telegram assistant daemon — the persistent MTProto process (push, not polling).

This engine is shipped in the installed telegram capability bundle. A project
keeps only its policy/config under capabilities/telegram/service/:
  settings.json — connection, direct_messages, allowed_users, allowed_groups,
                  defaults
  context.md    — soft-gate prompt injected into every worker turn
  voice-agent.md — system prompt for a direct call answered by voice

Runtime state follows the selected Telegram connection:
  <connection-state>/session.session          auth session
  <connection-state>/service/register.json    daemon register
  <connection-state>/service/progress/        progress outbox
  <connection-state>/service/worker-sessions/ hot session copies

Four gates, not to be conflated:
  1. door         — direct_messages mode in private chats; allowed_groups in groups,
                    only when the assistant is explicitly addressed                  (HARD)
  2. control authority — who may run service control commands like /set and /stop    (HARD)
  3. tool authority — what the worker (claude/codex) may call (later, via flags)     (HARD)
  4. soft-gates   — behavioural guidance in context.md                              (SOFT)
This file implements gates 1 and 2; context.md carries gate 4.

Telegram is the source of truth: the worker rebuilds context from the live tail
each turn. The register holds only what Telegram doesn't — the watermark and the
per-channel /set overrides.
"""
import asyncio
import contextlib
import fcntl
import hashlib
import html
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import telethon
from telethon import TelegramClient, events
from telethon.tl.functions.phone import GetGroupCallChainBlocksRequest
from telethon.tl.types import InputGroupCallInviteMessage, MessageActionConferenceCall, MessageActionInviteToGroupCall, MessageService, UpdateNewMessage

import pytgcalls
from pytgcalls import PyTgCalls, filters
from pytgcalls.types import (
    CallConfig,
    ChatUpdate,
    Device,
    Direction,
    ExternalMedia,
    MediaStream,
    RecordStream,
)
from pytgcalls.types.raw import AudioParameters

# Import shared call recording helpers (same directory)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from call_recording_helpers import (
    finalize_mp3_capture,
    iso_utc,
    send_recording_to_chat,
    write_metadata,
)
import voice_agent
from settings_schema import validate_settings
sys.path.pop(0)

logging.getLogger("telethon").setLevel(logging.CRITICAL)

HERE = Path(__file__).resolve().parent
WORKER_BIN = HERE / "worker-bin"
CALL_RECORDER_BIN = HERE / "call_recorder.py"
# How long a conference invite is given to grow the first block of its chain,
# and how often that chain is read while waiting.
CONFERENCE_CHAIN_TIMEOUT = 15.0
CONFERENCE_CHAIN_INTERVAL = 0.5
# How long a conference capture may stand still before the call counts as over,
# and how often its size is checked.
CAPTURE_STALL_TIMEOUT = 8.0
CAPTURE_STALL_INTERVAL = 1.0
# When to re-ask who is in a conference, as seconds after the join.
CONFERENCE_AUDIO_MAP_RETRIES = (2.0, 3.0, 5.0, 10.0)
CONFERENCE_PEER_INTERVAL = 2.0
# Silence is not absence. A call can be quiet for minutes and must not be cut
# short for it, so the absence of incoming audio is only the cheap hint that
# makes it worth asking Telegram who is still in the call; a conference with
# people talking is never questioned at all. Two empty answers are required,
# because one failed round-trip must not end a live recording.
CONFERENCE_QUIET_BEFORE_CHECK = 20.0
CONFERENCE_JOIN_GRACE = 30.0
# A conference is not always finished when its invite arrives. One whose chain
# answers within this is joined straight away; one that takes longer is held
# until its newest block has stood unchanged for the settle time and joined
# then, which is worth the wait: a chain that first answered after 2.9s grew
# another block while being held and then recorded cleanly. A chain still
# producing new blocks when the budget runs out is declined, because no join
# into one of those has yet been seen to work.
CONFERENCE_CHAIN_READY_LIMIT = 2.0
CONFERENCE_CHAIN_SETTLE_STEP = 2.0
CONFERENCE_CHAIN_SETTLE_MIN = 4.0
CONFERENCE_CHAIN_SETTLE_BUDGET = 12.0
CONFERENCE_EMPTY_ANSWERS = 2
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
CRED_FILE = CONFIG_HOME / "telegram" / "credentials.env"
USER_CONN_FILE = CONFIG_HOME / "telegram" / "connections.json"
DEFAULT_SESSION = STATE_HOME / "telegram" / "session"
if (not DEFAULT_SESSION.with_suffix(".session").exists()
        and (CONFIG_HOME / "telegram" / "session.session").exists()):
    DEFAULT_SESSION = CONFIG_HOME / "telegram" / "session"
WORKER_CHOICES = ("claude", "codex", "stub")
WORKER_NAMES = set(WORKER_CHOICES)
CLAUDE_EFFORT_CHOICES = ("default", "low", "medium", "high", "xhigh", "max")
CLAUDE_EFFORTS = set(CLAUDE_EFFORT_CHOICES)
CODEX_REASONING_CHOICES = ("default", "low", "medium", "high", "xhigh")
CODEX_REASONING_EFFORTS = set(CODEX_REASONING_CHOICES)
CODEX_SERVICE_TIER_CHOICES = ("default", "fast", "priority")
CODEX_SERVICE_TIERS = set(CODEX_SERVICE_TIER_CHOICES)
TELEGRAM_MESSAGE_LIMIT = 4096


class SettingsError(ValueError):
    """A settings/layout value that cannot safely become live configuration."""


def _find_project_root():
    start = (os.environ.get("TELEGRAM_SERVICE_PROJECT_ROOT")
             or os.environ.get("CLAUDE_PROJECT_DIR")
             or os.getcwd())
    here = Path(start).expanduser().resolve()
    home = Path.home().resolve()
    for d in (here, *here.parents):
        if d == home:
            break
        if ((d / "capabilities" / "settings.json").is_file()
                or (d / ".capabilities").is_dir()
                or (d / ".env").exists()
                or (d / ".env.local").exists() or (d / ".git").is_dir()):
            return d
    return here


PROJECT_ROOT = _find_project_root()


def _validated_project_envelope(value, provider):
    raw = str(value or "").strip()
    if not raw:
        raise SettingsError(f"{provider} returned an empty project envelope")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise SettingsError(f"{provider} returned a relative project envelope: {raw!r}")
    try:
        resolved = candidate.resolve()
        resolved.relative_to(PROJECT_ROOT.resolve())
    except (OSError, ValueError) as exc:
        raise SettingsError(
            f"{provider} returned a project envelope outside {PROJECT_ROOT}: {candidate}") from exc
    return resolved


def _project_capabilities_dir():
    """Consume the launcher's resolved envelope, or ask the manager directly."""
    supplied = (os.environ.get("TELEGRAM_SERVICE_PROJECT_ENVELOPE")
                or os.environ.get("CAPABILITIES_PROJECT_ENVELOPE"))
    if supplied:
        return _validated_project_envelope(supplied, "service launcher")
    source_manager = Path(__file__).resolve().parents[3] / "bin" / "capabilities"
    manager = (os.environ.get("CAPABILITIES_MANAGER_BIN")
               or (str(source_manager) if source_manager.is_file() else "capabilities"))
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(PROJECT_ROOT)
    try:
        proc = subprocess.run(
            [manager, "path", "--json"], cwd=PROJECT_ROOT, env=env,
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SettingsError(f"could not resolve the project envelope through capabilities: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SettingsError(
            f"capabilities path failed with exit {proc.returncode}: {detail}")
    try:
        answer = json.loads(proc.stdout)
    except (TypeError, ValueError) as exc:
        raise SettingsError("capabilities path returned malformed JSON") from exc
    if not isinstance(answer, dict):
        raise SettingsError("capabilities path returned a non-object answer")
    manager_root = Path(str(answer.get("project_root") or ""))
    if not manager_root.is_absolute() or manager_root.resolve() != PROJECT_ROOT.resolve():
        raise SettingsError("capabilities path resolved a different project root")
    return _validated_project_envelope(
        answer.get("project_envelope"), answer.get("provider") or "capabilities manager")


PROJECT_CAPABILITIES_DIR = _project_capabilities_dir()
SERVICE_DIR = PROJECT_CAPABILITIES_DIR / "telegram" / "service"
SETTINGS_FILE = Path(os.environ.get("TELEGRAM_SERVICE_SETTINGS")
                     or SERVICE_DIR / "settings.json")
CONTEXT_FILE = Path(os.environ.get("TELEGRAM_SERVICE_CONTEXT")
                    or SERVICE_DIR / "context.md")


_RECORDS = None
_RECORDS_MODULE = None


def _records_module():
    global _RECORDS_MODULE
    if _RECORDS_MODULE is None:
        path = Path(__file__).with_name("store.py")
        spec = importlib.util.spec_from_file_location("telegram_service_store", path)
        if spec is None or spec.loader is None:
            raise SettingsError(f"cannot load records adapter from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RECORDS_MODULE = module
    return _RECORDS_MODULE


def _records():
    """The same adapter the CLIs read through, opened once here as well."""
    global _RECORDS
    if _RECORDS is None:
        _store = _records_module()
        _RECORDS = _store.open_records(PROJECT_CAPABILITIES_DIR, CONFIG_HOME)
    return _RECORDS


def _read_project_layout(reload_file=False):
    """Validate the launcher-owned absolute project layout snapshot."""
    raw = os.environ.get("TELEGRAM_SERVICE_PROJECT_LAYOUT", "").strip()
    layout_file = globals().get("PROJECT_LAYOUT_FILE")
    if reload_file and layout_file is not None and layout_file.is_file():
        try:
            raw = layout_file.read_text()
        except OSError as exc:
            raise SettingsError(f"cannot read project layout snapshot: {exc}") from exc
    if not raw:
        # Direct daemon use remains ContextKit-independent, with only the manager-
        # resolved envelope exposed. The service launcher supplies richer layers.
        return {
            "project_root": str(PROJECT_ROOT.resolve()),
            "capabilities": str(PROJECT_CAPABILITIES_DIR.resolve()),
            "provider": "minimal",
        }
    try:
        layout = json.loads(raw)
    except ValueError as exc:
        raise SettingsError(f"project layout is not valid JSON: {exc}") from exc
    if not isinstance(layout, dict):
        raise SettingsError("project layout must be a JSON object")
    allowed = {
        "project_root", "capabilities", "context", "routines", "assets",
        "memory", "deployment", "provider",
    }
    unknown = set(layout) - allowed
    if unknown:
        key = sorted(unknown)[0]
        raise SettingsError(f"project_layout.{key}: unsupported property {key!r}")
    declared_root = Path(str(layout.get("project_root") or ""))
    if not declared_root.is_absolute() or declared_root.resolve() != PROJECT_ROOT.resolve():
        raise SettingsError("project_layout.project_root does not match the daemon project")
    result = {"project_root": str(PROJECT_ROOT.resolve()),
              "provider": str(layout.get("provider") or "unknown")}
    for key in ("capabilities", "context", "routines", "assets", "memory", "deployment"):
        value = layout.get(key)
        if value is None:
            continue
        candidate = Path(str(value)).expanduser()
        if not candidate.is_absolute():
            raise SettingsError(f"project_layout.{key} must be absolute")
        try:
            resolved = candidate.resolve()
            resolved.relative_to(PROJECT_ROOT.resolve())
        except (OSError, ValueError) as exc:
            raise SettingsError(f"project_layout.{key} is outside the project") from exc
        result[key] = str(resolved)
    if Path(result.get("capabilities", "")).resolve() != PROJECT_CAPABILITIES_DIR.resolve():
        raise SettingsError("project_layout.capabilities does not match the resolved envelope")
    return result

# Per-instance channel toggle. Default on (opt-out) — set falsey on instances
# that should not consume the Telegram channel.
CHANNEL_ENABLED = os.environ.get(
    "TELEGRAM_SERVICE_ENABLED",
    os.environ.get("TELEGRAM_CHANNEL_ENABLED", "true"),
).strip().lower() \
    in ("1", "true", "yes", "on")


def _read_settings():
    try:
        settings = {key: row["value"] for key, row in
                    _records().resolve("telegram", "setting").items()
                    if key != "connection.default"}
    except _records_module().StoreError as exc:
        raise SettingsError(
            f"Telegram service records are unavailable: {exc.message}") from exc
    if not settings:
        raise SettingsError(
            f"Telegram service settings not found in {_records().source}")
    if not isinstance(settings, dict):
        raise SettingsError(f"{SETTINGS_FILE} must contain a JSON object")
    try:
        validate_settings(settings, PROJECT_ROOT, SERVICE_DIR)
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc
    return settings


def _load_settings():
    try:
        return _read_settings()
    except SettingsError as exc:
        sys.exit(str(exc))


def _runtime_settings(settings, project_layout=None):
    """Validate and derive every setting that may be replaced while live.

    Nothing in this function mutates module state. Reload first builds this
    complete snapshot, then publishes it, so malformed JSON never leaves a
    half-old, half-new policy behind.
    """
    allowed = settings.get("allowed_users", {})
    allowed_groups = settings.get("allowed_groups", {})
    direct_messages = settings.get("direct_messages", {})
    defaults = settings.get("defaults", {})
    direct_mode = str(direct_messages.get("mode") or "allowed_users").strip().lower()
    voice_defaults = (defaults.get("voice_agent")
                      if isinstance(defaults.get("voice_agent"), dict) else {})
    try:
        voice_progress_interval = float(
            voice_defaults.get("progress_interval")
            or voice_agent.DEFAULT_PROGRESS_INTERVAL)
    except (TypeError, ValueError) as exc:
        raise SettingsError("settings.defaults.voice_agent.progress_interval must be a number") from exc
    if voice_progress_interval <= 0:
        raise SettingsError("settings.defaults.voice_agent.progress_interval must be positive")
    voice_timezone, voice_timezone_name = voice_agent.resolve_timezone(
        voice_defaults.get("timezone"))
    voice_prompt_file = str(
        os.environ.get("TELEGRAM_SERVICE_VOICE_CONTEXT")
        or voice_defaults.get("prompt_file")
        or "voice-agent.md")
    voice_context_file = Path(voice_prompt_file)
    if not voice_context_file.is_absolute():
        voice_context_file = SERVICE_DIR / voice_context_file
    assistant_name = str(
        settings.get("assistant_name") or defaults.get("assistant_name") or "Assistant")
    default_worker = str(
        os.environ.get("TELEGRAM_SERVICE_WORKER")
        or os.environ.get("TG_WORKER")
        or defaults.get("worker")
        or "stub"
    ).strip().lower()
    if default_worker not in WORKER_NAMES:
        default_worker = "stub"

    def positive_seconds(name, default, minimum=0.01):
        try:
            return max(minimum, float(defaults.get(name, default)))
        except (TypeError, ValueError):
            return float(default)

    sync_interval = positive_seconds("sync_interval", 20)
    aliases = defaults.get("group_aliases")
    if aliases is None:
        group_aliases = (assistant_name,)
    elif isinstance(aliases, (list, tuple)):
        group_aliases = tuple(str(alias) for alias in aliases if str(alias).strip())
    else:
        raise SettingsError("settings.defaults.group_aliases must be a JSON array")

    return {
        "SETTINGS": settings,
        "ALLOWED": allowed,
        "ALLOWED_GROUPS": allowed_groups,
        "DIRECT_MESSAGES": direct_messages,
        "DIRECT_MESSAGE_MODE": direct_mode,
        "ALLOW_ANY_DIRECT": direct_mode in ("anyone", "all", "open", "public"),
        "DIRECT_DEFAULT_ROLE": direct_messages.get("default_role") or "direct_user",
        "DEFAULTS": defaults,
        "VOICE_AGENT_DEFAULTS": voice_defaults,
        "VOICE_TIMEZONE": voice_timezone,
        "VOICE_TIMEZONE_NAME": voice_timezone_name,
        "VOICE_RECORDING_CAPTION": str(voice_defaults.get("recording_caption") or "").strip(),
        "VOICE_PROGRESS_INTERVAL": voice_progress_interval,
        "VOICE_CONTEXT_FILE": voice_context_file,
        "ASSISTANT_NAME": assistant_name,
        "DEFAULT_GROUP_ALIASES": group_aliases or (assistant_name,),
        "DEFAULT_WORKER": default_worker,
        "SYNC_INTERVAL": sync_interval,
        "SYNC_STALE_AFTER": max(
            sync_interval * 2, positive_seconds("sync_stale_after", 60)),
        "PROJECT_LAYOUT": project_layout if project_layout is not None else PROJECT_LAYOUT,
    }


def _publish_runtime_settings(runtime):
    globals().update(runtime)


# --- static policy (project) --------------------------------------------------
SETTINGS = _load_settings()
PROJECT_LAYOUT = _read_project_layout()
_publish_runtime_settings(_runtime_settings(SETTINGS, PROJECT_LAYOUT))
SETTINGS_GENERATION = 1
SETTINGS_RELOAD_ATTEMPTS = 0
CONTROL_DEFAULTS = {
    "roles": {
        "supervisor": {"commands": ["status", "set", "reload", "stop", "help"]},
        "channel_admin": {"commands": ["status", "set", "help"]},
        "direct_user": {"commands": ["status", "help"]},
        "group_member": {"commands": ["status", "help"]},
    }
}


# --- config/state resolution --------------------------------------------------
def _parse_env_file(path):
    out = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        out[key] = value.strip().strip('"').strip("'")
    return out


def _project_env_value(key):
    for path in (PROJECT_ROOT / ".env.local", PROJECT_ROOT / ".env"):
        value = _parse_env_file(path).get(key)
        if value:
            return value
    return os.environ.get(key)


def _env_value(key):
    for path in (PROJECT_ROOT / ".env.local", PROJECT_ROOT / ".env"):
        value = _parse_env_file(path).get(key)
        if value:
            return value
    value = _parse_env_file(CRED_FILE).get(key)
    if value:
        return value
    return os.environ.get(key)


def _project_path(value):
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _config_value(*keys):
    for key in keys:
        value = _project_env_value(key)
        if value:
            return value
    return None


def _connection_file_override():
    value = _config_value(
        "TELEGRAM_SERVICE_CONNECTIONS_FILE",
        "TG_CONNECTIONS_FILE",
        "TELEGRAM_CONNECTIONS_FILE",
    )
    return _project_path(value) if value else None


def _connections_envelope():
    override = _connection_file_override()
    candidates = [override] if override else [
        PROJECT_CAPABILITIES_DIR / "telegram" / "connections.json",
        USER_CONN_FILE,
    ]
    for path in candidates:
        if path is None:
            continue
        if not path.exists():
            if override:
                sys.exit(f"Telegram connections envelope not found: {path}")
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            sys.exit(f"{path} is not valid JSON: {e}")
        except OSError as e:
            sys.exit(f"cannot read {path}: {e}")
        if not isinstance(data.get("connections"), dict) or not data["connections"]:
            sys.exit(f"{path} is not a valid Telegram connections envelope")
        return data, path
    return None, None


def _select_connection(data):
    wanted = (
        _config_value("TELEGRAM_SERVICE_CONNECTION", "TG_CONNECTION", "TELEGRAM_CONNECTION")
        or SETTINGS.get("connection")
    )
    if data is None:
        if wanted and wanted != "default":
            sys.exit(
                f"no Telegram connections registry; requested connection {wanted!r}")
        return "default", None
    conns = data["connections"]
    wanted = wanted or data.get("default")
    if wanted:
        if wanted not in conns:
            sys.exit(f"Telegram connection {wanted!r} not found in {CONN_FILE}")
        return wanted, conns[wanted]
    if len(conns) == 1:
        cid = next(iter(conns))
        return cid, conns[cid]
    sys.exit(
        f"{CONN_FILE} defines {len(conns)} Telegram connections and no default; "
        "set TELEGRAM_SERVICE_CONNECTION, TG_CONNECTION, or TELEGRAM_CONNECTION"
    )


CONNECTIONS, CONN_FILE = _connections_envelope()
CONNECTION, CONNECTION_ENTRY = _select_connection(CONNECTIONS)
try:
    EXPECTED_ACCOUNT_ID = int((CONNECTION_ENTRY or {}).get("expected_account_id"))
    if EXPECTED_ACCOUNT_ID <= 0:
        raise ValueError
except (TypeError, ValueError):
    EXPECTED_ACCOUNT_ID = None
if not bool((CONNECTION_ENTRY or {}).get("allow_write", False)):
    sys.exit(
        f"Telegram connection {CONNECTION!r} does not allow writes; "
        "set allow_write: true in connections.json before running the assistant service"
    )


def _session_stem(cid, conn):
    if conn is None:
        return _project_path(_env_value("TELEGRAM_SESSION")) if _env_value("TELEGRAM_SESSION") else DEFAULT_SESSION
    if conn.get("session"):
        return _project_path(conn["session"])
    return STATE_HOME / "telegram" / cid / "session"


SESSION = _session_stem(CONNECTION, CONNECTION_ENTRY)
CONNECTION_STATE_DIR = SESSION.parent


def _service_state_dir():
    value = _config_value("TELEGRAM_SERVICE_STATE_DIR")
    return _project_path(value) if value else CONNECTION_STATE_DIR / "service"


SERVICE_STATE_DIR = _service_state_dir()
REGISTER = SERVICE_STATE_DIR / "register.json"
EXPECTED_CONTROL_DIR = STATE_HOME / "telegram" / str(EXPECTED_ACCOUNT_ID) / "control"
CONTROL_DIR = Path(os.environ.get("TELEGRAM_ACCOUNT_CONTROL_DIR") or EXPECTED_CONTROL_DIR).expanduser()
if CONTROL_DIR.resolve() != EXPECTED_CONTROL_DIR.resolve():
    sys.exit(f"account control dir mismatch: expected {EXPECTED_CONTROL_DIR}, got {CONTROL_DIR}")
LOCK_FILE = CONTROL_DIR / "daemon.lock"
OWNER_FILE = CONTROL_DIR / "owner.json"
OWNERSHIP_FILE = CONTROL_DIR / "ownership-v1.json"
PID_FILE = SERVICE_STATE_DIR / "daemon.pid"
LOG_FILE = SERVICE_STATE_DIR / "daemon.log"
HEALTH_FILE = SERVICE_STATE_DIR / "health.json"
PROGRESS_DIR = SERVICE_STATE_DIR / "progress"
WORKER_SESSION_DIR = SERVICE_STATE_DIR / "worker-sessions"
# The worker session each caller is carrying, by caller. Deliberately not under
# worker-sessions/, which holds copies of the Telegram account session — a
# different thing entirely, and one that is auth material where this is a bare
# id pointing at a codex rollout.
LANES_FILE = SERVICE_STATE_DIR / "lanes.json"
AUTHORITY_DIR = SERVICE_STATE_DIR / "authority"
CALL_RECORDING_REQUEST_DIR = SERVICE_STATE_DIR / "call-recording-requests"
PROJECT_LAYOUT_FILE = SERVICE_STATE_DIR / "project-layout.json"
STATE_SCHEMA_FILE = SERVICE_STATE_DIR / "state-schema.json"
SERVICE_STATE_VERSION = 1
LAUNCH_NONCE = os.environ.get("TELEGRAM_SERVICE_LAUNCH_NONCE") or ""
SERVICE_MODE = os.environ.get("TELEGRAM_SERVICE_MODE") or "canonical"
DEV_SESSION_ID = os.environ.get("TELEGRAM_SERVICE_DEV_SESSION") or None
_CLI_BUNDLE_RAW = os.environ.get("TELEGRAM_REAL_TELEGRAM") or ""
CLI_BUNDLE_PATH = Path(_CLI_BUNDLE_RAW).expanduser() if _CLI_BUNDLE_RAW else None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


class _TelethonWarnings(logging.Handler):
    """Surface what Telethon retries over.

    Telethon retries a request through server errors and reports only the
    attempt count once it gives up, so the error class that actually came back
    lives in a warning nothing was reading."""

    def emit(self, record):
        with contextlib.suppress(Exception):
            log(f"telethon: {record.getMessage()}")


_telethon_log = logging.getLogger("telethon")
_telethon_log.setLevel(logging.WARNING)
_telethon_log.addHandler(_TelethonWarnings())


# The ssrc of every incoming audio channel the media stack currently holds.
# Nothing else surfaces this: a conference reports no disconnect when the other
# side leaves, because being alone in a group call is a legal state. The one
# thing that changes is that its audio channel is removed, and the media stack
# says so out loud.
MEDIA_AUDIO_PEERS: set[int] = set()
_AUDIO_CHANNEL_SSRC = re.compile(
    r"(?P<verb>Adding|Removing) incoming audio channel with ssrc (?P<ssrc>\d+)")


def _track_audio_peer(message):
    """Follow the incoming audio channels named in one media-stack line.

    Derived from the log because the transport offers no other view of it: the
    participants API refuses a conference migrated from a p2p call, whose chat
    id is a positive user id, and the frames themselves never pass through this
    process — ntgcalls writes them straight into its own encoder.

    Fed only while the `ntgcalls` logger is low enough to carry channel lines,
    which it is not by default. With nothing feeding it the set stays empty, and
    both readers treat empty as "ask Telegram" rather than as "nobody is here",
    so the answer stays correct and only costs a round-trip.
    """
    found = _AUDIO_CHANNEL_SSRC.search(message)
    if not found:
        return
    ssrc = int(found.group("ssrc"))
    if found.group("verb") == "Adding":
        MEDIA_AUDIO_PEERS.add(ssrc)
    else:
        MEDIA_AUDIO_PEERS.discard(ssrc)


class _MediaStackLog(logging.Handler):
    """Surface what the media stack says about a call.

    Importing pytgcalls raises the `ntgcalls` logger from NOTSET to CRITICAL,
    so the one layer that knows why a call dropped is silent by default. That
    is why a conference which evicts this account leaves nothing behind: the
    Telegram-side updates simply stop, no exception is raised, and the
    transport never gets to say what it saw.

    Consecutive duplicates are collapsed rather than rate-limited. The native
    layer repeats one condition instead of emitting many distinct ones, and a
    cap that drops by volume would eventually drop the single line worth
    having.
    """

    def __init__(self):
        super().__init__()
        self._last = None
        self._repeats = 0

    def emit(self, record):
        with contextlib.suppress(Exception):
            message = record.getMessage()
            if message == self._last:
                self._repeats += 1
                return
            if self._repeats:
                log(f"call-media: previous line repeated {self._repeats}x")
                self._repeats = 0
            self._last = message
            _track_audio_peer(message)
            log(f"call-media[{record.levelname.lower()}]: {message}")


# The level the import leaves behind is the level that stands. Importing
# pytgcalls raises this logger from NOTSET to CRITICAL, and claiming it back at
# INFO — which is what made the 2026-08-19/21 call failures readable at all —
# puts the media stack's own log thread into the interpreter on every line it
# emits. On 2026-08-22 that thread was one of three sitting in
# PyEval_AcquireThread while a Python thread held the GIL inside a blocking
# ntgcalls call, and the daemon was wedged for seventeen minutes. The deadlock
# is in the binding and not in this handler, but this is the one side of it
# under local control, so the level is left alone and the question of whether
# that is enough is answered by whether conferences stop wedging.
#
# The handler stays attached: anything the stack considers critical still gets
# through, and it costs nothing when nothing is emitted. Raise the level here to
# read the media stack again, knowing what it widens.
_media_log = logging.getLogger("ntgcalls")
_media_log.addHandler(_MediaStackLog())


async def settle_conference_chain(invite_msg_id, block, waited, read_block):
    """Hold a conference that is still being built until its chain stops.

    Nothing is re-sent and nothing is answered again: the caller stays invited,
    their phone goes on showing the call, and the join happens a few seconds
    later on a block that has had time to stand.

    Growth is what the budget is spent on. A chain still issuing new blocks is
    one still admitting people, and no join into one of those has been seen to
    work, so a chain that keeps changing until the budget is gone is declined.
    """
    digest = hashlib.sha256(block).hexdigest()[:12]
    log(f"call: conference chain for invite {invite_msg_id} was still being "
        f"built — first block {digest} after {waited:.1f}s; letting it settle "
        f"instead of joining now")
    deadline = time.monotonic() + CONFERENCE_CHAIN_SETTLE_BUDGET
    seen_at = time.monotonic()
    while True:
        age = time.monotonic() - seen_at
        pause = min(CONFERENCE_CHAIN_SETTLE_STEP,
                    CONFERENCE_CHAIN_SETTLE_MIN - age)
        if pause <= 0:
            log(f"call: conference chain for invite {invite_msg_id} settled — "
                f"block {digest} unchanged for {age:.1f}s, joining")
            return block
        if time.monotonic() + pause > deadline:
            raise RuntimeError(
                f"conference chain for invite {invite_msg_id} was still growing "
                f"after {CONFERENCE_CHAIN_SETTLE_BUDGET:.0f}s of waiting, so "
                f"this invite is declined")
        await asyncio.sleep(pause)
        fresh = await read_block(invite_msg_id)
        if not fresh:
            raise RuntimeError(
                f"conference chain for invite {invite_msg_id} went empty while "
                f"settling")
        fresh_digest = hashlib.sha256(fresh).hexdigest()[:12]
        if fresh_digest != digest:
            log(f"call: conference chain for invite {invite_msg_id} still "
                f"growing — block {digest} became {fresh_digest} after "
                f"{time.monotonic() - seen_at:.1f}s")
            block, digest = fresh, fresh_digest
            seen_at = time.monotonic()


def reconcile_orphaned_recordings():
    """Settle recordings a previous process left open.

    A call recording is closed by the process that opened it, so one that ends
    with the process — a restart, a crash, the stuck conference this was written
    for — keeps saying `recording` for as long as its metadata survives. Four
    such rows had accumulated by 2026-08-21, and each one makes `stop_reason`
    less trustworthy as evidence about how calls actually end.

    The audio is left exactly where it is. Nothing is delivered: an abandoned
    capture is mostly the silence that followed the call, and guessing otherwise
    would put an hour of it into a chat.
    """
    folder = CONNECTION_STATE_DIR / "calls" / "recordings"
    settled = 0
    for path in sorted(folder.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("status") not in ("joining", "recording"):
            continue
        record["status"] = "interrupted"
        record["stop_reason"] = "process_ended"
        record.setdefault("recording_ended_at", None)
        delivery = record.get("delivery")
        if isinstance(delivery, dict) and delivery.get("status") == "pending":
            delivery["status"] = "skipped"
            delivery["error"] = "interrupted"
        try:
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            settled += 1
        except OSError:
            continue
    if settled:
        log(f"calls: settled {settled} recording(s) left open by a previous process")


def write_health(state=None, **updates):
    """Atomically publish update-stream liveness for `telegram service status`."""
    health = {}
    if state != "starting":
        try:
            current = json.loads(HEALTH_FILE.read_text())
            if isinstance(current, dict):
                health.update(current)
        except (OSError, ValueError):
            pass
    health.update({
        "connection": CONNECTION,
        "pid": os.getpid(),
        "updated_at": now(),
        "sync_interval_seconds": SYNC_INTERVAL,
        "stale_after_seconds": SYNC_STALE_AFTER,
        "telethon_version": getattr(telethon, "__version__", None),
        **_owner_provenance(),
        **updates,
    })
    if state is not None:
        health["state"] = state
    SERVICE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = HEALTH_FILE.with_name(f".{HEALTH_FILE.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(health, indent=2, ensure_ascii=False) + "\n")
    os.replace(temp, HEALTH_FILE)


def _atomic_json(path, value):
    """Atomically replace one continuity or ownership JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False,
                                    sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _file_sha256(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _owner_provenance():
    """Describe the actual launched bundle and state without secret values."""
    return {
        "schema": "telegram.daemon-owner.v1",
        "launch_nonce": LAUNCH_NONCE,
        "pid": os.getpid(),
        "daemon_path": str(Path(__file__).resolve()),
        "cli_bundle_path": str(CLI_BUNDLE_PATH.resolve()) if CLI_BUNDLE_PATH else None,
        "payload_sha256": _file_sha256(Path(__file__).resolve()),
        "mode": SERVICE_MODE,
        "dev_session_id": DEV_SESSION_ID,
        "project_root": str(PROJECT_ROOT.resolve()),
        "connection": CONNECTION,
        "account_id": EXPECTED_ACCOUNT_ID,
        "auth_session_path": str(SESSION.resolve()),
        "service_state_path": str(SERVICE_STATE_DIR.resolve()),
        "health_file": str(HEALTH_FILE.resolve()),
    }


def _require_hardened_state():
    """Validate cutover and exact state version before opening Telegram."""
    marker = None
    try:
        marker = json.loads(OWNERSHIP_FILE.read_text())
    except (OSError, ValueError):
        pass
    if (not isinstance(marker, dict)
            or marker.get("schema") != "telegram.ownership.v1"
            or marker.get("account_id") != EXPECTED_ACCOUNT_ID
            or marker.get("protocol_version") != 1):
        sys.exit(f"ownership migration required: {OWNERSHIP_FILE}")
    state_marker = None
    try:
        state_marker = json.loads(STATE_SCHEMA_FILE.read_text())
    except FileNotFoundError:
        _atomic_json(STATE_SCHEMA_FILE, {
            "schema": "telegram.service-state.v1", "version": 1,
            "adopted_at": now(),
        })
        state_marker = {"schema": "telegram.service-state.v1", "version": 1}
    except (OSError, ValueError):
        pass
    if (not isinstance(state_marker, dict)
            or state_marker.get("schema") != "telegram.service-state.v1"
            or state_marker.get("version") != SERVICE_STATE_VERSION):
        sys.exit(f"service state version mismatch: {STATE_SCHEMA_FILE}")
    if not LAUNCH_NONCE:
        sys.exit("service launch nonce is required")


def reload_runtime_settings():
    """Replace live policy from settings.json without disconnecting Telegram.

    The connection owns the Telethon session and state directories, so changing
    it cannot be a reload: accepting that would report one connection while the
    existing socket and files still belong to another. All other derived values
    are validated before any live global is replaced.
    """
    global SETTINGS_GENERATION, SETTINGS_RELOAD_ATTEMPTS
    SETTINGS_RELOAD_ATTEMPTS += 1
    attempted_at = now()
    try:
        candidate = _read_settings()
        if candidate.get("connection") != SETTINGS.get("connection"):
            raise SettingsError("settings.connection changed; a daemon restart is required")
        project_layout = _read_project_layout(reload_file=True)
        runtime = _runtime_settings(candidate, project_layout)
    except (SettingsError, ValueError) as exc:
        error = str(exc)
        write_health(
            settings_reload_attempts=SETTINGS_RELOAD_ATTEMPTS,
            settings_reload_attempted_at=attempted_at,
            settings_reload_error=error,
        )
        log(f"settings reload refused: {error}")
        return {
            "ok": False,
            "status": "reload_refused",
            "attempt": SETTINGS_RELOAD_ATTEMPTS,
            "generation": SETTINGS_GENERATION,
            "error": error,
            "instruction": "The settings were not changed. Tell the caller why.",
        }
    _publish_runtime_settings(runtime)
    SETTINGS_GENERATION += 1
    routes = _route_map()
    write_health(
        settings_reload_attempts=SETTINGS_RELOAD_ATTEMPTS,
        settings_reload_attempted_at=attempted_at,
        settings_reload_error=None,
        settings_generation=SETTINGS_GENERATION,
        settings_reloaded_at=attempted_at,
        sync_interval_seconds=SYNC_INTERVAL,
        stale_after_seconds=SYNC_STALE_AFTER,
        routes=routes,
    )
    log(f"settings reloaded: generation={SETTINGS_GENERATION} "
        f"attempt={SETTINGS_RELOAD_ATTEMPTS} routes={len(routes)}")
    return {
        "ok": True,
        "status": "reloaded",
        "attempt": SETTINGS_RELOAD_ATTEMPTS,
        "generation": SETTINGS_GENERATION,
        "reloaded_at": attempted_at,
        "instruction": "Say briefly that the settings are applied. The call stayed connected.",
    }


def _short_error(exc, limit=220):
    text = str(exc).replace("\n", " ")
    marker = "Remaining bytes:"
    if marker in text:
        text = text.split(marker, 1)[0] + marker + " <truncated>"
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return f"{type(exc).__name__}: {text}"


def _is_tl_layer_error(exc):
    text = str(exc)
    return type(exc).__name__ == "TypeNotFoundError" or "matching Constructor ID" in text


def acquire_daemon_lock():
    CONTROL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONTROL_DIR.chmod(0o700)
    handle = LOCK_FILE.open("a+")
    LOCK_FILE.chmod(0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"telegram daemon already running for {CONNECTION}; lock: {LOCK_FILE}")
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


# --- credentials (resolve the connection the way the capability does) ---------
def resolve_creds():
    if CONNECTION_ENTRY is None:
        api_id_raw = _env_value("TELEGRAM_API_ID")
        secret_env = "TELEGRAM_API_HASH"
    else:
        api_id_raw = CONNECTION_ENTRY.get("api_id")
        secret_env = CONNECTION_ENTRY.get("secret_env") or "TELEGRAM_API_HASH"
    if not api_id_raw:
        sys.exit("TELEGRAM_API_ID not resolved (project .env.local, credentials.env, or env)")
    api_id = int(api_id_raw)
    api_hash = _env_value(secret_env)
    if not api_hash:
        sys.exit(f"{secret_env} not resolved (project .env.local, credentials.env, or env)")
    return api_id, api_hash


GEMINI_SECRET_ENV = ((CONNECTION_ENTRY or {}).get("gemini_secret_env")
                     or "GOOGLE_API_KEY")


NAME = "telegram"


def _document_key(path):
    """The document key a service file corresponds to, by the one convention
    the import follows: `context.md` is `context`, `context/<x>.md` is
    `context.<x>`, and anything else is its own stem."""
    try:
        rel = Path(path).resolve().relative_to(SERVICE_DIR.resolve())
    except (OSError, ValueError):
        rel = Path(path)
    stem = rel.stem.replace("_", "-").lower()
    if rel.parent.name == "context":
        return f"context.{stem}"
    return stem


def read_service_document(path):
    """Prose the daemon serves at request time — the soft-gate context, a
    channel's own context, the voice prompt.

    The adapter answers from wherever this project keeps its records, and the
    daemon is not told which. Nothing falls back between them: a project on the
    store that cannot reach it raises rather than serving an empty prompt, and
    the dispatch loop turns that into a failed job the requester hears about.
    A prompt the daemon could not read is not a prompt that is empty."""
    doc = _service_document(path)
    return (doc["body"] if doc else "").strip()


def _service_document(path):
    """One document as the records surface holds it, or None where the project
    keeps no such document."""
    return _records().document_read(NAME, _document_key(path))


def read_voice_context():
    """The voice channel's own system prompt, owned by the project."""
    return read_service_document(VOICE_CONTEXT_FILE)


def voice_call_readiness():
    """What answering a call by voice needs, resolved when the phone rings: the
    Gemini key the connection names, and the project's own voice prompt. Neither
    is substituted — missing either, the call falls through to the recording
    path, so a caller with call_recording on is still recorded."""
    api_key = _env_value(GEMINI_SECRET_ENV)
    if not api_key:
        return None, None, f"{GEMINI_SECRET_ENV} not resolved"
    voice_context = read_voice_context()
    if not voice_context:
        return None, None, f"no voice prompt at {VOICE_CONTEXT_FILE}"
    return api_key, voice_context, None


# --- access policy ------------------------------------------------------------
def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _as_mapping(value):
    return value if isinstance(value, dict) else {}


def _deep_merge(base, overlay):
    out = dict(base or {})
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _call_recording_mode(group_policy):
    policy = _as_mapping(_as_mapping(group_policy).get("call_recording"))
    value = str(policy.get("mode") or "disabled").strip().lower().replace("-", "_")
    aliases = {
        "off": "disabled",
        "none": "disabled",
        "automatic": "auto",
        "request": "on_request",
        "command": "on_request",
        "manual": "on_request",
    }
    return aliases.get(value, value) if aliases.get(value, value) in {
        "disabled", "auto", "on_request"
    } else "disabled"


def configured_call_recording_groups():
    groups = {"auto": [], "on_request": [], "send_to_chat": []}
    for key, raw_policy in ALLOWED_GROUPS.items():
        policy = raw_policy if isinstance(raw_policy, dict) else {}
        mode = _call_recording_mode(policy)
        if mode == "disabled":
            continue
        try:
            chat_id = int(key)
        except (TypeError, ValueError):
            continue
        if chat_id < 0:
            groups[mode].append(chat_id)
            call_policy = _as_mapping(policy.get("call_recording"))
            if call_policy.get("send_to_chat") is True:
                groups["send_to_chat"].append(chat_id)
    for values in groups.values():
        values.sort()
    return groups


def configured_call_recording_users():
    users = {"allowed_callers": []}
    for key, raw_policy in ALLOWED.items():
        policy = raw_policy if isinstance(raw_policy, dict) else {}
        call_policy = _as_mapping(policy.get("call_recording"))
        mode = str(call_policy.get("mode") or "disabled").strip().lower()
        if mode in ("enabled", "auto", "on"):
            try:
                user_id = int(key)
            except (TypeError, ValueError):
                continue
            if user_id > 0:
                users["allowed_callers"].append(user_id)
    users["allowed_callers"].sort()
    return users


def configured_voice_agent_users():
    """Direct callers the assistant answers by talking. Independent of call_recording:
    either switch alone answers the call, and both together record it."""
    users = {}
    for key, raw_policy in ALLOWED.items():
        policy = _as_mapping(raw_policy)
        voice_policy = _as_mapping(policy.get("voice_agent"))
        mode = str(voice_policy.get("mode") or "disabled").strip().lower()
        if mode not in ("enabled", "auto", "on"):
            continue
        try:
            user_id = int(key)
        except (TypeError, ValueError):
            continue
        if user_id <= 0:
            continue
        # Three levels, narrowest first: this caller, then the project's voice
        # defaults, then the built-in — so a project can set them at all.
        def resolved(field, fallback):
            return (voice_policy.get(field)
                    or VOICE_AGENT_DEFAULTS.get(field)
                    or fallback)
        try:
            history = int(resolved("history", voice_agent.DEFAULT_HISTORY_MESSAGES))
        except (TypeError, ValueError):
            history = voice_agent.DEFAULT_HISTORY_MESSAGES
        # Tools are the one field that layers rather than replaces: a project
        # names the set its calls run on, and a caller turns one on or off
        # without restating the rest. Everything starts off — a tool the model
        # is holding is a tool it will reach for.
        tools = {name: False for name in voice_agent.TOOL_NAMES}
        for layer in (VOICE_AGENT_DEFAULTS.get("tools"),
                      voice_policy.get("tools")):
            for name, on in _as_mapping(layer).items():
                if name in tools:
                    tools[name] = bool(on)
        users[user_id] = {
            "name": policy.get("name") or str(user_id),
            "model": resolved("model", voice_agent.DEFAULT_MODEL),
            "voice": resolved("voice", voice_agent.DEFAULT_VOICE),
            "greeting": resolved("greeting", None),
            "history": max(0, history),
            "tools": tools,
        }
    return users


def call_recorder_command():
    # Group watching runs on the daemon's own PyTgCalls instance (not as a subprocess).
    # A second update-consuming connection suppresses p2p INCOMING_CALL delivery.
    return None


def sender_role_for(sender_id, group_policy, is_direct):
    """The role a sender carries, resolved once for every path that starts a
    worker. A voice caller is authenticated by caller id alone, exactly like a
    direct-message sender, so both paths must land on the same role — role is
    what `_authority_policy_for` turns into tool authority, and a second
    resolution here would let a phone call widen what a message may reach."""
    sid = str(sender_id)
    allowed = _as_mapping(ALLOWED.get(sid))
    member = _as_mapping(_as_mapping(_as_mapping(group_policy).get("members")).get(sid))
    default_role = (DIRECT_DEFAULT_ROLE if is_direct else
                    _as_mapping(group_policy).get("member_role")
                    or _as_mapping(group_policy).get("role") or "group_member")
    return allowed.get("role") or member.get("role") or default_role


def _member_policy(sender_id, group_policy):
    return _as_mapping(
        _as_mapping(_as_mapping(group_policy).get("members")).get(str(sender_id)))


def _is_agent_member(sender_id, group_policy):
    return str(_member_policy(sender_id, group_policy).get("kind") or "").strip().lower() == "agent"


def _may_address(sender_id, group_policy):
    """Whether this sender may invoke the assistant in this group.

    The group member entry wins over the global `allowed_users` default, so a
    person silenced everywhere can be handed the assistant back in one room.
    Reading a muted sender's messages is untouched: only invocation is closed.
    """
    member = _member_policy(sender_id, group_policy)
    if "may_address" in member:
        return member["may_address"] is not False
    return _as_mapping(ALLOWED.get(str(sender_id))).get("may_address") is not False


def _agent_dialogue_policy(group_policy):
    policy = _as_mapping(_as_mapping(group_policy).get("agent_dialogue"))
    try:
        max_turns = max(0, int(policy.get("max_turns") or 0))
    except (TypeError, ValueError):
        max_turns = 0
    return {
        "max_turns": max_turns,
        "reset_on_human_message": policy.get("reset_on_human_message") is not False,
    }


def _agent_peers(group_policy):
    peers = []
    for sender_id, raw in _as_mapping(_as_mapping(group_policy).get("members")).items():
        member = _as_mapping(raw)
        if str(member.get("kind") or "").strip().lower() != "agent":
            continue
        aliases = _as_list(member.get("address_aliases"))
        if not aliases and member.get("name"):
            aliases = [member["name"]]
        peers.append({
            "id": str(sender_id),
            "name": member.get("name") or str(sender_id),
            "aliases": aliases,
        })
    return peers


def _agent_dialogue_state(reg, key):
    return _channel_row(reg, key).setdefault("agent_dialogue", {"turns": 0})


def _agent_dialogue_snapshot(reg, key, group_policy):
    policy = _agent_dialogue_policy(group_policy)
    if not policy["max_turns"]:
        return None
    state = _agent_dialogue_state(reg, key)
    return {
        "turns": max(0, int(state.get("turns") or 0)),
        "max_turns": policy["max_turns"],
        "reset_on_human_message": policy["reset_on_human_message"],
    }


def _reset_agent_dialogue_for_human(reg, key, message, group_policy):
    """Reset the finite agent exchange when a human re-enters the channel.

    The caller persists the register only when this returns True. Unaddressed
    human messages count too: they are the natural boundary between autonomous
    agent exchanges, not requests that the assistant needs to answer.
    """
    policy = _agent_dialogue_policy(group_policy)
    sender_id = getattr(message, "sender_id", None)
    if (not policy["max_turns"] or not policy["reset_on_human_message"]
            or sender_id is None or _is_agent_member(sender_id, group_policy)):
        return False
    state = _agent_dialogue_state(reg, key)
    if not int(state.get("turns") or 0):
        return False
    state.update({
        "turns": 0,
        "reset_message_id": getattr(message, "id", None),
        "reset_at": now(),
    })
    return True


def _admit_agent_turn(reg, key, message, group_policy):
    """Consume one configured agent turn, or reject it once the cap is full.

    A turn means one explicitly addressed incoming request from a configured
    agent and the single Marvin response it may produce. The message job is the
    idempotency boundary, so callers invoke this only after successfully
    reserving that job.
    """
    sender_id = getattr(message, "sender_id", None)
    if not _is_agent_member(sender_id, group_policy):
        return True, None
    policy = _agent_dialogue_policy(group_policy)
    if not policy["max_turns"]:
        return True, None
    state = _agent_dialogue_state(reg, key)
    turns = max(0, int(state.get("turns") or 0))
    if turns >= policy["max_turns"]:
        return False, {
            "turns": turns,
            "max_turns": policy["max_turns"],
        }
    turns += 1
    state.update({
        "turns": turns,
        "last_agent_message_id": getattr(message, "id", None),
        "last_agent_sender_id": str(sender_id),
        "updated_at": now(),
    })
    return True, {
        "turns": turns,
        "max_turns": policy["max_turns"],
    }


def _policy_allowed_capabilities(policy):
    if not isinstance(policy, dict):
        return None
    if "allowed_capabilities" in policy:
        return policy.get("allowed_capabilities")
    return policy.get("capabilities")


def _normalize_capability_rule(rule):
    if rule is True or rule == "*":
        return True
    if rule in (False, None):
        return False
    if isinstance(rule, list):
        return {"allow": True, "verbs": rule}
    if isinstance(rule, dict):
        out = dict(rule)
        out.setdefault("allow", True)
        return out
    return bool(rule)


def _normalize_allowed_capabilities(value):
    if value is True or value == "*":
        return {"*": True}
    if isinstance(value, list):
        return {str(name): True for name in value}
    if isinstance(value, dict):
        return {
            str(name): _normalize_capability_rule(rule)
            for name, rule in value.items()
        }
    return {}


def _authority_policy_for(job, group_policy, is_direct):
    authority = _as_mapping(SETTINGS.get("authority"))
    if not authority and not (
        isinstance(group_policy, dict) and (
            group_policy.get("authority") or _policy_allowed_capabilities(group_policy)
        )
    ):
        return None

    role = job.get("sender_role") or ("direct_user" if is_direct else "group_member")
    policy = _deep_merge(
        _as_mapping(authority.get("default")),
        _as_mapping(_as_mapping(authority.get("roles")).get(role)),
    )

    sender_id = str(job.get("sender_id") or "")
    if sender_id in ALLOWED and isinstance(ALLOWED[sender_id], dict):
        row = ALLOWED[sender_id]
        policy = _deep_merge(policy, _as_mapping(row.get("authority")))
        caps = _policy_allowed_capabilities(row)
        if caps is not None:
            policy["allowed_capabilities"] = caps

    if isinstance(group_policy, dict):
        policy = _deep_merge(policy, _as_mapping(group_policy.get("authority")))
        caps = _policy_allowed_capabilities(group_policy)
        if caps is not None:
            policy["allowed_capabilities"] = caps
        member = _as_mapping(_as_mapping(group_policy.get("members")).get(sender_id))
        policy = _deep_merge(policy, _as_mapping(member.get("authority")))
        caps = _policy_allowed_capabilities(member)
        if caps is not None:
            policy["allowed_capabilities"] = caps

    caps = _normalize_allowed_capabilities(_policy_allowed_capabilities(policy))
    return {
        "version": 1,
        "source": "telegram",
        "connection": CONNECTION,
        "chat_id": job.get("chat_id"),
        "topic_id": job.get("topic_id"),
        "topic_title": job.get("topic_title"),
        "chat_type": "private" if is_direct else "group",
        "chat_name": (group_policy or {}).get("name") if isinstance(group_policy, dict) else None,
        "sender_id": sender_id,
        "sender_name": job.get("sender_name"),
        "sender_role": role,
        "allowed_capabilities": caps,
    }


def _authority_summary(ctx):
    if not ctx:
        return "not declared"
    caps = ctx.get("allowed_capabilities") or {}
    if caps.get("*") is True:
        return "all capabilities"
    bits = []
    for name, rule in sorted(caps.items()):
        if rule is False:
            continue
        if rule is True:
            bits.append(name)
            continue
        if isinstance(rule, dict):
            detail = []
            if rule.get("scope"):
                detail.append(f"scope={rule['scope']}")
            if rule.get("verbs"):
                detail.append("verbs=" + ",".join(map(str, rule["verbs"])))
            bits.append(f"{name} ({'; '.join(detail)})" if detail else name)
    return ", ".join(bits) if bits else "no capabilities"


def _chat_key_candidates(chat_id):
    key = str(chat_id)
    candidates = [key]
    try:
        n = int(chat_id)
    except (TypeError, ValueError):
        return candidates
    for item in (str(abs(n)), key[4:] if key.startswith("-100") else None):
        if item and item not in candidates:
            candidates.append(item)
    return candidates


def _group_policy(chat_id):
    for key in _chat_key_candidates(chat_id):
        policy = ALLOWED_GROUPS.get(key)
        if isinstance(policy, dict):
            return key, policy
        if policy is True:
            return key, {}
    return None, None


class RouteUnavailable(RuntimeError):
    """A configured route names a directory that cannot serve this request."""


ROUTE_PROJECT_MARKERS = (
    ("capabilities", "settings.json"), (".contextkit", "config.toml"),
    (".capabilities",), (".env",), (".env.local",), (".git",),
)


def _looks_like_project(root):
    """The marker set every capability CLI walks up looking for.

    A target carrying none of them is not a project: the capability contract
    would climb past it, resolve some parent instead, and the worker would run
    against a project nobody named.
    """
    return any(root.joinpath(*marker).exists() for marker in ROUTE_PROJECT_MARKERS)


def _route_target(raw, label):
    """Resolve one configured route into a usable worker directory.

    Judged at dispatch rather than at settings load, because a settings failure
    exits the process: a neighbouring repository that was renamed would
    otherwise stop the daemon that serves every other channel. Here it costs
    the one request that names it.
    """
    candidate = Path(str(raw)).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RouteUnavailable(
            f"{label} points at {candidate}, which does not exist") from exc
    if not resolved.is_dir():
        raise RouteUnavailable(f"{label} points at {resolved}, which is not a directory")
    if not _looks_like_project(resolved):
        markers = ", ".join("/".join(marker) for marker in ROUTE_PROJECT_MARKERS)
        raise RouteUnavailable(
            f"{label} points at {resolved}, which carries no project marker ({markers})")
    return resolved


def _topic_policy(group_policy, topic_id):
    """The entry a forum topic contributes to its group's policy."""
    if topic_id is None:
        return {}
    topics = _as_mapping(_as_mapping(group_policy).get("topics"))
    return _as_mapping(topics.get(str(topic_id)))


def _route_for(job, group_policy, is_direct):
    """The configured route for one request: topic, then chat, then direct user.

    Returns the raw settings value and the settings path that supplied it, so a
    refusal can name where the route was written. Nothing configured leaves the
    daemon's own project standing, which is the last row of this order rather
    than a privileged default.
    """
    policy = _as_mapping(group_policy)
    chat_id = job.get("chat_id")
    topic = _topic_policy(group_policy, job.get("topic_id"))
    if topic.get("project"):
        return topic["project"], f"allowed_groups.{chat_id}.topics.{job.get('topic_id')}.project"
    if policy.get("project"):
        return policy["project"], f"allowed_groups.{chat_id}.project"
    if is_direct:
        sender = _as_mapping(ALLOWED.get(str(job.get("sender_id"))))
        if sender.get("project"):
            return sender["project"], f"allowed_users.{job.get('sender_id')}.project"
    return None, None


def _route_map():
    """Every route the settings declare, in one readable answer.

    Reachability is deliberately absent: this says where channels are pointed,
    and a route that cannot serve a request says so in the chat that asked.
    """
    routes = []
    for chat_id, raw in ALLOWED_GROUPS.items():
        policy = _as_mapping(raw) if isinstance(raw, dict) else {}
        if policy.get("project"):
            routes.append({"scope": f"group {chat_id}",
                           "project": str(policy["project"])})
        for topic_id, entry in _as_mapping(policy.get("topics")).items():
            entry = _as_mapping(entry)
            if entry.get("project"):
                routes.append({"scope": f"group {chat_id} topic {topic_id}",
                               "project": str(entry["project"])})
    for user_id, raw in ALLOWED.items():
        policy = _as_mapping(raw)
        if policy.get("project"):
            routes.append({"scope": f"direct {user_id}",
                           "project": str(policy["project"])})
    return routes


def _group_aliases(policy):
    aliases = []
    for field in ("aliases", "address_aliases", "mentions"):
        aliases.extend(_as_list((policy or {}).get(field)))
    return aliases or list(DEFAULT_GROUP_ALIASES)


def _message_text(message):
    return (
        getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or getattr(message, "message", None)
        or ""
    )


def _is_spoken_media(message):
    return bool(
        getattr(message, "voice", False)
        or getattr(message, "audio", False)
        or getattr(message, "video_note", False)
    )


def _is_telegram_voice_note(message):
    """True for Telegram voice notes specifically (not arbitrary audio files)."""
    return bool(getattr(message, "voice", False))


def _voice_transcription_mode(policy, reg, key):
    """Resolve the effective voice transcription mode for a group channel.

    Returns "auto", "disabled", or None (disabled). The static group policy is the
    default; a runtime /set override in the register takes precedence.
    """
    if policy is None:
        return None
    runtime = (reg.get(key, {}).get("settings", {}) or {}).get("voice_transcription")
    if runtime is not None:
        normalized = str(runtime).strip().lower()
        if normalized in ("auto", "on", "enabled", "true"):
            return "auto"
        return "disabled"
    static = (policy.get("voice_transcription") or {}).get("mode")
    if static is not None:
        normalized = str(static).strip().lower()
        if normalized in ("auto", "on", "enabled"):
            return "auto"
        return "disabled"
    return "disabled"


def _message_kind(message):
    if _is_spoken_media(message):
        return "voice"
    if getattr(message, "file", None):
        return "media"
    return "text"


def _text_names_me(text, me, policy):
    if not text:
        return False
    username = getattr(me, "username", None)
    if username and re.search(rf"(?iu)(?<![\w@])@{re.escape(username)}(?!\w)", text):
        return True
    for alias in _group_aliases(policy):
        if re.search(rf"(?iu)(?<!\w){re.escape(alias)}(?!\w)", text):
            return True
    return False


def _is_call_recording_request(text):
    value = str(text or "").strip()
    if _command_name(value) == "/record":
        return True
    normalized = value.casefold().replace("ё", "е")
    asks_to_record = re.search(
        r"\b(?:запиши|записывай|начни\s+запись|включи\s+запись|record)\b",
        normalized,
    )
    names_call = re.search(r"\b(?:звонок|созвон|разговор|call)\b", normalized)
    return bool(asks_to_record and names_call)


async def _reply_is_to_me(message, me):
    if not getattr(message, "is_reply", False):
        return False
    try:
        replied = await message.get_reply_message()
    except Exception:
        return False
    if not replied:
        return False
    return bool(getattr(replied, "out", False) or getattr(replied, "sender_id", None) == me.id)


async def _message_addresses_me(message, me, policy):
    # A tagged answer is addressed to a live session, not to this daemon. It
    # beats every other signal — reply, mention, name — because those are how
    # agents write to each other and none of them says who consumes the answer.
    if _marker_line(_message_text(message), NO_REPLY_MARKER):
        return False
    if not _may_address(getattr(message, "sender_id", None), policy):
        return False
    agent_sender = _is_agent_member(getattr(message, "sender_id", None), policy)
    if (policy or {}).get("require_reference") is False and not agent_sender:
        return True
    if (_call_recording_mode(policy) == "on_request"
            and _command_name(_message_text(message)) == "/record"):
        return True
    if getattr(message, "mentioned", False):
        return True
    if _text_names_me(_message_text(message), me, policy):
        return True
    # A reply is transport context, not an implicit invocation, for another
    # configured agent. It must name or mention Marvin explicitly above.
    if agent_sender:
        return False
    return await _reply_is_to_me(message, me)


async def _event_access(event, me, reg):
    """Gate 1: the door. Determines if a message should be processed.

    Returns access dict with kind/policy, or None to ignore. For groups with
    voice_transcription auto, unaddressed Telegram voice notes are admitted as
    ambient (transcribe + echo, no worker dispatch).
    """
    sender_id = str(event.sender_id) if event.sender_id is not None else None
    if getattr(event, "is_private", False):
        if sender_id in ALLOWED or ALLOW_ANY_DIRECT:
            return {"kind": "private", "policy": None}
        return None
    group_key, policy = _group_policy(event.chat_id)
    if policy is None:
        return None
    key = str(event.chat_id)
    addressed = await _message_addresses_me(event.message, me, policy)
    if addressed:
        return {"kind": "group", "group_key": group_key, "policy": policy, "addressed": True}
    if (_is_telegram_voice_note(event.message)
            and _voice_transcription_mode(policy, reg, key) == "auto"):
        return {"kind": "group", "group_key": group_key, "policy": policy, "addressed": False, "ambient_voice": True}
    return None


async def _event_chat_ref(event, is_direct=False):
    attrs = ("input_chat", "input_sender") if is_direct else ("input_chat",)
    for attr in attrs:
        value = getattr(event, attr, None)
        if value:
            return value
    methods = (
        ("get_input_chat", "get_input_sender", "get_chat")
        if is_direct else
        ("get_input_chat", "get_chat")
    )
    for method in methods:
        fn = getattr(event, method, None)
        if not fn:
            continue
        try:
            value = await fn()
        except Exception:
            continue
        if value:
            return value
    return event.chat_id


def _incoming_in_scope(message, group_policy):
    if getattr(message, "out", False):
        return False
    if group_policy is not None:
        return True
    return ALLOW_ANY_DIRECT or str(getattr(message, "sender_id", None)) in ALLOWED


def _tail_in_scope(message, group_policy):
    return bool(getattr(message, "out", False) or _incoming_in_scope(message, group_policy))


def _entity_name(entity, fallback):
    if not entity:
        return fallback
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    title = getattr(entity, "title", None)
    username = getattr(entity, "username", None)
    name = " ".join(v for v in (first, last) if v) or title
    return name or (f"@{username}" if username else fallback)


async def _sender_profile(message, group_policy=None, direct=False):
    sid = str(getattr(message, "sender_id", None))
    if sid in ALLOWED:
        row = ALLOWED[sid]
        return {"id": sid, "name": row.get("name", sid), "role": row.get("role", "unknown")}
    member_rows = (group_policy or {}).get("members") or {}
    if sid in member_rows:
        row = member_rows[sid]
        return {"id": sid, "name": row.get("name", sid), "role": row.get("role", "group_member")}
    try:
        sender = await message.get_sender()
    except Exception:
        sender = None
    if direct:
        role = DIRECT_DEFAULT_ROLE
    else:
        role = (group_policy or {}).get("member_role") or (group_policy or {}).get("role") or "group_member"
    return {"id": sid, "name": _entity_name(sender, sid), "role": role}


def _reply_target(message_id, sender_id, group_policy, is_direct):
    """Where an outbound response is threaded for this sender.

    A configured agent peer receives top-level responses instead of
    Telegram replies. That breaks mechanical bot-to-bot reply loops while the
    response text remains free to address the peer by name when the model
    deliberately wants to hand it another turn.
    """
    if is_direct:
        return None
    if _is_agent_member(sender_id, group_policy):
        return None
    return message_id


def _delivery_description(reply_to, is_direct):
    if is_direct:
        return "final response is sent as a plain direct message"
    if reply_to is None:
        return ("final response is sent as a new top-level group message, never as a "
                "Telegram reply to this sender")
    return "final response is sent as a reply to the request message"


# --- register (dynamic state: watermark + per-channel /set overrides) ---------
def load_register():
    return json.loads(REGISTER.read_text()) if REGISTER.exists() else {}


def save_register(reg):
    """Commit the watermark register atomically so crashes retain the prior file."""
    _atomic_json(REGISTER, reg)


def _channel_row(reg, key):
    return reg.setdefault(key, {"last_processed_message_id": 0})


def _job_map(reg, key):
    return _channel_row(reg, key).setdefault("jobs", {})


def _voice_echo_senders(reg, key):
    """Mapping of echo message ID → sender name for voice transcription echoes.
    Used to attribute outgoing echo messages to the original speaker in conversation history."""
    return _channel_row(reg, key).setdefault("voice_echo_senders", {})


def _record_voice_echo_sender(reg, key, echo_ids, sender_name):
    """Record that the given echo message ID(s) should be attributed to sender_name."""
    if not echo_ids or not sender_name:
        return
    mapping = _voice_echo_senders(reg, key)
    for echo_id in echo_ids:
        mapping[str(echo_id)] = sender_name
    _prune_voice_echo_senders(reg, key)


def _prune_voice_echo_senders(reg, key):
    """Keep only the most recent entries. Limit = 2 × tail_size to ensure all messages
    in a worker's conversation history can be attributed, even if the tail is full of echoes."""
    s = channel_settings(reg, key)
    limit = 2 * s.get("tail_size", 50)
    mapping = _voice_echo_senders(reg, key)
    if len(mapping) > limit:
        # Remove oldest entries (lowest message IDs)
        sorted_ids = sorted(mapping.keys(), key=lambda x: int(x) if x.isdigit() else 0)
        to_remove = sorted_ids[:len(mapping) - limit]
        for msg_id in to_remove:
            mapping.pop(msg_id, None)


def _job_id(message_id):
    return str(message_id)


def _job_message_id(job):
    try:
        return int(job.get("message_id") or 0)
    except (TypeError, ValueError):
        return 0


def _queued_jobs(reg, key):
    jobs = _job_map(reg, key)
    return sorted(
        (job for job in jobs.values() if job.get("status") == "queued"),
        key=_job_message_id,
    )


def _has_pending_jobs(reg, key):
    return any(job.get("status") in ("preparing", "queued", "running")
               for job in _job_map(reg, key).values())


def _message_is_known(reg, key, message_id):
    """True once a message is reserved as a job or covered by the channel watermark."""
    row = reg.get(key)
    if not isinstance(row, dict):
        return False
    jobs = row.get("jobs")
    if isinstance(jobs, dict) and _job_id(message_id) in jobs:
        return True
    try:
        watermark = int(row.get("last_processed_message_id") or 0)
        return int(message_id) <= watermark
    except (TypeError, ValueError):
        return False


def _catch_up_message_is_known(reg, key, legacy_key, message_id):
    """Honor exact legacy jobs always, but its watermark only before topic state."""
    if _message_is_known(reg, key, message_id):
        return True
    if key == legacy_key:
        return False
    legacy_row = reg.get(legacy_key)
    legacy_jobs = legacy_row.get("jobs") if isinstance(legacy_row, dict) else None
    if isinstance(legacy_jobs, dict) and _job_id(message_id) in legacy_jobs:
        return True
    return key not in reg and _message_is_known(reg, legacy_key, message_id)


def _recover_incomplete_jobs(reg):
    changed = False
    for row in reg.values():
        if not isinstance(row, dict):
            continue
        for job in (row.get("jobs") or {}).values():
            if job.get("status") in ("preparing", "running"):
                previous = job.get("status")
                job["status"] = "queued"
                job.pop("started_at", None)
                job["last_error"] = f"service restarted while job was {previous}"
                changed = True
    return changed


def _prune_jobs(reg, key):
    jobs = _job_map(reg, key)
    changed = False
    for jid, job in list(jobs.items()):
        if job.get("status") in ("done", "error", "stopped"):
            jobs.pop(jid, None)
            changed = True
    return changed


def _prune_all_jobs(reg):
    changed = False
    for key in list(reg.keys()):
        if isinstance(reg.get(key), dict):
            changed = _prune_jobs(reg, key) or changed
    return changed


def _default_as_none(value):
    return None if value in (None, "", "default") else value


def _active_worker(row):
    settings = (row or {}).get("settings", {})
    worker = str(settings.get("worker") or DEFAULT_WORKER).strip().lower()
    return worker if worker in WORKER_NAMES else DEFAULT_WORKER


def migrate_register(reg):
    """Migrate old per-channel settings.model into workers.<active>.model, and
    carry a forum General row onto the topic id it now keys on."""
    changed = False
    for key in list(reg.keys()):
        # General moved off the bare chat key the moment its chat declared a
        # topics map. Without the move, catch-up would find no watermark and
        # replay the room from wherever the tail begins.
        try:
            chat_id, topic_id = _channel_identity(key)
        except (TypeError, ValueError):
            continue
        if topic_id is not None:
            continue
        _, policy = _group_policy(chat_id)
        if not _as_mapping(_as_mapping(policy).get("topics")):
            continue
        general = _channel_key(chat_id, GENERAL_TOPIC_ID)
        if general not in reg:
            reg[general] = reg.pop(key)
            changed = True
    for row in reg.values():
        if not isinstance(row, dict):
            continue
        settings = row.setdefault("settings", {})
        if "worker" in settings:
            worker = str(settings.get("worker") or DEFAULT_WORKER).strip().lower()
            if worker not in WORKER_NAMES:
                settings["worker"] = DEFAULT_WORKER
                changed = True
        if "model" in settings:
            old_model = settings.pop("model")
            model = _default_as_none(old_model)
            if model:
                worker = _active_worker(row)
                row.setdefault("workers", {}).setdefault(worker, {})["model"] = model
            changed = True
    return changed


def _worker_settings(row, worker):
    cfg = dict((DEFAULTS.get("workers") or {}).get(worker, {}))
    cfg.update(((row or {}).get("workers") or {}).get(worker, {}))
    return {k: _default_as_none(v) for k, v in cfg.items()}


def _worker_flags(worker, cfg):
    out = {}
    if worker == "claude":
        out["effort"] = cfg.get("effort")
    elif worker == "codex":
        out["reasoning_effort"] = cfg.get("reasoning_effort")
        out["service_tier"] = cfg.get("service_tier")
    return out


def voice_agent_settings():
    """Worker policy for a task started from a call: the project's own worker
    settings, overlaid by whatever `defaults.voice_agent` names.

    Work asked for by voice is operational and quick — look something up, file
    something, write something down — so a project points this at a fast model
    rather than the deep-reasoning mode it codes with. The timeout is the text
    worker's: the tool returns at once, so nothing waits on the clock, and task
    size is bounded by the model choice instead."""
    worker = str(VOICE_AGENT_DEFAULTS.get("worker") or DEFAULT_WORKER).strip().lower()
    if worker not in WORKER_NAMES:
        worker = DEFAULT_WORKER
    cfg = dict(_as_mapping(_as_mapping(DEFAULTS.get("workers")).get(worker)))
    cfg.update(_as_mapping(_as_mapping(VOICE_AGENT_DEFAULTS.get("workers")).get(worker)))
    cfg = {k: _default_as_none(v) for k, v in cfg.items()}
    session_mode = str(_as_mapping(VOICE_AGENT_DEFAULTS.get("session")).get("mode")
                       or "carry").strip().lower()
    return {
        "worker": worker,
        "worker_settings": cfg,
        "model": cfg.get("model"),
        "worker_timeout": DEFAULTS.get("worker_timeout", 90),
        "session_mode": session_mode,
        **_worker_flags(worker, cfg),
    }


def voice_task_job(caller_id, caller_name, text, task_id):
    """A call's task shaped exactly like a direct message's job, so the one
    authority resolution applies to it unchanged."""
    key = str(caller_id)
    return {
        "message_id": task_id,
        "chat_id": key,
        "sender_id": key,
        "sender_name": caller_name or key,
        "sender_role": sender_role_for(key, None, True),
        "kind": "voice_call_task",
        "text": text,
    }


VOICE_TASK_DELIVERY = (
    "the caller is on a live phone call and your reply is read aloud to them — "
    "answer in one or two short spoken sentences, plain words only, no markdown, "
    "no lists, no URLs")

VOICE_SUMMARY_DELIVERY = (
    "your reply is posted into the chat as the record of a call that just "
    "ended — write it as plain prose in the language of the call, no preamble, "
    "no headings, no markdown")

VOICE_SUMMARY_TASK = (
    "A voice call has just ended. Write a short summary of it for the chat, so "
    "that a later conversation knows what was discussed. Cover what was asked "
    "for, what was decided, and anything left open. Do not invent anything that "
    "is not in the transcript. Three or four sentences at most.\n\n"
    "Transcript:\n")

# Generic shell tools, described by what a caller waiting on the line would
# understand. Anything not here is named by its own command instead, so a
# capability the project happens to have is recognised without being listed.
SHELL_STAGES = {
    "rg": "searching for it", "grep": "searching for it",
    "fd": "searching for it", "find": "searching for it", "ls": "searching for it",
    "cat": "reading what it found", "head": "reading what it found",
    "tail": "reading what it found", "sed": "reading what it found",
    "awk": "reading what it found", "less": "reading what it found",
    "jq": "reading what it found",
    "git": "checking the repository",
    "python": "working through the numbers", "python3": "working through the numbers",
    "node": "working through the numbers",
}


def _shell_stage(command):
    """Describe a shell step by what it actually does.

    Codex wraps every command in `<shell> -lc "…"`, so the first word is the
    shell and says nothing. Look past the wrapper at the real command, and name
    the tool being used — that is what a caller waiting on the line cares about.
    """
    if isinstance(command, (list, tuple)):
        parts = [str(part) for part in command]
        text = parts[-1] if parts else ""
    else:
        text = str(command or "")
    for marker in (" -lc ", " -c "):
        if marker in text:
            text = text.split(marker, 1)[1]
    words = text.strip().strip("'\"").split()
    if not words:
        return "running a command"
    head = words[0].rsplit("/", 1)[-1]
    verb = words[1] if len(words) > 1 and not words[1].startswith("-") else ""
    if head == "telegram" and verb == "send":
        # The worker reporting its progress is not progress. Its words arrive
        # through the outbox and outrank anything derived here anyway.
        return None
    if head in SHELL_STAGES:
        return SHELL_STAGES[head]
    return f"asking {head} to {verb}" if verb else f"running {head}"


def codex_thread_started(line):
    """The thread id out of a `thread.started` event, None for any other line.

    Read live from the output pump rather than from the run's final stdout,
    because the runs whose classification matters most are the ones that never
    produce final stdout: a worker that is killed or times out is reported by its
    stderr alone. Whether the thread ever opened is what separates a resume that
    executed nothing — safe to run again — from a turn that may already have done
    half of what it was asked.
    """
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if isinstance(event, dict) and event.get("type") == "thread.started":
        return event.get("thread_id")
    return None


def codex_context_compacted(line):
    """True when the event says codex just summarized the thread to fit.

    A session nobody ever resets eventually fills its context window, and codex
    handles that by itself. What it cannot promise is that the summary keeps the
    instructions the session was opened with — so the one thing the daemon has to
    know is that it happened."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return False
    if not isinstance(event, dict):
        return False
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    return (item.get("type") or item.get("item_type")) == "context_compaction"


def codex_event_stage(line):
    """Turn one codex `--json` event into a short, host-neutral phrase.

    Structural rather than narrated: the worker does not have to remember to
    report anything, because what it is doing is already in the event stream.
    Returns None for events that say nothing a waiting caller would care about.
    """
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind == "thread.started":
        return "starting"
    if kind == "turn.completed":
        # The turn is already over when this arrives, so anything it produces
        # is a report about the past dressed as a report about now. The result
        # itself is a second away and says the same thing truthfully.
        return None
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = item.get("type") or item.get("item_type")
    if item_type in ("command_execution", "local_shell_call", "shell_call"):
        return _shell_stage(item.get("command"))
    if item_type in ("file_change", "patch_apply", "apply_patch"):
        return "changing files"
    if item_type in ("mcp_tool_call", "tool_call", "function_call"):
        return "using a tool"
    if item_type == "web_search":
        return "searching the web"
    if item_type == "reasoning":
        return "working it out"
    return None


def voice_task_preamble(chat_id, seconds):
    """Prepended to a task asked for by voice.

    A caller on the line hears silence, not a quiet chat, so the instruction
    goes into the task text itself with the command and the chat spelled out.
    Where those lines go is the part the worker cannot guess: to the assistant
    holding the call, which decides what is worth saying aloud.
    """
    return (
        "Someone is waiting on a live phone call for this, so silence costs "
        "them directly.\n\n"
        "Report what you are doing as you go, by running\n"
        f'    {WORKER_BIN / "telegram"} send {chat_id} "<one short line>"\n'
        "spelled with that exact path. A worker that runs a shell finds the "
        "same thing under the bare name, but one that executes commands "
        "directly resolves the name its own way and reaches the real Telegram "
        "instead - which sends your working notes to the caller as messages "
        "rather than to the assistant holding the call.\n"
        "as your very first action before you look at anything, and again "
        f"after each real step - roughly every {seconds:.0f} seconds while the "
        "work continues.\n\n"
        "Report facts, not status. Each line is one concrete thing you just "
        "did or just learned: which tool you asked, what it answered, what you "
        "are about to try, what did not work and what you are doing instead. "
        "\"Looking through yesterday's calls\", \"got the list, twelve of them, "
        "counting the failed ones\", \"that connection is read-only, trying the "
        "other one\" all tell the caller where the work stands. \"Still "
        "working\", \"almost done\", \"finishing up\" and \"one moment\" tell "
        "them nothing and are worse than saying nothing at all - never send a "
        "line whose content is only that time is passing. Never guess at how "
        "far along you are or how much is left.\n\n"
        "One plain line each time, in the language of the task, no paths, no "
        "commands, no markdown.\n\n"
        "These lines go to the assistant on the call, not to the caller. Do not "
        "message the caller yourself and do not address them; the assistant "
        "decides what to say aloud. The answer you return at the end is the "
        "result — the progress lines are not.\n\n"
        "The task:\n"
    )


def voice_task_authority(caller_id, caller_name, text, task_id="voice-task"):
    """Tool authority for a task started from a call — the direct-message path's
    own resolution, given the caller as its sender and nothing widened."""
    return _authority_policy_for(
        voice_task_job(caller_id, caller_name, text, task_id), None, True)


def channel_settings(reg, key):
    """Project defaults and channel policy overlaid by this channel's /set overrides."""
    row = reg.get(key, {})
    s = row.get("settings", {})
    _, group_policy = _group_policy(_channel_identity(key)[0])
    configured_timeout = (
        group_policy.get("worker_timeout", DEFAULTS.get("worker_timeout", 90))
        if isinstance(group_policy, dict)
        else DEFAULTS.get("worker_timeout", 90)
    )
    worker = _active_worker(row)
    cfg = _worker_settings(row, worker)
    out = {
        "tail_size": s.get("tail_size", DEFAULTS.get("tail_size", 50)),
        "debounce": s.get("debounce", DEFAULTS.get("debounce", 3)),
        "worker_timeout": s.get("worker_timeout", configured_timeout),
        "progress_after": s.get("progress_after", DEFAULTS.get("progress_after", 15)),
        "max_parallel_jobs": s.get("max_parallel_jobs", DEFAULTS.get("max_parallel_jobs", 2)),
        "max_attempts": s.get("max_attempts", DEFAULTS.get("max_attempts", 3)),
        "worker": worker,
        "worker_settings": cfg,
        "model": cfg.get("model"),
    }
    out.update(_worker_flags(worker, cfg))
    return out


def _set_worker_setting(reg, key, worker, field, value):
    row = reg.setdefault(key, {})
    cfg = row.setdefault("workers", {}).setdefault(worker, {})
    value = value.strip()
    if field == "model":
        cfg["model"] = _default_as_none(value)
        return f"{worker}.model = {value}"
    if worker == "claude" and field in ("effort", "reasoning"):
        effort = value.lower()
        if effort not in CLAUDE_EFFORTS:
            raise ValueError(f"claude effort must be one of {_values(CLAUDE_EFFORT_CHOICES)}")
        cfg["effort"] = _default_as_none(effort)
        return f"claude.effort = {effort}"
    if worker == "codex" and field in ("effort", "reasoning", "reasoning_effort"):
        effort = value.lower()
        if effort not in CODEX_REASONING_EFFORTS:
            raise ValueError(f"codex reasoning must be one of {_values(CODEX_REASONING_CHOICES)}")
        cfg["reasoning_effort"] = _default_as_none(effort)
        return f"codex.reasoning = {effort}"
    if worker == "codex" and field in ("speed", "service-tier", "service_tier"):
        tier = value.lower()
        if tier not in CODEX_SERVICE_TIERS:
            raise ValueError(f"codex speed must be one of {_values(CODEX_SERVICE_TIER_CHOICES)}")
        cfg["service_tier"] = None if tier == "default" else ("priority" if tier == "fast" else tier)
        shown = "default" if cfg["service_tier"] is None else cfg["service_tier"]
        return f"codex.service_tier = {shown}"
    raise ValueError(f"{field} is not supported for worker {worker}")


def _status(reg, key):
    s = channel_settings(reg, key)
    wm = reg.get(key, {}).get("last_processed_message_id", 0)
    jobs = (reg.get(key, {}).get("jobs") or {}).values()
    counts = {}
    for job in jobs:
        status = job.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    worker = s["worker"]
    lines = [
        f"settings [{key}]:",
        f"  tail = {s['tail_size']}",
        f"  debounce = {s['debounce']}s",
        f"  worker = {worker}",
        f"  {worker}.model = {s.get('model') or 'default'}",
    ]
    if worker == "claude":
        lines.append(f"  claude.effort = {s.get('effort') or 'default'}")
    elif worker == "codex":
        lines.append(f"  codex.reasoning = {s.get('reasoning_effort') or 'default'}")
        lines.append(f"  codex.service_tier = {s.get('service_tier') or 'default'}")
    lines.append(f"  worker-timeout = {s['worker_timeout']}s")
    _, group_policy = _group_policy(_channel_identity(key)[0])
    if group_policy is not None:
        mode = s.get("voice_transcription") or _voice_transcription_mode(group_policy, reg, key)
        lines.append(f"  voice-transcription = {mode}")
    lines.append(f"  watermark = {wm}")
    if counts:
        lines.append("  jobs = " + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    return "\n".join(lines)


def _values(items):
    return "|".join(items)


def _model_hint(worker):
    if worker == "claude":
        return "default|opus|sonnet|haiku|fable|<full-claude-model-id>"
    if worker == "codex":
        return "default|gpt-5.5|gpt-5.4|gpt-5.4-mini|gpt-5.3-codex-spark|<codex-model-id>"
    return "default"


def _set_help(reg, key, topic=None):
    s = channel_settings(reg, key)
    active = s["worker"]
    topic = (topic or "").strip().lower()
    if topic in ("help", "?"):
        topic = ""
    target_worker, field = None, topic
    if "." in topic:
        maybe_worker, field = topic.split(".", 1)
        if maybe_worker in WORKER_NAMES:
            target_worker = maybe_worker
    worker = target_worker or active

    if topic == "worker":
        return f"usage: /set worker <{_values(WORKER_CHOICES)}>\ncurrent: {active}"
    if topic == "tail":
        return f"usage: /set tail <1..500>\ncurrent: {s['tail_size']}"
    if topic == "debounce":
        return f"usage: /set debounce <0..300>\ncurrent: {s['debounce']}s"
    if topic in ("worker-timeout", "worker_timeout", "timeout"):
        return ("usage: /set worker-timeout <1..3600|default>\n"
                f"current: {s['worker_timeout']}s")
    if topic in ("voice-transcription", "voice_transcription", "voice"):
        _, group_policy = _group_policy(_channel_identity(key)[0])
        if group_policy is None:
            return "voice-transcription setting is only available in groups (direct messages always transcribe voice)"
        current = s.get("voice_transcription") or _voice_transcription_mode(group_policy, reg, key)
        return f"usage: /set voice-transcription <auto|disabled>\ncurrent: {current}\naliases: on=auto, off=disabled"
    if field == "model" and worker in WORKER_NAMES:
        current = channel_settings(reg, key).get("model") if worker == active else \
            _worker_settings(reg.get(key, {}), worker).get("model")
        return f"usage: /set {worker}.model <{_model_hint(worker)}>\ncurrent: {current or 'default'}"
    if field in ("effort", "reasoning", "reasoning_effort") and worker == "claude":
        current = channel_settings(reg, key).get("effort") if worker == active else \
            _worker_settings(reg.get(key, {}), worker).get("effort")
        return f"usage: /set claude.effort <{_values(CLAUDE_EFFORT_CHOICES)}>\ncurrent: {current or 'default'}"
    if field in ("effort", "reasoning", "reasoning_effort") and worker == "codex":
        current = channel_settings(reg, key).get("reasoning_effort") if worker == active else \
            _worker_settings(reg.get(key, {}), worker).get("reasoning_effort")
        return f"usage: /set codex.reasoning <{_values(CODEX_REASONING_CHOICES)}>\ncurrent: {current or 'default'}"
    if field in ("speed", "service-tier", "service_tier") and worker == "codex":
        current = channel_settings(reg, key).get("service_tier") if worker == active else \
            _worker_settings(reg.get(key, {}), worker).get("service_tier")
        return (f"usage: /set codex.speed <{_values(CODEX_SERVICE_TIER_CHOICES)}>\n"
                f"current: {current or 'default'}\nfast is an alias for priority")
    if topic in WORKER_NAMES:
        lines = [f"{topic} settings:", f"  /set {topic}.model <{_model_hint(topic)}>"]
        if topic == "claude":
            lines.append(f"  /set claude.effort <{_values(CLAUDE_EFFORT_CHOICES)}>")
        elif topic == "codex":
            lines.append(f"  /set codex.reasoning <{_values(CODEX_REASONING_CHOICES)}>")
            lines.append(f"  /set codex.speed <{_values(CODEX_SERVICE_TIER_CHOICES)}>")
        return "\n".join(lines)
    if field in ("speed", "service-tier", "service_tier"):
        return "speed/service-tier is only available for codex\nusage: /set codex.speed <default|fast|priority>"

    lines = [
        "usage: /set <setting> <value>",
        f"active worker: {active}",
        "settings:",
        "  tail <1..500>",
        "  debounce <0..300>",
        "  worker-timeout <1..3600|default>",
        f"  worker <{_values(WORKER_CHOICES)}>",
        f"  model <{_model_hint(active)}>  (active worker)",
        "  reasoning <default|low|medium|high|xhigh>  (codex active)",
        "  effort <default|low|medium|high|xhigh|max>  (claude active)",
        "  speed <default|fast|priority>  (codex active)",
    ]
    _, group_policy = _group_policy(_channel_identity(key)[0])
    if group_policy is not None:
        lines.append("  voice-transcription <auto|disabled>  (groups only)")
    lines.extend([
        "worker-specific:",
        "  codex.model / codex.reasoning / codex.speed",
        "  claude.model / claude.effort",
    ])
    return "\n".join(lines)


def set_channel_setting(reg, key, k, v):
    """The single setter — validates, persists into the channel row, confirms."""
    s = reg.setdefault(key, {}).setdefault("settings", {})
    if k == "tail":
        n = int(v)
        if not 1 <= n <= 500:
            raise ValueError("tail must be 1..500")
        s["tail_size"] = n
        return f"tail = {n}"
    if k == "debounce":
        n = int(v)
        if not 0 <= n <= 300:
            raise ValueError("debounce must be 0..300 (seconds; 0 = dispatch immediately)")
        s["debounce"] = n
        return f"debounce = {n}s"
    if k in ("worker-timeout", "worker_timeout", "timeout"):
        if v.strip().lower() == "default":
            s.pop("worker_timeout", None)
            effective = channel_settings(reg, key)["worker_timeout"]
            return f"worker-timeout = default ({effective}s effective)"
        n = int(v)
        if not 1 <= n <= 3600:
            raise ValueError("worker-timeout must be 1..3600 seconds or default")
        s["worker_timeout"] = n
        return f"worker-timeout = {n}s"
    if k == "worker":
        worker = v.strip().lower()
        if worker not in WORKER_NAMES:
            raise ValueError(f"worker must be one of {_values(WORKER_CHOICES)}")
        s["worker"] = worker
        return f"worker = {worker}"
    if k in ("voice-transcription", "voice_transcription", "voice"):
        _, group_policy = _group_policy(_channel_identity(key)[0])
        if group_policy is None:
            raise ValueError("voice-transcription setting is only available in groups (direct messages always transcribe voice)")
        mode = v.strip().lower()
        if mode in ("auto", "on", "enabled", "true"):
            s["voice_transcription"] = "auto"
            return "voice-transcription = auto (unaddressed voice notes will be transcribed)"
        elif mode in ("disabled", "off", "false"):
            s["voice_transcription"] = "disabled"
            return "voice-transcription = disabled (only addressed voice notes will be transcribed)"
        else:
            raise ValueError("voice-transcription must be auto or disabled (aliases: on=auto, off=disabled)")
    target_worker, field = None, k
    if "." in k:
        maybe_worker, field = k.split(".", 1)
        if maybe_worker in WORKER_NAMES:
            target_worker = maybe_worker
    worker = target_worker or channel_settings(reg, key)["worker"]
    if field in ("model", "effort", "reasoning", "reasoning_effort",
                 "speed", "service-tier", "service_tier"):
        return _set_worker_setting(reg, key, worker, field, v)
    raise ValueError("unknown setting; use tail, debounce, worker-timeout, worker, model, reasoning, "
                     "effort, speed, service-tier, or <worker>.<setting>")


def _command_name(text):
    parts = text.strip().split()
    if not parts:
        return ""
    return parts[0].split("@", 1)[0].lower()


def _control_command_key(command):
    key = str(command or "").strip().split("@", 1)[0].lower().lstrip("/")
    return key if key in {"status", "set", "reload", "stop"} else "help"


def _control_commands_allow(commands, command):
    if commands is True or commands == "*":
        return True
    key = _control_command_key(command)
    if isinstance(commands, list):
        allowed = {str(item).strip().lower().lstrip("/") for item in commands}
        return "*" in allowed or key in allowed
    if isinstance(commands, dict):
        rule = commands.get(key, commands.get("*"))
        if rule is True or rule == "*":
            return True
        if isinstance(rule, dict):
            if rule.get("deny") is True:
                return False
            if rule.get("enabled") is False or rule.get("allow") is False:
                return False
            return True
    return False


def _control_policy_for(profile, group_policy=None):
    role = (profile or {}).get("role") or "unknown"
    sender_id = str((profile or {}).get("id") or "")
    control = _as_mapping(SETTINGS.get("control"))
    policy = _deep_merge(
        _as_mapping(_as_mapping(CONTROL_DEFAULTS.get("roles")).get(role)),
        _as_mapping(_as_mapping(control.get("roles")).get(role)),
    )

    if sender_id in ALLOWED and isinstance(ALLOWED[sender_id], dict):
        policy = _deep_merge(policy, _as_mapping(ALLOWED[sender_id].get("control")))
    if isinstance(group_policy, dict):
        policy = _deep_merge(policy, _as_mapping(group_policy.get("control")))
        member = _as_mapping(_as_mapping(group_policy.get("members")).get(sender_id))
        policy = _deep_merge(policy, _as_mapping(member.get("control")))
    return policy


def _control_command_allowed(command, profile, group_policy=None):
    policy = _control_policy_for(profile, group_policy)
    return _control_commands_allow(policy.get("commands"), command)


def _control_denied_reply(command, profile):
    cmd = "/" + _control_command_key(command)
    role = (profile or {}).get("role") or "unknown"
    if cmd == "/help":
        cmd = str(command or "that command")
    return f"nope: {cmd} is not allowed for role {role}"


def handle_command(reg, key, text):
    """Parse a /command. Returns reply text; mutates reg for /set (caller saves)."""
    parts = text.strip().split()
    cmd = _command_name(text)
    if cmd == "/status":
        return _status(reg, key)
    if cmd == "/set":
        if len(parts) < 2:
            return _set_help(reg, key)
        if len(parts) < 3:
            return _set_help(reg, key, parts[1].lower())
        try:
            return "ok, " + set_channel_setting(reg, key, parts[1].lower(), parts[2])
        except ValueError as e:
            return f"nope: {e}\n\n{_set_help(reg, key, parts[1].lower())}"
    return "commands: /status, /reload, /stop, /set help"


# --- voice transcription (Deepgram) -------------------------------------------
def deepgram_transcribe(audio, mime="audio/ogg"):
    """Transcribe voice-note bytes via Deepgram's prerecorded API. Blocking (urllib) —
    call from an executor. Returns the transcript, or None on any failure (no key,
    network, empty). Telegram voice notes are OGG/Opus."""
    key = _env_value("DEEPGRAM_API_KEY")
    if not key:
        return None
    url = ("https://api.deepgram.com/v1/listen"
           "?model=nova-3&smart_format=true&detect_language=true")
    req = urllib.request.Request(url, data=audio, method="POST",
                                 headers={"Authorization": f"Token {key}",
                                          "Content-Type": mime})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
        text = data["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
        return text or None
    except Exception as e:
        log(f"deepgram error: {type(e).__name__}: {str(e)[:120]}")
        return None


# --- workers ------------------------------------------------------------------
# Each worker turns (tail, state) into a normalized dict:
#   {"reply": <text>, "silent": <optional bool>, "meta": {harness, model, is_error,
#     tokens:{input,output,cache_read,cache_write}, cost_usd, duration_ms, session_id}}
# so the dispatch loop stays harness-blind and logs token/cost metadata uniformly.
# `state` is the daemon-assembled channel state (time, channel/harness, participants,
# settings, context-window size, previous-turn usage) — assembled in run_worker.
def now_display():
    """Human time for the state block: UTC always, plus Tallinn local best-effort."""
    utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        loc = utc.astimezone(ZoneInfo("Europe/Tallinn"))
        return f"{utc.isoformat(timespec='seconds')} (UTC) / {loc.strftime('%H:%M')} Tallinn"
    except Exception:
        return f"{utc.isoformat(timespec='seconds')} (UTC)"


def _message_display_time(message):
    """Return one compact Tallinn calendar stamp for a Telegram message."""
    value = getattr(message, "date", None)
    if not isinstance(value, datetime):
        return None, None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        value = value.astimezone(ZoneInfo("Europe/Tallinn"))
    except Exception:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d"), value.strftime("%H:%M")


def _reply_to_message_id(message):
    value = getattr(message, "reply_to_msg_id", None)
    if value is None:
        value = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


CHANNEL_TOPIC_MARKER = "#topic:"
GENERAL_TOPIC_ID = 1
# Two agents share one room, and each side runs a daemon that answers on its
# own. A request tagged EXTERNAL says its answer is consumed by a live session
# rather than by the peer's daemon, so the daemon stamps NO_REPLY on everything
# it sends for that request and the peer's daemon stays out of the exchange.
# The daemon decides both, never the model: a convention the worker has to
# remember is a convention that gets forgotten mid-conversation.
EXTERNAL_REQUEST_MARKER = "#external"
NO_REPLY_MARKER = "#noreply"


def _message_topic_id(message):
    """Canonical forum-topic root id, or None for ordinary chat messages."""
    reply = getattr(message, "reply_to", None)
    # Telegram marks every message inside a forum topic with forum_topic. The
    # general topic leaves the flag unset and carries reply_to_top_id with the
    # root of a plain reply chain, so the flag alone separates a topic from an
    # ordinary threaded conversation.
    is_topic = bool(
        getattr(message, "forum_topic", False)
        or getattr(reply, "forum_topic", False)
    )
    if not is_topic:
        return _general_topic_id(message)
    for value in (
        getattr(message, "reply_to_top_id", None),
        getattr(reply, "reply_to_top_id", None),
        getattr(message, "topic_id", None),
    ):
        try:
            if value is not None and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            pass
    # A topic's root service message and a direct post into a topic expose only
    # reply_to_msg_id / the message id rather than reply_to_top_id.
    for value in (
        getattr(message, "reply_to_msg_id", None),
        getattr(reply, "reply_to_msg_id", None),
        getattr(message, "id", None),
    ):
        try:
            if value is not None and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            pass
    return None


def _general_topic_id(message):
    """General as a topic id, for a chat that asked for its rooms by name.

    Telegram lists General as topic 1, but the wire marks nothing: its messages
    carry no forum_topic flag and arrive looking like ordinary chat messages.
    A chat that declared a topics map asked for its rooms to be addressable, and
    General is one of them — without this it is the only room in a forum that can
    be neither routed nor given its own prose.

    A chat that declared no map keeps General on the bare chat key, which is
    where it has always lived and where a plain group's messages belong.
    """
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        return None
    _, policy = _group_policy(chat_id)
    if _as_mapping(_as_mapping(policy).get("topics")):
        return GENERAL_TOPIC_ID
    return None


def _marker_line(text, marker):
    """Whether a message carries a protocol tag, which is its last line alone.

    Position is what separates the tag from a conversation about it. Agents
    discuss these tokens — this repository's own guide names them — and an
    inline match would let a message explaining the protocol silence the daemon
    it explains.
    """
    lines = [line.strip() for line in str(text or "").strip().splitlines()]
    return bool(lines) and lines[-1] == marker


def _with_marker(text, marker):
    """One outgoing message with its tag on the last line of its own."""
    body = str(text or "").rstrip()
    return f"{body}\n\n{marker}" if body else marker


def _message_topic_title(message):
    action = getattr(message, "action", None)
    value = getattr(action, "title", None) or getattr(message, "topic_title", None)
    text = str(value or "").strip()
    return text or None


def _channel_key(chat_id, topic_id=None):
    base = str(chat_id)
    return f"{base}{CHANNEL_TOPIC_MARKER}{int(topic_id)}" if topic_id is not None else base


def _channel_identity(key):
    raw = str(key)
    if CHANNEL_TOPIC_MARKER not in raw:
        return raw, None
    chat_id, topic = raw.rsplit(CHANNEL_TOPIC_MARKER, 1)
    try:
        return chat_id, int(topic)
    except (TypeError, ValueError):
        return raw, None


def _format_conversation(tail):
    """Render a compact readable timeline without JSON property overhead."""
    lines = [
        ("Reply markers define conversational relationships. Message proximity alone "
         "does not mean that a message addresses Marvin."),
    ]
    current_date = None
    for message in tail:
        message_date = message.get("date")
        if message_date and message_date != current_date:
            lines.append(f"--- {message_date} ---")
            current_date = message_date
        identity = " ".join(
            part for part in (
                message.get("time"),
                f"#{message['id']}" if message.get("id") else None,
            ) if part)
        reply = message.get("in_reply_to") or {}
        reply_marker = ""
        if reply.get("id") and reply.get("sender"):
            target = reply["sender"] + (" (assistant)" if reply.get("is_assistant") else "")
            reply_marker = f" | reply to #{reply['id']} by {target}"
        metadata = f"[{identity}{reply_marker}] " if identity or reply_marker else ""
        lines.append(f'{metadata}{message["sender"]}: {message["text"]}')
    return "\n".join(lines)


def _settings_summary(s):
    bits = [
        f"tail_size={s.get('tail_size')}",
        f"worker={s.get('worker')}",
        f"model={s.get('model') or 'default'}",
        f"debounce={s.get('debounce')}s",
        f"timeout={s.get('worker_timeout')}s",
        f"progress_after={s.get('progress_after')}s",
        f"parallel={s.get('max_parallel_jobs')}",
        f"max_attempts={s.get('max_attempts')}",
    ]
    if s.get("worker") == "claude":
        bits.append(f"effort={s.get('effort') or 'default'}")
    elif s.get("worker") == "codex":
        bits.append(f"reasoning={s.get('reasoning_effort') or 'default'}")
        bits.append(f"service_tier={s.get('service_tier') or 'default'}")
    return ", ".join(bits)


def _service_context_path(value):
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = SERVICE_DIR / path
    try:
        resolved = path.resolve()
        service_root = SERVICE_DIR.resolve()
    except OSError:
        return path
    if resolved == service_root or service_root in resolved.parents:
        return resolved
    return None


def _channel_context_from_policy(policy):
    if not isinstance(policy, dict):
        return ""
    parts = []
    inline = str(policy.get("context") or "").strip()
    if inline:
        parts.append(inline)
    context_file = policy.get("context_file")
    path = _service_context_path(context_file)
    if context_file and path is None:
        parts.append(f"[channel context file ignored: {context_file}]")
    elif (doc := _service_document(path) if path else None) is not None:
        # Held apart deliberately: a document that exists and says nothing is
        # not the same as one the project does not have, and only the second
        # is worth telling the room about.
        if (doc["body"] or "").strip():
            parts.append(doc["body"].strip())
    elif context_file:
        parts.append(f"[channel context file missing: {context_file}]")
    return "\n\n".join(parts).strip()


def _context_mode(policy):
    return str(_as_mapping(policy).get("context_mode") or "extend").strip().lower()


def _channel_context(policies):
    """Channel prose in the order it was authored, and whether it stands alone.

    The room speaks first and the topic after it, so a topic adds what is
    specific to its lane rather than restating the room. A level that declares
    itself exclusive cuts every layer above it — the room's prose and the
    service context with it — because a lane that has to be told the room's
    rules is not the lane the author asked for. It never cuts below itself: a
    room that claims its own prose still lets its topics add theirs.
    """
    parts = []
    exclusive = False
    for policy in policies:
        if _context_mode(policy) == "exclusive":
            parts = []
            exclusive = True
        text = _channel_context_from_policy(policy)
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip(), exclusive


def resumed_prompt(st):
    """The next turn inside a thread the worker is already holding.

    Everything the full prompt establishes — the soft gate, who is on the other
    end, the active settings, how to report progress — that thread already
    carries. Restating it every turn would grow the thread for nothing and invite
    the model to re-argue instructions it has already accepted, which is most of
    what resuming was meant to avoid. Only what has actually moved on since the
    last turn is worth sending: the clock, and the new request."""
    req = st.get("current_request") or {}
    lines = []
    if st.get("now"):
        lines.append(f"Time: {st['now']}")
    if req.get("delivery"):
        lines.append(f"Delivery: {req['delivery']}")
    if lines:
        lines.append("")
    lines.append(req.get("text") or "")
    return "\n".join(lines)


def build_prompt(tail, state=None):
    """Assemble the worker prompt: soft-gate context (context.md) + the daemon-resolved channel
    state (time, channel/harness, participants + roles, active settings, context-window size,
    previous-turn token usage) + the live tail. State is assembled here and passed in, so the
    worker reads its situation from the context, not by inferring it from chat history."""
    st = state or {}
    if st.get("resume_session") and not st.get("resume_reanchor"):
        return resumed_prompt(st)
    # An exclusive channel answers with its own prose alone. The service context
    # goes with the room's, which is the point of the mode and the cost of it.
    context = ("" if st.get("context_exclusive")
               else read_service_document(CONTEXT_FILE))
    channel_context = (st.get("channel_context") or "").strip()
    progress_command = None
    if st.get("chat_id") is not None:
        progress_command = (
            f'{WORKER_BIN / "telegram"} send {st["chat_id"]} "<one short line>"')
        # Prose that names the progress command in either spelling is rewritten
        # to the shim path, wherever the prose came from.
        context, channel_context = (
            text.replace("{{TELEGRAM_PROGRESS_COMMAND}}", progress_command).replace(
                "telegram send <chat_id> <text>", progress_command)
            for text in (context, channel_context))
    if channel_context:
        channel_context = "--- Channel-specific context ---\n" + channel_context + "\n\n"
    lines = []
    if st.get("now"):
        lines.append(f"Time: {st['now']}")
    if st.get("chat_id"):
        channel_bits = [
            f"chat_id={st['chat_id']}",
            f"type={st.get('chat_type') or 'private'}",
            f"connection={st.get('connection')}",
            f"harness={st.get('harness')}",
        ]
        if st.get("chat_name"):
            channel_bits.append(f"name={st['chat_name']}")
        lines.append("Channel: " + ", ".join(channel_bits))
    if st.get("participants"):
        lines.append("Counterpart(s): " + ", ".join(
            f'{p["name"]} (role: {p["role"]})' for p in st["participants"]))
    s = st.get("settings") or {}
    if s:
        lines.append(f"Settings: {_settings_summary(s)}")
    if st.get("authority"):
        lines.append(f"Tool authority: {_authority_summary(st['authority'])}")
    dialogue = st.get("agent_dialogue")
    if dialogue:
        reset = "; a human message resets the counter" if dialogue.get("reset_on_human_message") else ""
        lines.append(
            f"Agent dialogue: turn {dialogue['turns']}/{dialogue['max_turns']}{reset}. "
            "A reply relationship from an agent is context only; another turn requires an explicit name or mention."
        )
    peers = st.get("agent_peers") or []
    if peers:
        rendered = []
        for peer in peers:
            aliases = ", ".join(peer.get("aliases") or [])
            rendered.append(f"{peer['name']} (address as: {aliases})" if aliases else peer["name"])
        lines.append(
            "Agent peers: " + "; ".join(rendered)
            + ". Start a response with a peer's address only when deliberately handing it the next turn."
        )
    if st.get("messages") is not None:
        ctx = f"Context window: {st['messages']} msgs (of max {s.get('tail_size', '?')})"
        if st.get("history_chars") is not None:
            ctx += f", ~{st['history_chars']} chars of history"
        lines.append(ctx)
    req = st.get("current_request") or {}
    if req:
        lines.append("Delivery: " + (
            req.get("delivery")
            or ("final reply is sent by the daemon "
                + ("as a reply to the request message"
                   if req.get("reply_to") else "as a plain direct message"))))
    pu = st.get("prev_usage")
    if pu:
        lines.append(f"Previous turn (rough context scale): input ~{pu.get('input')} tok., "
                     f"output {pu.get('output')} tok.")
    if progress_command and progress_command not in context \
            and progress_command not in channel_context:
        lines.append(
            f"Progress: for work longer than about 15 seconds, send one short line "
            f"with {progress_command} before going deep.")
    block = ("--- Channel state ---\n" + "\n".join(lines) + "\n\n") if lines else ""
    request_block = ""
    if req:
        request_lines = [
            "--- Current request ---",
            f"Message: #{req.get('message_id')}",
            f"From: {req.get('sender_name')} (role: {req.get('sender_role')})",
            f"Kind: {req.get('kind')}",
        ]
        reply = req.get("in_reply_to") or {}
        if reply.get("id") and reply.get("sender"):
            target = reply["sender"] + (" (assistant)" if reply.get("is_assistant") else "")
            request_lines.append(f"Reply to: #{reply['id']} by {target}")
        request_lines.extend([
            "Answer this request only. Other addressed messages in the tail are separate jobs.",
            req.get("text") or "",
            "",
        ])
        request_block = "\n".join(request_lines)
    history = _format_conversation(tail)
    return f"{context}\n\n{channel_context}{block}{request_block}--- Conversation ---\n{history}"


def message_tail_text(m):
    """Worker-visible text for one message: its text/caption, plus a marker for any attachment so a
    media message never silently drops from the tail. The marker carries what the worker needs to
    fetch the file on demand (filename + msg id) via `telegram download` — the daemon does not
    download attachments; the file is pulled lazily by whoever actually needs it. Voice is excluded:
    it is transcribed and echoed separately, so its echo already carries the words."""
    text = m.text or ""
    if m.file and not m.voice:
        if m.file.name:
            name = m.file.name
        elif m.photo:
            name = f"photo-{m.id}.jpg"
        else:
            name = f"file-{m.id}"
        marker = f"[attachment: {name} | msg {m.id}]"
        text = f"{text}\n{marker}" if text else marker
    return text or None


def _safe_file_part(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "chat"


def queue_call_recording_request(chat_id, message_id, profile):
    CALL_RECORDING_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "requested_at": now(),
        "requested_by": {
            "user_id": str((profile or {}).get("id") or ""),
            "name": (profile or {}).get("name"),
            "role": (profile or {}).get("role"),
        },
    }
    name = f"{_safe_file_part(chat_id)}-{int(message_id)}.json"
    path = CALL_RECORDING_REQUEST_DIR / name
    temporary = path.with_name(f".{name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)
    return path


def prepare_worker_session(key, message_id):
    src = Path(str(SESSION) + ".session")
    if not src.exists():
        return None
    WORKER_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    dst = WORKER_SESSION_DIR / f"{_safe_file_part(key)}-{message_id}.session"
    for suffix in ("", "-journal", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(str(dst) + suffix).unlink()
    try:
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=3) as source:
            with sqlite3.connect(dst, timeout=3) as target:
                source.backup(target)
    except sqlite3.Error:
        try:
            shutil.copy2(src, dst)
        except OSError:
            return None
    return str(dst.with_suffix(""))


def cleanup_worker_session(worker_session):
    if not worker_session:
        return
    for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
        with contextlib.suppress(OSError):
            Path(worker_session + suffix).unlink()


WORKER_ENV_DROP = ("TELEGRAM_SERVICE_LAUNCH_NONCE", "SSH_AUTH_SOCK")
WORKER_ENV_DROP_PREFIXES = ("CLAUDE_CODE_", "CLAUDECODE", "VSCODE_")
WORKER_ENV_DROP_WHEN_ROUTED = (
    "CAPABILITIES_PROJECT_ENVELOPE", "TELEGRAM_SERVICE_PROJECT_ROOT",
    "TELEGRAM_SERVICE_PROJECT_ENVELOPE", "TELEGRAM_SERVICE_PROJECT_LAYOUT",
)


def worker_env(state=None):
    st = state or {}
    env = os.environ.copy()
    # The launch nonce is the daemon's proof of ownership and the agent-channel
    # keys are a way into another editor session; neither has business in a
    # child. The forwarded ssh agent is a credential the worker never asked for.
    for name in WORKER_ENV_DROP:
        env.pop(name, None)
    for name in [key for key in env if key.startswith(WORKER_ENV_DROP_PREFIXES)]:
        env.pop(name, None)
    project_dir = st.get("project_dir")
    if project_dir is not None and Path(project_dir) != PROJECT_ROOT:
        # The launcher pins this daemon's own envelope, and a capability invoked
        # with it pointing outside its resolved project exits 6. Dropping the pin
        # and naming the target is the entire binding: every capability CLI
        # resolves its project from CLAUDE_PROJECT_DIR before it falls back to cwd.
        for name in WORKER_ENV_DROP_WHEN_ROUTED:
            env.pop(name, None)
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    real_telegram = env.get("TELEGRAM_REAL_TELEGRAM") or shutil.which("telegram")
    if real_telegram:
        env["TELEGRAM_REAL_TELEGRAM"] = real_telegram
    env["PATH"] = f"{WORKER_BIN}{os.pathsep}{env.get('PATH', '')}"
    if st.get("progress_outbox"):
        env["TELEGRAM_PROGRESS_OUTBOX"] = st["progress_outbox"]
    if st.get("worker_session"):
        env["TELEGRAM_WORKER_SESSION"] = st["worker_session"]
    if st.get("authority_context"):
        env["CAPABILITIES_AUTH_CONTEXT"] = st["authority_context"]
    if st.get("chat_type"):
        env["TELEGRAM_PROGRESS_CHAT_TYPE"] = st["chat_type"]
    if st.get("chat_id") is not None:
        env["TELEGRAM_AUTHORIZED_CHAT_ID"] = str(st["chat_id"])
    if st.get("topic_id") is not None:
        env["TELEGRAM_AUTHORIZED_TOPIC_ID"] = str(st["topic_id"])
    else:
        env.pop("TELEGRAM_AUTHORIZED_TOPIC_ID", None)
    if st.get("connection") is not None:
        env["TELEGRAM_AUTHORIZED_CONNECTION"] = str(st["connection"])
    req = st.get("current_request") or {}
    if req.get("reply_to"):
        env["TELEGRAM_PROGRESS_REPLY_TO"] = str(req["reply_to"])
    return env


def write_authority_context(authority, stem):
    """Write one request authority envelope owner-only and without a reusable
    name. The path is handed to a child process, then unlinked by its owner."""
    AUTHORITY_DIR.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        prefix=f"{_safe_file_part(stem)}-", suffix=".json", dir=AUTHORITY_DIR)
    path = Path(raw_path)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(
                json.dumps(authority, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n")
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return str(path)


CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


# What a call may open. An allowed list rather than a forbidden one: the project
# holds credentials, sessions and bank material, and a caller on a phone must not
# be one clever path away from any of it.
PROJECT_READ_LAYERS = ("context", "routines", "assets", "memory", "capabilities", "deployment")
# And inside those, what is never opened however it is reached.
PROJECT_READ_DENY_NAMES = (
    ".env", ".netrc", "connections.json", "credentials.json", "secrets.json",
)
PROJECT_READ_DENY_PARTS = (
    "state", ".state", "credential", "credentials", "secret", "secrets",
    "token", "tokens", ".git", ".env", ".env.local",
)
PROJECT_READ_DENY_SUFFIXES = (".session", ".key", ".pem", ".p12", ".sqlite", ".db")


def resolve_project_file(wanted):
    """A project-relative path, or a refusal saying why. Resolved before it is
    judged, so a path that climbs out with '..' or follows a link is caught by
    where it lands rather than by how it was spelled."""
    raw = str(wanted or "").strip()
    if not raw:
        return None, "no_path"
    try:
        root = PROJECT_ROOT.resolve()
        candidate = Path(raw)
        # `refs` and `ids` hand out absolute paths while the project body lists
        # relative ones. Both name the same file, so both are accepted — an
        # absolute path is still judged by where it lands, not by its form.
        path = (candidate if candidate.is_absolute() else PROJECT_ROOT / raw).resolve()
        path.relative_to(root)
    except (ValueError, OSError):
        return None, "outside_project"

    matched_root = None
    for name in PROJECT_READ_LAYERS:
        value = PROJECT_LAYOUT.get(name)
        if not value:
            continue
        layer_root = Path(value).resolve()
        try:
            path.relative_to(layer_root)
            matched_root = layer_root
            break
        except ValueError:
            continue
    if matched_root is None:
        return None, "not_readable_here"
    parts = path.relative_to(matched_root).parts
    if not parts:
        return None, "not_readable_here"
    lowered = [p.lower() for p in parts]
    if any(p in PROJECT_READ_DENY_PARTS for p in lowered):
        return None, "not_readable_here"
    if any(lowered[-1] == n or lowered[-1].startswith(n + ".")
           for n in PROJECT_READ_DENY_NAMES):
        return None, "not_readable_here"
    if path.suffix.lower() in PROJECT_READ_DENY_SUFFIXES:
        return None, "not_readable_here"
    return path, None


def capability_allowed(authority, capability):
    """Whether this caller's authority reaches that capability. No authority
    declared at all leaves the CLI's own gate as the only one, which is the
    same answer the worker path gives."""
    if authority is None:
        return True
    caps = authority.get("allowed_capabilities") or {}
    if caps.get("*") is True:
        return True
    rule = caps.get(capability)
    if rule is True:
        return True
    return isinstance(rule, dict) and rule.get("allow") is not False


def truncate_capability_output(text, limit):
    """Bound what reaches the model, and say so where it was cut: a silent
    truncation reads as a complete answer, which is how a call ends up stating
    half a list as the whole of it."""
    body = text or ""
    if len(body) <= limit:
        return body, False
    return body[:limit] + "\n…[cut]", True


# The contract verbs every capability answers. On a call they pass the primer
# untouched: reaching for one is already the behaviour the primer exists to
# produce, and charging a round trip for asking what a tool is would teach the
# opposite of what is wanted.
CONTRACT_VERBS = ("help", "guide", "ids", "connections", "refs", "stub", "manifest")


def voice_capability_step(args, capability, helped):
    """What to do with a call's next capability command: `help` when the model
    asks for the one verb that says how a tool is called, `contract` for the
    other verbs, `prime` when a real command arrives at a tool nobody has read
    yet, and `run` once it has been read.

    Whole rather than split across a closure, because the rule it carries is
    easy to get subtly wrong: only help stands in for help. A call that opened
    with `guide` has been told what a tool is *for* and still has every flag left
    to guess — and it guessed, twice, on the caller's time."""
    first = args[0] if args else None
    if first == "help":
        return "help"
    if first in CONTRACT_VERBS:
        return "contract"
    if capability not in helped:
        return "prime"
    return "run"


def read_project_file_result(wanted):
    """Resolve, read and bound one project file, as the answer the model gets.
    Whole rather than split across a closure: the reading is the part that can
    be wrong, so it is the part a test has to be able to reach."""
    path, refusal = resolve_project_file(wanted)
    if refusal == "no_path":
        return {"ok": False, "status": refusal,
                "instruction": "Name the file, relative to the project."}
    if refusal is not None:
        return {"ok": False, "status": refusal,
                "instruction": "That part of the project is not open to a call. "
                               "Say you cannot see it; do not look for another "
                               "way in."}
    if not path.is_file():
        return {"ok": False, "status": "not_found",
                "instruction": "There is no such file. Say so rather than guessing "
                               "at what it would have said."}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {"ok": False, "status": "unreadable",
                "instruction": "That file would not open. Say you could not read it."}
    body, truncated = truncate_capability_output(
        text, voice_agent.CAPABILITY_OUTPUT_LIMIT)
    result = {"ok": True, "status": "ok",
              "path": str(path.relative_to(PROJECT_ROOT.resolve())),
              "text": body, "truncated": truncated}
    if truncated:
        result["instruction"] = ("Only the start of the file is here. Reading it "
                                 "again returns the same start; if the rest matters, "
                                 "hand the question to agent_task.")
    return result


async def _read_bounded_stream(stream, limit, *, tail=False):
    """Drain a child pipe completely while retaining only a fixed amount."""
    kept = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if tail:
            kept.extend(chunk)
            del kept[:-limit]
        elif len(kept) < limit:
            kept.extend(chunk[:limit - len(kept)])
    return bytes(kept).decode(errors="replace")


async def run_capability_process(binary, args, env, timeout):
    """The subprocess half on its own: no shell, its own process group, and a
    hard ceiling that kills the group rather than letting the call hang on it.
    Both pipes are drained, but only bounded slices are retained in memory."""
    proc = await asyncio.create_subprocess_exec(
        binary, *args, stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, start_new_session=True, cwd=str(PROJECT_ROOT))
    stdout_task = asyncio.create_task(_read_bounded_stream(
        proc.stdout, voice_agent.CAPABILITY_OUTPUT_LIMIT + 1))
    stderr_task = asyncio.create_task(_read_bounded_stream(proc.stderr, 4096, tail=True))
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_process_group(proc)
        await proc.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return None, None, None
    out, err = await asyncio.gather(stdout_task, stderr_task)
    return proc.returncode, out, err


def _kill_process_group(proc):
    """Kill the process group created for a worker, even if its leader already exited."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False


def _stream_worker_proc(proc, on_line):
    """Drain both pipes concurrently, handing stdout lines over as they arrive.

    Reading one pipe to completion before the other deadlocks as soon as the
    unread one fills, so each gets its own thread."""
    collected = {"out": [], "err": []}

    def pump(stream, key, callback):
        try:
            for line in stream:
                collected[key].append(line)
                if callback is not None:
                    try:
                        callback(line)
                    except Exception:
                        pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, "out", on_line), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, "err", None), daemon=True),
    ]
    for t in threads:
        t.start()
    proc.wait()
    for t in threads:
        t.join(timeout=5)
    return "".join(collected["out"]), "".join(collected["err"])


def run_worker_proc(chat, cmd, procs, env=None, cancel_event=None, on_line=None,
                    cwd=None):
    """Run a worker subprocess in its own process group (start_new_session) and register it in
    the caller's `procs` map until the async job finalizes, so /stop can SIGKILL the whole group —
    claude/codex spawn children, so killing the group, not just the lone parent, is what stops
    the run. Returns (returncode, stdout, stderr); a killed run comes back with a negative
    returncode, which the caller raises on like any nonzero exit. With `on_line`, stdout is
    handed over line by line while the run is still going, for a caller that cannot wait for
    the end of it. `cwd` is the routed project for this request; without one the run
    happens in the daemon's own project."""
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("worker cancelled before process start")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True,
                            cwd=str(cwd or PROJECT_ROOT), env=env)
    procs[chat] = proc
    # Cancellation can race with Popen. Register first, then honor a cancellation
    # that arrived while the process was being created so no late process escapes.
    if cancel_event is not None and cancel_event.is_set():
        _kill_process_group(proc)
    if on_line is not None:
        out, err = _stream_worker_proc(proc, on_line)
    else:
        out, err = proc.communicate()
    return proc.returncode, out, err


def worker_stub(chat, tail, state=None, procs=None):
    last = tail[-1]["text"] if tail else ""
    reply = f"[harness-stub] tail {len(tail)} msgs, last: «{last[:80]}»."
    return {"reply": reply, "meta": {"harness": "stub", "model": None, "is_error": False,
                                     "tokens": {}, "cost_usd": None, "duration_ms": None,
                                     "session_id": None}}


def worker_claude(chat, tail, state=None, procs=None):
    """Headless `claude -p`. --output-format json carries the reply (.result) plus
    usage / cost / model / session metadata in one object. --dangerously-skip-permissions gives
    full tool access (this is the isolated agent box, mirroring the codex worker); the behavioural
    boundary is the soft-gate in context.md, not a permission gate."""
    cmd = ["claude", "-p", build_prompt(tail, state), "--output-format", "json",
           "--dangerously-skip-permissions"]
    resume_session = (state or {}).get("resume_session")
    if resume_session:
        cmd += ["--resume", str(resume_session)]
    model = ((state or {}).get("settings") or {}).get("model")
    if model:
        cmd += ["--model", model]
    effort = ((state or {}).get("settings") or {}).get("effort")
    if effort:
        cmd += ["--effort", effort]
    proc_key = ((state or {}).get("proc_key") or chat)
    rc, out, err = run_worker_proc(
        proc_key, cmd, procs, env=worker_env(state),
        cancel_event=(state or {}).get("cancel_event"),
        cwd=(state or {}).get("project_dir"))
    if rc != 0:
        detail = (err.strip() or out.strip() or f"exit {rc}")[:500]
        raise RuntimeError(f"claude worker failed: {detail}")
    obj = json.loads(out)
    reply = (obj.get("result") or "").strip()
    if obj.get("is_error") or not reply:
        raise RuntimeError(f"claude worker error: {str(obj.get('subtype') or obj.get('result'))[:160]}")
    u = obj.get("usage") or {}
    meta = {"harness": "claude",
            "model": next(iter(obj.get("modelUsage") or {}), None) or model,
            "is_error": False,
            "tokens": {"input": u.get("input_tokens"), "output": u.get("output_tokens"),
                       "cache_read": u.get("cache_read_input_tokens"),
                       "cache_write": u.get("cache_creation_input_tokens")},
            "cost_usd": obj.get("total_cost_usd"),
            "duration_ms": obj.get("duration_ms"),
            "session_id": obj.get("session_id")}
    return {"reply": reply, "meta": meta}


def _codex_meta(stdout, model):
    """Pull usage + thread id out of codex's --json JSONL event stream (codex gives no USD
    cost under ChatGPT auth → cost_usd stays null)."""
    usage, thread_id = {}, None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "turn.completed":
            usage = o.get("usage") or {}
        elif o.get("type") == "thread.started":
            thread_id = o.get("thread_id")
    return {"harness": "codex", "model": model, "is_error": False,
            "tokens": {"input": usage.get("input_tokens"), "output": usage.get("output_tokens"),
                       "cache_read": usage.get("cached_input_tokens"), "cache_write": None},
            "cost_usd": None, "duration_ms": None, "session_id": thread_id}


def _codex_turn_completed(stdout):
    """True only when Codex's JSONL protocol confirms a successful turn end."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            return True
    return False


def discover_codex_images(thread_id):
    """Discover Codex-generated images for a thread, returning valid local paths.
    
    Codex saves images to ~/.codex/generated_images/<thread-id>/. Return only
    regular files with supported extensions inside that exact directory, capped
    at Telegram's album limit (10).
    """
    if not thread_id:
        return []
    codex_home = Path.home() / ".codex" / "generated_images"
    thread_dir = codex_home / str(thread_id)
    if not thread_dir.is_dir():
        return []
    try:
        resolved_thread = thread_dir.resolve()
        resolved_codex = codex_home.resolve()
    except OSError:
        return []
    if resolved_codex not in resolved_thread.parents and resolved_thread != resolved_codex:
        return []
    
    supported_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    images = []
    try:
        for item in sorted(thread_dir.iterdir()):
            if not item.is_file():
                continue
            if item.suffix.lower() not in supported_exts:
                continue
            try:
                if item.stat().st_size == 0:
                    continue
            except OSError:
                continue
            images.append(item)
            if len(images) >= 10:
                break
    except OSError:
        pass
    return images


def worker_codex(chat, tail, state=None, procs=None):
    """Headless `codex exec`. Full access (bypass
    approvals+sandbox) mirrors the claude worker's; --skip-git-repo-check because /app is not
    a git repo. The final message comes from -o; --json carries usage metadata on stdout.

    With `resume_session` the run continues that thread instead of opening one.
    The `resume` subcommand takes the same flags with a single exception: it has
    no --color, and passing it exits before the model is ever reached. With --json
    on a pipe there is nothing to colourise anyway."""
    fd, out = tempfile.mkstemp(prefix="tg-codex-", suffix=".txt")
    os.close(fd)
    try:
        resume_session = (state or {}).get("resume_session")
        if resume_session:
            cmd = ["codex", "exec", "resume", str(resume_session),
                   build_prompt(tail, state),
                   "--dangerously-bypass-approvals-and-sandbox",
                   "--skip-git-repo-check", "--json", "-o", out]
        else:
            cmd = ["codex", "exec", build_prompt(tail, state),
                   "--dangerously-bypass-approvals-and-sandbox",
                   "--skip-git-repo-check", "--json", "--color", "never", "-o", out]
        model = ((state or {}).get("settings") or {}).get("model")
        if model:
            cmd += ["-m", model]
        reasoning = ((state or {}).get("settings") or {}).get("reasoning_effort")
        if reasoning:
            cmd += ["-c", f'model_reasoning_effort="{reasoning}"']
        service_tier = ((state or {}).get("settings") or {}).get("service_tier")
        if service_tier:
            cmd += ["-c", f'service_tier="{service_tier}"']
        proc_key = ((state or {}).get("proc_key") or chat)
        rc, stdout_txt, err = run_worker_proc(
            proc_key, cmd, procs, env=worker_env(state),
            cwd=(state or {}).get("project_dir"),
            cancel_event=(state or {}).get("cancel_event"),
            on_line=(state or {}).get("on_worker_line"))
        if rc != 0:
            raise RuntimeError(f"codex worker failed: {err.strip()[:200]}")
        reply = Path(out).read_text().strip()
        if not reply:
            if _codex_turn_completed(stdout_txt):
                return {
                    "reply": "",
                    "silent": True,
                    "meta": _codex_meta(stdout_txt, model),
                }
            raise RuntimeError("codex worker produced no final message")
        return {"reply": reply, "meta": _codex_meta(stdout_txt, model)}
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


WORKERS = {"stub": worker_stub, "claude": worker_claude, "codex": worker_codex}


class WorkerLane:
    """The worker session a caller keeps, and the decisions about it.

    A caller rarely wants one thing. The next task builds on what the last one
    found, and a fresh worker thread each time throws that away: the caller waits
    on the line through the same connection, the same lookup, the same id, before
    anything new can begin. A lane remembers the thread a task ran in and offers
    it to the next one — across calls, not only within one, so hanging up and
    ringing back lands in the conversation that was already going.

    Nothing expires it. Not an age, not a turn count, not a restart. Codex
    summarizes a thread by itself once it fills its context window, so length
    takes care of itself, and the only way to a new session is asking for one.

    It keeps state and decides; it never runs anything. Execution stays with the
    caller, which owns the process, its timeout and its cancellation — so a lane
    can be reasoned about, and tested, without a daemon around it.
    """

    def __init__(self):
        self.session_id = None
        self.turns = 0
        # Set when the worker reports summarizing the thread to fit. The next
        # turn then re-states the operating context in full: compaction is free
        # to summarize away the instructions the session was opened with, and a
        # thread meant to live forever cannot be left drifting from them.
        self.needs_reanchor = False
        # One row per session this call touched, for the call's own metadata.
        # The carried-over session is seeded in as the first row.
        self.sessions = []

    def classify(self, harness, resumed, thread_started):
        """What a failed attempt means for the session.

        The whole discriminator is whether a thread ever opened. A resume that
        never opened one executed nothing, so the same task can be run again in a
        fresh session and the caller need never know. Anything else may already
        have created a record, sent a message, or moved money, and running it a
        second time would do that twice.

        The harness matters because only codex reports the event: claude's output
        arrives as one object at the end and there is no live signal at all, so
        every claude failure is treated as the dangerous kind. That is the
        conservative reading, and it costs nothing — a retry is a convenience,
        never a correctness requirement."""
        if resumed and harness == "codex" and not thread_started:
            return "session_lost"
        return "task_failed"

    def refresh(self, record):
        """Take up whatever session is on record now.

        Read before every task rather than once when the call is answered. A task
        from the previous call can finish and pin its session seconds after the
        next call is already up — which is precisely when someone hangs up and
        rings straight back to carry on. A lane fixed at pickup would not see it,
        and the caller would get a worker that knows nothing about what they were
        just talking about."""
        session_id = (record or {}).get("session_id")
        if not session_id or str(session_id) == self.session_id:
            return
        self.session_id = str(session_id)
        self.turns = int(record.get("turns") or 0)
        self.needs_reanchor = bool(record.get("needs_reanchor"))
        if not self.sessions or self.sessions[-1]["session_id"] != self.session_id:
            self.sessions.append({"session_id": self.session_id,
                                  "turns": self.turns, "closed": None})

    def open(self, session_id):
        """Take the thread the moment the worker reports opening it.

        Recorded when the work starts, not when it finishes. Between those two
        there is half a minute of real work, and a caller who rings back inside
        that window would otherwise find nothing on record and get a second
        thread that knows nothing — which is the gap this whole thing exists to
        close. A turn that then fails clears the record again, so what survives a
        failure is still nothing."""
        if not session_id or str(session_id) == self.session_id:
            return
        self.session_id = str(session_id)
        self.turns = 0
        self.sessions.append(
            {"session_id": self.session_id, "turns": 0, "closed": None})

    def pin(self, session_id):
        """Count the turn that finished, on the session it ran in.

        The thread was already taken by `open` when the worker started; this only
        records that a turn on it completed. Called on a normal worker return and
        on no other path, so the count says how many turns actually landed."""
        session_id = str(session_id) if session_id else None
        if not session_id:
            return
        if session_id != self.session_id:
            self.session_id = session_id
            self.turns = 0
        self.turns += 1
        if not self.sessions or self.sessions[-1]["session_id"] != session_id:
            self.sessions.append(
                {"session_id": session_id, "turns": 0, "closed": None})
        self.sessions[-1]["turns"] = self.turns

    def clear(self, reason):
        """Let go of the session, naming what ended it.

        Every path that stops trusting the thread comes through here, so the
        reason reaches the log and the call's metadata rather than being guessed
        at afterwards."""
        if self.session_id is not None and self.sessions:
            self.sessions[-1]["closed"] = reason
        self.session_id = None
        self.turns = 0
        self.needs_reanchor = False


_LANES_LOCK = threading.Lock()


def _read_lanes():
    if not LANES_FILE.exists():
        return {}
    try:
        stored = json.loads(LANES_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def load_lane(caller_id):
    """The lane this caller left behind, or an empty one.

    Read from disk rather than from memory, so a session outlives the call it was
    opened in — and the daemon that opened it. What is stored is a codex thread
    id, not the conversation: that lives in codex's own rollout, which is why a
    restart or a reboot changes nothing here."""
    lane = WorkerLane()
    record = _read_lanes().get(str(caller_id))
    if not isinstance(record, dict) or not record.get("session_id"):
        return lane
    lane.session_id = str(record["session_id"])
    lane.turns = int(record.get("turns") or 0)
    lane.needs_reanchor = bool(record.get("needs_reanchor"))
    lane.sessions.append({"session_id": lane.session_id, "turns": lane.turns,
                          "closed": None})
    return lane


def save_lane(caller_id, lane):
    """Write the carried session back, or drop the entry once there is none.

    Read and write are one critical section, and the file lands by rename. One
    caller's entry is written from the asyncio side when a turn settles and from
    the worker's output pump the moment a thread opens — different threads, one
    file holding every caller — so an unguarded read-modify-write could drop
    another caller's session while carrying this one."""
    key = str(caller_id)
    with _LANES_LOCK:
        lanes = _read_lanes()
        if lane is None or not lane.session_id:
            lanes.pop(key, None)
        else:
            lanes[key] = {"session_id": lane.session_id, "turns": lane.turns,
                          "needs_reanchor": lane.needs_reanchor,
                          "updated_at": iso_utc()}
        SERVICE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = LANES_FILE.with_name(
            f".{LANES_FILE.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(lanes, indent=2, ensure_ascii=False))
        os.replace(temporary, LANES_FILE)


def forget_lanes():
    """Drop every carried session. This is what `/reload` means by a new start:
    the one deliberate way to a fresh thread, since nothing else ends one."""
    with _LANES_LOCK:
        carried = len([row for row in _read_lanes().values()
                       if isinstance(row, dict) and row.get("session_id")])
        with contextlib.suppress(OSError):
            LANES_FILE.unlink()
    return carried


class NotAuthorized(Exception):
    """No usable session yet — the supervisor keeps the container alive and retries so
    the one-time `telegram login` can run inside it (the box starts before login exists)."""


class SessionUnhealthy(Exception):
    """The MTProto client is alive but no longer decoding Telegram updates reliably."""


class IdentityMismatch(Exception):
    """The durable session belongs to a different Telegram account."""


class WorkerTimedOut(Exception):
    """The worker future exceeded its configured deadline without being cancelled."""


async def run_session(client):
    await client.connect()
    if not await client.is_user_authorized():
        raise NotAuthorized(
            f"session not authorized — exec in and run: telegram login --connection {CONNECTION}")
    me = await client.get_me()
    write_health(
        expected_account_id=EXPECTED_ACCOUNT_ID,
        actual_account_id=int(me.id),
        actual_account=(getattr(me, "username", None)
                        or getattr(me, "first_name", None)),
    )
    if EXPECTED_ACCOUNT_ID is not None and int(me.id) != EXPECTED_ACCOUNT_ID:
        raise IdentityMismatch(
            f"Telegram account mismatch: expected {EXPECTED_ACCOUNT_ID}, got {me.id} "
            f"({getattr(me, 'username', None) or getattr(me, 'first_name', 'unknown')})")
    allowed_labels = [f"{v.get('name', k)}({k})" for k, v in ALLOWED.items()]
    allowed_group_labels = [
        f"{(v if isinstance(v, dict) else {}).get('name', k)}({k})"
        for k, v in ALLOWED_GROUPS.items()
    ]
    log(f"watching as {me.first_name} (id {me.id}); connection={CONNECTION}; "
        f"default_worker={DEFAULT_WORKER}")
    log(f"connection state dir: {CONNECTION_STATE_DIR}")
    log(f"service state dir: {SERVICE_STATE_DIR}")
    log(f"direct messages: mode={DIRECT_MESSAGE_MODE}; default_role={DIRECT_DEFAULT_ROLE}")
    log(f"allowed: {allowed_labels}")
    log(f"allowed groups: {allowed_group_labels}; group aliases: {list(DEFAULT_GROUP_ALIASES)}")
    reconcile_orphaned_recordings()
    routes = _route_map()
    if routes:
        log("worker routes: " + "; ".join(
            f"{route['scope']} -> {route['project']}" for route in routes))
    else:
        log(f"worker routes: none; every channel runs in {PROJECT_ROOT}")
    write_health(routes=routes)

    reg = load_register()
    if migrate_register(reg):
        save_register(reg)
    recovered = _recover_incomplete_jobs(reg)
    pruned = _prune_all_jobs(reg)
    if recovered or pruned:
        save_register(reg)
    # Per-chat concurrency is capped by max_parallel_jobs. The unit of work is an
    # addressed Telegram message, persisted in register.jobs and deduped by message id.
    busy, timers, runners = set(), {}, {}
    procs, stopping = {}, set()   # live worker per chat + chats whose worker /stop just killed
    closing = False

    def kill_worker_proc(key, reason):
        proc = procs.get(key)
        if not proc:
            return False
        try:
            killed = _kill_process_group(proc)
            if not killed:
                log(f"{key}: worker process group {proc.pid} already gone ({reason})")
                return False
            log(f"{key}: killed worker pgid {proc.pid} ({reason})")
            return True
        except PermissionError as e:
            log(f"{key}: worker kill failed ({reason}): {e}")
            return False

    def kill_all_workers(reason):
        for key in list(procs):
            kill_worker_proc(key, reason)

    def release_worker_proc(key):
        proc = procs.pop(key, None)
        if proc is not None and _kill_process_group(proc):
            log(f"{key}: killed lingering worker pgid {proc.pid} during job cleanup")

    def proc_key_for(chat_key, job):
        return f"{chat_key}:{job.get('message_id')}"

    def message_chunks(text, reserve=0):
        """Split outbound text without exceeding Telegram's message length limit.

        `reserve` holds back room for a tag appended to every chunk afterwards.
        """
        text = str(text)
        limit = max(1, TELEGRAM_MESSAGE_LIMIT - reserve)
        if len(text) <= limit:
            return [text]

        chunks = []
        while len(text) > limit:
            boundary = max(
                text.rfind("\n", 0, limit + 1),
                text.rfind(" ", 0, limit + 1),
            )
            if boundary <= 0:
                boundary = limit
            else:
                boundary += 1  # Keep the whitespace with the preceding chunk.
            chunks.append(text[:boundary])
            text = text[boundary:]
        chunks.append(text)
        return chunks

    async def send_channel_message(ent_id, text, is_direct, reply_to=None,
                                   force_reply=False, mark=None, **kwargs):
        """One path for every message the service sends, so chunking at
        Telegram's length limit happens once. A direct chat is a single thread,
        so a `reply_to` meant for a group's addressed message is dropped there —
        unless `force_reply` says the reply relationship is itself the point.

        `mark` tags every chunk rather than the last one: a peer daemon reads
        each message on its own, and an untagged tail chunk would wake it."""
        sent = None
        sent_ids = []
        threaded = reply_to is not None and (force_reply or not is_direct)
        for chunk in message_chunks(text, reserve=len(mark) + 2 if mark else 0):
            body = _with_marker(chunk, mark) if mark else chunk
            if threaded:
                sent = await client.send_message(ent_id, body, reply_to=reply_to, **kwargs)
            else:
                sent = await client.send_message(ent_id, body, **kwargs)
            sent_ids.append(sent.id)
        return sent, sent_ids

    def thread_reply_target(message, sender_id, group_policy, is_direct):
        target = _reply_target(message.id, sender_id, group_policy, is_direct)
        if not is_direct and target is None:
            target = _message_topic_id(message)
        return target

    async def handle_call_recording_request(
            key, chat_id, message, group_policy, chat_ref):
        text = _message_text(message)
        if not _is_call_recording_request(text):
            return False
        mode = _call_recording_mode(group_policy)
        if mode == "disabled" and _command_name(text) != "/record":
            return False
        job = reserve_job(
            key, message, group_policy, False, "call-recording", chat_id=chat_id)
        if job is None:
            return True
        try:
            profile = await _sender_profile(message, group_policy, direct=False)
            if mode == "on_request":
                request_path = queue_call_recording_request(chat_id, message.id, profile)
                reply = "Пробую присоединиться к активному звонку и начать запись."
                log(f"{key}: call recording requested by {profile['id']} ({request_path.name})")
            elif mode == "auto":
                reply = "Для этой группы уже включена автоматическая запись звонков."
                log(f"{key}: call recording request received; automatic mode is enabled")
            else:
                reply = "Запись звонков отключена в настройках этой группы."
                log(f"{key}: call recording request denied; group mode is disabled")
            mark_job_finished(key, job, "done")
        except Exception:
            jobs = _job_map(reg, key)
            if jobs.get(_job_id(message.id)) is job:
                jobs.pop(_job_id(message.id), None)
                save_register(reg)
            raise
        _, _ = await send_channel_message(
            chat_ref, reply, False,
            reply_to=thread_reply_target(message, profile["id"], group_policy, False))
        return True

    async def drain_progress(key, outbox, ent_id, is_direct, reply_to, offset,
                             mark=None):
        path = Path(outbox)
        if not path.exists():
            return offset
        try:
            with path.open("r", encoding="utf-8") as fh:
                fh.seek(offset)
                lines = fh.readlines()
                offset = fh.tell()
        except OSError:
            return offset
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            text = str(item.get("text") or "").strip()
            if not text or text in ("-", ".", "..."):
                continue
            _, _ = await send_channel_message(
                ent_id, text, is_direct,
                reply_to=None if is_direct else reply_to, mark=mark)
            log(f"{key}: progress job msg={reply_to or 'direct'} «{text[:80]}»")
        return offset

    async def pump_progress(key, outbox, ent_id, is_direct, reply_to, stop_event,
                            mark=None):
        offset = 0
        while not stop_event.is_set():
            offset = await drain_progress(
                key, outbox, ent_id, is_direct, reply_to, offset, mark=mark)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
        await drain_progress(
            key, outbox, ent_id, is_direct, reply_to, offset, mark=mark)

    def reserve_job(key, message, group_policy, is_direct, reason, chat_id=None):
        """Persist ownership of a message before any transcription or other await.

        The preparing state is recovered as queued on startup. This reservation is the
        idempotency boundary for both live delivery and startup catch-up.
        """
        jobs = _job_map(reg, key)
        jid = _job_id(message.id)
        if _message_is_known(reg, key, message.id):
            return None
        sender_id = str(getattr(message, "sender_id", None))
        member = (group_policy or {}).get("members", {}).get(sender_id, {})
        allowed = ALLOWED.get(sender_id, {})
        sender_name = allowed.get("name") or member.get("name") or sender_id
        sender_role = sender_role_for(sender_id, group_policy, is_direct)
        job = {
            "message_id": message.id,
            "chat_id": str(chat_id if chat_id is not None else _channel_identity(key)[0]),
            "channel_key": key,
            # Recorded on the job rather than re-read at delivery: the job is
            # what survives a restart or a retry, and the answer owes the tag
            # even when it is produced by a later attempt.
            "external_request": _marker_line(_message_text(message),
                                             EXTERNAL_REQUEST_MARKER),
            "topic_id": _message_topic_id(message),
            "topic_title": _message_topic_title(message),
            "sender_id": sender_id,
            "sender_name": sender_name,
            "sender_role": sender_role,
            "kind": _message_kind(message),
            "text": message_tail_text(message) or _message_text(message)
                    or f"[{_message_kind(message)}]",
            "status": "preparing",
            "attempts": 0,
            "enqueued_at": now(),
            "source": reason,
        }
        jobs[jid] = job
        row = _channel_row(reg, key)
        row["last_seen_message_id"] = max(message.id, row.get("last_seen_message_id", 0))
        _prune_jobs(reg, key)
        save_register(reg)
        return job

    async def finalize_job(key, message, group_policy, is_direct, reason, job,
                           text_override=None):
        if _job_map(reg, key).get(_job_id(message.id)) is not job:
            return False
        profile = await _sender_profile(message, group_policy, direct=is_direct)
        text = (text_override if text_override is not None else
                message_tail_text(message) or _message_text(message)
                or f"[{_message_kind(message)}]")
        job.update({
            "sender_id": profile["id"],
            "sender_name": profile["name"],
            "sender_role": profile["role"],
            "text": text,
            "status": "queued",
        })
        save_register(reg)
        log(f"{key}: enqueued job msg={message.id} from {profile['id']} ({reason})")
        return True

    def retry_job(key, job, error):
        if _job_map(reg, key).get(_job_id(job.get("message_id"))) is not job:
            return False
        job["status"] = "queued"
        job.pop("started_at", None)
        job.pop("finished_at", None)
        job["last_error"] = str(error)[:500]
        save_register(reg)
        return True

    def mark_job_finished(key, job, status, meta=None, error=None, reply_message_id=None):
        row = _channel_row(reg, key)
        job["status"] = status
        job["finished_at"] = now()
        if error:
            job["error"] = str(error)[:500]
        if reply_message_id:
            job["reply_message_id"] = reply_message_id
        message_id = _job_message_id(job)
        row["last_processed_message_id"] = max(message_id, row.get("last_processed_message_id", 0))
        if meta:
            tok = (meta or {}).get("tokens", {})
            row["last_usage"] = {"input": tok.get("input"), "output": tok.get("output")}
        if status in ("done", "error", "stopped"):
            _job_map(reg, key).pop(_job_id(message_id), None)
        save_register(reg)

    def admit_or_finish_agent_job(key, message, group_policy, job, reason):
        admitted, turn = _admit_agent_turn(reg, key, message, group_policy)
        if admitted:
            if turn:
                job["agent_dialogue_turn"] = turn["turns"]
                save_register(reg)
                log(f"{key}: admitted agent turn {turn['turns']}/{turn['max_turns']} "
                    f"msg={message.id} ({reason})")
            return True
        mark_job_finished(key, job, "done")
        log(f"{key}: suppressed agent msg={message.id}; dialogue limit "
            f"{turn['turns']}/{turn['max_turns']} reached ({reason})")
        return False

    async def build_tail_and_participants(key, ent_id, s, group_policy, is_direct, job):
        topic_id = job.get("topic_id")
        if topic_id is None:
            raw = await client.get_messages(ent_id, limit=s["tail_size"])
        else:
            try:
                raw = await client.get_messages(
                    ent_id, limit=s["tail_size"], reply_to=int(topic_id))
            except TypeError:
                # Test doubles and older clients may not expose reply_to; the
                # defensive filter below still guarantees isolation.
                raw = await client.get_messages(ent_id, limit=s["tail_size"] * 4)
        visible = []
        echo_senders = _voice_echo_senders(reg, key)
        for m in reversed(raw or []):
            if topic_id is not None and _message_topic_id(m) != int(topic_id):
                continue
            if not _tail_in_scope(m, group_policy):
                continue
            t = message_tail_text(m)
            if t is None:
                continue
            if m.out:
                # Check if this outgoing message is a recorded voice echo
                sender = echo_senders.get(str(m.id), ASSISTANT_NAME)
                is_assistant = str(m.id) not in echo_senders
            else:
                sender = (await _sender_profile(m, group_policy, direct=is_direct))["name"]
                is_assistant = False
            message_date, message_time = _message_display_time(m)
            visible.append({
                "message": m,
                "id": m.id,
                "sender": sender,
                "is_assistant": is_assistant,
                "text": t,
                "date": message_date,
                "time": message_time,
            })

        by_id = {row["id"]: row for row in visible}
        tail = []
        for row in visible:
            target = by_id.get(_reply_to_message_id(row["message"]))
            entry = {k: v for k, v in row.items() if k != "message"}
            if target is not None:
                entry["in_reply_to"] = {
                    "id": target["id"],
                    "sender": target["sender"],
                    "is_assistant": target["is_assistant"],
                }
            tail.append(entry)

        profiles = {}
        for m in raw or []:
            if topic_id is not None and _message_topic_id(m) != int(topic_id):
                continue
            if _incoming_in_scope(m, group_policy):
                profile = await _sender_profile(m, group_policy, direct=is_direct)
                profiles[profile["id"]] = profile
        profiles.setdefault(job.get("sender_id"), {
            "id": job.get("sender_id"),
            "name": job.get("sender_name"),
            "role": job.get("sender_role"),
        })
        participants = [{"name": p["name"], "role": p["role"]} for p in profiles.values() if p.get("name")]
        return tail, participants

    async def terminate_worker(proc_key, future, cancel_event, reason):
        """Close the cancellation/Popen race, kill the whole group, and reap the worker future."""
        cancel_event.set()
        kill_worker_proc(proc_key, reason)
        if future is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=5)
        except asyncio.TimeoutError:
            kill_worker_proc(proc_key, f"{reason}; worker future did not settle")
            log(f"{proc_key}: worker future did not settle within 5s ({reason})")
        except (Exception, asyncio.CancelledError):
            pass

    async def run_one_job(key, ent_id, job):
        s = channel_settings(reg, key)
        base_chat_id, _ = _channel_identity(key)
        _, group_policy = _group_policy(job.get("chat_id") or base_chat_id)
        is_direct = group_policy is None

        # Check max attempts cap before incrementing
        current_attempts = int(job.get("attempts") or 0)
        max_attempts = s.get("max_attempts", 3)
        if current_attempts >= max_attempts:
            log(f"{key}: job msg={job.get('message_id')} exceeded max attempts ({current_attempts}/{max_attempts}), marking as failed")
            participants = [{"name": job.get("sender_name"), "role": job.get("sender_role")}]
            error_msg = f"Failed after {current_attempts} attempts - worker process did not complete"
            await fail_job(key, ent_id, job, is_direct, participants, error_msg)
            return

        job["status"] = "running"
        job["started_at"] = now()
        job["attempts"] = current_attempts + 1
        save_register(reg)

        proc_key = proc_key_for(key, job)
        future = None
        progress_task = None
        progress_stop = None
        worker_session = None
        authority_context = None
        participants = [{"name": job.get("sender_name"), "role": job.get("sender_role")}]
        cancel_event = threading.Event()
        try:
            tail, participants = await build_tail_and_participants(
                key, ent_id, s, group_policy, is_direct, job)
            reply_msg_id = _reply_target(
                job.get("message_id"), job.get("sender_id"), group_policy, is_direct)
            delivery_reply_id = reply_msg_id
            if not is_direct and delivery_reply_id is None and job.get("topic_id") is not None:
                delivery_reply_id = int(job["topic_id"])
            current_request = {
                "message_id": job.get("message_id"),
                "sender_id": job.get("sender_id"),
                "sender_name": job.get("sender_name"),
                "sender_role": job.get("sender_role"),
                "kind": job.get("kind"),
                "text": job.get("text"),
                "reply_to": reply_msg_id,
                "delivery": _delivery_description(reply_msg_id, is_direct),
            }
            current_tail_entry = next(
                (item for item in tail if item.get("id") == job.get("message_id")), {})
            if current_tail_entry.get("in_reply_to"):
                current_request["in_reply_to"] = current_tail_entry["in_reply_to"]
            PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
            progress_outbox = PROGRESS_DIR / f"{_safe_file_part(key)}-{job.get('message_id')}.jsonl"
            with contextlib.suppress(OSError):
                progress_outbox.unlink()
            worker_session = prepare_worker_session(key, job.get("message_id"))
            authority = _authority_policy_for(job, group_policy, is_direct)
            route_value, route_label = _route_for(job, group_policy, is_direct)
            project_dir = (_route_target(route_value, route_label)
                           if route_value else PROJECT_ROOT)
            channel_context, context_exclusive = _channel_context([
                _as_mapping(ALLOWED.get(str(job.get("sender_id")))) if is_direct
                else group_policy,
                _topic_policy(group_policy, job.get("topic_id")),
            ])
            if authority is not None:
                authority_context = write_authority_context(
                    authority, f"{key}-{job.get('message_id')}")
            state = {"now": now_display(), "chat_id": job.get("chat_id") or base_chat_id,
                     "channel_key": key, "connection": CONNECTION,
                     "chat_type": "private" if is_direct else "group",
                     "chat_name": (group_policy or {}).get("name"),
                     "topic_id": job.get("topic_id"),
                     "topic_title": job.get("topic_title"),
                     "harness": s["worker"], "participants": participants, "settings": s,
                     "messages": len(tail),
                     "history_chars": sum(len(m["text"]) for m in tail),
                     "channel_context": channel_context,
                     "context_exclusive": context_exclusive,
                     "prev_usage": reg.get(key, {}).get("last_usage"),
                     "current_request": current_request,
                     "agent_dialogue": _agent_dialogue_snapshot(reg, key, group_policy),
                     "agent_peers": _agent_peers(group_policy),
                     "authority": authority,
                     "authority_context": authority_context,
                     "progress_outbox": str(progress_outbox),
                     "worker_session": worker_session,
                     "cancel_event": cancel_event,
                     "project_dir": project_dir,
                     "proc_key": proc_key}
            # Everything this job sends carries the tag, not only the final
            # answer: a progress line or an error notice naming the peer would
            # wake the daemon this exchange is trying to keep out of it.
            answer_mark = NO_REPLY_MARKER if job.get("external_request") else None
            routed = "" if project_dir == PROJECT_ROOT else f" project={project_dir}"
            log(f"{key}: dispatch job msg={job.get('message_id')} tail={s['tail_size']} "
                f"worker={s['worker']} model={s['model'] or 'default'} msgs={len(tail)}{routed}")
            loop = asyncio.get_running_loop()
            progress_stop = asyncio.Event()
            progress_task = asyncio.create_task(
                pump_progress(key, str(progress_outbox), ent_id, is_direct,
                              delivery_reply_id, progress_stop, mark=answer_mark))
            future = loop.run_in_executor(None, WORKERS[s["worker"]], key, tail, state, procs)
            async with client.action(ent_id, "typing"):
                done, _ = await asyncio.wait({future}, timeout=float(s["worker_timeout"]))
                if not done:
                    raise WorkerTimedOut
                result = await future
            if closing:
                retry_job(key, job, "session closed before worker reply was delivered")
                return
            reply, meta = result["reply"], result["meta"]
            if result.get("silent"):
                log(f"{key}: completed silently job msg={job.get('message_id')} · "
                    f"{meta.get('harness')}/{meta.get('model') or '?'}")
                mark_job_finished(key, job, "done", meta=meta)
                return
            
            # Discover and send Codex-generated images
            images = []
            if meta.get("harness") == "codex" and meta.get("session_id"):
                images = discover_codex_images(meta["session_id"])
            
            if images:
                try:
                    caption = (_with_marker(reply, answer_mark)
                               if answer_mark else reply)
                    if len(caption) <= 1024:
                        sent = await client.send_file(
                            ent_id, images, caption=caption,
                            reply_to=delivery_reply_id)
                    else:
                        sent = await client.send_file(
                            ent_id, images, reply_to=delivery_reply_id)
                        _, _ = await send_channel_message(
                            ent_id, reply, is_direct, reply_to=delivery_reply_id,
                            mark=answer_mark)
                    image_count = len(images) if isinstance(images, list) else 1
                    log(f"{key}: sent {image_count} image(s) with reply job msg={job.get('message_id')}")
                except Exception as send_err:
                    log(f"{key}: image send failed, delivering text only: {send_err}")
                    sent, _ = await send_channel_message(
                        ent_id, reply, is_direct, reply_to=delivery_reply_id,
                        mark=answer_mark)
            else:
                sent, _ = await send_channel_message(
                    ent_id, reply, is_direct, reply_to=delivery_reply_id,
                    mark=answer_mark)
            
            tok = meta.get("tokens", {})
            cost = f" ${meta['cost_usd']:.4f}" if meta.get("cost_usd") else ""
            log(f"{key}: replied job msg={job.get('message_id')} «{reply[:80]}» · "
                f"{meta.get('harness')}/{meta.get('model') or '?'}"
                f" · in={tok.get('input')} out={tok.get('output')} cache_r={tok.get('cache_read')}{cost}")
            mark_job_finished(
                key, job, "done", meta=meta, reply_message_id=getattr(sent, "id", None))
        except WorkerTimedOut:
            await terminate_worker(
                proc_key, future, cancel_event, f"timeout after {s['worker_timeout']}s")
            e = RuntimeError(f"{s['worker']} worker timed out after {s['worker_timeout']}s")
            if closing:
                retry_job(key, job, e)
                log(f"{key}: worker timed out during session close; job requeued")
                return
            await fail_job(key, ent_id, job, is_direct, participants, e)
            return
        except RouteUnavailable as e:
            log(f"{key}: route unavailable for job msg={job.get('message_id')}: {e}")
            await fail_job(key, ent_id, job, is_direct, participants, e)
            return
        except asyncio.CancelledError:
            await terminate_worker(proc_key, future, cancel_event, "job task cancelled")
            current = _job_map(reg, key).get(_job_id(job.get("message_id")))
            if current is job and job.get("status") == "running":
                retry_job(key, job, "job task cancelled before completion")
                log(f"{key}: cancelled job msg={job.get('message_id')}; requeued")
            else:
                log(f"{key}: cancelled job msg={job.get('message_id')}; already finalized")
            raise
        except Exception as e:
            await terminate_worker(proc_key, future, cancel_event, "job failed before completion")
            current = _job_map(reg, key).get(_job_id(job.get("message_id")))
            if current is not job or job.get("status") == "stopped":
                stopping.discard(key)
                log(f"{key}: run stopped by /stop")
                return
            if closing:
                retry_job(key, job, e)
                log(f"{key}: worker interrupted during session close; job requeued: {_short_error(e)}")
                return
            await fail_job(key, ent_id, job, is_direct, participants, e)
            return
        finally:
            if progress_stop is not None:
                progress_stop.set()
            if progress_task is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(progress_task, timeout=5)
            cleanup_worker_session(worker_session)
            if authority_context:
                with contextlib.suppress(OSError):
                    Path(authority_context).unlink()
            release_worker_proc(proc_key)

    async def fail_job(key, ent_id, job, is_direct, participants, error):
        log(f"{key}: worker error job msg={job.get('message_id')}: {error}")
        is_supervisor = (
            job.get("sender_role") == "supervisor"
            or any(p.get("role") == "supervisor" for p in participants)
        )
        notice = (f"Worker error:\n{error}" if is_supervisor
                  else "Something went wrong while processing this. Please tell an administrator.")
        try:
            _, group_policy = _group_policy(job.get("chat_id") or _channel_identity(key)[0])
            reply_to = _reply_target(
                job.get("message_id"), job.get("sender_id"), group_policy, is_direct)
            if not is_direct and reply_to is None and job.get("topic_id") is not None:
                reply_to = int(job["topic_id"])
            _, _ = await send_channel_message(
                ent_id, notice, is_direct, reply_to=reply_to,
                mark=NO_REPLY_MARKER if job.get("external_request") else None)
        except Exception as se:
            log(f"{key}: failed to send error notice: {se}")
        mark_job_finished(key, job, "error", error=error)

    async def run_queue(key, ent_id):
        """Per-chat queue runner. Each addressed message is its own persisted job and
        receives its own final response; the live tail is context, not the delivery unit."""
        if key in busy:
            return
        busy.add(key)
        active = {}
        try:
            while not closing:
                s = channel_settings(reg, key)
                limit = max(1, int(s.get("max_parallel_jobs") or 1))
                queued = _queued_jobs(reg, key)
                while queued and len(active) < limit:
                    job = queued.pop(0)
                    active[asyncio.create_task(run_one_job(key, ent_id, job))] = job
                if not active:
                    break
                done, _ = await asyncio.wait(
                    active, timeout=1, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    job = active.pop(task)
                    try:
                        await task
                    except asyncio.CancelledError:
                        if job.get("status") == "running":
                            retry_job(key, job, "worker task vanished from queue runner")
                        log(f"{key}: worker task cancelled msg={job.get('message_id')}")
                    except BaseException as e:
                        if job.get("status") == "running":
                            retry_job(key, job, f"worker task escaped: {_short_error(e)}")
                        log(f"{key}: worker task escaped msg={job.get('message_id')}: "
                            f"{_short_error(e)}")
        finally:
            pending = list(active.items())
            for task, _ in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*(task for task, _ in pending), return_exceptions=True)
            for _, job in pending:
                if job.get("status") == "running":
                    retry_job(key, job, "queue runner stopped before worker completion")
            busy.discard(key)

    async def debounce(key, ent_id):
        current = asyncio.current_task()
        try:
            await asyncio.sleep(channel_settings(reg, key)["debounce"])
        except asyncio.CancelledError:
            return
        finally:
            if timers.get(key) is current:
                timers.pop(key, None)
        if closing:
            return
        existing = runners.get(key)
        if existing is not None and not existing.done():
            return
        runner = asyncio.create_task(run_queue(key, ent_id))
        runners[key] = runner

        def runner_done(task):
            if runners.get(key) is task:
                runners.pop(key, None)
            if task.cancelled():
                log(f"{key}: queue runner cancelled")
            else:
                error = task.exception()
                if error is not None:
                    log(f"{key}: queue runner error: {_short_error(error)}")
            if not closing and _queued_jobs(reg, key):
                arm(key, ent_id)

        runner.add_done_callback(runner_done)

    def arm(key, ent_id):
        if key in timers:
            timers[key].cancel()
        timers[key] = asyncio.create_task(debounce(key, ent_id))

    def stop_running(key):
        """/stop: abort this chat's in-flight worker and clear queued jobs."""
        stopped = False
        for run_key, proc in list(procs.items()):
            if run_key != key and not run_key.startswith(f"{key}:"):
                continue
            if not proc:
                continue
            stopping.add(key)
            if kill_worker_proc(run_key, "/stop"):
                stopped = True
            else:
                stopping.discard(key)
        t = timers.pop(key, None)
        if t:
            t.cancel()
            stopped = True
        for job in list(_job_map(reg, key).values()):
            if job.get("status") in ("preparing", "queued", "running"):
                mark_job_finished(key, job, "stopped", error="/stop")
                stopped = True
        return "Stopped." if stopped else "Nothing is running right now."

    async def echo_voice_message(message, chat_id, key, is_direct, group_policy=None,
                                 sender_name=None):
        """Transcribe a Telegram voice note and echo the text into the chat.

        The echo is the durable, worker-visible representation of the voice note. Both live
        events and startup catch-up use this helper so a voice that arrived while the daemon was
        down cannot be silently skipped by the text-only tail renderer.

        In groups, the echo is a reply to the original voice message (Telegram's reply preview
        shows sender attribution), so no visible prefix is added.
        In DMs, "Твоё сообщение:" is prefixed because DM echoes are plain messages.
        """
        log(f"{key}: <- voice {message.id}, transcribing")
        transcript = None
        try:
            async with client.action(chat_id, "typing"):
                audio = await message.download_media(file=bytes)
                loop = asyncio.get_running_loop()
                mime = getattr(getattr(message, "file", None), "mime_type", None) or "audio/ogg"
                transcript = await loop.run_in_executor(None, deepgram_transcribe, audio, mime)
        except Exception as e:
            log(f"{key}: voice download error: {e}")
        spoken = transcript or "[голосовое — не удалось расшифровать]"
        try:
            if is_direct:
                text = f"Твоё сообщение:\n<blockquote>{html.escape(spoken)}</blockquote>"
            else:
                text = f"<blockquote>{html.escape(spoken)}</blockquote>"
            _, echo_ids = await send_channel_message(
                chat_id,
                text,
                is_direct,
                parse_mode="html",
                reply_to=thread_reply_target(
                    message, getattr(message, "sender_id", None),
                    group_policy, is_direct))
            # Record echo attribution so conversation history shows the sender, not the assistant
            if not is_direct and sender_name and echo_ids:
                _record_voice_echo_sender(reg, key, echo_ids, sender_name)
                save_register(reg)
            log(f"{key}: voice echo «{spoken[:50]}»")
        except Exception as e:
            # The job reservation remains the idempotency record. Retrying the echo on a
            # later catch-up could duplicate a message whose send succeeded remotely.
            log(f"{key}: voice echo send failed msg={message.id}: {_short_error(e)}")
        return spoken

    async def registered_chat_ref(key):
        chat_id, _ = _channel_identity(key)
        try:
            cid = int(chat_id)
        except ValueError:
            return None
        try:
            return await client.get_input_entity(cid)
        except Exception:
            pass
        try:
            async for dialog in client.iter_dialogs(limit=500):
                if str(dialog.id) == chat_id:
                    return getattr(dialog, "input_entity", None) or dialog.entity
        except Exception as e:
            log(f"{key}: dialog lookup failed: {_short_error(e)}")
        return cid

    async def catch_up_known(reason):
        # Reconcile allowed chats against their durable message-id watermarks. Startup
        # recovers downtime; the periodic pass closes gaps left by a silently dropped
        # MTProto update packet.
        tl_failures = 0
        checked = 0
        failures = 0
        total_enqueued = 0
        for key in list(reg.keys()):
            ent = await registered_chat_ref(key)
            if ent is None:
                continue
            try:
                chat_id, topic_id = _channel_identity(key)
                cid = int(chat_id)
            except ValueError:
                continue
            _, group_policy = _group_policy(chat_id)
            if cid < 0 and group_policy is None:
                continue
            try:
                checked += 1
                limit = channel_settings(reg, key)["tail_size"]
                if topic_id is None:
                    raw = await client.get_messages(ent, limit=limit)
                else:
                    try:
                        raw = await client.get_messages(ent, limit=limit, reply_to=topic_id)
                    except TypeError:
                        raw = await client.get_messages(ent, limit=limit * 4)
            except Exception as e:
                log(f"{key}: catch-up skipped; cannot resolve chat: {_short_error(e)}")
                failures += 1
                if _is_tl_layer_error(e):
                    tl_failures += 1
                continue
            enqueued = {}
            watermarks = {}
            for m in reversed(raw):
                if topic_id is not None and _message_topic_id(m) != topic_id:
                    continue
                if getattr(m, "out", False):
                    continue
                if getattr(m, "action", None) is not None:
                    continue
                message_key = _channel_key(chat_id, _message_topic_id(m))
                if _catch_up_message_is_known(
                        reg, message_key, chat_id, m.id):
                    continue
                is_direct = group_policy is None
                if (group_policy is not None and message_key in reg
                        and _reset_agent_dialogue_for_human(
                            reg, message_key, m, group_policy)):
                    save_register(reg)
                    log(f"{message_key}: human msg={m.id} reset agent dialogue during catch-up")
                addressed = False
                if group_policy is not None:
                    addressed = await _message_addresses_me(m, me, group_policy)
                    is_ambient_voice = (not addressed
                                        and _is_telegram_voice_note(m)
                                        and _voice_transcription_mode(
                                            group_policy, reg, message_key) == "auto")
                    if not addressed and not is_ambient_voice:
                        continue
                elif not _incoming_in_scope(m, group_policy):
                    continue
                else:
                    is_ambient_voice = False
                if group_policy is not None and await handle_call_recording_request(
                        message_key, cid, m, group_policy, ent):
                    continue
                if is_ambient_voice:
                    job = reserve_job(
                        message_key, m, group_policy, is_direct,
                        f"catch-up/{reason}/ambient", chat_id=chat_id)
                    if job is None:
                        continue
                    profile = await _sender_profile(m, group_policy, direct=is_direct)
                    spoken = await echo_voice_message(
                        m, ent, message_key, is_direct, group_policy,
                        sender_name=profile["name"])
                    # If transcript names the assistant, finalize and dispatch
                    if (spoken and spoken != "[голосовое — не удалось расшифровать]"
                            and _text_names_me(spoken, me, group_policy)
                            and _may_address(m.sender_id, group_policy)):
                        if not admit_or_finish_agent_job(
                                message_key, m, group_policy, job,
                                f"catch-up/{reason}/ambient-addressed"):
                            continue
                        if await finalize_job(
                                message_key, m, group_policy, is_direct,
                                f"catch-up/{reason}/ambient-addressed", job,
                                text_override=spoken):
                            watermarks.setdefault(
                                message_key,
                                reg.get(message_key, {}).get("last_processed_message_id", 0))
                            enqueued[message_key] = enqueued.get(message_key, 0) + 1
                        continue
                    # Otherwise echo-only, no worker
                    mark_job_finished(message_key, job, "done")
                    continue
                job = reserve_job(
                    message_key, m, group_policy, is_direct,
                    f"catch-up/{reason}", chat_id=chat_id)
                if job is None:
                    continue
                if not admit_or_finish_agent_job(
                        message_key, m, group_policy, job, f"catch-up/{reason}"):
                    continue
                spoken = None
                if _is_spoken_media(m):
                    profile = await _sender_profile(m, group_policy, direct=is_direct)
                    sender_name = None if is_direct else profile["name"]
                    spoken = await echo_voice_message(
                        m, ent, message_key, is_direct, group_policy,
                        sender_name=sender_name)
                if await finalize_job(
                        message_key, m, group_policy, is_direct,
                        f"catch-up/{reason}", job,
                        text_override=spoken):
                    watermarks.setdefault(
                        message_key,
                        reg.get(message_key, {}).get("last_processed_message_id", 0))
                    enqueued[message_key] = enqueued.get(message_key, 0) + 1
            if enqueued:
                total_enqueued += sum(enqueued.values())
                for message_key, count in enqueued.items():
                    log(f"{message_key}: catch-up/{reason} enqueued {count} "
                        f"since watermark {watermarks[message_key]}")
                    arm(message_key, ent)
        if tl_failures:
            raise SessionUnhealthy(
                f"Telegram TL decode failed for {tl_failures}/{checked} catch-up chats")
        return {
            "checked": checked,
            "failures": failures,
            "enqueued": total_enqueued,
        }

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event):
        if event.out:
            return
        write_health(
            last_live_update_at=now(),
        )
        topic_id = _message_topic_id(event.message)
        raw_key = _channel_key(event.chat_id, topic_id)
        _, raw_group_policy = _group_policy(event.chat_id)
        if raw_group_policy is not None and _reset_agent_dialogue_for_human(
                reg, raw_key, event.message, raw_group_policy):
            save_register(reg)
            log(f"{raw_key}: human msg={event.message.id} reset agent dialogue")
        access = await _event_access(event, me, reg)          # gate 1: the door
        if not access:
            # A tagged message is the common reason to ignore one here, and the
            # tag is easy to put on a request by mistake. Say so, or the silence
            # that follows looks like the daemon missing the message.
            reason = (" (carries " + NO_REPLY_MARKER + ")"
                      if _marker_line(_message_text(event.message), NO_REPLY_MARKER)
                      else "")
            log(f"{event.chat_id}: ignored {_message_kind(event.message)} "
                f"from {event.sender_id}{reason}")
            return
        if getattr(event.message, "action", None) is not None:
            return
        key = raw_key
        text = _message_text(event.message)
        group_policy = access.get("policy")
        is_direct = access["kind"] == "private"
        chat_ref = await _event_chat_ref(event, is_direct=is_direct)
        if _message_is_known(reg, key, event.message.id):
            log(f"{key}: already queued/done msg={event.message.id}")
            return
        if not is_direct and await handle_call_recording_request(
                key, event.chat_id, event.message, group_policy, chat_ref):
            return
        if text.startswith("/"):                          # control path — act now
            profile = await _sender_profile(event.message, group_policy, direct=is_direct)
            cmd = _command_name(text)
            log(f"{key}: /command «{text[:40]}» from {profile['id']} ({profile['role']})")
            if not _control_command_allowed(cmd, profile, group_policy):
                reply = _control_denied_reply(cmd, profile)
                log(f"{key}: denied command {cmd} for {profile['id']} ({profile['role']})")
            elif cmd == "/stop":
                reply = stop_running(key)
            elif cmd == "/reload":
                result = reload_runtime_settings()
                if result["ok"]:
                    # Nothing else ever ends a carried worker session, so this is
                    # the one deliberate way to start a conversation over.
                    dropped = forget_lanes()
                    reply = "ok, settings reloaded without disconnecting"
                    if dropped:
                        reply += "; next call starts a new worker session"
                        log(f"{key}: /reload dropped {dropped} carried "
                            "worker session(s)")
                else:
                    reply = f"nope: {result['error']}"
            else:
                reply = handle_command(reg, key, text)
            row = reg.setdefault(key, {})
            row["last_processed_message_id"] = max(event.message.id, row.get("last_processed_message_id", 0))
            save_register(reg)
            if reply:
                _, _ = await send_channel_message(
                    chat_ref, reply, access["kind"] == "private",
                    reply_to=thread_reply_target(
                        event.message, event.message.sender_id,
                        group_policy, access["kind"] == "private"))
            return
        if access.get("ambient_voice"):                   # unaddressed voice in auto mode — check transcript
            job = reserve_job(
                key, event.message, group_policy, is_direct, "live/ambient",
                chat_id=event.chat_id)
            if job is None:
                log(f"{key}: already queued/done msg={event.message.id}")
                return
            profile = await _sender_profile(event.message, group_policy, direct=is_direct)
            spoken = await echo_voice_message(
                event.message, chat_ref, key, is_direct, group_policy,
                sender_name=profile["name"])
            # If transcript names the assistant, dispatch as an addressed request
            if (spoken and spoken != "[голосовое — не удалось расшифровать]"
                    and _text_names_me(spoken, me, group_policy)
                    and _may_address(event.message.sender_id, group_policy)):
                if not admit_or_finish_agent_job(
                        key, event.message, group_policy, job, "live/ambient-addressed"):
                    return
                enqueued = await finalize_job(
                    key, event.message, group_policy, is_direct, "live/ambient-addressed", job, text_override=spoken)
                if not enqueued:
                    log(f"{key}: already queued/done msg={event.message.id}")
                    return
                if key in busy:
                    log(f"{key}: ambient voice msg={event.message.id} transcript addressed; worker busy")
                else:
                    log(f"{key}: ambient voice msg={event.message.id} transcript addressed; arming worker")
                    arm(key, chat_ref)
                return
            # Otherwise echo-only, no worker
            mark_job_finished(key, job, "done")
            log(f"{key}: ambient voice msg={event.message.id} transcribed; no worker dispatch")
            return
        job = reserve_job(
            key, event.message, group_policy, is_direct, "live",
            chat_id=event.chat_id)
        if job is None:
            log(f"{key}: already queued/done msg={event.message.id}")
            return
        if not admit_or_finish_agent_job(
                key, event.message, group_policy, job, "live"):
            return
        spoken = None
        if _is_spoken_media(event.message):               # transcribe → echo (visible + attributed by reply)
            profile = await _sender_profile(event.message, group_policy, direct=is_direct)
            sender_name = None if is_direct else profile["name"]
            spoken = await echo_voice_message(
                event.message, chat_ref, key, is_direct, group_policy,
                sender_name=sender_name)
        else:
            log(f"{key}: <- {_message_kind(event.message)} «{text[:60]}» "
                f"(debounce {channel_settings(reg, key)['debounce']}s)")
        enqueued = await finalize_job(
            key, event.message, group_policy, is_direct, "live", job, text_override=spoken)
        if not enqueued:
            log(f"{key}: already queued/done msg={event.message.id}")
            return
        if key in busy:                                   # worker running — the drain picks it up
            log(f"{key}: queued msg={event.message.id}; worker busy")
            return
        arm(key, chat_ref)

    async def periodic_sync():
        while True:
            await asyncio.sleep(SYNC_INTERVAL)
            try:
                # Ask Telethon to run the protocol-native getDifference path, then
                # reconcile bounded recent history by our durable watermarks. The
                # second pass remains authoritative if Telethon dropped an undecodable
                # update packet without disconnecting the socket.
                catch_up = getattr(client, "catch_up", None)
                if catch_up is not None:
                    await catch_up()
                    await asyncio.sleep(0)
                stats = await catch_up_known("periodic")
                state = "degraded" if stats["failures"] else "healthy"
                write_health(
                    state,
                    last_sync_at=now(),
                    last_catch_up_reason="periodic",
                    last_sync_stats=stats,
                    last_error=(
                        f"{stats['failures']} chat sync failure(s)"
                        if stats["failures"] else None
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                write_health(
                    "unhealthy",
                    last_error=_short_error(exc),
                )
                if isinstance(exc, SessionUnhealthy):
                    raise
                raise SessionUnhealthy(
                    f"periodic Telegram sync failed: {_short_error(exc)}") from exc

    # Catch up over known rooms (register keys are chat_ids) since the watermark.
    startup_stats = await catch_up_known("startup")
    startup_state = "degraded" if startup_stats["failures"] else "healthy"
    write_health(
        startup_state,
        session_started_at=now(),
        last_sync_at=now(),
        last_catch_up_reason="startup",
        last_sync_stats=startup_stats,
        last_error=(
            f"{startup_stats['failures']} chat sync failure(s)"
            if startup_stats["failures"] else None
        ),
    )
    for key in list(reg.keys()):
        if not _has_pending_jobs(reg, key):
            continue
        ent = await registered_chat_ref(key)
        if ent is not None:
            arm(key, ent)

    # --- call listener (PyTgCalls on daemon's client) -----------------------------
    # P2P and group call recording on the daemon's own client. Exactly one
    # update-consuming Telethon connection per account — a second subprocess breaks
    # p2p INCOMING_CALL delivery.
    calls = None
    users = configured_call_recording_users()
    groups = configured_call_recording_groups()
    voice_users = configured_voice_agent_users()
    has_p2p_recording = bool(users["allowed_callers"])
    has_voice_agent = bool(voice_users)
    has_p2p_calls = has_p2p_recording or has_voice_agent
    has_group_recording = bool(groups["auto"] or groups["on_request"])

    if has_p2p_calls or has_group_recording:
        calls = PyTgCalls(client)

    # P2P direct calls: recorded, answered by the voice agent, or both
    if has_p2p_calls:
        allowed_callers = set(users["allowed_callers"])
        active_recording = {
            "task": None,
            "caller_id": None,
            "output": None,
            "capture": None,
            "metadata_path": None,
            "metadata": None,
            "started_at": None,
            "mode": None,
        }
        seen_invite_ids = set()

        def recording_output(caller_id: int, mode: str) -> Path:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            return (STATE_HOME / "telegram" / CONNECTION / "calls" / "recordings"
                    / f"{timestamp}-{mode}-{caller_id}.ogg")

        library_last_block = calls._app._bind_client.get_conference_last_block

        async def read_chain_last_block(invite_msg_id: int):
            """Ask once for the last block of a conference's chain."""
            result = await client(GetGroupCallChainBlocksRequest(
                call=InputGroupCallInviteMessage(msg_id=invite_msg_id),
                sub_chain_id=0, offset=-1, limit=1))
            blocks = [b for u in getattr(result, "updates", None) or []
                      for b in getattr(u, "blocks", None) or []]
            return blocks[-1] if blocks else None

        async def conference_last_block(chat_id: int, invite_msg_id=None):
            """Wait for the last block of a conference's chain to exist.

            Joining an E2EE conference means deriving a block from the chain the
            other participants already signed. The invite arrives before that
            chain has anything in it — a conference grown out of a running 1:1
            call is still being built when its service message lands — and the
            library reads the chain exactly once, then reads an empty answer as
            a request to create a new conference and invite the caller into it.
            Reading until a block appears is what makes the join reachable.

            Only a read made against an invite is corrected here. The library
            asks for this chain on its own path too, without an invite, and
            answers that from the cached call — forcing an invite request there
            costs a direct call its answer, because the failure retries beneath
            it outlive the window to pick the call up."""
            if not invite_msg_id:
                return await library_last_block(chat_id, invite_msg_id)
            deadline = time.monotonic() + CONFERENCE_CHAIN_TIMEOUT
            started = time.monotonic()
            reads = 0
            while True:
                reads += 1
                block = await read_chain_last_block(invite_msg_id)
                if block:
                    log(f"call: conference chain for invite {invite_msg_id} — "
                        f"block of {len(block)} bytes after {reads} read(s) "
                        f"in {time.monotonic() - started:.1f}s")
                    return block
                if time.monotonic() >= deadline:
                    log(f"call: conference chain for invite {invite_msg_id} — "
                        f"still empty after {reads} read(s)")
                    return None
                await asyncio.sleep(CONFERENCE_CHAIN_INTERVAL)

        # The join runs inside the library, so the corrected reader has to be the
        # one it calls.
        calls._app._bind_client.get_conference_last_block = conference_last_block

        async def require_conference_chain(caller_id: int, invite_msg_id: int):
            """Join a conference only on a chain that has stopped moving.

            A chain with no block at all leaves the library's "create a
            conference instead" fallback reachable, and that fallback invites
            the caller into a conference of our own making. It also means there
            is no conference to join: Telegram does not always finish turning a
            1:1 call into a group one, and when it does not, the chain stays
            empty and the caller loses the call whatever this account does. An
            invite declined here without a single block ever being appended has
            already cost its caller the call, so nothing about the join is
            worth hardening against that.
            """
            started = time.monotonic()
            try:
                block = await conference_last_block(caller_id, invite_msg_id)
            except Exception as exc:
                raise RuntimeError(
                    f"conference chain unreadable for invite {invite_msg_id} — "
                    f"{type(exc).__name__}: {exc}") from exc
            if not block:
                raise RuntimeError(
                    f"conference chain empty for invite {invite_msg_id}")
            waited = time.monotonic() - started
            if waited > CONFERENCE_CHAIN_READY_LIMIT:
                await settle_conference_chain(
                    invite_msg_id, block, waited, read_chain_last_block)
            else:
                log(f"call: conference chain readable — last block "
                    f"{len(block)} bytes in {waited:.1f}s")

        async def refresh_conference_audio_map(chat_id: int, reason: str):
            """Rebuild the participant-to-audio-stream map for a live call.

            The library remaps audio whenever a participant update arrives, so
            what is left to cover is the join itself: anyone whose stream is not
            in the snapshot taken as this account joins is never decoded and
            never reaches the recording, and no update follows for someone who
            was already there. Dropping the cached participants forces a real
            answer rather than the hour-old one."""
            try:
                cache = calls._app._bind_client._cache
                cache._call_participants_cache.pop(chat_id)
            except Exception as exc:
                log(f"call: audio map — participant cache not cleared "
                    f"({type(exc).__name__}: {exc})")
            try:
                await calls._handle_request_participants(chat_id)
                log(f"call: conference audio map refreshed ({reason})")
            except Exception as exc:
                log(f"call: conference audio map refresh failed ({reason}) — "
                    f"{type(exc).__name__}: {exc}")

        async def settle_conference_audio_map(chat_id: int):
            """Re-ask a few times over the first seconds of a conference.

            A participant who is still being admitted when the join completes
            carries no stream to map yet."""
            elapsed = 0.0
            for delay in CONFERENCE_AUDIO_MAP_RETRIES:
                await asyncio.sleep(delay)
                elapsed += delay
                if active_recording["caller_id"] != chat_id:
                    return
                await refresh_conference_audio_map(
                    chat_id, f"+{elapsed:g}s after join")

        async def record_call(caller_id: int, mode: str, call_config: CallConfig):
            output = recording_output(caller_id, mode)
            capture = output.with_suffix(".mp3")
            output.parent.mkdir(parents=True, exist_ok=True)
            metadata_path = output.with_suffix(".json")
            started_at = datetime.now(timezone.utc)
            metadata = {
                "schema_version": 3,
                "status": "joining",
                "connection": CONNECTION,
                "caller_id": str(caller_id),
                "mode": mode,
                "started_at": iso_utc(started_at),
                "recording_started_at": None,
                "recording_ended_at": None,
                "duration_seconds": None,
                "stop_reason": None,
                "audio": {
                    "path": str(output),
                    "format": output.suffix.lstrip("."),
                    "codec": "opus",
                    "bytes": 0,
                    "settled": False,
                    "capture_method": "pytgcalls_mp3_then_ffmpeg_ogg",
                    "source": {
                        "path": str(capture),
                        "format": "mp3",
                        "bytes": 0,
                        "retained": False,
                    },
                    "conversion": {
                        "status": "pending",
                        "error": None,
                    },
                },
                "delivery": {
                    "enabled": True,
                    "status": "pending",
                    "attempts": 0,
                    "message_id": None,
                    "sent_at": None,
                    "error": None,
                },
            }
            write_metadata(metadata_path, metadata)

            active_recording.update({
                "caller_id": caller_id,
                "output": output,
                "capture": capture,
                "metadata_path": metadata_path,
                "metadata": metadata,
                "started_at": started_at,
                "mode": mode,
            })

            try:
                if mode == "conference":
                    await require_conference_chain(caller_id, call_config.conference)
                await calls.record(caller_id, RecordStream(capture), config=call_config)
                metadata["status"] = "recording"
                metadata["recording_started_at"] = iso_utc()
                write_metadata(metadata_path, metadata)
                log(f"call: recording {mode} from {caller_id} to {output}")
                asyncio.create_task(watch_capture_stall(caller_id, capture))
                if mode == "conference":
                    asyncio.create_task(settle_conference_audio_map(caller_id))
                    asyncio.create_task(watch_conference_peers(caller_id))
            except Exception as exc:
                # A media connection that never reached CONNECTED is reported as
                # an argument-less exception, so the type carries the message.
                log(f"call: failed to start {mode} recording from {caller_id}: "
                    f"{type(exc).__name__}: {exc}")
                if mode == "conference":
                    # Two different things go wrong here and the caller can act
                    # on only one of them. A conference that never came into
                    # existence is not one this account failed to join.
                    notice = (
                        "Конференция не создалась — Telegram не довёл перевод "
                        "звонка в групповой, это у него бывает через раз. "
                        "Я в неё не заходил. Перезвони и позови снова."
                        if "empty" in str(exc) else
                        "Не подключился к конференции: она всё ещё собиралась, "
                        "я подождал сколько мог. Позови ещё раз через "
                        "несколько секунд.")
                    with contextlib.suppress(Exception):
                        await client.send_message(caller_id, notice)
                metadata.update({
                    "status": "join_failed",
                    "stop_reason": "join_failed",
                })
                metadata["delivery"].update({
                    "status": "skipped",
                    "error": "join_failed",
                })
                write_metadata(metadata_path, metadata)
                active_recording.update({
                    "task": None,
                    "caller_id": None,
                    "output": None,
                    "capture": None,
                    "metadata_path": None,
                    "metadata": None,
                    "started_at": None,
                    "mode": None,
                })
                return

        async def start_call_recording(caller_id: int, mode: str, call_config: CallConfig):
            if voice_call_busy():
                log(f"call: ignoring incoming {mode} from {caller_id} — recorder busy")
                return
            if active_recording["task"] is not None:
                # A recording with no incoming audio is a call that already
                # ended, and holding the slot for it turns one lost call into
                # every later one being refused while the caller watches an
                # account that says Invited and never joins.
                if MEDIA_AUDIO_PEERS:
                    log(f"call: ignoring incoming {mode} from {caller_id} — recorder busy")
                    return
                held = active_recording["caller_id"]
                others = await conference_others(held) if held is not None else None
                if others:
                    log(f"call: ignoring incoming {mode} from {caller_id} — recorder busy")
                    return
                # An unanswerable question counts as deserted here, unlike in the
                # watchdog: a caller is waiting on this slot right now, and the
                # recording it holds has had no audio at all.
                log(f"call: preempting a deserted recording for {mode} "
                    f"from {caller_id}")
                await finalize_call_recording("preempted")
            # A previous call's channels are not this call's silence.
            MEDIA_AUDIO_PEERS.clear()
            task = asyncio.create_task(record_call(caller_id, mode, call_config))
            active_recording["task"] = task

        # --- Gemini Live voice agent (answers a direct call by talking) -------
        # The two media slots of one p2p call are independent: record() takes the
        # inbound PCM, play() feeds the outbound PCM. Both honour AudioParameters
        # exactly, so the Live API's own 16 kHz in / 24 kHz out need no resampling.
        active_voice_call = {
            "session": None,
            "caller_id": None,
            "caller_name": None,
            "record": False,
            "output": None,
            "caller_pcm": None,
            "agent_pcm": None,
            "metadata_path": None,
            "metadata": None,
            "started_at": None,
            "lane": None,
            "starting": False,
            "finishing": False,
        }

        voice_task_counter = {"n": 0}
        # Voice tasks in flight, by caller. Kept here rather than on a call,
        # because a task outlives the call that started it: two workers on one
        # carried session would be two processes writing one codex rollout.
        voice_tasks_running = {}

        def voice_call_busy():
            return active_voice_call["starting"] or active_voice_call["session"] is not None

        async def tail_voice_progress(path, on_progress):
            """Follow one task's progress file. Every line the worker writes is
            collected; what reaches the conversation is the call's decision, not
            this reader's."""
            offset = 0
            while True:
                await asyncio.sleep(1.0)
                if not path.exists():
                    continue
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        fh.seek(offset)
                        lines = fh.readlines()
                        offset = fh.tell()
                except OSError:
                    continue
                for line in lines:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    note = str(item.get("text") or "").strip()
                    if note and note not in ("-", ".", "..."):
                        on_progress(note, "worker")

        async def run_voice_task(caller_id, caller_name, text,
                                 delivery=VOICE_TASK_DELIVERY, on_progress=None,
                                 lane=None):
            """One task the caller asked for mid-call, run by the project's own
            worker — the same machinery a message runs, under the authority the
            same user's messages resolve to.

            Returns the worker's whole result, not just its reply. What the worker
            reports about itself — the session it ran in above all — is the
            daemon's business, and each caller decides for itself how much of it
            may cross into the conversation.

            With a `lane`, the session a finished turn ran in is kept for the rest
            of the call. Without one — the post-call summary takes this path — every
            run stands alone, which is what a summary wants."""
            s = voice_agent_settings()
            key = str(caller_id)

            async def attempt(resume_session, seen):
                """One worker run, from its own clean slate.

                Everything a second attempt must not inherit is built here rather
                than shared: its own task id, process key, cancellation flag and
                Telegram session copy. A retry that reused the cancellation would
                be refused before it started, and one that reused the process key
                would leave /stop unable to say which run it was killing."""
                voice_task_counter["n"] += 1
                task_id = f"voice-{int(time.time())}-{voice_task_counter['n']}"
                worker_text = text
                if on_progress is not None:
                    worker_text = voice_task_preamble(
                        caller_id, VOICE_PROGRESS_INTERVAL) + text
                job = voice_task_job(caller_id, caller_name, worker_text, task_id)
                authority = _authority_policy_for(job, None, True)
                proc_key = f"{key}#voice-{task_id}"
                authority_context = None
                worker_session = None
                cancel_event = threading.Event()
                progress_task = None

                def _offer_stage(line):
                    """Called from the worker's output pump, one JSONL event at a time."""
                    thread_id = codex_thread_started(line)
                    if thread_id and not seen["thread_id"]:
                        seen["thread_id"] = thread_id
                        if lane is not None:
                            # Written here, seconds into the run, rather than on
                            # the way out: the whole point is that there is no
                            # moment where a caller ringing back finds nothing.
                            lane.open(thread_id)
                            save_lane(caller_id, lane)
                    if codex_context_compacted(line):
                        seen["compacted"] = True
                    stage = codex_event_stage(line)
                    if stage:
                        on_progress(stage, "stream")

                PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
                progress_outbox = PROGRESS_DIR / f"{_safe_file_part(key)}-{task_id}.jsonl"
                with contextlib.suppress(OSError):
                    progress_outbox.unlink()
                try:
                    worker_session = prepare_worker_session(key, task_id)
                    if authority is not None:
                        authority_context = write_authority_context(
                            authority, f"{key}-{task_id}")
                    state = {"now": now_display(), "chat_id": key, "connection": CONNECTION,
                             "chat_type": "private", "chat_name": None,
                             "harness": s["worker"],
                             "participants": [{"name": job["sender_name"],
                                               "role": job["sender_role"]}],
                             "settings": s, "messages": 0, "history_chars": 0,
                             "current_request": {
                                 "message_id": task_id,
                                 "sender_id": key,
                                 "sender_name": job["sender_name"],
                                 "sender_role": job["sender_role"],
                                 "kind": job["kind"],
                                 "text": worker_text,
                                 "reply_to": None,
                                 "delivery": delivery,
                             },
                             "authority": authority,
                             "authority_context": authority_context,
                             "progress_outbox": str(progress_outbox),
                             "on_worker_line": (_offer_stage if on_progress is not None
                                                else None),
                             "worker_session": worker_session,
                             "resume_session": resume_session,
                             "resume_reanchor": reanchor,
                             "cancel_event": cancel_event,
                             "proc_key": proc_key}
                    log(f"voice: task {task_id} dispatched worker={s['worker']} "
                        f"model={s['model'] or 'default'} "
                        f"session={resume_session or 'new'} "
                        f"authority={_authority_summary(authority)}")
                    if on_progress is not None:
                        progress_task = asyncio.create_task(
                            tail_voice_progress(progress_outbox, on_progress))
                    loop = asyncio.get_running_loop()
                    future = loop.run_in_executor(
                        None, WORKERS[s["worker"]], key, [], state, procs)
                    done, _ = await asyncio.wait({future},
                                                 timeout=float(s["worker_timeout"]))
                    if not done:
                        await terminate_worker(
                            proc_key, future, cancel_event,
                            f"voice task timeout after {s['worker_timeout']}s")
                        raise RuntimeError(
                            f"{s['worker']} worker timed out after {s['worker_timeout']}s")
                    return await future
                finally:
                    if progress_task is not None:
                        progress_task.cancel()
                        with contextlib.suppress(BaseException):
                            await progress_task
                    with contextlib.suppress(OSError):
                        progress_outbox.unlink()
                    cleanup_worker_session(worker_session)
                    if authority_context:
                        with contextlib.suppress(OSError):
                            Path(authority_context).unlink()
                    release_worker_proc(proc_key)

            # Resuming is only safe while the event stream is being watched: the
            # live `thread.started` is what separates a session that never opened
            # from a turn that half-ran, and without it every failure would look
            # retryable. No watcher, no resume — the coupling is real, so it is
            # spelled out here rather than left for a later caller to trip over.
            watching = lane is not None and on_progress is not None
            if watching:
                lane.refresh(_read_lanes().get(str(caller_id)))
                voice_tasks_running[key] = voice_tasks_running.get(key, 0) + 1
            resume_session = lane.session_id if watching else None
            reanchor = bool(lane.needs_reanchor) if watching else False
            try:
                while True:
                    # Per attempt, never per call. A cell that outlived one task
                    # would report the previous task's thread as this one's, so a
                    # session that was lost would read as one that half-ran, and
                    # the recovery this exists for would quietly never happen.
                    seen = {"thread_id": None, "compacted": False}
                    try:
                        result = await attempt(resume_session, seen)
                    except asyncio.CancelledError:
                        # Read before the rest: a task killed early looks exactly
                        # like a session that never opened, and mistaking one for
                        # the other would re-run work the caller walked away from.
                        if lane is not None:
                            lane.clear("cancelled")
                            save_lane(caller_id, lane)
                        raise
                    except Exception:
                        if lane is None:
                            raise
                        verdict = lane.classify(s["worker"], bool(resume_session),
                                                bool(seen["thread_id"]))
                        lane.clear(verdict)
                        save_lane(caller_id, lane)
                        if verdict != "session_lost":
                            raise
                        log(f"voice: session {resume_session} could not be resumed "
                            "— running the task again in a new one")
                        # The retry runs fresh, so a second failure can only
                        # classify as task_failed and raise. One extra run at
                        # most, bounded by the shape rather than by a counter.
                        resume_session = None
                        reanchor = False
                        continue
                    if lane is not None:
                        lane.pin((result.get("meta") or {}).get("session_id"))
                        # Set when this turn compacted, cleared when it did not —
                        # so a thread that summarized itself re-states its context
                        # on the next turn, whether that turn comes later in this
                        # call or in a call days from now.
                        lane.needs_reanchor = bool(seen["compacted"])
                        save_lane(caller_id, lane)
                    return result
            finally:
                if watching:
                    voice_tasks_running[key] = voice_tasks_running.get(key, 1) - 1
                    if voice_tasks_running[key] <= 0:
                        voice_tasks_running.pop(key, None)

        async def run_voice_capability(caller_id, caller_name, capability, args):
            """One capability read for the caller, answered inside the turn that
            asked for it. It runs under the authority that caller's own messages
            resolve to — the same resolution a task gets — so speaking to the
            assistant reaches exactly what writing to it reaches, and no more."""
            key = str(caller_id)
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", capability or ""):
                return {"ok": False, "status": "not_a_capability",
                        "instruction": "That is not a tool name. Give the name on "
                                       "its own, such as 'clickup'."}
            binary = shutil.which(capability)
            if not binary:
                return {"ok": False, "status": "not_installed",
                        "instruction": f"There is no tool called {capability} on this "
                                       "project. Run 'capabilities' with 'list' to see "
                                       "what there is."}

            job = voice_task_job(caller_id, caller_name, "", f"cap-{capability}")
            authority = _authority_policy_for(job, None, True)
            caps = (authority or {}).get("allowed_capabilities") or {}
            rule = True if caps.get("*") is True else caps.get(capability)
            allowed = rule is True or (isinstance(rule, dict) and rule.get("allow") is not False)
            if authority is not None and not allowed:
                log(f"voice: run_capability {capability} refused for {key} "
                    f"(authority={_authority_summary(authority)})")
                return {"ok": False, "status": "not_allowed",
                        "instruction": f"You cannot reach {capability} for this caller. "
                                       "Say so plainly and offer what you can do instead."}

            authority_context = None
            if authority is not None:
                authority_context = write_authority_context(
                    authority, f"{key}-cap-{capability}")
            env = worker_env({"authority_context": authority_context,
                              "chat_type": "private"})

            try:
                # Three calls running, three of them spent guessing a flag: an
                # instruction in the prompt did not survive the distance to the
                # decision, so the first call to a tool is made to carry help and
                # identifiers. These discovery reads receive the same authority
                # envelope as the eventual command.
                helped = active_voice_call.setdefault("helped_capabilities", set())
                step = voice_capability_step(args, capability, helped)
                if step in ("help", "prime"):
                    helped.add(capability)
                if step == "prime":
                    primer = {}
                    for verb in ("help", "ids"):
                        argv = [verb] if verb == "help" else [verb, "list"]
                        code, out, _ = await run_capability_process(
                            binary, argv, env, voice_agent.CAPABILITY_TIMEOUT)
                        if code == 0 and out:
                            primer[verb], _ = truncate_capability_output(
                                out, voice_agent.CAPABILITY_OUTPUT_LIMIT)
                    log(f"voice: run_capability {capability} primed "
                        f"({', '.join(primer) or 'nothing'})")
                    return {"ok": False, "status": "read_this_first",
                            "help": primer.get("help"),
                            "identifiers": primer.get("ids"),
                            "instruction": f"Here is what {capability} takes, and the "
                                           "identifiers it knows. Make your real call "
                                           "now, using values from this — never one you "
                                           "made up or heard said."}

                code, out, err = await run_capability_process(
                    binary, args, env, voice_agent.CAPABILITY_TIMEOUT)
                if code is None:
                    log(f"voice: run_capability {capability} abandoned after "
                        f"{voice_agent.CAPABILITY_TIMEOUT}s")
                    return {"ok": False, "status": "too_slow",
                            "instruction": "That took too long to answer on a call. "
                                           "Hand the same work to agent_task instead, "
                                           "and tell the caller you are on it."}
            except OSError as exc:
                log(f"voice: run_capability {capability} failed to start — "
                    f"{type(exc).__name__}: {exc}")
                return {"ok": False, "status": "could_not_run",
                        "instruction": "That tool would not start. Say you could not "
                                       "check, and do not try it again on this call."}
            finally:
                if authority_context:
                    with contextlib.suppress(OSError):
                        Path(authority_context).unlink()

            if code == 4:
                # The check above is this daemon's own per-caller ACL, not the
                # capability manager's gate, so a tool the project has not
                # enabled still reaches here. Whichever gate fired — project,
                # global, or the tool's own connection — the tool's message is
                # the truth of the refusal and ours was a guess. That message is
                # written to stderr before anything is emitted, so stderr is
                # where the whole of it lives and stdout is usually empty.
                refusal, refusal_cut = truncate_capability_output(
                    err, voice_agent.CAPABILITY_OUTPUT_LIMIT)
                body, body_cut = truncate_capability_output(
                    out, voice_agent.CAPABILITY_OUTPUT_LIMIT)
                log(f"voice: run_capability {capability} refused by policy "
                    f"(exit 4) — {(err or out or '').strip()[:200] or 'no message'}")
                return {"ok": False, "status": "refused_by_policy",
                        "exit_code": code, "stderr": refusal, "stdout": body,
                        "truncated": refusal_cut or body_cut,
                        "instruction": f"{capability} refused this itself, and that "
                                       "refusal stands. Say what its message says. "
                                       "If it names a grant only the caller can "
                                       "make, ask for it plainly; never lift a "
                                       "gate, change a setting or a connection, or "
                                       "look for a way round."}

            body = out or ""
            truncated = len(body) > voice_agent.CAPABILITY_OUTPUT_LIMIT
            if truncated:
                body = body[:voice_agent.CAPABILITY_OUTPUT_LIMIT] + "\n…[cut]"
            result = {"ok": code == 0, "status": "ok" if code == 0 else "failed",
                      "exit_code": code, "stdout": body, "truncated": truncated,
                      "stderr_tail": (err or "")[-400:] if code != 0 else None}
            if truncated:
                result["instruction"] = ("Only the first part of the output is here. "
                                         "Running this again returns the same first "
                                         "part — the cut is where the output ends, "
                                         "not a mishap. Ask a narrower question, or "
                                         "hand it to agent_task.")
            elif code != 0:
                result["instruction"] = ("The tool refused or failed. Say what you "
                                         "could not check rather than guessing at "
                                         "the answer.")
            return result

        # Finalisation started from inside the session's own receiver would be
        # cancelled by the `stop()` it runs, so it is handed to a task instead
        # and kept referenced until it is done.
        stream_loss_finalisers = set()

        async def end_call_after_stream_loss(caller_id):
            """The speech session is gone, so the call is over whether or not the
            line is still open. Ending it says that; holding the slots open
            leaves the caller talking to a session that stopped listening, with
            nothing to distinguish it from a long pause.

            This goes through the ordinary ending — which stops the session,
            joins the tracks, delivers the recording, writes the summary and
            only then leaves the call. Leaving first looks like the same thing
            and is not: the ending hangs off the `LEFT_CALL` update, which our
            own leave does not raise, so the call would end with its recording
            and summary silently dropped."""
            log(f"voice: speech session lost — ending the call with {caller_id}")

            async def _finalise():
                try:
                    await finish_voice_call("speech_session_lost")
                except Exception as exc:
                    log(f"voice: could not end the call cleanly — "
                        f"{type(exc).__name__}: {exc}")
                    # A failure before finish_voice_call releases the active
                    # slot must not make every later call look permanently busy.
                    if active_voice_call.get("caller_id") == caller_id:
                        active_voice_call["finishing"] = False
                finally:
                    try:
                        await flush_call_summary()
                    except Exception as exc:
                        log(f"voice: could not flush the call summary — "
                            f"{type(exc).__name__}: {exc}")

            task = asyncio.create_task(_finalise())
            stream_loss_finalisers.add(task)
            task.add_done_callback(stream_loss_finalisers.discard)

        async def read_voice_project_file(wanted):
            """One project file, read for the caller. The project body names
            these; without this the call could only be told they exist."""
            result = read_project_file_result(wanted)
            if not result.get("ok"):
                log(f"voice: read_project_file {str(wanted)[:100]} "
                    f"-> {result.get('status')}")
            return result

        # The summary of the call that just ended, in flight while the recording
        # is still being joined and uploaded — so it is ready to reply to it.
        pending_summary = {}

        async def generate_call_summary(caller_id, caller_name, transcript, metadata):
            """Write the call up from its transcript, not its audio: audio is not
            context, and a summary in the chat enters the next call's tail through
            the ordinary path, timestamped by being a message."""
            record = {"status": "skipped", "error": None, "message_id": None}
            metadata["voice_agent"]["summary"] = record
            if len(transcript) < 2:
                record["error"] = "too_short"
                return None
            body = voice_agent.transcript_text(transcript, ASSISTANT_NAME, caller_name)
            if not body.strip():
                record["error"] = "empty_transcript"
                return None
            try:
                result = await run_voice_task(
                    caller_id, caller_name,
                    VOICE_SUMMARY_TASK + body,
                    delivery=VOICE_SUMMARY_DELIVERY)
            except Exception as exc:
                record.update({"status": "failed", "error": _short_error(exc)})
                log(f"voice: call summary failed — {_short_error(exc)}")
                return None
            text = str(result.get("reply") or "").strip()
            if not text:
                record.update({"status": "failed", "error": "empty_reply"})
                log("voice: call summary came back empty")
                return None
            return text

        async def flush_call_summary():
            """Post the summary as a reply to the recording it describes, so the
            two read as one thing. A call with no recording still gets its
            summary, standing alone; a summary that fails never costs the
            recording, which was delivered before this is awaited."""
            state = dict(pending_summary)
            pending_summary.clear()
            task = state.get("task")
            if task is None:
                return
            try:
                text = await task
            except Exception as exc:
                log(f"voice: call summary task failed — {_short_error(exc)}")
                return
            if not text:
                return
            metadata = state["metadata"]
            record = metadata["voice_agent"].get("summary") or {}
            reply_to = (metadata.get("delivery") or {}).get("message_id")
            try:
                sent, _ = await send_channel_message(
                    state["caller_id"], text, True,
                    reply_to=reply_to, force_reply=True)
            except Exception as exc:
                record.update({"status": "failed", "error": _short_error(exc)})
                log(f"voice: cannot post call summary — {_short_error(exc)}")
                write_metadata(state["metadata_path"], metadata)
                return
            record.update({"status": "sent", "message_id": getattr(sent, "id", None),
                           "reply_to": reply_to})
            write_metadata(state["metadata_path"], metadata)
            log(f"voice: call summary posted to {state['caller_id']} "
                f"({len(text)} chars, reply_to={reply_to})")

        async def deliver_voice_task_result(caller_id, completion):
            """The call ended before the task did: what the caller asked for
            reaches them as a direct message instead of being spoken."""
            text = str(completion.get("result") or "").strip()
            if not completion.get("ok"):
                text = f"Worker error:\n{text}" if text else "Worker error."
            if not text:
                return
            await send_channel_message(caller_id, text, True)
            log(f"voice: task {completion.get('job_id')} result delivered to "
                f"{caller_id} after the call ended")

        async def voice_chat_history(caller_id, limit, caller_name):
            if limit <= 0:
                return ""
            try:
                messages = await client.get_messages(caller_id, limit=limit)
            except Exception as exc:
                log(f"voice: cannot read history for {caller_id}: {_short_error(exc)}")
                return ""
            rows = []
            for m in reversed(messages):
                if getattr(m, "action", None) is not None:
                    continue
                text = message_tail_text(m)
                if not text:
                    continue
                who = ASSISTANT_NAME if getattr(m, "out", False) else caller_name
                # Stamped in the same zone the prompt states as "now", so the
                # model can tell minutes old from weeks old.
                stamp = ""
                when = getattr(m, "date", None)
                if when is not None:
                    with contextlib.suppress(Exception):
                        stamp = f"[{when.astimezone(VOICE_TIMEZONE):%Y-%m-%d %H:%M}] "
                rows.append(f"{stamp}{who}: {text}")
            return "\n".join(rows)

        async def start_voice_call(caller_id, policy, api_key, voice_context):
            record = caller_id in set(
                configured_call_recording_users()["allowed_callers"])
            caller_name = policy["name"]
            caller_profile = {
                "id": str(caller_id),
                "name": caller_name,
                "role": sender_role_for(caller_id, None, True),
            }
            # Off unless the project named it, and reload additionally unless
            # this caller's control role permits it: settings widen what a call
            # may hold, they never widen who may do it.
            enabled_tools = dict(policy.get("tools") or {})
            def tool_enabled(name):
                return bool(enabled_tools.get(name))
            voice_reload = (reload_runtime_settings
                            if tool_enabled("reload_service")
                            and _control_command_allowed("/reload", caller_profile)
                            else None)
            output = recording_output(caller_id, "voice")
            output.parent.mkdir(parents=True, exist_ok=True)
            metadata_path = output.with_suffix(".json")
            caller_pcm = output.with_name(f"{output.stem}-caller.pcm") if record else None
            agent_pcm = output.with_name(f"{output.stem}-agent.pcm") if record else None
            started_at = datetime.now(timezone.utc)
            metadata = {
                "schema_version": 3,
                "status": "joining",
                "connection": CONNECTION,
                "caller_id": str(caller_id),
                "mode": "voice_agent",
                "started_at": iso_utc(started_at),
                "recording_started_at": None,
                "recording_ended_at": None,
                "duration_seconds": None,
                "stop_reason": None,
                "voice_agent": {
                    "enabled": True,
                    "status": "starting",
                    "model": policy["model"],
                    "voice": policy["voice"],
                    "history_messages": policy["history"],
                    "error": None,
                    "transcript": [],
                    "transcript_text": "",
                },
                "audio": {
                    "path": str(output) if record else None,
                    "format": "ogg" if record else None,
                    "codec": "opus" if record else None,
                    "bytes": 0,
                    "settled": False,
                    "capture_method": "external_pcm_two_track_then_ffmpeg_stereo_ogg",
                    "tracks": [],
                    "conversion": {
                        "status": "pending" if record else "disabled",
                        "error": None,
                    },
                },
                "delivery": {
                    "enabled": record,
                    "status": "pending" if record else "disabled",
                    "attempts": 0,
                    "message_id": None,
                    "sent_at": None,
                    "error": None,
                },
            }
            write_metadata(metadata_path, metadata)

            # `session` is bound just below; the closure reads it when a running
            # task reports, which cannot happen before the session exists.
            def note_task_progress(note, source="stream"):
                session.note_progress(note, source)

            # `fresh` removes the lane rather than disabling it: with nothing to
            # carry a session, every task runs on its own exactly as it did
            # before any of this existed.
            lane = (load_lane(caller_id)
                    if voice_agent_settings()["session_mode"] == "carry"
                    else None)
            if lane is not None and lane.session_id:
                log(f"voice: carrying session {lane.session_id} "
                    f"({lane.turns} turn(s)) into this call")

            async def run_call_task(text):
                """The runner deals in spoken answers, so it is handed one and
                nothing else. A completion is serialized whole into the model's
                next prompt, so anything the worker reports about itself has to
                stop here — on this side of the boundary — rather than rely on
                every later caller remembering not to pass it on."""
                result = await run_voice_task(caller_id, caller_name, text,
                                              on_progress=note_task_progress,
                                              lane=lane)
                return result["reply"]

            task_runner = voice_agent.VoiceTaskRunner(
                run_call_task,
                lambda completion: deliver_voice_task_result(caller_id, completion),
                log=log,
                elsewhere=lambda: voice_tasks_running.get(str(caller_id), 0) > 0,
            )
            session = voice_agent.VoiceCallSession(
                calls,
                caller_id,
                api_key=api_key,
                model=policy["model"],
                voice=policy["voice"],
                # The prompt is set once the call is answered: building it means
                # reading the chat tail, and that must not delay the pickup.
                system_instruction="",
                caller_name=caller_name,
                greeting=policy.get("greeting"),
                caller_track=caller_pcm,
                agent_track=agent_pcm,
                task_runner=task_runner if tool_enabled("agent_task") else None,
                send_to_chat=(
                    (lambda body: send_channel_message(caller_id, body, True))
                    if tool_enabled("send_to_chat") else None),
                capability_runner=(
                    (lambda capability, args: run_voice_capability(
                        caller_id, caller_name, capability, args))
                    if tool_enabled("run_capability") else None),
                file_reader=(read_voice_project_file
                             if tool_enabled("read_project_file") else None),
                reload_service=voice_reload,
                on_stream_end=lambda: end_call_after_stream_loss(caller_id),
                progress_interval=VOICE_PROGRESS_INTERVAL,
                log=log,
            )
            active_voice_call.update({
                "session": session,
                "caller_id": caller_id,
                "caller_name": caller_name,
                # Per call, not per daemon: what one caller was shown the help
                # for is not something the next caller has seen.
                "helped_capabilities": set(),
                "record": record,
                "output": output,
                "caller_pcm": caller_pcm,
                "agent_pcm": agent_pcm,
                "metadata_path": metadata_path,
                "metadata": metadata,
                "started_at": started_at,
                "lane": lane,
                "finishing": False,
            })
            try:
                # record() answers the call and claims the inbound slot; play()
                # then claims the outbound one on the same connection.
                await calls.record(
                    caller_id,
                    RecordStream(
                        audio=True,
                        audio_parameters=AudioParameters(voice_agent.CALLER_RATE, 1),
                    ),
                    config=CallConfig(timeout=60),
                )
                await calls.play(
                    caller_id,
                    MediaStream(
                        ExternalMedia.AUDIO,
                        audio_parameters=AudioParameters(voice_agent.AGENT_RATE, 1),
                        audio_flags=MediaStream.Flags.REQUIRED,
                        video_flags=MediaStream.Flags.IGNORE,
                    ),
                )
                session.start_pump()
                # Answered, and both slots are already carrying audio. Reading
                # the chat tail now costs a moment of dead air; reading it before
                # the answer costs the ring window, after which pytgcalls tries
                # to *place* a call instead of accepting one.
                tail_started = time.monotonic()
                history = await voice_chat_history(
                    caller_id, policy["history"], caller_name)
                log(f"voice: chat tail for {caller_id} — {len(history)} chars in "
                    f"{time.monotonic() - tail_started:.1f}s")
                session.set_system_instruction(voice_agent.build_system_prompt(
                    voice_context, history,
                    now_line=voice_agent.current_time_line(
                        VOICE_TIMEZONE, VOICE_TIMEZONE_NAME)))
                await session.start_agent()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                log(f"voice: cannot answer call from {caller_id}: {error}")
                with contextlib.suppress(Exception):
                    await session.stop()
                with contextlib.suppress(Exception):
                    await calls.leave_call(caller_id)
                metadata.update({"status": "join_failed", "stop_reason": "join_failed"})
                metadata["voice_agent"].update({"status": "failed", "error": error})
                metadata["delivery"].update({"status": "skipped", "error": "join_failed"})
                write_metadata(metadata_path, metadata)
                for path in (caller_pcm, agent_pcm):
                    if path is not None:
                        with contextlib.suppress(OSError):
                            path.unlink()
                active_voice_call.update({
                    "session": None, "caller_id": None, "caller_name": None,
                    "record": False, "output": None, "caller_pcm": None,
                    "agent_pcm": None, "metadata_path": None, "metadata": None,
                    "started_at": None, "lane": None, "finishing": False,
                })
                return
            metadata.update({
                "status": "in_call",
                "recording_started_at": iso_utc(),
            })
            metadata["voice_agent"]["status"] = "talking"
            write_metadata(metadata_path, metadata)
            log(f"voice: answering {caller_name} ({caller_id}) with "
                f"{policy['model']}/{policy['voice']}; record={record}")

        def begin_voice_call(caller_id, policy, api_key, voice_context):
            """Claim the slot synchronously, then set the call up off the update
            handler: reading history and opening the Live socket must not hold up
            the PyTgCalls dispatcher."""
            active_voice_call["starting"] = True

            async def run():
                try:
                    await start_voice_call(caller_id, policy, api_key, voice_context)
                finally:
                    active_voice_call["starting"] = False

            asyncio.create_task(run())

        async def finish_voice_call(reason):
            if active_voice_call["session"] is None or active_voice_call["finishing"]:
                return
            active_voice_call["finishing"] = True
            session = active_voice_call["session"]
            caller_id = active_voice_call["caller_id"]
            caller_name = active_voice_call["caller_name"]
            record = active_voice_call["record"]
            output = active_voice_call["output"]
            caller_pcm = active_voice_call["caller_pcm"]
            agent_pcm = active_voice_call["agent_pcm"]
            metadata = active_voice_call["metadata"]
            metadata_path = active_voice_call["metadata_path"]
            started_at = active_voice_call["started_at"]
            # Read out with the rest, well before the reset below clears it: the
            # metadata this feeds is written afterwards, and a lane read then
            # would always be gone.
            lane = active_voice_call["lane"]

            summary = await session.stop()
            tracks = session.tracks
            with contextlib.suppress(Exception):
                await calls.leave_call(caller_id)
            active_voice_call.update({
                "session": None, "caller_id": None, "caller_name": None,
                "record": False, "output": None, "caller_pcm": None,
                "agent_pcm": None, "metadata_path": None, "metadata": None,
                "started_at": None, "lane": None, "finishing": False,
            })

            # The call ending is not the session ending — that is the whole point
            # of carrying one. It is also not this lane's last word: a task that
            # outlived the call is still holding it and may yet pin a turn onto
            # it, so clearing here would reset a count that is still being kept.
            wall_duration = (datetime.now(timezone.utc) - started_at).total_seconds()
            transcript = summary["transcript"]
            metadata["voice_agent"].update({
                "status": "complete",
                "error": summary["pump_error"],
                "interruptions": summary["interruptions"],
                "dropped_input_chunks": summary["dropped_input_chunks"],
                "caller_seconds": summary["caller_seconds"],
                "agent_seconds": summary["agent_seconds"],
                "agent_voiced_seconds": summary["agent_voiced_seconds"],
                "messages_sent": summary["messages_sent"],
                "tasks": summary["tasks"],
                # What the model never sees: which session each stretch of the
                # call ran in, and what ended it. When continuity misbehaves this
                # is the only place that says so.
                "worker_sessions": lane.sessions if lane is not None else [],
                "transcript": transcript,
                "transcript_text": voice_agent.transcript_text(
                    transcript, ASSISTANT_NAME, caller_name),
            })
            metadata.update({
                "recording_ended_at": iso_utc(),
                "wall_duration_seconds": round(wall_duration, 3),
                "stop_reason": reason,
            })
            metadata["audio"]["tracks"] = tracks
            log(f"voice: call with {caller_id} ended ({reason}); "
                f"{len(transcript)} turns, {summary['interruptions']} interruption(s)")

            # Started now so it runs while the recording is joined and uploaded;
            # awaited afterwards, by which time it can reply to the recording.
            pending_summary.update({
                "task": asyncio.create_task(generate_call_summary(
                    caller_id, caller_name, transcript, metadata)),
                "caller_id": caller_id,
                "metadata": metadata,
                "metadata_path": metadata_path,
            })

            if not record:
                metadata.update({
                    "status": "conversation_only",
                    "duration_seconds": round(wall_duration, 3),
                })
                write_metadata(metadata_path, metadata)
                return

            finalized = await voice_agent.join_tracks_to_stereo(
                caller_pcm, agent_pcm, output)
            status = "complete" if finalized["status"] == "complete" else "conversion_failed"
            media_duration = finalized.get("duration_seconds") or wall_duration
            metadata.update({
                "status": status,
                "duration_seconds": media_duration,
            })
            metadata["audio"]["bytes"] = finalized["output_bytes"]
            metadata["audio"]["settled"] = finalized["status"] == "complete"
            metadata["audio"]["conversion"].update({
                "status": finalized["status"],
                "error": finalized["error"],
            })
            metadata["audio"]["sources_retained"] = finalized["sources_retained"]

            MIN_BYTES = 1024
            MIN_DURATION = 1.0
            is_empty = (
                finalized["status"] == "complete"
                and (finalized["output_bytes"] < MIN_BYTES
                     or (media_duration or 0) < MIN_DURATION)
            )
            if finalized["status"] != "complete":
                metadata["delivery"].update({
                    "status": "skipped",
                    "error": finalized["error"],
                })
                write_metadata(metadata_path, metadata)
                log(f"voice: recording join failed — {finalized['error']}")
                return
            if is_empty:
                metadata["delivery"].update({
                    "status": "skipped",
                    "error": "audio_not_received",
                })
                write_metadata(metadata_path, metadata)
                log(f"voice: recording empty (bytes={finalized['output_bytes']}, "
                    f"duration={media_duration:.1f}s) — not delivered")
                return
            write_metadata(metadata_path, metadata)
            await send_recording_to_chat(
                client,
                caller_id,
                output,
                metadata_path,
                metadata,
                emit_event_fn=lambda event, **fields: log(f"voice-delivery: {event} {fields}"),
                caption=VOICE_RECORDING_CAPTION or None,
            )
            log(f"voice: recording delivered to {caller_id}")

        @calls.on_update(filters.stream_frame(Direction.INCOMING, Device.MICROPHONE))
        async def inbound_call_audio(_call_client: PyTgCalls, update):
            session = active_voice_call["session"]
            if session is not None and update.chat_id == active_voice_call["caller_id"]:
                session.on_incoming_frames(update.frames)

        @calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def incoming_p2p_call(_call_client: PyTgCalls, update: ChatUpdate):
            caller_id = update.chat_id
            current_voice_users = configured_voice_agent_users()
            current_allowed_callers = set(
                configured_call_recording_users()["allowed_callers"])
            if caller_id in current_voice_users:
                api_key, voice_context, blocked = voice_call_readiness()
                if blocked:
                    log(f"voice: {blocked} — cannot answer {caller_id} by voice")
                else:
                    if voice_call_busy() or active_recording["task"] is not None:
                        log(f"call: ignoring voice call from {caller_id} — a call is already active")
                        return
                    begin_voice_call(caller_id, current_voice_users[caller_id], api_key,
                                     voice_context)
                    return
            if caller_id not in current_allowed_callers:
                log(f"call: ignoring p2p from {caller_id} — not in allowed_callers")
                return
            await start_call_recording(caller_id, "p2p", CallConfig(timeout=60))

        async def finalize_call_recording(stop_reason: str = "call_closed"):
            """Close the active p2p or conference recording and deliver it.

            A conference is not chat-bound and announces neither its start nor
            its end through the chat-update stream, so this is reached from the
            capture watchdog as well as from the call-ended handler."""
            caller_id = active_recording["caller_id"]
            if caller_id is None:
                return
            if stop_reason != "call_closed":
                with contextlib.suppress(Exception):
                    await calls.leave_call(caller_id)
            output = active_recording["output"]
            capture = active_recording["capture"]
            metadata_path = active_recording["metadata_path"]
            metadata = active_recording["metadata"]
            started_at = active_recording["started_at"]
            mode = active_recording["mode"]

            log(f"call: ended from {caller_id} ({stop_reason})")

            # Clear active state immediately
            active_recording.update({
                "task": None,
                "caller_id": None,
                "output": None,
                "capture": None,
                "metadata_path": None,
                "metadata": None,
                "started_at": None,
                "mode": None,
            })

            if output is None or capture is None or metadata is None:
                log(f"call: cannot finalize — incomplete recording state")
                return

            # Finalize: convert MP3→OGG
            recording_ended_at = iso_utc()
            wall_duration = (datetime.now(timezone.utc) - started_at).total_seconds()

            finalized = await finalize_mp3_capture(
                capture, output, keep_source=mode == "conference")
            status = "complete" if finalized["status"] == "complete" else "conversion_failed"
            media_duration = finalized.get("duration_seconds") or wall_duration

            metadata.update({
                "status": status,
                "recording_ended_at": recording_ended_at,
                "duration_seconds": media_duration,
                "wall_duration_seconds": round(wall_duration, 3),
                "stop_reason": stop_reason,
            })
            metadata["audio"]["bytes"] = finalized["output_bytes"]
            metadata["audio"]["settled"] = finalized["status"] == "complete"
            metadata["audio"]["source"].update({
                "bytes": finalized["source_bytes"],
                "retained": finalized["source_retained"],
            })
            metadata["audio"]["conversion"].update({
                "status": finalized["status"],
                "error": finalized["error"],
            })
            if finalized.get("cleanup_error"):
                metadata["audio"]["source"]["cleanup_error"] = finalized["cleanup_error"]

            # Empty-capture guard (same as conference path in call_recorder.py)
            MIN_BYTES = 1024
            MIN_DURATION = 1.0
            is_empty = (
                finalized["status"] == "complete"
                and (finalized["output_bytes"] < MIN_BYTES or (media_duration or 0) < MIN_DURATION)
            )

            if is_empty:
                metadata["delivery"].update({
                    "status": "skipped",
                    "error": "audio_not_received",
                })
                write_metadata(metadata_path, metadata)
                log(f"call: recording empty (bytes={finalized['output_bytes']}, duration={media_duration:.1f}s) — not delivered")
            else:
                write_metadata(metadata_path, metadata)
                # Deliver to caller's direct chat
                if metadata["delivery"]["enabled"] and finalized["status"] == "complete":
                    await send_recording_to_chat(
                        client,
                        caller_id,
                        output,
                        metadata_path,
                        metadata,
                        emit_event_fn=lambda event, **fields: log(f"call-delivery: {event} {fields}"),
                    )
                    log(f"call: recording delivered to {caller_id}")
                elif finalized["status"] != "complete":
                    log(f"call: recording conversion failed — {finalized['error']}")
                else:
                    log(f"call: recording complete, delivery disabled")

        @calls.on_update()
        async def log_call_update(_call_client: PyTgCalls, update):
            """Name every update the library emits during a live recording.

            What ends a call is not always announced through a handler we
            listen for — being dropped from a conference reaches us as nothing
            at all — so while a recording is open, everything gets named."""
            if active_recording["caller_id"] is None and \
                    active_voice_call["caller_id"] is None:
                return
            status = getattr(update, "status", None)
            blocks = getattr(update, "blocks", None)
            detail = "" if blocks is None else f" blocks={[len(b) for b in blocks]}"
            log(f"call-update: {type(update).__name__}"
                f"{'' if status is None else ' ' + str(status)} "
                f"chat_id={getattr(update, 'chat_id', None)}{detail}")

        @calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def call_ended(_call_client: PyTgCalls, update: ChatUpdate):
            if active_voice_call["caller_id"] == update.chat_id:
                await finish_voice_call("call_closed")
                await flush_call_summary()
                return
            if active_recording["caller_id"] != update.chat_id:
                return
            await finalize_call_recording("call_closed")

        async def conference_others(chat_id: int):
            """Who Telegram still lists in this call, besides this account.

            Asked fresh: the participant list is cached for an hour, which is
            the same staleness the audio map has to work around, so the cache is
            dropped before asking. Returns None when the question could not be
            put, which is not the same answer as nobody.

            The public participants method refuses a chat id that is not
            negative, and a conference migrated from a p2p call is keyed by the
            caller's user id, so the client-level call is the one that answers.
            """
            try:
                cache = calls._app._bind_client._cache
                cache._call_participants_cache.pop(chat_id)
            except Exception:
                pass
            try:
                participants = await calls._app.get_group_call_participants(chat_id)
            except Exception as exc:
                log(f"call: could not ask who is in the call "
                    f"({type(exc).__name__}: {exc})")
                return None
            return [p for p in (participants or [])
                    if int(getattr(p, "user_id", 0)) != int(me.id)]

        async def watch_conference_peers(caller_id: int):
            """Close a conference recording once nobody else is in the call.

            Being alone in a group call is legal, so the transport reports no
            disconnect and keeps handing over frames — silence, encoded for as
            long as the process lives. The byte-growth watchdog cannot see that,
            because silence has bytes; the recording that prompted this ran for
            an hour after its call was over, holding the only recorder slot.

            What ends a call is the last participant leaving, not the room going
            quiet, so quiet only decides when to ask. Telegram's own list gives
            the answer.
            """
            joined_at = time.monotonic()
            quiet_for = 0.0
            empty_answers = 0
            while active_recording["caller_id"] == caller_id:
                await asyncio.sleep(CONFERENCE_PEER_INTERVAL)
                if MEDIA_AUDIO_PEERS:
                    quiet_for = 0.0
                    empty_answers = 0
                    continue
                # Measured from the join rather than from the first channel: a
                # conference that never produces one at all is the failure this
                # watches for, and waiting for a channel before starting to
                # count made that the one case it could not see.
                if time.monotonic() - joined_at < CONFERENCE_JOIN_GRACE:
                    continue
                quiet_for += CONFERENCE_PEER_INTERVAL
                if quiet_for < CONFERENCE_QUIET_BEFORE_CHECK:
                    continue
                quiet_for = 0.0
                others = await conference_others(caller_id)
                if others is None or others:
                    empty_answers = 0
                    continue
                empty_answers += 1
                if empty_answers >= CONFERENCE_EMPTY_ANSWERS:
                    log("call: no one left in the call — closing the recording")
                    await finalize_call_recording("call_deserted")
                    return

        async def watch_capture_stall(caller_id: int, capture: Path):
            """Close a conference recording once its capture stops growing.

            A normal hangup arrives as a chat update, but being removed from a
            conference emits nothing at all. The MP3 encoder writes continuously
            while the call is up — silence included — so a file that stops
            growing is the call being over, and it is the only end signal a
            kicked conference gives us."""
            still = 0.0
            last_size = -1
            joined_at = time.monotonic()
            first_frames = False
            while active_recording["caller_id"] == caller_id:
                await asyncio.sleep(CAPTURE_STALL_INTERVAL)
                size = capture.stat().st_size if capture.exists() else 0
                if size and not first_frames:
                    first_frames = True
                    log(f"call: first frames written "
                        f"{time.monotonic() - joined_at:.1f}s after the join")
                if size != last_size:
                    last_size = size
                    still = 0.0
                    continue
                still += CAPTURE_STALL_INTERVAL
                if still >= CAPTURE_STALL_TIMEOUT and size > 0:
                    log(f"call: capture stopped growing for {still:.0f}s "
                        f"at {size} bytes — closing the recording")
                    await finalize_call_recording("capture_stalled")
                    return

        @client.on(events.Raw)
        async def conference_invite(event):
            if not isinstance(event, UpdateNewMessage):
                return
            message = event.message
            if not isinstance(message, MessageService):
                return
            action = getattr(message, "action", None)
            if action is None:
                return
            caller_id = getattr(message, "sender_id", None)
            if caller_id is None:
                peer_id = getattr(message, "peer_id", None)
                if peer_id is not None and hasattr(peer_id, "user_id"):
                    caller_id = peer_id.user_id
            if (caller_id is None or caller_id not in set(
                    configured_call_recording_users()["allowed_callers"])):
                return
            if message.id in seen_invite_ids:
                return
            if isinstance(action, MessageActionConferenceCall) and not action.missed:
                seen_invite_ids.add(message.id)
                try:
                    call_config = CallConfig(conference=message.id)
                except TypeError:
                    log(f"call: conference recording unavailable on py-tgcalls {getattr(pytgcalls, '__version__', 'unknown')} — upgrade to 3.x needed")
                    return
                await start_call_recording(caller_id, "conference", call_config)
            elif isinstance(action, MessageActionInviteToGroupCall):
                seen_invite_ids.add(message.id)
                try:
                    call_config = CallConfig(conference=message.id)
                except TypeError:
                    log(f"call: conference recording unavailable on py-tgcalls {getattr(pytgcalls, '__version__', 'unknown')} — upgrade to 3.x needed")
                    return
                await start_call_recording(caller_id, "conference", call_config)

    # Group call recording (voice chats in configured groups)
    group_watcher_task = None
    if has_group_recording:
        auto_groups = set(groups["auto"])
        on_request_groups = set(groups["on_request"])
        send_to_chat_groups = set(groups["send_to_chat"])
        all_groups = auto_groups | on_request_groups
        recorded_call_ids = {}
        active_group_recording = None
        group_call_closed_event = asyncio.Event()
        schema_fallback_groups = set()
        schema_retry_after = {}
        join_retry_after = {}

        # Helpers for group call inspection
        bridge = getattr(getattr(calls, "_app", None), "_bind_client", None)

        async def active_group_call(chat_id: int):
            """Check if a group has an active voice chat."""
            get_call = getattr(bridge, "get_call", None)
            if get_call is None:
                return None
            try:
                from telethon.errors.common import TypeNotFoundError
                call_ref = await get_call(chat_id)
                schema_fallback_groups.discard(chat_id)
                cache = getattr(bridge, "_cache", None)
                if call_ref is not None and cache is not None:
                    cache.set_cache(chat_id, call_ref)
                return call_ref
            except TypeNotFoundError:
                # Telegram schema updated before Telethon — skip probing for now
                if chat_id not in schema_fallback_groups:
                    log(f"group-call-probe: schema fallback for {chat_id}")
                    schema_fallback_groups.add(chat_id)
                return None
            except Exception as exc:
                log(f"group-call-probe: failed for {chat_id}: {exc}")
                return None

        async def start_group_recording(chat_id: int, trigger: str, request: dict | None = None) -> bool:
            """Join and record an active group voice chat."""
            nonlocal active_group_recording
            call_ref = await active_group_call(chat_id)
            if call_ref is None:
                recorded_call_ids.pop(chat_id, None)
                return False
            call_id = int(call_ref.id)
            if recorded_call_ids.get(chat_id) == call_id:
                return False

            retry_key = (chat_id, call_id)
            if time.time() < join_retry_after.get(retry_key, 0):
                return False

            try:
                entity = await client.get_entity(chat_id)
                title = getattr(entity, "title", None) or str(chat_id)
            except Exception:
                title = str(chat_id)

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output = (CONNECTION_STATE_DIR / "calls" / "recordings"
                      / f"{timestamp}-{_safe_file_part(str(chat_id))}-call-{call_id}.ogg")
            capture = output.with_suffix(".mp3")
            metadata_path = output.with_suffix(".json")
            output.parent.mkdir(parents=True, exist_ok=True)

            metadata = {
                "schema_version": 3,
                "status": "joining",
                "connection": CONNECTION,
                "account_id": str((await client.get_me()).id),
                "chat_id": str(chat_id),
                "chat_title": title,
                "telegram_call_id": str(call_id),
                "detected_at": iso_utc(),
                "recording_started_at": None,
                "recording_ended_at": None,
                "duration_seconds": None,
                "trigger": trigger,
                "request": request,
                "stop_reason": None,
                "join": {
                    "attempts": 0,
                    "status": "joining",
                    "errors": [],
                },
                "audio": {
                    "path": str(output),
                    "format": output.suffix.lstrip("."),
                    "codec": "opus",
                    "bytes": 0,
                    "settled": False,
                    "capture_method": "pytgcalls_mp3_then_ffmpeg_ogg",
                    "source": {
                        "path": str(capture),
                        "format": "mp3",
                        "bytes": 0,
                        "retained": False,
                    },
                    "conversion": {
                        "status": "pending",
                        "error": None,
                    },
                },
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

            # Attempt to join (with retries)
            from pytgcalls.types import GroupCallConfig
            from pytgcalls.exceptions import NoActiveGroupCall
            joined = False
            JOIN_ATTEMPTS = 3
            for attempt in range(1, JOIN_ATTEMPTS + 1):
                metadata["join"].update({"attempts": attempt, "status": "joining"})
                write_metadata(metadata_path, metadata)
                cache = getattr(bridge, "_cache", None)
                if cache is not None:
                    cache.set_cache(chat_id, call_ref)
                try:
                    await asyncio.wait_for(
                        calls.record(
                            chat_id,
                            RecordStream(capture),
                            config=GroupCallConfig(auto_start=False),
                        ),
                        timeout=20,
                    )
                    await calls.mute(chat_id)
                except NoActiveGroupCall:
                    with contextlib.suppress(OSError):
                        capture.unlink()
                    metadata.update({
                        "status": "not_started",
                        "stop_reason": "no_active_voice_chat",
                    })
                    metadata["join"]["status"] = "no_active_voice_chat"
                    metadata["delivery"].update({
                        "status": "skipped",
                        "error": "no_active_voice_chat",
                    })
                    write_metadata(metadata_path, metadata)
                    return False
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    metadata["join"]["errors"].append({
                        "attempt": attempt,
                        "error": error,
                        "at": iso_utc(),
                    })
                    metadata["join"]["status"] = "retrying"
                    write_metadata(metadata_path, metadata)
                    log(f"group-call: join failed for {chat_id} call {call_id} attempt {attempt}: {error}")
                    with contextlib.suppress(Exception):
                        await calls.leave_call(chat_id)
                    with contextlib.suppress(OSError):
                        capture.unlink()
                    if attempt < JOIN_ATTEMPTS:
                        current_call = await active_group_call(chat_id)
                        if current_call is None or int(current_call.id) != call_id:
                            metadata.update({
                                "status": "not_started",
                                "stop_reason": "voice_chat_closed_during_join",
                            })
                            metadata["join"]["status"] = "call_closed"
                            metadata["delivery"].update({
                                "status": "skipped",
                                "error": "voice_chat_closed_during_join",
                            })
                            write_metadata(metadata_path, metadata)
                            return False
                        call_ref = current_call
                        await asyncio.sleep(2 * attempt)
                        continue
                    break
                else:
                    joined = True
                    metadata["join"]["status"] = "joined"
                    join_retry_after.pop(retry_key, None)
                    break

            if not joined:
                metadata.update({"status": "join_failed", "stop_reason": "join_failed"})
                metadata["join"]["status"] = "failed"
                metadata["delivery"].update({
                    "status": "skipped",
                    "error": "join_failed",
                })
                join_retry_after[retry_key] = time.time() + 15
                write_metadata(metadata_path, metadata)
                return False

            # Recording started
            started_at = datetime.now(timezone.utc)
            recorded_call_ids[chat_id] = call_id
            active_group_recording = {
                "chat_id": chat_id,
                "call_id": call_id,
                "output": output,
                "capture": capture,
                "metadata_path": metadata_path,
                "metadata": metadata,
                "started_at": started_at,
                "trigger": trigger,
                "request": request,
            }
            group_call_closed_event.clear()
            metadata.update({
                "status": "recording",
                "recording_started_at": iso_utc(started_at),
            })
            write_metadata(metadata_path, metadata)
            log(f"group-call: started recording {chat_id} call {call_id} ({trigger})")
            return True

        async def finish_group_recording(reason: str):
            """Finalize an active group recording."""
            nonlocal active_group_recording
            if active_group_recording is None:
                return
            current = active_group_recording
            chat_id = current["chat_id"]
            metadata = current["metadata"]
            recording_ended_at = iso_utc()
            wall_duration = (datetime.now(timezone.utc) - current["started_at"]).total_seconds()

            with contextlib.suppress(Exception):
                await calls.leave_call(chat_id)

            output = current["output"]
            capture = current["capture"]
            finalized = await finalize_mp3_capture(capture, output)
            status = "complete" if finalized["status"] == "complete" else "conversion_failed"
            media_duration = finalized.get("duration_seconds") or wall_duration

            metadata.update({
                "status": status,
                "recording_ended_at": recording_ended_at,
                "duration_seconds": media_duration,
                "wall_duration_seconds": round(wall_duration, 3),
                "stop_reason": reason,
            })
            metadata["audio"]["bytes"] = finalized["output_bytes"]
            metadata["audio"]["settled"] = finalized["status"] == "complete"
            metadata["audio"]["source"].update({
                "bytes": finalized["source_bytes"],
                "retained": finalized["source_retained"],
            })
            metadata["audio"]["conversion"].update({
                "status": finalized["status"],
                "error": finalized["error"],
            })
            if finalized.get("cleanup_error"):
                metadata["audio"]["source"]["cleanup_error"] = finalized["cleanup_error"]
            write_metadata(current["metadata_path"], metadata)

            log(f"group-call: finished {chat_id} call {current['call_id']} "
                f"({media_duration:.1f}s, {finalized['output_bytes']} bytes) — {reason}")

            if metadata["delivery"]["enabled"] and finalized["status"] == "complete":
                await send_recording_to_chat(
                    client,
                    chat_id,
                    output,
                    current["metadata_path"],
                    metadata,
                    emit_event_fn=lambda event, **fields: log(f"group-call-delivery: {event} {fields}"),
                )

            active_group_recording = None
            group_call_closed_event.clear()

        @calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def group_call_closed(_call_client: PyTgCalls, update: ChatUpdate):
            if active_group_recording is not None and update.chat_id == active_group_recording["chat_id"]:
                group_call_closed_event.set()

        async def watch_groups_loop():
            """Background task: poll for active group calls and process requests."""
            POLL_SECONDS = 2.0
            while not closing:
                # These sets are deliberately refreshed in place: a settings
                # reload changes the next poll without restarting PyTgCalls or
                # disturbing a recording already in progress.
                current_groups = configured_call_recording_groups()
                auto_groups.clear()
                auto_groups.update(current_groups["auto"])
                on_request_groups.clear()
                on_request_groups.update(current_groups["on_request"])
                send_to_chat_groups.clear()
                send_to_chat_groups.update(current_groups["send_to_chat"])
                # Finish active recording if call ended
                if active_group_recording is not None:
                    try:
                        await asyncio.wait_for(group_call_closed_event.wait(), timeout=POLL_SECONDS)
                    except asyncio.TimeoutError:
                        pass
                    if group_call_closed_event.is_set():
                        await finish_group_recording("voice_chat_closed")
                        continue

                    # Verify call still active
                    chat_id = active_group_recording["chat_id"]
                    call_ref = await active_group_call(chat_id)
                    if call_ref is None or int(call_ref.id) != active_group_recording["call_id"]:
                        group_call_closed_event.set()
                        await finish_group_recording("voice_chat_closed")
                        continue
                    continue

                # Process on-request recording files
                for path in sorted(CALL_RECORDING_REQUEST_DIR.glob("*.json")):
                    request = None
                    try:
                        request = json.loads(path.read_text())
                    except Exception:
                        pass
                    with contextlib.suppress(OSError):
                        path.unlink()
                    if not isinstance(request, dict):
                        continue
                    try:
                        req_chat_id = int(request.get("chat_id"))
                    except (TypeError, ValueError):
                        continue
                    if req_chat_id not in on_request_groups and req_chat_id not in auto_groups:
                        continue
                    if await start_group_recording(req_chat_id, "on_request", request):
                        break

                # Auto groups: poll for active calls
                if active_group_recording is None:
                    for chat_id in sorted(auto_groups):
                        call_ref = await active_group_call(chat_id)
                        if call_ref is None:
                            recorded_call_ids.pop(chat_id, None)
                            continue
                        if recorded_call_ids.get(chat_id) == int(call_ref.id):
                            continue
                        if await start_group_recording(chat_id, "automatic"):
                            break

                # Wait for next poll
                if active_group_recording is None:
                    await asyncio.sleep(POLL_SECONDS)

        group_watcher_task = asyncio.create_task(watch_groups_loop())
        log(f"group-call watcher: started; auto={sorted(auto_groups)}; on_request={sorted(on_request_groups)}")

    # Start PyTgCalls (shared by p2p and group recording)
    if calls is not None:
        with contextlib.redirect_stdout(sys.stderr):
            await calls.start()

        # Every state other than CONNECTED is raised as one argument-less
        # exception, so FAILED, TIMEOUT and CLOSED — three different causes —
        # arrive indistinguishable. The media stack names the establishment of a
        # connection but not the transition that ends one, which is the half
        # that matters when a leg drops out of a live call.
        _handle_connection_changed = calls._handle_connection_changed

        async def log_connection_changed(chat_id, net_state):
            log(f"call-net: chat_id={chat_id} state={net_state.state} "
                f"kind={net_state.kind}")
            return await _handle_connection_changed(chat_id, net_state)

        calls._handle_connection_changed = log_connection_changed
        features = []
        if has_p2p_recording:
            features.append(f"p2p(allowed_callers={sorted(allowed_callers)})")
        if has_voice_agent:
            key_state = (GEMINI_SECRET_ENV if _env_value(GEMINI_SECRET_ENV)
                         else f"NO {GEMINI_SECRET_ENV}")
            prompt_state = ("prompt" if read_voice_context()
                            else f"NO {VOICE_CONTEXT_FILE}")
            features.append(
                f"voice_agent(callers={sorted(voice_users)}, {key_state}, {prompt_state})")
        if has_group_recording:
            features.append(f"groups(auto={len(auto_groups)}, on_request={len(on_request_groups)})")
        log(f"call listener: started on daemon client; {', '.join(features)}")

    log("live — reacting in real time. Ctrl-C to stop.")
    sync_task = asyncio.create_task(periodic_sync())
    disconnected_task = asyncio.create_task(client.run_until_disconnected())
    try:
        done, _ = await asyncio.wait(
            (sync_task, disconnected_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if sync_task in done:
            await sync_task
            raise SessionUnhealthy("periodic Telegram sync stopped unexpectedly")
        await disconnected_task
    finally:
        closing = True
        # Stream-loss finalisation is deliberately detached from the Gemini
        # receiver so stop() cannot cancel its own caller. It is still owned by
        # this session: reconnect or shutdown waits for it, then finalises any
        # other active call and flushes its summary before workers are killed.
        if has_p2p_calls:
            finalisers = list(stream_loss_finalisers)
            if finalisers:
                await asyncio.gather(*finalisers, return_exceptions=True)
            if active_voice_call.get("session") is not None:
                try:
                    await finish_voice_call("session_closing")
                except Exception as exc:
                    log(f"voice: session-close finalisation failed — {_short_error(exc)}")
            try:
                await flush_call_summary()
            except Exception as exc:
                log(f"voice: session-close summary flush failed — {_short_error(exc)}")
        cleanup_tasks = [sync_task, disconnected_task]
        if group_watcher_task is not None:
            cleanup_tasks.append(group_watcher_task)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        pending_timers = list(timers.values())
        for task in pending_timers:
            task.cancel()
        timers.clear()
        if pending_timers:
            await asyncio.gather(*pending_timers, return_exceptions=True)
        kill_all_workers("session closing")
        pending_runners = list(runners.values())
        for task in pending_runners:
            task.cancel()
        if pending_runners:
            await asyncio.gather(*pending_runners, return_exceptions=True)
        runners.clear()
        kill_all_workers("session closing cleanup")


async def stop_call_recorder_process(process, reason):
    if process is None or process.returncode is not None:
        return
    log(f"call recorder: stopping ({reason})")
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        log("call recorder: SIGTERM timeout; killing process group")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def supervise_call_recorder():
    command = call_recorder_command()
    if command is None:
        return
    backoff = 2
    process = None
    try:
        while True:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                log(f"call recorder: cannot start ({_short_error(exc)}); retrying in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            groups = configured_call_recording_groups()
            log(f"call recorder: started pid={process.pid} auto={groups['auto']} "
                f"on_request={groups['on_request']} send_to_chat={groups['send_to_chat']}")
            return_code = await process.wait()
            process = None
            log(f"call recorder: exited code={return_code}; retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    finally:
        await stop_call_recorder_process(process, "telegram daemon shutdown")


async def main():
    """Supervise the session so neither a transient crash nor a missing login kills the
    daemon. Telegram's rolling MTProto schema can diverge from Telethon's generated
    layer, so getDifference can raise TypeNotFoundError mid-run; reconnect with backoff
    and watermark reconciliation recover what was missed. A not-yet-authorized session
    (first deploy on the box, or a revoked session) is not fatal either — the container
    stays up and retries so `telegram login` can be run inside it. Complements the box's
    `restart: unless-stopped`."""
    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()

    shutdown_requested = False

    def request_shutdown():
        nonlocal shutdown_requested
        shutdown_requested = True
        main_task.cancel()

    def request_reload():
        reload_runtime_settings()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_shutdown)
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGHUP, request_reload)
    lock_handle = None
    wrote_pid = False
    call_recorder_task = None
    try:
        if not CHANNEL_ENABLED:
            log("telegram channel disabled (TELEGRAM_SERVICE_ENABLED not truthy) — idling; "
                "set it true on instances that should run the channel")
            await asyncio.Event().wait()           # stay alive so restart:unless-stopped doesn't loop
            return
        if EXPECTED_ACCOUNT_ID is None:
            sys.exit(
                f"Telegram connection {CONNECTION!r} has no positive expected_account_id")
        api_id, api_hash = resolve_creds()
        CONNECTION_STATE_DIR.mkdir(parents=True, exist_ok=True)   # telethon opens the session sqlite here;
        SERVICE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        _require_hardened_state()
        lock_handle = acquire_daemon_lock()
        _atomic_json(OWNER_FILE, {
            **_owner_provenance(), "started_at": now(),
        })
        backoff = 2                                 # the connection-namespace dir may not exist yet (fresh volume)
        PID_FILE.write_text(f"{os.getpid()}\n")
        wrote_pid = True
        write_health("starting", last_error=None)
        if call_recorder_command() is not None:
            call_recorder_task = asyncio.create_task(supervise_call_recorder())
        while True:
            client = TelegramClient(str(SESSION), api_id, api_hash)
            try:
                await run_session(client)
                backoff = 2                       # clean disconnect — reset
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except NotAuthorized as e:
                write_health("unhealthy", last_error=str(e))
                log(f"{e} — container stays up; retrying in 15s")
                await asyncio.sleep(15)
            except SessionUnhealthy as e:
                write_health("unhealthy", last_error=str(e))
                log(f"{e}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except IdentityMismatch as e:
                write_health("unhealthy", last_error=str(e),
                             expected_account_id=EXPECTED_ACCOUNT_ID)
                log(str(e))
                raise
            except Exception as e:
                write_health("unhealthy", last_error=_short_error(e))
                log(f"session crashed ({_short_error(e, 120)}) — "
                    f"likely Telegram TL-layer drift; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
    except (KeyboardInterrupt, asyncio.CancelledError):
        if not shutdown_requested:
            raise
        log("shutdown requested; stopping telegram daemon")
    finally:
        with contextlib.suppress(Exception):
            write_health("stopped", stopped_at=now())
        if call_recorder_task is not None:
            call_recorder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_recorder_task
        if wrote_pid:
            with contextlib.suppress(OSError):
                if PID_FILE.read_text().strip() == str(os.getpid()):
                    PID_FILE.unlink()
        if lock_handle is not None:
            with contextlib.suppress(Exception):
                owner = json.loads(OWNER_FILE.read_text())
                if (isinstance(owner, dict)
                        and owner.get("launch_nonce") == LAUNCH_NONCE
                        and owner.get("pid") == os.getpid()):
                    OWNER_FILE.unlink()
            with contextlib.suppress(Exception):
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                lock_handle.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
