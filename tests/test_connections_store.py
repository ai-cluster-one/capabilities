"""A project's connections and grants in its store.

A project in store mode resolves its connections and grants from the contract's
two tables, `capabilities_connections` and `capabilities_connection_grants`,
exactly as a project in files mode resolves them from its files: the same
entries, grant before use, the write gate, the read-only switch and the same
reports. `capabilities connections import` moves a project from its files into
its store; a project that was never moved keeps its files, store or no store.

These tests drive the real manager and a real connection-bearing capability
(coolify, against an HTTP instance that is nobody's) in a project that exists
only for the test, against a throwaway PostgreSQL on a loopback port that the
module starts when `initdb` and `pg_ctl` are on PATH and psycopg is importable,
and skips otherwise. Each test gets a database of its own.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
COOLIFY = REPO / "capabilities" / "coolify" / "bin" / "coolify"
ADMIN = "pgadmin"
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")

sys.path.insert(0, str(REPO / "contract"))
import store as S  # noqa: E402


def test_the_node_is_the_host_name():
    assert S.node_name() == socket.gethostname()


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

    def url(self, database: str, port: int | None = None) -> str:
        return (f"postgresql://{ADMIN}@127.0.0.1:{port or self.port}/{database}"
                "?sslmode=disable")


@pytest.fixture(scope="module")
def cluster():
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-connections-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


# --- an instance that is nobody's, and a project that is the test's -------------

class _Instance(BaseHTTPRequestHandler):
    seen: list

    def _answer(self, body) -> None:
        self.seen.append((self.command, self.path.split("?", 1)[0]))
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._answer([])

    def do_POST(self):  # noqa: N802
        self._answer({"deployments": []})

    def log_message(self, *_args):
        pass


@pytest.fixture()
def instance():
    seen: list = []
    handler = type("Handler", (_Instance,), {"seen": seen})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()


PROJECT_ID = "prj_" + "0a1b2c3d4e5f"


@pytest.fixture()
def lab(tmp_path, instance, cluster):
    url, seen = instance
    database = cluster.database()
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "capabilities"
    (envelope / "coolify").mkdir(parents=True)
    (envelope / "settings.json").write_text(
        json.dumps({"capabilities": {"coolify": {"enabled": True}}}))
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": PROJECT_ID, "slug": "lab"}))
    (envelope / "coolify" / "connections.json").write_text(json.dumps({
        "default": "box",
        "connections": {
            "box": {"base_url": url, "secret_env": "COOLIFY_TOKEN", "allow_write": True},
            "spare": {"base_url": url, "secret_env": "COOLIFY_TOKEN"},
            "off": {"base_url": url, "secret_env": "COOLIFY_TOKEN", "enabled": False},
        }}))
    (project / ".env").write_text(f"AGENTKIT_DB_URL={cluster.url(database)}\n"
                                  "AGENTKIT_DB_SCHEMA=lab\n"
                                  "COOLIFY_TOKEN=test-token-1234\n")
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
    })
    for leaked in ("CAPABILITIES_READ_ONLY", "COOLIFY_BASE_URL", "COOLIFY_TOKEN",
                   "VIRTUAL_ENV", "CAPABILITIES_PROJECT_ENVELOPE",
                   "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CAPABILITIES_PROJECT_ID",
                   "CAPABILITIES_PROJECT_ID_ROOT", *DB_KEYS):
        env.pop(leaked, None)
    return {"project": project, "envelope": envelope, "env": env, "seen": seen,
            "database": database, "cluster": cluster, "url": url,
            "config": tmp_path / "config"}


def _run(lab, exe: Path, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**lab["env"], **(extra or {})}
    return subprocess.run([str(exe), *args], cwd=lab["project"], env=env, text=True,
                          capture_output=True, timeout=240)


def _manager(lab, *args: str, **kw) -> subprocess.CompletedProcess:
    return _run(lab, MANAGER, *args, **kw)


def _coolify(lab, *args: str, **kw) -> subprocess.CompletedProcess:
    return _run(lab, COOLIFY, *args, **kw)


def _ok(result: subprocess.CompletedProcess):
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _error(result: subprocess.CompletedProcess) -> dict:
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines, result.stdout
    return json.loads(lines[-1])["error"]


def _rows(lab, query: str) -> list[tuple]:
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("SET search_path TO lab")
        return conn.execute(query).fetchall()


def _tables(lab) -> list[str]:
    with lab["cluster"].admin(lab["database"]) as conn:
        return [row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'lab' ORDER BY 1")]


def _without_sources(report: dict) -> dict:
    """The report with the origin of each connection-tier value left out: a file
    in files mode, the store's table in store mode, and nothing else differs."""
    out = json.loads(json.dumps(report))
    for entry in out["connections"].values():
        for key in entry["keys"]:
            if key["tier"] == "connection":
                key["source"] = None
    return out


