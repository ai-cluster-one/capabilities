#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


DEPLOYMENT = Path(__file__).resolve().parents[1] / "bin" / "deployment"
AUTOMATIONS = Path(__file__).resolve().parents[2] / "automations" / "bin" / "automations"


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
        self.registry = Path(self.tmp.name) / "registry"
        manifest_dir = self.registry / "automations"
        manifest_dir.mkdir(parents=True)
        manifest = subprocess.run(
            [str(AUTOMATIONS), "manifest", "--json"], capture_output=True,
            text=True, check=True, timeout=30,
        ).stdout
        (manifest_dir / "manifest.json").write_text(manifest)

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
            env={**os.environ, "CAPABILITIES_HOME": str(self.registry)},
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
        self.assertNotIn('command: ["automations", "service", "run"]', compose)
        self.assertIn("automations_state:/home/agent/.local/state/capabilities/projects", compose)
        runtime = json.loads((self.root / "deployment" / "runtime.json").read_text())
        self.assertEqual(runtime["services"]["agent"]["embedded_services"], ["automations"])
        self.assertEqual(
            runtime["volumes"]["automations_state"]["mount"],
            "/home/agent/.local/state/capabilities/projects",
        )
        supervisor = (self.root / "supervisord.conf").read_text()
        self.assertIn("command=automations service run", supervisor)
        lock = (self.root / "deployment" / "capabilities.lock").read_text().splitlines()
        self.assertIn("automations", lock)
        env = (self.root / ".env.example").read_text()
        self.assertIn("AUTOMATIONS_ENVIRONMENT=production", env)
        docker = shutil.which("docker")
        compose_available = (subprocess.run(
            [docker, "compose", "version"], capture_output=True, text=True,
            timeout=30, check=False,
        ).returncode == 0) if docker else False
        if compose_available:
            parsed = subprocess.run(
                ["docker", "compose", "config", "--quiet"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(parsed.returncode, 0, parsed.stderr)

    def test_explicit_no_suppresses_service(self) -> None:
        result = self.setup("--with-automations", "no")
        self.assertFalse(result["with_automations"])
        compose = (self.root / "docker-compose.yaml").read_text()
        self.assertNotIn('command: ["automations", "service", "run"]', compose)
        self.assertFalse((self.root / "supervisord.conf").exists())


if __name__ == "__main__":
    unittest.main()
