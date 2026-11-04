"""History read back by number, against a fake MTProto numbering: the chat
filter that keeps another chat's messages out, empty numbers, the request
budget, forum topics and the reply chain. No network."""

from __future__ import annotations

import asyncio
import datetime as dt

from _cli import load_service

t = load_service("transport")
BOT = 123456
TOKEN = f"{BOT}:fake"


# Stand-ins named as Telethon names its types, which is all the reader looks at.
class PeerUser:
    def __init__(self, user_id):
        self.user_id = user_id


class PeerChannel:
    def __init__(self, channel_id):
        self.channel_id = channel_id


class MessageReplyHeader:
    def __init__(self, reply_to_msg_id=None, reply_to_top_id=None, forum_topic=False):
        self.reply_to_msg_id, self.reply_to_top_id = reply_to_msg_id, reply_to_top_id
        self.forum_topic = forum_topic


class Message:
    def __init__(self, id, peer, text="", out=False, from_id=None, reply_to=None):
        self.id, self.peer_id, self.message, self.out = id, peer, text, out
        self.from_id, self.reply_to, self.media, self.entities = from_id, reply_to, None, []
        self.date = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)


class MessageActionTopicCreate:
    pass


class MessageService(Message):
    def __init__(self, id, peer):
        super().__init__(id, peer)
        self.action = MessageActionTopicCreate()


class MessageEmpty:
    def __init__(self, id):
        self.id = id


class User:
    def __init__(self, id, first_name, bot=False):
        self.id, self.first_name, self.last_name, self.username, self.bot = \
            id, first_name, None, None, bot


class Numbering:
    """The bot's numbers: whatever `messages` holds, else empty."""

    def __init__(self, messages, users=()):
        self.messages = {m.id: m for m in messages}
        self.users = {u.id: u for u in users}
        self.asked: list[list[int]] = []

    async def fetch(self, chat_id, ids):
        self.asked.append(ids)
        return [self.messages.get(i, MessageEmpty(i)) for i in ids], self.users


def reader(numbering):
    found = t.HistoryReader(TOKEN, 1, "hash", "/nonexistent")
    found.fetch = numbering.fetch
    return found


def run(coro):
    return asyncio.run(coro)


def test_a_private_read_keeps_only_this_chat_and_both_sides():
    ann, bob = 42, 43
    messages = [Message(1, PeerUser(ann), "hello"), Message(2, PeerUser(bob), "not yours"),
                Message(3, PeerUser(ann), "answer", out=True), Message(5, PeerUser(ann), "more")]
    numbering = Numbering(messages, [User(ann, "Ann")])
    reading = run(reader(numbering).read_back(ann, 6, limit=40))
    assert [m.message_id for m in reading.messages] == [1, 3, 5]
    assert [m.own for m in reading.messages] == [False, True, False]
    assert reading.messages[0].sender_name == "Ann" and reading.messages[1].sender_id == BOT
    assert reading.requests == 1 and reading.as_dict()["complete"] is False
    assert reading.line == "history: read 3 of 40, nothing older found"


def test_the_budget_stops_a_read_and_says_so():
    ann = 42
    messages = [Message(i, PeerUser(ann), f"m{i}") for i in range(1, 1001, 100)]
    numbering = Numbering(messages)
    reading = run(reader(numbering).read_back(ann, 1001, limit=40, max_requests=3))
    assert reading.requests == 3 and len(numbering.asked) == 3
    assert [m.message_id for m in reading.messages] == [701, 801, 901]
    assert "budget of 3 requests" in reading.line


def test_a_full_tail_stops_at_the_limit():
    ann = 42
    messages = [Message(i, PeerUser(ann), f"m{i}") for i in range(1, 300)]
    reading = run(reader(Numbering(messages)).read_back(ann, 300, limit=40))
    assert [m.message_id for m in reading.messages] == list(range(260, 300))
    assert reading.line == "history: read 40 of 40" and reading.requests == 1


def test_a_forum_read_keeps_one_topic():
    chat = -1000000000077
    channel = PeerChannel(77)
    messages = [MessageService(5, channel),
                Message(6, channel, "in topic", reply_to=MessageReplyHeader(5, None, True)),
                Message(7, channel, "general"),
                Message(8, channel, "reply in topic", reply_to=MessageReplyHeader(6, 5, True))]
    reading = run(reader(Numbering(messages)).read_back(chat, 9, limit=10, topic_id=5,
                                                        is_forum=True))
    assert [m.message_id for m in reading.messages] == [5, 6, 8]
    assert reading.messages[1].reply_to is None and reading.messages[2].reply_to == 6
    general = run(reader(Numbering(messages)).read_back(chat, 9, limit=10,
                                                        topic_id=t.GENERAL_TOPIC, is_forum=True))
    assert [m.message_id for m in general.messages] == [7]


def test_the_reply_chain_climbs_past_the_tail(monkeypatch):
    ann = 42
    messages = [Message(1, PeerUser(ann), "the original"),
                Message(150, PeerUser(ann), "answer", reply_to=MessageReplyHeader(1)),
                Message(299, PeerUser(ann), "now", reply_to=MessageReplyHeader(150))]
    numbering = Numbering(messages)

    class Reader(t.HistoryReader):
        async def open(self):
            self.fetch = numbering.fetch
            return self

        async def close(self):
            pass

    monkeypatch.setattr(t, "HistoryReader", Reader)
    reading = run(t.read_history(TOKEN, 1, "h", "/x", ann, 300, limit=1,
                                 reply_chain_of=150, include_anchor=True))
    assert [m.message_id for m in reading.messages] == [1, 150, 299]
