"""Strict, dependency-free validation for Telegram assistant settings."""

import re
from pathlib import Path


TOP_LEVEL = {
    "connection", "environment", "assistant_name", "direct_messages",
    "allowed_users", "allowed_groups", "control", "authority", "defaults",
}
# Which half of the machine this daemon is. The same box runs development and
# production, and the job queue is filtered by this: without it a development
# job is eligible for a production runner.
ENVIRONMENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.IGNORECASE)
# What the media stack may be read at. A call under investigation is read at
# debug; anything a normal call needs is at info.
MEDIA_LOG_LEVELS = {"debug", "info", "warning", "error", "critical"}
DIRECT_MODES = {"allowed_users", "anyone", "all", "open", "public"}
VOICE_MODES = {"disabled", "enabled", "auto", "on"}
# Whether a channel may hand work to a runner. Off means the register is not
# offered to that turn at all, so the channel always answers synchronously.
DELEGATION_MODES = {"allowed", "disabled", "on", "enabled", "auto"}
VOICE_TOOLS = {"agent_task", "send_to_chat", "run_capability",
               "read_project_file", "reload_service"}
# Which speech stack answers a call. Each provider carries its own wire, its own
# prompts and its own defaults, so the two can be given different words and
# compared on their own terms rather than through one set of settings that
# happens to suit whichever was written first.
VOICE_PROVIDERS = {"gemini", "gptlive"}
# The project-owned documents each provider seeds, by the second half of their
# key. `<provider>-voice-agent` is addressed to the model that speaks and
# `<provider>-backend` to the worker behind it. `<provider>-delegation` is read
# where a project writes one and seeded for nobody: how a hand-off works is the
# capability's own, and this is only room to add to it.
VOICE_PROVIDER_DOCUMENTS = {
    "gemini": ("voice-agent",),
    "gptlive": ("voice-agent", "backend"),
}
VOICE_PROVIDER_OPTIONAL_DOCUMENTS = ("delegation",)
# The provider whose document keys predate the split, and whose prompts a
# project may already have written under the unsuffixed key.
VOICE_PROVIDER_INCUMBENT = "gemini"
VOICE_SESSION_MODES = {"carry", "fresh"}
TRANSCRIPTION_MODES = {"disabled", "auto"}
CALL_GROUP_MODES = {
    "disabled", "off", "none", "auto", "automatic", "on_request",
    "on-request", "request", "command", "manual",
}
CALL_USER_MODES = {"disabled", "off", "enabled", "auto", "on"}
CONTROL_COMMANDS = {"status", "set", "reload", "stop", "help", "*"}
CONTEXT_MODES = {"extend", "exclusive"}
# The mechanical triggers a group may switch off one by one, and the modes of
# its reply gate: `shadow` decides and logs, `live` acts.
GROUP_TRIGGERS = {"username", "alias", "reply"}
GATE_MODES = {"shadow", "live"}
GATE_HARNESSES = {"claude", "codex"}
# What becomes of work a restart interrupted: kept for inspection, or
# queued as a fresh attempt.
JOB_RECOVERY = {"inspect", "requeue"}

# The properties each object of the settings accepts, and the bounds of each
# number. The validator below and `json_schema` read the same names, so the
# schema a control panel is given is the one a reload holds the settings to.
DIRECT_MESSAGE_KEYS = {"mode", "default_role"}
TOPIC_KEYS = {"project", "profile", "context", "context_file", "context_mode"}
CAPABILITY_RULE_KEYS = {"allow", "deny", "enabled", "scope", "verbs", "connections"}
CONNECTION_GRANT_KEYS = {"allow_write"}
AUTHORITY_POLICY_KEYS = {"allowed_capabilities", "capabilities"}
AUTHORITY_KEYS = {"default", "roles"}
CONTROL_KEYS = {"roles"}
CONTROL_RULE_KEYS = {"commands"}
COMMAND_RULE_KEYS = {"allow", "deny", "enabled"}
CALL_RECORDING_USER_KEYS = {"mode"}
CALL_RECORDING_GROUP_KEYS = {"mode", "send_to_chat"}
VOICE_SESSION_KEYS = {"mode"}
VOICE_AGENT_KEYS = {"model", "voice", "greeting", "history", "tools", "provider"}
# The profile a call's task and the post-call summary run on. A participant's
# own voice entry has none: the voice task resolves its profile from the
# defaults alone.
VOICE_AGENT_DEFAULTS_KEYS = {"profile", "timezone", "progress_interval",
                             "recording_caption", "prompt_file", "session"}
VOICE_AGENT_PARTICIPANT_KEYS = {"mode"}
PARTICIPANT_KEYS = {
    "name", "username", "role", "may_address", "delegation",
    "control", "authority",
    "allowed_capabilities", "capabilities", "context", "context_file",
    "context_mode", "call_recording", "voice_agent", "project",
}
MEMBER_KEYS = {"kind", "address_aliases"}
# A user entry is the only declaration of the channel a person is alone in, so
# it is where that channel's worker window and profile belong. A group member
# has no channel of their own here: their turn runs in the group's, whose window
# and profile the group policy already owns.
USER_KEYS = {"worker_timeout", "profile"}
MEMBER_KINDS = {"human", "agent"}
GROUP_KEYS = {
    "name", "role", "member_role", "aliases", "address_aliases", "mentions",
    "require_reference", "members", "agent_dialogue", "worker_timeout", "profile",
    "voice_transcription", "call_recording", "delegation",
    "control", "authority",
    "allowed_capabilities", "capabilities", "context", "context_file",
    "context_mode", "project", "topics", "triggers", "gate",
}
AGENT_DIALOGUE_KEYS = {"max_turns", "reset_on_human_message"}
VOICE_TRANSCRIPTION_KEYS = {"mode"}
GATE_KEYS = {"enabled", "mode", "brief", "context_messages", "context_chars",
             "max_batch", "model", "effort"}
