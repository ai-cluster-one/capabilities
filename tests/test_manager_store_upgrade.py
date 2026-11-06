#!/usr/bin/env python3
"""A schema major crosses only through `capabilities store upgrade`.

`store upgrade --check` changes nothing and answers, for the store this folder
resolves, each owner's major against what the installed releases read,
whether the upgrade can run and what stops it, and every node the store holds.
`store upgrade` takes the newest line's manager and hands over to it, takes a
dump and proves it, migrates every owner under the upgrade's flag - a
quarantined capability's too - goes on from where it stopped, speaks as
events, and lists the nodes left to bring up. A node last seen before the
store moved reads unreachable, and `node update --node` refuses it.

The major is made here: a capability whose second release marks a step
`major=2`, and a core major written into the store the way a crossing writes
it, or crossed by a newest-line manager whose library carries a core step
marked major. The stores are databases of a throwaway PostgreSQL on a loopback
port, started when `initdb`, `pg_ctl`, `pg_dump` and `pg_restore` are on PATH
and psycopg is importable, and skipped otherwise.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import runpy
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
MANIFEST_REL = ".capability-source/manager-release.json"
ADMIN = "pgadmin"
SCHEMA = "lab"
PROJECT_ID = "prj_" + "5a6b7c8d9e0f"
NODE_OLD, NODE_NEW, NODE_SILENT = "1a1b1c1d1e1f", "2a2b2c2d2e2f", "3a3b3c3d3e3f"
UPGRADE = "capabilities store upgrade"
M = runpy.run_path(str(MANAGER), run_name="manager_store_upgrade_test")


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
    for tool in ("initdb", "pg_ctl", "pg_dump", "pg_restore", "uv"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-store-upgrade-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


# --- a capability whose second release crosses its owner's major ------------------

PROBE = '''#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "capabilities-contract>=0.42,<1",
# ]
# ///
"""probe - keeps one table, moved by its second release across a major."""

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

NAME = "probe"
SUMMARY = "Keeps one table, moved by its second release across a major."
SCOPE = "project"
CRED_KEYS = []
WRITE_VERBS = set()
WRITE_DEFAULT = True
DOCS_BASE = ""
STATE = False
INVENTORY = None
POST_INSTALL: list[dict] = []
CONN_FIELDS: list[dict] = []
TABLES = True

_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
CREDENTIALS_ENV = _CONFIG_HOME / NAME / "credentials.env"
_STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))

from capabilities_contract.capability import bind as _contract_bind
from capabilities_contract.db import Step

_contract_bind(globals(), "core")
_contract_bind(globals(), "store")
_contract_bind(globals(), "connections")

STEPS = [Step("0001-items", "CREATE TABLE probe_items (id bigint PRIMARY KEY, body text)")]
MAJOR = 1
{second}


def _tables():
    stop = os.environ.get("PROBE_STOP_FILE")
    if stop and Path(stop).exists() and sys.argv[1:] == ["migrate"]:
        _die(5, "probe_stopped", "probe's migration was stopped from outside")
    return STEPS, MAJOR, 0


def main() -> None:
    _contract(sys.argv[1:])
    _die(6, "input", "unknown command")


if __name__ == "__main__":
    main()
'''
SECOND = '''STEPS.append(Step("0002-items-moved",
                  "ALTER TABLE probe_items RENAME TO probe_entries", major=2))
MAJOR = 2'''


def _probe(root: Path, second: bool, extra: str = "") -> Path:
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin" / "probe").write_text(
        PROBE.replace("{second}", (SECOND if second else "") + "\n" + extra))
    (root / "bin" / "probe").chmod(0o755)
    (root / "CHANGELOG.md").write_text("# probe - change log\n\n## 2026-11-02 - "
                                       + ("Second" if second else "First") + "\n\nIt.\n")
    return root


# --- one machine with its project, store and nodes ---------------------------------

class Machine:
    def __init__(self, tmp: Path, cluster: Cluster):
        self.tmp, self.cluster = tmp, cluster
        self.database = cluster.database()
        self.url = cluster.url(self.database)
        self.project = tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        (self.project / ".env.local").write_text(
            f"CAPABILITIES_PROJECT_ID={PROJECT_ID}\nAGENTKIT_DB_URL={self.url}\n"
            f"AGENTKIT_DB_SCHEMA={SCHEMA}\n")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.data = tmp / "data"
        self.state = tmp / "state"
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("AGENTKIT_DB_", "CAPABILITIES_"))
                and k not in ("VIRTUAL_ENV", "AGENT_NODE_ID", "AGENT_NODE_NAME",
                              "CLAUDE_PROJECT_DIR")}
        base.update({
            "HOME": str(tmp / "home"), "XDG_CONFIG_HOME": str(tmp / "config"),
            "XDG_CACHE_HOME": str(tmp / "cache"), "XDG_STATE_HOME": str(self.state),
            "XDG_DATA_HOME": str(self.data),
            "CAPABILITIES_HOME": str(tmp / "home" / ".capabilities"),
            "CAPABILITIES_BIN": str(self.bin), "CAPABILITIES_SOURCE": "http://127.0.0.1:9/main",
            "CLAUDE_PROJECT_DIR": str(self.project),
            "AGENTKIT_DB_URL": self.url, "AGENTKIT_DB_SCHEMA": SCHEMA,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"})
        self.base = base

    def env(self, **extra: str) -> dict:
        env = {**self.base, **extra}
        return {k: v for k, v in env.items() if v is not None}

    def run(self, *args: str, exe: Path | None = None, **extra) -> subprocess.CompletedProcess:
        return subprocess.run([str(exe or MANAGER), *args], cwd=self.project,
                              env=self.env(**extra), text=True, capture_output=True,
                              timeout=900)

    def ok(self, *args: str, **extra) -> dict:
        result = self.run(*args, **extra)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    def refused(self, *args: str, **extra) -> tuple[int, dict]:
        result = self.run(*args, **extra)
        line = next(line for line in reversed(result.stderr.splitlines())
                    if line.lstrip().startswith("{"))
        return result.returncode, json.loads(line)["error"]

    def sql(self, query: str, params: tuple = ()):
        with self.cluster.connect(self.database) as conn:
            conn.execute(f"SET search_path TO {SCHEMA}")
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else None

    def books(self):
        return (self.sql("SELECT owner, step, checksum, applied_at FROM schema_ledger "
                         "ORDER BY owner, step"),
                self.sql("SELECT owner, major, minor, updated_at FROM schema_version "
                         "ORDER BY owner"))

    def nodes(self) -> None:
        """Three nodes: one seen a minute ago, one with no state row."""
        for node, label in ((NODE_OLD, "old-node"), (NODE_SILENT, "silent-node")):
            self.ok("node", "add", node, "--label", label, "--kind", "server")
        self.sql("INSERT INTO capabilities_node_states (node, reported_at, fresh_until) "
                 "VALUES (%s, now() - interval '1 minute', now() + interval '1 hour')",
                 (NODE_OLD,))

    def probe(self, quarantined: bool = False) -> Path:
        """probe installed at its first release with a row in its table, then
        its second release taken, whose migration refuses to cross alone."""
        source = _probe(self.tmp / "src" / "probe", second=False)
        installed = self.ok("install", "probe", "--from", str(source), "--allow")
        assert installed["migrate"]["ok"] is True, installed
        if quarantined:
            self.ok("quarantine", "probe")
        self.sql("INSERT INTO probe_items VALUES (1, 'kept')")
        _probe(source, second=True)
        return source


def _events(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def _steps(events: list[dict]) -> list[tuple]:
    return [(e["step"], e.get("owner") or e.get("capability"), e["state"])
            for e in events if e["event"] == "step"]


@pytest.fixture()
def machine(tmp_path, cluster):
    return Machine(tmp_path, cluster)


# --- the check -------------------------------------------------------------------

def test_check_changes_nothing_and_answers_what_the_upgrade_would_do(machine) -> None:
    machine.ok("node", "add", NODE_OLD, "--label", "old-node", "--kind", "server")
    machine.probe()
    # The check remembers what the readers answered; the second release moves
    # what that answer was made of, so the check below asks them again.
    assert machine.ok("store", "upgrade", "--check")["state"] == "current"
    assert list((machine.tmp / "cache" / "capabilities" / "store-upgrade-readers").glob("*"))
    updated = machine.ok("update", "probe")["updated"][0]
    # The second release arrives, and its migration refuses to cross alone,
    # naming the upgrade.
    assert updated["migrate"]["ok"] is False
    assert updated["migrate"]["error"]["code"] == "store_upgrade_needed"
    assert UPGRADE in updated["migrate"]["error"]["hint"]
    before = machine.books()
    # The store is the project's (.env.local), reached by the capabilities the
    # check asks as well.
    checked = machine.ok("store", "upgrade", "--check", AGENTKIT_DB_URL=None,
                         AGENTKIT_DB_SCHEMA=None)
    assert machine.books() == before
    assert checked["state"] == "upgrade_available"
    assert checked["upgrade_needed"] is True and checked["can_run"] is True
    assert checked["blockers"] == [] and checked["next"] == [UPGRADE]
    assert checked["store"]["level"] == "project" and checked["store"]["major"] == 1
    assert checked["store"]["reachable"] is True
    owners = {entry["owner"]: entry for entry in checked["owners"]}
    assert owners["probe"]["by"] == "capability"
    assert owners["probe"]["state"] == "upgrade_needed" and owners["probe"]["crosses"]
    assert owners["probe"]["store"] == {"major": 1, "minor": 0}
    assert owners["probe"]["reads"] == {"major": 2, "minor": 0}
    assert owners["capabilities"]["by"] == "manager"
    assert owners["capabilities"]["state"] in ("current", "pending")
    assert owners["capabilities"]["crosses"] is False
    assert owners["core"]["crosses"] is False
    assert checked["available"]["major"] is None and "error" in checked["available"]
    key = checked["store"]["key"]
    assert checked["backup"]["dir"] == str(machine.data / "capabilities" / "backups" / key)
    assert checked["backup"]["free_bytes"] >= checked["backup"]["needed_bytes"] > 0
    assert checked["tools"]["pg_dump"]["major"] >= checked["store"]["server_major"]
    [node] = checked["nodes"]
    assert node["node"] == NODE_OLD and node["reads_store"] is True
    assert set(node) >= {"label", "kind", "last_seen_at", "reachable", "runs", "command"}
    assert not (machine.data / "capabilities" / "backups").exists()

    # An owner the store still records whose tables are gone, as a retired
    # tool's, has nothing to move and stops nothing.
    machine.sql("INSERT INTO schema_version (owner, major, minor) VALUES ('ghost', 1, 0)")
    assert machine.ok("store", "upgrade", "--check")["blockers"] == []
    # What stops it is in the answer with what to do, and the upgrade refuses.
    machine.sql("CREATE TABLE ghost_items (id int)")
    tools = {Path(shutil.which(t)).parent for t in ("pg_dump", "pg_restore")}
    path = os.pathsep.join(p for p in machine.base["PATH"].split(os.pathsep)
                           if Path(p) not in tools)
    blocked = machine.ok("store", "upgrade", "--check", PATH=path)
    assert blocked["state"] == "blocked" and blocked["can_run"] is False
    codes = {b["code"]: b for b in blocked["blockers"]}
    # A client installed beside PATH, as Homebrew's or Postgres.app's, is found.
    server = blocked["store"]["server_major"]
    if _clients_beside_path():
        assert set(codes) == {"owner_not_installed"}
    elif _fetchable(server):
        # The upgrade fetches its own where it can: a step, not a blocker.
        assert set(codes) == {"owner_not_installed"}
        assert blocked["tools"]["fetch"] == _plan(machine)
    else:
        assert set(codes) == {"owner_not_installed", "pg_dump_missing", "pg_restore_missing"}
        for code in ("pg_dump_missing", "pg_restore_missing"):
            assert f"PostgreSQL {server}" in codes[code]["message"]
            assert f"{server}" in codes[code]["hint"] and "install" in codes[code]["hint"]
    assert codes["owner_not_installed"]["hint"].startswith("install it here")
    assert all(b["hint"] for b in blocked["blockers"])
    before = machine.books()
    code, error = machine.refused("store", "upgrade", PATH=path)
    assert (code, error["code"]) == (6, "store_upgrade_blocked")
    assert machine.books() == before
    assert not (machine.data / "capabilities" / "backups").exists()
    assert not (machine.data / "capabilities" / "pg-client").exists()


def _fetchable(server: int) -> bool:
    """Whether the upgrade fetches its own client on this machine for a
    server of major `server`."""
    return M["_pg_client_pin"]() is not None and server <= M["PG_CLIENT_MAJOR"]


def _plan(machine) -> dict:
    """The client fetch the machine's check names, under its data home."""
    key, pin = M["_pg_client_pin"]()
    version = M["PG_CLIENT_VERSION"]
    return {"version": version, "platform": key,
            "url": M["PG_CLIENT_URL"].format(version=version, platform=key),
            "sha256": pin["sha256"], "bytes": pin["bytes"],
            "dir": str(machine.data / "capabilities" / "pg-client" / f"{version}-{key}")}


