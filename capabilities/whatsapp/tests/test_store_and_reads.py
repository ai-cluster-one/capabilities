#!/usr/bin/env python3
"""What the capability decides without a network: how the protocol's values are
read, how the store is written, and what a read is allowed to leave unsaid.

Every case here runs with no account and no engine. The store-backed cases need
a throwaway Postgres named by WHATSAPP_TEST_DSN and skip without one.
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class StoreCase(unittest.TestCase):
    """A case with a store of its own: the throwaway database, under an account
    no other case writes."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = _cli.store_cfg()
        self.db = wa._open_store(self.cfg)
        self.addCleanup(self.db.close)

    def count(self, table: str) -> int:
        return self.db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE account = %s",
            (self.db.account,)).fetchone()[0]

    def add_chat(self, chat_id: str) -> None:
        self.db.execute(
            "INSERT INTO whatsapp_chats (account, id) VALUES (%s, %s)",
            (self.db.account, chat_id))


class StoreConfiguration(unittest.TestCase):
    """Without a store there is no capture to read or write, and saying so is
    a configuration answer, not an empty one."""

    def test_no_setting_and_no_override_refuses_as_configuration(self):
        with mock.patch.dict(os.environ, _cli.without_db_env(), clear=True):
            with self.assertRaises(wa._Refusal) as caught:
                wa._open_store({"id": "test", "home": tempfile.mkdtemp()})
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertEqual(caught.exception.code, "store_not_configured")
        self.assertTrue(caught.exception.hint)

    def test_a_file_database_is_refused_as_configuration(self):
        # The library refuses a URL that is not PostgreSQL; this is the
        # capability carrying that refusal out as a configuration answer.
        with mock.patch.dict(os.environ, {**_cli.no_db_env(),
                                          "AGENTKIT_DB_URL": "sqlite:///tmp/x.db"}):
            with self.assertRaises(wa._Refusal) as caught:
                wa._open_store({"id": "test", "home": tempfile.mkdtemp()})
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertEqual(caught.exception.code, "store_not_postgres")


def _machine_file(config_home: str, *, port: int | None = None) -> Path:
    """A machine store setting naming the test database by its fields, or the
    same host on a port nothing listens on."""
    import json
    from urllib.parse import parse_qs, urlparse
    url = urlparse(_cli.STORE_DSN)
    body = {"schema": "agentkit.store.v1", "host": url.hostname,
            "port": port or url.port or 5432, "database": url.path.lstrip("/"),
            "user": url.username,
            "sslmode": (parse_qs(url.query).get("sslmode") or ["require"])[-1]}
    if url.password:
        body["password"] = url.password
    path = Path(config_home) / "agentkit" / "store.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    return path


@_cli.needs_store
class ProjectDatabase(unittest.TestCase):
    """The capture follows the project's database: its .env answers before the
    machine file, and a listener keeps what it resolved at launch."""

    def setUp(self):
        env = _cli.no_db_env()
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_home = env["XDG_CONFIG_HOME"]
        self.root = Path(tempfile.mkdtemp())
        rooted = mock.patch.object(wa, "_project_root", return_value=self.root)
        rooted.start()
        self.addCleanup(rooted.stop)
        self.addCleanup(setattr, wa, "_STORE_SETTING_PINNED", None)

    def open(self):
        db = wa._open_store(_cli.store_cfg())
        self.addCleanup(db.close)
        return db

    def test_without_a_project_database_the_machine_file_answers(self):
        machine = _machine_file(self.config_home)
        db = self.open()
        self.assertEqual((db.setting.level, db.setting.sources),
                         ("machine", (str(machine),)))

    def test_a_database_in_the_project_env_wins_over_the_machine_file(self):
        _machine_file(self.config_home, port=1)
        (self.root / ".env").write_text(f"AGENTKIT_DB_URL={_cli.STORE_DSN}\n")
        db = self.open()
        self.assertEqual((db.setting.level, db.setting.sources),
                         ("project", (str(self.root / ".env"),)))
        db.execute("SELECT 1")

    def test_a_listener_keeps_the_database_it_resolved_at_launch(self):
        _machine_file(self.config_home, port=1)
        (self.root / ".env").write_text(f"AGENTKIT_DB_URL={_cli.STORE_DSN}\n")
        wa._pin_store_setting(self.root)
        (self.root / ".env").unlink()
        db = self.open()
        self.assertEqual(db.setting.level, "project")
        db.conn.close()
        db.reconnect()
        self.assertEqual(db.execute("SELECT 1").fetchone()[0], 1)


