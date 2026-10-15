#!/usr/bin/env python3
"""The service processes read settings and connections through the records
adapter: in a files-mode project from the envelope's files and then the config
home's, in a store-mode project connections and grants from its store while
any file left on disk is not read."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from test_assistant_service import STORE_DSN, import_daemon
from test_call_recorder_delivery import import_call_recorder

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "service"))
import records  # noqa: E402
sys.path.pop(0)

SETTINGS = {"connection": "test",
            "allowed_users": {"777": {"name": "Tester", "role": "owner"}}}


def write_json(path: Path, body: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body) + "\n")
    return path


def store_env(schema: str) -> dict:
    return {"AGENTKIT_DB_URL": STORE_DSN, "AGENTKIT_DB_SCHEMA": schema}


def seed_store(project: Path, config_home: Path, schema: str, cid: str, entry: dict) -> None:
    """Declare `cid` in the store and grant it to `project`, as the manager
    would after an import."""
    with mock.patch.dict(os.environ, store_env(schema)):
        adapter = records.open_records(project / "capabilities", config_home, project)
        try:
            adapter.set("telegram", "connection", cid, entry)
            adapter.set("telegram", "grant", cid, {"enabled": True, "allow_write": True})
        finally:
            adapter.close()


def store_project(project: Path, schema: str, extra_env: dict | None = None) -> None:
    """Move `project` to store mode, keeping the id it was given, and point its
    database at `schema`."""
    identity_file = project / "capabilities" / "project.json"
    identity = (json.loads(identity_file.read_text()) if identity_file.is_file()
                else {"id": f"prj_test_{uuid.uuid4().hex}", "slug": "testproject"})
    identity["records"] = "store"
    write_json(identity_file, identity)
    (project / ".env.local").write_text(
        "".join(f"{key}={value}\n"
                for key, value in {**store_env(schema), **(extra_env or {})}.items()))


class DaemonFilesMode(unittest.TestCase):
    def test_settings_merge_the_project_over_the_config_home(self):
        def records(project, config_home):
            write_json(config_home / "telegram" / "service" / "settings.json",
                       {"connection": "elsewhere", "defaults": {"profile": "global"},
                        "allowed_groups": {}})
            write_json(project / "capabilities" / "telegram" / "connections.json",
                       {"default": "test", "connections": {
                           "test": {"api_id": 1, "allow_write": True}}})

        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), SETTINGS, connections_file=False,
                                   before_import=records)
            settings = daemon._read_record_settings()
        self.assertEqual(settings["connection"], "test")
        self.assertEqual(settings["defaults"], {"profile": "global"})
        self.assertEqual(settings["allowed_groups"], {})
        self.assertNotIn("connection.default", settings)

    def test_the_project_connection_is_read_from_the_envelope(self):
        def records(project, config_home):
            write_json(config_home / "telegram" / "connections.json",
                       {"default": "machine", "connections": {
                           "machine": {"api_id": 2, "allow_write": True}}})
            write_json(project / "capabilities" / "telegram" / "connections.json",
                       {"default": "test", "connections": {
                           "test": {"api_id": 1, "allow_write": True,
                                    "expected_account_id": 41,
                                    "session": "state/test"}}})

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            daemon = import_daemon(tmp, SETTINGS, connections_file=False,
                                   before_import=records)
            project = tmp / "project"
            self.assertEqual(daemon.CONNECTION, "test")
            self.assertEqual(daemon.CONNECTION_ENTRY,
                             {"api_id": 1, "allow_write": True,
                              "expected_account_id": 41, "session": "state/test"})
            self.assertEqual(daemon.EXPECTED_ACCOUNT_ID, 41)
            self.assertEqual(daemon.SESSION, project / "state" / "test")
            self.assertEqual(daemon.CONN_FILE, str(
                project / "capabilities" / "telegram" / "connections.json"))
            self.assertEqual(daemon.CONNECTIONS["withheld"], ["machine"])

    def test_a_connection_the_project_was_not_granted_is_refused(self):
        def records(project, config_home):
            write_json(config_home / "telegram" / "connections.json",
                       {"connections": {"test": {"api_id": 2, "allow_write": True}}})

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as raised:
                import_daemon(Path(td), SETTINGS, connections_file=False,
                              before_import=records)
        self.assertIn("is not granted to this project", str(raised.exception))


class SettingsIgnoreTheConnectionsFile(unittest.TestCase):
    """Settings are read without the connections file, so a malformed one
    leaves them readable, as it always has."""

    def test_reload_reads_settings_beside_a_malformed_connections_file(self):
        def records(project, config_home):
            write_json(project / "capabilities" / "telegram" / "connections.json",
                       {"connections": {"test": {"api_id": 1, "allow_write": True}}})

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            daemon = import_daemon(tmp, SETTINGS, connections_file=False,
                                   before_import=records)
            (tmp / "project" / "capabilities" / "telegram" / "connections.json") \
                .write_text("{not json")
            self.assertEqual(daemon._read_settings()["connection"], "test")

    def test_the_override_starts_beside_a_malformed_project_connections_file(self):
        def records(project, config_home):
            (project / "capabilities" / "telegram" / "connections.json").write_text("{not json")

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            daemon = import_daemon(tmp, SETTINGS, before_import=records)
            self.assertEqual(daemon.CONNECTION, "test")
            self.assertEqual(daemon.CONN_FILE, tmp / "connections.json")
            self.assertEqual(daemon.SETTINGS["connection"], "test")

    def test_store_mode_reads_settings_beside_a_leftover_malformed_file(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td).resolve() / "project"
            envelope = project / "capabilities"
            write_json(envelope / "project.json",
                       {"id": "prj_test_settings", "slug": "p", "records": "store"})
            write_json(envelope / "telegram" / "service" / "settings.json", SETTINGS)
            (envelope / "telegram" / "connections.json").write_text("{not json")
            adapter = records.open_records(envelope, Path(td) / "config", project)
            self.assertEqual(adapter.mode_of("connection"), "store")
            self.assertEqual(records.service_settings(adapter), SETTINGS)


class DaemonStoreMode(unittest.TestCase):
    def test_connections_come_from_the_store_and_settings_from_files(self):
        if not STORE_DSN:
            raise unittest.SkipTest("TELEGRAM_TEST_DSN is unset")
        schema = f"tgrec_{uuid.uuid4().hex[:12]}"
        cid = f"conn-{uuid.uuid4().hex[:8]}"

        def records(project, config_home):
            store_project(project, schema, {"TELEGRAM_SERVICE_CONNECTION": cid})
            # Files a store-mode project no longer reads: a stale copy of the
            # same connection, and one the store never declared.
            stale = {"api_id": 9, "allow_write": False, "expected_account_id": 99}
            write_json(project / "capabilities" / "telegram" / "connections.json",
                       {"default": cid, "connections": {cid: stale, "ghost": stale}})
            seed_store(project, config_home, schema, cid,
                       {"api_id": 1, "expected_account_id": 42})

        with tempfile.TemporaryDirectory() as td:
            daemon = import_daemon(Path(td), {**SETTINGS, "connection": cid},
                                   store=True,
                                   connections_file=False, before_import=records)
            try:
                self.assertEqual(daemon.CONNECTION, cid)
                self.assertEqual(daemon.CONNECTION_ENTRY,
                                 {"api_id": 1, "expected_account_id": 42,
                                  "allow_write": True})
                self.assertEqual(daemon.EXPECTED_ACCOUNT_ID, 42)
                self.assertTrue(str(daemon.CONN_FILE).startswith(f"store:{schema}."))
                self.assertNotIn("ghost", daemon.CONNECTIONS["connections"])
                self.assertNotIn("ghost", daemon.CONNECTIONS["withheld"])
                self.assertEqual(daemon.SETTINGS["connection"], cid)
            finally:
                daemon._RECORDS.close()


class RecorderConnections(unittest.TestCase):
    def setUp(self):
        self.recorder = import_call_recorder()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve() / "project"
        self.config_home = Path(self.tmp.name).resolve() / "config"
        (self.project / "capabilities").mkdir(parents=True)
        (self.project / "capabilities" / "project.json").write_text("{}")
        patcher = mock.patch.object(self.recorder, "CONFIG_HOME", self.config_home)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"TELEGRAM_API_HASH": "hash"})
        env.start()
        self.addCleanup(env.stop)
        for key in ("TELEGRAM_SERVICE_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE"):
            os.environ.pop(key, None)

    def test_files_mode_reads_the_envelope_then_grants_from_the_config_home(self):
        write_json(self.config_home / "telegram" / "connections.json",
                   {"connections": {"machine": {"api_id": 2, "session": "/s/machine"}}})
        write_json(self.project / "capabilities" / "telegram" / "connections.json",
                   {"default": "test", "connections": {"test": {"api_id": 1}}})
        config = self.recorder.resolve_connection(self.project, None)
        self.assertEqual((config["id"], config["api_id"]), ("test", 1))
        self.assertEqual(config["registry"], str(
            self.project / "capabilities" / "telegram" / "connections.json"))
        with self.assertRaises(self.recorder.RecorderError) as raised:
            self.recorder.resolve_connection(self.project, "machine")
        self.assertEqual(raised.exception.code, "connection_not_granted")

        write_json(self.project / "capabilities" / "telegram" / "connections.json",
                   {"default": "test", "connections": {
                       "test": {"api_id": 1}, "machine": {"enabled": True}}})
        config = self.recorder.resolve_connection(self.project, "machine")
        self.assertEqual((config["api_id"], config["session"]), (2, Path("/s/machine")))

    def test_store_mode_reads_the_store_not_the_files(self):
        if not STORE_DSN:
            raise unittest.SkipTest("TELEGRAM_TEST_DSN is unset")
        schema = f"tgrec_{uuid.uuid4().hex[:12]}"
        cid = f"conn-{uuid.uuid4().hex[:8]}"
        write_json(self.project / "capabilities" / "project.json",
                   {"id": f"prj_test_{uuid.uuid4().hex}", "slug": "testproject"})
        store_project(self.project, schema)
        write_json(self.project / "capabilities" / "telegram" / "connections.json",
                   {"default": cid, "connections": {cid: {"api_id": 9}}})
        seed_store(self.project, self.config_home, schema, cid, {"api_id": 1})
        config = self.recorder.resolve_connection(self.project, cid)
        self.assertEqual((config["id"], config["api_id"]), (cid, 1))
        self.assertTrue(config["registry"].startswith(f"store:{schema}."))


if __name__ == "__main__":
    unittest.main()
