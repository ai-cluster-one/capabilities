#!/usr/bin/env python3
"""Every credential `telegram connections` reports for a connection: its app,
and the keys its service reads - the voice-call keys its `gemini_secret_env`
and `openai_secret_env` name, else GOOGLE_API_KEY and OPENAI_API_KEY, and the
transcription key - each with where it resolved from, masked."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_check import import_cli, restore_modules  # noqa: E402

RESOLVED = {"GOOGLE_API_KEY": ("google-key-0001", "vault", "agent vault"),
            "TELEGRAM_OWN_GEMINI_KEY": ("own-gemini-0002", "project", "/p/.env.local")}


class ConnectionKeysTests(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        self.addCleanup(restore_modules, self.cli)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.reg = {"connections": {"plain": {"expected_account_id": 1},
                                    "own": {"expected_account_id": 2,
                                            "gemini_secret_env": "TELEGRAM_OWN_GEMINI_KEY"}},
                    "sources": {"plain": "c.json", "own": "c.json"}}
        for target, value in (("_connections_registry", lambda: (self.reg, None)),
                              ("_state_dir", lambda: self.tmp),
                              ("_resolve_env_key",
                               lambda key: RESOLVED.get(key, (None, None, None)))):
            patcher = mock.patch.object(self.cli, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def keys(self, cid: str) -> dict:
        report = self.cli._connections_report()
        return {k["key"]: k for k in report["connections"][cid]["keys"]}

    def test_the_google_key_reports_where_it_resolved_from(self):
        google = self.keys("plain")["GOOGLE_API_KEY"]
        self.assertEqual((google["set"], google["tier"], google["source"], google["value"]),
                         (True, "vault", "agent vault", "…0001"))
        self.assertFalse(google["required"])

    def test_a_connection_naming_its_own_key_reports_that_key(self):
        keys = self.keys("own")
        self.assertEqual(keys["TELEGRAM_OWN_GEMINI_KEY"]["value"], "…0002")
        self.assertNotIn("GOOGLE_API_KEY", keys)

    def test_an_unset_key_is_reported_unset_and_the_app_rows_stay(self):
        keys = self.keys("plain")
        for key in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY"):
            self.assertEqual((keys[key]["set"], keys[key]["tier"]), (False, None))
        self.assertEqual(list(keys)[:3], ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "session"])

    def test_without_a_registry_the_flat_keys_carry_the_voice_keys_once(self):
        self.reg = None
        names = [k["key"] for k in self.cli._connections_report()["connections"]["default"]["keys"]]
        self.assertEqual(names, [k["key"] for k in self.cli.CRED_KEYS]
                         + ["GOOGLE_API_KEY", "OPENAI_API_KEY"])


if __name__ == "__main__":
    unittest.main()