def _clients_beside_path() -> bool:
    """Whether this machine has a PostgreSQL client where the manager looks
    beside PATH, which a test that strips PATH cannot take away."""
    return any(shutil.which("pg_dump", path=str(folder)) for folder in M["_pg_tool_dirs"]())


def _older_client(folder: Path, major: int) -> None:
    """A pg_dump and pg_restore of `major` that answer --version and fail at
    anything else, as an older client first on PATH would against the server."""
    folder.mkdir(parents=True)
    for name in ("pg_dump", "pg_restore"):
        tool = folder / name
        tool.write_text(f'#!/bin/sh\nif [ "$1" = --version ]; then echo "{name} (PostgreSQL) '
                        f'{major}.6"; exit 0; fi\necho "{name} {major}.6 cannot" >&2\nexit 1\n')
        tool.chmod(0o755)


def test_a_client_of_the_servers_major_beside_an_older_one_first_on_path_is_used(
    machine,
) -> None:
    machine.probe()
    machine.ok("update", "probe")
    server = machine.ok("store", "upgrade", "--check")["store"]["server_major"]
    # An older client first on PATH, the server's major in a Homebrew keg.
    older = machine.tmp / "older" / "bin"
    _older_client(older, server - 1)
    brew = machine.tmp / "brew"
    keg = brew / "opt" / f"postgresql@{server}" / "bin"
    keg.mkdir(parents=True)
    for name in ("pg_dump", "pg_restore"):
        (keg / name).symlink_to(shutil.which(name))
    tools = {Path(shutil.which(t)).parent for t in ("pg_dump", "pg_restore")}
    path = os.pathsep.join([str(older)] + [p for p in machine.base["PATH"].split(os.pathsep)
                                           if Path(p) not in tools])
    checked = machine.ok("store", "upgrade", "--check", PATH=path, HOMEBREW_PREFIX=str(brew))
    assert checked["can_run"] is True and checked["blockers"] == [], checked["blockers"]
    for name in ("pg_dump", "pg_restore"):
        assert checked["tools"][name]["path"] == str(keg / name)
        assert checked["tools"][name]["major"] == server
    assert checked["tools"]["fetch"] is None
    if not _clients_beside_path():
        # With only the older one, it is named, and so is what to install.
        blocked = machine.ok("store", "upgrade", "--check", PATH=path, HOMEBREW_PREFIX=None)
        codes = {b["code"]: b for b in blocked["blockers"]}
        if _fetchable(server):
            # It fetches its own rather than stopping at the older one.
            assert codes == {} and blocked["tools"]["fetch"] == _plan(machine)
            assert blocked["can_run"] is True
            assert f"fetches the PostgreSQL {M['PG_CLIENT_VERSION']} client" in blocked["message"]
            assert blocked["tools"]["pg_dump"]["path"] == str(older / "pg_dump")
        for name in (() if _fetchable(server) else ("pg_dump", "pg_restore")):
            assert blocked["tools"][name] == {"path": str(older / name),
                                              "version": f"{server - 1}.6",
                                              "major": server - 1}
            assert str(older / name) in codes[f"{name}_too_old"]["message"]
            assert f"PostgreSQL {server}" in codes[f"{name}_too_old"]["message"]
            assert f"{server}" in codes[f"{name}_too_old"]["hint"]
    # The upgrade's dump and its proof run with the chosen tools; the older
    # ones would fail.
    upgraded = machine.run("store", "upgrade", "--events", PATH=path, HOMEBREW_PREFIX=str(brew))
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    dump = next(e for e in _events(upgraded.stdout) if e["event"] == "step"
                and e["step"] == "dump" and e["state"] == "done")["check"]
    assert dump["proven"]["ok"] is True and dump["proven"]["restore_list"] is True
    # A client of the server's major installed here is used and nothing is fetched.
    assert not any(e.get("step") == "client" for e in _events(upgraded.stdout))
    assert not (machine.data / "capabilities" / "pg-client").exists()