@_cli.needs_store
class StoreSchema(StoreCase):
    """Every object the capability creates is its own, by name."""

    def test_every_created_object_is_named_for_the_capability(self):
        rows = self.db.execute(
            """SELECT c.relname FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema()""").fetchall()
        platform = ("schema_ledger", "schema_version")
        names = sorted(r["relname"] for r in rows
                       if not r["relname"].startswith(platform))
        self.assertEqual([n for n in names if not n.startswith("whatsapp_")], [])
        self.assertTrue({"whatsapp_chats", "whatsapp_messages",
                         "whatsapp_identities", "whatsapp_meta",
                         "whatsapp_enrichment", "whatsapp_chunks"} <= set(names))
        ledger = self.db.execute(
            "SELECT step FROM schema_ledger WHERE owner = 'whatsapp'"
            " ORDER BY step").fetchall()
        self.assertEqual([r["step"] for r in ledger],
                         [step.id for step in wa.STORE_STEPS])

    def test_two_accounts_never_see_each_others_rows(self):
        with wa._writing(self.db):
            self.add_chat("a@g.us")
        other = wa._open_store(_cli.store_cfg())
        try:
            self.assertIsNone(wa._chat_row(other, "a@g.us"))
            self.assertIsNotNone(wa._chat_row(self.db, "a@g.us"))
        finally:
            other.close()

    def test_times_and_flags_read_back_as_the_readers_expect(self):
        with wa._writing(self.db):
            wa._upsert_chat(self.db, "a@g.us", None, 1788864505,
                            end_of_history=1)
            wa._upsert_message(self.db, {"chat_id": "a@g.us", "id": "m1",
                                         "ts": 1788864505, "from_me": 1,
                                         "is_voice": 0,
                                         "captured_at": wa._iso_now(),
                                         "media_key": b"\x00\x01"})
        chat = wa._chat_row(self.db, "a@g.us")
        self.assertEqual(chat["last_ts"], 1788864505)
        self.assertEqual(chat["end_of_history"], 1)
        self.assertEqual(chat["is_group"], 1)
        message = self.db.execute(
            "SELECT * FROM whatsapp_messages WHERE account = %s AND id = 'm1'",
            (self.db.account,)).fetchone()
        self.assertEqual(message["ts"], 1788864505)
        self.assertEqual((message["from_me"], message["is_voice"]), (1, 0))
        self.assertEqual(message["media_key"], b"\x00\x01")
        datetime.datetime.fromisoformat(message["captured_at"])

    def test_a_chat_keeps_its_latest_activity(self):
        with wa._writing(self.db):
            wa._upsert_chat(self.db, "a@g.us", "First", 2000)
            wa._upsert_chat(self.db, "a@g.us", None, 1000)
        chat = wa._chat_row(self.db, "a@g.us")
        self.assertEqual((chat["name"], chat["last_ts"]), ("First", 2000))


@_cli.needs_store
class StoreWrites(StoreCase):
    """Two threads write here — the caller's and the engine's — so a write is
    serialised and whole or it is not applied."""

    def test_a_nested_write_commits_once_with_its_caller(self):
        with wa._writing(self.db):
            self.add_chat("a@g.us")
            with wa._writing(self.db):
                self.add_chat("b@g.us")
        self.assertEqual(self.count("whatsapp_chats"), 2)

    def test_a_failed_write_leaves_nothing_behind(self):
        with self.assertRaises(RuntimeError):
            with wa._writing(self.db):
                self.add_chat("c@g.us")
                raise RuntimeError("interrupted")
        self.assertEqual(self.count("whatsapp_chats"), 0)

    def test_a_write_from_another_thread_waits_for_the_open_one(self):
        import threading
        done = threading.Event()

        def other():
            with wa._writing(self.db):
                self.add_chat("t@g.us")
            done.set()

        with wa._writing(self.db):
            self.add_chat("s@g.us")
            worker = threading.Thread(target=other)
            worker.start()
            self.assertFalse(done.wait(0.5))
        worker.join(5)
        self.assertTrue(done.is_set())
        self.assertEqual(self.count("whatsapp_chats"), 2)

    def test_a_new_store_records_the_parser_that_built_it(self):
        self.assertEqual(wa._get_meta(self.db, "schema"), wa.STORE_VERSION)

    def test_capture_fills_gaps_and_never_overwrites(self):
        row = {"chat_id": "a@g.us", "id": "m1", "ts": 100, "text": "first",
               "sync_type": "RECENT"}
        with wa._writing(self.db):
            self.assertTrue(wa._upsert_message(self.db, row))
            self.assertFalse(wa._upsert_message(
                self.db, {**row, "text": "second", "push_name": "<PERSON>"}))
        held = self.db.execute(
            "SELECT text, push_name FROM whatsapp_messages"
            " WHERE account = %s AND id = 'm1'", (self.db.account,)).fetchone()
        self.assertEqual(held["text"], "first")
        self.assertEqual(held["push_name"], "<PERSON>")

    def test_a_rebuild_is_authoritative_over_what_it_replays(self):
        row = {"chat_id": "a@g.us", "id": "m1", "ts": 100, "text": "misparsed"}
        with wa._writing(self.db):
            wa._upsert_message(self.db, row)
            wa._upsert_message(self.db, {**row, "text": "correct"},
                               authoritative=True)
        self.assertEqual(self.db.execute(
            "SELECT text FROM whatsapp_messages WHERE account = %s AND id = 'm1'",
            (self.db.account,)).fetchone()["text"],
            "correct")


