#!/usr/bin/env python3
"""The one-time import of a capture an earlier version kept in a SQLite file.

The fixture is a messages.db built with that version's own schema, so the
import is checked against the file it will meet rather than a description of
it. The rows land in a throwaway Postgres named by WHATSAPP_TEST_DSN; without
one the store-backed cases skip.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

wa = _cli.load()

# The schema the capability wrote before its store moved to Postgres, verbatim.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY, name TEXT, is_group INTEGER DEFAULT 0,
  last_ts INTEGER, unread INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
  end_of_history INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  chat_id TEXT NOT NULL, id TEXT NOT NULL,
  sender TEXT, sender_lid TEXT, from_me INTEGER DEFAULT 0, ts INTEGER,
  kind TEXT, text TEXT, push_name TEXT, quoted_id TEXT,
  mimetype TEXT, seconds INTEGER, is_voice INTEGER DEFAULT 0,
  file_name TEXT, file_length INTEGER, direct_path TEXT,
  media_key BLOB, file_enc_sha256 BLOB, file_sha256 BLOB,
  sync_type TEXT, captured_at TEXT,
  PRIMARY KEY (chat_id, id));
CREATE INDEX IF NOT EXISTS messages_by_time ON messages(chat_id, ts);
CREATE TABLE IF NOT EXISTS enrichment (
  chat_id TEXT NOT NULL, id TEXT NOT NULL,
  media_path TEXT, media_state TEXT, media_error TEXT,
  transcript TEXT, transcript_language TEXT, transcript_confidence REAL,
  transcript_model TEXT, transcript_provider TEXT, transcript_at TEXT,
  transcript_error TEXT, effective_text TEXT, updated_at TEXT,
  PRIMARY KEY (chat_id, id));
CREATE TABLE IF NOT EXISTS identities (
  jid TEXT PRIMARY KEY, lid TEXT, name TEXT, push_name TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS identities_by_lid ON identities(lid);
CREATE TABLE IF NOT EXISTS chunks (
  n INTEGER PRIMARY KEY AUTOINCREMENT, sync_type TEXT, conversations INTEGER,
  messages INTEGER, progress INTEGER, at TEXT, raw_file TEXT);
"""

ACCOUNT = "15551234567"
GROUP = "120363000000000003@g.us"
DIRECT = "15559876543@s.whatsapp.net"
LID = "300@lid"


def build_legacy(path: Path, *, account: str | None = ACCOUNT) -> None:
    """A legacy capture holding one of everything the import carries."""
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.executescript(LEGACY_SCHEMA)
        meta = [("schema", "1"), ("contacts_copied_at", "1700000000.5")]
        if account:
            meta.append(("account_id", account))
        db.executemany("INSERT INTO meta VALUES (?, ?)", meta)
        db.executemany(
            "INSERT INTO chats VALUES (?,?,?,?,?,?,?,?)",
            [(GROUP, "<GROUP>", 1, 1700000300, 2, 0, 1, "2023-11-14T22:13:20+00:00"),
             (DIRECT, None, 0, 1700000100, 0, 1, 0, "2023-11-14T22:13:20+00:00")])
        db.executemany(
            "INSERT INTO messages (chat_id, id, sender, sender_lid, from_me, ts,"
            " kind, text, push_name, is_voice, seconds, mimetype, direct_path,"
            " media_key, file_enc_sha256, file_sha256, file_length, sync_type,"
            " captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(GROUP, "g1", None, LID, 0, 1700000200, "conversation", "old one",
              "<PERSON>", 0, None, None, None, None, None, None, None, "RECENT",
              "2023-11-14T22:13:20+00:00"),
             (GROUP, "g2", None, None, 1, 1700000300, "audioMessage", None, None,
              1, 4, "audio/ogg", "/v/a", b"\x01", b"\x02", b"\x03", 99, "RECENT",
              "2023-11-14T22:13:20+00:00"),
             (DIRECT, "d1", None, None, 0, 1700000100, "conversation", "hello",
              None, 0, None, None, None, None, None, None, None, "INITIAL_BOOTSTRAP",
              "2023-11-14T22:13:20+00:00")])
        db.executemany(
            "INSERT INTO identities VALUES (?,?,?,?,?)",
            [(DIRECT, None, "<CONTACT>", None, "2023-11-14T22:13:20+00:00"),
             ("15550001111@s.whatsapp.net", LID, None, "<PERSON>",
              "2023-11-14T22:13:20+00:00")])
        db.execute(
            "INSERT INTO enrichment (chat_id, id, media_state, media_error,"
            " transcript, transcript_language, transcript_confidence,"
            " transcript_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (GROUP, "g2", "expired", "410", "spoken words", "en", 0.75,
             "2023-11-15T10:00:00.250000+00:00", "2023-11-15T10:00:00+00:00"))
        db.executemany(
            "INSERT INTO chunks (sync_type, conversations, messages, progress, at,"
            " raw_file) VALUES (?,?,?,?,?,?)",
            [("INITIAL_BOOTSTRAP", 2, 2, 50, "2023-11-14T22:13:20+00:00",
              "00001-INITIAL_BOOTSTRAP.pb"),
             ("RECENT", 1, 2, None, "2023-11-14T22:14:00+00:00",
              "20231114T221400-ab12cd34-RECENT.pb")])
        db.commit()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LegacyCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = _cli.store_cfg(expected_account_id=ACCOUNT, engine="whatsmeow",
                                  mode="read", messages_dir=None)
        self.legacy = Path(self.cfg["home"]) / "messages.db"

    def run_import(self, **values):
        return wa._wm_import_legacy(self.cfg, types.SimpleNamespace(
            file=values.get("file")))

    def store(self):
        db = wa._open_store(self.cfg)
        self.addCleanup(db.close)
        return db

    def count(self, db, table: str) -> int:
        return db.execute(f"SELECT COUNT(*) FROM whatsapp_{table} WHERE account = %s",
                          (db.account,)).fetchone()[0]


