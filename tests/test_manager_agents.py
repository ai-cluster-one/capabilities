"""The kernel's tables: the agent registry, the agent's nodes and the store's one
row of global facts, migrated in the contract's ledger, and the manager verbs
that write them for the project a call runs in.

These tests drive the real manager in a project that exists only for the test,
against a throwaway PostgreSQL on a loopback port that the module starts when
`initdb` and `pg_ctl` are on PATH and psycopg is importable, and skips
otherwise. Each test gets a database of its own.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
import zlib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
ADMIN = "pgadmin"
SCHEMA = "kernel"
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")
PROJECT_ID = "prj_" + "0a1b2c3d4e5f"


def _png() -> bytes:
    import struct

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


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
    root = Path(tempfile.mkdtemp(prefix="capabilities-kernel-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


class Lab:
    def __init__(self, tmp_path: Path, cluster: Cluster):
        self.tmp = tmp_path
        self.cluster = cluster
        self.database = cluster.database()
        self.project = tmp_path / "project"
        (self.project / ".git").mkdir(parents=True)
        (self.project / "capabilities").mkdir()
        (self.project / "capabilities" / "project.json").write_text(json.dumps(
            {"schema": "capabilities.project.v1", "slug": "lab", "id": PROJECT_ID}))
        (self.project / ".env.local").write_text(
            f"AGENTKIT_DB_URL={cluster.url(self.database)}\nAGENTKIT_DB_SCHEMA={SCHEMA}\n")

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in DB_KEYS and not k.startswith("CAPABILITIES_")}
        env.update({
            "HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "CAPABILITIES_HOME": str(self.tmp / "registry"),
            "CLAUDE_PROJECT_DIR": str(self.project),
            **extra,
        })
        return env

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MANAGER), *args], cwd=self.project,
                              env=self.env(**extra), text=True, capture_output=True,
                              timeout=120)

    def ok(self, *args: str) -> dict:
        result = self.run(*args)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def refused(self, *args: str, **extra: str) -> tuple[int, dict]:
        result = self.run(*args, **extra)
        line = next(line for line in reversed(result.stderr.splitlines())
                    if line.lstrip().startswith("{"))
        return result.returncode, json.loads(line)["error"]

    def sql(self, query: str, params: tuple = ()):
        with self.cluster.connect(self.database) as conn:
            return conn.execute(query, params).fetchall()


@pytest.fixture()
def lab(tmp_path, cluster):
    return Lab(tmp_path, cluster)


def _seed_contextkit(lab: Lab, rows: list[tuple]) -> None:
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA}.contextkit_agents ("
            "id text PRIMARY KEY, name text, description text, icon_sha text, icon bytea, "
            "created_at timestamptz NOT NULL DEFAULT now(), "
            "updated_at timestamptz NOT NULL DEFAULT now())")
        for row in rows:
            conn.execute(f"INSERT INTO {SCHEMA}.contextkit_agents "
                         "(id, name, description, icon_sha, icon) VALUES (%s, %s, %s, %s, %s)",
                         row)


# --- the tables -----------------------------------------------------------------

def test_the_ledger_creates_the_kernel_tables_with_their_indexes_and_comments(lab):
    lab.ok("agent", "show")

    steps = [r[0] for r in lab.sql(
        f"SELECT step FROM {SCHEMA}.schema_ledger WHERE owner = 'capabilities' ORDER BY step")]
    assert steps[8:13] == ["0009-agents", "0010-agents-from-contextkit", "0011-agent-nodes",
                           "0012-agent-nodes-master", "0013-global"]
    indexes = {r[0]: r[1] for r in lab.sql(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s", (SCHEMA,))}
    assert "WHERE is_master" in indexes["capabilities_agent_nodes_master"]
    assert "(agent_id, node)" in indexes["capabilities_agent_nodes_pkey"]
    for table in ("capabilities_agents", "capabilities_agent_nodes", "capabilities_global"):
        [(comment,)] = lab.sql("SELECT obj_description(%s::regclass)", (f"{SCHEMA}.{table}",))
        assert comment
    assert lab.sql(f"SELECT singleton, global_migrated_at FROM {SCHEMA}.capabilities_global") \
        == [(True, None)]


def test_contextkit_registry_rows_are_taken_over_once(lab):
    icon = _png()
    import hashlib
    sha = hashlib.sha256(icon).hexdigest()
    _seed_contextkit(lab, [("prj_00000000aaa1", "Alpha", "", None, None),
                           ("prj_00000000aaa2", "", "Second", sha, icon)])

    lab.ok("agent", "show")
    _seed_contextkit(lab, [("prj_00000000aaa3", "Late", None, None, None)])
    lab.sql(f"UPDATE {SCHEMA}.contextkit_agents SET name = 'Renamed' "
            "WHERE id = 'prj_00000000aaa1' RETURNING id")
    lab.ok("agent", "show")

    rows = lab.sql(f"SELECT id, name, description, icon_sha, icon FROM "
                   f"{SCHEMA}.capabilities_agents ORDER BY id")
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("prj_00000000aaa1", "Alpha", None, None),
        ("prj_00000000aaa2", None, "Second", sha)]
    assert bytes(rows[1][4]) == icon


# --- the agent ------------------------------------------------------------------

def test_init_registers_the_agent_row(lab):
    result = lab.ok("init")

    assert result["agent"]["registered"] is True and result["agent"]["created"] is True
    assert lab.sql(f"SELECT id FROM {SCHEMA}.capabilities_agents") == [(PROJECT_ID,)]
    assert lab.ok("init")["agent"]["created"] is False


def test_a_new_agent_starts_in_the_store_with_this_node_as_master(lab):
    (lab.project / "capabilities" / "project.json").unlink()
    node = {"AGENT_NODE_NAME": "lab-node"}

    code, error = lab.refused("init", **node)
    assert (code, error["code"]) == (6, "global_not_migrated")
    assert list((lab.project / "capabilities").iterdir()) == []

    migrated = lab.run("migrate", "--global", "--apply", **node)
    assert migrated.returncode == 0, migrated.stderr
    first = lab.run("init", **node)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["agent"]["migrated"] is True
    again = lab.run("init", **node)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["agent"]["created"] is False

    identity = json.loads((lab.project / "capabilities" / "project.json").read_text())
    assert identity["records"] == "store"
    assert not (lab.project / "capabilities" / "settings.json").exists()
    assert lab.sql(f"SELECT migrated_at IS NOT NULL FROM {SCHEMA}.capabilities_agents "
                   "WHERE id = %s", (identity["id"],)) == [(True,)]
    assert lab.sql(f"SELECT node, is_master FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE agent_id = %s", (identity["id"],)) == [("lab-node", True)]


def test_agent_show_and_set(lab, tmp_path):
    shown = lab.ok("agent", "show")
    assert (shown["agent_id"], shown["registered"], shown["agent"]) == (PROJECT_ID, False, None)

    icon = tmp_path / "icon.png"
    icon.write_bytes(_png())
    written = lab.ok("agent", "set", "--name", "Lab Agent", "--description", "A lab",
                     "--icon", str(icon), "--onboarding-status", "pending",
                     "--onboarding-routine", "onboard")
    assert (written["created"], written["changed"]) == (True, True)
    agent = written["agent"]
    assert (agent["name"], agent["description"], agent["onboarding_status"],
            agent["onboarding_routine"], agent["icon_bytes"]) == (
        "Lab Agent", "A lab", "pending", "onboard", len(_png()))

    assert lab.ok("agent", "set", "--name", "Lab Agent")["changed"] is False
    out = tmp_path / "out.png"
    assert lab.ok("agent", "show", "--icon-out", str(out))["registered"] is True
    assert out.read_bytes() == _png()

    cleared = lab.ok("agent", "set", "--no-icon", "--name", "",
                     "--onboarding-status", "none")["agent"]
    assert (cleared["name"], cleared["icon_sha"], cleared["onboarding_status"]) == (
        None, None, None)

    not_png = tmp_path / "x.txt"
    not_png.write_text("hello")
    assert lab.refused("agent", "set", "--icon", str(not_png))[0] == 6
    assert lab.refused("agent", "set", "--onboarding-status", "maybe")[0] == 6


# --- the nodes ------------------------------------------------------------------

def test_nodes_are_added_changed_listed_and_removed(lab):
    added = lab.ok("node", "add", "mac-one", "--kind", "mac", "--master")
    assert added["node"] == {"node": "mac-one", "kind": "mac", "address": None,
                             "is_master": True, "may_write": False, "target": None,
                             "last_seen_at": None}
    lab.ok("node", "add", "srv-one", "--kind", "server", "--address", "203.0.113.10",
           "--target", "prod", "--may-write")

    assert lab.ok("node", "set", "srv-one", "--may-write")["changed"] is False
    assert lab.ok("node", "set", "srv-one", "--address", "")["node"]["address"] is None

    listed = lab.ok("node", "list")["nodes"]
    assert [(n["node"], n["is_master"], n["may_write"], n["target"]) for n in listed] == [
        ("mac-one", True, False, None), ("srv-one", False, True, "prod")]

    removed = lab.ok("node", "remove", "srv-one")["removed"]
    assert removed["node"] == "srv-one"
    assert [n["node"] for n in lab.ok("node", "list")["nodes"]] == ["mac-one"]
    code, error = lab.refused("node", "remove", "srv-one")
    assert (code, error["code"]) == (6, "node_unknown")


def test_an_agent_has_one_master(lab):
    lab.ok("node", "add", "mac-one", "--kind", "mac", "--master")
    lab.ok("node", "add", "srv-one", "--kind", "server")

    for args in (("node", "add", "srv-two", "--kind", "server", "--master"),
                 ("node", "set", "srv-one", "--master")):
        code, error = lab.refused(*args)
        assert (code, error["code"]) == (6, "master_exists")
        assert "mac-one" in error["message"]

    lab.ok("node", "set", "mac-one", "--no-master")
    assert lab.ok("node", "set", "srv-one", "--master")["node"]["is_master"] is True

    import psycopg
    with pytest.raises(psycopg.errors.UniqueViolation):
        lab.sql(f"INSERT INTO {SCHEMA}.capabilities_agent_nodes (agent_id, node, kind, "
                "is_master) VALUES (%s, 'raw', 'mac', true)", (PROJECT_ID,))


def test_node_refuses_what_it_does_not_take(lab):
    assert lab.refused("node", "add", "x")[1]["message"] == "node add needs --kind mac|server"
    assert lab.refused("node", "add", "x", "--kind", "vm")[0] == 6
    assert lab.refused("node", "add", "x", "--kind", "mac", "--bogus")[0] == 6
    assert lab.refused("node", "add", "x", "--kind", "mac", "--master", "--no-master")[0] == 6
    lab.ok("node", "add", "x", "--kind", "mac")
    assert lab.refused("node", "add", "x", "--kind", "mac")[1]["code"] == "node_exists"
    assert lab.refused("node", "set", "x")[0] == 6
    assert lab.refused("node", "set", "nope", "--kind", "mac")[1]["code"] == "node_unknown"


def test_writes_are_refused_under_the_read_only_switch(lab):
    lab.ok("node", "add", "x", "--kind", "mac")
    for args in (("agent", "set", "--name", "X"), ("node", "add", "y", "--kind", "mac"),
                 ("node", "set", "x", "--may-write"), ("node", "remove", "x")):
        code, error = lab.refused(*args, CAPABILITIES_READ_ONLY="1")
        assert (code, error["code"]) == (4, "read_only_switch")
    result = lab.run("node", "list", CAPABILITIES_READ_ONLY="1")
    assert result.returncode == 0, result.stderr
    assert [n["node"] for n in json.loads(result.stdout)["nodes"]] == ["x"]


# --- agent creation from ContextKit's templates --------------------------------

def _square_png(side: int = 64) -> bytes:
    import struct

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + b"\x10\x20\x30" * side for _ in range(side))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


AGENT_MD = "---\ntitle: Agent\ndescription: Who the agent is.\nload: inline\norder: 2\n---\n\n# Agent\n"


def _templates(tmp_path: Path) -> Path:
    """A ContextKit templates folder: one starter context file and one agent template."""
    root = tmp_path / "templates"
    (root / "context" / "identity").mkdir(parents=True)
    (root / "context" / "identity" / "MISSION.md").write_text(
        "---\ntitle: Mission\ndescription: What the agent is for.\nload: inline\norder: 1\n---\n\n"
        "# Mission\n")
    agent = root / "agents" / "demo"
    (agent / "context" / "identity").mkdir(parents=True)
    (agent / "routines").mkdir()
    (agent / "template.toml").write_text(
        'id = "demo"\nname = "Demo"\ndescription = "A demo agent."\nicon = "icon.png"\n'
        'capabilities = ["absent-cap"]\nagent_names = ["Ada"]\n\n[onboarding]\nroutine = "onboarding"\n')
    (agent / "icon.png").write_bytes(_square_png())
    (agent / "context" / "identity" / "AGENT.md").write_text(AGENT_MD)
    (agent / "routines" / "onboarding.md").write_text(
        "---\nname: onboarding\ndescription: Get to know the owner.\n---\n\n# Onboarding\n")
    return root


class Creation:
    """A new, empty agent folder in the lab's store, with the global records moved."""

    def __init__(self, lab: Lab, tmp_path: Path):
        self.lab = lab
        self.folder = tmp_path / "agent"
        self.folder.mkdir()
        self.extra = {"AGENT_NODE_NAME": "lab-node", "CLAUDE_PROJECT_DIR": str(self.folder),
                      "CONTEXTKIT_TEMPLATES_DIR": str(_templates(tmp_path)),
                      "AGENTKIT_DB_URL": lab.cluster.url(lab.database),
                      "AGENTKIT_DB_SCHEMA": SCHEMA}
        migrated = lab.run("migrate", "--global", "--apply", **self.extra)
        assert migrated.returncode == 0, migrated.stderr

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MANAGER), *args], cwd=self.folder,
                              env=self.lab.env(**self.extra), text=True, capture_output=True,
                              timeout=120)


