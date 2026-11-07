#!/usr/bin/env python3
"""A connection that binds no workspace keeps its service state under its id
and its rows under the account it always named: state made under the name the
connection had before connections were given ids is found and moved under the
id, and the rows it captured are still the ones it reads. No network, no store."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

su = _cli.load()
CID = "a1b2c3d4e5f6"


class FormerName(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(su, "_connection_former_name",
                                    lambda wanted: "work" if wanted == CID else None,
                                    create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_service_state_made_under_the_former_name_is_found_and_moved(self):
        with tempfile.TemporaryDirectory() as state:
            made = Path(state) / "slacker" / "work" / "service" / "health.json"
            made.parent.mkdir(parents=True)
            made.write_text("{}")
            err = io.StringIO()
            with mock.patch.object(su, "_STATE_HOME", Path(state)), \
                    contextlib.redirect_stderr(err):
                home = su._service_home({"id": CID, "account_binding": None})
            self.assertEqual(home, Path(state) / "slacker" / CID / "service")
            self.assertEqual((home / "health.json").read_text(), "{}")
            self.assertFalse((Path(state) / "slacker" / "work").exists())
            self.assertIn("work", err.getvalue())

    def test_the_rows_are_still_keyed_by_the_account_they_were_captured_under(self):
        self.assertEqual(su._store_account({"id": CID, "account_binding": None}), "work")
        self.assertEqual(su._store_account({"id": CID, "account_binding": "T001"}), "T001")
        self.assertEqual(su._store_account({"id": "b2c3d4e5f6a1", "account_binding": None}),
                         "b2c3d4e5f6a1")


if __name__ == "__main__":
    unittest.main()