def _client_archive(root: Path, major: int) -> Path:
    """A client archive laid out as the pinned builds are: its tools answer
    --version as `major`, a library is a link to the file beside it, and the
    server and an extension ride along."""
    top = root / "build" / "postgresql-x"
    (top / "bin").mkdir(parents=True)
    (top / "lib").mkdir()
    for name in ("pg_dump", "pg_restore", "psql", "postgres"):
        tool = top / "bin" / name
        tool.write_text(f'#!/bin/sh\necho "{name} (PostgreSQL) {major}.6"\n')
        tool.chmod(0o755)
    (top / "lib" / "libpq.so.5.18").write_bytes(b"libpq")
    (top / "lib" / "libpq.so.5").symlink_to("libpq.so.5.18")
    (top / "lib" / "plpgsql.so").write_bytes(b"plpgsql")
    (top / "LICENSE").write_text("licence")
    archive = root / "served" / "client.tar.gz"
    archive.parent.mkdir()
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(top, arcname=top.name)
    return archive


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """A served client archive and the pin the manager's fetch reads for it,
    with the fetched client's folder under `tmp_path`."""
    pytest.importorskip("httpx")
    archive = _client_archive(tmp_path, M["PG_CLIENT_MAJOR"])
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    # The globals the manager's functions read, which runpy's answer copies.
    monkeypatch.setitem(M["_pg_client_fetch"].__globals__, "PG_CLIENT_PINS", {"test-platform": {
        "sha256": digest, "bytes": archive.stat().st_size, "libs": ("libpq.so.5",)}})
    with _serve(archive.parent) as base:
        yield {"version": M["PG_CLIENT_VERSION"], "platform": "test-platform",
               "url": f"{base}/client.tar.gz", "sha256": digest,
               "bytes": archive.stat().st_size, "dir": str(tmp_path / "pg-client" / "x")}


