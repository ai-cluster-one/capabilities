"""Strict, dependency-free validation for Telegram assistant settings."""

from pathlib import Path


TOP_LEVEL = {
    "connection", "assistant_name", "direct_messages", "allowed_users",
    "allowed_groups", "control", "authority", "defaults",
}
WORKERS = {"claude", "codex", "stub"}
# What the media stack may be read at. A call under investigation is read at
# debug; anything a normal call needs is at info.
MEDIA_LOG_LEVELS = {"debug", "info", "warning", "error", "critical"}
DIRECT_MODES = {"allowed_users", "anyone", "all", "open", "public"}
VOICE_MODES = {"disabled", "enabled", "auto", "on"}
VOICE_TOOLS = {"agent_task", "send_to_chat", "run_capability",
               "read_project_file", "reload_service"}
VOICE_SESSION_MODES = {"carry", "fresh"}
TRANSCRIPTION_MODES = {"disabled", "auto"}
CALL_GROUP_MODES = {
    "disabled", "off", "none", "auto", "automatic", "on_request",
    "on-request", "request", "command", "manual",
}
CALL_USER_MODES = {"disabled", "off", "enabled", "auto", "on"}
CONTROL_COMMANDS = {"status", "set", "reload", "stop", "help", "*"}
CONTEXT_MODES = {"extend", "exclusive"}


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


def _route_project(value, path):
    """A worker route target, judged by its shape alone.

    Existence is deliberately not asserted here. A settings failure exits the
    daemon, so a neighbouring project that was renamed or unmounted would stop
    the service for every other channel. The dispatch check owns that failure
    and spends it on the one request that names the route.
    """
    _string(value, path, nonempty=True)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        _fail(path, "must be an absolute path, or start with ~")
    home = Path.home()
    if home not in candidate.parents:
        _fail(path, f"must be a path inside {home}")