def test_init_template_creates_an_agent_from_a_contextkit_template(lab, tmp_path):
    made = Creation(lab, tmp_path)
    listed = made.run("templates", "--json")
    assert listed.returncode == 0, listed.stderr
    templates = json.loads(listed.stdout)
    assert [s["path"] for s in templates["starter"]] == ["context/identity/MISSION.md"]
    assert [(a["id"], a["capabilities"], a["onboarding"]) for a in templates["agents"]] == [
        ("demo", ["absent-cap"], {"routine": "onboarding"})]

    result = made.run("init", "--template=demo", "--name=Probe", "--json")
    # The agent is created; the capability this machine does not have is reported.
    assert result.returncode == 1, result.stderr
    answer = json.loads(result.stdout)
    assert [(c["name"], c["result"]) for c in answer["capabilities"]] == [("absent-cap", "failed")]
    folder = made.folder
    assert (folder / ".git").is_dir()
    assert (folder / ".contextkit" / "config.toml").read_text().startswith(
        'version = 1\ntype = "agent-project"\n')
    assert (folder / "context" / "identity" / "AGENT.md").read_text() == AGENT_MD
    assert (folder / "routines" / "onboarding.md").is_file()
    assert not (folder / "context" / "identity" / "MISSION.md").exists()
    for layer in ("assets/sessions", "assets/plans", "assets/research", ".contextkit/audits"):
        assert (folder / layer).is_dir()
    icon = _square_png()
    import hashlib
    sha = hashlib.sha256(icon).hexdigest()
    assert (folder / ".contextkit" / f"icon-{sha[:12]}.png").read_bytes() == icon

    identity = json.loads((folder / "capabilities" / "project.json").read_text())
    assert identity["records"] == "store"
    assert lab.sql(f"SELECT name, description, icon_sha, onboarding_status, onboarding_routine, "
                   f"migrated_at IS NOT NULL FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (identity["id"],)) == [("Probe", "A demo agent.", sha, "pending", "onboarding", True)]
    assert lab.sql(f"SELECT node, is_master FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE agent_id = %s", (identity["id"],)) == [("lab-node", True)]

    # Both hosts are wired to the manager, and the first build is made.
    claude = json.loads((folder / ".claude" / "settings.json").read_text())
    assert claude["hooks"]["SessionStart"][0]["hooks"][0]["command"] == \
        "/bin/bash -lc 'capabilities session-start --host claude'"
    assert "exec capabilities session-start --host codex" in \
        (folder / ".codex" / "hooks" / "build-context.sh").read_text()
    assert (folder / ".contextkit" / "generated" / "context.md").is_file()
    assert answer["build"]["links"] == {"codex": "linked", "claude": "linked"}


def test_init_template_refuses_a_folder_that_is_not_empty(lab, tmp_path):
    made = Creation(lab, tmp_path)
    (made.folder / "notes.txt").write_text("mine")
    result = made.run("init", "--template", "demo")
    assert (result.returncode, json.loads(result.stderr)["error"]["code"]) == (6, "folder_not_empty")
    assert sorted(p.name for p in made.folder.iterdir()) == ["notes.txt"]


def _install_agenttemplates(registry: Path, templates: Path) -> Path:
    """An installed agenttemplates payload carrying `templates` in its bundle."""
    payload = registry / "agenttemplates"
    payload.mkdir(parents=True)
    (payload / "agenttemplates").write_text("#!/bin/sh\n")
    return shutil.copytree(templates, payload / "templates")


def test_templates_are_read_from_agenttemplates_when_it_is_installed(tmp_path):
    bundled = _templates(tmp_path / "bundled")
    contextkit = shutil.copytree(bundled, tmp_path / "contextkit" / "templates")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CAPABILITIES_", "CLAUDE"))}
    env.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_STATE_HOME": str(tmp_path / "state"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"),
                "CONTEXTKIT_TEMPLATES_DIR": str(contextkit)})

    def templates() -> str:
        result = subprocess.run([sys.executable, str(MANAGER), "templates", "--json"],
                                cwd=tmp_path, env=env, text=True, capture_output=True,
                                timeout=120)
        assert result.returncode == 0, result.stderr
        return result.stdout

    fallback = templates()
    assert json.loads(fallback)["source"] == str(contextkit)
    bundled = _install_agenttemplates(tmp_path / "registry", bundled)
    answer = templates()
    assert json.loads(answer)["source"] == str(bundled)
    # The same set answers the same bytes; only the folder it names differs.
    assert answer == fallback.replace(str(contextkit), str(bundled))


