"""Load the capability's executable and its engine as modules, and run it as a
child, for tests.

The store-backed checks read MEMORY_TEST_DSN, a libpq URL for a throwaway
database, and each case binds a schema of its own: a CLI run as a child reads
it from the environment level of the store setting (AGENTKIT_DB_URL and
AGENTKIT_DB_SCHEMA), and the engine in this process is handed the setting
directly. Every child runs with a home of its own under the case's temporary
directory, so nothing reaches the real home.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

CAPABILITY_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = next((path for path in (CAPABILITY_DIR / "bin" / "memory", CAPABILITY_DIR / "memory")
                 if path.is_file()), CAPABILITY_DIR / "bin" / "memory")
ENGINE_PATH = CAPABILITY_DIR / "engine.py"
FAKES = Path(__file__).resolve().parent / "fakes"
DSN = os.environ.get("MEMORY_TEST_DSN")

needs_store = pytest.mark.skipif(not DSN, reason="MEMORY_TEST_DSN names no throwaway database")

# What would name another store, or another project, to a child.
CLEARED = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT", "CLAUDE_PROJECT_DIR",
           "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
           "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_READ_ONLY",
           "CAPABILITIES_AUTH_CONTEXT", "MEMORY_ACTOR", "CLAUDECODE")


def engine():
    """The bundled engine as a module."""
    spec = importlib.util.spec_from_file_location("memory_engine_under_test", ENGINE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["memory_engine_under_test"] = module
    spec.loader.exec_module(module)
    return module


def load():
    """The executable as a module."""
    spec = importlib.util.spec_from_loader("memory_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI_PATH)
    source = CLI_PATH.read_text().replace('if __name__ == "__main__":\n    main()\n', "")
    exec(compile(source, str(CLI_PATH), "exec"), module.__dict__)
    return module


def dsn_url() -> str:
    base = DSN or "postgresql://nobody@127.0.0.1:5432/none"
    if "sslmode=" not in base:
        base += ("&" if "?" in base else "?") + "sslmode=disable"
    return base


def setting(schema: str):
    """The throwaway database as the store in force, bound to `schema`."""
    from capabilities_contract import db
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn_url())
    found = {"host": info.get("host") or "127.0.0.1", "port": int(info.get("port") or 5432),
             "database": info.get("dbname"), "user": info.get("user"),
             "sslmode": info.get("sslmode") or "disable"}
    if info.get("password"):
        found["password"] = info["password"]
    return db.Setting(schema=schema, level="environment", sources=("MEMORY_TEST_DSN",), **found)


def new_schema() -> str:
    return "memtest_" + uuid.uuid4().hex[:10]


def drop_schema(schema: str) -> None:
    import psycopg
    with psycopg.connect(dsn_url(), autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def register(schema: str, agents=(), nodes=()) -> None:
    """Register `agents` and `nodes` in `schema`, as the manager would, making only the
    core tables when they are missing: a row may only name an agent and a node the
    store holds."""
    import psycopg
    from capabilities_contract import db
    from capabilities_contract.db import _migrate as core
    with psycopg.connect(dsn_url(), autocommit=True) as conn:
        db.migrate(conn, core.CORE_OWNER, core.CORE_STEPS, major=core.CORE_VERSION[0],
                   minor=core.CORE_VERSION[1], schema=schema)
        for agent in agents:
            conn.execute(f'INSERT INTO "{schema}".agents (id, kind) VALUES '
                         "(%s, 'agent') ON CONFLICT (id) DO NOTHING", (agent,))
        for node in nodes:
            conn.execute(f'INSERT INTO "{schema}".nodes (id, label, kind) VALUES '
                         "(%s, %s, 'mac') ON CONFLICT (id) DO NOTHING", (node, node))


# `register` for a child: the core tables, the agents and the node the child runs as
# (minted in its config home as the child would), made by the interpreter the
# executable runs on, whose library may not be this process's. argv is the URL, the
# schema and the agents' ids; the environment is the child's.
REGISTER = """
import sys
import psycopg
from capabilities_contract import db
from capabilities_contract.db import _migrate as core
from capabilities_contract.capability.tiers.store import this_node
url, schema, agents = sys.argv[1], sys.argv[2], sys.argv[3:]
with psycopg.connect(url, autocommit=True) as conn:
    db.migrate(conn, core.CORE_OWNER, core.CORE_STEPS, major=core.CORE_VERSION[0],
               minor=core.CORE_VERSION[1], schema=schema)
    for agent in agents:
        conn.execute(f'INSERT INTO "{schema}".agents (id, kind) VALUES (%s, %s) '
                     "ON CONFLICT (id) DO NOTHING", (agent, "agent"))
    node = this_node()
    conn.execute(f'INSERT INTO "{schema}".nodes (id, label, kind) VALUES (%s, %s, %s) '
                 "ON CONFLICT (id) DO NOTHING", (node["id"], node["label"], "mac"))
