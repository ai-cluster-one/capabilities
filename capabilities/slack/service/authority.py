"""Build the per-job CAPABILITIES_AUTH_CONTEXT envelope from service settings.

The precedence mirrors the Telegram assistant service:
role -> sender -> channel -> channel member. The most-specific
``allowed_capabilities`` value replaces the less-specific value wholesale.
Capability CLIs enforce this on their normal invocation path; the worker itself
is trusted and unrestricted.
"""

import re

_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


def _deep_merge(base, overlay):
    out = dict(base or {})
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _policy_caps(policy):
    if not isinstance(policy, dict):
        return None
    if "allowed_capabilities" in policy:
        return policy.get("allowed_capabilities")
    return policy.get("capabilities")


def _overlay_policy(policy, row):
    if not isinstance(row, dict):
        return policy
    policy = _deep_merge(policy, row.get("authority") or {})
    caps = _policy_caps(row)
    if caps is not None:
        policy["allowed_capabilities"] = caps
    return policy


def _normalize_rule(rule):
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


def normalize_allowed_capabilities(value):
    if value is True or value == "*":
        return {"*": True}
    if isinstance(value, list):
        return {str(name): True for name in value}
    if isinstance(value, dict):
        return {str(name): _normalize_rule(rule) for name, rule in value.items()}
    return {}


def build_auth_context(
    settings,
    *,
    role,
    connection,
    conversation,
    sender_id,
    sender_name,
    channel_id=None,
):
    settings = settings or {}
    authority = settings.get("authority") or {}
    roles = authority.get("roles") or {}
    policy = _deep_merge(authority.get("default"), roles.get(role))
    role_caps = _policy_caps(roles.get(role))
    if role_caps is not None:
        policy["allowed_capabilities"] = role_caps

    sender = (settings.get("allowed_users") or {}).get(str(sender_id))
    policy = _overlay_policy(policy, sender)

    channel_id = channel_id or str(conversation).split(":", 1)[0]
    channel = (settings.get("allowed_channels") or {}).get(str(channel_id))
    policy = _overlay_policy(policy, channel)
    member = (
        (channel.get("members") or {}).get(str(sender_id))
        if isinstance(channel, dict)
        else None
    )
    policy = _overlay_policy(policy, member)

    caps = normalize_allowed_capabilities(_policy_caps(policy))
    allowed_capability_names(caps)
    return {
        "version": 1,
        "source": "slack",
        "connection": connection,
        "chat_id": conversation,
        "channel_id": channel_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_role": role,
        "allowed_capabilities": caps,
    }


def _rule_allows(rule) -> bool:
    if rule is True:
        return True
    if not isinstance(rule, dict):
        return False
    return not (
        rule.get("deny") is True
        or rule.get("enabled") is False
        or rule.get("allow") is False
    )


def allowed_capability_names(caps) -> list[str]:
    """Return effective names; ``*`` represents all installed capabilities."""
    normalized = normalize_allowed_capabilities(caps)
    names = [str(name) for name, rule in normalized.items() if _rule_allows(rule)]
    invalid = sorted(
        {name for name in names if name != "*" and not _CAPABILITY_NAME.fullmatch(name)}
    )
    if invalid:
        raise ValueError(f"invalid capability names: {', '.join(invalid)}")
    return sorted(set(names))


def summarize(caps) -> str:
    names = allowed_capability_names(caps)
    if "*" in names:
        return "all capabilities"
    return ", ".join(names) if names else "no capabilities"
