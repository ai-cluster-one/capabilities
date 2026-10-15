#!/usr/bin/env python3
"""The service's project layout snapshot: the memory layer is `memory` in the
body root ContextKit names for a bound project, else <root>/memory; the
deployment layer is <root>/deployment for every project."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_outbound_actions import import_cli  # noqa: E402


class LayoutCase(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "project"
        (self.root / "capabilities").mkdir(parents=True)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def layout(self, body_root=None):
        def run(argv, **kw):
            self.calls.append(argv)
            if argv == ["contextkit", "path", "--json"] and body_root is not None:
                return subprocess.CompletedProcess(argv, 0, json.dumps(
                    {"project": str(self.root), "body_root": body_root, "layers": {}}), "")
            return subprocess.CompletedProcess(argv, 2, "", "unknown layer\n")
        with mock.patch.object(self.cli, "_project_capabilities_dir",
                               lambda root: root / "capabilities"), \
                mock.patch.object(self.cli.subprocess, "run", run):
            return self.cli._resolve_project_layout(self.root)

    def bind(self):
        (self.root / ".contextkit").mkdir()
        (self.root / ".contextkit" / "config.toml").write_text("version = 1\n")


class MemoryLayer(LayoutCase):
    def test_a_bound_project_with_a_body_folder_reads_memory_there(self):
        self.bind()
        (self.root / "agent" / "memory").mkdir(parents=True)
        layout = self.layout("agent")
        self.assertEqual(layout["memory"], str(self.root / "agent" / "memory"))
        self.assertIn(["contextkit", "path", "--json"], self.calls)
        self.assertNotIn(["contextkit", "path", "memory"], self.calls)

    def test_a_bound_project_root_body_reads_its_root_memory(self):
        self.bind()
        (self.root / "memory").mkdir()
        self.assertEqual(self.layout(".")["memory"], str(self.root / "memory"))

    def test_a_bound_project_without_a_memory_folder_has_no_memory_layer(self):
        self.bind()
        self.assertNotIn("memory", self.layout("agent"))

    def test_a_bound_project_whose_contextkit_names_no_body_root_has_no_memory_layer(self):
        self.bind()
        (self.root / "memory").mkdir()
        self.assertNotIn("memory", self.layout(None))

    def test_a_project_without_contextkit_reads_its_root_memory(self):
        (self.root / "memory").mkdir()
        layout = self.layout(None)
        self.assertEqual(layout["memory"], str(self.root / "memory"))
        self.assertEqual(self.calls, [])


class DeploymentLayer(LayoutCase):
    def assert_never_asked_for_a_deployment_layer(self):
        self.assertNotIn(["contextkit", "path", "deployment"], self.calls)

    def test_a_bound_project_root_body_reads_its_root_deployment(self):
        self.bind()
        (self.root / "deployment").mkdir()
        self.assertEqual(self.layout(".")["deployment"], str(self.root / "deployment"))
        self.assert_never_asked_for_a_deployment_layer()

    def test_a_bound_project_with_a_body_folder_reads_its_root_deployment(self):
        self.bind()
        (self.root / "agent").mkdir()
        (self.root / "deployment").mkdir()
        self.assertEqual(self.layout("agent")["deployment"], str(self.root / "deployment"))
        self.assert_never_asked_for_a_deployment_layer()

    def test_a_deployment_folder_inside_the_body_folder_is_not_the_layer(self):
        self.bind()
        (self.root / "agent" / "deployment").mkdir(parents=True)
        self.assertNotIn("deployment", self.layout("agent"))

    def test_a_project_without_contextkit_reads_its_root_deployment(self):
        (self.root / "deployment").mkdir()
        layout = self.layout(None)
        self.assertEqual(layout["deployment"], str(self.root / "deployment"))
        self.assertEqual(self.calls, [])

    def test_a_project_without_a_deployment_folder_has_no_deployment_layer(self):
        self.bind()
        self.assertNotIn("deployment", self.layout("."))


if __name__ == "__main__":
    unittest.main()
