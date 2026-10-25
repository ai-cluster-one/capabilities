"""A project's records in its store.

A project in store mode resolves its connections and grants from the contract's
tables, `capabilities_connections` and `capabilities_connection_grants`,
exactly as a project in files mode resolves them from its files: the same
entries, grant before use, the write gate, the read-only switch and the same
reports. Its identifiers and its gate come from `capabilities_identifiers` and
`capabilities_policy` the same way, a global row beneath the project's own.
`capabilities connections import` moves a project from its files into its
store; a project that was never moved keeps its files, store or no store, and
one that moved only its connections and grants keeps the rest in files.

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

from capabilities_contract.capability import store as S  # noqa: E402


def _resolve_fresh(monkeypatch, tmp_path, platform="linux"):
    monkeypatch.delenv(S.NODE_ID_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(S, "_mac_node_id", None)


def test_a_machine_mints_its_node_id_once_and_resolves_it_after(monkeypatch, tmp_path):
    _resolve_fresh(monkeypatch, tmp_path)
    monkeypatch.setenv(S.NODE_NAME_ENV, "lab-node")
    first = S.node_identity()
    assert first["source"] == "minted" and S.NODE_ID_RE.fullmatch(first["id"])
    assert first["id"] not in (socket.gethostname(), "lab-node")
    assert (tmp_path / "config" / "capabilities" / "node-id").read_text().strip() == first["id"]
    assert S.node_identity() == {"id": first["id"], "source": "file"}
    assert S.node_name() == S.node_id() == first["id"]
    assert S.this_node() == {"id": first["id"], "source": "file", "label": "lab-node"}


def test_a_mac_derives_its_node_id_from_its_hardware(monkeypatch, tmp_path):
    _resolve_fresh(monkeypatch, tmp_path, "darwin")
    monkeypatch.setattr(S, "_mac_hardware_uuid", lambda: "00000000-1111-2222-3333-444444444444")
    found = S.node_identity()
    assert found["source"] == "hardware" and S.NODE_ID_RE.fullmatch(found["id"])
    assert "00000000" not in found["id"] and not (tmp_path / "config").exists()
    monkeypatch.setattr(S, "_mac_node_id", None)
    assert S.node_identity() == found
    monkeypatch.setattr(S, "_mac_node_id", None)
    monkeypatch.setattr(S, "_mac_hardware_uuid", lambda: "00000000-1111-2222-3333-555555555555")
    assert S.node_id() != found["id"]


def test_agent_node_id_overrides_and_must_be_an_id(monkeypatch, tmp_path):
    _resolve_fresh(monkeypatch, tmp_path)
    monkeypatch.setenv(S.NODE_ID_ENV, " 0123456789ab ")
    assert S.node_identity() == {"id": "0123456789ab", "source": "env"}
    assert not (tmp_path / "config").exists()
    # A node registered before ids is still keyed by its name, which a box
    # compiled for its deployment target is handed.
    monkeypatch.setenv(S.NODE_ID_ENV, "lab-node")
    assert S.node_identity() == {"id": "lab-node", "source": "env"}
    for bad in ("lab node", "-lab", "lab/node"):
        monkeypatch.setenv(S.NODE_ID_ENV, bad)
        with pytest.raises(S.StoreError) as refused:
            S.node_id()
        assert refused.value.slug == "bad_node_id"


def test_a_node_id_file_that_holds_no_id_is_refused(monkeypatch, tmp_path):
    _resolve_fresh(monkeypatch, tmp_path)
    (tmp_path / "config" / "capabilities").mkdir(parents=True)
    (tmp_path / "config" / "capabilities" / "node-id").write_text("work-mac\n")
    with pytest.raises(S.StoreError) as refused:
        S.node_id()
    assert refused.value.slug == "node_unidentified"
    assert S.write_node_id("abcdefabcdef") == "abcdefabcdef"
    assert S.node_identity() == {"id": "abcdefabcdef", "source": "file"}


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
                   "CAPABILITIES_PROJECT_ID_ROOT", "AGENT_NODE_NAME", "AGENT_NODE_ID",
                   *DB_KEYS):
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


def _text(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


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


def _here(lab) -> str:
    """The node id this lab's machine resolves for itself."""
    return _ok(_manager(lab, "node", "id"))["id"]


# --- the import ----------------------------------------------------------------------

def test_import_moves_the_project_and_it_resolves_the_same_set(lab):
    before = _ok(_coolify(lab, "connections"))
    connection_before = _ok(_manager(lab, "get", "coolify", "connection", "box"))
    result = _import(lab)
    assert result["mode"] == "store" and result["previous_mode"] == "files"
    assert result["agent_id"] == PROJECT_ID and result["node"] == _here(lab)
    assert result["capabilities"]["coolify"]["connections"]["imported"] == ["box", "off", "spare"]
    identity = json.loads((lab["envelope"] / "project.json").read_text())
    assert identity["records"] == "store" and "id" not in identity
    assert f"CAPABILITIES_PROJECT_ID={PROJECT_ID}\n" in (lab["envelope"].parent / ".env.local").read_text()
    assert "connections" not in identity
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


def test_help_names_the_home_the_project_keeps_its_connections_in(lab):
    before = _text(_coolify(lab, "help"))
    assert "This project keeps its connections in files" in before
    assert str(lab["envelope"] / "coolify" / "connections.json") in before
    _import(lab)
    after = _text(_coolify(lab, "help"))
    home = after.split("═══ Where connections live", 1)[1]
    assert "This project keeps its connections in its store" in home
    assert "capabilities_connections" in home
    assert str(lab["envelope"] / "coolify" / "connections.json") not in after


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
    assert hidden.returncode == 6 and _error(hidden)["code"] == "unknown_connection"


# --- the import writes only the connections the project uses -------------------------

def _offer_global(lab, connections: dict, default: str | None = None) -> None:
    """The config home offers these coolify connections to every project."""
    global_dir = lab["config"] / "coolify"
    global_dir.mkdir(parents=True, exist_ok=True)
    body: dict = {"connections": {
        cid: {"base_url": lab["url"], "secret_env": "COOLIFY_TOKEN", **extra}
        for cid, extra in connections.items()}}
    if default:
        body["default"] = default
    (global_dir / "connections.json").write_text(json.dumps(body))


def _grant_in_envelope(lab, **grants: dict) -> None:
    registry = lab["envelope"] / "coolify" / "connections.json"
    body = json.loads(registry.read_text())
    body["connections"].update(grants)
    registry.write_text(json.dumps(body))


def _stored_connections(lab) -> list[str]:
    return [row[0] for row in _rows(lab, "SELECT name FROM capabilities_connections "
                                         "ORDER BY name")]


def _stored_grants(lab) -> list[str]:
    return [row[0] for row in _rows(lab, "SELECT connection FROM "
                                         "capabilities_connection_grants ORDER BY connection")]


def test_import_writes_the_declared_and_used_connections_and_skips_the_rest(lab):
    _offer_global(lab, {"shared": {}, "hidden": {}, "elsewhere": {"enabled": True}})
    _grant_in_envelope(lab, shared={"enabled": True})
    before = _ok(_coolify(lab, "connections"))
    result = _import(lab)
    report = result["capabilities"]["coolify"]
    assert report["connections"]["imported"] == ["box", "off", "shared", "spare"]
    assert report["grants"]["imported"] == ["box", "off", "shared", "spare"]
    assert result["skipped"] == {"coolify": ["elsewhere", "hidden"]}
    assert _stored_connections(lab) == ["box", "off", "shared", "spare"]
    assert _without_sources(_ok(_coolify(lab, "connections"))) == _without_sources(before)