@_cli.needs_store
class Import(LegacyCase):
    """Everything the legacy file holds reaches the account's rows."""

    def setUp(self):
        super().setUp()
        build_legacy(self.legacy)

    def test_every_table_is_carried_into_the_account(self):
        result = self.run_import()
        self.assertEqual(result["legacy"], {"chats": 2, "messages": 3,
                                            "identities": 2, "enrichment": 1,
                                            "chunks": 2})
        self.assertEqual(result["imported"], {"chats": 2, "messages": 3,
                                              "identities": 2, "enrichment": 1,
                                              "chunks": 2, "meta": 2})
        db = self.store()
        for table, n in (("chats", 2), ("messages", 3), ("identities", 2),
                         ("enrichment", 1), ("chunks", 2)):
            self.assertEqual(self.count(db, table), n, table)
        self.assertEqual(wa._get_meta(db, "account_id"), ACCOUNT)
        self.assertEqual(wa._get_meta(db, "contacts_copied_at"), "1700000000.5")
        self.assertEqual(wa._get_meta(db, "schema"), wa.STORE_VERSION)

    def test_imported_rows_read_as_captured_ones(self):
        self.run_import()
        db = self.store()
        chat = wa._chat_row(db, GROUP)
        self.assertEqual((chat["name"], chat["is_group"], chat["last_ts"],
                          chat["unread"], chat["end_of_history"]),
                         ("<GROUP>", 1, 1700000300, 2, 1))
        messages = wa._read_messages(db, GROUP, limit=None, from_ts=None, to_ts=None)
        self.assertEqual([m["id"] for m in messages], ["g1", "g2"])
        self.assertEqual(messages[0]["from"]["id"], "15550001111@s.whatsapp.net")
        self.assertEqual(messages[1]["type"], "ptt")
        self.assertEqual(messages[1]["media"]["state"], "expired")
        self.assertEqual(messages[1]["transcription"]["text"], "spoken words")
        self.assertEqual(messages[1]["transcription"]["transcribed_at"],
                         "2023-11-15T10:00:00.250000+00:00")
        raw = db.execute(
            "SELECT media_key, file_length FROM whatsapp_messages"
            " WHERE account = %s AND id = 'g2'", (db.account,)).fetchone()
        self.assertEqual((raw["media_key"], raw["file_length"]), (b"\x01", 99))
        self.assertEqual(wa._store_facts(self.cfg)["chunks"], 2)

    def test_a_second_run_imports_nothing_and_changes_nothing(self):
        self.run_import()
        db = self.store()
        tables = ("chats", "messages", "identities", "enrichment", "chunks", "meta")

        def snapshot():
            return {t: [tuple(r) for r in db.execute(
                f"SELECT * FROM whatsapp_{t} WHERE account = %s ORDER BY 2, 3",
                (db.account,))] for t in tables}

        before = snapshot()
        again = self.run_import()
        self.assertEqual(set(again["imported"].values()), {0})
        self.assertEqual(snapshot(), before)

    def test_the_source_file_is_left_exactly_as_it_was(self):
        def journal():
            return sorted(p.name for p in self.legacy.parent.iterdir()
                          if p.name.startswith("messages.db"))

        before = digest(self.legacy)
        self.run_import()
        self.run_import()
        self.assertEqual(digest(self.legacy), before)
        self.assertEqual(journal(), ["messages.db"])

    def test_a_named_file_is_read_instead(self):
        other = Path(tempfile.mkdtemp()) / "copy.db"
        other.write_bytes(self.legacy.read_bytes())
        self.legacy.unlink()
        self.assertEqual(self.run_import(file=str(other))["imported"]["messages"], 3)


