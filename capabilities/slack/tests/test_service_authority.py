from authority import build_auth_context, summarize


def _settings(roles):
    return {"authority": {"roles": roles}}


def test_none_when_no_authority_configured():
    assert build_auth_context({}, role="default", connection="c", conversation="D1",
                              sender_id="U1", sender_name="A") is None


def test_default_role_capabilities():
    s = _settings({"default": {"allowed_capabilities": {"slack": {"scope": "current_chat"}}}})
    ctx = build_auth_context(s, role="default", connection="ionwater", conversation="D1",
                             sender_id="U1", sender_name="Alice")
    assert ctx["allowed_capabilities"] == {"slack": {"scope": "current_chat"}}
    assert ctx["source"] == "slack"
    assert ctx["sender_role"] == "default"
    assert ctx["chat_id"] == "D1"
    assert ctx["sender_id"] == "U1"


def test_role_caps_override_default_caps():
    s = _settings({
        "default": {"allowed_capabilities": {"slack": {"scope": "current_chat"}}},
        "supervisor": {"allowed_capabilities": {"*": True}},
    })
    ctx = build_auth_context(s, role="supervisor", connection="c", conversation="D1",
                             sender_id="U1", sender_name="A")
    assert ctx["allowed_capabilities"] == {"*": True}


def test_role_without_caps_inherits_default_caps():
    s = _settings({
        "default": {"allowed_capabilities": {"youtrack": True}},
        "reader": {"control": {"commands": ["status"]}},  # no allowed_capabilities
    })
    ctx = build_auth_context(s, role="reader", connection="c", conversation="D1",
                             sender_id="U1", sender_name="A")
    assert ctx["allowed_capabilities"] == {"youtrack": True}


def test_missing_role_uses_default_only():
    s = _settings({"default": {"allowed_capabilities": {"youtrack": True}}})
    ctx = build_auth_context(s, role="ghost", connection="c", conversation="D1",
                             sender_id="U1", sender_name="A")
    assert ctx["allowed_capabilities"] == {"youtrack": True}


def test_summarize_reads_caps():
    assert summarize({"*": True}) == "all capabilities"
    assert "youtrack" in summarize({"youtrack": True, "slack": {"scope": "current_chat"}})
    assert summarize({}) == "no capabilities"
