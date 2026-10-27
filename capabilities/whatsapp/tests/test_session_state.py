#!/usr/bin/env python3
"""Whether a connection's session lives on this machine, as `whatsapp
connections` reports it, and the manifest's word that a connection is a
device-bound session. The registry, the state home and the engine's session
file are real, under a home of the case's own; nothing reaches WhatsApp or a
WAHA server."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _cli  # noqa: E402


def link(home: Path, account: str) -> Path:
    """The device row the engine writes once a device is linked."""
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "session.db") as db:
        db.execute("CREATE TABLE whatsmeow_device (jid TEXT, push_name TEXT)")
        db.execute("INSERT INTO whatsmeow_device VALUES (?, ?)",
                   (f"{account}:7@s.whatsapp.net", "Person"))
    db.close()
    return home / "session.db"


class SessionState(unittest.TestCase):

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.home, True)
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
               "XDG_STATE_HOME": str(self.home / "state"),
               "XDG_DATA_HOME": str(self.home / "data")}
        for patcher in (mock.patch.dict(os.environ, env),
                        # Outside any project `connections` is a machine read.
                        mock.patch.object(sys, "argv", ["whatsapp", "connections"])):
            patcher.start()
            self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, cwd)
        self.wa = _cli.load()
        self.wa._project_root = lambda *a, **k: None
        self.state = self.home / "state" / "whatsapp"
        self.wa._state_dir = lambda: self.state
        registry = self.home / "config" / "whatsapp" / "connections.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"default": "here", "connections": {
            "here": {"expected_account_id": "15550000001"},
            "absent": {"expected_account_id": "15550000002"},
            "logged-out": {"expected_account_id": "15550000003"},
            "by-number": {"number": "+1 555 000 0004"},
            "bridge": {"engine": "waha", "base_url": "http://127.0.0.1:9",
                       "expected_account_id": "15550000005"}}}))

    def sessions(self) -> dict:
        report = self.wa._connections_report()
        return {cid: entry["session"] for cid, entry in report["connections"].items()}

    def test_each_connection_says_whether_its_session_is_here(self):
        link(self.state / "15550000001", "15550000001")
        link(self.state / "15550000003", "15550000003")
        link(self.state / "15550000004", "15550000004")
        health = self.state / "15550000003" / "service" / "health.json"
        health.parent.mkdir(parents=True)
        # As the service records a device removed from the account.
        health.write_text(json.dumps({"state": "logged_out", "connected": False}))
        self.assertEqual(self.sessions(), {
            "here": {"live": True, "account": "15550000001"},
            "absent": {"live": False, "account": "15550000002"},
            "logged-out": {"live": False, "account": "15550000003"},
            "by-number": {"live": True, "account": "15550000004"},
            "bridge": {"live": None, "account": "15550000005", "reason": "waha_server"}})

    def test_a_running_service_leaves_the_session_live(self):
        link(self.state / "15550000001", "15550000001")
        health = self.state / "15550000001" / "service" / "health.json"
        health.parent.mkdir(parents=True)
        health.write_text(json.dumps({"state": "connected"}))
        self.assertTrue(self.sessions()["here"]["live"])

    def test_reading_creates_no_home_and_leaves_the_session_as_it_was(self):
        session = link(self.state / "15550000001", "15550000001")
        before = session.read_bytes()
        self.sessions()
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["15550000001"])
        self.assertEqual(sorted(p.name for p in session.parent.iterdir()), ["session.db"])
        self.assertEqual(session.read_bytes(), before)

    def test_the_manifest_declares_connections_device_bound(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["whatsapp", "manifest", "--json"]), \
                mock.patch.object(self.wa, "_gate", lambda: None), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            self.wa.main()
        manifest = json.loads(out.getvalue())
        self.assertIs(manifest["connections"]["device_bound"], True)
        self.assertIn("service", manifest)


if __name__ == "__main__":
    unittest.main()