def _import(lab) -> dict:
    return _ok(_manager(lab, "connections", "import"))


# --- files mode stays files mode ---------------------------------------------------

def test_a_project_never_moved_keeps_its_files_with_a_store_configured(lab):
    report = _ok(_coolify(lab, "connections"))
    assert set(report["connections"]) == {"box", "spare"}
    source = report["connections"]["box"]["keys"][0]["source"]
    assert source.endswith("capabilities/coolify/connections.json")
    assert _ok(_manager(lab, "get", "coolify", "grant", "box")) == {"allow_write": True}
    assert _tables(lab) == []


# --- the import ----------------------------------------------------------------------

def test_import_moves_the_project_and_it_resolves_the_same_set(lab):
    before = _ok(_coolify(lab, "connections"))
    connection_before = _ok(_manager(lab, "get", "coolify", "connection", "box"))
    result = _import(lab)
    assert result["mode"] == "store" and result["previous_mode"] == "files"
    assert result["agent_id"] == PROJECT_ID and result["node"] == socket.gethostname()
    assert result["capabilities"]["coolify"]["connections"]["imported"] == ["box", "off", "spare"]
    identity = json.loads((lab["envelope"] / "project.json").read_text())
    assert identity["connections"] == "store" and identity["id"] == PROJECT_ID
    after = _ok(_coolify(lab, "connections"))
    assert _without_sources(after) == _without_sources(before)
    assert after["connections"]["box"]["keys"][0]["source"] == \
        "store:lab.capabilities_connections"
    assert _ok(_manager(lab, "get", "coolify", "connection", "box")) == connection_before
    assert _ok(_manager(lab, "get", "coolify", "grant", "box")) == {
        "enabled": True, "allow_write": True, "node": None, "is_default": True}
    assert _manager(lab, "get", "coolify", "setting", "connection.default").stdout.strip() \
        == "box"
    rows = _rows(lab, "SELECT agent_id, connection, node, enabled, allow_write, is_default "
                      "FROM capabilities_connection_grants ORDER BY connection")
    assert rows == [(PROJECT_ID, "box", None, True, True, True),
                    (PROJECT_ID, "off", None, False, None, False),
                    (PROJECT_ID, "spare", None, True, None, False)]
    specs = dict(_rows(lab, "SELECT name, spec FROM capabilities_connections"))
    assert specs["box"] == {"base_url": lab["url"], "secret_env": "COOLIFY_TOKEN"}
    assert "test-token" not in json.dumps(specs)


def test_a_second_import_changes_nothing(lab):
    _import(lab)
    again = _import(lab)
    assert again["previous_mode"] == "store"
    assert again["capabilities"]["coolify"]["connections"] == {
        "imported": [], "unchanged": ["box", "off", "spare"]}
    assert again["capabilities"]["coolify"]["grants"] == {
        "imported": [], "unchanged": ["box", "off", "spare"]}


def test_import_refuses_a_row_that_differs_and_writes_nothing(lab):
    _import(lab)
    _ok(_manager(lab, "set", "coolify", "grant", "box", '{"allow_write": false}'))
    before = _rows(lab, "SELECT * FROM capabilities_connection_grants ORDER BY connection")
    refused = _manager(lab, "connections", "import")
    assert refused.returncode == 6, refused.stdout + refused.stderr
    error = _error(refused)
    assert error["code"] == "import_conflict" and "'box'" in error["message"]
    assert _rows(lab, "SELECT * FROM capabilities_connection_grants ORDER BY connection") \
        == before


