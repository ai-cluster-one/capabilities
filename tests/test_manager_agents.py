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
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
import zlib
from pathlib import Path

import pytest



def nid(label: str) -> str:
    """The node id a test gives the machine it plays as `label`: the label's
    first six characters as hex, so the ids sort as their labels do."""
    return label.encode()[:6].ljust(6, b"\0").hex()


def node_env(label: str) -> dict:
    """The environment of a process on that machine: its id, and its label."""
    return {"AGENT_NODE_ID": nid(label), "AGENT_NODE_NAME": label}


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

    def ok(self, *args: str, **extra: str) -> dict:
        result = self.run(*args, **extra)
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
    node = {**node_env("lab-node")}

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
                   "WHERE agent_id = %s", (agent_id,)) == [(nid("lab-node"), True)]


def _bound(folder: Path) -> str | None:
    """The id the folder's .env.local binds it to, or None."""
    found = [line.split("=", 1)[1] for line in (folder / ".env.local").read_text().splitlines()
             if line.startswith("CAPABILITIES_PROJECT_ID=")]
    return found[0] if found else None


def test_init_id_raises_an_agent_the_store_holds_in_a_folder_of_its_own(lab, tmp_path):
    node = {**node_env("lab-node")}
    migrated = lab.run("migrate", "--global", "--apply", **node)
    assert migrated.returncode == 0, migrated.stderr
    (lab.project / "capabilities" / "project.json").unlink()
    created = lab.run("init", **node)
    assert created.returncode == 0, created.stderr
    agent_id = _bound(lab.project)
    added = lab.run("node", "add", nid("srv-node"), "--label", "srv-node", "--kind", "server", **node)
    assert added.returncode == 0, added.stderr
    setting = (lab.project / ".env.local").read_text().replace(
        f"CAPABILITIES_PROJECT_ID={agent_id}\n", "")

    def init_in(folder: Path, *args: str) -> subprocess.CompletedProcess:
        folder.mkdir()
        (folder / ".env.local").write_text(setting)
        return subprocess.run(
            [sys.executable, str(MANAGER), "init", *args], cwd=folder,
            env=lab.env(CLAUDE_PROJECT_DIR=str(folder), **node_env("srv-node")),
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


def test_a_folder_an_agent_is_raised_in_does_not_name_it(lab, tmp_path):
    # A node raises an agent the store already holds in /app: an agent with no
    # slug and no name is not named "app" after it.
    node = {**node_env("lab-node")}
    migrated = lab.run("migrate", "--global", "--apply", **node)
    assert migrated.returncode == 0, migrated.stderr
    (lab.project / "capabilities" / "project.json").unlink()
    created = lab.run("init", **node)
    assert created.returncode == 0, created.stderr
    agent_id = _bound(lab.project)
    added = lab.run("node", "add", nid("srv-node"), "--label", "srv-node", "--kind", "server", **node)
    assert added.returncode == 0, added.stderr
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"UPDATE {SCHEMA}.capabilities_agents SET slug = NULL, name = NULL "
                     "WHERE id = %s", (agent_id,))
    setting = (lab.project / ".env.local").read_text().replace(
        f"CAPABILITIES_PROJECT_ID={agent_id}\n", "")
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env.local").write_text(setting)
    raised = subprocess.run(
        [sys.executable, str(MANAGER), "init", "--id", agent_id], cwd=app,
        env=lab.env(CLAUDE_PROJECT_DIR=str(app), **node_env("srv-node")),
        text=True, capture_output=True, timeout=120)
    assert raised.returncode == 0, raised.stderr
    assert json.loads(raised.stdout)["agent"]["slug"] is None
    assert lab.sql(f"SELECT slug FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (agent_id,)) == [(None,)]


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
    node = {**node_env("lab-node")}
    migrated = lab.run("migrate", "--global", "--apply", **node)
    assert migrated.returncode == 0, migrated.stderr
    (lab.project / "capabilities" / "project.json").unlink()
    created = lab.run("init", **node)
    assert created.returncode == 0, created.stderr
    agent_id = _bound(lab.project)
    # init records the folder the new agent lives in on its master node.
    assert lab.sql(f"SELECT node, folder FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE agent_id = %s", (agent_id,)) == [(nid("lab-node"), str(lab.project))]
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
    first = materialize(agent_id, str(folder), **node_env("box-node"))
    assert first.returncode == 0, first.stderr
    answer = json.loads(first.stdout)
    assert answer["changed"] is True and answer["folder"] == str(folder)
    assert (answer["node"]["node"], answer["node"]["kind"], answer["node"]["is_master"],
            answer["node"]["folder"]) == (nid("box-node"), "server", False, str(folder))
    assert (folder / ".env.local").read_text() == (
        f"CAPABILITIES_PROJECT_ID={agent_id}\nCAPABILITIES_PROJECT_ID_ROOT={folder}\n")
    assert "capabilities session-start" in (folder / ".claude" / "settings.json").read_text()
    assert (folder / ".capabilities-generated" / "context.md").is_file()
    files = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    again = materialize(agent_id, str(folder), **node_env("box-node"))
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["changed"] is False
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == files

    # A folder bound to another agent, and a second folder for the agent on
    # this node while its first is still bound to it, are refused unwritten.
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / ".env.local").write_text("CAPABILITIES_PROJECT_ID=prj_00000000beef\n")
    assert code(materialize(agent_id, str(taken), **node_env("box-node"))) \
        == (6, "folder_bound")
    assert (taken / ".env.local").read_text() == "CAPABILITIES_PROJECT_ID=prj_00000000beef\n"
    second = tmp_path / "second"
    assert code(materialize(agent_id, str(second), **node_env("box-node"))) \
        == (6, "agent_elsewhere")
    assert not second.exists()
    assert code(materialize("prj_00000000dead", str(second), **node_env("box-node"))) \
        == (6, "agent_unknown")
    assert code(materialize(agent_id, str(second), AGENT_NODE_ID="box node")) \
        == (6, "bad_node_id")

    listed = json.loads(subprocess.run(
        [sys.executable, str(MANAGER), "agent", "list"], cwd=tmp_path,
        env=lab.env(CLAUDE_PROJECT_DIR=str(tmp_path),
                    AGENTKIT_DB_URL=lab.cluster.url(lab.database), AGENTKIT_DB_SCHEMA=SCHEMA),
        text=True, capture_output=True, timeout=120, check=True).stdout)["agents"]
    assert [(a["id"], [(n["node"], n["folder"]) for n in a["nodes"]]) for a in listed] == [
        (agent_id, [(nid("box-node"), str(folder)), (nid("lab-node"), str(lab.project))])]

    # An agent the store holds under the bare twelve-hex id is materialized
    # too.
    bare = "4a5b6c7d8e9f"
    lab.sql(f"INSERT INTO {SCHEMA}.capabilities_agents (id) VALUES (%s) RETURNING id", (bare,))
    plain = tmp_path / "plain"
    placed = materialize(bare, str(plain), **node_env("box-node"))
    assert placed.returncode == 0, placed.stderr
    assert json.loads(placed.stdout)["node"]["folder"] == str(plain)
    assert f"CAPABILITIES_PROJECT_ID={bare}\n" in (plain / ".env.local").read_text()

    # A refusal while it wires the folder leaves no binding and no node row.
    broken = tmp_path / "broken"
    (broken / ".claude").mkdir(parents=True)
    (broken / ".claude" / "settings.json").write_text("{not json")
    refused = materialize(bare, str(broken), **node_env("other-node"))
    assert refused.returncode != 0
    assert not (broken / ".env.local").exists()
    assert lab.sql(f"SELECT node FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE agent_id = %s", (bare,)) == [(nid("box-node"),)]


def test_nodes_are_added_changed_listed_and_removed(lab):
    added = lab.ok("node", "add", nid("mac-one"), "--label", "mac-one", "--kind", "mac", "--master")
    assert added["node"] == {"node": nid("mac-one"), "label": "mac-one", "kind": "mac",
                             "address": None,
                             "is_master": True, "may_write": False, "target": None,
                             "last_seen_at": None, "coolify_connection": None,
                             "coolify_server": None, "coolify_resource": None,
                             "public_key": None, "folder": None, "disabled_at": None}
    lab.ok("node", "add", nid("srv-one"), "--label", "srv-one", "--kind", "server", "--address", "203.0.113.10",
           "--target", "prod", "--may-write")

    assert lab.ok("node", "set", nid("srv-one"), "--may-write")["changed"] is False
    assert lab.ok("node", "set", nid("srv-one"), "--address", "")["node"]["address"] is None

    listed = lab.ok("node", "list")["nodes"]
    assert [(n["node"], n["is_master"], n["may_write"], n["target"]) for n in listed] == [
        (nid("mac-one"), True, False, None), (nid("srv-one"), False, True, "prod")]

    removed = lab.ok("node", "remove", nid("srv-one"))["removed"]
    assert removed["node"] == nid("srv-one")
    assert [n["node"] for n in lab.ok("node", "list")["nodes"]] == [nid("mac-one")]
    code, error = lab.refused("node", "remove", nid("srv-one"))
    assert (code, error["code"]) == (6, "node_unknown")


def test_a_coolify_node_names_its_connection_server_and_resource_and_its_key(lab):
    added = lab.ok("node", "add", nid("box-one"), "--label", "box-one", "--kind", "server",
                   "--coolify-connection", "infra", "--coolify-server", "srv-uuid",
                   "--coolify-resource", "res-uuid")["node"]
    assert (added["coolify_connection"], added["coolify_server"],
            added["coolify_resource"], added["public_key"]) == (
        "infra", "srv-uuid", "res-uuid", None)

    key = "A" * 42 + "Q="
    assert lab.ok("node", "set", nid("box-one"), "--public-key", key)["node"]["public_key"] == key
    assert lab.refused("node", "set", nid("box-one"), "--public-key", "not-a-key")[0] == 6

    code, error = lab.refused("node", "add", nid("box-two"), "--label", "box-two", "--kind", "server",
                              "--coolify-resource", "res-two")
    assert (code, error["code"]) == (6, "coolify_incomplete")
    code, error = lab.refused("node", "set", nid("box-one"), "--coolify-server", "")
    assert (code, error["code"]) == (6, "coolify_incomplete")
    assert lab.sql(f"SELECT coolify_server FROM {SCHEMA}.capabilities_nodes "
                   "WHERE id = %s", (nid("box-one"),)) == [("srv-uuid",)]


