#!/usr/bin/env python3
"""Tests for the enabled-capability list `project_overview` reports: the project's own gate asked of the records adapter, an empty gate an empty list, an absent gate null and an unreadable one "unreadable".

Run with: python3 capabilities/geminitalk/tests/test_project_overview.py
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
    (path for path in (CAPABILITY / "bin" / "geminitalk", CAPABILITY / "geminitalk")
     if path.is_file()),
    CAPABILITY / "bin" / "geminitalk")


def _load_module():
    name = f"geminitalk_overview_under_test_{time.time_ns()}"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


class EnabledCapabilities(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.project = tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        self.envelope = self.project / "capabilities"
        self.envelope.mkdir()
        (self.envelope / "project.json").write_text(json.dumps(
            {"schema": "capabilities.project.v1", "id": "prj_0a1b2c3d4e5f", "slug": "lab"}))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CAPABILITIES_", "AGENTKIT_DB", "GEMINI"))}
        env.update(HOME=str(tmp / "home"), XDG_CONFIG_HOME=str(tmp / "config"),
                   XDG_STATE_HOME=str(tmp / "state"), XDG_CACHE_HOME=str(tmp / "cache"),
                   CLAUDE_PROJECT_DIR=str(self.project))
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.geminitalk = _load_module()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _enabled(self):
        return self.geminitalk.tool_project_overview(False)["enabled_capabilities"]

    def _gate(self, text: str) -> None:
        (self.envelope / "settings.json").write_text(text)

    def test_the_gate_lists_what_it_does_not_disable(self):
        self._gate(json.dumps({"capabilities": {
            "emta": {"enabled": True}, "geminitalk": {}, "coolify": {"enabled": False}}}))
        self.assertEqual(self._enabled(), ["emta", "geminitalk"])

    def test_an_empty_gate_is_an_empty_list(self):
        self._gate(json.dumps({"capabilities": {}}))
        self.assertEqual(self._enabled(), [])
        self._gate(json.dumps({}))
        self.assertEqual(self._enabled(), [])

    def test_an_absent_gate_is_null(self):
        self.assertIsNone(self._enabled())

    def test_an_unreadable_gate_says_so(self):
        self._gate("{not json")
        self.assertEqual(self._enabled(), "unreadable")


if __name__ == "__main__":
    unittest.main()
