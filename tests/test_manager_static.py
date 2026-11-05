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


def _literal(path: Path, name: str):
    """The literal value `path` assigns to `name`, a tuple target included."""
    import ast
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if getattr(target, "id", None) == name:
                return ast.literal_eval(node.value)
            if isinstance(target, ast.Tuple):
                names = [getattr(item, "id", None) for item in target.elts]
                if name in names:
                    return ast.literal_eval(node.value)[names.index(name)]
    raise AssertionError(f"{path} assigns no {name}")


class LeaseStepsTests(unittest.TestCase):
    def test_the_manager_alone_makes_the_lease_table_with_the_steps_stores_hold(self):
        # The manager's supervisor makes deployment_service_leases under the
        # ledger entry deployment gave it, which refuses a step whose SQL
        # changed, so each step keeps the checksum stores already hold; and
        # deployment declares and applies none of it.
        import hashlib
        steps = _literal(MANAGER, "SUPERVISE_LEASE_STEPS")
        self.assertEqual(
            [(step, hashlib.sha256(sql.encode("utf-8")).hexdigest()) for step, sql in steps],
            [("0001-service-leases",
              "fa19c921e39a42a9070e78efabe9d05de191cf41acfc113a000f84a575d72d7b"),
             ("0002-service-lease-errors",
              "c2b45f216e881d2140b3d1f4d41a2bca31513e52f6efe0ec86463c5aa0fff16c"),
             ("0003-agent-names",
              "31f18acd4e64cf58a51145bd459e220707467a08af7b4295ecbf5f574b00d0a0")])
        self.assertEqual(_literal(MANAGER, "SUPERVISE_LEASE_OWNER"), "deployment")
        self.assertEqual(_literal(MANAGER, "SUPERVISE_LEASE_VERSION"), (2, 0))
        deployment = (REPO / "capabilities" / "deployment" / "bin" / "deployment").read_text()
        for name in ("TABLES", "LEASE_STEPS", "_tables"):
            self.assertNotRegex(deployment, rf"(?m)^(def )?{name}\b")


if __name__ == "__main__":
    unittest.main()
