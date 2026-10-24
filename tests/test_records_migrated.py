"""An agent whose records moved into the store.

The contract ledger gives grants a form for every agent and identifiers a node,
and keeps settings as rows: `capabilities_settings` (one row per leaf path),
`capabilities_settings_documents` (the documents that exist, with their
generation) and `capabilities_settings_changes` (the append-only trail). An
agent whose `capabilities_agents.migrated_at` is set reads every record and its
settings from those rows - its own over every agent's, and this node's over
every node's - on a node it registered, the one whose id `node_id()` resolves
(AGENT_NODE_ID in these tests); its files are not read. Its master writes the shared rows, a node the master lets write
writes rows of its own, and while `migrating_since` is set only the migration
on the master writes. An agent that never moved reads its files and its store
exactly as before.

These tests drive the real manager and real capabilities (coolify, memory,
tasks) in a project that exists only for the test, against a throwaway
PostgreSQL on a loopback port that the module starts when `initdb` and
`pg_ctl` are on PATH and psycopg is importable, and skips otherwise. Each test
gets a database of its own.
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

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
COOLIFY = REPO / "capabilities" / "coolify" / "bin" / "coolify"
MEMORY = REPO / "capabilities" / "memory" / "bin" / "memory"
TASKS = REPO / "capabilities" / "tasks" / "bin" / "tasks"
ADMIN = "pgadmin"
SCHEMA = "lab"
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")
PROJECT_ID = "prj_" + "0a1b2c3d4e5f"


def nid(label: str) -> str:
    """The twelve-hex node id a test gives the node it labels `label`; ids
    sort as their labels do."""
    return label.encode()[:6].ljust(6, b"\0").hex()


def node_env(label: str) -> dict:
    """The environment of a process running on the node labelled `label`."""
    return {"AGENT_NODE_ID": nid(label), "AGENT_NODE_NAME": label}


MASTER_LABEL = "mac1"
SERVER_LABEL = "srv1"
MASTER = nid(MASTER_LABEL)
SERVER = nid(SERVER_LABEL)
LABELS = {MASTER: MASTER_LABEL, SERVER: SERVER_LABEL}

from capabilities_contract.capability import store as S  # noqa: E402


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

    def admin(self, database: str = "postgres"):
        import psycopg
        conn = psycopg.connect(host="127.0.0.1", port=self.port, user=ADMIN,
                               dbname=database, connect_timeout=10)
        conn.autocommit = True
        return conn

    def database(self) -> str:
        name = "t_" + uuid.uuid4().hex[:12]
        with self.admin() as conn:
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
    root = Path(tempfile.mkdtemp(prefix="capabilities-migrated-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


# --- a project that is the test's ------------------------------------------------

class Lab:
    def __init__(self, tmp_path: Path, cluster: Cluster):
        self.tmp = tmp_path
        self.cluster = cluster
        self.database = cluster.database()
        self.project = tmp_path / "project"
        self.envelope = self.project / "capabilities"
        self.config = tmp_path / "config"
        (self.project / ".git").mkdir(parents=True)
        self.envelope.mkdir()
        self.identity({"records": "store"})
        (self.project / ".env.local").write_text(
            f"AGENTKIT_DB_URL={cluster.url(self.database)}\nAGENTKIT_DB_SCHEMA={SCHEMA}\n"
            "COOLIFY_TOKEN=test-token-1234\n")

    def identity(self, extra: dict) -> None:
        (self.envelope / "project.json").write_text(json.dumps(
            {"schema": "capabilities.project.v1", "slug": "lab", "id": PROJECT_ID, **extra}))

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in DB_KEYS and not k.startswith("CAPABILITIES_")
               and k not in ("AGENT_NODE_ID", "AGENT_NODE_NAME", "VIRTUAL_ENV", "COOLIFY_TOKEN",
                             "COOLIFY_BASE_URL", "EDITOR", "VISUAL")}
        env.update({
            "HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "CAPABILITIES_HOME": str(self.tmp / "registry"),
            "CLAUDE_PROJECT_DIR": str(self.project),
            "USER": "tester",
            **node_env(MASTER_LABEL),
        })
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, exe: Path, *args: str, stdin: str | None = None,
            **extra) -> subprocess.CompletedProcess:
        return subprocess.run([str(exe), *args], cwd=self.project, env=self.env(**extra),
                              text=True, capture_output=True, timeout=240, input=stdin)

    def ok(self, exe: Path, *args: str, **extra):
        result = self.run(exe, *args, **extra)
        assert result.returncode == 0, result.stdout + result.stderr
        try:
            return json.loads(result.stdout)
        except ValueError:
            return result.stdout.strip()

    def refused(self, exe: Path, *args: str, **extra) -> tuple[int, dict]:
        result = self.run(exe, *args, **extra)
        assert result.returncode != 0, result.stdout
        line = next(line for line in reversed(result.stderr.splitlines())
                    if line.lstrip().startswith("{"))
        return result.returncode, json.loads(line)["error"]

    def sql(self, query: str, params: tuple = ()):
        with self.cluster.admin(self.database) as conn:
            conn.execute(f"SET search_path TO {SCHEMA}")
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def file(self, relative: str, body) -> None:
        path = self.project / relative if not relative.startswith("~/") \
            else self.config / relative[2:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body if isinstance(body, str) else json.dumps(body))

    def migrate(self, *, master: str = MASTER) -> None:
        """Register the agent with `master` as its master node and mark it
        moved into the store."""
        self.ok(MANAGER, "node", "add", master, "--label", LABELS[master], "--kind", "mac",
                "--master", AGENT_NODE_ID=master, AGENT_NODE_NAME=LABELS[master])
        self.sql("UPDATE capabilities_agents SET migrated_at = now() WHERE id = %s",
                 (PROJECT_ID,))


@pytest.fixture()
def lab(tmp_path, cluster):
    return Lab(tmp_path, cluster)


def _stale_files(lab: Lab) -> None:
    """Files that say something else than the rows, everywhere a project in
    files or store mode would read them."""
    lab.file("capabilities/settings.json",
             {"capabilities": {"coolify": {"enabled": False}, "memory": {"enabled": False}}})
    lab.file("capabilities/coolify/connections.json", {
        "default": "stale",
        "connections": {"stale": {"base_url": "http://stale.invalid",
                                  "secret_env": "COOLIFY_TOKEN", "allow_write": True}}})
    lab.file("capabilities/coolify/identifiers.json",
             {"team": {"value": "stale-team", "note": ""}})
    lab.file("capabilities/memory/service/settings.json", {"window_days": 99})
    lab.file("~/memory/service/settings.json", {"max_entries": 999})


# --- the schema -------------------------------------------------------------------

def _ledger_at_p2(lab: Lab) -> None:
    """A store the previous release migrated: steps 0001-0013 at version 1.2."""
    from capabilities_contract import db
    conn = db.connect(application_name="test",
                      setting=db.resolve_setting(lab.project))
    try:
        db.migrate(conn, S.STORE_OWNER, S.record_table_steps()[:13], major=1, minor=2)
    finally:
        conn.close()


@pytest.mark.parametrize("from_p2", [False, True])
def test_the_ledger_applies_the_new_steps_on_a_fresh_store_and_on_one_at_p2(lab, from_p2):
    if from_p2:
        _ledger_at_p2(lab)
        assert [r[0] for r in lab.sql("SELECT step FROM schema_ledger ORDER BY step")][-1] \
            == "0013-global"
        lab.sql("INSERT INTO capabilities_identifiers (agent_id, capability, label, value) "
                "VALUES (%s, 'coolify', 'team', '\"kept\"')", (PROJECT_ID,))
    lab.ok(MANAGER, "node", "add", MASTER, "--label", MASTER_LABEL, "--kind", "mac",
           "--master")

    steps = [r[0] for r in lab.sql(
        "SELECT step FROM schema_ledger WHERE owner = 'capabilities' ORDER BY step")]
    assert steps[-13:] == ["0014-connection-grants-global", "0015-identifiers-node",
                           "0016-settings", "0017-settings-documents",
                           "0018-settings-changes", "0019-references", "0020-connection-owner",
                           "0021-agent-slug", "0022-agent-node-coolify", "0023-agent-binding",
                           "0024-agent-node-folder", "0025-policy-service", "0026-nodes"]
    assert lab.sql("SELECT major, minor FROM schema_version WHERE owner = 'capabilities'") \
        == [(1, 11)]
    indexes = {r[0]: r[1] for r in lab.sql(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s", (SCHEMA,))}
    assert "COALESCE(agent_id, ''::text)" in indexes["capabilities_connection_grants_scope_key"]
    assert "WHERE is_default" in indexes["capabilities_connection_grants_scope_default"]
    assert "COALESCE(node, ''::text)" in indexes["capabilities_identifiers_key"]
    assert "path" in indexes["capabilities_settings_key"]
    assert "COALESCE(agent_id" in indexes["capabilities_settings_documents_key"]
    triggers = {r[0] for r in lab.sql(
        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")}
    assert {"capabilities_settings_guard", "capabilities_settings_changes_append_only",
            "capabilities_settings_changes_no_truncate", "capabilities_connections_owner",
            "capabilities_connection_grants_owner"} <= triggers
    for table in ("capabilities_connections", "capabilities_settings",
                  "capabilities_settings_documents",
                  "capabilities_settings_changes", "capabilities_connection_grants",
                  "capabilities_identifiers"):
        [(comment,)] = lab.sql("SELECT obj_description(%s::regclass)", (f"{SCHEMA}.{table}",))
        assert comment, table
    [(comment,)] = lab.sql("SELECT obj_description('lab.capabilities_settings'::regclass)")
    assert "pg_advisory_xact_lock(hashtext(concat_ws(chr(31), schema" in comment
    for column in ("path", "value", "node", "agent_id", "updated_by"):
        [(said,)] = lab.sql("SELECT col_description('lab.capabilities_settings'::regclass, "
                            "attnum) FROM pg_attribute WHERE attrelid = "
                            "'lab.capabilities_settings'::regclass AND attname = %s", (column,))
        assert said, column
    if from_p2:
        assert lab.sql("SELECT value, node FROM capabilities_identifiers") == [("kept", None)]


def test_the_settings_triggers_refuse_an_object_and_an_overlapping_path(lab):
    import psycopg
    lab.ok(MANAGER, "node", "add", MASTER, "--label", MASTER_LABEL, "--kind", "mac",
           "--master")
    insert = ("INSERT INTO capabilities_settings (agent_id, capability, node, path, value, "
              "updated_by) VALUES (%s, 'memory', %s, %s, %s::jsonb, 'tester@mac1')")
    lab.sql(insert, (PROJECT_ID, None, ["a", "b"], "1"))
    lab.sql(insert, (PROJECT_ID, None, ["e"], "{}"))
    lab.sql(insert, (PROJECT_ID, None, ["n"], "null"))
    lab.sql(insert, (PROJECT_ID, None, ["l"], '[{"x": 1}]'))
    with pytest.raises(psycopg.errors.CheckViolation, match="object with keys"):
        lab.sql(insert, (PROJECT_ID, None, ["o"], '{"k": 1}'))
    with pytest.raises(psycopg.errors.CheckViolation, match="overlaps"):
        lab.sql(insert, (PROJECT_ID, None, ["a"], "2"))
    with pytest.raises(psycopg.errors.CheckViolation, match="overlaps"):
        lab.sql(insert, (PROJECT_ID, None, ["a", "b", "c"], "2"))
    # Another document - another node, another agent - is free to hold them.
    lab.sql(insert, (PROJECT_ID, SERVER, ["a"], "2"))
    lab.sql(insert, (None, None, ["a", "b", "c"], "2"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        lab.sql(insert, (None, None, ["a", "b", "c"], "3"))
    with pytest.raises(psycopg.errors.CheckViolation, match="object with keys"):
        lab.sql("UPDATE capabilities_settings SET value = '{\"k\": 1}' WHERE path = '{e}'")


def test_the_trail_is_append_only(lab):
    import psycopg
    lab.ok(MANAGER, "node", "add", MASTER, "--label", MASTER_LABEL, "--kind", "mac",
           "--master")
    lab.sql("INSERT INTO capabilities_settings_changes (agent_id, capability, action, path, "
            "new_value, changed_by) VALUES (%s, 'memory', 'set', '{a}', '1', 'x@y')",
            (PROJECT_ID,))
    for statement in ("UPDATE capabilities_settings_changes SET changed_by = 'z@y'",
                      "DELETE FROM capabilities_settings_changes",
                      "TRUNCATE capabilities_settings_changes"):
        with pytest.raises(psycopg.errors.RestrictViolation, match="append-only"):
            lab.sql(statement)
    assert lab.sql("SELECT count(*) FROM capabilities_settings_changes") == [(1,)]


def test_grants_and_defaults_for_every_agent_cannot_duplicate(lab):
    import psycopg
    lab.ok(MANAGER, "node", "add", MASTER, "--label", MASTER_LABEL, "--kind", "mac",
           "--master")
    lab.sql("INSERT INTO capabilities_connections (capability, name, spec) "
            "VALUES ('coolify', 'box', '{}'), ('coolify', 'two', '{}')")
    grant = ("INSERT INTO capabilities_connection_grants (agent_id, capability, connection, "
             "node, allow_write, is_default) VALUES (NULL, 'coolify', %s, %s, true, %s)")
    lab.sql(grant, ("box", None, True))
    with pytest.raises(psycopg.errors.UniqueViolation):
        lab.sql(grant, ("box", None, False))
    with pytest.raises(psycopg.errors.UniqueViolation):
        lab.sql(grant, ("two", None, True))
    lab.sql(grant, ("box", SERVER, True))
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql(grant.replace("NULL", "''"), ("two", None, False))


# --- a connection's owner ------------------------------------------------------------

OTHER = "prj_" + "9f8e7d6c5b4a"


def _ledger_before_owners(lab: Lab) -> None:
    """A store the previous release migrated: steps 0001-0019 at version 1.4."""
    from capabilities_contract import db
    conn = db.connect(application_name="test", setting=db.resolve_setting(lab.project))
    try:
        db.migrate(conn, S.STORE_OWNER, S.record_table_steps()[:19], major=1, minor=4)
    finally:
        conn.close()


def _owners(lab: Lab) -> dict:
    return {(cap, name): owner for cap, name, owner in lab.sql(
        "SELECT capability, name, owner FROM capabilities_connections")}


def test_a_store_made_before_owners_gives_each_row_its_owner_by_the_rule(lab):
    _ledger_before_owners(lab)
    anonymous = "prj_" + "aaaaaaaaaaaa"
    lab.sql("INSERT INTO capabilities_agents (id, name) VALUES (%s, 'Ada'), "
            "(%s, 'Clinic Desk'), (%s, NULL)", (PROJECT_ID, OTHER, anonymous))
    lab.sql("INSERT INTO capabilities_connections (capability, name, spec) VALUES "
            "('agentrooms', 'ada', '{}'), ('telegram', '1000000001', '{\"name\": \"Ada\"}'), "
            "('rclonec', 'ada_documents', '{}'), ('agentrooms', 'clinic', '{}'), "
            "('stripe', 'default', '{}'), ('telegram', 'adams', '{}'), "
            "('clickup', 'ada', '{}'), ('telegram', 'ada', '{}'), "
            "('coolify', 'nobody', '{}'), ('ical', 'loose', '{}')")
    grant = ("INSERT INTO capabilities_connection_grants "
             "(agent_id, capability, connection, node, enabled) VALUES (%s, %s, %s, %s, true)")
    for agent, capability, name, node in (
            (PROJECT_ID, "agentrooms", "ada", None), (PROJECT_ID, "agentrooms", "ada", SERVER),
            (PROJECT_ID, "telegram", "1000000001", None),
            (PROJECT_ID, "rclonec", "ada_documents", None),
            (OTHER, "agentrooms", "clinic", None), (PROJECT_ID, "stripe", "default", None),
            (PROJECT_ID, "telegram", "adams", None), (PROJECT_ID, "clickup", "ada", None),
            (OTHER, "clickup", "ada", None), (PROJECT_ID, "telegram", "ada", None),
            (None, "telegram", "ada", None), (anonymous, "coolify", "nobody", None)):
        lab.sql(grant, (agent, capability, name, node))

    lab.ok(MANAGER, "node", "add", MASTER, "--label", MASTER_LABEL, "--kind", "mac",
           "--master")

    assert _owners(lab) == {
        # granted to exactly one agent and named for it
        ("agentrooms", "ada"): PROJECT_ID, ("telegram", "1000000001"): PROJECT_ID,
        ("rclonec", "ada_documents"): PROJECT_ID, ("agentrooms", "clinic"): OTHER,
        # the rest stay shared
        ("stripe", "default"): None, ("telegram", "adams"): None,
        ("clickup", "ada"): None, ("telegram", "ada"): None,
        ("coolify", "nobody"): None, ("ical", "loose"): None}
    listed = lab.ok(MANAGER, "connections", "list", "agentrooms", "clickup")
    assert set(listed["capabilities"]) == {"agentrooms", "clickup"}
    ada, clinic = listed["capabilities"]["agentrooms"]
    assert clinic == {
        "connection": "clinic", "owner": OTHER, "owner_name": "Clinic Desk",
        "shared": False, "grants": [{"agent": OTHER, "agent_name": "Clinic Desk",
                                     "node": None, "enabled": True, "allow_write": None,
                                     "is_default": False}]}
    assert ada["owner"] == PROJECT_ID and [g["node"] for g in ada["grants"]] == [None, SERVER]
    [shared] = listed["capabilities"]["clickup"]
    assert shared["shared"] is True and shared["owner"] is None
    assert [g["agent"] for g in shared["grants"]] == sorted([PROJECT_ID, OTHER])


def test_an_agents_own_connection_is_granted_to_no_other_agent(lab):
    import psycopg
    lab.ok(MANAGER, "node", "add", MASTER, "--label", MASTER_LABEL, "--kind", "mac",
           "--master")
    lab.sql("INSERT INTO capabilities_connections (capability, name, spec, owner) VALUES "
            "('telegram', 'mine', '{}', %s), ('telegram', 'ours', '{}', NULL)", (PROJECT_ID,))
    grant = ("INSERT INTO capabilities_connection_grants "
             "(agent_id, capability, connection, node, allow_write) "
             "VALUES (%s, 'telegram', %s, %s, %s)")
    lab.sql(grant, (PROJECT_ID, "mine", None, False))
    lab.sql(grant, (PROJECT_ID, "mine", SERVER, True))
    for agent in (OTHER, None):
        with pytest.raises(psycopg.errors.CheckViolation):
            lab.sql(grant, (agent, "mine", None, None))
    # A shared connection is read-write for one agent and read-only for another.
    lab.sql(grant, (OTHER, "ours", None, True))
    lab.sql(grant, (PROJECT_ID, "ours", None, False))
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql("UPDATE capabilities_connections SET owner = %s WHERE name = 'ours'",
                (PROJECT_ID,))
    with pytest.raises(psycopg.errors.CheckViolation):
        lab.sql("UPDATE capabilities_connection_grants SET agent_id = %s "
                "WHERE connection = 'mine'", (OTHER,))
    [ours] = [c for c in lab.ok(MANAGER, "connections", "list", "telegram")
              ["capabilities"]["telegram"] if c["connection"] == "ours"]
    assert {g["agent"]: g["allow_write"] for g in ours["grants"]} == {
        OTHER: True, PROJECT_ID: False}


def test_a_moved_agent_adds_its_own_connection_with_its_grant_and_a_machine_one_shared(moved):
    lab = moved
    fields = ("--field", "base_url=http://box.invalid", "--env", "secret_env=COOLIFY_TOKEN")
    added = lab.ok(COOLIFY, "connection", "add", "mine", "--project", *fields)
    assert added["granted"] is True and "hint" not in added
    assert _owners(lab) == {("coolify", "mine"): PROJECT_ID}
    assert lab.sql("SELECT agent_id, node, enabled, allow_write FROM "
                   "capabilities_connection_grants") == [(PROJECT_ID, None, True, None)]
    lab.ok(COOLIFY, "connection", "add", "box", "--machine", *fields)
    assert _owners(lab)[("coolify", "box")] is None
    assert lab.sql("SELECT count(*) FROM capabilities_connection_grants "
                   "WHERE connection = 'box'") == [(0,)]
    lab.ok(MANAGER, "set", "coolify", "grant", "mine", '{"allow_write": false}')
    listed = {c["connection"]: c for c in
              lab.ok(MANAGER, "connections", "list")["capabilities"]["coolify"]}
    assert listed["box"]["shared"] is True and listed["box"]["grants"] == []
    assert listed["mine"]["owner"] == PROJECT_ID and listed["mine"]["shared"] is False
    assert [(g["agent"], g["enabled"], g["allow_write"]) for g in listed["mine"]["grants"]] \
        == [(PROJECT_ID, True, False)]
    # Another agent's own connection is neither changed nor removed from here.
    lab.sql("INSERT INTO capabilities_connections (capability, name, spec, owner) "
            "VALUES ('coolify', 'theirs', '{}', %s)", (OTHER,))
    _code, error = lab.refused(COOLIFY, "connection", "add", "theirs", "--project",
                               "--replace", *fields)
    assert error["code"] == "connection_owned"
    _code, error = lab.refused(COOLIFY, "connection", "remove", "theirs")
    assert error["code"] == "connection_owned"
    # Its own goes with its own grant, which takes it from no one else.
    removed = lab.ok(COOLIFY, "connection", "remove", "mine")
    assert removed["action"] == "remove"
    assert ("coolify", "mine") not in _owners(lab)


# --- what a migrated agent reads ----------------------------------------------------

def _rows_for_reads(lab: Lab) -> None:
    """Rows that say what the agent should read: its own, every agent's, this
    node's and every node's, each differing from the stale files."""
    lab.sql("INSERT INTO capabilities_connections (capability, name, spec) VALUES "
            "('coolify', 'box', '{\"base_url\": \"http://box.invalid\", "
            "\"secret_env\": \"COOLIFY_TOKEN\"}'), "
            "('coolify', 'glob', '{\"base_url\": \"http://glob.invalid\", "
            "\"secret_env\": \"COOLIFY_TOKEN\"}')")
    lab.sql("INSERT INTO capabilities_connection_grants "
            "(agent_id, capability, connection, node, enabled, allow_write, is_default) VALUES "
            "(%s, 'coolify', 'box', NULL, true, NULL, false), "
            "(%s, 'coolify', 'glob', NULL, true, NULL, false), "
            "(NULL, 'coolify', 'box', NULL, NULL, true, false), "
            "(NULL, 'coolify', 'glob', NULL, true, false, true), "
            "(%s, 'coolify', 'box', %s, NULL, false, true)",
            (PROJECT_ID, PROJECT_ID, PROJECT_ID, SERVER))
    lab.sql("INSERT INTO capabilities_identifiers (agent_id, capability, node, label, value) "
            "VALUES (%s, 'coolify', NULL, 'team', '\"agent-team\"'), "
            "(NULL, 'coolify', NULL, 'team', '\"global-team\"'), "
            "(NULL, 'coolify', NULL, 'region', '\"global-region\"'), "
            "(%s, 'coolify', %s, 'team', '\"server-team\"'), "
            "(NULL, 'coolify', %s, 'region', '\"server-region\"')",
            (PROJECT_ID, PROJECT_ID, SERVER, SERVER))
    lab.sql("INSERT INTO capabilities_policy (agent_id, capability, node, enabled) VALUES "
            "(%s, 'coolify', NULL, true), (NULL, 'memory', NULL, true), "
            "(NULL, 'tasks', NULL, true), (%s, 'tasks', %s, false)",
            (PROJECT_ID, PROJECT_ID, SERVER))
    settings = ("INSERT INTO capabilities_settings (agent_id, capability, node, path, value, "
                "updated_by) VALUES (%s, 'memory', %s, %s, %s::jsonb, 'seed@mac1')")
    lab.sql(settings, (PROJECT_ID, None, ["window_days"], "5"))
    lab.sql(settings, (PROJECT_ID, SERVER, ["window_days"], "6"))
    lab.sql(settings, (None, None, ["window_days"], "30"))
    lab.sql(settings, (None, None, ["max_entries"], "40"))
    lab.sql("INSERT INTO capabilities_settings_documents (agent_id, capability, node, "
            "generation) VALUES (%s, 'memory', NULL, 11), (%s, 'memory', %s, 12), "
            "(NULL, 'memory', NULL, 7)", (PROJECT_ID, PROJECT_ID, SERVER))


def _register(lab: Lab, node: str, *flags: str) -> None:
    lab.ok(MANAGER, "node", "add", node, "--label", LABELS[node], "--kind", "server",
           *flags)


def test_a_migrated_agent_reads_rows_and_never_its_stale_files(lab):
    _stale_files(lab)
    lab.migrate()
    _rows_for_reads(lab)

    ids = lab.ok(COOLIFY, "ids", "list")
    assert ids["team"]["value"] == "agent-team"
    assert ids["region"]["value"] == "global-region"
    connections = lab.ok(COOLIFY, "connections")
    assert sorted(connections["connections"]) == ["box", "glob"]
    assert connections["default"] == "glob"
    assert lab.ok(MANAGER, "get", "coolify", "grant", "box") == {
        "enabled": True, "node": None, "is_default": False, "scope": "project"}
    assert lab.ok(MANAGER, "get", "coolify", "setting", "connection.default") == "glob"
    assert lab.ok(MANAGER, "get", "memory", "setting", "window_days") == 5
    assert lab.ok(MANAGER, "get", "memory", "setting", "max_entries") == 40

    settings = lab.ok(MEMORY, "settings")
    assert settings["generations"] == {"project": 11, "global": 7}
    homes = {h["scope"]: h for h in settings["homes"]}
    assert homes["project"]["document"] == {"window_days": 5}
    assert homes["project"]["path"] == f"store:{SCHEMA}.capabilities_settings"
    assert homes["global"]["document"] == {"window_days": 30, "max_entries": 40}
    assert lab.ok(MEMORY, "settings", "get", "window_days") == 5


def test_rows_for_this_node_are_read_over_the_shared_rows(lab):
    _stale_files(lab)
    lab.migrate()
    _register(lab, SERVER)
    _rows_for_reads(lab)
    on_server = node_env(SERVER_LABEL)

    ids = lab.ok(COOLIFY, "ids", "list", **on_server)
    assert ids["team"]["value"] == "server-team"
    assert ids["region"]["value"] == "server-region"
    assert lab.ok(MANAGER, "get", "coolify", "grant", "box", **on_server) == {
        "allow_write": False, "node": SERVER, "is_default": True, "scope": "project"}
    assert lab.ok(MANAGER, "get", "coolify", "setting", "connection.default",
                  **on_server) == "box"
    settings = lab.ok(MEMORY, "settings", **on_server)
    assert settings["generations"] == {"project": 12, "global": 7}
    assert {h["scope"]: h["document"] for h in settings["homes"]}["project"] \
        == {"window_days": 6}
    code, error = lab.refused(TASKS, "settings", **on_server)
    assert (code, error["code"]) == (4, "disabled")
    assert lab.run(TASKS, "settings").returncode == 0


def test_a_global_grant_never_enables_a_connection_by_itself(lab):
    lab.migrate()
    lab.sql("INSERT INTO capabilities_connections (capability, name, spec) VALUES "
            "('coolify', 'glob', '{\"base_url\": \"http://glob.invalid\", "
            "\"secret_env\": \"COOLIFY_TOKEN\"}')")
    lab.sql("INSERT INTO capabilities_connection_grants "
            "(agent_id, capability, connection, enabled, allow_write) "
            "VALUES (NULL, 'coolify', 'glob', true, true)")
    lab.sql("INSERT INTO capabilities_policy (agent_id, capability, enabled) "
            "VALUES (NULL, 'coolify', true)")
    code, error = lab.refused(COOLIFY, "connections")
    assert (code, error["code"]) == (4, "connection_not_granted")
    assert "withheld: glob" in error["message"]
    lab.sql("INSERT INTO capabilities_connection_grants "
            "(agent_id, capability, connection, enabled) VALUES (%s, 'coolify', 'glob', true)",
            (PROJECT_ID,))
    assert "glob" in lab.ok(COOLIFY, "connections")["connections"]


def test_a_migrated_agent_refuses_without_a_node_name_or_on_an_unregistered_node(lab):
    lab.migrate()
    lab.sql("INSERT INTO capabilities_policy (agent_id, capability, enabled) "
            "VALUES (%s, 'coolify', true)", (PROJECT_ID,))
    # Without AGENT_NODE_ID the process mints this machine's id, which is not
    # one of the agent's nodes; an AGENT_NODE_ID that is no id is refused.
    code, error = lab.refused(COOLIFY, "ids", "list", AGENT_NODE_ID=None, AGENT_NODE_NAME=None)
    minted = (lab.config / "capabilities" / "node-id").read_text().strip()
    assert error["code"] == "node_unregistered" and minted in error["message"]
    assert MASTER in error["hint"]
    code, error = lab.refused(COOLIFY, "ids", "list", AGENT_NODE_ID="a stranger")
    assert error["code"] == "bad_node_id" and code == 6
    stranger = node_env("stranger")
    code, error = lab.refused(COOLIFY, "ids", "list", **stranger)
    assert error["code"] == "node_unregistered"
    assert stranger["AGENT_NODE_ID"] in error["message"] and MASTER in error["hint"]
    code, error = lab.refused(MANAGER, "get", "coolify", "grant", "box", **stranger)
    assert error["code"] == "node_unregistered"


def test_a_files_mode_agent_marked_migrated_reads_rows_too(lab):
    _stale_files(lab)
    lab.identity({})
    lab.migrate()
    lab.sql("INSERT INTO capabilities_policy (agent_id, capability, enabled) "
            "VALUES (%s, 'coolify', true)", (PROJECT_ID,))
    lab.sql("INSERT INTO capabilities_identifiers (agent_id, capability, label, value) "
            "VALUES (%s, 'coolify', 'team', '\"row-team\"')", (PROJECT_ID,))
    assert lab.ok(COOLIFY, "ids", "get", "team") == "row-team"


# --- what a migrated agent writes ----------------------------------------------------

def _trail(lab: Lab) -> list[tuple]:
    return lab.sql("SELECT action, path, old_value, new_value, changed_by, node, agent_id "
                   "FROM capabilities_settings_changes ORDER BY id")


def _document(lab: Lab, capability: str = "memory", agent: str | None = PROJECT_ID,
              node: str | None = None) -> dict:
    rows = lab.sql("SELECT path, value FROM capabilities_settings WHERE capability = %s "
                   "AND coalesce(agent_id, '') = coalesce(%s, '') "
                   "AND coalesce(node, '') = coalesce(%s, '')", (capability, agent, node))
    return S.settings_tree(rows)


def _generation(lab: Lab, capability: str = "memory", agent: str | None = PROJECT_ID,
                node: str | None = None):
    found = lab.sql("SELECT generation FROM capabilities_settings_documents "
                    "WHERE capability = %s AND coalesce(agent_id, '') = coalesce(%s, '') "
                    "AND coalesce(node, '') = coalesce(%s, '')", (capability, agent, node))
    return found[0][0] if found else None


@pytest.fixture()
def moved(lab):
    _stale_files(lab)
    lab.migrate()
    lab.sql("INSERT INTO capabilities_policy (agent_id, capability, enabled) VALUES "
            "(%s, 'memory', true), (%s, 'tasks', true), (%s, 'coolify', true)",
            (PROJECT_ID, PROJECT_ID, PROJECT_ID))
    return lab


def test_settings_set_writes_rows_a_trail_and_a_generation(moved):
    lab = moved
    out = lab.ok(MEMORY, "settings", "set", "window_days", "9")
    assert out["previous"] is None and out["value"] == 9
    assert out["path"] == f"store:{SCHEMA}.capabilities_settings"
    assert _document(lab) == {"window_days": 9}
    [(action, path, old, new, by, node, agent)] = _trail(lab)
    assert (action, path, old, new, by, node, agent) == (
        "set", ["window_days"], None, 9, f"tester@{MASTER}", None, PROJECT_ID)
    [(trail_id,)] = lab.sql("SELECT max(id) FROM capabilities_settings_changes")
    assert out["generation"] == trail_id == _generation(lab)
    again = lab.ok(MEMORY, "settings", "set", "window_days", "10")
    assert again["previous"] == 9 and again["generation"] > out["generation"]
    assert _trail(lab)[-1][:4] == ("set", ["window_days"], 9, 10)
    assert lab.ok(MEMORY, "settings")["generations"]["project"] == again["generation"]
    # The stale file is never written.
    assert json.loads((lab.envelope / "memory/service/settings.json").read_text()) \
        == {"window_days": 99}


def test_a_document_the_check_refuses_writes_nothing(moved):
    lab = moved
    lab.ok(MEMORY, "settings", "set", "window_days", "9")
    before = (_trail(lab), _document(lab), _generation(lab))
    code, error = lab.refused(MEMORY, "settings", "set", "max_entries", "1")
    assert (code, error["code"]) == (6, "settings_invalid")
    code, error = lab.refused(MEMORY, "settings", "put", "--scope", "project",
                              stdin='{"bogus": 1}')
    assert (code, error["code"]) == (6, "settings_invalid")
    assert (_trail(lab), _document(lab), _generation(lab)) == before


def test_removing_the_last_child_leaves_an_empty_object_and_unset_removes(moved):
    lab = moved
    lab.ok(TASKS, "settings", "--scope", "project", "set", "lanes.alpha.max_parallel", "2")
    assert _document(lab, "tasks") == {"version": 1, "lanes": {"alpha": {"max_parallel": 2}}}
    out = lab.ok(TASKS, "settings", "--scope", "project", "unset", "lanes.alpha.max_parallel")
    assert out["previous"] == 2
    assert _document(lab, "tasks") == {"version": 1, "lanes": {"alpha": {}}}
    rows = lab.sql("SELECT path, value FROM capabilities_settings WHERE capability = 'tasks' "
                   "ORDER BY path")
    assert rows == [(["lanes", "alpha"], {}), (["version"], 1)]
    trail = [row[:4] for row in _trail(lab)]
    # Deletions first, one row per path.
    assert trail[-2:] == [("unset", ["lanes", "alpha", "max_parallel"], 2, None),
                          ("set", ["lanes", "alpha"], None, {})]
    code, error = lab.refused(TASKS, "settings", "--scope", "project", "unset", "lanes.beta")
    assert (code, error["code"]) == (3, "not_found")


def test_a_document_without_rows_reads_as_its_seed_and_the_seed_becomes_rows(moved):
    lab = moved
    shown = {h["scope"]: h for h in lab.ok(TASKS, "settings")["homes"]}
    assert shown["project"]["exists"] is False and shown["project"]["document"] is None
    assert lab.ok(TASKS, "settings", "--scope", "project", "get", "version") == 1
    lab.ok(TASKS, "settings", "--scope", "project", "set", "poll_seconds", "7")
    assert _document(lab, "tasks") == {"version": 1, "poll_seconds": 7}
    assert sorted(row[1] for row in _trail(lab)) == [["poll_seconds"], ["version"]]


def test_set_of_an_object_replaces_the_subtree_and_a_path_may_hold_dots(moved):
    lab = moved
    lab.ok(TASKS, "settings", "--scope", "project", "set", "lanes", '{"a": {"max_parallel": 1}, "b": {}}')
    lab.ok(TASKS, "settings", "--scope", "project", "set", "lanes", '{"c": {"max_parallel": 3}}')
    assert _document(lab, "tasks")["lanes"] == {"c": {"max_parallel": 3}}
    lab.ok(TASKS, "settings", "--scope", "project", "set", '["lanes", "d.e"]', '{"max_parallel": 4}')
    assert _document(lab, "tasks")["lanes"]["d.e"] == {"max_parallel": 4}
    assert lab.ok(TASKS, "settings", "--scope", "project", "get", '["lanes", "d.e", "max_parallel"]') == 4


def test_capabilities_set_writes_a_literal_top_level_key_and_the_default_to_grants(moved):
    lab = moved
    lab.ok(MANAGER, "set", "telegram", "setting", "binding.example", '{"room": "x"}')
    assert _document(lab, "telegram") == {"binding.example": {"room": "x"}}
    assert lab.ok(MANAGER, "get", "telegram", "setting", "binding.example") == {"room": "x"}
    assert _trail(lab)[-1][1] == ["binding.example", "room"]
    lab.ok(MANAGER, "set", "coolify", "connection", "box",
           '{"base_url": "http://box.invalid", "secret_env": "COOLIFY_TOKEN"}')
    out = lab.ok(MANAGER, "set", "coolify", "setting", "connection.default", "box")
    assert out["records"]["source"] == f"store:{SCHEMA}.capabilities_connection_grants"
    assert lab.sql("SELECT agent_id, connection, node, is_default FROM "
                   "capabilities_connection_grants") == [(PROJECT_ID, "box", None, True)]
    assert lab.sql("SELECT count(*) FROM capabilities_settings WHERE capability = 'coolify'") \
        == [(0,)]


def test_export_and_edit_keep_hand_editing(moved, tmp_path):
    lab = moved
    lab.ok(MEMORY, "settings", "set", "window_days", "9")
    assert lab.ok(MEMORY, "settings", "export", "--scope", "project") == {"window_days": 9}
    editor = tmp_path / "editor.py"
    editor.write_text("import json, sys\n"
                      "p = sys.argv[1]\n"
                      "d = json.load(open(p))\n"
                      "d['window_days'] = 12\n"
                      "d['block_chars'] = 4000\n"
                      "json.dump(d, open(p, 'w'))\n")
    command = f"{sys.executable} {editor}"
    code, error = lab.refused(MEMORY, "settings", "edit", "--scope", "project",
                              EDITOR=command)
    assert error["code"] == "confirm_required"
    assert _document(lab) == {"window_days": 9}
    result = lab.run(MEMORY, "settings", "edit", "--scope", "project", "--yes",
                     EDITOR=command)
    assert result.returncode == 0, result.stderr
    assert "~ window_days: 9 -> 12" in result.stderr and "+ block_chars: 4000" in result.stderr
    assert json.loads(result.stdout)["changed"] is True
    assert _document(lab) == {"window_days": 12, "block_chars": 4000}
    bad = tmp_path / "bad.py"
    bad.write_text("import json, sys\njson.dump({'bogus': 1}, open(sys.argv[1], 'w'))\n")
    code, error = lab.refused(MEMORY, "settings", "edit", "--scope", "project", "--yes",
                              EDITOR=f"{sys.executable} {bad}")
    assert error["code"] == "settings_invalid"
    assert _document(lab) == {"window_days": 12, "block_chars": 4000}


def test_identifiers_policy_and_grants_are_written_as_rows(moved):
    lab = moved
    lab.ok(COOLIFY, "ids", "set", "team", "row-team")
    assert lab.sql("SELECT agent_id, node, value FROM capabilities_identifiers") \
        == [(PROJECT_ID, None, "row-team")]
    lab.ok(MANAGER, "disable", "memory", "--project")
    assert lab.sql("SELECT enabled FROM capabilities_policy WHERE capability = 'memory' "
                   "AND agent_id = %s", (PROJECT_ID,)) == [(False,)]
    # The stale gate file is neither read nor written.
    assert json.loads((lab.envelope / "settings.json").read_text()) == {
        "capabilities": {"coolify": {"enabled": False}, "memory": {"enabled": False}}}
    assert lab.ok(MANAGER, "enable", "memory", "--project")["gate"] \
        == f"store:{SCHEMA}.capabilities_policy"
    lab.ok(MANAGER, "set", "coolify", "connection", "box",
           '{"base_url": "http://box.invalid", "secret_env": "COOLIFY_TOKEN"}')
    lab.ok(MANAGER, "set", "coolify", "grant", "box", '{"enabled": true}')
    assert lab.sql("SELECT agent_id, node, enabled FROM capabilities_connection_grants") \
        == [(PROJECT_ID, None, True)]


# --- who may write -------------------------------------------------------------------

def test_off_the_master_shared_writes_are_refused(moved):
    lab = moved
    _register(lab, SERVER)
    on_server = node_env(SERVER_LABEL)
    for exe, args in ((MEMORY, ("settings", "set", "window_days", "9")),
                      (COOLIFY, ("ids", "set", "team", "x")),
                      (MANAGER, ("enable", "coolify", "--project")),
                      (MANAGER, ("set", "telegram", "setting", "a", "1"))):
        code, error = lab.refused(exe, *args, **on_server)
        assert error["code"] == "node_read_only", (exe, error)
        assert MASTER in error["message"] + error.get("hint", "")
    assert lab.sql("SELECT count(*) FROM capabilities_settings") == [(0,)]
    assert lab.sql("SELECT count(*) FROM capabilities_identifiers") == [(0,)]


def test_a_node_allowed_to_write_writes_rows_marked_with_its_node(moved):
    lab = moved
    _register(lab, SERVER, "--may-write")
    lab.ok(MEMORY, "settings", "set", "window_days", "9")
    on_server = node_env(SERVER_LABEL)
    out = lab.ok(MEMORY, "settings", "set", "window_days", "3", **on_server)
    assert out["node"] == SERVER
    assert _document(lab) == {"window_days": 9}
    assert _document(lab, node=SERVER) == {"window_days": 3}
    assert _trail(lab)[-1][4:6] == (f"tester@{SERVER}", SERVER)
    assert lab.ok(MEMORY, "settings", "get", "window_days", **on_server) == 3
    assert lab.ok(MEMORY, "settings", "get", "window_days") == 9
    lab.ok(COOLIFY, "ids", "set", "team", "node-team", **on_server)
    assert lab.sql("SELECT node, value FROM capabilities_identifiers") \
        == [(SERVER, "node-team")]
    lab.ok(MANAGER, "set", "coolify", "connection", "box",
           '{"base_url": "http://box.invalid", "secret_env": "COOLIFY_TOKEN"}')
    code, error = lab.refused(MANAGER, "set", "coolify", "connection", "box",
                              '{"base_url": "http://other.invalid"}', **on_server)
    assert error["code"] == "node_read_only"
    lab.ok(MANAGER, "set", "coolify", "grant", "box", '{"enabled": true}', **on_server)
    assert lab.sql("SELECT agent_id, node, enabled FROM capabilities_connection_grants") \
        == [(PROJECT_ID, SERVER, True)]


def test_an_open_migration_freezes_every_write_but_the_migration_on_the_master(moved):
    lab = moved
    lab.sql("UPDATE capabilities_agents SET migrating_since = now()")
    for args, exe in ((("settings", "set", "window_days", "9"), MEMORY),
                      (("ids", "set", "team", "x"), COOLIFY),
                      (("set", "telegram", "setting", "a", "1"), MANAGER)):
        code, error = lab.refused(exe, *args)
        assert error["code"] == "agent_migrating", error
        assert MASTER in error["message"]
    assert lab.sql("SELECT count(*) FROM capabilities_settings") == [(0,)]
    env = lab.env()
    previous = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        for actor, node, allowed in (("migrate", MASTER, True), (None, MASTER, False),
                                     ("migrate", SERVER, False)):
            os.environ.update(AGENT_NODE_ID=node, AGENT_NODE_NAME=LABELS[node])
            if node == SERVER and not lab.sql(
                    "SELECT 1 FROM capabilities_agent_nodes WHERE node = %s", (SERVER,)):
                _register(lab, SERVER)
            adapter = S.open_records(lab.envelope, lab.config, project_root=lab.project,
                                     actor=actor)
            try:
                if allowed:
                    adapter.set("memory", "setting", "window_days", 4)
                else:
                    with pytest.raises(S.StoreError) as refused:
                        adapter.set("memory", "setting", "window_days", 4)
                    assert refused.value.slug == "agent_migrating"
            finally:
                adapter.close()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert _document(lab) == {"window_days": 4}
    assert _trail(lab)[-1][4] == f"migrate@{MASTER}"


def test_an_open_migration_freezes_a_files_mode_agents_file_writes(lab):
    lab.identity({})
    lab.file("capabilities/settings.json", {"capabilities": {"memory": {"enabled": True}}})
    lab.ok(MANAGER, "node", "add", MASTER, "--label", MASTER_LABEL, "--kind", "mac",
           "--master")
    before = lab.ok(MEMORY, "settings", "set", "window_days", "9")
    assert before["path"].endswith("memory/service/settings.json")
    lab.sql("UPDATE capabilities_agents SET migrating_since = now()")
    code, error = lab.refused(MEMORY, "settings", "set", "window_days", "10")
    assert error["code"] == "agent_migrating"
    code, error = lab.refused(MANAGER, "set", "telegram", "setting", "a", "1")
    assert error["code"] == "agent_migrating"
    assert json.loads((lab.envelope / "memory/service/settings.json").read_text()) \
        == {"window_days": 9}


# --- what an agent that never moved does -----------------------------------------

def test_an_agent_without_migrated_at_keeps_its_files_and_store_mode(lab):
    lab.identity({})
    lab.file("capabilities/settings.json", {"capabilities": {"memory": {"enabled": True}}})
    lab.file("capabilities/memory/service/settings.json", {"window_days": 8})
    lab.ok(MANAGER, "node", "add", MASTER, "--label", MASTER_LABEL, "--kind", "mac",
           "--master")
    assert lab.ok(MEMORY, "settings", "get", "window_days") == 8
    shown = lab.ok(MEMORY, "settings")
    assert "generations" not in shown
    lab.ok(MEMORY, "settings", "set", "window_days", "9")
    assert json.loads((lab.envelope / "memory/service/settings.json").read_text()) \
        == {"window_days": 9}
    assert lab.sql("SELECT count(*) FROM capabilities_settings") == [(0,)]


def test_a_files_mode_project_with_a_store_configured_creates_no_tables(lab):
    lab.identity({})
    lab.file("capabilities/settings.json", {"capabilities": {"memory": {"enabled": True}}})
    lab.ok(MEMORY, "settings", "set", "window_days", "9")
    assert lab.ok(MEMORY, "settings", "get", "window_days") == 9
    with lab.cluster.admin(lab.database) as conn:
        assert conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = %s",
                            (SCHEMA,)).fetchone() == (0,)


def test_a_service_and_the_migration_are_recorded_by_name(moved, monkeypatch):
    lab = moved
    for key, value in lab.env().items():
        monkeypatch.setenv(key, value)
    for key in DB_KEYS:
        monkeypatch.delenv(key, raising=False)
    adapter = S.open_records(lab.envelope, lab.config, project_root=lab.project,
                             actor="memory-service")
    try:
        adapter.set("memory", "setting", "window_days", 4)
        adapter.set("memory", "setting", "block_chars", 4000, actor="migrate")
        adapter.set("memory", "setting", "max_entries", 50, actor="memory set by hand")
    finally:
        adapter.close()
    assert [row[4] for row in _trail(lab)] == [
        f"memory-service@{MASTER}", f"migrate@{MASTER}", f"memory-service@{MASTER}"]
    adapter = S.open_records(lab.envelope, lab.config, project_root=lab.project)
    try:
        adapter.set("memory", "setting", "window_days", 5)
    finally:
        adapter.close()
    assert _trail(lab)[-1][4] == f"tester@{MASTER}"
