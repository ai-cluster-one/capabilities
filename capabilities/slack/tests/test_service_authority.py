import pytest
from authority import (
    allowed_capability_names,
    build_auth_context,
    summarize,
)


def _settings(roles):
    return {"authority": {"roles": roles}}


def test_missing_authority_fails_closed():
    ctx = build_auth_context(
        {},
        role="default",
        connection="c",
        conversation="D1",
        sender_id="U1",
        sender_name="A",
    )
    assert ctx["allowed_capabilities"] == {}


def test_default_role_capabilities():
    s = _settings(
        {"default": {"allowed_capabilities": {"slack": {"scope": "current_chat"}}}}
    )
    ctx = build_auth_context(
        s,
        role="default",
        connection="workspace",
        conversation="D1",
        sender_id="U1",
        sender_name="Alice",
    )
    assert ctx["allowed_capabilities"] == {"slack": {"scope": "current_chat"}}
    assert ctx["source"] == "slack"
    assert ctx["sender_role"] == "default"
    assert ctx["chat_id"] == "D1"
    assert ctx["sender_id"] == "U1"


def test_wildcard_role_caps_are_rejected():
    s = _settings(
        {
            "default": {"allowed_capabilities": {"slack": {"scope": "current_chat"}}},
            "supervisor": {"allowed_capabilities": {"*": True}},
        }
    )
    with pytest.raises(ValueError, match="wildcard"):
        build_auth_context(
            s,
            role="supervisor",
            connection="c",
            conversation="D1",
            sender_id="U1",
            sender_name="A",
        )


def test_role_without_caps_inherits_default_caps():
    s = _settings(
        {
            "default": {"allowed_capabilities": {"youtrack": True}},
            "reader": {"control": {"commands": ["status"]}},  # no allowed_capabilities
        }
    )
    ctx = build_auth_context(
        s,
        role="reader",
        connection="c",
        conversation="D1",
        sender_id="U1",
        sender_name="A",
    )
    assert ctx["allowed_capabilities"] == {"youtrack": True}


def test_missing_role_uses_default_only():
    s = _settings({"default": {"allowed_capabilities": {"youtrack": True}}})
    ctx = build_auth_context(
        s,
        role="ghost",
        connection="c",
        conversation="D1",
        sender_id="U1",
        sender_name="A",
    )
    assert ctx["allowed_capabilities"] == {"youtrack": True}


def test_summarize_reads_caps():
    assert "youtrack" in summarize(
        {"youtrack": True, "slack": {"scope": "current_chat"}}
    )
    assert summarize({}) == "no capabilities"


def test_allowed_names_exclude_denied_rules_and_validate_names():
    assert allowed_capability_names(
        {
            "youtrack": True,
            "mail": {"deny": True},
            "stripe": {"allow": False},
        }
    ) == ["youtrack"]
    with pytest.raises(ValueError, match="invalid capability"):
        allowed_capability_names({"../escape": True})