def test_import_refuses_a_connection_another_project_declared_differently(lab):
    _import(lab)
    registry = lab["envelope"] / "coolify" / "connections.json"
    body = json.loads(registry.read_text())
    body["connections"]["spare"]["base_url"] = "http://127.0.0.1:1"
    registry.write_text(json.dumps(body))
    identity = lab["envelope"] / "project.json"
    files_mode = {k: v for k, v in json.loads(identity.read_text()).items()
                  if k != "connections"}
    identity.write_text(json.dumps({**files_mode, "id": "prj_ffffffffffff"}))
    refused = _manager(lab, "connections", "import")
    assert refused.returncode == 6 and _error(refused)["code"] == "import_conflict"
    assert "connections" not in json.loads(identity.read_text())
    assert _rows(lab, "SELECT count(*) FROM capabilities_connection_grants "
                      "WHERE agent_id = 'prj_ffffffffffff'") == [(0,)]


def test_import_is_refused_under_the_read_only_switch(lab):
    refused = _manager(lab, "connections", "import", extra={"CAPABILITIES_READ_ONLY": "1"})
    assert refused.returncode == 4 and _error(refused)["code"] == "read_only_switch"
    assert "connections" not in json.loads((lab["envelope"] / "project.json").read_text())
    assert _tables(lab) == []


def test_a_global_identity_keeps_its_grant_through_the_import(lab):
    global_dir = lab["config"] / "coolify"
    global_dir.mkdir(parents=True)
    (global_dir / "connections.json").write_text(json.dumps({"connections": {
        "shared": {"base_url": lab["url"], "secret_env": "COOLIFY_TOKEN",
                   "allow_write": False},
        "hidden": {"base_url": lab["url"], "secret_env": "COOLIFY_TOKEN"}}}))
    registry = lab["envelope"] / "coolify" / "connections.json"
    body = json.loads(registry.read_text())
    body["connections"]["shared"] = {"enabled": True}
    registry.write_text(json.dumps(body))
    before = _ok(_coolify(lab, "connections"))
    assert set(before["connections"]) == {"box", "spare", "shared"}
    result = _import(lab)
    assert "shared" in result["capabilities"]["coolify"]["grants"]["imported"]
    after = _ok(_coolify(lab, "connections"))
    assert _without_sources(after) == _without_sources(before)
    assert after["connections"]["shared"]["allow_write"] is False
    hidden = _coolify(lab, "--connection", "hidden", "resources")
    assert hidden.returncode == 4 and _error(hidden)["code"] == "connection_not_granted"


# --- store mode, as a capability sees it --------------------------------------------

def test_an_ungranted_store_connection_is_refused_until_granted(lab):
    _import(lab)
    declared = _ok(_manager(lab, "set", "coolify", "connection", "other", json.dumps(
        {"base_url": lab["url"], "secret_env": "COOLIFY_TOKEN"})))
    assert declared["records"] == {"mode": "store",
                                   "source": "store:lab.capabilities_connections"}
    refused = _coolify(lab, "--connection", "other", "resources")
    assert refused.returncode == 4, refused.stdout + refused.stderr
    assert _error(refused)["code"] == "connection_not_granted"
    assert not lab["seen"]
    switched_off = _coolify(lab, "--connection", "off", "resources")
    assert _error(switched_off)["code"] == "connection_not_granted"
    _ok(_manager(lab, "set", "coolify", "grant", "other", '{"enabled": true}'))
    _ok(_coolify(lab, "--connection", "other", "resources"))
    assert ("GET", "/api/v1/resources") in lab["seen"]


def test_a_read_only_grant_refuses_a_write_verb(lab):
    _import(lab)
    _ok(_coolify(lab, "deploy", "some-app"))
    assert ("POST", "/api/v1/deploy") in lab["seen"]
    lab["seen"].clear()
    _ok(_manager(lab, "set", "coolify", "grant", "box", '{"allow_write": false}'))
    refused = _coolify(lab, "deploy", "some-app")
    assert refused.returncode == 4 and _error(refused)["code"] == "read_only"
    assert "capabilities set coolify grant box" in _error(refused)["hint"]
    assert not [call for call in lab["seen"] if call[0] != "GET"]
    report = _ok(_coolify(lab, "connections"))
    assert report["connections"]["box"]["allow_write"] is False
    _ok(_manager(lab, "set", "coolify", "grant", "box", '{"allow_write": true}'))
    switched = _coolify(lab, "deploy", "some-app", extra={"CAPABILITIES_READ_ONLY": "1"})
    assert switched.returncode == 4 and _error(switched)["code"] == "read_only_switch"
    under_switch = _ok(_coolify(lab, "connections", extra={"CAPABILITIES_READ_ONLY": "1"}))
    assert under_switch["connections"]["box"]["allow_write"] is False


