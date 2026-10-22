#!/usr/bin/env python3
"""The service's prompts as versions in `telegram_prompts`: a save is a new
version and the earlier stays readable, the store keeps history immutable, and
the service directory's files are imported and exported by key.

These cases read TELEGRAM_TEST_DSN, a throwaway database's URL, and skip
without one; each case binds a schema of its own.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


TELEGRAM_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = TELEGRAM_DIR / "service"
STORE_DSN = os.environ.get("TELEGRAM_TEST_DSN")
from capabilities_contract.db import KEYS as DB_KEYS  # noqa: E402


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jobs = _module("telegram_service_jobs_prompts_test", SERVICE_DIR / "jobs.py")
prompts = _module("telegram_service_prompts_test", SERVICE_DIR / "prompts.py")
schema = _module("telegram_settings_schema_prompts_test", SERVICE_DIR / "settings_schema.py")


@unittest.skipUnless(STORE_DSN, "TELEGRAM_TEST_DSN is unset")
class PromptStoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        envelope = self.root / "capabilities"
        envelope.mkdir()
        self.project_id = f"prj_test_{uuid.uuid4().hex}"
        (envelope / "project.json").write_text(json.dumps({"id": self.project_id}))
        env = {k: v for k, v in os.environ.items() if k not in DB_KEYS}
        env.update({"XDG_CONFIG_HOME": str(self.root / "config"),
                    "AGENTKIT_DB_URL": STORE_DSN})
        with mock.patch.dict(os.environ, env, clear=True):
            setting = jobs.store_setting(self.root)
        setting = dataclasses.replace(setting, schema=f"tgtest_{uuid.uuid4().hex}")
        self.store, _register = jobs.open_register(envelope, "development",
                                                   setting=setting, profile_tables=False)
        self.addCleanup(self.store.close)
        self.conn = self.store.conn

    def test_an_edit_is_a_new_version_and_the_earlier_stays_readable(self):
        first, saved = prompts.save(self.conn, self.project_id, "context", "one\n", "a@n")
        self.assertTrue(saved)
        second, saved = prompts.save(self.conn, self.project_id, "context", "two\n", "a@n",
                                     base=first["id"])
        self.assertTrue(saved)
        self.assertEqual(prompts.active(self.conn, self.project_id, "context")["body"], "two\n")
        earlier = prompts.version(self.conn, self.project_id, "context", first["id"])
        self.assertEqual((earlier["body"], earlier["active"]), ("one\n", False))
        self.assertEqual([v["id"] for v in prompts.versions(self.conn, self.project_id, "context")],
                         [second["id"], first["id"]])
        again, saved = prompts.save(self.conn, self.project_id, "context", "two\n", "a@n")
        self.assertEqual((again["id"], saved), (second["id"], False))

    def test_a_save_over_a_version_it_did_not_read_is_a_conflict(self):
        first, _ = prompts.save(self.conn, self.project_id, "job-worker", "one", "a@n")
        prompts.save(self.conn, self.project_id, "job-worker", "two", "b@n")
        with self.assertRaises(prompts.PromptError) as caught:
            prompts.save(self.conn, self.project_id, "job-worker", "three", "a@n",
                         base=first["id"])
        self.assertEqual(caught.exception.slug, "conflict")
        self.assertEqual(prompts.active(self.conn, self.project_id, "job-worker")["body"], "two")

    def test_the_store_keeps_every_version_as_it_was_saved(self):
        row, _ = prompts.save(self.conn, self.project_id, "context", "kept", "a@n")
        for sql in ("UPDATE telegram_prompts SET body = 'changed' WHERE id = %s::uuid",
                    "DELETE FROM telegram_prompts WHERE id = %s::uuid"):
            with self.assertRaises(Exception):
                self.conn.execute(sql, (row["id"],))
        self.assertEqual(prompts.active(self.conn, self.project_id, "context")["body"], "kept")
        with self.assertRaises(prompts.PromptError):
            prompts.save(self.conn, self.project_id, "reference.notes", "x", "a@n")

    def test_the_service_files_import_by_key_and_export_back(self):
        source = self.root / "service"
        (source / "context").mkdir(parents=True)
        (source / "context.md").write_text("dialogue\n")
        (source / "job-worker.md").write_text("worker\n")
        (source / "Voice_Agent.md").write_text("voice\n")
        (source / "context" / "ops_room.md").write_text("ops\n")
        (source / "settings.json").write_text("{}")
        report = prompts.import_files(self.conn, self.project_id, source,
                                      schema.document_key, "a@n")
        self.assertEqual({r["key"]: r["outcome"] for r in report},
                         {"context": "saved", "job-worker": "saved",
                          "voice-agent": "saved", "context.ops-room": "saved"})
        again = prompts.import_files(self.conn, self.project_id, source,
                                     schema.document_key, "a@n")
        self.assertEqual({r["outcome"] for r in again}, {"unchanged"})

        (source / "context.md").write_text("dialogue, edited\n")
        report = prompts.export_files(self.conn, self.project_id, source, schema.document_key)
        self.assertEqual({r["key"]: r["outcome"] for r in report}["context"], "written")
        self.assertEqual((source / "context.md").read_text(), "dialogue\n")
        self.assertEqual((source / "Voice_Agent.md").read_text(), "voice\n")
        self.assertFalse((source / "voice-agent.md").exists())

        out = self.root / "out"
        prompts.export_files(self.conn, self.project_id, out, schema.document_key)
        self.assertEqual(sorted(str(p.relative_to(out)) for p in out.rglob("*.md")),
                         ["context.md", "context/ops-room.md", "job-worker.md",
                          "voice-agent.md"])


if __name__ == "__main__":
    unittest.main()
