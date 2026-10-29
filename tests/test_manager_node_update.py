"""A node updates only when asked, and an update never leaves it broken.

`node update --check` answers what is newer than what the node runs - the
manager release, a capability payload, the contract library an environment
holds - with what each brings, and writes it onto the node's state row,
changing nothing. `node update` runs `self-update` and `update`, asks the
doctor of every service placed on the node, and where a step or a doctor
fails returns the node by itself to what it ran before. `node update --node`
asks a node through the store, and that node's `node run` takes the request
up; nothing else starts an update.

These tests drive the real manager, installed on the node from a release it
was given and updating to a release served over HTTP from the test, with a
capability made here whose service's doctor answers as each copy of it says,
against a throwaway PostgreSQL on a loopback port that the module starts when
`initdb` and `pg_ctl` are on PATH and psycopg is importable, and skips
otherwise. Each test gets a database of its own.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
MANIFEST_REL = ".capability-source/manager-release.json"
ADMIN = "pgadmin"
SCHEMA = "lab"
PROJECT_ID = "prj_" + "0a1b2c3d4e5f"
NODE = "0a0b0c0d0e0f"
DB_KEYS = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
           "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
           "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT")
OUTGOING = "## 2026-10-01 — What the node runs\n\nThe release installed on the node.\n"
INCOMING = "## 2026-10-02 — What arrived since\n\nThe release the source serves.\n"
PROBE_OLD = "## 2026-10-01 — First\n\nThe copy installed on the node.\n"
PROBE_NEW = "## 2026-10-02 — Second\n\nThe copy the source holds now.\n"


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
    for tool in ("initdb", "pg_ctl", "uv"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-node-update-pg-"))
    found = Cluster(root)
    try:
        found.start()
        yield found
    finally:
        found.stop()
        shutil.rmtree(root, ignore_errors=True)


# --- the manager's releases, a capability with a service, and a node ------------

def _release(root: Path, tag: str, log: str) -> Path:
    """A manager release in `root`, as the source publishes one: the manager,
    every asset the repository's release names, and a change log of `log`."""
    assets = json.loads((REPO / MANIFEST_REL).read_text())["assets"]
    files = {rel: (REPO / rel).read_bytes() for rel in assets}
    files["manager/CHANGELOG.md"] = f"# capabilities — change log\n\n{log}".encode()
    manager = MANAGER.read_bytes() + f"\n# the {tag} manager\n".encode()
    for rel, body in {**files, "bin/capabilities": manager}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body)
    (root / "bin" / "capabilities").chmod(0o755)
    manifest = {"schema": "capabilities.manager-release.v1",
                "manager": {"path": "bin/capabilities",
                            "sha256": hashlib.sha256(manager).hexdigest()},
                "assets": {rel: hashlib.sha256(body).hexdigest()
                           for rel, body in files.items()}}
    (root / MANIFEST_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_REL).write_text(json.dumps(manifest, ensure_ascii=False, indent=2,
                                                sort_keys=True) + "\n")
    return root


PROBE = '''#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "capabilities-contract>=0.8,<1",
# ]
# ///
"""probe - a service whose doctor answers as this copy says."""

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

NAME = "probe"
SUMMARY = "A service whose doctor answers as this copy says."
SCOPE = "project"
CRED_KEYS = []
WRITE_VERBS = set()
WRITE_DEFAULT = True
DOCS_BASE = ""
STATE = False
INVENTORY = None
POST_INSTALL: list[dict] = []
CONN_FIELDS: list[dict] = []
DOCTOR = "{doctor}"

_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
CREDENTIALS_ENV = _CONFIG_HOME / NAME / "credentials.env"
_STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))

from capabilities_contract.capability import bind as _contract_bind

_contract_bind(globals(), "core")
_contract_bind(globals(), "store")
_contract_bind(globals(), "connections")

SERVICE = {{
    "name": "probe", "summary": "Answers its doctor.", "verbs": ["run", "doctor"],
    "deploy": {{
        "schema": "capabilities.service.deploy.v1", "default_policy": "auto",
        "command": ["probe", "service", "run"], "doctor": ["probe", "service", "doctor"],
        "environment": {{"required": [], "optional": []}}, "mounts": [],
        "restart": "on-failure"}}}}


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["manifest"]:
        _emit({{**_manifest_core(), "service": SERVICE}})
        return
    if argv[:2] == ["service", "doctor"]:
        if DOCTOR == "slow":
            import time
            time.sleep(18)
        if DOCTOR in ("ready", "slow"):
            _emit({{"ok": True}})
            return
        _die(5, "probe_broken", "this copy of probe cannot do its work")
    _contract(argv)
    _die(6, "input", "unknown command")


if __name__ == "__main__":
    main()
'''


