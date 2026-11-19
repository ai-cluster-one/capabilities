"""Strict, dependency-free validation for the Slack assistant settings.

The surface is closed: a key it does not name is refused with the path that
carries it and the keys that would be read there, never ignored. It is loaded
by the CLI (`settings` through `_settings_check`, `service init`, `run`,
`doctor`, `reload`) and by the listener's own reload, so one walk decides what
a settings document means.

A person is named by their Slack user id (`U...`, or `W...` on an Enterprise
Grid org), a conversation by its id (`C...`, `G...`, `D...`), and one thread
of a conversation by `<conversation id>/<thread ts>`.
"""

import re

TOP_LEVEL = {
    "connection", "environment", "assistant_name", "direct_messages",
    "allowed_users", "allowed_channels", "authority", "defaults",
    # The pilot's template carried a `_help` object explaining itself; a
    # project that copied it keeps starting. It is read as nothing.
    "_help",
}
ENVIRONMENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.IGNORECASE)
USER_RE = re.compile(r"[UW][A-Z0-9]{2,30}")
CONVERSATION_RE = re.compile(r"[CDG][A-Z0-9]{2,30}")
TS_RE = re.compile(r"[0-9]{1,12}\.[0-9]{1,6}")
PROFILE_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")
DIRECT_MODES = {"allowlist", "anyone", "off"}
# What wakes a turn in a listed conversation: a message that mentions the
# account (or calls it by an alias, or replies in a thread it takes part in),
# or every message from a sender who may address it.
WAKE_MODES = {"mention", "message"}
# Who in a listed conversation may start a turn: anyone in it, only the senders
# the settings list under allowed_users, or the user ids given.
MAY_ADDRESS_MODES = {"anyone", "allowed_users"}

USER_KEYS = {"name", "role", "profile", "worker_timeout", "context",
             "voice_transcription"}
CHANNEL_KEYS = {"name", "wake", "may_address", "member_role", "aliases",
                "reply_in_thread", "follow_threads", "profile", "worker_timeout",
                "context", "voice_transcription"}
DEFAULT_KEYS = {"tail_size", "debounce", "max_age", "worker_timeout",
                "max_parallel_dialogue", "profile", "max_parallel_jobs",
                "job_profile", "job_recovery", "voice_transcription"}
NUMERIC_DEFAULTS = {
    "tail_size": (1, 500, True),
    "debounce": (0, 300, False),
    "max_age": (10, 86400, True),
    "worker_timeout": (1, 3600, False),
    "max_parallel_dialogue": (1, 32, True),
    "max_parallel_jobs": (1, 32, True),
}
# What happens to a job whose listener went away while it ran: `requeue`
# continues it on its recorded session, `inspect` stops it and reports it to
# the conversation. A job without a recorded session is always stopped.
JOB_RECOVERY = {"requeue", "inspect"}
# Which audio clips the listener transcribes before the turn: none, those
# addressed without their words (any in a direct message; in a conversation
# one that wakes a turn on its own), or every one from a sender who may
# address the conversation, whose transcript then decides whether it woke.
VOICE_TRANSCRIPTION = ("off", "addressed", "auto")
VOICE_TRANSCRIPTION_DEFAULT = "addressed"


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
        _fail(path, "must be " + ("a whole number" if integer else "a number"))
    if not minimum <= value <= maximum:
        _fail(path, f"must be between {minimum} and {maximum}")


def _enum(value, choices, path):
    if not isinstance(value, str) or value not in choices:
        _fail(path, "must be one of: " + ", ".join(sorted(choices)))


def _string_list(value, path):
    if not isinstance(value, list):
        _fail(path, "must be a JSON array")
    for index, item in enumerate(value):
        _string(item, f"{path}[{index}]", nonempty=True)


def _profile(value, path):
    """A profile is named, never described here: the name resolves through
    the harness runner's own discovery, and the version it finds is checked at
    start, reload and in the doctor rather than by this walk."""
    if value is None:
        return
    _string(value, path, nonempty=True)
    if not PROFILE_RE.fullmatch(value):
        _fail(path, "must be a profile name: letters, digits, '.', '-' and '_'")


def _voice(value, path):
    if value.get("voice_transcription") is not None:
        _enum(value["voice_transcription"], VOICE_TRANSCRIPTION,
              f"{path}.voice_transcription")


def _common(value, path):
    if "profile" in value:
        _profile(value["profile"], f"{path}.profile")
    if "worker_timeout" in value:
        _number(value["worker_timeout"], f"{path}.worker_timeout", 1, 3600,
                nullable=True)
    if "context" in value:
        _string(value["context"], f"{path}.context", nullable=True)
    _voice(value, path)


def _user(value, path):
    value = _object(value, path)
    _unknown(value, USER_KEYS, path)
    for key in ("name", "role"):
        if key in value:
            _string(value[key], f"{path}.{key}", nonempty=True)
    _common(value, path)


def conversation_key(key):
    """(conversation id, thread ts or None) for an `allowed_channels` key, or
    None when the key is neither a conversation id nor one of its threads."""
    channel, _, thread = str(key).partition("/")
    if not CONVERSATION_RE.fullmatch(channel):
        return None
    if thread and not TS_RE.fullmatch(thread):
        return None
    return channel, (thread or None)