def test_the_fetched_client_keeps_only_the_tools_and_their_libraries(pinned) -> None:
    tools = M["_pg_client_fetch"](pinned)
    dest = Path(pinned["dir"])
    for name in ("pg_dump", "pg_restore", "psql"):
        assert tools[name] == {"path": str(dest / "bin" / name),
                               "version": f"{M['PG_CLIENT_MAJOR']}.6",
                               "major": M["PG_CLIENT_MAJOR"]}
    kept = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*"))
    assert kept == ["bin", "bin/pg_dump", "bin/pg_restore", "bin/psql", "lib",
                    "lib/libpq.so.5", "lib/libpq.so.5.18"]
    assert os.readlink(dest / "lib" / "libpq.so.5") == "libpq.so.5.18"
    assert [p.name for p in dest.parent.iterdir()] == [dest.name]


def test_a_fetched_client_whose_sha256_is_not_the_pin_is_refused_and_not_kept(
    pinned,
) -> None:
    with pytest.raises(M["_UpgradeFailed"]) as refused:
        M["_pg_client_fetch"]({**pinned, "sha256": "0" * 64})
    assert (refused.value.exit_code, refused.value.code) == (6, "pg_client_checksum_mismatch")
    assert pinned["sha256"] in refused.value.message and "0" * 64 in refused.value.message
    assert list(Path(pinned["dir"]).parent.iterdir()) == []


def test_with_nothing_to_cross_it_changes_nothing_and_the_switch_refuses(machine) -> None:
    source = _probe(machine.tmp / "src" / "probe", second=False)
    machine.ok("install", "probe", "--from", str(source), "--allow")
    machine.ok("node", "add", NODE_OLD, "--label", "old-node", "--kind", "server")
    before = machine.books()
    checked = machine.ok("store", "upgrade", "--check")
    assert checked["state"] == "current" and checked["upgrade_needed"] is False
    answer = machine.ok("store", "upgrade")
    assert answer["upgraded"] is False and answer["state"] == "current"
    assert "nothing to cross" in answer["message"]
    assert machine.books() == before
    assert not (machine.data / "capabilities").exists()
    assert not (machine.state / "capabilities" / "store-upgrade").exists()
    code, error = machine.refused("store", "upgrade", CAPABILITIES_READ_ONLY="1")
    assert (code, error["code"]) == (4, "read_only_switch")
    assert machine.ok("store", "upgrade", "--check",
                      CAPABILITIES_READ_ONLY="1")["state"] == "current"


# --- the upgrade -----------------------------------------------------------------