def test_import_writes_a_config_home_default_of_an_enabled_capability(lab):
    registry = lab["envelope"] / "coolify" / "connections.json"
    body = json.loads(registry.read_text())
    body.pop("default")
    registry.write_text(json.dumps(body))
    _offer_global(lab, {"shared": {}, "hidden": {}}, default="shared")
    result = _import(lab)
    assert result["skipped"] == {"coolify": ["hidden"]}
    assert _stored_connections(lab) == ["box", "off", "shared", "spare"]
    assert _rows(lab, "SELECT connection, enabled, is_default FROM "
                      "capabilities_connection_grants WHERE connection = 'shared'") \
        == [("shared", None, True)]
    assert _manager(lab, "get", "coolify", "setting", "connection.default").stdout.strip() \
        == "shared"


def test_import_skips_config_home_connections_of_a_capability_the_project_disables(lab):
    (lab["envelope"] / "settings.json").write_text(
        json.dumps({"capabilities": {"coolify": {"enabled": False}}}))
    _offer_global(lab, {"shared": {}, "hidden": {}})
    _grant_in_envelope(lab, shared={"enabled": True})
    result = _import(lab)
    assert result["skipped"] == {"coolify": ["hidden", "shared"]}
    assert _stored_connections(lab) == ["box", "off", "spare"]
    assert _stored_grants(lab) == ["box", "off", "spare"]


def test_a_second_import_with_config_home_connections_changes_nothing(lab):
    _offer_global(lab, {"shared": {}, "hidden": {}})
    _grant_in_envelope(lab, shared={"enabled": True})
    _import(lab)
    rows = _rows(lab, "SELECT * FROM capabilities_connections ORDER BY name")
    grants = _rows(lab, "SELECT * FROM capabilities_connection_grants ORDER BY connection")
    again = _import(lab)
    assert again["capabilities"]["coolify"]["connections"] == {
        "imported": [], "unchanged": ["box", "off", "shared", "spare"]}
    assert again["capabilities"]["coolify"]["grants"] == {
        "imported": [], "unchanged": ["box", "off", "shared", "spare"]}
    assert again["skipped"] == {"coolify": ["hidden"]}
    assert _rows(lab, "SELECT * FROM capabilities_connections ORDER BY name") == rows
    assert _rows(lab, "SELECT * FROM capabilities_connection_grants "
                      "ORDER BY connection") == grants


def test_import_keeps_a_config_home_row_an_earlier_import_wrote(lab):
    _offer_global(lab, {"hidden": {}})
    _import(lab)
    spec = {"base_url": lab["url"], "secret_env": "COOLIFY_TOKEN"}
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("SET search_path TO lab")
        conn.execute("INSERT INTO capabilities_connections (capability, name, spec) "
                     "VALUES ('coolify', 'hidden', %s::jsonb)", (json.dumps(spec),))
    again = _import(lab)
    assert again["capabilities"]["coolify"]["connections"]["unchanged"] == [
        "box", "hidden", "off", "spare"]
    assert again["skipped"] == {"coolify": ["hidden"]}
    assert _stored_connections(lab) == ["box", "hidden", "off", "spare"]


def test_a_store_mode_project_takes_up_a_config_home_connection_with_set(lab):
    _offer_global(lab, {"hidden": {}})
    assert _import(lab)["skipped"] == {"coolify": ["hidden"]}
    spec = json.loads((lab["config"] / "coolify" / "connections.json")
                      .read_text())["connections"]["hidden"]
    _ok(_manager(lab, "set", "coolify", "connection", "hidden", json.dumps(spec)))
    _ok(_manager(lab, "set", "coolify", "grant", "hidden", '{"enabled": true}'))
    _ok(_coolify(lab, "--connection", "hidden", "resources"))
    assert ("GET", "/api/v1/resources") in lab["seen"]


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


def test_a_store_connection_another_project_is_granted_is_not_removed(lab):
    _import(lab)
    other = "prj_" + "9f8e7d6c5b4a"
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("SET search_path TO lab")
        conn.execute("INSERT INTO capabilities_connection_grants "
                     "(agent_id, capability, connection, node, enabled) "
                     "VALUES (%s, 'coolify', 'spare', 'elsewhere', true)", (other,))
    _ok(_manager(lab, "set", "coolify", "grant", "spare", '{"enabled": false}'))
    refused = _coolify(lab, "connection", "remove", "spare")
    assert refused.returncode == 6, refused.stdout + refused.stderr
    error = _error(refused)
    assert error["code"] == "connection_granted"
    assert other in error["message"] and PROJECT_ID not in error["message"]
    assert "spare" in _stored_connections(lab)
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("SET search_path TO lab")
        conn.execute("UPDATE capabilities_connection_grants SET enabled = false "
                     "WHERE agent_id = %s", (other,))
    removed = _ok(_coolify(lab, "connection", "remove", "spare"))
    assert removed["action"] == "remove"
    assert "spare" not in _stored_connections(lab)


def test_a_store_connection_is_written_over_only_with_replace(lab):
    _import(lab)
    refused = _coolify(lab, "connection", "add", "box", "--field", f"base_url={lab['url']}/x")
    assert refused.returncode == 6 and _error(refused)["code"] == "connection_exists"
    spec = _rows(lab, "SELECT spec FROM capabilities_connections WHERE name = 'box'")[0][0]
    assert spec["base_url"] == lab["url"]
    _ok(_coolify(lab, "connection", "add", "box", "--replace",
                 "--field", f"base_url={lab['url']}/x"))
    spec = _rows(lab, "SELECT spec FROM capabilities_connections WHERE name = 'box'")[0][0]
    assert spec["base_url"] == lab["url"] + "/x"


def test_a_store_connection_added_in_the_project_is_its_own_and_granted(lab):
    _import(lab)
    added = _ok(_coolify(lab, "connection", "add", "fresh", "--project",
                         "--field", f"base_url={lab['url']}",
                         "--env", "secret_env=COOLIFY_TOKEN"))
    assert added["granted"] is True and "hint" not in added
    assert _rows(lab, "SELECT owner FROM capabilities_connections WHERE name = 'fresh'") \
        == [(PROJECT_ID,)]
    assert _rows(lab, "SELECT agent_id, node, enabled FROM capabilities_connection_grants "
                      "WHERE connection = 'fresh'") == [(PROJECT_ID, None, True)]
    replaced = _ok(_coolify(lab, "connection", "add", "box", "--replace",
                            "--field", f"base_url={lab['url']}"))
    assert replaced["granted"] is True and "hint" not in replaced
    assert _rows(lab, "SELECT owner FROM capabilities_connections WHERE name = 'box'") \
        == [(None,)]
    removed = _ok(_coolify(lab, "connection", "remove", "fresh"))
    assert removed["action"] == "remove" and "fresh" not in _stored_connections(lab)


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
    node = _here(lab)
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


def test_a_node_handed_its_id_is_matched_by_it_and_not_the_one_it_resolves(lab):
    declared = {"AGENT_NODE_ID": NODE}
    result = _ok(_manager(lab, "connections", "import", extra=declared))
    assert result["node"] == NODE != _here(lab)
    _ok(_manager(lab, "set", "coolify", "grant", "box", '{"allow_write": false}'))
    _ok(_manager(lab, "set", "coolify", "grant", "box",
                 json.dumps({"node": NODE, "allow_write": True})))
    assert _ok(_manager(lab, "get", "coolify", "grant", "box", extra=declared)) == {
        "allow_write": True, "node": NODE, "is_default": False}
    _ok(_coolify(lab, "deploy", "x", extra=declared))
    # The host name names another node now, so the grant for every node applies.
    assert _error(_coolify(lab, "deploy", "x"))["code"] == "read_only"


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


# --- identifiers and the gate --------------------------------------------------

NODE = "6c61622d6e6f"  # its label lab-node, as hex
BOX = "626f782d6e6f"  # box-node


