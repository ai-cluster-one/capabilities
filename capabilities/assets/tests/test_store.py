"""The tables as the store holds them, and what the store itself refuses: a
version is never edited or deleted, one version per document is active, and a
document changes only by being retired."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
import pytest

from _cli import CLI_PATH, connect, needs_store, world  # noqa: F401

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
        assert set(columns) == {"assets_documents", "assets_versions", "assets_files",
                                "assets_file_operations"}
        assert [c[0] for c in columns["assets_documents"]] == [
            "agent_id", "id", "kind", "name", "created_at", "created_by", "retired_at"]
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
        assert "(agent_id, kind, name)" in indexes["assets_documents_name"]
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
        assert ledger == ["0001-documents", "0002-versions", "0003-guards", "0004-files",
                          "0005-files-guard", "0006-file-operations", "0007-agent-names"]


def test_the_store_refuses_what_a_column_does_not_mean(world):  # noqa: F811
    _root, conn = _migrated(world)
    with conn:
        for values in (("", "plan", "x"), ("prj_a", "draft", "x"), ("prj_a", "plan", "")):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO assets_documents (agent_id, kind, name, created_by) "
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


SHA = "0" * 64


def _file_row(conn, project="prj_a", name="research/x/crash.ips", **over):
    """Insert one assets_files row the way the CLI writes it; `over` replaces a column."""
    row = conn.execute("SELECT gen_random_uuid()").fetchone()[0]
    values = {"agent_id": project, "id": row, "document_id": None, "name": name,
              "media_type": "application/octet-stream", "bytes": 3, "sha256": SHA,
              "storage_key": f"{project}/{row}/{name}", "created_by": "tester", **over}
    columns = ", ".join(values)
    conn.execute(f"INSERT INTO assets_files ({columns}) VALUES "
                 f"({', '.join(['%s'] * len(values))})", list(values.values()))
    return row


def test_the_files_table_has_the_approved_shape(world):  # noqa: F811
    _root, conn = _migrated(world)
    with conn:
        columns = [(c, t, n) for c, t, n in conn.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'assets_files' "
            "ORDER BY ordinal_position")]
        assert [c[0] for c in columns] == [
            "agent_id", "id", "document_id", "name", "media_type", "bytes", "sha256",
            "storage_key", "created_at", "created_by", "retired_at"]
        types = {c: t for c, t, _n in columns}
        assert types["id"] == types["document_id"] == "uuid" and types["bytes"] == "bigint"
        assert types["created_at"] == types["retired_at"] == "timestamp with time zone"
        nullable = {c for c, _t, n in columns if n == "YES"}
        assert nullable == {"document_id", "retired_at"}
        index = dict(conn.execute("SELECT indexname, indexdef FROM pg_indexes "
                                  "WHERE schemaname = current_schema()"))["assets_files_name"]
        assert "UNIQUE" in index and "(agent_id, name)" in index
        assert "WHERE (retired_at IS NULL)" in index
        assert conn.execute("SELECT obj_description('assets_files'::regclass, 'pg_class')"
                            ).fetchone()[0]
        foreign = conn.execute(
            "SELECT confrelid::regclass::text FROM pg_constraint WHERE conrelid = "
            "'assets_files'::regclass AND contype = 'f'").fetchall()
        assert foreign == [("assets_documents",)]


def test_the_store_refuses_what_a_file_column_does_not_mean(world):  # noqa: F811
    _root, conn = _migrated(world)
    with conn:
        for over in ({"agent_id": ""}, {"name": ""}, {"media_type": ""},
                     {"media_type": "octet-stream"}, {"bytes": -1}, {"sha256": "ABC"},
                     {"sha256": "A" * 64}, {"storage_key": "prj_a/elsewhere"},
                     {"created_by": ""}):
            with pytest.raises(psycopg.errors.CheckViolation):
                _file_row(conn, **over)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _file_row(conn, document_id="00000000-0000-0000-0000-000000000000")


def test_a_file_row_changes_only_by_being_retired_once(world):  # noqa: F811
    _root, conn = _migrated(world)
    with conn:
        row = _file_row(conn)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _file_row(conn)
        for statement in ("UPDATE assets_files SET sha256 = repeat('1', 64) WHERE id = %s",
                          "UPDATE assets_files SET bytes = 4 WHERE id = %s",
                          "UPDATE assets_files SET name = 'other' WHERE id = %s",
                          "DELETE FROM assets_files WHERE id = %s"):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(statement, (row,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("TRUNCATE assets_files CASCADE")
        conn.execute("UPDATE assets_files SET retired_at = now() WHERE id = %s", (row,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE assets_files SET retired_at = NULL WHERE id = %s", (row,))
        _file_row(conn)


# Every name and comment in an owner's tables that still says project, where the
# store now says agent: table columns, relations, constraints, triggers, comments, and
# the bodies of the owner's functions.
_SAYS_PROJECT = """
SELECT 'column ' || c.relname || '.' || a.attname FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
   AND a.attname ~* 'project'
