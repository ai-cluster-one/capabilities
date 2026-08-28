#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import pytest
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from datetime import datetime, timezone


CAPABILITY = Path(__file__).resolve().parents[1]
CLI = CAPABILITY / "bin" / "automations"
RUNTIME_PATH = CAPABILITY / "service" / "runtime.py"
MANAGER = CAPABILITY.parents[1] / "bin" / "capabilities"

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
        # writing its projects into the developer's real one.
        self.store_path = Path(self.tmp.name) / "store.db"
        self._store_url_before = os.environ.get("CAPABILITIES_STORE_URL")
        os.environ["CAPABILITIES_STORE_URL"] = str(self.store_path)
        self.env = dict(os.environ)
        self.env.update(
            {
                "CLAUDE_PROJECT_DIR": str(self.root),
                "CAPABILITIES_STORE_URL": str(Path(self.tmp.name) / "store.db"),
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
        if self._store_url_before is None:
            os.environ.pop("CAPABILITIES_STORE_URL", None)
        else:
            os.environ["CAPABILITIES_STORE_URL"] = self._store_url_before
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
        store, ledger = RUNTIME.open_ledger(self.root, config)
        ledger.update(row["id"], status="succeeded",
                      finished_at=datetime.now(timezone.utc).isoformat())
        store.close()

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
            {"workers": {"x": {"engine": "claude", "model": "m", "mode": "act"}}},
            {"workers": {"x": {"engine": "claude"}}},
            {"default": "absent"},
            {"unknown": True},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(RUNTIME.ConfigError):
                    RUNTIME.load_agents({"agents": case})

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

    def test_agents_verb_lists_profiles(self) -> None:
        listed = json.loads(self.cli("agents").stdout)
        self.assertEqual(listed["default"], "sonnet")
        self.assertIn("haiku", listed["workers"])

    def test_agent_rejects_unknown_profile(self) -> None:
        proc = self.cli("agent", "--profile", "absent", "hello", check=False)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(json.loads(proc.stderr)["error"]["code"], "unknown_agent")

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
            daemon.store.close()
        rows = RUNTIME.list_runs(self.root, RUNTIME.load_config(self.root, config_path),
                                 limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["trigger"], "schedule")

    def test_startup_recovery_requeues_by_policy(self) -> None:
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        state_dir = self.root / "capabilities" / "automations" / "state"
        config = RUNTIME.load_config(self.root, config_path)
        row = RUNTIME.enqueue_manual(self.root, config, state_dir, "job")
        self.assertIsNotNone(row)
        store, ledger = RUNTIME.open_ledger(self.root, config)
        ledger.update(row["id"], status="running")
        store.close()
        daemon = RUNTIME.Daemon(self.root, config_path, state_dir)
        try:
            daemon.recover()
        finally:
            daemon.store.close()
        rows = RUNTIME.list_runs(self.root, config, limit=10)
        original = next(item for item in rows if item["id"] == row["id"])
        recovered = next(item for item in rows if item["parent_run_id"] == row["id"])
        self.assertEqual(original["status"], "interrupted")
        self.assertEqual(recovered["status"], "pending")
        self.assertEqual(recovered["trigger"], "recovery")

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

def _store_with_automation(tmp_path):
    """A store holding one project and one automation, as two machines sharing
    a database would see it."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    import store as store_mod
    import runtime as rt

    st = store_mod.SQLiteStore.open(str(tmp_path / "shared.db"))
    st.migrate()
    st.project_register("11111111-2222-3333-4444-555555555555", "marvin")
    st.migrate(rt.STORE_NAMESPACE, rt.STORE_VERSION, rt.STORE_MIGRATIONS)
    project_id = st._project_id("marvin")
    automation_id = rt.store_upsert(st, "project", project_id, {
        "slug": "nightly", "name": None, "description": None, "enabled": 1,
        "script_key": "script.nightly", "schedule": "0 3 * * *", "every_seconds": None,
        "timeout_seconds": 300.0, "max_parallel": 1, "max_pending": 1,
        "overlap": "skip", "retries": 0, "arguments": [], "environments": [],
    })
    st._conn.commit()
    return st, project_id, automation_id


def _claim(st, project_id, automation_id, dedupe):
    """One machine trying to claim one scheduled firing."""
    import uuid as _uuid
    st._execute(
        "INSERT INTO runs (id, project_id, automation_id, automation_slug, environment, "
        "trigger, scheduled_for, dedupe_key, status, queued_at, log_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(_uuid.uuid4()), project_id, automation_id, "nightly", "production",
         "schedule", "2026-08-23T03:00:00+00:00", dedupe, "pending",
         "2026-08-23T03:00:00+00:00", "/dev/null"))
    st._conn.commit()


def test_two_machines_cannot_both_claim_one_scheduled_firing(tmp_path):
    """The whole reason the ledger moves to a shared store."""
    import sqlite3 as _sqlite3
    st, project_id, automation_id = _store_with_automation(tmp_path)
    dedupe = "marvin:production:nightly:2026-08-23T03:00:00+00:00"

    _claim(st, project_id, automation_id, dedupe)
    with pytest.raises(_sqlite3.IntegrityError):
        _claim(st, project_id, automation_id, dedupe)

    assert st._execute("SELECT COUNT(*) FROM runs WHERE dedupe_key = ?",
                       (dedupe,)).fetchone()[0] == 1
    st.close()


def test_a_different_firing_of_the_same_automation_still_claims(tmp_path):
    st, project_id, automation_id = _store_with_automation(tmp_path)
    _claim(st, project_id, automation_id, "marvin:production:nightly:2026-08-23T03:00:00+00:00")
    _claim(st, project_id, automation_id, "marvin:production:nightly:2026-08-24T03:00:00+00:00")
    assert st._execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    st.close()


def test_a_run_cannot_name_an_automation_that_is_not_there(tmp_path):
    """The uuid reference is a real constraint, not a naming convention."""
    import sqlite3 as _sqlite3
    st, project_id, _automation_id = _store_with_automation(tmp_path)
    with pytest.raises(_sqlite3.IntegrityError):
        _claim(st, project_id, "99999999-9999-9999-9999-999999999999", "x")
    st.close()


def _ledger(st, project_id, environment="production"):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    import runtime as rt
    return rt.RunLedger(st._conn, project_id, environment)


def test_the_ledger_answers_only_about_its_own_project(tmp_path):
    """The reason scoping is a boundary and not a WHERE clause fifteen callers
    are trusted to remember."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    import runtime as rt

    st, mine, automation_id = _store_with_automation(tmp_path)
    st.project_register("99999999-8888-7777-6666-555555555555", "other")
    theirs = st._project_id("other")
    other_automation = rt.store_upsert(st, "project", theirs, {
        "slug": "nightly", "name": None, "description": None, "enabled": 1,
        "script_key": "script.nightly", "schedule": "0 3 * * *", "every_seconds": None,
        "timeout_seconds": 300.0, "max_parallel": 1, "max_pending": 1,
        "overlap": "skip", "retries": 0, "arguments": [], "environments": [],
    })
    st._conn.commit()

    ours = _ledger(st, mine)
    others = _ledger(st, theirs)
    (tmp_path / "runs").mkdir(exist_ok=True)

    assert ours.claim(automation_id, "nightly", tmp_path, trigger="manual") is not None
    assert others.claim(other_automation, "nightly", tmp_path, trigger="manual") is not None

    assert len(ours.list()) == 1
    assert len(others.list()) == 1
    assert ours.counts() == {"pending": 1}
    assert st._execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2

    # and a run belonging to the other project is invisible, not merely filtered
    theirs_run = others.list()[0]
    assert ours.get(theirs_run["id"]) is None
    st.close()


def test_two_ledgers_racing_one_firing_leave_one_run(tmp_path):
    st, project_id, automation_id = _store_with_automation(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    a, b = _ledger(st, project_id), _ledger(st, project_id)
    dedupe = "marvin:production:nightly:2026-08-23T03:00:00+00:00"

    first = a.claim(automation_id, "nightly", tmp_path, trigger="schedule", dedupe_key=dedupe)
    second = b.claim(automation_id, "nightly", tmp_path, trigger="schedule", dedupe_key=dedupe)

    assert first is not None and second is None
    assert len(a.list()) == 1
    st.close()


def test_the_ledger_counts_per_automation_within_the_project(tmp_path):
    st, project_id, automation_id = _store_with_automation(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    led = _ledger(st, project_id)
    led.claim(automation_id, "nightly", tmp_path, trigger="manual")

    assert led.count_for("nightly", "pending") == 1
    assert led.count_for("other-thing", "pending") == 0
    assert led.has_active("nightly", ["pending"]) is True
    assert led.running() == 0
    st.close()


# --- a store whose runs table predates the nullable automation reference ------

def _stale_store(tmp_path):
    """A store as it stands on a machine whose `runs` table was created before
    the automation reference was allowed to be empty."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    import store as store_mod
    import runtime as rt

    st = store_mod.SQLiteStore.open(str(tmp_path / "stale.db"))
    st.migrate()
    st.project_register("11111111-2222-3333-4444-555555555555", "marvin")
    stale = [step.replace("automation_id     TEXT REFERENCES automations(id),",
                          "automation_id     TEXT NOT NULL REFERENCES automations(id),")
             for step in rt.STORE_MIGRATIONS]
    st.migrate(rt.STORE_NAMESPACE, rt.STORE_VERSION, stale)
    st._conn.commit()
    return st, rt


def test_a_stale_runs_table_is_named_by_the_defect_check(tmp_path):
    st, rt = _stale_store(tmp_path)
    assert rt.runs_schema_defect(st) == "automation_id"
    st.close()


def test_repairing_the_runs_table_keeps_its_rows_and_widens_the_column(tmp_path):
    st, rt = _stale_store(tmp_path)
    project_id = st._project_id("marvin")
    automation_id = rt.store_upsert(st, "project", project_id, {
        "slug": "nightly", "name": None, "description": None, "enabled": 1,
        "script_key": "script.nightly", "schedule": "0 3 * * *", "every_seconds": None,
        "timeout_seconds": 300.0, "max_parallel": 1, "max_pending": 1,
        "overlap": "skip", "retries": 0, "arguments": [], "environments": [],
    })
    st._conn.commit()
    _claim(st, project_id, automation_id, "marvin:production:nightly:2026-08-23T03:00:00+00:00")

    outcome = rt.repair_runs_schema(st)
    assert outcome["repaired"] is True
    assert outcome["rows_preserved"] == 1
    assert rt.runs_schema_defect(st) is None
    assert st._execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    indexes = {row[0] for row in st._execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'runs'")}
    assert {"runs_status_idx", "runs_automation_idx"} <= indexes
    st.close()


def test_repairing_a_healthy_runs_table_changes_nothing(tmp_path):
    st, _project_id, _automation_id = _store_with_automation(tmp_path)
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    import runtime as rt
    assert rt.repair_runs_schema(st)["repaired"] is False
    st.close()


def test_a_run_the_schema_cannot_hold_fails_loudly(tmp_path):
    """The failure this replaces was silent: the scheduler read `None` as
    someone else's claim and dropped the firing."""
    import sqlite3 as _sqlite3
    st, rt = _stale_store(tmp_path)
    project_id = st._project_id("marvin")
    ledger = rt.RunLedger(st._conn, project_id, "production")
    with pytest.raises(_sqlite3.IntegrityError):
        ledger.claim(None, "nightly", tmp_path, trigger="manual")
    st.close()


def test_a_dedupe_collision_still_yields_rather_than_raising(tmp_path):
    st, project_id, automation_id = _store_with_automation(tmp_path)
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    import runtime as rt
    ledger = rt.RunLedger(st._conn, project_id, "production")
    dedupe = "marvin:production:nightly:2026-08-23T03:00:00+00:00"
    assert ledger.claim(automation_id, "nightly", tmp_path,
                        trigger="schedule", dedupe_key=dedupe) is not None
    assert ledger.claim(automation_id, "nightly", tmp_path,
                        trigger="schedule", dedupe_key=dedupe) is None
    st.close()