def _global_files(lab) -> None:
    """A global identifier and a global gate entry in the config home, beside
    the project's own."""
    (lab["config"] / "coolify").mkdir(parents=True, exist_ok=True)
    (lab["config"] / "coolify" / "identifiers.json").write_text(json.dumps(
        {"region": {"value": "eu", "note": "where it runs"},
         "team": {"value": "global-team", "note": ""}}))
    (lab["config"] / "capabilities").mkdir(parents=True, exist_ok=True)
    (lab["config"] / "capabilities" / "settings.json").write_text(json.dumps(
        {"capabilities": {"lab-other": {"enabled": True}}}))


def test_import_moves_identifiers_and_the_gate_and_they_resolve_the_same(lab):
    _global_files(lab)
    _ok(_coolify(lab, "ids", "set", "team", "one", "--note", "the team"))
    before = _ok(_coolify(lab, "ids", "list"))
    assert before == {
        "region": {"value": "eu", "note": "where it runs", "scope": "global"},
        "team": {"value": "one", "note": "the team", "scope": "project"}}
    listed_before = _ok(_manager(lab, "list"))
    result = _import(lab)
    assert result["identifiers"]["coolify"] == {
        "project": {"imported": ["team"], "unchanged": []},
        "global": {"imported": ["region", "team"], "unchanged": []}}
    assert result["policy"] == {"project": {"imported": ["coolify"], "unchanged": []},
                                "global": {"imported": ["lab-other"], "unchanged": []}}
    assert set(result["store"]["tables"]) >= {"capabilities_identifiers",
                                             "capabilities_policy"}
    assert _rows(lab, "SELECT agent_id, label, value, note FROM capabilities_identifiers "
                      "ORDER BY agent_id NULLS FIRST, label") == [
        (None, "region", "eu", "where it runs"), (None, "team", "global-team", ""),
        (PROJECT_ID, "team", "one", "the team")]
    assert _rows(lab, "SELECT agent_id, capability, node, enabled FROM capabilities_policy "
                      "ORDER BY agent_id NULLS FIRST") == [
        (None, "lab-other", None, True), (PROJECT_ID, "coolify", None, True)]
    assert _ok(_coolify(lab, "ids", "list")) == before
    assert _ok(_manager(lab, "list")) == listed_before
    # The files are kept and no longer read for these records.
    (lab["envelope"] / "coolify" / "identifiers.json").write_text(json.dumps(
        {"team": {"value": "changed-in-files", "note": ""}}))
    (lab["envelope"] / "settings.json").write_text(json.dumps(
        {"capabilities": {"coolify": {"enabled": False}}}))
    assert _text(_coolify(lab, "ids", "get", "team")) == "one"
    _ok(_coolify(lab, "deploy", "x"))
    # Files that now say otherwise than the store are a conflict, not an update.
    refused = _manager(lab, "connections", "import")
    assert refused.returncode == 6 and _error(refused)["code"] == "import_conflict"


def test_the_gate_is_written_to_and_read_from_the_store(lab):
    _import(lab)
    settings_before = (lab["envelope"] / "settings.json").read_text()
    disabled = _ok(_manager(lab, "disable", "coolify", "--project"))
    assert disabled["gate"] == "store:lab.capabilities_policy"
    refused = _coolify(lab, "deploy", "x")
    assert refused.returncode == 4
    assert _error(refused)["code"] == "disabled"
    assert "store:lab.capabilities_policy" in _error(refused)["message"]
    assert (lab["envelope"] / "settings.json").read_text() == settings_before
    _ok(_manager(lab, "inherit", "coolify", "--project"))
    assert _error(_coolify(lab, "deploy", "x"))["code"] == "not_enabled"
    _ok(_manager(lab, "enable", "coolify", "--global"))
    assert _rows(lab, "SELECT agent_id, capability, node, enabled "
                      "FROM capabilities_policy") == [(None, "coolify", None, True)]
    _ok(_coolify(lab, "deploy", "x"))
    # A row naming this node is read before the row for every node.
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("INSERT INTO lab.capabilities_policy (agent_id, capability, node, "
                     "enabled) VALUES (NULL, 'coolify', %s, false)", (NODE,))
        conn.execute("INSERT INTO lab.capabilities_policy (agent_id, capability, node, "
                     "enabled) VALUES (%s, 'coolify', 'another-node', false)",
                     (PROJECT_ID,))
    declared = {"AGENT_NODE_ID": NODE}
    assert _error(_coolify(lab, "deploy", "x", extra=declared))["code"] == "not_enabled"
    _ok(_coolify(lab, "deploy", "x"))
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("INSERT INTO lab.capabilities_policy (agent_id, capability, node, "
                     "enabled) VALUES (%s, 'coolify', %s, true)", (PROJECT_ID, NODE))
    _ok(_coolify(lab, "deploy", "x", extra=declared))


def test_a_policy_row_is_written_and_removed_for_one_node(lab):
    # Files carry no node, so before the import a row for one node is refused.
    refused = _manager(lab, "enable", "coolify", "--project", "--node", NODE)
    assert refused.returncode == 6 and _error(refused)["code"] == "node_requires_store"
    _import(lab)
    declared = {"AGENT_NODE_ID": NODE}
    disabled = _ok(_manager(lab, "disable", "coolify", "--project", "--node", NODE))
    assert {k: disabled[k] for k in ("disabled", "scope", "node", "gate")} == {
        "disabled": "coolify", "scope": "project", "node": NODE,
        "gate": "store:lab.capabilities_policy"}
    assert _rows(lab, "SELECT agent_id, capability, node, enabled FROM capabilities_policy "
                      "WHERE capability = 'coolify' ORDER BY node NULLS FIRST") == [
        (PROJECT_ID, "coolify", None, True), (PROJECT_ID, "coolify", NODE, False)]
    # The row naming this node answers before the row for every node.
    assert _error(_coolify(lab, "deploy", "x", extra=declared))["code"] == "disabled"
    _ok(_coolify(lab, "deploy", "x"))
    _ok(_manager(lab, "enable", "coolify", "--global", "--node", "another-node"))
    removed = _ok(_manager(lab, "inherit", "coolify", "--project", "--node", NODE))
    assert removed["removed"] is True and removed["node"] == NODE
    assert _rows(lab, "SELECT agent_id, node, enabled FROM capabilities_policy "
                      "WHERE capability = 'coolify' ORDER BY agent_id NULLS FIRST") == [
        (None, "another-node", True), (PROJECT_ID, None, True)]
    _ok(_coolify(lab, "deploy", "x", extra=declared))
    assert _ok(_manager(lab, "inherit", "coolify", "--project", "--node", NODE))[
        "removed"] is False
    deny = _manager(lab, "disable", "coolify", "--project", "--node", NODE, "--deny")
    assert deny.returncode == 6 and "--deny takes no --node" in _error(deny)["message"]