def test_a_grant_naming_this_node_is_used_before_the_grant_for_every_node(lab):
    _import(lab)
    node = socket.gethostname()
    _ok(_manager(lab, "set", "coolify", "grant", "box", '{"allow_write": false}'))
    _ok(_manager(lab, "set", "coolify", "grant", "box",
                 json.dumps({"node": "another-node", "allow_write": True})))
    assert _error(_coolify(lab, "deploy", "x"))["code"] == "read_only"
    _ok(_manager(lab, "set", "coolify", "grant", "box",
                 json.dumps({"node": node, "allow_write": True})))
    assert _ok(_manager(lab, "get", "coolify", "grant", "box")) == {
        "allow_write": True, "node": node, "is_default": False}
    _ok(_coolify(lab, "deploy", "x"))
    assert ("POST", "/api/v1/deploy") in lab["seen"]
    # The default pointer for this node wins over the one for every node.
    _ok(_manager(lab, "set", "coolify", "grant", "spare",
                 json.dumps({"node": node, "is_default": True})))
    assert _manager(lab, "get", "coolify", "setting",
                    "connection.default").stdout.strip() == "spare"
    assert _ok(_coolify(lab, "connections"))["default"] == "spare"


def test_a_store_that_cannot_be_reached_is_store_unreachable(lab):
    _import(lab)
    closed = _free_port()
    (lab["project"] / ".env").write_text(
        f"AGENTKIT_DB_URL={lab['cluster'].url(lab['database'], port=closed)}\n"
        "AGENTKIT_DB_SCHEMA=lab\nCOOLIFY_TOKEN=test-token-1234\n")
    for result in (_coolify(lab, "connections"), _coolify(lab, "deploy", "x"),
                   _manager(lab, "get", "coolify", "grant", "box")):
        assert result.returncode == 5, result.stdout + result.stderr
        assert _error(result)["code"] == "store_unreachable"
    doctor = _manager(lab, "doctor")
    assert doctor.returncode == 7
    assert any("store_unreachable" in finding
               for finding in json.loads(doctor.stdout)["findings"])
    assert not lab["seen"]


# --- doctor -----------------------------------------------------------------------

def _doctor(lab) -> dict:
    """The doctor's report. The sandbox has no skill installed, so doctor finds
    that whatever the store says; what is asserted is what it says about the
    store."""
    result = _manager(lab, "doctor")
    assert result.returncode in (0, 7), result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert not [finding for finding in report["findings"]
                if "store" in finding or "agent" in finding], report["findings"]
    return report


def test_doctor_reports_unknown_agents_as_information_only(lab):
    _import(lab)
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("INSERT INTO lab.capabilities_connection_grants "
                     "(agent_id, capability, connection, enabled) "
                     "VALUES ('prj_999999999999', 'coolify', 'spare', true)")
    first = _doctor(lab)
    assert [entry["code"] for entry in first["information"]] == ["agents_registry_absent"]
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("CREATE TABLE lab.contextkit_agents (id text PRIMARY KEY, name text)")
        conn.execute("INSERT INTO lab.contextkit_agents VALUES (%s, 'lab')", (PROJECT_ID,))
    second = _doctor(lab)
    assert second["information"] == [{
        "code": "grant_agent_unknown", "agent_id": "prj_999999999999", "grants": 1,
        "message": "1 connection grant(s) name agent prj_999999999999, which "
                   "ContextKit's registry does not know"}]
    assert second["findings"] == first["findings"]
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("INSERT INTO lab.contextkit_agents VALUES ('prj_999999999999', 'x')")
    assert "information" not in _doctor(lab)


def test_doctor_in_files_mode_does_not_reach_the_store(lab):
    assert "information" not in _doctor(lab)
    assert _tables(lab) == []
