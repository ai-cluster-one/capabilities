"""Archiving an agent: `agent archive` checks from the store alone that nothing
of the agent runs and marks it, stopping nothing itself; `--check` answers the
same without writing; `agent restore` clears the mark; an archived agent leaves
`agent list` and is refused what would start something for it; and
`service stop --agent` stops a service of an agent with no folder here.

These tests drive the real manager against a throwaway PostgreSQL on a loopback
port, which the module starts when `initdb` and `pg_ctl` are on PATH and
psycopg is importable, and skips otherwise. Each test gets a database of its own.
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
ADMIN = "pgadmin"
SCHEMA = "kernel"
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")
PROJECT_ID = "prj_" + "0a1b2c3d4e5f"
# The markers an agent harness or a conveyor turn sets in its commands'
# environment; the lab strips them, so a run inside one tests the same.
MARKERS = ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CODEX_THREAD_ID", "TASKS_EXECUTION")
PLAIN, BOXED, PLACED, TURNING, SYSTEM = ("a1a1a1a1a1a1", "b2b2b2b2b2b2", "c3c3c3c3c3c3",
                                         "d4d4d4d4d4d4", "e5e5e5e5e5e5")


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
    root = Path(tempfile.mkdtemp(prefix="capabilities-archive-pg-"))
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


def _seed(lab: Lab) -> None:
    """Five agents: one with nothing, one on a server of its own (and a Mac and a
    shared server, which never block), one with a placed service holding a live
    lease, one with a turn in flight and a stale one, and a system agent."""
    lab.ok("agent", "show", cwd=lab.project)  # the core, migrated
    lab.sql("INSERT INTO agents (id, slug, name) VALUES (%s, 'plain', 'Plain'), "
            "(%s, 'boxed', 'Boxed'), (%s, 'placed', 'Placed'), (%s, 'turning', 'Turning'), "
            "(%s, 'keeper', 'Keeper')", (PLAIN, BOXED, PLACED, TURNING, SYSTEM))
    lab.sql("INSERT INTO nodes (id, label, kind) VALUES (%s, 'box-own', 'server'), "
            "(%s, 'box-shared', 'server'), (%s, 'mac-me', 'mac'), (%s, 'box-off', 'server')",
            (nid("box-ow"), nid("box-sh"), nid("mac-me"), nid("box-of")))
    lab.sql("UPDATE nodes SET disabled_at = now() WHERE id = %s", (nid("box-of"),))
    lab.sql("INSERT INTO agent_nodes (agent_id, node, is_master) VALUES "
            "(%s, %s, true), (%s, %s, false), (%s, %s, false), (%s, %s, false), "
            "(%s, %s, true), (%s, %s, true), (%s, %s, false), (%s, %s, true)",
            (BOXED, nid("box-ow"), BOXED, nid("mac-me"), BOXED, nid("box-sh"),
             BOXED, nid("box-of"), PLACED, nid("box-sh"), PLAIN, nid("mac-me"),
             TURNING, nid("mac-me"), SYSTEM, nid("mac-me")))
    lab.sql("UPDATE agents SET kind = 'system' WHERE id = %s", (SYSTEM,))
    lab.sql("INSERT INTO policies (agent_id, capability, node, enabled, service) "
            "VALUES (%s, 'tasks', %s, true, true)", (PLACED, nid("box-sh")))
    lab.sql("UPDATE policies SET mode = 'own' WHERE agent_id = %s", (PLACED,))
    lab.sql("INSERT INTO service_leases (agent_id, capability, node, mode, expires_at) "
            "VALUES (%s, 'tasks', %s, 'own', now() + interval '3 minutes')",
            (PLACED, nid("box-sh")))
    lab.sql("INSERT INTO service_instances (agent_id, capability, node, state) "
            "VALUES (%s, 'tasks', %s, 'ready')", (PLACED, nid("box-sh")))
    # agentrooms' turn table, as far as the check reads it.
    lab.sql("CREATE TABLE agentrooms_turns (id text PRIMARY KEY, agent_id text, node text, "
            "outcome text, started_at timestamptz NOT NULL DEFAULT now())")
    lab.sql("INSERT INTO agentrooms_turns (id, agent_id, outcome, started_at) VALUES "
            "('turn-now', %s, NULL, now() - interval '2 minutes'), "
            "('turn-stale', %s, NULL, now() - interval '3 days'), "
            "('turn-done', %s, 'answered', now())", (TURNING, TURNING, TURNING))


def _check(lab: Lab, ref: str, **kw) -> dict:
    return lab.ok("agent", "archive", ref, "--check", "--json", **kw)


def test_the_check_names_what_runs_from_the_store_alone_and_writes_nothing(lab):
    _seed(lab)
    before = lab.sql("SELECT id, archived_at FROM agents ORDER BY id")

    plain = _check(lab, "plain")
    assert plain["agent"] == {"id": PLAIN, "name": "Plain", "slug": "plain", "kind": "agent",
                              "archived_at": None, "archived_by": None}
    assert (plain["can_archive"], plain["blocking"]) == (True, [])
    assert plain["checked_at"]

    # Only the server it lives on alone blocks: not the Mac, the shared server or
    # the disabled one.
    boxed = _check(lab, BOXED)
    assert boxed["can_archive"] is False
    [node] = boxed["blocking"]
    assert node == {
        "kind": "node", "state": "in_service",
        "node": {"id": nid("box-ow"), "label": "box-own", "kind": "server",
                 "sole_agent": True, "last_seen_at": None},
        "stop": {"argv": ["capabilities", "node", "disable", nid("box-ow")]},
        "remove": {"argv": ["capabilities", "node", "remove", nid("box-ow")]}}

    placed = _check(lab, "placed")
    [service] = placed["blocking"]
    assert service["instance"]["state"] == "ready"
    assert {k: service[k] for k in ("kind", "capability", "mode", "placed", "state")} == {
        "kind": "service", "capability": "tasks", "mode": "own", "placed": True,
        "state": "running"}
    assert service["lease_expires_at"]
    assert service["node"]["sole_agent"] is False and service["node"]["kind"] == "server"
    assert service["stop"] == {"argv": ["capabilities", "service", "stop", "tasks", "--agent",
                                        PLACED, "--node", nid("box-sh")]}

    # A turn in flight blocks; one started days ago, whose holder went away, never.
    turning = _check(lab, "turning")
    assert [(w["kind"], w["capability"], w["id"], w["state"], w["node"], w["stop"])
            for w in turning["blocking"]] == [
        ("work", "agentrooms", "turn-now", "in_flight", None, None)]

    # An instance row alone never makes a row; once the turn ends nothing blocks.
    lab.sql("INSERT INTO service_instances (agent_id, capability, node, state) "
            "VALUES (%s, 'tasks', %s, 'ready')", (TURNING, nid("mac-me")))
    lab.sql("UPDATE agentrooms_turns SET outcome = 'answered' WHERE id = 'turn-now'")
    assert _check(lab, "turning")["can_archive"] is True

    code, error = lab.refused("agent", "archive", "keeper", "--check")
    assert (code, error["code"]) == (4, "system_agent")
    code, error = lab.refused("agent", "archive", "nobody", "--check")
    assert (code, error["code"]) == (6, "agent_unknown")
    # Read under the read-only switch too, and nothing was written.
    assert _check(lab, "plain", CAPABILITIES_READ_ONLY="1")["can_archive"] is True
    assert lab.sql("SELECT id, archived_at FROM agents ORDER BY id") == before


def test_archive_marks_once_nothing_runs_and_restore_clears_only_the_mark(lab):
    _seed(lab)
    code, error = lab.refused("agent", "archive", "placed")
    assert (code, error["code"]) == (6, "agent_live")
    assert error["detail"]["blocking"][0]["kind"] == "service"
    assert set(error["detail"]) == {"agent", "can_archive", "blocking", "checked_at"}
    code, error = lab.refused("agent", "archive", "boxed")
    assert (code, error["code"]) == (6, "agent_live")
    code, error = lab.refused("agent", "archive", "keeper")
    assert (code, error["code"]) == (4, "system_agent")
    code, error = lab.refused("agent", "archive", "plain", CAPABILITIES_READ_ONLY="1")
    assert (code, error["code"]) == (4, "read_only_switch")
    assert lab.sql("SELECT count(*) FROM agents WHERE archived_at IS NOT NULL") == [(0,)]

    done = lab.ok("agent", "archive", "plain")
    assert (done["archived"], done["archived_by"], done["changed"]) == (
        PLAIN, f"tester@{nid('mac-me')}", True)
    again = lab.ok("agent", "archive", PLAIN)
    assert (again["changed"], again["archived_at"]) == (False, done["archived_at"])

    listed = [a["id"] for a in lab.ok("agent", "list")["agents"]]
    assert PLAIN not in listed and PLACED in listed
    [archived] = lab.ok("agent", "list", "--archived")["agents"]
    assert archived == {"id": PLAIN, "name": "Plain", "slug": "plain", "has_icon": False,
                        "archived_at": archived["archived_at"],
                        "archived_by": f"tester@{nid('mac-me')}", "folder": None}
    state = lab.ok("agent", "state", "plain")
    assert state["archived_at"] and state["archived_by"] == f"tester@{nid('mac-me')}"
    assert lab.ok("agent", "state", "placed")["archived_at"] is None
    assert _check(lab, "plain")["agent"]["archived_at"]

    # The store refuses to start anything for it.
    import psycopg
    with pytest.raises(psycopg.errors.IntegrityError) as refused:
        lab.sql("INSERT INTO service_leases (agent_id, capability, node, mode, expires_at) "
                "VALUES (%s, 'tasks', %s, 'own', now() + interval '1 minute')",
                (PLAIN, nid("mac-me")))
    assert refused.value.sqlstate == "23R01"

    code, error = lab.refused("agent", "restore", "plain", CAPABILITIES_READ_ONLY="1")
    assert (code, error["code"]) == (4, "read_only_switch")
    assert lab.ok("agent", "restore", "plain") == {"restored": PLAIN, "changed": True}
    assert lab.ok("agent", "restore", "plain") == {"restored": PLAIN, "changed": False}
    assert PLAIN in [a["id"] for a in lab.ok("agent", "list")["agents"]]
    assert lab.ok("agent", "list", "--archived")["agents"] == []
    # Restore started nothing: the agent's link and records are as they were.
    assert lab.sql("SELECT node FROM agent_nodes WHERE agent_id = %s", (PLAIN,)) == [
        (nid("mac-me"),)]
    assert lab.sql("SELECT count(*) FROM policies WHERE agent_id = %s", (PLAIN,)) == [(0,)]


def test_service_stop_names_an_agent_with_no_folder_here_and_then_it_can_be_archived(lab):
    _seed(lab)
    argv = _check(lab, "placed")["blocking"][0]["stop"]["argv"]
    assert argv[:3] == ["capabilities", "service", "stop"]
    code, error = lab.refused(*argv[1:], "--wait", "0", CAPABILITIES_READ_ONLY="1")
    assert (code, error["code"]) == (4, "read_only_switch")
    code, error = lab.refused("service", "stop", "tasks", "--agent", "keeper", "--wait", "0")
    assert (code, error["code"]) == (4, "system_agent")
    # Its node still reports it, so the stop is unplaced and waits.
    code, error = lab.refused(*argv[1:], "--wait", "1")
    assert (code, error["code"]) == (6, "service_still_running")
    assert error["detail"]["changed"] is True and error["detail"]["placed_on"] == []
    assert lab.sql("SELECT service FROM policies WHERE agent_id = %s", (PLACED,)) == [
        (False,)]
    # The node's supervisor stops it and drops its row: stop answers at once.
    lab.sql("DELETE FROM service_instances WHERE agent_id = %s", (PLACED,))
    stopped = lab.ok(*argv[1:], "--wait", "5")
    assert {k: stopped[k] for k in ("stopped", "agent_id", "nodes", "changed")} == {
        "stopped": "tasks", "agent_id": PLACED, "nodes": [nid("box-sh")], "changed": False}
    # Its lease runs out by itself; until then the row says so.
    [row] = _check(lab, "placed")["blocking"]
    assert (row["placed"], row["state"], row["instance"]) == (False, "lease_ending", None)
    lab.sql("UPDATE service_leases SET expires_at = now() - interval '1 second' "
            "WHERE agent_id = %s", (PLACED,))
    assert lab.ok("agent", "archive", "placed")["changed"] is True


def test_an_archived_agent_is_refused_what_would_start_something_for_it(lab, tmp_path):
    lab.ok("node", "add", nid("mac-me"), "--label", "mac-me", "--kind", "mac",
           cwd=lab.project)
    assert lab.ok("agent", "archive", PROJECT_ID, cwd=lab.project)["changed"] is True
    shown = lab.ok("agent", "show", cwd=lab.project)
    assert shown["archived_at"] and shown["archived_by"] == f"tester@{nid('mac-me')}"
    again = tmp_path / "again"
    again.mkdir()
    (again / ".env.local").write_text(lab.store_lines())
    for args in (("place", "tasks", "--node", nid("mac-me")),
                 ("place", "tasks", "--all"),
                 ("service", "start", "tasks", "--node", nid("mac-me"), "--wait", "0"),
                 ("node", "add", nid("box-new"), "--label", "box-new", "--kind", "server"),
                 ("agent", "materialize", PROJECT_ID, str(tmp_path / "again"))):
        code, error = lab.refused(*args, cwd=lab.project)
        assert (code, error["code"]) == (4, "agent_archived"), (args, error)
    assert (again / ".env.local").read_text() == lab.store_lines()
    assert lab.sql("SELECT count(*) FROM agent_nodes WHERE agent_id = %s",
                   (PROJECT_ID,)) == [(1,)]
    # Reading and stopping stay open.
    assert lab.ok("agent", "state", PROJECT_ID, cwd=lab.project)["archived_at"]
    assert lab.ok("agent", "restore", PROJECT_ID, cwd=lab.project)["changed"] is True
    assert lab.ok("agent", "show", cwd=lab.project)["archived_at"] is None


def test_the_deployment_lease_table_refuses_a_lease_of_an_archived_agent(lab):
    """The lease table's own minor step (deployment 2.2), which needs core 4.2."""
    import importlib.machinery
    import importlib.util
    lab.ok("node", "add", nid("mac-me"), "--label", "mac-me", "--kind", "mac",
           cwd=lab.project)
    loader = importlib.machinery.SourceFileLoader("capabilities_manager_archive",
                                                  str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    from capabilities_contract import db
    conn = db.connect(application_name="test", setting=db.resolve_setting(lab.project),
                      transaction=True)
    try:
        done = db.migrate(conn, module.SUPERVISE_LEASE_OWNER, module._supervise_lease_steps(),
                          major=2, minor=2)
        assert "0005-agent-archived" in done.applied
    finally:
        conn.close()
    lease = ("INSERT INTO deployment_service_leases (agent_id, capability, node, expires_at) "
             "VALUES (%s, 'tasks', %s, now() + interval '1 minute') "
             "ON CONFLICT (agent_id, capability) DO UPDATE SET expires_at = "
             "EXCLUDED.expires_at")
    lab.sql(lease, (PROJECT_ID, nid("mac-me")))
    lab.sql("UPDATE agents SET archived_at = now(), archived_by = 'x@y' WHERE id = %s",
            (PROJECT_ID,))
    import psycopg
    with pytest.raises(psycopg.errors.IntegrityError) as refused:
        lab.sql(lease, (PROJECT_ID, nid("mac-me")))
    assert refused.value.sqlstate == "23R01"
    # Giving a lease up stays allowed.
    lab.sql("DELETE FROM deployment_service_leases WHERE agent_id = %s", (PROJECT_ID,))


def test_a_guarded_write_in_flight_holds_the_archive_back_and_is_then_seen(lab):
    """The archive's lock on the agent's row conflicts with the FOR SHARE every
    store guard takes: a lease being taken holds the archive back until it
    commits, and the check then reads it."""
    import time
    _seed(lab)
    writer = lab.cluster.connect(lab.database)
    writer.autocommit = False
    try:
        writer.execute(f"SET search_path TO {SCHEMA}")
        writer.execute("INSERT INTO service_leases (agent_id, capability, node, mode, "
                       "expires_at) VALUES (%s, 'tasks', %s, 'own', now() + interval "
                       "'3 minutes')", (PLAIN, nid("mac-me")))
        archive = subprocess.Popen([sys.executable, str(MANAGER), "agent", "archive", "plain"],
                                   cwd=lab.elsewhere, env=lab.env(lab.elsewhere), text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(4)
        assert archive.poll() is None
        writer.commit()
        _out, err = archive.communicate(timeout=60)
    finally:
        writer.close()
    assert archive.returncode == 6, err
    error = json.loads(err.strip().splitlines()[-1])["error"]
    assert error["code"] == "agent_live"
    assert [(r["kind"], r["capability"], r["state"]) for r in error["detail"]["blocking"]] == [
        ("service", "tasks", "lease_ending")]
    assert lab.sql("SELECT archived_at FROM agents WHERE id = %s", (PLAIN,)) == [(None,)]


@pytest.mark.parametrize("marker", MARKERS)
def test_inside_a_harness_another_agent_is_refused_and_its_own_is_not(lab, marker):
    _seed(lab)
    inside = {marker: "1"}
    # From a folder of no agent, and from this agent's own folder, another agent
    # is refused, and nothing is written.
    for cwd in (lab.elsewhere, lab.project):
        for args in (("agent", "archive", "plain"),
                     ("service", "stop", "tasks", "--agent", "placed", "--wait", "0")):
            code, error = lab.refused(*args, cwd=cwd, **inside)
            assert (code, error["code"]) == (4, "harness_session"), (args, error)
            assert marker in error["message"] and "Inbox" in error["hint"]
    assert lab.sql("SELECT count(*) FROM agents WHERE archived_at IS NOT NULL") == [(0,)]
    assert lab.sql("SELECT service FROM policies WHERE agent_id = %s", (PLACED,)) == [
        (True,)]
    # The check writes nothing and stays open.
    assert _check(lab, "plain", **inside)["can_archive"] is True
    # Its own agent: the services stop and the archive goes through, and
    # restore stays open.
    lab.ok("node", "add", nid("mac-me"), "--label", "mac-me", "--kind", "mac",
           cwd=lab.project)
    stopped = lab.ok("service", "stop", "tasks", "--agent", PROJECT_ID, "--wait", "0",
                     cwd=lab.project, **inside)
    assert stopped["agent_id"] == PROJECT_ID
    assert lab.ok("agent", "archive", PROJECT_ID, cwd=lab.project, **inside)["changed"] is True
    assert lab.ok("agent", "restore", PROJECT_ID, cwd=lab.project, **inside)["changed"] is True
    # With no marker, another agent is archived and stopped as before.
    assert lab.ok("agent", "archive", "plain")["changed"] is True
    code, error = lab.refused("service", "stop", "tasks", "--agent", "placed", "--wait", "0")
    assert (code, error["code"], error["detail"]["changed"]) == (
        6, "service_still_running", True)
