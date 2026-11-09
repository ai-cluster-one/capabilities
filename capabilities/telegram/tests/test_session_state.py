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
            "absent": {"live": False, "account": "1002", "reason": "session_missing"},
            "revoked": {"live": False, "account": "1003", "reason": "not_signed_in"},
            "unsigned": {"live": False, "account": "1004", "reason": "not_signed_in"},
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


class MachineLocalSessionTests(unittest.TestCase):
    """A connection record is read by every machine that reaches the store, so
    a session path it carries is one machine's path: another machine signs the
    connection in under its own state home, and the machine that set the path
    keeps it."""

    def setUp(self):
        self.cli = import_cli()
        self.addCleanup(restore_modules, self.cli)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.state = self.tmp / "state" / "telegram"
        self.home = self.tmp / "home"
        # As a Mac wrote it: a path in that machine's home, which this one lacks.
        self.elsewhere = self.tmp / "Users" / "someone" / ".config" / "telegram" / "session"
        self.conns = {"personal": {"expected_account_id": 1001,
                                   "session": str(self.elsewhere)}}
        reg = {"default": "personal", "connections": self.conns,
               "sources": {"personal": str(self.tmp / "connections.json")}}
        keys = {"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "hash"}
        for target, value in (("_connections_registry", lambda: (reg, None)),
                              ("_state_dir", lambda: self.state),
                              ("_STATE_HOME", self.tmp / "state"),
                              ("_resolve_env_key", lambda key: (keys.get(key), None, None))):
            patcher = mock.patch.object(self.cli, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(Path, "home", lambda: self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self) -> str:
        return self.cli._load_config("personal", None)["session"]

    def reported(self) -> str:
        keys = self.cli._connections_report()["connections"]["personal"]["keys"]
        return next(k["value"] for k in keys if k["key"] == "session")

    def test_another_machines_path_resolves_under_this_machines_state_home(self):
        own = str(self.state / "personal" / "session")
        self.assertEqual(self.session(), own)
        self.assertEqual(self.reported(), own)
        self.assertEqual(self.cli._service_session_stem(self.tmp, "personal", None)[1],
                         Path(own))
        self.assertFalse((self.tmp / "Users").exists())

    def test_the_machine_holding_the_session_keeps_its_path(self):
        write_session(self.elsewhere, signed_in_as=1001)
        self.assertEqual(self.session(), str(self.elsewhere))
        self.assertEqual(self.reported(), str(self.elsewhere))
        self.assertEqual(self.cli._connections_report()["connections"]["personal"]["session"],
                         {"live": True, "account": "1001"})

    def test_a_path_in_this_machines_home_is_kept_before_its_first_login(self):
        mine = self.home / ".config" / "telegram" / "session"
        self.conns["personal"]["session"] = str(mine)
        self.assertEqual(self.session(), str(mine))

    def test_a_flag_still_overrides_the_connection(self):
        flag = str(self.tmp / "flag" / "session")
        self.assertEqual(self.cli._load_config("personal", flag)["session"], flag)

    def failure(self, error: BaseException) -> tuple[int, dict]:
        async def fail(cfg):
            raise error
        args = __import__("types").SimpleNamespace(cmd="login", phone=None, events=False)
        err = __import__("io").StringIO()
        with mock.patch.object(self.cli, "cmd_login_qr", fail), \
                mock.patch.object(self.cli, "_refuse_account_held", lambda *a: None), \
                __import__("contextlib").redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                self.cli._connection_command(args, "personal", None)
        return raised.exception.code, json.loads(err.getvalue().strip().splitlines()[-1])["error"]

    def test_a_local_file_error_is_not_reported_as_the_network(self):
        code, error = self.failure(PermissionError(13, "Permission denied", "/Users"))
        self.assertEqual((code, error["code"]), (6, "local_file_error"))
        self.assertIn("/Users", error["message"])
        self.assertNotIn("network", error["message"].lower())

    def test_a_failure_reaching_telegram_is_still_the_network(self):
        code, error = self.failure(ConnectionError("Connection to Telegram failed 5 time(s)"))
        self.assertEqual((code, error["code"]), (5, "network_error"))


class FormerNameSessionTests(unittest.TestCase):
    """A session signed in before connections were given ids lies under the
    connection's former name; the connection, named by its id now, finds it
    there and moves it under the id once, so no new sign-in is needed."""

    CID = "a1b2c3d4e5f6"

    def setUp(self):
        self.cli = import_cli()
        self.addCleanup(restore_modules, self.cli)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.state = self.tmp / "state" / "telegram"
        conns = {self.CID: {"expected_account_id": 1001}}
        reg = {"default": self.CID, "connections": conns,
               "sources": {self.CID: str(self.tmp / "connections.json")}}
        keys = {"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "hash"}
        for target, value in (("_connections_registry", lambda: (reg, None)),
                              ("_state_dir", lambda: self.state),
                              ("_STATE_HOME", self.tmp / "state"),
                              ("_resolve_env_key", lambda key: (keys.get(key), None, None))):
            patcher = mock.patch.object(self.cli, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.cli, "_connection_former_name",
                                    lambda cid: "personal" if cid == self.CID else None,
                                    create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_session_made_under_the_former_name_is_found_and_moved(self):
        write_session(self.state / "personal" / "session", signed_in_as=1001)
        err = __import__("io").StringIO()
        with __import__("contextlib").redirect_stderr(err):
            session = self.cli._load_config(self.CID, None)["session"]
        self.assertEqual(session, str(self.state / self.CID / "session"))
        self.assertTrue((self.state / self.CID / "session.session").is_file())
        self.assertFalse((self.state / "personal").exists())
        self.assertIn("personal", err.getvalue())
        self.assertEqual(self.cli._connections_report()["connections"][self.CID]["session"],
                         {"live": True, "account": "1001"})



class AccountNamedFormerFolderTests(unittest.TestCase):
    """A connection whose former name was its account id kept its session in
    the same folder as the account's own records - the service control folder
    and the record of which connection holds the account - which stay keyed
    by the account. Moving the folder under the connection's id takes the
    session and leaves the account's records at the account, on a first
    resolve and on a machine where an earlier resolve moved them away."""

    CID = "a1b2c3d4e5f6"
    ACCOUNT = 1001

    def setUp(self):
        self.cli = import_cli()
        self.addCleanup(restore_modules, self.cli)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.state = self.tmp / "state" / "telegram"
        self.old = self.state / str(self.ACCOUNT)
        self.new = self.state / self.CID
        self.entry = {"expected_account_id": self.ACCOUNT}
        conns = {self.CID: self.entry}
        reg = {"default": self.CID, "connections": conns,
               "sources": {self.CID: str(self.tmp / "connections.json")}}
        keys = {"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "hash"}
        for target, value in (("_connections_registry", lambda: (reg, None)),
                              ("_state_dir", lambda: self.state),
                              ("_STATE_HOME", self.tmp / "state"),
                              ("_resolve_env_key", lambda key: (keys.get(key), None, None)),
                              ("_connection_former_name",
                               lambda cid: str(self.ACCOUNT) if cid == self.CID else None)):
            patcher = mock.patch.object(self.cli, target, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def account_records(self, folder: Path) -> None:
        (folder / "control").mkdir(parents=True, exist_ok=True)
        (folder / "control" / "ownership-v1.json").write_text(json.dumps({
            "schema": "telegram.ownership.v1", "account_id": self.ACCOUNT,
            "protocol_version": 1}))
        (folder / "linked-by.json").write_text(json.dumps(
            {"connection": str(self.ACCOUNT), "owner": None}))

    def resolve(self) -> str:
        with __import__("contextlib").redirect_stderr(__import__("io").StringIO()):
            return self.cli._load_config(self.CID, None)["session"]

    def assert_account_records_at_the_account(self):
        self.assertEqual(self.cli._require_ownership_marker(self.ACCOUNT)["account_id"],
                         self.ACCOUNT)
        self.assertIsNotNone(self.cli._linked_by(str(self.ACCOUNT)))
        self.assertFalse((self.new / "control").exists())
        self.assertFalse((self.new / "linked-by.json").exists())

    def test_the_first_move_leaves_the_account_records_at_the_account(self):
        write_session(self.old / "session", signed_in_as=self.ACCOUNT)
        self.account_records(self.old)
        self.assertEqual(self.resolve(), str(self.new / "session"))
        self.assertTrue((self.new / "session.session").is_file())
        self.assert_account_records_at_the_account()
        self.assertEqual(self.cli._connections_report()["connections"][self.CID]["session"],
                         {"live": True, "account": str(self.ACCOUNT)})

    def test_a_recorded_session_path_under_the_former_folder_is_read_under_the_id(self):
        self.entry["session"] = str(self.old / "session")
        write_session(self.old / "session", signed_in_as=self.ACCOUNT)
        self.account_records(self.old)
        self.assertEqual(self.resolve(), str(self.new / "session"))
        self.assertFalse((self.old / "session.session").exists())
        self.assert_account_records_at_the_account()

    def test_a_machine_that_already_moved_them_gets_them_back(self):
        # What an earlier resolve left: everything under the id, and the
        # former folder made again by a process that read the recorded path,
        # with a session nobody signed in. That file is left as it is.
        self.entry["session"] = str(self.old / "session")
        write_session(self.new / "session", signed_in_as=self.ACCOUNT)
        self.account_records(self.new)
        (self.new / "service").mkdir()
        stray = write_session(self.old / "session", auth_key=b"")
        (self.old / "service").mkdir()
        before = stray.read_bytes()
        self.assertEqual(self.resolve(), str(self.new / "session"))
        self.assert_account_records_at_the_account()
        self.assertEqual(stray.read_bytes(), before)
        self.assertEqual(self.cli._connections_report()["connections"][self.CID]["session"],
                         {"live": True, "account": str(self.ACCOUNT)})

    def test_a_login_made_again_in_the_former_folder_stays_the_one_read(self):
        # An already-crossed machine where the stray session was signed in
        # again by hand: both files hold a login, and the one in use today,
        # in the former folder, stays the one read. Neither file is touched.
        import hashlib
        self.entry["session"] = str(self.old / "session")
        moved = write_session(self.new / "session", signed_in_as=self.ACCOUNT)
        self.account_records(self.new)
        again = write_session(self.old / "session", auth_key=b"\x02" * 256,
                              signed_in_as=self.ACCOUNT)
        sums = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in (moved, again)}
        self.assertEqual(self.resolve(), str(self.old / "session"))
        self.assert_account_records_at_the_account()
        self.assertEqual(self.cli._connections_report()["connections"][self.CID]["session"],
                         {"live": True, "account": str(self.ACCOUNT)})
        self.assertEqual({f: hashlib.sha256(f.read_bytes()).hexdigest() for f in sums}, sums)

    def test_records_already_at_the_account_are_not_written_over(self):
        self.account_records(self.new)
        (self.new / "control" / "owner.json").write_text('{"moved": true}')
        write_session(self.new / "session", signed_in_as=self.ACCOUNT)
        (self.old / "control").mkdir(parents=True)
        (self.old / "control" / "owner.json").write_text("{}")
        (self.old / "linked-by.json").write_text(json.dumps(
            {"connection": "elsewhere", "owner": None}))
        self.resolve()
        self.assertEqual((self.old / "control" / "owner.json").read_text(), "{}")
        self.assertEqual(self.cli._require_ownership_marker(self.ACCOUNT)["account_id"],
                         self.ACCOUNT)
        self.assertEqual(self.cli._linked_by(str(self.ACCOUNT))["connection"], "elsewhere")
        self.assertEqual((self.new / "control" / "owner.json").read_text(), '{"moved": true}')
        self.assertTrue((self.new / "linked-by.json").is_file())


if __name__ == "__main__":
    unittest.main()