def test_an_agent_has_one_master(lab):
    lab.ok("node", "add", nid("mac-one"), "--label", "mac-one", "--kind", "mac", "--master")
    lab.ok("node", "add", nid("srv-one"), "--label", "srv-one", "--kind", "server")

    for args in (("node", "add", nid("srv-two"), "--label", "srv-two", "--kind", "server", "--master"),
                 ("node", "set", nid("srv-one"), "--master")):
        code, error = lab.refused(*args)
        assert (code, error["code"]) == (6, "master_exists")
        assert nid("mac-one") in error["message"]

    lab.ok("node", "set", nid("mac-one"), "--no-master")
    assert lab.ok("node", "set", nid("srv-one"), "--master")["node"]["is_master"] is True

    import psycopg
    with pytest.raises(psycopg.errors.UniqueViolation):
        lab.sql(f"INSERT INTO {SCHEMA}.capabilities_nodes (id, label, kind) "
                "VALUES ('raw', 'raw', 'mac') RETURNING id")
        lab.sql(f"INSERT INTO {SCHEMA}.capabilities_agent_nodes (agent_id, node, "
                "is_master) VALUES (%s, 'raw', true)", (PROJECT_ID,))


def test_node_refuses_what_it_does_not_take(lab):
    x = nid("x")
    assert lab.refused("node", "add", x, "--label", "x")[1]["message"] == (
        f"node {x} is new: node add needs --kind mac|server")
    assert lab.refused("node", "add", x, "--kind", "mac")[1]["message"] == (
        f"node {x} is new: node add needs --label, the name it is shown by")
    assert lab.refused("node", "add", "-x", "--label", "x", "--kind", "mac")[0] == 6
    assert lab.refused("node", "add", "x y", "--label", "x", "--kind", "mac")[1]["code"] == "input"
    assert lab.refused("node", "add", x, "--label", "x", "--kind", "vm")[0] == 6
    assert lab.refused("node", "add", x, "--label", "x", "--kind", "mac", "--bogus")[0] == 6
    assert lab.refused("node", "add", x, "--label", "x", "--kind", "mac", "--master",
                       "--no-master")[0] == 6
    lab.ok("node", "add", x, "--label", "x", "--kind", "mac")
    assert lab.refused("node", "add", x, "--kind", "mac")[1]["code"] == "node_exists"
    assert lab.refused("node", "set", x)[0] == 6
    assert lab.refused("node", "set", x, "--label", "")[1]["code"] == "input"
    assert lab.refused("node", "set", nid("nope"), "--kind", "mac")[1]["code"] == "node_unknown"


def test_a_node_reports_itself_seen_and_no_other(lab):
    lab.ok("node", "add", nid("box-one"), "--label", "box-one", "--kind", "server")
    lab.ok("node", "add", nid("box-two"), "--label", "box-two", "--kind", "server")

    def seen(label: str) -> dict:
        result = lab.run("node", "seen", **node_env(label))
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    first = seen("box-one")
    assert (first["node"], first["agent_id"]) == (nid("box-one"), PROJECT_ID)
    assert first["last_seen_at"] is not None
    lab.sql(f"UPDATE {SCHEMA}.capabilities_nodes "
            "SET last_seen_at = now() - interval '1 hour' WHERE id = %s RETURNING id",
            (nid("box-one"),))
    again = seen("box-one")
    assert again["last_seen_at"] >= first["last_seen_at"]
    assert lab.sql(f"SELECT last_seen_at > now() - interval '1 minute' FROM "
                   f"{SCHEMA}.capabilities_nodes WHERE id = %s", (nid("box-one"),)) == [(True,)]
    listed = {n["node"]: n["last_seen_at"] for n in lab.ok("node", "list")["nodes"]}
    assert listed == {nid("box-one"): again["last_seen_at"], nid("box-two"): None}

    code, error = lab.refused("node", "seen", **node_env("box-three"))
    assert (code, error["code"]) == (6, "node_unknown")
    assert lab.refused("node", "seen", nid("box-two"), **node_env("box-one"))[0] == 6


def _state_tools(lab: Lab, account: str = "lab@example.test") -> dict:
    """The environment of a node whose tools answer as the test says - Claude
    Code signed in as `account`, the GitHub CLI signed out, Codex not
    installed - whose image names its version and boot line, and whose
    registry holds one capability whose connections are device-bound."""
    tools = lab.tmp / "tools"
    tools.mkdir()
    for name, body in (
            ("claude", f"printf '%s\\n' '{{\"loggedIn\": true, \"email\": \"{account}\"}}'"),
            ("gh", "printf '%s\\n' '{\"hosts\": {}}'; exit 1")):
        (tools / name).write_text(f"#!/bin/sh\n{body}\n")
        (tools / name).chmod(0o755)
    image = lab.tmp / "image"
    image.mkdir()
    (image / "VERSION").write_text("0.0.12\n")
    (image / "agent-node.state").write_text("running agent lab as node box-one\n")
    registry = lab.tmp / "registry" / "phonelink"
    registry.mkdir(parents=True)
    (registry / "manifest.json").write_text(json.dumps({
        "name": "phonelink",
        "connections": {"fields": None, "setup": [], "link": [], "device_bound": True}}))
    # Its connections, as it answers them in the folder it is asked in.
    (registry / "phonelink").write_text(
        "#!/bin/sh\n[ \"$1 $2\" = 'connections --json' ] || exit 2\n"
        "printf '%s\\n' '{\"connections\": {\"main\": {\"session\": "
        "{\"live\": true, \"account\": \"+10000000000\"}}, \"spare\": {\"session\": "
        "{\"live\": false, \"account\": null}}, \"remote\": {\"session\": {\"live\": null}}}}'\n")
    (registry / "phonelink").chmod(0o755)
    return {"PATH": str(tools), "AGENT_NODE_VERSION_FILE": str(image / "VERSION"),
            "AGENT_NODE_STATE_FILE": str(image / "agent-node.state")}


def test_a_session_that_is_down_on_a_node_says_why(lab):
    lab.ok("node", "add", nid("box-one"), "--label", "box-one", "--kind", "server")
    here = {**node_env("box-one"), **_state_tools(lab)}
    keys = [{"key": "API_ID", "required": True, "set": True},
            {"key": "API_HASH", "required": True, "set": False},
            {"key": "VOICE_KEY", "required": False, "set": False}]
    answer = json.dumps({"connections": {
        "personal": {"keys": keys, "session": {"live": False, "account": None,
                                               "reason": "session_missing"}},
        "unsigned": {"keys": keys[:1], "session": {"live": False, "account": None,
                                                   "reason": "not_signed_in"}},
        "gone": {"keys": keys[:1], "session": {"live": False, "account": None,
                                               "reason": "session_missing"}},
        "quiet": {"session": {"live": False, "account": None}},
        "odd": {"session": {"live": False, "account": None, "reason": "logged_out"}},
        "main": {"keys": keys, "session": {"live": True, "account": "+10000000000"}}}})
    script = lab.tmp / "registry" / "phonelink" / "phonelink"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(answer)}\n")

    expected = [("gone", False, "session_missing", None),
                ("main", True, None, None),
                ("odd", False, None, None),
                ("personal", False, "key_missing", ["API_HASH"]),
                ("quiet", False, None, None),
                ("unsigned", False, "not_signed_in", None)]
    reported = lab.ok("node", "state", "report", **here)
    assert [(s["connection"], s["live"], s["reason"], s["reason_keys"])
            for s in reported["sessions"]] == expected
    [node] = lab.ok("node", "state")["nodes"]
    assert [(s["connection"], s["live"], s["reason"], s["reason_keys"])
            for s in node["sessions"]] == expected
    assert [s["account"] for s in node["sessions"]][1] == "+10000000000"


def test_a_node_reports_its_state_and_the_agent_reads_every_nodes(lab):
    lab.ok("node", "add", nid("box-one"), "--label", "box-one", "--kind", "server")
    lab.ok("node", "add", nid("box-two"), "--label", "box-two", "--kind", "server")
    here = {**node_env("box-one"), **_state_tools(lab)}

    reported = lab.ok("node", "state", "report", **here)
    assert (reported["node"], reported["agent_id"]) == (nid("box-one"), PROJECT_ID)
    state = reported["state"]
    assert state["reachable"] is True and state["fresh_until"] > state["reported_at"]
    assert (state["claude_login"], state["claude_account"]) == ("signed_in", "lab@example.test")
    assert (state["gh_login"], state["gh_account"]) == ("signed_out", None)
    assert (state["codex_login"], state["codex_account"]) == ("absent", None)
    assert state["image_version"] == "0.0.12"
    assert state["boot_line"] == "running agent lab as node box-one"
    assert state["cpu_count"] >= 1 and state["load_1"] >= 0
    assert 0 <= state["memory_used_bytes"] and state["memory_limit_bytes"] > 0
    assert 0 < state["disk_used_bytes"] < state["disk_total_bytes"]
    assert state["booted_at"] and state["uptime_seconds"] >= 0
    assert [(s["capability"], s["connection"], s["live"], s["account"])
            for s in reported["sessions"]] == [("phonelink", "main", True, "+10000000000"),
                                               ("phonelink", "spare", False, None)]

    nodes = {n["node"]: n for n in lab.ok("node", "state")["nodes"]}
    assert set(nodes) == {nid("box-one"), nid("box-two")}
    one, two = nodes[nid("box-one")], nodes[nid("box-two")]
    assert one["label"] == "box-one" and one["reachable"] is True
    assert {k: one[k] for k in state if k != "uptime_seconds"} == \
        {k: v for k, v in state.items() if k != "uptime_seconds"}
    assert [s["connection"] for s in one["sessions"]] == ["main", "spare"]
    assert two["reachable"] is False and two["reported_at"] is None and two["sessions"] == []
    # Off the image the node says nothing of an image.
    bare = {**here}
    del bare["AGENT_NODE_VERSION_FILE"], bare["AGENT_NODE_STATE_FILE"]
    again = lab.ok("node", "state", "report", **bare)["state"]
    assert (again["image_version"], again["boot_line"]) == (None, None)
    assert again["reported_at"] >= state["reported_at"]

    # Once fresh_until has passed by the store's clock, the node reads unreachable.
    lab.sql(f"UPDATE {SCHEMA}.capabilities_node_states SET reported_at = now() - "
            "interval '2 minutes', fresh_until = now() - interval '1 second' "
            "WHERE node = %s RETURNING node", (nid("box-one"),))
    assert {n["node"]: n["reachable"] for n in lab.ok("node", "state")["nodes"]} == {
        nid("box-one"): False, nid("box-two"): False}

    code, error = lab.refused("node", "state", "report", **node_env("box-three"))
    assert (code, error["code"]) == (6, "node_unknown")
    assert lab.refused("node", "state", "report", "x", **here)[0] == 6
    assert lab.refused("node", "state", "report", CAPABILITIES_READ_ONLY="1",
                       **here)[1]["code"] == "read_only_switch"
    assert lab.run("node", "state", CAPABILITIES_READ_ONLY="1").returncode == 0