def test_a_service_moves_between_nodes_and_its_capability_stays_usable_on_both(lab):
    # The defect: moving a service off a node turned its capability off there.
    # Placing the service and making the capability usable are answers apart.
    _import(lab)
    registry = Path(lab["env"]["CAPABILITIES_HOME"]) / "coolify"
    registry.mkdir(parents=True)
    (registry / "coolify").write_text("#!/bin/sh\n")

    def declare(placement: str) -> None:
        (registry / "manifest.json").write_text(json.dumps({"name": "coolify", "service": {
            "name": "svc", "summary": "s", "verbs": [], "placement": placement}}))

    def rows():
        return _rows(lab, "SELECT node, enabled, service FROM capabilities_policy "
                          "WHERE capability = 'coolify' ORDER BY node NULLS FIRST")

    here, there = {"AGENT_NODE_ID": NODE}, {"AGENT_NODE_ID": BOX}
    declare("exclusive")
    _ok(_manager(lab, "enable", "coolify", "--project", "--node", NODE))
    assert rows() == [(None, True, False), (NODE, True, True)]
    moved = _ok(_manager(lab, "place", "coolify", "--node", BOX))
    assert {k: moved[k] for k in ("placed", "node", "placement", "changed", "placed_on",
                                  "unplaced_from", "enabled_on_node")} == {
        "placed": "coolify", "node": BOX, "placement": "exclusive", "changed": True,
        "placed_on": [BOX], "unplaced_from": [NODE], "enabled_on_node": True}
    assert rows() == [(None, True, False), (BOX, True, True), (NODE, True, False)]
    _ok(_coolify(lab, "deploy", "x", extra=here))
    _ok(_coolify(lab, "deploy", "x", extra=there))
    assert _ok(_manager(lab, "place", "coolify", "--node", BOX))["changed"] is False
    # A shared service is placed beside the nodes it runs on.
    declare("shared")
    shared = _ok(_manager(lab, "place", "coolify", "--node", NODE))
    assert shared["placed_on"] == [BOX, NODE] and "unplaced_from" not in shared
    unplaced = _ok(_manager(lab, "unplace", "coolify", "--node", NODE))
    assert unplaced["placed_on"] == [BOX]
    assert rows() == [(None, True, False), (BOX, True, True), (NODE, True, False)]
    _ok(_coolify(lab, "deploy", "x", extra=here))
    # disable --node and inherit --node say when they take the service off.
    disabled = _ok(_manager(lab, "disable", "coolify", "--project", "--node", BOX))
    assert disabled["unplaced"] == BOX and "place" in disabled["warning"]
    refused = _manager(lab, "place", "coolify", "--node", BOX)
    assert refused.returncode == 6 and _error(refused)["code"] == "disabled_on_node"
    _ok(_manager(lab, "enable", "coolify", "--project", "--node", BOX))
    inherited = _ok(_manager(lab, "inherit", "coolify", "--project", "--node", BOX))
    assert inherited["unplaced"] == BOX and "warning" in inherited
    assert "unplaced" not in _ok(_manager(lab, "inherit", "coolify", "--project",
                                          "--node", NODE))
    (registry / "manifest.json").write_text(json.dumps({"name": "coolify"}))
    assert _error(_manager(lab, "place", "coolify", "--node", NODE))["code"] == "no_service"


def test_a_shared_service_is_placed_on_every_node_and_a_move_shows_until_the_lease_follows(
        lab):
    _import(lab)
    registry = Path(lab["env"]["CAPABILITIES_HOME"]) / "coolify"
    registry.mkdir(parents=True)
    (registry / "coolify").write_text("#!/bin/sh\n")

    def declare(placement: str) -> None:
        (registry / "manifest.json").write_text(json.dumps({"name": "coolify", "service": {
            "name": "svc", "summary": "s", "verbs": [], "placement": placement}}))

    def placed() -> list[tuple]:
        return _rows(lab, "SELECT node FROM capabilities_policy WHERE capability = 'coolify' "
                          "AND service ORDER BY node")

    def sql(query: str) -> None:
        with lab["cluster"].admin(lab["database"]) as conn:
            conn.execute("SET search_path TO lab")
            conn.execute(query)

    here = {"AGENT_NODE_ID": NODE}
    declare("shared")
    assert _error(_manager(lab, "place", "coolify", "--all", extra=here))["code"] == "no_nodes"
    _ok(_manager(lab, "node", "add", NODE, "--label", "lab-node", "--kind", "mac", "--master",
                 extra=here))
    _ok(_manager(lab, "node", "add", BOX, "--label", "box-node", "--kind", "server",
                 extra=here))
    # An agent on several nodes keeps its records in the store, under its
    # nodes' write authority.
    sql("UPDATE capabilities_agents SET migrated_at = now()")
    both = _manager(lab, "place", "coolify", "--all", "--node", NODE, extra=here)
    assert both.returncode == 6 and _error(both)["code"] == "input"

    # A node that would refuse refuses the whole command, and nothing is written.
    _ok(_manager(lab, "disable", "coolify", "--project", "--node", BOX, extra=here))
    refused = _manager(lab, "place", "coolify", "--all", extra=here)
    assert refused.returncode == 6 and _error(refused)["code"] == "disabled_on_node"
    assert [r["node"] for r in _error(refused)["detail"]["refused"]] == [BOX]
    assert placed() == []
    off_master = _manager(lab, "place", "coolify", "--all", extra={"AGENT_NODE_ID": BOX})
    assert off_master.returncode != 0 and placed() == []
    assert {r["node"] for r in _error(off_master)["detail"]["refused"]} == {BOX, NODE}
    _ok(_manager(lab, "inherit", "coolify", "--project", "--node", BOX, extra=here))

    # A write that fails part-way names the nodes it placed; running it again finishes.
    sql("CREATE FUNCTION lab.refuse_lab_node() RETURNS trigger LANGUAGE plpgsql AS "
        f"$$ BEGIN IF NEW.node = '{NODE}' THEN RAISE EXCEPTION 'refused'; END IF; "
        "RETURN NEW; END $$; CREATE TRIGGER refuse_lab_node BEFORE INSERT OR UPDATE "
        "ON lab.capabilities_policy FOR EACH ROW EXECUTE FUNCTION lab.refuse_lab_node()")
    partial = _manager(lab, "place", "coolify", "--all", extra=here)
    assert partial.returncode != 0
    assert _error(partial)["detail"] == {"placed": [BOX], "failed": NODE}
    assert placed() == [(BOX,)]
    sql("DROP TRIGGER refuse_lab_node ON lab.capabilities_policy")
    every = _ok(_manager(lab, "place", "coolify", "--all", extra=here))
    assert {k: every[k] for k in ("placed", "nodes", "placement", "changed", "placed_on",
                                  "enabled_on_nodes")} == {
        "placed": "coolify", "nodes": [BOX, NODE], "placement": "shared",
        "changed": True, "placed_on": [BOX, NODE], "enabled_on_nodes": [NODE]}
    again = _ok(_manager(lab, "place", "coolify", "--all", extra=here))
    assert again["changed"] is False and "enabled_on_nodes" not in again
    report = _ok(_manager(lab, "place", "coolify", extra=here))
    assert {k: report[k] for k in ("placement", "placed_on", "holder", "lease", "agrees")} == {
        "placement": "shared", "placed_on": [BOX, NODE], "holder": None,
        "lease": None, "agrees": None}

    # An exclusive service moves with --node; the report agrees once the lease follows.
    declare("exclusive")
    exclusive = _manager(lab, "place", "coolify", "--all", extra=here)
    assert exclusive.returncode == 6 and _error(exclusive)["code"] == "placement_exclusive"
    _ok(_manager(lab, "place", "coolify", "--node", BOX, extra=here))
    report = _ok(_manager(lab, "place", "coolify", extra=here))
    assert (report["placed_on"], report["holder"], report["lease"], report["agrees"]) == (
        [BOX], None, None, False)
    sql("CREATE TABLE lab.deployment_service_leases (project text NOT NULL, "
        "capability text NOT NULL, node text NOT NULL, acquired_at timestamptz NOT "
        "NULL DEFAULT now(), renewed_at timestamptz NOT NULL DEFAULT now(), expires_at "
        "timestamptz NOT NULL, PRIMARY KEY (project, capability)); "
        "INSERT INTO lab.deployment_service_leases (project, capability, node, "
        f"expires_at) VALUES ('{PROJECT_ID}', 'coolify', '{NODE}', "
        "now() + interval '3 minutes')")
    moving = _ok(_manager(lab, "place", "coolify", extra=here))
    assert (moving["holder"], moving["lease"]["live"], moving["agrees"]) == (NODE, True, False)
    sql(f"UPDATE lab.deployment_service_leases SET node = '{BOX}'")
    assert _ok(_manager(lab, "place", "coolify", extra=here))["agrees"] is True
    sql("UPDATE lab.deployment_service_leases SET expires_at = now() - interval '1 minute'")
    expired = _ok(_manager(lab, "place", "coolify", extra=here))
    assert (expired["holder"], expired["lease"]["live"], expired["agrees"]) == (
        None, False, False)

    # The report writes nothing, so the read-only switch lets it through.
    read_only = {**here, "CAPABILITIES_READ_ONLY": "1"}
    assert _ok(_manager(lab, "place", "coolify", extra=read_only))["placed_on"] == [BOX]
    assert _manager(lab, "place", "coolify", "--all", extra=read_only).returncode == 4
    assert _manager(lab, "place", "coolify", "--node", NODE, extra=read_only).returncode == 4


