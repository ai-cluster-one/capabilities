"""Build the per-job CAPABILITIES_AUTH_CONTEXT envelope from service settings.

Shape matches the shared capability _auth_gate. The Slack service is stricter
than the generic envelope: missing policy means no capabilities, and wildcard
grants are rejected because a headless ingress must enumerate its authority."""

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


def build_auth_context(
    settings, *, role, connection, conversation, sender_id, sender_name
):
    roles = ((settings or {}).get("authority") or {}).get("roles") or {}
    policy = _deep_merge(roles.get("default"), roles.get(role))
    role_caps = (roles.get(role) or {}).get("allowed_capabilities")
    if role_caps is not None:
        policy["allowed_capabilities"] = role_caps  # most-specific level wins wholesale
    caps = policy.get("allowed_capabilities", {})
    allowed_capability_names(caps)
    return {
        "version": 1,
        "source": "slack",
        "connection": connection,
        "chat_id": conversation,
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
    """Return explicit allowed capability names or reject an unsafe envelope."""
    if caps in (True, "*"):
        raise ValueError(
            "wildcard capability authority is not allowed for Slack workers"
        )
    if isinstance(caps, list):
        names = [str(name) for name in caps]
    elif isinstance(caps, dict):
        if "*" in caps:
            raise ValueError(
                "wildcard capability authority is not allowed for Slack workers"
            )
        names = [str(name) for name, rule in caps.items() if _rule_allows(rule)]
    elif caps in (None, False):
        names = []
    else:
        raise ValueError("allowed_capabilities must be a list or object")
    invalid = sorted({name for name in names if not _CAPABILITY_NAME.fullmatch(name)})
    if invalid:
        raise ValueError(f"invalid capability names: {', '.join(invalid)}")
    return sorted(set(names))


def summarize(caps) -> str:
    names = allowed_capability_names(caps)
    return ", ".join(names) if names else "no capabilities"


def network_domains(settings, role) -> list[str]:
    """Return the effective explicit network allow-list for a role."""
    roles = ((settings or {}).get("authority") or {}).get("roles") or {}
    policy = _deep_merge(roles.get("default"), roles.get(role))
    domains = policy.get("network_domains") or []
    return sorted(set(domains))
