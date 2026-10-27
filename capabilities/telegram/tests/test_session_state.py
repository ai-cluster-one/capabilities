#!/usr/bin/env python3
"""Whether a connection's session lives on this machine, as `telegram
connections` reports it, and the manifest's word that a connection is a
device-bound session. Everything is read from local files; nothing reaches
Telegram."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_check import import_cli, restore_modules  # noqa: E402


def write_session(stem: Path, *, auth_key: bytes = b"\x01" * 256,
                  signed_in_as: int | None = None) -> Path:
    """A session file in Telethon's own SQLite layout: the auth key in
    `sessions`, and for a signed-in client the row Telethon keeps for itself
    under entity id 0, its account id as the hash."""
    path = stem.with_name(stem.name + ".session")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("create table version (version integer primary key)")
        db.execute("create table sessions (dc_id integer primary key, server_address text,"
                   " port integer, auth_key blob, takeout_id integer, tmp_auth_key blob)")
        db.execute("create table entities (id integer primary key, hash integer not null,"
                   " username text, phone integer, name text, date integer)")
        db.execute("insert into version values (7)")
        db.execute("insert into sessions values (2, '149.154.167.51', 443, ?, null, ?)",
                   (auth_key, b""))
        if signed_in_as is not None:
            db.execute("insert into entities values (0, ?, null, null, null, null)",
                       (signed_in_as,))
    db.close()
    return path


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        self.addCleanup(restore_modules, self.cli)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.state = self.tmp / "state" / "telegram"
        self.conns = {"here": {"expected_account_id": 1001},
                      "absent": {"expected_account_id": 1002},
                      "revoked": {"expected_account_id": 1003},
                      "unsigned": {"expected_account_id": 1004},
                      "unbound": {}}
        reg = {"default": "here", "connections": self.conns,
               "sources": {cid: str(self.tmp / "connections.json") for cid in self.conns}}
        for target, value in (("_connections_registry", lambda: (reg, None)),
                              ("_state_dir", lambda: self.state),
                              ("_STATE_HOME", self.tmp / "state"),
                              ("_resolve_env_key", lambda key: (None, None, None))):
            patcher = mock.patch.object(self.cli, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sessions(self) -> dict:
        report = self.cli._connections_report()
        return {cid: entry["session"] for cid, entry in report["connections"].items()}

    def test_each_connection_says_whether_its_session_is_here(self):
        write_session(self.state / "here" / "session", signed_in_as=1001)
        write_session(self.state / "revoked" / "session", signed_in_as=1003)
        write_session(self.state / "unsigned" / "session")
        write_session(self.state / "unbound" / "session", signed_in_as=1005)
        health = self.state / "revoked" / "service" / "health.json"
        health.parent.mkdir(parents=True)
        # As the daemon records a session Telegram no longer accepts.
        health.write_text(json.dumps({
            "state": "unhealthy", "session_authorized": False,
            "last_error": "session not authorized",
            "auth_session_path": str((self.state / "revoked" / "session").resolve())}))
        self.assertEqual(self.sessions(), {
            "here": {"live": True, "account": "1001"},
            "absent": {"live": False, "account": "1002"},
            "revoked": {"live": False, "account": "1003"},
            "unsigned": {"live": False, "account": "1004"},
            "unbound": {"live": True, "account": None}})

    def test_a_service_that_found_the_session_authorized_leaves_it_live(self):
        write_session(self.state / "here" / "session", signed_in_as=1001)
        health = self.state / "here" / "service" / "health.json"
        health.parent.mkdir(parents=True)
        health.write_text(json.dumps({"state": "healthy", "session_authorized": True}))
        self.assertTrue(self.sessions()["here"]["live"])

    def test_the_account_owner_record_names_the_health_of_another_state_dir(self):
        write_session(self.state / "here" / "session", signed_in_as=1001)
        elsewhere = self.tmp / "elsewhere" / "health.json"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(json.dumps({
            "session_authorized": False,
            "auth_session_path": str((self.state / "here" / "session").resolve())}))
        owner = self.cli._service_control_paths(1001)["owner"]
        owner.parent.mkdir(parents=True)
        owner.write_text(json.dumps({"health_file": str(elsewhere)}))
        self.assertFalse(self.sessions()["here"]["live"])
        # A record about another session file of the same account is not this one's.
        elsewhere.write_text(json.dumps({
            "session_authorized": False, "auth_session_path": str(self.tmp / "other")}))
        self.assertTrue(self.sessions()["here"]["live"])

    def test_the_verified_identity_is_the_account(self):
        self.conns["here"]["identity"] = {"id": "1001", "display_name": "Here @here"}
        self.assertEqual(self.sessions()["here"]["account"], "1001")

    def test_reading_leaves_the_session_files_as_they_were(self):
        path = write_session(self.state / "here" / "session", signed_in_as=1001)
        before = sorted(p.name for p in path.parent.iterdir()), path.read_bytes()
        self.sessions()
        self.assertEqual((sorted(p.name for p in path.parent.iterdir()), path.read_bytes()),
                         before)

    def test_the_manifest_declares_connections_device_bound(self):
        self.assertIs(self.cli._manifest_payload()["connections"]["device_bound"], True)


if __name__ == "__main__":
    unittest.main()