def test_a_writer_that_predates_the_service_column_places_where_it_enables(lab):
    # A manager on an older library writes enabled alone, meaning what
    # enable --node and disable --node always meant: the service follows.
    _import(lab)

    def write(node: str, enabled: bool) -> tuple:
        return _rows(lab, (
            "INSERT INTO capabilities_policy (agent_id, capability, node, enabled) "
            f"VALUES ('{PROJECT_ID}', 'coolify', '{node}', {enabled}) ON CONFLICT "
            "((coalesce(agent_id, '')), capability, (coalesce(node, ''))) "
            "DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now() "
            "RETURNING node, enabled, service"))[0]

    assert write(NODE, True) == (NODE, True, True)
    assert write(NODE, False) == (NODE, False, False)
    assert write(NODE, True) == (NODE, True, True)
    assert _rows(lab, "INSERT INTO capabilities_policy (agent_id, capability, enabled) "
                      f"VALUES ('{PROJECT_ID}', 'slack', true) RETURNING service") == [
        (False,)]


def test_a_store_mode_project_marked_only_by_project_json_gets_no_file_gate(lab):
    # A project whose records are in the store keeps its gate in the store, so
    # project.json alone marks it and no write brings back a file gate.
    _import(lab)
    (lab["envelope"] / "settings.json").unlink()
    gate = lab["envelope"] / "settings.json"
    enabled = _ok(_manager(lab, "enable", "coolify", "--project"))
    assert enabled["gate"] == "store:lab.capabilities_policy"
    assert not gate.exists()
    _ok(_manager(lab, "enable", "coolify", "--project", "--node", NODE))
    assert not gate.exists()
    _ok(_manager(lab, "disable", "coolify", "--project"))
    assert not gate.exists()
    _ok(_manager(lab, "inherit", "coolify", "--project", "--node", NODE))
    assert not gate.exists()
    assert _rows(lab, "SELECT agent_id, capability, node, enabled FROM capabilities_policy "
                      "WHERE capability = 'coolify' ORDER BY node NULLS FIRST") == [
        (PROJECT_ID, "coolify", None, False)]
    assert _error(_coolify(lab, "deploy", "x"))["code"] == "disabled"
    _ok(_manager(lab, "enable", "coolify", "--project"))
    _ok(_coolify(lab, "deploy", "x"))
    _ok(_manager(lab, "init"))
    assert not gate.exists()


def test_ids_set_global_writes_a_global_row_beneath_the_project_row(lab):
    _import(lab)
    assert _ok(_coolify(lab, "ids", "set", "shared", "g", "--global")) == {
        "set": "shared", "scope": "global"}
    assert _ok(_coolify(lab, "ids", "list"))["shared"] == {
        "value": "g", "note": "", "scope": "global"}
    _ok(_coolify(lab, "ids", "set", "shared", "p"))
    assert _rows(lab, "SELECT agent_id, value FROM capabilities_identifiers "
                      "WHERE label = 'shared' ORDER BY agent_id NULLS FIRST") == [
        (None, "g"), (PROJECT_ID, "p")]
    assert _text(_coolify(lab, "ids", "get", "shared")) == "p"
    _ok(_coolify(lab, "ids", "rm", "shared"))
    assert _text(_coolify(lab, "ids", "get", "shared")) == "g"
    _ok(_coolify(lab, "ids", "rm", "shared", "--global"))
    assert _coolify(lab, "ids", "get", "shared").returncode == 3
    assert _rows(lab, "SELECT count(*) FROM capabilities_identifiers") == [(0,)]


def test_ids_set_global_in_files_mode_writes_the_config_home(lab):
    _ok(_coolify(lab, "ids", "set", "shared", "g", "--global", "--note", "n"))
    held = json.loads((lab["config"] / "coolify" / "identifiers.json").read_text())
    assert held == {"shared": {"value": "g", "note": "n"}}
    assert not (lab["envelope"] / "coolify" / "identifiers.json").exists()
    assert _ok(_coolify(lab, "ids", "list"))["shared"]["scope"] == "global"
    assert _tables(lab) == []


def test_a_project_with_only_its_connections_in_the_store_keeps_the_rest_in_files(lab):
    _ok(_coolify(lab, "ids", "set", "team", "one"))
    _import(lab)
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("DELETE FROM lab.capabilities_identifiers")
        conn.execute("DELETE FROM lab.capabilities_policy")
    identity_file = lab["envelope"] / "project.json"
    identity = json.loads(identity_file.read_text())
    identity.pop("records")
    identity["connections"] = "store"
    identity_file.write_text(json.dumps(identity))
    # Connections from the store, the gate and identifiers from files.
    report = _ok(_coolify(lab, "connections"))
    assert report["connections"]["box"]["keys"][0]["source"] == \
        "store:lab.capabilities_connections"
    assert _text(_coolify(lab, "ids", "get", "team")) == "one"
    _ok(_coolify(lab, "deploy", "x"))
    assert _ok(_manager(lab, "disable", "coolify", "--project"))["gate"].endswith(
        "capabilities/settings.json")
    assert _error(_coolify(lab, "deploy", "x"))["code"] == "disabled"
    _ok(_manager(lab, "enable", "coolify", "--project"))
    again = _import(lab)
    assert again["previous_mode"] == "connections"
    assert again["identifiers"]["coolify"]["project"]["imported"] == ["team"]
    identity = json.loads(identity_file.read_text())
    assert identity["records"] == "store" and "connections" not in identity


def test_import_refuses_store_rows_that_would_change_what_the_project_resolves(lab):
    _import(lab)
    held = _rows(lab, "SELECT count(*) FROM capabilities_identifiers")
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("INSERT INTO lab.capabilities_identifiers (agent_id, capability, "
                     "label, value) VALUES (NULL, 'coolify', 'stray', '\"x\"')")
        conn.execute("INSERT INTO lab.capabilities_policy (agent_id, capability, "
                     "enabled) VALUES (NULL, 'lab-stray', true)")
    refused = _manager(lab, "connections", "import")
    assert refused.returncode == 6, refused.stdout + refused.stderr
    error = _error(refused)
    assert error["code"] == "import_conflict"
    assert "global identifier 'stray'" in error["message"]
    assert "lab-stray global policy" in error["message"]
    assert _rows(lab, "SELECT count(*) FROM capabilities_identifiers") == [(held[0][0] + 1,)]


def test_a_store_holding_the_gate_that_cannot_be_reached_refuses_with_exit_5(lab):
    _import(lab)
    closed = _free_port()
    (lab["project"] / ".env").write_text(
        f"AGENTKIT_DB_URL={lab['cluster'].url(lab['database'], port=closed)}\n"
        "AGENTKIT_DB_SCHEMA=lab\nCOOLIFY_TOKEN=test-token-1234\n")
    for result in (_coolify(lab, "deploy", "x"), _coolify(lab, "ids", "list"),
                   _manager(lab, "enable", "coolify", "--project")):
        assert result.returncode == 5, result.stdout + result.stderr
        assert _error(result)["code"] == "store_unreachable"


# --- the connections the records reader holds ---------------------------------------

