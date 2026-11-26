"""A machine service joined by enable serves every agent living on a node that
has the capability enabled - its own row or the global one - and places it on
no node itself, and `service.machine.join` is validated with the rest of its
declaration.

The store tests run the manager's own query against tables the real manager
migrates in a throwaway PostgreSQL on a loopback port, which the module starts
when `initdb` and `pg_ctl` are on PATH and psycopg is importable, and skips
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
from pathlib import Path

import runpy

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
ADMIN = "pgadmin"
SCHEMA = "kernel"
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")
PROJECT_ID = "prj_" + "0a1b2c3d4e5f"
# The markers an agent harness or a conveyor turn sets in its commands'
# environment; the lab strips them, so a run inside one tests the same.
MARKERS = ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CODEX_THREAD_ID", "TASKS_EXECUTION")


def nid(label: str) -> str:
    return label.encode()[:6].ljust(6, b"\0").hex()


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
    root = Path(tempfile.mkdtemp(prefix="capabilities-join-pg-"))
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
        (self.project / ".env.local").write_text(self.store_lines())
        # A folder bound to no agent, with the same store: a machine where the
        # agents archived here have no folder.
        self.elsewhere = tmp_path / "elsewhere"
        self.elsewhere.mkdir()
        (self.elsewhere / ".env.local").write_text(self.store_lines())

    def store_lines(self) -> str:
        return (f"AGENTKIT_DB_URL={self.cluster.url(self.database)}\n"
                f"AGENTKIT_DB_SCHEMA={SCHEMA}\n")

    def env(self, cwd: Path, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in DB_KEYS and not k.startswith("CAPABILITIES_")
               and k not in ("AGENT_NODE_ID", "AGENT_NODE_NAME") and k not in MARKERS}
        env.update({
            "HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "CAPABILITIES_HOME": str(self.tmp / "registry"),
            "CLAUDE_PROJECT_DIR": str(cwd),
            "AGENT_NODE_ID": nid("mac-me"), "AGENT_NODE_NAME": "mac-me",
            "USER": "tester",
            **extra,
        })
        return env

    def run(self, *args: str, cwd: Path | None = None, **extra: str):
        cwd = cwd or self.elsewhere
        return subprocess.run([sys.executable, str(MANAGER), *args], cwd=cwd,
                              env=self.env(cwd, **extra), text=True, capture_output=True,
                              timeout=120)

    def ok(self, *args: str, **kw) -> dict:
        result = self.run(*args, **kw)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def refused(self, *args: str, **kw) -> tuple[int, dict]:
        result = self.run(*args, **kw)
        line = next(line for line in reversed(result.stderr.splitlines())
                    if line.lstrip().startswith("{"))
        return result.returncode, json.loads(line)["error"]

    def sql(self, query: str, params: tuple = ()):
        with self.cluster.connect(self.database) as conn:
            conn.execute(f"SET search_path TO {SCHEMA}")
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else None


@pytest.fixture()
def lab(tmp_path, cluster):
    return Lab(tmp_path, cluster)


M = runpy.run_path(str(MANAGER), run_name="manager_machine_join_test")
MAC, BOX = nid("mac-me"), nid("box-ow")
AGENTS = {"inherits": "a1a1a1a1a1a1", "disabled": "b2b2b2b2b2b2", "off_here": "c3c3c3c3c3c3",
          "own_box": "d4d4d4d4d4d4", "archived": "e5e5e5e5e5e5", "no_folder": "f6f6f6f6f6f6",
          "shared_here": "a7a7a7a7a7a7"}


def _seed(lab: Lab) -> None:
    lab.ok("agent", "show", cwd=lab.project)  # the core, migrated
    lab.sql("INSERT INTO nodes (id, label, kind) VALUES (%s, 'mac-me', 'mac'), "
            "(%s, 'box-own', 'server') ON CONFLICT DO NOTHING", (MAC, BOX))
    for slug, agent_id in AGENTS.items():
        lab.sql("INSERT INTO agents (id, slug, name) VALUES (%s, %s, %s)",
                (agent_id, slug.replace("_", "-"), slug))
        lab.sql("INSERT INTO agent_nodes (agent_id, node, is_master, folder) "
                "VALUES (%s, %s, true, %s)",
                (agent_id, MAC, None if slug == "no_folder" else f"/agents/{slug}"))
    lab.sql("UPDATE agents SET archived_at = now() WHERE id = %s", (AGENTS["archived"],))
    # memory enabled for every agent at the global scope, as the core tier arrives.
    lab.sql("DELETE FROM policies WHERE capability = 'memory'")
    lab.sql("INSERT INTO policies (agent_id, capability, node, enabled) "
            "VALUES (NULL, 'memory', NULL, true)")
    lab.sql("INSERT INTO policies (agent_id, capability, node, enabled) VALUES "
            "(%s, 'memory', NULL, false), (%s, 'memory', %s, false)",
            (AGENTS["disabled"], AGENTS["off_here"], MAC))
    # One agent placed on a server node, served by its own service there, one
    # placed here in mode shared: a placement of either mode says where it is served.
    lab.sql("INSERT INTO policies (agent_id, capability, node, enabled, service, mode) "
            "VALUES (%s, 'memory', %s, true, true, 'own'), "
            "(%s, 'memory', %s, true, true, 'shared')",
            (AGENTS["own_box"], BOX, AGENTS["shared_here"], MAC))
    # A capability enabled for one agent alone, with no global row.
    lab.sql("INSERT INTO policies (agent_id, capability, node, enabled) "
            "VALUES (%s, 'lifeline', NULL, true)", (AGENTS["disabled"],))


def _joined(lab: Lab, node: str, capability: str) -> list[tuple]:
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"SET search_path TO {SCHEMA}")
        return [tuple(row) for row in conn.execute(
            M["JOINED_BY_ENABLE_SQL"], {"node": node, "capability": capability}).fetchall()]


def test_an_agent_with_the_capability_enabled_and_placed_nowhere_is_joined(lab):
    _seed(lab)
    joined = _joined(lab, MAC, "memory")
    # Inherited from the global row counts; its own row for every node or for
    # this node switching it off does not; a placement it wrote (its own on a
    # server node, or shared here, which the placement read already serves)
    # says where it is served instead; an archived agent and one with no folder
    # here are left out.
    assert joined == [(AGENTS["inherits"], "/agents/inherits", "inherits")]
    # Its own row enables it with no global row; absent at both scopes denies.
    assert _joined(lab, MAC, "lifeline") == [(AGENTS["disabled"], "/agents/disabled",
                                              "disabled")]
    assert _joined(lab, MAC, "absent") == []
    # Nothing lives on the server node.
    assert _joined(lab, BOX, "memory") == []


def test_only_a_service_declaring_join_enabled_is_joined_by_enable(lab, monkeypatch):
    _seed(lab)
    joined_by_enable = M["_joined_by_enable"]
    scope = joined_by_enable.__globals__
    with lab.cluster.connect(lab.database) as conn:
        conn.execute(f"SET search_path TO {SCHEMA}")
        monkeypatch.setitem(scope, "_service_machine_join", lambda name: "placement")
        assert joined_by_enable(conn, MAC, "memory") == []
        monkeypatch.setitem(scope, "_service_machine_join", lambda name: "enabled")
        assert joined_by_enable(conn, MAC, "memory") == [
            (AGENTS["inherits"], "/agents/inherits", "inherits")]


def _machine(**extra) -> dict:
    return {"schema": "capabilities.service.machine.v1",
            "command": ["memory", "service", "run", "--machine"],
            "doctor": ["memory", "service", "doctor", "--machine"],
            "projects": "$XDG_CONFIG_HOME/memory/service/projects.json", **extra}


def test_service_machine_join_takes_placement_or_enabled_and_nothing_else():
    validate = M["_validate_service_machine"]
    verbs = ["run", "status", "doctor"]
    assert validate("memory", _machine(), verbs) == []
    assert validate("memory", _machine(join="enabled"), verbs) == []
    assert validate("memory", _machine(join="placement"), verbs) == []
    assert validate("memory", _machine(join="always"), verbs) == [
        "service.machine.join must be one of placement, enabled"]


def test_own_is_refused_for_a_service_with_a_machine_command_and_no_project_command(
        monkeypatch, capsys):
    check = M["_placement_mode_check"]
    scope = check.__globals__
    monkeypatch.setitem(scope, "_service_machine_argv",
                        lambda name: ["memory", "service", "run", "--machine"])
    monkeypatch.setitem(scope, "_node_service_command",
                        lambda name: ([], "memory's service declares no deploy command"))
    with pytest.raises(SystemExit) as refused:
        check("memory", "own")
    assert refused.value.code == 6
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "no_project_service"
    check("memory", "shared")
    check("memory", None)
    # A service that declares a project command too keeps its own mode.
    monkeypatch.setitem(scope, "_node_service_command",
                        lambda name: (["tasks", "service", "run"], None))
    check("tasks", "own")
