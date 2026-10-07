#!/usr/bin/env python3
"""Sending: every message is a row before it is anything else, whoever holds
the connection sends it, and what became of it is written on that row. Also
the capture spool that keeps what arrives while the store is down.

The engine's client is stood in for, so nothing here reaches WhatsApp. The
store-backed cases need a throwaway Postgres named by WHATSAPP_TEST_DSN and
skip without one; the cases that build protocol messages need the engine's
message definitions and skip where the engine does not load.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

wa = _cli.load()
ENGINE = _cli.engine_available(wa)
needs_engine = unittest.skipUnless(ENGINE, "the engine does not load on this host")

CHAT = "15550001111@s.whatsapp.net"
OWN = "15550000000@s.whatsapp.net"


def _request(text="hello", **extra) -> dict:
    return {"chat_id": CHAT, "text": text, "reply_to": None, "mentions": [],
            "typing": False, **extra}


class FakeClient:
    """The engine's client as a sender sees it: it records what it was asked
    to do and answers with an id of its own."""

    def __init__(self, *, fail=None, before_answer=None):
        self.calls: list = []
        self.sent: list = []
        self.fail = fail
        self.before_answer = before_answer
        self.minted = 0

    def send_chat_presence(self, to, state, media):
        self.calls.append(("presence", state.name))

    def send_message(self, to, message):
        self.calls.append(("send",))
        if self.fail:
            raise RuntimeError(self.fail)
        self.minted += 1
        minted = f"WA{self.minted:04d}{os.getpid()}"
        self.sent.append(message)
        if self.before_answer:
            self.before_answer(minted)
        return types.SimpleNamespace(ID=minted, Timestamp=1788864505000)


class Sender:
    """A connected session reduced to what sending touches."""

    def __init__(self, db, client=None):
        self.db = db
        self._client = client or FakeClient()

    def account(self):
        return {"jid": OWN}


class StoreCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = _cli.store_cfg(engine=wa.ENGINE_INHOUSE)
        self.db = wa._open_store(self.cfg)
        self.addCleanup(self.db.close)

    def rows(self, where="TRUE", params=()):
        return self.db.execute(
            f"SELECT * FROM whatsapp_messages WHERE account = %s AND {where}"
            " ORDER BY requested_at NULLS FIRST, id",
            (self.db.account, *params)).fetchall()

    def captured(self, message_id, text="earlier", from_me=0, sender=None):
        wa._write_live(self.db, {
            "chat_id": CHAT, "id": message_id, "from_me": from_me,
            "sender": sender, "ts": 1788864000, "kind": "conversation",
            "text": text, "sync_type": "LIVE", "captured_at": wa._iso_now()})


@_cli.needs_store
class Schema(StoreCase):
    """The delivery columns arrive as a step of their own, and what was
    captured before reads back as it did."""

    def test_the_delivery_steps_are_in_the_ledger_at_the_new_minor(self):
        steps = [r["step"] for r in self.db.execute(
            "SELECT step FROM schema_ledger WHERE owner = 'whatsapp'"
            " ORDER BY step").fetchall()]
        self.assertEqual(steps[-3:], ["0010-messages-delivery",
                                      "0011-messages-by-local-id",
                                      "0012-messages-outbound"])
        version = self.db.execute(
            "SELECT major, minor FROM schema_version WHERE owner = 'whatsapp'"
        ).fetchone()
        self.assertEqual((version["major"], version["minor"]), (1, 1))

    def test_a_captured_message_reads_back_as_before(self):
        self.captured("m1")
        row = self.rows()[0]
        self.assertIsNone(row["delivery"])
        self.assertIsNone(row["local_id"])
        read = wa._read_messages(self.db, CHAT, limit=None, from_ts=None,
                                 to_ts=None)
        self.assertEqual([m["id"] for m in read], ["m1"])

    def test_a_message_not_yet_sent_is_not_part_of_the_conversation(self):
        self.captured("m1")
        queued = wa._queue_outgoing(self.db, _request())
        read = wa._read_messages(self.db, CHAT, limit=None, from_ts=None,
                                 to_ts=None)
        self.assertEqual([m["id"] for m in read], ["m1"])
        self.assertEqual(wa._coverage(self.db, CHAT, limit=None,
                                      from_ts=None)["held"], 1)
        self.assertEqual(queued["delivery"], "pending")


@_cli.needs_store
class Rows(StoreCase):
    """A send is a row: pending, claimed once, and finished as sent or failed."""

    def test_a_queued_message_is_pending_with_an_internal_id(self):
        row = wa._queue_outgoing(self.db, _request())
        self.assertEqual(row["delivery"], "pending")
        self.assertTrue(row["local_id"].startswith("out-"))
        self.assertEqual(row["id"], row["local_id"])
        self.assertEqual(row["from_me"], 1)
        self.assertEqual(row["sync_type"], wa.OUTBOUND_SYNC)

    def test_a_message_is_claimed_once_and_in_order(self):
        first = wa._queue_outgoing(self.db, _request("one"))
        second = wa._queue_outgoing(self.db, _request("two"))
        self.assertEqual(wa._claim_outgoing(self.db)["local_id"], first["local_id"])
        self.assertEqual(wa._claim_outgoing(self.db)["local_id"], second["local_id"])
        self.assertIsNone(wa._claim_outgoing(self.db))

    def test_sent_takes_whatsapps_id_and_its_echo_adds_no_row(self):
        row = wa._queue_outgoing(self.db, _request())
        wa._claim_outgoing(self.db, row["local_id"])
        wa._apply_delivery(self.db, {"local_id": row["local_id"], "state": "sent",
                                     "message_id": "WAX1", "timestamp": 1788864505})
        self.captured("WAX1", text="hello", from_me=1, sender=OWN)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["delivery"], rows[0]["local_id"]),
                         ("WAX1", "sent", row["local_id"]))
        self.assertEqual(rows[0]["sender"], OWN)
        self.assertEqual(rows[0]["ts"], 1788864505)

    def test_an_echo_that_arrives_first_takes_the_delivery(self):
        row = wa._queue_outgoing(self.db, _request())
        wa._claim_outgoing(self.db, row["local_id"])
        self.captured("WAX2", text="hello", from_me=1, sender=OWN)
        wa._apply_delivery(self.db, {"local_id": row["local_id"], "state": "sent",
                                     "message_id": "WAX2", "timestamp": 1788864505})
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["delivery"], rows[0]["local_id"]),
                         ("WAX2", "sent", row["local_id"]))

    def test_an_outcome_written_twice_changes_nothing(self):
        row = wa._queue_outgoing(self.db, _request())
        outcome = {"local_id": row["local_id"], "state": "sent",
                   "message_id": "WAX3", "timestamp": 1788864505}
        wa._apply_delivery(self.db, outcome)
        wa._apply_delivery(self.db, {**outcome, "state": "failed", "error": "x"})
        self.assertEqual(self.rows()[0]["delivery"], "sent")

    def test_a_quote_of_a_message_the_store_lacks_is_refused_unwritten(self):
        with self.assertRaises(wa._Refusal) as caught:
            wa._queue_outgoing(self.db, _request(reply_to="nope"))
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (3, "quoted_not_found"))
        self.assertEqual(self.rows(), [])


@_cli.needs_store
@needs_engine
class DirectSend(StoreCase):
    """Without a service the invocation sends the row itself."""

    def _session_class(self, client):
        test = self

        class Direct(Sender):
            def __init__(self, cfg, props=None):
                super().__init__(wa._open_store(cfg), client)
                test.session = self
                self.errors = []

            def open(self, expect_session=True):
                return self

            def require_connected(self):
                pass

            def require_account(self):
                return {"id": "15550000000"}

            def drain(self, **kwargs):
                pass

            def refresh_identities(self):
                return 0

            def report_handler_errors(self):
                pass

            def close(self):
                self.db.close()
        return Direct

    def test_pending_then_sent_with_the_minted_id_and_no_second_row(self):
        client = FakeClient(before_answer=lambda minted: None)
        seen = {}

        def look(minted):
            seen["state"] = self.rows()[0]["delivery"]
        client.before_answer = look
        with mock.patch.object(wa, "Session", self._session_class(client)):
            result = wa._send_direct(self.cfg, _request())
        self.assertEqual(seen["state"], "sending")
        self.assertEqual(result["delivery"], "sent")
        self.assertEqual(result["sent_by"], "direct")
        self.assertTrue(result["message_id"].startswith("WA"))
        self.assertEqual(result["chat"], CHAT)
        self.assertEqual(result["length"], 5)
        self.captured(result["message_id"], text="hello", from_me=1, sender=OWN)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], result["message_id"])

    def test_a_failed_send_is_a_failed_row_and_exit_5(self):
        client = FakeClient(fail="recipient does not exist")
        with mock.patch.object(wa, "Session", self._session_class(client)):
            with self.assertRaises(wa._Refusal) as caught:
                wa._send_direct(self.cfg, _request())
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (5, "send_failed"))
        row = self.rows()[0]
        self.assertEqual(row["delivery"], "failed")
        self.assertIn("recipient does not exist", row["delivery_error"])


@_cli.needs_store
class ServiceSend(StoreCase):
    """While the service holds the account, `send` leaves the row to it and
    returns the service's answer."""

    def _listener(self, outcome):
        def run():
            other = wa._open_store(self.cfg)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    row = wa._claim_outgoing(other)
                    if row is not None:
                        wa._apply_delivery(other, {"local_id": row["local_id"],
                                                   **outcome})
                        return
                    time.sleep(0.05)
            finally:
                other.close()
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 5)

    def test_the_services_answer_is_returned(self):
        self._listener({"state": "sent", "message_id": "WAS1",
                        "timestamp": 1788864505})
        with mock.patch.object(wa, "OUTBOUND_POLL", 0.05):
            result = wa._send_via_service(self.cfg, _request(), wait=10)
        self.assertEqual((result["message_id"], result["delivery"],
                          result["sent_by"]), ("WAS1", "sent", "service"))
        self.assertEqual(result["timestamp"], 1788864505)

    def test_the_services_failure_is_exit_5(self):
        self._listener({"state": "failed", "error": "no route"})
        with mock.patch.object(wa, "OUTBOUND_POLL", 0.05):
            with self.assertRaises(wa._Refusal) as caught:
                wa._send_via_service(self.cfg, _request(), wait=10)
        self.assertEqual(caught.exception.code, "send_failed")
        self.assertIn("no route", caught.exception.message)

    def test_no_answer_in_time_reports_the_row_still_pending(self):
        with mock.patch.object(wa, "OUTBOUND_POLL", 0.05):
            with self.assertRaises(wa._Refusal) as caught:
                wa._send_via_service(self.cfg, _request(), wait=0.2)
        row = self.rows()[0]
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (5, "send_pending"))
        self.assertIn(row["local_id"], caught.exception.message)
        self.assertEqual(row["delivery"], "pending")

    def test_send_hands_off_when_a_service_owns_the_account(self):
        args = types.SimpleNamespace(chat_id=CHAT, text="hi", reply_to=None,
                                     mentions=None, typing=False)
        with mock.patch.object(wa, "_service_owner", return_value={"pid": 1}), \
                mock.patch.object(wa, "_send_direct") as direct, \
                mock.patch.object(wa, "_send_via_service",
                                  return_value={"ok": True}) as via:
            wa._wm_send(self.cfg, args)
        direct.assert_not_called()
        via.assert_called_once()

    def test_a_service_that_started_meanwhile_gets_the_row(self):
        args = types.SimpleNamespace(chat_id=CHAT, text="hi", reply_to=None,
                                     mentions=None, typing=False)
        owners = iter([None, {"pid": 1}])
        busy = wa._Refusal(8, "session_busy", "held")
        with mock.patch.object(wa, "_service_owner",
                               side_effect=lambda paths: next(owners)), \
                mock.patch.object(wa, "_send_direct", side_effect=busy), \
                mock.patch.object(wa, "_send_via_service",
                                  return_value={"ok": True}) as via:
            wa._wm_send(self.cfg, args)
        via.assert_called_once()


