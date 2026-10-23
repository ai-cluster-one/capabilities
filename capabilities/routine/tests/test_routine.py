"""routine's store tables and verbs, against a throwaway PostgreSQL.

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
CLI = CAPABILITY / "bin" / "routine"
MANAGER = CAPABILITY.parents[1] / "bin" / "capabilities"
ADMIN = "pgadmin"
SCHEMA = "rtn"
PROJECT_ID = "prj_" + "1b2c3d4e5f6a"
CLEARED_PREFIXES = ("AGENTKIT_DB_", "CAPABILITIES_", "CLAUDE")

FILES = {
    "defect-intake.md": ("---\nname: defect-intake\ndescription: Run when a defect arrives.\n"
                         "---\n\n# Defect Intake\n\nReproduce it first.\n"),
    "pitstop.md": ("---\nname: pitstop\ntitle: Пит-стоп\ndescription: Run for a Mac health "
                   "check — disk, memory.\n---\n\n# Pit Stop\n\nÜks rida.\nNo newline at the end"),
    "processor/bank-transaction.md": ("---\nname: bank-transaction\ndescription: Run per bank "
                                      "transaction.\n---\n"),
}
OUT_OF_ORDER = ("---\ntitle: Telegram History\ndescription: Run for old messages.\nload: stub\n"
                "order: 300\nname: telegram-history\n---\n\n# Telegram History\n")


def load_module():
    spec = importlib.util.spec_from_loader("routine_cli", loader=None)
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
    root = Path(tempfile.mkdtemp(prefix="capabilities-routine-pg-"))
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
            {"capabilities": {"routine": {"enabled": True}}}))
        self.routines = self.project / "routines"
        for rel, text in FILES.items():
            path = self.routines / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))

    def env(self, **extra) -> dict:
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
        return {k: v for k, v in env.items() if v is not None}

    def run(self, *args: str, stdin: str | None = None, **extra):
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
        version, normalised = cli.parse(text)
        assert (cli.render(version), normalised) == (text, [])
    assert cli.parse(FILES["defect-intake.md"])[0]["title"] is None
    assert cli.parse(FILES["processor/bank-transaction.md"])[0]["body"] == ""
    for bad in ("no front matter\n",
                "---\nname: n\ndescription: D\n",
                "---\nname: n\n---\n",
                "---\ndescription: D\n---\n",
                "---\nname: n\ndescription:  D\n---\n",
                "---\nname: n\ndescription: D \n---\n",
                "---\nname: n\nname: n\ndescription: D\n---\n",
                "---\nname: n\n\ndescription: D\n---\n",
                "---\nname: n\ndescription:D\n---\n",
                "---\nname: n\ntitle: \ndescription: D\n---\n"):
        with pytest.raises(cli.Unreadable):
            cli.parse(bad)


def test_keys_out_of_order_or_beyond_the_three_are_read_and_named_as_normalised():
    cli = load_module()
    version, normalised = cli.parse(OUT_OF_ORDER)
    assert (version["name"], version["title"]) == ("telegram-history", "Telegram History")
    assert version["body"] == "\n# Telegram History\n"
    assert normalised == ["dropped the front matter's load, order",
                          "put title, description, name in the order name, title, description"]
    assert cli.render(version) == ("---\nname: telegram-history\ntitle: Telegram History\n"
                                   "description: Run for old messages.\n---\n"
                                   "\n# Telegram History\n")


# --- the tables ------------------------------------------------------------------

def test_the_ledger_creates_the_tables_with_their_indexes_comments_and_guard(lab):
    lab.ok("list", "--store", "--json")
    steps = [r[0] for r in lab.sql(
        f"SELECT step FROM {SCHEMA}.schema_ledger WHERE owner = 'routine' ORDER BY step")]
    assert steps == ["0001-documents", "0002-versions", "0003-versions-guard", "0004-global",
                     "0005-enablements"]
    indexes = {r[0]: r[1] for r in lab.sql(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s", (SCHEMA,))}
    assert "WHERE active" in indexes["routine_versions_active"]
    assert ("(COALESCE(project_id, ''::text), name) WHERE (retired_at IS NULL)"
            in indexes["routine_documents_name"])
    for table in ("routine_documents", "routine_versions", "routine_enablements"):
        [(comment,)] = lab.sql("SELECT obj_description(%s::regclass)", (f"{SCHEMA}.{table}",))
        assert comment
    commented = {r[0] for r in lab.sql(
        "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass "
        "AND col_description(a.attrelid, a.attnum) IS NOT NULL", (f"{SCHEMA}.routine_versions",))}
    assert {"title", "description", "body", "active", "note"} <= commented
    nullable = dict(lab.sql(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'routine_versions'", (SCHEMA,)))
    assert (nullable["title"], nullable["description"]) == ("YES", "NO")


def test_the_store_keeps_versions_immutable_but_for_clearing_active(lab):
    import psycopg
    lab.ok("import")
    [(vid,)] = lab.sql(f"SELECT v.id FROM {SCHEMA}.routine_versions v JOIN "
                       f"{SCHEMA}.routine_documents d ON d.id = v.document_id "
                       "WHERE d.name = 'pitstop'")
    for statement in (f"UPDATE {SCHEMA}.routine_versions SET body = 'x' WHERE id = %s",
                      f"UPDATE {SCHEMA}.routine_versions SET title = 'x', active = false "
                      "WHERE id = %s",
                      f"DELETE FROM {SCHEMA}.routine_versions WHERE id = %s"):
        with pytest.raises(psycopg.errors.CheckViolation):
            lab.sql(statement, (vid,))
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(f"TRUNCATE {SCHEMA}.routine_versions CASCADE")
    lab.sql(f"UPDATE {SCHEMA}.routine_versions SET active = false WHERE id = %s", (vid,))
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(f"UPDATE {SCHEMA}.routine_versions SET active = true WHERE id = %s", (vid,))
    [(doc,)] = lab.sql(f"SELECT document_id FROM {SCHEMA}.routine_versions WHERE id = %s", (vid,))
    insert = (f"INSERT INTO {SCHEMA}.routine_versions (document_id, description, body, "
              "created_by) VALUES (%s, 'd', '', 'x')")
    lab.sql(insert, (doc,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        lab.sql(insert, (doc,))


# --- import and export -----------------------------------------------------------

def test_import_then_export_writes_every_file_back_byte_for_byte(lab):
    (lab.routines / "helper.py").write_text("print('not a routine')\n")
    first = lab.ok("import")
    assert first["counts"] == {"created": 3, "skipped": 1}
    assert first["folder"] == str(lab.routines.resolve())
    assert [s["path"] for s in first["skipped"]] == ["helper.py"]
    (lab.routines / "helper.py").unlink()
    out = lab.tmp / "out"
    shutil.copytree(lab.routines, out)
    (out / "pitstop.md").write_text("stale\n")
    exported = lab.ok("export", str(out))
    assert exported["counts"] == {"written": 1, "unchanged": 2}
    assert _tree(out) == _tree(lab.routines)
    fresh = lab.tmp / "fresh"
    assert lab.ok("export", str(fresh))["counts"] == {"written": 3}
    assert (fresh / "bank-transaction.md").read_bytes() == \
        FILES["processor/bank-transaction.md"].encode()
    assert lab.ok("import")["counts"] == {"unchanged": 3}


def test_import_versions_a_changed_file_and_refuses_a_folder_with_an_unreadable_one(lab):
    lab.ok("import")
    intake = lab.routines / "defect-intake.md"
    intake.write_text(FILES["defect-intake.md"].replace("first", "first, always"))
    again = lab.ok("import")
    assert again["counts"] == {"versioned": 1, "unchanged": 2}
    assert len(lab.ok("history", "defect-intake", "--json")["versions"]) == 2

    (lab.routines / "broken.md").write_text("no front matter\n")
    (lab.routines / "other").mkdir()
    (lab.routines / "other" / "pitstop.md").write_text(FILES["pitstop.md"])
    (lab.routines / "renamed.md").write_text(FILES["defect-intake.md"])
    intake.write_text(FILES["defect-intake.md"])
    code, error = lab.refused("import")
    assert (code, error["code"]) == (6, "not_canonical")
    for named in ("broken.md", "other/pitstop.md", "renamed.md"):
        assert named in error["message"]
    assert len(lab.ok("history", "defect-intake", "--json")["versions"]) == 2


def test_import_reads_a_file_contextkit_accepts_out_of_order_and_says_so(lab):
    (lab.routines / "telegram-history.md").write_text(OUT_OF_ORDER)
    found = lab.ok("import")
    [item] = [r for r in found["routines"] if r["name"] == "telegram-history"]
    assert item["outcome"] == "created" and len(item["normalised"]) == 2
    assert lab.ok("show", "telegram-history", "--json")["title"] == "Telegram History"


# --- save, show, history, diff ---------------------------------------------------

def test_save_adds_a_version_against_its_base_and_refuses_a_stale_one(lab):
    lab.ok("import")
    shown = lab.ok("show", "pitstop", "--json")
    base = shown["version"]
    assert shown["rendered"] == FILES["pitstop.md"]
    assert lab.run("show", "pitstop").stdout == FILES["pitstop.md"]

    edited = shown["rendered"].replace("Пит-стоп", "Pit stop").replace("Üks", "Kaks")
    saved = lab.ok("save", "pitstop", "--from", "-", "--base", base,
                   "--note", "title in English", stdin=edited)
    assert (saved["changed"], saved["outcome"], saved["base"]) == (True, "versioned", base)
    assert lab.ok("show", "pitstop", "--json")["title"] == "Pit stop"
    old = lab.ok("show", "pitstop", "--version", base[:8], "--json")
    assert (old["title"], old["version"], old["active"]) == ("Пит-стоп", base, False)

    code, error = lab.refused("save", "pitstop", "--from", "-", "--base", base,
                              stdin=edited + "more\n")
    assert (code, error["code"]) == (7, "stale_base")
    assert saved["version"] in error["message"]

    same = lab.ok("save", "pitstop", "--from", "-", stdin=edited)
    assert (same["changed"], same["version"]) == (False, saved["version"])

    history = lab.ok("history", "pitstop", "--json")["versions"]
    assert [v["version"] for v in history] == [saved["version"], base]
    assert [v["active"] for v in history] == [True, False]
    assert history[0]["note"] == "title in English"
    assert history[0]["created_by"] == "tester@test-node"

    diff = lab.run("diff", "pitstop").stdout
    assert f"--- pitstop.md@{base[:8]}" in diff
    assert "-title: Пит-стоп\n+title: Pit stop\n" in diff
    assert lab.run("diff", "pitstop", saved["version"], base).stdout.count("+Üks rida.") == 1

    code, error = lab.refused("save", "pitstop", "--from", "-", stdin="no front\n")
    assert (code, error["code"]) == (6, "not_canonical")
    code, error = lab.refused("save", "other-name", "--from", "-", stdin=edited)
    assert (code, error["code"]) == (6, "not_canonical")
    assert "'pitstop', not 'other-name'" in error["message"]


def test_a_new_name_starts_a_routine_and_its_only_version_diffs_against_nothing(lab):
    text = FILES["defect-intake.md"].replace("defect-intake", "fresh")
    created = lab.ok("save", "fresh", "--from", "-", stdin=text)
    assert (created["outcome"], created["base"]) == ("created", None)
    assert lab.run("diff", "fresh").stdout.startswith("--- /dev/null\n")
    code, error = lab.refused("save", "../x", "--from", "-", stdin=text)
    assert (code, error["code"]) == (6, "input")
    code, error = lab.refused("show")
    assert (code, error["code"]) == (6, "input")


# --- list, retire, the read-only switch ------------------------------------------

def test_list_store_lists_the_store_and_retire_frees_a_name(lab):
    lab.ok("import")
    listed = lab.ok("list", "--store", "--json")
    assert (listed["project"], listed["count"]) == (PROJECT_ID, 3)
    assert [r["name"] for r in listed["routines"]] == ["bank-transaction", "defect-intake",
                                                       "pitstop"]
    assert lab.ok("retire", "pitstop")["retired"] is True
    assert lab.refused("show", "pitstop")[0] == 3
    assert lab.refused("retire", "pitstop")[0] == 3
    assert lab.ok("list", "--store", "--json")["count"] == 2
    assert lab.ok("save", "pitstop", "--from", "-",
                  stdin=FILES["pitstop.md"])["outcome"] == "created"
    [(count,)] = lab.sql(f"SELECT count(*) FROM {SCHEMA}.routine_documents WHERE name = 'pitstop'")
    assert count == 2


def test_list_without_store_reads_the_files_as_before_and_needs_no_store(lab):
    legacy = lab.project / ".routines"
    legacy.mkdir()
    (legacy / "defect-intake.md").write_text(FILES["defect-intake.md"])
    result = lab.run("list", "--json", AGENTKIT_DB_URL=None, AGENTKIT_DB_SCHEMA=None)
    assert result.returncode == 0, result.stderr
    assert [e["name"] for e in json.loads(result.stdout)] == ["defect-intake"]
    code, error = lab.refused("list", "--store", AGENTKIT_DB_URL=None,
                              AGENTKIT_DB_SCHEMA=None)
    assert (code, error["code"]) == (6, "store_not_configured")


def test_the_read_only_switch_refuses_every_write(lab):
    lab.ok("import")
    for args in (("save", "pitstop", "--from", "-"), ("retire", "pitstop"), ("import",)):
        code, error = lab.refused(*args, stdin=FILES["pitstop.md"],
                                  CAPABILITIES_READ_ONLY="1")
        assert (code, error["code"]) == (4, "read_only_switch")
    assert lab.run("list", "--store", CAPABILITIES_READ_ONLY="1").returncode == 0
    assert lab.run("show", "pitstop", CAPABILITIES_READ_ONLY="1").returncode == 0


# --- the agent's routines and their titles ------------------------------------------

def test_titles_lists_the_folder_as_contextkit_does_and_creates_no_table(lab):
    listed = lab.ok("titles", "--json")
    assert (listed["source"], listed["folder"], listed["count"], listed["warnings"]) == \
        ("folder", str(lab.routines.resolve()), 3, [])
    assert listed["routines"][1] == {
        "name": "pitstop", "title": "Пит-стоп", "title_source": "front_matter",
        "description": "Run for a Mac health check — disk, memory.", "source": "folder",
        "path": str(lab.routines.resolve() / "pitstop.md")}
    assert [(r["name"], r["title"], r["title_source"]) for r in listed["routines"]] == [
        ("defect-intake", "defect-intake", "name"), ("pitstop", "Пит-стоп", "front_matter"),
        ("bank-transaction", "bank-transaction", "name")]
    assert lab.sql("SELECT to_regclass(%s)", (f"{SCHEMA}.routine_documents",)) == [(None,)]


def test_title_sets_and_clears_a_folder_routine_as_contextkit_writes_it(lab):
    intake = lab.routines / "defect-intake.md"
    answer = lab.ok("title", "defect-intake", 'Разбор "дефекта"')
    assert (answer["source"], answer["changed"], answer["routine"]["title"]) == \
        ("folder", ["title"], 'Разбор "дефекта"')
    assert intake.read_text() == FILES["defect-intake.md"].replace(
        "name: defect-intake\n", 'name: defect-intake\ntitle: "Разбор \\"дефекта\\""\n')
    assert lab.ok("title", "defect-intake", 'Разбор "дефекта"')["changed"] == []
    cleared = lab.ok("title", "defect-intake", "--clear")
    assert (cleared["changed"], cleared["routine"]["title_source"]) == (["title"], "name")
    assert intake.read_text() == FILES["defect-intake.md"]
    assert lab.refused("title", "nowhere", "x")[0] == 3
    assert lab.refused("title", "pitstop", "two\nlines")[0] == 6
    code, error = lab.refused("title", "pitstop", "x", CAPABILITIES_READ_ONLY="1")
    assert (code, error["code"]) == (4, "read_only_switch")


def test_titles_and_title_read_and_version_the_store_once_it_holds_the_routines(lab):
    lab.ok("import")
    listed = lab.ok("titles", "--json")
    assert (listed["source"], listed["count"]) == ("store", 3)
    assert [w for w in listed["warnings"] if "no longer read" in w]
    pitstop = next(r for r in listed["routines"] if r["name"] == "pitstop")
    assert (pitstop["title"], pitstop["source"]) == ("Пит-стоп", "store")
    before = (lab.routines / "defect-intake.md").read_bytes()
    answer = lab.ok("title", "defect-intake", 'Разбор "дефекта"')
    assert (answer["source"], answer["changed"], answer["routine"]["title"]) == \
        ("store", ["title"], 'Разбор "дефекта"')
    assert (lab.routines / "defect-intake.md").read_bytes() == before
    assert lab.ok("show", "defect-intake") == FILES["defect-intake.md"].replace(
        "name: defect-intake\n", 'name: defect-intake\ntitle: "Разбор \\"дефекта\\""\n')
    assert lab.ok("title", "defect-intake", 'Разбор "дефекта"')["changed"] == []
    assert lab.ok("title", "pitstop", "Пит-стоп")["changed"] == []
    assert lab.ok("title", "defect-intake", "--clear")["changed"] == ["title"]
    assert lab.ok("show", "defect-intake") == FILES["defect-intake.md"]
    assert lab.ok("history", "defect-intake", "--json")["count"] == 3
    assert lab.refused("title", "nowhere", "x")[0] == 3


def test_a_store_that_does_not_answer_is_read_past_but_refuses_a_title(lab):
    dead = f"postgresql://{ADMIN}@127.0.0.1:{_free_port()}/none?sslmode=disable"
    listed = lab.ok("titles", "--json", AGENTKIT_DB_URL=dead)
    assert (listed["source"], listed["count"]) == ("folder", 3)
    assert listed["warnings"] and "routines folder is used" in listed["warnings"][0]
    assert lab.refused("title", "pitstop", "x", AGENTKIT_DB_URL=dead)[0] == 5
    assert (lab.routines / "pitstop.md").read_text() == FILES["pitstop.md"]


# --- global routines ----------------------------------------------------------------

OTHER_ID = "prj_" + "9a8b7c6d5e4f"
SHARED = "---\nname: shared\ndescription: Run in every agent.\n---\n\n# Shared\n"


def _other_project(lab: Lab) -> dict:
    """A second project in the lab's store, as the env its runs take."""
    other = lab.tmp / "other"
    (other / ".git").mkdir(parents=True)
    (other / "capabilities").mkdir()
    (other / "capabilities" / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "slug": "other", "id": OTHER_ID}))
    (other / "capabilities" / "settings.json").write_text(json.dumps(
        {"capabilities": {"routine": {"enabled": True}}}))
    return {"CLAUDE_PROJECT_DIR": str(other)}


