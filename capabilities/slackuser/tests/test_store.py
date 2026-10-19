#!/usr/bin/env python3
"""The store the capture writes and the reads answer from: round-trips, the
dedup that makes a redelivery harmless, edits, deletions and reactions, the
incremental-sync cursor, and the reconnect that must lose and double nothing.

These cases need a throwaway Postgres named by SLACKUSER_TEST_DSN and skip
without one. Each writes under an account of its own."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

su = _cli.load()


def _msg(ts: str, text: str = "hi", **extra) -> dict:
    return {"type": "message", "ts": ts, "user": "U1", "text": text, **extra}


class _FakeClient:
    """A Slack session with no socket: it serves a canned per-channel history,
    respecting `oldest` as Slack does (exclusive), so a sync asks it only for
    what is newer than the stored cursor."""

    def __init__(self, history: dict[str, list[dict]]):
        self._history = history

    def set(self, channel: str, messages: list[dict]) -> None:
        self._history[channel] = messages

    def history(self, channel, oldest=None, latest=None, limit=200, pages=10):
        for msg in sorted(self._history.get(channel, []), key=lambda m: float(m["ts"])):
            if oldest is not None and float(msg["ts"]) <= float(oldest):
                continue
            if latest is not None and float(msg["ts"]) > float(latest):
                continue
            yield msg


@_cli.needs_store
class StoreRoundTrip(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = _cli.store_cfg()
        self.db = su._open_store(self.cfg)
        self.addCleanup(self.db.close)

    def _count(self, channel="C1", deleted=None):
        q = ("SELECT count(*) AS n FROM slackuser_messages "
             "WHERE account = %s AND channel_id = %s")
        p = [self.db.account, channel]
        if deleted is not None:
            q += " AND deleted = %s"
            p.append(deleted)
        return self.db.execute(q, p).fetchone()["n"]

    def test_the_schema_version_is_recorded(self):
        self.assertEqual(su._get_meta(self.db, "schema"), su.STORE_VERSION)

    def test_two_accounts_never_see_each_others_rows(self):
        su._upsert_message(self.db, "C1", _msg("1.0"), "live", authoritative=True)
        other = su._open_store(_cli.store_cfg())
        self.addCleanup(other.close)
        self.assertEqual(self._count(), 1)
        self.assertEqual(
            other.execute("SELECT count(*) AS n FROM slackuser_messages "
                          "WHERE account = %s", (other.account,)).fetchone()["n"], 0)

    def test_a_message_reads_back_with_its_fields(self):
        su._upsert_message(self.db, "C1",
                           _msg("100.000100", "hello", thread_ts="90.0"),
                           "live", authoritative=True)
        rows = su._store_messages(self.db, "C1", 10, None, None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "hello")
        self.assertEqual(rows[0]["thread_ts"], "90.0")
        self.assertIsNotNone(rows[0]["ts_at"])

    def test_a_redelivered_message_is_not_doubled(self):
        self.assertTrue(su._upsert_message(self.db, "C1", _msg("1.0"), "live",
                                           authoritative=True))
        self.assertFalse(su._upsert_message(self.db, "C1", _msg("1.0"), "live",
                                            authoritative=True))
        self.assertEqual(self._count(), 1)

    def test_history_never_clobbers_a_live_row(self):
        su._upsert_message(self.db, "C1", _msg("1.0", "live text"), "live",
                           authoritative=True)
        # A later backfill of the same ts must not overwrite the live content.
        inserted = su._upsert_message(self.db, "C1", _msg("1.0", "stale"),
                                      "backfill", authoritative=False)
        self.assertFalse(inserted)
        rows = su._store_messages(self.db, "C1", 10, None, None)
        self.assertEqual(rows[0]["text"], "live text")

    def test_an_edit_replaces_the_text(self):
        su._upsert_message(self.db, "C1", _msg("1.0", "before"), "live",
                           authoritative=True)
        su._apply_edit(self.db, "C1",
                       _msg("1.0", "after", edited={"ts": "2.0", "user": "U1"}))
        rows = su._store_messages(self.db, "C1", 10, None, None)
        self.assertEqual(rows[0]["text"], "after")
        self.assertTrue(rows[0]["edited"])

    def test_a_deletion_is_a_soft_flag_that_reads_hide(self):
        su._upsert_message(self.db, "C1", _msg("1.0"), "live", authoritative=True)
        su._apply_delete(self.db, "C1", "1.0")
        self.assertEqual(self._count(deleted=True), 1)
        self.assertEqual(su._store_messages(self.db, "C1", 10, None, None), [])

    def test_a_reaction_is_added_and_removed_by_user(self):
        su._upsert_message(self.db, "C1", _msg("1.0"), "live", authoritative=True)
        su._apply_reaction(self.db, "C1", "1.0", "eyes", "U2", added=True)
        su._apply_reaction(self.db, "C1", "1.0", "eyes", "U3", added=True)
        rows = su._store_messages(self.db, "C1", 10, None, None)
        self.assertEqual(sorted(rows[0]["reactions"]["eyes"]), ["U2", "U3"])
        su._apply_reaction(self.db, "C1", "1.0", "eyes", "U2", added=False)
        rows = su._store_messages(self.db, "C1", 10, None, None)
        self.assertEqual(rows[0]["reactions"]["eyes"], ["U3"])

    def test_a_reaction_on_an_unseen_message_is_dropped(self):
        su._apply_reaction(self.db, "C1", "9.0", "eyes", "U2", added=True)
        self.assertEqual(self._count(), 0)


@_cli.needs_store
class EventHandling(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = su._open_store(_cli.store_cfg())
        self.addCleanup(self.db.close)

    def test_a_new_message_event_is_captured(self):
        change = su._handle_event(self.db, {"type": "message", "channel": "C1",
                                            "ts": "1.0", "user": "U1", "text": "hi"})
        self.assertEqual(change["change"], "message")
        self.assertEqual(len(su._store_messages(self.db, "C1", 10, None, None)), 1)

    def test_a_changed_event_edits_in_place(self):
        su._handle_event(self.db, {"type": "message", "channel": "C1",
                                   "ts": "1.0", "text": "before"})
        change = su._handle_event(self.db, {
            "type": "message", "subtype": "message_changed", "channel": "C1",
            "message": {"ts": "1.0", "text": "after",
                        "edited": {"ts": "2.0", "user": "U1"}}})
        self.assertEqual(change["change"], "edit")
        rows = su._store_messages(self.db, "C1", 10, None, None)
        self.assertEqual(rows[0]["text"], "after")

    def test_a_deleted_event_hides_the_message(self):
        su._handle_event(self.db, {"type": "message", "channel": "C1", "ts": "1.0"})
        su._handle_event(self.db, {"type": "message", "subtype": "message_deleted",
                                   "channel": "C1", "deleted_ts": "1.0"})
        self.assertEqual(su._store_messages(self.db, "C1", 10, None, None), [])

    def test_a_reaction_event_updates_the_message(self):
        su._handle_event(self.db, {"type": "message", "channel": "C1", "ts": "1.0"})
        su._handle_event(self.db, {"type": "reaction_added", "user": "U2",
                                   "reaction": "tada",
                                   "item": {"type": "message", "channel": "C1",
                                            "ts": "1.0"}})
        rows = su._store_messages(self.db, "C1", 10, None, None)
        self.assertEqual(rows[0]["reactions"]["tada"], ["U2"])

    def test_an_unknown_event_changes_nothing(self):
        self.assertIsNone(su._handle_event(self.db, {"type": "user_typing"}))


@_cli.needs_store
class IncrementalSyncAndReconnect(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = su._open_store(_cli.store_cfg())
        self.addCleanup(self.db.close)

    def _count(self, channel="C1"):
        return self.db.execute(
            "SELECT count(*) AS n FROM slackuser_messages "
            "WHERE account = %s AND channel_id = %s",
            (self.db.account, channel)).fetchone()["n"]

    def test_the_first_sync_takes_everything_and_sets_the_cursor(self):
        client = _FakeClient({"C1": [_msg("1.0"), _msg("2.0"), _msg("3.0")]})
        result = su._sync_channel(self.db, client, "C1", full=False, limit=100)
        self.assertEqual(result["new"], 3)
        self.assertEqual(self._count(), 3)
        self.assertEqual(su._store_covers_newest(self.db, "C1"), "3.0")

    def test_an_incremental_sync_asks_only_for_what_is_newer(self):
        client = _FakeClient({"C1": [_msg("1.0"), _msg("2.0"), _msg("3.0")]})
        su._sync_channel(self.db, client, "C1", full=False, limit=100)
        # Two new messages arrive while the store held the cursor at 3.0.
        client.set("C1", [_msg("1.0"), _msg("2.0"), _msg("3.0"),
                          _msg("4.0"), _msg("5.0")])
        result = su._sync_channel(self.db, client, "C1", full=False, limit=100)
        # Only 4.0 and 5.0 are fetched (oldest is exclusive), nothing re-stored.
        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["new"], 2)
        self.assertEqual(self._count(), 5)
        self.assertEqual(su._store_covers_newest(self.db, "C1"), "5.0")

    def test_a_reconnect_backfill_closes_the_gap_without_duplicates(self):
        # Live capture wrote 1,2,3 and then the connection dropped.
        for ts in ("1.0", "2.0", "3.0"):
            su._handle_event(self.db, {"type": "message", "channel": "C1", "ts": ts})
        su._advance_cursor(self.db, "C1", "1.0", "3.0")
        # While disconnected, 3 (redelivered), 4 and 5 are on the server.
        client = _FakeClient({"C1": [_msg("3.0"), _msg("4.0"), _msg("5.0")]})
        # The reconnect backfill asks from the cursor and dedups on ts.
        su._sync_channel(self.db, client, "C1", full=False, limit=100)
        self.assertEqual(self._count(), 5)          # 1..5, no gap
        rows = su._store_messages(self.db, "C1", 10, None, None)
        self.assertEqual([r["ts"] for r in rows],
                         ["1.0", "2.0", "3.0", "4.0", "5.0"])   # no duplicate 3.0

    def test_a_full_sync_refetches_from_the_beginning(self):
        su._advance_cursor(self.db, "C1", "10.0", "10.0")
        client = _FakeClient({"C1": [_msg("1.0"), _msg("2.0")]})
        result = su._sync_channel(self.db, client, "C1", full=True, limit=100)
        self.assertEqual(result["new"], 2)          # ignored the cursor


if __name__ == "__main__":
    unittest.main()