def _channel(value, path):
    value = _object(value, path)
    _unknown(value, CHANNEL_KEYS, path)
    for key in ("name", "member_role"):
        if key in value:
            _string(value[key], f"{path}.{key}", nonempty=True)
    if "wake" in value:
        _enum(value["wake"], WAKE_MODES, f"{path}.wake")
    for key in ("reply_in_thread", "follow_threads"):
        if key in value:
            _boolean(value[key], f"{path}.{key}")
    if "aliases" in value:
        _string_list(value["aliases"], f"{path}.aliases")
        for index, alias in enumerate(value["aliases"]):
            try:
                re.compile(alias)
            except re.error as exc:
                _fail(f"{path}.aliases[{index}]", f"is not a valid pattern: {exc}")
    if "may_address" in value:
        rule = value["may_address"]
        if isinstance(rule, list):
            _string_list(rule, f"{path}.may_address")
            for index, user in enumerate(rule):
                if not USER_RE.fullmatch(user):
                    _fail(f"{path}.may_address[{index}]",
                          "must be a Slack user id (U... or W...)")
        elif not isinstance(rule, str) or rule not in MAY_ADDRESS_MODES:
            _fail(f"{path}.may_address",
                  "must be one of: " + ", ".join(sorted(MAY_ADDRESS_MODES))
                  + ", or a list of Slack user ids")
    _common(value, path)


def _capability_rule(value, path):
    if isinstance(value, bool) or value == "*":
        return
    if isinstance(value, list):
        _string_list(value, path)
        return
    value = _object(value, path)
    _unknown(value, {"allow", "deny", "enabled", "scope", "verbs", "connections"},
             path)
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
                grant = _object(grant, f"{path}.connections.{connection}")
                _unknown(grant, {"allow_write"}, f"{path}.connections.{connection}")
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


def _defaults(value, path):
    value = _object(value, path)
    _unknown(value, DEFAULT_KEYS, path)
    for key, (minimum, maximum, integer) in NUMERIC_DEFAULTS.items():
        if key in value:
            _number(value[key], f"{path}.{key}", minimum, maximum,
                    integer=integer, nullable=True)
    if "profile" in value:
        _profile(value["profile"], f"{path}.profile")
    if "job_profile" in value:
        _profile(value["job_profile"], f"{path}.job_profile")
    if "job_recovery" in value and value["job_recovery"] is not None:
        _enum(value["job_recovery"], JOB_RECOVERY, f"{path}.job_recovery")
    _voice(value, path)


def validate_settings(settings):
    """Validate the whole settings document; return the same object."""
    settings = _object(settings, "settings")
    _unknown(settings, TOP_LEVEL, "settings")
    if "connection" in settings:
        # Empty, as the pilot's template wrote it, is the registry's default.
        _string(settings["connection"], "settings.connection", nullable=True)
    if "environment" in settings and settings["environment"] is not None:
        _string(settings["environment"], "settings.environment", nonempty=True)
        if not ENVIRONMENT_RE.fullmatch(settings["environment"]):
            _fail("settings.environment", f"must match {ENVIRONMENT_RE.pattern}")
    if "assistant_name" in settings:
        _string(settings["assistant_name"], "settings.assistant_name",
                nullable=True, nonempty=True)
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
        for user, policy in users.items():
            path = f"settings.allowed_users.{user}"
            if not USER_RE.fullmatch(str(user)):
                _fail(path, "key must be a Slack user id (U... or W...)")
            _user(policy, path)
    if "allowed_channels" in settings:
        channels = _object(settings["allowed_channels"], "settings.allowed_channels")
        for key, policy in channels.items():
            path = f"settings.allowed_channels.{key}"
            if conversation_key(key) is None:
                _fail(path, "key must be a conversation id (C..., G... or D...), "
                      "or <conversation id>/<thread ts> for one thread")
            _channel(policy, path)
    if "authority" in settings:
        _authority(settings["authority"], "settings.authority")
    if "defaults" in settings:
        _defaults(settings["defaults"], "settings.defaults")
    return settings


def dialogue_configured(settings):
    """Whether these settings admit anyone at all. Without an allowed user, a
    listed conversation or open direct messages the listener answers nothing,
    and needs neither a profile nor the harness runner."""
    settings = settings if isinstance(settings, dict) else {}
    direct = (settings.get("direct_messages") or {}).get("mode") or "allowlist"
    if direct == "anyone":
        return True
    if settings.get("allowed_channels"):
        return True
    return bool(settings.get("allowed_users")) and direct != "off"


def profile_names(settings, default, job_default=None):
    """Every profile name the settings reach, the default first, each once,
    with the positions that name it. The job profile is reached when the
    settings name one or a default for it is given."""
    settings = settings if isinstance(settings, dict) else {}
    found = {}

    def add(name, where):
        if name:
            found.setdefault(name, []).append(where)

    add((settings.get("defaults") or {}).get("profile") or default, "defaults.profile")
    add((settings.get("defaults") or {}).get("job_profile") or job_default,
        "defaults.job_profile")
    for user, policy in (settings.get("allowed_users") or {}).items():
        add((policy or {}).get("profile"), f"allowed_users.{user}.profile")
    for key, policy in (settings.get("allowed_channels") or {}).items():
        add((policy or {}).get("profile"), f"allowed_channels.{key}.profile")
    return found


def voice_scopes(settings):
    """Every admitted scope whose audio clips the listener would transcribe,
    as the settings position that decides it."""
    settings = settings if isinstance(settings, dict) else {}
    default = (settings.get("defaults") or {}).get("voice_transcription") \
        or VOICE_TRANSCRIPTION_DEFAULT
    direct = (settings.get("direct_messages") or {}).get("mode") or "allowlist"
    found = []
    if direct != "off":
        if direct == "anyone" and default != "off":
            found.append("defaults.voice_transcription")
        for user, policy in (settings.get("allowed_users") or {}).items():
            mode = (policy or {}).get("voice_transcription") or default
            if mode != "off":
                found.append(f"allowed_users.{user}")
    for key, policy in (settings.get("allowed_channels") or {}).items():
        mode = (policy or {}).get("voice_transcription") or default
        if mode != "off":
            found.append(f"allowed_channels.{key}")
    return found