def test_a_nodes_state_follows_it_through_a_rekey(lab):
    lab.ok("agent", "show")
    lab.sql(f"INSERT INTO {SCHEMA}.capabilities_agents (id) VALUES (%s) ON CONFLICT DO NOTHING "
            "RETURNING id", (PROJECT_ID,))
    lab.sql(f"INSERT INTO {SCHEMA}.capabilities_nodes (id, label, kind) VALUES "
            "('box-one', 'box-one', 'server') RETURNING id")
    lab.sql(f"INSERT INTO {SCHEMA}.capabilities_agent_nodes (agent_id, node, is_master) "
            "VALUES (%s, 'box-one', true) RETURNING node", (PROJECT_ID,))
    old = {"AGENT_NODE_ID": "box-one", **_state_tools(lab)}
    lab.ok("node", "state", "report", **old)
    planned = lab.ok("node", "rekey", "box-one", nid("box-one"), "--dry-run")
    assert {(r["table"], r["rows"], r["how"]) for r in planned["rows"]} >= {
        ("capabilities_node_states", 1, "foreign key"),
        ("capabilities_node_logins", 1, "foreign key"),
        ("capabilities_node_sessions", 2, "foreign key")}
    lab.ok("node", "rekey", "box-one", nid("box-one"))
    assert lab.sql(f"SELECT node FROM {SCHEMA}.capabilities_node_states") == [(nid("box-one"),)]
    assert lab.sql(f"SELECT DISTINCT node FROM {SCHEMA}.capabilities_node_sessions") == [
        (nid("box-one"),)]
    [node] = lab.ok("node", "state")["nodes"]
    assert node["node"] == nid("box-one") and node["reachable"] is True


def _other_agent(lab: Lab, agent_id: str) -> Lab:
    """Another agent in the same store, in a folder, home and registry of its
    own: an agent of another OS user on the same machine."""
    other = Lab.__new__(Lab)
    other.tmp, other.cluster, other.database = lab.tmp / "other", lab.cluster, lab.database
    other.project = other.tmp / "project"
    (other.project / ".git").mkdir(parents=True)
    (other.project / "capabilities").mkdir()
    (other.project / "capabilities" / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "slug": "other", "id": agent_id}))
    (other.project / ".env.local").write_text((lab.project / ".env.local").read_text())
    return other


def test_agents_of_two_os_users_on_one_machine_keep_their_own_sign_ins_and_sessions(lab):
    # A Mac's id is its hardware's, so the manager of each OS user on it
    # reports under the one node id; neither drops nor overwrites what the
    # other's agents hold there.
    other_id = "prj_" + "0f1e2d3c4b5a"
    other = _other_agent(lab, other_id)
    lab.ok("node", "add", nid("box-one"), "--label", "box-one", "--kind", "mac")
    other.ok("node", "add", nid("box-one"), "--label", "box-one", "--kind", "mac")
    mine = {**node_env("box-one"), **_state_tools(lab)}
    theirs = {**node_env("box-one"), **_state_tools(other, "other@example.test")}

    other.ok("node", "state", "report", **theirs)
    lab.ok("node", "state", "report", **mine)
    for agent, account in ((lab, "lab@example.test"), (other, "other@example.test")):
        [node] = agent.ok("node", "state")["nodes"]
        assert (node["reachable"], node["claude_login"], node["claude_account"]) == (
            True, "signed_in", account)
        assert [(s["connection"], s["live"]) for s in node["sessions"]] == [
            ("main", True), ("spare", False)]

    # An agent no longer linked to the node loses what it held there at the
    # next report of any manager on it.
    lab.sql(f"DELETE FROM {SCHEMA}.capabilities_agent_nodes WHERE agent_id = %s "
            "RETURNING agent_id", (other_id,))
    lab.ok("node", "state", "report", **mine)
    assert lab.sql(f"SELECT (SELECT count(*) FROM {SCHEMA}.capabilities_node_sessions "
                   f"WHERE project_id = %s) + (SELECT count(*) FROM "
                   f"{SCHEMA}.capabilities_node_logins WHERE project_id = %s)",
                   (other_id, other_id)) == [(0,)]
    [node] = lab.ok("node", "state")["nodes"]
    assert node["claude_account"] == "lab@example.test" and len(node["sessions"]) == 2


def _placed_dialer(lab: Lab) -> None:
    """A capability whose service needs gh whatever its configuration and
    answers in the agent's folder that its turns run on Claude Code and
    Codex."""
    registry = lab.tmp / "registry" / "dialer"
    registry.mkdir(parents=True)
    (registry / "manifest.json").write_text(json.dumps({
        "name": "dialer", "service": {"name": "dialer", "sign_ins": {
            "tools": ["gh"], "harness": ["dialer", "service", "harness", "--json"]}}}))
    (registry / "dialer").write_text(
        "#!/bin/sh\n[ \"$*\" = 'service harness --json' ] || exit 2\n"
        "[ -d capabilities ] || exit 3\n"
        "printf '%s\\n' '{\"harness\": [\"codex\", \"claude\", \"stub\"]}'\n")
    (registry / "dialer").chmod(0o755)


def test_a_node_is_expected_to_have_what_its_placed_services_need_and_the_owner_says(lab):
    lab.ok("node", "add", nid("box-one"), "--label", "box-one", "--kind", "server")
    lab.ok("node", "add", nid("box-two"), "--label", "box-two", "--kind", "server")
    here = {**node_env("box-one"), **_state_tools(lab)}
    _placed_dialer(lab)

    # Nothing placed: gh signed out, Codex absent and a dead session raise nothing.
    lab.ok("node", "state", "report", **here)
    one = {n["node"]: n for n in lab.ok("node", "state")["nodes"]}[nid("box-one")]
    assert (one["health"], one["attention"]) == ("green", [])
    assert [one[f"{t}_expected"] for t in ("claude", "codex", "gh")] == [False] * 3
    assert [s["expected"] for s in one["sessions"]] == [False, False]

    # The agent places dialer's and phonelink's services on box-one.
    for name in ("dialer", "phonelink"):
        lab.sql(f"INSERT INTO {SCHEMA}.capabilities_policy (agent_id, capability, node, "
                "enabled, service) VALUES (%s, %s, %s, true, true) RETURNING capability",
                (PROJECT_ID, name, nid("box-one")))
    lab.ok("node", "state", "report", **here)
    nodes = {n["node"]: n for n in lab.ok("node", "state")["nodes"]}
    one, two = nodes[nid("box-one")], nodes[nid("box-two")]
    assert [(one[f"{t}_needed"], one[f"{t}_override"], one[f"{t}_expected"])
            for t in ("claude", "codex", "gh")] == [(True, None, True)] * 3
    assert [(s["connection"], s["needed"], s["expected"]) for s in one["sessions"]] == [
        ("main", True, True), ("spare", True, True)]
    assert one["health"] == "yellow"
    assert one["attention"] == [
        {"kind": "sign_in", "tool": "codex", "reason": "absent"},
        {"kind": "sign_in", "tool": "gh", "reason": "signed_out"},
        {"kind": "session", "capability": "phonelink", "connection": "spare",
         "reason": "not_live"}]
    assert (two["health"], two["attention"]) == ("red", [])

    # The owner's word stands over what the services need, per node.
    for args in (("sign-in", "codex", "off"), ("sign-in", "gh", "off"),
                 ("session", "phonelink", "spare", "off")):
        said = lab.ok("node", "expect", "box-one", *args)
        assert (said["node"], said["override"], said["before"], said["changed"]) == (
            nid("box-one"), False, None, True)
    assert lab.ok("node", "expect", "box-one", "sign-in", "gh", "off")["changed"] is False
    said = lab.ok("node", "expect", nid("box-two"), "session", "phonelink", "main", "on")
    assert (said["kind"], said["capability"], said["connection"]) == (
        "session", "phonelink", "main")
    nodes = {n["node"]: n for n in lab.ok("node", "state")["nodes"]}
    one, two = nodes[nid("box-one")], nodes[nid("box-two")]
    assert (one["health"], one["attention"]) == ("green", [])
    assert (one["gh_needed"], one["gh_override"], one["gh_expected"]) == (True, False, False)
    assert two["attention"] == [{"kind": "session", "capability": "phonelink",
                                 "connection": "main", "reason": "not_reported"}]
    assert [(s["connection"], s["live"], s["expected"]) for s in two["sessions"]] == [
        ("main", None, True)]
    # default takes the word back.
    said = lab.ok("node", "expect", "box-one", "sign-in", "gh", "default")
    assert (said["override"], said["before"]) == (None, False)
    one = {n["node"]: n for n in lab.ok("node", "state")["nodes"]}[nid("box-one")]
    assert one["attention"] == [{"kind": "sign_in", "tool": "gh", "reason": "signed_out"}]

    # A harness command that cannot answer keeps nothing it never said, and
    # the declared tools stand.
    (lab.tmp / "registry" / "dialer" / "dialer").write_text("#!/bin/sh\nexit 1\n")
    lab.ok("node", "state", "report", **here)
    one = {n["node"]: n for n in lab.ok("node", "state")["nodes"]}[nid("box-one")]
    assert [one[f"{t}_needed"] for t in ("claude", "codex", "gh")] == [False, False, True]

    for args, code in ((("box-one", "sign-in", "stub", "on"), "input"),
                       (("box-one", "sign-in", "gh"), "input"),
                       (("box-one", "session", "dialer", "main", "on"), "not_device_bound"),
                       (("box-three", "sign-in", "gh", "on"), "node_unknown")):
        assert lab.refused("node", "expect", *args)[1]["code"] == code
    assert lab.refused("node", "expect", "box-one", "sign-in", "gh", "on",
                       CAPABILITIES_READ_ONLY="1")[1]["code"] == "read_only_switch"
    planned = lab.ok("node", "rekey", nid("box-one"), nid("box-six"), "--dry-run")
    assert {(r["table"], r["rows"]) for r in planned["rows"]} >= {
        ("capabilities_node_login_expectations", 1),
        ("capabilities_node_session_expectations", 1)}