def test_the_records_reader_reads_on_the_one_working_connection(lab, monkeypatch):
    """Each read borrows the process's working connection and gives it back;
    what was read is served from the adapter's cache, the adapter holds no
    connection between reads, and the process keeps one, idle, for the next."""
    _import(lab)
    monkeypatch.setenv("AGENTKIT_DB_URL", lab["cluster"].url(lab["database"]))
    monkeypatch.setenv("AGENTKIT_DB_SCHEMA", "lab")
    opened = []
    from capabilities_contract import db
    real = db.connect

    def counted(**kwargs):
        opened.append(kwargs.get("transaction"))
        return real(**kwargs)

    monkeypatch.setattr(db, "connect", counted)
    records = S.open_records(lab["envelope"], lab["config"] / "coolify")
    try:
        first = records.connections("coolify")
        assert records._conn is None
        assert records.connections("coolify") == first  # from the cache
        assert records.mode_of("connection") == "store"
        assert records._conn is None
        assert opened and all(kind is True for kind in opened)
        with lab["cluster"].admin(lab["database"]) as admin:
            assert admin.execute(
                "SELECT state, count(*) FROM pg_stat_activity WHERE datname = %s "
                "AND pid <> pg_backend_pid() GROUP BY state",
                (lab["database"],)).fetchall() == [("idle", 1)]
    finally:
        records.close()
        db.disconnect()


def test_the_records_reader_reaches_the_store_at_its_transaction_address(lab):
    """The store's address names a port nothing answers on and its transaction
    address the store itself: a capability resolves its connections from the
    store, so none of its connections went to the address."""
    _import(lab)
    before = _ok(_coolify(lab, "connections"))
    dead = _free_port()
    (lab["project"] / ".env").write_text(
        f"AGENTKIT_DB_URL={lab['cluster'].url(lab['database'], port=dead)}\n"
        f"AGENTKIT_DB_TRANSACTION_PORT={lab['cluster'].port}\n"
        "AGENTKIT_DB_SCHEMA=lab\nCOOLIFY_TOKEN=test-token-1234\n")
    after = _ok(_coolify(lab, "connections"))
    assert after == before
    assert _ok(_manager(lab, "get", "coolify", "grant", "box"))["enabled"] is True
    # With the transaction address gone too, the store does not answer.
    (lab["project"] / ".env").write_text(
        f"AGENTKIT_DB_URL={lab['cluster'].url(lab['database'])}\n"
        f"AGENTKIT_DB_TRANSACTION_PORT={dead}\n"
        "AGENTKIT_DB_SCHEMA=lab\nCOOLIFY_TOKEN=test-token-1234\n")
    refused = _coolify(lab, "connections")
    assert refused.returncode != 0
    assert "store_unreachable" in refused.stdout + refused.stderr


# --- the store's global rows: no connection is written to a file ----------------