def test_a_global_routine_reaches_a_project_only_once_it_is_enabled(lab):
    other = _other_project(lab)
    saved = lab.ok("save", "shared", "--from", "-", "--global", stdin=SHARED, **other)
    assert (saved["outcome"], saved["global"]) == ("created", True)
    assert [(r["name"], r["author"]) for r in
            lab.ok("list", "--store", "--global", "--json")["routines"]] == [("shared", OTHER_ID)]
    assert lab.ok("show", "shared", "--global") == SHARED
    assert lab.ok("list", "--store", "--json")["count"] == 0
    assert lab.refused("show", "shared")[0] == 3
    assert lab.ok("titles", "--json")["source"] == "folder"

    enabled = lab.ok("enable", "shared")
    assert (enabled["changed"], enabled["author"]) == (True, OTHER_ID)
    assert lab.ok("enable", "shared")["changed"] is False
    [(by,)] = lab.sql(f"SELECT enabled_by FROM {SCHEMA}.routine_enablements "
                      "WHERE project_id = %s", (PROJECT_ID,))
    assert by == "tester@test-node"
    [listed] = lab.ok("list", "--store", "--json")["routines"]
    assert (listed["name"], listed["global"], listed["author"]) == ("shared", True, OTHER_ID)
    assert lab.ok("show", "shared") == SHARED
    assert lab.ok("show", "shared", "--json")["global"] is True
    titles = lab.ok("titles", "--json")
    assert (titles["source"], [r["name"] for r in titles["routines"]]) == ("store", ["shared"])
    assert lab.refused("title", "shared", "Mine")[1]["code"] == "global_routine"

    # A routine of the project's own of the same name shadows it.
    own = SHARED.replace("Run in every agent.", "Run here.")
    lab.ok("save", "shared", "--from", "-", stdin=own)
    [listed] = lab.ok("list", "--store", "--json")["routines"]
    assert (listed["global"], listed.get("shadows_global")) == (False, True)
    assert lab.ok("show", "shared") == own
    lab.ok("retire", "shared")
    assert lab.ok("show", "shared") == SHARED

    assert lab.ok("disable", "shared")["changed"] is True
    assert lab.ok("disable", "shared")["changed"] is False
    assert lab.ok("list", "--store", "--json")["count"] == 0
    assert lab.refused("show", "shared")[0] == 3


