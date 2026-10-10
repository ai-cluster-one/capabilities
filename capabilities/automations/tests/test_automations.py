#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import pytest
import shutil
import signal
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
                            for key in ("AGENTKIT_DB_URL", "XDG_CONFIG_HOME")}
        os.environ["XDG_CONFIG_HOME"] = str(Path(self.tmp.name) / "xdg-config")
        if DSN:
            os.environ["AGENTKIT_DB_URL"] = DSN
        else:
            os.environ.pop("AGENTKIT_DB_URL", None)
        self.env = dict(os.environ)
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
timezone = "UTC"
shutdown_grace_seconds = 1
recovery = "retry"
environment = "test"

[[automations]]
id = "job"
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
timeout_seconds = 5
max_parallel = 1
max_pending = 2
overlap = "queue"
retries = 0

[[automations]]
id = "agentbin"
environments = ["test"]
script = "capabilities/automations/scripts/agentbin.py"
timeout_seconds = 5
max_parallel = 1
max_pending = 1
overlap = "skip"
retries = 0

[[automations]]
id = "slow"
environments = ["test"]
script = "capabilities/automations/scripts/slow.py"
timeout_seconds = 60
max_parallel = 1
max_pending = 1
overlap = "skip"
retries = 0

[[automations]]
id = "timeout"
environments = ["test"]
script = "capabilities/automations/scripts/slow.py"
timeout_seconds = 1
max_parallel = 1
max_pending = 1
overlap = "skip"
retries = 0