DELEGATION_KEYS = {"mode"}
DEFAULTS_KEYS = {
    "assistant_name", "tail_size", "sync_interval", "sync_stale_after",
    "debounce", "worker_timeout", "progress_after", "progress_from_stream",
    "max_parallel_dialogue", "max_parallel_jobs", "job_poll_interval",
    "job_recovery", "max_attempts", "group_aliases", "profile",
    "voice_agent", "delegation",
    "media_log_level", "link_preview",
}
# (minimum, maximum, integer) of each number, by where it sits.
CHANNEL_WORKER_TIMEOUT = (1, 3600, False)
VOICE_HISTORY = (0, 500, True)
# Zero is the setting's off position, not a bad value: the assistant still
# receives what the worker reports and can answer about it when asked, and
# simply stops volunteering it.
VOICE_PROGRESS_INTERVAL = (0, 3600, False)
AGENT_DIALOGUE_MAX_TURNS = (0, 100, True)
GATE_NUMBERS = {
    "context_messages": (0, 100, True),
    "context_chars": (0, 20000, True),
    "max_batch": (1, 100, True),
}
GATE_BRIEF_MAX = 2000
DEFAULTS_NUMBERS = {
    "tail_size": (1, 500, True),
    "sync_interval": (0.01, 3600, False),
    "sync_stale_after": (0.01, 86400, False),
    "debounce": (0, 300, False),
    "worker_timeout": (0.01, 3600, False),
    "progress_after": (0, 3600, False),
    "max_parallel_dialogue": (1, 32, True),
    "max_parallel_jobs": (1, 32, True),
    "job_poll_interval": (0.05, 300, False),
    "max_attempts": (1, 20, True),
}
# The top-level keys a running service cannot take on `service reload`. The
# connection owns the Telethon session and state directories, and the job
# register is opened once with the environment as its filter; every other key
# is adopted by a reload.
RESTART_KEYS = ("connection", "environment")


def _fail(path, message):
    raise ValueError(f"{path}: {message}")


def _object(value, path):
    if not isinstance(value, dict):
        _fail(path, "must be a JSON object")
    return value


def _unknown(value, allowed, path):
    for key in value:
        if key not in allowed:
            supported = ", ".join(sorted(allowed))
            _fail(f"{path}.{key}",
                  f"unsupported property {key!r}; supported properties: {supported}")


def _string(value, path, *, nullable=False, nonempty=False):
    if nullable and value is None:
        return
    if not isinstance(value, str):
        _fail(path, "must be a string" + (" or null" if nullable else ""))
    if nonempty and not value.strip():
        _fail(path, "must not be empty")


def _boolean(value, path):
    if not isinstance(value, bool):
        _fail(path, "must be a boolean")


def _number(value, path, minimum, maximum, *, integer=False, nullable=False):
    if nullable and value is None:
        return
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind):
        _fail(path, "must be " + ("an integer" if integer else "a number"))
    if not minimum <= value <= maximum:
        _fail(path, f"must be between {minimum} and {maximum}")


