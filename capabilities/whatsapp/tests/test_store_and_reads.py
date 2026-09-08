#!/usr/bin/env python3
"""What the capability decides without a network: how the protocol's values are
read, how the store is written, and what a read is allowed to leave unsaid.

Every case here runs with no account, no engine and no dependency to install.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

wa = _cli.load()


class Timestamps(unittest.TestCase):
    """History chunks carry seconds and live events carry milliseconds. Reading
    one as the other misplaces every message by five decades, silently."""

    def test_seconds_are_kept(self):
        self.assertEqual(wa._ts_seconds(1788864505), 1788864505)

    def test_milliseconds_become_seconds(self):
        self.assertEqual(wa._ts_seconds(1788864505000), 1788864505)

    def test_absent_and_unparsable_are_none(self):
        self.assertIsNone(wa._ts_seconds(0))
        self.assertIsNone(wa._ts_seconds(None))
        self.assertIsNone(wa._ts_seconds("later"))


class ChatIdentifiers(unittest.TestCase):
    def test_bare_number_becomes_a_jid(self):
        self.assertEqual(wa._normalize_chat_id("+372 5555 5555"),
                         "37255555555@s.whatsapp.net")

    def test_the_c_us_form_is_accepted(self):
        self.assertEqual(wa._normalize_chat_id("15551234567@c.us"),
                         "15551234567@s.whatsapp.net")

    def test_groups_and_lids_are_left_alone(self):
        self.assertEqual(wa._normalize_chat_id("120363@g.us"), "120363@g.us")
        self.assertEqual(wa._normalize_chat_id("2539788@lid"), "2539788@lid")

    def test_a_device_suffix_is_dropped(self):
        self.assertEqual(wa._bare_jid("3725258198:33@s.whatsapp.net"),
                         "3725258198@s.whatsapp.net")

    def test_an_empty_chat_id_is_refused(self):
        with self.assertRaises(wa._Refusal) as caught:
            wa._normalize_chat_id("")
        self.assertEqual(caught.exception.exit_code, 6)


class EngineSelection(unittest.TestCase):
    """Which engine answers is the connection's declaration, and an entry that
    carries an address is a bridge entry whether or not it said so."""

    def test_silence_means_the_engine_the_cli_carries(self):
        self.assertEqual(wa._entry_engine({}), ("whatsmeow", None, False))

    def test_an_address_derives_the_bridge(self):
        self.assertEqual(wa._entry_engine({"base_url": "https://example"}),
                         ("waha", "GOWS", False))

    def test_a_named_engine_is_taken_at_its_word(self):
        self.assertEqual(wa._entry_engine({"engine": "waha"}), ("waha", "GOWS", True))
        self.assertEqual(wa._entry_engine({"engine": "GOWS"}), ("waha", "GOWS", True))
        self.assertEqual(
            wa._entry_engine({"engine": "waha", "waha_engine": "webjs"}),
            ("waha", "WEBJS", True))

    def test_an_unknown_engine_survives_to_be_refused_by_name(self):
        self.assertEqual(wa._entry_engine({"engine": "pigeon"}), ("pigeon", None, True))

    def test_an_address_with_the_in_house_engine_is_incoherent(self):
        _cfg, problem = wa._build_cfg("x", {"engine": "whatsmeow",
                                            "base_url": "https://example"})
        self.assertEqual(problem["code"], "engine_mismatch")


class MessageTypes(unittest.TestCase):
    def test_voice_is_distinguished_from_an_audio_file(self):
        self.assertEqual(wa._canonical_type("audioMessage", 1), "ptt")
        self.assertEqual(wa._canonical_type("audioMessage", 0), "audio")

    def test_both_text_forms_read_as_text(self):
        self.assertEqual(wa._canonical_type("conversation", 0), "text")
        self.assertEqual(wa._canonical_type("extendedTextMessage", 0), "text")

    def test_a_message_that_could_not_be_decrypted_says_so(self):
        self.assertEqual(wa._canonical_type("placeholderMessage", 0), "undecryptable")

    def test_an_unmapped_kind_keeps_its_own_name(self):
        self.assertEqual(wa._canonical_type("weirdMessage", 0), "weird")


class HandlerFaults(unittest.TestCase):
    """An exception inside an event handler dies at the callback boundary and
    looks exactly like an idle server. It is recorded and printed, never lost."""

    def test_a_raising_handler_is_recorded_and_printed(self):
        session = wa.Session.__new__(wa.Session)
        session.errors = []
        import contextlib
        import io
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            session._guard(lambda event: 1 / 0)(None, None)
        self.assertEqual(len(session.errors), 1)
        self.assertIn("ZeroDivisionError", session.errors[0])
        self.assertIn("engine handler failed", captured.getvalue())
        self.assertIn("Traceback", captured.getvalue())

    def test_recorded_faults_are_raised_to_the_caller(self):
        session = wa.Session.__new__(wa.Session)
        session.errors = ["RuntimeError: boom"]
        with self.assertRaises(wa._Refusal) as caught:
            session.report_handler_errors()
        self.assertEqual(caught.exception.exit_code, 5)


class StoreWrites(unittest.TestCase):
    """Two threads write here — the caller's and the engine's — so a write is
    serialised and whole or it is not applied."""

    def setUp(self):
        self.cfg = {"id": "test", "home": tempfile.mkdtemp()}
        self.db = wa._open_store(self.cfg)

    def tearDown(self):
        self.db.close()

    def test_a_nested_write_commits_once_with_its_caller(self):
        with wa._writing(self.db):
            self.db.execute("INSERT INTO chats (id) VALUES ('a@g.us')")
            with wa._writing(self.db):
                self.db.execute("INSERT INTO chats (id) VALUES ('b@g.us')")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM chats").fetchone()[0], 2)

    def test_a_failed_write_leaves_nothing_behind(self):
        with self.assertRaises(RuntimeError):
            with wa._writing(self.db):
                self.db.execute("INSERT INTO chats (id) VALUES ('c@g.us')")
                raise RuntimeError("interrupted")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM chats").fetchone()[0], 0)

    def test_a_new_store_records_the_parser_that_built_it(self):
        held = self.db.execute(
            "SELECT value FROM meta WHERE key = 'schema'").fetchone()
        self.assertEqual(held["value"], wa.STORE_VERSION)

    def test_capture_fills_gaps_and_never_overwrites(self):
        row = {"chat_id": "a@g.us", "id": "m1", "ts": 100, "text": "first",
               "sync_type": "RECENT"}
        with wa._writing(self.db):
            self.assertTrue(wa._upsert_message(self.db, row))
            self.assertFalse(wa._upsert_message(
                self.db, {**row, "text": "second", "push_name": "<PERSON>"}))
        held = self.db.execute(
            "SELECT text, push_name FROM messages WHERE id = 'm1'").fetchone()
        self.assertEqual(held["text"], "first")
        self.assertEqual(held["push_name"], "<PERSON>")

    def test_a_rebuild_is_authoritative_over_what_it_replays(self):
        row = {"chat_id": "a@g.us", "id": "m1", "ts": 100, "text": "misparsed"}
        with wa._writing(self.db):
            wa._upsert_message(self.db, row)
            wa._upsert_message(self.db, {**row, "text": "correct"},
                               authoritative=True)
        self.assertEqual(self.db.execute(
            "SELECT text FROM messages WHERE id = 'm1'").fetchone()["text"],
            "correct")


class Coverage(unittest.TestCase):
    """Whether the store can answer the window asked for, counted inside that
    window: recent depth says nothing about the fifty before a named date."""

    def setUp(self):
        self.cfg = {"id": "test", "home": tempfile.mkdtemp()}
        self.db = wa._open_store(self.cfg)
        with wa._writing(self.db):
            wa._upsert_chat(self.db, "a@g.us", None, 9000)
            for mid, ts in (("m1", 1000), ("m2", 9000)):
                wa._upsert_message(self.db, {"chat_id": "a@g.us", "id": mid, "ts": ts})

    def tearDown(self):
        self.db.close()

    def test_an_until_bound_is_counted_inside_the_window(self):
        held = wa._coverage(self.db, "a@g.us", limit=2, from_ts=None, to_ts=5000)
        self.assertEqual(held["held"], 1)
        self.assertFalse(held["covered"])

    def test_a_limit_is_covered_by_what_is_held(self):
        self.assertTrue(wa._coverage(
            self.db, "a@g.us", limit=2, from_ts=None, to_ts=None)["covered"])

    def test_the_stores_true_reach_is_reported_beside_the_windows(self):
        held = wa._coverage(self.db, "a@g.us", limit=2, from_ts=None, to_ts=5000)
        self.assertEqual(held["oldest"], 1000)
        self.assertEqual(held["oldest_overall"], 1000)

    def test_the_protocols_own_floor_covers_any_window(self):
        self.db.execute("UPDATE chats SET end_of_history = 1 WHERE id = 'a@g.us'")
        self.assertTrue(wa._coverage(
            self.db, "a@g.us", limit=999, from_ts=None, to_ts=None)["covered"])
        self.db.execute("UPDATE chats SET end_of_history = 0 WHERE id = 'a@g.us'")
        self.assertFalse(wa._coverage(
            self.db, "a@g.us", limit=999, from_ts=None, to_ts=None)["covered"])


class ReachVerdict(unittest.TestCase):
    """A window the caller asked for and did not get must not read as one that
    was answered."""

    SHORT = {"covered": False, "held": 5, "oldest": 1700000000,
             "oldest_overall": 1600000000}

    def verdict(self, reach, coverage=None, from_ts=None):
        try:
            wa._reach_verdict(reach, 5, "x@g.us", coverage or self.SHORT,
                              from_ts=from_ts)
            return "answered"
        except wa._Refusal as refusal:
            return refusal.code

    def test_an_unreachable_phone_is_its_own_failure(self):
        self.assertEqual(
            self.verdict({"ok": False, "reason": "phone_unreachable"}),
            "phone_unreachable")

    def test_a_round_cap_short_of_a_named_date_is_a_shortfall(self):
        self.assertEqual(self.verdict({"ok": True, "rounds": 2}, from_ts=1),
                         "window_incomplete")

    def test_a_stalled_reach_is_reported_as_itself(self):
        self.assertEqual(
            self.verdict({"ok": True, "stalled": True, "rounds": 1}, from_ts=1),
            "reach_stalled")

    def test_a_limit_is_a_cap_the_caller_set_not_a_promise(self):
        self.assertEqual(self.verdict({"ok": True, "rounds": 2}), "answered")

    def test_a_chat_at_its_floor_is_a_complete_answer(self):
        self.assertEqual(self.verdict({"ok": True, "floor": True}, from_ts=1),
                         "answered")

    def test_a_covered_window_needs_no_verdict(self):
        self.assertEqual(
            self.verdict({"ok": False, "reason": "phone_unreachable"},
                         coverage={"covered": True, "held": 5}),
            "answered")


class AccountBinding(unittest.TestCase):
    """One store holds one account. The connection may pin it; the store pins it
    in any case, from the first account it was written for."""

    def verdict(self, stored, authorises_as, expected=None):
        cfg = {"id": "test", "home": tempfile.mkdtemp(),
               "expected_account_id": expected}
        session = wa.Session.__new__(wa.Session)
        session.cfg = cfg
        session.db = wa._open_store(cfg)
        if stored:
            with wa._writing(session.db):
                session.db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES "
                    "('account_id', ?)", (stored,))
        session.account = lambda: {"id": authorises_as, "jid": None,
                                   "lid": None, "name": None}
        try:
            session.require_account()
            held = session.db.execute(
                "SELECT value FROM meta WHERE key = 'account_id'").fetchone()
            return "accepted", held["value"]
        except wa._Refusal as refusal:
            return "refused", refusal.code
        finally:
            session.db.close()

    def test_a_fresh_store_adopts_the_account_it_is_written_for(self):
        self.assertEqual(self.verdict(None, "15551234567"),
                         ("accepted", "15551234567"))

    def test_the_same_account_is_accepted(self):
        self.assertEqual(self.verdict("15551234567", "15551234567"),
                         ("accepted", "15551234567"))

    def test_a_second_account_is_refused(self):
        self.assertEqual(self.verdict("15551234567", "15559999999"),
                         ("refused", "store_account_mismatch"))

    def test_the_connections_own_binding_answers_first(self):
        self.assertEqual(self.verdict(None, "15559999999", expected="15551234567"),
                         ("refused", "account_identity_mismatch"))


class ExportEnvelope(unittest.TestCase):
    """An export names one schema version, so every message in it answers to
    that version — including one carried forward from an older file."""

    def test_the_published_example_is_built_by_the_producer(self):
        contract = wa.cmd_contract()
        self.assertEqual(contract["schema_version"], wa.SCHEMA_VERSION)
        message = contract["message"]
        self.assertEqual(set(message["from"]), {"id", "lid", "name", "from_me"})
        self.assertEqual(message["type"], "ptt")
        self.assertEqual(message["media"]["state"], "fetched")

    def test_a_carried_over_message_gains_the_current_keys(self):
        old = {"id": "m1", "timestamp": 1, "type": "image",
               "from": {"id": "15551234567@s.whatsapp.net", "name": "<PERSON>",
                        "from_me": False},
               "media": {"filename": "old.jpg",
                         "download_error": "403: gone from the store"}}
        current = wa._as_current_envelope(old)
        self.assertIn("lid", current["from"])
        self.assertIsNone(current["from"]["lid"])
        self.assertEqual(current["media"]["state"], "expired")
        self.assertEqual(current["media"]["error"], "403: gone from the store")
        self.assertNotIn("download_error", current["media"])
        self.assertIn("reply_to", current)
        self.assertIn("transcription", current)

    def test_a_fetched_attachment_carries_forward_as_fetched(self):
        current = wa._as_current_envelope(
            {"id": "m2", "media": {"filename": "a.jpg", "file_path": "/tmp/a.jpg"}})
        self.assertEqual(current["media"]["state"], "fetched")

    def test_the_original_is_not_mutated(self):
        old = {"id": "m3", "media": {"fetch_error": "boom"}}
        wa._as_current_envelope(old)
        self.assertIn("fetch_error", old["media"])


class SenderIdentities(unittest.TestCase):
    """A LID identifies a person without naming them, so both forms are kept and
    the addressable one is preferred."""

    def index(self):
        return {"100@lid": {"name": "<PERSON>", "jid": "15551234567@s.whatsapp.net"}}

    def row(self, **overrides):
        base = {"chat_id": "g@g.us", "id": "m", "sender": None, "sender_lid": None,
                "from_me": 0, "ts": 1, "kind": "conversation", "text": "hi",
                "push_name": None, "quoted_id": None, "mimetype": None,
                "seconds": None, "is_voice": 0, "file_name": None,
                "file_length": None, "direct_path": None, "media_path": None,
                "media_state": None, "media_error": None, "transcript": None,
                "transcript_language": None, "transcript_confidence": None,
                "transcript_model": None, "transcript_provider": None,
                "transcript_at": None, "transcript_error": None}
        base.update(overrides)
        return base

    def test_a_lid_resolves_to_the_phone_form_when_one_is_known(self):
        self.assertEqual(
            wa._sender_id(self.row(sender_lid="100@lid"), self.index(), True),
            "15551234567@s.whatsapp.net")

    def test_an_unresolvable_lid_is_still_returned(self):
        self.assertEqual(
            wa._sender_id(self.row(sender_lid="999@lid"), {}, True), "999@lid")

    def test_a_direct_message_is_answered_for_by_its_chat(self):
        self.assertEqual(
            wa._sender_id(self.row(chat_id="15551234567@s.whatsapp.net"), {}, False),
            "15551234567@s.whatsapp.net")

    def test_our_own_message_has_no_sender_to_address(self):
        self.assertIsNone(wa._sender_id(self.row(from_me=1), {}, False))

    def test_the_envelope_keeps_both_forms(self):
        envelope = wa._envelope(self.row(sender_lid="100@lid"), "<CHAT>", True,
                                self.index())
        self.assertEqual(envelope["from"]["id"], "15551234567@s.whatsapp.net")
        self.assertEqual(envelope["from"]["lid"], "100@lid")
        self.assertEqual(envelope["from"]["name"], "<PERSON>")


class Batching(unittest.TestCase):
    def test_parameters_are_batched_so_a_long_chat_cannot_exhaust_them(self):
        self.assertEqual([list(b) for b in wa._batched(range(5), size=2)],
                         [[0, 1], [2, 3], [4]])
        self.assertEqual(list(wa._batched([])), [])


class RawChunkNames(unittest.TestCase):
    def test_the_sync_type_is_recoverable_from_the_file_name(self):
        self.assertEqual(
            wa._raw_sync_type(Path("20260908T143536-ab12cd34-RECENT.pb")), "RECENT")
        self.assertEqual(
            wa._raw_sync_type(Path("00001-INITIAL_BOOTSTRAP.pb")), "INITIAL_BOOTSTRAP")


class AtomicWrites(unittest.TestCase):
    def test_a_failed_write_leaves_the_previous_file_intact(self):
        target = Path(tempfile.mkdtemp()) / "messages.json"
        wa._write_json(target, {"messages": ["first"]})

        class Unserialisable:
            pass

        with self.assertRaises(TypeError):
            wa._write_json(target, {"messages": [Unserialisable()]})
        self.assertEqual(target.read_text(), '{\n  "messages": [\n    "first"\n  ]\n}')
        self.assertEqual(list(target.parent.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