class Requests(unittest.TestCase):
    """What a send asks for is settled before anything is written."""

    def _args(self, **values):
        base = {"chat_id": "+1 555 000 1111", "text": "hi", "reply_to": None,
                "mentions": None, "typing": False}
        return types.SimpleNamespace(**{**base, **values})

    def test_mentions_become_jids_and_lead_the_text_when_absent(self):
        request = wa._outgoing_request(self._args(
            text="see @15550002222", mentions=["+1 555 000 2222", "15550003333"]))
        self.assertEqual(request["mentions"], ["15550002222@s.whatsapp.net",
                                               "15550003333@s.whatsapp.net"])
        self.assertEqual(request["text"], "@15550003333 see @15550002222")
        self.assertEqual(request["chat_id"], CHAT)

    def test_a_bad_mention_is_refused(self):
        with self.assertRaises(wa._Refusal) as caught:
            wa._outgoing_request(self._args(mentions=["nobody"]))
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (6, "bad_mention"))

    def test_an_empty_text_is_refused(self):
        with self.assertRaises(wa._Refusal) as caught:
            wa._outgoing_request(self._args(text=" "))
        self.assertEqual(caught.exception.code, "empty_message")


@_cli.needs_store
@needs_engine
class Building(StoreCase):
    """The protocol message each row becomes, as the engine is handed it."""

    def test_plain_text_is_a_plain_conversation(self):
        row = wa._queue_outgoing(self.db, _request())
        message = wa._outgoing_message(self.db, row, OWN)
        self.assertEqual(message.conversation, "hello")
        self.assertFalse(message.HasField("extendedTextMessage"))

    def test_a_reply_quotes_the_message_it_names(self):
        self.captured("Q1", text="the question", sender=CHAT)
        row = wa._queue_outgoing(self.db, _request("the answer", reply_to="Q1"))
        message = wa._outgoing_message(self.db, row, OWN)
        ext = message.extendedTextMessage
        self.assertEqual(ext.text, "the answer")
        self.assertEqual(ext.contextInfo.stanzaID, "Q1")
        self.assertEqual(ext.contextInfo.participant, CHAT)
        self.assertEqual(ext.contextInfo.quotedMessage.conversation, "the question")

    def test_a_reply_to_our_own_message_names_the_account(self):
        self.captured("Q2", text="mine", from_me=1, sender=OWN)
        row = wa._queue_outgoing(self.db, _request("again", reply_to="Q2"))
        ext = wa._outgoing_message(self.db, row, OWN).extendedTextMessage
        self.assertEqual(ext.contextInfo.participant, OWN)

    def test_mentions_are_carried_as_mentioned_jids(self):
        request = wa._outgoing_request(types.SimpleNamespace(
            chat_id=CHAT, text="hi", reply_to=None,
            mentions=["15550002222"], typing=False))
        row = wa._queue_outgoing(self.db, request)
        ext = wa._outgoing_message(self.db, row, OWN).extendedTextMessage
        self.assertEqual(list(ext.contextInfo.mentionedJID),
                         ["15550002222@s.whatsapp.net"])
        self.assertEqual(ext.text, "@15550002222 hi")

    def test_typing_shows_composing_then_paused_then_sends(self):
        row = wa._queue_outgoing(self.db, _request(typing=True))
        claimed = wa._claim_outgoing(self.db)
        session = Sender(self.db)
        pauses = []
        outcome = wa._deliver(session, claimed, pause=pauses.append,
                              jitter=lambda a, b: (a + b) / 2)
        self.assertEqual(session._client.calls, [
            ("presence", "CHAT_PRESENCE_COMPOSING"),
            ("presence", "CHAT_PRESENCE_PAUSED"), ("send",)])
        self.assertEqual(pauses, [sum(wa.TYPING_SECONDS) / 2])
        self.assertEqual(outcome["state"], "sent")
        self.assertEqual(outcome["local_id"], row["local_id"])

    def test_the_typing_moment_is_jittered_within_its_bounds(self):
        wa._queue_outgoing(self.db, _request(typing=True))
        claimed = wa._claim_outgoing(self.db)
        pauses = []
        wa._deliver(Sender(self.db), claimed, pause=pauses.append)
        low, high = wa.TYPING_SECONDS
        self.assertTrue(low <= pauses[0] <= high)

    def test_without_typing_no_presence_is_sent(self):
        wa._queue_outgoing(self.db, _request())
        session = Sender(self.db)
        wa._deliver(session, wa._claim_outgoing(self.db))
        self.assertEqual(session._client.calls, [("send",)])