def _outside(lab, exe: Path, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run outside any project, the store named by the environment as a machine's
    setting would name it."""
    elsewhere = lab["project"].parent / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    env = {k: v for k, v in lab["env"].items() if k != "CLAUDE_PROJECT_DIR"}
    env.update({"AGENTKIT_DB_URL": lab["cluster"].url(lab["database"]),
                "AGENTKIT_DB_SCHEMA": "lab", "COOLIFY_TOKEN": "test-token-1234",
                **(extra or {})})
    return subprocess.run([str(exe), *args], cwd=elsewhere, env=env, text=True,
                          capture_output=True, timeout=240)


def _global_rows_in_force(lab) -> None:
    """The store holds the rows every agent inherits, as `migrate --global` leaves it."""
    applied = _ok(_outside(lab, MANAGER, "migrate", "--global", "--apply"))
    assert applied["global_migrated_at"]


def _add_machine(lab, cid: str, *more: str) -> dict:
    return _ok(_outside(lab, COOLIFY, "connection", "add", cid, "--machine",
                        "--field", f"base_url={lab['url']}", "--env",
                        "secret_env=COOLIFY_TOKEN", *more))


def test_outside_a_project_a_machine_connection_is_a_store_row_once_the_global_rows_are_in_force(lab):
    registry = lab["config"] / "coolify" / "connections.json"
    before = _add_machine(lab, "early")
    assert before["registry"] == str(registry) and registry.is_file()
    registry.unlink()
    _global_rows_in_force(lab)
    added = _add_machine(lab, "mach")
    assert added["registry"] == "store:lab.capabilities_connections"
    assert not registry.exists()
    assert _rows(lab, "SELECT name, owner, spec FROM capabilities_connections "
                      "WHERE name = 'mach'") == [
        ("mach", None, {"base_url": lab["url"], "secret_env": "COOLIFY_TOKEN"})]
    listed = _ok(_outside(lab, COOLIFY, "connections"))
    assert listed["connections"]["mach"]["keys"][0]["source"] == \
        "store:lab.capabilities_connections"
    refused = _outside(lab, COOLIFY, "connection", "add", "mach", "--machine",
                       "--field", f"base_url={lab['url']}/x")
    assert _error(refused)["code"] == "connection_exists"
    _ok(_outside(lab, COOLIFY, "connection", "add", "mach", "--machine", "--replace",
                 "--field", f"base_url={lab['url']}/x"))
    assert _rows(lab, "SELECT spec FROM capabilities_connections WHERE name = 'mach'")[0][0][
        "base_url"] == lab["url"] + "/x"
    _ok(_outside(lab, COOLIFY, "connection", "remove", "mach", "--machine"))
    assert _stored_connections(lab) == []
    assert not registry.exists()


def test_a_config_home_file_answers_only_for_a_connection_the_store_holds_no_row_of(lab):
    _global_rows_in_force(lab)
    _add_machine(lab, "mach")
    _offer_global(lab, {"legacy": {}, "mach": {"base_url": "http://stale.invalid"}})
    listed = _ok(_outside(lab, COOLIFY, "connections"))["connections"]
    assert set(listed) == {"legacy", "mach"}
    assert listed["mach"]["keys"][0]["source"] == "store:lab.capabilities_connections"
    assert listed["mach"]["keys"][0]["value"] == lab["url"]
    assert listed["legacy"]["keys"][0]["source"] == str(lab["config"] / "coolify"
                                                        / "connections.json")


def test_outside_a_project_an_agents_own_connection_is_not_the_machines(lab):
    _global_rows_in_force(lab)
    _add_machine(lab, "mach")
    other = "prj_" + "9f8e7d6c5b4a"
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("INSERT INTO lab.capabilities_connections (capability, name, spec, owner) "
                     "VALUES ('coolify', 'theirs', %s::jsonb, %s)",
                     ('{"base_url": "http://theirs.invalid"}', other))
    listed = _ok(_outside(lab, COOLIFY, "connections"))
    assert set(listed["connections"]) == {"mach"}


def test_a_machine_connection_an_agent_is_granted_is_not_removed(lab):
    _global_rows_in_force(lab)
    _add_machine(lab, "mach")
    other = "prj_" + "9f8e7d6c5b4a"
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("INSERT INTO lab.capabilities_connection_grants "
                     "(agent_id, capability, connection, enabled) "
                     "VALUES (%s, 'coolify', 'mach', true)", (other,))
    refused = _outside(lab, COOLIFY, "connection", "remove", "mach", "--machine")
    assert refused.returncode == 6, refused.stdout + refused.stderr
    error = _error(refused)
    assert error["code"] == "connection_granted" and other in error["message"]
    assert _stored_connections(lab) == ["mach"]


def test_a_project_in_files_mode_writes_no_connection_to_a_file_once_the_global_rows_are_in_force(lab):
    _global_rows_in_force(lab)
    registry = lab["envelope"] / "coolify" / "connections.json"
    held = registry.read_text()
    refused = _coolify(lab, "connection", "add", "fresh", "--project",
                       "--field", f"base_url={lab['url']}", "--env",
                       "secret_env=COOLIFY_TOKEN")
    assert refused.returncode == 6, refused.stdout + refused.stderr
    assert _error(refused)["code"] == "connections_in_store"
    refused = _manager(lab, "set", "coolify", "grant", "spare", '{"enabled": false}')
    assert _error(refused)["code"] == "connections_in_store"
    assert registry.read_text() == held
    added = _ok(_coolify(lab, "connection", "add", "shared", "--global",
                         "--field", f"base_url={lab['url']}", "--env",
                         "secret_env=COOLIFY_TOKEN"))
    assert added["registry"] == "store:lab.capabilities_connections"
    assert _stored_connections(lab) == ["shared"]
    assert not (lab["config"] / "coolify" / "connections.json").exists()
    assert set(_ok(_coolify(lab, "connections"))["connections"]) == {"box", "spare"}


def test_a_global_gate_set_outside_a_project_is_a_store_row_once_the_global_rows_are_in_force(lab):
    gate = lab["config"] / "capabilities" / "settings.json"
    _global_rows_in_force(lab)
    held = gate.read_text() if gate.exists() else None
    enabled = _ok(_outside(lab, MANAGER, "enable", "coolify", "--global"))
    assert enabled["gate"] == "store:lab.capabilities_policy"
    assert _rows(lab, "SELECT agent_id, capability, node, enabled FROM capabilities_policy"
                      " WHERE capability = 'coolify'") == [(None, "coolify", None, True)]
    assert (gate.read_text() if gate.exists() else None) == held
    inherited = _ok(_outside(lab, MANAGER, "inherit", "coolify", "--global"))
    assert inherited["removed"] is True and inherited["gate"] == "store:lab.capabilities_policy"
    assert _rows(lab, "SELECT 1 FROM capabilities_policy WHERE capability = 'coolify'") == []


def test_retire_connections_moves_the_config_home_files_once_the_store_holds_them(lab):
    refused = _outside(lab, MANAGER, "migrate", "--retire-connections")
    assert _error(refused)["code"] == "global_not_migrated"
    _global_rows_in_force(lab)
    _add_machine(lab, "mach")
    home = lab["config"] / "coolify"
    home.mkdir(parents=True, exist_ok=True)
    spec = {"base_url": lab["url"], "secret_env": "COOLIFY_TOKEN"}
    (home / "connections.json").write_text(json.dumps(
        {"default": "mach", "connections": {"mach": {**spec, "allow_write": True},
                                            "lost": {"base_url": "http://lost.invalid"}}}))
    (home / "credentials.env").write_text("COOLIFY_MACH_TOKEN=x\n")
    (home / "account.session").write_text("kept")
    refused = _outside(lab, MANAGER, "migrate", "--retire-connections")
    assert refused.returncode == 6, refused.stdout + refused.stderr
    error = _error(refused)
    assert error["code"] == "connections_not_in_store"
    assert error["detail"]["connections"] == [
        {"capability": "coolify", "connection": "lost", "file": str(home / "connections.json"),
         "reason": "missing"}]
    assert (home / "connections.json").is_file()
    body = json.loads((home / "connections.json").read_text())
    del body["connections"]["lost"]
    (home / "connections.json").write_text(json.dumps(body))
    retired = _ok(_outside(lab, MANAGER, "migrate", "--retire-connections"))
    backup = Path(retired["backup"])
    assert backup.parent == lab["config"] / "capabilities" / "retired"
    assert retired["retired"] == [{"file": str(home / "connections.json"),
                                   "backup": str(backup / "coolify" / "connections.json"),
                                   "connections": 1}]
    assert not (home / "connections.json").exists()
    assert json.loads((backup / "coolify" / "connections.json").read_text()) == body
    assert sorted(p.name for p in home.iterdir()) == ["account.session", "credentials.env"]
    assert "mach" in _ok(_outside(lab, COOLIFY, "connections"))["connections"]


def test_retire_records_moves_the_config_home_gate_and_identifiers_once_the_store_holds_them(lab):
    refused = _outside(lab, MANAGER, "migrate", "--retire-records")
    assert _error(refused)["code"] == "global_not_migrated"
    gate = lab["config"] / "capabilities" / "settings.json"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text(json.dumps({"capabilities": {"coolify": {"enabled": True}}}))
    home = lab["config"] / "coolify"
    home.mkdir(parents=True, exist_ok=True)
    ids = {"identifiers": {"team": {"value": "t-1", "note": "the team"}}}
    (home / "identifiers.json").write_text(json.dumps(ids))
    (home / "credentials.env").write_text("COOLIFY_MACH_TOKEN=x\n")
    _global_rows_in_force(lab)
    ids["identifiers"]["lost"] = {"value": "x"}
    (home / "identifiers.json").write_text(json.dumps(ids))
    refused = _outside(lab, MANAGER, "migrate", "--retire-records")
    assert refused.returncode == 6, refused.stdout + refused.stderr
    error = _error(refused)
    assert error["code"] == "records_not_in_store"
    assert error["detail"]["values"] == [
        {"file": str(home / "identifiers.json"), "capability": "coolify", "key": "lost",
         "reason": "missing"}]
    assert gate.is_file() and (home / "identifiers.json").is_file()
    del ids["identifiers"]["lost"]
    (home / "identifiers.json").write_text(json.dumps(ids))
    refused = _outside(lab, MANAGER, "migrate", "--retire-records",
                       "--root", str(lab["project"]))
    assert _error(refused)["code"] == "agents_unmigrated"
    assert gate.is_file() and (home / "identifiers.json").is_file()
    retired = _ok(_outside(lab, MANAGER, "migrate", "--retire-records"))
    backup = Path(retired["backup"])
    assert backup.parent == lab["config"] / "capabilities" / "retired"
    assert retired["retired"] == [
        {"file": str(gate), "backup": str(backup / "capabilities" / "settings.json")},
        {"file": str(home / "identifiers.json"),
         "backup": str(backup / "coolify" / "identifiers.json")}]
    assert not gate.exists() and not (home / "identifiers.json").exists()
    assert json.loads((backup / "coolify" / "identifiers.json").read_text()) == ids
    assert sorted(p.name for p in home.iterdir()) == ["credentials.env"]
    report = _ok(_outside(lab, MANAGER, "migrate", "--report"))
    assert report["config_home"]["files"] == []


# --- the node's service manager and the service handles ----------------------------

def _service_lab(lab, placement: str = "exclusive", folder: bool = True) -> dict:
    """The lab's agent in the store on this node, NODE, its folder recorded
    unless `folder` is false, as on a box, where no `capabilities init` reaches
    the store, with coolify standing in for a capability that bundles a service."""
    _import(lab)
    registry = Path(lab["env"]["CAPABILITIES_HOME"]) / "coolify"
    registry.mkdir(parents=True)
    (registry / "coolify").write_text("#!/bin/sh\n")
    (registry / "manifest.json").write_text(json.dumps({"name": "coolify", "service": {
        "name": "svc", "summary": "s", "verbs": [], "placement": placement,
        "deploy": {"command": ["coolify", "service", "run"], "restart": "on-failure"}}}))
    here = {"AGENT_NODE_ID": NODE}
    _ok(_manager(lab, "node", "add", NODE, "--label", "lab-node", "--kind", "server",
                 extra=here))
    if folder:
        with lab["cluster"].admin(lab["database"]) as conn:
            conn.execute("UPDATE lab.capabilities_agent_nodes SET folder = %s",
                         (str(lab["project"]),))
    return here


def _instance(lab, node: str, state: str, **columns) -> None:
    """Write the row a runner on `node` writes of coolify's service."""
    row = {"project_id": PROJECT_ID, "capability": "coolify", "node": node,
           "state": state, "started_at": "2026-10-25T10:00:00+00:00", **columns}
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("SET search_path TO lab")
        conn.execute("DELETE FROM capabilities_service_instances WHERE node = %s", (node,))
        conn.execute(f"INSERT INTO capabilities_service_instances ({', '.join(row)}) "
                     f"VALUES ({', '.join(['%s'] * len(row))})", list(row.values()))


def _wait_for(check, seconds: float = 20.0):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.2)
    return check()


