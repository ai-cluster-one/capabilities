import daemon


def test_map_tail_orders_and_labels():
    msgs = [{"user": "U1", "text": "second", "ts": "2.0"},
            {"user": "U1", "text": "first", "ts": "1.0"}]  # history is newest-first
    out = daemon.map_tail(msgs, bot_user_id="B1")
    assert out == [{"sender": "U1", "text": "first"},
                   {"sender": "U1", "text": "second"}]


def test_map_tail_labels_bot_and_skips_empty():
    msgs = [{"bot_id": "B1", "text": "hi from bot", "ts": "3.0"},
            {"user": "U1", "text": "", "ts": "2.0"},
            {"user": "U1", "text": "hey", "ts": "1.0"}]
    out = daemon.map_tail(msgs, bot_user_id="B1")
    assert {"sender": "assistant", "text": "hi from bot"} in out
    assert all(m["text"] for m in out)


def test_read_outbox_incremental(tmp_path):
    p = tmp_path / "o.jsonl"
    p.write_text('{"text": "one"}\n{"text": "two"}\n')
    lines, off = daemon.read_outbox(p, 0)
    assert lines == ["one", "two"]
    p.write_text('{"text": "one"}\n{"text": "two"}\n{"text": "three"}\n')
    lines2, off2 = daemon.read_outbox(p, off)
    assert lines2 == ["three"]


def test_synth_payload_dm():
    m = {"user": "U1", "text": "hi", "ts": "1.0"}
    p = daemon.synth_payload("D1", m, bot_user_id="B1")
    assert p["channel_type"] == "im" and p["type"] == "message" and p["channel"] == "D1"


def test_synth_payload_channel_requires_mention():
    assert daemon.synth_payload("C1", {"user": "U1", "text": "hello", "ts": "1.0"},
                                bot_user_id="B1") is None
    p = daemon.synth_payload("C1", {"user": "U1", "text": "<@B1> hello", "ts": "1.0"},
                             bot_user_id="B1")
    assert p["type"] == "app_mention"


def test_synth_payload_skips_bot_and_subtype():
    assert daemon.synth_payload("D1", {"bot_id": "B", "text": "x", "ts": "1.0"}, "B") is None
    assert daemon.synth_payload("D1", {"subtype": "channel_join", "text": "x",
                                       "ts": "1.0", "user": "U1"}, "B") is None


def test_worker_env_sets_shim_and_paths(tmp_path):
    env = daemon.worker_env({"PATH": "/usr/bin"}, outbox="/o", conversation="D1",
                            authority_path="/a.json", real_slack="/real/slack",
                            worker_bin="/wb")
    assert env["SLACK_WORKER_OUTBOX"] == "/o"
    assert env["SLACK_WORKER_CONVERSATION"] == "D1"
    assert env["SLACK_REAL_SLACK"] == "/real/slack"
    assert env["CAPABILITIES_AUTH_CONTEXT"] == "/a.json"
    assert env["PATH"].startswith("/wb")
