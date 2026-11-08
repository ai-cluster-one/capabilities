#!/usr/bin/env python3
"""The daemon is launched outside the CLI's own uv script environment.

`uv run --script` exports VIRTUAL_ENV and puts that environment's bin first on
PATH. A daemon launched with them resolves its own `uv run --script` onto the
CLI's cached interpreter, which on an Apple Silicon Mac can be an x86_64 Python
with no ntgcalls wheel.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_outbound_actions import import_cli  # noqa: E402


class DaemonLaunchEnvironment(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.venv = Path(tmp.name) / "environments-v2" / "telegram-abc"
        (self.venv / "bin").mkdir(parents=True)
        self.other = Path(tmp.name) / "other-bin"
        self.other.mkdir()

    def _env(self, extra: dict) -> dict:
        with mock.patch.dict(os.environ, extra, clear=False):
            return self.cli._daemon_base_env()

    def test_cli_uv_environment_is_dropped(self):
        path = os.pathsep.join([str(self.venv / "bin"), str(self.other), "/usr/bin"])
        env = self._env({"VIRTUAL_ENV": str(self.venv), "PATH": path,
                         "CAPABILITIES_DEV_SESSION": "s1", "UV_CACHE_DIR": "/c"})
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertEqual(env["PATH"], os.pathsep.join([str(self.other), "/usr/bin"]))
        self.assertEqual(env["CAPABILITIES_DEV_SESSION"], "s1")
        self.assertEqual(env["UV_CACHE_DIR"], "/c")

    def test_path_is_untouched_without_a_virtual_env(self):
        path = os.pathsep.join([str(self.venv / "bin"), "/usr/bin"])
        with mock.patch.dict(os.environ, {"PATH": path}, clear=False):
            os.environ.pop("VIRTUAL_ENV", None)
            env = self.cli._daemon_base_env()
        self.assertEqual(env["PATH"], path)
        self.assertNotIn("VIRTUAL_ENV", env)


if __name__ == "__main__":
    unittest.main()