def _topics(value, path, project_root, service_dir):
    """Per-topic overrides inside one group policy.

    A topic carries where its work runs and what prose precedes it. It carries
    no authority tier: a topic inherits its group's rights and cannot narrow or
    widen them.
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
        _unknown(entry, {"project", "context", "context_file", "context_mode"},
                 entry_path)
        if "project" in entry:
            _route_project(entry["project"], f"{entry_path}.project")
        _overlay(entry, entry_path, project_root, service_dir)


def _worker_rule(value, path, worker):
    value = _object(value, path)
    allowed = {"model"}
    if worker == "claude":
        allowed.add("effort")
    elif worker == "codex":
        allowed.update({"reasoning_effort", "service_tier"})
    _unknown(value, allowed, path)
    if "model" in value:
        _string(value["model"], f"{path}.model", nullable=True)
    if "effort" in value:
        _enum(value["effort"], {"default", "low", "medium", "high", "xhigh", "max"},
              f"{path}.effort", nullable=True)
    if "reasoning_effort" in value:
        _enum(value["reasoning_effort"],
              {"default", "low", "medium", "high", "xhigh"},
              f"{path}.reasoning_effort", nullable=True)
    if "service_tier" in value:
        _enum(value["service_tier"], {"default", "fast", "priority"},
              f"{path}.service_tier", nullable=True)


def _workers(value, path):
    value = _object(value, path)
    _unknown(value, WORKERS, path)
    for worker, rule in value.items():
        _worker_rule(rule, f"{path}.{worker}", worker)


def _capability_rule(value, path):
    if isinstance(value, bool) or value == "*":
        return
    if isinstance(value, list):
        _string_list(value, path)
        return
    value = _object(value, path)
    allowed = {"allow", "deny", "enabled", "scope", "verbs"}
    _unknown(value, allowed, path)
    for key in ("allow", "deny", "enabled"):
        if key in value:
            _boolean(value[key], f"{path}.{key}")
    if "scope" in value:
        _string(value["scope"], f"{path}.scope", nonempty=True)
    if "verbs" in value:
        _string_list(value["verbs"], f"{path}.verbs")


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
    allowed = {"allowed_capabilities", "capabilities"}
    _unknown(value, allowed, path)
    for key in allowed & value.keys():
        _capabilities(value[key], f"{path}.{key}")


def _authority(value, path):
    value = _object(value, path)
    _unknown(value, {"default", "roles"}, path)
    if "default" in value:
        _authority_policy(value["default"], f"{path}.default")
    if "roles" in value:
        roles = _object(value["roles"], f"{path}.roles")
        for role, policy in roles.items():
            _string(role, f"{path}.roles key", nonempty=True)
            _authority_policy(policy, f"{path}.roles.{role}")


def _control_rule(value, path):
    value = _object(value, path)
    _unknown(value, {"commands"}, path)
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
        _unknown(rule, {"allow", "deny", "enabled"}, f"{path}.commands.{command}")
        for key, item in rule.items():
            _boolean(item, f"{path}.commands.{command}.{key}")


def _control(value, path):
    value = _object(value, path)
    _unknown(value, {"roles"}, path)
    if "roles" in value:
        roles = _object(value["roles"], f"{path}.roles")
        for role, rule in roles.items():
            _string(role, f"{path}.roles key", nonempty=True)
            _control_rule(rule, f"{path}.roles.{role}")


def _call_recording(value, path, *, group):
    value = _object(value, path)
    allowed = {"mode", "send_to_chat"} if group else {"mode"}
    _unknown(value, allowed, path)
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
    _unknown(value, {"mode"}, path)
    if "mode" in value:
        _enum(value["mode"], VOICE_SESSION_MODES, f"{path}.mode")


def _voice_agent(value, path, *, defaults=False, project_root=None, service_dir=None):
    value = _object(value, path)
    allowed = {"worker", "workers", "model", "voice", "greeting", "history", "tools"}
    if defaults:
        allowed.update({"timezone", "progress_interval", "recording_caption",
                        "prompt_file", "session"})
    else:
        allowed.add("mode")
    _unknown(value, allowed, path)
    if "tools" in value:
        _voice_tools(value["tools"], f"{path}.tools")
    if "session" in value:
        _voice_session(value["session"], f"{path}.session")
    if "mode" in value:
        _enum(value["mode"], VOICE_MODES, f"{path}.mode")
    if "worker" in value:
        _enum(value["worker"], WORKERS, f"{path}.worker", nullable=True)
    if "workers" in value:
        _workers(value["workers"], f"{path}.workers")
    for key in ("model", "voice", "greeting", "timezone", "recording_caption"):
        if key in value:
            _string(value[key], f"{path}.{key}", nullable=True)
    if "history" in value:
        _number(value["history"], f"{path}.history", 0, 500, integer=True, nullable=defaults)
    if "progress_interval" in value:
        _number(value["progress_interval"], f"{path}.progress_interval", 0.1, 3600,
                nullable=True)
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
    allowed = {
        "name", "username", "role", "may_address", "control", "authority",
        "allowed_capabilities", "capabilities", "context", "context_file",
        "context_mode", "call_recording", "voice_agent", "project",
    }
    if member:
        allowed.update({"kind", "address_aliases"})
    _unknown(value, allowed, path)
    for key in ("name", "username", "role"):
        if key in value:
            _string(value[key], f"{path}.{key}", nonempty=True)
    if "may_address" in value:
        _boolean(value["may_address"], f"{path}.may_address")
    if "kind" in value:
        _enum(value["kind"], {"human", "agent"}, f"{path}.kind")
    if "address_aliases" in value:
        _string_list(value["address_aliases"], f"{path}.address_aliases")
    if "control" in value:
        _control_rule(value["control"], f"{path}.control")
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
        _route_project(value["project"], f"{path}.project")
    _overlay(value, path, project_root, service_dir)


def _group(value, path, project_root, service_dir):
    if value is True:
        return
    value = _object(value, path)
    allowed = {
        "name", "role", "member_role", "aliases", "address_aliases", "mentions",
        "require_reference", "members", "agent_dialogue", "worker_timeout",
        "voice_transcription", "call_recording", "control", "authority",
        "allowed_capabilities", "capabilities", "context", "context_file",
        "context_mode", "project", "topics",
    }
    _unknown(value, allowed, path)
    for key in ("name", "role", "member_role"):
        if key in value:
            _string(value[key], f"{path}.{key}", nonempty=True)
    for key in ("aliases", "address_aliases", "mentions"):
        if key in value:
            _string_list(value[key], f"{path}.{key}")
    if "require_reference" in value:
        _boolean(value["require_reference"], f"{path}.require_reference")
    if "worker_timeout" in value:
        _number(value["worker_timeout"], f"{path}.worker_timeout", 1, 3600)
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
        _unknown(policy, {"max_turns", "reset_on_human_message"}, f"{path}.agent_dialogue")
        if "max_turns" in policy:
            _number(policy["max_turns"], f"{path}.agent_dialogue.max_turns", 0, 100,
                    integer=True)
        if "reset_on_human_message" in policy:
            _boolean(policy["reset_on_human_message"],
                     f"{path}.agent_dialogue.reset_on_human_message")
    if "voice_transcription" in value:
        policy = _object(value["voice_transcription"], f"{path}.voice_transcription")
        _unknown(policy, {"mode"}, f"{path}.voice_transcription")
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
        _route_project(value["project"], f"{path}.project")
    if "topics" in value:
        _topics(value["topics"], f"{path}.topics", project_root, service_dir)
    _overlay(value, path, project_root, service_dir)


def _defaults(value, path, project_root, service_dir):
    value = _object(value, path)
    allowed = {
        "assistant_name", "tail_size", "sync_interval", "sync_stale_after",
        "debounce", "worker_timeout", "progress_after", "max_parallel_jobs",
        "max_attempts", "group_aliases", "worker", "workers", "voice_agent",
        "media_log_level",
    }
    _unknown(value, allowed, path)
    if "assistant_name" in value:
        _string(value["assistant_name"], f"{path}.assistant_name", nonempty=True)
    numeric = {
        "tail_size": (1, 500, True),
        "sync_interval": (0.01, 3600, False),
        "sync_stale_after": (0.01, 86400, False),
        "debounce": (0, 300, False),
        "worker_timeout": (0.01, 3600, False),
        "progress_after": (0, 3600, False),
        "max_parallel_jobs": (1, 32, True),
        "max_attempts": (1, 20, True),
    }
    for key, (minimum, maximum, integer) in numeric.items():
        if key in value:
            _number(value[key], f"{path}.{key}", minimum, maximum, integer=integer)
    if "group_aliases" in value:
        _string_list(value["group_aliases"], f"{path}.group_aliases")
    if "worker" in value:
        _enum(value["worker"], WORKERS, f"{path}.worker")
    if "media_log_level" in value:
        _enum(value["media_log_level"], MEDIA_LOG_LEVELS,
              f"{path}.media_log_level")
    if "workers" in value:
        _workers(value["workers"], f"{path}.workers")
    if "voice_agent" in value:
        _voice_agent(value["voice_agent"], f"{path}.voice_agent", defaults=True,
                     project_root=project_root, service_dir=service_dir)


def validate_settings(settings, project_root, service_dir):
    """Validate the complete public settings surface; return the same object."""
    settings = _object(settings, "settings")
    _unknown(settings, TOP_LEVEL, "settings")
    if "connection" in settings:
        _string(settings["connection"], "settings.connection", nullable=True, nonempty=True)
    if "assistant_name" in settings:
        _string(settings["assistant_name"], "settings.assistant_name", nonempty=True)
    if "direct_messages" in settings:
        direct = _object(settings["direct_messages"], "settings.direct_messages")
        _unknown(direct, {"mode", "default_role"}, "settings.direct_messages")
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