def test_a_disabled_node_stays_on_record_expects_nothing_and_takes_nothing(lab):
    lab.ok("node", "add", nid("box-one"), "--label", "box-one", "--kind", "server",
           "--address", "box-one.example.test")
    lab.ok("node", "add", nid("box-two"), "--label", "box-two", "--kind", "server")
    here = {**node_env("box-one"), **_state_tools(lab)}
    _placed_dialer(lab)
    for name in ("dialer", "phonelink"):
        lab.sql(f"INSERT INTO {SCHEMA}.capabilities_policy (agent_id, capability, node, "
                "enabled, service) VALUES (%s, %s, %s, true, true) RETURNING capability",
                (PROJECT_ID, name, nid("box-one")))
    lab.ok("node", "state", "report", **here)
    lab.ok("node", "expect", "box-two", "session", "phonelink", "main", "on")
    nodes = {n["node"]: n for n in lab.ok("node", "state")["nodes"]}
    assert (nodes[nid("box-one")]["health"], nodes[nid("box-two")]["health"]) == (
        "yellow", "red")

    said = lab.ok("node", "disable", "box-one")
    assert (said["node"]["node"], said["changed"]) == (nid("box-one"), True)
    assert said["node"]["disabled_at"] is not None
    assert lab.ok("node", "disable", nid("box-one"))["changed"] is False
    lab.ok("node", "disable", "box-two")
    # Kept on record: its row, its address, its placements, its state.
    listed = {n["node"]: n for n in lab.ok("node", "list")["nodes"]}
    assert listed[nid("box-one")]["address"] == "box-one.example.test"
    assert listed[nid("box-one")]["disabled_at"] == said["node"]["disabled_at"]
    assert lab.sql(f"SELECT count(*) FROM {SCHEMA}.capabilities_policy WHERE node = %s "
                   "AND service", (nid("box-one"),)) == [(2,)]
    nodes = {n["node"]: n for n in lab.ok("node", "state")["nodes"]}
    for node in nodes.values():
        assert (node["health"], node["attention"]) == ("disabled", [])
        assert node["disabled_at"] is not None
        assert [node[f"{t}_expected"] for t in ("claude", "codex", "gh")] == [False] * 3
        assert all(s["expected"] is False for s in node["sessions"])
    one = nodes[nid("box-one")]
    assert one["reachable"] is True and one["claude_login"] == "signed_in"
    assert (one["gh_needed"], one["sessions"][0]["needed"]) == (True, True)
    # It is asked no update.
    code, error = lab.refused("node", "update", "--node", "box-one")
    assert (code, error["code"]) == (6, "node_disabled")

    said = lab.ok("node", "enable", "box-one")
    assert (said["changed"], said["node"]["disabled_at"]) == (True, None)
    assert lab.ok("node", "enable", "box-one")["changed"] is False
    nodes = {n["node"]: n for n in lab.ok("node", "state")["nodes"]}
    assert nodes[nid("box-one")]["health"] == "yellow"
    assert nodes[nid("box-one")]["gh_expected"] is True
    assert nodes[nid("box-two")]["health"] == "disabled"

    for verb in ("disable", "enable"):
        assert lab.refused("node", verb, "box-three")[1]["code"] == "node_unknown"
        code, error = lab.refused("node", verb, "box-one", CAPABILITIES_READ_ONLY="1")
        assert (code, error["code"]) == (4, "read_only_switch")
        assert lab.refused("node", verb)[1]["code"] == "input"


def test_writes_are_refused_under_the_read_only_switch(lab):
    lab.ok("node", "add", nid("x"), "--label", "x", "--kind", "mac")
    for args in (("agent", "set", "--name", "X"),
                 ("node", "add", nid("y"), "--label", "y", "--kind", "mac"),
                 ("node", "set", "x", "--may-write"), ("node", "remove", "x"),
                 ("node", "seen")):
        code, error = lab.refused(*args, CAPABILITIES_READ_ONLY="1")
        assert (code, error["code"]) == (4, "read_only_switch")
    result = lab.run("node", "list", CAPABILITIES_READ_ONLY="1")
    assert result.returncode == 0, result.stderr
    assert [n["node"] for n in json.loads(result.stdout)["nodes"]] == [nid("x")]



def test_a_node_known_by_its_name_moves_to_its_id_and_a_label_renames_it(lab):
    # A node registered before ids: its name is its id, and rows name it.
    lab.ok("agent", "show")
    lab.sql(f"INSERT INTO {SCHEMA}.capabilities_agents (id) VALUES (%s) ON CONFLICT DO NOTHING "
            "RETURNING id", (PROJECT_ID,))
    for statement, params in (
            ("INSERT INTO {s}.capabilities_nodes (id, label, kind) VALUES "
             "('work-mac', 'work-mac', 'mac'), ('box-one', 'box-one', 'server') RETURNING id", ()),
            ("UPDATE {s}.capabilities_nodes SET target = 'work-mac' WHERE id = 'box-one' "
             "RETURNING id", ()),
            ("INSERT INTO {s}.capabilities_agent_nodes (agent_id, node, is_master) VALUES "
             "(%s, 'work-mac', true), (%s, 'box-one', false) RETURNING node",
             (PROJECT_ID, PROJECT_ID)),
            ("INSERT INTO {s}.capabilities_policy (agent_id, capability, node, enabled, service) "
             "VALUES (%s, 'tasks', 'work-mac', true, true) RETURNING node", (PROJECT_ID,)),
            ("INSERT INTO {s}.capabilities_settings_changes (agent_id, capability, node, action, "
             "path, new_value, changed_by) VALUES (%s, 'tasks', 'work-mac', 'set', '{{a}}', '1', "
             "'user@work-mac') RETURNING node", (PROJECT_ID,))):
        lab.sql(statement.format(s=SCHEMA), params)
    mac = node_env("work-mac")

    def everything() -> dict:
        tables = [t for (t,) in lab.sql("SELECT tablename FROM pg_tables WHERE schemaname = %s "
                                        "ORDER BY 1", (SCHEMA,))]
        return {t: sorted(r for (r,) in lab.sql(f"SELECT to_jsonb(x)::text FROM {SCHEMA}.{t} x"))
                for t in tables}

    planned = lab.ok("node", "rekey", "work-mac", "--dry-run", **mac)
    assert (planned["new_id"], planned["store"]["moved"]) == (nid("work-mac"), False)
    assert {(r["table"], r["rows"]) for r in planned["rows"]} >= {
        ("capabilities_agent_nodes", 1), ("capabilities_policy", 1),
        ("capabilities_settings_changes", 1)}
    moved = lab.ok("node", "rekey", "work-mac", **mac)
    assert moved["store"]["moved"] is True and moved["label"] == "work-mac"
    nodes = {n["node"]: n for n in lab.ok("node", "list", **mac)["nodes"]}
    assert set(nodes) == {nid("work-mac"), "box-one"}
    assert (nodes[nid("work-mac")]["label"], nodes[nid("work-mac")]["is_master"]) == ("work-mac", True)
    # The id moved where it named the node, and nowhere else: not the other
    # node's target, which only reads the same, nor the history text.
    assert nodes["box-one"]["target"] == "work-mac"
    assert lab.sql(f"SELECT node FROM {SCHEMA}.capabilities_policy") == [(nid("work-mac"),)]
    assert lab.sql(f"SELECT node, changed_by FROM {SCHEMA}.capabilities_settings_changes") == [
        (nid("work-mac"), "user@work-mac")]
    again = lab.ok("node", "rekey", "work-mac", **mac)
    assert (again["store"]["already_moved"], again["total_rows"]) == (True, 0)
    assert lab.ok("node", "seen", **mac)["node"] == nid("work-mac")

    # A rename is the label alone: no other row changes.
    before = everything()
    renamed = lab.ok("node", "set", "work-mac", "--label", "Office Mac", **mac)["node"]
    assert (renamed["node"], renamed["label"]) == (nid("work-mac"), "Office Mac")
    after = everything()
    changed = {t for t in before if before[t] != after[t]}
    assert changed == {"capabilities_nodes"}
    assert [r for r in after["capabilities_nodes"] if r not in before["capabilities_nodes"]] \
        == [r for r in after["capabilities_nodes"] if nid("work-mac") in r]

    # A live lease on a node keeps it where it is.
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"CREATE TABLE {SCHEMA}.deployment_service_leases (project text, capability "
                     "text, node text NOT NULL, expires_at timestamptz NOT NULL)")
        conn.execute(f"INSERT INTO {SCHEMA}.deployment_service_leases VALUES (%s, 'tasks', "
                     "'box-one', now() + interval '3 minutes')", (PROJECT_ID,))
    code, error = lab.refused("node", "rekey", "box-one", nid("box-one"), **mac)
    assert (code, error["code"]) == (6, "node_services_running")
    assert "box-one" in {n["node"] for n in lab.ok("node", "list", **mac)["nodes"]}



def test_a_node_registered_under_its_id_beside_its_name_merges_into_one_link(lab):
    # A box registered before ids, then registered again under its id, as
    # `node_unregistered`'s hint says: the agent is linked on both.
    lab.ok("agent", "show")
    lab.sql(f"INSERT INTO {SCHEMA}.capabilities_agents (id) VALUES (%s) ON CONFLICT DO NOTHING "
            "RETURNING id", (PROJECT_ID,))
    for statement in (
            "INSERT INTO {s}.capabilities_nodes (id, label, kind) VALUES "
            "('work-mac', 'work-mac', 'mac'), ('box-two', 'box-two', 'server') RETURNING id",
            "INSERT INTO {s}.capabilities_agent_nodes (agent_id, node, is_master, may_write, "
            "folder) VALUES (%s, 'work-mac', true, true, NULL), "
            "(%s, 'box-two', false, true, '/app') RETURNING node"):
        lab.sql(statement.format(s=SCHEMA), (PROJECT_ID, PROJECT_ID) if "%s" in statement else ())
    # The master is still keyed by its name, which the override takes.
    master = {"AGENT_NODE_ID": "work-mac", "AGENT_NODE_NAME": "work-mac"}
    lab.ok("node", "add", nid("box-two"), "--label", "box", "--kind", "server", **master)
    moved = lab.ok("node", "rekey", "box-two", nid("box-two"), **master)
    assert moved["merged"] is True and moved["store"]["moved"] is True
    links = lab.sql(f"SELECT node, is_master, may_write, folder FROM {SCHEMA}.capabilities_agent_nodes "
                    "WHERE agent_id = %s ORDER BY node", (PROJECT_ID,))
    assert sorted(links) == sorted([(nid("box-two"), False, True, "/app"),
                                    ("work-mac", True, True, None)])
    assert {n["node"] for n in lab.ok("node", "list", **master)["nodes"]} == {
        nid("box-two"), "work-mac"}

def _fake_tool(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)


