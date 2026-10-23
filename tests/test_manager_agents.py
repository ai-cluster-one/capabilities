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

    # The id is the binding and the store holds the rest: no project.json.
    assert not (lab.project / "capabilities" / "project.json").exists()
    agent_id = _bound(lab.project)
    assert not (lab.project / "capabilities" / "settings.json").exists()
    # The slug is the folder's name, the agent having none yet.
    assert lab.sql(f"SELECT slug FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (agent_id,)) == [("project",)]
    assert lab.sql(f"SELECT migrated_at IS NOT NULL FROM {SCHEMA}.capabilities_agents "
                   "WHERE id = %s", (agent_id,)) == [(True,)]
    assert lab.sql(f"SELECT node, is_master FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE agent_id = %s", (agent_id,)) == [("lab-node", True)]


def _bound(folder: Path) -> str | None:
    """The id the folder's .env.local binds it to, or None."""
    found = [line.split("=", 1)[1] for line in (folder / ".env.local").read_text().splitlines()
             if line.startswith("CAPABILITIES_PROJECT_ID=")]
    return found[0] if found else None


def test_init_id_raises_an_agent_the_store_holds_in_a_folder_of_its_own(lab, tmp_path):
    node = {"AGENT_NODE_NAME": "lab-node"}
    migrated = lab.run("migrate", "--global", "--apply", **node)
    assert migrated.returncode == 0, migrated.stderr
    (lab.project / "capabilities" / "project.json").unlink()
    created = lab.run("init", **node)
    assert created.returncode == 0, created.stderr
    agent_id = _bound(lab.project)
    added = lab.run("node", "add", "srv-node", "--kind", "server", **node)
    assert added.returncode == 0, added.stderr
    setting = (lab.project / ".env.local").read_text().replace(
        f"CAPABILITIES_PROJECT_ID={agent_id}\n", "")

    def init_in(folder: Path, *args: str) -> subprocess.CompletedProcess:
        folder.mkdir()
        (folder / ".env.local").write_text(setting)
        return subprocess.run(
            [sys.executable, str(MANAGER), "init", *args], cwd=folder,
            env=lab.env(CLAUDE_PROJECT_DIR=str(folder), AGENT_NODE_NAME="srv-node"),
            text=True, capture_output=True, timeout=120)

    # A folder holding nothing but the store setting is raised as the agent.
    server = tmp_path / "server"
    raised = init_in(server, "--id", agent_id)
    assert raised.returncode == 0, raised.stderr
    answer = json.loads(raised.stdout)
    assert answer["raised"] == {"id": agent_id, "verified": True, "migrated": True,
                                "name": None, "store": {"level": "project", "schema": SCHEMA}}
    assert (server / ".env.local").read_text() == setting + f"CAPABILITIES_PROJECT_ID={agent_id}\n"
    assert not (server / "capabilities" / "project.json").exists()
    assert not (server / "capabilities" / "settings.json").exists()
    assert answer["agent"]["registered"] is True and answer["agent"]["created"] is False

    # An id the store does not hold is refused, and nothing is written.
    stray = tmp_path / "stray"
    refused = init_in(stray, "--id", "prj_00000000dead")
    assert refused.returncode == 6, refused.stderr
    assert json.loads(refused.stderr.strip().splitlines()[-1])["error"]["code"] == "agent_unknown"
    assert (stray / ".env.local").read_text() == setting
    assert not (stray / "capabilities").exists()


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

def test_agent_materialize_puts_a_stored_agent_in_a_folder_on_this_machine(lab, tmp_path):
    node = {"AGENT_NODE_NAME": "lab-node"}
    migrated = lab.run("migrate", "--global", "--apply", **node)
    assert migrated.returncode == 0, migrated.stderr
    (lab.project / "capabilities" / "project.json").unlink()
    created = lab.run("init", **node)
    assert created.returncode == 0, created.stderr
    agent_id = _bound(lab.project)
    # init records the folder the new agent lives in on its master node.
    assert lab.sql(f"SELECT node, folder FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE agent_id = %s", (agent_id,)) == [("lab-node", str(lab.project))]
    setting = (lab.project / ".env.local").read_text().replace(
        f"CAPABILITIES_PROJECT_ID={agent_id}\n", "")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env.local").write_text(setting)

    def materialize(*args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(MANAGER), "agent", "materialize", *args], cwd=elsewhere,
            env=lab.env(CLAUDE_PROJECT_DIR=str(elsewhere),
                        AGENTKIT_DB_URL=lab.cluster.url(lab.database),
                        AGENTKIT_DB_SCHEMA=SCHEMA, **extra),
            text=True, capture_output=True, timeout=120)

    def code(result: subprocess.CompletedProcess) -> tuple[int, str]:
        return result.returncode, json.loads(
            result.stderr.strip().splitlines()[-1])["error"]["code"]

    folder = tmp_path / "box" / "agent"
    first = materialize(agent_id, str(folder), AGENT_NODE_NAME="box-node")
    assert first.returncode == 0, first.stderr
    answer = json.loads(first.stdout)
    assert answer["changed"] is True and answer["folder"] == str(folder)
    assert (answer["node"]["node"], answer["node"]["kind"], answer["node"]["is_master"],
            answer["node"]["folder"]) == ("box-node", "server", False, str(folder))
    assert (folder / ".env.local").read_text() == (
        f"CAPABILITIES_PROJECT_ID={agent_id}\nCAPABILITIES_PROJECT_ID_ROOT={folder}\n")
    assert "capabilities session-start" in (folder / ".claude" / "settings.json").read_text()
    assert (folder / ".capabilities-generated" / "context.md").is_file()
    files = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    again = materialize(agent_id, str(folder), AGENT_NODE_NAME="box-node")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["changed"] is False
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == files

    # A folder bound to another agent, and a second folder for the agent on
    # this node while its first is still bound to it, are refused unwritten.
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / ".env.local").write_text("CAPABILITIES_PROJECT_ID=prj_00000000beef\n")
    assert code(materialize(agent_id, str(taken), AGENT_NODE_NAME="box-node")) \
        == (6, "folder_bound")
    assert (taken / ".env.local").read_text() == "CAPABILITIES_PROJECT_ID=prj_00000000beef\n"
    second = tmp_path / "second"
    assert code(materialize(agent_id, str(second), AGENT_NODE_NAME="box-node")) \
        == (6, "agent_elsewhere")
    assert not second.exists()
    assert code(materialize("prj_00000000dead", str(second), AGENT_NODE_NAME="box-node")) \
        == (6, "agent_unknown")
    assert code(materialize(agent_id, str(second))) == (6, "node_unnamed")

    listed = json.loads(subprocess.run(
        [sys.executable, str(MANAGER), "agent", "list"], cwd=tmp_path,
        env=lab.env(CLAUDE_PROJECT_DIR=str(tmp_path),
                    AGENTKIT_DB_URL=lab.cluster.url(lab.database), AGENTKIT_DB_SCHEMA=SCHEMA),
        text=True, capture_output=True, timeout=120, check=True).stdout)["agents"]
    assert [(a["id"], [(n["node"], n["folder"]) for n in a["nodes"]]) for a in listed] == [
        (agent_id, [("box-node", str(folder)), ("lab-node", str(lab.project))])]