def test_init_template_reads_the_template_from_agenttemplates(lab, tmp_path):
    made = Creation(lab, tmp_path)
    _install_agenttemplates(tmp_path / "registry", Path(made.extra["CONTEXTKIT_TEMPLATES_DIR"]))
    made.extra["CONTEXTKIT_TEMPLATES_DIR"] = str(tmp_path / "nothing-here")
    result = made.run("init", "--template=demo", "--name=Probe", "--json")
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["template"] == "demo"
    assert (made.folder / "context" / "identity" / "AGENT.md").read_text() == AGENT_MD


def test_the_empty_agent_is_laid_named_and_bootstrapped(lab, tmp_path):
    made = Creation(lab, tmp_path)
    for args in (("init", "--with-template", "--json"),
                 ("identity", "set", "--name=Probe", "--json"),
                 ("bootstrap", "--target", "claude", "--target", "codex", "--yes", "--json")):
        result = made.run(*args)
        assert result.returncode == 0, (args, result.stderr)
    folder = made.folder
    assert (folder / "context" / "identity" / "MISSION.md").is_file()
    assert (folder / ".env.local").is_file() and (folder / ".contextkit" / "README.md").is_file()
    identity = json.loads((folder / "capabilities" / "project.json").read_text())
    assert lab.sql(f"SELECT name, migrated_at IS NOT NULL FROM {SCHEMA}.capabilities_agents "
                   "WHERE id = %s", (identity["id"],)) == [("Probe", True)]
    log = subprocess.run(["git", "log", "--format=%s"], cwd=folder, text=True,
                         capture_output=True)
    assert log.stdout.strip() == "ContextKit pre-bootstrap checkpoint"
    assert "capabilities session-start --host claude" in \
        (folder / ".claude" / "settings.json").read_text()
    assert (folder / ".contextkit.md").is_symlink()