def _daemon(cfg, settings=None):
    daemon = wa._ServiceDaemon(cfg, Path(tempfile.mkdtemp()),
                               settings or {"environment": "test"},
                               launch_nonce="nonce-1")
    daemon.log = lambda message: None
    return daemon


@_cli.needs_store
@needs_engine
class Listener(StoreCase):
    """The listener sends what waits for its account, within its rate, and
    never sends twice."""

    def test_the_rate_caps_sends_per_minute_and_the_rest_wait(self):
        for n in range(5):
            wa._queue_outgoing(self.db, _request(f"m{n}"))
        daemon = _daemon(self.cfg, {"send_rate": 2})
        now = [1000.0]
        daemon.clock = lambda: now[0]
        session = Sender(self.db)
        daemon.maintain_store(self.db)
        self.assertEqual(daemon.deliver_pending(session, self.db), 2)
        self.assertEqual(daemon.deliver_pending(session, self.db), 0)
        now[0] += 30
        self.assertEqual(daemon.deliver_pending(session, self.db), 0)
        self.assertEqual(len(self.rows("delivery = 'pending'")), 3)
        now[0] += 31
        self.assertEqual(daemon.deliver_pending(session, self.db), 2)
        now[0] += 61
        self.assertEqual(daemon.deliver_pending(session, self.db), 1)
        sent = [r["text"] for r in self.rows("delivery = 'sent'")]
        self.assertEqual(sorted(sent), ["m0", "m1", "m2", "m3", "m4"])
        self.assertEqual(len(session._client.sent), 5)
        self.assertEqual(daemon.health["messages_sent"], 5)

    def test_the_default_rate_applies_without_a_setting(self):
        self.assertEqual(_daemon(self.cfg).send_rate(), wa.SEND_RATE_DEFAULT)

    def test_a_send_left_sending_is_failed_at_start_and_never_resent(self):
        row = wa._queue_outgoing(self.db, _request("half-sent"))
        wa._claim_outgoing(self.db)
        daemon = _daemon(self.cfg)
        session = Sender(self.db)
        self.assertEqual(daemon.deliver_pending(session, self.db), 0)
        daemon.maintain_store(self.db)
        failed = self.rows("local_id = %s", (row["local_id"],))[0]
        self.assertEqual((failed["delivery"], failed["delivery_error"]),
                         ("failed", wa.DELIVERY_INTERRUPTED))
        self.assertEqual(daemon.deliver_pending(session, self.db), 0)
        self.assertEqual(session._client.calls, [])

    def test_nothing_is_sent_before_interrupted_sends_are_settled(self):
        wa._queue_outgoing(self.db, _request())
        daemon = _daemon(self.cfg)
        self.assertEqual(daemon.deliver_pending(Sender(self.db), self.db), 0)