def _probe(root: Path, doctor: str, log: str) -> Path:
    """probe's bundle in `root`: its doctor answers `doctor`."""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "service").mkdir(exist_ok=True)
    (root / "service" / "README.md").write_text("probe's service\n")
    (root / "bin" / "probe").write_text(PROBE.format(doctor=doctor))
    (root / "bin" / "probe").chmod(0o755)
    (root / "CHANGELOG.md").write_text(f"# probe — change log\n\n{log}")
    return root


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


@pytest.fixture()
def served(tmp_path):
    """The source: the incoming release, served over HTTP."""
    root = _release(tmp_path / "incoming", "incoming", INCOMING + "\n" + OUTGOING)
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class Node:
    def __init__(self, tmp: Path, cluster: Cluster, source: str):
        self.tmp, self.cluster = tmp, cluster
        self.database = cluster.database()
        self.cap_home = tmp / "home" / ".capabilities"
        self.bin = tmp / "bin"
        self.link = self.bin / "capabilities"
        self.project = tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        (self.project / "capabilities").mkdir()
        (self.project / "capabilities" / "project.json").write_text(json.dumps(
            {"schema": "capabilities.project.v1", "slug": "lab", "id": PROJECT_ID}))
        (self.project / ".env.local").write_text(
            f"AGENTKIT_DB_URL={cluster.url(self.database)}\nAGENTKIT_DB_SCHEMA={SCHEMA}\n")
        self.base = {k: v for k, v in os.environ.items()
                     if k not in DB_KEYS and not k.startswith("CAPABILITIES_")
                     and k not in ("VIRTUAL_ENV", "AGENT_NODE_ID", "AGENT_NODE_NAME",
                                   "CLAUDE_PROJECT_DIR")}
        self.base.update({
            "HOME": str(tmp / "home"), "XDG_CONFIG_HOME": str(tmp / "config"),
            "XDG_CACHE_HOME": str(tmp / "cache"), "XDG_STATE_HOME": str(tmp / "state"),
            "XDG_DATA_HOME": str(tmp / "data"), "CAPABILITIES_HOME": str(self.cap_home),
            "CAPABILITIES_BIN": str(self.bin), "CAPABILITIES_SOURCE": source,
            "AGENT_NODE_ID": NODE, "AGENT_NODE_NAME": "lab-node",
            "CLAUDE_PROJECT_DIR": str(self.project),
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"})
        self.source = tmp / "src" / "probe"
        # The node runs the outgoing release, linked as the manager on PATH,
        # and probe from its source, its service placed on this node.
        outgoing = _release(tmp / "outgoing", "outgoing", OUTGOING)
        self.ok("self-update", "--from", str(outgoing), exe=MANAGER)
        self.before = os.readlink(self.link)
        _probe(self.source, "ready", PROBE_OLD)
        self.ok("install", "probe", "--from", str(self.source))
        self.ok("allow", "probe")
        self.ok("node", "add", NODE, "--label", "lab-node", "--kind", "server")
        self.sql("UPDATE capabilities_agent_nodes SET folder = %s", (str(self.project),))
        self.sql("INSERT INTO capabilities_policy (agent_id, capability, node, enabled,"
                 " service) VALUES (%s, 'probe', %s, true, true)", (PROJECT_ID, NODE))

    def env(self, **extra: str) -> dict:
        return {**self.base, **extra}

    def run(self, *args: str, exe: Path | None = None, **extra: str):
        return subprocess.run([str(exe or self.link), *args], cwd=self.project,
                              env=self.env(**extra), text=True, capture_output=True,
                              timeout=900)

    def ok(self, *args: str, exe: Path | None = None, **extra: str) -> dict:
        result = self.run(*args, exe=exe, **extra)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    def refused(self, *args: str, **extra: str) -> tuple[int, dict]:
        result = self.run(*args, **extra)
        line = next(line for line in reversed(result.stderr.splitlines())
                    if line.lstrip().startswith("{"))
        return result.returncode, json.loads(line)["error"]

    def sql(self, query: str, params: tuple = ()):
        with self.cluster.connect(self.database) as conn:
            conn.execute(f"SET search_path TO {SCHEMA}")
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else None

    def uv(self, *args: str) -> subprocess.CompletedProcess:
        result = subprocess.run(["uv", *args], env=self.env(), text=True,
                                capture_output=True, timeout=300)
        assert result.returncode == 0, result.stderr
        return result

    def library(self, version: str | None = None) -> str:
        """The contract library probe's own environment holds, set to
        `version` first where given."""
        script = str(self.cap_home / "probe" / "probe")
        self.uv("sync", "--quiet", "--script", script)
        python = json.loads(self.uv("sync", "--quiet", "--script", script, "--dry-run",
                                    "--output-format", "json").stdout
                            )["sync"]["environment"]["python"]["path"]
        if version:
            self.uv("pip", "install", "--quiet", "--python", python,
                    f"capabilities-contract=={version}")
        return subprocess.run(
            [python, "-c", "import importlib.metadata as m;"
                           "print(m.version('capabilities-contract'))"],
            text=True, capture_output=True, timeout=60).stdout.strip()

    def newer_probe(self, doctor: str) -> None:
        _probe(self.source, doctor, PROBE_NEW + "\n" + PROBE_OLD)

    def probe_log(self) -> str:
        return (self.cap_home / "probe" / "CHANGELOG.md").read_text()

    def restarts(self) -> list:
        return self.sql("SELECT version FROM schema_channel WHERE channel ="
                        " 'capabilities_service_restart' AND scope = %s",
                        (f"{PROJECT_ID}/probe/{NODE}",))


def _tree(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel] = "-> " + os.readlink(path)
        elif path.is_file() and "__pycache__" not in path.parts:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.fixture()
def node(tmp_path, cluster, served):
    return Node(tmp_path, cluster, served)


# --- availability ----------------------------------------------------------------

def test_check_answers_what_is_newer_and_what_it_brings_and_changes_nothing(node):
    node.newer_probe("ready")
    assert node.library("0.30.0") == "0.30.0"
    registry, links = _tree(node.cap_home), _tree(node.bin)

    checked = node.ok("node", "update", "--check")

    assert checked["node"] == NODE and checked["update_available"] is True
    found = {c["kind"]: c for c in checked["offer"]["components"]}
    assert (found["manager"]["installed"], found["manager"]["available"]) == (
        "2026-10-01 — What the node runs", "2026-10-02 — What arrived since")
    assert [e["heading"] for e in found["manager"]["entries"]] == [
        "2026-10-02 — What arrived since"]
    assert (found["capability"]["name"], found["capability"]["installed"],
            found["capability"]["available"]) == (
        "probe", "2026-10-01 — First", "2026-10-02 — Second")
    assert [e["heading"] for e in found["capability"]["entries"]] == ["2026-10-02 — Second"]
    library = found["contract_library"]
    assert (library["installed"], library["available"], library["capabilities"]) == (
        "0.30.0", "0.32.0", ["probe"])
    assert any("capabilities-contract 0.31.0" in e["heading"] for e in library["entries"])
    assert not any("capabilities-contract 0.30.0" in e["heading"] for e in library["entries"])
    assert checked["offer"]["errors"] == []
    # Nothing moved, and nothing asked for an update.
    assert _tree(node.cap_home) == registry and _tree(node.bin) == links
    assert node.library() == "0.30.0"
    assert node.sql("SELECT count(*) FROM capabilities_node_updates") == [(0,)]
    [(available, offer)] = node.sql("SELECT update_available, update_offer FROM"
                                    " capabilities_node_states WHERE node = %s", (NODE,))
    assert available is True and offer == checked["offer"]
    [state] = node.ok("node", "state")["nodes"]
    assert state["update"]["available"] is True
    assert state["update"]["checked_at"] == checked["checked_at"]
    assert state["update"]["offer"] == checked["offer"] and state["update"]["last_run"] is None
    # The node's regular state write leaves what it checked as it is.
    node.ok("node", "state", "report")
    assert node.sql("SELECT update_available FROM capabilities_node_states") == [(True,)]
    code, error = node.refused("node", "update", "--check", CAPABILITIES_READ_ONLY="1")
    assert (code, error["code"]) == (4, "read_only_switch")


# --- an update, and its return to what the node ran ------------------------------

def test_an_update_moves_everything_asks_the_doctors_and_restarts_the_placed_service(node):
    node.newer_probe("ready")
    assert node.library("0.30.0") == "0.30.0"

    done = node.ok("node", "update")

    run = done["update"]
    assert run["state"] == "updated", run
    assert os.readlink(node.link) != node.before
    assert "the incoming manager" in node.link.resolve().read_text()
    assert PROBE_NEW in node.probe_log() and node.library() == "0.32.0"
    assert run["doctors"] == {"after_update": [
        {"capability": "probe", "agent_id": PROJECT_ID, "state": "ready"}]}
    assert run["versions_before"]["manager"]["links"][str(node.link)] == node.before
    assert run["versions_after"]["capabilities"]["probe"]["version"] == "2026-10-02 — Second"
    assert node.restarts() == [(1,)]
    assert done["availability"]["update_available"] is False
    [(state, by, started)] = node.sql("SELECT state, requested_by, started_at IS NOT NULL"
                                      " FROM capabilities_node_updates")
    assert (state, started) == ("updated", True) and by
    [listed] = node.ok("node", "state")["nodes"]
    assert listed["update"]["last_run"]["id"] == run["id"]
    assert listed["update"]["available"] is False

    again = node.ok("node", "update")
    assert again["update"]["state"] == "current" and node.restarts() == [(1,)]


def test_a_red_doctor_returns_the_node_to_what_it_ran_and_restarts_nothing(node):
    node.newer_probe("broken")
    assert node.library("0.30.0") == "0.30.0"
    payload = _tree(node.cap_home / "probe")

    code, error = node.refused("node", "update")

    assert (code, error["code"]) == (6, "update_rolled_back")
    run = error["detail"]["update"]
    assert run["state"] == "rolled_back"
    assert "the doctor of probe" in run["reason"] and "probe_broken" in run["reason"]
    assert run["doctors"]["after_update"][0]["state"] == "unusable"
    assert run["doctors"]["after_rollback"] == [
        {"capability": "probe", "agent_id": PROJECT_ID, "state": "ready"}]
    assert os.readlink(node.link) == node.before
    assert _tree(node.cap_home / "probe") == payload and node.library() == "0.30.0"
    assert run["versions_after"] == run["versions_before"]
    assert node.restarts() == []
    assert node.sql("SELECT state FROM capabilities_node_updates") == [("rolled_back",)]
    assert not any((node.cap_home / ".manager" / "node-update").glob("*-*"))


def test_a_failing_step_returns_the_node_and_a_run_that_stopped_is_closed(node):
    stale = node.sql("INSERT INTO capabilities_node_updates (node, state, started_at)"
                     " VALUES (%s, 'running', now()) RETURNING id", (NODE,))[0][0]
    shutil.move(node.source, node.tmp / "moved")

    code, error = node.refused("node", "update")

    assert (code, error["code"]) == (6, "update_rolled_back")
    run = error["detail"]["update"]
    assert run["reason"].startswith("update failed: not_found"), run["reason"]
    assert error["detail"]["interrupted"] == [str(stale)]
    assert os.readlink(node.link) == node.before
    assert PROBE_NEW not in node.probe_log()
    [(state, reason)] = node.sql("SELECT state, reason FROM capabilities_node_updates"
                                 " WHERE id = %s", (stale,))
    assert state == "failed" and reason.startswith("interrupted")
    assert node.restarts() == []


# --- asking a node, and the node's manager taking it up --------------------------

def test_node_run_updates_only_when_a_row_asks_and_then_runs_the_new_release(node):
    # The new copy's doctor takes its time, so the update outlasts a state beat.
    node.newer_probe("slow")
    calls = node.tmp / "supervisorctl.log"
    (node.bin / "supervisorctl").write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    (node.bin / "supervisorctl").chmod(0o755)
    env = node.env(SUPERVISOR_SERVER_URL="unix:///tmp/test.sock")
    jobs = node.tmp / "jobs"
    node_run = subprocess.Popen([str(node.link), "node", "run", "--supervisor", str(jobs),
                                 "--every", "300"], cwd=node.project, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _wait(lambda: (jobs / f"probe.{PROJECT_ID}.conf").exists(), 60)
        # Newer releases are there and no row asks: nothing starts.
        time.sleep(8)
        assert node.sql("SELECT count(*) FROM capabilities_node_updates") == [(0,)]
        assert os.readlink(node.link) == node.before

        asked = node.ok("node", "update", "--node", "lab-node")
        assert asked["node"] == NODE and asked["requested"]["state"] == "requested"
        code, error = node.refused("node", "update", "--node", NODE)
        assert (code, error["code"]) == (6, "update_pending")
        assert node.refused("node", "update", "--node", NODE,
                            CAPABILITIES_READ_ONLY="1")[1]["code"] == "read_only_switch"

        def state():
            return node.sql("SELECT state FROM capabilities_node_updates WHERE id = %s",
                            (asked["requested"]["id"],))[0][0]
        assert _wait(lambda: state() == "running", 20)
        beats = set()
        while state() == "running":
            beats.update(r[0] for r in node.sql("SELECT reported_at FROM"
                                                " capabilities_node_states"))
            time.sleep(1)
        assert state() == "updated"
        # The state kept being written while the update ran.
        assert len(beats) >= 2
        node_run.wait(timeout=60)
    finally:
        if node_run.poll() is None:
            node_run.terminate()
        _out, err = node_run.communicate(timeout=60)
    assert node_run.returncode == 75, err
    assert "updates this node, as a row of capabilities_node_updates asks" in err
    assert "the manager on PATH is another release now" in err
    said = [json.loads(line) for line in calls.read_text().splitlines()]
    assert ["-s", "unix:///tmp/test.sock", "restart", f"probe.{PROJECT_ID}"] in said
    assert os.readlink(node.link) != node.before


def test_node_run_on_a_disabled_node_runs_nothing_writes_no_state_and_comes_back(node):
    (node.bin / "supervisorctl").write_text(f"#!{sys.executable}\n")
    (node.bin / "supervisorctl").chmod(0o755)
    env = node.env(SUPERVISOR_SERVER_URL="unix:///tmp/test.sock")
    job = node.tmp / "jobs" / f"probe.{PROJECT_ID}.conf"
    node_run = subprocess.Popen([str(node.link), "node", "run", "--supervisor",
                                 str(job.parent), "--every", "300"], cwd=node.project,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)

    def reported():
        return node.sql("SELECT reported_at FROM capabilities_node_states")
    try:
        assert _wait(job.exists, 60) and _wait(reported, 30)
        node.ok("node", "disable", "lab-node")
        # The placement announce reaches it at once, not on its 300-second reconcile.
        assert _wait(lambda: not job.exists(), 20)
        last = reported()
        time.sleep(20)
        assert reported() == last
        assert node.refused("node", "update", "--node", NODE)[1]["code"] == "node_disabled"
        assert node.sql("SELECT count(*) FROM capabilities_policy WHERE node = %s"
                        " AND service", (NODE,)) == [(1,)]
        node.ok("node", "enable", "lab-node")
        assert _wait(job.exists, 20) and _wait(lambda: reported() != last, 30)
    finally:
        node_run.terminate()
        _out, err = node_run.communicate(timeout=60)
    assert f"stops job probe.{PROJECT_ID}: this node is disabled" in err
    assert "this node is enabled again" in err


def _wait(check, seconds: float):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.3)
    return check()
