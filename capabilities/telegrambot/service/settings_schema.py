"""The telegrambot service settings: one closed surface, one spelling per key.

A key the walk does not name is refused with the path that carries it and the
keys that would be read there, never ignored. The CLI reads it (`settings`,
`service doctor`, `reload`) and so does the running service on every reload, so
one walk decides what a settings document means. Every key is optional: an
empty document is a bot that answers nobody yet and records who wrote to it.

  assistant_name     the name the bot answers to in a group (an alias), and the
                     name it goes by in its prompt; else the bot's own first name
  prompt_mode        extend (default): the agent's prompt (`bot`) after the
                     standard behaviour; replace: in place of it
  direct_messages    {mode: allowed_users|anyone|off, default_role}
  allowed_users      {<user id>: {name, role, may_address, profile,
                     worker_timeout, context, context_prompt, context_mode,
                     project, voice_transcription, voice_echo, delegation}}
  allowed_groups     {<chat id>: {name, member_role, members, may_address,
                     aliases, require_reference, triggers, gate, profile,
                     worker_timeout, voice_transcription, voice_echo, context,
                     context_prompt, context_mode, project, delegation,
                     agent_dialogue, topics}}
  agent_dialogue     {max_turns}: turns in a row other bots may start in one chat
                     before a person writes; 4 by default, 0 for no limit
  roles              {<role>: {profile}}: the profile a sender's turn runs on when
                     their role names one, in place of the chat's or topic's
  control            {roles: {<role>: {commands}}}
  authority          {default, roles: {<role>: {allowed_capabilities}}}
  defaults           tail_size, history_requests, debounce, worker_timeout,
                     progress_after, progress_from_stream, max_parallel_jobs,
                     job_recovery, profile, job_profile, voice_transcription,
                     voice_echo, delegation
"""

from __future__ import annotations

import re

TOP_LEVEL = {"assistant_name", "prompt_mode", "direct_messages", "allowed_users",
             "allowed_groups", "agent_dialogue", "roles", "control", "authority",
             "defaults"}
PROMPT_MODES = ("extend", "replace")
DIRECT_MODES = ("allowed_users", "anyone", "off")
MAY_ADDRESS = ("anyone", "members")
CONTEXT_MODES = ("extend", "exclusive")
VOICE_MODES = ("off", "addressed", "auto")
DELEGATION = ("allowed", "disabled")
JOB_RECOVERY = ("requeue", "inspect")
TRIGGERS = ("username", "alias", "reply")
GATE_MODES = ("shadow", "live")
CONTROL_COMMANDS = ("status", "stop", "profiles", "reload", "set", "help")
PROFILE_PREFIX = "telegrambot-"
PROFILE_RE = re.compile(r"telegrambot-[A-Za-z0-9._-]{1,100}")
PROMPT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

DEFAULTS = {
    "tail_size": 40,
    "history_requests": 5,
    "debounce": 2,
    "worker_timeout": 120,
    "progress_after": 15,
    "progress_from_stream": False,
    "max_parallel_jobs": 1,
    "job_recovery": "requeue",
    "profile": None,              # the harness the machine signed in to (profiles.py)
    "job_profile": None,          # the job profile of that same harness
    "voice_transcription": "addressed",
    "voice_echo": True,
    "delegation": "disabled",
}
NUMBERS = {
    "tail_size": (1, 100, True),
    "history_requests": (1, 20, True),
    "debounce": (0, 300, False),
    "worker_timeout": (1, 3600, False),
    "progress_after": (0, 3600, False),
    "max_parallel_jobs": (1, 32, True),
}
DEFAULT_MAX_TURNS = 4

COMMON_KEYS = {"name", "profile", "worker_timeout", "context", "context_prompt",
               "context_mode", "project", "voice_transcription", "voice_echo",
               "delegation"}
USER_KEYS = COMMON_KEYS | {"role", "may_address"}
MEMBER_KEYS = {"name", "role", "may_address"}
GROUP_KEYS = COMMON_KEYS | {"member_role", "members", "may_address", "aliases",
                            "require_reference", "triggers", "gate", "agent_dialogue",
                            "topics"}
TOPIC_KEYS = {"name", "profile", "context", "context_prompt", "context_mode", "project"}
ROLE_KEYS = {"profile"}
GATE_KEYS = {"enabled", "mode", "brief", "context_messages", "context_chars", "max_batch",
             "model", "effort"}


