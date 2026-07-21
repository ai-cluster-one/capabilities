#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
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
            daemon.db.close()
        rows = RUNTIME.list_runs(state_dir / "automations.db", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["trigger"], "schedule")

    def test_startup_recovery_requeues_by_policy(self) -> None:
        config_path = self.root / "capabilities" / "automations" / "service" / "config.toml"
        state_dir = self.root / "capabilities" / "automations" / "state"
        config = RUNTIME.load_config(self.root, config_path)
        db_path = state_dir / "automations.db"
        with contextlib.closing(RUNTIME.connect(db_path)) as db:
            row = RUNTIME.enqueue(db, config, state_dir, "job", trigger="manual")
            self.assertIsNotNone(row)
            db.execute("UPDATE runs SET status = 'running' WHERE id = ?", (row["id"],))
            db.commit()
        daemon = RUNTIME.Daemon(self.root, config_path, state_dir)
        try:
            daemon.recover()
        finally:
            daemon.db.close()
        rows = RUNTIME.list_runs(db_path, limit=10)
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
                if row["automation_id"] == "flaky" and row["parent_run_id"] == first["id"]
            ]
            if retries and retries[0]["status"] == "succeeded":
                self.assertEqual(retries[0]["attempt"], 2)
                return
            time.sleep(0.1)
        self.fail("automatic retry did not succeed")


if __name__ == "__main__":
    unittest.main()
