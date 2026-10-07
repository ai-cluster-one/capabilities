"""Strict, dependency-free validation for the WhatsApp assistant settings.

The surface is closed: a key it does not name is refused with the path that
carries it and the keys that would be read there, never ignored. It is loaded
by the CLI (`service init`, `start`, `run`, `doctor`, `reload`) and by the
listener's own reload, so one walk decides what a settings document means.
"""

import re

TOP_LEVEL = {
    "connection", "environment", "assistant_name", "direct_messages",
    "allowed_users", "allowed_groups", "control", "authority", "defaults",
    # Read as `defaults.send_rate`; kept so a listener seeded before the
    # dialogue keys existed starts unchanged.
    "send_rate",
}
DEPRECATED_TOP_LEVEL = {"send_rate": "defaults.send_rate"}
ENVIRONMENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.IGNORECASE)
# A person is named by the phone number of their WhatsApp account, digits with
# the country code; a leading + is accepted and means nothing.
PHONE_RE = re.compile(r"\+?[1-9][0-9]{5,15}")
GROUP_RE = re.compile(r"[0-9]+(-[0-9]+)?@g\.us")
PROFILE_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")
DIRECT_MODES = {"allowlist", "anyone", "off"}
# Who in an allowed group may start a turn: any member, only the senders the
# settings list under allowed_users, or the phone numbers given.
MAY_ADDRESS_MODES = {"anyone", "allowed_users"}
CONTROL_COMMANDS = {"status", "set", "reload", "stop", "help", "*"}
SEND_RATE_CEILING = 120

USER_KEYS = {"name", "role", "profile", "worker_timeout", "context"}
GROUP_KEYS = {"name", "require_reference", "aliases", "may_address",
              "member_role", "profile", "worker_timeout", "context"}
DEFAULT_KEYS = {"tail_size", "debounce", "max_age", "worker_timeout",
                "max_parallel_dialogue", "profile", "send_rate"}
