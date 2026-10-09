#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "capabilities-contract==0.3.0",
#     "puremagic>=1.28",
#     "neonize @ https://github.com/ai-cluster-one/neonize/releases/download/v0.5.2-histsync.1/neonize-0.5.2-py3-none-any.whl",
# ]
# ///
"""How a history chunk becomes rows, checked against the protocol's own message
definitions rather than against a description of them.

The definitions ship inside the engine, which is built per platform, so these
skip where it was not built. Run directly to fetch it: `./test_history_chunks.py`.
The rows land in a throwaway Postgres named by WHATSAPP_TEST_DSN; without one
these skip too.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

wa = _cli.load()
ENGINE = _cli.engine_available(wa)
if ENGINE:
    from neonize.proto.waHistorySync import (  # noqa: E402
        WAWebProtobufsHistorySync_pb2 as history_proto)


class StoreCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)


@unittest.skipUnless(ENGINE, "the engine was not built for this host")
@_cli.needs_store
class EndOfHistory(StoreCase):
    """`endOfHistoryTransfer` says a transfer ended. Only one value of the
    sibling type field says the phone holds nothing older, and reading the
    boolean alone silences reach-back for exactly the chats that have depth."""

    def ingest(self, transfer_type):
        cfg = _cli.store_cfg()
        db = wa._open_store(cfg)
        data = history_proto.HistorySync(
            syncType=history_proto.HistorySync.RECENT)
        conversation = data.conversations.add()
        conversation.ID = "x@g.us"
        conversation.endOfHistoryTransfer = True
        conversation.endOfHistoryTransferType = transfer_type
        held = conversation.messages.add().message
        held.key.ID = "m1"
        held.messageTimestamp = 1700000000
        held.message.conversation = "hello"
        wa._ingest_history(db, data, wa._connection_home(cfg), "RECENT")
        flag = wa._chat_row(db, "x@g.us")["end_of_history"]
        covered = wa._coverage(db, "x@g.us", limit=500, from_ts=None,
                               to_ts=None)["covered"]
        db.close()
        return flag, covered

    def test_nothing_more_on_the_primary_closes_the_chat(self):
        conversation = history_proto.Conversation
        self.assertEqual(
            self.ingest(conversation.COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY),
            (1, True))

    def test_every_other_ending_leaves_the_chat_open(self):
        conversation = history_proto.Conversation
        for name in ("COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY",
                     "COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY",
                     "COMPLETE_ON_DEMAND_SYNC_WITH_MORE_MSG_ON_PRIMARY_BUT_NO_ACCESS"):
            with self.subTest(ending=name):
                self.assertEqual(self.ingest(getattr(conversation, name)),
                                 (0, False))


@unittest.skipUnless(ENGINE, "the engine was not built for this host")
@_cli.needs_store
class RawChunks(StoreCase):
    """The protobuf is on disk before anything parses it, under a name that
    cannot collide: the engine dispatches events with nothing serialising them,
    and two deliveries naming the same file would have one overwrite the other."""

    def setUp(self):
        super().setUp()
        self.cfg = _cli.store_cfg()
        self.db = wa._open_store(self.cfg)
        self.home = wa._connection_home(self.cfg)

    def tearDown(self):
        self.db.close()

    def corrupt_text(self) -> None:
        self.db.execute(
            "UPDATE whatsapp_messages SET text = 'corrupted' WHERE account = %s",
            (self.db.account,))

    def text(self) -> str:
        return self.db.execute(
            "SELECT text FROM whatsapp_messages WHERE account = %s",
            (self.db.account,)).fetchone()["text"]

    def chunk(self, chat="y@g.us", message_id="m1", text="hello", ts=1700000000):
        data = history_proto.HistorySync(
            syncType=history_proto.HistorySync.RECENT)
        conversation = data.conversations.add()
        conversation.ID = chat
        held = conversation.messages.add().message
        held.key.ID = message_id
        held.messageTimestamp = ts
        held.message.conversation = text
        return data

    def test_repeated_deliveries_never_overwrite_each_other(self):
        for _ in range(3):
            wa._ingest_history(self.db, self.chunk(), self.home, "RECENT")
        files = sorted((self.home / "raw").glob("*.pb"))
        self.assertEqual(len(files), 3)
        rows = [r["raw_file"] for r in self.db.execute(
            "SELECT raw_file FROM whatsapp_chunks WHERE account = %s",
            (self.db.account,))]
        self.assertEqual(len(set(rows)), 3)
        for name in rows:
            self.assertTrue((self.home / "raw" / name).exists())

    def test_a_rebuild_restores_rows_a_bad_parser_wrote(self):
        wa._ingest_history(self.db, self.chunk(text="the real text"),
                           self.home, "RECENT")
        with wa._writing(self.db):
            self.corrupt_text()
            wa._set_meta(self.db, "schema", "0")
        self.db.close()
        self.db = wa._open_store(self.cfg)
        self.assertEqual(self.text(), "the real text")
        self.assertEqual(wa._get_meta(self.db, "schema"), wa.STORE_VERSION)

    def test_a_rebuild_that_could_not_finish_stays_pending(self):
        wa._ingest_history(self.db, self.chunk(), self.home, "RECENT")
        (self.home / "raw" / "20260101T000000-deadbeef-RECENT.pb").write_bytes(
            b"\xff\xff not a protobuf \xff\xff")
        with wa._writing(self.db):
            wa._set_meta(self.db, "schema", "0")
        self.db.close()
        self.db = wa._open_store(self.cfg)
        version = wa._get_meta(self.db, "schema")
        note = wa._get_meta(self.db, "rebuild_incomplete")
        self.assertEqual(version, "0", "the version must not claim a finished rebuild")
        self.assertIsNotNone(note, "the incomplete rebuild must be recorded")
        self.assertIn("unreadable", note)

    def test_a_store_written_before_versioning_is_rebuilt(self):
        wa._ingest_history(self.db, self.chunk(text="the real text"),
                           self.home, "RECENT")
        with wa._writing(self.db):
            self.corrupt_text()
            wa._drop_meta(self.db, "schema")
        self.db.close()
        self.db = wa._open_store(self.cfg)
        self.assertEqual(self.text(), "the real text")


@unittest.skipUnless(ENGINE, "the engine was not built for this host")
@_cli.needs_store
class LiveMessages(StoreCase):
    """A message as it arrives, through the same capture path a history chunk
    takes: its milliseconds become seconds, its sender's two forms are both
    kept, and a redelivery is the same row."""

    def event(self, message_id="L1", text="live hello"):
        import types
        engine = wa._engine()
        message = type(history_proto.HistorySync().conversations.add()
                       .messages.add().message.message)()
        message.conversation = text
        source = types.SimpleNamespace(
            Chat=engine["build_jid"]("120363000000000002", "g.us"),
            Sender=engine["build_jid"]("200", "lid"),
            SenderAlt=engine["build_jid"]("15551234567", "s.whatsapp.net"),
            IsFromMe=False)
        info = types.SimpleNamespace(MessageSource=source, ID=message_id,
                                     Timestamp=1700000000000, Pushname="<PERSON>")
        return types.SimpleNamespace(Info=info, Message=message)

    def test_a_live_message_is_captured_once(self):
        cfg = _cli.store_cfg()
        db = wa._open_store(cfg)
        try:
            self.assertEqual(wa._ingest_live(db, self.event()), 1)
            self.assertEqual(wa._ingest_live(db, self.event()), 0)
            row = db.execute(
                "SELECT * FROM whatsapp_messages WHERE account = %s",
                (db.account,)).fetchone()
            self.assertEqual((row["text"], row["ts"], row["sender"],
                              row["sender_lid"], row["sync_type"]),
                             ("live hello", 1700000000,
                              "15551234567@s.whatsapp.net", "200@lid", "LIVE"))
            chat = wa._chat_row(db, "120363000000000002@g.us")
            self.assertEqual(chat["last_ts"], 1700000000)
            self.assertEqual(wa._identity_index(db, {"200@lid"})["200@lid"]["jid"],
                             "15551234567@s.whatsapp.net")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
