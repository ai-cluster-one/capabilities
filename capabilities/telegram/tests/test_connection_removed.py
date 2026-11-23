#!/usr/bin/env python3
"""What `connection remove` of a machine connection takes away on this machine
beside its record: the contract calls `_connection_removed(id, entry, scope)`
once the record is gone, and telegram answers by removing the session files
that connection signs in through here. Local files only; nothing reaches
Telegram or a store."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_check import import_cli, restore_modules  # noqa: E402

CID = "0a1b2c3d4e5f"


class _Records:
    def __init__(self, held: dict | Exception):
        self.held = held

    def connections(self, _name, include_disabled=False):
        if isinstance(self.held, Exception):
            raise self.held
        return self.held


class ConnectionRemovedTests(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        self.addCleanup(restore_modules, self.cli)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state" / "telegram"
        self.records = _Records({})
        for target, value in (("_state_dir", lambda: self.state),
                              ("_records", lambda: self.records),
                              ("DEFAULT_SESSION", self.state / "session")):
            patcher = mock.patch.object(self.cli, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def session(self, stem: Path, journal: bool = False) -> Path:
        path = stem.with_name(stem.name + ".session")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"login")
        if journal:
            path.with_name(path.name + "-journal").write_bytes(b"")
        return path

    def test_a_machine_connection_takes_its_sessions_and_their_empty_folders(self):
        own = self.session(self.state / CID / "session", journal=True)
        filed = self.session(self.state / "filed" / "session")
        removed = self.cli._connection_removed(CID, {"id": CID, "label": "filed"}, "global")
        self.assertEqual(set(removed), {
            str(own), str(own) + "-journal", str(own.parent), str(filed), str(filed.parent)})
        self.assertFalse((self.state / CID).exists() or (self.state / "filed").exists())

    def test_a_session_another_connection_or_the_machine_default_uses_is_left(self):
        shared = self.session(self.tmp / "shared" / "session")
        default = self.session(self.state / "session")
        self.records = _Records({"bbbbbbbbbbbb": {"value": {"session": str(shared)[:-8]}}})
        removed = self.cli._connection_removed(
            CID, {"id": CID, "label": "filed", "session": str(shared)[:-8]}, "global")
        self.assertEqual(removed, [])
        self.assertTrue(shared.is_file() and default.is_file())
        # A folder that holds more than the session stays.
        own = self.session(self.state / CID / "session")
        (own.parent / "service").mkdir()
        self.assertEqual(self.cli._connection_removed(CID, {"id": CID}, "global"), [str(own)])
        self.assertTrue((own.parent / "service").is_dir())

    def test_a_project_connection_or_unreadable_records_remove_nothing(self):
        own = self.session(self.state / CID / "session")
        self.assertEqual(self.cli._connection_removed(CID, {"id": CID}, "project"), [])
        self.records = _Records(self.cli.StoreError("store_unreachable", "away"))
        self.assertEqual(self.cli._connection_removed(CID, {"id": CID}, "global"), [])
        self.assertTrue(own.is_file())


if __name__ == "__main__":
    unittest.main()