[[automations]]
id = "flaky"
environments = ["test"]
script = "capabilities/automations/scripts/flaky.py"
timeout_seconds = 5
max_parallel = 1
max_pending = 1
overlap = "queue"
retries = 1
"""
        )

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

        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config = RUNTIME.load_config(self.root, config_path)
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

    def test_automation_block_refuses_a_key_it_does_not_read(self) -> None:
        # The dropped key was the whole defect: `args` where the runtime reads
        # `arguments` left the automation running on schedule, exiting zero, and
        # passing nothing — a green record every tick and no work done.
        with self.assertRaises(RUNTIME.ConfigError) as caught:
            RUNTIME.normalise_config(self.root, {"version": 1, "automations": [{
                "id": "job", "script": "capabilities/automations/scripts/job.py",
                "args": ["--apply"], "retires": 2,
            }]})
        self.assertEqual(str(caught.exception),
                         "automations[0] has unknown key(s): args, retires")

        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "misspelled"
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
args = ["--apply"]
""")
        refused = self.cli("service", "doctor", check=False)
        self.assertEqual(refused.returncode, 6)
        self.assertEqual(json.loads(refused.stderr)["error"]["code"], "invalid_config")
        self.assertIn("automations[5] has unknown key(s): args", refused.stderr)

    def test_every_key_the_block_loop_reads_is_accepted(self) -> None:
        # The refusal above is only correct while this set is exactly what the
        # loop below it reads, so the set is pinned from the other side: a key
        # dropped from it would start refusing a config that was always valid.
        declared = {
            "id": "full", "name": "Full block",
            "description": "Every key a block may carry, in one place.",
            "script": "capabilities/automations/scripts/job.py",
            "enabled": False, "every_seconds": 30, "timeout_seconds": 12,
            "max_parallel": 2, "max_pending": 3, "overlap": "queue",
            "retries": 1, "arguments": ["--apply"], "environments": ["test"],
        }
        normalised = RUNTIME.normalise_config(
            self.root, {"version": 1, "automations": [declared]})["automations"][0]
        for key, value in declared.items():
            self.assertEqual(normalised[key], value, key)
        # `schedule` is the last and cannot share a block with `every_seconds`.
        scheduled = {**declared, "schedule": "0 3 * * *"}
        scheduled.pop("every_seconds")
        self.assertEqual(RUNTIME.normalise_config(
            self.root, {"version": 1, "automations": [scheduled]})["automations"][0]["schedule"],
            "0 3 * * *")
        self.assertEqual(set(RUNTIME.AUTOMATION_KEYS), set(declared) | {"schedule"})

    def test_a_labelled_automation_reaches_the_listing(self) -> None:
        # `name` and `description` have one consumer, a person reading the
        # listing, so a value that loads and is then dropped between the config
        # and this output has not arrived anywhere.
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "labelled"
name = "Nightly digest"
description = "Summarises yesterday's runs before the morning review."
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
""")
        listed = {item["id"]: item
                  for item in json.loads(self.cli("list").stdout)["automations"]}
        self.assertEqual(listed["labelled"]["name"], "Nightly digest")
        self.assertEqual(listed["labelled"]["description"],
                         "Summarises yesterday's runs before the morning review.")
        # Both are optional, and a block declaring neither reads as it always did.
        self.assertIsNone(listed["job"]["name"])
        self.assertIsNone(listed["job"]["description"])

    def test_a_label_must_be_text(self) -> None:
        for key in ("name", "description"):
            with self.subTest(key=key), self.assertRaises(RUNTIME.ConfigError) as caught:
                RUNTIME.normalise_config(self.root, {"version": 1, "automations": [{
                    "id": "job", "script": "capabilities/automations/scripts/job.py",
                    key: 12,
                }]})
            self.assertEqual(str(caught.exception), f"automations[0].{key} must be a string")

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

    def test_config_fingerprint_ignores_the_process_environment(self) -> None:
        # The daemon and whatever asks it for a health answer need not share an
        # environment, and if the fingerprint moved with one they would disagree
        # permanently: a stale verdict no restart could ever clear.
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        before = os.environ.get("AUTOMATIONS_ENVIRONMENT")
        try:
            os.environ["AUTOMATIONS_ENVIRONMENT"] = "production"
            production = RUNTIME.config_fingerprint(
                RUNTIME.load_config(self.root, config_path))
            os.environ["AUTOMATIONS_ENVIRONMENT"] = "development"
            development = RUNTIME.config_fingerprint(
                RUNTIME.load_config(self.root, config_path))
        finally:
            if before is None:
                os.environ.pop("AUTOMATIONS_ENVIRONMENT", None)
            else:
                os.environ["AUTOMATIONS_ENVIRONMENT"] = before
        self.assertEqual(production, development)

        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "added"
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
""")
        self.assertNotEqual(
            production,
            RUNTIME.config_fingerprint(RUNTIME.load_config(self.root, config_path)),
        )

    @needs_store
    def test_doctor_fails_while_the_daemon_runs_a_superseded_configuration(self) -> None:
        self.cli("service", "start")
        self.assertTrue(json.loads(self.cli("doctor").stdout)["ok"])

        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "added-after-start"
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
schedule = "0 3 * * *"
""")

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
    def test_doctor_stays_quiet_about_configuration_while_stopped(self) -> None:
        # A stopped daemon is not running the wrong declaration; it is not
        # running one at all, and saying otherwise would restart nothing.
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "added-while-stopped"
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
""")
        report = json.loads(self.cli("doctor").stdout)
        self.assertTrue(report["ok"])
        self.assertNotIn("config_stale", report)

    def _supervised_daemon(self, environment: str) -> subprocess.Popen:
        state = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])
        daemon = subprocess.Popen(
            [str(CLI), "service", "run"], cwd=self.root,
            env={**self.env, "AUTOMATIONS_ENVIRONMENT": environment},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 15
        while time.time() < deadline and not (state / "daemon.pid").is_file():
            time.sleep(0.1)
        self.assertTrue((state / "daemon.pid").is_file(), "supervised daemon did not start")
        return daemon

    @needs_store
    def test_doctor_reports_the_environment_its_daemon_loaded(self) -> None:
        # A supervisor starts the daemon with its own selector and the shell that
        # asks afterwards carries none, so an answer computed from the asking
        # process describes nobody. Both belong in the payload, told apart.
        daemon = self._supervised_daemon("test")
        try:
            report = json.loads(self.cli("doctor").stdout)
            self.assertTrue(report["ok"])
            self.assertEqual(report["environment"], "test")
            self.assertEqual(report["daemon_environment"], "test")
            self.assertEqual(report["service"]["daemon_environment"], "test")
            self.assertNotIn("environment_idle", report)

            # Asked from an environment of its own, the invocation's answer moves
            # and the daemon's does not, which is what makes them two answers.
            elsewhere = json.loads(subprocess.run(
                [str(CLI), "doctor"], cwd=self.root,
                env={**self.env, "AUTOMATIONS_ENVIRONMENT": "staging"},
                capture_output=True, text=True, timeout=30).stdout)
            self.assertEqual(elsewhere["environment"], "staging")
            self.assertEqual(elsewhere["daemon_environment"], "test")
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)

    @needs_store
    def test_doctor_fails_when_the_daemon_schedules_none_of_the_declarations(self) -> None:
        # The failure this closes: a daemon started under a selector no
        # automation is declared for is alive, answering, and scheduling
        # nothing, and a count of the declarations reports it as healthy. What a
        # supervisor acts on is `ok`, so this is where it has to show.
        daemon = self._supervised_daemon("production")
        try:
            probe = self.cli("service", "doctor", check=False)
            self.assertEqual(probe.returncode, 6)
            report = json.loads(probe.stdout)
            self.assertFalse(report["ok"])
            self.assertEqual(report["daemon_environment"], "production")
            self.assertEqual(report["environment"], "test")
            self.assertTrue(report["service"]["running"])
            self.assertEqual(report["environment_idle"]["daemon_environment"], "production")
            self.assertEqual(report["environment_idle"]["declared"], ["test"])
            self.assertIn("scheduling nothing", report["environment_idle"]["message"])
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)

        # Stopped, the daemon is not scheduling the wrong thing; there is
        # nothing to be wrong about, and the answer goes quiet again.
        stopped = json.loads(self.cli("doctor").stdout)
        self.assertTrue(stopped["ok"])
        self.assertIsNone(stopped["daemon_environment"])
        self.assertNotIn("environment_idle", stopped)

    @needs_store
    def test_an_automation_declared_for_every_environment_keeps_doctor_quiet(self) -> None:
        # An automation naming no environment runs under all of them, so a
        # daemon holding one is scheduling whatever its selector is, and a
        # finding about the selector would be about nothing.
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "everywhere"
script = "capabilities/automations/scripts/job.py"
schedule = "0 3 * * *"
""")
        daemon = self._supervised_daemon("production")
        try:
            report = json.loads(self.cli("doctor").stdout)
            self.assertTrue(report["ok"])
            self.assertEqual(report["daemon_environment"], "production")
            self.assertNotIn("environment_idle", report)
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)

    @needs_store
    def test_a_daemon_that_published_no_environment_is_reported_without_failing(self) -> None:
        # A daemon started by an older payload published the bare fingerprint
        # and keeps running across an upgrade, scheduling correctly all the
        # while. Its declaration still reads, so `config_stale` stays quiet, and
        # an absent environment is the absence of the fact that would decide
        # rather than evidence of a mismatch: reading it as one fails every
        # working deployment on its first health check after the upgrade. So it
        # is said in the payload, and `ok` is left to what can be shown.
        daemon = self._supervised_daemon("test")
        try:
            state = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])
            legacy = state / RUNTIME.DAEMON_FINGERPRINT_FILE
            fingerprint = RUNTIME.read_config_fingerprint(state)
            legacy.write_text(fingerprint + "\n")
            self.assertEqual(RUNTIME.read_config_fingerprint(state), fingerprint)
            self.assertIsNone(RUNTIME.read_daemon_environment(state))

            probe = self.cli("service", "doctor", check=False)
            self.assertEqual(probe.returncode, 0)
            report = json.loads(probe.stdout)
            self.assertTrue(report["ok"])
            self.assertNotIn("config_stale", report)
            self.assertNotIn("environment_idle", report)
            self.assertIsNone(report["daemon_environment"])
            self.assertIsNone(report["service"]["daemon_environment"])
            self.assertIsNone(report["environment_unknown"]["daemon_environment"])
            self.assertEqual(report["environment_unknown"]["declared"], ["test"])
            self.assertIn("did not record which environment",
                          report["environment_unknown"]["message"])
            self.assertIn("cannot say", report["environment_unknown"]["message"])
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)

        # Once that daemon is gone there is nothing left to be unable to speak
        # for, so the notice goes with it rather than lingering.
        stopped = json.loads(self.cli("doctor").stdout)
        self.assertTrue(stopped["ok"])
        self.assertNotIn("environment_unknown", stopped)

    @needs_store
    def test_inventory_judges_automations_by_the_running_daemons_environment(self) -> None:
        # Whether an automation is active is decided by the daemon that runs it,
        # under the environment it was started with, and a terminal asking later
        # carries a different one. Judged from the asker, every automation a
        # supervised daemon is running reads as inactive.
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "nightly"
environments = ["production"]
script = "capabilities/automations/scripts/job.py"
schedule = "0 3 * * *"
""")

        def states(report: dict) -> dict:
            return {item["name"]: item["state"] for item in report["items"]}

        def active(report: dict) -> dict:
            return next(m for m in report["metrics"] if m["label"] == "active here")

        # No daemon: this invocation's environment is the only one there is.
        idle = json.loads(self.cli("inventory").stdout)
        self.assertEqual(idle["service"]["state"], "stopped")
        self.assertIs(idle["service"]["ok"], True)
        self.assertEqual(states(idle)["job"], "active")
        self.assertEqual(states(idle)["nightly"], "other environment")
        self.assertIn("test", active(idle)["note"])

        daemon = self._supervised_daemon("production")
        try:
            report = json.loads(self.cli("inventory").stdout)
            self.assertEqual(report["service"]["state"], "running")
            self.assertIn("production", report["service"]["detail"])
            self.assertIs(report["service"]["ok"], True)
            self.assertEqual(states(report)["nightly"], "active")
            self.assertEqual(states(report)["job"], "other environment")
            self.assertEqual(active(report)["value"], 1)
            self.assertIn("production", active(report)["note"])

            # A daemon that published no environment leaves the question open,
            # and the report says enabled rather than guessing either way.
            state = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])
            legacy = state / RUNTIME.DAEMON_FINGERPRINT_FILE
            legacy.write_text(RUNTIME.read_config_fingerprint(state) + "\n")
            unknown = json.loads(self.cli("inventory").stdout)
            self.assertEqual(states(unknown)["nightly"], "enabled")
            self.assertEqual(states(unknown)["job"], "enabled")
            self.assertNotIn("active here", [m["label"] for m in unknown["metrics"]])

            # The verdict is doctor's own local finding: a declaration edited
            # under a running daemon reads as a problem, with no store asked.
            config_path.write_text(config_path.read_text() + """
[[automations]]
id = "later"
script = "capabilities/automations/scripts/job.py"
schedule = "0 4 * * *"
""")
            stale = json.loads(self.cli("inventory").stdout)["service"]
            self.assertIs(stale["ok"], False)
            self.assertTrue(stale["problem"].startswith("config_stale: "))
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)

    def test_inventory_item_carries_id_description_and_schedule_as_data(self) -> None:
        # A reader addresses an automation by its id and reads its schedule
        # without parsing display text; the contract's item has no such fields,
        # so they ride in `attributes` under fixed labels.
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "nightly"
name = "Nightly digest"
description = "Summarises yesterday's runs."
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
schedule = "0 3 * * *"

