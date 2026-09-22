#!/usr/bin/env python3
"""Focused coverage for outgoing Telegram media and reactions."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from importlib.machinery import SourceFileLoader
from pathlib import Path


TELEGRAM_DIR = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import CLI_PATH  # noqa: E402

WORKER_SHIM_PATH = TELEGRAM_DIR / "service" / "worker-bin" / "telegram"


class _Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _ReactionEmoji:
    def __init__(self, *, emoticon):
        self.emoticon = emoticon


class _Error(Exception):
    pass


def import_cli():
    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = object

    errors = types.ModuleType("telethon.errors")
    errors.FloodWaitError = _Error
    errors.RPCError = _Error
    errors.SessionPasswordNeededError = _Error

    rpc_errors = types.ModuleType("telethon.errors.rpcerrorlist")
    rpc_errors.ApiIdInvalidError = _Error
    rpc_errors.AuthKeyUnregisteredError = _Error
    rpc_errors.UsernameInvalidError = _Error
    rpc_errors.UsernameNotOccupiedError = _Error

    tl = types.ModuleType("telethon.tl")
    functions = types.ModuleType("telethon.tl.functions")
    functions.messages = types.SimpleNamespace(
        SendReactionRequest=_Request, GetForumTopicsRequest=_Request)
    tl_types = types.ModuleType("telethon.tl.types")
    for name in ("Channel", "Chat", "MessageEmpty", "User"):
        setattr(tl_types, name, type(name, (), {}))
    tl_types.ReactionEmoji = _ReactionEmoji
    tl.functions = functions
    tl.types = tl_types

    modules = {
        "telethon": telethon,
        "telethon.errors": errors,
        "telethon.errors.rpcerrorlist": rpc_errors,
        "telethon.tl": tl,
        "telethon.tl.functions": functions,
        "telethon.tl.types": tl_types,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        name = f"telegram_outbound_test_{time.time_ns()}"
        spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, str(CLI_PATH)))
        if spec is None or spec.loader is None:
            raise AssertionError("cannot import telegram CLI")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def import_worker_shim():
    name = f"telegram_worker_shim_test_{time.time_ns()}"
    spec = importlib.util.spec_from_loader(
        name, SourceFileLoader(name, str(WORKER_SHIM_PATH)))
    if spec is None or spec.loader is None:
        raise AssertionError("cannot import Telegram worker shim")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Client:
    def __init__(self):
        self.files = []
        self.messages = []
        self.requests = []
        self.disconnected = False

    async def send_message(self, entity, text, **kwargs):
        self.messages.append((entity, text, kwargs))
        return types.SimpleNamespace(id=713)

    async def send_file(self, entity, path, **kwargs):
        self.files.append((entity, path, kwargs))
        return types.SimpleNamespace(id=812)

    async def __call__(self, request):
        self.requests.append(request)

    async def disconnect(self):
        self.disconnected = True


class _ForumClient(_Client):
    """Answers GetForumTopicsRequest with queued pages, newest topic first."""

    def __init__(self, pages):
        super().__init__()
        self.pages = list(pages)

    async def __call__(self, request):
        self.requests.append(request)
        return self.pages.pop(0) if self.pages else types.SimpleNamespace(
            topics=[], messages=[])


def _topic(topic_id, title, top_message, **flags):
    return types.SimpleNamespace(
        id=topic_id, title=title, top_message=top_message,
        unread_count=flags.pop("unread_count", 0), **flags)


class _HistoryClient(_Client):
    """Answers iter_messages with one history and records how it was asked.

    A limit is spent the way Telegram spends one - nothing at all comes back
    for a limit of zero - so an unscoped answer can be held against a scoped
    one at the same limit.
    """

    def __init__(self, history):
        super().__init__()
        self.history = list(history)
        self.asked = []

    async def iter_messages(self, _entity, **kwargs):
        self.asked.append(kwargs)
        limit = kwargs.get("limit")
        for spent, message in enumerate(self.history):
            if limit is not None and spent >= limit:
                return
            yield message

    async def get_me(self):
        return types.SimpleNamespace(
            id=4242424242, first_name="Agent", last_name="", username="agent")


def _reply_header(*, forum_topic=False, top_id=None, msg_id=None):
    """The MessageReplyHeader Telegram attaches, where threading actually lives.

    `forum_topic` and `reply_to_top_id` sit on the header and on nothing else;
    a Telethon Message carries neither, which is why a caller reading
    `reply_to` alone cannot tell a threaded reply inside a topic from a reply
    in a plain group.
    """
    return types.SimpleNamespace(
        forum_topic=forum_topic, reply_to_top_id=top_id, reply_to_msg_id=msg_id)


def _forum_message(message_id, *, text="", reply_to=None, forum_topic=False):
    return types.SimpleNamespace(
        id=message_id, date=None, edit_date=None, sender=None, sender_id=77,
        message=text, forum_topic=forum_topic, reply_to=reply_to,
        reply_to_msg_id=getattr(reply_to, "reply_to_msg_id", None),
        forward=None, action=None, voice=False, audio=False, video_note=False,
        video=False, sticker=False, photo=False, document=False,
        web_preview=False, poll=None, contact=False, geo=False, media=None,
        download_media=None,
    )


class _DialogClient(_Client):
    """Answers iter_dialogs with one fixed listing, in the order given."""

    def __init__(self, dialogs):
        super().__init__()
        self.dialogs = list(dialogs)
        self.limits = []

    async def iter_dialogs(self, limit=None):
        self.limits.append(limit)
        for dialog in self.dialogs:
            yield dialog


def _entity(class_name, **flags):
    """A dialog entity of the class Telegram would file it under."""
    entity = type(class_name, (), {})()
    for key, value in flags.items():
        setattr(entity, key, value)
    return entity


def _dialog(dialog_id, name, entity, unread=0):
    return types.SimpleNamespace(
        id=dialog_id, name=name, entity=entity, unread_count=unread)


class _ExportClient(_Client):
    def __init__(self, messages):
        super().__init__()
        self.messages = messages

    async def get_me(self):
        return types.SimpleNamespace(
            id=8200881535, first_name="Marvin", last_name="", username="marvin")

    async def iter_messages(self, _entity, **_kwargs):
        for message in self.messages:
            yield message


class OutboundActionsTests(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        self.client = _Client()

        async def authorize(_client):
            return None

        async def resolve(_client, _chat):
            return "chat-entity"

        self.cli.make_client = lambda _cfg: self.client
        self.cli._require_auth = authorize
        self.cli.resolve_chat = resolve
        self.cli.entity_label = lambda entity: str(entity)

    def test_send_media_keeps_caption_reply_and_document_choice(self):
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "report.pdf"
            media.write_bytes(b"pdf")
            result = asyncio.run(self.cli.cmd_send_media(
                {"id": "test"}, "-1001", str(media), "Here", 71, True))

        self.assertEqual(result["sent_id"], 812)
        self.assertEqual(result["to"], "chat-entity")
        entity, path, kwargs = self.client.files[0]
        self.assertEqual(entity, "chat-entity")
        self.assertEqual(path, str(media))
        self.assertEqual(kwargs, {
            "caption": "Here", "reply_to": 71, "force_document": True,
        })
        self.assertTrue(self.client.disconnected)

    def test_react_builds_one_reaction_for_each_requested_emoji(self):
        result = asyncio.run(self.cli.cmd_react(
            {"id": "test"}, "-1001", 99, ["👍", "🔥"]))

        self.assertEqual(result, {
            "reacted_to": 99,
            "to": "chat-entity",
            "reactions": ["👍", "🔥"],
        })
        request = self.client.requests[0]
        self.assertEqual(request.peer, "chat-entity")
        self.assertEqual(request.msg_id, 99)
        self.assertEqual([item.emoticon for item in request.reaction], ["👍", "🔥"])
        self.assertTrue(self.client.disconnected)

    def test_session_snapshot_is_an_independent_sqlite_copy(self):
        """A model-host CLI call must never share the daemon's live session DB."""
        with tempfile.TemporaryDirectory() as td:
            session = Path(td) / "live"
            source = session.with_suffix(".session")
            with sqlite3.connect(source) as db:
                db.execute("create table state (value text)")
                db.execute("insert into state values ('daemon')")

            isolated, temporary = self.cli._session_snapshot({"session": str(session)})
            try:
                clone = Path(isolated["session"]).with_suffix(".session")
                self.assertNotEqual(clone, source)
                with sqlite3.connect(clone) as db:
                    self.assertEqual(db.execute("select value from state").fetchone(), ("daemon",))
                    db.execute("insert into state values ('worker')")

                with sqlite3.connect(source) as db:
                    self.assertEqual(db.execute("select value from state").fetchall(), [("daemon",)])
            finally:
                temporary.cleanup()

    def test_send_media_rejects_missing_file_before_connecting(self):
        with self.assertRaisesRegex(ValueError, "media file not found"):
            asyncio.run(self.cli.cmd_send_media(
                {"id": "test"}, "-1001", "/does/not/exist", None, None, False))
        self.assertEqual(self.client.files, [])

    def test_send_without_a_reply_target_stays_unaddressed(self):
        result = asyncio.run(self.cli.cmd_send({"id": "test"}, "-1001", "hello"))

        self.assertEqual(result, {
            "sent_id": 713, "to": "chat-entity", "reply_to": None,
        })
        _entity, text, kwargs = self.client.messages[0]
        self.assertEqual(text, "hello")
        self.assertEqual(kwargs, {"reply_to": None})
        self.assertTrue(self.client.disconnected)

    def test_send_addresses_a_forum_topic_through_its_root_id(self):
        result = asyncio.run(self.cli.cmd_send(
            {"id": "test"}, "-1001", "hello", None, 7151))

        self.assertEqual(result["reply_to"], 7151)
        self.assertEqual(self.client.messages[0][2], {"reply_to": 7151})

    def test_send_reply_selects_the_replied_message_topic(self):
        result = asyncio.run(self.cli.cmd_send(
            {"id": "test"}, "-1001", "hello", 7597))

        self.assertEqual(result["reply_to"], 7597)
        self.assertEqual(self.client.messages[0][2], {"reply_to": 7597})

    def test_send_refuses_two_conflicting_reply_targets(self):
        with self.assertRaises(SystemExit) as stopped:
            asyncio.run(self.cli.cmd_send(
                {"id": "test"}, "-1001", "hello", 7597, 7151))

        self.assertEqual(stopped.exception.code, 6)
        self.assertEqual(self.client.messages, [])

    def test_send_refuses_a_non_positive_message_id(self):
        for reply_to, topic in ((0, None), (-4, None), (None, 0), (None, -4)):
            with self.assertRaises(SystemExit) as stopped:
                asyncio.run(self.cli.cmd_send(
                    {"id": "test"}, "-1001", "hello", reply_to, topic))
            self.assertEqual(stopped.exception.code, 6)
        self.assertEqual(self.client.messages, [])

    def test_send_media_addresses_a_forum_topic_through_its_root_id(self):
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "report.pdf"
            media.write_bytes(b"pdf")
            result = asyncio.run(self.cli.cmd_send_media(
                {"id": "test"}, "-1001", str(media), None, None, False, 7151))

        self.assertEqual(result["reply_to"], 7151)
        self.assertEqual(self.client.files[0][2]["reply_to"], 7151)

    def _use_forum(self, pages):
        self.client = _ForumClient(pages)
        self.cli.make_client = lambda _cfg: self.client

        async def resolve(_client, _chat):
            return types.SimpleNamespace(forum=True, title="Example Forum")

        self.cli.resolve_chat = resolve

    def test_topics_report_the_root_id_the_send_verbs_take(self):
        page = types.SimpleNamespace(
            topics=[_topic(7151, "Tech and setup", 7614, pinned=True),
                    _topic(1, "General", 12)],
            messages=[types.SimpleNamespace(id=7614, date="d")])
        self._use_forum([page])

        result = asyncio.run(self.cli.cmd_topics({"id": "test"}, "-1001", 100, None))

        self.assertEqual([(t["id"], t["title"]) for t in result],
                         [(7151, "Tech and setup"), (1, "General")])
        self.assertTrue(result[0]["pinned"])
        self.assertEqual(result[0]["top_message"], 7614)
        self.assertEqual(self.client.requests[0].q, None)
        self.assertTrue(self.client.disconnected)

    def test_topics_skip_a_deleted_slot_without_a_title(self):
        page = types.SimpleNamespace(
            topics=[_topic(7151, "Tech and setup", 7614),
                    types.SimpleNamespace(id=42, top_message=None)],
            messages=[])
        self._use_forum([page])

        result = asyncio.run(self.cli.cmd_topics({"id": "test"}, "-1001", 100, None))

        self.assertEqual([t["id"] for t in result], [7151])

    def test_topics_page_forward_until_the_limit_is_met(self):
        pages = [
            types.SimpleNamespace(
                topics=[_topic(30, "third", 300)],
                messages=[types.SimpleNamespace(id=300, date="d300")]),
            types.SimpleNamespace(
                topics=[_topic(20, "second", 200)],
                messages=[types.SimpleNamespace(id=200, date="d200")]),
            types.SimpleNamespace(topics=[], messages=[]),
        ]
        self._use_forum(pages)

        result = asyncio.run(self.cli.cmd_topics({"id": "test"}, "-1001", 5, None))

        self.assertEqual([t["title"] for t in result], ["third", "second"])
        second_request = self.client.requests[1]
        self.assertEqual(second_request.offset_topic, 30)
        self.assertEqual(second_request.offset_id, 300)
        self.assertEqual(second_request.offset_date, "d300")

    def test_topics_refuse_a_chat_that_has_no_forum(self):
        self.client = _ForumClient([])
        self.cli.make_client = lambda _cfg: self.client

        async def resolve(_client, _chat):
            return types.SimpleNamespace(forum=False, title="Example Group")

        self.cli.resolve_chat = resolve

        with self.assertRaises(SystemExit) as stopped:
            asyncio.run(self.cli.cmd_topics({"id": "test"}, "-1002", 100, None))

        self.assertEqual(stopped.exception.code, 3)
        self.assertEqual(self.client.requests, [])
        self.assertTrue(self.client.disconnected)

    def _use_history(self, history, *, forum=True, title="Example Forum"):
        self.client = _HistoryClient(history)
        self.cli.make_client = lambda _cfg: self.client

        async def resolve(_client, _chat):
            return types.SimpleNamespace(forum=forum, title=title)

        self.cli.resolve_chat = resolve

    # A forum whose messages carry each shape the question has an answer for:
    # posted straight into a topic, threaded inside it, in General, and in
    # another topic entirely.
    DIRECT = _forum_message(
        7300, text="posted straight into the topic", forum_topic=True,
        reply_to=_reply_header(forum_topic=True, msg_id=7151))
    THREADED = _forum_message(
        7301, text="replying inside the topic", forum_topic=True,
        reply_to=_reply_header(forum_topic=True, top_id=7151, msg_id=7300))
    GENERAL = _forum_message(12, text="in General")
    OTHER = _forum_message(
        9100, text="another topic entirely", forum_topic=True,
        reply_to=_reply_header(forum_topic=True, msg_id=9000))

    def test_read_reports_which_topic_each_message_is_in(self):
        """`reply_to` answers three different things for one question, which is
        why `topic_id` answers it instead."""
        self._use_history([self.OTHER, self.GENERAL, self.THREADED, self.DIRECT])

        result = asyncio.run(self.cli.cmd_read(
            {"id": "test"}, "-1001", 50, None, True))

        self.assertEqual([(row["id"], row["reply_to"], row["topic_id"])
                          for row in result],
                         [(7300, 7151, 7151), (7301, 7300, 7151),
                          (12, None, 1), (9100, 9000, 9000)])

    def test_read_of_a_chat_without_topics_reports_no_topic_at_all(self):
        """Null against General's 1 is what separates a forum's General from a
        group that never had topics."""
        self._use_history([_forum_message(5, text="plain group talk")],
                          forum=False, title="Example Group")

        result = asyncio.run(self.cli.cmd_read(
            {"id": "test"}, "-1002", 50, None, True))

        self.assertEqual([row["topic_id"] for row in result], [None])

    def test_read_scoped_to_a_topic_asks_for_that_thread_and_keeps_both_shapes(self):
        self._use_history([self.OTHER, self.GENERAL, self.THREADED, self.DIRECT])

        result = asyncio.run(self.cli.cmd_read(
            {"id": "test"}, "-1001", 50, None, True, 7151))

        self.assertEqual(self.client.asked,
                         [{"limit": None, "search": None, "reply_to": 7151}])
        self.assertEqual([row["id"] for row in result], [7300, 7301])

    def test_read_scoped_to_general_walks_the_chat_because_general_has_no_root(self):
        self._use_history([self.OTHER, self.GENERAL, self.THREADED, self.DIRECT])

        result = asyncio.run(self.cli.cmd_read(
            {"id": "test"}, "-1001", 50, None, True, 1))

        self.assertEqual(self.client.asked, [{"limit": None, "search": None}])
        self.assertEqual([row["id"] for row in result], [12])

    def test_a_scoped_limit_counts_the_messages_handed_back(self):
        # The topic holds two of these four messages, so a limit of one proves the
        # count is of what came back rather than of what was looked at: an unwanted
        # message never consumes the budget.
        self._use_history([self.OTHER, self.GENERAL, self.THREADED, self.DIRECT])

        result = asyncio.run(self.cli.cmd_read(
            {"id": "test"}, "-1001", 1, None, True, 7151))

        self.assertEqual([row["id"] for row in result], [7301])

    def test_a_limit_of_nothing_hands_back_nothing_scoped_as_unscoped(self):
        """A budget of nothing is spent before the first message, so a limit
        means the same on a read and on a search whether or not a topic
        narrows the walk - including the one limit that asks for no message at
        all."""
        history = [self.OTHER, self.GENERAL, self.THREADED, self.DIRECT]
        for topic in (None, 7151, 1):
            self._use_history(history)
            read = asyncio.run(self.cli.cmd_read(
                {"id": "test"}, "-1001", 0, None, True, topic))
            self._use_history(history)
            found = asyncio.run(self.cli.cmd_search(
                {"id": "test"}, "-1001", "topic", 0, True, topic))

            self.assertEqual((topic, read, found), (topic, [], []))

    def test_an_export_limit_of_nothing_stays_the_whole_history(self):
        """A number is no limit at all on this verb, which is why a scoped
        export of zero hands back the topic rather than nothing: the meaning of
        the number is the verb's and scoping does not move it."""
        history = [self.OTHER, self.GENERAL, self.THREADED, self.DIRECT]
        counts = []
        for topic in (None, 7151):
            self._use_history(history)
            with tempfile.TemporaryDirectory() as td:
                counts.append(asyncio.run(self.cli.cmd_export(
                    {"id": "test"}, "-1001", str(Path(td) / "export.json"),
                    None, 0, None, False, False, False, False,
                    topic))["message_count"])
                self.assertEqual(self.client.asked[0]["limit"], None)

        self.assertEqual(counts, [4, 2])

    def test_an_unscoped_read_asks_the_chat_exactly_as_before(self):
        self._use_history([self.GENERAL])

        asyncio.run(self.cli.cmd_read({"id": "test"}, "-1001", 50, None, True))

        self.assertEqual(self.client.asked, [{"limit": 50, "search": None}])

    def test_a_scoped_search_keeps_the_query_and_resolves_the_topic_itself(self):
        """Telegram drops a query the moment a reply target is set, so a scoped
        search asks its own question and settles the topic on what comes back."""
        self._use_history([self.OTHER, self.GENERAL, self.THREADED, self.DIRECT])

        result = asyncio.run(self.cli.cmd_search(
            {"id": "test"}, "-1001", "topic", 50, True, 7151))

        self.assertEqual(self.client.asked,
                         [{"limit": None, "search": "topic"}])
        self.assertEqual([row["id"] for row in result], [7300, 7301])

    def test_an_unscoped_search_asks_the_chat_exactly_as_before(self):
        self._use_history([self.GENERAL])

        asyncio.run(self.cli.cmd_search(
            {"id": "test"}, "-1001", "talk", 50, True))

        self.assertEqual(self.client.asked, [{"limit": 50, "search": "talk"}])

    def test_reading_a_topic_of_a_chat_that_has_no_forum_is_refused(self):
        self._use_history([], forum=False, title="Example Group")

        with self.assertRaises(SystemExit) as stopped:
            asyncio.run(self.cli.cmd_read(
                {"id": "test"}, "-1002", 50, None, True, 7151))

        self.assertEqual(stopped.exception.code, 3)
        self.assertEqual(self.client.asked, [])
        self.assertTrue(self.client.disconnected)

    def test_a_topic_is_refused_before_connecting_unless_it_is_a_message_id(self):
        for topic in (0, -4):
            self._use_history([self.GENERAL])
            with self.assertRaises(SystemExit) as stopped:
                asyncio.run(self.cli.cmd_read(
                    {"id": "test"}, "-1001", 50, None, True, topic))
            self.assertEqual(stopped.exception.code, 6)
            self.assertEqual(self.client.asked, [])

    def test_export_records_the_topic_of_every_message_and_scopes_to_one(self):
        self._use_history([self.OTHER, self.GENERAL, self.THREADED, self.DIRECT])
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "export.json"
            unscoped = asyncio.run(self.cli.cmd_export(
                {"id": "test"}, "-1001", str(output), None, None, None,
                False, False, False, False))
            payload = json.loads(output.read_text())

        self.assertEqual(unscoped["message_count"], 4)
        self.assertEqual({row["id"]: row["topic_id"] for row in payload["messages"]},
                         {7300: 7151, 7301: 7151, 12: 1, 9100: 9000})
        self.assertEqual(self.client.asked, [{"limit": None, "search": None}])

        self._use_history([self.OTHER, self.GENERAL, self.THREADED, self.DIRECT])
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "topic.json"
            scoped = asyncio.run(self.cli.cmd_export(
                {"id": "test"}, "-1001", str(output), None, None, None,
                False, False, False, False, 7151))
            payload = json.loads(output.read_text())

        self.assertEqual(scoped["message_count"], 2)
        self.assertEqual([row["id"] for row in payload["messages"]], [7300, 7301])
        self.assertEqual(self.client.asked,
                         [{"limit": None, "search": None, "reply_to": 7151}])

    def _use_dialogs(self, dialogs):
        self.client = _DialogClient(dialogs)
        self.cli.make_client = lambda _cfg: self.client

    def test_chats_separate_a_group_from_a_feed_and_a_bot_from_a_person(self):
        """`kind` files a supergroup, a gigagroup and a broadcast under one
        word, and a bot under the word for a person."""
        self._use_dialogs([
            _dialog(-1001, "Working supergroup",
                    _entity("Channel", megagroup=True, broadcast=False)),
            _dialog(-1002, "Announcements",
                    _entity("Channel", megagroup=False, broadcast=True)),
            _dialog(-1003, "Broadcast group",
                    _entity("Channel", megagroup=False, gigagroup=True, broadcast=True)),
            _dialog(-1004, "Legacy group", _entity("Chat")),
            _dialog(4001, "A person", _entity("User", bot=False)),
            _dialog(4002, "A bot", _entity("User", bot=True)),
            _dialog(-1005, "Banned from this one", _entity("ChatForbidden")),
        ])

        result = asyncio.run(self.cli.cmd_chats({"id": "test"}, 50))

        self.assertEqual([entry["category"] for entry in result],
                         ["group", "channel", "group", "group",
                          "user", "bot", "unknown"])
        self.assertEqual([entry["kind"] for entry in result[:2]],
                         ["Channel", "Channel"])
        self.assertEqual(self.client.limits, [50])
        self.assertTrue(self.client.disconnected)

    def test_chats_flag_a_forum_beside_its_category_rather_than_instead_of_it(self):
        """A forum is a group whose messages live in topics, so a caller
        filtering for groups still matches it and knows to ask `topics`."""
        self._use_dialogs([
            _dialog(-1001, "Forum", _entity("Channel", megagroup=True, forum=True)),
            _dialog(-1002, "Plain supergroup", _entity("Channel", megagroup=True)),
            _dialog(4001, "A person", _entity("User")),
        ])

        result = asyncio.run(self.cli.cmd_chats({"id": "test"}, 50))

        self.assertEqual([(entry["category"], entry["forum"]) for entry in result],
                         [("group", True), ("group", False), ("user", False)])

    def test_chats_keep_every_field_a_caller_already_reads(self):
        self._use_dialogs([
            _dialog(-1001, "Working supergroup",
                    _entity("Channel", megagroup=True), unread=3),
            _dialog(-1002, None, _entity("Chat"), unread=0),
        ])

        result = asyncio.run(self.cli.cmd_chats({"id": "test"}, 50))

        self.assertEqual(
            [{key: entry[key] for key in ("id", "kind", "name", "unread")}
             for entry in result],
            [{"id": -1001, "kind": "Channel", "name": "Working supergroup", "unread": 3},
             {"id": -1002, "kind": "Chat", "name": "(untitled)", "unread": 0}])

    def test_worker_scope_covers_the_topics_verb(self):
        shim = import_worker_shim()
        self.assertEqual(
            shim.parse_command_and_chat(["telegram", "topics", "-1001"]),
            ("topics", "-1001"))

    def test_worker_authority_refuses_topic_substitution_without_an_outbox(self):
        """A daemon-authorized worker inherits its topic; it cannot name another."""
        shim = import_worker_shim()
        env = {
            "TELEGRAM_AUTHORIZED_CHAT_ID": "-1001",
            "TELEGRAM_AUTHORIZED_TOPIC_ID": "77",
            "TELEGRAM_AUTHORIZED_CONNECTION": "8200881535",
        }
        for flag in ("--topic=99", "--thread=99"):
            argv = ["telegram", "send", "-1001", "hello", flag]
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(sys, "argv", argv):
                os.environ.pop("TELEGRAM_PROGRESS_OUTBOX", None)
                with self.assertRaises(SystemExit) as stopped:
                    shim.main()
            self.assertEqual(stopped.exception.code, 4)

    def test_worker_scope_recognizes_new_outbound_chat_commands(self):
        shim = import_worker_shim()
        self.assertEqual(
            shim.parse_command_and_chat(
                ["telegram", "--connection", "main", "send-media", "-1001", "a.jpg"]),
            ("send-media", "-1001"),
        )
        self.assertEqual(
            shim.parse_command_and_chat(
                ["telegram", "react", "-1001", "99", "👍"]),
            ("react", "-1001"),
        )

    def _cross_connection_env(self, tmpdir, grant=None):
        env = {
            "TELEGRAM_PROGRESS_OUTBOX": str(Path(tmpdir) / "outbox.jsonl"),
            "TELEGRAM_AUTHORIZED_CHAT_ID": "-1001",
            "TELEGRAM_AUTHORIZED_CONNECTION": "assistant",
            "TELEGRAM_WORKER_SESSION": str(Path(tmpdir) / "worker-session"),
            "TELEGRAM_REAL_TELEGRAM": "/usr/bin/true",
        }
        rule = {"allow": True}
        if grant is not None:
            rule["connections"] = grant
        context = {
            "version": 1,
            "connection": "assistant",
            "chat_id": "-1001",
            "sender_role": "supervisor",
            "allowed_capabilities": {"*": True, "telegram": rule},
        }
        env["CAPABILITIES_AUTH_CONTEXT"] = json.dumps(context)
        return env

    def _run_shim(self, shim, env, argv):
        """Run the shim's main() with exec captured rather than performed."""
        calls = []
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(shim.os, "execvp",
                                  lambda program, args: calls.append((program, args))):
            shim.main()
        return calls

    def test_worker_drops_its_session_when_it_names_another_connection(self):
        """One account is never read through another account's session."""
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._cross_connection_env(tmpdir, grant=["principal"])
            calls = self._run_shim(
                shim, env, ["telegram", "read", "555", "--connection", "principal"])
        self.assertEqual(len(calls), 1)
        _, args = calls[0]
        self.assertIn("--connection", args)
        # The receiving account's session must not be handed to another account.
        self.assertNotIn("--session", args)

    def test_worker_keeps_its_own_session_when_it_names_its_own_connection(self):
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._cross_connection_env(tmpdir, grant=["principal"])
            calls = self._run_shim(
                shim, env, ["telegram", "read", "555", "--connection", "assistant"])
        self.assertEqual(len(calls), 1)
        self.assertIn("--session", calls[0][1])

    def test_worker_refuses_a_session_path_on_a_daemon_turn(self):
        """A session path names a file, so no grant can make it safe."""
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._cross_connection_env(tmpdir, grant=["principal"])
            env.pop("TELEGRAM_PROGRESS_OUTBOX")
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(
                        sys, "argv",
                        ["telegram", "read", "555", "--session", "/tmp/elsewhere"]):
                os.environ.pop("TELEGRAM_PROGRESS_OUTBOX", None)
                with self.assertRaises(SystemExit) as stopped:
                    shim.main()
        self.assertEqual(stopped.exception.code, 4)

    # --- the CLI owns the cross-connection policy -------------------------

    def _service_context(self, grant=None, connection="assistant"):
        rule = {"allow": True}
        if grant is not None:
            rule["connections"] = grant
        return json.dumps({
            "version": 1,
            "source": "telegram",
            "connection": connection,
            "chat_id": "-1001",
            "sender_role": "supervisor",
            "allowed_capabilities": {"*": True, "telegram": rule},
        })

    def test_service_request_reads_a_granted_second_connection(self):
        cli = import_cli()
        env = {"CAPABILITIES_AUTH_CONTEXT": self._service_context(grant=["principal"]),
               "TELEGRAM_WORKER_SESSION": "/tmp/assistant-session"}
        with mock.patch.dict(os.environ, env, clear=False):
            connection, session = cli._auth_connection_gate(
                "principal", None, "read")
        self.assertEqual(connection, "principal")
        # The receiving account's session must not be used to read another.
        self.assertIsNone(session)

    def test_service_request_refuses_an_ungranted_connection(self):
        cli = import_cli()
        env = {"CAPABILITIES_AUTH_CONTEXT": self._service_context()}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(SystemExit) as stopped:
                cli._auth_connection_gate("principal", None, "read")
        self.assertEqual(stopped.exception.code, 4)

    def test_legacy_connection_list_remains_read_only(self):
        """The original list grants reads and never grows write authority."""
        cli = import_cli()
        env = {"CAPABILITIES_AUTH_CONTEXT": self._service_context(grant=["principal"])}
        for command in ("send", "send-media", "react"):
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaises(SystemExit) as stopped:
                    cli._auth_connection_gate("principal", None, command)
            self.assertEqual(stopped.exception.code, 4)

    def test_service_request_writes_through_an_explicitly_writable_connection(self):
        cli = import_cli()
        grant = {"principal": {"allow_write": True}}
        env = {"CAPABILITIES_AUTH_CONTEXT": self._service_context(grant=grant)}
        for command in ("send", "send-media", "react"):
            with mock.patch.dict(os.environ, env, clear=False):
                connection, session = cli._auth_connection_gate(
                    "principal", None, command)
            self.assertEqual(connection, "principal")
            self.assertIsNone(session)

    def test_service_request_refuses_write_without_the_role_write_grant(self):
        cli = import_cli()
        env = {"CAPABILITIES_AUTH_CONTEXT": self._service_context(
            grant={"principal": {}})}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(SystemExit) as stopped:
                cli._auth_connection_gate("principal", None, "send")
        self.assertEqual(stopped.exception.code, 4)

    def test_connection_write_gate_still_has_the_last_word(self):
        cli = import_cli()
        with self.assertRaises(SystemExit) as stopped:
            cli._write_gate("principal", False, "send")
        self.assertEqual(stopped.exception.code, 4)
        cli._write_gate("principal", True, "send")

    def test_service_request_keeps_its_own_connection_and_session(self):
        cli = import_cli()
        env = {"CAPABILITIES_AUTH_CONTEXT": self._service_context(grant=["principal"]),
               "TELEGRAM_WORKER_SESSION": "/tmp/assistant-session"}
        with mock.patch.dict(os.environ, env, clear=False):
            connection, session = cli._auth_connection_gate(
                "assistant", "/tmp/assistant-session", "read")
        self.assertEqual(connection, "assistant")
        self.assertEqual(session, "/tmp/assistant-session")

    def test_service_request_refuses_a_session_path(self):
        """A session names a file rather than a declared connection."""
        cli = import_cli()
        env = {"CAPABILITIES_AUTH_CONTEXT": self._service_context(grant=["principal"]),
               "TELEGRAM_WORKER_SESSION": "/tmp/assistant-session"}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(SystemExit) as stopped:
                cli._auth_connection_gate("principal", "/tmp/elsewhere", "read")
        self.assertEqual(stopped.exception.code, 4)

    def test_ordinary_cli_use_is_untouched_by_the_gate(self):
        cli = import_cli()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CAPABILITIES_AUTH_CONTEXT", None)
            self.assertEqual(
                cli._auth_connection_gate("principal", "/tmp/anywhere", "send"),
                ("principal", "/tmp/anywhere"))

    def _worker_env(self, outbox):
        return {
            "TELEGRAM_PROGRESS_OUTBOX": str(outbox),
            "TELEGRAM_AUTHORIZED_CHAT_ID": "-1001",
            "TELEGRAM_AUTHORIZED_TOPIC_ID": "77",
            "TELEGRAM_AUTHORIZED_CONNECTION": "8200881535",
            "TELEGRAM_AUTHORIZED_REQUESTER_ID": "777",
            "TELEGRAM_AUTHORIZED_ORIGIN_MESSAGE_ID": "88",
            "TELEGRAM_AUTHORIZED_JOB_ENGINE": "codex",
            "TELEGRAM_AUTHORIZED_JOB_MODEL": "gpt-test",
        }

    def test_a_jobs_help_request_is_not_scoped(self):
        """The prompts name this command instead of listing the verbs, so it has
        to answer inside a worker. Its parsers take no chat, no topic and no
        actor, and appending them turns the one discoverable surface into an
        argument error."""
        shim = import_worker_shim()
        for asking in (["jobs", "help"],
                       ["jobs", "-h"],
                       ["jobs", "submit", "-h"],
                       ["jobs", "register", "--help"]):
            self.assertTrue(shim._is_jobs_help(asking), asking)
        for acting in (["jobs", "active"],
                       ["jobs", "register", "help me with this"],
                       ["jobs", "show", "12"],
                       # Only the verb slot and the flag right after it are
                       # read. Anything further along is text somebody wrote,
                       # and it does not get to decide whether a call is scoped.
                       ["jobs", "register", "--", "-h"],
                       ["jobs", "amend", "12", "--help"]):
            self.assertFalse(shim._is_jobs_help(acting), acting)

    def test_the_scope_goes_where_it_parses(self):
        """A description starting with a dash travels after `--`, and everything
        after that marker is positional. Scope appended to the tail would arrive
        as extra positional arguments and kill the call, so it goes in right
        after the verb, ahead of anything the requester wrote."""
        shim = import_worker_shim()
        with mock.patch.dict(os.environ, self._worker_env(Path("/dev/null")),
                             clear=False):
            self.assertEqual(
                shim.scoped_jobs_argv(["jobs", "register", "--", "-h"],
                                      ["telegram", "jobs", "register", "--", "-h"]),
                ["jobs", "register", "--chat", "-1001", "--topic-id", "77",
                 "--actor", "777", "--requested-by", "777",
                 "--origin-message-id", "88", "--engine", "codex",
                 "--model", "gpt-test", "--", "-h"])
            self.assertEqual(
                shim.scoped_jobs_argv(["jobs", "show", "12"],
                                      ["telegram", "jobs", "show", "12"]),
                ["jobs", "show", "--chat", "-1001", "--topic-id", "77",
                 "--actor", "777", "12"])
            # A help request is never scoped, so it is never reordered either.
            self.assertEqual(shim.scoped_jobs_argv(["jobs", "help"],
                                                   ["telegram", "jobs", "help"]),
                             ["jobs", "help"])

    def test_worker_jobs_are_pinned_to_the_authorized_channel(self):
        """`jobs` reaches the register directly, so what the shim owns is the
        scope: the authorized chat and topic are appended, and a turn that names
        another one is refused before the real CLI is reached."""
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "progress.jsonl"
            with mock.patch.dict(os.environ, self._worker_env(outbox), clear=False):
                shim.enforce_job_scope(["telegram", "jobs", "active"])
                self.assertEqual(shim.job_scope_arguments(
                                     ["telegram", "jobs", "active"]),
                                 ["--chat", "-1001", "--topic-id", "77",
                                  "--actor", "777"])
                self.assertEqual(shim.job_scope_arguments(
                                     ["telegram", "jobs", "register", "work"]),
                                 ["--chat", "-1001", "--topic-id", "77",
                                  "--actor", "777",
                                  "--requested-by", "777",
                                  "--origin-message-id", "88",
                                  "--engine", "codex", "--model", "gpt-test"])
                for denied in (["telegram", "jobs", "active", "--chat", "-9999"],
                               ["telegram", "jobs", "list", "--all-channels"],
                               ["telegram", "jobs", "list", "--chat=-9999"],
                               ["telegram", "jobs", "register", "work",
                                "--requested-by", "999"]):
                    with self.assertRaises(SystemExit) as stopped:
                        shim.enforce_job_scope(denied)
                    self.assertEqual(stopped.exception.code, 4)
            self.assertFalse(outbox.exists())

    def test_a_registered_topic_key_is_one_the_daemon_can_resolve(self):
        """The shim pins a forum topic onto every `jobs` call, so the CLI writes
        the key of a job the daemon has to find its way back to. Both sides ask
        the register for the spelling; when they disagreed instead, every job
        registered in a topic finished and then retried delivery forever against
        a channel nothing could resolve."""
        cli = import_cli()
        key = cli._job_channel_key("-1001", "77")
        self.assertEqual(cli._jobs_module().channel_identity(key), ("-1001", 77))
        self.assertEqual(cli._job_channel_key("-1001", None), "-1001")
        with self.assertRaises(SystemExit) as stopped:
            cli._job_channel_key("-1001", "General")
        self.assertEqual(stopped.exception.code, 6)

    def test_a_routed_turn_still_registers_into_the_daemons_own_queue(self):
        """The queue is the daemon's - its slots, its leases, its delivery loop -
        and every read its runner does is partitioned by the daemon's project.
        A routed turn resolves the project it was routed to, so without this pin
        `jobs register` wrote a row into a partition nobody drains and `jobs
        active` read back the empty one beside it, while the handoff still told
        the requester the work had been accepted."""
        shim = import_worker_shim()
        routed = {
            "TELEGRAM_SERVICE_PROJECT_ROOT": "/home/daemonproj",
            "TELEGRAM_SERVICE_PROJECT_ENVELOPE": "/home/daemonproj/capabilities",
            "CLAUDE_PROJECT_DIR": "/home/routedproj",
            "CAPABILITIES_PROJECT_ENVELOPE": "",
        }
        with mock.patch.dict(os.environ, routed, clear=False):
            os.environ.pop("CAPABILITIES_PROJECT_ENVELOPE")
            shim.pin_jobs_to_service_project(["telegram", "jobs", "register", "work"])
            self.assertEqual(os.environ["CLAUDE_PROJECT_DIR"], "/home/daemonproj")
            self.assertEqual(os.environ["CAPABILITIES_PROJECT_ENVELOPE"],
                             "/home/daemonproj/capabilities")
            # The handed envelope names the root it answers for, so a descendant
            # standing elsewhere passes it over rather than applying it there.
            self.assertEqual(os.environ["CAPABILITIES_PROJECT_ENVELOPE_ROOT"],
                             "/home/daemonproj")

        with mock.patch.dict(os.environ, routed, clear=False):
            os.environ.pop("CAPABILITIES_PROJECT_ENVELOPE")
            # Routing exists so the work happens in the project it names, and
            # every call that is not about the queue keeps reaching it.
            shim.pin_jobs_to_service_project(["telegram", "read", "-1001"])
            self.assertEqual(os.environ["CLAUDE_PROJECT_DIR"], "/home/routedproj")
            self.assertNotIn("CAPABILITIES_PROJECT_ENVELOPE", os.environ)

    def _unscoped_jobs_call(self):
        return types.SimpleNamespace(chat=None, topic_id=None, actor=None,
                                     jobs_cmd="list")

    @staticmethod
    def _forget_daemon_scope():
        for name in ("TELEGRAM_DAEMON_CHILD", "TELEGRAM_AUTHORIZED_REQUESTER_ID",
                     "TELEGRAM_AUTHORIZED_CHAT_ID", "TELEGRAM_AUTHORIZED_TOPIC_ID"):
            os.environ.pop(name, None)

    def test_a_daemon_child_that_names_no_requester_is_refused_the_register(self):
        """The register is keyed by requester, so a child arriving without one
        used to fall through as an unscoped caller and be answered for every
        requester in the project. A launcher that said nothing about who it acts
        for is a missing answer, and the refusal names it rather than widening."""
        cli = import_cli()
        with mock.patch.dict(os.environ, {}, clear=False):
            self._forget_daemon_scope()
            os.environ["TELEGRAM_DAEMON_CHILD"] = "1"
            with self.assertRaises(SystemExit) as stopped:
                cli._job_scope(self._unscoped_jobs_call())
        self.assertEqual(stopped.exception.code, 4)

    def test_an_unstamped_caller_still_reads_the_whole_register(self):
        """A maintainer at a terminal owns this project and every job in it.
        Nothing the daemon did not launch carries the stamp, so that call stays
        the call it always was."""
        cli = import_cli()
        with mock.patch.dict(os.environ, {}, clear=False):
            self._forget_daemon_scope()
            self.assertEqual(cli._job_scope(self._unscoped_jobs_call()),
                             (None, None, None))

    def test_worker_jobs_without_an_authorized_chat_are_refused(self):
        shim = import_worker_shim()
        with mock.patch.dict(os.environ, {"TELEGRAM_AUTHORIZED_CHAT_ID": ""},
                             clear=False):
            with self.assertRaises(SystemExit) as stopped:
                    shim.enforce_job_scope(["telegram", "jobs", "active"])
        self.assertEqual(stopped.exception.code, 4)

    def test_worker_job_registration_is_attributed_without_a_handoff(self):
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "progress.jsonl"
            env = self._worker_env(outbox)
            env["TELEGRAM_REAL_TELEGRAM"] = "/real/telegram"
            row = {
                "id": "job-123",
                "description": "Count every topic",
                "state": "waiting",
            }
            # Registering is an ordinary passthrough now: no result to read and
            # nothing to signal, so the shim hands the process straight over.
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(
                        sys, "argv", ["telegram", "jobs", "register",
                                      "Count every topic"]), \
                    mock.patch.object(shim.os, "execvp") as execvp, \
                    mock.patch.object(sys, "stdout"):
                shim.main()

            invoked = [execvp.call_args.args[0]] + list(
                execvp.call_args.args[1])[1:]
            # The scope sits between the verb and what the requester wrote, so
            # a description passed after `--` still parses as a description.
            self.assertEqual(invoked, [
                "/real/telegram", "jobs", "register",
                "--chat", "-1001", "--topic-id", "77",
                "--actor", "777", "--requested-by", "777",
                "--origin-message-id", "88", "--engine", "codex",
                "--model", "gpt-test",
                "Count every topic",
            ])
            # Registering only opens a draft. The turn that wrote it still has
            # questions to ask, so nothing tells the daemon to end it yet.
            self.assertFalse(outbox.exists())

    def test_worker_job_submit_signals_the_handoff(self):
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "progress.jsonl"
            env = self._worker_env(outbox)
            env["TELEGRAM_REAL_TELEGRAM"] = "/real/telegram"
            row = {"id": "job-123", "description": "Count every topic",
                   "state": "waiting"}
            completed = types.SimpleNamespace(
                returncode=0, stdout=json.dumps(row) + "\n", stderr="")
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(
                        sys, "argv", ["telegram", "jobs", "submit", "job-123",
                                      "--confirm-active-jobs-checked"]), \
                    mock.patch.object(shim.subprocess, "run",
                                      return_value=completed) as run, \
                    mock.patch.object(sys, "stdout"):
                self.assertEqual(shim.main(), 0)

            invoked = run.call_args.args[0]
            self.assertEqual(invoked[0], "/real/telegram")
            self.assertIn("--chat", invoked)
            self.assertIn("-1001", invoked)
            event = json.loads(outbox.read_text())
            self.assertEqual(event["event"], "job_submitted")
            self.assertEqual(event["job_id"], "job-123")
            self.assertEqual(event["description"], "Count every topic")

    def test_failed_worker_job_submit_does_not_signal_a_handoff(self):
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "progress.jsonl"
            completed = types.SimpleNamespace(
                returncode=6, stdout="", stderr="bad submit\n")
            with mock.patch.object(shim.subprocess, "run", return_value=completed), \
                    mock.patch.object(sys, "stdout"), \
                    mock.patch.object(sys, "stderr"):
                self.assertEqual(
                    shim.run_job_handoff(["telegram", "jobs", "submit", "x"],
                                         str(outbox)),
                    6)
            self.assertFalse(outbox.exists())

    def test_worker_send_rejects_unknown_flag_after_text_before_outbox(self):
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "progress.jsonl"
            with mock.patch.dict(os.environ, self._worker_env(outbox), clear=False):
                with self.assertRaises(SystemExit) as stopped:
                    shim.write_progress(
                        ["telegram", "send", "-1001", "hello", "--parse-mode"])
            self.assertEqual(stopped.exception.code, 6)
            self.assertFalse(outbox.exists())

    def test_worker_send_rejects_chat_substitution_before_outbox(self):
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "progress.jsonl"
            with mock.patch.dict(os.environ, self._worker_env(outbox), clear=False):
                with self.assertRaises(SystemExit) as stopped:
                    shim.write_progress(["telegram", "send", "-9999", "hello"])
            self.assertEqual(stopped.exception.code, 4)
            self.assertFalse(outbox.exists())

    def test_worker_send_rejects_topic_and_session_substitution(self):
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "progress.jsonl"
            for flag in ("--topic=99", "--session=/tmp/other"):
                argv = ["telegram", "send", "-1001", "hello", flag]
                with mock.patch.dict(os.environ, self._worker_env(outbox), clear=False), \
                     mock.patch.object(sys, "argv", argv):
                    with self.assertRaises(SystemExit) as stopped:
                        shim.main()
                self.assertEqual(stopped.exception.code, 4)
            self.assertFalse(outbox.exists())

    def test_cross_connection_send_bypasses_the_daemon_outbox(self):
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "progress.jsonl"
            env = self._worker_env(outbox)
            env["TELEGRAM_REAL_TELEGRAM"] = "/usr/bin/true"
            calls = self._run_shim(
                shim, env,
                ["telegram", "send", "555", "hello", "--topic=99",
                 "--connection", "principal"])
            self.assertEqual(len(calls), 1)
            self.assertIn("--connection", calls[0][1])
            self.assertIn("--topic=99", calls[0][1])
            self.assertNotIn("--session", calls[0][1])
            self.assertFalse(outbox.exists())

    def test_worker_send_queues_only_the_authorized_scope(self):
        shim = import_worker_shim()
        with tempfile.TemporaryDirectory() as td:
            outbox = Path(td) / "progress.jsonl"
            with mock.patch.dict(os.environ, self._worker_env(outbox), clear=False):
                self.assertTrue(shim.write_progress(
                    ["telegram", "send", "current", "checking the logs"]))
            record = json.loads(outbox.read_text())
            self.assertEqual(record["chat"], "-1001")
            self.assertEqual(record["topic_id"], "77")
            self.assertEqual(record["connection"], "8200881535")
            self.assertEqual(record["text"], "checking the logs")

    def test_poll_entities_and_unknown_media_do_not_abort_export(self):
        class TextWithEntities:
            def __init__(self, text):
                self.text = text

        def message(message_id, *, poll=None, media=None, text=None):
            return types.SimpleNamespace(
                id=message_id, date=None, edit_date=None, sender=None,
                sender_id=77, reply_to_msg_id=None, forward=None,
                message=text, action=None, voice=False, audio=False,
                video_note=False, video=False, sticker=False, photo=False,
                document=False, web_preview=False, poll=poll, contact=False,
                geo=False, media=media, download_media=None,
            )

        poll = types.SimpleNamespace(
            poll=types.SimpleNamespace(
                id=91,
                question=TextWithEntities("Choose one"),
                answers=[
                    types.SimpleNamespace(text=TextWithEntities("Alpha"), option=b"a"),
                    types.SimpleNamespace(text=TextWithEntities("Beta"), option=b"b"),
                ],
                closed=True, public_voters=False, multiple_choice=False, quiz=False,
            ),
            results=types.SimpleNamespace(results=[], total_voters=3),
        )
        messages = [
            message(3, text="later message"),
            message(2, media=object()),
            message(1, poll=poll, media=poll),
        ]
        self.client = _ExportClient(messages)
        self.cli.make_client = lambda _cfg: self.client
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "export.json"
            result = asyncio.run(self.cli.cmd_export(
                {"id": "8200881535"}, "-1001", str(output), None, None, None,
                False, False, False, False))
            payload = json.loads(output.read_text())

        self.assertEqual(result["message_count"], 3)
        by_id = {row["id"]: row for row in payload["messages"]}
        self.assertEqual(by_id[1]["media"]["question"], "Choose one")
        self.assertEqual(
            [answer["text"] for answer in by_id[1]["media"]["answers"]],
            ["Alpha", "Beta"],
        )
        self.assertEqual(by_id[2]["type"], "unsupported")
        self.assertEqual(by_id[3]["text"], "later message")