@_cli.needs_store
class TheLiveStoreWins(LegacyCase):
    """The store is the newer record: what it holds stands, and only gaps in
    identities and enrichment are filled from the file."""

    def setUp(self):
        super().setUp()
        build_legacy(self.legacy)
        db = self.store()
        with wa._writing(db):
            wa._upsert_chat(db, GROUP, "<NEWER NAME>", 1700009999)
            wa._upsert_message(db, {"chat_id": GROUP, "id": "g1", "ts": 1700000200,
                                    "text": "newer text", "sender_lid": LID})
            wa._remember_identity(db, DIRECT, "400@lid", name=None,
                                  push_name="<NEWER PUSH>")
            wa._record_media(db, GROUP, "g2", path="/held/a.ogg", state="fetched")
            wa._set_meta(db, "account_id", ACCOUNT)
            wa._set_meta(db, "contacts_copied_at", "1800000000.0")
        self.db = db

    def test_rows_already_held_are_not_overwritten(self):
        result = self.run_import()
        self.assertEqual(result["imported"]["chats"], 1)
        self.assertEqual(result["imported"]["messages"], 2)
        chat = wa._chat_row(self.db, GROUP)
        self.assertEqual((chat["name"], chat["last_ts"]),
                         ("<NEWER NAME>", 1700009999))
        held = self.db.execute(
            "SELECT text FROM whatsapp_messages WHERE account = %s AND id = 'g1'",
            (self.db.account,)).fetchone()
        self.assertEqual(held["text"], "newer text")
        self.assertEqual(wa._get_meta(self.db, "contacts_copied_at"), "1800000000.0")

    def test_identities_and_enrichment_fill_only_what_is_missing(self):
        self.run_import()
        identity = self.db.execute(
            "SELECT lid, name, push_name FROM whatsapp_identities"
            " WHERE account = %s AND jid = %s", (self.db.account, DIRECT)).fetchone()
        self.assertEqual(tuple(identity), ("400@lid", "<CONTACT>", "<NEWER PUSH>"))
        enrichment = self.db.execute(
            "SELECT media_path, media_state, media_error, transcript"
            " FROM whatsapp_enrichment WHERE account = %s AND id = 'g2'",
            (self.db.account,)).fetchone()
        self.assertEqual(tuple(enrichment),
                         ("/held/a.ogg", "fetched", "410", "spoken words"))


@_cli.needs_store
class Refusals(LegacyCase):

    def test_no_file_is_not_found(self):
        with self.assertRaises(wa._Refusal) as caught:
            self.run_import()
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (3, "legacy_store_not_found"))

    def test_a_file_that_is_not_a_legacy_store_is_refused(self):
        self.legacy.write_bytes(b"not a database at all" * 100)
        before = digest(self.legacy)
        with self.assertRaises(wa._Refusal) as caught:
            self.run_import()
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (6, "legacy_store_unreadable"))
        self.assertEqual(digest(self.legacy), before)

    def test_a_database_without_the_legacy_tables_is_refused(self):
        with contextlib.closing(sqlite3.connect(self.legacy)) as db:
            db.execute("CREATE TABLE other (x)")
            db.commit()
        with self.assertRaises(wa._Refusal) as caught:
            self.run_import()
        self.assertEqual(caught.exception.code, "legacy_store_unreadable")

    def test_another_accounts_history_is_refused(self):
        build_legacy(self.legacy, account="15550002222")
        with self.assertRaises(wa._Refusal) as caught:
            self.run_import()
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (2, "store_account_mismatch"))
        self.assertEqual(self.count(self.store(), "messages"), 0)


class NoStore(unittest.TestCase):
    def test_no_store_setting_is_refused_before_the_file_is_read(self):
        home = Path(tempfile.mkdtemp())
        build_legacy(home / "messages.db")
        env = {k: v for k, v in os.environ.items() if k != "CAPABILITIES_STORE_URL"}
        env["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(wa._Refusal) as caught:
                wa._wm_import_legacy({"id": "test", "home": str(home)},
                                     types.SimpleNamespace(file=None))
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (6, "store_not_configured"))


if __name__ == "__main__":
    unittest.main()
