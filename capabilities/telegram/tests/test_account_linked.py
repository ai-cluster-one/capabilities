#!/usr/bin/env python3
"""One Telegram account is reached through one connection: once `holder` holds
ACCOUNT on this machine, another connection for that account is refused by
`connection add` and by `login`, naming `holder`, before anything is written,
and `holder` itself, or anyone once `holder` is removed, is answered as
before. Nothing reaches Telegram."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_check import import_cli, restore_modules  # noqa: E402

ACCOUNT = 1001


class _Records:
    """The records as a project on files answers them: every connection is the
    machine's."""

    project_id = None

    def __init__(self, conns: dict):
        self.conns = conns

    def connections(self, name, include_disabled=False):
        return dict(self.conns)

    def resolve_scope(self, name, collection, scope):
        return dict(self.conns) if scope == "global" else {}

    def mode_of(self, collection, key=None):
        return "file"


class _Client:
    """A client already signed in, or signing in as ACCOUNT by QR."""

    def __init__(self, me_id: int, authorized: bool):
        self.me = types.SimpleNamespace(id=me_id)
        self.authorized = authorized
        self.logged_out = False

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return self.me

    async def qr_login(self):
        me = self.me

        class _Qr:
            url, expires = "tg://login?token=x", types.SimpleNamespace(
                isoformat=lambda: "2026-10-27T00:00:00+00:00")

            async def wait(self):
                return me
        return _Qr()

    async def log_out(self):
        self.logged_out = True

    async def disconnect(self):
        return None


class AccountLinked(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        self.addCleanup(restore_modules, self.cli)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conns = {"holder": {"expected_account_id": ACCOUNT},
                      "other": {"expected_account_id": ACCOUNT},
                      "unbound": {}}
        self.added, self.made = [], []
        self.client = None
        reg = {"default": "holder", "connections": self.conns,
               "sources": {cid: str(self.tmp / "connections.json") for cid in self.conns}}
        keys = {"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "hash"}
        for target, value in (
                ("_connections_registry", lambda: (reg, None)),
                ("_records", lambda: _Records(self.conns)),
                ("_STATE_HOME", self.tmp / "state"),
                ("_state_dir", lambda: self.tmp / "state" / "telegram"),
                ("_resolve_env_key", lambda key: (keys.get(key), None, None)),
                ("_record_identity", lambda *a, **k: True),
                ("_contract_connection", self.added.append),
                ("make_client", lambda cfg: self.made.append(cfg) or self.client)):
            patcher = mock.patch.object(self.cli, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.record = self.tmp / "state" / "telegram" / str(ACCOUNT) / "linked-by.json"
        self.cli._record_linked_by("holder", ACCOUNT)

    def refused(self, call) -> dict:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                call()
        self.assertEqual(raised.exception.code, 6)
        return json.loads(err.getvalue().strip().splitlines()[-1])["error"]

    def login(self, cid: str, me_id: int = ACCOUNT, authorized: bool = False) -> None:
        self.client = _Client(me_id, authorized)
        args = types.SimpleNamespace(cmd="login", phone=None, events=False)
        self.cli._connection_command(args, cid, None)

    def assert_names_the_holder(self, error: dict) -> None:
        self.assertEqual(error["code"], "number_linked")
        self.assertEqual(error["account"], str(ACCOUNT))
        self.assertEqual(error["holder"], {"connection": "holder", "owner": None})

    def test_login_records_the_holder(self):
        self.assertEqual(json.loads(self.record.read_text()),
                         {"connection": "holder", "owner": None})

    def test_add_of_a_held_account_is_refused_naming_the_holder(self):
        error = self.refused(lambda: self.cli._cmd_connection(
            ["add", "second", "--machine", "--field", f"expected_account_id={ACCOUNT}"]))
        self.assert_names_the_holder(error)
        self.assertEqual(self.added, [])

    def test_add_reads_the_account_from_stdin(self):
        stdin = io.StringIO(json.dumps({"expected_account_id": ACCOUNT}))
        with mock.patch.object(sys, "stdin", stdin):
            error = self.refused(lambda: self.cli._cmd_connection(
                ["add", "second", "--machine", "--stdin"]))
        self.assert_names_the_holder(error)
        self.assertEqual(self.added, [])

    def test_the_holder_may_replace_its_own_entry(self):
        argv = ["add", "holder", "--machine", "--replace",
                "--field", f"expected_account_id={ACCOUNT}"]
        self.cli._cmd_connection(argv)
        self.assertEqual(self.added, [argv])

    def test_login_of_a_connection_naming_a_held_account_is_refused_first(self):
        error = self.refused(lambda: self.login("other"))
        self.assert_names_the_holder(error)
        self.assertEqual(self.made, [])
        self.assertEqual(json.loads(self.record.read_text())["connection"], "holder")

    def test_a_login_discovering_a_held_account_is_signed_out_and_refused(self):
        error = self.refused(lambda: self.login("unbound"))
        self.assert_names_the_holder(error)
        self.assertTrue(self.client.logged_out)
        self.assertEqual(json.loads(self.record.read_text())["connection"], "holder")

    def test_a_session_signed_in_before_is_refused_but_left_signed_in(self):
        error = self.refused(lambda: self.login("unbound", authorized=True))
        self.assert_names_the_holder(error)
        self.assertFalse(self.client.logged_out)

    def test_the_holder_logging_in_again_is_not_refused(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.login("holder", authorized=True)
        self.assertTrue(json.loads(out.getvalue())["ok"])

    def test_a_removed_holder_holds_nothing(self):
        del self.conns["holder"]
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            self.login("unbound")
        self.assertTrue(json.loads(out.getvalue())["ok"])
        self.assertFalse(self.client.logged_out)
        self.assertEqual(json.loads(self.record.read_text())["connection"], "unbound")

    def test_doctor_adopts_only_an_account_nobody_holds(self):
        self.cli._record_linked_by("other", ACCOUNT, adopt=True)
        self.assertEqual(json.loads(self.record.read_text())["connection"], "holder")
        self.cli._record_linked_by("other", 2002, adopt=True)
        adopted = self.tmp / "state" / "telegram" / "2002" / "linked-by.json"
        self.assertEqual(json.loads(adopted.read_text())["connection"], "other")


if __name__ == "__main__":
    unittest.main()
