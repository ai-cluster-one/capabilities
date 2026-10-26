#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import pytest
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from datetime import datetime, timezone


CAPABILITY = Path(__file__).resolve().parents[1]
CLI = next((path for path in (
    CAPABILITY / "bin" / "automations", CAPABILITY / "automations")
    if path.is_file()), CAPABILITY / "bin" / "automations")
RUNTIME_PATH = CAPABILITY / "service" / "runtime.py"
MANAGER = next((path for path in (
    CAPABILITY.parents[1] / "bin" / "capabilities",
    CAPABILITY.parents[1] / ".manager" / "capabilities")
    if path.is_file()), Path(shutil.which("capabilities") or "capabilities"))

# The run ledger lives in PostgreSQL. The cases that reach it read
# AUTOMATIONS_TEST_DSN, a throwaway database's URL, and skip when it is unset;
# every case works under a project id of its own, so cases never see each
# other's runs.
DSN = os.environ.get("AUTOMATIONS_TEST_DSN")
needs_store = unittest.skipUnless(DSN, "AUTOMATIONS_TEST_DSN is unset")

SPEC = importlib.util.spec_from_file_location("automations_runtime_test", RUNTIME_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load automations runtime")
RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNTIME
SPEC.loader.exec_module(RUNTIME)


# The nodes the tests run as, by id: a label's first six characters as hex.
HERE = "686572650000"
LAB_NODE = "6c61622d6e6f"
NODE_B = "6e6f64652d62"
ELSEWHERE = "656c73657768"
PRODUCTION = "70726f647563"


class AutomationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        (self.root / "capabilities").mkdir(parents=True)
        (self.root / "capabilities" / "settings.json").write_text(
            json.dumps({"capabilities": {"automations": {"enabled": True}}}) + "\n"
        )
        # Runs are scoped to a project, so the project has to have said who it
        # is. The manager writes this file; a fixture that skipped it was only
        # ever describing a project that could not exist.
        # A slug belongs to one project, so a fixture that reuses one across
        # tests is describing two projects wearing the same label — which the
        # store refuses, correctly.
        self.project_id = str(uuid.uuid4())
        self.project_slug = "fixture-" + self.project_id[:8]
        (self.root / "capabilities" / "project.json").write_text(
            json.dumps({"schema": "capabilities.project.v1",
                        "id": self.project_id, "slug": self.project_slug}) + "\n"
        )
        # In-process code reads the ambient environment, not `self.env`, so a
        # fixture that only pointed the subprocesses at a scratch store was
        # writing its projects into the developer's real one. The store is the
        # throwaway database or none at all, and the machine's store setting is
        # out of reach either way.
        self.store_path = Path(self.tmp.name) / "store.db"
        self._env_before = {key: os.environ.get(key)
                            for key in ("AGENTKIT_DB_URL", "XDG_CONFIG_HOME", "AGENT_NODE_ID")}
        os.environ["XDG_CONFIG_HOME"] = str(Path(self.tmp.name) / "xdg-config")
        # The node this machine runs as, for the runtime loaded here as for
        # every process the tests start.
        os.environ["AGENT_NODE_ID"] = HERE
        if DSN:
            os.environ["AGENTKIT_DB_URL"] = DSN
        else:
            os.environ.pop("AGENTKIT_DB_URL", None)
        self.env = dict(os.environ)
        self.env.pop("AGENT_NODE_NAME", None)
        self.env.update(
            {
                "CLAUDE_PROJECT_DIR": str(self.root),
                "AUTOMATIONS_ENVIRONMENT": "test",
                "XDG_STATE_HOME": str(Path(self.tmp.name) / "xdg-state"),
            }
        )
        self.cli("service", "init")
        service = self.root / "capabilities" / "automations" / "service"
        scripts = self.root / "capabilities" / "automations" / "scripts"
        (scripts / "job.py").write_text(
            "#!/usr/bin/env python3\nimport os\nprint('done:' + os.environ['AUTOMATION_RUN_ID'])\n"
        )
        (scripts / "agentbin.py").write_text(
            "#!/usr/bin/env python3\nimport os\n"
            "print('bin:' + os.environ.get('AUTOMATIONS_BIN', 'MISSING'))\n"
        )
        (scripts / "slow.py").write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n"
        )
        (scripts / "flaky.py").write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "attempt = int(os.environ['AUTOMATION_ATTEMPT'])\n"
            "print(f'attempt:{attempt}')\n"
            "sys.exit(7 if attempt == 1 else 0)\n"
        )
        (service / "config.toml").write_text(
            """version = 1
[engine]
tick_seconds = 0.1
max_parallel = 2
shutdown_grace_seconds = 1
recovery = "retry"
environment = "test"
"""
        )
        # The automations are definitions in the store, scheduled on the node
        # the CLI runs as here: the id the fixture hands it.
        self.node = HERE
        if DSN:
            for slug, script, timeout, retries in (
                    ("job", "job.py", 5, 0), ("agentbin", "agentbin.py", 5, 0),
                    ("slow", "slow.py", 60, 0), ("timeout", "slow.py", 1, 0),
                    ("flaky", "flaky.py", 5, 1)):
                self.define(slug, script_path=f"capabilities/automations/scripts/{script}",
                            timeout_seconds=timeout, retries=retries, node=self.node)

    def define(self, slug: str, **fields) -> dict:
        """Save a version of a definition straight into the store, as `set` does."""
        with RUNTIME.RunLedger(self.project_id, root=self.root).open() as ledger:
            current = ledger.definition(slug)
            base = ({name: (list(current[name]) if name == "arguments" else current[name])
                     for name in RUNTIME.DEFINITION_FIELDS} if current else {})
            checked = RUNTIME.check_definition(self.root, slug, {**base, **fields})
            row, _created = ledger.save_definition(slug, checked, by="fixture")
        return row

    def place(self, *nodes: str) -> None:
        """Place this project's automations service on exactly `nodes`, as the
        manager's `capabilities place` writes it: a policy row naming each node
        with its service placed, in the contract's own tables."""
        from capabilities_contract import db
        from capabilities_contract.capability.tiers import store
        major, minor = store.STORE_TABLES_VERSION
        with db.connect(application_name="automations-test",
                        setting=RUNTIME.resolve_store(self.root)) as conn:
            conn.autocommit = True
            db.migrate(conn, store.STORE_OWNER, store.record_table_steps(),
                       major=major, minor=minor)
            with conn.transaction():
                conn.execute("DELETE FROM capabilities_policy WHERE agent_id = %s "
                             "AND capability = 'automations'", (self.project_id,))
                for node in nodes:
                    conn.execute("INSERT INTO capabilities_policy (agent_id, capability, "
                                 "node, enabled, service) VALUES (%s, 'automations', %s, "
                                 "true, true)", (self.project_id, node))

    def tearDown(self) -> None:
        with contextlib.suppress(Exception):
            self.cli("service", "stop", "--timeout", "2", "--force")
        for key, value in self._env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [str(CLI), *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and proc.returncode != 0:
            self.fail(f"{args} exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def wait_status(self, run_id: str, wanted: set[str], timeout: float = 8) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = json.loads(self.cli("show", run_id).stdout)
            if row["status"] in wanted:
                return row
            time.sleep(0.1)
        self.fail(f"run {run_id} did not reach {wanted}")

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            self.fail(f"git {args} exited {proc.returncode}\nstderr={proc.stderr}")
        return proc.stdout

    def commit_project(self) -> None:
        """Make the fixture a repository whose envelope carries the manager's guard.

        The guard is the manager's, so the manager writes it here rather than
        this suite restating what it holds."""
        home = Path(self.tmp.name) / "manager-home"
        home.mkdir(exist_ok=True)
        env = {**self.env, "HOME": str(home), "CAPABILITIES_HOME": str(home / ".capabilities"),
               "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache")}
        proc = subprocess.run([str(MANAGER), "init"], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "project")

    def staged(self) -> list[str]:
        """Everything a project that commits its whole body would commit now."""
        self.git("add", "-A")
        return self.git("diff", "--cached", "--name-status").splitlines()

    def live_daemon(self, state_dir: str | Path) -> int:
        """The pid of the daemon running on `state_dir`, once it has written it.

        `service start` answers a moment after the launch, and on a loaded
        machine that is before the daemon has written its pid file, so the
        `pid` in the answer can be null and the file can still name the daemon
        before it."""
        pid_file = Path(state_dir) / "daemon.pid"
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                return pid
            except (OSError, ValueError):
                time.sleep(0.1)
        self.fail(f"no running daemon wrote {pid_file}")

    def kill_daemon(self, pid: int) -> None:
        """End a daemon the way a crash or a reboot does: nothing runs after it."""
        os.kill(pid, signal.SIGKILL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"daemon {pid} survived SIGKILL")

    def test_agent_profiles_ship_without_configuration(self) -> None:
        agents = RUNTIME.load_agents({})
        self.assertEqual(agents["default"], "sonnet")
        self.assertEqual(sorted(agents["workers"]), ["haiku", "opus", "sonnet"])
        self.assertEqual(agents["workers"]["haiku"]["engine"], "claude")
        self.assertEqual(agents["workers"]["sonnet"]["mode"], "read")

    def test_default_state_is_project_scoped_under_xdg(self) -> None:
        status = json.loads(self.cli("service", "status").stdout)
        expected = (Path(self.env["XDG_STATE_HOME"]) / "capabilities" / "projects"
                    / self.project_slug / "automations")
        self.assertEqual(status["state_dir"], str(expected))
        self.assertNotEqual(expected, self.root / "capabilities" / "automations" / "state")

    def test_explicit_state_override_wins(self) -> None:
        override = Path(self.tmp.name) / "operator-state"
        self.env["AUTOMATIONS_STATE_DIR"] = str(override)
        status = json.loads(self.cli("service", "status").stdout)
        self.assertEqual(status["state_dir"], str(override))

    @needs_store
    def test_service_start_copies_durable_legacy_state_and_repoints_logs(self) -> None:
        legacy = self.root / "capabilities" / "automations" / "state"
        (legacy / "runs").mkdir(parents=True)
        (legacy / "scripts").mkdir()
        (legacy / "pytgcalls-upstream.json").write_text('{"cursor": 1}\n')
        (legacy / "scripts" / "cached.py").write_text("generated\n")
        (legacy / "automations.db").write_text("obsolete\n")

        config = self.loaded()
        row = RUNTIME.enqueue_manual(self.root, config, legacy, "job")
        self.assertIsNotNone(row)
        old_log = Path(row["log_path"])
        old_log.write_text("historical output\n")
        with RUNTIME.open_ledger(self.root, config) as ledger:
            ledger.update(row["id"], status="succeeded",
                          finished_at=datetime.now(timezone.utc).isoformat())

        started = json.loads(self.cli("service", "start").stdout)
        self.assertIn("state_migration", started)
        self.assertTrue(started["state_migration"]["source_preserved"])
        target = Path(started["state_dir"])
        self.assertEqual((target / "pytgcalls-upstream.json").read_text(), '{"cursor": 1}\n')
        self.assertFalse((target / "automations.db").exists())
        self.assertFalse((target / "scripts" / "cached.py").exists())
        shown = json.loads(self.cli("show", row["id"]).stdout)
        self.assertEqual(Path(shown["log_path"]), target / "runs" / old_log.name)
        self.assertEqual(Path(shown["log_path"]).read_text(), "historical output\n")
        self.assertTrue(old_log.is_file())

    @needs_store
    def test_service_start_refuses_conflicting_legacy_state(self) -> None:
        legacy = self.root / "capabilities" / "automations" / "state"
        legacy.mkdir(parents=True)
        (legacy / "cursor.json").write_text('{"source": 1}\n')
        target = (Path(self.env["XDG_STATE_HOME"]) / "capabilities" / "projects"
                  / self.project_slug / "automations")
        target.mkdir(parents=True)
        (target / "cursor.json").write_text('{"target": 2}\n')

        refused = self.cli("service", "start", check=False)
        self.assertEqual(refused.returncode, 6)
        error = json.loads(refused.stderr)["error"]
        self.assertEqual(error["code"], "state_migration_failed")
        self.assertEqual((legacy / "cursor.json").read_text(), '{"source": 1}\n')
        self.assertEqual((target / "cursor.json").read_text(), '{"target": 2}\n')

    @needs_store
    def test_service_starts_again_after_the_daemon_wrote_to_its_own_log(self) -> None:
        # The daemon's log is the one file the service itself makes diverge:
        # start opens the XDG copy in append mode, so the first line the daemon
        # writes there leaves it unequal to the legacy original. A migration
        # that compared them would refuse every start from then on.
        legacy = self.root / "capabilities" / "automations" / "state"
        legacy.mkdir(parents=True)
        (legacy / "daemon.log").write_text("output from the in-repo daemon\n")

        started = json.loads(self.cli("service", "start").stdout)
        self.assertTrue(started["started"])
        target = Path(started["state_dir"])

        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text().replace("max_parallel = 2", "max_parallel = 3", 1))
        self.assertTrue(json.loads(self.cli("service", "reload").stdout)["reloaded"])
        self.cli("service", "stop", "--timeout", "5", "--force")
        self.assertNotEqual((target / "daemon.log").read_bytes(),
                            (legacy / "daemon.log").read_bytes())
        self.assertTrue((target / "daemon.log").read_text().startswith(
            "output from the in-repo daemon\n"))

        restarted = json.loads(self.cli("service", "start").stdout)
        self.assertTrue(restarted["started"])
        self.assertEqual((legacy / "daemon.log").read_text(), "output from the in-repo daemon\n")

    @needs_store
    def test_run_supervised_project_still_reads_its_migrated_daemon_log(self) -> None:
        # `service start` is the only thing that ever creates the XDG daemon.log,
        # so a project supervised by `service run` has nothing there but what the
        # migration carried over. Declining to carry the file would turn this
        # project's `service logs` into logs_not_found.
        legacy = self.root / "capabilities" / "automations" / "state"
        legacy.mkdir(parents=True)
        (legacy / "daemon.log").write_text("output from the in-repo daemon\n")
        target = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])

        supervised = subprocess.Popen(
            [str(CLI), "service", "run"], cwd=self.root, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 15
            while time.time() < deadline and not (target / "daemon.pid").is_file():
                time.sleep(0.1)
            self.assertTrue((target / "daemon.pid").is_file(), "supervised daemon did not start")
        finally:
            supervised.terminate()
            supervised.wait(timeout=15)

        logs = json.loads(self.cli("service", "logs").stdout)
        self.assertEqual(logs["log_file"], str(target / "daemon.log"))
        self.assertEqual(logs["lines"], ["output from the in-repo daemon"])
        self.assertEqual((legacy / "daemon.log").read_text(), "output from the in-repo daemon\n")

    @needs_store
    def test_status_answers_for_the_state_root_a_supervised_daemon_pinned(self) -> None:
        # A supervisor's environment is invisible to a shell opened afterwards,
        # so an invocation that re-derives the root answers about a directory the
        # daemon never wrote to, and a live daemon reads as `running: false`. The
        # daemon records where its state went; the answer follows that record for
        # as long as a daemon is behind it, an explicit override still outranks
        # it, and a record no daemon answers for is reported and not followed.
        pinned = Path(self.tmp.name) / "pinned-state"
        default = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])
        self.assertNotEqual(pinned, default)

        supervised = subprocess.Popen(
            [str(CLI), "service", "run"], cwd=self.root,
            env={**self.env, "AUTOMATIONS_STATE_DIR": str(pinned)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 15
            while time.time() < deadline and not (pinned / "daemon.pid").is_file():
                time.sleep(0.1)
            self.assertTrue((pinned / "daemon.pid").is_file(), "supervised daemon did not start")

            status = json.loads(self.cli("service", "status").stdout)
            self.assertTrue(status["running"])
            self.assertEqual(status["pid"], int((pinned / "daemon.pid").read_text().strip()))
            self.assertEqual(status["state_dir"], str(pinned))
            self.assertEqual(status["state_dir_source"], "daemon_record")
            self.assertIn(str(default), status["state_dirs_considered"])

            elsewhere = Path(self.tmp.name) / "elsewhere"
            named = json.loads(subprocess.run(
                [str(CLI), "service", "status"], cwd=self.root,
                env={**self.env, "AUTOMATIONS_STATE_DIR": str(elsewhere)},
                capture_output=True, text=True, timeout=30).stdout)
            self.assertEqual(named["state_dir"], str(elsewhere))
            self.assertEqual(named["state_dir_source"], "override")
            self.assertFalse(named["running"])
        finally:
            supervised.terminate()
            supervised.wait(timeout=15)

        spent = json.loads(self.cli("service", "status").stdout)
        self.assertFalse(spent["running"])
        self.assertEqual(spent["state_dir"], str(default))
        self.assertEqual(spent["state_dir_source"], "default")
        self.assertIn(str(pinned), spent["state_dirs_considered"])

    @needs_store
    def test_a_daemon_starts_when_its_state_root_cannot_be_recorded(self) -> None:
        # An envelope that refuses the record - a read-only mount, foreign
        # ownership - is an inability to write down a fact about a daemon that
        # is starting regardless, and `service run` is a container's foreground
        # process. So the daemon must still exist; what must not happen is that
        # it goes unrecorded in silence, which is the invisibility the record
        # exists to remove. Each surface says so in the form it has: `run`
        # execs and has stderr, `start` returns a payload, and `status` is
        # where a reader checks a `running` they doubt.
        envelope = self.root / "capabilities" / "automations"
        marker = envelope / "state" / "state-root.json"
        pinned = Path(self.tmp.name) / "container-state"
        envelope.chmod(0o555)
        try:
            supervised = subprocess.Popen(
                [str(CLI), "service", "run"], cwd=self.root,
                env={**self.env, "AUTOMATIONS_STATE_DIR": str(pinned)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.time() + 15
                while time.time() < deadline and not (pinned / "daemon.pid").is_file():
                    time.sleep(0.1)
                self.assertTrue((pinned / "daemon.pid").is_file(),
                                "daemon did not start when its state root could not be recorded")
            finally:
                supervised.terminate()
                _out, err = supervised.communicate(timeout=15)
            self.assertIn("state root not recorded", err)
            self.assertFalse(marker.exists())

            started = json.loads(self.cli("service", "start").stdout)
            self.assertTrue(started["running"])
            self.assertIn("state root not recorded", started["state_record_error"])

            status = json.loads(self.cli("service", "status").stdout)
            self.assertTrue(status["running"])
            self.assertIn("cannot be written", status["state_record_error"])
        finally:
            envelope.chmod(0o755)
            self.cli("service", "stop", "--force", check=False)

    @needs_store
    def test_the_state_root_record_stays_out_of_what_the_project_commits(self) -> None:
        # The record names a path on this machine, and some projects commit their
        # whole body on an interval. It lives in the project state home, which the
        # manager's guard keeps out of every commit, whether the daemon was started
        # on the default root or under a supervisor's override.
        self.commit_project()
        marker = self.root / "capabilities" / "automations" / "state" / "state-root.json"

        started = json.loads(self.cli("service", "start").stdout)
        self.live_daemon(started["state_dir"])
        self.assertEqual(json.loads(marker.read_text())["state_dir"], started["state_dir"])
        self.assertEqual(self.staged(), [])
        self.cli("service", "stop", "--timeout", "5")

        pinned = Path(self.tmp.name) / "supervised-state"
        supervised = subprocess.Popen(
            [str(CLI), "service", "run"], cwd=self.root,
            env={**self.env, "AUTOMATIONS_STATE_DIR": str(pinned)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 15
            while time.time() < deadline and not (pinned / "daemon.pid").is_file():
                time.sleep(0.1)
            self.assertTrue((pinned / "daemon.pid").is_file(), "supervised daemon did not start")
            self.assertEqual(json.loads(marker.read_text())["state_dir"], str(pinned))
            self.assertEqual(self.staged(), [])
        finally:
            supervised.terminate()
            supervised.wait(timeout=15)

    @needs_store
    def test_a_daemon_an_earlier_release_started_is_still_found_and_stopped(self) -> None:
        # An earlier release wrote the record beside the state home. A daemon it
        # started under an override is findable through that record alone, so a
        # later shell still answers about it and `stop` still reaches it, and the
        # record goes with the daemon.
        envelope = self.root / "capabilities" / "automations"
        unguarded = envelope / "state-root.json"
        pinned = Path(self.tmp.name) / "earlier-release-state"
        started = json.loads(subprocess.run(
            [str(CLI), "service", "start"], cwd=self.root,
            env={**self.env, "AUTOMATIONS_STATE_DIR": str(pinned)},
            capture_output=True, text=True, timeout=30, check=True).stdout)
        pid = self.live_daemon(started["state_dir"])
        # What that release leaves behind: the same record, in its old place only.
        (envelope / "state" / "state-root.json").unlink()
        (envelope / "state").rmdir()
        unguarded.write_text(json.dumps({"state_dir": str(pinned)}, indent=2) + "\n")

        status = json.loads(self.cli("service", "status").stdout)
        self.assertTrue(status["running"])
        self.assertEqual(status["pid"], pid)
        self.assertEqual(status["state_dir"], str(pinned))
        self.assertEqual(status["state_dir_source"], "daemon_record")

        stopped = json.loads(self.cli("service", "stop", "--timeout", "10").stdout)
        self.assertTrue(stopped["stopped"])
        self.assertEqual(stopped["pid"], pid)
        self.assertNotIn("state_record_error", stopped)
        self.assertFalse(unguarded.exists())
        self.assertFalse((pinned / "daemon.pid").exists())

    @needs_store
    def test_the_first_start_stops_the_project_carrying_an_earlier_record(self) -> None:
        # A record an earlier release left beside the state home, committed with
        # the body, leaves the project at the first daemon start: the project's
        # next commit takes it out and adds nothing in its place.
        self.commit_project()
        unguarded = self.root / "capabilities" / "automations" / "state-root.json"
        unguarded.write_text(json.dumps(
            {"state_dir": str(Path(self.tmp.name) / "spent-state")}, indent=2) + "\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "a record an earlier release wrote")

        started = json.loads(self.cli("service", "start").stdout)
        self.assertTrue(started["started"])
        self.assertNotIn("state_record_error", started)
        self.assertFalse(unguarded.exists())
        self.assertEqual(self.staged(),
                         ["D\tcapabilities/automations/state-root.json"])
        self.git("commit", "-qm", "snapshot")
        self.assertFalse([path for path in self.git("ls-files").splitlines()
                          if path.endswith("state-root.json")])

    @needs_store
    def test_the_state_root_record_never_reads_as_the_envelope_layout(self) -> None:
        # The record lives in the directory the envelope layout kept its state in,
        # and the upgrade from that layout takes that directory's existence for
        # one. Nothing the record does - a start, a stop, a daemon that died with
        # its record still in place - may set the upgrade off or be copied by it.
        state_home = self.root / "capabilities" / "automations" / "state"
        first = json.loads(self.cli("service", "start").stdout)
        self.assertNotIn("state_migration", first)
        self.assertTrue((state_home / "state-root.json").is_file())
        self.live_daemon(first["state_dir"])
        self.cli("service", "stop", "--timeout", "5")
        self.assertFalse(state_home.exists())

        second = json.loads(self.cli("service", "start").stdout)
        self.assertNotIn("state_migration", second)
        self.kill_daemon(self.live_daemon(second["state_dir"]))
        self.assertTrue((state_home / "state-root.json").is_file())

        third = json.loads(self.cli("service", "start").stdout)
        self.assertTrue(third["started"])
        self.assertNotIn("state_migration", third)
        self.assertFalse((Path(third["state_dir"]) / "state-root.json").exists())
        self.live_daemon(third["state_dir"])
        self.cli("service", "stop", "--timeout", "5")
        self.assertFalse(state_home.exists())

    @needs_store
    def test_a_legacy_state_directory_the_record_shares_is_migrated_as_before(self) -> None:
        # A directory the record found already there is the envelope layout as
        # it always was, emptied or not: it is still migrated and kept, and the
        # record sharing it is neither copied nor a conflict.
        legacy = self.root / "capabilities" / "automations" / "state"
        legacy.mkdir(parents=True)
        (legacy / "cursor.json").write_text('{"cursor": 1}\n')

        first = json.loads(self.cli("service", "start").stdout)
        self.assertEqual(first["state_migration"]["files_copied"], 1)
        target = Path(first["state_dir"])
        self.kill_daemon(self.live_daemon(target))
        self.assertTrue((legacy / "state-root.json").is_file())

        second = json.loads(self.cli("service", "start").stdout)
        self.assertTrue(second["started"])
        self.assertEqual(second["state_migration"]["files_copied"], 0)
        self.assertFalse((target / "state-root.json").exists())
        self.live_daemon(target)
        self.cli("service", "stop", "--timeout", "5")
        self.assertEqual(sorted(path.name for path in legacy.iterdir()), ["cursor.json"])

        (legacy / "cursor.json").unlink()
        third = json.loads(self.cli("service", "start").stdout)
        self.assertEqual(third["state_migration"]["files_copied"], 0)
        self.live_daemon(target)
        self.cli("service", "stop", "--timeout", "5")
        self.assertTrue(legacy.is_dir())
        self.assertEqual(list(legacy.iterdir()), [])

    def test_declared_agent_adds_and_overrides_field_by_field(self) -> None:
        agents = RUNTIME.load_agents({
            "agents": {
                "default": "terra",
                "workers": {
                    "terra": {"engine": "codex", "model": "gpt-5.6-terra",
                              "effort": "high", "service_tier": "priority"},
                    "haiku": {"timeout_seconds": 42},
                },
            }
        })
        self.assertEqual(agents["default"], "terra")
        terra = agents["workers"]["terra"]
        self.assertEqual(terra["engine"], "codex")
        self.assertEqual(terra["service_tier"], "priority")
        self.assertEqual(terra["mode"], "read")
        haiku = agents["workers"]["haiku"]
        self.assertEqual(haiku["timeout_seconds"], 42)
        self.assertEqual(haiku["model"], "haiku")
        self.assertEqual(haiku["engine"], "claude")

    def test_agent_config_rejects_bad_shapes(self) -> None:
        cases = [
            {"workers": {"x": {"engine": "gemini", "model": "m"}}},
            {"workers": {"x": {"engine": "claude", "model": "m", "modle": "typo"}}},
            {"workers": {"x": {"engine": "claude", "model": "m", "effort": "turbo"}}},
            {"workers": {"x": {"engine": "claude", "model": "m", "service_tier": "priority"}}},
            {"workers": {"x": {"engine": "claude", "model": "m", "mode": "admin"}}},
            {"workers": {"x": {"engine": "claude"}}},
            {"default": "absent"},
            {"unknown": True},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(RUNTIME.ConfigError):
                    RUNTIME.load_agents({"agents": case})

    LEGACY = """
[[automations]]
id = "full"
name = "Full block"
description = "Every key a block may carry, in one place."
script = "capabilities/automations/scripts/job.py"
enabled = false
every_seconds = 300
timeout_seconds = 12.5
max_parallel = 2
max_pending = 3
overlap = "queue"
retries = 1
arguments = ["--apply"]
environments = ["test"]

[[automations]]
id = "nightly"
script = "capabilities/automations/scripts/job.py"
schedule = "0 3 * * *"
environments = ["production"]
"""

    def legacy_config(self, extra: str = LEGACY) -> Path:
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        text = config_path.read_text().replace(
            "[engine]\n", "# why the engine is tuned so\n[engine]\ntimezone = \"Etc/GMT-3\"\n")
        config_path.write_text(text + extra + "\n[agents]\ndefault = \"haiku\"\n")
        return config_path

    @needs_store
    def test_a_config_still_declaring_automations_refuses_and_names_the_import(self) -> None:
        # Nothing reads [[automations]] any more, so a command that went on as if
        # the file were empty would stop every automation without a word.
        self.legacy_config()
        for args in (("list",), ("doctor",), ("run", "job"), ("service", "start"),
                     ("set", "job", "--enabled", "false")):
            with self.subTest(args=args):
                refused = self.cli(*args, check=False)
                self.assertEqual(refused.returncode, 6, refused.stderr)
                error = json.loads(refused.stderr)["error"]
                self.assertEqual(error["code"], "definitions_not_imported")
                self.assertIn("automations import --apply", error["hint"])
        # A job mid-migration still gets its agent turn: `agents` reads [agents].
        self.assertEqual(json.loads(self.cli("agents").stdout)["default"], "haiku")

    @needs_store
    def test_import_moves_every_automation_as_a_path_and_empties_the_file(self) -> None:
        config_path = self.legacy_config()
        # `production` names no node here, so nothing is imported until one does.
        refused = self.cli("import", check=False)
        self.assertEqual(refused.returncode, 6)
        error = json.loads(refused.stderr)["error"]
        self.assertEqual(error["code"], "definitions_not_importable")
        self.assertIn("'nightly'", error["message"])
        self.assertIn("--node", error["message"])

        planned = json.loads(self.cli("import", "--node", f"production={NODE_B}").stdout)
        self.assertFalse(planned["applied"])
        self.assertEqual({a["automation"]: a["outcome"] for a in planned["automations"]},
                         {"full": "would_be_created", "nightly": "would_be_created"})
        self.assertIn("[[automations]]", config_path.read_text())

        applied = json.loads(self.cli("import", "--apply", "--node", f"production={NODE_B}").stdout)
        self.assertTrue(applied["applied"] and applied["config_rewritten"])
        text = config_path.read_text()
        self.assertNotIn("[[automations]]", text)
        self.assertNotIn("timezone", text)
        self.assertIn("# why the engine is tuned so\n[engine]\n", text)
        self.assertIn("[agents]\ndefault = \"haiku\"", text)

        listed = {a["id"]: a for a in json.loads(self.cli("list").stdout)["automations"]}
        full, nightly = listed["full"], listed["nightly"]
        self.assertEqual(full["description"],
                         "Full block - Every key a block may carry, in one place.")
        self.assertEqual((full["schedule"], full["timezone"], full["node"]),
                         ("*/5 * * * *", "Etc/GMT-3", self.node))
        self.assertEqual((full["enabled"], full["timeout_seconds"], full["retries"],
                          full["arguments"], full["script"], full["body"]),
                         (False, 13, 1, ["--apply"],
                          "capabilities/automations/scripts/job.py", False))
        self.assertEqual((nightly["schedule"], nightly["node"]), ("0 3 * * *", NODE_B))

        # Importing again finds nothing in the file, and the store unchanged.
        again = json.loads(self.cli("import", "--apply").stdout)
        self.assertEqual(again["automations"], [])

    @needs_store
    def test_import_refuses_an_entry_it_cannot_carry_and_imports_none(self) -> None:
        for extra, said in (
                ('\n[[automations]]\nid = "x"\nscript = "capabilities/automations/scripts/job.py"\n'
                 'args = ["--apply"]\n', "unknown key(s): args"),
                ('\n[[automations]]\nid = "x"\nscript = "capabilities/automations/scripts/job.py"\n'
                 'every_seconds = 30\n', "no five-field cron"),
                ('\n[[automations]]\nid = "job"\nscript = "capabilities/automations/scripts/slow.py"\n',
                 "differs from config.toml")):
            with self.subTest(said=said):
                config_path = self.legacy_config(extra)
                refused = self.cli("import", "--apply", check=False)
                self.assertEqual(refused.returncode, 6, refused.stderr)
                self.assertIn(said, refused.stderr)
                self.assertIn("[[automations]]", config_path.read_text())
                self.cli("service", "init", "--force")

    def test_a_definition_is_judged_whole_before_it_is_saved(self) -> None:
        script = "capabilities/automations/scripts/job.py"
        good = RUNTIME.check_definition(self.root, "job", {"script_path": script})
        self.assertEqual((good["timezone"], good["node"], good["enabled"]), ("UTC", None, True))
        for fields, said in (
                ({}, "exactly one of script_path and script_body"),
                ({"script_path": script, "script_body": "#!/bin/sh\n"}, "exactly one"),
                ({"script_path": "/etc/passwd"}, "relative to the project root"),
                ({"script_path": "../outside.py"}, "must not climb out"),
                ({"script_body": "echo no shebang\n"}, "#! line"),
                ({"script_path": script, "schedule": "every day"}, "five-field cron"),
                ({"script_path": script, "timezone": "Mars/Base"}, "unknown time zone"),
                ({"script_path": script, "node": " "}, "must name a node"),
                ({"script_path": script, "description": 12}, "description must be text"),
                ({"script_path": script, "retries": -1}, "retries must be an integer"),
                ({"script_path": script, "colour": "red"}, "unknown field(s): colour")):
            with self.subTest(said=said), self.assertRaises(RUNTIME.ConfigError) as caught:
                RUNTIME.check_definition(self.root, "job", fields)
            self.assertIn(said, str(caught.exception))
        with self.assertRaises(RUNTIME.ConfigError):
            RUNTIME.check_definition(self.root, "Job", {"script_path": script})

    @needs_store
    def test_a_described_automation_reaches_the_listing(self) -> None:
        # `description` has one consumer, a person reading the listing, so a
        # value that is saved and then dropped before this output has not
        # arrived anywhere.
        self.cli("set", "labelled", "--script", "capabilities/automations/scripts/job.py",
                 "--description", "Summarises yesterday's runs before the morning review.")
        listed = {item["id"]: item
                  for item in json.loads(self.cli("list").stdout)["automations"]}
        self.assertEqual(listed["labelled"]["description"],
                         "Summarises yesterday's runs before the morning review.")
        self.assertIsNone(listed["job"]["description"])

    @needs_store
    def test_a_definition_the_store_holds_but_does_not_validate_is_skipped_alone(self) -> None:
        # Another writer can put a row the CLI would refuse; the others still load.
        with RUNTIME.RunLedger(self.project_id, root=self.root).open() as ledger:
            ledger.save_definition("broken", {**RUNTIME.DEFINITION_DEFAULTS,
                                              "script_path": "capabilities/x.py",
                                              "schedule": "not a cron"}, by="elsewhere")
        listed = json.loads(self.cli("list").stdout)
        self.assertIn("job", {a["id"] for a in listed["automations"]})
        self.assertNotIn("broken", {a["id"] for a in listed["automations"]})
        self.assertIn("'broken' is skipped", listed["invalid_definitions"][0])
        doctor = self.cli("doctor", check=False)
        self.assertEqual(doctor.returncode, 6)
        self.assertIn("'broken' is skipped", json.loads(doctor.stdout)["invalid_definitions"][0])

    def test_agent_command_fences_read_and_opens_write(self) -> None:
        import importlib.util as _ilu
        spec = _ilu.spec_from_loader("automations_cli_test", loader=None)
        cli = _ilu.module_from_spec(spec)
        cli.__dict__["__file__"] = str(CLI)
        exec(compile(CLI.read_text(), str(CLI), "exec"), cli.__dict__)
        base = {"engine": "claude", "model": "sonnet", "effort": "high",
                "mode": "read", "timeout_seconds": 60.0, "service_tier": None}
        answer = Path(self.tmp.name) / "answer.txt"
        read = cli.__dict__["_agent_command"](base, self.root, None, answer)
        self.assertIn("plan", read)
        self.assertNotIn("bypassPermissions", read)
        # The fence has to remove the tools, not merely pre-approve three of
        # them: an allow rule leaves Bash and Write in the turn for whatever the
        # machine's own settings decide about them.
        self.assertEqual(read[read.index("--tools") + 1], "Read,Glob,Grep")
        write = cli.__dict__["_agent_command"]({**base, "mode": "write"},
                                               self.root, None, answer)
        self.assertIn("bypassPermissions", write)
        self.assertNotIn("plan", write)
        codex = cli.__dict__["_agent_command"](
            {**base, "engine": "codex", "model": "gpt-5.6-sol",
             "service_tier": "priority"}, self.root, None, answer)
        self.assertIn("read-only", codex)
        self.assertIn("model_service_tier=priority", codex)
        codex_write = cli.__dict__["_agent_command"](
            {**base, "engine": "codex", "model": "gpt-5.6-sol", "mode": "write"},
            self.root, None, answer)
        self.assertIn("workspace-write", codex_write)

    def test_agent_command_hands_only_act_the_machine(self) -> None:
        # `act` is the one mode that drops the codex sandbox rather than
        # choosing a narrower one, so the flag it produces is the whole
        # difference between a fenced worker and one holding the machine.
        # Nothing else in the capability names that flag, so a build that moved
        # it - off `act`, or onto `write` - changed who gets the machine without
        # anything failing.
        import importlib.util as _ilu
        spec = _ilu.spec_from_loader("automations_cli_act_test", loader=None)
        cli = _ilu.module_from_spec(spec)
        cli.__dict__["__file__"] = str(CLI)
        exec(compile(CLI.read_text(), str(CLI), "exec"), cli.__dict__)
        agent_command = cli.__dict__["_agent_command"]
        answer = Path(self.tmp.name) / "answer.txt"
        base = {"engine": "codex", "model": "gpt-5.6-sol", "effort": None,
                "mode": "act", "timeout_seconds": 60.0, "service_tier": None}
        act = agent_command(base, self.root, None, answer)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", act)
        # Dropping the sandbox means emitting no `-s` at all. A mode that still
        # selected one would still be sandboxed, whatever else it carried.
        self.assertNotIn("-s", act)
        for mode in ("read", "write"):
            with self.subTest(mode=mode):
                fenced = agent_command({**base, "mode": mode}, self.root, None, answer)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", fenced)
                self.assertIn("-s", fenced)
        # Claude has no sandbox to drop, so `act` reaches it as the permissive
        # mode and never carries codex's flag into the other engine's argv.
        claude_act = agent_command({**base, "engine": "claude", "model": "sonnet"},
                                   self.root, None, answer)
        self.assertIn("bypassPermissions", claude_act)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", claude_act)

    def test_agents_verb_lists_profiles(self) -> None:
        listed = json.loads(self.cli("agents").stdout)
        self.assertEqual(listed["default"], "sonnet")
        self.assertIn("haiku", listed["workers"])

    def _under_switch(self, value: str | None, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.pop("CAPABILITIES_READ_ONLY", None)
        if value is not None:
            env["CAPABILITIES_READ_ONLY"] = value
        return subprocess.run([str(CLI), *args], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=30)

    @needs_store
    def test_the_read_only_switch_refuses_what_changes_and_keeps_reads(self) -> None:
        # A writing agent profile, so the refusal of a turn that may change the
        # project is proven without ever starting an engine.
        config = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config.write_text(config.read_text() + (
            '\n[agents.workers.writer]\nengine = "claude"\nmodel = "sonnet"\n'
            'mode = "act"\n'))
        before = config.read_text()
        for args in (("service", "start"), ("service", "run"), ("service", "reload"),
                     ("service", "init", "--force"), ("run", "job"),
                     ("cancel", "some-run"), ("retry", "some-run"),
                     ("set", "job", "--enabled", "false"),
                     ("agent", "--profile", "writer", "change it")):
            with self.subTest(args=args):
                proc = self._under_switch("1", *args)
                self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
                error = json.loads(proc.stderr.strip().splitlines()[-1])["error"]
                self.assertEqual(error["code"], "read_only_switch")
                self.assertIn("CAPABILITIES_READ_ONLY", error["message"])
        self.assertEqual(config.read_text(), before)
        self.assertFalse(json.loads(self.cli("service", "status").stdout)["running"])
        # Reads keep working, and the run store they open is operational state.
        for args in (("list",), ("runs",), ("agents",), ("service", "status")):
            with self.subTest(args=args):
                proc = self._under_switch("true", *args)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    @needs_store
    def test_with_the_switch_off_a_manual_run_is_still_enqueued(self) -> None:
        for value in (None, "0", "false"):
            with self.subTest(value=value):
                proc = self._under_switch(value, "run", "job")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_agent_rejects_unknown_profile(self) -> None:
        proc = self.cli("agent", "--profile", "absent", "hello", check=False)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(json.loads(proc.stderr)["error"]["code"], "unknown_agent")

    @needs_store
    def test_job_receives_the_cli_path(self) -> None:
        self.cli("service", "start")
        queued = json.loads(self.cli("run", "agentbin").stdout)
        row = self.wait_status(queued["run"]["id"], {"succeeded"})
        logs = json.loads(self.cli("logs", row["id"]).stdout)
        reported = logs["lines"][-1].removeprefix("bin:")
        self.assertNotEqual(reported, "MISSING")
        self.assertTrue(os.access(reported, os.X_OK), reported)

    def loaded(self) -> dict:
        """The declaration as the daemon reads it: the file, and the store's
        definitions."""
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config = RUNTIME.load_config(self.root, config_path)
        with RUNTIME.open_ledger(self.root, config) as ledger:
            RUNTIME.apply_definitions(self.root, config, ledger.definitions())
        return config

    @needs_store
    def test_config_fingerprint_ignores_the_process_environment(self) -> None:
        # The daemon and whatever asks it for a health answer need not share an
        # environment, and if the fingerprint moved with one they would disagree
        # permanently: a stale verdict no restart could ever clear.
        before = os.environ.get("AUTOMATIONS_ENVIRONMENT")
        try:
            os.environ["AUTOMATIONS_ENVIRONMENT"] = "production"
            production = RUNTIME.config_fingerprint(self.loaded())
            os.environ["AUTOMATIONS_ENVIRONMENT"] = "development"
            development = RUNTIME.config_fingerprint(self.loaded())
        finally:
            if before is None:
                os.environ.pop("AUTOMATIONS_ENVIRONMENT", None)
            else:
                os.environ["AUTOMATIONS_ENVIRONMENT"] = before
        self.assertEqual(production, development)
        # A definition saved in the store moves it, from whichever machine.
        self.define("added", script_path="capabilities/automations/scripts/job.py")
        self.assertNotEqual(production, RUNTIME.config_fingerprint(self.loaded()))

    @needs_store
    def test_doctor_fails_while_the_daemon_runs_a_superseded_configuration(self) -> None:
        self.cli("service", "start")
        self.assertTrue(json.loads(self.cli("doctor").stdout)["ok"])

        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text().replace(
            "max_parallel = 2", "max_parallel = 3", 1))

        probe = self.cli("service", "doctor", check=False)
        self.assertEqual(probe.returncode, 6)
        report = json.loads(probe.stdout)
        self.assertFalse(report["ok"])
        self.assertIn("config_stale", report)
        self.assertNotEqual(report["config_stale"]["loaded"],
                            report["config_stale"]["current"])

        # Reloading is the whole remedy: the answer goes clean again without
        # stopping anything.
        self.cli("service", "reload")
        self.assertTrue(json.loads(self.cli("doctor").stdout)["ok"])

    @needs_store
    def test_a_definition_saved_anywhere_reaches_the_running_daemon_unasked(self) -> None:
        # The store announces every saved definition, and the daemon that
        # listens re-reads them: no reload, no file on its machine touched.
        started = json.loads(self.cli("service", "start").stdout)
        pid = self.live_daemon(started["state_dir"])
        self.define("added", script_path="capabilities/automations/scripts/job.py",
                    schedule="0 3 * * *", node=self.node)
        deadline = time.time() + 10
        report = None
        while time.time() < deadline:
            report = json.loads(self.cli("doctor", check=False).stdout)
            if report["ok"]:
                break
            time.sleep(0.2)
        self.assertTrue(report["ok"], report)
        self.assertEqual(json.loads(self.cli("service", "status").stdout)["pid"], pid)

    @needs_store
    def test_doctor_stays_quiet_about_configuration_while_stopped(self) -> None:
        # A stopped daemon is not running the wrong declaration; it is not
        # running one at all, and saying otherwise would restart nothing.
        self.define("added-while-stopped",
                    script_path="capabilities/automations/scripts/job.py")
        report = json.loads(self.cli("doctor").stdout)
        self.assertTrue(report["ok"])
        self.assertNotIn("config_stale", report)

    def _supervised_daemon(self, node: str) -> subprocess.Popen:
        state = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])
        daemon = subprocess.Popen(
            [str(CLI), "service", "run"], cwd=self.root,
            env={**self.env, "AGENT_NODE_ID": node},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 15
        while time.time() < deadline and not (state / "daemon.pid").is_file():
            time.sleep(0.1)
        self.assertTrue((state / "daemon.pid").is_file(), "supervised daemon did not start")
        return daemon

    @needs_store
    def test_doctor_reports_the_node_its_daemon_runs_as(self) -> None:
        # A supervisor starts the daemon with its own node name and the shell
        # that asks afterwards may carry none, so an answer computed from the
        # asking process describes nobody. Both belong in the payload, told apart.
        daemon = self._supervised_daemon(self.node)
        try:
            report = json.loads(self.cli("doctor").stdout)
            self.assertTrue(report["ok"])
            self.assertEqual(report["daemon_node"], self.node)
            self.assertEqual(report["service"]["daemon_node"], self.node)
            self.assertNotIn("node_idle", report)
            elsewhere = json.loads(subprocess.run(
                [str(CLI), "doctor"], cwd=self.root,
                env={**self.env, "AGENT_NODE_ID": ELSEWHERE},
                capture_output=True, text=True, timeout=30).stdout)
            self.assertEqual(elsewhere["node"], ELSEWHERE)
            self.assertEqual(elsewhere["daemon_node"], self.node)
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)

    @needs_store
    def test_doctor_fails_when_the_daemon_schedules_none_of_the_definitions(self) -> None:
        # The failure this closes: a daemon started as a node no automation
        # names is alive, answering, and scheduling nothing, and a count of the
        # definitions reports it as healthy. What a supervisor acts on is `ok`,
        # so this is where it has to show.
        daemon = self._supervised_daemon(PRODUCTION)
        try:
            probe = self.cli("service", "doctor", check=False)
            self.assertEqual(probe.returncode, 6)
            report = json.loads(probe.stdout)
            self.assertFalse(report["ok"])
            self.assertEqual(report["daemon_node"], PRODUCTION)
            self.assertEqual(report["node_idle"]["nodes"], [self.node])
            self.assertIn("scheduling nothing", report["node_idle"]["message"])
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)
        stopped = json.loads(self.cli("doctor").stdout)
        self.assertTrue(stopped["ok"])
        self.assertIsNone(stopped["daemon_node"])
        self.assertNotIn("node_idle", stopped)

    @needs_store
    def test_moving_the_service_carries_the_automations_of_the_node_it_left(self) -> None:
        # The failure this closes: the service moved to another node, every
        # automation still named the node it left, and the new node's daemon
        # scheduled nothing (node_idle) while the old node ran no daemon at all.
        # An automation naming no node follows the service, and so does one
        # naming a node the service is no longer placed on.
        self.define("unpinned", script_path="capabilities/automations/scripts/job.py",
                    schedule="0 3 * * *")
        self.place(PRODUCTION)
        daemon = self._supervised_daemon(PRODUCTION)
        try:
            probe = self.cli("doctor", check=False)
            self.assertEqual(probe.returncode, 0, probe.stdout)
            report = json.loads(probe.stdout)
            self.assertTrue(report["ok"])
            self.assertEqual(report["daemon_node"], PRODUCTION)
            self.assertNotIn("node_idle", report)
            inventory = json.loads(self.cli("inventory").stdout)
            self.assertIs(inventory["service"]["ok"], True)
            states = {item["name"]: item["state"] for item in inventory["items"]}
            self.assertEqual(states["job"], "active")
            self.assertEqual(states["unpinned"], "active")
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)
        listed = json.loads(subprocess.run(
            [str(CLI), "list"], cwd=self.root, env={**self.env, "AGENT_NODE_ID": PRODUCTION},
            capture_output=True, text=True, timeout=30).stdout)
        self.assertEqual(listed["placed_on"], [PRODUCTION])
        by_id = {item["id"]: item for item in listed["automations"]}
        # The stored node stays as saved; it is the placement that decides.
        self.assertEqual(by_id["job"]["node"], self.node)
        self.assertTrue(by_id["job"]["follows_service"])
        self.assertTrue(by_id["job"]["scheduled_here"])
        self.assertIsNone(by_id["unpinned"]["node"])
        self.assertTrue(by_id["unpinned"]["scheduled_here"])

        # A pin to a node the service is still placed on holds, and that
        # node alone schedules it.
        self.place(PRODUCTION, self.node)
        listed = json.loads(self.cli("list").stdout)
        by_id = {item["id"]: item for item in listed["automations"]}
        self.assertFalse(by_id["job"]["follows_service"])
        self.assertTrue(by_id["job"]["scheduled_here"])
        elsewhere = json.loads(subprocess.run(
            [str(CLI), "list"], cwd=self.root, env={**self.env, "AGENT_NODE_ID": PRODUCTION},
            capture_output=True, text=True, timeout=30).stdout)
        by_id = {item["id"]: item for item in elsewhere["automations"]}
        self.assertFalse(by_id["job"]["scheduled_here"])
        self.assertTrue(by_id["unpinned"]["scheduled_here"])

    @needs_store
    def test_a_manual_run_of_an_automation_following_the_service_goes_to_its_node(self) -> None:
        self.define("unpinned", script_path="capabilities/automations/scripts/job.py")
        self.place(PRODUCTION)
        queued = json.loads(self.cli("run", "unpinned").stdout)
        self.assertEqual(queued["run"]["node"], PRODUCTION)
        self.place()
        queued = json.loads(self.cli("run", "unpinned").stdout)
        self.assertEqual(queued["run"]["node"], self.node)

    @needs_store
    def test_a_daemon_that_published_no_node_is_reported_without_failing(self) -> None:
        # A daemon started by an older payload published no node and keeps
        # running across an upgrade; the absence of the fact that would decide
        # is not a mismatch.
        daemon = self._supervised_daemon(self.node)
        try:
            state = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])
            legacy = state / RUNTIME.DAEMON_FINGERPRINT_FILE
            fingerprint = RUNTIME.read_config_fingerprint(state)
            legacy.write_text(fingerprint + "\n")
            self.assertIsNone(RUNTIME.read_daemon_node(state))
            probe = self.cli("service", "doctor", check=False)
            self.assertEqual(probe.returncode, 0, probe.stdout)
            report = json.loads(probe.stdout)
            self.assertTrue(report["ok"])
            self.assertIsNone(report["daemon_node"])
            self.assertNotIn("node_idle", report)
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)

    @needs_store
    def test_inventory_judges_automations_by_the_running_daemons_node(self) -> None:
        # Whether an automation is active is decided by the daemon that runs it,
        # as the node it was started as, and a terminal asking later may be
        # another. Judged from the asker, every automation a supervised daemon
        # runs would read as another node's.
        self.define("nightly", script_path="capabilities/automations/scripts/job.py",
                    schedule="0 3 * * *", node=PRODUCTION)

        def states(report: dict) -> dict:
            return {item["name"]: item["state"] for item in report["items"]}

        def active(report: dict) -> dict:
            return next(m for m in report["metrics"] if m["label"] == "active here")

        idle = json.loads(self.cli("inventory").stdout)
        self.assertEqual(idle["service"]["state"], "stopped")
        self.assertIs(idle["service"]["ok"], True)
        self.assertEqual(states(idle)["job"], "active")
        self.assertEqual(states(idle)["nightly"], "other node")
        self.assertIn(self.node, active(idle)["note"])

        daemon = self._supervised_daemon(PRODUCTION)
        try:
            report = json.loads(self.cli("inventory").stdout)
            self.assertEqual(report["service"]["state"], "running")
            self.assertIn(PRODUCTION, report["service"]["detail"])
            self.assertIs(report["service"]["ok"], True)
            self.assertEqual(states(report)["nightly"], "active")
            self.assertEqual(states(report)["job"], "other node")
            self.assertEqual(active(report)["value"], 1)

            state = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])
            legacy = state / RUNTIME.DAEMON_FINGERPRINT_FILE
            legacy.write_text(RUNTIME.read_config_fingerprint(state) + "\n")
            unknown = json.loads(self.cli("inventory").stdout)
            self.assertEqual(states(unknown)["nightly"], "enabled")
            self.assertNotIn("active here", [m["label"] for m in unknown["metrics"]])

            config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
            config_path.write_text(config_path.read_text().replace(
                "max_parallel = 2", "max_parallel = 3", 1))
            stale = json.loads(self.cli("inventory").stdout)["service"]
            self.assertIs(stale["ok"], False)
            self.assertTrue(stale["problem"].startswith("config_stale: "))
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)

    @needs_store
    def test_inventory_item_carries_id_description_and_schedule_as_data(self) -> None:
        # A reader addresses an automation by its id and reads its schedule
        # without parsing display text; the contract's item has no such fields,
        # so they ride in `attributes` under fixed labels.
        self.define("nightly", script_path="capabilities/automations/scripts/job.py",
                    description="Summarises yesterday's runs.", schedule="0 3 * * *",
                    timezone="Etc/GMT-3", node=self.node)
        self.define("bodied", script_body="#!/bin/sh\necho hi\n")
        items = json.loads(self.cli("inventory").stdout)["items"]
        by_id = {}
        for item in items:
            self.assertEqual(set(item), {"group", "name", "state", "detail", "attributes"})
            attributes = [(row["label"], row["value"]) for row in item["attributes"]]
            by_id[dict(attributes)["id"]] = (item, attributes)

        nightly, attrs = by_id["nightly"]
        self.assertEqual(nightly["name"], "nightly")
        self.assertEqual(nightly["detail"], "0 3 * * *")
        self.assertEqual(attrs, [
            ("id", "nightly"), ("description", "Summarises yesterday's runs."),
            ("schedule_kind", "cron"), ("schedule_value", "0 3 * * *"),
            ("timezone", "Etc/GMT-3"),
            ("script", "capabilities/automations/scripts/job.py"), ("node", self.node)])

        bodied, attrs = by_id["bodied"]
        self.assertEqual((bodied["state"], bodied["detail"]), ("active", "manual"))
        self.assertEqual(attrs, [("id", "bodied"), ("schedule_kind", "manual"),
                                 ("script", "stored body")])

    @needs_store
    def test_set_saves_a_version_and_an_unchanged_set_saves_none(self) -> None:
        first = json.loads(self.cli("set", "job", "--description", "Does the job").stdout)
        self.assertTrue(first["created"])
        self.assertEqual(first["changed"], ["description"])
        self.assertEqual(first["item"]["description"], "Does the job")
        same = json.loads(self.cli("set", "job", "--description", "Does the job").stdout)
        self.assertFalse(same["created"])
        self.assertEqual(same["version"], first["version"])
        moved = json.loads(self.cli("set", "job", "--node", NODE_B, "--schedule",
                                    "*/10 * * * *", "--timezone", "Etc/GMT-3",
                                    "--timeout", "30", "--retries", "2",
                                    "--arguments", '["--all"]').stdout)
        self.assertEqual(moved["changed"], ["arguments", "schedule", "timezone", "node",
                                            "timeout_seconds", "retries"])
        cleared = json.loads(self.cli("set", "job", "--node", "", "--schedule", "",
                                      "--description", "").stdout)
        self.assertEqual((cleared["item"]["node"], cleared["item"]["schedule"],
                          cleared["item"]["description"]), (None, None, None))
        history = json.loads(self.cli("history", "job").stdout)["versions"]
        self.assertEqual([v["active"] for v in history], [False, False, False, True])
        self.assertEqual(history[0]["created_by"], "fixture")
        self.assertEqual(history[2]["node"], NODE_B)
        # A writer that read an older version is told rather than overwriting.
        stale = self.cli("set", "job", "--enabled", "false", "--base", first["version"],
                         check=False)
        self.assertEqual(stale.returncode, 6)
        self.assertEqual(json.loads(stale.stderr)["error"]["code"], "conflict")

    @needs_store
    def test_set_creates_with_a_script_and_moves_a_script_into_a_body(self) -> None:
        missing = self.cli("set", "new-one", "--enabled", "true", check=False)
        self.assertEqual(missing.returncode, 3)
        created = json.loads(self.cli("set", "new-one", "--script",
                                      "capabilities/automations/scripts/job.py").stdout)
        self.assertTrue(created["created"])
        body = Path(self.tmp.name) / "body.sh"
        body.write_text("#!/bin/sh\necho from the store\n")
        moved = json.loads(self.cli("set", "new-one", "--body", str(body)).stdout)
        self.assertEqual((moved["item"]["script"], moved["item"]["body"]), (None, True))
        self.assertEqual(sorted(moved["changed"]), ["script_body", "script_path"])
        version = json.loads(self.cli("history", "new-one").stdout)["versions"][-1]
        self.assertEqual(version["script_body"], "#!/bin/sh\necho from the store\n")
        removed = json.loads(self.cli("remove", "new-one").stdout)
        self.assertEqual(removed["removed"], moved["version"])
        self.assertNotIn("new-one", {a["id"] for a in json.loads(self.cli("list").stdout)
                                     ["automations"]})
        self.assertEqual(self.cli("remove", "new-one", check=False).returncode, 3)

    @needs_store
    def test_set_refuses_invalid_requests_and_saves_nothing(self) -> None:
        before = json.loads(self.cli("history", "job").stdout)["versions"]
        cases = [
            (("--colour", "red"), "input"),
            (("--enabled", "yes"), "invalid_enabled"),
            (("--description", "two\nlines"), "invalid_text"),
            (("--description", "x" * 501), "too_long"),
            (("--schedule", "every day"), "invalid_definition"),
            (("--timezone", "Mars/Base"), "invalid_definition"),
            (("--timeout", "soon"), "input"),
            (("--script", "../outside.py"), "invalid_definition"),
            (("--script", "a.py", "--body", "-"), "input"),
            (("--arguments", "--all"), "input"),
            ((), "input"),
        ]
        for args, code in cases:
            with self.subTest(args=args):
                refused = self.cli("set", "job", *args, check=False)
                self.assertEqual(refused.returncode, 6, refused.stderr)
                self.assertEqual(json.loads(refused.stderr)["error"]["code"], code)
        self.assertEqual(json.loads(self.cli("history", "job").stdout)["versions"], before)

    @needs_store
    def test_writing_definitions_requires_an_explicit_project_enable(self) -> None:
        # Global inheritance runs the other verbs, but writing the project's
        # definitions is gated as `service init` is.
        config_home = Path(self.tmp.name) / "xdg-config"
        (config_home / "capabilities").mkdir(parents=True, exist_ok=True)
        (config_home / "capabilities" / "settings.json").write_text(
            json.dumps({"capabilities": {"automations": {"enabled": True}}}) + "\n")
        (self.root / "capabilities" / "settings.json").write_text(
            json.dumps({"capabilities": {}}) + "\n")
        self.env["XDG_CONFIG_HOME"] = str(config_home)
        before = json.loads(self.cli("history", "job").stdout)["versions"]
        self.cli("list")
        for args in (("set", "job", "--enabled", "false"), ("remove", "job"),
                     ("import", "--apply")):
            with self.subTest(args=args):
                refused = self.cli(*args, check=False)
                self.assertEqual(refused.returncode, 4, refused.stderr)
                self.assertIn("project_enable_required", refused.stderr)
        self.assertEqual(json.loads(self.cli("history", "job").stdout)["versions"], before)

    def test_without_a_store_the_service_refuses_and_says_why(self) -> None:
        """Runtime state has no local default: with no store configured nothing
        that would keep a run starts, and each answer names the fix."""
        self.env.pop("AGENTKIT_DB_URL", None)
        for args in (("service", "start"), ("service", "run"), ("service", "doctor"),
                     ("doctor",), ("run", "job")):
            proc = self.cli(*args, check=False)
            self.assertEqual(proc.returncode, 6, (args, proc.stderr))
            error = json.loads(proc.stderr)["error"]
            self.assertEqual(error["code"], "store_not_configured", args)
            self.assertIn("capabilities store set", error["hint"], args)
        status = json.loads(self.cli("service", "status").stdout)
        self.assertFalse(status["running"])
        self.assertIsNone(status["store"])
        self.assertEqual(status["store_error"]["code"], "store_not_configured")
        self.assertFalse(self.store_path.exists())

    @needs_store
    def test_a_manual_run_is_a_row_of_the_run_ledger(self) -> None:
        import socket
        status = json.loads(self.cli("service", "status").stdout)
        self.assertEqual(status["store"], "environment")
        self.assertEqual(status["store_sources"], ["AGENTKIT_DB_URL"])
        self.assertEqual(status["schema"], "agentkit")
        run = json.loads(self.cli("run", "job").stdout)["run"]
        conn = RUNTIME._database().connect(application_name="automations-test")
        try:
            row = conn.execute(
                "SELECT project_id, automation_slug, trigger, host "
                "FROM automations_runs WHERE id = %s", [run["id"]]).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (self.project_id, "job", "manual", socket.gethostname()))
        self.assertFalse(self.store_path.exists())
        repaired = json.loads(self.cli("doctor", "--repair").stdout)["repaired"]
        self.assertFalse(repaired["repaired"])

    @needs_store
    def test_a_run_is_recorded_under_the_node_s_declared_name(self) -> None:
        # A manual run goes to the automation's node, and to the machine that
        # asked when it names none; the host is the machine, whatever its node.
        self.env["AGENT_NODE_ID"] = LAB_NODE
        self.define("job", node=None)
        run = json.loads(self.cli("run", "job").stdout)["run"]
        placed = json.loads(self.cli("run", "slow").stdout)["run"]
        conn = RUNTIME._database().connect(application_name="automations-test")
        try:
            row = conn.execute(
                "SELECT node, host, environment FROM automations_runs WHERE id = %s",
                [run["id"]]).fetchone()
            other = conn.execute(
                "SELECT node FROM automations_runs WHERE id = %s", [placed["id"]]).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (LAB_NODE, socket.gethostname(), LAB_NODE))
        self.assertEqual(other, (self.node,))

    @needs_store
    def test_a_run_the_daemon_queues_is_recorded_under_the_node_s_declared_name(self) -> None:
        self.env["AGENT_NODE_ID"] = LAB_NODE
        self.define("flaky", node=LAB_NODE)
        self.cli("service", "start")
        first = json.loads(self.cli("run", "flaky").stdout)["run"]
        self.wait_status(first["id"], {"failed"})
        deadline = time.time() + 8
        row = None
        conn = RUNTIME._database().connect(application_name="automations-test")
        try:
            while time.time() < deadline and row is None:
                row = conn.execute(
                    "SELECT trigger, node FROM automations_runs WHERE parent_run_id = %s",
                    [first["id"]]).fetchone()
                time.sleep(0.1)
        finally:
            conn.close()
        self.assertEqual(row, ("retry", LAB_NODE))

    @needs_store
    def test_manual_run_history_and_logs(self) -> None:
        doctor = json.loads(self.cli("doctor").stdout)
        self.assertTrue(doctor["ok"])
        self.cli("service", "start")
        queued = json.loads(self.cli("run", "job").stdout)
        row = self.wait_status(queued["run"]["id"], {"succeeded"})
        self.assertEqual(row["exit_code"], 0)
        logs = json.loads(self.cli("logs", row["id"]).stdout)
        self.assertIn("done:", logs["lines"][-1])

    def test_manager_installs_complete_bundle(self) -> None:
        home = Path(self.tmp.name) / "install-home"
        cap_home = home / ".capabilities"
        bin_dir = Path(self.tmp.name) / "install-bin"
        home.mkdir()
        bin_dir.mkdir()
        env = dict(self.env)
        env.update(
            {
                "HOME": str(home),
                # The install writes this machine's ceiling; keep it scratch.
                "XDG_CONFIG_HOME": str(home / ".config"),
                "CAPABILITIES_HOME": str(cap_home),
                "CAPABILITIES_BIN": str(bin_dir),
                "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
            }
        )
        proc = subprocess.run(
            [str(MANAGER), "install", "automations", "--from", str(CAPABILITY), "--yes"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((cap_home / "automations" / "service" / "runtime.py").is_file())
        manifest = subprocess.run(
            [str(bin_dir / "automations"), "manifest", "--json"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(manifest.returncode, 0, manifest.stderr)
        self.assertEqual(json.loads(manifest.stdout)["service"]["name"], "scheduler")
        service = json.loads(manifest.stdout)["service"]
        self.assertIn("$XDG_STATE_HOME", service["state"])
        mounts = {item["name"]: item for item in service["deploy"]["mounts"]}
        self.assertEqual(mounts["automations_state"]["target"],
                         "{agent_home}/.local/state/capabilities/projects")

    @needs_store
    def test_service_reload_publishes_an_edited_declaration_without_restarting(self) -> None:
        self.cli("service", "start")
        pid = json.loads(self.cli("service", "status").stdout)["pid"]

        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text().replace(
            "max_parallel = 2", "max_parallel = 3", 1))
        self.assertFalse(json.loads(self.cli("service", "doctor", check=False).stdout)["ok"])

        published = json.loads(self.cli("service", "reload").stdout)
        self.assertTrue(published["reloaded"])
        # The same process took it up. A new pid here would mean the daemon was
        # replaced, which is the thing a reload exists to avoid.
        self.assertEqual(published["pid"], pid)
        self.assertTrue(json.loads(self.cli("service", "doctor").stdout)["ok"])
        self.assertEqual(json.loads(self.cli("service", "status").stdout)["pid"], pid)

        # Asking again is not an error and does not signal a daemon that is
        # already current.
        again = json.loads(self.cli("service", "reload").stdout)
        self.assertFalse(again["reloaded"])

    @needs_store
    def test_service_reload_refuses_a_declaration_that_does_not_load(self) -> None:
        self.cli("service", "start")
        pid = json.loads(self.cli("service", "status").stdout)["pid"]
        state = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])
        loaded = RUNTIME.read_config_fingerprint(state)

        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        good = config_path.read_text()
        config_path.write_text(good + "\nthis is not toml [[[\n")

        refused = self.cli("service", "reload", check=False)
        self.assertEqual(refused.returncode, 6)
        self.assertIn("invalid_config", refused.stderr)
        # A daemon scheduling work correctly is not disturbed by an edit that
        # cannot be read. Liveness is asked of the process itself, because every
        # verb that would answer it also has to read the file that is broken.
        os.kill(pid, 0)
        self.assertEqual(RUNTIME.read_config_fingerprint(state), loaded)

        # And once the file parses again it is the same daemon that answers.
        config_path.write_text(good)
        self.assertEqual(json.loads(self.cli("service", "status").stdout)["pid"], pid)

    @needs_store
    def test_service_reload_leaves_running_work_alone(self) -> None:
        self.cli("service", "start")
        queued = json.loads(self.cli("run", "slow").stdout)
        run_id = queued["run"]["id"]
        self.wait_status(run_id, {"running"})

        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text().replace(
            "max_parallel = 2", "max_parallel = 3", 1))
        self.assertTrue(json.loads(self.cli("service", "reload").stdout)["reloaded"])

        # This is the whole difference between reloading and restarting: work
        # already dispatched finishes under the declaration that started it.
        row = self.wait_status(run_id, {"running"})
        self.assertEqual(row["status"], "running")
        self.cli("cancel", run_id)
        self.wait_status(run_id, {"canceled"})

    @needs_store
    def test_cancel_running_job(self) -> None:
        self.cli("service", "start")
        queued = json.loads(self.cli("run", "slow").stdout)
        run_id = queued["run"]["id"]
        self.wait_status(run_id, {"running"})
        self.cli("cancel", run_id)
        row = self.wait_status(run_id, {"canceled"})
        self.assertEqual(row["status"], "canceled")

    @needs_store
    def test_a_disabled_automation_is_not_runnable(self) -> None:
        self.define("job", enabled=False)
        proc = self.cli("run", "job", check=False)
        self.assertEqual(proc.returncode, 6)
        self.assertIn("not_runnable", proc.stderr)

    def test_numeric_cron_matching(self) -> None:
        monday = datetime(2026, 7, 20, 8, 10, tzinfo=timezone.utc)
        self.assertTrue(RUNTIME.cron_matches("*/5 8 * * 1", monday))
        self.assertFalse(RUNTIME.cron_matches("*/5 9 * * 1", monday))
        with self.assertRaises(RUNTIME.ConfigError):
            RUNTIME.parse_cron("0 8 * JAN MON")

    @needs_store
    def test_ticker_deduplicates_one_firing(self) -> None:
        self.define("scheduled", script_path="capabilities/automations/scripts/job.py",
                    schedule="*/5 * * * *", timezone="Etc/GMT-3", node=self.node)
        self.define("elsewhere", script_path="capabilities/automations/scripts/job.py",
                    schedule="*/5 * * * *", node=NODE_B)
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        state_dir = self.root / "schedule-state"
        daemon = RUNTIME.Daemon(self.root, config_path, state_dir)
        try:
            when = datetime(2026, 7, 20, 8, 10, 15, tzinfo=timezone.utc)
            daemon.schedule_due(when)
            daemon.schedule_due(when)
        finally:
            daemon.runs.close()
        rows = RUNTIME.list_runs(self.root, self.loaded(), limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["automation_slug"], "scheduled")
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["trigger"], "schedule")
        self.assertEqual(rows[0]["dedupe_key"], "scheduled:2026-07-20T08:10:00+00:00")
        self.assertEqual(rows[0]["node"], self.node)
        self.assertIsNotNone(rows[0]["definition_id"])

    @needs_store
    def test_startup_recovery_requeues_by_policy(self) -> None:
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        state_dir = self.root / "capabilities" / "automations" / "state"
        config = self.loaded()
        row = RUNTIME.enqueue_manual(self.root, config, state_dir, "job")
        self.assertIsNotNone(row)
        with RUNTIME.open_ledger(self.root, config) as ledger:
            ledger.update(row["id"], status="running")
        daemon = RUNTIME.Daemon(self.root, config_path, state_dir)
        try:
            daemon.recover()
        finally:
            daemon.runs.close()
        rows = RUNTIME.list_runs(self.root, config, limit=10)
        original = next(item for item in rows if item["id"] == row["id"])
        recovered = next(item for item in rows if item["parent_run_id"] == row["id"])
        self.assertEqual(original["status"], "interrupted")
        self.assertEqual(recovered["status"], "pending")
        self.assertEqual(recovered["trigger"], "recovery")

    @needs_store
    def test_a_script_body_runs_from_the_store_as_a_file_script_runs(self) -> None:
        # The body is written to a file of its own for the one run, run through
        # its #! line in the project with the same variables, and removed after.
        self.define("bodied", script_body="#!/bin/sh\n"
                    "echo \"$AUTOMATION_ID $1 $AUTOMATION_ENVIRONMENT $(pwd)\"\n",
                    arguments=["first"], node=self.node)
        started = json.loads(self.cli("service", "start").stdout)
        run = json.loads(self.cli("run", "bodied").stdout)["run"]
        row = self.wait_status(run["id"], {"succeeded", "failed"})
        self.assertEqual(row["status"], "succeeded", row)
        self.assertEqual(row["summary"], f"bodied first test {self.root.resolve()}")
        self.assertEqual(row["definition_id"],
                         json.loads(self.cli("history", "bodied").stdout)["versions"][-1]["version"])
        self.assertEqual(list((Path(started["state_dir"]) / "bodies").iterdir()), [])

    @needs_store
    def test_a_run_receives_the_result_the_last_run_recorded(self) -> None:
        # A run records its result on its own row; the next run of the same
        # automation is handed the last one recorded, and a run that records
        # nothing (here, once the result is two long) hands nothing new on.
        self.define("counter", script_body="#!/bin/sh\n"
                    "echo \"got:${AUTOMATION_LAST_RESULT-none}\"\n"
                    "[ \"${#AUTOMATION_LAST_RESULT}\" -ge 2 ] && exit 0\n"
                    "\"$AUTOMATIONS_BIN\" result \"n${AUTOMATION_LAST_RESULT-}\" >/dev/null\n",
                    node=self.node)
        self.cli("service", "start")
        rows = []
        for _ in range(3):
            run = json.loads(self.cli("run", "counter").stdout)["run"]
            rows.append(self.wait_status(run["id"], {"succeeded", "failed"}))
        self.assertEqual([row["status"] for row in rows], ["succeeded"] * 3, rows)
        self.assertEqual([row["summary"] for row in rows],
                         ["got:none", "got:n", "got:nn"])
        self.assertEqual([row["result"] for row in rows], ["n", "nn", None])
        # Outside a job there is no run to record on.
        outside = self.cli("result", "x", check=False)
        self.assertEqual(outside.returncode, 6, outside.stdout)

    @needs_store
    def test_two_machines_on_one_store_never_take_each_others_runs(self) -> None:
        # A second machine is a checkout of its own of the same project, with a
        # state root and a node name of its own, on the same store. Each node
        # recovers, claims and fires only its own runs, and switching an
        # automation's node from either machine moves where it runs without
        # touching the other machine's files.
        root_b = Path(self.tmp.name) / "machine-b" / "project"
        shutil.copytree(self.root, root_b)
        env_b = {**self.env, "CLAUDE_PROJECT_DIR": str(root_b), "AGENT_NODE_ID": NODE_B,
                 "XDG_STATE_HOME": str(Path(self.tmp.name) / "machine-b" / "xdg-state")}

        def cli_b(*args: str) -> dict:
            proc = subprocess.run([str(CLI), *args], cwd=root_b, env=env_b,
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        started_b = cli_b("service", "start")
        try:
            self.assertEqual(started_b["daemon_node"], NODE_B)
            self.cli("service", "start")
            here = self.wait_status(json.loads(self.cli("run", "job").stdout)["run"]["id"],
                                    {"succeeded"})
            self.assertEqual((here["node"], here["host"]), (self.node, socket.gethostname()))
            self.assertTrue(here["log_path"].startswith(
                json.loads(self.cli("service", "status").stdout)["state_dir"]))
            # Moved to node-b from this machine: its runs are node-b's, and run
            # there, from node-b's own checkout and state root.
            self.cli("set", "job", "--node", NODE_B)
            there = json.loads(self.cli("run", "job").stdout)["run"]
            self.assertEqual(there["node"], NODE_B)
            finished = self.wait_status(there["id"], {"succeeded"})
            self.assertTrue(finished["log_path"].startswith(started_b["state_dir"]), finished)
            # A restart of this node closes what it ran and nothing of node-b's.
            mine = json.loads(self.cli("run", "slow").stdout)["run"]
            self.wait_status(mine["id"], {"running"})
            self.cli("set", "slow", "--node", NODE_B)
            theirs = json.loads(self.cli("run", "slow").stdout)["run"]
            self.cli("service", "stop", "--timeout", "10")
            self.assertEqual(json.loads(self.cli("show", mine["id"]).stdout)["status"],
                             "interrupted")
            self.wait_status(theirs["id"], {"running"}, timeout=15)
            self.cli("service", "start")
            time.sleep(1)
            self.assertEqual(json.loads(self.cli("show", theirs["id"]).stdout)["status"],
                             "running")
            self.cli("cancel", theirs["id"])
            self.wait_status(theirs["id"], {"canceled"})
        finally:
            cli_b("service", "stop", "--timeout", "10", "--force")

    @needs_store
    def test_timeout_and_automatic_retry(self) -> None:
        self.cli("service", "start")
        timeout_run = json.loads(self.cli("run", "timeout").stdout)["run"]
        timed_out = self.wait_status(timeout_run["id"], {"failed"})
        self.assertIn("timed out", timed_out["summary"])

        first = json.loads(self.cli("run", "flaky").stdout)["run"]
        self.wait_status(first["id"], {"failed"})
        deadline = time.time() + 8
        while time.time() < deadline:
            rows = json.loads(self.cli("runs", "--limit", "20").stdout)["runs"]
            retries = [
                row
                for row in rows
                if row["automation_slug"] == "flaky" and row["parent_run_id"] == first["id"]
            ]
            if retries and retries[0]["status"] == "succeeded":
                self.assertEqual(retries[0]["attempt"], 2)
                return
            time.sleep(0.1)
        self.fail("automatic retry did not succeed")


if __name__ == "__main__":
    unittest.main()


# --- the ledger on a shared store --------------------------------------------

@pytest.fixture
def ledger_env(tmp_path, monkeypatch):
    """The throwaway database as the store, and the machine's setting out of reach."""
    if not DSN:
        pytest.skip("AUTOMATIONS_TEST_DSN is unset")
    monkeypatch.setenv("AGENTKIT_DB_URL", DSN)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    (tmp_path / "runs").mkdir(exist_ok=True)
    opened = []

    def ledger(project_id=None, node="node-a"):
        monkeypatch.setenv("AUTOMATIONS_NODE_NAME", node)
        led = RUNTIME.RunLedger(project_id or f"prj_{uuid.uuid4().hex[:12]}").open()
        opened.append(led)
        return led

    yield ledger
    for led in opened:
        led.close()


DEDUPE = "nightly:2026-08-23T03:00:00+00:00"


def test_two_machines_cannot_both_claim_one_scheduled_firing(tmp_path, ledger_env):
    """The whole reason the ledger lives in a shared store: two daemons of one
    project, on two machines, race one firing and exactly one owns it."""
    a = ledger_env()
    b = ledger_env(a.project_id, node=NODE_B)
    first = a.claim("nightly", tmp_path, trigger="schedule", dedupe_key=DEDUPE)
    second = b.claim("nightly", tmp_path, trigger="schedule", dedupe_key=DEDUPE)
    assert first is not None and second is None
    assert len(a.list()) == 1


def test_a_different_firing_of_the_same_automation_still_claims(tmp_path, ledger_env):
    led = ledger_env()
    assert led.claim("nightly", tmp_path, trigger="schedule", dedupe_key=DEDUPE)
    assert led.claim("nightly", tmp_path, trigger="schedule",
                     dedupe_key=DEDUPE.replace("08-23", "08-24"))
    assert len(led.list()) == 2


def test_two_projects_never_take_each_others_firings(tmp_path, ledger_env):
    """One database serves every project, and two projects in directories of the
    same name build the same dedupe key; each still owns its own firing."""
    ours, theirs = ledger_env(), ledger_env()
    assert ours.claim("nightly", tmp_path, trigger="schedule", dedupe_key=DEDUPE)
    assert theirs.claim("nightly", tmp_path, trigger="schedule", dedupe_key=DEDUPE)


def test_the_ledger_answers_only_about_its_own_project(tmp_path, ledger_env):
    """The reason scoping is a boundary and not a WHERE clause fifteen callers
    are trusted to remember."""
    ours, others = ledger_env(), ledger_env()
    assert ours.claim("nightly", tmp_path, trigger="manual") is not None
    assert others.claim("nightly", tmp_path, trigger="manual") is not None

    assert len(ours.list()) == 1
    assert len(others.list()) == 1
    assert ours.counts() == {"pending": 1}

    # and a run belonging to the other project is invisible, not merely filtered
    theirs_run = others.list()[0]
    assert ours.get(theirs_run["id"]) is None


def test_the_ledger_counts_per_automation_within_the_project(tmp_path, ledger_env):
    led = ledger_env()
    led.claim("nightly", tmp_path, trigger="manual")

    assert led.count_for("nightly", "pending") == 1
    assert led.count_for("other-thing", "pending") == 0
    assert led.has_active("nightly", ["pending"]) is True
    assert led.running() == 0


def test_two_machines_on_one_store_read_only_their_own_runs(tmp_path, ledger_env):
    """Recovery, the queue and the running count read only the node's own runs,
    while one run of an automation at a time still holds across both."""
    a = ledger_env(node="node-a")
    b = ledger_env(a.project_id, node=NODE_B)
    queued = a.claim("nightly", tmp_path, trigger="manual")
    started = a.claim("other", tmp_path, trigger="manual")
    a.update(started["id"], status="running", pid=os.getpid())
    assert [row["id"] for row in a.pending()] == [queued["id"]]
    assert b.pending() == []
    assert [row["id"] for row in a.unfinished()] == [started["id"]]
    assert b.unfinished() == []
    assert (a.running(), b.running()) == (1, 0)
    assert b.has_active("other", ["running"]) is True
    assert b.claim("nightly", tmp_path, trigger="manual", node="node-a")["node"] == "node-a"
    assert b.pending() == []


def test_another_nodes_run_past_its_timeout_is_closed_for_the_automation(tmp_path, ledger_env):
    """A node that went away cannot hold its automation forever: past the
    timeout and grace another node marks its run interrupted, and never its own."""
    a = ledger_env(node="node-a")
    b = ledger_env(a.project_id, node=NODE_B)
    gone = a.claim("nightly", tmp_path, trigger="manual")
    a.update(gone["id"], status="running", started_at="2026-01-01T00:00:00+00:00")
    mine = b.claim("nightly", tmp_path, trigger="manual")
    b.update(mine["id"], status="running", started_at="2026-01-01T00:00:00+00:00")
    assert b.expire_foreign("nightly", 60) == 1
    assert a.get(gone["id"])["status"] == "interrupted"
    assert "node-a" in a.get(gone["id"])["summary"]
    assert b.get(mine["id"])["status"] == "running"
    fresh = a.claim("nightly", tmp_path, trigger="manual")
    assert b.expire_foreign("nightly", 60) == 0
    assert a.get(fresh["id"])["status"] == "pending"


def test_a_writer_from_before_the_node_column_still_records_its_runs(tmp_path, ledger_env):
    """A machine still on the release before `node` inserts none; its host was
    its node name then, and the run is that node's rather than refused."""
    led = ledger_env()
    led._execute(
        "INSERT INTO automations_runs (id, project_id, automation_slug, environment, "
        "trigger, status, queued_at, log_path, host) VALUES "
        "(%s, %s, 'nightly', 'production', 'manual', 'pending', %s, '/tmp/x.log', 'old-node')",
        [uuid.uuid4().hex, led.project_id, RUNTIME.iso()])
    assert [row["node"] for row in led.list()] == ["old-node"]


def test_a_run_reads_as_it_always_has_and_says_where_it_was_recorded(tmp_path, ledger_env):
    """Times come back as the ISO text they were written as and the cancel flag
    as 0 or 1, so `runs` and `show` print what they printed before; the host is
    the machine the run was recorded on."""
    import socket
    led = ledger_env()
    row = led.claim("nightly", tmp_path, trigger="schedule",
                    scheduled_for="2026-08-23T03:00:00+00:00", dedupe_key=DEDUPE)
    assert row["scheduled_for"] == "2026-08-23T03:00:00+00:00"
    assert row["queued_at"].endswith("+00:00")
    assert row["cancel_requested"] == 0
    assert row["host"] == socket.gethostname()
    assert row["project_id"] == led.project_id
    assert "automation_id" not in row
    led.update(row["id"], cancel_requested=True, finished_at="2026-08-23T03:00:05+00:00")
    row = led.get(row["id"])
    assert row["cancel_requested"] == 1
    assert row["finished_at"] == "2026-08-23T03:00:05+00:00"


def test_a_run_the_schema_cannot_hold_fails_loudly(tmp_path, ledger_env):
    """The dedupe key is the only conflict a claim treats as someone else's win;
    a run the table cannot hold raises instead of vanishing."""
    led = ledger_env()
    with pytest.raises(Exception) as caught:
        led.claim("nightly", tmp_path, trigger="manual", definition_id=str(uuid.uuid4()))
    assert "foreign key" in str(caught.value)


def test_a_dedupe_collision_still_yields_rather_than_raising(tmp_path, ledger_env):
    led = ledger_env()
    assert led.claim("nightly", tmp_path, trigger="schedule", dedupe_key=DEDUPE) is not None
    assert led.claim("nightly", tmp_path, trigger="schedule", dedupe_key=DEDUPE) is None


def test_a_dropped_connection_is_noticed_and_reopened(tmp_path, ledger_env):
    """The store is across a network; a connection the server ends between
    statements, or inside a transaction that has only read, is replaced by the
    database library on the next one, which carries on, and one it ends inside
    a transaction that took a lock is noticed as lost, not taken for a fault in
    the work, and a new one carries on."""
    led = ledger_env()
    led.claim("nightly", tmp_path, trigger="manual")
    ended = led.conn.info.backend_pid
    ledger_env().conn.execute("SELECT pg_terminate_backend(%s)", [ended])
    assert len(led.list()) == 1
    assert not led.lost() and led.conn.info.backend_pid != ended
    with led.transaction():
        led.list()
        ledger_env().conn.execute("SELECT pg_terminate_backend(%s)",
                                  [led.conn.info.backend_pid])
        assert len(led.list()) == 1
    with pytest.raises(Exception), led.transaction():
        led.conn.execute("SELECT pg_advisory_xact_lock(1)")
        ledger_env().conn.execute("SELECT pg_terminate_backend(%s)",
                                  [led.conn.info.backend_pid])
        led.list()
    assert led.lost()
    led.reopen()
    assert not led.lost()
    assert len(led.list()) == 1


def test_the_ledger_refuses_without_a_store(tmp_path, monkeypatch):
    """Runtime state has no local default: with no store configured the ledger
    does not open, and says so in the library's words."""
    monkeypatch.delenv("AGENTKIT_DB_URL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    root = tmp_path / "project"
    (root / "capabilities").mkdir(parents=True)
    (root / "capabilities" / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v2", "slug": "nostore"}))
    (root / ".env.local").write_text("CAPABILITIES_PROJECT_ID=prj_nostore00000\n")
    with pytest.raises(RUNTIME.StoreUnavailable) as caught:
        RUNTIME.open_ledger(root, {"engine": {"environment": "development"}})
    assert caught.value.slug == "store_not_configured"
    assert "capabilities store set" in caught.value.hint
    assert not (tmp_path / "store.db").exists()


def test_registration_stamps_the_id_its_launcher_resolved(tmp_path, monkeypatch):
    """The id a run is registered under is the one the automations CLI resolved
    for a write: installed in process, handed to the daemon it launches, and
    the project root's own binding in .env.local only where no launcher
    resolved one; project.json's is never it."""
    identity = {"id": "prj_stale0000000", "slug": "labelled"}
    (tmp_path / ".env.local").write_text("CAPABILITIES_PROJECT_ID=prj_copy00000000\n")
    monkeypatch.setattr(RUNTIME, "PROJECT_ID_FOR_WRITE", None)
    monkeypatch.delenv("CAPABILITIES_PROJECT_ID", raising=False)
    monkeypatch.delenv("CAPABILITIES_PROJECT_ID_ROOT", raising=False)
    assert RUNTIME._registration_id(tmp_path, identity) == "prj_copy00000000"
    monkeypatch.setenv("CAPABILITIES_PROJECT_ID", "prj_handed000000")
    monkeypatch.setenv("CAPABILITIES_PROJECT_ID_ROOT", str(tmp_path))
    assert RUNTIME._registration_id(tmp_path, identity) == "prj_handed000000"
    monkeypatch.setenv("CAPABILITIES_PROJECT_ID_ROOT", str(tmp_path / "elsewhere"))
    assert RUNTIME._registration_id(tmp_path, identity) == "prj_copy00000000"
    asked = []
    monkeypatch.setattr(RUNTIME, "PROJECT_ID_FOR_WRITE",
                        lambda strict=True: asked.append(strict) or "prj_resolved0000")
    assert RUNTIME._registration_id(tmp_path, identity) == "prj_resolved0000"
    assert RUNTIME._registration_id(tmp_path, identity, strict=False) == "prj_resolved0000"
    assert asked == [True, False]


def test_a_read_with_no_id_to_read_under_reads_as_empty(tmp_path, ledger_env, monkeypatch):
    """A read never refuses over the project id, and a project with no id reads
    as empty, never as every project."""
    other = ledger_env()
    other.claim("nightly", tmp_path, trigger="manual")
    root = tmp_path / "project"
    (root / "capabilities").mkdir(parents=True)
    (root / "capabilities" / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "slug": "unstamped"}))
    monkeypatch.setattr(RUNTIME, "PROJECT_ID_FOR_WRITE", lambda strict=True: None)
    monkeypatch.delenv("CAPABILITIES_PROJECT_ID", raising=False)
    with RUNTIME.open_ledger(root, {"engine": {"environment": "development"}},
                             strict=False) as ledger:
        assert ledger.project_id == ""
        assert ledger.list(limit=10) == []
    with pytest.raises(RUNTIME.ConfigError):
        RUNTIME.open_ledger(root, {"engine": {"environment": "development"}})


def test_an_automation_follows_the_service_unless_pinned_where_it_is_placed() -> None:
    def item(node, enabled=True):
        return {"node": node, "enabled": enabled}
    # Unpinned: wherever the service is placed, and everywhere while no
    # placement is recorded.
    assert RUNTIME.scheduled_here(item(None), NODE_B, [NODE_B])
    assert not RUNTIME.scheduled_here(item(None), HERE, [NODE_B])
    assert RUNTIME.scheduled_here(item(None), HERE, None)
    assert RUNTIME.scheduled_here(item(None), HERE, [])
    # A pin holds where the service is placed, and where nothing is recorded.
    assert RUNTIME.scheduled_here(item(HERE), HERE, [HERE, NODE_B])
    assert not RUNTIME.scheduled_here(item(HERE), NODE_B, [HERE, NODE_B])
    assert RUNTIME.scheduled_here(item(HERE), HERE, None)
    assert not RUNTIME.scheduled_here(item(HERE), NODE_B, None)
    # A pin to a node the service left follows the service.
    assert RUNTIME.follows_service(item(HERE), [NODE_B])
    assert RUNTIME.scheduled_here(item(HERE), NODE_B, [NODE_B])
    assert not RUNTIME.scheduled_here(item(HERE), HERE, [NODE_B])
    assert not RUNTIME.scheduled_here(item(None, enabled=False), NODE_B, [NODE_B])
    assert RUNTIME.manual_run_node(item(None), HERE, [NODE_B]) == NODE_B
    assert RUNTIME.manual_run_node(item(None), HERE, None) == HERE
    assert RUNTIME.manual_run_node(item(HERE), NODE_B, [HERE]) == HERE
    assert RUNTIME.manual_run_node(item(HERE), NODE_B, [NODE_B]) == NODE_B


# --- supervising jobs while the store is gone ---------------------------------
#
# The daemon's own table of the processes it started is the truth about its
# jobs; the ledger is written from it afterwards. These cases hold a ledger
# whose every statement fails as a dropped connection would, so nothing about
# stopping, timing out or killing a job can wait on a write.

class _StoreGone(Exception):
    pass


class DownLedger:
    """A ledger whose store is gone: every statement raises."""

    environment = "test"
    warnings: list[str] = []

    def lost(self) -> bool:
        return True

    def reopen(self) -> None:
        raise RUNTIME.StoreUnavailable("store_unreachable", "cannot reach the store")

    def close(self) -> None:
        pass

    def __getattr__(self, name):
        def fail(*_args, **_kwargs):
            raise _StoreGone(f"{name}: server closed the connection unexpectedly")
        return fail


class UpLedger:
    """A ledger whose store answers: it keeps the columns written per run."""

    environment = "test"
    warnings: list[str] = []
    node = "node-a"
    host = "host-a"

    def __init__(self, cancel: set[str] = frozenset()):
        self.rows: dict[str, dict] = {}
        self.cancel = set(cancel)

    def lost(self) -> bool:
        return False

    def close(self) -> None:
        pass

    def get(self, run_id):
        return {**self.rows.get(run_id, {}), "cancel_requested": int(run_id in self.cancel),
                "automation_slug": "job", "attempt": 1, "parent_run_id": None}

    def update(self, run_id, **columns):
        self.rows.setdefault(run_id, {}).update(columns)


STUBBORN = "trap '' TERM\necho ready\nwhile :; do sleep 0.1; done"


def _scheduler(tmp_path, *, grace=0.5, timeout=60):
    """A daemon over a scratch project with two automations: `job` ends on
    SIGTERM, `stubborn` ignores it and ends only when killed."""
    root = tmp_path / "project"
    (root / "jobs").mkdir(parents=True)
    declared = "version = 1\n[engine]\nmax_parallel = 8\n" \
               f"shutdown_grace_seconds = {grace}\nenvironment = \"test\"\n"
    rows = []
    for slug, body in (("job", "exec sleep 60"), ("stubborn", STUBBORN)):
        job = root / "jobs" / f"{slug}.sh"
        job.write_text(f"#!/bin/sh\n{body}\n")
        job.chmod(0o755)
        rows.append({**RUNTIME.DEFINITION_DEFAULTS, "id": str(uuid.uuid4()), "slug": slug,
                     "script_path": f"jobs/{slug}.sh", "timeout_seconds": timeout,
                     "node": "node-a", "created_at": None, "created_by": None})
    config_path = root / "config.toml"
    config_path.write_text(declared)
    said: list[str] = []
    daemon = RUNTIME.Daemon(root, config_path, tmp_path / "state", slug="scratch",
                            loader=lambda: RUNTIME.load_config(root, config_path),
                            runs=DefinedLedger(rows), log=said.append)
    daemon.runs = DownLedger()
    return daemon, said


class _QuietLink:
    generation = 1

    def take_changed(self, _project_id):
        return False


class DefinedLedger:
    """A ledger that answers only with the definitions it was given, for a
    daemon to be built over."""

    warnings: list[str] = []
    project_id = "prj_fixture"
    node = "node-a"
    host = "host-a"
    link = _QuietLink()

    def __init__(self, rows):
        self.rows = rows

    def definitions(self):
        return self.rows

    def placed_nodes(self):
        return None


def _start(daemon, run_id, slug="job"):
    """Start a run of `slug`; a `stubborn` one is returned once its trap is set."""
    log_path = daemon.state_dir / "runs" / f"{run_id}.log"
    row = {"id": run_id, "automation_slug": slug, "attempt": 1, "trigger": "manual",
           "environment": "test", "log_path": str(log_path)}
    with contextlib.suppress(_StoreGone):
        daemon._start(row)
    until = time.monotonic() + 10
    while slug == "stubborn" and "ready" not in (
            log_path.read_text() if log_path.exists() else ""):
        assert time.monotonic() < until, "the stubborn job never set its trap"
        time.sleep(0.02)
    return daemon.children[run_id].process


def _alive(process) -> bool:
    """Whether anything in the job's process group still runs. A group whose
    leader is an unreaped zombie answers EPERM on macOS, and runs nothing."""
    try:
        os.killpg(process.pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.fixture
def reaped():
    """Every job group a case starts is gone after it, whatever the case did."""
    started: list = []
    yield started
    for process in started:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            process.wait(timeout=2)


def test_a_job_started_as_the_store_drops_is_still_the_daemons(tmp_path, reaped):
    daemon, _ = _scheduler(tmp_path)
    with pytest.raises(_StoreGone):
        daemon._start({"id": "r1", "automation_slug": "job", "attempt": 1,
                       "trigger": "manual", "environment": "test",
                       "log_path": str(daemon.state_dir / "runs" / "r1.log")})
    child = daemon.children["r1"]
    reaped.append(child.process)
    assert _alive(child.process) and not child.started

    daemon.runs = UpLedger()
    daemon.reap()
    assert daemon.runs.rows["r1"]["status"] == "running"
    assert daemon.runs.rows["r1"]["pid"] == child.process.pid
    assert child.started and "r1" in daemon.children


def test_a_job_that_overruns_is_stopped_while_the_store_is_gone(tmp_path, reaped):
    daemon, said = _scheduler(tmp_path, timeout=1)
    process = _start(daemon, "r1")
    reaped.append(process)
    until = time.monotonic() + 4
    while _alive(process) and time.monotonic() < until:
        daemon.tick()
        time.sleep(0.05)
    daemon.tick()
    assert not _alive(process)
    assert any("lost the store connection" in line for line in said)
    held = daemon.children["r1"].outcome
    assert held["status"] == "failed" and "timed out" in held["summary"]

    daemon.runs = UpLedger()
    daemon.reap()
    assert daemon.runs.rows["r1"]["status"] == "failed"
    assert daemon.runs.rows["r1"]["started_at"]
    assert "timed out" in daemon.runs.rows["r1"]["summary"]
    assert daemon.children == {}


def test_a_cancel_the_store_cannot_be_asked_for_waits_and_is_then_taken(tmp_path, reaped):
    daemon, _ = _scheduler(tmp_path)
    process = _start(daemon, "r1")
    reaped.append(process)
    with pytest.raises(_StoreGone):
        daemon.reap()
    assert _alive(process)

    daemon.runs = UpLedger(cancel={"r1"})
    until = time.monotonic() + 4
    while "r1" in daemon.children and time.monotonic() < until:
        daemon.reap()
        time.sleep(0.05)
    assert not _alive(process)
    assert daemon.runs.rows["r1"]["status"] == "canceled"


def test_a_stop_with_the_store_gone_ends_every_job_and_names_what_it_could_not_record(
        tmp_path, reaped):
    daemon, said = _scheduler(tmp_path, grace=0.5)
    honours = _start(daemon, "r1")
    ignore_a, ignore_b = _start(daemon, "r2", "stubborn"), _start(daemon, "r3", "stubborn")
    reaped.extend([honours, ignore_a, ignore_b])
    time.sleep(0.3)

    began = time.monotonic()
    daemon.shutdown()
    assert time.monotonic() - began < 0.5 + 2 * 3 + 1
    assert not any(_alive(p) for p in (honours, ignore_a, ignore_b))
    assert daemon.children == {}
    for run_id in ("r1", "r2", "r3"):
        assert any(line.startswith(f"run {run_id} ended with exit code") for line in said)


def test_a_stop_with_the_store_up_records_every_job_interrupted(tmp_path, reaped):
    daemon, _ = _scheduler(tmp_path, grace=0.5)
    daemon.runs = UpLedger()
    honours = _start(daemon, "r1")
    ignores = _start(daemon, "r2", "stubborn")
    reaped.extend([honours, ignores])
    time.sleep(0.3)
    daemon.shutdown("stopping for the test")
    rows = daemon.runs.rows
    assert {rows[r]["status"] for r in ("r1", "r2")} == {"interrupted"}
    assert rows["r1"]["exit_code"] == -signal.SIGTERM
    assert rows["r2"].get("exit_code") in (None, -signal.SIGKILL)
    assert rows["r2"]["summary"] == "stopping for the test"
    assert not _alive(honours) and not _alive(ignores)


def test_the_machine_service_ends_every_projects_jobs_with_the_store_gone(tmp_path, reaped):
    daemons = []
    for name in ("one", "two"):
        daemon, _ = _scheduler(tmp_path / name, grace=0.3)
        reaped.extend([_start(daemon, f"{name}-a", "stubborn"),
                       _start(daemon, f"{name}-b", "stubborn")])
        daemons.append(daemon)
    time.sleep(0.3)
    machine = RUNTIME.MachineService.__new__(RUNTIME.MachineService)
    slots = [types.SimpleNamespace(daemon=d, release=lambda said: None) for d in daemons]
    machine.slots = lambda: slots
    machine.draining, machine.entries = [], {}
    RUNTIME.MachineService.shutdown(machine)
    assert not any(_alive(p) for p in reaped)
    assert all(d.children == {} for d in daemons)


def test_the_machine_service_times_out_jobs_while_its_store_link_is_lost(tmp_path, reaped):
    daemon, _ = _scheduler(tmp_path, timeout=1)
    process = _start(daemon, "r1")
    reaped.append(process)
    machine = RUNTIME.MachineService.__new__(RUNTIME.MachineService)
    link = types.SimpleNamespace(lost=lambda: True)
    slots = [types.SimpleNamespace(daemon=daemon, link=link)]
    machine.slots = lambda: slots
    machine.draining, machine.entries = [], {}
    machine._take_list = machine.publish = lambda: None
    machine._reconnect = lambda link, why: None
    machine.links = {"the store": link}
    machine.settings = {}
    until = time.monotonic() + 4
    while _alive(process) and time.monotonic() < until:
        machine.step()
        time.sleep(0.05)
    machine.step()
    assert not _alive(process)
    assert daemon.children["r1"].outcome["status"] == "failed"