UNION ALL SELECT 'relation ' || c.relname FROM pg_class c
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND c.relname ~* 'project'
UNION ALL SELECT 'constraint ' || o.conname FROM pg_constraint o
  JOIN pg_class c ON c.oid = o.conrelid
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND o.conname ~* 'project'
UNION ALL SELECT 'trigger ' || g.tgname FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND g.tgname ~* 'project'
UNION ALL SELECT 'comment on ' || c.relname || coalesce('.' || a.attname, '')
  FROM pg_description d JOIN pg_class c ON c.oid = d.objoid AND d.classoid = 'pg_class'::regclass
  LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid AND d.objsubid > 0
 WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname LIKE %(like)s
   AND d.description ~* 'project'
UNION ALL SELECT 'function ' || p.proname FROM pg_proc p
 WHERE p.pronamespace = %(schema)s::regnamespace AND p.proname LIKE %(like)s
   AND p.prosrc ~* 'project'
"""


def _load_cli():
    spec = importlib.util.spec_from_loader("assets_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI_PATH)
    source = Path(CLI_PATH).read_text().replace('if __name__ == "__main__":\n    main()\n', "")
    exec(compile(source, str(CLI_PATH), "exec"), module.__dict__)
    return module


def test_a_store_at_major_1_is_renamed_in_place_and_keeps_its_rows(world, monkeypatch):  # noqa: F811
    from capabilities_contract import db
    cli = _load_cli()
    old = [step for step in cli.TABLE_STEPS if step[0] != "0007-agent-names"]
    pid = "prj_" + "0a0b0c0d0e0f"
    root = world.project(pid=pid)
    conn = connect(world.schema)
    with conn:
        conn.execute(f'CREATE SCHEMA "{world.schema}"')
        db.migrate(conn, "assets", old, major=1, minor=2, schema=world.schema)
        conn.execute(f'SET search_path TO "{world.schema}"')
        document = conn.execute(
            "INSERT INTO assets_documents (project_id, kind, name, created_by) "
            "VALUES (%s, 'plan', 'plans/kept', 'tester') RETURNING id", (pid,)).fetchone()[0]
        conn.execute("INSERT INTO assets_versions (document_id, title, body, created_by) "
                     "VALUES (%s, 'Kept', '# Kept\n', 'tester')", (document,))
        file = conn.execute("SELECT gen_random_uuid()").fetchone()[0]
        conn.execute("INSERT INTO assets_files (project_id, id, document_id, name, media_type, "
                     "bytes, sha256, storage_key, created_by) VALUES (%s, %s, %s, "
                     "'plans/kept.png', 'image/png', 3, %s, %s, 'tester')",
                     (pid, file, document, SHA, f"{pid}/{file}/plans/kept.png"))
        conn.execute("INSERT INTO assets_file_operations (project_id, file_id, operation, "
                     "created_by) VALUES (%s, %s, 'created', 'tester')", (pid, file))

        # Outside the store upgrade the major is not crossed; inside it, it is.
        monkeypatch.delenv(db.UPGRADE_ENV, raising=False)
        with pytest.raises(db.DbError) as held:
            db.migrate(conn, "assets", cli.TABLE_STEPS, major=cli.TABLES_MAJOR,
                       minor=cli.TABLES_MINOR, schema=world.schema)
        assert held.value.slug == "store_upgrade_needed"
        monkeypatch.setenv(db.UPGRADE_ENV, "1")
        result = db.migrate(conn, "assets", cli.TABLE_STEPS, major=cli.TABLES_MAJOR,
                            minor=cli.TABLES_MINOR, schema=world.schema)
        monkeypatch.delenv(db.UPGRADE_ENV)
        assert result.applied == ["0007-agent-names"]
        assert (result.major, result.minor) == (2, 0)
        conn.execute(f'SET search_path TO "{world.schema}"')
        for table in ("assets_documents", "assets_files", "assets_file_operations"):
            assert conn.execute(f"SELECT agent_id FROM {table}").fetchall() == [(pid,)], table
        assert conn.execute(_SAYS_PROJECT, {"schema": world.schema, "like": "assets\\_%"}
                            ).fetchall() == []
        # The guards compare the renamed column: they still refuse an edit and
        # still let a record be retired once.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE assets_documents SET name = 'plans/other' WHERE id = %s",
                         (document,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE assets_files SET bytes = 4 WHERE id = %s", (file,))
        # A release that knows only major 1 refuses the store rather than misreading it.
        with pytest.raises(db.DbError) as refused:
            db.migrate(conn, "assets", old, major=1, minor=2, schema=world.schema)
        assert refused.value.slug == "schema_too_new"

    assert [d["name"] for d in world.ok(root, "list")["documents"]] == ["plans/kept"]
    [listed] = world.ok(root, "files", "list")["files"]
    assert (listed["name"], listed["document"]) == ("plans/kept.png", "plans/kept")
    [logged] = world.ok(root, "files", "log")["operations"]
    assert (logged["operation"], logged["file"]) == ("created", str(file))
    with connect(world.schema) as conn:
        conn.execute("UPDATE assets_files SET retired_at = now() WHERE id = %s", (file,))
        conn.execute("UPDATE assets_documents SET retired_at = now() WHERE id = %s", (document,))
    assert world.ok(root, "list")["count"] == 0