# --- the context documents a build composes ---------------------------------------


_CONTEXT_FILES = {
    "identity/MISSION": ("Mission", "What the agent is for.", "inline", 10, "\n# Mission\n\nServe.\n"),
    "guidelines/STYLE": ('"Style"', "How it writes.", "stub", 20, "\n# Style\n"),
    "guidelines/ORDER": ("Order", "Same order, sorted by path.", "inline", 10, "\n# Order\n"),
}


def _context_text(title, description, load, order, body) -> str:
    return (f"---\ntitle: {title}\ndescription: {description}\nload: {load}\norder: {order}\n"
            f"---\n{body}")


def _seed_context_documents(lab: Lab, documents: list[tuple]) -> None:
    """The `context` capability's two tables, holding `documents`:
    (project_id, name, retired, [(title, description, load, position, body, active)])."""
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.execute(f"CREATE TABLE {SCHEMA}.context_documents (id uuid PRIMARY KEY "
                     "DEFAULT gen_random_uuid(), project_id text, name text NOT NULL, "
                     "created_at timestamptz NOT NULL DEFAULT now(), created_by text NOT NULL, "
                     "retired_at timestamptz)")
        conn.execute(f"CREATE TABLE {SCHEMA}.context_versions (id uuid PRIMARY KEY "
                     "DEFAULT gen_random_uuid(), document_id uuid NOT NULL REFERENCES "
                     f"{SCHEMA}.context_documents (id), title text NOT NULL, description text "
                     "NOT NULL, load text NOT NULL, position integer NOT NULL, body text NOT NULL, "
                     "active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL "
                     "DEFAULT now(), created_by text NOT NULL, note text)")
        for project, name, retired, versions in documents:
            (document,) = conn.execute(
                f"INSERT INTO {SCHEMA}.context_documents (project_id, name, created_by, retired_at) "
                "VALUES (%s, %s, 'tester@lab', CASE WHEN %s THEN now() END) RETURNING id",
                (project, name, retired)).fetchone()
            for version in versions:
                conn.execute(f"INSERT INTO {SCHEMA}.context_versions (document_id, title, "
                             "description, load, position, body, active, created_by) "
                             "VALUES (%s, %s, %s, %s, %s, %s, %s, 'tester@lab')",
                             (document, *version))