def _enum(value, choices, path, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or value not in choices:
        _fail(path, "must be one of: " + ", ".join(sorted(choices)))


def _string_list(value, path):
    if not isinstance(value, list):
        _fail(path, "must be a JSON array")
    for index, item in enumerate(value):
        _string(item, f"{path}[{index}]", nonempty=True)


def _safe_service_path(value, path, project_root, service_dir):
    if value is None:
        return
    _string(value, path, nonempty=True)
    candidate = Path(value).expanduser()
    candidate = candidate if candidate.is_absolute() else service_dir / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(service_dir.resolve())
        resolved.relative_to(project_root.resolve())
    except (OSError, ValueError):
        _fail(path, "must resolve inside the Telegram service directory")


def _route_project(value, path, project_root):
    """A worker route target, judged by its shape alone.

    Existence is deliberately not asserted here. A settings failure exits the
    daemon, so a neighbouring project that was renamed or unmounted would stop
    the service for every other channel. The dispatch check owns that failure
    and spends it on the one request that names the route.

    A target lies inside the home or is the agent's own folder, or inside it:
    on a node the agent lives at /app, outside the home.
    """
    _string(value, path, nonempty=True)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        _fail(path, "must be an absolute path, or start with ~")
    home = Path.home()
    own = Path(project_root)
    if home not in candidate.parents and candidate != own and own not in candidate.parents:
        _fail(path, f"must be a path inside {home}, or this agent's folder {own}")


def _topics(value, path, project_root, service_dir):
    """Per-topic overrides inside one group policy.

    A topic carries where its work runs, the profile its turns run on and what
    prose precedes it. It carries no authority tier: a topic inherits its
    group's rights and cannot narrow or widen them.
    """
    value = _object(value, path)
    for topic_id, entry in value.items():
        entry_path = f"{path}.{topic_id}"
        try:
            if int(topic_id) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            _fail(entry_path, "topic key must be a positive forum topic ID")
        entry = _object(entry, entry_path)
        _unknown(entry, TOPIC_KEYS, entry_path)
        if "project" in entry:
            _route_project(entry["project"], f"{entry_path}.project", project_root)
        if "profile" in entry:
            _profile(entry["profile"], f"{entry_path}.profile")
        _overlay(entry, entry_path, project_root, service_dir)


def _profile(value, path):
    """A profile name, judged by its shape alone. Whether it names a telegram
    profile, resolves and fits is the profile module's to say, at start, at
    reload and in the doctor, where the store can be read."""
    _string(value, path, nonempty=True)


def _capability_rule(value, path):
    if isinstance(value, bool) or value == "*":
        return
    if isinstance(value, list):
        _string_list(value, path)
        return
    value = _object(value, path)
    _unknown(value, CAPABILITY_RULE_KEYS, path)
    for key in ("allow", "deny", "enabled"):
        if key in value:
            _boolean(value[key], f"{path}.{key}")
    if "scope" in value:
        _string(value["scope"], f"{path}.scope", nonempty=True)
    if "verbs" in value:
        _string_list(value["verbs"], f"{path}.verbs")
    if "connections" in value:
        connections = value["connections"]
        if isinstance(connections, list):
            _string_list(connections, f"{path}.connections")
        else:
            connections = _object(connections, f"{path}.connections")
            for connection, grant in connections.items():
                _string(connection, f"{path}.connections key", nonempty=True)
                grant = _object(grant, f"{path}.connections.{connection}")
                _unknown(grant, CONNECTION_GRANT_KEYS,
                         f"{path}.connections.{connection}")
                if "allow_write" in grant:
                    _boolean(grant["allow_write"],
                             f"{path}.connections.{connection}.allow_write")


def _capabilities(value, path):
    if value is True or value == "*":
        return
    if isinstance(value, list):
        _string_list(value, path)
        return
    value = _object(value, path)
    for name, rule in value.items():
        _string(name, f"{path} key", nonempty=True)
        _capability_rule(rule, f"{path}.{name}")


def _authority_policy(value, path):
    value = _object(value, path)
    _unknown(value, AUTHORITY_POLICY_KEYS, path)
    for key in AUTHORITY_POLICY_KEYS & value.keys():
        _capabilities(value[key], f"{path}.{key}")


def _authority(value, path):
    value = _object(value, path)
    _unknown(value, AUTHORITY_KEYS, path)
    if "default" in value:
        _authority_policy(value["default"], f"{path}.default")
    if "roles" in value:
        roles = _object(value["roles"], f"{path}.roles")
        for role, policy in roles.items():
            _string(role, f"{path}.roles key", nonempty=True)
            _authority_policy(policy, f"{path}.roles.{role}")


def _control_rule(value, path):
    value = _object(value, path)
    _unknown(value, CONTROL_RULE_KEYS, path)
    if "commands" not in value:
        return
    commands = value["commands"]
    if commands is True or commands == "*":
        return
    if isinstance(commands, list):
        _string_list(commands, f"{path}.commands")
        unknown = set(commands) - CONTROL_COMMANDS
        if unknown:
            _fail(f"{path}.commands", f"unsupported command {sorted(unknown)[0]!r}")
        return
    commands = _object(commands, f"{path}.commands")
    for command, rule in commands.items():
        if command not in CONTROL_COMMANDS:
            _fail(f"{path}.commands.{command}", f"unsupported command {command!r}")
        if isinstance(rule, bool) or rule == "*":
            continue
        rule = _object(rule, f"{path}.commands.{command}")
        _unknown(rule, COMMAND_RULE_KEYS, f"{path}.commands.{command}")
        for key, item in rule.items():
            _boolean(item, f"{path}.commands.{command}.{key}")


def _control(value, path):
    value = _object(value, path)
    _unknown(value, CONTROL_KEYS, path)
    if "roles" in value:
        roles = _object(value["roles"], f"{path}.roles")
        for role, rule in roles.items():
            _string(role, f"{path}.roles key", nonempty=True)
            _control_rule(rule, f"{path}.roles.{role}")


def _call_recording(value, path, *, group):
    value = _object(value, path)
    _unknown(value, CALL_RECORDING_GROUP_KEYS if group else CALL_RECORDING_USER_KEYS, path)
    if "mode" in value:
        _enum(value["mode"], CALL_GROUP_MODES if group else CALL_USER_MODES,
              f"{path}.mode")
    if "send_to_chat" in value:
        _boolean(value["send_to_chat"], f"{path}.send_to_chat")


def _voice_tools(value, path):
    """Which tools a call declares to the model. Every one is off unless this
    names it: a tool the model is holding is a tool it will reach for, and the
    set that suits one project's calls is not the set that suits another's."""
    value = _object(value, path)
    _unknown(value, VOICE_TOOLS, path)
    for name in value:
        _boolean(value[name], f"{path}.{name}")


def _voice_session(value, path):
    """Whether a caller keeps one worker session or gets a new one every time.

    `carry` keeps the session a finished task ran in and offers it to the next
    task and the next call, so a follow-up builds on what was already found
    instead of paying for the same lookups again. Nothing expires it; `/reload`
    is the way to a new one. `fresh` gives every task its own session — the way
    out if continuity ever proves worse than the rediscovery it replaces."""
    value = _object(value, path)
    _unknown(value, VOICE_SESSION_KEYS, path)
    if "mode" in value:
        _enum(value["mode"], VOICE_SESSION_MODES, f"{path}.mode")


def _voice_agent(value, path, *, defaults=False, project_root=None, service_dir=None):
    value = _object(value, path)
    _unknown(value, VOICE_AGENT_KEYS | (VOICE_AGENT_DEFAULTS_KEYS if defaults
                                        else VOICE_AGENT_PARTICIPANT_KEYS), path)
    if "tools" in value:
        _voice_tools(value["tools"], f"{path}.tools")
    if "session" in value:
        _voice_session(value["session"], f"{path}.session")
    if "mode" in value:
        _enum(value["mode"], VOICE_MODES, f"{path}.mode")
    if "provider" in value:
        _enum(value["provider"], VOICE_PROVIDERS, f"{path}.provider", nullable=True)
    if "profile" in value:
        _profile(value["profile"], f"{path}.profile")
    for key in ("model", "voice", "greeting", "timezone", "recording_caption"):
        if key in value:
            _string(value[key], f"{path}.{key}", nullable=True)
    if "history" in value:
        minimum, maximum, integer = VOICE_HISTORY
        _number(value["history"], f"{path}.history", minimum, maximum, integer=integer,
                nullable=defaults)
    if "progress_interval" in value:
        minimum, maximum, integer = VOICE_PROGRESS_INTERVAL
        _number(value["progress_interval"], f"{path}.progress_interval", minimum, maximum,
                integer=integer, nullable=True)
    if "prompt_file" in value:
        _safe_service_path(value["prompt_file"], f"{path}.prompt_file",
                           project_root, service_dir)


def _overlay(value, path, project_root, service_dir):
    if "context" in value:
        _string(value["context"], f"{path}.context")
    if "context_file" in value:
        _safe_service_path(value["context_file"], f"{path}.context_file",
                           project_root, service_dir)
    if "context_mode" in value:
        _enum(value["context_mode"], CONTEXT_MODES, f"{path}.context_mode")


def _participant(value, path, project_root, service_dir, *, member=False):
    value = _object(value, path)
    _unknown(value, PARTICIPANT_KEYS | (MEMBER_KEYS if member else USER_KEYS), path)
    for key in ("name", "username", "role"):
        if key in value:
            _string(value[key], f"{path}.{key}", nonempty=True)
    if "worker_timeout" in value:
        minimum, maximum, integer = CHANNEL_WORKER_TIMEOUT
        _number(value["worker_timeout"], f"{path}.worker_timeout", minimum, maximum,
                integer=integer)
    if "profile" in value:
        _profile(value["profile"], f"{path}.profile")
    if "may_address" in value:
        _boolean(value["may_address"], f"{path}.may_address")
    if "kind" in value:
        _enum(value["kind"], MEMBER_KINDS, f"{path}.kind")
    if "address_aliases" in value:
        _string_list(value["address_aliases"], f"{path}.address_aliases")
    if "control" in value:
        _control_rule(value["control"], f"{path}.control")
    if "delegation" in value:
        _delegation(value["delegation"], f"{path}.delegation")
    if "authority" in value:
        _authority_policy(value["authority"], f"{path}.authority")
    for key in ("allowed_capabilities", "capabilities"):
        if key in value:
            _capabilities(value[key], f"{path}.{key}")
    if "call_recording" in value:
        _call_recording(value["call_recording"], f"{path}.call_recording", group=False)
    if "voice_agent" in value:
        _voice_agent(value["voice_agent"], f"{path}.voice_agent")
    if "project" in value:
        _route_project(value["project"], f"{path}.project", project_root)
    _overlay(value, path, project_root, service_dir)


def _group(value, path, project_root, service_dir):
    if value is True:
        return
    value = _object(value, path)
    _unknown(value, GROUP_KEYS, path)
    for key in ("name", "role", "member_role"):
        if key in value:
            _string(value[key], f"{path}.{key}", nonempty=True)
    for key in ("aliases", "address_aliases", "mentions"):
        if key in value:
            _string_list(value[key], f"{path}.{key}")
    if "require_reference" in value:
        _boolean(value["require_reference"], f"{path}.require_reference")
    if "worker_timeout" in value:
        minimum, maximum, integer = CHANNEL_WORKER_TIMEOUT
        _number(value["worker_timeout"], f"{path}.worker_timeout", minimum, maximum,
                integer=integer)
    if "profile" in value:
        _profile(value["profile"], f"{path}.profile")
    if "members" in value:
        members = _object(value["members"], f"{path}.members")
        for user_id, member in members.items():
            try:
                if int(user_id) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                _fail(f"{path}.members.{user_id}", "member key must be a positive Telegram user ID")
            _participant(member, f"{path}.members.{user_id}", project_root, service_dir,
                         member=True)
    if "agent_dialogue" in value:
        policy = _object(value["agent_dialogue"], f"{path}.agent_dialogue")
        _unknown(policy, AGENT_DIALOGUE_KEYS, f"{path}.agent_dialogue")
        if "max_turns" in policy:
            minimum, maximum, integer = AGENT_DIALOGUE_MAX_TURNS
            _number(policy["max_turns"], f"{path}.agent_dialogue.max_turns", minimum,
                    maximum, integer=integer)
        if "reset_on_human_message" in policy:
            _boolean(policy["reset_on_human_message"],
                     f"{path}.agent_dialogue.reset_on_human_message")
    if "delegation" in value:
        _delegation(value["delegation"], f"{path}.delegation")
    if "voice_transcription" in value:
        policy = _object(value["voice_transcription"], f"{path}.voice_transcription")
        _unknown(policy, VOICE_TRANSCRIPTION_KEYS, f"{path}.voice_transcription")
        if "mode" in policy:
            _enum(policy["mode"], TRANSCRIPTION_MODES, f"{path}.voice_transcription.mode")
    if "call_recording" in value:
        _call_recording(value["call_recording"], f"{path}.call_recording", group=True)
    if "control" in value:
        _control_rule(value["control"], f"{path}.control")
    if "authority" in value:
        _authority_policy(value["authority"], f"{path}.authority")
    for key in ("allowed_capabilities", "capabilities"):
        if key in value:
            _capabilities(value[key], f"{path}.{key}")
    if "project" in value:
        _route_project(value["project"], f"{path}.project", project_root)
    if "topics" in value:
        _topics(value["topics"], f"{path}.topics", project_root, service_dir)
    if "triggers" in value:
        _triggers(value["triggers"], f"{path}.triggers")
    if "gate" in value:
        _gate(value["gate"], f"{path}.gate")
    _overlay(value, path, project_root, service_dir)


def _triggers(value, path):
    """Which mechanical triggers start a turn in this group. Absent, all three
    do; an empty list switches every one off, which leaves the group to the
    gate, a command and `require_reference: false`."""
    if not isinstance(value, list):
        _fail(path, "must be a JSON array")
    for index, item in enumerate(value):
        _enum(item, GROUP_TRIGGERS, f"{path}[{index}]")
    if len(set(value)) != len(value):
        _fail(path, "must not name a trigger twice")


def _per_harness(value, path):
    """A gate `model` or `effort`: one value for whichever harness the channel
    runs, or one per harness. Neither chooses the harness."""
    if isinstance(value, dict):
        _unknown(value, GATE_HARNESSES, path)
        for harness, item in value.items():
            _string(item, f"{path}.{harness}", nonempty=True)
        return
    _string(value, path, nonempty=True)


def _gate(value, path):
    """The reply gate of one group: off unless `enabled`, `shadow` unless a
    mode is named."""
    value = _object(value, path)
    _unknown(value, GATE_KEYS, path)
    if "enabled" in value:
        _boolean(value["enabled"], f"{path}.enabled")
    if "mode" in value:
        _enum(value["mode"], GATE_MODES, f"{path}.mode")
    if "brief" in value:
        _string(value["brief"], f"{path}.brief")
        if len(value["brief"]) > GATE_BRIEF_MAX:
            _fail(f"{path}.brief", f"must be at most {GATE_BRIEF_MAX} characters")
    for key, (minimum, maximum, integer) in GATE_NUMBERS.items():
        if key in value:
            _number(value[key], f"{path}.{key}", minimum, maximum, integer=integer)
    for key in ("model", "effort"):
        if key in value:
            _per_harness(value[key], f"{path}.{key}")


def _delegation(value, path):
    policy = _object(value, path)
    _unknown(policy, DELEGATION_KEYS, path)
    if "mode" in policy:
        _enum(policy["mode"], DELEGATION_MODES, f"{path}.mode")


def _defaults(value, path, project_root, service_dir):
    value = _object(value, path)
    _unknown(value, DEFAULTS_KEYS, path)
    if "link_preview" in value:
        _boolean(value["link_preview"], f"{path}.link_preview")
    if "progress_from_stream" in value:
        _boolean(value["progress_from_stream"], f"{path}.progress_from_stream")
    if "assistant_name" in value:
        _string(value["assistant_name"], f"{path}.assistant_name", nonempty=True)
    if "delegation" in value:
        _delegation(value["delegation"], f"{path}.delegation")
    for key, (minimum, maximum, integer) in DEFAULTS_NUMBERS.items():
        if key in value:
            _number(value[key], f"{path}.{key}", minimum, maximum, integer=integer)
    if "group_aliases" in value:
        _string_list(value["group_aliases"], f"{path}.group_aliases")
    if "profile" in value:
        _profile(value["profile"], f"{path}.profile")
    if "media_log_level" in value:
        _enum(value["media_log_level"], MEDIA_LOG_LEVELS,
              f"{path}.media_log_level")
    if "job_recovery" in value:
        _enum(value["job_recovery"], JOB_RECOVERY, f"{path}.job_recovery")
    if "voice_agent" in value:
        _voice_agent(value["voice_agent"], f"{path}.voice_agent", defaults=True,
                     project_root=project_root, service_dir=service_dir)


# --- The worker keys a profile replaced ---------------------------------------
#
# A room names the profile its turns run on. The keys that named a worker and
# its knobs before that - `worker` and `workers` in the defaults and in a voice
# entry, a channel register row's `/set worker` and `/set model` fields, and the
# environment's default worker - are refused wherever they are found, naming
# the command that moves them, so nothing silently changes what a room runs.

MIGRATION_COMMAND = "telegram service migrate-profiles"
OLD_ENVIRONMENT = ("TELEGRAM_SERVICE_WORKER", "TG_WORKER")
OLD_KEYS = ("worker", "workers")


class OldWorkerSettings(ValueError):
    """Settings, a register or an environment still carrying the keys a
    profile replaced. The message names every place and the command that moves
    them."""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def old_worker_keys(settings):
    """Every place `settings` still carries a worker key, as a settings path."""
    settings = _mapping(settings)
    found = []

    def keys(owner, path):
        found.extend(f"{path}.{key}" for key in OLD_KEYS if key in _mapping(owner))

    defaults = _mapping(settings.get("defaults"))
    keys(defaults, "settings.defaults")
    keys(defaults.get("voice_agent"), "settings.defaults.voice_agent")
    for user_id, policy in _mapping(settings.get("allowed_users")).items():
        keys(_mapping(policy).get("voice_agent"),
             f"settings.allowed_users.{user_id}.voice_agent")
    for chat_id, policy in _mapping(settings.get("allowed_groups")).items():
        for user_id, member in _mapping(_mapping(policy).get("members")).items():
            keys(_mapping(member).get("voice_agent"),
                 f"settings.allowed_groups.{chat_id}.members.{user_id}.voice_agent")
    return found


def old_register_fields(register):
    """Every channel register row still carrying the worker fields `/set worker`,
    `/set model` and the per-worker `/set` wrote, as `<channel key>.<field>`."""
    found = []
    for key, row in _mapping(register).items():
        row = _mapping(row)
        settings = _mapping(row.get("settings"))
        found.extend(f"{key}.settings.{field}" for field in ("worker", "model")
                     if field in settings)
        if "workers" in row:
            found.append(f"{key}.workers")
    return found


def old_environment(environ):
    """The environment names of the default worker that are set."""
    return [name for name in OLD_ENVIRONMENT if str(environ.get(name) or "").strip()]


def old_names_sentence(places):
    """The refusal for the places that still carry a worker key."""
    return (f"{', '.join(places)}: a worker is no longer named in settings, a channel's "
            "register or the environment; a room names the profile its turns run on. "
            f"Run `{MIGRATION_COMMAND}` to see the moves, then with --apply to make them")


def refuse_old_worker_settings(settings):
    places = old_worker_keys(settings)
    if places:
        raise OldWorkerSettings(old_names_sentence(places))


def validate_settings(settings, project_root, service_dir):
    """Validate the complete public settings surface; return the same object."""
    settings = _object(settings, "settings")
    refuse_old_worker_settings(settings)
    _unknown(settings, TOP_LEVEL, "settings")
    if "connection" in settings:
        _string(settings["connection"], "settings.connection", nullable=True, nonempty=True)
    if "environment" in settings:
        _string(settings["environment"], "settings.environment", nonempty=True)
        if not ENVIRONMENT_RE.fullmatch(settings["environment"]):
            _fail("settings.environment",
                  f"must match {ENVIRONMENT_RE.pattern}")
    if "assistant_name" in settings:
        _string(settings["assistant_name"], "settings.assistant_name", nonempty=True)
    if "direct_messages" in settings:
        direct = _object(settings["direct_messages"], "settings.direct_messages")
        _unknown(direct, DIRECT_MESSAGE_KEYS, "settings.direct_messages")
        if "mode" in direct:
            _enum(direct["mode"], DIRECT_MODES, "settings.direct_messages.mode")
        if "default_role" in direct:
            _string(direct["default_role"], "settings.direct_messages.default_role",
                    nonempty=True)
    if "allowed_users" in settings:
        users = _object(settings["allowed_users"], "settings.allowed_users")
        for user_id, policy in users.items():
            try:
                if int(user_id) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                _fail(f"settings.allowed_users.{user_id}",
                      "key must be a positive Telegram user ID")
            _participant(policy, f"settings.allowed_users.{user_id}",
                         project_root, service_dir)
    if "allowed_groups" in settings:
        groups = _object(settings["allowed_groups"], "settings.allowed_groups")
        for chat_id, policy in groups.items():
            try:
                if int(chat_id) >= 0:
                    raise ValueError
            except (TypeError, ValueError):
                _fail(f"settings.allowed_groups.{chat_id}",
                      "key must be a negative Telegram chat ID")
            _group(policy, f"settings.allowed_groups.{chat_id}",
                   project_root, service_dir)
    if "control" in settings:
        _control(settings["control"], "settings.control")
    if "authority" in settings:
        _authority(settings["authority"], "settings.authority")
    if "defaults" in settings:
        _defaults(settings["defaults"], "settings.defaults", project_root, service_dir)
    return settings


# --- The settings as JSON Schema ----------------------------------------------
#
# What `telegram service schema` prints: the surface `validate_settings` holds a
# document to, as JSON Schema (draft 2020-12), built from the same key sets,
# choices and bounds, so a document the schema refuses is one a reload refuses.
# A few constraints are the validator's alone - a prompt file must resolve inside
# the service directory, a route inside the home - and are named under
# `x-constraint` rather than encoded.
#
# The `x-` keywords carry what a control panel needs and a validator ignores:
#   x-apply       `reload` or `restart`: how a running service takes the key
#   x-keyed-by    what the keys of a map are: a Telegram user, chat or topic id, a role...
#   x-entry-label the field that names an entry of the map
#   x-inherits    where an absent value comes from, nearest first; `<...>` is not a
#                 settings path but the environment or the built-in that applies
#   x-layers      how a value combines with the levels around it, where it merges
#                 or replaces rather than only filling in
#   x-document    a prompt file the context verbs read and write, and its key rule
#   x-documents   the service's own prompt documents, by key

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
USER_ID_PATTERN = r"^[1-9][0-9]*$"
GROUP_ID_PATTERN = r"^-[1-9][0-9]*$"
TOPIC_ID_PATTERN = r"^[1-9][0-9]*$"
DOCUMENT_KEY_RULE = ("the file's name without `.md`, lower-cased with `_` as `-`, "
                     "prefixed `context.` when the file sits in `context/`; "
                     "`telegram context show|edit|put <key>` reads and writes it")
# The service's own prompt documents, read by key on every turn of their kind.
SERVICE_DOCUMENTS = {
    "context": "the whole prompt a dialogue turn runs on",
    "delegation": "added after `context` wherever the job register is offered to the turn",
    "job-worker": "the whole prompt a job worker runs on",
    "voice-agent": "what the speaking model of a call is told "
                   f"(the `{VOICE_PROVIDER_INCUMBENT}` provider's, where it writes no own)",
}


def _js(kind, *, nullable=False, **extra):
    node = {"type": [kind, "null"] if nullable else kind}
    node.update(extra)
    return node


def _js_text(*, nonempty=False, nullable=False, **extra):
    if nonempty:
        extra = {"minLength": 1, "pattern": r"\S", **extra}
    return _js("string", nullable=nullable, **extra)


def _js_number(bounds, *, nullable=False, **extra):
    minimum, maximum, integer = bounds
    return _js("integer" if integer else "number", nullable=nullable,
               minimum=minimum, maximum=maximum, **extra)


def _js_enum(choices, *, nullable=False, **extra):
    values = sorted(choices) + ([None] if nullable else [])
    return {"enum": values, **extra}


def _js_texts(**extra):
    return {"type": "array", "items": _js_text(nonempty=True), **extra}


def _js_object(keys, properties, **extra):
    missing = set(keys) - properties.keys()
    assert not missing, f"schema has no property for {sorted(missing)}"
    return {"type": "object", "additionalProperties": False,
            "properties": {key: properties[key] for key in sorted(keys)}, **extra}


def _js_map(value, keyed_by, *, pattern=None, label=None, **extra):
    node = {"type": "object", "additionalProperties": value, "x-keyed-by": keyed_by}
    node["propertyNames"] = {"pattern": pattern} if pattern else {"minLength": 1}
    if label:
        node["x-entry-label"] = label
    node.update(extra)
    return node


def _ref(name):
    return {"$ref": f"#/$defs/{name}"}


def _ecma_pattern(compiled):
    """A Python pattern as JSON Schema writes it: anchored, case spelled out."""
    pattern = compiled.pattern
    if compiled.flags & re.IGNORECASE:
        pattern = pattern.replace("a-z", "A-Za-z")
    return f"^{pattern}$"


def json_schema():
    """The settings `validate_settings` accepts, as JSON Schema with the hints
    a control panel renders it from."""
    service_path = _js_text(
        nonempty=True, nullable=True,
        **{"x-constraint": "a path relative to the service directory, or absolute, "
                           "that resolves inside it",
           "x-document": {"verb": "context", "key": DOCUMENT_KEY_RULE}})
    route = _js_text(
        nonempty=True, pattern=r"^(/|~)",
        description="the project a request's work runs in",
        **{"x-constraint": "an absolute path or one starting with `~`, inside the "
                           "home directory; whether it exists is checked when a "
                           "request names it"})
    profile = _js_text(nonempty=True, description="a telegram harness profile name; "
                       "`telegram profiles list` shows them")
    default_profile = ["<TELEGRAM_SERVICE_PROFILE>", "defaults.profile", "<stub>"]
    overlay = {
        "context": _js_text(description="prose this level adds to the prompt"),
        "context_file": {**service_path,
                         "description": "a prompt file this level adds to the prompt"},
        "context_mode": _js_enum(
            CONTEXT_MODES, description="`exclusive` drops every layer above this one, "
            "the service context with it"),
    }
    def rights(merged_over):
        merges = f"merged over {merged_over}"
        replaces = (f"replaces the capabilities of {merged_over}; `allowed_capabilities` "
                    "is read where both are written")
        return {
            "control": {**_ref("controlRule"), "x-layers": merges},
            "authority": {**_ref("authorityPolicy"), "x-layers": merges},
            "allowed_capabilities": {**_ref("capabilities"), "x-layers": replaces},
            "capabilities": {**_ref("capabilities"), "x-layers": replaces},
        }
    defs = {
        "capabilityRule": {"anyOf": [
            {"type": "boolean"}, {"const": "*"}, _js_texts(),
            _js_object(CAPABILITY_RULE_KEYS, {
                "allow": _js("boolean"), "deny": _js("boolean"),
                "enabled": _js("boolean"), "scope": _js_text(nonempty=True),
                "verbs": _js_texts(),
                "connections": {"anyOf": [
                    _js_texts(),
                    _js_map(_js_object(CONNECTION_GRANT_KEYS,
                                       {"allow_write": _js("boolean")}),
                            "connection"),
                ]},
            })]},
        "capabilities": {"anyOf": [
            {"const": True}, {"const": "*"}, _js_texts(),
            _js_map(_ref("capabilityRule"), "capability"),
        ]},
        "authorityPolicy": _js_object(AUTHORITY_POLICY_KEYS, {
            "allowed_capabilities": _ref("capabilities"),
            "capabilities": _ref("capabilities")}),
        "commandRule": {"anyOf": [
            {"type": "boolean"}, {"const": "*"},
            _js_object(COMMAND_RULE_KEYS, {k: _js("boolean") for k in COMMAND_RULE_KEYS}),
        ]},
        "controlRule": _js_object(CONTROL_RULE_KEYS, {"commands": {"anyOf": [
            {"const": True}, {"const": "*"},
            {"type": "array", "items": _js_enum(CONTROL_COMMANDS)},
            {"type": "object", "additionalProperties": _ref("commandRule"),
             "propertyNames": _js_enum(CONTROL_COMMANDS), "x-keyed-by": "command"},
        ]}}),
        "delegation": _js_object(DELEGATION_KEYS, {"mode": _js_enum(DELEGATION_MODES)}),
        "voiceTools": _js_object(VOICE_TOOLS, {k: _js("boolean") for k in VOICE_TOOLS},
                                 **{"x-layers": "each tool named here turns that one on "
                                    "or off over `defaults.voice_agent.tools`; every tool "
                                    "is off where nothing names it"}),
    }
    voice_common = {
        "model": _js_text(nullable=True), "voice": _js_text(nullable=True),
        "greeting": _js_text(nullable=True),
        "tools": _ref("voiceTools"),
        "provider": _js_enum(VOICE_PROVIDERS, nullable=True),
    }
    defs["voiceAgent"] = _js_object(VOICE_AGENT_KEYS | VOICE_AGENT_PARTICIPANT_KEYS, {
        **{k: {**v, "x-inherits": [f"defaults.voice_agent.{k}", "<provider default>"]}
           for k, v in voice_common.items()},
        "history": _js_number(VOICE_HISTORY, **{"x-inherits": [
            "defaults.voice_agent.history", "<built-in>"]}),
        "mode": _js_enum(VOICE_MODES, description="whether the assistant answers this "
                         "caller's calls by talking"),
    })
    defs["voiceAgentDefaults"] = _js_object(VOICE_AGENT_KEYS | VOICE_AGENT_DEFAULTS_KEYS, {
        **voice_common,
        "history": _js_number(VOICE_HISTORY, nullable=True),
        "profile": {**profile, "x-inherits": default_profile[:]},
        "timezone": _js_text(nullable=True), "recording_caption": _js_text(nullable=True),
        "progress_interval": _js_number(VOICE_PROGRESS_INTERVAL, nullable=True),
        "prompt_file": service_path,
        "session": _js_object(VOICE_SESSION_KEYS,
                              {"mode": _js_enum(VOICE_SESSION_MODES)}),
    })

    def participant(member):
        props = {
            "name": _js_text(nonempty=True, description="what the entry is called"),
            "username": _js_text(nonempty=True),
            "role": _js_text(nonempty=True, **(
                {"x-inherits": ["allowed_groups.<chat_id>.member_role",
                                "allowed_groups.<chat_id>.role", "<group_member>"],
                 "description": "the person's `allowed_users` role, where it names one, "
                                "wins over this"} if member else
                {"x-inherits": ["direct_messages.default_role", "<direct_user>"],
                 "description": "the role this person carries in every chat"})),
            "may_address": _js("boolean", **{"x-inherits": (
                ["allowed_users.<user_id>.may_address", "<true>"] if member
                else ["<true>"])}),
            "delegation": (_ref("delegation") if member else
                           {**_ref("delegation"),
                            "x-inherits": ["defaults.delegation", "<off>"]}),
            **rights("the group's, which is merged over this person's `allowed_users` "
                     "entry and the role's" if member else
                     "the role's; in a group the group's and the member entry's are "
                     "merged over this"),
            **overlay,
            "call_recording": _js_object(CALL_RECORDING_USER_KEYS, {
                "mode": _js_enum(CALL_USER_MODES)}),
            "voice_agent": _ref("voiceAgent"),
            "project": {**route, "x-inherits": ["<the service's own project>"]},
        }
        if member:
            props["kind"] = _js_enum(MEMBER_KINDS)
            props["address_aliases"] = _js_texts()
        else:
            props["worker_timeout"] = _js_number(CHANNEL_WORKER_TIMEOUT, **{
                "x-inherits": ["defaults.worker_timeout", "<built-in>"]})
            props["profile"] = {**profile, "x-inherits": default_profile[:]}
        return _js_object(PARTICIPANT_KEYS | (MEMBER_KEYS if member else USER_KEYS), props)

    defs["user"] = participant(False)
    defs["member"] = participant(True)
    defs["topic"] = _js_object(TOPIC_KEYS, {
        "project": {**route, "x-inherits": ["allowed_groups.<chat_id>.project",
                                            "<the service's own project>"]},
        "profile": {**profile, "x-inherits": ["allowed_groups.<chat_id>.profile",
                                              *default_profile]},
        **overlay,
    }, description="A forum topic's own lane. It carries no rights: it inherits its "
       "group's and cannot narrow or widen them, and its prose follows the group's.",
       **{"x-rights-from": "allowed_groups.<chat_id>"})
    defs["gate"] = _js_object(GATE_KEYS, {
        "enabled": _js("boolean"), "mode": _js_enum(GATE_MODES),
        "brief": _js_text(maxLength=GATE_BRIEF_MAX),
        **{k: _js_number(b) for k, b in GATE_NUMBERS.items()},
        **{k: {"anyOf": [_js_text(nonempty=True),
                         _js_object(GATE_HARNESSES,
                                    {h: _js_text(nonempty=True) for h in GATE_HARNESSES})]}
           for k in ("model", "effort")},
    })
    defs["group"] = _js_object(GROUP_KEYS, {
        "name": _js_text(nonempty=True, description="what the group is called"),
        "role": _js_text(nonempty=True),
        "member_role": _js_text(nonempty=True, **{"x-inherits": [
            "allowed_groups.<chat_id>.role", "<group_member>"]}),
        **{k: _js_texts(**{"x-inherits": ["defaults.group_aliases", "<assistant_name>"]})
           for k in ("aliases", "address_aliases", "mentions")},
        "require_reference": _js("boolean"),
        "members": _js_map(_ref("member"), "telegram_user_id", pattern=USER_ID_PATTERN,
                           label="name"),
        "agent_dialogue": _js_object(AGENT_DIALOGUE_KEYS, {
            "max_turns": _js_number(AGENT_DIALOGUE_MAX_TURNS),
            "reset_on_human_message": _js("boolean")}),
        "worker_timeout": _js_number(CHANNEL_WORKER_TIMEOUT, **{
            "x-inherits": ["defaults.worker_timeout", "<built-in>"]}),
        "profile": {**profile, "x-inherits": default_profile[:]},
        "voice_transcription": _js_object(VOICE_TRANSCRIPTION_KEYS, {
            "mode": _js_enum(TRANSCRIPTION_MODES)}),
        "call_recording": _js_object(CALL_RECORDING_GROUP_KEYS, {
            "mode": _js_enum(CALL_GROUP_MODES), "send_to_chat": _js("boolean")}),
        "delegation": {**_ref("delegation"), "x-inherits": [
            "allowed_users.<sender_id>.delegation", "defaults.delegation", "<off>"]},
        **rights("the sender's `allowed_users` entry and the role's; a member's own "
                 "entry is merged over this"),
        **overlay,
        "project": {**route, "x-inherits": ["<the service's own project>"]},
        "topics": _js_map(_ref("topic"), "forum_topic_id", pattern=TOPIC_ID_PATTERN),
        "triggers": {"type": "array", "items": _js_enum(GROUP_TRIGGERS),
                     "uniqueItems": True,
                     "description": "absent, every trigger starts a turn"},
        "gate": _ref("gate"),
    })
    defaults_numbers = {k: _js_number(b) for k, b in DEFAULTS_NUMBERS.items()}
    defaults = _js_object(DEFAULTS_KEYS, {
        **defaults_numbers,
        "assistant_name": _js_text(nonempty=True),
        "progress_from_stream": _js("boolean"),
        "link_preview": _js("boolean"),
        "job_recovery": _js_enum(JOB_RECOVERY),
        "group_aliases": _js_texts(**{"x-inherits": ["<assistant_name>"]}),
        "profile": {**profile, "description": "the profile a channel naming none runs "
                    "on; the environment's TELEGRAM_SERVICE_PROFILE wins over it",
                    "x-inherits": ["<stub>"]},
        "voice_agent": _ref("voiceAgentDefaults"),
        "delegation": _ref("delegation"),
        "media_log_level": _js_enum(MEDIA_LOG_LEVELS),
    }, description="What every channel takes where its own entry names nothing.")
    roles = _js_map(_ref("authorityPolicy"), "role")
    properties = {
        "connection": _js_text(nonempty=True, nullable=True,
                               description="the connection the service runs on"),
        "environment": _js_text(nonempty=True, pattern=_ecma_pattern(ENVIRONMENT_RE),
                                description="which half of the machine this service is; "
                                "jobs are filtered by it"),
        "assistant_name": _js_text(nonempty=True, **{"x-inherits": [
            "defaults.assistant_name", "<Assistant>"]}),
        "direct_messages": _js_object(DIRECT_MESSAGE_KEYS, {
            "mode": _js_enum(DIRECT_MODES), "default_role": _js_text(nonempty=True)}),
        "allowed_users": _js_map(_ref("user"), "telegram_user_id",
                                 pattern=USER_ID_PATTERN, label="name",
                                 description="direct chats, one entry per person"),
        "allowed_groups": _js_map({"anyOf": [{"const": True}, _ref("group")]},
                                  "telegram_chat_id", pattern=GROUP_ID_PATTERN,
                                  label="name", description="groups, each with its "
                                  "members and forum topics"),
        "control": _js_object(CONTROL_KEYS, {"roles": _js_map(_ref("controlRule"), "role")},
                              **{"x-layers": "each role's rule is merged over the "
                                 "built-in rule of that role"}),
        "authority": _js_object(AUTHORITY_KEYS, {
            "default": _ref("authorityPolicy"), "roles": roles},
            **{"x-layers": "`default`, then the sender's role, then the person's and "
               "the group's own entries"}),
        "defaults": defaults,
    }
    for key, node in properties.items():
        node["x-apply"] = "restart" if key in RESTART_KEYS else "reload"
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "title": "Telegram assistant service settings",
        "type": "object",
        "additionalProperties": False,
        "properties": {key: properties[key] for key in sorted(TOP_LEVEL)},
        "$defs": defs,
        "x-documents": [{"key": key, "read": read} for key, read in
                        SERVICE_DOCUMENTS.items()]
                       + [{"key": f"{provider}-{kind}", "read": f"the `{provider}` "
                           f"provider's `{kind}` prompt"}
                          for provider, kinds in sorted(VOICE_PROVIDER_DOCUMENTS.items())
                          for kind in kinds + VOICE_PROVIDER_OPTIONAL_DOCUMENTS],
    }