def test_agent_state_reads_an_agent_across_its_nodes(lab):
    lab.ok("node", "add", nid("box-one"), "--label", "box-one", "--kind", "server", "--master", "--may-write",
           "--address", "203.0.113.10")
    lab.ok("node", "add", nid("box-two"), "--label", "box-two", "--kind", "server", "--coolify-connection", "infra",
           "--coolify-server", "srv-uuid", "--coolify-resource", "res-uuid")
    lab.ok("node", "add", nid("mac-one"), "--label", "mac-one", "--kind", "mac")
    lab.sql(f"UPDATE {SCHEMA}.capabilities_agent_nodes SET folder = %s "
            "WHERE node = %s RETURNING node", (str(lab.project.resolve()), nid("box-one")))
    lab.sql(f"UPDATE {SCHEMA}.capabilities_nodes SET last_seen_at = now() "
            "WHERE id = %s RETURNING id", (nid("box-one"),))
    lab.sql(f"UPDATE {SCHEMA}.capabilities_agents SET slug = 'lab' WHERE id = %s "
            "RETURNING id", (PROJECT_ID,))
    for agent, name, node, enabled, service in (
            (PROJECT_ID, "tasks", nid("box-one"), True, True),
            (PROJECT_ID, "tasks", nid("box-two"), True, True),
            (PROJECT_ID, "telegram", nid("box-two"), True, True),
            (PROJECT_ID, "telegram", None, False, False),
            (None, "memory", None, True, False),
            (None, "telegram", nid("mac-one"), True, False)):
        lab.sql(f"INSERT INTO {SCHEMA}.capabilities_policy (agent_id, capability, node, "
                "enabled, service) VALUES (%s, %s, %s, %s, %s) RETURNING capability",
                (agent, name, node, enabled, service))
    with lab.cluster.connect(lab.database) as conn:
        # deployment's lease table, as its runner makes it.
        conn.execute(f"CREATE TABLE {SCHEMA}.deployment_service_leases (project text "
                     "NOT NULL, capability text NOT NULL, node text NOT NULL, acquired_at "
                     "timestamptz NOT NULL DEFAULT now(), renewed_at timestamptz NOT NULL "
                     "DEFAULT now(), expires_at timestamptz NOT NULL, "
                     "PRIMARY KEY (project, capability))")
        conn.execute(f"INSERT INTO {SCHEMA}.deployment_service_leases (project, capability, "
                     "node, expires_at) VALUES (%s, 'telegram', %s, now() + "
                     "interval '3 minutes'), (%s, 'tasks', %s, now() - "
                     "interval '1 minute')",
                     (PROJECT_ID, nid("box-two"), PROJECT_ID, nid("box-one")))
    for name, service in (("tasks", {"name": "tasks", "placement": "shared"}),
                          ("telegram", {"name": "telegram"})):
        bundle = lab.tmp / "registry" / name
        bundle.mkdir(parents=True)
        (bundle / name).write_text("#!/bin/sh\n")
        (bundle / "manifest.json").write_text(json.dumps({"name": name, "service": service}))
    tools = lab.tmp / "tools"
    _fake_tool(tools, "deployment", "import json, os\nprint(json.dumps({'ok': True, "
               f"'agent_id': {PROJECT_ID!r}, 'live_within_seconds': 180, 'nodes': ["
               f"{{'node': {nid('box-one')!r}, 'live': True}}, "
               f"{{'node': {nid('box-two')!r}, 'live': False}}, "
               f"{{'node': {nid('mac-one')!r}, 'live': False}}]}}))\n"
               "open(os.environ['STATE_CALLS'], 'a').write(os.getcwd() + '\\n')\n")
    _fake_tool(tools, "coolify", "import json, sys\nassert sys.argv[1:] == ['--connection', "
               "'infra', 'servers', 'srv-uuid'], sys.argv\n"
               "print(json.dumps({'uuid': 'srv-uuid', 'ip': '198.51.100.7'}))\n")
    calls = lab.tmp / "calls"
    path = f"{tools}{os.pathsep}{os.environ.get('PATH', '')}"

    def state(ref: str, **extra: str) -> dict:
        result = lab.run("agent", "state", ref, "--json", **node_env("box-one"),
                         PATH=path, STATE_CALLS=str(calls), **extra)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    nodes_now = (f"SELECT l.node, n.last_seen_at, l.folder FROM {SCHEMA}.capabilities_agent_nodes "
                 f"l JOIN {SCHEMA}.capabilities_nodes n ON n.id = l.node ORDER BY l.node")
    before = lab.sql(nodes_now)
    answer = state(PROJECT_ID)
    assert (answer["agent_id"], answer["this_node"]) == (PROJECT_ID, nid("box-one"))
    assert answer["liveness"] == {"by": "deployment node list",
                                  "folder": str(lab.project.resolve()),
                                  "live_within_seconds": 180, "reason": None}
    assert calls.read_text().splitlines() == [str(lab.project.resolve())]
    nodes = {n["node"]: n for n in answer["nodes"]}
    assert list(nodes) == [nid("box-one"), nid("box-two"), nid("mac-one")]
    one, two, mac = nodes[nid("box-one")], nodes[nid("box-two")], nodes[nid("mac-one")]
    assert (one["is_master"], one["may_write"], one["folder"]) == (
        True, True, str(lab.project.resolve()))
    assert (one["address"], one["address_source"]) == ("203.0.113.10", "node")
    assert (two["address"], two["address_source"]) == ("198.51.100.7", "coolify")
    assert (mac["address"], mac["address_source"]) == (None, None)
    assert one["last_seen_at"] is not None and 0 <= one["seen_seconds_ago"] < 60
    assert (two["last_seen_at"], two["seen_seconds_ago"]) == (None, None)
    assert (one["live"], two["live"], mac["live"]) == (True, False, False)
    assert one["gate"] == {
        "memory": {"effective": "enabled", "source": "global", "node": None},
        "tasks": {"effective": "enabled", "source": "project", "node": nid("box-one")},
        "telegram": {"effective": "disabled", "source": "project", "node": None}}
    assert two["gate"]["telegram"] == {"effective": "enabled", "source": "project",
                                       "node": nid("box-two")}
    assert mac["gate"]["tasks"] == {"effective": "disabled", "source": "default",
                                    "node": None}
    assert mac["gate"]["telegram"] == {"effective": "disabled", "source": "project",
                                       "node": None}
    assert (one["placed"], two["placed"], mac["placed"]) == (
        ["tasks"], ["tasks", "telegram"], [])
    assert one["leases"] == [] and mac["leases"] == []
    assert [lease["capability"] for lease in two["leases"]] == ["telegram"]
    assert set(two["leases"][0]) == {"capability", "acquired_at", "renewed_at", "expires_at"}
    assert answer["services"] == [
        {"capability": "tasks", "placement": "shared", "nodes": None,
         "placed_on": [nid("box-one"), nid("box-two")], "holders": []},
        {"capability": "telegram", "placement": "exclusive", "nodes": None,
         "placed_on": [nid("box-two")], "holders": [nid("box-two")]}]

    # By slug, from outside any project, under the read-only switch: the same
    # read, with no folder of the agent on this node to ask deployment in.
    elsewhere = lab.tmp / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [sys.executable, str(MANAGER), "agent", "state", "lab"], cwd=elsewhere,
        env={**lab.env(**node_env("mac-one"), PATH=path, STATE_CALLS=str(calls),
                       CAPABILITIES_READ_ONLY="1",
                       AGENTKIT_DB_URL=lab.cluster.url(lab.database),
                       AGENTKIT_DB_SCHEMA=SCHEMA),
             "CLAUDE_PROJECT_DIR": str(elsewhere)},
        text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    remote = json.loads(result.stdout)
    assert remote["agent_id"] == PROJECT_ID
    assert remote["liveness"]["reason"] == "no_folder_here"
    assert all(n["live"] is None for n in remote["nodes"])
    assert [n["placed"] for n in remote["nodes"]] == [["tasks"], ["tasks", "telegram"], []]
    assert calls.read_text().splitlines() == [str(lab.project.resolve())]
    assert lab.sql(nodes_now) == before

    code, error = lab.refused("agent", "state", "nobody", PATH=path)
    assert (code, error["code"]) == (6, "agent_unknown")
    assert lab.refused("agent", "state")[0] == 6


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


# The capabilities that write a new agent's body layers to its store, played by
# scripts that record each call: the arguments and the project it ran as.
LAYER_CAPABILITIES = ("context", "routine", "assets")
FAKE_IMPORT = """import json, os, sys
if sys.argv[1:2] != ["import"]:
    sys.exit(1)
with open(os.environ["IMPORT_LOG"], "a") as log:
    log.write(json.dumps({"capability": %r, "args": sys.argv[1:],
                          "project": os.environ.get("CLAUDE_PROJECT_DIR")}) + "\\n")
fail = os.environ.get("IMPORT_FAIL") == %r
if fail:
    sys.stderr.write(json.dumps({"error": {"code": "store_unreachable", "message": "forced"}}) + "\\n")
    sys.exit(5)
print(json.dumps({"imported": []}))
"""


def _install_agenttemplates(registry: Path, templates: Path) -> Path:
    """An installed agenttemplates payload carrying `templates` in its bundle."""
    payload = registry / "agenttemplates"
    payload.mkdir(parents=True)
    (payload / "agenttemplates").write_text("#!/bin/sh\n")
    return shutil.copytree(templates, payload / "templates")


class Creation:
    """A new, empty agent folder in the lab's store, with the global records
    moved, the templates installed with agenttemplates and the layer
    capabilities recording their imports."""

    def __init__(self, lab: Lab, tmp_path: Path):
        self.lab = lab
        self.folder = tmp_path / "agent"
        self.folder.mkdir()
        self.templates = _install_agenttemplates(tmp_path / "registry", _templates(tmp_path))
        for name in LAYER_CAPABILITIES:
            _fake_tool(tmp_path / "registry" / name, name, FAKE_IMPORT % (name, name))
        self.log = tmp_path / "imports.jsonl"
        self.extra = {**node_env("lab-node"), "CLAUDE_PROJECT_DIR": str(self.folder),
                      "IMPORT_LOG": str(self.log),
                      "AGENTKIT_DB_URL": lab.cluster.url(lab.database),
                      "AGENTKIT_DB_SCHEMA": SCHEMA}
        migrated = lab.run("migrate", "--global", "--apply", **self.extra)
        assert migrated.returncode == 0, migrated.stderr

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MANAGER), *args], cwd=self.folder,
                              env=self.lab.env(**self.extra, **extra), text=True,
                              capture_output=True, timeout=120)

    def imports(self) -> list[tuple]:
        if not self.log.exists():
            return []
        return [(c["capability"], c["args"], c["project"])
                for c in map(json.loads, self.log.read_text().splitlines())]


def _no_contextkit(folder: Path) -> None:
    """A new agent's folder holds no ContextKit binding, no body folder and no
    ContextKit lines in its .gitignore."""
    assert not (folder / ".contextkit").exists()
    for layer in ("context", "routines", "assets"):
        assert not (folder / layer).exists()
    ignore = (folder / ".gitignore").read_text()
    assert "ContextKit" not in ignore and ".contextkit/" not in ignore
    assert {".env", ".env.local", ".env.*.local"} <= set(ignore.splitlines())