def _fail(path, message):
    raise ValueError(f"{path}: {message}")


def _object(value, path) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be a JSON object")
    return value


def _unknown(value, allowed, path):
    for key in value:
        if key not in allowed:
            _fail(f"{path}.{key}", f"unsupported property {key!r}; supported properties: "
                  + ", ".join(sorted(allowed)))


def _string(value, path, *, nonempty=True):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        _fail(path, "must be a non-empty string" if nonempty else "must be a string")


def _boolean(value, path):
    if not isinstance(value, bool):
        _fail(path, "must be true or false")


def _number(value, path, low, high, integer):
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind):
        _fail(path, "must be " + ("a whole number" if integer else "a number"))
    if not low <= value <= high:
        _fail(path, f"must be between {low} and {high}")


def _enum(value, choices, path):
    if value not in choices:
        _fail(path, "must be one of: " + ", ".join(choices))


def _strings(value, path):
    if not isinstance(value, list):
        _fail(path, "must be a JSON array")
    for index, item in enumerate(value):
        _string(item, f"{path}[{index}]")


def _id_key(key, path, sign):
    """A Telegram id as a settings key: positive for a person or a topic,
    negative for a group."""
    try:
        value = int(key)
    except (TypeError, ValueError):
        _fail(f"{path}.{key}", "the key must be a Telegram id, a whole number")
    if (sign > 0 and value <= 0) or (sign < 0 and value >= 0):
        _fail(f"{path}.{key}", "the key must be a "
              + ("positive id (a person or a topic)" if sign > 0 else
                 "negative id (a group: -<id>, a supergroup: -100<id>)"))
    return value


def profile_name(value, path):
    _string(value, path)
    if not PROFILE_RE.fullmatch(value):
        _fail(path, f"must be a profile named {PROFILE_PREFIX}<name>")


def _common(value, path):
    if "name" in value:
        _string(value["name"], f"{path}.name")
    if "profile" in value:
        profile_name(value["profile"], f"{path}.profile")
    if "worker_timeout" in value:
        _number(value["worker_timeout"], f"{path}.worker_timeout", 1, 3600, False)
    if "context" in value:
        _string(value["context"], f"{path}.context", nonempty=False)
    if "context_prompt" in value:
        _string(value["context_prompt"], f"{path}.context_prompt")
        if not PROMPT_NAME_RE.fullmatch(value["context_prompt"]):
            _fail(f"{path}.context_prompt", "must name a prompt: lowercase letters, digits, "
                  "_ and -; it is read from the prompt key context.<name>")
    if "context_mode" in value:
        _enum(value["context_mode"], CONTEXT_MODES, f"{path}.context_mode")
    if "project" in value:
        _string(value["project"], f"{path}.project")
        if not (value["project"].startswith("/") or value["project"].startswith("~")):
            _fail(f"{path}.project", "must be an absolute path (or start with ~)")
    if "voice_transcription" in value:
        _enum(value["voice_transcription"], VOICE_MODES, f"{path}.voice_transcription")
    if "voice_echo" in value:
        _boolean(value["voice_echo"], f"{path}.voice_echo")
    if "delegation" in value:
        _enum(value["delegation"], DELEGATION, f"{path}.delegation")


def _user(value, path):
    value = _object(value, path)
    _unknown(value, USER_KEYS, path)
    _common(value, path)
    if "role" in value:
        _string(value["role"], f"{path}.role")
    if "may_address" in value:
        _boolean(value["may_address"], f"{path}.may_address")


def _gate(value, path):
    value = _object(value, path)
    _unknown(value, GATE_KEYS, path)
    if "enabled" in value:
        _boolean(value["enabled"], f"{path}.enabled")
    if "mode" in value:
        _enum(value["mode"], GATE_MODES, f"{path}.mode")
    if "brief" in value:
        _string(value["brief"], f"{path}.brief", nonempty=False)
    for key, (low, high) in (("context_messages", (0, 100)), ("context_chars", (0, 20000)),
                             ("max_batch", (1, 50))):
        if key in value:
            _number(value[key], f"{path}.{key}", low, high, True)
    for key in ("model", "effort"):
        if key in value:
            item = value[key]
            if isinstance(item, dict):
                _unknown(item, {"claude", "codex"}, f"{path}.{key}")
                for harness, one in item.items():
                    _string(one, f"{path}.{key}.{harness}")
            else:
                _string(item, f"{path}.{key}")