def document_key(relative):
    """The document key a prompt file answers to, from its path relative to the
    service directory: `context.md` is `context`, `context/<x>.md` is
    `context.<x>`, anything else its own stem."""
    rel = Path(relative)
    stem = rel.stem.replace("_", "-").lower()
    if rel.parent.name == "context":
        return f"context.{stem}"
    return stem


def named_documents(settings):
    """Every prompt file `settings` name, as (settings path, value)."""
    settings = _mapping(settings)
    found = []

    def take(owner, path, key="context_file"):
        value = _mapping(owner).get(key)
        if isinstance(value, str) and value.strip():
            found.append((f"{path}.{key}", value))

    for user_id, policy in _mapping(settings.get("allowed_users")).items():
        take(policy, f"settings.allowed_users.{user_id}")
    for chat_id, policy in _mapping(settings.get("allowed_groups")).items():
        base = f"settings.allowed_groups.{chat_id}"
        take(policy, base)
        for user_id, member in _mapping(_mapping(policy).get("members")).items():
            take(member, f"{base}.members.{user_id}")
        for topic_id, topic in _mapping(_mapping(policy).get("topics")).items():
            take(topic, f"{base}.topics.{topic_id}")
    take(_mapping(settings.get("defaults")).get("voice_agent"),
         "settings.defaults.voice_agent", "prompt_file")
    return found
