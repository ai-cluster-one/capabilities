"""A service runs where the gate on its node places it, and on one machine.

Each supervised service runs through `deployment runner`, which reads the gate
for this project on this node every reconcile, holds the service's lease in the
project's store, and starts or stops the service to match. These tests drive the
real runner and the real manager in projects that exist only for the test: in
files mode with no database at all, and in store mode against a throwaway
PostgreSQL on a loopback port that the module starts when `initdb` and `pg_ctl`
are on PATH and psycopg is importable, and skips otherwise. The service is a
child the test can observe: a Python process that prints and sleeps.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest


# The nodes these tests play, by id: each is its label's first six characters
# as hex, so the ids sort as the labels do.
LABELS = {"6e6f64652d61": "node-a", "6e6f64652d62": "node-b", "6e6f64652d66": "node-f",
          "6d61632d6100": "mac-a", "626f782d6200": "box-b", "626f782d6300": "box-c"}

CAP_ROOT = Path(__file__).resolve().parents[2]
REPO = CAP_ROOT.parent
DEPLOYMENT = CAP_ROOT / "deployment" / "bin" / "deployment"
MANAGER = REPO / "bin" / "capabilities"
ADMIN = "pgadmin"
EVERY = "5"
PROJECT_ID = "prj_" + "5e1f0c2d3b4a"
# Nothing here may reach a database the machine running the tests declares.
SCRUBBED = ("AGENTKIT_DB_URL", "AGENTKIT_DB_HOST", "AGENTKIT_DB_PORT", "AGENTKIT_DB_NAME",
            "AGENTKIT_DB_USER", "AGENTKIT_DB_PASSWORD", "AGENTKIT_DB_SCHEMA",
            "AGENTKIT_DB_SSLMODE", "AGENTKIT_DB_SSLROOTCERT", "AGENTKIT_STORE_URL",
            "CAPABILITIES_STORE_URL", "CAPABILITIES_READ_ONLY", "AGENT_NODE_NAME",
            "AGENT_NODE_ID",
            "CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
            "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
            "CLAUDE_PROJECT_DIR", "VIRTUAL_ENV")


def _env(home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in SCRUBBED}
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                "XDG_STATE_HOME": str(home / "state"), "XDG_DATA_HOME": str(home / "data"),
                "XDG_CACHE_HOME": str(home / "cache"),
                "CAPABILITIES_HOME": str(home / "registry")})
    return env


def _project(root: Path, identity: dict, settings: dict | None = None,
             dotenv: str | None = None) -> Path:
    (root / ".git").mkdir(parents=True)
    envelope = root / "capabilities"
    envelope.mkdir()
    (envelope / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", **identity}))
    if settings is not None:
        (envelope / "settings.json").write_text(json.dumps({"capabilities": settings}))
    if dotenv is not None:
        (root / ".env").write_text(dotenv)
    return root


class Runner:
    """One `deployment runner` for the observable child, on one node."""

    def __init__(self, root: Path, env: dict[str, str], node: str, log: Path,
                 child: list[str] | None = None):
        self.root, self.node, self.log_path = root, node, log
        self.log = log.open("w")
        child = child or [sys.executable, "-c",
                          "import os, time; print('child up', os.getpid(), flush=True); "
                          "time.sleep(600)"]
        self.proc = subprocess.Popen(
            [str(DEPLOYMENT), "runner", "fixture", "--every", EVERY, "--", *child],
            cwd=root, env={**env, "AGENT_NODE_ID": node}, stdout=self.log,
            stderr=subprocess.STDOUT, start_new_session=True)

    def state(self) -> dict:
        path = self.root / "capabilities" / "deployment" / "state" / "runners" / "fixture.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def text(self) -> str:
        return self.log_path.read_text()

    def until(self, check, timeout: float = 25.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state and check(state):
                return state
            assert self.proc.poll() is None, self.text()
            time.sleep(0.3)
        raise AssertionError(f"{self.node}: {self.state()}\n{self.text()}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.log.close()


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_files_mode_places_the_service_by_the_projects_own_enable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = _env(home)
    root = _project(tmp_path / "project", {"id": PROJECT_ID, "slug": "files"},
                    settings={"deployment": {"enabled": True}})
    # A global enable makes the CLI usable here and never places the service.
    (home / "config" / "capabilities").mkdir(parents=True)
    (home / "config" / "capabilities" / "settings.json").write_text(
        json.dumps({"capabilities": {"fixture": {"enabled": True}}}))
    runner = Runner(root, env, "6e6f64652d66", tmp_path / "runner.log")
    try:
        idle = runner.until(lambda s: s.get("tick_at") is not None)
        assert idle["want"] is False and idle["running"] is False
        assert "nothing inherited or global places a service" in idle["reason"]
        (root / "capabilities" / "settings.json").write_text(json.dumps({"capabilities": {
            "deployment": {"enabled": True}, "fixture": {"enabled": True}}}))
        running = runner.until(lambda s: s.get("running"))
        # No database is resolvable, so no lease is taken and the gate decides.
        assert running["lease"]["state"] == "unavailable"
        assert running["gate"]["row"] == {"scope": "project", "node": None, "enabled": True}
        child = running["child"]["pid"]
        assert _alive(child)
        report = json.loads(subprocess.run(
            [str(DEPLOYMENT), "migrate", "status"], cwd=root, env=env,
            capture_output=True, text=True, timeout=120).stderr.splitlines()[-1])
        assert report["error"]["code"] == "store_not_configured"
    finally:
        runner.stop()
    assert "child up" in runner.text()
    assert not _alive(child)


REFUSING = [sys.executable, "-c",
            "import json, sys; print('child ' + 'start', flush=True); "
            "sys.stderr.write(json.dumps({'error': {'code': 'joined_to_machine_service', "
            "'message': 'this project is served by the machine service'}}) + '\\n'); "
            "sys.exit(4)"]


def test_a_policy_refusal_holds_the_service_idle_until_the_gate_changes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = _env(home)
    settings = {"deployment": {"enabled": True}, "fixture": {"enabled": True}}
    root = _project(tmp_path / "project", {"id": PROJECT_ID, "slug": "files"},
                    settings=settings)
    (root / "deployment").mkdir()
    (root / "deployment" / "runtime.json").write_text(json.dumps(
        {"profile": "agent-box", "services": {"agent": {"embedded_services": ["fixture"]}}}))
    runner = Runner(root, env, "6e6f64652d66", tmp_path / "runner.log", child=REFUSING)
    try:
        held = runner.until(lambda s: s.get("refusal"))
        assert held["want"] is False and held["running"] is False
        assert held["refusal"]["exit"] == 4
        assert held["refusal"]["slug"] == "joined_to_machine_service"
        assert "served by the machine service" in held["refusal"]["message"]
        assert "held idle" in held["reason"]
        # Two more reconciles with the same gate answer start nothing.
        first_tick = held["tick_at"]
        runner.until(lambda s: s.get("tick_at") not in (None, first_tick), timeout=15)
        time.sleep(int(EVERY) + 1)
        assert runner.text().count("child start") == 1
        assert runner.text().count("joined_to_machine_service") >= 1
        report = json.loads(subprocess.run(
            [str(DEPLOYMENT), "services"], cwd=root, env={**env, "AGENT_NODE_ID": "6e6f64652d66"},
            capture_output=True, text=True, timeout=120).stdout)
        [seen] = report["services"]
        assert seen["running"] is False
        assert seen["refusal"]["slug"] == "joined_to_machine_service"
        # The gate answer flips: the hold is lifted, and once it places the
        # service again the runner tries once more.
        (root / "capabilities" / "settings.json").write_text(json.dumps({"capabilities": {
            **settings, "fixture": {"enabled": False}}}))
        runner.until(lambda s: not s.get("refusal"))
        (root / "capabilities" / "settings.json").write_text(json.dumps({"capabilities": settings}))
        runner.until(lambda s: s.get("refusal") and s["tick_at"] and
                     runner.text().count("child start") == 2)
        assert "the gate answer for 6e6f64652d66 changed" in runner.text()
    finally:
        runner.stop()


# --- a throwaway PostgreSQL ------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def cluster():
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="deployment-runner-pg-"))
    port = _free_port()
    env = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    try:
        subprocess.run(["initdb", "-D", str(root / "data"), "-U", ADMIN, "-E", "UTF8",
                        "--auth=trust"], env=env, check=True, capture_output=True, timeout=120)
        with (root / "data" / "postgresql.conf").open("a") as conf:
            conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n"
                       "unix_socket_directories = ''\n")
        subprocess.run(["pg_ctl", "-D", str(root / "data"), "-l", str(root / "pg.log"),
                        "-w", "-t", "60", "start"], env=env, check=True,
                       capture_output=True, timeout=120)
        yield port
    finally:
        subprocess.run(["pg_ctl", "-D", str(root / "data"), "-m", "immediate", "stop"],
                       env=env, capture_output=True, timeout=60)
        shutil.rmtree(root, ignore_errors=True)


def _database(port: int) -> tuple[str, object]:
    import psycopg
    name = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(host="127.0.0.1", port=port, user=ADMIN, dbname="postgres",
                         autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name} ENCODING 'UTF8' TEMPLATE template0")
    return name, lambda query: _rows(port, name, query)


def _rows(port: int, database: str, query: str) -> list[tuple]:
    import psycopg
    with psycopg.connect(host="127.0.0.1", port=port, user=ADMIN, dbname=database,
                         autocommit=True) as conn:
        conn.execute("SET search_path TO lab")
        return conn.execute(query).fetchall()


def _manager(root: Path, env: dict[str, str], *args: str) -> dict:
    proc = subprocess.run([str(MANAGER), *args], cwd=root, env=env, capture_output=True,
                          text=True, timeout=240)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def test_the_gate_on_each_node_starts_and_stops_the_service_and_one_node_holds_it(
        tmp_path: Path, cluster) -> None:
    database, rows = _database(cluster)
    env = _env(tmp_path / "home")
    dotenv = (f"AGENTKIT_DB_URL=postgresql://{ADMIN}@127.0.0.1:{cluster}/{database}"
              "?sslmode=disable\nAGENTKIT_DB_SCHEMA=lab\n")
    identity = {"id": PROJECT_ID, "slug": "lab", "records": "store"}
    # Two checkouts of one project, standing for two machines.
    a = _project(tmp_path / "a", identity, dotenv=dotenv)
    b = _project(tmp_path / "b", identity, dotenv=dotenv)
    _manager(a, env, "enable", "deployment", "--project")
    runners = {}
    try:
        runners["6e6f64652d61"] = Runner(a, env, "6e6f64652d61", tmp_path / "a.log")
        idle = runners["6e6f64652d61"].until(lambda s: s.get("tick_at") is not None)
        assert idle["running"] is False
        assert idle["gate"]["reason"] == "no policy row answers for fixture on 6e6f64652d61"

        # A row for every node makes the CLI usable and places nothing.
        _manager(a, env, "enable", "fixture", "--project")
        runners["6e6f64652d61"].until(lambda s: "row for every node answers" in s["reason"])
        assert runners["6e6f64652d61"].state()["running"] is False

        written = _manager(a, env, "enable", "fixture", "--project", "--node", "6e6f64652d61")
        assert written["node"] == "6e6f64652d61"
        running = runners["6e6f64652d61"].until(lambda s: s.get("running"))
        assert running["lease"]["state"] == "held" and running["lease"]["holder"] == "6e6f64652d61"
        first = running["child"]["pid"]
        assert rows("SELECT capability, node FROM deployment_service_leases") == [
            ("fixture", "6e6f64652d61")]

        _manager(b, env, "enable", "fixture", "--project", "--node", "6e6f64652d62")
        runners["6e6f64652d62"] = Runner(b, env, "6e6f64652d62", tmp_path / "b.log")
        refused = runners["6e6f64652d62"].until(lambda s: s["lease"].get("state") == "held_elsewhere")
        assert refused["running"] is False and refused["lease"]["holder"] == "6e6f64652d61"
        assert "held by 6e6f64652d61 until" in runners["6e6f64652d62"].text()
        # A person on node-b sees what its gate answers, that nothing runs
        # here, and who holds the lease.
        (b / "deployment").mkdir()
        (b / "deployment" / "runtime.json").write_text(json.dumps(
            {"profile": "agent-box", "services": {"agent": {"embedded_services": ["fixture"]}}}))
        proc = subprocess.run([str(DEPLOYMENT), "services"], cwd=b,
                              env={**env, "AGENT_NODE_ID": "6e6f64652d62"},
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout)
        assert report["node"] == "6e6f64652d62" and report["records"] == "store"
        [seen] = report["services"]
        assert seen["name"] == "fixture" and seen["running"] is False
        assert seen["gate"]["placed"] is True
        assert seen["gate"]["row"] == {"scope": "project", "node": "6e6f64652d62", "enabled": True,
                                       "service": True}
        assert seen["lease"]["state"] == "held_elsewhere" and seen["lease"]["holder"] == "6e6f64652d61"

        _manager(a, env, "disable", "fixture", "--project", "--node", "6e6f64652d61")
        stopped = runners["6e6f64652d61"].until(lambda s: not s.get("running")
                                          and s["lease"].get("state") != "held")
        assert "disables fixture" in stopped["reason"]
        assert not _alive(first)
        taken = runners["6e6f64652d62"].until(lambda s: s.get("running"))
        assert taken["lease"]["holder"] == "6e6f64652d62"
        assert "released the lease for fixture" in runners["6e6f64652d61"].text()

        _manager(b, env, "inherit", "fixture", "--project", "--node", "6e6f64652d62")
        runners["6e6f64652d62"].until(lambda s: not s.get("running"))
    finally:
        for runner in runners.values():
            runner.stop()
    assert rows("SELECT * FROM deployment_service_leases") == []


# --- a store-mode project compiles every service --------------------------------

def _registry_manifest(name: str) -> dict:
    script = CAP_ROOT / name / "bin" / name
    return json.loads(subprocess.run([str(script), "manifest", "--json"],
                                     capture_output=True, text=True, timeout=60,
                                     check=True).stdout)


def test_a_store_mode_project_compiles_every_service_and_retires_the_switches(
        tmp_path: Path, cluster) -> None:
    database, _rows_of = _database(cluster)
    env = _env(tmp_path / "home")
    for name in ("telegram", "automations", "slack"):
        path = Path(env["CAPABILITIES_HOME"]) / name / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(_registry_manifest(name)))
    root = _project(tmp_path / "project", {"id": PROJECT_ID, "slug": "project",
                                           "records": "store"},
                    dotenv=(f"AGENTKIT_DB_URL=postgresql://{ADMIN}@127.0.0.1:{cluster}/"
                            f"{database}?sslmode=disable\nAGENTKIT_DB_SCHEMA=lab\n"))
    _manager(root, env, "enable", "deployment", "--project")

    def deployment(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(DEPLOYMENT), *args], cwd=root, env=env,
                              capture_output=True, text=True, timeout=120)

    setup = deployment("setup", "--provider", "manual", "--force")
    assert setup.returncode == 0, setup.stderr
    # No capability is enabled anywhere: the gate on each node decides.
    assert json.loads(setup.stdout)["embedded_services"] == ["automations", "slack", "telegram"]
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    assert runtime["service_policy"] == {"shape": "embedded", "capabilities": {}}
    supervisor = (root / "deployment/targets/production/supervisord.conf").read_text()
    assert ("command=deployment runner telegram --every 60 --restart unless-stopped "
            "-- telegram service run") in supervisor
    refused = deployment("setup", "--provider", "manual", "--force", "--with-telegram", "no")
    assert refused.returncode == 6
    assert json.loads(refused.stderr.splitlines()[-1])["error"]["code"] == "retired_option"

    runtime["service_policy"] = {
        "auto_include": False, "default_mode": "embedded",
        "capabilities": {"telegram": "enabled", "automations": "disabled"},
        "placement": {"slack": "local"}}
    runtime["services"]["agent"]["placement"] = "node"
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    retired = {"service_policy.auto_include", "service_policy.default_mode",
               "service_policy.placement", "service_policy.capabilities.telegram",
               "service_policy.capabilities.automations", "services.agent.placement"}
    check = deployment("sync", "--check")
    assert check.returncode == 7
    payload = json.loads(check.stdout)
    warnings = [f["message"] for f in payload["findings"] if f["severity"] == "warning"]
    for key in retired:
        assert any(message.startswith(key) for message in warnings), key
    assert set(payload["retired"]) == retired
    synced = deployment("sync")
    assert synced.returncode == 0, synced.stdout + synced.stderr
    result = json.loads(synced.stdout)
    # A disabled service is not kept from running by the runtime any more.
    assert result["services"] == ["telegram"]
    assert result["embedded_services"] == ["automations", "slack"]
    rewritten = json.loads(runtime_path.read_text())
    assert rewritten["service_policy"] == {"shape": "embedded",
                                           "capabilities": {"telegram": "container"}}
    assert "placement" not in rewritten["services"]["agent"]
    assert "placement" not in rewritten["services"]["telegram"]
    assert json.loads(deployment("sync", "--check").stdout)["retired"] == []


def _backends(port: int, database: str) -> dict[int, str]:
    """Every connection to `database` other than this one: pid to state."""
    import psycopg
    with psycopg.connect(host="127.0.0.1", port=port, user=ADMIN, dbname=database,
                         autocommit=True) as conn:
        return dict(conn.execute(
            "SELECT pid, state FROM pg_stat_activity WHERE datname = %s "
            "AND pid <> pg_backend_pid()", (database,)).fetchall())


def _held_across(port: int, database: str, seconds: float) -> set[int]:
    """The connections to `database` seen at the start and still there after
    `seconds`, sampled throughout: a connection opened for one read, write or
    renewal and closed after it is never seen twice so far apart."""
    first = time.monotonic()
    seen: dict[int, float] = {}
    held: set[int] = set()
    while time.monotonic() - first < seconds:
        now = time.monotonic()
        for pid in _backends(port, database):
            seen.setdefault(pid, now)
            if now - seen[pid] >= 2.0:
                held.add(pid)
        time.sleep(0.2)
    return held


def test_a_runner_holds_a_connection_only_while_its_service_runs(tmp_path: Path, cluster) -> None:
    database, rows = _database(cluster)
    env = _env(tmp_path / "home")
    dotenv = (f"AGENTKIT_DB_URL=postgresql://{ADMIN}@127.0.0.1:{cluster}/{database}"
              "?sslmode=disable\nAGENTKIT_DB_SCHEMA=lab\n")
    root = _project(tmp_path / "a", {"id": PROJECT_ID, "slug": "lab", "records": "store"},
                    dotenv=dotenv)
    _manager(root, env, "enable", "deployment", "--project")
    _manager(root, env, "enable", "fixture", "--project")
    runner = Runner(root, env, "6e6f64652d61", tmp_path / "a.log")
    try:
        idle = runner.until(lambda s: "row for every node answers" in (s.get("reason") or ""))
        assert idle["running"] is False
        # Its service not placed here, the runner reads the gate every reconcile
        # and holds nothing between the reads.
        assert _held_across(cluster, database, 3 * int(EVERY)) == set()
        _manager(root, env, "enable", "fixture", "--project", "--node", "6e6f64652d61")
        running = runner.until(lambda s: s.get("running"))
        assert running["lease"]["state"] == "held"
        # Placed, it renews the lease every reconcile on its one working
        # connection, kept between the renewals.
        assert len(_held_across(cluster, database, 3 * int(EVERY))) == 1
        assert runner.state()["lease"]["state"] == "held"
    finally:
        runner.stop()
    assert rows("SELECT * FROM deployment_service_leases") == []


FAILING = [sys.executable, "-c",
           "import sys; sys.stderr.write('project: must be a path inside the agent home\\n'); "
           "sys.exit(6)"]


def test_a_service_that_fails_leaves_its_error_on_the_lease_for_another_machine(
        tmp_path: Path, cluster) -> None:
    database, rows = _database(cluster)
    env = _env(tmp_path / "home")
    dotenv = (f"AGENTKIT_DB_URL=postgresql://{ADMIN}@127.0.0.1:{cluster}/{database}"
              "?sslmode=disable\nAGENTKIT_DB_SCHEMA=lab\n")
    identity = {"id": PROJECT_ID, "slug": "lab", "records": "store"}
    a = _project(tmp_path / "a", identity, dotenv=dotenv)
    b = _project(tmp_path / "b", identity, dotenv=dotenv)
    _manager(a, env, "enable", "deployment", "--project")
    _manager(a, env, "enable", "fixture", "--project", "--node", "node-a")
    runner = Runner(a, env, "node-a", tmp_path / "a.log", child=FAILING)
    try:
        failed = runner.until(lambda s: (s.get("last_exit") or {}).get("code") == 6)
        assert "must be a path inside" in failed["last_exit"]["error"]
        [(node, code, error)] = rows(
            "SELECT node, last_exit, last_error FROM deployment_service_leases")
        assert (node, code) == ("node-a", 6) and "must be a path inside" in error
        # Another machine, reading the store, sees why it stopped.
        (b / "deployment").mkdir()
        (b / "deployment" / "runtime.json").write_text(json.dumps(
            {"profile": "agent-box", "services": {"agent": {"embedded_services": ["fixture"]}}}))
        proc = subprocess.run([str(DEPLOYMENT), "services"], cwd=b,
                              env={**env, "AGENT_NODE_NAME": "node-b"},
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        [seen] = json.loads(proc.stdout)["services"]
        assert seen["lease"]["holder"] == "node-a"
        assert seen["lease"]["last_error"]["exit"] == 6
        assert "must be a path inside" in seen["lease"]["last_error"]["error"]
    finally:
        runner.stop()


def test_each_reconcile_reports_the_node_seen_and_a_node_seen_within_three_is_live(
        tmp_path: Path, cluster) -> None:
    database, rows = _database(cluster)
    env = _env(tmp_path / "home")
    # The runner reports through the manager on PATH: this tree's.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "capabilities").symlink_to(MANAGER)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    dotenv = (f"AGENTKIT_DB_URL=postgresql://{ADMIN}@127.0.0.1:{cluster}/{database}"
              "?sslmode=disable\nAGENTKIT_DB_SCHEMA=lab\n")
    identity = {"id": PROJECT_ID, "slug": "lab", "records": "store"}
    # Two checkouts of one project, a Mac and a node of the standard image.
    roots = {"6d61632d6100": _project(tmp_path / "a", identity, dotenv=dotenv),
             "626f782d6200": _project(tmp_path / "b", identity, dotenv=dotenv)}
    a = roots["6d61632d6100"]
    (a / "deployment").mkdir()
    (a / "deployment" / "runtime.json").write_text(json.dumps(
        {"profile": "agent-box", "service_policy": {"reconcile_seconds": int(EVERY)},
         "services": {"agent": {"embedded_services": ["fixture"]}}}))
    _manager(a, env, "enable", "deployment", "--project")
    for node, kind in (("6d61632d6100", "mac"), ("626f782d6200", "server"), ("626f782d6300", "server")):
        _manager(a, env, "node", "add", node, "--label", LABELS[node], "--kind", kind)

    def seen() -> dict:
        return dict(rows("SELECT id, last_seen_at > now() - interval '1 minute' "
                         "FROM capabilities_nodes WHERE last_seen_at IS NOT NULL"))

    def until_seen(want: dict) -> None:
        deadline = time.monotonic() + 40
        while seen() != want:
            assert time.monotonic() < deadline, (seen(), *(r.text() for r in runners.values()))
            for runner in runners.values():
                assert runner.proc.poll() is None, runner.text()
            time.sleep(0.3)

    def listed() -> dict:
        proc = subprocess.run([str(DEPLOYMENT), "node", "list"], cwd=a, env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    runners = {}
    try:
        # No service is placed on either node, so neither holds a lease.
        for node, root in roots.items():
            runners[node] = Runner(root, env, node, tmp_path / f"{node}.log")
        until_seen({"6d61632d6100": True, "626f782d6200": True})
        rows("UPDATE capabilities_nodes SET last_seen_at = now() - interval '1 hour' "
             "WHERE last_seen_at IS NOT NULL RETURNING id")
        until_seen({"6d61632d6100": True, "626f782d6200": True})
        for runner in runners.values():
            state = runner.state()
            assert state["want"] is False and state["lease"].get("state") != "held"
            assert "cannot report" not in runner.text()

        report = listed()
        assert (report["reconcile_seconds"], report["live_within_seconds"]) == (5, 15)
        assert {n["node"]: n["live"] for n in report["nodes"]} == {
            "626f782d6200": True, "626f782d6300": False, "6d61632d6100": True}

        # box-b falls silent: past three reconciles it no longer reads live.
        runners.pop("626f782d6200").stop()
        rows("UPDATE capabilities_nodes SET last_seen_at = now() - interval '16 seconds' "
             "WHERE id = '626f782d6200' RETURNING id")
        nodes = {n["node"]: n for n in listed()["nodes"]}
        assert (nodes["626f782d6200"]["live"], nodes["6d61632d6100"]["live"]) == (False, True)
        assert nodes["626f782d6200"]["seen_seconds_ago"] >= 16
    finally:
        for runner in runners.values():
            runner.stop()


# --- what each node runs, and whether it can do its work -------------------------

# The fixture's declared doctor answers as the file `doctor-says` in the
# project tells it: ready by default, unusable in the contract's own words -
# exit 5 and an error envelope - or ready with a setting skipped here.
DOCTOR = [sys.executable, "-c", (
    "import json, pathlib, sys\n"
    "says = pathlib.Path('doctor-says')\n"
    "says = says.read_text().strip() if says.exists() else ''\n"
    "if says == 'unusable':\n"
    "    print(json.dumps({'ok': False}))\n"
    "    sys.stderr.write(json.dumps({'error': {'code': 'service_unhealthy', "
    "'message': 'session not authorized'}}) + '\\n')\n"
    "    sys.exit(5)\n"
    "print(json.dumps({'ok': True, 'skipped_settings': "
    "[{'setting': 'groups.g.project'}] if says == 'skipped' else []}))\n")]
PAYLOAD = "ab" * 32


def _installed_fixture(env: dict[str, str], placement: str) -> None:
    home = Path(env["CAPABILITIES_HOME"]) / "fixture"
    home.mkdir(parents=True, exist_ok=True)
    (home / "manifest.json").write_text(json.dumps(
        {"name": "fixture", "service": {"placement": placement,
                                        "deploy": {"doctor": DOCTOR}}}))
    (home / "meta.json").write_text(json.dumps({"name": "fixture",
                                                "payload_sha256": PAYLOAD}))


INSTANCES = ("SELECT node, state, ready, problem, problem_code, pid, host, payload_sha256, "
             "last_exit_code, last_error, skipped_settings "
             "FROM capabilities_service_instances WHERE project_id = '" + PROJECT_ID
             + "' AND capability = 'fixture' ORDER BY node")


def _instances(rows, check, runners, timeout: float = 45.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        found = {row[0]: dict(zip(("node", "state", "ready", "problem", "problem_code",
                                   "pid", "host", "payload_sha256", "last_exit_code",
                                   "last_error", "skipped_settings"), row))
                 for row in rows(INSTANCES)}
        if check(found):
            return found
        assert time.monotonic() < deadline, (found, *(r.text() for r in runners))
        for runner in runners:
            assert runner.proc.poll() is None, runner.text()
        time.sleep(0.3)


def _store_project(tmp_path: Path, cluster: int, env: dict[str, str], *names: str):
    database, rows = _database(cluster)
    dotenv = (f"AGENTKIT_DB_URL=postgresql://{ADMIN}@127.0.0.1:{cluster}/{database}"
              "?sslmode=disable\nAGENTKIT_DB_SCHEMA=lab\n")
    identity = {"id": PROJECT_ID, "slug": "lab", "records": "store"}
    roots = [_project(tmp_path / name, identity, dotenv=dotenv) for name in names]
    _manager(roots[0], env, "enable", "deployment", "--project")
    return rows, roots


def test_an_exclusive_service_records_on_its_node_whether_it_can_do_its_work(
        tmp_path: Path, cluster) -> None:
    env = _env(tmp_path / "home")
    _installed_fixture(env, "exclusive")
    rows, (a,) = _store_project(tmp_path, cluster, env, "a")
    node = "6e6f64652d61"
    _manager(a, env, "enable", "fixture", "--project", "--node", node)
    runner = Runner(a, env, node, tmp_path / "a.log")
    try:
        ready = _instances(rows, lambda f: (f.get(node) or {}).get("state") == "ready",
                           [runner])[node]
        assert ready["ready"] is True and ready["problem"] is None
        assert ready["pid"] == runner.state()["child"]["pid"]
        assert ready["host"] == socket.gethostname() and ready["payload_sha256"] == PAYLOAD

        # Running, and its doctor says it cannot work: unusable, with why.
        (a / "doctor-says").write_text("unusable")
        unusable = _instances(rows, lambda f: f[node]["state"] == "unusable", [runner])[node]
        assert unusable["ready"] is False and unusable["pid"] == ready["pid"]
        assert (unusable["problem"], unusable["problem_code"]) == (
            "session not authorized", "service_unhealthy")

        (a / "doctor-says").write_text("skipped")
        degraded = _instances(rows, lambda f: f[node]["state"] == "degraded", [runner])[node]
        assert degraded["ready"] is True
        assert degraded["skipped_settings"] == [{"setting": "groups.g.project"}]

        # Unplaced, the node no longer runs it, and its row goes.
        _manager(a, env, "disable", "fixture", "--project", "--node", node)
        _instances(rows, lambda f: f == {}, [runner])
        assert "cannot record" not in runner.text()
    finally:
        runner.stop()


def test_a_shared_service_has_a_row_on_each_node_and_a_failing_one_says_why(
        tmp_path: Path, cluster) -> None:
    env = _env(tmp_path / "home")
    _installed_fixture(env, "shared")
    rows, (a, b) = _store_project(tmp_path, cluster, env, "a", "b")
    node_a, node_b = "6e6f64652d61", "6e6f64652d62"
    for node in (node_a, node_b):
        _manager(a, env, "enable", "fixture", "--project", "--node", node)
    runners = [Runner(a, env, node_a, tmp_path / "a.log", child=FAILING),
               Runner(b, env, node_b, tmp_path / "b.log")]
    try:
        found = _instances(rows, lambda f: (f.get(node_a) or {}).get("state") == "failing"
                           and (f.get(node_b) or {}).get("state") == "ready", runners)
        failing = found[node_a]
        assert failing["ready"] is False and failing["last_exit_code"] == 6
        assert "must be a path inside" in failing["last_error"]
        assert rows("SELECT * FROM deployment_service_leases") == []

        # A runner that stops takes its node's row with it.
        runners.pop().stop()
        found = _instances(rows, lambda f: set(f) == {node_a}, runners)
        assert found[node_a]["state"] in ("failing", "starting")
    finally:
        for runner in runners:
            runner.stop()
    assert rows(INSTANCES) == []
