"""Build the per-job CAPABILITIES_AUTH_CONTEXT envelope from service settings.

Shape matches bin/slack's _auth_gate: allowed_capabilities may be True/'*', a
list, or a {capability: rule} dict. Returns None when no authority policy is
configured (then the ordinary project gate is the whole policy)."""


def _deep_merge(base, overlay):
    out = dict(base or {})
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def build_auth_context(settings, *, role, connection, conversation,
                       sender_id, sender_name):
    roles = ((settings or {}).get("authority") or {}).get("roles") or {}
    if not roles:
        return None
    policy = _deep_merge(roles.get("default"), roles.get(role))
    role_caps = (roles.get(role) or {}).get("allowed_capabilities")
    if role_caps is not None:
        policy["allowed_capabilities"] = role_caps   # most-specific level wins wholesale
    caps = policy.get("allowed_capabilities")
    return {
        "version": 1,
        "source": "slack",
        "connection": connection,
        "chat_id": conversation,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_role": role,
        "allowed_capabilities": caps if caps is not None else {},
    }


def summarize(caps) -> str:
    if caps is True or caps == "*" or (isinstance(caps, dict) and caps.get("*") is True):
        return "all capabilities"
    if isinstance(caps, list):
        return ", ".join(map(str, caps)) or "no capabilities"
    if isinstance(caps, dict):
        names = [k for k, v in caps.items() if v is not False]
        return ", ".join(sorted(names)) if names else "no capabilities"
    return "no capabilities"
