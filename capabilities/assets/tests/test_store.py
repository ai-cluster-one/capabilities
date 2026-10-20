"""The tables as the store holds them, and what the store itself refuses: a
version is never edited or deleted, one version per document is active, and a
document changes only by being retired."""

from __future__ import annotations

import psycopg
import pytest

from _cli import connect, needs_store, world  # noqa: F401

pytestmark = needs_store


def _migrated(world):  # noqa: F811
    root = world.project()
    world.ok(root, "migrate")
    return root, connect(world.schema)


def test_the_tables_have_the_planned_shape(world):  # noqa: F811
    _root, conn = _migrated(world)
    with conn:
        columns = {}
        for table, column, kind, nullable, generated in conn.execute(
                "SELECT table_name, column_name, data_type, is_nullable, is_generated "
                "FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name LIKE 'assets\\_%' ORDER BY table_name, ordinal_position"):
            columns.setdefault(table, []).append((column, kind, nullable, generated))
        assert set(columns) == {"assets_documents", "assets_versions"}
        assert [c[0] for c in columns["assets_documents"]] == [
            "project_id", "id", "kind", "name", "created_at", "created_by", "retired_at"]
        assert [c[0] for c in columns["assets_versions"]] == [
            "id", "document_id", "title", "body", "search", "active", "created_at",
            "created_by", "note"]
        types = {c[0]: c[1] for c in columns["assets_documents"] + columns["assets_versions"]}
        assert types["id"] == "uuid" and types["document_id"] == "uuid"
        assert types["created_at"] == types["retired_at"] == "timestamp with time zone"
        assert types["search"] == "tsvector" and types["active"] == "boolean"
        assert next(c for c in columns["assets_versions"] if c[0] == "search")[3] == "ALWAYS"
        indexes = dict(conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()"))
        assert "WHERE (retired_at IS NULL)" in indexes["assets_documents_name"]
        assert "UNIQUE" in indexes["assets_documents_name"]
        assert "(project_id, kind, name)" in indexes["assets_documents_name"]
        assert "UNIQUE" in indexes["assets_versions_active"]
        assert "WHERE active" in indexes["assets_versions_active"]
        assert "USING gin (search)" in indexes["assets_versions_search"]
        generated = conn.execute(
            "SELECT generation_expression FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name = 'search'").fetchone()[0]
        assert "'simple'::regconfig" in generated
        commented = {row[0] for row in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND obj_description(c.oid, 'pg_class') "
            "IS NOT NULL")}
        assert {"assets_documents", "assets_versions"} <= commented
        ledger = [row[0] for row in conn.execute(
            "SELECT step FROM schema_ledger WHERE owner = 'assets' ORDER BY applied_at, step")]
        assert ledger == ["0001-documents", "0002-versions", "0003-guards"]


def test_the_store_refuses_what_a_column_does_not_mean(world):  # noqa: F811
    _root, conn = _migrated(world)
    with conn:
        for values in (("", "plan", "x"), ("prj_a", "draft", "x"), ("prj_a", "plan", "")):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO assets_documents (project_id, kind, name, created_by) "
                             "VALUES (%s, %s, %s, 'tester')", values)


def test_a_version_is_immutable_and_never_deleted(world):  # noqa: F811
    root, conn = _migrated(world)
    saved = world.ok(root, "save", "plans/p", "--from", "-", stdin=b"# P\n\none\n")
    version = saved["version"]["id"]
    with conn:
        for statement in ("UPDATE assets_versions SET body = 'changed' WHERE id = %s",
                          "UPDATE assets_versions SET title = 'changed' WHERE id = %s",
                          "UPDATE assets_versions SET note = 'changed' WHERE id = %s",
                          "DELETE FROM assets_versions WHERE id = %s"):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(statement, (version,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("TRUNCATE assets_versions CASCADE")
        conn.execute("UPDATE assets_versions SET active = false WHERE id = %s", (version,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE assets_versions SET active = true WHERE id = %s", (version,))
        body = conn.execute("SELECT body FROM assets_versions WHERE id = %s",
                            (version,)).fetchone()[0]
        assert body == "# P\n\none\n"


def test_one_version_per_document_is_active(world):  # noqa: F811
    root, conn = _migrated(world)
    saved = world.ok(root, "save", "plans/p", "--from", "-", stdin=b"# P\n")
    with conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO assets_versions (document_id, title, body, created_by) "
                         "VALUES (%s, 'P', 'another', 'tester')", (saved["document"]["id"],))


def test_a_document_is_never_deleted_and_retires_once(world):  # noqa: F811
    root, conn = _migrated(world)
    saved = world.ok(root, "save", "plans/p", "--from", "-", stdin=b"# P\n")
    document = saved["document"]["id"]
    with conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("DELETE FROM assets_documents WHERE id = %s", (document,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE assets_documents SET name = 'plans/q' WHERE id = %s",
                         (document,))
        conn.execute("UPDATE assets_documents SET retired_at = now() WHERE id = %s",
                     (document,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE assets_documents SET retired_at = NULL WHERE id = %s",
                         (document,))