[[automations]]
id = "poller"
script = "capabilities/automations/scripts/job.py"
every_seconds = 30
""")
        items = json.loads(self.cli("inventory").stdout)["items"]
        by_id = {}
        for item in items:
            self.assertEqual(set(item), {"group", "name", "state", "detail", "attributes"})
            attributes = [(row["label"], row["value"]) for row in item["attributes"]]
            by_id[dict(attributes)["id"]] = (item, attributes)

        nightly, attrs = by_id["nightly"]
        self.assertEqual(nightly["name"], "Nightly digest")
        self.assertEqual(nightly["detail"], "0 3 * * *")
        self.assertEqual(attrs, [
            ("id", "nightly"), ("description", "Summarises yesterday's runs."),
            ("schedule_kind", "cron"), ("schedule_value", "0 3 * * *"),
            ("script", "capabilities/automations/scripts/job.py"),
            ("environments", "test")])

        poller, attrs = by_id["poller"]
        self.assertEqual(poller["name"], "poller")
        self.assertEqual(poller["detail"], "every 30s")
        self.assertEqual(attrs, [
            ("id", "poller"), ("schedule_kind", "interval"), ("schedule_value", 30),
            ("script", "capabilities/automations/scripts/job.py")])

        job, attrs = by_id["job"]
        self.assertEqual(job["detail"], "manual")
        self.assertEqual(attrs[:2], [("id", "job"), ("schedule_kind", "manual")])
        labels = [label for label, _ in attrs]
        self.assertNotIn("description", labels)
        self.assertNotIn("schedule_value", labels)

    SET_CONFIG = (
        "# Header comment - must survive.\n"
        "version = 1\n"
        "\n"
        "[engine]\n"
        "tick_seconds = 0.1   # fast\n"
        "environment = \"test\"\n"
        "\n"
        "# Why nightly exists.\n"
        "[[automations]]\n"
        "id = \"nightly\"\n"
        "  name = 'Nightly'   # tile label\n"
        "description = \"\"\"old \\\n"
        "text\"\"\"\n"
        "script = \"capabilities/automations/scripts/job.py\"\n"
        "schedule = \"0 3 * * *\"\n"
        "environments = [\n"
        "  \"test\",  # here\n"
        "]\n"
        "\n"
        "[[ automations ]]   # manual\n"
        "\"id\" = \"job\"\n"
        "script = \"capabilities/automations/scripts/job.py\"\n"
        "enabled = true\n"
    )

    def _set_config(self, text: str | None = None, newline: str = "\n") -> Path:
        path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        path.write_bytes((text or self.SET_CONFIG).replace("\n", newline).encode())
        return path

    def test_set_inserts_each_flag_as_one_line_and_touches_nothing_else(self) -> None:
        path = self._set_config()
        answer = json.loads(self.cli(
            "set", "job", "--name", 'Job "quoted" \\ back', "--description",
            "Runs on demand", "--enabled", "false").stdout)
        self.assertEqual(answer["changed"], ["name", "description", "enabled"])
        self.assertEqual(path.read_text(), self.SET_CONFIG.replace(
            '"id" = "job"\n',
            '"id" = "job"\nname = "Job \\"quoted\\" \\\\ back"\n'
            'description = "Runs on demand"\n').replace(
            "enabled = true\n", "enabled = false\n"))
        item = answer["item"]
        self.assertEqual(item["name"], 'Job "quoted" \\ back')
        self.assertEqual(item["state"], "disabled")
        self.assertEqual(item["attributes"][:3], [
            {"label": "id", "value": "job"},
            {"label": "description", "value": "Runs on demand"},
            {"label": "schedule_kind", "value": "manual"}])
        # The answer is the item `inventory` reports for the same automation.
        inventory = json.loads(self.cli("inventory").stdout)["items"]
        self.assertIn(item, inventory)
        self.assertFalse(answer["daemon"]["running"])
        self.assertFalse(answer["daemon"]["reloaded"])

    def test_set_replaces_values_in_place_and_clears_them(self) -> None:
        path = self._set_config()
        self.cli("set", "nightly", "--name", "Renamed", "--description", "One line")
        self.assertEqual(path.read_text(), self.SET_CONFIG.replace(
            "  name = 'Nightly'   # tile label\n", '  name = "Renamed"   # tile label\n').replace(
            'description = """old \\\ntext"""\n', 'description = "One line"\n'))

        answer = json.loads(self.cli("set", "nightly", "--name", "", "--description=").stdout)
        self.assertEqual(answer["changed"], ["name", "description"])
        self.assertEqual(path.read_text(), self.SET_CONFIG.replace(
            "  name = 'Nightly'   # tile label\n", "").replace(
            'description = """old \\\ntext"""\n', ""))
        # Cleared, the name falls back to the id and no description is carried.
        self.assertEqual(answer["item"]["name"], "nightly")
        self.assertNotIn("description", [a["label"] for a in answer["item"]["attributes"]])

        # Clearing what is not declared, and enabling what omits `enabled`,
        # change nothing and write nothing.
        before = path.read_bytes()
        again = json.loads(self.cli("set", "nightly", "--name", "", "--enabled", "true").stdout)
        self.assertEqual(again["changed"], [])
        self.assertEqual(path.read_bytes(), before)

        # A key added later lands after the nearest earlier key the entry has.
        self.cli("set", "nightly", "--enabled", "false", "--name", "-dash-first")
        self.assertIn('id = "nightly"\nname = "-dash-first"\nenabled = false\nscript',
                      path.read_text())

    def test_set_keeps_crlf_line_endings(self) -> None:
        path = self._set_config(newline="\r\n")
        self.cli("set", "job", "--name", "Job")
        self.assertEqual(path.read_bytes(), self.SET_CONFIG.replace(
            '"id" = "job"\n', '"id" = "job"\nname = "Job"\n').replace(
            "\n", "\r\n").encode())

    def test_set_refuses_invalid_requests_and_writes_nothing(self) -> None:
        path = self._set_config()
        before = path.read_bytes()
        refused = [
            (3, "not_found", ("set", "missing", "--name", "x")),
            (6, "input", ("set", "job")),
            (6, "input", ("set",)),
            (6, "input", ("set", "job", "--title", "x")),
            (6, "input", ("set", "job", "--name", "a", "--name", "b")),
            (6, "input", ("set", "job", "--name")),
            (6, "invalid_enabled", ("set", "job", "--enabled", "yes")),
            (6, "invalid_enabled", ("set", "job", "--enabled", "True")),
            (6, "invalid_enabled", ("set", "job", "--enabled", "1")),
            (6, "invalid_text", ("set", "job", "--name", "a\tb")),
            (6, "invalid_text", ("set", "job", "--name", "a\x7fb")),
            (6, "invalid_text", ("set", "job", "--description", "a\nb")),
            (6, "invalid_text", ("set", "job", "--description", "a b")),
            (6, "invalid_text", ("set", "job", "--description", "a b")),
            (6, "invalid_text", ("set", "job", "--name", "   ")),
            (6, "too_long", ("set", "job", "--name", "n" * 81)),
            (6, "too_long", ("set", "job", "--description", "d" * 501)),
            # Valid text with an invalid flag beside it writes nothing either.
            (6, "invalid_enabled", ("set", "job", "--name", "ok", "--enabled", "no")),
        ]
        for code, slug, args in refused:
            with self.subTest(args=args):
                proc = self.cli(*args, check=False)
                self.assertEqual(proc.returncode, code, proc.stderr)
                self.assertEqual(json.loads(proc.stderr)["error"]["code"], slug)
                self.assertEqual(path.read_bytes(), before)
        # The limits themselves are allowed, counted in characters.
        self.cli("set", "job", "--name", "n" * 80, "--description", "é" * 500)

    def test_set_refuses_an_edit_that_leaves_an_invalid_configuration(self) -> None:
        # The edited file is loaded exactly as the daemon loads it before it
        # replaces anything, so a file that would not load stays as it was.
        for broken in (self.SET_CONFIG + "\n[[automations]]\nid = \"x\"\n"
                       "script = \"capabilities/automations/scripts/job.py\"\nbogus = 1\n",
                       self.SET_CONFIG + "\nthis is not toml [[[\n"):
            with self.subTest(broken=broken[-20:]):
                path = self._set_config(broken)
                proc = self.cli("set", "job", "--name", "x", check=False)
                self.assertEqual(proc.returncode, 6, proc.stderr)
                self.assertIn("invalid_config", proc.stderr)
                self.assertEqual(path.read_text(), broken)
                self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                                 ["config.toml"])

    def test_set_requires_an_explicit_project_enable(self) -> None:
        # Global inheritance runs the other verbs, but writing the project's
        # config.toml is gated as `service init` is.
        config_home = Path(self.tmp.name) / "xdg-config"
        (config_home / "capabilities").mkdir(parents=True)
        (config_home / "capabilities" / "settings.json").write_text(
            json.dumps({"capabilities": {"automations": {"enabled": True}}}) + "\n")
        (self.root / "capabilities" / "settings.json").write_text(
            json.dumps({"capabilities": {}}) + "\n")
        self.env["XDG_CONFIG_HOME"] = str(config_home)
        path = self._set_config()
        self.cli("list")
        proc = self.cli("set", "job", "--name", "x", check=False)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("project_enable_required", proc.stderr)
        self.assertEqual(path.read_text(), self.SET_CONFIG)

    @needs_store
    def test_set_publishes_the_change_to_a_running_daemon(self) -> None:
        self.cli("service", "start")
        pid = json.loads(self.cli("service", "status").stdout)["pid"]
        answer = json.loads(self.cli("set", "job", "--enabled", "false").stdout)
        self.assertEqual(answer["daemon"], {"running": True, "reloaded": True, "pid": pid})
        self.assertEqual(answer["item"]["state"], "disabled")
        # The same process took it up and is current: no restart, no stale config.
        self.assertTrue(json.loads(self.cli("service", "doctor").stdout)["ok"])
        self.assertEqual(json.loads(self.cli("service", "status").stdout)["pid"], pid)
        state = Path(json.loads(self.cli("service", "status").stdout)["state_dir"])
        loaded = RUNTIME.read_config_fingerprint(state)

        # Nothing changed: the daemon is not signalled.
        same = json.loads(self.cli("set", "job", "--enabled", "false").stdout)
        self.assertFalse(same["daemon"]["reloaded"])
        self.assertEqual(RUNTIME.read_config_fingerprint(state), loaded)

        on = json.loads(self.cli("set", "job", "--enabled", "true").stdout)
        self.assertTrue(on["daemon"]["reloaded"])
        self.assertEqual(on["item"]["state"], "active")

        self.cli("service", "stop")
        stopped = json.loads(self.cli("set", "job", "--name", "Job").stdout)
        self.assertEqual(stopped["daemon"]["running"], False)
        self.assertEqual(stopped["daemon"]["reloaded"], False)
        self.assertEqual(stopped["changed"], ["name"])

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
        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "added-after-start"
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
schedule = "0 3 * * *"
""")
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
        config_path.write_text(config_path.read_text() + """
[[automations]]
id = "added-mid-flight"
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
schedule = "0 4 * * *"
""")
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

    def test_environment_gate(self) -> None:
        self.env["AUTOMATIONS_ENVIRONMENT"] = "production"
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
    def test_ticker_deduplicates_one_interval_bucket(self) -> None:
        config_path = self.root / "schedule.toml"
        config_path.write_text(
            """version = 1
[engine]
tick_seconds = 1
max_parallel = 1
timezone = "UTC"
environment = "test"

[[automations]]
id = "scheduled"
environments = ["test"]
script = "capabilities/automations/scripts/job.py"
every_seconds = 60
timeout_seconds = 5
max_parallel = 1
max_pending = 1
overlap = "skip"
retries = 0
"""
        )
        state_dir = self.root / "schedule-state"
        daemon = RUNTIME.Daemon(self.root, config_path, state_dir)
        try:
            when = datetime(2026, 7, 20, 8, 10, 15, tzinfo=timezone.utc)
            daemon.schedule_due(when)
            daemon.schedule_due(when)
        finally:
            daemon.runs.close()
        rows = RUNTIME.list_runs(self.root, RUNTIME.load_config(self.root, config_path),
                                 limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["trigger"], "schedule")

    @needs_store
    def test_startup_recovery_requeues_by_policy(self) -> None:
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        state_dir = self.root / "capabilities" / "automations" / "state"
        config = RUNTIME.load_config(self.root, config_path)
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

    def ledger(project_id=None, environment="production"):
        led = RUNTIME.RunLedger(project_id or f"prj_{uuid.uuid4().hex[:12]}",
                                environment).open()
        opened.append(led)
        return led

    yield ledger
    for led in opened:
        led.close()


