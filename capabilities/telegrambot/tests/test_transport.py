"""The HTTP Bot API layer and the one message shape. No network: every request
goes to a fake Bot API through httpx's mock transport."""

from __future__ import annotations

import json

import httpx
import pytest

from _cli import load_service

t = load_service("transport")
TOKEN = "123456:fake-token-for-tests"


def bot(handler, sleeps=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return t.BotAPI(TOKEN, base="https://bot.test", client=client,
                    sleep=(sleeps.append if sleeps is not None else lambda _s: None))


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def test_chat_ids_name_their_kind():
    assert t.chat_kind(42) == "private"
    assert t.chat_kind(-4242) == "group"
    assert t.chat_kind(-1001234567890) == "supergroup"
    assert t.channel_of(-1001234567890) == 1234567890
    assert t.bot_id_of(TOKEN) == 123456 and t.bot_id_of("nope") is None
    with pytest.raises(t.TelegramError) as caught:
        t.parse_chat_id("@somebody")
    assert caught.value.exit_code == 6


def test_a_private_message_takes_the_one_shape():
    msg = t.from_update({"message_id": 7, "date": 1, "text": "hi @Helper_Bot",
                         "from": {"id": 42, "is_bot": False, "first_name": "Ann",
                                  "last_name": "Lee", "username": "ann"},
                         "chat": {"id": 42, "type": "private", "first_name": "Ann"},
                         "entities": [{"type": "mention", "offset": 3, "length": 11}]},
                        own_id=123456)
    assert (msg.chat_id, msg.message_id, msg.sender_id, msg.sender_name) == (42, 7, 42, "Ann Lee")
    assert msg.topic_id is None and not msg.own and not msg.is_bot
    assert msg.mentions == ["helper_bot"]


def test_a_forum_post_names_its_topic_and_not_a_reply():
    # A post straight into topic 5 arrives as a reply to the topic's root.
    msg = t.from_update({"message_id": 9, "message_thread_id": 5, "is_topic_message": True,
                         "text": "x", "from": {"id": 1}, "reply_to_message": {"message_id": 5},
                         "chat": {"id": -1009, "type": "supergroup", "is_forum": True}})
    assert msg.topic_id == 5 and msg.reply_to is None
    general = t.from_update({"message_id": 10, "text": "y", "from": {"id": 1},
                             "chat": {"id": -1009, "type": "supergroup", "is_forum": True}})
    assert general.topic_id == t.GENERAL_TOPIC
    plain = t.from_update({"message_id": 11, "text": "z", "from": {"id": 1},
                           "reply_to_message": {"message_id": 3},
                           "chat": {"id": -1009, "type": "supergroup"}})
    assert plain.topic_id is None and plain.reply_to == 3


def test_a_voice_message_is_an_attachment_and_a_bot_says_so():
    msg = t.from_update({"message_id": 3, "from": {"id": 9, "is_bot": True},
                         "chat": {"id": 9, "type": "private"},
                         "voice": {"file_id": "F", "duration": 4, "mime_type": "audio/ogg"}})
    assert msg.attachment["kind"] == "voice" and msg.attachment["file_id"] == "F"
    assert msg.is_bot and not msg.service


def test_a_long_reply_is_cut_at_a_paragraph():
    text = ("a" * 3000) + "\n\n" + ("b" * 3000)
    parts = t.split_text(text, 4096)
    assert parts == ["a" * 3000, "b" * 3000]
    assert all(len(p) <= 4096 for p in t.split_text("x " * 5000, 4096))


def test_rate_limit_waits_as_telegram_asks():
    calls, sleeps = [], []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                             "parameters": {"retry_after": 7}})
        return ok({"id": 123456, "username": "helper_bot"})

    assert bot(handler, sleeps).get_me()["username"] == "helper_bot"
    assert sleeps == [7.0] and calls == ["/bot123456:fake-token-for-tests/getMe"] * 2


def test_a_send_that_timed_out_is_not_sent_again():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(t.TelegramError) as caught:
        bot(handler).send_text(42, "hello")
    assert caught.value.code == "delivery_unknown" and caught.value.exit_code == 5
    assert len(calls) == 1


def test_refusals_carry_their_category():
    def handler(request):
        method = request.url.path.rsplit("/", 1)[1]
        if method == "getMe":
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        if method == "getUpdates":
            return httpx.Response(409, json={"ok": False, "description":
                                             "Conflict: terminated by other getUpdates request"})
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    api = bot(handler)
    with pytest.raises(t.TelegramError) as caught:
        api.get_me()
    assert (caught.value.exit_code, caught.value.code) == (2, "token_rejected")
    with pytest.raises(t.TelegramError) as caught:
        api.updates(None, timeout=0)
    assert (caught.value.exit_code, caught.value.code) == (5, "poll_conflict")
    with pytest.raises(t.TelegramError) as caught:
        api.get_chat(-5)
    assert (caught.value.exit_code, caught.value.code) == (3, "chat_not_found")


def test_a_reply_goes_to_its_topic_and_only_the_first_part_answers():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return ok({"message_id": 100 + len(sent)})

    ids = bot(handler).send_text(-1009, "a" * 5000, topic_id=5, reply_to=9)
    assert ids == [101, 102]
    assert sent[0]["message_thread_id"] == 5 and sent[0]["reply_parameters"]["message_id"] == 9
    assert "reply_parameters" not in sent[1]
    sent.clear()
    bot(handler).send_text(-1009, "general", topic_id=t.GENERAL_TOPIC)
    assert "message_thread_id" not in sent[0]


def test_a_reaction_is_one_emoji_or_none():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return ok(True)

    api = bot(handler)
    api.react(42, 7, "👍")
    api.react(42, 7, None)
    assert sent[0]["reaction"] == [{"type": "emoji", "emoji": "👍"}] and sent[1]["reaction"] == []


def test_deepgram_check_reads_the_key():
    def handler(request):
        good = request.headers["authorization"] == "Token good"
        return httpx.Response(200 if good else 401, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert t.deepgram_check("good", client) == {"ok": True}
    assert t.deepgram_check("bad", client)["ok"] is False
