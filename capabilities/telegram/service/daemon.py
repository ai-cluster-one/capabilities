#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "telethon==1.43.2",
#     "py-tgcalls==2.3.3",
# ]
# ///
"""
Telegram assistant daemon — the persistent MTProto process (push, not polling).

This engine is shipped in the installed telegram capability bundle. A project
keeps only its policy/config under capabilities/telegram/service/:
  settings.json — connection, direct_messages, allowed_users, allowed_groups,
                  defaults
  context.md    — soft-gate prompt injected into every worker turn

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
import html
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
from telethon.tl.types import MessageActionConferenceCall, MessageActionInviteToGroupCall, MessageService, UpdateNewMessage

import pytgcalls
from pytgcalls import PyTgCalls, filters
from pytgcalls.types import CallConfig, ChatUpdate, RecordStream

# Import shared call recording helpers (same directory)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from call_recording_helpers import (
    finalize_mp3_capture,
    iso_utc,
    send_recording_to_chat,
    write_metadata,
)
sys.path.pop(0)

logging.getLogger("telethon").setLevel(logging.CRITICAL)

HERE = Path(__file__).resolve().parent
WORKER_BIN = HERE / "worker-bin"
CALL_RECORDER_BIN = HERE / "call_recorder.py"
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


def _project_capabilities_dir():
    current = PROJECT_ROOT / "capabilities"
    legacy = PROJECT_ROOT / ".capabilities"
    if (current / "settings.json").is_file() or not legacy.is_dir():
        return current
    return legacy


PROJECT_CAPABILITIES_DIR = _project_capabilities_dir()
SERVICE_DIR = PROJECT_CAPABILITIES_DIR / "telegram" / "service"
SETTINGS_FILE = Path(os.environ.get("TELEGRAM_SERVICE_SETTINGS")
                     or SERVICE_DIR / "settings.json")
CONTEXT_FILE = Path(os.environ.get("TELEGRAM_SERVICE_CONTEXT")
                    or SERVICE_DIR / "context.md")

# Per-instance channel toggle. Default on (opt-out) — set falsey on instances
# that should not consume the Telegram channel.
CHANNEL_ENABLED = os.environ.get(
    "TELEGRAM_SERVICE_ENABLED",
    os.environ.get("TELEGRAM_CHANNEL_ENABLED", "true"),
).strip().lower() \
    in ("1", "true", "yes", "on")


def _load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except OSError:
        sys.exit(f"Telegram service settings not found: {SETTINGS_FILE}")
    except json.JSONDecodeError as e:
        sys.exit(f"{SETTINGS_FILE} is not valid JSON: {e}")


# --- static policy (project) --------------------------------------------------
SETTINGS = _load_settings()
ALLOWED = SETTINGS.get("allowed_users", {})           # {telegram_id(str): {...}}
ALLOWED_GROUPS = SETTINGS.get("allowed_groups", {})   # {chat_id(str): {...}}
DIRECT_MESSAGES = SETTINGS.get("direct_messages", {})
DIRECT_MESSAGE_MODE = str(DIRECT_MESSAGES.get("mode") or "allowed_users").strip().lower()
ALLOW_ANY_DIRECT = DIRECT_MESSAGE_MODE in ("anyone", "all", "open", "public")
DIRECT_DEFAULT_ROLE = DIRECT_MESSAGES.get("default_role") or "direct_user"
DEFAULTS = SETTINGS.get("defaults", {})
ASSISTANT_NAME = SETTINGS.get("assistant_name") or DEFAULTS.get("assistant_name") or "Assistant"
DEFAULT_GROUP_ALIASES = tuple(DEFAULTS.get("group_aliases") or (ASSISTANT_NAME,))
CONTROL_DEFAULTS = {
    "roles": {
        "supervisor": {"commands": ["status", "set", "stop", "help"]},
        "channel_admin": {"commands": ["status", "set", "help"]},
        "direct_user": {"commands": ["status", "help"]},
        "group_member": {"commands": ["status", "help"]},
    }
}
DEFAULT_WORKER = (
    os.environ.get("TELEGRAM_SERVICE_WORKER")
    or os.environ.get("TG_WORKER")
    or DEFAULTS.get("worker")
    or "stub"
).strip().lower()
if DEFAULT_WORKER not in WORKER_NAMES:
    DEFAULT_WORKER = "stub"


def _positive_seconds(name, default, minimum=0.01):
    try:
        return max(minimum, float(DEFAULTS.get(name, default)))
    except (TypeError, ValueError):
        return float(default)


SYNC_INTERVAL = _positive_seconds("sync_interval", 20)
SYNC_STALE_AFTER = max(
    SYNC_INTERVAL * 2,
    _positive_seconds("sync_stale_after", 60),
)


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
LOCK_FILE = SERVICE_STATE_DIR / "daemon.lock"
PID_FILE = SERVICE_STATE_DIR / "daemon.pid"
LOG_FILE = SERVICE_STATE_DIR / "daemon.log"
HEALTH_FILE = SERVICE_STATE_DIR / "health.json"
PROGRESS_DIR = SERVICE_STATE_DIR / "progress"
WORKER_SESSION_DIR = SERVICE_STATE_DIR / "worker-sessions"
AUTHORITY_DIR = SERVICE_STATE_DIR / "authority"
CALL_RECORDING_REQUEST_DIR = SERVICE_STATE_DIR / "call-recording-requests"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


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
        **updates,
    })
    if state is not None:
        health["state"] = state
    SERVICE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = HEALTH_FILE.with_name(f".{HEALTH_FILE.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(health, indent=2, ensure_ascii=False) + "\n")
    os.replace(temp, HEALTH_FILE)


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
    SERVICE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("w")
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


def call_recorder_command():
    # Group watching runs on the daemon's own PyTgCalls instance (not as a subprocess).
    # A second update-consuming connection suppresses p2p INCOMING_CALL delivery.
    return None


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
    if (policy or {}).get("require_reference") is False:
        return True
    if (_call_recording_mode(policy) == "on_request"
            and _command_name(_message_text(message)) == "/record"):
        return True
    if getattr(message, "mentioned", False):
        return True
    if _text_names_me(_message_text(message), me, policy):
        return True
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


# --- register (dynamic state: watermark + per-channel /set overrides) ---------
def load_register():
    return json.loads(REGISTER.read_text()) if REGISTER.exists() else {}


def save_register(reg):
    SERVICE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    REGISTER.write_text(json.dumps(reg, indent=2, ensure_ascii=False))


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
    if _job_id(message_id) in _job_map(reg, key):
        return True
    try:
        watermark = int(_channel_row(reg, key).get("last_processed_message_id") or 0)
        return int(message_id) <= watermark
    except (TypeError, ValueError):
        return False


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
    """Migrate old per-channel settings.model into workers.<active>.model."""
    changed = False
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


def channel_settings(reg, key):
    """Defaults (settings.json) overlaid by this channel's /set overrides."""
    row = reg.get(key, {})
    s = row.get("settings", {})
    worker = _active_worker(row)
    cfg = _worker_settings(row, worker)
    out = {
        "tail_size": s.get("tail_size", DEFAULTS.get("tail_size", 50)),
        "debounce": s.get("debounce", DEFAULTS.get("debounce", 3)),
        "worker_timeout": s.get("worker_timeout", DEFAULTS.get("worker_timeout", 90)),
        "progress_after": s.get("progress_after", DEFAULTS.get("progress_after", 15)),
        "max_parallel_jobs": s.get("max_parallel_jobs", DEFAULTS.get("max_parallel_jobs", 2)),
        "max_attempts": s.get("max_attempts", DEFAULTS.get("max_attempts", 3)),
        "worker": worker,
        "worker_settings": cfg,
        "model": cfg.get("model"),
    }
    if worker == "claude":
        out["effort"] = cfg.get("effort")
    elif worker == "codex":
        out["reasoning_effort"] = cfg.get("reasoning_effort")
        out["service_tier"] = cfg.get("service_tier")
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
    _, group_policy = _group_policy(key)
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
    if topic in ("voice-transcription", "voice_transcription", "voice"):
        _, group_policy = _group_policy(key)
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
        f"  worker <{_values(WORKER_CHOICES)}>",
        f"  model <{_model_hint(active)}>  (active worker)",
        "  reasoning <default|low|medium|high|xhigh>  (codex active)",
        "  effort <default|low|medium|high|xhigh|max>  (claude active)",
        "  speed <default|fast|priority>  (codex active)",
    ]
    _, group_policy = _group_policy(key)
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
    if k == "worker":
        worker = v.strip().lower()
        if worker not in WORKER_NAMES:
            raise ValueError(f"worker must be one of {_values(WORKER_CHOICES)}")
        s["worker"] = worker
        return f"worker = {worker}"
    if k in ("voice-transcription", "voice_transcription", "voice"):
        _, group_policy = _group_policy(key)
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
    raise ValueError("unknown setting; use tail, debounce, worker, model, reasoning, "
                     "effort, speed, service-tier, or <worker>.<setting>")