def test_init_template_creates_an_agent_in_the_store_from_a_template(lab, tmp_path):
    made = Creation(lab, tmp_path)
    listed = made.run("templates", "--json")
    assert listed.returncode == 0, listed.stderr
    templates = json.loads(listed.stdout)
    assert templates["source"] == str(made.templates)
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
    _no_contextkit(folder)
    # The template's body layers are written to the store by the capabilities that own them.
    template = made.templates / "agents" / "demo"
    assert made.imports() == [
        ("context", ["import", str(template / "context")], str(folder)),
        ("routine", ["import", str(template / "routines")], str(folder))]
    assert [item["by"] for item in answer["imported"]] == ["context import", "routine import"]
    icon = _square_png()
    import hashlib
    sha = hashlib.sha256(icon).hexdigest()
    assert answer["identity"]["icon_sha"] == sha
    assert not list(folder.rglob("icon-*.png"))

    assert not (folder / "capabilities" / "project.json").exists()
    agent_id = _bound(folder)
    # A new agent's slug is its name, transliterated.
    assert lab.sql(f"SELECT slug FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (agent_id,)) == [("probe",)]
    assert lab.sql(f"SELECT name, description, icon_sha, onboarding_status, onboarding_routine, "
                   f"migrated_at IS NOT NULL FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (agent_id,)) == [("Probe", "A demo agent.", sha, "pending", "onboarding", True)]
    assert lab.sql(f"SELECT icon FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (agent_id,)) == [(icon,)]
    assert lab.sql(f"SELECT node, is_master FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE agent_id = %s", (agent_id,)) == [(nid("lab-node"), True)]

    # Both hosts are wired to the manager, and the first build is made in the manager's place.
    claude = json.loads((folder / ".claude" / "settings.json").read_text())
    assert claude["hooks"]["SessionStart"][0]["hooks"][0]["command"] == \
        "/bin/bash -lc 'capabilities session-start --host claude'"
    assert "exec capabilities session-start --host codex" in \
        (folder / ".codex" / "hooks" / "build-context.sh").read_text()
    assert (folder / ".capabilities-generated" / "context.md").is_file()
    assert answer["build"]["links"] == {"codex": "linked", "claude": "linked"}


def test_init_template_that_fails_partway_leaves_nothing_behind(lab, tmp_path):
    made = Creation(lab, tmp_path)
    agents = f"SELECT count(*) FROM {SCHEMA}.capabilities_agents"
    before = lab.sql(agents)
    # The routines are written after the agent is registered, bound and its context written.
    result = made.run("init", "--template=demo", "--name=Probe", "--json", IMPORT_FAIL="routine")
    assert result.returncode == 5, result.stderr
    assert json.loads(result.stderr.splitlines()[-1])["error"]["code"] == "layer_import_failed"
    assert [c[0] for c in made.imports()] == ["context", "routine"]
    assert list(made.folder.iterdir()) == []
    assert lab.sql(agents) == before
    assert lab.sql(f"SELECT count(*) FROM {SCHEMA}.capabilities_agents WHERE slug = 'probe'") \
        == [(0,)]

    # Add agent works again.
    again = made.run("init", "--template=demo", "--name=Probe", "--json")
    assert again.returncode == 1, again.stderr
    assert lab.sql(f"SELECT name FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (_bound(made.folder),)) == [("Probe",)]


def test_init_template_refuses_a_folder_that_is_not_empty(lab, tmp_path):
    made = Creation(lab, tmp_path)
    (made.folder / "notes.txt").write_text("mine")
    result = made.run("init", "--template", "demo")
    assert (result.returncode, json.loads(result.stderr)["error"]["code"]) == (6, "folder_not_empty")
    assert sorted(p.name for p in made.folder.iterdir()) == ["notes.txt"]


def test_templates_are_read_from_agenttemplates_alone(tmp_path):
    contextkit = _templates(tmp_path / "contextkit")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CAPABILITIES_", "CLAUDE"))}
    env.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_STATE_HOME": str(tmp_path / "state"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"),
                "CONTEXTKIT_TEMPLATES_DIR": str(contextkit)})
    (tmp_path / "home" / ".contextkit").mkdir(parents=True)
    shutil.copytree(contextkit, tmp_path / "home" / ".contextkit" / "templates")

    def templates() -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MANAGER), "templates", "--json"],
                              cwd=tmp_path, env=env, text=True, capture_output=True, timeout=120)

    # A ContextKit install is no source of templates.
    missing = templates()
    assert missing.returncode == 6
    assert json.loads(missing.stderr)["error"]["code"] == "templates_not_found"
    bundled = _install_agenttemplates(tmp_path / "registry", contextkit)
    answer = templates()
    assert answer.returncode == 0, answer.stderr
    assert json.loads(answer.stdout)["source"] == str(bundled)


def test_the_empty_agent_is_laid_named_and_bootstrapped(lab, tmp_path):
    made = Creation(lab, tmp_path)
    icon = tmp_path / "icon.png"
    icon.write_bytes(_square_png())
    for args in (("init", "--with-template", "--json"),
                 ("identity", "set", "--name=Probe", f"--icon={icon}", "--json"),
                 ("bootstrap", "--target", "claude", "--target", "codex", "--yes", "--json")):
        result = made.run(*args)
        assert result.returncode == 0, (args, result.stderr)
    folder = made.folder
    _no_contextkit(folder)
    assert made.imports() == [
        ("context", ["import", str(made.templates / "context")], str(folder))]
    assert (folder / ".env.local").is_file()
    import hashlib
    assert lab.sql(f"SELECT name, icon_sha, migrated_at IS NOT NULL "
                   f"FROM {SCHEMA}.capabilities_agents WHERE id = %s", (_bound(folder),)) \
        == [("Probe", hashlib.sha256(_square_png()).hexdigest(), True)]
    assert not list(folder.rglob("icon-*.png"))
    log = subprocess.run(["git", "log", "--format=%s"], cwd=folder, text=True,
                         capture_output=True)
    assert log.stdout.strip() == "capabilities pre-bootstrap checkpoint"
    committed = subprocess.run(["git", "ls-files"], cwd=folder, text=True,
                               capture_output=True).stdout.split()
    assert ".env.local" not in committed
    assert "capabilities session-start --host claude" in \
        (folder / ".claude" / "settings.json").read_text()
    assert (folder / ".capabilities-generated" / "context.md").is_file()


def test_init_with_template_leaves_an_existing_agents_doctrine_alone(lab, tmp_path):
    made = Creation(lab, tmp_path)
    for _ in range(2):
        result = made.run("init", "--with-template", "--json")
        assert result.returncode == 0, result.stderr
    # Only the init that created the agent wrote the starter files to its store.
    assert made.imports() == [
        ("context", ["import", str(made.templates / "context")], str(made.folder))]
    assert "imported" not in json.loads(result.stdout)


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
    # The global document takes the folder's place once the agent enables it.
    _enable_context_documents(lab, PROJECT_ID, ["SHARED"])
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


def _enable_context_documents(lab: Lab, project: str, names: list[str]) -> None:
    """The `context` capability's enablements, as its ledger's step makes the
    table: `project` enables each global document of `names`."""
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.context_enablements (project_id text "
                     f"NOT NULL, document_id uuid NOT NULL REFERENCES {SCHEMA}.context_documents "
                     "(id), enabled_at timestamptz NOT NULL DEFAULT now(), enabled_by text "
                     "NOT NULL, PRIMARY KEY (project_id, document_id))")
        conn.execute(f"INSERT INTO {SCHEMA}.context_enablements (project_id, document_id, "
                     f"enabled_by) SELECT %s, id, 'tester@lab' FROM {SCHEMA}.context_documents "
                     "WHERE project_id IS NULL AND retired_at IS NULL AND name = ANY(%s)",
                     (project, names))


def test_build_composes_a_global_document_once_the_agent_enables_it(lab, tmp_path):
    # One agent bound through ContextKit with its context in the store, one
    # bound by its id alone with its context in a folder.
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[identity]\nid = "{PROJECT_ID}"\n')
    bare = Folder(lab, tmp_path / "bare")
    (bare.path / "context" / "identity").mkdir(parents=True)
    (bare.path / "context" / "identity" / "MISSION.md").write_text(
        _context_text(*_CONTEXT_FILES["identity/MISSION"]))
    _seed_context_documents(lab, [
        (PROJECT_ID, "identity/MISSION", False, [(*_CONTEXT_FILES["identity/MISSION"], True)]),
        (None, "SHARED", False, [("Shared", "Enabled.", "inline", 5, "\n# Shared\n", True)]),
        (None, "UNUSED", False, [("Unused", "Not enabled.", "inline", 6, "\n# Unused\n", True)]),
        (None, "identity/MISSION", False,
         [("Theirs", "Shadowed.", "inline", 1, "\n# Theirs\n", True)]),
    ])

    def build(run) -> str:
        result = run("build", "--print")
        assert result.returncode == 0, result.stderr
        assert "# Mission" in result.stdout
        return result.stdout

    for run in (lab.run, bare.run):
        before = build(run)
        assert not {"# Shared", "# Unused", "# Theirs"} & set(before.splitlines())
    for agent in (PROJECT_ID, BARE_ID):
        _enable_context_documents(lab, agent, ["SHARED", "identity/MISSION"])
    for run in (lab.run, bare.run):
        enabled = build(run)
        assert "## Global Source: `context show SHARED --global`" in enabled
        assert "# Shared" in enabled.splitlines()
        assert not {"# Unused", "# Theirs"} & set(enabled.splitlines())


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


def test_an_agent_in_the_store_without_a_context_folder_names_why_the_store_gave_none(lab):
    # With no folder to fall back on, the build names what kept the store's
    # documents from it rather than a context folder the agent never had.
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[identity]\nid = "{PROJECT_ID}"\n')
    _seed_context_documents(lab, [
        (PROJECT_ID, "identity/MISSION", False, [("Stored", "In the store.", "bogus", 1,
                                                  "\n# Stored\n", True)])])
    code, error = lab.refused("build", "--print")
    assert (code, error["code"]) == (6, "context_file_invalid")
    assert "Invalid frontmatter load in context/identity/MISSION.md: bogus" in error["message"]
    (project / ".env.local").write_text(
        "AGENTKIT_DB_URL=postgresql://nobody@127.0.0.1:1/none?sslmode=disable\n"
        f"AGENTKIT_DB_SCHEMA={SCHEMA}\n")
    code, error = lab.refused("build", "--print")
    assert error["code"] == "context_store_unread" and code != 0
    assert "no context folder `context/`" in error["message"]
    assert "source directory not found" not in error["message"]

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


