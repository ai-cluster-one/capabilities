"""Run the capability's executable as a child against a throwaway store, for
tests.

The store-backed checks read ASSETS_TEST_DSN, a libpq URL for a throwaway
database, and each case binds a schema of its own: the CLI reads it from the
environment level of the store setting (AGENTKIT_DB_URL and
AGENTKIT_DB_SCHEMA). Every child runs with a home of its own under the case's
temporary directory, and every key that could name another store or another
project is cleared, so nothing reaches the real home or the real store.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

CAPABILITY_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = next((path for path in (CAPABILITY_DIR / "bin" / "assets", CAPABILITY_DIR / "assets")
                 if path.is_file()), CAPABILITY_DIR / "bin" / "assets")
DSN = os.environ.get("ASSETS_TEST_DSN")

needs_store = pytest.mark.skipif(not DSN, reason="ASSETS_TEST_DSN names no throwaway database")

# What would name another store, or another project, to a child.
CLEARED = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT", "AGENTKIT_DB_TRANSACTION_HOST",
           "AGENTKIT_DB_TRANSACTION_PORT", "CLAUDE_PROJECT_DIR",
           "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
           "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_READ_ONLY",
           "CAPABILITIES_AUTH_CONTEXT", "CLAUDECODE")


def dsn_url() -> str:
    base = DSN or "postgresql://nobody@127.0.0.1:5432/none"
    if "sslmode=" not in base:
        base += ("&" if "?" in base else "?") + "sslmode=disable"
    return base


def new_schema() -> str:
    return "assetstest_" + uuid.uuid4().hex[:10]


def drop_schema(schema: str) -> None:
    import psycopg
    with psycopg.connect(dsn_url(), autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def connect(schema: str):
    """A plain connection to the throwaway database, bound to `schema`."""
    import psycopg
    conn = psycopg.connect(dsn_url(), autocommit=True)
    conn.execute(f'SET search_path TO "{schema}"')
    return conn


class World:
    """One case's machine: a home, a store schema, and a project in it."""

    def __init__(self, tmp: Path, schema: str):
        self.tmp = tmp
        self.schema = schema
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.agents: list[str] = []
        self.registered: set[str] = set()

    def register(self) -> None:
        """Register the projects' agents, as the manager would, before a child runs: a
        row may only name an agent the store holds. Only the core tables are made for
        it, by the library the executable runs on."""
        pending = [agent for agent in self.agents if agent not in self.registered]
        if not pending or not DSN:
            return
        done = subprocess.run([cli_python(), "-I", "-c", REGISTER, dsn_url(), self.schema,
                               *pending], capture_output=True, text=True, timeout=180)
        assert done.returncode == 0, done.stdout + done.stderr
        self.registered.update(pending)

    def env(self, **extra) -> dict:
        self.register()
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        env.update({"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
                    "XDG_STATE_HOME": str(self.home / "state"),
                    "XDG_CACHE_HOME": str(self.home / "cache"),
                    "XDG_DATA_HOME": str(self.home / "data"),
                    "CAPABILITIES_HOME": str(self.home / "registry"),
                    "AGENTKIT_DB_URL": dsn_url(), "AGENTKIT_DB_SCHEMA": self.schema})
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def project(self, name: str = "agent", pid: str | None = None) -> Path:
        root = self.tmp / name
        envelope = root / "capabilities"
        envelope.mkdir(parents=True, exist_ok=True)
        (root / ".git").mkdir(exist_ok=True)
        pid = pid or "prj_" + uuid.uuid4().hex[:12]
        self.agents.append(pid)
        (envelope / "project.json").write_text(json.dumps({
            "id": pid, "schema": "capabilities.project.v1",
            "slug": name}))
        (envelope / "settings.json").write_text(json.dumps(
            {"capabilities": {"assets": {"enabled": True}}}))
        (envelope / ".gitignore").write_text("*/state/\n")
        return root

    def run(self, cwd: Path, *args: str, stdin: bytes | None = None, timeout: float = 180,
            **extra) -> subprocess.CompletedProcess:
        return subprocess.run([str(CLI_PATH), *args], cwd=cwd, env=self.env(**extra),
                              input=stdin, capture_output=True, timeout=timeout)

    def ok(self, cwd: Path, *args: str, **kw) -> dict:
        done = self.run(cwd, *args, **kw)
        assert done.returncode == 0, done.stdout.decode() + done.stderr.decode()
        return json.loads(done.stdout)


# Make the core tables in a schema and register agents in it, run by the interpreter
# the executable runs on: argv is the URL, the schema and the agents' ids.
REGISTER = """
import sys
import psycopg
from capabilities_contract import db
from capabilities_contract.db import _migrate as core
url, schema, agents = sys.argv[1], sys.argv[2], sys.argv[3:]
with psycopg.connect(url, autocommit=True) as conn:
    db.migrate(conn, core.CORE_OWNER, core.CORE_STEPS, major=core.CORE_VERSION[0],
               minor=core.CORE_VERSION[1], schema=schema)
    for agent in agents:
        conn.execute(f'INSERT INTO "{schema}".agents (id, kind) VALUES (%s, %s) '
                     "ON CONFLICT (id) DO NOTHING", (agent, "agent"))
"""


@functools.lru_cache(maxsize=1)
def cli_python() -> str:
    """The interpreter of the environment uv runs the executable in."""
    for argv in (["uv", "sync", "--script", str(CLI_PATH)],
                 ["uv", "python", "find", "--script", str(CLI_PATH)]):
        done = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip()


def error(done: subprocess.CompletedProcess) -> dict:
    for line in reversed(done.stderr.decode().strip().splitlines()):
        try:
            return json.loads(line)["error"]
        except (ValueError, KeyError, TypeError):
            continue
    raise AssertionError(f"no error envelope: {done.stdout!r}{done.stderr!r}")


@pytest.fixture
def world(tmp_path: Path):
    schema = new_schema()
    yield World(tmp_path, schema)
    drop_schema(schema)