def _agent_dialogue(value, path):
    value = _object(value, path)
    _unknown(value, {"max_turns"}, path)
    if "max_turns" in value:
        _number(value["max_turns"], f"{path}.max_turns", 0, 100, True)


def _group(value, path):
    value = _object(value, path)
    _unknown(value, GROUP_KEYS, path)
    _common(value, path)
    if "member_role" in value:
        _string(value["member_role"], f"{path}.member_role")
    if "members" in value:
        members = _object(value["members"], f"{path}.members")
        for key, member in members.items():
            _id_key(key, f"{path}.members", 1)
            member = _object(member, f"{path}.members.{key}")
            _unknown(member, MEMBER_KEYS, f"{path}.members.{key}")
            for field in ("name", "role"):
                if field in member:
                    _string(member[field], f"{path}.members.{key}.{field}")
            if "may_address" in member:
                _boolean(member["may_address"], f"{path}.members.{key}.may_address")
    if "may_address" in value:
        _enum(value["may_address"], MAY_ADDRESS, f"{path}.may_address")
    if "aliases" in value:
        _strings(value["aliases"], f"{path}.aliases")
    if "require_reference" in value:
        _boolean(value["require_reference"], f"{path}.require_reference")
    if "triggers" in value:
        _strings(value["triggers"], f"{path}.triggers")
        for index, one in enumerate(value["triggers"]):
            _enum(one, TRIGGERS, f"{path}.triggers[{index}]")
        if len(set(value["triggers"])) != len(value["triggers"]):
            _fail(f"{path}.triggers", "names a trigger twice")
    if "gate" in value:
        _gate(value["gate"], f"{path}.gate")
    if "agent_dialogue" in value:
        _agent_dialogue(value["agent_dialogue"], f"{path}.agent_dialogue")
    if "topics" in value:
        topics = _object(value["topics"], f"{path}.topics")
        for key, topic in topics.items():
            _id_key(key, f"{path}.topics", 1)
            topic = _object(topic, f"{path}.topics.{key}")
            _unknown(topic, TOPIC_KEYS, f"{path}.topics.{key}")
            _common(topic, f"{path}.topics.{key}")


def _commands(value, path):
    if value == "*":
        return
    _strings(value, path)
    for index, one in enumerate(value):
        if one != "*":
            _enum(one, CONTROL_COMMANDS, f"{path}[{index}]")


def _capability_rule(value, path):
    if isinstance(value, bool) or value == "*":
        return
    if isinstance(value, list):
        _strings(value, path)
        return
    value = _object(value, path)
    _unknown(value, {"allow", "deny", "enabled", "scope", "verbs", "connections"}, path)
    for key in ("allow", "deny", "enabled"):
        if key in value:
            _boolean(value[key], f"{path}.{key}")
    if "scope" in value:
        _string(value["scope"], f"{path}.scope")
    if "verbs" in value:
        _strings(value["verbs"], f"{path}.verbs")
    if "connections" in value:
        _strings(value["connections"], f"{path}.connections")


def _authority_policy(value, path):
    value = _object(value, path)
    _unknown(value, {"allowed_capabilities"}, path)
    if "allowed_capabilities" in value:
        caps = value["allowed_capabilities"]
        if caps is True or caps == "*":
            return
        if isinstance(caps, list):
            _strings(caps, f"{path}.allowed_capabilities")
            return
        for name, rule in _object(caps, f"{path}.allowed_capabilities").items():
            _capability_rule(rule, f"{path}.allowed_capabilities.{name}")


def _defaults(value, path):
    value = _object(value, path)
    _unknown(value, set(DEFAULTS), path)
    for key, (low, high, integer) in NUMBERS.items():
        if key in value:
            _number(value[key], f"{path}.{key}", low, high, integer)
    for key in ("progress_from_stream", "voice_echo"):
        if key in value:
            _boolean(value[key], f"{path}.{key}")
    for key in ("profile", "job_profile"):
        if key in value:
            profile_name(value[key], f"{path}.{key}")
    if "job_recovery" in value:
        _enum(value["job_recovery"], JOB_RECOVERY, f"{path}.job_recovery")
    if "voice_transcription" in value:
        _enum(value["voice_transcription"], VOICE_MODES, f"{path}.voice_transcription")
    if "delegation" in value:
        _enum(value["delegation"], DELEGATION, f"{path}.delegation")