class ServiceDoctorExitTests(unittest.TestCase):
    """`service doctor` is what a container entrypoint and a deploy step gate
    on, so its verdict has to reach the exit code. The payload is the contract a
    consuming project already reads; only the exit code answers for `ok`."""

    PAYLOAD_KEYS = {"ok", "service", "workers", "connection", "note"}

    def _run_doctor(self, cli, tmp, health):
        """Drive the real dispatch for one runtime-health shape and return
        (exit_code, payload). The verdict is computed by the real
        `cmd_service_doctor` over the real `_service_runtime_health`; only the
        initialization, connection and status plumbing around it is stood in
        for. `health=None` means no daemon at all."""
        health_path = Path(tmp) / "health.json"
        running = health is not None
        if health:
            health_path.write_text(json.dumps(health))
        runtime = cli._service_runtime_health(health_path, running)

        def fake_status(connection_flag, session_flag):
            return {"initialized": True, "running": running,
                    "healthy": runtime["healthy"], "health": runtime,
                    "connection": "probe", "expectation_mismatches": []}

        cfg = {"id": "probe", "allow_write": True,
               "session": str(Path(tmp) / "probe")}
        captured = io.StringIO()
        with mock.patch.object(cli, "_gate", lambda: None), \
                mock.patch.object(cli, "_contract", lambda argv: None), \
                mock.patch.object(cli, "_service_project_root", lambda: Path(tmp)), \
                mock.patch.object(cli, "_require_service_initialized", lambda root: None), \
                mock.patch.object(cli, "_validate_service_settings", lambda root: None), \
                mock.patch.object(cli, "_service_wanted_connection",
                                  lambda root, flag: "probe"), \
                mock.patch.object(cli, "_load_config", lambda *a, **k: cfg), \
                mock.patch.object(cli, "_write_gate", lambda *a: None), \
                mock.patch.object(cli, "cmd_service_status", fake_status), \
                mock.patch.object(sys, "argv", ["telegram", "service", "doctor"]), \
                contextlib.redirect_stdout(captured):
            try:
                cli.main()
                code = 0
            except SystemExit as stopped:
                code = stopped.code
        return code, json.loads(captured.getvalue())

    @staticmethod
    def _stamp(age_seconds):
        moment = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return moment.isoformat().replace("+00:00", "Z")

    def test_a_live_daemon_with_no_health_record_fails(self):
        """`health.json` absent under a live pid is state `unknown` — the
        daemon is up and nothing says its sync is current."""
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self._run_doctor(cli, tmp, health={})
        self.assertEqual(payload["service"]["health"]["state"], "unknown")
        self.assertFalse(payload["ok"])
        self.assertEqual(code, 5)

    def test_a_live_daemon_whose_sync_went_stale_fails(self):
        """A health record older than its own staleness window is state
        `stale` — the sync stopped without the process dying."""
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self._run_doctor(cli, tmp, health={
                "state": "healthy", "last_sync_at": self._stamp(900),
                "stale_after_seconds": 60})
        self.assertEqual(payload["service"]["health"]["state"], "stale")
        self.assertFalse(payload["ok"])
        self.assertEqual(code, 5)

    def test_a_fresh_daemon_passes(self):
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self._run_doctor(cli, tmp, health={
                "state": "healthy", "last_sync_at": self._stamp(1),
                "stale_after_seconds": 600})
        self.assertEqual(payload["service"]["health"]["state"], "healthy")
        self.assertTrue(payload["ok"])
        self.assertEqual(code, 0)

    def test_a_stopped_service_passes(self):
        """No daemon is not a failing daemon; the verdict is unchanged."""
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self._run_doctor(cli, tmp, health=None)
        self.assertEqual(payload["service"]["health"]["state"], "stopped")
        self.assertTrue(payload["ok"])
        self.assertEqual(code, 0)

    def test_the_failing_verdict_still_emits_the_whole_payload(self):
        """A consuming project reads this payload; the exit code is added to it
        rather than taken out of it."""
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            failing = self._run_doctor(cli, tmp, health={})[1]
            passing = self._run_doctor(cli, tmp, health=None)[1]
        for payload in (failing, passing):
            self.assertEqual(set(payload), self.PAYLOAD_KEYS)
            self.assertEqual(payload["connection"], "probe")
            self.assertIn("update sync", payload["note"])