def test_upgrade_dumps_migrates_a_quarantined_capability_resumes_and_lists_nodes(
    machine,
) -> None:
    machine.nodes()
    machine.probe(quarantined=True)
    machine.ok("update", "probe")
    stop = machine.tmp / "stop"
    stop.write_text("")
    first = machine.run("store", "upgrade", "--events", PROBE_STOP_FILE=str(stop))
    assert first.returncode == 5, first.stdout + first.stderr
    events = _events(first.stdout)
    assert _steps(events) == [
        ("check", None, "started"), ("check", None, "done"),
        ("manager", None, "skipped"), ("capabilities", None, "skipped"),
        ("dump", None, "started"), ("dump", None, "done"),
        ("migrate", None, "started"),
        ("migrate", "core", "started"), ("migrate", "core", "done"),
        ("migrate", "capabilities", "started"), ("migrate", "capabilities", "done"),
        ("migrate", "probe", "started"), ("migrate", "probe", "failed"),
        ("migrate", None, "failed")]
    assert events[-1]["event"] == "error" and events[-1]["exit"] == 5
    error = events[-1]["error"]
    assert error["code"] == "migrate_failed" and "probe_stopped" in json.dumps(error)
    assert error["detail"]["resume"] == UPGRADE
    dump = next(e for e in events if e["event"] == "step" and e["step"] == "dump"
                and e["state"] == "done")["check"]
    path = Path(dump["path"])
    assert path.parent.parent == machine.data / "capabilities" / "backups"
    assert path.suffix == ".dump" and path.is_file()
    assert dump["proven"]["ok"] is True and dump["proven"]["restore_list"] is True
    assert dump["proven"]["missing"] == []
    assert dump["proven"]["table_data"] >= dump["proven"]["tables"] > 0
    assert dump["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    listing = subprocess.run(["pg_restore", "--list", str(path)], capture_output=True,
                             text=True, timeout=60).stdout
    assert f"TABLE DATA {SCHEMA} probe_items " in listing
    assert machine.sql("SELECT major FROM schema_version WHERE owner = 'probe'") == [(1,)]
    mtime = path.stat().st_mtime_ns

    stop.unlink()
    second = machine.run("store", "upgrade", "--events", PROBE_STOP_FILE=str(stop))
    assert second.returncode == 0, second.stdout + second.stderr
    events = _events(second.stdout)
    assert _steps(events)[2:] == [
        ("manager", None, "skipped"), ("capabilities", None, "skipped"),
        ("dump", None, "skipped"), ("migrate", None, "started"),
        ("migrate", "core", "skipped"), ("migrate", "capabilities", "skipped"),
        ("migrate", "probe", "started"), ("migrate", "probe", "done"),
        ("migrate", None, "done")]
    assert events[-1]["event"] == "done"
    answer = events[-1]["result"]
    assert answer["upgraded"] is True and answer["resumed"] is True
    assert answer["backup"]["kept"] is True and answer["backup"]["path"] == str(path)
    assert answer["backup"]["proven"]["ok"] is True
    assert path.stat().st_mtime_ns == mtime
    assert len(list(path.parent.glob("*.dump"))) == 1
    owners = {entry["owner"]: entry for entry in answer["owners"]}
    assert owners["probe"]["before"] == {"major": 1, "minor": 0}
    assert owners["probe"]["after"] == {"major": 2, "minor": 0}
    assert owners["probe"]["quarantined"] is True
    assert owners["probe"]["applied"] == ["0002-items-moved"]
    # A quarantined capability's tables moved, rows and all.
    assert machine.sql("SELECT id, body FROM probe_entries") == [(1, "kept")]
    left = {node["node"]: node for node in answer["nodes_to_bring_up"]}
    assert set(left) == {NODE_OLD, NODE_SILENT}
    assert all(node["command"] and node["reason"] for node in left.values())
    assert answer["next"]
    # Nothing is left to cross.
    assert machine.ok("store", "upgrade", "--check")["state"] == "current"


@contextmanager
def _serve(root: Path):
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


# The newest line's manager reads major 2, and its library carries a core step
# that crosses the store's core to it.
NEWEST = '''
import capabilities_contract.db as _store_db
import capabilities_contract.db._migrate as _store_migrate
_store_migrate.CORE_STEPS = (_store_migrate.Step("core-0001-major", "SELECT 1", major=2),)
_store_migrate.CORE_VERSION = (2, 0)
_store_migrate.STORE_MAJOR = _store_db.STORE_MAJOR = 2
# the newest manager
'''


def _manager_release(root: Path, store_major: int, manager: bytes,
                     library: str | None = None) -> None:
    """A manager release in `root`, as the source publishes one, reading
    `store_major` and naming `library` where given."""
    assets = json.loads((REPO / MANIFEST_REL).read_text())["assets"]
    files = {rel: (REPO / rel).read_bytes() for rel in assets}
    for rel, body in {**files, "bin/capabilities": manager}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body)
    (root / "bin" / "capabilities").chmod(0o755)
    manifest = {"schema": "capabilities.manager-release.v1", "store_major": store_major,
                "manager": {"path": "bin/capabilities",
                            "sha256": hashlib.sha256(manager).hexdigest()},
                "assets": {rel: hashlib.sha256(body).hexdigest()
                           for rel, body in files.items()}}
    if library:
        manifest["library"] = library
    (root / MANIFEST_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_REL).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _newest_release(root: Path) -> None:
    text = MANAGER.read_text().replace("\nSTORE_MAJOR = 1\n", "\nSTORE_MAJOR = 2\n", 1)
    head, tail, end = text.rpartition('\nif __name__ == "__main__":')
    _manager_release(root, 2, (head + NEWEST + tail + end).encode())


def _legacy_link(machine) -> Path:
    """The manager on PATH, as a release installed before this one."""
    link = machine.bin / "capabilities"
    legacy = Path(machine.base["CAPABILITIES_HOME"]) / ".manager" / "capabilities"
    legacy.parent.mkdir(parents=True)
    shutil.copy2(MANAGER, legacy)
    link.symlink_to(legacy)
    return link