def validate_settings(data) -> dict:
    """The document itself when it holds; else ValueError naming the path."""
    data = _object(data if data is not None else {}, "settings")
    _unknown(data, TOP_LEVEL, "settings")
    if "assistant_name" in data:
        _string(data["assistant_name"], "settings.assistant_name")
    if "prompt_mode" in data:
        _enum(data["prompt_mode"], PROMPT_MODES, "settings.prompt_mode")
    if "direct_messages" in data:
        direct = _object(data["direct_messages"], "settings.direct_messages")
        _unknown(direct, {"mode", "default_role"}, "settings.direct_messages")
        if "mode" in direct:
            _enum(direct["mode"], DIRECT_MODES, "settings.direct_messages.mode")
        if "default_role" in direct:
            _string(direct["default_role"], "settings.direct_messages.default_role")
    for key, entry in _object(data.get("allowed_users") or {}, "settings.allowed_users").items():
        _id_key(key, "settings.allowed_users", 1)
        _user(entry, f"settings.allowed_users.{key}")
    for key, entry in _object(data.get("allowed_groups") or {}, "settings.allowed_groups").items():
        _id_key(key, "settings.allowed_groups", -1)
        _group(entry, f"settings.allowed_groups.{key}")
    if "agent_dialogue" in data:
        _agent_dialogue(data["agent_dialogue"], "settings.agent_dialogue")
    for role, rule in _object(data.get("roles") or {}, "settings.roles").items():
        rule = _object(rule, f"settings.roles.{role}")
        _unknown(rule, ROLE_KEYS, f"settings.roles.{role}")
        if "profile" in rule:
            profile_name(rule["profile"], f"settings.roles.{role}.profile")
    if "control" in data:
        control = _object(data["control"], "settings.control")
        _unknown(control, {"roles"}, "settings.control")
        for role, rule in _object(control.get("roles") or {}, "settings.control.roles").items():
            rule = _object(rule, f"settings.control.roles.{role}")
            _unknown(rule, {"commands"}, f"settings.control.roles.{role}")
            if "commands" in rule:
                _commands(rule["commands"], f"settings.control.roles.{role}.commands")
    if "authority" in data:
        authority = _object(data["authority"], "settings.authority")
        _unknown(authority, {"default", "roles"}, "settings.authority")
        if "default" in authority:
            _authority_policy(authority["default"], "settings.authority.default")
        for role, rule in _object(authority.get("roles") or {},
                                  "settings.authority.roles").items():
            _authority_policy(rule, f"settings.authority.roles.{role}")
    if "defaults" in data:
        _defaults(data["defaults"], "settings.defaults")
    return data


def profile_names(settings: dict) -> dict[str, list[str]]:
    """Every profile name the settings reach, with the positions that name it."""
    found: dict[str, list[str]] = {}

    def note(name, where):
        if name:
            found.setdefault(name, []).append(where)

    defaults = settings.get("defaults") or {}
    note(defaults.get("profile"), "defaults.profile")
    note(defaults.get("job_profile"), "defaults.job_profile")
    for key, user in (settings.get("allowed_users") or {}).items():
        note((user or {}).get("profile"), f"allowed_users.{key}.profile")
    for key, group in (settings.get("allowed_groups") or {}).items():
        note((group or {}).get("profile"), f"allowed_groups.{key}.profile")
        for topic, entry in ((group or {}).get("topics") or {}).items():
            note((entry or {}).get("profile"), f"allowed_groups.{key}.topics.{topic}.profile")
    for role, rule in (settings.get("roles") or {}).items():
        note((rule or {}).get("profile"), f"roles.{role}.profile")
    return found


def context_prompts(settings: dict) -> list[str]:
    """Every `context.<name>` prompt key the settings name."""
    names = set()
    for user in (settings.get("allowed_users") or {}).values():
        if (user or {}).get("context_prompt"):
            names.add(user["context_prompt"])
    for group in (settings.get("allowed_groups") or {}).values():
        if (group or {}).get("context_prompt"):
            names.add(group["context_prompt"])
        for topic in ((group or {}).get("topics") or {}).values():
            if (topic or {}).get("context_prompt"):
                names.add(topic["context_prompt"])
    return sorted(f"context.{n}" for n in names)