def test_nodes_are_added_changed_listed_and_removed(lab):
    added = lab.ok("node", "add", "mac-one", "--kind", "mac", "--master")
    assert added["node"] == {"node": "mac-one", "kind": "mac", "address": None,
                             "is_master": True, "may_write": False, "target": None,
                             "last_seen_at": None, "coolify_connection": None,
                             "coolify_server": None, "coolify_resource": None,
                             "public_key": None, "folder": None}
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


def test_a_coolify_node_names_its_connection_server_and_resource_and_its_key(lab):
    added = lab.ok("node", "add", "box-one", "--kind", "server",
                   "--coolify-connection", "infra", "--coolify-server", "srv-uuid",
                   "--coolify-resource", "res-uuid")["node"]
    assert (added["coolify_connection"], added["coolify_server"],
            added["coolify_resource"], added["public_key"]) == (
        "infra", "srv-uuid", "res-uuid", None)

    key = "A" * 42 + "Q="
    assert lab.ok("node", "set", "box-one", "--public-key", key)["node"]["public_key"] == key
    assert lab.refused("node", "set", "box-one", "--public-key", "not-a-key")[0] == 6

    code, error = lab.refused("node", "add", "box-two", "--kind", "server",
                              "--coolify-resource", "res-two")
    assert (code, error["code"]) == (6, "coolify_incomplete")
    code, error = lab.refused("node", "set", "box-one", "--coolify-server", "")
    assert (code, error["code"]) == (6, "coolify_incomplete")
    assert lab.sql(f"SELECT coolify_server FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE node = 'box-one'") == [("srv-uuid",)]


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

    assert not (folder / "capabilities" / "project.json").exists()
    agent_id = _bound(folder)
    # A new agent's slug is its name, transliterated.
    assert lab.sql(f"SELECT slug FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (agent_id,)) == [("probe",)]
    assert lab.sql(f"SELECT name, description, icon_sha, onboarding_status, onboarding_routine, "
                   f"migrated_at IS NOT NULL FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (agent_id,)) == [("Probe", "A demo agent.", sha, "pending", "onboarding", True)]
    assert lab.sql(f"SELECT node, is_master FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE agent_id = %s", (agent_id,)) == [("lab-node", True)]

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
    assert lab.sql(f"SELECT name, migrated_at IS NOT NULL FROM {SCHEMA}.capabilities_agents "
                   "WHERE id = %s", (_bound(folder),)) == [("Probe", True)]
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
    # A document the store holds is named by the command that reads it, not
    # by a file path.
    from_store = from_folder.replace(
        f"`{(tmp_path / 'global' / 'SHARED.md').resolve()}`", "`context show SHARED --global`")
    for name in _CONTEXT_FILES:
        from_store = from_store.replace(f"`context/{name}.md`", f"`context show {name}`")
    from_store = from_store.replace(
        "These context files are visible by metadata only. Load the path when the task needs "
        "that doctrine.", "These context documents are visible by metadata only. Run the "
        "command beside one when the task needs that doctrine.")
    assert "**Style** (`context show guidelines/STYLE`)" in from_store
    warning = ("- context documents are read from the store; the context folder `context/` "
               "is no longer read and can be removed")
    beside_folder = build()
    assert warning in beside_folder.splitlines()
    assert beside_folder.replace(warning + "\n", "", 1) == from_store

    # Without the folders the store alone composes the same context.
    shutil.rmtree(project / "context")
    shutil.rmtree(tmp_path / "global")
    assert build() == from_store


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



def test_an_agent_that_lives_in_the_store_builds_with_no_context_folder(lab):
    # A store-only agent has no context folder by design: what its store holds
    # is its context, before any document is written there and after.
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[identity]\nid = "{PROJECT_ID}"\n')
    assert not (project / "context").exists()
    for result in (lab.run("build", "--print"), lab.run("hooks", "install", "--claude")):
        assert result.returncode == 0, result.stderr
    _seed_context_documents(lab, [("prj_000000000000", "identity/OTHER", False,
                                   [("Other", "Not this agent's.", "inline", 1, "\n# Other\n",
                                     True)])])
    empty = lab.run("build", "--print")
    assert empty.returncode == 0, empty.stderr
    assert "# Other" not in empty.stdout

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


def test_build_indexes_a_global_routine_once_the_agent_enables_it(lab):
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[identity]\nid = "{PROJECT_ID}"\n')
    (project / "context" / "identity").mkdir(parents=True)
    (project / "context" / "identity" / "MISSION.md").write_text(
        _context_text(*_CONTEXT_FILES["identity/MISSION"]))
    _seed_routines(lab, [(PROJECT_ID, "alpha", False, [("Run first by name.", "# Alpha\n", True)])])
    with lab.cluster.connect(lab.database) as conn:
        # The tables as the routine ledger's global step leaves them.
        conn.execute(f"ALTER TABLE {SCHEMA}.routine_documents ALTER COLUMN project_id "
                     "DROP NOT NULL")
        conn.execute(f"CREATE TABLE {SCHEMA}.routine_enablements (project_id text NOT NULL, "
                     f"document_id uuid NOT NULL REFERENCES {SCHEMA}.routine_documents (id), "
                     "enabled_at timestamptz NOT NULL DEFAULT now(), enabled_by text NOT NULL)")
        globals_ = {}
        for name, description in (("shared", "Run in every agent."), ("alpha", "Shadowed."),
                                  ("unused", "Not enabled.")):
            (document,) = conn.execute(
                f"INSERT INTO {SCHEMA}.routine_documents (project_id, name, created_by) "
                "VALUES (NULL, %s, 'tester@lab') RETURNING id", (name,)).fetchone()
            conn.execute(f"INSERT INTO {SCHEMA}.routine_versions (document_id, description, "
                         "body, created_by) VALUES (%s, %s, '# x\n', 'tester@lab')",
                         (document, description))
            globals_[name] = document

    def build() -> str:
        result = lab.run("build", "--print")
        assert result.returncode == 0, result.stderr
        return result.stdout

    def enable(name: str, on: bool = True) -> None:
        with lab.cluster.connect(lab.database) as conn:
            if on:
                conn.execute(f"INSERT INTO {SCHEMA}.routine_enablements (project_id, document_id, "
                             "enabled_by) VALUES (%s, %s, 'tester@lab')",
                             (PROJECT_ID, globals_[name]))
            else:
                conn.execute(f"DELETE FROM {SCHEMA}.routine_enablements WHERE document_id = %s",
                             (globals_[name],))

    before = build()
    assert "- **alpha** (`routine show alpha`) - Run first by name.\n" in before
    assert "routine show shared" not in before and "routine show unused" not in before
    enable("shared")
    enable("alpha")
    enabled = build()
    assert ("- **alpha** (`routine show alpha`) - Run first by name.\n"
            "- **shared** (`routine show shared`) - Run in every agent.\n") in enabled
    assert "Shadowed." not in enabled and "routine show unused" not in enabled
    enable("shared", on=False)
    assert build() == before


def test_build_points_the_onboarding_block_at_the_routine_where_the_agent_keeps_it(lab):
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[identity]\nid = "{PROJECT_ID}"\n\n'
        '[onboarding]\nstatus = "pending"\nroutine = "onboard"\n')
    (project / "context" / "identity").mkdir(parents=True)
    (project / "context" / "identity" / "MISSION.md").write_text(
        _context_text(*_CONTEXT_FILES["identity/MISSION"]))
    (project / "routines").mkdir()
    (project / "routines" / "onboard.md").write_text(
        "---\nname: onboard\ndescription: Get to know the owner.\n---\n# Onboard\n")

    def build() -> str:
        result = lab.run("build", "--print")
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert "run the routine [onboard](routines/onboard.md) and carry it" in build()

    _seed_routines(lab, [(PROJECT_ID, "onboard", False, [("Get to know the owner.",
                                                          "# Onboard\n", True)])])
    shutil.rmtree(project / "routines")
    from_store = build()
    assert "run the routine **onboard** (`routine show onboard`) and carry it" in from_store
    assert "is not declared in" not in from_store


# --- an agent bound by its id alone ---------------------------------------------

BARE_ID = "prj_00000000cc01"


class Folder:
    """A folder of its own beside the lab's store setting, run by the manager."""

    def __init__(self, lab: Lab, path: Path, agent_id: str | None = BARE_ID):
        self.lab, self.path = lab, path
        path.mkdir(parents=True)
        setting = f"AGENTKIT_DB_URL={lab.cluster.url(lab.database)}\nAGENTKIT_DB_SCHEMA={SCHEMA}\n"
        (path / ".env.local").write_text(
            setting + (f"CAPABILITIES_PROJECT_ID={agent_id}\n" if agent_id else ""))

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MANAGER), *args], cwd=self.path,
                              env=self.lab.env(CLAUDE_PROJECT_DIR=str(self.path)),
                              text=True, capture_output=True, timeout=120)

    def ok(self, *args: str) -> dict:
        result = self.run(*args)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def refused(self, *args: str) -> tuple[int, dict]:
        result = self.run(*args)
        line = next(line for line in reversed(result.stderr.splitlines())
                    if line.lstrip().startswith("{"))
        return result.returncode, json.loads(line)["error"]

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                 *args], cwd=self.path, text=True, capture_output=True,
                                timeout=60)
        assert result.returncode == 0, result.stderr
        return result.stdout