def test_upgrade_hands_over_to_the_newest_lines_manager_which_crosses_the_core(
    machine,
) -> None:
    link = _legacy_link(machine)
    machine.nodes()
    _newest_release(machine.tmp / "src" / "main")
    with _serve(machine.tmp / "src") as base:
        source = f"{base}/main"
        checked = machine.ok("store", "upgrade", "--check", exe=link,
                             CAPABILITIES_SOURCE=source)
        assert checked["available"]["major"] == 2 and checked["target_major"] == 2
        assert checked["state"] == "upgrade_available"
        result = machine.run("store", "upgrade", "--events", exe=link,
                             CAPABILITIES_SOURCE=source)
    assert result.returncode == 0, result.stdout + result.stderr
    events = _events(result.stdout)
    handover = next(e for e in events if e["event"] == "handover")
    assert link.resolve() == Path(handover["manager"]).resolve()
    assert link.resolve().read_text().count("# the newest manager") == 1
    steps = _steps(events)
    assert ("manager", None, "done") in steps
    # The new manager carries on: it finds the manager phase done.
    after = steps[steps.index(("manager", None, "done")) + 1:]
    assert after[:3] == [("check", None, "started"), ("check", None, "done"),
                         ("manager", None, "skipped")]
    assert ("capabilities", None, "done") in after and ("dump", None, "done") in after
    assert ("migrate", "core", "done") in after
    answer = events[-1]["result"]
    assert answer["major"] == {"before": 1, "after": 2}
    assert answer["manager"]["release"] == handover["release"]
    assert machine.sql("SELECT major FROM schema_version WHERE owner = 'core'") == [(2,)]
    left = {node["node"] for node in answer["nodes_to_bring_up"]}
    assert left == {NODE_OLD, NODE_SILENT}
    # The node seen before the store moved reads unreachable, and takes no request.
    state = machine.ok("node", "state", exe=link)
    old = next(node for node in state["nodes"] if node["node"] == NODE_OLD)
    assert old["reachable"] is False and old["health"] == "red"
    assert old["cannot_read_store"]["store_major"] == 2
    code, error = machine.refused("node", "update", "--node", "old-node", exe=link)
    assert (code, error["code"]) == (6, "node_cannot_read_store")


# --- nodes against a store that moved ---------------------------------------------

def test_a_node_seen_before_the_crossing_reads_unreachable_and_takes_no_request(
    machine,
) -> None:
    machine.nodes()
    machine.ok("node", "add", NODE_NEW, "--label", "new-node", "--kind", "server")
    # The core crosses to major 2 the way the upgrade writes it, after the old
    # node last reported; the new node reports after.
    machine.sql("INSERT INTO schema_version (owner, major, minor) VALUES ('core', 2, 0)")
    machine.sql("INSERT INTO capabilities_node_states (node, reported_at, fresh_until) "
                "VALUES (%s, now() + interval '1 second', now() + interval '1 hour')",
                (NODE_NEW,))
    state = {node["node"]: node for node in machine.ok("node", "state")["nodes"]}
    old, new = state[NODE_OLD], state[NODE_NEW]
    assert new["reachable"] is True and new["cannot_read_store"] is None
    assert old["reachable"] is False and old["health"] == "red"
    unread = old["cannot_read_store"]
    assert unread["store_major"] == 2 and unread["since"] and unread["last_seen_at"]
    assert "schema major 2" in unread["message"] and unread["command"]
    assert state[NODE_SILENT]["reachable"] is False

    code, error = machine.refused("node", "update", "--node", "old-node")
    assert (code, error["code"]) == (6, "node_cannot_read_store")
    assert error["hint"].startswith("bring it up first")
    assert machine.refused("node", "update", "--node", "silent-node")[1]["code"] \
        == "node_cannot_read_store"
    assert machine.sql("SELECT count(*) FROM capabilities_node_updates") == [(0,)]
    asked = machine.ok("node", "update", "--node", "new-node")
    assert asked["ok"] is True and asked["requested"]["state"] == "requested"

    checked = machine.ok("store", "upgrade", "--check")
    nodes = {node["node"]: node for node in checked["nodes"]}
    assert nodes[NODE_OLD]["reads_store"] is False and nodes[NODE_OLD]["reason"]
    assert nodes[NODE_NEW]["reads_store"] is True


# --- the commands reach the person --------------------------------------------------

def test_a_store_refusal_keeps_the_commands_it_names() -> None:
    hint = "the store moved: run `capabilities self-update`, then `capabilities update`"
    said = M["_node_update_error"](json.dumps({"error": {
        "code": "schema_too_new", "message": "the core tables are at 2.0", "hint": hint}}), 6)
    assert said.endswith(hint)
    assert M["_upgrade_message"]("store_upgrade_needed", "it crosses", f"run `{UPGRADE}`") \
        == f"it crosses; run `{UPGRADE}`"
    assert M["_upgrade_message"]("store_busy", "busy", "wait") == "busy"

    class Refusal(Exception):
        slug, message, hint = "store_upgrade_needed", "the core crosses", f"run `{UPGRADE}`"

    class Runner:
        here = NODE_OLD
        said: dict = {}
        lines: list = []
        say_once = M["_NodeManager"].say_once

        def say(self, message):
            self.lines.append(message)

        def _connect(self):
            raise Refusal()

    runner = Runner()
    assert M["_NodeManager"].update_requested(runner) is False
    assert M["_NodeManager"].update_requested(runner) is False
    assert runner.lines == [f"cannot read whether an update is asked: the core crosses; "
                            f"run `{UPGRADE}`"]


