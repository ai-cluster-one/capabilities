"""teams' tables and verbs, against a throwaway PostgreSQL.

The module starts a cluster on a loopback port when `initdb` and `pg_ctl` are
on PATH and psycopg is importable, and skips otherwise; each test gets a
database of its own and a project that exists only for the test. The CLI runs
as a child with a home of its own, so nothing reaches the real one.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = CAPABILITY / "bin" / "teams"
MANAGER = CAPABILITY.parents[1] / "bin" / "capabilities"
ADMIN = "pgadmin"
SCHEMA = "tms"
AGENT = "prj_" + "0a1b2c3d4e5f"
OTHER = "prj_" + "1b2c3d4e5f60"
CLEARED_PREFIXES = ("AGENTKIT_DB_", "CAPABILITIES_", "CLAUDE")
TEST_NODE = "746573742d6e"

STYLE = "---\ndescription: How we write.\nload: always\n---\n# Style\n\nOne line per paragraph.\n"
DEPLOY = "---\ndescription: Read before a release.\nload: on-request\n---\nRelease from main.\n"


def load_module():
    spec = importlib.util.spec_from_loader("teams_cli", loader=None)
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
    root = Path(tempfile.mkdtemp(prefix="capabilities-teams-pg-"))
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
            {"schema": "capabilities.project.v1", "slug": "lab", "id": AGENT}))
        (envelope / "settings.json").write_text(json.dumps(
            {"capabilities": {"teams": {"enabled": True}}}))

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
            "AGENT_NODE_ID": TEST_NODE,
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

    def register(self, *agents: str) -> None:
        for agent in agents:
            self.sql(f"INSERT INTO {SCHEMA}.agents (id, kind) VALUES (%s, 'agent')"
                     " ON CONFLICT (id) DO NOTHING", (agent,))

    def team(self, name: str = "crew", *flags: str) -> dict:
        return self.ok("create", name, "--description", f"The {name}: turn to it for releases.",
                       *flags)


@pytest.fixture()
def lab(tmp_path, cluster):
    return Lab(tmp_path, cluster)


# --- the file form and size ------------------------------------------------------

def test_the_file_form_reads_back_byte_for_byte_and_refuses_what_would_not():
    cli = load_module()
    for text in (STYLE, DEPLOY, "---\ndescription: Empty.\nload: always\n---\n"):
        assert cli.render(cli.parse(text)) == text
    for bad in ("no front matter\n",
                "---\ndescription: D\nload: always\n",
                "---\nload: always\ndescription: D\n---\n",
                "---\ndescription: D\nload: inline\n---\n",
                "---\ndescription: D\nload: always\nextra: x\n---\n",
                "---\ndescription:  D\nload: always\n---\n"):
        with pytest.raises(cli.Unreadable):
            cli.parse(bad)


def test_a_size_is_a_quarter_of_the_characters_rounded_up():
    cli = load_module()
    assert [cli.tokens(text) for text in ("", "a", "abcd", "abcde")] == [0, 1, 1, 2]


# --- the tables ------------------------------------------------------------------

def test_the_ledger_creates_the_tables_with_their_comments_and_guard(lab):
    lab.ok("list", "--all", "--json")
    steps = [r[0] for r in lab.sql(
        f"SELECT step FROM {SCHEMA}.schema_ledger WHERE owner = 'teams' ORDER BY step")]
    assert steps == ["0001-teams", "0002-members", "0003-documents", "0004-versions",
                     "0005-versions-guard", "0006-keys", "0007-ids"]
    for table in ("teams_teams", "teams_members", "teams_documents", "teams_versions"):
        [(comment,)] = lab.sql("SELECT obj_description(%s::regclass)", (f"{SCHEMA}.{table}",))
        assert comment
    lab.team()
    lab.ok("doc", "save", "crew", "style", "--from", "-", stdin=STYLE)
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(f"UPDATE {SCHEMA}.teams_versions SET body = 'changed'")
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(f"DELETE FROM {SCHEMA}.teams_versions")


# --- teams, members and modes ----------------------------------------------------

def test_a_team_is_made_once_and_a_repeat_with_the_same_line_changes_nothing(lab):
    assert lab.team()["changed"] is True
    assert lab.team()["changed"] is False
    code, error = lab.refused("create", "crew", "--description", "Another line.")
    assert (code, error["code"]) == (6, "team_exists")
    for bad in ("0a1b2c3d4e5f", " crew", "", "a" * 129):
        code, error = lab.refused("create", bad, "--description", "x")
        assert (code, error["code"]) == (6, "input"), bad
    code, error = lab.refused("create", "two", "--description", "a\nb")
    assert (code, error["code"]) == (6, "input")


def test_list_answers_this_agents_teams_and_says_when_there_is_none(lab):
    lab.team("crew")
    lab.team("ops", "--always")
    lab.register(AGENT, OTHER)
    assert "is a member of no team" in lab.ok("list")
    assert lab.ok("members", "add", "ops", AGENT)["changed"] is True
    assert lab.ok("members", "add", "ops", AGENT)["changed"] is False
    lab.ok("members", "add", "crew", OTHER)
    mine = lab.ok("list", "--json")
    assert mine["agent"] == AGENT
    assert [(t["name"], t["mode"]) for t in mine["teams"]] == [("ops", "always")]
    every = lab.ok("list", "--all", "--json")
    assert [(t["name"], t["member"]) for t in every["teams"]] == [("crew", False), ("ops", True)]
    assert lab.ok("members", "remove", "ops", AGENT)["changed"] is True
    assert lab.ok("members", "remove", "ops", AGENT)["changed"] is False
    assert lab.ok("list", "--json")["teams"] == []


def test_the_mode_switches_and_a_repeat_changes_nothing(lab):
    lab.team()
    assert lab.ok("mode", "crew", "always") == {"team": "crew", "mode": "always",
                                                "changed": True}
    assert lab.ok("mode", "crew", "always")["changed"] is False
    assert lab.ok("show", "crew", "--json")["mode"] == "always"
    assert lab.ok("mode", "crew", "ordinary")["changed"] is True
    code, error = lab.refused("mode", "none", "always")
    assert (code, error["code"]) == (3, "not_found")


# --- documents -------------------------------------------------------------------

def test_a_document_saves_with_a_base_check_and_keeps_its_history(lab):
    lab.team()
    first = lab.ok("doc", "save", "crew", "style", "--from", "-", stdin=STYLE)
    assert (first["created"], first["changed"], first["previous"]) == (True, True, None)
    assert lab.ok("doc", "show", "crew", "style") == STYLE
    again = lab.ok("doc", "save", "crew", "style", "--from", "-", stdin=STYLE)
    assert again == {"team": "crew", "document": "style", "version": first["version"],
                     "changed": False}
    edited = STYLE.replace("One line", "Each line")
    second = lab.ok("doc", "save", "crew", "style", "--from", "-", "--base",
                    first["version"][:8], "--note", "say it plainly", stdin=edited)
    assert second["previous"] == first["version"]
    code, error = lab.refused("doc", "save", "crew", "style", "--from", "-", "--base",
                              first["version"], stdin=STYLE.replace("One", "Two"))
    assert (code, error["code"]) == (7, "stale_base")
    assert second["version"] in error["message"]
    history = lab.ok("doc", "history", "crew", "style", "--json")["versions"]
    assert [(v["version"], v["active"], v["note"]) for v in history] == [
        (second["version"], True, "say it plainly"), (first["version"], False, None)]
    assert lab.ok("doc", "show", "crew", "style", "--version", first["version"]) == STYLE
    shown = lab.ok("doc", "show", "crew", "style", "--json")
    assert (shown["version"], shown["load"], shown["tokens"]) == (
        second["version"], "always", (len(edited.split("---\n", 2)[2]) + 3) // 4)


def test_a_save_that_does_not_read_as_the_file_form_is_refused(lab):
    lab.team()
    code, error = lab.refused("doc", "save", "crew", "style", "--from", "-",
                              stdin="no front matter\n")
    assert (code, error["code"]) == (6, "not_canonical")
    code, error = lab.refused("doc", "save", "none", "style", "--from", "-", stdin=STYLE)
    assert (code, error["code"]) == (3, "not_found")
    assert lab.sql(f"SELECT count(*) FROM {SCHEMA}.teams_documents") == [(0,)]


def test_a_document_switches_on_and_off_and_show_sizes_the_team(lab):
    lab.team()
    lab.ok("doc", "save", "crew", "style", "--from", "-", stdin=STYLE)
    lab.ok("doc", "save", "crew", "deploy", "--from", "-", stdin=DEPLOY)
    style, deploy = (len(t.split("---\n", 2)[2]) for t in (STYLE, DEPLOY))
    view = lab.ok("show", "crew", "--json")
    assert [(d["name"], d["load"], d["enabled"]) for d in view["documents"]] == [
        ("deploy", "on-request", True), ("style", "always", True)]
    assert (view["tokens"], view["always_tokens"]) == (
        (style + 3) // 4 + (deploy + 3) // 4, (style + 3) // 4)
    assert lab.ok("doc", "off", "crew", "style") == {"team": "crew", "document": "style",
                                                    "enabled": False, "changed": True}
    assert lab.ok("doc", "off", "crew", "style")["changed"] is False
    view = lab.ok("show", "crew", "--json")
    assert (view["tokens"], view["always_tokens"]) == ((deploy + 3) // 4, 0)
    assert lab.ok("doc", "on", "crew", "style")["changed"] is True
    plain = lab.ok("show", "crew")
    assert "mode: ordinary" in plain and "style  always  on" in plain
    code, error = lab.refused("doc", "off", "crew", "none")
    assert (code, error["code"]) == (3, "not_found")


# --- gates -----------------------------------------------------------------------

def test_every_write_is_refused_under_the_read_only_switch_and_reads_still_answer(lab):
    lab.team()
    lab.ok("doc", "save", "crew", "style", "--from", "-", stdin=STYLE)
    for argv in (("create", "two", "--description", "x"), ("mode", "crew", "always"),
                 ("members", "add", "crew", AGENT), ("members", "remove", "crew", AGENT),
                 ("doc", "save", "crew", "style", "--from", "-"),
                 ("doc", "off", "crew", "style"), ("doc", "on", "crew", "style"),
                 ("rename", "crew", "two"), ("describe", "crew", "--description", "x"),
                 ("retire", "crew"), ("doc", "rename", "crew", "style", "two"),
                 ("doc", "retire", "crew", "style")):
        code, error = lab.refused(*argv, stdin=STYLE, CAPABILITIES_READ_ONLY="1")
        assert (code, error["code"]) == (4, "read_only_switch"), argv
    assert lab.ok("doc", "show", "crew", "style", CAPABILITIES_READ_ONLY="1") == STYLE
    assert lab.ok("show", "crew", "--json", CAPABILITIES_READ_ONLY="1")["mode"] == "ordinary"


def test_a_project_that_disables_teams_is_refused(lab):
    (lab.project / "capabilities" / "settings.json").write_text(json.dumps(
        {"capabilities": {"teams": {"enabled": False}}}))
    code, _error = lab.refused("list", "--all")
    assert code == 4


def test_the_manifest_carries_a_constant_contribution_and_no_connection(lab):
    manifest = lab.ok("manifest", "--json")
    assert manifest["name"] == "teams"
    assert manifest["contribution"]["markdown"].startswith("Teams carry instructions")
    assert manifest["credentials"]["keys"] == []
    answer = lab.ok("connections")
    assert (answer["default"], answer["connections"]) == (None, {})


# --- ids and names ---------------------------------------------------------------

BASE_REF = os.environ.get("TEAMS_BASE_REF", "ccf7b821")


def _released_cli(tmp: Path) -> Path:
    """teams as released at teams 1.1, the writer and reader older than ids."""
    source = subprocess.run(["git", "-C", str(CAPABILITY), "show",
                             f"{BASE_REF}:capabilities/teams/bin/teams"],
                            capture_output=True, text=True)
    if source.returncode != 0:
        pytest.skip(f"the released teams at {BASE_REF} is not in this checkout")
    path = tmp / "teams-1.1"
    path.write_text(source.stdout)
    return path


def _old(lab, cli: Path, *args: str, stdin: str | None = None):
    done = subprocess.run([sys.executable, str(cli), *args], cwd=lab.project, env=lab.env(),
                          input=stdin, text=True, capture_output=True, timeout=180)
    assert done.returncode == 0, done.stderr
    return done.stdout


# The joins a reader released before ids makes: the manager's and AgentKit's.
OLD_READER = (f"SELECT t.name, d.name, v.body FROM {SCHEMA}.teams_teams t "
              f"JOIN {SCHEMA}.teams_members m ON m.team = t.name "
              f"JOIN {SCHEMA}.teams_documents d ON d.team = t.name "
              f"JOIN {SCHEMA}.teams_versions v ON v.team = d.team AND v.document = d.name "
              "AND v.active WHERE m.agent_id = %s AND d.enabled ORDER BY t.name, d.name")


def _mirrors_hold(lab) -> None:
    """Every copy of a name equals the current name of what its id points at."""
    assert lab.sql(f"""
        SELECT count(*) FROM {SCHEMA}.teams_members m JOIN {SCHEMA}.teams_teams t
            ON t.id = m.team_id WHERE m.team <> t.name""") == [(0,)]
    assert lab.sql(f"""
        SELECT count(*) FROM {SCHEMA}.teams_documents d JOIN {SCHEMA}.teams_teams t
            ON t.id = d.team_id WHERE d.team <> t.name""") == [(0,)]
    assert lab.sql(f"""
        SELECT count(*) FROM {SCHEMA}.teams_versions v JOIN {SCHEMA}.teams_documents d
            ON d.id = v.document_id WHERE v.team <> d.team OR v.document <> d.name""") == [(0,)]


def test_a_store_at_teams_1_1_keeps_every_team_member_and_document_through_the_ids(lab):
    old = _released_cli(lab.tmp)
    _old(lab, old, "create", "crew", "--description", "The crew.", "--always")
    lab.register(AGENT, OTHER)
    _old(lab, old, "create", "ops", "--description", "Operations.")
    for agent in (AGENT, OTHER):
        _old(lab, old, "members", "add", "crew", agent)
    _old(lab, old, "members", "add", "ops", AGENT)
    _old(lab, old, "doc", "save", "crew", "style", "--from", "-", stdin=STYLE)
    _old(lab, old, "doc", "save", "crew", "style", "--from", "-",
         stdin=STYLE.replace("One", "Each"))
    _old(lab, old, "doc", "save", "crew", "deploy", "--from", "-", stdin=DEPLOY)
    _old(lab, old, "doc", "off", "crew", "deploy")
    before = lab.sql(OLD_READER, (AGENT,))
    assert lab.sql(f"SELECT major, minor FROM {SCHEMA}.schema_version "
                   "WHERE owner = 'teams'") == [(1, 1)]

    view = lab.ok("show", "crew", "--json")
    assert lab.sql(f"SELECT major, minor FROM {SCHEMA}.schema_version "
                   "WHERE owner = 'teams'") == [(1, 2)]
    assert re.fullmatch(r"[0-9a-f]{12}", view["id"])
    assert sorted(view["members"]) == sorted([AGENT, OTHER])
    assert [(d["name"], d["enabled"]) for d in view["documents"]] == [
        ("deploy", False), ("style", True)]
    assert all(re.fullmatch(r"[0-9a-f]{12}", d["id"]) for d in view["documents"])
    style = next(d for d in view["documents"] if d["name"] == "style")
    history = lab.ok("doc", "history", view["id"], style["id"], "--json")
    assert len(history["versions"]) == 2 and history["document_id"] == style["id"]
    assert lab.sql(f"SELECT count(*) FROM {SCHEMA}.teams_members") == [(3,)]
    assert lab.sql(OLD_READER, (AGENT,)) == before
    _mirrors_hold(lab)


def test_a_writer_and_a_reader_released_before_ids_keep_working_on_ids(lab):
    old = _released_cli(lab.tmp)
    lab.team("crew")
    lab.register(AGENT)
    _old(lab, old, "create", "ops", "--description", "Operations.")
    _old(lab, old, "members", "add", "ops", AGENT)
    _old(lab, old, "doc", "save", "ops", "style", "--from", "-", stdin=STYLE)
    _old(lab, old, "doc", "save", "ops", "style", "--from", "-",
         stdin=STYLE.replace("One", "Each"))
    _old(lab, old, "doc", "off", "ops", "style")
    _old(lab, old, "doc", "on", "ops", "style")
    _old(lab, old, "mode", "ops", "always")
    view = lab.ok("show", "ops", "--json")
    assert (view["mode"], view["members"]) == ("always", [AGENT])
    [doc] = view["documents"]
    assert (doc["name"], doc["enabled"]) == ("style", True)
    assert lab.ok("doc", "show", "ops", doc["id"]) == STYLE.replace("One", "Each")
    assert "Each line" in _old(lab, old, "doc", "show", "ops", "style")
    assert "ops  always" in _old(lab, old, "list")
    _mirrors_hold(lab)


def test_a_rename_keeps_the_id_and_every_copy_of_the_name_moves_in_the_same_transaction(lab):
    made = lab.team("crew")
    lab.register(AGENT)
    lab.ok("members", "add", "crew", AGENT)
    lab.ok("doc", "save", "crew", "style", "--from", "-", stdin=STYLE)
    doc_id = lab.ok("show", "crew", "--json")["documents"][0]["id"]

    renamed = lab.ok("rename", "crew", "Release crew")
    assert (renamed["id"], renamed["previous"], renamed["changed"]) == (
        made["id"], "crew", True)
    assert lab.ok("rename", made["id"], "Release crew")["changed"] is False
    assert lab.ok("show", "Release crew", "--json")["id"] == made["id"]
    code, error = lab.refused("show", "crew")
    assert (code, error["code"]) == (3, "not_found")
    moved = lab.ok("doc", "rename", "Release crew", "style", "House style")
    assert (moved["id"], moved["document"]) == (doc_id, "House style")
    assert lab.ok("doc", "show", made["id"], "House style") == STYLE
    _mirrors_hold(lab)
    assert lab.sql(OLD_READER, (AGENT,)) == [("Release crew", "House style",
                                              STYLE.split("---\n", 2)[2])]

    # Inside one transaction, the copies already read the new name.
    with lab.cluster.connect(lab.database) as conn:
        conn.autocommit = False
        conn.execute(f"UPDATE {SCHEMA}.teams_teams SET name = 'crew two' WHERE id = %s",
                     (made["id"],))
        conn.execute(f"UPDATE {SCHEMA}.teams_documents SET name = 'style two' WHERE id = %s",
                     (doc_id,))
        assert conn.execute(OLD_READER, (AGENT,)).fetchall() == [
            ("crew two", "style two", STYLE.split("---\n", 2)[2])]
        conn.rollback()

    lab.team("ops")
    code, error = lab.refused("rename", "ops", "Release crew")
    assert (code, error["code"]) == (6, "name_taken")
    code, error = lab.refused("rename", "ops", "0a1b2c3d4e5f")
    assert (code, error["code"]) == (6, "input")
    assert lab.ok("describe", "ops", "--description", "Operations.")["changed"] is True
    assert lab.ok("show", "ops", "--json")["description"] == "Operations."
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(f"UPDATE {SCHEMA}.teams_teams SET id = '0123456789ab' WHERE id = %s",
                (made["id"],))
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(f"UPDATE {SCHEMA}.teams_documents SET id = '0123456789ab' WHERE id = %s",
                (doc_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(f"UPDATE {SCHEMA}.teams_teams SET name = '0123456789ab' WHERE id = %s",
                (made["id"],))


def test_retiring_frees_the_name_and_keeps_every_version(lab):
    made = lab.team("crew")
    lab.register(AGENT)
    lab.ok("members", "add", "crew", AGENT)
    lab.ok("doc", "save", "crew", "style", "--from", "-", stdin=STYLE)
    lab.ok("doc", "save", "crew", "deploy", "--from", "-", stdin=DEPLOY)
    deploy = next(d for d in lab.ok("show", "crew", "--json")["documents"]
                  if d["name"] == "deploy")

    retired = lab.ok("doc", "retire", "crew", "deploy")
    assert (retired["id"], retired["retired"]) == (deploy["id"], True)
    assert [d["name"] for d in lab.ok("show", "crew", "--json")["documents"]] == ["style"]
    assert lab.ok("doc", "show", "crew", deploy["id"]) == DEPLOY
    code, error = lab.refused("doc", "show", "crew", "deploy")
    assert (code, error["code"]) == (3, "not_found")
    again = lab.ok("doc", "save", "crew", "deploy", "--from", "-", stdin=DEPLOY)
    assert again["created"] is True
    assert lab.sql(OLD_READER, (AGENT,)) == [("crew", "deploy", DEPLOY.split("---\n", 2)[2]),
                                             ("crew", "style", STYLE.split("---\n", 2)[2])]

    out = lab.ok("retire", "crew")
    assert (out["id"], out["members_removed"]) == (made["id"], [AGENT])
    assert lab.ok("list", "--all", "--json")["teams"] == []
    assert lab.ok("list", "--json")["teams"] == []
    assert lab.ok("show", made["id"], "--json")["retired_at"]
    code, error = lab.refused("show", "crew")
    assert (code, error["code"]) == (3, "not_found")
    code, error = lab.refused("members", "add", made["id"], AGENT)
    assert (code, error["code"]) == (3, "not_found")
    assert lab.team("crew")["id"] != made["id"]
    assert lab.sql(f"SELECT count(*) FROM {SCHEMA}.teams_versions") == [(3,)]
    _mirrors_hold(lab)