GUIDE_PATH = TELEGRAM_DIR / "guides" / "assistant-service.md"

# What the stubbed binaries answer, shaped on what the real ones printed on
# 2026-09-18: claude 2.1.267 answers one result document and, on a model it
# will not run, exits 1 with `is_error` and a 404 sentence in `result`; codex
# 0.152.1 answers a JSONL stream and, on such a model, exits 1 after a local
# metadata note, `turn.started`, and the provider's 400 on `error` and
# `turn.failed`. The stubs record every invocation so a test can count them.
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
model = args[args.index("--model") + 1] if "--model" in args else None
with open(os.environ["FAKE_WORKER_LOG"], "a") as log:
    log.write(json.dumps({"binary": "claude", "model": model, "cwd": os.getcwd()}) + "\\n")
if model in os.environ.get("FAKE_REFUSED_MODELS", "").split(","):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": f"There's an issue with the selected model ({model}). "
                                "It may not exist or you may not have access to it.",
                      "api_error_status": 404}))
    sys.stderr.write("[claude-code:unrecognized_model] {}\\n")
    sys.exit(1)
if os.environ.get("FAKE_HANG"):
    import time
    time.sleep(30)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "OK", "usage": {}}))
"""

FAKE_CODEX = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["exec", "--help"]:
    print("usage: codex exec")
    sys.exit(0)
model = args[args.index("-m") + 1] if "-m" in args else None
out = args[args.index("-o") + 1] if "-o" in args else None
with open(os.environ["FAKE_WORKER_LOG"], "a") as log:
    log.write(json.dumps({"binary": "codex", "model": model, "cwd": os.getcwd()}) + "\\n")
sys.stderr.write("Reading additional input from stdin...\\n")
if model in os.environ.get("FAKE_REFUSED_MODELS", "").split(","):
    refusal = "unexpected status 400 Bad Request: " + json.dumps({"error": {
        "type": "invalid_request_error",
        "message": f"The '{model}' model is not supported when using Codex "
                   "with a ChatGPT account."}})
    print(json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "error",
        "message": f"Model metadata for `{model}` not found. Defaulting to fallback metadata"}}))
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "error", "message": refusal}))
    print(json.dumps({"type": "turn.failed", "error": {"message": refusal}}))
    sys.exit(1)
print(json.dumps({"type": "thread.started", "thread_id": "thread-1"}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
if out:
    open(out, "w").write("OK")
"""


