#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "puremagic>=1.28",
#     "neonize @ https://github.com/ai-cluster-one/neonize/releases/download/v0.4.7-histsync.2/neonize-0.4.7-py3-none-any.whl",
# ]
# ///
"""How a history chunk becomes rows, checked against the protocol's own message
definitions rather than against a description of them.

The definitions ship inside the engine, which is built per platform, so these
skip where it was not built. Run directly to fetch it: `./test_history_chunks.py`.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

wa = _cli.load()
ENGINE = _cli.engine_available(wa)
if ENGINE:
    from neonize.proto.waHistorySync import (  # noqa: E402
        WAWebProtobufsHistorySync_pb2 as history_proto)


@unittest.skipUnless(ENGINE, "the engine was not built for this host")
class EndOfHistory(unittest.TestCase):
    """`endOfHistoryTransfer` says a transfer ended. Only one value of the
    sibling type field says the phone holds nothing older, and reading the
    boolean alone silences reach-back for exactly the chats that have depth."""

    def ingest(self, transfer_type):
        cfg = {"id": "test", "home": tempfile.mkdtemp()}
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
        flag = db.execute(
            "SELECT end_of_history FROM chats WHERE id = 'x@g.us'").fetchone()[0]
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
class RawChunks(unittest.TestCase):
    """The protobuf is on disk before anything parses it, under a name that
    cannot collide: the engine dispatches events with nothing serialising them,
    and two deliveries naming the same file would have one overwrite the other."""

    def setUp(self):
        self.cfg = {"id": "test", "home": tempfile.mkdtemp()}
        self.db = wa._open_store(self.cfg)
        self.home = wa._connection_home(self.cfg)

    def tearDown(self):
        self.db.close()

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
        rows = [r["raw_file"] for r in self.db.execute("SELECT raw_file FROM chunks")]
        self.assertEqual(len(set(rows)), 3)
        for name in rows:
            self.assertTrue((self.home / "raw" / name).exists())

    def test_a_rebuild_restores_rows_a_bad_parser_wrote(self):
        wa._ingest_history(self.db, self.chunk(text="the real text"),
                           self.home, "RECENT")
        with wa._writing(self.db):
            self.db.execute("UPDATE messages SET text = 'corrupted'")
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', '0')")
        self.db.close()
        self.db = wa._open_store(self.cfg)
        held = self.db.execute("SELECT text FROM messages").fetchone()
        self.assertEqual(held["text"], "the real text")
        self.assertEqual(self.db.execute(
            "SELECT value FROM meta WHERE key = 'schema'").fetchone()["value"],
            wa.STORE_VERSION)

    def test_a_rebuild_that_could_not_finish_stays_pending(self):
        wa._ingest_history(self.db, self.chunk(), self.home, "RECENT")
        (self.home / "raw" / "20260101T000000-deadbeef-RECENT.pb").write_bytes(
            b"\xff\xff not a protobuf \xff\xff")
        with wa._writing(self.db):
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', '0')")
        self.db.close()
        self.db = wa._open_store(self.cfg)
        version = self.db.execute(
            "SELECT value FROM meta WHERE key = 'schema'").fetchone()["value"]
        note = self.db.execute(
            "SELECT value FROM meta WHERE key = 'rebuild_incomplete'").fetchone()
        self.assertEqual(version, "0", "the version must not claim a finished rebuild")
        self.assertIsNotNone(note, "the incomplete rebuild must be recorded")
        self.assertIn("unreadable", note["value"])

    def test_a_store_written_before_versioning_is_rebuilt(self):
        wa._ingest_history(self.db, self.chunk(text="the real text"),
                           self.home, "RECENT")
        with wa._writing(self.db):
            self.db.execute("UPDATE messages SET text = 'corrupted'")
            self.db.execute("DELETE FROM meta WHERE key = 'schema'")
        self.db.close()
        self.db = wa._open_store(self.cfg)
        self.assertEqual(
            self.db.execute("SELECT text FROM messages").fetchone()["text"],
            "the real text")


if __name__ == "__main__":
    unittest.main()