@_cli.needs_store
class Coverage(StoreCase):
    """Whether the store can answer the window asked for, counted inside that
    window: recent depth says nothing about the fifty before a named date."""

    def setUp(self):
        super().setUp()
        with wa._writing(self.db):
            wa._upsert_chat(self.db, "a@g.us", None, 9000)
            for mid, ts in (("m1", 1000), ("m2", 9000)):
                wa._upsert_message(self.db, {"chat_id": "a@g.us", "id": mid, "ts": ts})

    def set_floor(self, value: bool) -> None:
        self.db.execute(
            "UPDATE whatsapp_chats SET end_of_history = %s"
            " WHERE account = %s AND id = 'a@g.us'", (value, self.db.account))

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
        self.set_floor(True)
        self.assertTrue(wa._coverage(
            self.db, "a@g.us", limit=999, from_ts=None, to_ts=None)["covered"])
        self.set_floor(False)
        self.assertFalse(wa._coverage(
            self.db, "a@g.us", limit=999, from_ts=None, to_ts=None)["covered"])


@_cli.needs_store
class VerbsOnTheStore(StoreCase):
    """The read verbs answer from the store alone when nothing needs reaching
    for, so their whole answer is decided here."""

    GROUP = "120363000000000001@g.us"
    PHONE = "15551234567@s.whatsapp.net"
    LID = "100@lid"

    def setUp(self):
        super().setUp()
        self.cfg.update(engine="whatsmeow", mode="read", messages_dir=None)
        with wa._writing(self.db):
            wa._upsert_chat(self.db, self.GROUP, "<GROUP>", 1000)
            wa._remember_identity(self.db, self.PHONE, self.LID, name="<PERSON>")
            wa._upsert_message(self.db, {
                "chat_id": self.GROUP, "id": "m1", "ts": 1000, "kind": "conversation",
                "text": "one", "sender_lid": self.LID, "from_me": 0})
            wa._upsert_message(self.db, {
                "chat_id": self.GROUP, "id": "m2", "ts": 2000, "kind": "audioMessage",
                "is_voice": 1, "seconds": 3, "mimetype": "audio/ogg",
                "direct_path": "/v/x", "media_key": b"k", "file_enc_sha256": b"e",
                "file_sha256": b"s", "file_length": 10, "from_me": 1})

    def args(self, **values):
        import types
        base = {"json": True, "fresh": False, "search": None, "limit": None,
                "from_date": None, "to_date": None, "rounds": 0,
                "format": "conversation"}
        return types.SimpleNamespace(**{**base, **values})

    def test_chats_lists_what_the_store_holds(self):
        chats = wa._wm_chats(self.cfg, self.args())
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["id"], self.GROUP)
        self.assertEqual((chats[0]["name"], chats[0]["group"]), ("<GROUP>", True))
        self.assertEqual((chats[0]["messages"], chats[0]["oldest"],
                          chats[0]["last_activity"]), (2, 1000, 2000))

    def test_messages_are_read_oldest_first_with_senders_resolved(self):
        messages = wa._wm_messages(self.cfg, self.args(chat_id=self.GROUP))
        self.assertEqual([m["id"] for m in messages], ["m1", "m2"])
        self.assertEqual(messages[0]["from"]["id"], self.PHONE)
        self.assertEqual(messages[0]["from"]["name"], "<PERSON>")
        self.assertEqual(messages[1]["type"], "ptt")
        self.assertTrue(messages[1]["from"]["from_me"])
        day = datetime.datetime.fromtimestamp(2000).strftime("%Y-%m-%d")
        window = wa._wm_messages(self.cfg, self.args(
            chat_id=self.GROUP, from_date=day, to_date=day))
        self.assertIn("m2", [m["id"] for m in window])

    def test_a_limit_keeps_the_newest(self):
        messages = wa._wm_messages(self.cfg, self.args(chat_id=self.GROUP, limit=1))
        self.assertEqual([m["id"] for m in messages], ["m2"])

    def test_contact_answers_under_either_identity(self):
        by_lid = wa._wm_contact(self.cfg, self.args(contact_id=self.LID))
        by_phone = wa._wm_contact(self.cfg, self.args(contact_id=self.PHONE))
        self.assertEqual(by_lid["id"], self.PHONE)
        self.assertEqual((by_phone["lid"], by_phone["name"]), (self.LID, "<PERSON>"))

    def test_enrichment_is_kept_beside_the_message(self):
        wa._record_media(self.db, self.GROUP, "m2", path=None, state="expired",
                         error="410")
        wa._record_transcript(self.db, self.GROUP, "m2", {
            "text": "spoken", "language": "en", "confidence": 0.9,
            "model": "m", "provider": "p",
            "transcribed_at": "2026-01-01T00:00:00.123456+00:00"})
        message = wa._read_messages(self.db, self.GROUP, limit=None,
                                    from_ts=None, to_ts=None)[1]
        self.assertEqual(message["media"]["state"], "expired")
        self.assertEqual(message["transcription"]["text"], "spoken")
        self.assertEqual(message["transcription"]["confidence"], 0.9)
        self.assertEqual(message["transcription"]["transcribed_at"],
                         "2026-01-01T00:00:00.123456+00:00")
        self.assertEqual(wa._pending_media(self.db, self.GROUP, {"ptt"},
                                           [message]), [])

    def test_attachments_not_yet_settled_are_pending(self):
        messages = wa._read_messages(self.db, self.GROUP, limit=None,
                                     from_ts=None, to_ts=None)
        self.assertEqual(wa._pending_media(self.db, self.GROUP, {"ptt"}, messages),
                         ["m2"])

    def test_a_prior_exports_transcriptions_are_adopted_once(self):
        prior = {"messages": [{"id": "m2", "transcription": {"text": "old"}},
                              {"id": "m1"}]}
        self.assertEqual(wa._adopt_prior_transcriptions(self.db, self.GROUP, prior), 1)
        self.assertEqual(wa._adopt_prior_transcriptions(self.db, self.GROUP, prior), 0)

    def test_export_renders_the_store_without_a_connection(self):
        wa._record_media(self.db, self.GROUP, "m2", path=None, state="expired")
        out = Path(tempfile.mkdtemp()) / "messages.json"
        result = wa._wm_export(self.cfg, self.args(
            chat_id=self.GROUP, out=str(out), media_dir=None, no_photos=False,
            no_stickers=False, videos=False, documents=False))
        self.assertEqual(result["message_count"], 2)
        import json
        written = json.loads(out.read_text())
        self.assertEqual([m["id"] for m in written["messages"]], ["m1", "m2"])

    def test_the_facts_status_and_health_report(self):
        facts = wa._store_facts(self.cfg)
        self.assertEqual((facts["chats"], facts["messages"], facts["voice_notes"],
                          facts["with_media"]), (1, 2, 1, 1))
        self.assertEqual(facts["oldest"],
                         datetime.datetime.fromtimestamp(1000).isoformat())
        self.assertIn(self.db.account, facts["location"])
        self.assertIn("Location:", wa._wm_status(self.cfg))


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


@_cli.needs_store
class AccountBinding(unittest.TestCase):
    """One account's capture holds one account. The connection may pin it; the
    store pins it in any case, from the first account it was written for."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)

    def verdict(self, stored, authorises_as, expected=None):
        cfg = _cli.store_cfg(expected_account_id=expected)
        session = wa.Session.__new__(wa.Session)
        session.cfg = cfg
        session.db = wa._open_store(cfg)
        if stored:
            with wa._writing(session.db):
                wa._set_meta(session.db, "account_id", stored)
        session.account = lambda: {"id": authorises_as, "jid": None,
                                   "lid": None, "name": None}
        try:
            session.require_account()
            return "accepted", wa._get_meta(session.db, "account_id")
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
