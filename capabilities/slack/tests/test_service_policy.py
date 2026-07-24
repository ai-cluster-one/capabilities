from policy import (parse_event, accept, route, conversation_key, resolve_role,
                    strip_mention, control_command, control_allowed, select_catchup)


def _im(user="U1", channel="D1", ts="100.1", text="hi", subtype=None, bot_id=None):
    e = {"type": "message", "channel_type": "im", "user": user,
         "channel": channel, "ts": ts, "text": text}
    if subtype:
        e["subtype"] = subtype
    if bot_id:
        e["bot_id"] = bot_id
    return e


def _mention(user="U1", channel="C1", ts="200.2", text="<@B1> hi"):
    return {"type": "app_mention", "user": user, "channel": channel,
            "ts": ts, "text": text}


# --- parse_event ---

def test_parse_dm():
    evt = parse_event(_im())
    assert evt == {"kind": "im", "user": "U1", "channel": "D1",
                   "ts": "100.1", "text": "hi", "thread_ts": None}


def test_parse_app_mention():
    evt = parse_event(_mention())
    assert evt["kind"] == "channel"
    assert evt["channel"] == "C1"


def test_parse_ignores_bot_message():
    assert parse_event(_im(bot_id="B9")) is None


def test_parse_ignores_message_subtype():
    assert parse_event(_im(subtype="channel_join")) is None


def test_parse_ignores_other_event_type():
    assert parse_event({"type": "reaction_added"}) is None


def test_parse_keeps_thread_ts():
    e = _im()
    e["thread_ts"] = "099.9"
    assert parse_event(e)["thread_ts"] == "099.9"


# --- accept (DMs) ---

def test_dm_accepted_when_user_allowed():
    settings = {"direct_messages": {"mode": "allowed_users"},
                "allowed_users": {"U1": "Alice"}}
    assert accept(parse_event(_im(user="U1")), settings) is True


def test_dm_refused_when_user_not_allowed():
    settings = {"direct_messages": {"mode": "allowed_users"},
                "allowed_users": {"U2": "Bob"}}
    assert accept(parse_event(_im(user="U1")), settings) is False


def test_dm_accepted_when_mode_open():
    settings = {"direct_messages": {"mode": "open"}, "allowed_users": {}}
    assert accept(parse_event(_im(user="U1")), settings) is True


# --- accept (channels) ---

def test_channel_accepted_when_allowed():
    settings = {"allowed_channels": {"C1": "#eng"},
                "default_channel_policy": "allowed_only"}
    assert accept(parse_event(_mention(channel="C1")), settings) is True


def test_channel_refused_when_not_allowed():
    settings = {"allowed_channels": {"C2": "#other"},
                "default_channel_policy": "allowed_only"}
    assert accept(parse_event(_mention(channel="C1")), settings) is False


def test_channel_accepted_when_default_open():
    settings = {"allowed_channels": {}, "default_channel_policy": "open"}
    assert accept(parse_event(_mention(channel="C1")), settings) is True


# --- route (tier) ---

def test_route_answer_for_whitelisted_dm_user():
    settings = {"auto_answer": {"users": ["U1"], "channels": []}}
    assert route(parse_event(_im(user="U1")), settings) == "answer"


def test_route_relay_for_non_whitelisted_dm_user():
    settings = {"auto_answer": {"users": ["U2"], "channels": []}}
    assert route(parse_event(_im(user="U1")), settings) == "relay"


def test_route_answer_for_whitelisted_channel():
    settings = {"auto_answer": {"users": [], "channels": ["C1"]}}
    assert route(parse_event(_mention(channel="C1")), settings) == "answer"


def test_route_relay_when_auto_answer_absent():
    assert route(parse_event(_im(user="U1")), {}) == "relay"


# --- conversation_key ---

def test_conversation_key_dm_is_channel():
    assert conversation_key(parse_event(_im(channel="D1"))) == "D1"


def test_conversation_key_channel_is_thread_root():
    e = _mention(channel="C1", ts="200.2")
    assert conversation_key(parse_event(e)) == "C1:200.2"
    e2 = _mention(channel="C1", ts="201.0")
    e2["thread_ts"] = "200.2"
    assert conversation_key(parse_event(e2)) == "C1:200.2"


# --- resolve_role ---

def test_resolve_role_from_allowed_users_dict():
    s = {"allowed_users": {"U1": {"name": "A", "role": "supervisor"}}}
    assert resolve_role(parse_event(_im(user="U1")), s) == "supervisor"


def test_resolve_role_dm_default():
    s = {"direct_messages": {"default_role": "default"}, "allowed_users": {"U1": "A"}}
    assert resolve_role(parse_event(_im(user="U1")), s) == "default"


def test_resolve_role_falls_back_to_default_literal():
    assert resolve_role(parse_event(_im(user="U9")), {}) == "default"


# --- strip_mention ---

def test_strip_mention_removes_leading_mentions():
    assert strip_mention("<@B1> stop") == "stop"
    assert strip_mention("  <@B1>  <@B2> status ") == "status"
    assert strip_mention("hello") == "hello"


# --- control_command ---

def test_control_command_detects_keywords():
    assert control_command("<@B1> stop") == "stop"
    assert control_command("status") == "status"
    assert control_command("/stop") == "stop"
    assert control_command("please stop the thing") is None
    assert control_command("what's the status of X") is None


# --- control_allowed ---

def test_control_allowed_by_role():
    s = {"control": {"roles": {"supervisor": {"commands": ["status", "stop"]}}}}
    assert control_allowed("stop", "supervisor", s) is True
    assert control_allowed("stop", "default", s) is False
    assert control_allowed("status", "supervisor", s) is True


# --- select_catchup ---

def test_select_catchup_watermark_age_count():
    msgs = [{"ts": f"{t}.0"} for t in (100, 150, 190, 195, 199)]
    out = select_catchup(msgs, watermark="150.0", now_ts="200.0",
                         max_age_seconds=20, max_messages=50)
    # > watermark 150 AND within 20s of now(200) => 190,195,199 ; sorted ascending
    assert [m["ts"] for m in out] == ["190.0", "195.0", "199.0"]


def test_select_catchup_count_cap_keeps_most_recent():
    msgs = [{"ts": f"{t}.0"} for t in range(100, 110)]
    out = select_catchup(msgs, watermark=None, now_ts="200.0",
                         max_age_seconds=None, max_messages=3)
    assert [m["ts"] for m in out] == ["107.0", "108.0", "109.0"]


# --- route guards ---

def test_route_guards_none():
    assert route(None, {}) == "relay"