def test_the_guide_is_served() -> None:
    proc = subprocess.run([str(MANAGER), "guide", "store-upgrade"], capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (REPO / "guides" / "store-upgrade.md").read_text()
    menu = json.loads(subprocess.run([str(MANAGER), "guide"], capture_output=True,
                                     text=True, timeout=120).stdout)
    assert "store-upgrade" in [entry["topic"] for entry in menu]


# --- a kept line holds what it runs on; a node at another major says nothing -------

def _uv_logging_syncs(machine) -> Path:
    """A uv on PATH that logs every contract-library sync and passes every call on."""
    real = shutil.which("uv", path=machine.base["PATH"])
    root = machine.tmp / "logging-uv"
    root.mkdir()
    (root / "uv").write_text(f"""#!/bin/sh
case " $* " in
  *" sync "*"--upgrade-package capabilities-contract"*) echo "$*" >> "{root}/log";;
esac
exec "{real}" "$@"
""")
    (root / "uv").chmod(0o755)
    machine.base["PATH"] = str(root) + os.pathsep + machine.base["PATH"]
    return root / "log"


def test_a_lines_library_range_holds_the_managers_own_environment(machine) -> None:
    link = _legacy_link(machine)
    machine.ok("node", "add", NODE_OLD, "--label", "old-node", "--kind", "server")
    _newest_release(machine.tmp / "src" / "main")
    _manager_release(machine.tmp / "src" / "store-major-1", 1,
                     MANAGER.read_bytes() + b"\n# the line manager\n", ">=0.42,<0.50")
    log = _uv_logging_syncs(machine)
    with _serve(machine.tmp / "src") as base:
        result = machine.ok("self-update", exe=link, CAPABILITIES_SOURCE=f"{base}/main")
    assert result["store_major"]["taken"] == "line"
    assert result["library"] == ">=0.42,<0.50"
    assert link.resolve().read_text().rstrip().endswith("# the line manager")
    synced = [line for line in log.read_text().splitlines()
              if f"--script {result['manager']} " in f"{line} "]
    assert synced and all("--upgrade-package capabilities-contract>=0.42,<0.50" in line
                          for line in synced)


def _publish(root: Path, tag: str, store_major: int) -> None:
    """A catalogue tree in `root` carrying resend marked `tag`."""
    script = REPO / "capabilities" / "resend" / "bin" / "resend"
    target = root / "capabilities" / "resend" / "bin" / "resend"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(script.read_bytes() + f"\n# {tag}\n".encode())
    (root / ".capability-source").mkdir(parents=True, exist_ok=True)
    (root / ".capability-source" / "catalog.json").write_text(json.dumps(
        {"capabilities": {"resend": {"path": "capabilities/resend",
                                     "payload_sha256": "0" * 64,
                                     "store_major": store_major}},
         "store_major": store_major}))


def test_install_takes_the_stores_line_and_the_newest_without_a_store(machine) -> None:
    machine.ok("node", "add", NODE_OLD, "--label", "old-node", "--kind", "server")
    _publish(machine.tmp / "src" / "main", "main", 2)
    _publish(machine.tmp / "src" / "store-major-1", "line", 1)
    installed = Path(machine.base["CAPABILITIES_HOME"]) / "resend"

    def tag() -> str:
        return (installed / "resend").read_text().rstrip().splitlines()[-1]
    with _serve(machine.tmp / "src") as base:
        canonical = f"{base}/main/capabilities/resend/bin/resend"
        answer = machine.ok("install", "resend", CAPABILITIES_SOURCE=f"{base}/main")
        assert tag() == "# line"
        assert answer["store_major"]["taken"] == "line"
        assert answer["store_major"]["command"] == UPGRADE
        assert json.loads((installed / "meta.json").read_text())["source"] == canonical
        machine.ok("uninstall", "resend")
        # No store: the newest.
        elsewhere = machine.tmp / "elsewhere"
        elsewhere.mkdir()
        result = subprocess.run(
            [str(MANAGER), "install", "resend"], cwd=elsewhere, text=True,
            capture_output=True, timeout=600,
            env=machine.env(CAPABILITIES_SOURCE=f"{base}/main", AGENTKIT_DB_URL=None,
                            AGENTKIT_DB_SCHEMA=None, CLAUDE_PROJECT_DIR=None))
        assert result.returncode == 0, result.stderr
        assert "store_major" not in json.loads(result.stdout)
        assert tag() == "# main"


def test_a_manager_at_another_major_writes_nothing_that_says_its_node_reads_the_store(
    machine,
) -> None:
    machine.nodes()
    # The store crosses to major 2; this manager reads major 1.
    machine.sql("INSERT INTO schema_version (owner, major, minor) VALUES ('core', 2, 0)")

    def written():
        return (machine.sql("SELECT reported_at, fresh_until FROM capabilities_node_states"
                            " WHERE node = %s", (NODE_OLD,)),
                machine.sql("SELECT last_seen_at FROM capabilities_nodes WHERE id = %s",
                            (NODE_OLD,)),
                machine.sql("SELECT count(*) FROM capabilities_node_updates"))
    before = written()
    for verb in (("node", "state", "report"), ("node", "seen"), ("node", "update")):
        code, error = machine.refused(*verb, AGENT_NODE_ID=NODE_OLD)
        assert (code, error["code"]) == (6, "schema_too_new"), verb
        assert "capabilities self-update" in error["hint"]
    assert written() == before
    # The node's manager loop says so once and asks nothing of the store.
    db = M["_db_module"]()

    class Runner:
        root, setting, migrated, here = machine.project, None, False, NODE_OLD
        store = M["_store_module"]()
        said: dict = {}
        lines: list = []
        say_once = M["_NodeManager"].say_once
        _connect = M["_NodeManager"]._connect

        def say(self, message):
            self.lines.append(message)
    Runner.db = db
    env = {"AGENTKIT_DB_URL": machine.url, "AGENTKIT_DB_SCHEMA": SCHEMA}
    held = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        runner = Runner()
        with pytest.raises(db.DbError) as refused:
            runner._connect()
        assert refused.value.slug == "schema_too_new"
        assert M["_NodeManager"].update_requested(runner) is False
    finally:
        for k, v in held.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    assert runner.lines and "capabilities self-update" in runner.lines[0]
    assert written() == before
    # So the node stays unreachable, and takes no request.
    old = next(n for n in machine.ok("node", "state")["nodes"] if n["node"] == NODE_OLD)
    assert old["reachable"] is False and old["cannot_read_store"]["store_major"] == 2
    assert machine.refused("node", "update", "--node", "old-node")[1]["code"] \
        == "node_cannot_read_store"


def test_a_store_at_another_major_is_refused_with_the_commands_not_read_as_disabled(
    machine,
) -> None:
    machine.ok("install", "resend", "--from", str(REPO / "capabilities" / "resend"), "--allow")
    machine.ok("enable", "resend", "--project")
    assert machine.ok("list")["installed"][0]["name"] == "resend"
    machine.sql("INSERT INTO schema_version (owner, major, minor) VALUES ('core', 2, 0)")
    code, error = machine.refused("list")
    assert (code, error["code"]) == (6, "schema_too_new")
    assert "capabilities self-update" in error["hint"]
    doctor = machine.run("doctor")
    assert doctor.returncode == 7
    findings = json.loads(doctor.stdout)["findings"]
    assert any("schema_too_new" in f and "capabilities self-update" in f for f in findings)
    assert machine.ok("store", "upgrade", "--check")["state"] == "behind"


# --- a further owner a capability migrates ----------------------------------------

# A library's table probe migrates before its own, as tasks migrates the harness
# runner's; its second release moves that owner across a major.
LIBRARY_OWNER = '''
LIBRARY_STEPS = [Step("0001-profiles", "CREATE TABLE harness_profiles (id bigint PRIMARY KEY)")]
LIBRARY_MAJOR = {major}
if LIBRARY_MAJOR == 2:
    LIBRARY_STEPS.append(Step("0002-profiles-moved",
                              "ALTER TABLE harness_profiles ADD COLUMN body text", major=2))


def _owner_tables():
    return [("harness", LIBRARY_STEPS, LIBRARY_MAJOR, 0)]
'''


def test_an_owner_a_capability_migrates_for_a_library_is_its_own_to_cross(machine) -> None:
    source = _probe(machine.tmp / "src" / "probe", second=False,
                    extra=LIBRARY_OWNER.format(major=1))
    installed = machine.ok("install", "probe", "--from", str(source), "--allow")
    assert installed["migrate"]["ok"] is True, installed
    manifest = json.loads((Path(machine.base["CAPABILITIES_HOME"]) / "probe"
                           / "manifest.json").read_text())
    assert manifest["owners"] == ["harness", "probe"]
    assert machine.sql("SELECT major FROM schema_version WHERE owner = 'harness'") == [(1,)]
    # Nothing to cross, and the store's harness tables are no stranger here.
    checked = machine.ok("store", "upgrade", "--check")
    assert checked["state"] == "current" and checked["blockers"] == []
    # The second release moves harness across a major; probe's own stays.
    _probe(source, second=False, extra=LIBRARY_OWNER.format(major=2))
    machine.ok("update", "probe")
    checked = machine.ok("store", "upgrade", "--check")
    assert checked["state"] == "upgrade_available", checked["blockers"]
    owners = {entry["owner"]: entry for entry in checked["owners"]}
    harness = owners["harness"]
    assert (harness["by"], harness["capability"], harness["further"]) \
        == ("capability", "probe", True)
    assert harness["store"] == {"major": 1, "minor": 0}
    assert harness["reads"] == {"major": 2, "minor": 0}
    assert harness["state"] == "upgrade_needed" and harness["crosses"] is True
    assert owners["probe"]["crosses"] is False
    result = machine.run("store", "upgrade", "--events")
    assert result.returncode == 0, result.stdout + result.stderr
    events = _events(result.stdout)
    migrated = [step for step in _steps(events) if step[0] == "migrate" and step[1]]
    assert migrated[-4:] == [("migrate", "harness", "started"), ("migrate", "probe", "started"),
                             ("migrate", "harness", "done"), ("migrate", "probe", "done")]
    answer = {entry["owner"]: entry for entry in events[-1]["result"]["owners"]}
    assert answer["harness"]["after"] == {"major": 2, "minor": 0}
    assert answer["harness"]["by"] == "probe"
    assert answer["harness"]["applied"] == ["0002-profiles-moved"]
    assert machine.ok("store", "upgrade", "--check")["state"] == "current"


def test_tasks_declares_the_harness_tables_so_a_store_holding_them_is_not_blocked(
    machine,
) -> None:
    installed = machine.ok("install", "tasks", "--from", str(REPO / "capabilities" / "tasks"),
                           "--allow")
    assert installed["migrate"]["ok"] is True, installed
    manifest = json.loads((Path(machine.base["CAPABILITIES_HOME"]) / "tasks"
                           / "manifest.json").read_text())
    assert manifest["owners"] == ["harness", "tasks"]
    assert {row[0] for row in machine.sql("SELECT owner FROM schema_version")} \
        >= {"harness", "tasks"}
    checked = machine.ok("store", "upgrade", "--check")
    assert checked["state"] == "current" and checked["blockers"] == []
    owners = {entry["owner"]: entry for entry in checked["owners"]}
    assert owners["harness"]["capability"] == "tasks" and owners["harness"]["further"]
    assert owners["harness"]["reads"]["major"] == owners["harness"]["store"]["major"]