def test_an_agent_without_a_contextkit_binding_keeps_its_settings_in_its_row(lab, tmp_path):
    import psycopg
    folder = Folder(lab, tmp_path / "bare")
    written = folder.ok("agent", "set", "--collaboration-mode", "direct")["agent"]
    assert (written["collaboration_mode"], written["body_root"]) == ("direct", None)
    assert folder.refused("agent", "set", "--collaboration-mode", "sometimes")[0] == 6
    for bad in ("", "/abs", "../up", "a/../b", ".hidden", " agent"):
        with pytest.raises(psycopg.errors.CheckViolation):
            lab.sql(f"UPDATE {SCHEMA}.capabilities_agents SET body_root = %s WHERE id = %s "
                    "RETURNING id", (bad, BARE_ID))

    built = folder.ok("build")
    output = folder.path / ".capabilities-generated" / "context.md"
    assert built["output"] == str(output)
    text = output.read_text()
    assert "Source config: the agent's row (`capabilities agent show`)" in text
    assert "## Collaboration Mode" not in text

    # The envelope sits in the body root the row names, read on a record miss.
    lab.sql(f"UPDATE {SCHEMA}.capabilities_agents SET body_root = 'agent' WHERE id = %s "
            "RETURNING id", (BARE_ID,))
    records = lab.tmp / "cache" / "capabilities" / "envelope"
    shutil.rmtree(records, ignore_errors=True)
    path = folder.ok("path", "--json")
    assert path["project_envelope"] == str(folder.path / "agent" / "capabilities")
    assert (path["body_root"], path["layers"]["context"]) == (
        "agent", str(folder.path / "agent" / "context"))
    assert "This project keeps its visible body under `agent/`." in folder.ok(
        "build", "--print", "--json")["text"]

    # While the store does not answer, the answer recorded for the folder
    # stands, past its expiry too; with none recorded, an existing
    # <root>/capabilities folder is the envelope; without that either, it is
    # refused rather than guessed.
    env_local = folder.path / ".env.local"
    env_local.write_text(env_local.read_text().replace(
        f":{lab.cluster.port}/", f":{_free_port()}/"))
    [(record, entry)] = [(path, json.loads(path.read_text())) for path in records.iterdir()
                         if json.loads(path.read_text())["root"] == str(folder.path.resolve())]
    entry["expires_at"] = entry["written_at"] + 1
    record.write_text(json.dumps(entry))
    assert folder.ok("path", "--json")["project_envelope"] == str(
        folder.path / "agent" / "capabilities")
    shutil.rmtree(records)
    code, error = folder.refused("path", "--json")
    assert (code, error["code"]) == (5, "store_unreachable")
    (folder.path / "capabilities").mkdir()
    assert folder.ok("path", "--json")["project_envelope"] == str(folder.path / "capabilities")
    assert not records.exists() or not any(records.iterdir())