def test_path_names_the_store_as_the_home_of_a_layer_the_store_holds(lab):
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[identity]\nid = "{PROJECT_ID}"\n')
    stored = ("context", "assets", "routines")
    for name in stored:
        (project / name).mkdir()

    def layers() -> dict:
        result = lab.run("path", "--json")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["layers"]

    in_folders = layers()
    assert {name: in_folders[name] for name in stored} == {
        name: str((project / name).resolve()) for name in stored}

    # The store holds the agent's documents, routines and an asset: each of
    # those layers names the store and the command that lists it, and keeps
    # the folder its import and export default to.
    _seed_context_documents(lab, [(PROJECT_ID, "identity/MISSION", False,
                                   [(*_CONTEXT_FILES["identity/MISSION"], True)])])
    _seed_routines(lab, [(PROJECT_ID, "alpha", False, [("Run first.", "# Alpha\n", True)])])
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"CREATE TABLE {SCHEMA}.assets_documents (project_id text NOT NULL, "
                     "name text NOT NULL, retired_at timestamptz)")
        conn.execute(f"INSERT INTO {SCHEMA}.assets_documents (project_id, name) "
                     "VALUES (%s, 'plans/x')", (PROJECT_ID,))
    reads = {"context": "context list", "assets": "assets list",
             "routines": "routine list --store"}
    in_store = {name: {"where": "store", "read": reads[name], "folder": in_folders[name]}
                for name in stored}
    assert layers() == {**in_folders, **in_store}

    # Without the folders the answer stays the store's.
    for name in stored:
        shutil.rmtree(project / name)
    assert layers() == {**in_folders, **in_store}


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


def test_build_leaves_out_a_routine_the_agent_switched_off(lab):
    project = lab.project
    (project / ".contextkit").mkdir()
    (project / ".contextkit" / "config.toml").write_text(
        f'version = 1\ntype = "agent-project"\n\n[identity]\nid = "{PROJECT_ID}"\n')
    (project / "context" / "identity").mkdir(parents=True)
    (project / "context" / "identity" / "MISSION.md").write_text(
        _context_text(*_CONTEXT_FILES["identity/MISSION"]))
    _seed_routines(lab, [(PROJECT_ID, "alpha", False, [("Run first by name.", "# Alpha\n", True)]),
                         (PROJECT_ID, "beta", False, [("Run second.", "# Beta\n", True)])])

    def build() -> str:
        result = lab.run("build", "--print")
        assert result.returncode == 0, result.stderr
        return result.stdout

    before = build()
    # The column as the routine ledger's switching step leaves it.
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"ALTER TABLE {SCHEMA}.routine_documents ADD COLUMN disabled_at timestamptz")
    assert build() == before

    def switch(name: str, off: bool) -> None:
        with lab.cluster.connect(lab.database) as conn:
            conn.execute(f"UPDATE {SCHEMA}.routine_documents SET disabled_at = "
                         + ("now()" if off else "NULL") + " WHERE name = %s", (name,))

    switch("alpha", True)
    one_off = build()
    assert "routine show alpha" not in one_off
    assert "- **beta** (`routine show beta`) - Run second.\n" in one_off
    switch("beta", True)
    all_off = build()
    assert "generated from the store's active routine versions" in all_off
    assert "routine show alpha" not in all_off and "routine show beta" not in all_off
    switch("alpha", False)
    switch("beta", False)
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

    def ok(self, *args: str, **extra: str) -> dict:
        result = self.run(*args, **extra)
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
    # It has no context folder, so its context is the store's (as `build` reads it).
    assert (path["body_root"], path["layers"]["context"]) == (
        "agent", {"where": "store", "read": "context list",
                  "folder": str(folder.path / "agent" / "context")})
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


def test_agent_convert_fills_only_what_the_row_holds_empty_and_names_each_difference(
        lab, tmp_path):
    import hashlib
    config = BOUND_CONFIG + ('\n[identity]\nname = "File Name"\n'
                             'description = "Written in the file"\n')
    folder = _bound_agent(lab, tmp_path / "held", config)
    root = folder.path
    folder.ok("agent", "set", "--collaboration-mode", "ask")
    env_before = (root / ".env.local").read_text()

    def row():
        return lab.sql(f"SELECT name, description, collaboration_mode, body_root, "
                       f"onboarding_status, icon_sha FROM {SCHEMA}.capabilities_agents "
                       "WHERE id = %s", (BARE_ID,))[0]

    before = row()
    sha = hashlib.sha256(_png()).hexdigest()
    differs = [{"field": "collaboration_mode", "file": "direct", "store": "ask"},
               {"field": "name", "file": "File Name", "store": "Bound"}]
    fill = {"body_root": "agent", "onboarding": {"status": "pending", "routine": "welcome"},
            "description": "Written in the file", "icon_sha": sha}

    planned = folder.ok("agent", "convert", "--dry-run")
    assert (planned["dry_run"], planned["changed"]) == (True, False)
    assert planned["fill"] == fill
    assert sorted(planned["differs"], key=lambda d: d["field"]) == differs
    assert row() == before
    assert (root / ".contextkit" / "config.toml").read_text() == config
    assert not (root / ".capabilities-generated").exists()
    assert (root / ".env.local").read_text() == env_before
    assert folder.git("status", "--porcelain") == ""

    converted = folder.ok("agent", "convert")
    assert converted["carried"] == fill
    assert sorted(converted["differs"], key=lambda d: d["field"]) == differs
    assert row() == ("Bound", "Written in the file", "ask", "agent", "pending", sha)
    assert not (root / ".contextkit").exists()