def _command_name(text):
    parts = text.strip().split()
    if not parts:
        return ""
    return parts[0].split("@", 1)[0].lower()


def _control_command_key(command):
    key = str(command or "").strip().split("@", 1)[0].lower().lstrip("/")
    return key if key in {"status", "set", "stop"} else "help"


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
    return "commands: /status, /stop, /set help"


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
#   {"reply": <text>, "meta": {harness, model, is_error,
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
    elif path and path.exists():
        try:
            text = path.read_text().strip()
        except OSError as e:
            text = f"[channel context file unreadable: {context_file}: {e}]"
        if text:
            parts.append(text)
    elif context_file:
        parts.append(f"[channel context file missing: {context_file}]")
    return "\n\n".join(parts).strip()


def build_prompt(tail, state=None):
    """Assemble the worker prompt: soft-gate context (context.md) + the daemon-resolved channel
    state (time, channel/harness, participants + roles, active settings, context-window size,
    previous-turn token usage) + the live tail. State is assembled here and passed in, so the
    worker reads its situation from the context, not by inferring it from chat history."""
    context = CONTEXT_FILE.read_text().strip() if CONTEXT_FILE.exists() else ""
    st = state or {}
    channel_context = (st.get("channel_context") or "").strip()
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
    if st.get("messages") is not None:
        ctx = f"Context window: {st['messages']} msgs (of max {s.get('tail_size', '?')})"
        if st.get("history_chars") is not None:
            ctx += f", ~{st['history_chars']} chars of history"
        lines.append(ctx)
    req = st.get("current_request") or {}
    if req:
        lines.append("Delivery: final reply is sent by the daemon "
                     + ("as a reply to the request message"
                        if req.get("reply_to") else "as a plain direct message"))
    pu = st.get("prev_usage")
    if pu:
        lines.append(f"Previous turn (rough context scale): input ~{pu.get('input')} tok., "
                     f"output {pu.get('output')} tok.")
    block = ("--- Channel state ---\n" + "\n".join(lines) + "\n\n") if lines else ""
    request_block = ""
    if req:
        request_block = "\n".join([
            "--- Current request ---",
            f"Message: #{req.get('message_id')}",
            f"From: {req.get('sender_name')} (role: {req.get('sender_role')})",
            f"Kind: {req.get('kind')}",
            "Answer this request only. Other addressed messages in the tail are separate jobs.",
            req.get("text") or "",
            "",
        ])
    history = "\n".join(
        f'[{m.get("id")}] {m["sender"]}: {m["text"]}' if m.get("id") else f'{m["sender"]}: {m["text"]}'
        for m in tail)
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