def _bound_agent(lab: Lab, path: Path, config: str) -> Folder:
    """A ContextKit-bound agent in a Git repository with its body under agent/,
    its row registered and Claude wired, everything committed."""
    folder = Folder(lab, path)
    folder.git("init", "-q")
    (path / ".gitignore").write_text(".env.local\n")
    for rel, body in {
        "agent/context/MISSION.md": "---\ntitle: Mission\ndescription: Why.\nload: inline\norder: 1\n---\n# Mission\n",
        "agent/routines/welcome.md": "---\nname: welcome\ndescription: Run to onboard.\n---\n# Welcome\n",
        ".contextkit/config.toml": config,
        ".contextkit/README.md": "# ContextKit Binding\n",
        ".claude/settings.json": json.dumps({"permissions": {"allow": ["Bash(ls)"]}}),
    }.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(body)
    import hashlib
    (path / ".contextkit" / f"icon-{hashlib.sha256(_png()).hexdigest()}.png").write_bytes(_png())
    folder.ok("agent", "set", "--name", "Bound")
    folder.ok("hooks", "install", "--claude")
    folder.git("add", "-A")
    folder.git("commit", "-qm", "bound")
    return folder


BOUND_CONFIG = ('version = 1\ntype = "agent-project"\n\n[body]\nroot = "agent"\n\n'
                '[collaboration]\nmode = "direct"\n\n[onboarding]\nstatus = "pending"\n'
                'routine = "welcome"\n')