"""


@functools.lru_cache(maxsize=1)
def cli_python() -> str:
    """The interpreter of the environment uv runs the executable in."""
    for argv in (["uv", "sync", "--script", str(CLI_PATH)],
                 ["uv", "python", "find", "--script", str(CLI_PATH)]):
        done = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip()


def manager_bin() -> str | None:
    repo_manager = CAPABILITY_DIR.parents[1] / "bin" / "capabilities"
    return str(repo_manager) if repo_manager.is_file() else None


class World:
    """One case's machine: a home, a store schema, and projects in it."""

    def __init__(self, tmp: Path, schema: str | None):
        self.tmp = tmp
        self.schema = schema
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.config = tmp / "config"
        self.state = tmp / "state"
        self.agents: list[str] = []
        self.registered: set[str] = set()

    def env(self, **extra) -> dict:
        env = self._env(**extra)
        # The agents of the projects made so far and the node a child runs as here, each
        # registered once before a child names it.
        node = (env.get("XDG_CONFIG_HOME"), env.get("AGENT_NODE_ID"))
        pending = [agent for agent in self.agents if agent not in self.registered]
        if (pending or node not in self.registered) and self.schema and DSN:
            done = subprocess.run([cli_python(), "-I", "-c", REGISTER, dsn_url(), self.schema,
                                   *pending], env=env, capture_output=True, text=True,
                                  timeout=180)
            assert done.returncode == 0, done.stdout + done.stderr
            self.registered.update([*pending, node])
        return env

    def _env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        env.update({"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config),
                    "XDG_STATE_HOME": str(self.state),
                    "XDG_CACHE_HOME": str(self.tmp / "cache"),
                    "XDG_DATA_HOME": str(self.tmp / "data"),
                    "CAPABILITIES_HOME": str(self.tmp / "registry"),
                    "PATH": f"{FAKES}{os.pathsep}{os.environ.get('PATH', '')}"})
        manager = manager_bin()
        if manager:
            env["CAPABILITIES_MANAGER_BIN"] = manager
        if self.schema:
            env["AGENTKIT_DB_URL"] = dsn_url()
            env["AGENTKIT_DB_SCHEMA"] = self.schema
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def enable_globally(self) -> None:
        gate = self.config / "capabilities" / "settings.json"
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text(json.dumps({"capabilities": {"memory": {"enabled": True}}}))

    def project(self, name: str, *, enabled: bool | None = True, pid: str | None = None) -> Path:
        root = self.tmp / name
        envelope = root / "capabilities"
        envelope.mkdir(parents=True, exist_ok=True)
        (root / ".git").mkdir(exist_ok=True)
        (envelope / "project.json").write_text(json.dumps({
            "schema": "capabilities.project.v2", "slug": name}))
        pid = pid or "prj_" + uuid.uuid4().hex[:12]
        self.agents.append(pid)
        (root / ".env.local").write_text(f"CAPABILITIES_PROJECT_ID={pid}\n")
        gate = {"capabilities": {}}
        if enabled is not None:
            gate["capabilities"]["memory"] = {"enabled": enabled}
        (envelope / "settings.json").write_text(json.dumps(gate))
        (envelope / ".gitignore").write_text("*/state/\n")
        return root

    def run(self, cwd: Path, *args: str, stdin: str | None = None, timeout: float = 120,
            **extra) -> subprocess.CompletedProcess:
        return subprocess.run([str(CLI_PATH), *args], cwd=cwd, env=self.env(**extra),
                              input=stdin, text=True, capture_output=True, timeout=timeout)

    def ok(self, cwd: Path, *args: str, **kw) -> dict:
        done = self.run(cwd, *args, **kw)
        assert done.returncode == 0, done.stdout + done.stderr
        return json.loads(done.stdout)


def error(done: subprocess.CompletedProcess) -> dict:
    for line in reversed(done.stderr.strip().splitlines()):
        try:
            return json.loads(line)["error"]
        except (ValueError, KeyError, TypeError):
            continue
    raise AssertionError(f"no error envelope: {done.stdout}{done.stderr}")


def project_id(root: Path) -> str:
    """The id the project's .env.local binds it to."""
    for line in (root / ".env.local").read_text().splitlines():
        if line.startswith("CAPABILITIES_PROJECT_ID="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{root} is bound to no id")
