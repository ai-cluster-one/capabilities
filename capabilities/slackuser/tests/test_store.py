#!/usr/bin/env python3
"""The store the capture writes and the reads answer from: round-trips, the
dedup that makes a redelivery harmless, edits, deletions and reactions, the
incremental-sync cursor, and the reconnect that must lose and double nothing.

These cases need a throwaway Postgres named by SLACKUSER_TEST_DSN and skip
without one. Each writes under an account of its own."""

from __future__ import annotations

import argparse
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


def _ts(i: int) -> str:
    """The i-th message's ts, fixed-width as Slack's are."""
    return f"{1700000000 + i}.000100"


class _FakeClient(su.SlackClient):
    """A Slack session with no socket, paged by the capability's own
    SlackClient.history: it answers conversations.history from a canned
    per-channel history as Slack does - newest first, `oldest` and `latest`
    exclusive, at most `limit` per page behind a next_cursor."""

    def __init__(self, history: dict[str, list[dict]]):
        self._history = history

    def set(self, channel: str, messages: list[dict]) -> None:
        self._history[channel] = messages

    def _call(self, method: str, **kwargs) -> dict:
        assert method == "conversations.history", method
        oldest, latest = kwargs.get("oldest"), kwargs.get("latest")
        pool = sorted(
            (m for m in self._history.get(kwargs["channel"], [])
             if (oldest is None or float(m["ts"]) > float(oldest))
             and (latest is None or float(m["ts"]) < float(latest))),
            key=lambda m: -float(m["ts"]))
        start = int(kwargs.get("cursor") or 0)
        end = start + kwargs["limit"]
        more = end < len(pool)
        return {"ok": True, "messages": pool[start:end], "has_more": more,
                "response_metadata": {"next_cursor": str(end) if more else ""}}


class _Budgeted(_FakeClient):
    """A Slack session that refuses conversations.history as rate-limited
    once `budget` calls are spent."""

    budget = 10**6

    def _call(self, method: str, **kwargs) -> dict:
        if self.budget <= 0:
            su._refuse(5, "rate_limited", "Slack rate-limited the session")
        self.budget -= 1
        return super()._call(method, **kwargs)


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
        self.cfg = _cli.store_cfg()
        self.db = su._open_store(self.cfg)
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

    def test_a_gap_larger_than_the_limit_is_closed_whole(self):
        # More messages arrive between two syncs than one sync's limit.
        client = _FakeClient({"C1": [_msg(_ts(i)) for i in range(1, 11)]})
        su._sync_channel(self.db, client, "C1", full=False, limit=1000)
        client.set("C1", [_msg(_ts(i)) for i in range(1, 1211)])
        su._sync_channel(self.db, client, "C1", full=False, limit=1000)
        self.assertEqual(self._count(), 1210)
        self.assertEqual(su._store_covers_newest(self.db, "C1"), _ts(1210))

    def test_a_reconnect_backfill_after_a_long_drop_loses_nothing(self):
        client = _FakeClient({"C1": [_msg(_ts(i)) for i in range(1, 11)]})
        su._sync_channel(self.db, client, "C1", full=False, limit=500)
        # 600 messages arrive while the capture is disconnected.
        client.set("C1", [_msg(_ts(i)) for i in range(1, 611)])
        su._sync_channel(self.db, client, "C1", full=False, limit=500)
        self.assertEqual(self._count(), 610)

    def test_a_limited_fresh_read_leaves_the_cursor_for_the_next_sync(self):
        client = _FakeClient({"C1": [_msg(_ts(i)) for i in range(1, 11)]})
        su._sync_channel(self.db, client, "C1", full=False, limit=1000)
        client.set("C1", [_msg(_ts(i)) for i in range(1, 61)])
        args = argparse.Namespace(channel="C1", fresh=True, limit=5,
                                  oldest=None, latest=None, connection=None)
        with mock.patch.object(su, "get_config", return_value=self.cfg), \
                mock.patch.object(su, "_open_store", return_value=_Unclosed(self.db)), \
                mock.patch.object(su, "SlackClient", return_value=client):
            su.cmd_history(args)
        # The five newest are stored, but they do not join the covered range.
        self.assertEqual(su._store_covers_newest(self.db, "C1"), _ts(10))
        su._sync_channel(self.db, client, "C1", full=False, limit=1000)
        self.assertEqual(self._count(), 60)
        self.assertEqual(su._store_covers_newest(self.db, "C1"), _ts(60))

    def test_a_gap_deeper_than_the_rate_budget_closes_over_rounds(self):
        client = _Budgeted({c: [_msg(_ts(i)) for i in range(10)]
                            for c in ("C1", "C2", "C3")})
        for c in ("C1", "C2", "C3"):
            su._sync_channel(self.db, client, c, full=False, limit=1000)
        # A drop: C1 falls 3000 behind, C2 and C3 50 each; Slack allows
        # 10 history calls per backfill round.
        client.set("C1", [_msg(_ts(i)) for i in range(3010)])
        client.set("C2", [_msg(_ts(i)) for i in range(60)])
        client.set("C3", [_msg(_ts(i)) for i in range(60)])
        for _ in range(4):
            client.budget = 10
            for c in ("C1", "C2", "C3"):
                try:
                    su._sync_channel(self.db, client, c, full=False, limit=500)
                except su._Refusal:
                    pass
        self.assertEqual(self._count("C1"), 3010)
        self.assertEqual(self._count("C2"), 60)
        self.assertEqual(self._count("C3"), 60)
        self.assertEqual(su._store_covers_newest(self.db, "C1"), _ts(3009))
        self.assertEqual(su._open_gap(self.db, "C1"), (None, None))


class _Unclosed:
    """The case's store handed to a verb that closes what it opened."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