class Outage:
    """Makes a store fail the way an unreachable one does, on demand."""

    def __init__(self, db):
        import psycopg
        self.down = False
        real = db.execute

        def execute(sql, params=None):
            if self.down:
                raise psycopg.OperationalError("server closed the connection")
            return real(sql, params)
        db.execute = execute


@_cli.needs_store
class Spool(StoreCase):
    """What arrives while the store is down is kept, then written once, in
    order, when it answers."""

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.spool = wa._CaptureSpool(self.dir / "capture-spool.jsonl", self.dir)
        self.outage = Outage(self.db)

    def live(self, message_id, text="x"):
        return {"kind": "live", "row": wa._spool_row({
            "chat_id": CHAT, "id": message_id, "from_me": 0, "ts": 1788864000,
            "kind": "imageMessage", "text": text, "sync_type": "LIVE",
            "media_key": b"\x00\xff", "captured_at": wa._iso_now()})}

    def ids(self):
        return [r["id"] for r in self.rows()]

    def test_an_outage_spools_and_recovery_writes_each_once_in_order(self):
        self.assertEqual(self.spool.capture(self.db, self.live("a")), 1)
        self.outage.down = True
        for message_id in ("b", "c"):
            self.assertIsNone(self.spool.capture(self.db, self.live(message_id)))
        self.assertEqual(self.spool.depth(), 2)
        self.assertIn("OperationalError", self.spool.store_error)
        self.outage.down = False
        # The store answers again, but a newer capture still queues behind the
        # older ones rather than overtaking them.
        self.assertIsNone(self.spool.capture(self.db, self.live("d")))
        self.assertEqual(self.ids(), ["a"])
        order = []
        real = wa._apply_spooled

        def applying(db, home, record, data=None):
            order.append(record["row"]["id"])
            return real(db, home, record, data)
        with mock.patch.object(wa, "_apply_spooled", side_effect=applying):
            result = self.spool.replay(self.db)
        self.assertEqual(order, ["b", "c", "d"])
        self.assertEqual(result, {"written": 3, "rejected": 0, "left": 0})
        self.assertEqual(sorted(self.ids()), ["a", "b", "c", "d"])
        self.assertFalse(self.spool.path.exists())
        self.assertIsNone(self.spool.store_error)
        media = self.rows("id = 'b'")[0]["media_key"]
        self.assertEqual(media, b"\x00\xff")

    def test_a_replay_cut_short_resumes_without_duplicates(self):
        self.outage.down = True
        for message_id in ("a", "b", "c"):
            self.spool.capture(self.db, self.live(message_id))
        kept = self.spool.path.read_text()
        self.outage.down = False
        calls = [0]
        real = wa._apply_spooled

        def flaky(db, home, record, data=None):
            calls[0] += 1
            if calls[0] == 3:
                import psycopg
                raise psycopg.OperationalError("gone again")
            return real(db, home, record, data)
        with mock.patch.object(wa, "_apply_spooled", side_effect=flaky):
            result = self.spool.replay(self.db)
        self.assertEqual(result["left"], 1)
        # A crash between writing and rewriting the file: everything replays.
        self.spool.path.write_text(kept)
        self.spool = wa._CaptureSpool(self.spool.path, self.dir)
        self.assertEqual(self.spool.depth(), 3)
        self.spool.replay(self.db)
        self.assertEqual(sorted(self.ids()), ["a", "b", "c"])
        self.assertEqual(self.spool.depth(), 0)

    def test_a_capture_refused_for_its_content_is_not_spooled(self):
        bad = self.live("a")
        bad["row"]["chat_id"] = None
        with self.assertRaises(Exception):
            self.spool.capture(self.db, bad)
        self.assertEqual(self.spool.depth(), 0)

    def test_a_kept_capture_refused_for_its_content_is_set_aside(self):
        self.outage.down = True
        bad = self.live("a")
        bad["row"]["chat_id"] = None
        self.spool.capture(self.db, bad)
        self.spool.capture(self.db, self.live("b"))
        self.outage.down = False
        result = self.spool.replay(self.db)
        self.assertEqual((result["written"], result["rejected"], result["left"]),
                         (1, 1, 0))
        self.assertEqual(self.ids(), ["b"])
        rejected = [json.loads(line) for line in
                    self.spool.rejected_path.read_text().splitlines()]
        self.assertEqual(len(rejected), 1)

    def test_a_delivery_answer_is_kept_through_an_outage(self):
        row = wa._queue_outgoing(self.db, _request())
        wa._claim_outgoing(self.db)
        self.outage.down = True
        self.spool.capture(self.db, {"kind": "delivery", "outcome": {
            "local_id": row["local_id"], "state": "sent", "message_id": "WAO1",
            "timestamp": 1788864505}})
        self.outage.down = False
        self.spool.replay(self.db)
        self.assertEqual(self.rows()[0]["delivery"], "sent")
        self.assertEqual(self.rows()[0]["id"], "WAO1")

    def test_the_listener_reconnects_replays_and_reports_the_depth(self):
        daemon = _daemon(self.cfg)
        daemon.spool = self.spool
        self.outage.down = True
        self.spool.capture(self.db, self.live("a"))
        self.spool.capture(self.db, self.live("b"))
        daemon.maintain_store(self.db)
        self.assertEqual(daemon.health["spool_depth"], 2)
        self.assertIn("OperationalError", daemon.health["store_error"])
        self.assertGreater(daemon.next_store_attempt, 0)
        self.outage.down = False
        daemon.next_store_attempt = 0
        # The connection the outage left behind is replaced, not reused.
        with mock.patch.object(type(self.db), "broken",
                               side_effect=[True, False, False, False, False]):
            daemon.maintain_store(self.db)
        self.assertEqual(daemon.health["spool_depth"], 0)
        self.assertIsNone(daemon.health["store_error"])
        self.assertEqual(sorted(self.ids()), ["a", "b"])
        self.assertTrue(daemon.recovered)

    def test_status_reports_the_spool_depth(self):
        cfg = {**self.cfg}
        paths = wa._service_paths(cfg)
        spool = wa._CaptureSpool(paths["spool"], paths["home"])
        self.outage.down = True
        spool.capture(self.db, self.live("a"))
        with mock.patch.object(wa, "_service_project_root", return_value=Path("/p")), \
                mock.patch.object(wa, "_service_settings", return_value={}), \
                mock.patch.object(wa, "_service_cfg", return_value=cfg):
            status = wa._cmd_service_status(None)
        self.assertEqual(status["spool_depth"], 1)
        self.assertFalse(status["running"])


