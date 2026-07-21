#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


DEPLOYMENT = Path(__file__).resolve().parents[1] / "bin" / "deployment"


class DeploymentAutomationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        (self.root / "capabilities").mkdir(parents=True)
        (self.root / "capabilities" / "settings.json").write_text(
            json.dumps(
                {
                    "capabilities": {
                        "deployment": {"enabled": True},
                        "automations": {"enabled": True},
                    }
                }
            )
            + "\n"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def setup(self, *extra: str) -> dict:
        proc = subprocess.run(
            [
                str(DEPLOYMENT),
                "setup",
                "--provider",
                "manual",
                "--with-telegram",
                "no",
                "--force",
                *extra,
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_auto_adds_automations_service(self) -> None:
        result = self.setup()
        self.assertTrue(result["with_automations"])
        compose = (self.root / "docker-compose.yaml").read_text()
        self.assertIn('command: ["automations", "service", "run"]', compose)
        self.assertIn("automations_state:/app/capabilities/automations/state", compose)
        runtime = json.loads((self.root / "deployment" / "runtime.json").read_text())
        self.assertEqual(runtime["services"]["automations"]["capability"], "automations")
        self.assertEqual(
            runtime["volumes"]["automations_state"]["mount"],
            "/app/capabilities/automations/state",
        )
        lock = (self.root / "deployment" / "capabilities.lock").read_text().splitlines()
        self.assertIn("automations", lock)
        env = (self.root / ".env.example").read_text()
        self.assertIn("AUTOMATIONS_ENVIRONMENT=production", env)
        if shutil.which("docker"):
            parsed = subprocess.run(
                ["docker", "compose", "config", "--quiet"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(parsed.returncode, 0, parsed.stderr)

    def test_explicit_no_suppresses_service(self) -> None:
        result = self.setup("--with-automations", "no")
        self.assertFalse(result["with_automations"])
        compose = (self.root / "docker-compose.yaml").read_text()
        self.assertNotIn('command: ["automations", "service", "run"]', compose)


if __name__ == "__main__":
    unittest.main()
