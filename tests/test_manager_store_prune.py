#!/usr/bin/env python3
"""`capabilities store prune` lists what the store's migrations left behind and
drops only what nothing can still read, after a proven dump.

A leftover is a pre-image table (`<owner>_premig_*`) or a refusing stub
(`db.moved_stub`). The plan changes nothing. A leftover is kept while an owner
on the store is not current, while no release here answers for a pre-image's
owner, while the store library names a pre-image as one it reads, and, for a
stub, while a node cannot read the store's major. `--apply` takes and proves a
dump first and drops in one transaction, so a refused drop drops nothing.

The store is a database of a throwaway PostgreSQL on a loopback port, started
when `initdb`, `pg_ctl`, `pg_dump` and `pg_restore` are on PATH and psycopg is
importable, and skipped otherwise. The owners and nodes the plan weighs are
given in-process, since `store upgrade --check`'s tests own how they are asked.
"""

from __future__ import annotations

import json
import os
import runpy
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
SCHEMA = "lab"
# The owners the manager answers for on a fresh store: the core, and the
# legacy owner the core's first step records.
MANAGER_OWNERS = [{"owner": "core", "by": "manager", "state": "current"},
                  {"owner": "capabilities", "by": "manager", "state": "current"}]


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
    for tool in ("initdb", "pg_ctl", "pg_dump", "pg_restore"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-store-prune-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def M(cluster, tmp_path, monkeypatch):
    """The manager, in-process, on a store of its own: the core migrated, one
    owner `probe` with a pre-image its step kept, and a stub under a name a
    major step moved. Every owner answers current and no node lags until a
    test says otherwise."""
    database = cluster.database()
    for key in [k for k in os.environ if k.startswith("AGENTKIT_DB_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("AGENTKIT_DB_URL", cluster.url(database))
    monkeypatch.setenv("AGENTKIT_DB_SCHEMA", SCHEMA)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    # run_path answers a copy of the module's globals; its functions read the
    # globals themselves, which are what a test replaces a part of in.
    manager = runpy.run_path(str(MANAGER), run_name="manager_store_prune_test")
    manager = manager["cmd_store_prune"].__globals__
    db = manager["_db_module"]()
    monkeypatch.delenv(manager["_store_module"]().READ_ONLY_ENV, raising=False)
    conn = db.connect(application_name="store-prune-test", transaction=True)
    try:
        db.migrate(conn, "probe", [
            ("0001-items", "CREATE TABLE probe_items (id bigint PRIMARY KEY)"),
            ("0002-kept", "CREATE TABLE probe_premig_m1 AS SELECT 1 AS id; "
                          "COMMENT ON TABLE probe_premig_m1 IS 'kept by step 0002-kept'"),
        ], major=1, minor=0)
    finally:
        conn.close()
    with cluster.connect(database) as admin:
        admin.execute(f"SET search_path = {SCHEMA}")
        admin.execute(db.moved_stub("probe_old", ["id"], "run capabilities update"))
    manager["_project_root"] = lambda: None
    manager["STORE_BACKUP_DIR"] = tmp_path / "backups"
    manager["_store_upgrade_readers"] = lambda setting, survey, asked=None: [
        *MANAGER_OWNERS,
        {"owner": "probe", "by": "capability", "capability": "probe", "state": "current"}]
    manager["_test_database"] = database
    return manager


def _run(M, capsys, *argv: str) -> tuple[int, dict]:
    capsys.readouterr()
    code = 0
    try:
        M["cmd_store_prune"]("--apply" in argv)
    except SystemExit as exc:
        code = exc.code or 0
    out, err = capsys.readouterr()
    return code, (json.loads(out) if code == 0 else json.loads(err.strip().splitlines()[-1]))


def _held(cluster, M) -> set[str]:
    with cluster.connect(M["_test_database"]) as conn:
        return {row[0] for row in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'v')", (SCHEMA,))}


def _by_name(answer: dict) -> dict:
    return {entry["name"]: entry for entry in answer["leftovers"]}


def test_the_plan_lists_each_leftover_and_drops_nothing(cluster, M, capsys):
    before = _held(cluster, M)
    code, answer = _run(M, capsys)
    assert code == 0
    found = _by_name(answer)
    assert found["probe_premig_m1"]["kind"] == "pre-image"
    assert found["probe_premig_m1"]["owner"] == "probe"
    assert found["probe_premig_m1"]["rows"] == 1
    assert found["probe_premig_m1"]["comment"] == "kept by step 0002-kept"
    assert found["probe_old"]["kind"] == "stub"
    assert "probe_items" not in found
    assert {"probe_premig_m1", "probe_old"} <= set(answer["drop"])
    assert answer["applied"] is False
    assert answer["next"] == ["capabilities store prune --apply"]
    assert _held(cluster, M) == before


def test_a_preimage_the_store_library_reads_is_kept(cluster, M, capsys):
    names = M["_db_module"]().CONNECTION_NAMES_TABLE
    code, answer = _run(M, capsys)
    entry = _by_name(answer)[names]
    assert entry["drop"] is False
    assert [r["code"] for r in entry["read_by"]] == ["read_by_library"]
    assert names in answer["keep"]


def test_an_owner_not_current_keeps_every_leftover(cluster, M, capsys):
    M["_store_upgrade_readers"] = lambda setting, survey, asked=None: [
        *MANAGER_OWNERS,
        {"owner": "probe", "by": "capability", "capability": "probe", "state": "pending"}]
    code, answer = _run(M, capsys)
    assert code == 0
    assert answer["drop"] == []
    for entry in answer["leftovers"]:
        assert "owner_not_current" in [r["code"] for r in entry["read_by"]]


def test_a_preimage_whose_owner_no_release_here_answers_is_kept(cluster, M, capsys):
    M["_store_upgrade_readers"] = lambda setting, survey, asked=None: MANAGER_OWNERS
    code, answer = _run(M, capsys)
    found = _by_name(answer)
    assert [r["code"] for r in found["probe_premig_m1"]["read_by"]] == ["owner_not_answered"]
    assert answer["not_answered"] == ["probe"]
    assert found["probe_old"]["drop"] is True


def test_a_stub_is_kept_while_a_node_cannot_read_the_store(cluster, M, capsys):
    M["_store_nodes_answer"] = lambda survey: [
        {"node": "1a1b1c1d1e1f", "label": "old-node", "disabled": False, "reads_store": False}]
    code, answer = _run(M, capsys)
    found = _by_name(answer)
    assert [r["code"] for r in found["probe_old"]["read_by"]] == ["node_behind"]
    assert found["probe_premig_m1"]["drop"] is True
    assert answer["nodes_behind"] == ["old-node"]


def test_apply_takes_a_proven_dump_then_drops_only_what_nothing_reads(cluster, M, capsys):
    names = M["_db_module"]().CONNECTION_NAMES_TABLE
    code, answer = _run(M, capsys, "--apply")
    assert code == 0, answer
    assert answer["applied"] is True
    assert {"probe_old", "probe_premig_m1"} <= set(answer["dropped"])
    assert names not in answer["dropped"]
    assert answer["backup"]["proven"]["ok"] is True
    assert Path(answer["backup"]["path"]).is_file()
    held = _held(cluster, M)
    assert "probe_premig_m1" not in held and "probe_old" not in held
    assert names in held and "probe_items" in held


def test_apply_drops_nothing_when_the_dump_fails(cluster, M, capsys):
    def failed(*args, **kwargs):
        raise M["_UpgradeFailed"](5, "dump_failed", "pg_dump failed: no room")
    M["_store_dump"] = failed
    before = _held(cluster, M)
    code, answer = _run(M, capsys, "--apply")
    assert code == 5
    assert answer["error"]["code"] == "dump_failed"
    assert _held(cluster, M) == before


def test_a_drop_something_depends_on_drops_nothing(cluster, M, capsys):
    with cluster.connect(M["_test_database"]) as conn:
        conn.execute(f"CREATE VIEW {SCHEMA}.probe_reads AS SELECT id FROM {SCHEMA}.probe_premig_m1")
    before = _held(cluster, M)
    code, answer = _run(M, capsys, "--apply")
    assert code == 5
    assert answer["error"]["code"] == "prune_failed"
    assert "probe_premig_m1" in answer["error"]["message"]
    assert _held(cluster, M) == before


def test_apply_is_refused_under_the_read_only_switch(cluster, M, capsys, monkeypatch):
    monkeypatch.setenv(M["_store_module"]().READ_ONLY_ENV, "1")
    before = _held(cluster, M)
    code, answer = _run(M, capsys, "--apply")
    assert code == 4
    assert answer["error"]["code"] == "read_only_switch"
    assert _held(cluster, M) == before
