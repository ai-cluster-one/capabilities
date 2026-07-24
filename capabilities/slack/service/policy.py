"""Pure decision logic for the slack service daemon: event normalization,
the accept-gate (who/where is listened to), and the tier router
(answer vs relay). No network, no I/O — unit-testable in isolation."""

import re


def parse_event(payload_event: dict) -> dict | None:
    """Normalize a Slack event, or return None for anything not handled."""
    if not isinstance(payload_event, dict):
        return None
    etype = payload_event.get("type")
    # Ignore bot/self messages and edited/joined/etc. subtypes.
    if payload_event.get("bot_id") or payload_event.get("subtype"):
        return None
    if etype == "message" and payload_event.get("channel_type") == "im":
        kind = "im"
    elif etype == "app_mention":
        kind = "channel"
    else:
        return None
    user = payload_event.get("user")
    channel = payload_event.get("channel")
    ts = payload_event.get("ts")
    if not (user and channel and ts):
        return None
    return {"kind": kind, "user": user, "channel": channel, "ts": ts,
            "text": payload_event.get("text", ""),
            "thread_ts": payload_event.get("thread_ts")}


def accept(evt: dict, settings: dict) -> bool:
    """The accept-gate: admit an event per the inbound policy."""
    if evt is None:
        return False
    if evt["kind"] == "im":
        dm = settings.get("direct_messages") or {}
        if dm.get("mode") == "open":
            return True
        return evt["user"] in (settings.get("allowed_users") or {})
    # channel
    if settings.get("default_channel_policy") == "open":
        return True
    return evt["channel"] in (settings.get("allowed_channels") or {})


def route(evt: dict, settings: dict) -> str:
    """Tier decision: 'answer' (invoke worker) or 'relay' (inbox)."""
    if evt is None:
        return "relay"
    auto = settings.get("auto_answer") or {}
    if evt["kind"] == "im":
        return "answer" if evt["user"] in (auto.get("users") or []) else "relay"
    return "answer" if evt["channel"] in (auto.get("channels") or []) else "relay"


CONTROL_COMMANDS = ("stop", "status")


def conversation_key(evt: dict) -> str:
    """Serialization/queue key: the DM channel, or channel:thread-root for mentions."""
    if evt["kind"] == "im":
        return evt["channel"]
    root = evt.get("thread_ts") or evt["ts"]
    return f'{evt["channel"]}:{root}'


def resolve_role(evt: dict, settings: dict) -> str:
    entry = (settings.get("allowed_users") or {}).get(evt["user"])
    if isinstance(entry, dict) and entry.get("role"):
        return entry["role"]
    if evt["kind"] == "im":
        return (settings.get("direct_messages") or {}).get("default_role") or "default"
    centry = (settings.get("allowed_channels") or {}).get(evt["channel"])
    if isinstance(centry, dict) and centry.get("default_role"):
        return centry["default_role"]
    return "default"


def strip_mention(text: str) -> str:
    return re.sub(r"^\s*(?:<@[^>]+>\s*)+", "", text or "").strip()


def control_command(text: str) -> str | None:
    t = strip_mention(text).lstrip("/").strip().lower()
    return t if t in CONTROL_COMMANDS else None


def control_allowed(command: str, role: str, settings: dict) -> bool:
    roles = (settings.get("control") or {}).get("roles") or {}
    cmds = (roles.get(role) or {}).get("commands")
    if cmds is True or cmds == "*":
        return True
    if isinstance(cmds, list):
        allowed = {str(c).strip().lower().lstrip("/") for c in cmds}
        return "*" in allowed or command in allowed
    return False


def _ts_le(a, b) -> bool:
    try:
        return float(a) <= float(b)
    except (TypeError, ValueError):
        return str(a) <= str(b)


def select_catchup(messages, *, watermark, now_ts, max_age_seconds, max_messages):
    """Pure: pick which history messages to replay. Keeps ts > watermark and
    (if bounded) within max_age of now; returns oldest-first, capped to the
    most-recent max_messages."""
    out = []
    for m in messages or []:
        ts = m.get("ts")
        if ts is None:
            continue
        if watermark is not None and _ts_le(ts, watermark):
            continue
        if max_age_seconds is not None:
            try:
                if float(now_ts) - float(ts) > float(max_age_seconds):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(m)
    out.sort(key=lambda m: float(m["ts"]))
    if max_messages is not None and len(out) > max_messages:
        out = out[-int(max_messages):]
    return out