def test_agent_convert_carries_a_bound_agent_into_its_row_as_one_change(lab, tmp_path):
    import hashlib
    folder = _bound_agent(lab, tmp_path / "bound", BOUND_CONFIG)
    root = folder.path
    before = (root / ".contextkit" / "generated" / "context.md").read_text()

    converted = folder.ok("agent", "convert")

    assert converted["changed"] is True
    assert converted["carried"] == {
        "body_root": "agent", "collaboration_mode": "direct",
        "onboarding": {"status": "pending", "routine": "welcome"},
        "icon_sha": hashlib.sha256(_png()).hexdigest()}
    agent = converted["agent"]
    assert (agent["body_root"], agent["collaboration_mode"], agent["onboarding_status"],
            agent["onboarding_routine"], agent["icon_bytes"]) == (
        "agent", "direct", "pending", "welcome", len(_png()))
    assert not (root / ".contextkit").exists()
    output = root / ".capabilities-generated" / "context.md"
    link = root / ".claude" / "rules" / "CONTEXT.md"
    assert link.is_symlink() and link.resolve() == output.resolve()
    text = output.read_text()
    assert text != before and "# Onboarding" in text and "## Collaboration Mode" not in text
    assert "This project keeps its visible body under `agent/`." in text
    assert "# Foreign Instruction Files" not in text
    settings = json.loads((root / ".claude" / "settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    assert ".capabilities-generated/" in (root / ".gitignore").read_text().splitlines()
    assert set(converted["untracked"]) == {
        ".claude/rules/CONTEXT.md", ".claude/settings.json", ".contextkit/README.md",
        ".contextkit/config.toml", ".contextkit/generated/context.md",
        f".contextkit/icon-{hashlib.sha256(_png()).hexdigest()}.png"}
    status = folder.git("status", "--porcelain").splitlines()
    assert sorted(status) == sorted([f"D  {path}" for path in converted["untracked"]]
                                    + ["M  .gitignore"])
    assert (root / ".claude" / "settings.json").is_file() and link.is_symlink()
    assert folder.ok("path", "--json")["project_envelope"] == str(root / "agent" / "capabilities")

    again = folder.ok("agent", "convert")
    assert (again["changed"], again["removed"], again["untracked"], again["wrote"]) == (
        False, [], [], [])
    assert folder.git("status", "--porcelain").splitlines() == status


def test_agent_convert_refuses_before_writing_anything(lab, tmp_path):
    def unchanged(folder: Folder, config: str) -> None:
        assert (folder.path / ".contextkit" / "config.toml").read_text() == config
        assert not (folder.path / ".capabilities-generated").exists()
        assert folder.git("status", "--porcelain") == ""
        assert lab.sql(f"SELECT body_root FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                       (BARE_ID,)) == [(None,)]

    (tmp_path / "global").mkdir()
    retired = BOUND_CONFIG + f'\n[sources]\nglobal_context = "{tmp_path / "global"}"\n'
    folder = _bound_agent(lab, tmp_path / "sources", retired)
    code, error = folder.refused("agent", "convert")
    assert (code, error["code"]) == (6, "setting_retired")
    assert "sources.global_context" in error["message"]
    unchanged(folder, retired)
    assert folder.ok("build")["output"] == str(folder.path / ".contextkit" / "generated" / "context.md")

    folder = _bound_agent(lab, tmp_path / "foreign", BOUND_CONFIG)
    (folder.path / ".contextkit" / "manager").mkdir()
    (folder.path / ".contextkit" / "manager" / "contextkit").write_text("#!/bin/sh\n")
    code, error = folder.refused("agent", "convert")
    assert (code, error["code"]) == (6, "contextkit_folder_not_empty")
    assert ".contextkit/manager" in error["message"]
    (folder.path / ".contextkit" / "manager" / "contextkit").unlink()
    (folder.path / ".contextkit" / "manager").rmdir()
    unchanged(folder, BOUND_CONFIG)

    # A refusal while it builds puts back what it had changed: the agent stays
    # bound, its host link on its own output.
    folder = _bound_agent(lab, tmp_path / "invalid", BOUND_CONFIG)
    link = folder.path / ".claude" / "rules" / "CONTEXT.md"
    (folder.path / "agent" / "context" / "BROKEN.md").write_text(
        "---\ntitle: Broken\ndescription: No order.\nload: inline\n---\n# Broken\n")
    folder.git("add", "-A")
    folder.git("commit", "-qm", "broken")
    code, error = folder.refused("agent", "convert")
    assert (code, error["code"]) == (6, "context_file_invalid")
    unchanged(folder, BOUND_CONFIG)
    assert link.resolve() == (folder.path / ".contextkit" / "generated" / "context.md").resolve()

    folder = _bound_agent(lab, tmp_path / "occupied", BOUND_CONFIG)
    link = folder.path / ".claude" / "rules" / "CONTEXT.md"
    occupied = folder.path / ".capabilities-generated" / "context.md"
    occupied.parent.mkdir()
    occupied.write_text("# Not generated\n")
    code, error = folder.refused("agent", "convert")
    assert (code, error["code"]) == (6, "output_not_generated")
    assert occupied.read_text() == "# Not generated\n"
    assert link.resolve() == (folder.path / ".contextkit" / "generated" / "context.md").resolve()
    assert folder.git("status", "--porcelain").splitlines() == ["?? .capabilities-generated/"]
    assert not folder.ok("build")["links"]["claude"].startswith("blocked")

    unknown = Folder(lab, tmp_path / "unknown", agent_id="prj_00000000dd01")
    (unknown.path / ".contextkit").mkdir()
    (unknown.path / ".contextkit" / "config.toml").write_text(BOUND_CONFIG)
    code, error = unknown.refused("agent", "convert")
    assert (code, error["code"]) == (6, "agent_unknown")
    assert (unknown.path / ".contextkit" / "config.toml").read_text() == BOUND_CONFIG