def test_build_composes_the_context_from_the_documents_in_the_store(lab, tmp_path):
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[sources]\nglobal_context = '
        f'"{tmp_path / "global"}"\n\n[identity]\nid = "{PROJECT_ID}"\n')
    for name, fields in _CONTEXT_FILES.items():
        (project / "context" / name).parent.mkdir(parents=True, exist_ok=True)
        (project / "context" / f"{name}.md").write_text(_context_text(*fields))
    (tmp_path / "global").mkdir()
    shared = ("Shared", "Doctrine every agent shares.", "inline", 5, "\n# Shared\n")
    (tmp_path / "global" / "SHARED.md").write_text(_context_text(*shared))

    def build() -> str:
        result = lab.run("build", "--print")
        assert result.returncode == 0, result.stderr
        return result.stdout

    from_folder = build()
    assert "## Global Source:" in from_folder and "**Style** (`context/guidelines/STYLE.md`)" \
        in from_folder

    # The store holds the same documents, beside a retired one, a version no
    # longer active, another project's document and a global one.
    _seed_context_documents(lab, [
        *[(PROJECT_ID, name, False, [(*fields[:3], fields[3], fields[4], True)])
          for name, fields in _CONTEXT_FILES.items() if name != "identity/MISSION"],
        (PROJECT_ID, "identity/MISSION", False,
         [("Old", "Before.", "stub", 99, "\n# Old\n", False),
          (*_CONTEXT_FILES["identity/MISSION"], True)]),
        (PROJECT_ID, "identity/GONE", True, [("Gone", "Retired.", "inline", 1, "\n# Gone\n", True)]),
        ("prj_000000000000", "identity/OTHER", False,
         [("Other", "Not this agent's.", "inline", 1, "\n# Other\n", True)]),
        (None, "SHARED", False, [(*shared, True)]),
    ])
    warning = ("- context documents are read from the store; the context folder `context/` "
               "is no longer read and can be removed")
    beside_folder = build()
    assert warning in beside_folder.splitlines()
    assert beside_folder.replace(warning + "\n", "", 1) == from_folder

    # Without the folders the store alone composes the same context.
    shutil.rmtree(project / "context")
    shutil.rmtree(tmp_path / "global")
    assert build() == from_folder