def test_only_the_author_project_changes_a_global_routine_and_hands_it_on(lab):
    other = _other_project(lab)
    lab.ok("save", "shared", "--from", "-", "--global", stdin=SHARED, **other)
    newer = SHARED.replace("# Shared", "# Shared, again")
    for args in (("save", "shared", "--from", "-", "--global"), ("retire", "shared", "--global"),
                 ("transfer", "shared", "--to", PROJECT_ID)):
        code, error = lab.refused(*args, stdin=newer)
        assert (code, error["code"]) == (4, "not_author"), args
    assert lab.ok("show", "shared", "--global") == SHARED

    handed = lab.ok("transfer", "shared", "--to", PROJECT_ID, **other)
    assert (handed["author"], handed["changed"]) == (PROJECT_ID, True)
    assert lab.refused("transfer", "shared", "--to", OTHER_ID, **other)[1]["code"] == "not_author"
    assert lab.refused("save", "shared", "--from", "-", "--global", stdin=newer,
                       **other)[1]["code"] == "not_author"
    assert lab.ok("save", "shared", "--from", "-", "--global", stdin=newer)["outcome"] == "versioned"
    assert lab.ok("show", "shared", "--global") == newer
    assert lab.ok("retire", "shared", "--global")["retired"] is True
    assert lab.ok("list", "--store", "--global", "--json")["count"] == 0
