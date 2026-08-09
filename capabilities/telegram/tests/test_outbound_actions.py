#!/usr/bin/env python3
"""Focused coverage for outgoing Telegram media and reactions."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from importlib.machinery import SourceFileLoader
from pathlib import Path


TELEGRAM_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = TELEGRAM_DIR / "bin" / "telegram"
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

    def _worker_env(self, outbox):
        return {
            "TELEGRAM_PROGRESS_OUTBOX": str(outbox),
            "TELEGRAM_AUTHORIZED_CHAT_ID": "-1001",
            "TELEGRAM_AUTHORIZED_TOPIC_ID": "77",
            "TELEGRAM_AUTHORIZED_CONNECTION": "8200881535",
        }

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
            for flag in ("--topic=99", "--session=/tmp/other", "--connection=other"):
                argv = ["telegram", "send", "-1001", "hello", flag]
                with mock.patch.dict(os.environ, self._worker_env(outbox), clear=False), \
                     mock.patch.object(sys, "argv", argv):
                    with self.assertRaises(SystemExit) as stopped:
                        shim.main()
                self.assertEqual(stopped.exception.code, 4)
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


if __name__ == "__main__":
    unittest.main()