def test_a_document_that_does_not_read_leaves_the_build_on_the_folder(lab):
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[identity]\nid = "{PROJECT_ID}"\n')
    (project / "context" / "identity").mkdir(parents=True)
    (project / "context" / "identity" / "MISSION.md").write_text(
        _context_text(*_CONTEXT_FILES["identity/MISSION"]))
    from_folder = lab.run("build", "--print")
    assert from_folder.returncode == 0, from_folder.stderr
    _seed_context_documents(lab, [
        (PROJECT_ID, "identity/MISSION", False, [("Stored", "In the store.", "bogus", 1,
                                                  "\n# Stored\n", True)])])
    result = lab.run("build", "--print")
    assert result.returncode == 0, result.stderr
    warning = ("- context documents not read from the store, so the context folder is used: "
               "Invalid frontmatter load in context/identity/MISSION.md: bogus")
    assert warning in result.stdout.splitlines()
    assert result.stdout.replace(warning + "\n", "", 1) == from_folder.stdout


def _seed_routines(lab: Lab, routines: list[tuple]) -> None:
    """The `routine` capability's two tables, holding `routines`:
    (project_id, name, retired, [(description, body, active)])."""
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.execute(f"CREATE TABLE {SCHEMA}.routine_documents (id uuid PRIMARY KEY "
                     "DEFAULT gen_random_uuid(), project_id text NOT NULL, name text NOT NULL, "
                     "created_at timestamptz NOT NULL DEFAULT now(), created_by text NOT NULL, "
                     "retired_at timestamptz)")
        conn.execute(f"CREATE TABLE {SCHEMA}.routine_versions (id uuid PRIMARY KEY "
                     "DEFAULT gen_random_uuid(), document_id uuid NOT NULL REFERENCES "
                     f"{SCHEMA}.routine_documents (id), title text, description text NOT NULL, "
                     "body text NOT NULL, active boolean NOT NULL DEFAULT true, created_at "
                     "timestamptz NOT NULL DEFAULT now(), created_by text NOT NULL, note text)")
        for project, name, retired, versions in routines:
            (document,) = conn.execute(
                f"INSERT INTO {SCHEMA}.routine_documents (project_id, name, created_by, retired_at) "
                "VALUES (%s, %s, 'tester@lab', CASE WHEN %s THEN now() END) RETURNING id",
                (project, name, retired)).fetchone()
            for version in versions:
                conn.execute(f"INSERT INTO {SCHEMA}.routine_versions (document_id, description, "
                             "body, active, created_by) VALUES (%s, %s, %s, %s, 'tester@lab')",
                             (document, *version))


