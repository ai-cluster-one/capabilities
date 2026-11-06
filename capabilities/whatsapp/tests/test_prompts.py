#!/usr/bin/env python3
"""The service's prompts in the store: what a turn and a job read, and the
import that moves a project's prompt files there.

Store-backed: needs a throwaway Postgres named by WHATSAPP_TEST_DSN and skips
without one; the dialogue it builds needs callva-harness-runner.
"""

from __future__ import annotations

import io
import types
import unittest
import uuid
from unittest import mock

import _cli
import test_dialogue as td

wa = td.wa


@_cli.needs_store
@td.needs_runner
class Prompts(td.DialogueCase):
    def setUp(self):
        super().setUp()
        self.files: dict[str, object] = {}
        records = types.SimpleNamespace(document_read=self.document_read)
        patcher = mock.patch.object(wa, "_records", return_value=records)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agent = f"prj_{uuid.uuid4().hex[:12]}"
        for name, value in (("_service_project_root", self.root),
                            ("_service_project_id", self.agent)):
            patcher = mock.patch.object(wa, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def document_read(self, cap, key):
        """The project's prompt files, as the records adapter answers them."""
        path = self.files.get(key)
        if path is None or not path.is_file():
            return None
        return {"body": path.read_text(), "path": str(path), "scope": ("project", None)}

    def write_file(self, key: str, data: bytes):
        path = self.root / "capabilities" / "whatsapp" / "service" / f"{key}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.files[key] = path
        return path

    def put(self, key: str, body: str) -> dict:
        args = types.SimpleNamespace(prompts_command="put", key=key, base=None, note=None)
        with mock.patch("sys.stdin", io.StringIO(body)):
            return wa.cmd_prompts(args)

    def test_a_saved_prompt_is_what_the_next_turn_and_job_read_with_no_file(self):
        d = self.make(project_id=self.agent)
        bundle = wa._service_template("job-worker.md").read_text().strip()
        self.assertEqual(d.context_document("job-worker"), bundle)
        for key in wa.SERVICE_DOCUMENTS:
            self.put(key, f"{key}: the agent's own text\n")
        for key in wa.SERVICE_DOCUMENTS:
            self.assertEqual(d.context_document(key), f"{key}: the agent's own text")
        self.put("context", "a second version\n")
        self.assertEqual(d.context_document(), "a second version")
        shown = wa.cmd_prompts(types.SimpleNamespace(prompts_command="show", key="context"))
        self.assertEqual((shown["source"], shown["body"]), ("store", "a second version\n"))
        listed = {row["key"]: row for row in
                  wa.cmd_prompts(types.SimpleNamespace(prompts_command="list"))["prompts"]}
        self.assertEqual({k: r["source"] for k, r in listed.items()},
                         {k: "store" for k in wa.SERVICE_DOCUMENTS})

    def test_a_project_not_yet_moved_reads_its_file_until_the_store_holds_one(self):
        d = self.make(project_id=self.agent)
        self.write_file("context", b"FILE CONTEXT\n")
        self.assertEqual(d.context_document(), "FILE CONTEXT")
        self.put("context", "STORED CONTEXT\n")
        self.assertEqual(d.context_document(), "STORED CONTEXT")

    def test_the_import_keeps_the_files_text_byte_for_byte_and_removes_them(self):
        texts = {"context": "Контекст\r\nline two  \n\n".encode(),
                 "delegation": b"no final newline",
                 "job-worker": b"\tindented\n\n\n"}
        paths = {key: self.write_file(key, data) for key, data in texts.items()}
        plan = wa._prompts_import(self.root, apply=False)
        self.assertEqual([(r["key"], r["action"]) for r in plan["prompts"]],
                         [(k, "write") for k in wa.SERVICE_DOCUMENTS])
        self.assertTrue(all(p.is_file() for p in paths.values()))
        done = wa._prompts_import(self.root, apply=True)
        self.assertEqual([r["key"] for r in done["imported"]], list(wa.SERVICE_DOCUMENTS))
        self.assertEqual(sorted(done["removed"]), sorted(str(p) for p in paths.values()))
        self.assertFalse(any(p.exists() for p in paths.values()))
        db = wa._prompts_db(self.root)
        self.addCleanup(db.close)
        for key, data in texts.items():
            self.assertEqual(wa._stored_prompt(db, self.agent, key)["body"].encode(), data)
        self.assertEqual(wa._prompts_import(self.root, apply=True)["prompts"], [])

    def test_a_file_that_differs_from_the_stored_version_refuses_the_import(self):
        self.put("context", "stored\n")
        path = self.write_file("context", b"older file\n")
        with self.assertRaises(wa._Refusal) as refused:
            wa._prompts_import(self.root, apply=True)
        self.assertEqual(refused.exception.code, "prompts_import_refused")
        self.assertTrue(path.is_file())
        shown = wa.cmd_prompts(types.SimpleNamespace(prompts_command="show", key="context"))
        self.assertEqual(shown["body"], "stored\n")


if __name__ == "__main__":
    unittest.main()
