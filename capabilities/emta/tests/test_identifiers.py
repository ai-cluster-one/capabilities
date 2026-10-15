#!/usr/bin/env python3
"""Tests for emta's internal identifier paths: the principal_person_id lookup and the silent write the ingest bootstrap makes, read and written through the records adapter, and an unreadable identifier source read as unset so the bootstrap falls back to the session.

Run with: uv run --no-project --with httpx python3 capabilities/emta/tests/test_identifiers.py
(the emta bin declares httpx in its PEP-723 header, so bare python3 may not import it)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next(
    (path for path in (CAPABILITY / "bin" / "emta", CAPABILITY / "emta")
     if path.is_file()),
    CAPABILITY / "bin" / "emta")


def _load_module():
    name = f"emta_identifiers_under_test_{time.time_ns()}"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


class _Response:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class IdentifierPaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.project = tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        self.envelope = self.project / "capabilities"
        (self.envelope / "emta").mkdir(parents=True)
        (self.envelope / "settings.json").write_text(
            json.dumps({"capabilities": {"emta": {"enabled": True}}}))
        self.ids = self.envelope / "emta" / "identifiers.json"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CAPABILITIES_", "AGENTKIT_DB", "EMTA_"))}
        env.update(HOME=str(tmp / "home"), XDG_CONFIG_HOME=str(tmp / "config"),
                   XDG_STATE_HOME=str(tmp / "state"), XDG_CACHE_HOME=str(tmp / "cache"),
                   CLAUDE_PROJECT_DIR=str(self.project))
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.emta = _load_module()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_a_recorded_identifier_is_read_and_coerced(self):
        self.ids.write_text(json.dumps({"principal_person_id": {"value": 749001, "note": "n"}}))
        self.assertEqual(self.emta._ids_get("principal_person_id"), "749001")
        self.assertIsNone(self.emta._ids_get("absent"))

    def test_the_silent_write_lands_in_the_project_identifiers(self):
        self.ids.write_text(json.dumps({"principal_person_id": {"value": 1, "note": "kept"}}))
        self.assertTrue(self.emta._ids_set_silent("principal_person_id", "555000"))
        self.assertEqual(json.loads(self.ids.read_text()),
                         {"principal_person_id": {"value": "555000", "note": "kept"}})
        self.assertEqual(self.emta._ids_get("principal_person_id"), "555000")

    def test_an_unreadable_identifier_source_reads_as_unset_and_is_not_written(self):
        self.ids.write_text("{not json")
        self.assertIsNone(self.emta._ids_get("principal_person_id"))
        self.assertFalse(self.emta._ids_set_silent("principal_person_id", "555000"))
        self.assertEqual(self.ids.read_text(), "{not json")

    def test_the_bootstrap_falls_back_to_the_session_over_an_unreadable_source(self):
        self.ids.write_text("{not json")
        body = {"data": {"principal": {"principalId": 555000}}, "ok": True}
        session: dict = {}
        with mock.patch.object(self.emta, "_send", return_value=_Response(body)), \
                mock.patch.object(self.emta, "_save_session") as saved:
            result = self.emta._bootstrap("main", session)
        self.assertEqual(result, {"ok": True, "status": 200, "ids_action": "session_fallback"})
        self.assertEqual(session["principal_person_id_fallback"], "555000")
        saved.assert_called_once_with("main", session)
        self.assertEqual(self.ids.read_text(), "{not json")

    def test_the_bootstrap_records_the_principal_when_it_can(self):
        body = {"data": {"principal": {"principalId": 555000}}, "ok": True}
        with mock.patch.object(self.emta, "_send", return_value=_Response(body)), \
                mock.patch.object(self.emta, "_save_session"):
            result = self.emta._bootstrap("main", {})
        self.assertEqual(result["ids_action"], "set")
        self.assertEqual(json.loads(self.ids.read_text())["principal_person_id"]["value"], "555000")


if __name__ == "__main__":
    unittest.main()
