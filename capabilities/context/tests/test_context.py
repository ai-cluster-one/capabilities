"""context's tables and verbs, against a throwaway PostgreSQL.

The module starts a cluster on a loopback port when `initdb` and `pg_ctl` are
on PATH and psycopg is importable, and skips otherwise; each test gets a
database of its own and a project that exists only for the test. The CLI runs
as a child with a home of its own, so nothing reaches the real one.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = CAPABILITY / "bin" / "context"
MANAGER = CAPABILITY.parents[1] / "bin" / "capabilities"
ADMIN = "pgadmin"
SCHEMA = "ctx"
PROJECT_ID = "prj_" + "0a1b2c3d4e5f"
CLEARED_PREFIXES = ("AGENTKIT_DB_", "CAPABILITIES_", "CLAUDE")

FILES = {
    "identity/MISSION.md": ("---\ntitle: Mission\ndescription: Always load for the mission.\n"
                            "load: inline\norder: 10\n---\n\n# Mission\n\nKeep it whole.\n"),
    "guidelines/STYLE.md": ("---\ntitle: Style\ndescription: Load when writing — any prose.\n"
                            "load: stub\norder: 120\n---\n\n# Style\n\nÜks rida, одна строка.\n"
                            "No newline at the end"),
    "product/deep/NOTE.md": ("---\ntitle: Note\ndescription: Load when deep.\nload: stub\n"
                             "order: -5\n---\n"),
}


def load_module():
    spec = importlib.util.spec_from_loader("context_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI)
    source = CLI.read_text().replace('if __name__ == "__main__":\n    main()\n', "")
    exec(compile(source, str(CLI), "exec"), module.__dict__)
    return module


# --- a throwaway PostgreSQL ------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Cluster:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.port = _free_port()
        self.env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}

    def run(self, *argv: str) -> None:
        result = subprocess.run(argv, env=self.env, text=True, capture_output=True,
                                timeout=120)
        assert result.returncode == 0, (argv, result.stdout + result.stderr)

    def start(self) -> None:
        self.run("initdb", "-D", str(self.data), "-U", ADMIN, "-E", "UTF8",
                 "--locale=en_US.UTF-8", "--auth=trust")
        with (self.data / "postgresql.conf").open("a") as conf:
            conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {self.port}\n"
                       "unix_socket_directories = ''\n")
        self.run("pg_ctl", "-D", str(self.data), "-l", str(self.root / "pg.log"),
                 "-w", "-t", "60", "start")

    def stop(self) -> None:
        subprocess.run(["pg_ctl", "-D", str(self.data), "-m", "immediate", "stop"],
                       env=self.env, capture_output=True, timeout=60)

    def connect(self, database: str = "postgres"):
        import psycopg
        conn = psycopg.connect(host="127.0.0.1", port=self.port, user=ADMIN,
                               dbname=database, connect_timeout=10)
        conn.autocommit = True
        return conn

    def database(self) -> str:
        name = "t_" + uuid.uuid4().hex[:12]
        with self.connect() as conn:
            conn.execute(f"CREATE DATABASE {name} ENCODING 'UTF8' TEMPLATE template0")
        return name

    def url(self, database: str) -> str:
        return f"postgresql://{ADMIN}@127.0.0.1:{self.port}/{database}?sslmode=disable"


@pytest.fixture(scope="module")
def cluster():
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-context-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


class Lab:
    def __init__(self, tmp: Path, cluster: Cluster):
        self.tmp = tmp
        self.cluster = cluster
        self.database = cluster.database()
        self.project = tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        envelope = self.project / "capabilities"
        envelope.mkdir()
        (envelope / "project.json").write_text(json.dumps(
            {"schema": "capabilities.project.v1", "slug": "lab", "id": PROJECT_ID}))
        (envelope / "settings.json").write_text(json.dumps(
            {"capabilities": {"context": {"enabled": True}}}))
        self.context = self.project / "context"
        for rel, text in FILES.items():
            path = self.context / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLEARED_PREFIXES)}
        env.update({
            "HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "CAPABILITIES_HOME": str(self.tmp / "registry"),
            "CAPABILITIES_MANAGER_BIN": str(MANAGER),
            "AGENTKIT_DB_URL": self.cluster.url(self.database),
            "AGENTKIT_DB_SCHEMA": SCHEMA,
            "AGENT_NODE_NAME": "test-node",
            "USER": "tester",
            **extra,
        })
        return env

    def run(self, *args: str, stdin: str | None = None, **extra: str):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=self.project,
                              env=self.env(**extra), input=stdin, text=True,
                              capture_output=True, timeout=180)

    def ok(self, *args: str, **kw):
        result = self.run(*args, **kw)
        assert result.returncode == 0, result.stderr
        try:
            return json.loads(result.stdout)
        except ValueError:
            return result.stdout

    def refused(self, *args: str, **kw) -> tuple[int, dict]:
        result = self.run(*args, **kw)
        line = next(line for line in reversed(result.stderr.splitlines())
                    if line.startswith('{"error"'))
        return result.returncode, json.loads(line)["error"]

    def sql(self, query: str, params: tuple = ()):
        with self.cluster.connect(self.database) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else None


@pytest.fixture()
def lab(tmp_path, cluster):
    return Lab(tmp_path, cluster)


def _tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


# --- the file form ---------------------------------------------------------------

def test_the_file_form_reads_back_byte_for_byte_and_refuses_what_would_not():
    cli = load_module()
    for text in FILES.values():
        assert cli.render(cli.parse(text)) == text
    version = cli.parse(FILES["product/deep/NOTE.md"])
    assert (version["position"], version["body"]) == (-5, "")
    for bad in ("no front matter\n",
                "---\ntitle: T\ndescription: D\nload: stub\norder: 1\n",
                "---\ndescription: D\ntitle: T\nload: stub\norder: 1\n---\n",
                "---\ntitle: T\ndescription: D\nload: always\norder: 1\n---\n",
                "---\ntitle: T\ndescription: D\nload: stub\norder: 07\n---\n",
                "---\ntitle: T\ndescription: D\nload: stub\norder: 1\nextra: x\n---\n",
                "---\ntitle:  T\ndescription: D\nload: stub\norder: 1\n---\n"):
        with pytest.raises(cli.Unreadable):
            cli.parse(bad)


# --- the tables ------------------------------------------------------------------

def test_the_ledger_creates_the_tables_with_their_indexes_comments_and_guard(lab):
    lab.ok("list", "--json")
    steps = [r[0] for r in lab.sql(
        f"SELECT step FROM {SCHEMA}.schema_ledger WHERE owner = 'context' ORDER BY step")]
    assert steps == ["0001-documents", "0002-versions", "0003-versions-guard"]
    indexes = {r[0]: r[1] for r in lab.sql(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s", (SCHEMA,))}
    assert "WHERE active" in indexes["context_versions_active"]
    assert "COALESCE(project_id, ''::text), name" in indexes["context_documents_name"]
    assert "WHERE (retired_at IS NULL)" in indexes["context_documents_name"]
    for table in ("context_documents", "context_versions"):
        [(comment,)] = lab.sql("SELECT obj_description(%s::regclass)", (f"{SCHEMA}.{table}",))
        assert comment
    commented = {r[0] for r in lab.sql(
        "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass "
        "AND col_description(a.attrelid, a.attnum) IS NOT NULL", (f"{SCHEMA}.context_versions",))}
    assert {"load", "position", "body", "active", "note"} <= commented


def test_the_store_keeps_versions_immutable_but_for_clearing_active(lab):
    import psycopg
    lab.ok("import")
    [(vid,)] = lab.sql(f"SELECT v.id FROM {SCHEMA}.context_versions v JOIN "
                       f"{SCHEMA}.context_documents d ON d.id = v.document_id "
                       "WHERE d.name = 'identity/MISSION'")
    for statement in (f"UPDATE {SCHEMA}.context_versions SET body = 'x' WHERE id = %s",
                      f"UPDATE {SCHEMA}.context_versions SET title = 'x', active = false "
                      "WHERE id = %s",
                      f"DELETE FROM {SCHEMA}.context_versions WHERE id = %s"):
        with pytest.raises(psycopg.errors.CheckViolation):
            lab.sql(statement, (vid,))
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(f"TRUNCATE {SCHEMA}.context_versions CASCADE")
    lab.sql(f"UPDATE {SCHEMA}.context_versions SET active = false WHERE id = %s", (vid,))
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(f"UPDATE {SCHEMA}.context_versions SET active = true WHERE id = %s", (vid,))
    [(doc,)] = lab.sql(f"SELECT document_id FROM {SCHEMA}.context_versions WHERE id = %s", (vid,))
    insert = (f"INSERT INTO {SCHEMA}.context_versions (document_id, title, description, load, "
              "position, body, created_by) VALUES (%s, 't', 'd', 'stub', 1, '', 'x')")
    lab.sql(insert, (doc,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        lab.sql(insert, (doc,))


# --- import and export -----------------------------------------------------------

def test_import_then_export_writes_every_file_back_byte_for_byte(lab):
    (lab.context / "notes.txt").write_text("not doctrine\n")
    first = lab.ok("import")
    assert first["counts"] == {"created": 3, "skipped": 1}
    assert first["folder"] == str(lab.context.resolve())
    assert [s["path"] for s in first["skipped"]] == ["notes.txt"]
    (lab.context / "notes.txt").unlink()
    out = lab.tmp / "out"
    exported = lab.ok("export", str(out))
    assert exported["counts"] == {"written": 3}
    assert _tree(out) == _tree(lab.context)
    assert lab.ok("export", str(out))["counts"] == {"unchanged": 3}
    assert lab.ok("import")["counts"] == {"unchanged": 3}


def test_import_versions_a_changed_file_and_refuses_a_folder_with_an_unreadable_one(lab):
    lab.ok("import")
    mission = lab.context / "identity" / "MISSION.md"
    mission.write_text(FILES["identity/MISSION.md"].replace("whole", "whole and current"))
    again = lab.ok("import")
    assert again["counts"] == {"versioned": 1, "unchanged": 2}
    assert len(lab.ok("history", "identity/MISSION", "--json")["versions"]) == 2

    (lab.context / "BROKEN.md").write_text("no front matter\n")
    mission.write_text(FILES["identity/MISSION.md"])
    code, error = lab.refused("import")
    assert (code, error["code"]) == (6, "not_canonical")
    assert "BROKEN.md" in error["message"]
    assert len(lab.ok("history", "identity/MISSION", "--json")["versions"]) == 2


# --- save, show, history, diff ---------------------------------------------------

def test_save_adds_a_version_against_its_base_and_refuses_a_stale_one(lab):
    lab.ok("import")
    shown = lab.ok("show", "identity/MISSION", "--json")
    base = shown["version"]
    assert shown["rendered"] == FILES["identity/MISSION.md"]
    assert lab.run("show", "identity/MISSION").stdout == FILES["identity/MISSION.md"]

    edited = shown["rendered"].replace("order: 10", "order: 11").replace("whole", "intact")
    saved = lab.ok("save", "identity/MISSION", "--from", "-", "--base", base,
                   "--note", "say intact", stdin=edited)
    assert (saved["changed"], saved["outcome"], saved["base"]) == (True, "versioned", base)
    assert lab.ok("show", "identity/MISSION", "--json")["order"] == 11
    assert lab.ok("show", "identity/MISSION", "--version", base[:8], "--json")["order"] == 10

    code, error = lab.refused("save", "identity/MISSION", "--from", "-", "--base", base,
                              stdin=edited + "more\n")
    assert (code, error["code"]) == (7, "stale_base")
    assert saved["version"] in error["message"]

    same = lab.ok("save", "identity/MISSION", "--from", "-", stdin=edited)
    assert (same["changed"], same["version"]) == (False, saved["version"])

    history = lab.ok("history", "identity/MISSION", "--json")["versions"]
    assert [v["version"] for v in history] == [saved["version"], base]
    assert [v["active"] for v in history] == [True, False]
    assert history[0]["note"] == "say intact"
    assert history[0]["created_by"] == "tester@test-node"

    diff = lab.run("diff", "identity/MISSION").stdout
    assert f"--- identity/MISSION.md@{base[:8]}" in diff
    assert "-order: 10\n+order: 11\n" in diff and "+Keep it intact.\n" in diff
    assert lab.run("diff", "identity/MISSION", saved["version"], base).stdout.count("+Keep it whole.") == 1

    code, error = lab.refused("save", "identity/MISSION", "--from", "-", stdin="no front\n")
    assert (code, error["code"]) == (6, "not_canonical")


def test_a_new_name_starts_a_document_and_its_only_version_diffs_against_nothing(lab):
    created = lab.ok("save", "fresh/ONE", "--from", "-", stdin=FILES["identity/MISSION.md"])
    assert (created["outcome"], created["base"]) == ("created", None)
    assert lab.run("diff", "fresh/ONE").stdout.startswith("--- /dev/null\n")
    code, error = lab.refused("save", "fresh/../x", "--from", "-",
                              stdin=FILES["identity/MISSION.md"])
    assert (code, error["code"]) == (6, "input")


# --- scope, retire, the read-only switch -----------------------------------------

def test_global_documents_are_apart_from_the_projects_and_retire_frees_a_name(lab):
    lab.ok("import")
    shared = lab.tmp / "shared"
    shutil.copytree(lab.context, shared)
    assert lab.ok("import", str(shared), "--global")["counts"] == {"created": 3}
    assert lab.ok("list", "--global", "--json")["count"] == 3
    assert lab.ok("list", "--json")["count"] == 3
    assert lab.refused("export", "--global")[1]["code"] == "input"

    assert lab.ok("retire", "identity/MISSION", "--global")["retired"] is True
    assert lab.refused("show", "identity/MISSION", "--global")[0] == 3
    assert lab.ok("show", "identity/MISSION", "--json")["title"] == "Mission"
    assert lab.refused("retire", "identity/MISSION", "--global")[0] == 3
    assert lab.ok("save", "identity/MISSION", "--global", "--from", "-",
                  stdin=FILES["identity/MISSION.md"])["outcome"] == "created"
    [(count,)] = lab.sql(f"SELECT count(*) FROM {SCHEMA}.context_documents "
                         "WHERE project_id IS NULL AND name = 'identity/MISSION'")
    assert count == 2


def test_the_read_only_switch_refuses_every_write(lab):
    lab.ok("import")
    for args in (("save", "identity/MISSION", "--from", "-"), ("retire", "identity/MISSION"),
                 ("import",)):
        code, error = lab.refused(*args, stdin=FILES["identity/MISSION.md"],
                                  CAPABILITIES_READ_ONLY="1")
        assert (code, error["code"]) == (4, "read_only_switch")
    assert lab.run("list", CAPABILITIES_READ_ONLY="1").returncode == 0
