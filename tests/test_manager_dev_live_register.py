"""A `dev live` takeover borrows the Telegram register the store holds.

Where the project names no database of its own, the session payload keeps its
records in the session's schema of the machine's database while the canonical
service keeps them in the machine's own schema, so the register - one
`telegram_channels` row per channel - is carried between the two around the
takeover. These tests drive the manager's carry against a throwaway PostgreSQL
on a loopback port that the module starts when `initdb` and `pg_ctl` are on
PATH and psycopg is importable, and skips otherwise.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
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
ADMIN = "pgadmin"
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")
CANONICAL = "agentkit"
LANE = "dev_telegram_test"
TABLE = """CREATE TABLE {schema}.telegram_channels (
    project_id text NOT NULL, environment text NOT NULL, connection text NOT NULL,
    channel_key text NOT NULL, last_processed_message_id bigint NOT NULL DEFAULT 0,
    settings jsonb NOT NULL DEFAULT '{{}}'::jsonb, state jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    {extra}
    PRIMARY KEY (project_id, environment, connection, channel_key))"""


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
        # No named locale, which a machine may not have installed; UTF8 all the same.
        self.run("initdb", "-D", str(self.data), "-U", ADMIN, "-E", "UTF8",
                 "--no-locale", "--auth=trust")
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
    root = Path(tempfile.mkdtemp(prefix="capabilities-devlive-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def manager():
    loader = importlib.machinery.SourceFileLoader("capabilities_manager_devlive", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def store(cluster, monkeypatch):
    """A database whose canonical schema and session schema each hold a
    register; the machine resolves it from the environment, as a project that
    names no database of its own does."""
    database = cluster.database()
    for key in DB_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENTKIT_DB_URL", cluster.url(database))
    conn = cluster.connect(database)
    conn.execute(f"CREATE SCHEMA {CANONICAL}")
    conn.execute(f"CREATE SCHEMA {LANE}")
    conn.execute(TABLE.format(schema=CANONICAL, extra=""))
    # The session payload's code is newer and keeps a column the canonical's lacks.
    conn.execute(TABLE.format(schema=LANE, extra="newer text,"))
    yield conn
    conn.close()


def _rows(conn, schema: str) -> list[tuple]:
    return conn.execute(
        f"SELECT project_id, connection, channel_key, last_processed_message_id, settings "
        f"FROM {schema}.telegram_channels ORDER BY 1, 2, 3").fetchall()


def _put(conn, schema: str, project: str, connection: str, channel: str, mark: int,
         settings: str = "{}") -> None:
    conn.execute(
        f"INSERT INTO {schema}.telegram_channels (project_id, environment, connection, "
        "channel_key, last_processed_message_id, settings) VALUES (%s, 'production', %s, "
        "%s, %s, %s::jsonb)", (project, connection, channel, mark, settings))


def test_the_takeover_starts_from_the_canonical_register_and_returns_it(manager, store):
    _put(store, CANONICAL, "prj_a", "main", "-200", 301, '{"tail_size": 5}')
    _put(store, CANONICAL, "prj_a", "other", "-200", 900)
    _put(store, CANONICAL, "prj_b", "main", "-200", 700)
    # What an earlier takeover in this session left behind is replaced.
    _put(store, LANE, "prj_a", "main", "-200", 100)
    _put(store, LANE, "prj_a", "main", "-999", 50)

    assert manager._dev_live_copy_register(CANONICAL, LANE, "prj_a", "main") == 1
    assert _rows(store, LANE) == [("prj_a", "main", "-200", 301, {"tail_size": 5})]

    # The payload answers 302 and 303 and opens a channel; then it is restored.
    store.execute(f"UPDATE {LANE}.telegram_channels SET last_processed_message_id = 303, "
                  "newer = 'kept by the payload' WHERE channel_key = '-200'")
    _put(store, LANE, "prj_a", "main", "-300", 12)
    assert manager._dev_live_copy_register(LANE, CANONICAL, "prj_a", "main") == 2
    assert _rows(store, CANONICAL) == [
        ("prj_a", "main", "-200", 303, {"tail_size": 5}),
        ("prj_a", "main", "-300", 12, {}),
        ("prj_a", "other", "-200", 900, {}),
        ("prj_b", "main", "-200", 700, {}),
    ]


def test_nothing_is_carried_where_a_schema_holds_no_register(manager, store):
    store.execute(f"DROP TABLE {CANONICAL}.telegram_channels")
    _put(store, LANE, "prj_a", "main", "-200", 100)

    assert manager._dev_live_copy_register(CANONICAL, LANE, "prj_a", "main") is None
    assert manager._dev_live_copy_register(LANE, CANONICAL, "prj_a", "main") is None
    assert _rows(store, LANE) == [("prj_a", "main", "-200", 100, {})]