class SendRateSetting(unittest.TestCase):
    def test_a_rate_in_range_is_accepted(self):
        self.assertEqual(wa._validate_service_settings({"send_rate": 5}),
                         {"send_rate": 5})
        wa._validate_service_settings({"send_rate": None})

    def test_a_rate_out_of_range_or_not_whole_is_refused(self):
        for bad in (0, wa.SEND_RATE_CEILING + 1, 2.5, "10", True):
            with self.assertRaisesRegex(ValueError, "send_rate"):
                wa._validate_service_settings({"send_rate": bad})


class WriteGate(unittest.TestCase):
    """A connection that may not write is refused before any row exists."""

    def test_send_on_a_read_only_connection_exits_4_unwritten(self):
        reg = {"default": "ro", "connections": {"ro": {"number": "15550000000"}}}
        argv = ["whatsapp", "send", CHAT, "hello", "--reply-to", "X",
                "--mention", "15550002222", "--typing"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(wa, "_gate"), \
                mock.patch.object(wa, "_contract"), \
                mock.patch.object(wa, "read_only_switch", return_value=False), \
                mock.patch.object(wa, "_connections_registry",
                                  return_value=(reg, None)), \
                mock.patch.object(wa, "_open_store") as store, \
                mock.patch.object(wa, "_queue_outgoing") as queue, \
                mock.patch.object(wa, "_session_lock") as lock, \
                mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                wa.main()
        self.assertEqual(caught.exception.code, 4)
        store.assert_not_called()
        queue.assert_not_called()
        lock.assert_not_called()


@_cli.needs_store
@needs_engine
class HeldConnection(StoreCase):
    """The same paths, driven the way the running listener drives them."""

    def event(self, message_id, text):
        from neonize.proto import Neonize_pb2 as proto
        e2e = wa._engine()["e2e"]
        user, _, server = CHAT.partition("@")
        return proto.Message(
            Info=proto.MessageInfo(
                MessageSource=proto.MessageSource(
                    Chat=proto.JID(User=user, Server=server),
                    Sender=proto.JID(User=user, Server=server), IsFromMe=False),
                ID=message_id, Timestamp=1788864505000),
            Message=e2e.Message(conversation=text))

    def test_messages_arriving_in_an_outage_reach_the_store_once_in_order(self):
        session = wa.Session.__new__(wa.Session)
        session.db = self.db
        session.live_messages = 0
        paths = wa._service_paths(self.cfg)
        session.spool = wa._CaptureSpool(paths["spool"], paths["home"])
        outage = Outage(self.db)
        session._on_message(self.event("E1", "before"))
        outage.down = True
        session._on_message(self.event("E2", "during"))
        session._on_message(self.event("E3", "during"))
        self.assertEqual(session.spool.depth(), 2)
        outage.down = False
        self.assertEqual([r["id"] for r in self.rows()], ["E1"])
        session._on_message(self.event("E4", "after"))
        self.assertEqual(session.spool.depth(), 3)
        session.spool.replay(self.db)
        session.spool.replay(self.db)
        rows = self.db.execute(
            "SELECT id, text FROM whatsapp_messages WHERE account = %s"
            " ORDER BY captured_at, id", (self.db.account,)).fetchall()
        self.assertEqual([r["id"] for r in rows], ["E1", "E2", "E3", "E4"])
        self.assertEqual(session.live_messages, 1)

    def test_the_running_listener_sends_what_waits(self):
        db = self.db

        class Held:
            def __init__(self):
                self.db = db
                self._client = FakeClient()
                self._client.is_connected = True
                self._logged_out = threading.Event()
                self._disconnected = threading.Event()
                self.errors, self.live_messages = [], 0
                self.offline_count = 0
                self.last_event_at = self.last_message_at = None
                self.stalled = False

            def open(self):
                return self

            def wait_connected(self, timeout=60):
                return True

            def require_account(self):
                return {"id": "15550000000"}

            def account(self):
                return {"jid": OWN}

            def refresh_identities(self):
                return 0

            def close(self):
                pass
        held = Held()
        stale = wa._queue_outgoing(db, _request("before the crash"))
        wa._claim_outgoing(db)
        for n in range(3):
            wa._queue_outgoing(db, _request(f"m{n}"))
        daemon = wa._ServiceDaemon(self.cfg, Path(tempfile.mkdtemp()), {},
                                   launch_nonce="n", session_factory=lambda: held)
        daemon.log = lambda message: None
        thread = threading.Thread(target=daemon.loop)
        with mock.patch.object(wa, "SERVICE_TICK", 0.01):
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and daemon.sent < 3:
                time.sleep(0.02)
            daemon.stop_requested.set()
            thread.join(5)
        self.assertEqual(daemon.sent, 3)
        states = {r["text"]: r["delivery"] for r in self.rows()}
        self.assertEqual(states, {"before the crash": "failed", "m0": "sent",
                                  "m1": "sent", "m2": "sent"})
        self.assertEqual(len(held._client.sent), 3)
        self.assertEqual(self.rows("local_id = %s", (stale["local_id"],))[0]
                         ["delivery_error"], wa.DELIVERY_INTERRUPTED)


if __name__ == "__main__":
    unittest.main()