NUMERIC_DEFAULTS = {
    "tail_size": (1, 500, True),
    "debounce": (0, 300, False),
    "max_age": (10, 86400, True),
    "worker_timeout": (1, 3600, False),
    "max_parallel_dialogue": (1, 32, True),
    "send_rate": (1, SEND_RATE_CEILING, True),
}


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
    the harness runner's own discovery, and the file it finds is checked at
    start, reload and in the doctor rather than by this walk."""
    if value is None:
        return
    _string(value, path, nonempty=True)
    if not PROFILE_RE.fullmatch(value):
        _fail(path, "must be a profile name: letters, digits, '.', '-' and '_'")


def phone_key(value):
    """The digits a phone-number key stands for."""
    return re.sub(r"\D", "", str(value or ""))


def _user(value, path):
    value = _object(value, path)
    _unknown(value, USER_KEYS, path)
    for key in ("name", "role"):
        if key in value:
            _string(value[key], f"{path}.{key}", nonempty=True)
    if "profile" in value:
        _profile(value["profile"], f"{path}.profile")
    if "worker_timeout" in value:
        _number(value["worker_timeout"], f"{path}.worker_timeout", 1, 3600,
                nullable=True)
    if "context" in value:
        _string(value["context"], f"{path}.context", nullable=True)


def _group(value, path):
    value = _object(value, path)
    _unknown(value, GROUP_KEYS, path)
    for key in ("name", "member_role"):
        if key in value:
            _string(value[key], f"{path}.{key}", nonempty=True)
    if "require_reference" in value:
        _boolean(value["require_reference"], f"{path}.require_reference")
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
            for index, phone in enumerate(rule):
                if not PHONE_RE.fullmatch(phone):
                    _fail(f"{path}.may_address[{index}]",
                          "must be a phone number with its country code")
        else:
            if not isinstance(rule, str) or rule not in MAY_ADDRESS_MODES:
                _fail(f"{path}.may_address",
                      "must be one of: " + ", ".join(sorted(MAY_ADDRESS_MODES))
                      + ", or a list of phone numbers")
    if "profile" in value:
        _profile(value["profile"], f"{path}.profile")
    if "worker_timeout" in value:
        _number(value["worker_timeout"], f"{path}.worker_timeout", 1, 3600,
                nullable=True)
    if "context" in value:
        _string(value["context"], f"{path}.context", nullable=True)


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
            _fail(f"{path}.commands", f"unsupported command {sorted(unknown)[0]!r}; "
                  "supported: " + ", ".join(sorted(CONTROL_COMMANDS)))
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


def _defaults(value, path):
    value = _object(value, path)
    _unknown(value, DEFAULT_KEYS, path)
    for key, (minimum, maximum, integer) in NUMERIC_DEFAULTS.items():
        if key in value:
            _number(value[key], f"{path}.{key}", minimum, maximum,
                    integer=integer, nullable=True)
    if "profile" in value:
        _profile(value["profile"], f"{path}.profile")


def validate_settings(settings):
    """Validate the whole settings document; return the same object."""
    settings = _object(settings, "settings")
    _unknown(settings, TOP_LEVEL, "settings")
    if "connection" in settings:
        _string(settings["connection"], "settings.connection", nullable=True,
                nonempty=True)
    if "environment" in settings and settings["environment"] is not None:
        _string(settings["environment"], "settings.environment", nonempty=True)
        if not ENVIRONMENT_RE.fullmatch(settings["environment"]):
            _fail("settings.environment", f"must match {ENVIRONMENT_RE.pattern}")
    if "assistant_name" in settings:
        _string(settings["assistant_name"], "settings.assistant_name",
                nullable=True, nonempty=True)
    if "send_rate" in settings:
        _number(settings["send_rate"], "settings.send_rate", 1, SEND_RATE_CEILING,
                integer=True, nullable=True)
        if "send_rate" in (settings.get("defaults") or {}):
            _fail("settings.send_rate", "is the deprecated spelling of "
                  "defaults.send_rate; set one of them, not both")
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
        seen = {}
        for phone, policy in users.items():
            path = f"settings.allowed_users.{phone}"
            if not PHONE_RE.fullmatch(str(phone)):
                _fail(path, "key must be a phone number with its country code, "
                      "digits only (a leading + is accepted)")
            digits = phone_key(phone)
            if digits in seen:
                _fail(path, f"names the same number as {seen[digits]!r}")
            seen[digits] = phone
            _user(policy, path)
    if "allowed_groups" in settings:
        groups = _object(settings["allowed_groups"], "settings.allowed_groups")
        for jid, policy in groups.items():
            path = f"settings.allowed_groups.{jid}"
            if not GROUP_RE.fullmatch(str(jid)):
                _fail(path, "key must be a group JID, <digits>@g.us")
            _group(policy, path)
    if "control" in settings:
        _control(settings["control"], "settings.control")
    if "authority" in settings:
        _authority(settings["authority"], "settings.authority")
    if "defaults" in settings:
        _defaults(settings["defaults"], "settings.defaults")
    return settings


def deprecations(settings):
    """What a valid document says in a spelling that is going away."""
    found = []
    for key, replacement in DEPRECATED_TOP_LEVEL.items():
        if isinstance(settings, dict) and key in settings:
            found.append(f"settings.{key} is read as {replacement}; name that instead")
    return found


def dialogue_configured(settings):
    """Whether these settings admit anyone at all. Without an allowed user, an
    allowed group or open direct messages the listener answers nothing, and
    needs neither a profile nor the harness runner."""
    settings = settings if isinstance(settings, dict) else {}
    direct = settings.get("direct_messages") or {}
    if (direct.get("mode") or "allowlist") == "anyone":
        return True
    if settings.get("allowed_groups"):
        return True
    return bool(settings.get("allowed_users")) and (direct.get("mode") or "allowlist") != "off"


def profile_names(settings, default):
    """Every profile name the settings reach, the default first, each once,
    with the positions that name it."""
    settings = settings if isinstance(settings, dict) else {}
    found = {}

    def add(name, where):
        if name:
            found.setdefault(name, []).append(where)

    add((settings.get("defaults") or {}).get("profile") or default, "defaults.profile")
    for phone, policy in (settings.get("allowed_users") or {}).items():
        add((policy or {}).get("profile"), f"allowed_users.{phone}.profile")
    for jid, policy in (settings.get("allowed_groups") or {}).items():
        add((policy or {}).get("profile"), f"allowed_groups.{jid}.profile")
    return found
