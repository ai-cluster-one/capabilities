#!/usr/bin/env python3
"""Linking a connection that names no account yet: `pair --qr` links it and
binds the account the link answers for, and under `--events` a first link
that does not complete leaves neither the connection nor its home behind.

The session stands in for the engine; the registry, the state home and the
engine's own session file are real, under a home of the case's own.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _cli  # noqa: E402

ACCOUNT = "15550002222"


class FakeSession:
    """Links at once as ACCOUNT, writing the device row the engine would."""

    linked = True

    def __init__(self, cfg, *, props=None):
        self.cfg = cfg
        self.home = Path(cfg["home"])
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = None if cfg.get("unbound") else object()
        self.qr_payload = "2@ref"
        self.chunks, self.errors = [], []

    def open(self, *, expect_session=True):
        return self

    def wait_connected(self, timeout=0):
        return False

    def wait_linked(self, **_):
        if not self.linked:
            return False
        with sqlite3.connect(self.home / "session.db") as db:
            db.execute("CREATE TABLE whatsmeow_device (jid TEXT, push_name TEXT)")
            db.execute("INSERT INTO whatsmeow_device VALUES (?, ?)",
                       (f"{ACCOUNT}:7@s.whatsapp.net", "Person"))
        return True

    def bind(self, account):
        self.cfg["account_key"] = account
        self.db = object()

    def account(self):
        return {"jid": f"{ACCOUNT}@s.whatsapp.net", "id": ACCOUNT}

    def require_account(self):
        return self.account()

    def drain(self, **_):
        pass

    def refresh_identities(self):
        pass

    def close(self):
        pass


class Unlinked(FakeSession):
    linked = False


class CancelledInBurst(FakeSession):
    """Linked, then cancelled while the history burst is captured."""

    def drain(self, **_):
        os.kill(os.getpid(), signal.SIGTERM)


class PairLink(unittest.TestCase):

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
               "XDG_STATE_HOME": str(self.home / "state"),
               "XDG_DATA_HOME": str(self.home / "data")}
        for patcher in (mock.patch.dict(os.environ, env),
                        # Outside any project the linking verb is what lends it
                        # the machine's connections.
                        mock.patch.object(sys, "argv", ["whatsapp", "pair", "--qr"])):
            patcher.start()
            self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, cwd)
        self.wa = _cli.load()
        self.wa._project_root = lambda *a, **k: None
        self.wa._state_dir = lambda: self.home / "state" / "whatsapp"
        self.wa._device_props = lambda cfg, days, full: None
        self.wa._store_facts = lambda cfg: {"chats": 0, "messages": 0,
                                            "location": "store"}
        with contextlib.redirect_stdout(io.StringIO()):
            self.wa._cmd_connection(["add", "test", "--machine"])

    def pair(self, session, *, events=True):
        args = types.SimpleNamespace(
            connection="test", session=None, qr=True, number=None, days=None,
            full=False, hold=1, wait=False, recreate=False, yes=False,
            events=events)
        self.wa._EVENTS_ON[0] = events
        self.addCleanup(self.wa._EVENTS_ON.__setitem__, 0, False)
        out = io.StringIO()
        code = None
        with mock.patch.object(self.wa, "Session", session), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                self.wa.cmd_pair(args)
            except self.wa._Refusal as refusal:
                code = refusal.code
        return code, [json.loads(line) for line in out.getvalue().splitlines() if line]

    def registry(self):
        try:
            reg, _ = self.wa._connections_registry()
        except SystemExit:      # a registry left with no connection refuses
            return {}
        return (reg or {}).get("connections", {})

    def test_qr_links_a_connection_with_no_account_and_binds_it(self):
        code, events = self.pair(FakeSession)
        self.assertIsNone(code)
        done = events[-1]
        self.assertEqual(done["event"], "done")
        self.assertEqual(done["result"]["identity_bound"]["expected_account_id"], ACCOUNT)
        self.assertEqual(self.registry()["test"]["expected_account_id"], ACCOUNT)
        state = self.home / "state" / "whatsapp"
        self.assertTrue((state / ACCOUNT / "session.db").is_file())
        self.assertFalse((state / "test").exists())

    def test_first_link_that_fails_leaves_nothing(self):
        code, events = self.pair(Unlinked)
        self.assertEqual(code, "never_linked")
        self.assertNotIn("test", self.registry())
        self.assertFalse((self.home / "state" / "whatsapp" / "test").exists())

    def test_link_cancelled_during_the_burst_keeps_a_bound_connection(self):
        code, _ = self.pair(CancelledInBurst)
        self.assertEqual(code, "cancelled")
        self.assertEqual(self.registry()["test"]["expected_account_id"], ACCOUNT)
        state = self.home / "state" / "whatsapp"
        self.assertTrue((state / ACCOUNT / "session.db").is_file())
        self.assertFalse((state / "test").exists())
        reg, _ = self.wa._connections_registry()
        _cfg, problem = self.wa._build_cfg(*self.wa._select_connection(reg, "test"))
        self.assertIsNone(problem)

    def test_another_verb_during_the_burst_finds_the_session(self):
        wa, seen = self.wa, {}

        class OtherVerbInBurst(FakeSession):
            def drain(inner, **_):
                # What `doctor` does for the connection while the burst runs.
                reg, _ = wa._connections_registry()
                cfg, problem = wa._build_cfg(*wa._select_connection(reg, "test"))
                seen["problem"] = problem
                seen["session"] = (wa._connection_home(cfg) / "session.db").is_file()

        code, events = self.pair(OtherVerbInBurst)
        self.assertIsNone(code)
        self.assertEqual(seen, {"problem": None, "session": True})
        state = self.home / "state" / "whatsapp"
        self.assertTrue((state / ACCOUNT / "session.db").is_file())
        self.assertFalse((state / ACCOUNT).is_symlink())
        self.assertFalse((state / "test").exists())

    def test_without_events_a_failed_link_keeps_the_connection(self):
        code, _ = self.pair(Unlinked, events=False)
        self.assertEqual(code, "never_linked")
        self.assertIn("test", self.registry())


class NumberLinked(unittest.TestCase):
    """One account is reached through one connection: once `test` has linked
    ACCOUNT, another connection for that number is refused, naming `test`,
    before anything is written, and `test` itself is answered as before."""

    pair, registry = PairLink.pair, PairLink.registry

    def setUp(self):
        PairLink.setUp(self)
        code, _ = self.pair(FakeSession)
        self.assertIsNone(code)
        self.state = self.home / "state" / "whatsapp"

    def refusal(self, call):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                call()
            except self.wa._Refusal as refusal:
                return refusal
        self.fail(f"not refused: {err.getvalue()}")

    def test_the_link_records_the_connection(self):
        self.assertEqual(self.wa._linked_by(self.state / ACCOUNT),
                         {"connection": "test", "owner": None})

    def test_add_of_a_linked_number_is_refused_naming_the_holder(self):
        refusal = self.refusal(lambda: self.wa._cmd_connection(
            ["add", "other", "--machine", "--field", f"number={ACCOUNT}"]))
        self.assertEqual((refusal.exit_code, refusal.code), (6, "number_linked"))
        self.assertEqual(refusal.fields, {"account": ACCOUNT, "holder": {
            "connection": "test", "owner": None}})
        self.assertNotIn("--", refusal.hint)
        self.assertNotIn("other", self.registry())

    def test_add_reads_the_number_from_stdin(self):
        stdin = io.StringIO(json.dumps({"expected_account_id": ACCOUNT}))
        with mock.patch.object(sys, "stdin", stdin):
            refusal = self.refusal(lambda: self.wa._cmd_connection(
                ["add", "other", "--machine", "--stdin"]))
        self.assertEqual(refusal.code, "number_linked")
        self.assertNotIn("other", self.registry())

    def test_pair_of_another_connection_is_refused_before_anything_is_written(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.wa._contract_connection(
                ["add", "other", "--machine", "--field", f"number={ACCOUNT}"])
        before = sorted(p.name for p in self.state.iterdir())
        args = types.SimpleNamespace(
            connection="other", session=None, qr=False, number=None, days=None,
            full=False, hold=1, wait=False, recreate=False, yes=False, events=False)
        refusal = self.refusal(lambda: self.wa.cmd_pair(args))
        self.assertEqual(refusal.code, "number_linked")
        self.assertEqual(refusal.fields["holder"]["connection"], "test")
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), before)

    def test_the_holder_itself_is_still_already_paired(self):
        args = types.SimpleNamespace(
            connection="test", session=None, qr=False, number=None, days=None,
            full=False, hold=1, wait=False, recreate=False, yes=False, events=False)
        refusal = self.refusal(lambda: self.wa.cmd_pair(args))
        self.assertEqual(refusal.code, "already_paired")

    def test_a_rename_is_not_refused_and_takes_the_record(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.wa._contract_connection(["remove", "test", "--machine"])
            self.wa._cmd_connection(
                ["add", "renamed", "--machine", "--field", f"number={ACCOUNT}"])
        self.assertIn("renamed", self.registry())
        self.wa._record_linked_by({"id": "renamed", "home": str(self.state / ACCOUNT)},
                                  adopt=True)
        self.assertEqual(self.wa._linked_by(self.state / ACCOUNT),
                         {"connection": "renamed", "owner": None})

    def test_a_store_project_does_not_take_a_files_projects_holder(self):
        home = self.state / ACCOUNT
        self.wa._write_json(home / self.wa.LINKED_BY,
                            {"connection": "test", "owner": "filesproject"})

        class StoreRecords:
            project_id = "storeproject"
            mode_of = staticmethod(lambda collection: "store")
            connections = staticmethod(lambda *a, **k: {"other": {}})
            connection_owner = staticmethod(lambda *a: None)

        with mock.patch.object(self.wa, "_records", lambda: StoreRecords):
            refusal = self.refusal(
                lambda: self.wa._refuse_number_linked(ACCOUNT, "other", "storeproject"))
            self.wa._record_linked_by({"id": "other", "home": str(home)}, adopt=True)
        self.assertEqual(refusal.fields["holder"],
                         {"connection": "test", "owner": "filesproject"})
        self.assertEqual(self.wa._linked_by(home),
                         {"connection": "test", "owner": "filesproject"})

    def test_an_unrecorded_home_is_not_refused(self):
        (self.state / ACCOUNT / self.wa.LINKED_BY).unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.wa._cmd_connection(
                ["add", "other", "--machine", "--field", f"number={ACCOUNT}"])
        self.assertIn("other", self.registry())


if __name__ == "__main__":
    unittest.main()
