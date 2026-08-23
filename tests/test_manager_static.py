"""Static checks over the manager, for defects no runtime test reaches.

`dev live start` shipped a `NameError` on its only code path: `_dev_live_inputs`
built its result dict with an `envelope` key whose value was never assigned. It
takes a live Telegram connection to reach that line, so no test in this suite
did, and the command failed for every caller until someone ran it.

Undefined names are exactly what a static pass finds for free, and the manager
is one file, so the pass is cheap enough to keep.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"


@unittest.skipUnless(shutil.which("uv"), "uv is how this repo runs Python")
class ManagerStaticTests(unittest.TestCase):
    def test_the_manager_has_no_undefined_names(self):
        result = subprocess.run(
            ["uv", "run", "--quiet", "--with", "pyflakes",
             "python", "-m", "pyflakes", str(MANAGER)],
            text=True, capture_output=True, check=False,
        )
        undefined = [line for line in result.stdout.splitlines()
                     if "undefined name" in line]
        self.assertEqual(undefined, [], "\n".join(undefined))


if __name__ == "__main__":
    unittest.main()