def worker_env(state=None):
    st = state or {}
    env = os.environ.copy()
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
    req = st.get("current_request") or {}
    if req.get("reply_to"):
        env["TELEGRAM_PROGRESS_REPLY_TO"] = str(req["reply_to"])
    return env


def _kill_process_group(proc):
    """Kill the process group created for a worker, even if its leader already exited."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False


def run_worker_proc(chat, cmd, procs, env=None, cancel_event=None):
    """Run a worker subprocess in its own process group (start_new_session) and register it in
    the caller's `procs` map until the async job finalizes, so /stop can SIGKILL the whole group —
    claude/codex spawn children, so killing the group, not just the lone parent, is what stops
    the run. Returns (returncode, stdout, stderr); a killed run comes back with a negative
    returncode, which the caller raises on like any nonzero exit."""
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("worker cancelled before process start")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True,
                            cwd=str(PROJECT_ROOT), env=env)
    procs[chat] = proc
    # Cancellation can race with Popen. Register first, then honor a cancellation
    # that arrived while the process was being created so no late process escapes.
    if cancel_event is not None and cancel_event.is_set():
        _kill_process_group(proc)
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
    model = ((state or {}).get("settings") or {}).get("model")
    if model:
        cmd += ["--model", model]
    effort = ((state or {}).get("settings") or {}).get("effort")
    if effort:
        cmd += ["--effort", effort]
    proc_key = ((state or {}).get("proc_key") or chat)
    rc, out, err = run_worker_proc(
        proc_key, cmd, procs, env=worker_env(state),
        cancel_event=(state or {}).get("cancel_event"))
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
    a git repo. The final message comes from -o; --json carries usage metadata on stdout."""
    fd, out = tempfile.mkstemp(prefix="tg-codex-", suffix=".txt")
    os.close(fd)
    try:
        cmd = ["codex", "exec", build_prompt(tail, state),
               "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
               "--json", "--color", "never", "-o", out]
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
            cancel_event=(state or {}).get("cancel_event"))
        if rc != 0:
            raise RuntimeError(f"codex worker failed: {err.strip()[:200]}")
        reply = Path(out).read_text().strip()
        if not reply:
            raise RuntimeError("codex worker produced no final message")
        return {"reply": reply, "meta": _codex_meta(stdout_txt, model)}
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


