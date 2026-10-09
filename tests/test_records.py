"""The files a project keeps its configuration in, read and written through
the records adapter."""

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

    def _declare_globally(self, body: dict) -> None:
        (self.globals / "coolify").mkdir(parents=True, exist_ok=True)
        (self.globals / "coolify" / "connections.json").write_text(json.dumps(body))

    def _declare_in_project(self, body: dict) -> None:
        (self.env / "coolify").mkdir(parents=True, exist_ok=True)
        (self.env / "coolify" / "connections.json").write_text(json.dumps(body))

    def test_a_grant_only_project_entry_does_not_blank_the_inherited_identity(self):
        """One file holds both records here, so a project that writes nothing but
        a decision writes an entry with no identity fields. It decides about the
        global connection; it never replaces it with a blank one."""
        self._declare_globally({"connections": {"default": {
            "base_url": "https://coolify.example", "secret_env": "COOLIFY_TOKEN"}}})
        self._declare_in_project({"connections": {"default": {"enabled": True}}})

        identity = self.r.resolve("coolify", "connection")["default"]
        self.assertEqual(identity["value"], {"base_url": "https://coolify.example",
                                             "secret_env": "COOLIFY_TOKEN"})
        self.assertEqual(identity["scope"], "global")
        usable = self.r.connections("coolify")["default"]
        self.assertEqual(usable["value"]["base_url"], "https://coolify.example")

    def test_a_fieldless_entry_with_nothing_to_inherit_is_still_a_connection(self):
        """The shape `audit` writes: an entry may carry a decision and no address
        at all, and where no scope below declares one it is the whole identity."""
        self._declare_in_project({"default": "a", "connections": {
            "a": {}, "b": {"allow_write": False}}})
        usable = self.r.connections("coolify")
        self.assertEqual(sorted(usable), ["a", "b"])
        self.assertFalse(usable["b"]["allow_write"])

    def test_a_global_connection_is_withheld_from_a_project_that_did_not_grant_it(self):
        self._declare_globally({"connections": {"personal": {
            "base_url": "https://coolify.example", "secret_env": "COOLIFY_TOKEN"}}})
        self.assertEqual(self.r.connections("coolify"), {})
        withheld = self.r.connections("coolify", include_disabled=True)["personal"]
        self.assertFalse(withheld["enabled"])
        self.assertEqual(withheld["scope"], ("global", None))

    def test_a_global_grant_does_not_bless_a_global_connection(self):
        self._declare_globally({"connections": {"personal": {
            "base_url": "https://coolify.example", "enabled": True}}})
        self.assertEqual(self.r.connections("coolify"), {})

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


class OpenRecords(unittest.TestCase):
    """Records are kept in files only, whatever a project declares."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = build_envelope(self.root, "fixture", str(uuid.uuid4()))
        self.globals = self.root / "config"

    def tearDown(self):
        self.tmp.cleanup()

    def _declare(self, **fields):
        body = json.loads((self.env / "project.json").read_text())
        body.update(fields)
        (self.env / "project.json").write_text(json.dumps(body))

    def test_a_project_that_says_nothing_reads_files(self):
        records = S.open_records(self.env, self.globals)
        self.assertEqual(records.mode, "files")
        self.assertEqual(records.source, str(self.env))
        self.assertEqual(records.get("clickup", "identifier", "flat-one"), "no-note")

    def test_a_project_declaring_files_reads_files(self):
        self._declare(store="files")
        self.assertEqual(S.open_records(self.env, self.globals).mode, "files")

    def test_a_project_declaring_a_database_is_refused(self):
        for declared in ("db", "maybe"):
            with self.subTest(declared=declared):
                self._declare(store=declared)
                with self.assertRaises(S.StoreError) as caught:
                    S.open_records(self.env, self.globals)
                self.assertEqual(caught.exception.slug, "bad_store_mode")

    def test_project_only_leaves_the_global_scope_out(self):
        (self.globals / "clickup").mkdir(parents=True)
        (self.globals / "clickup" / "identifiers.json").write_text(json.dumps(
            {"global-only": {"value": "hidden", "note": ""}}))
        self.assertIn("global-only", S.open_records(self.env, self.globals)
                      .resolve("clickup", "identifier"))
        self.assertNotIn("global-only", S.open_records(self.env, self.globals,
                                                       project_only=True)
                         .resolve("clickup", "identifier"))

    def test_the_tier_keeps_no_database(self):
        for name in ("Store", "SQLiteStore", "PostgresStore", "StoreRecords",
                     "open_store", "records_mode", "default_store_path",
                     "read_store_setting", "find_store_setting", "store_setting_url"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(S, name))
        self.assertNotIn("sqlite3", (Path(S.__file__)).read_text())


if __name__ == "__main__":
    unittest.main()
