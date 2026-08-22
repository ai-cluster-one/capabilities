"""The two places a project may keep its configuration, held to one answer.

The point of an adapter is that nothing downstream can tell which one it got.
That is only true if both give the same answer to the same question, so the
central test here asks every question twice."""

import json
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contract"))

import store as S  # noqa: E402


def build_envelope(root: Path, slug: str, project_id: str, mode: str = "files") -> Path:
    """An envelope with one of everything the layout can hold."""
    env = root / "capabilities"
    (env / "clickup").mkdir(parents=True)
    (env / "telegram" / "service" / "context").mkdir(parents=True)
    (env / "telegram" / "reference").mkdir(parents=True)

    (env / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "id": project_id,
         "slug": slug, **({"store": mode} if mode != "files" else {})}))
    (env / "settings.json").write_text(json.dumps(
        {"capabilities": {"clickup": {"enabled": True},
                          "telegram": {"enabled": True, "allow_write": True}}}))
    (env / "clickup" / "identifiers.json").write_text(json.dumps(
        {"identifiers": {"capabilities-board": {"value": "901213", "note": "the board"},
                         "flat-one": "no-note"}}))
    (env / "clickup" / "connections.json").write_text(json.dumps(
        {"default": "callva",
         "connections": {"callva": {"token_env": "CLICKUP_TOKEN", "allow_write": True},
                         "legacy": {"token_env": "OLD_TOKEN", "enabled": False}}}))
    (env / "telegram" / "connections.json").write_text(json.dumps(
        {"connections": {"8200881535": {"session_env": "TG_SESSION"}}}))
    (env / "telegram" / "service" / "settings.json").write_text(json.dumps(
        {"worker": "claude", "tail_size": 40}))
    (env / "telegram" / "service" / "context.md").write_text("the service prompt\n")
    (env / "telegram" / "service" / "context" / "iishnitsa.md").write_text("a room's prose\n")
    (env / "telegram" / "reference" / "project_session.md").write_text("a reference\n")
    return env


COLLECTIONS_UNDER_TEST = (
    ("capabilities", "policy"),
    ("clickup", "identifier"),
    ("clickup", "connection"),
    ("clickup", "grant"),
    ("clickup", "setting"),
    ("telegram", "connection"),
    ("telegram", "setting"),
)


class FileAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.slug = "fixture-" + uuid.uuid4().hex[:8]
        self.project_id = str(uuid.uuid4())
        self.env = build_envelope(self.root, self.slug, self.project_id)
        self.globals = self.root / "config"
        self.globals.mkdir()
        self.r = S.FileRecords(self.env, self.globals, self.project_id, self.slug)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_every_shape_the_layout_holds(self):
        self.assertEqual(self.r.get("clickup", "identifier", "capabilities-board"), "901213")
        self.assertEqual(self.r.get("clickup", "identifier", "flat-one"), "no-note")
        self.assertEqual(
            self.r.resolve("clickup", "identifier")["capabilities-board"]["note"], "the board")
        self.assertEqual(self.r.get("clickup", "connection", "callva"),
                         {"token_env": "CLICKUP_TOKEN"})
        self.assertEqual(self.r.get("clickup", "grant", "callva"), {"allow_write": True})
        self.assertEqual(self.r.get("clickup", "setting", "connection.default"), "callva")
        self.assertEqual(self.r.get("telegram", "setting", "tail_size"), 40)
        self.assertEqual(self.r.get("capabilities", "policy", "telegram"),
                         {"enabled": True, "allow_write": True})

    def test_a_grant_is_read_apart_from_the_identity_it_decides(self):
        """The file keeps them in one object; the records surface keeps them
        apart, because who a connection is and what it may do are different
        facts with different writers."""
        identity = self.r.get("clickup", "connection", "callva")
        self.assertNotIn("allow_write", identity)
        self.assertEqual(self.r.get("clickup", "grant", "legacy"), {"enabled": False})

    def test_a_disabled_connection_is_absent_unless_asked_for(self):
        self.assertNotIn("legacy", self.r.connections("clickup"))
        self.assertIn("legacy", self.r.connections("clickup", include_disabled=True))
        self.assertTrue(self.r.connections("clickup")["callva"]["allow_write"])

    def test_writes_land_where_the_reader_looks(self):
        self.r.set("clickup", "identifier", "new-id", "42", note="minted here")
        self.assertEqual(self.r.get("clickup", "identifier", "new-id"), "42")
        body = json.loads((self.env / "clickup" / "identifiers.json").read_text())
        self.assertEqual(body["identifiers"]["new-id"], {"value": "42", "note": "minted here"})

        self.r.set("clickup", "grant", "legacy", {"enabled": True})
        self.assertIn("legacy", self.r.connections("clickup"))

        self.assertTrue(self.r.delete("clickup", "identifier", "new-id"))
        self.assertIsNone(self.r.get("clickup", "identifier", "new-id"))
        self.assertFalse(self.r.delete("clickup", "identifier", "new-id"))

    def test_a_flat_envelope_is_written_flat(self):
        """The shape `audit` holds every capability to: labels at the top level,
        each carrying a value and a note."""
        flat = self.env / "telegram" / "identifiers.json"
        flat.write_text(json.dumps({"already": {"value": "here", "note": ""}}))
        self.r.set("telegram", "identifier", "audit-probe", {"v": 1}, note="a probe")
        body = json.loads(flat.read_text())
        self.assertNotIn("identifiers", body)
        self.assertEqual(body["audit-probe"], {"value": {"v": 1}, "note": "a probe"})
        self.assertEqual(self.r.get("telegram", "identifier", "already"), "here")

    def test_a_project_entry_shadows_a_global_one_by_entry(self):
        (self.globals / "clickup").mkdir(parents=True)
        (self.globals / "clickup" / "identifiers.json").write_text(json.dumps(
            {"identifiers": {"capabilities-board": {"value": "global"},
                             "global-only": {"value": "kept"}}}))
        resolved = self.r.resolve("clickup", "identifier")
        self.assertEqual(resolved["capabilities-board"]["value"], "901213")
        self.assertEqual(resolved["capabilities-board"]["scope"], "project")
        self.assertEqual(resolved["global-only"]["value"], "kept")
        self.assertEqual(resolved["global-only"]["scope"], "global")

    def test_project_only_does_not_inherit_global_entries(self):
        (self.globals / "clickup").mkdir(parents=True)
        (self.globals / "clickup" / "identifiers.json").write_text(json.dumps(
            {"global-only": {"value": "hidden", "note": ""}}))
        project_only = S.FileRecords(
            self.env, self.globals, self.project_id, self.slug,
            include_global=False)
        self.assertNotIn("global-only", project_only.resolve("clickup", "identifier"))

    def test_documents_are_found_by_the_key_the_store_would_use(self):
        self.assertEqual(
            sorted(self.r.document_keys("telegram")),
            ["context", "context.iishnitsa", "reference.project-session"])
        doc = self.r.document_read("telegram", "context.iishnitsa")
        self.assertEqual(doc["body"], "a room's prose\n")
        self.assertEqual(doc["scope"], ("project", self.project_id))

    def test_automation_scripts_belong_only_to_automations(self):
        scripts = self.env / "automations" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "daily.py").write_text("print('daily')\n")
        self.assertIn("script.daily", self.r.document_keys("automations"))
        self.assertNotIn("script.daily", self.r.document_keys("telegram"))

    def test_the_path_it_hands_out_is_the_file_itself(self):
        path = self.r.document_path("telegram", "context")
        self.assertEqual(path, self.env / "telegram" / "service" / "context.md")
        path.write_text("edited in place\n")
        self.assertEqual(self.r.document_read("telegram", "context")["body"],
                         "edited in place\n")

    def test_a_put_refuses_an_edit_that_started_from_something_else(self):
        doc = self.r.document_read("telegram", "context")
        self.r.document_put("telegram", "context", "changed by someone\n")
        with self.assertRaises(S.StoreError) as caught:
            self.r.document_put("telegram", "context", "mine\n", base=doc["hash"])
        self.assertEqual(caught.exception.slug, "stale_edit")

    def test_a_document_this_project_lacks_is_created_where_its_key_points(self):
        self.r.document_put("telegram", "context.newroom", "fresh\n")
        self.assertTrue((self.env / "telegram" / "service" / "context" / "newroom.md").is_file())
        self.assertEqual(self.r.document_read("telegram", "context.newroom")["body"], "fresh\n")

    def test_a_directory_says_what_it_cannot_answer(self):
        for call in (lambda: self.r.revisions("clickup"),
                     lambda: self.r.document_versions("telegram", "context")):
            with self.assertRaises(S.StoreError) as caught:
                call()
            self.assertEqual(caught.exception.slug, "files_mode")


class ModeIsReadOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.env = build_envelope(Path(self.tmp.name), "fixture", str(uuid.uuid4()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_project_that_says_nothing_keeps_files(self):
        self.assertEqual(S.records_mode(self.env)[0], "files")

    def test_the_declaration_is_what_answers(self):
        body = json.loads((self.env / "project.json").read_text())
        body["store"] = "db"
        (self.env / "project.json").write_text(json.dumps(body))
        self.assertEqual(S.records_mode(self.env)[0], "db")

    def test_a_declaration_nobody_understands_is_refused(self):
        (self.env / "project.json").write_text(json.dumps({"slug": "x", "store": "maybe"}))
        with self.assertRaises(S.StoreError) as caught:
            S.records_mode(self.env)
        self.assertEqual(caught.exception.slug, "bad_store_mode")


class BothAdaptersAgree(unittest.TestCase):
    """The load-bearing test. Every question is asked of a directory and of a
    database holding the same records, and the answers are compared."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.slug = "fixture-" + uuid.uuid4().hex[:8]
        self.project_id = str(uuid.uuid4())
        self.env = build_envelope(self.root, self.slug, self.project_id)
        self.globals = self.root / "config"
        self.globals.mkdir()
        self.files = S.FileRecords(self.env, self.globals, self.project_id, self.slug)

        self.store = S.SQLiteStore.open(str(self.root / "store.db"))
        self.store.migrate()
        self.store.project_register(self.project_id, self.slug)
        self.db = S.StoreRecords(self.store, S.Scopes(project=self.slug),
                                 ("project", self.slug))
        for capability, collection in COLLECTIONS_UNDER_TEST:
            for key, row in self.files.resolve(capability, collection).items():
                self.db.set(capability, collection, key, row["value"], note=row["note"])
        for key in self.files.document_keys("telegram"):
            doc = self.files.document_read("telegram", key)
            self.db.document_put("telegram", key, doc["body"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_every_collection_resolves_to_the_same_values(self):
        for capability, collection in COLLECTIONS_UNDER_TEST:
            with self.subTest(capability=capability, collection=collection):
                as_files = {k: v["value"] for k, v in
                            self.files.resolve(capability, collection).items()}
                as_rows = {k: v["value"] for k, v in
                           self.db.resolve(capability, collection).items()}
                self.assertEqual(as_files, as_rows)
                self.assertTrue(as_files, "the fixture holds nothing to compare")

    def test_notes_survive_the_crossing(self):
        self.assertEqual(
            self.db.resolve("clickup", "identifier")["capabilities-board"]["note"],
            "the board")

    def test_connections_are_decided_the_same_way(self):
        for capability in ("clickup", "telegram"):
            with self.subTest(capability=capability):
                a = self.files.connections(capability)
                b = self.db.connections(capability)
                self.assertEqual(sorted(a), sorted(b))
                for cid in a:
                    self.assertEqual(a[cid]["value"], b[cid]["value"])
                    self.assertEqual(a[cid]["allow_write"], b[cid]["allow_write"])
                    self.assertEqual(a[cid]["enabled"], b[cid]["enabled"])

    def test_a_disabled_connection_is_dropped_by_both(self):
        self.assertNotIn("legacy", self.files.connections("clickup"))
        self.assertNotIn("legacy", self.db.connections("clickup"))

    def test_documents_answer_to_the_same_keys_with_the_same_bodies(self):
        self.assertEqual(sorted(self.files.document_keys("telegram")),
                         sorted(self.db.document_keys("telegram")))
        for key in self.files.document_keys("telegram"):
            with self.subTest(key=key):
                self.assertEqual(self.files.document_read("telegram", key)["body"],
                                 self.db.document_read("telegram", key)["body"])

    def test_a_document_hashes_alike_on_both_sides(self):
        """The hash an edit is checked against must not depend on where the
        text was kept, or a checkout taken in one mode could never be put back
        in the other."""
        for key in self.files.document_keys("telegram"):
            with self.subTest(key=key):
                self.assertEqual(self.files.document_read("telegram", key)["hash"],
                                 self.db.document_read("telegram", key)["hash"])

    def test_only_the_store_hands_out_nothing_to_open(self):
        self.assertIsNotNone(self.files.document_path("telegram", "context"))
        self.assertIsNone(self.db.document_path("telegram", "context"))

    def test_project_only_store_does_not_inherit_global_entries(self):
        self.store.config_set("clickup", "identifier", "global-only", "hidden",
                              ("global", ""))
        project_only = S.StoreRecords(
            self.store, S.Scopes(project=self.slug, include_global=False),
            ("project", self.slug))
        self.assertNotIn("global-only", project_only.resolve("clickup", "identifier"))


if __name__ == "__main__":
    unittest.main()