WORKERS = {"stub": worker_stub, "claude": worker_claude, "codex": worker_codex}


class NotAuthorized(Exception):
    """No usable session yet — the supervisor keeps the container alive and retries so
    the one-time `telegram login` can run inside it (the box starts before login exists)."""


class SessionUnhealthy(Exception):
    """The MTProto client is alive but no longer decoding Telegram updates reliably."""


class WorkerTimedOut(Exception):
    """The worker future exceeded its configured deadline without being cancelled."""


async def run_session(client):
    await client.connect()
    if not await client.is_user_authorized():
        raise NotAuthorized(
            f"session not authorized — exec in and run: telegram login --connection {CONNECTION}")
    me = await client.get_me()
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

    def message_chunks(text):
        """Split outbound text without exceeding Telegram's message length limit."""
        text = str(text)
        if len(text) <= TELEGRAM_MESSAGE_LIMIT:
            return [text]

        chunks = []
        while len(text) > TELEGRAM_MESSAGE_LIMIT:
            boundary = max(
                text.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT + 1),
                text.rfind(" ", 0, TELEGRAM_MESSAGE_LIMIT + 1),
            )
            if boundary <= 0:
                boundary = TELEGRAM_MESSAGE_LIMIT
            else:
                boundary += 1  # Keep the whitespace with the preceding chunk.
            chunks.append(text[:boundary])
            text = text[boundary:]
        chunks.append(text)
        return chunks

    async def send_channel_message(ent_id, text, is_direct, reply_to=None, **kwargs):
        sent = None
        sent_ids = []
        for chunk in message_chunks(text):
            if is_direct or reply_to is None:
                sent = await client.send_message(ent_id, chunk, **kwargs)
            else:
                sent = await client.send_message(ent_id, chunk, reply_to=reply_to, **kwargs)
            sent_ids.append(sent.id)
        return sent, sent_ids

    async def handle_call_recording_request(key, message, group_policy, chat_ref):
        text = _message_text(message)
        if not _is_call_recording_request(text):
            return False
        mode = _call_recording_mode(group_policy)
        if mode == "disabled" and _command_name(text) != "/record":
            return False
        profile = await _sender_profile(message, group_policy, direct=False)
        if mode == "on_request":
            request_path = queue_call_recording_request(key, message.id, profile)
            reply = "Пробую присоединиться к активному звонку и начать запись."
            log(f"{key}: call recording requested by {profile['id']} ({request_path.name})")
        elif mode == "auto":
            reply = "Для этой группы уже включена автоматическая запись звонков."
            log(f"{key}: call recording request received; automatic mode is enabled")
        else:
            reply = "Запись звонков отключена в настройках этой группы."
            log(f"{key}: call recording request denied; group mode is disabled")
        row = reg.setdefault(key, {})
        row["last_processed_message_id"] = max(
            message.id, row.get("last_processed_message_id", 0))
        save_register(reg)
        _, _ = await send_channel_message(chat_ref, reply, False, reply_to=message.id)
        return True

    async def drain_progress(key, outbox, ent_id, is_direct, reply_to, offset):
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
            _, _ = await send_channel_message(ent_id, text, is_direct, reply_to=None if is_direct else reply_to)
            log(f"{key}: progress job msg={reply_to or 'direct'} «{text[:80]}»")
        return offset

    async def pump_progress(key, outbox, ent_id, is_direct, reply_to, stop_event):
        offset = 0
        while not stop_event.is_set():
            offset = await drain_progress(key, outbox, ent_id, is_direct, reply_to, offset)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
        await drain_progress(key, outbox, ent_id, is_direct, reply_to, offset)

    def reserve_job(key, message, group_policy, is_direct, reason):
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
        default_role = (DIRECT_DEFAULT_ROLE if is_direct else
                        (group_policy or {}).get("member_role")
                        or (group_policy or {}).get("role") or "group_member")
        sender_role = allowed.get("role") or member.get("role") or default_role
        job = {
            "message_id": message.id,
            "chat_id": key,
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

    async def build_tail_and_participants(key, ent_id, s, group_policy, is_direct, job):
        raw = await client.get_messages(ent_id, limit=s["tail_size"])
        tail = []
        echo_senders = _voice_echo_senders(reg, key)
        for m in reversed(raw or []):
            if not _tail_in_scope(m, group_policy):
                continue
            t = message_tail_text(m)
            if t is None:
                continue
            if m.out:
                # Check if this outgoing message is a recorded voice echo
                sender = echo_senders.get(str(m.id), ASSISTANT_NAME)
            else:
                sender = (await _sender_profile(m, group_policy, direct=is_direct))["name"]
            tail.append({"id": m.id, "sender": sender, "text": t})

        profiles = {}
        for m in raw or []:
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
        _, group_policy = _group_policy(key)
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
            current_request = {
                "message_id": job.get("message_id"),
                "sender_id": job.get("sender_id"),
                "sender_name": job.get("sender_name"),
                "sender_role": job.get("sender_role"),
                "kind": job.get("kind"),
                "text": job.get("text"),
                "reply_to": None if is_direct else job.get("message_id"),
            }
            PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
            progress_outbox = PROGRESS_DIR / f"{_safe_file_part(key)}-{job.get('message_id')}.jsonl"
            with contextlib.suppress(OSError):
                progress_outbox.unlink()
            worker_session = prepare_worker_session(key, job.get("message_id"))
            authority = _authority_policy_for(job, group_policy, is_direct)
            channel_context = _channel_context_from_policy(group_policy)
            if authority is not None:
                AUTHORITY_DIR.mkdir(parents=True, exist_ok=True)
                auth_path = AUTHORITY_DIR / f"{_safe_file_part(key)}-{job.get('message_id')}.json"
                auth_path.write_text(
                    json.dumps(authority, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                authority_context = str(auth_path)
            state = {"now": now_display(), "chat_id": key, "connection": CONNECTION,
                     "chat_type": "private" if is_direct else "group",
                     "chat_name": (group_policy or {}).get("name"),
                     "harness": s["worker"], "participants": participants, "settings": s,
                     "messages": len(tail),
                     "history_chars": sum(len(m["text"]) for m in tail),
                     "channel_context": channel_context,
                     "prev_usage": reg.get(key, {}).get("last_usage"),
                     "current_request": current_request,
                     "authority": authority,
                     "authority_context": authority_context,
                     "progress_outbox": str(progress_outbox),
                     "worker_session": worker_session,
                     "cancel_event": cancel_event,
                     "proc_key": proc_key}
            log(f"{key}: dispatch job msg={job.get('message_id')} tail={s['tail_size']} "
                f"worker={s['worker']} model={s['model'] or 'default'} msgs={len(tail)}")
            loop = asyncio.get_running_loop()
            progress_stop = asyncio.Event()
            progress_task = asyncio.create_task(
                pump_progress(key, str(progress_outbox), ent_id, is_direct,
                              job.get("message_id"), progress_stop))
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
            
            # Discover and send Codex-generated images
            images = []
            if meta.get("harness") == "codex" and meta.get("session_id"):
                images = discover_codex_images(meta["session_id"])
            
            reply_msg_id = None if is_direct else job.get("message_id")
            if images:
                try:
                    if len(reply) <= 1024:
                        sent = await client.send_file(
                            ent_id, images, caption=reply,
                            reply_to=reply_msg_id)
                    else:
                        sent = await client.send_file(
                            ent_id, images, reply_to=reply_msg_id)
                        _, _ = await send_channel_message(ent_id, reply, is_direct, reply_to=reply_msg_id)
                    image_count = len(images) if isinstance(images, list) else 1
                    log(f"{key}: sent {image_count} image(s) with reply job msg={job.get('message_id')}")
                except Exception as send_err:
                    log(f"{key}: image send failed, delivering text only: {send_err}")
                    sent, _ = await send_channel_message(ent_id, reply, is_direct, reply_to=reply_msg_id)
            else:
                sent, _ = await send_channel_message(ent_id, reply, is_direct, reply_to=reply_msg_id)
            
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
            _, _ = await send_channel_message(
                ent_id, notice, is_direct, reply_to=None if is_direct else job.get("message_id"))
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

    async def echo_voice_message(message, chat_id, key, is_direct, sender_name=None):
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
                reply_to=None if is_direct else message.id)
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
        try:
            cid = int(key)
        except ValueError:
            return None
        try:
            return await client.get_input_entity(cid)
        except Exception:
            pass
        try:
            async for dialog in client.iter_dialogs(limit=500):
                if str(dialog.id) == key:
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
                cid = int(key)
            except ValueError:
                continue
            _, group_policy = _group_policy(key)
            if cid < 0 and group_policy is None:
                continue
            wm = reg.get(key, {}).get("last_processed_message_id", 0)
            try:
                checked += 1
                raw = await client.get_messages(ent, limit=channel_settings(reg, key)["tail_size"])
            except Exception as e:
                log(f"{key}: catch-up skipped; cannot resolve chat: {_short_error(e)}")
                failures += 1
                if _is_tl_layer_error(e):
                    tl_failures += 1
                continue
            enqueued = 0
            for m in reversed(raw):
                if getattr(m, "out", False):
                    continue
                if getattr(m, "action", None) is not None:
                    continue
                if _message_is_known(reg, key, m.id):
                    continue
                is_direct = group_policy is None
                addressed = False
                if group_policy is not None:
                    addressed = await _message_addresses_me(m, me, group_policy)
                    is_ambient_voice = (not addressed
                                        and _is_telegram_voice_note(m)
                                        and _voice_transcription_mode(group_policy, reg, key) == "auto")
                    if not addressed and not is_ambient_voice:
                        continue
                elif not _incoming_in_scope(m, group_policy):
                    continue
                else:
                    is_ambient_voice = False
                if group_policy is not None and await handle_call_recording_request(
                        key, m, group_policy, ent):
                    continue
                if is_ambient_voice:
                    job = reserve_job(key, m, group_policy, is_direct, f"catch-up/{reason}/ambient")
                    if job is None:
                        continue
                    profile = await _sender_profile(m, group_policy, direct=is_direct)
                    spoken = await echo_voice_message(m, ent, key, is_direct, sender_name=profile["name"])
                    # If transcript names the assistant, finalize and dispatch
                    if spoken and spoken != "[голосовое — не удалось расшифровать]" and _text_names_me(spoken, me, group_policy):
                        if await finalize_job(
                                key, m, group_policy, is_direct, f"catch-up/{reason}/ambient-addressed", job,
                                text_override=spoken):
                            enqueued += 1
                        continue
                    # Otherwise echo-only, no worker
                    mark_job_finished(key, job, "done")
                    continue
                job = reserve_job(key, m, group_policy, is_direct, f"catch-up/{reason}")
                if job is None:
                    continue
                spoken = None
                if _is_spoken_media(m):
                    profile = await _sender_profile(m, group_policy, direct=is_direct)
                    sender_name = None if is_direct else profile["name"]
                    spoken = await echo_voice_message(m, ent, key, is_direct, sender_name=sender_name)
                if await finalize_job(
                        key, m, group_policy, is_direct, f"catch-up/{reason}", job,
                        text_override=spoken):
                    enqueued += 1
            if enqueued:
                total_enqueued += enqueued
                log(f"{key}: catch-up/{reason} enqueued {enqueued} since watermark {wm}")
                arm(key, ent)
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
        access = await _event_access(event, me, reg)          # gate 1: the door
        if not access:
            log(f"{event.chat_id}: ignored {_message_kind(event.message)} from {event.sender_id}")
            return
        if getattr(event.message, "action", None) is not None:
            return
        key = str(event.chat_id)
        text = _message_text(event.message)
        group_policy = access.get("policy")
        is_direct = access["kind"] == "private"
        chat_ref = await _event_chat_ref(event, is_direct=is_direct)
        if _message_is_known(reg, key, event.message.id):
            log(f"{key}: already queued/done msg={event.message.id}")
            return
        if not is_direct and await handle_call_recording_request(
                key, event.message, group_policy, chat_ref):
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
            else:
                reply = handle_command(reg, key, text)
            row = reg.setdefault(key, {})
            row["last_processed_message_id"] = max(event.message.id, row.get("last_processed_message_id", 0))
            save_register(reg)
            if reply:
                _, _ = await send_channel_message(
                    chat_ref, reply, access["kind"] == "private",
                    reply_to=None if access["kind"] == "private" else event.message.id)
            return
        if access.get("ambient_voice"):                   # unaddressed voice in auto mode — check transcript
            job = reserve_job(key, event.message, group_policy, is_direct, "live/ambient")
            if job is None:
                log(f"{key}: already queued/done msg={event.message.id}")
                return
            profile = await _sender_profile(event.message, group_policy, direct=is_direct)
            spoken = await echo_voice_message(event.message, chat_ref, key, is_direct, sender_name=profile["name"])
            # If transcript names the assistant, dispatch as an addressed request
            if spoken and spoken != "[голосовое — не удалось расшифровать]" and _text_names_me(spoken, me, group_policy):
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
        job = reserve_job(key, event.message, group_policy, is_direct, "live")
        if job is None:
            log(f"{key}: already queued/done msg={event.message.id}")
            return
        spoken = None
        if _is_spoken_media(event.message):               # transcribe → echo (visible + attributed by reply)
            profile = await _sender_profile(event.message, group_policy, direct=is_direct)
            sender_name = None if is_direct else profile["name"]
            spoken = await echo_voice_message(event.message, chat_ref, key, is_direct, sender_name=sender_name)
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
    has_p2p_recording = bool(users["allowed_callers"])
    has_group_recording = bool(groups["auto"] or groups["on_request"])

    if has_p2p_recording or has_group_recording:
        calls = PyTgCalls(client)

    # P2P call recording (incoming calls from allowed direct-message callers)
    if has_p2p_recording:
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
                await calls.record(caller_id, RecordStream(capture), config=call_config)
                metadata["status"] = "recording"
                metadata["recording_started_at"] = iso_utc()
                write_metadata(metadata_path, metadata)
                log(f"call: recording {mode} from {caller_id} to {output}")
            except Exception as exc:
                log(f"call: failed to start {mode} recording from {caller_id}: {exc}")
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
            if active_recording["task"] is not None:
                log(f"call: ignoring incoming {mode} from {caller_id} — recorder busy")
                return
            task = asyncio.create_task(record_call(caller_id, mode, call_config))
            active_recording["task"] = task

        @calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def incoming_p2p_call(_call_client: PyTgCalls, update: ChatUpdate):
            caller_id = update.chat_id
            if caller_id not in allowed_callers:
                log(f"call: ignoring p2p from {caller_id} — not in allowed_callers")
                return
            await start_call_recording(caller_id, "p2p", CallConfig(timeout=60))

        @calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def call_ended(_call_client: PyTgCalls, update: ChatUpdate):
            if active_recording["caller_id"] != update.chat_id:
                return

            caller_id = update.chat_id
            output = active_recording["output"]
            capture = active_recording["capture"]
            metadata_path = active_recording["metadata_path"]
            metadata = active_recording["metadata"]
            started_at = active_recording["started_at"]
            mode = active_recording["mode"]

            log(f"call: ended from {caller_id}")

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

            finalized = await finalize_mp3_capture(capture, output)
            status = "complete" if finalized["status"] == "complete" else "conversion_failed"
            media_duration = finalized.get("duration_seconds") or wall_duration

            metadata.update({
                "status": status,
                "recording_ended_at": recording_ended_at,
                "duration_seconds": media_duration,
                "wall_duration_seconds": round(wall_duration, 3),
                "stop_reason": "call_closed",
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
            if caller_id is None or caller_id not in allowed_callers:
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
        features = []
        if has_p2p_recording:
            features.append(f"p2p(allowed_callers={sorted(allowed_callers)})")
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

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_shutdown)
    lock_handle = None
    wrote_pid = False
    call_recorder_task = None
    try:
        if not CHANNEL_ENABLED:
            log("telegram channel disabled (TELEGRAM_SERVICE_ENABLED not truthy) — idling; "
                "set it true on instances that should run the channel")
            await asyncio.Event().wait()           # stay alive so restart:unless-stopped doesn't loop
            return
        api_id, api_hash = resolve_creds()
        CONNECTION_STATE_DIR.mkdir(parents=True, exist_ok=True)   # telethon opens the session sqlite here;
        lock_handle = acquire_daemon_lock()
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
            with contextlib.suppress(OSError):
                if LOCK_FILE.read_text().strip() == str(os.getpid()):
                    LOCK_FILE.unlink()
            with contextlib.suppress(Exception):
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                lock_handle.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