def test_node_run_keeps_a_job_per_service_placed_here_and_answers_a_restart(lab, tmp_path):
    here = _service_lab(lab)
    jobs = tmp_path / "jobs"
    calls = tmp_path / "supervisorctl.log"
    # Supervisor stands in as its control client: each call is logged, and a
    # restart starts the service again as the runner would, by moving its row.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "supervisorctl").write_text(f"""#!{sys.executable}
import json, sys
import psycopg
args = sys.argv[1:]
with open({str(calls)!r}, "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[2:3] == ["restart"]:
    with psycopg.connect(host="127.0.0.1", port={lab["cluster"].port}, user="{ADMIN}",
                         dbname="{lab["database"]}", autocommit=True) as conn:
        conn.execute("UPDATE lab.capabilities_service_instances SET started_at = now()")
""")
    (bin_dir / "supervisorctl").chmod(0o755)
    env = {**lab["env"], **here, "SUPERVISOR_SERVER_URL": "unix:///tmp/test.sock",
           "PATH": f"{bin_dir}{os.pathsep}{lab['env']['PATH']}"}

    def logged() -> list[list[str]]:
        return ([json.loads(line) for line in calls.read_text().splitlines()]
                if calls.exists() else [])

    job = jobs / f"coolify.{PROJECT_ID}.conf"
    node_run = subprocess.Popen([str(MANAGER), "node", "run", "--supervisor", str(jobs),
                                 "--every", "30"], cwd=lab["project"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Nothing placed: no job, and the supervisor is still asked once.
        assert _wait_for(lambda: ["-s", "unix:///tmp/test.sock", "update"] in logged())
        assert not job.exists()
        second = subprocess.run([str(MANAGER), "node", "run", "--supervisor", str(jobs)],
                                cwd=lab["project"], env=env, capture_output=True,
                                text=True, timeout=120)
        assert second.returncode == 6 and _error(second)["code"] == "node_run_running"

        # Placed here: a job within seconds, long before the 30s reconcile.
        _ok(_manager(lab, "place", "coolify", "--node", NODE, extra=here))
        assert _wait_for(job.exists, 15)
        text = job.read_text()
        assert f"[program:coolify.{PROJECT_ID}]" in text
        assert (f"node supervise coolify --agent {PROJECT_ID}\n" in text
                and "coolify service run" not in text)
        assert f"directory={lab['project']}\n" in text
        assert _wait_for(lambda: sum(call[2:] == ["update"] for call in logged()) == 2)

        # A restart asked through the store restarts that one program.
        _instance(lab, NODE, "ready")
        restarted = _ok(_manager(lab, "service", "restart", "coolify", "--wait", "20",
                                 extra=here))
        assert restarted["restarted"] == "coolify" and restarted["state"] == "ready"
        assert ["-s", "unix:///tmp/test.sock", "restart", f"coolify.{PROJECT_ID}"] in logged()

        # Placed elsewhere: the job goes.
        _ok(_manager(lab, "node", "add", BOX, "--label", "box-node", "--kind", "server",
                     extra=here))
        _ok(_manager(lab, "place", "coolify", "--node", BOX, extra=here))
        assert _wait_for(lambda: not job.exists(), 15)
        assert _wait_for(lambda: sum(call[2:] == ["update"] for call in logged()) == 3)
    finally:
        node_run.terminate()
        _out, err = node_run.communicate(timeout=60)
    assert node_run.returncode == 0, err
    assert "every job keeps running" in err


def test_node_run_runs_its_own_agents_services_where_the_store_records_no_folder(
        lab, tmp_path):
    here = _service_lab(lab, folder=False)
    assert _rows(lab, "SELECT folder FROM capabilities_agent_nodes") == [(None,)]
    _ok(_manager(lab, "place", "coolify", "--node", NODE, extra=here))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "supervisorctl").write_text("#!/bin/sh\n")
    (bin_dir / "supervisorctl").chmod(0o755)
    env = {**lab["env"], **here, "SUPERVISOR_SERVER_URL": "unix:///tmp/test.sock",
           "PATH": f"{bin_dir}{os.pathsep}{lab['env']['PATH']}"}
    job = tmp_path / "jobs" / f"coolify.{PROJECT_ID}.conf"
    node_run = subprocess.Popen([str(MANAGER), "node", "run", "--supervisor",
                                 str(tmp_path / "jobs"), "--services", "coolify"],
                                cwd=lab["project"], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    try:
        assert _wait_for(job.exists, 15)
        assert f"directory={lab['project']}\n" in job.read_text()
    finally:
        node_run.terminate()
        _out, err = node_run.communicate(timeout=60)
    assert node_run.returncode == 0, err


def test_the_service_handles_place_and_answer_what_the_node_reports(lab):
    here = _service_lab(lab)
    assert _ok(_manager(lab, "service", "status", extra=here))["services"] == []

    # start places it on this node and answers once the node's row is ready.
    _instance(lab, NODE, "ready", payload_sha256="a" * 64)
    started = _ok(_manager(lab, "service", "start", "coolify", "--wait", "5", extra=here))
    assert {k: started[k] for k in ("started", "node", "placement", "placed_on", "ready",
                                    "state")} == {
        "started": "coolify", "node": NODE, "placement": "exclusive",
        "placed_on": [NODE], "ready": True, "state": "ready"}
    assert started["instance"]["payload_sha256"] == "a" * 64

    # A row that says it cannot do its work is the answer, as the error's detail.
    _instance(lab, NODE, "unusable", problem="the session is not signed in",
              problem_code="service_unhealthy")
    unusable = _manager(lab, "service", "start", "coolify", "--wait", "5", extra=here)
    assert unusable.returncode == 6
    error = _error(unusable)
    assert error["code"] == "service_unusable"
    assert "not signed in" in error["message"]
    assert error["detail"]["instance"]["problem_code"] == "service_unhealthy"

    status = _ok(_manager(lab, "service", "status", "coolify", extra=here))
    assert status["services"][0]["capability"] == "coolify"
    assert status["services"][0]["placed_on"] == [NODE]
    assert status["services"][0]["ready_on"] == []
    assert status["services"][0]["instances"][0]["state"] == "unusable"

    # stop unplaces it and waits for the node to give its row up.
    still = _manager(lab, "service", "stop", "coolify", "--wait", "1", extra=here)
    assert still.returncode == 6 and _error(still)["code"] == "service_still_running"
    assert _rows(lab, "SELECT node FROM capabilities_policy WHERE capability = 'coolify' "
                      "AND service") == []
    with lab["cluster"].admin(lab["database"]) as conn:
        conn.execute("DELETE FROM lab.capabilities_service_instances")
    stopped = _ok(_manager(lab, "service", "stop", "coolify", "--wait", "5", extra=here))
    assert stopped["stopped"] == "coolify" and stopped["nodes"] == [NODE]

    # A restart needs a row to restart, and move is for an exclusive service.
    missing = _manager(lab, "service", "restart", "coolify", "--wait", "1", extra=here)
    assert _error(missing)["code"] == "service_not_running"
    registry = Path(lab["env"]["CAPABILITIES_HOME"]) / "coolify" / "manifest.json"
    manifest = json.loads(registry.read_text())
    manifest["service"]["placement"] = "shared"
    registry.write_text(json.dumps(manifest))
    shared = _manager(lab, "service", "move", "coolify", "--node", NODE, extra=here)
    assert shared.returncode == 6 and _error(shared)["code"] == "placement_shared"

    for verb in ("start", "restart"):
        read_only = _manager(lab, "service", verb, "coolify",
                             extra={**here, "CAPABILITIES_READ_ONLY": "1"})
        assert read_only.returncode == 4 and _error(read_only)["code"] == "read_only_switch"
    assert _ok(_manager(lab, "service", "status", extra={**here,
                                                         "CAPABILITIES_READ_ONLY": "1"}))