def test_agent_convert_keeps_the_row_icon_when_the_named_icon_is_gone(lab, tmp_path):
    folder = _bound_agent(lab, tmp_path / "gone", BOUND_CONFIG)
    root = folder.path
    icon = tmp_path / "icon.png"
    icon.write_bytes(_png())
    folder.ok("agent", "set", "--icon", str(icon))
    for icon in (root / ".contextkit").glob("icon-*.png"):
        icon.unlink()
    config = BOUND_CONFIG + '\n[identity]\nicon = "icon-aaaaaaaaaaaa.png"\n'
    (root / ".contextkit" / "config.toml").write_text(config)
    sha = lab.sql(f"SELECT icon_sha FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                  (BARE_ID,))[0][0]

    assert "icon_sha" not in folder.ok("agent", "convert", "--dry-run")["fill"]
    converted = folder.ok("agent", "convert")

    assert "icon_sha" not in converted["carried"]
    assert lab.sql(f"SELECT icon_sha FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (BARE_ID,)) == [(sha,)]
    assert not (root / ".contextkit").exists()


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


def test_agent_convert_binds_the_old_prj_form_of_an_id_to_the_agent_the_store_holds(
        lab, tmp_path):
    # The store holds the agent under the twelve hex characters alone; its
    # folders still carry prj_ before them, in the binding or in .env.local.
    held = "00000000cc0a"
    Folder(lab, tmp_path / "held", agent_id=held).ok("agent", "set", "--name", "Held")
    config = BOUND_CONFIG + f'\n[identity]\nname = "Held"\nid = "prj_{held}"\n'
    for name, carried in (("binding", None), ("env-local", f"prj_{held}")):
        folder = Folder(lab, tmp_path / name, agent_id=carried)
        for rel, body in {"agent/context/MISSION.md": "---\ntitle: Mission\ndescription: Why.\n"
                                                      "load: inline\norder: 1\n---\n# Mission\n",
                          ".contextkit/config.toml": config}.items():
            (folder.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder.path / rel).write_text(body)

        assert folder.ok("agent", "convert", "--dry-run")["agent_id"] == held
        converted = folder.ok("agent", "convert")

        assert converted["agent_id"] == held and converted["agent"]["name"] == "Held"
        assert _bound(folder.path) == held
        assert str(folder.path / ".env.local") in converted["wrote"]
        assert lab.sql(f"SELECT id, name, body_root FROM {SCHEMA}.capabilities_agents "
                       "WHERE id LIKE %s", (f"%{held}",)) == [(held, "Held", "agent")]


def test_agent_rekey_moves_an_agent_to_a_new_id_in_the_store_and_on_its_nodes(lab, tmp_path):
    old, new = PROJECT_ID, "0123456789ab"
    here = {**node_env("lab-node")}
    # A coolify that records what it was asked, standing in for the capability.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "coolify-calls"
    (bin_dir / "coolify").write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s|" "$*" "$CAPABILITIES_PROJECT_ID" >> {calls}\n'
        f"cat >> {calls}\necho >> {calls}\necho '{{\"ok\": true}}'\n")
    (bin_dir / "coolify").chmod(0o755)
    path = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    def execute(query: str, params: tuple = ()) -> None:
        with lab.cluster.connect(lab.database) as conn:
            conn.execute(query, params)

    lab.ok("agent", "set", "--name", "Lab")
    for name, kind, extra in (("lab-node", "mac", ()), ("far-mac", "mac", ()),
                              ("old-box", "server", ()),
                              ("box", "server", ("--coolify-connection", "infra",
                                                 "--coolify-server", "srv-uuid",
                                                 "--coolify-resource", "res-uuid"))):
        added = lab.run("node", "add", nid(name), "--label", name, "--kind", kind, *extra,
                        **here)
        assert added.returncode == 0, added.stderr
    # A capability's table whose guard refuses any change, a lease table, and
    # a free-text line that only mentions the id.
    execute(f"CREATE TABLE {SCHEMA}.lab_notes (id serial PRIMARY KEY, project_id text, "
            "note text)")
    execute(f"CREATE FUNCTION {SCHEMA}.lab_notes_guard() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'lab_notes is append-only'; END $$")
    execute(f"CREATE TRIGGER lab_notes_guard BEFORE UPDATE OR DELETE ON {SCHEMA}.lab_notes "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.lab_notes_guard()")
    execute(f"INSERT INTO {SCHEMA}.lab_notes (project_id, note) VALUES (%s, 'a'), "
            "(%s, %s), ('prj_00000000beef', 'other')", (old, old, f"made by {old}"))
    execute(f"CREATE TABLE {SCHEMA}.lab_leases (project_id text, node text, "
            "expires_at timestamptz)")
    execute(f"INSERT INTO {SCHEMA}.lab_leases VALUES (%s, '{nid('old-box')}', now() + interval '1 hour')",
            (old,))

    def rekey(*args: str, cwd: Path = lab.project, **extra: str):
        return subprocess.run(
            [sys.executable, str(MANAGER), "agent", "rekey", *args], cwd=cwd,
            env=lab.env(CLAUDE_PROJECT_DIR=str(cwd), **path, **extra),
            text=True, capture_output=True, timeout=120)

    def code(result) -> tuple[int, str]:
        return result.returncode, json.loads(
            result.stderr.strip().splitlines()[-1])["error"]["code"]

    def text_columns() -> list[tuple]:
        return sorted(lab.sql(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND data_type = 'text'", (SCHEMA,)))

    # The dry run lists every row, trigger, file and node, and changes nothing.
    env_before = (lab.project / ".env.local").read_text()
    dry = rekey(new, "--dry-run", **here)
    assert dry.returncode == 0, dry.stderr
    plan = json.loads(dry.stdout)
    rows = {(r["table"], r["column"]): r for r in plan["rows"]}
    assert plan["rows"][0]["table"] == "capabilities_agents"
    assert rows[("lab_notes", "project_id")]["rows"] == 2
    assert rows[("lab_notes", "project_id")]["keys"] == [["1"], ["2"]]
    assert rows[("capabilities_agent_nodes", "agent_id")]["rows"] == 4
    assert ("lab_notes", "note") not in rows
    assert {"table": "lab_notes", "trigger": "lab_notes_guard"} in plan["triggers"]
    assert plan["services_running"] == [
        {"capability": "lab", "node": nid("old-box"), "until": plan["services_running"][0]["until"],
         "lease": "lab_leases"}]
    assert [b["node"] for b in plan["bindings"]] == [nid("box"), nid("lab-node")]
    assert [u["node"] for u in plan["unreached"]] == [nid("far-mac"), nid("old-box")]
    assert (lab.project / ".env.local").read_text() == env_before
    assert lab.sql(f"SELECT count(*) FROM {SCHEMA}.lab_notes WHERE project_id = %s",
                   (old,)) == [(2,)]
    assert not calls.exists()

    # Refused, changing nothing: an older form as the new id, an id the store
    # holds, and a service still running.
    assert code(rekey("prj_0123456789ab", **here)) == (6, "input")
    execute(f"INSERT INTO {SCHEMA}.capabilities_agents (id) VALUES ('fedcba987654')")
    assert code(rekey("fedcba987654", **here)) == (6, "id_in_use")
    assert code(rekey(new, **here)) == (6, "agent_services_running")
    execute(f"UPDATE {SCHEMA}.lab_leases SET expires_at = now() - interval '1 minute'")
    assert lab.sql(f"SELECT count(*) FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (old,)) == [(1,)]

    done = rekey(new, **here)
    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout)
    assert (answer["old_id"], answer["new_id"], answer["store"]["moved"]) == (old, new, True)
    for table, column in text_columns():
        assert lab.sql(f'SELECT count(*) FROM {SCHEMA}."{table}" WHERE "{column}" = %s',
                       (old,)) == [(0,)], (table, column)
    assert lab.sql(f"SELECT id, project_id, note FROM {SCHEMA}.lab_notes ORDER BY id") == [
        (1, new, "a"), (2, new, f"made by {old}"), (3, "prj_00000000beef", "other")]
    assert lab.sql("SELECT tgenabled FROM pg_trigger WHERE tgname = 'lab_notes_guard'") \
        == [("O",)]
    assert lab.sql(f"SELECT node FROM {SCHEMA}.capabilities_agent_nodes WHERE agent_id = %s "
                   "ORDER BY node", (new,)) == [(nid("box"),), (nid("far-mac"),), (nid("lab-node"),),
                                                 (nid("old-box"),)]
    assert (lab.project / ".env.local").read_text() == env_before.replace(
        f"CAPABILITIES_PROJECT_ID={old}", f"CAPABILITIES_PROJECT_ID={new}")
    assert calls.read_text() == (
        f"--connection infra env set res-uuid CAPABILITIES_PROJECT_ID --value-stdin|{new}|{new}\n")
    assert [u["node"] for u in answer["unreached"]] == [nid("far-mac"), nid("old-box")]
    assert lab.ok("agent", "show")["agent"]["name"] == "Lab"

    # On another Mac of the agent the store has moved already: the same
    # command rebinds that folder alone.
    far = tmp_path / "far"
    far.mkdir()
    (far / ".env.local").write_text(env_before)
    assert code(rekey(cwd=far, **node_env("far-mac"))) == (6, "agent_unknown")
    moved = rekey(new, cwd=far, **node_env("far-mac"))
    assert moved.returncode == 0, moved.stderr
    assert json.loads(moved.stdout)["store"]["already_moved"] is True
    assert f"CAPABILITIES_PROJECT_ID={new}\n" in (far / ".env.local").read_text()
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / ".env.local").write_text(env_before)
    assert code(rekey(new, cwd=stranger, **node_env("no-such-node"))) \
        == (6, "agent_unknown")



# --- a system agent --------------------------------------------------------------

def _in(lab: Lab, path: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(MANAGER), *args], cwd=path,
                          env=lab.env(CLAUDE_PROJECT_DIR=str(path), **extra), text=True,
                          capture_output=True, timeout=120)


def _error_of(result: subprocess.CompletedProcess) -> tuple[int, str]:
    line = next(line for line in reversed(result.stderr.splitlines())
                if line.lstrip().startswith("{"))
    return result.returncode, json.loads(line)["error"]["code"]


def _store_folder(lab: Lab, path: Path) -> Path:
    path.mkdir()
    (path / ".env.local").write_text(f"AGENTKIT_DB_URL={lab.cluster.url(lab.database)}\n"
                                     f"AGENTKIT_DB_SCHEMA={SCHEMA}\n")
    return path


def test_a_system_agent_is_made_or_adopted_on_its_one_node_and_no_edit_changes_it(lab, tmp_path):
    mac_a, mac_b = node_env("mac-a"), node_env("mac-b")
    assert lab.run("migrate", "--global", "--apply", **mac_a).returncode == 0
    keeper = _store_folder(lab, tmp_path / "keeper")

    def run(*args, **extra):
        return _in(lab, keeper, *args, **{**mac_a, **extra})

    def ok(*args, **extra):
        result = run(*args, **extra)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout) if result.stdout.lstrip().startswith("{") else result.stdout

    agent = ok("init")["agent"]
    agent_id = agent["id"]
    assert (agent["kind"], agent["slug"]) == ("agent", "keeper")
    lab.sql(f"INSERT INTO {SCHEMA}.capabilities_policy (agent_id, capability, node, enabled)"
            " VALUES (NULL, 'askproject', NULL, true), (%s, 'tasks', NULL, true),"
            " (%s, 'memory', NULL, true) RETURNING capability", (agent_id, agent_id))
    lab.sql(f"INSERT INTO {SCHEMA}.capabilities_connections (capability, name, spec)"
            " VALUES ('clickup', 'work', '{}') RETURNING name")
    lab.sql(f"INSERT INTO {SCHEMA}.capabilities_connection_grants (agent_id, capability,"
            " connection, enabled, is_default) VALUES (%s, 'clickup', 'work', true, true)"
            " RETURNING agent_id", (agent_id,))
    (keeper / "AGENTS.md").write_text("# The app's own doctrine\n")
    with (keeper / ".env.local").open("a") as env_local:
        env_local.write("CAPABILITIES_CONTEXT_PROMPT=An override of the prompt.\n")
    before = ok("build", "--print")
    assert "# Foreign Instruction Files" in before and "An override of the prompt." in before
    assert {"## askproject", "## memory", "## tasks"} <= set(before.splitlines())

    # The existing agent is adopted in place: its id, grants and slug kept.
    adopted = ok("init", "--system", "--name", "Keeper")["agent"]
    assert (adopted["id"], adopted["kind"], adopted["slug"]) == (agent_id, "system", "keeper")
    assert lab.sql(f"SELECT id, name, kind FROM {SCHEMA}.capabilities_agents WHERE kind = "
                   "'system'") == [(agent_id, "Keeper", "system")]
    assert lab.sql(f"SELECT agent_id, connection FROM {SCHEMA}.capabilities_connection_grants"
                   ) == [(agent_id, "work")]
    assert ok("init", "--system")["agent"]["kind"] == "system"
    assert ok("agent", "show")["agent"]["kind"] == "system"
    assert _error_of(run("init", "--system", "--name", "Other")) == (4, "system_agent")
    assert ok("init")["agent"]["kind"] == "system"

    # Its composed context offers only what is enabled for it.
    text = ok("build", "--print")
    assert "## tasks" in text.splitlines()
    assert "## askproject" not in text and "## memory" not in text
    assert "# Foreign Instruction Files" not in text and "An override" not in text

    # No edit changes it; grants, settings, enable and keys stay open.
    elsewhere = _store_folder(lab, tmp_path / "elsewhere")
    for args in (("agent", "set", "--name", "Renamed"), ("identity", "set", "--name", "R"),
                 ("relabel", "keeper-two"), ("agent", "convert"),
                 ("node", "add", nid("mac-b"), "--label", "mac-b", "--kind", "mac"),
                 ("agent", "materialize", agent_id, str(elsewhere))):
        assert _error_of(run(*args)) == (4, "system_agent"), args
    assert lab.sql(f"SELECT name, slug FROM {SCHEMA}.capabilities_agents WHERE id = %s",
                   (agent_id,)) == [("Keeper", "keeper")]
    assert ok("key", "generate")["ok"] is True
    for args in (("enable", "routine", "--project"), ("disable", "tasks", "--project"),
                 ("set", "clickup", "workspace", "w1")):
        result = run(*args)
        assert result.returncode != 4 and "system_agent" not in result.stderr, args

    # One per node; another Mac's may share its slug, an ordinary agent may not.
    second = _store_folder(lab, tmp_path / "second")
    assert _error_of(_in(lab, second, "init", "--system", "--name", "Keeper", **mac_a)) == (
        6, "system_agent_exists")
    made = _in(lab, second, "init", "--system", "--name", "Keeper", **mac_b)
    assert made.returncode == 0, made.stderr
    other = json.loads(made.stdout)["agent"]
    assert (other["kind"], other["created"], other["slug"]) == ("system", True, "keeper")
    assert lab.sql(f"SELECT node, is_master FROM {SCHEMA}.capabilities_agent_nodes "
                   "WHERE agent_id = %s", (other["id"],)) == [(nid("mac-b"), True)]
    assert ok("agent", "state", "keeper")["agent"]["id"] == agent_id
    third = _store_folder(lab, tmp_path / "third")
    ordinary = _in(lab, third, "init", **mac_b)
    assert ordinary.returncode == 0, ordinary.stderr
    assert json.loads(ordinary.stdout)["agent"]["slug"] == "third"
    assert _error_of(_in(lab, third, "relabel", "keeper", **mac_b)) == (6, "slug_taken")
    added = _in(lab, third, "node", "add", nid("mac-c"), "--label", "mac-c", "--kind", "mac",
                **mac_b)
    assert added.returncode == 0, added.stderr
    assert _error_of(_in(lab, third, "init", "--system", **mac_b)) == (6, "system_agent_nodes")
    assert lab.sql(f"SELECT kind FROM {SCHEMA}.capabilities_agents WHERE slug = 'third'") == [
        ("agent",)]