class ServiceDoctorWorkerPreflightTests(unittest.TestCase):
    """`service doctor` asks each configured worker binary, once per distinct
    (worker, model) pair, whether it runs that model. The binaries on PATH here
    are stubs written by the test: the real ones would spend money and need a
    login, and the doctor's verdict has to come from the binary's own answer
    rather than from anything this repository holds about models."""

    ALL_POSITIONS = (
        "settings.defaults.workers.{worker}.model",
        "settings.defaults.voice_agent.workers.{worker}.model",
        "settings.allowed_users.42.voice_agent.workers.{worker}.model",
    )

    @staticmethod
    def _settings(*, defaults=None, voice=None, user=None, timeout=5):
        """A schema-valid document declaring worker models at the three
        positions the schema admits them, each block given as
        {worker: model}."""
        def block(models):
            return {worker: {"model": model} for worker, model in (models or {}).items()}
        return {
            "connection": "probe",
            "defaults": {"worker": "claude", "worker_timeout": timeout,
                         "workers": block(defaults),
                         "voice_agent": {"workers": block(voice)}},
            "allowed_users": {"42": {"name": "A caller",
                                     "voice_agent": {"mode": "enabled",
                                                     "workers": block(user)}}},
        }

    def _run_doctor(self, cli, tmp, settings, *, refused=(), binaries=("claude", "codex"),
                    hang=False):
        """Drive the real dispatch with the stub binaries on PATH. The verdict
        is computed by the real `cmd_service_doctor`, the real schema walk and
        the real `workers.probe_worker`; only the initialization, connection
        and daemon-status plumbing is stood in for. Returns (exit_code,
        payload, invocations)."""
        bindir = Path(tmp) / "bin"
        bindir.mkdir(exist_ok=True)
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            if name in binaries:
                (bindir / name).write_text(body)
                (bindir / name).chmod(0o755)
        log_path = Path(tmp) / "invocations.jsonl"
        log_path.write_text("")
        env = {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
               "FAKE_WORKER_LOG": str(log_path),
               "FAKE_REFUSED_MODELS": ",".join(refused)}
        if hang:
            env["FAKE_HANG"] = "1"

        def fake_status(connection_flag, session_flag):
            return {"initialized": True, "running": False, "healthy": False,
                    "health": {"state": "stopped"}, "connection": "probe",
                    "expectation_mismatches": []}

        cfg = {"id": "probe", "allow_write": True,
               "session": str(Path(tmp) / "probe")}
        captured = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli, "_gate", lambda: None), \
                mock.patch.object(cli, "_contract", lambda argv: None), \
                mock.patch.object(cli, "_service_project_root", lambda: Path(tmp)), \
                mock.patch.object(cli, "_service_dir", lambda root: Path(tmp) / "service"), \
                mock.patch.object(cli, "_require_service_initialized", lambda root: None), \
                mock.patch.object(cli, "_service_settings", lambda root: settings), \
                mock.patch.object(cli, "_service_wanted_connection",
                                  lambda root, flag: "probe"), \
                mock.patch.object(cli, "_load_config", lambda *a, **k: cfg), \
                mock.patch.object(cli, "_write_gate", lambda *a: None), \
                mock.patch.object(cli, "cmd_service_status", fake_status), \
                mock.patch.object(sys, "argv", ["telegram", "service", "doctor"]), \
                contextlib.redirect_stdout(captured):
            try:
                cli.main()
                code = 0
            except SystemExit as stopped:
                code = stopped.code
        invocations = [json.loads(line) for line in log_path.read_text().splitlines()]
        return code, json.loads(captured.getvalue()), invocations

    @staticmethod
    def _row(payload, worker, model):
        return next(row for row in payload["workers"]["checked"]
                    if row["worker"] == worker and row["model"] == model)

    def test_a_pair_the_binary_accepts_is_ok(self):
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload, calls = self._run_doctor(
                cli, tmp, self._settings(defaults={"claude": None, "codex": "fast-model"}))
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["workers"]["ok"])
        default = self._row(payload, "claude", None)
        pinned = self._row(payload, "codex", "fast-model")
        for row in (default, pinned):
            self.assertTrue(row["ok"])
            self.assertEqual(row["verdict"], "accepted")
        self.assertEqual(default["binary"], "claude")
        self.assertEqual(pinned["binary"], "codex")
        # The default pair is asked with no model flag at all — the binary's own
        # default, not one this repository picked for it.
        self.assertEqual(
            sorted((c["binary"], c["model"]) for c in calls),
            [("claude", None), ("codex", "fast-model")])

    def test_a_pair_the_binary_refuses_fails_the_verdict_and_names_both(self):
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload, _ = self._run_doctor(
                cli, tmp,
                self._settings(defaults={"claude": None, "codex": "bogus-model-x"}),
                refused=("bogus-model-x",))
        self.assertEqual(code, 5)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["workers"]["ok"])
        self.assertTrue(self._row(payload, "claude", None)["ok"])
        refused = self._row(payload, "codex", "bogus-model-x")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["verdict"], "model_refused")
        self.assertEqual(refused["binary"], "codex")
        self.assertIn("bogus-model-x", refused["notice"])
        self.assertIn("codex", refused["notice"])
        # The binary's own words travel with the verdict.
        self.assertIn("is not supported when using Codex", refused["reason"])

    def test_a_refusal_from_claude_is_read_the_same_way(self):
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload, _ = self._run_doctor(
                cli, tmp, self._settings(defaults={"claude": "bogus-model-x"}),
                refused=("bogus-model-x",))
        self.assertEqual(code, 5)
        refused = self._row(payload, "claude", "bogus-model-x")
        self.assertEqual(refused["verdict"], "model_refused")
        self.assertEqual(refused["binary"], "claude")
        self.assertIn("bogus-model-x", refused["notice"])
        self.assertIn("may not exist", refused["reason"])

    def test_every_position_the_schema_admits_a_model_at_is_checked(self):
        """A refusal planted only at the third position is found, and the
        positions the doctor reports are exactly the three the schema admits —
        read off the schema's own walk, not a list kept beside it."""
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload, _ = self._run_doctor(
                cli, tmp,
                self._settings(defaults={"claude": None},
                               voice={"claude": "fast-model"},
                               user={"codex": "bogus-model-x"}),
                refused=("bogus-model-x",))
        self.assertEqual(code, 5)
        refused = self._row(payload, "codex", "bogus-model-x")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["declared_at"],
                         ["settings.allowed_users.42.voice_agent.workers.codex.model"])
        with tempfile.TemporaryDirectory() as tmp:
            _, payload, _ = self._run_doctor(
                cli, tmp,
                self._settings(defaults={"claude": "m"}, voice={"claude": "m"},
                               user={"claude": "m"}))
        self.assertEqual(
            sorted(self._row(payload, "claude", "m")["declared_at"]),
            sorted(position.format(worker="claude") for position in self.ALL_POSITIONS))

    def test_an_identical_pair_declared_in_several_positions_is_asked_once(self):
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload, calls = self._run_doctor(
                cli, tmp,
                self._settings(defaults={"codex": "fast-model", "claude": None},
                               voice={"codex": "fast-model"},
                               user={"codex": "fast-model"}))
        self.assertEqual(code, 0)
        self.assertEqual([c for c in calls if c["binary"] == "codex"],
                         [{"binary": "codex", "model": "fast-model",
                           "cwd": mock.ANY}])
        row = self._row(payload, "codex", "fast-model")
        self.assertEqual(len(row["declared_at"]), 3)
        self.assertEqual(len(payload["workers"]["checked"]), 2)

    def test_the_in_process_stub_is_reported_without_a_binary(self):
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code, payload, calls = self._run_doctor(
                cli, tmp, self._settings(defaults={"stub": None}))
        self.assertEqual(code, 0)
        row = self._row(payload, "stub", None)
        self.assertTrue(row["ok"])
        self.assertEqual(row["verdict"], "in_process")
        self.assertIsNone(row["binary"])
        self.assertEqual(calls, [])

    def test_a_binary_missing_from_path_cannot_accept_its_pair(self):
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            bare = Path(tmp) / "bare"
            bare.mkdir()
            with mock.patch.dict(os.environ, {"PATH": str(bare)}):
                code, payload, _ = self._run_doctor(
                    cli, tmp, self._settings(defaults={"codex": None}),
                    binaries=())
        self.assertEqual(code, 5)
        row = self._row(payload, "codex", None)
        self.assertFalse(row["ok"])
        self.assertEqual(row["verdict"], "binary_missing")
        self.assertIn("codex", row["reason"])

    def test_a_binary_that_gives_no_verdict_in_time_is_not_ok(self):
        """The round-trip is held to `defaults.worker_timeout`, so the doctor
        finishes in bounded time whatever the binary does."""
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            code, payload, _ = self._run_doctor(
                cli, tmp, self._settings(defaults={"claude": None}, timeout=0.5),
                hang=True)
            elapsed = time.monotonic() - started
        self.assertEqual(code, 5)
        row = self._row(payload, "claude", None)
        self.assertEqual(row["verdict"], "timed_out")
        self.assertIn("0.5s", row["reason"])
        self.assertLess(elapsed, 10)

    def test_the_probe_runs_in_scratch_rather_than_in_the_project(self):
        """One minimal round-trip: no project context, no hooks, no files."""
        cli = import_cli()
        with tempfile.TemporaryDirectory() as tmp:
            _, _, calls = self._run_doctor(
                cli, tmp, self._settings(defaults={"claude": None}))
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(Path(calls[0]["cwd"]).resolve(), Path(tmp).resolve())
        self.assertIn("telegram-doctor-", calls[0]["cwd"])

    def test_the_guide_states_that_null_is_the_stable_setting(self):
        guide = GUIDE_PATH.read_text()
        self.assertIn("A worker's `model` is `null` by default and `null` is the "
                      "stable setting: it is the binary's own default", guide)
        self.assertIn("Every pin is an exception taken for a named reason", guide)
        self.assertIn("`telegram service doctor` verifies each distinct (worker, model) "
                      "pair the settings declare against the binary itself", guide)

    def test_help_says_what_doctor_spends(self):
        """An operator is told the cost before paying it: one live call per
        distinct pair, each held to the turn's own window."""
        help_text = " ".join(import_cli().__doc__.split())
        self.assertIn("`doctor` also spends one live call on each worker binary per "
                      "distinct configured (worker, model) pair", help_text)
        self.assertIn("each held to `defaults.worker_timeout`", help_text)


if __name__ == "__main__":
    unittest.main()
