import daemon
import pytest


def test_map_tail_orders_and_labels():
    msgs = [
        {"user": "U1", "text": "second", "ts": "2.0"},
        {"user": "U1", "text": "first", "ts": "1.0"},
    ]  # history is newest-first
    out = daemon.map_tail(msgs, bot_user_id="B1")
    assert out == [
        {"sender": "U1", "text": "first"},
        {"sender": "U1", "text": "second"},
    ]


def test_map_tail_labels_bot_and_skips_empty():
    msgs = [
        {"bot_id": "B1", "text": "hi from bot", "ts": "3.0"},
        {"user": "U1", "text": "", "ts": "2.0"},
        {"user": "U1", "text": "hey", "ts": "1.0"},
    ]
    out = daemon.map_tail(msgs, bot_user_id="B1")
    assert {"sender": "assistant", "text": "hi from bot"} in out
    assert all(m["text"] for m in out)


def test_read_outbox_incremental(tmp_path):
    p = tmp_path / "o.jsonl"
    p.write_text('{"text": "one"}\n{"text": "two"}\n')
    lines, off = daemon.read_outbox(p, 0)
    assert lines == ["one", "two"]
    p.write_text('{"text": "one"}\n{"text": "two"}\n{"text": "three"}\n')
    lines2, _off2 = daemon.read_outbox(p, off)
    assert lines2 == ["three"]


def test_synth_payload_dm():
    m = {"user": "U1", "text": "hi", "ts": "1.0"}
    p = daemon.synth_payload("D1", m, bot_user_id="B1")
    assert p["channel_type"] == "im" and p["type"] == "message" and p["channel"] == "D1"


def test_synth_payload_channel_requires_mention():
    assert (
        daemon.synth_payload(
            "C1", {"user": "U1", "text": "hello", "ts": "1.0"}, bot_user_id="B1"
        )
        is None
    )
    p = daemon.synth_payload(
        "C1", {"user": "U1", "text": "<@B1> hello", "ts": "1.0"}, bot_user_id="B1"
    )
    assert p["type"] == "app_mention"


def test_synth_payload_skips_bot_and_subtype():
    assert (
        daemon.synth_payload("D1", {"bot_id": "B", "text": "x", "ts": "1.0"}, "B")
        is None
    )
    assert (
        daemon.synth_payload(
            "D1",
            {"subtype": "channel_join", "text": "x", "ts": "1.0", "user": "U1"},
            "B",
        )
        is None
    )


def test_worker_env_sets_shim_and_preserves_trusted_worker_environment():
    env = daemon.worker_env(
        {
            "PATH": "/usr/bin",
            "SLACK_BOT_TOKEN": "bot-secret",
            "SLACK_APP_TOKEN": "app-secret",
            "OPENAI_API_KEY": "provider-secret",
            "DATABASE_URL": "project-secret",
        },
        outbox="/o",
        conversation="D1",
        authority_path="/a.json",
        worker_bin="/wb",
    )
    assert env["SLACK_WORKER_OUTBOX"] == "/o"
    assert env["SLACK_WORKER_CONVERSATION"] == "D1"
    assert env["CAPABILITIES_AUTH_CONTEXT"] == "/a.json"
    assert env["PATH"].startswith("/wb")
    assert "SLACK_BOT_TOKEN" not in env
    assert "SLACK_APP_TOKEN" not in env
    assert env["OPENAI_API_KEY"] == "provider-secret"
    assert env["DATABASE_URL"] == "project-secret"


def _defaults():
    return {
        "worker": "stub",
        "tail_size": 40,
        "worker_timeout": 120,
        "workers": {
            "claude": {"model": None, "effort": None},
            "codex": {
                "model": None,
                "reasoning_effort": None,
                "service_tier": None,
            },
            "stub": {"model": None},
        },
    }


def test_conversation_settings_overlay_worker_profile():
    row = {
        "settings": {"worker": "codex", "tail_size": 80},
        "workers": {
            "codex": {
                "model": "gpt-5.6",
                "reasoning_effort": "high",
                "service_tier": "priority",
            }
        },
    }
    resolved = daemon.conversation_settings(_defaults(), row)
    assert resolved["worker"] == "codex"
    assert resolved["tail_size"] == 80
    assert resolved["model"] == "gpt-5.6"
    assert resolved["reasoning_effort"] == "high"
    assert resolved["service_tier"] == "priority"


def test_set_conversation_setting_supports_telegram_style_role_controls():
    row, message = daemon.set_conversation_setting({}, _defaults(), "worker", "codex")
    assert message == "worker = codex"
    row, message = daemon.set_conversation_setting(
        row, _defaults(), "reasoning", "high"
    )
    assert message == "codex.reasoning = high"
    row, message = daemon.set_conversation_setting(row, _defaults(), "speed", "fast")
    assert message == "codex.service_tier = priority"
    resolved = daemon.conversation_settings(_defaults(), row)
    assert resolved["reasoning_effort"] == "high"
    assert resolved["service_tier"] == "priority"


def test_set_conversation_setting_rejects_invalid_values():
    with pytest.raises(ValueError, match="tail"):
        daemon.set_conversation_setting({}, _defaults(), "tail", "0")
    with pytest.raises(ValueError, match="worker"):
        daemon.set_conversation_setting({}, _defaults(), "worker", "unknown")