DEDUPE = "fixture:production:nightly:2026-08-23T03:00:00+00:00"


def test_two_machines_cannot_both_claim_one_scheduled_firing(tmp_path, ledger_env):
    """The whole reason the ledger lives in a shared store: two daemons of one
    project, on two machines, race one firing and exactly one owns it."""
    a = ledger_env()
    b = ledger_env(a.project_id)
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
    led = ledger_env(environment=None)
    with pytest.raises(Exception) as caught:
        led.claim("nightly", tmp_path, trigger="manual")
    assert "null value" in str(caught.value)


def test_a_dedupe_collision_still_yields_rather_than_raising(tmp_path, ledger_env):
    led = ledger_env()
    assert led.claim("nightly", tmp_path, trigger="schedule", dedupe_key=DEDUPE) is not None
    assert led.claim("nightly", tmp_path, trigger="schedule", dedupe_key=DEDUPE) is None


def test_a_dropped_connection_is_noticed_and_reopened(tmp_path, ledger_env):
    """The store is across a network; a connection the server ends is noticed as
    lost, not taken for a fault in the work, and a new one carries on."""
    led = ledger_env()
    led.claim("nightly", tmp_path, trigger="manual")
    ledger_env().conn.execute("SELECT pg_terminate_backend(%s)",
                              [led.conn.info.backend_pid])
    with pytest.raises(Exception):
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
        {"schema": "capabilities.project.v1", "id": "prj_nostore00000", "slug": "nostore"}))
    with pytest.raises(RUNTIME.StoreUnavailable) as caught:
        RUNTIME.open_ledger(root, {"engine": {"environment": "development"}})
    assert caught.value.slug == "store_not_configured"
    assert "capabilities store set" in caught.value.hint
    assert not (tmp_path / "store.db").exists()


def test_registration_stamps_the_id_its_launcher_resolved(tmp_path, monkeypatch):
    """The id a run is registered under is the one the automations CLI resolved
    for a write: installed in process, handed to the daemon it launches, and
    project.json's own only where no launcher resolved one."""
    identity = {"id": "prj_copy00000000", "slug": "labelled"}
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
    for slug, body in (("job", "exec sleep 60"), ("stubborn", STUBBORN)):
        job = root / "jobs" / f"{slug}.sh"
        job.write_text(f"#!/bin/sh\n{body}\n")
        job.chmod(0o755)
        declared += f"\n[[automations]]\nid = \"{slug}\"\nscript = \"jobs/{slug}.sh\"\n" \
                    f"timeout_seconds = {timeout}\nmax_parallel = 8\n"
    config_path = root / "config.toml"
    config_path.write_text(declared)
    said: list[str] = []
    daemon = RUNTIME.Daemon(root, config_path, tmp_path / "state", slug="scratch",
                            loader=lambda: RUNTIME.load_config(root, config_path),
                            runs=DownLedger(), log=said.append)
    return daemon, said


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
