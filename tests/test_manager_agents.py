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