def test_build_composes_the_routine_index_from_the_routines_in_the_store(lab):
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[identity]\nid = "{PROJECT_ID}"\n')
    (project / "context" / "identity").mkdir(parents=True)
    (project / "context" / "identity" / "MISSION.md").write_text(
        _context_text(*_CONTEXT_FILES["identity/MISSION"]))
    (project / "routines" / "ops").mkdir(parents=True)
    (project / "routines" / "zeta.md").write_text(
        "---\nname: zeta\ndescription: Run last by name.\nload: inline\norder: 1\n---\n# Zeta\n")
    (project / "routines" / "ops" / "alpha.md").write_text(
        "---\nname: alpha\ndescription: Run first by name.\n---\n# Alpha\n")

    def build() -> str:
        result = lab.run("build", "--print")
        assert result.returncode == 0, result.stderr
        return result.stdout

    from_folder = build()
    assert "- **[alpha](routines/ops/alpha.md)** - Run first by name." in from_folder

    # The store holds both, beside a version no longer active, a retired
    # routine and another project's routine.
    _seed_routines(lab, [
        (PROJECT_ID, "zeta", False, [("Before.", "# Old\n", False),
                                     ("Run last by name.", "# Zeta\n", True)]),
        (PROJECT_ID, "alpha", False, [("Run first by name.", "# Alpha\n", True)]),
        (PROJECT_ID, "gone", False, []),
        (PROJECT_ID, "retired", True, [("Retired.", "# Retired\n", True)]),
        ("prj_000000000000", "other", False, [("Not this agent's.", "# Other\n", True)]),
    ])
    index = ("<!-- Routine index - generated from the store's active routine versions. -->\n\n"
             "# Routines (repeatable procedures)\n\n"
             "Each is a self-contained recipe. Run `routine show <name>` for its text when you "
             "need to run that procedure.\n\n"
             "- **alpha** (`routine show alpha`) - Run first by name.\n"
             "- **zeta** (`routine show zeta`) - Run last by name.\n")
    warning = ("- routines are read from the store; the routines folder `routines/` "
               "is no longer read and can be removed")
    beside_folder = build()
    assert warning in beside_folder.splitlines()
    assert index in beside_folder
    assert "routines/ops/alpha.md" not in beside_folder

    # Without the folder the store alone composes the same index, and no
    # warning says the routine layer is missing.
    shutil.rmtree(project / "routines")
    from_store = build()
    assert from_store == beside_folder.replace(f"Generation warnings:\n\n{warning}\n\n", "", 1)
    assert "routine layer not present" not in from_store
