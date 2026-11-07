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
    agent_id text NOT NULL, environment text NOT NULL, connection_id text NOT NULL,
    channel_key text NOT NULL, last_processed_message_id bigint NOT NULL DEFAULT 0,
    settings jsonb NOT NULL DEFAULT '{{}}'::jsonb, state jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    {extra}
    PRIMARY KEY (agent_id, environment, connection_id, channel_key))"""
# The connections the register rows name, by their ids.
MAIN, OTHER = "a1b2c3d4e5f6", "b2c3d4e5f6a1"


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
        f"SELECT agent_id, connection_id, channel_key, last_processed_message_id, settings "
        f"FROM {schema}.telegram_channels ORDER BY 1, 2, 3").fetchall()


def _put(conn, schema: str, project: str, connection: str, channel: str, mark: int,
         settings: str = "{}") -> None:
    conn.execute(
        f"INSERT INTO {schema}.telegram_channels (agent_id, environment, connection_id, "
        "channel_key, last_processed_message_id, settings) VALUES (%s, 'production', %s, "
        "%s, %s, %s::jsonb)", (project, connection, channel, mark, settings))


def test_the_takeover_starts_from_the_canonical_register_and_returns_it(manager, store):
    _put(store, CANONICAL, "prj_a", MAIN, "-200", 301, '{"tail_size": 5}')
    _put(store, CANONICAL, "prj_a", OTHER, "-200", 900)
    _put(store, CANONICAL, "prj_b", MAIN, "-200", 700)
    # What an earlier takeover in this session left behind is replaced.
    _put(store, LANE, "prj_a", MAIN, "-200", 100)
    _put(store, LANE, "prj_a", MAIN, "-999", 50)

    assert manager._dev_live_copy_register(CANONICAL, LANE, "prj_a", MAIN) == 1
    assert _rows(store, LANE) == [("prj_a", MAIN, "-200", 301, {"tail_size": 5})]

    # The payload answers 302 and 303 and opens a channel; then it is restored.
    store.execute(f"UPDATE {LANE}.telegram_channels SET last_processed_message_id = 303, "
                  "newer = 'kept by the payload' WHERE channel_key = '-200'")
    _put(store, LANE, "prj_a", MAIN, "-300", 12)
    assert manager._dev_live_copy_register(LANE, CANONICAL, "prj_a", MAIN) == 2
    assert _rows(store, CANONICAL) == [
        ("prj_a", MAIN, "-200", 303, {"tail_size": 5}),
        ("prj_a", MAIN, "-300", 12, {}),
        ("prj_a", OTHER, "-200", 900, {}),
        ("prj_b", MAIN, "-200", 700, {}),
    ]


def test_nothing_is_carried_where_a_schema_holds_no_register(manager, store):
    store.execute(f"DROP TABLE {CANONICAL}.telegram_channels")
    _put(store, LANE, "prj_a", MAIN, "-200", 100)

    assert manager._dev_live_copy_register(CANONICAL, LANE, "prj_a", MAIN) is None
    assert manager._dev_live_copy_register(LANE, CANONICAL, "prj_a", MAIN) is None
    assert _rows(store, LANE) == [("prj_a", MAIN, "-200", 100, {})]


def test_the_takeover_selects_a_connection_by_its_id_or_its_label(manager):
    # A store connection is keyed by its id and called by its label; the
    # project's `connection` setting, a --connection flag and the default
    # pointer name it by either, and the takeover keeps the id.
    registry = {"default": "personal",
                "connections": {MAIN: {"allow_write": True}, OTHER: {"allow_write": False}},
                "labels": {MAIN: "work", OTHER: "personal"}}
    assert manager._dev_select_connection(registry, "work")[0] == MAIN
    assert manager._dev_select_connection(registry, MAIN)[0] == MAIN
    assert manager._dev_select_connection(registry, None)[0] == OTHER
    with pytest.raises(SystemExit):
        manager._dev_select_connection(registry, "elsewhere")
    # A connections file a payload reads holds no labels: there a connection
    # is called by its key.
    assert manager._dev_registry_file(registry) == {
        "default": "personal", "connections": registry["connections"]}


def test_a_session_kept_under_the_former_name_moves_to_the_id_once(manager, tmp_path, capfd):
    base = tmp_path / "telegram"
    (base / "work").mkdir(parents=True)
    (base / "work" / "session").write_text("signed in")
    found = manager._connection_state_dir(base, MAIN, "work")
    assert found == base / MAIN
    assert (found / "session").read_text() == "signed in"
    assert not (base / "work").exists()
    assert "moved" in capfd.readouterr().err
    # Moved once: from then on the id's folder is the one, with nothing said.
    assert manager._connection_state_dir(base, MAIN, "work") == base / MAIN
    assert capfd.readouterr().err == ""
    # Where both are there the id's is used, and that is said too.
    (base / "work").mkdir()
    assert manager._connection_state_dir(base, MAIN, "work") == base / MAIN
    assert (base / "work").is_dir() and str(base / MAIN) in capfd.readouterr().err
    # No former name: the id's folder, made by whoever signs in first.
    assert manager._connection_state_dir(base, OTHER, None) == base / OTHER


def test_a_dev_run_projects_a_nodes_coolify_connection_with_the_connections(
        manager, cluster, monkeypatch):
    # A node names the coolify connection it was created through by the
    # connection's id, a reference to its row: the session schema takes the
    # node and then the connection, and the node keeps naming it there.
    import dataclasses
    from capabilities_contract import db
    from capabilities_contract.capability import store as S
    database = cluster.database()
    for key in DB_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENTKIT_DB_URL", cluster.url(database))
    setting = dataclasses.replace(db.resolve_setting(None), schema=CANONICAL)
    conn = db.connect(application_name="test", setting=setting, transaction=True)
    try:
        major, minor = S.STORE_TABLES_VERSION
        db.migrate(conn, S.STORE_OWNER, S.record_table_steps(), major=major, minor=minor)
    finally:
        conn.close()
    raw = cluster.connect(database)
    raw.execute(f"SET search_path TO {CANONICAL}")
    raw.execute("INSERT INTO agents (id) VALUES ('prj_a')")
    [(infra,)] = raw.execute("INSERT INTO connections (capability, label, spec) VALUES "
                             "('coolify', 'infra', '{}') RETURNING id").fetchall()
    raw.execute("INSERT INTO grants (agent_id, capability, connection_id, enabled) "
                "VALUES ('prj_a', 'coolify', %s, true)", (infra,))
    raw.execute("INSERT INTO nodes (id, label, kind, coolify_connection_id, coolify_server, "
                "coolify_resource) VALUES ('a1a2a3a4a5a6', 'box', 'server', %s, 'srv', 'res')",
                (infra,))
    raw.execute("INSERT INTO agent_nodes (agent_id, node) VALUES ('prj_a', 'a1a2a3a4a5a6')")

    counts = manager._dev_project_rows(setting, LANE, "prj_a", "coolify")
    assert (counts["nodes"], counts["connections"], counts["grants"]) == (1, 1, 1)
    raw.execute(f"SET search_path TO {LANE}")
    assert raw.execute("SELECT id, coolify_connection_id FROM nodes").fetchall() == [
        ("a1a2a3a4a5a6", infra)]
    assert raw.execute("SELECT node, coolify_connection_id FROM agent_nodes").fetchall() == [
        ("a1a2a3a4a5a6", infra)]
    assert raw.execute("SELECT connection_id FROM grants").fetchall() == [(infra,)]
    # Projected again, nothing is left over from the first and nothing breaks.
    assert manager._dev_project_rows(setting, LANE, "prj_a", "coolify")["nodes"] == 1
    assert raw.execute("SELECT coolify_connection_id FROM nodes").fetchall() == [(infra,)]
    raw.close()
