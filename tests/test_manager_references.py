"""A project's references in the store: `capabilities_references`.

Every capability's references for a project are one versioned table of the
contract's: the manager imports `capabilities/<name>/reference/*.md` into it
and exports it back byte for byte, a save adds a version under a base check,
and every capability reads a reference the store holds before its file, the
file where the store holds none or no store is configured.

These tests drive the real manager and a real capability (coolify) in a
project that exists only for the test, against a throwaway PostgreSQL on a
loopback port that the module starts when `initdb` and `pg_ctl` are on PATH
and psycopg is importable, and skips otherwise. Each test gets a database of
its own.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
COOLIFY = REPO / "capabilities" / "coolify" / "bin" / "coolify"
ADMIN = "pgadmin"
SCHEMA = "lab"
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")
PROJECT_ID = "prj_" + "5e1f0a2b3c4d"

# Bytes a text read would not keep: CRLF line ends, a non-ASCII character and
# no final newline; and a file that opens with a byte-order mark.
ACCOUNTS = ("---\r\nname: account-map\r\ndescription: How entries map to accounts\r\n"
            "---\r\n\r\nCaf\u00e9 rules apply.").encode("utf-8")
MARKED = "\ufeffNo front matter.\n".encode("utf-8")
TONE = b"---\nname: tone\ndescription: How replies are worded\n---\n\nShort.\n"


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

    def admin(self, database: str = "postgres"):
        import psycopg
        conn = psycopg.connect(host="127.0.0.1", port=self.port, user=ADMIN,
                               dbname=database, connect_timeout=10)
        conn.autocommit = True
        return conn

    def database(self) -> str:
        name = "t_" + uuid.uuid4().hex[:12]
        with self.admin() as conn:
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
    root = Path(tempfile.mkdtemp(prefix="capabilities-references-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


class Lab:
    def __init__(self, tmp_path: Path, cluster: Cluster | None):
        self.tmp = tmp_path
        self.cluster = cluster
        self.project = tmp_path / "project"
        self.envelope = self.project / "capabilities"
        (self.project / ".git").mkdir(parents=True)
        self.envelope.mkdir()
        (self.envelope / "project.json").write_text(json.dumps(
            {"schema": "capabilities.project.v2", "slug": "lab"}))
        (self.envelope / "settings.json").write_text(json.dumps(
            {"capabilities": {"coolify": {"enabled": True}}}))
        lines = [f"CAPABILITIES_PROJECT_ID={PROJECT_ID}"]
        if cluster is not None:
            self.database = cluster.database()
            lines += [f"AGENTKIT_DB_URL={cluster.url(self.database)}",
                      f"AGENTKIT_DB_SCHEMA={SCHEMA}"]
        (self.project / ".env.local").write_text("\n".join(lines) + "\n")

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in DB_KEYS and not k.startswith("CAPABILITIES_")
               and k not in ("AGENT_NODE_NAME", "VIRTUAL_ENV", "EDITOR", "VISUAL")}
        env.update({
            "HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "CAPABILITIES_HOME": str(self.tmp / "registry"),
            "CLAUDE_PROJECT_DIR": str(self.project),
            "USER": "tester",
            "AGENT_NODE_NAME": "mac1",
        })
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, exe: Path, *args: str, stdin: bytes | None = None,
            **extra) -> subprocess.CompletedProcess:
        return subprocess.run([str(exe), *args], cwd=self.project, env=self.env(**extra),
                              capture_output=True, timeout=240, input=stdin)

    def ok(self, exe: Path, *args: str, stdin: bytes | None = None, **extra):
        result = self.run(exe, *args, stdin=stdin, **extra)
        assert result.returncode == 0, (result.stdout + result.stderr).decode()
        try:
            return json.loads(result.stdout)
        except ValueError:
            return result.stdout

    def refused(self, exe: Path, *args: str, **extra) -> tuple[int, dict]:
        result = self.run(exe, *args, **extra)
        assert result.returncode != 0, result.stdout.decode()
        line = next(line for line in reversed(result.stderr.decode().splitlines())
                    if line.lstrip().startswith("{"))
        return result.returncode, json.loads(line)["error"]

    def sql(self, query: str, params: tuple = ()):
        with self.cluster.admin(self.database) as conn:
            conn.execute(f"SET search_path TO {SCHEMA}")
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def reference(self, capability: str, name: str, body: bytes) -> Path:
        path = self.envelope / capability / "reference" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path


@pytest.fixture()
def lab(tmp_path, cluster):
    return Lab(tmp_path, cluster)


def test_a_folder_imported_and_exported_is_the_same_bytes(lab):
    accounts = lab.reference("coolify", "account-map", ACCOUNTS)
    tone = lab.reference("other", "Reply_Tone", TONE)
    marked = lab.reference("other", "marked", MARKED)
    (accounts.parent / "notes.txt").write_text("not a reference")

    done = lab.ok(MANAGER, "references", "import")
    assert sorted((r["capability"], r["name"], r["outcome"]) for r in done["imported"]) \
        == [("coolify", "account-map", "added"), ("other", "Reply_Tone", "added"),
            ("other", "marked", "added")]
    assert done["skipped"] == [str(accounts.parent / "notes.txt")]
    assert lab.sql("SELECT count(*) FROM capabilities_references WHERE active") == [(3,)]
    again = lab.ok(MANAGER, "references", "import")
    assert {r["outcome"] for r in again["imported"]} == {"unchanged"}

    shutil.rmtree(lab.envelope / "coolify")
    shutil.rmtree(lab.envelope / "other")
    out = lab.ok(MANAGER, "references", "export")
    assert {r["outcome"] for r in out["exported"]} == {"written"}
    assert accounts.read_bytes() == ACCOUNTS
    assert tone.read_bytes() == TONE
    assert marked.read_bytes() == MARKED
    assert {r["outcome"] for r in lab.ok(MANAGER, "references", "export")["exported"]} \
        == {"unchanged"}
    assert lab.ok(MANAGER, "references", "show", "coolify", "account-map") == ACCOUNTS


def test_an_import_with_a_file_it_cannot_read_writes_nothing(lab):
    lab.reference("coolify", "account-map", ACCOUNTS)
    lab.reference("coolify", "broken", b"\xff\xfe not utf-8")
    code, error = lab.refused(MANAGER, "references", "import")
    assert (code, error["code"]) == (6, "import_refused")
    assert "broken.md" in error["message"]
    if lab.sql("SELECT to_regclass('capabilities_references')") != [(None,)]:
        assert lab.sql("SELECT count(*) FROM capabilities_references") == [(0,)]


def test_a_save_adds_a_version_under_its_base_and_history_shows_both(lab):
    lab.reference("coolify", "account-map", ACCOUNTS)
    first = lab.ok(MANAGER, "references", "import")["imported"][0]["version"]
    edited = ACCOUNTS + b"\r\nOne more rule."

    code, error = lab.refused(MANAGER, "references", "save", "coolify", "account-map",
                              "--from", "-", "--base", "000000")
    assert (code, error["code"]) == (6, "unknown_base")
    saved = lab.ok(MANAGER, "references", "save", "coolify", "account-map", "--from", "-",
                   "--base", first[:8], "--note", "one more rule", stdin=edited)
    assert saved["changed"] is True and saved["previous"] == first
    code, error = lab.refused(MANAGER, "references", "save", "coolify", "account-map",
                              "--from", "-", "--base", first)
    assert (code, error["code"]) == (6, "stale_base")
    same = lab.ok(MANAGER, "references", "save", "coolify", "account-map", "--from", "-",
                  stdin=edited)
    assert same["changed"] is False

    history = lab.ok(MANAGER, "references", "history", "coolify", "account-map")["versions"]
    assert [(v["version"], v["active"], v["note"]) for v in history] \
        == [(saved["version"], True, "one more rule"), (first, False, None)]
    assert lab.ok(MANAGER, "references", "show", "coolify", "account-map") == edited
    assert lab.ok(MANAGER, "references", "show", "coolify", "account-map",
                  "--version", first) == ACCOUNTS


def test_the_store_keeps_history_immutable(lab):
    import psycopg
    lab.reference("coolify", "account-map", ACCOUNTS)
    lab.ok(MANAGER, "references", "import")
    for statement in ("UPDATE capabilities_references SET body = 'x'",
                      "DELETE FROM capabilities_references",
                      "TRUNCATE capabilities_references"):
        with pytest.raises(psycopg.errors.CheckViolation):
            lab.sql(statement)
    with pytest.raises(psycopg.errors.UniqueViolation):
        lab.sql("INSERT INTO capabilities_references (project_id, capability, name, body, "
                "created_by) VALUES (%s, 'coolify', 'Account_Map', 'x', 't@n')", (PROJECT_ID,))


def test_a_capability_reads_the_store_before_its_file_and_the_file_otherwise(lab):
    lab.reference("coolify", "account-map", ACCOUNTS)
    lab.ok(MANAGER, "references", "import")
    shutil.rmtree(lab.envelope / "coolify")
    lab.reference("coolify", "only-a-file", TONE.replace(b"tone", b"only-a-file"))

    listed = {r["name"]: r for r in lab.ok(COOLIFY, "refs")}
    assert set(listed) == {"account-map", "only-a-file"}
    assert "path" not in listed["account-map"] and "path" in listed["only-a-file"]
    assert lab.ok(COOLIFY, "refs", "show", "account-map") == ACCOUNTS + b"\n"
    assert lab.ok(COOLIFY, "context", "show", "reference.account-map") == ACCOUNTS + b"\n"
    assert lab.ok(COOLIFY, "refs", "show", "only-a-file") \
        == TONE.replace(b"tone", b"only-a-file")

    # A file of the key the store holds is not read while the store holds it,
    # and is read again once the reference is retired.
    lab.reference("coolify", "account-map", TONE.replace(b"tone", b"account-map"))
    assert lab.ok(COOLIFY, "refs", "show", "account-map") == ACCOUNTS + b"\n"
    lab.ok(MANAGER, "references", "retire", "coolify", "account-map")
    assert lab.ok(COOLIFY, "refs", "show", "account-map") \
        == TONE.replace(b"tone", b"account-map")


def test_context_put_saves_a_stored_reference_as_a_new_version(lab):
    lab.reference("coolify", "account-map", ACCOUNTS)
    lab.ok(MANAGER, "references", "import")
    shutil.rmtree(lab.envelope / "coolify")
    edit = lab.ok(COOLIFY, "context", "edit", "reference.account-map")
    path = Path(edit["path"])
    assert not path.is_relative_to(lab.envelope / "coolify" / "reference")
    path.write_text(path.read_text() + "\nEdited.\n")
    lab.ok(COOLIFY, "context", "put", "reference.account-map")
    history = lab.ok(MANAGER, "references", "history", "coolify", "account-map")["versions"]
    assert len(history) == 2 and history[0]["active"]
    assert lab.ok(COOLIFY, "refs", "show", "account-map").endswith(b"Edited.\n")
    assert not (lab.envelope / "coolify" / "reference").exists()


def test_the_composed_menu_names_a_stored_reference_by_its_verb(lab):
    lab.ok(MANAGER, "install", "coolify", "--from", str(COOLIFY.parents[1]))
    lab.ok(MANAGER, "allow", "coolify")
    lab.ok(MANAGER, "enable", "coolify", "--project")
    lab.reference("coolify", "account-map", ACCOUNTS)
    lab.ok(MANAGER, "references", "import")
    shutil.rmtree(lab.envelope / "coolify")
    composed = lab.ok(MANAGER, "context", "--fragment").decode()
    assert "`coolify refs show account-map` — How entries map to accounts" \
        in composed


def test_a_project_with_only_folders_reads_them_as_before(tmp_path):
    lab = Lab(tmp_path, None)
    body = TONE.replace(b"tone", b"account-map")
    path = lab.reference("coolify", "account-map", body)
    assert lab.ok(COOLIFY, "refs") == [{"name": "account-map",
                                        "description": "How replies are worded",
                                        "key": "reference.account-map", "path": str(path)}]
    assert lab.ok(COOLIFY, "refs", "show", "account-map") == body
    code, error = lab.refused(MANAGER, "references", "list")
    assert (code, error["code"]) == (6, "store_not_configured")


def test_a_store_without_the_table_leaves_the_files_read(lab):
    path = lab.reference("coolify", "account-map", TONE)
    assert lab.ok(COOLIFY, "refs")[0]["path"] == str(path)
    assert lab.ok(MANAGER, "references", "list")["references"] == []


def test_the_writes_are_refused_under_the_read_only_switch(lab):
    lab.reference("coolify", "account-map", ACCOUNTS)
    for verb in (("import",), ("export",), ("retire", "coolify", "account-map")):
        code, error = lab.refused(MANAGER, "references", *verb, CAPABILITIES_READ_ONLY="1")
        assert (code, error["code"]) == (4, "read_only_switch")
