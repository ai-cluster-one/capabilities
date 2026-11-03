#!/usr/bin/env python3
"""Tests that the real callva script serves a process holding one credential.

Each case runs the script as a subprocess with the environment an embedding server
gives it - a fresh home, a minimal PATH with no manager - against a local HTTP
server that records every request.

Run with: uv run --with httpx --with "capabilities-contract>=0.42,<1" python3 capabilities/callva/tests/test_headless.py
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "callva", CAPABILITY / "callva")
    if path.is_file()), CAPABILITY / "bin" / "callva")
TOKEN = "test-token-not-a-real-one"


class _Recorder(http.server.BaseHTTPRequestHandler):
    def _answer(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization")))
        body = json.dumps({"data": [{"id": "A1", "name": "n"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

    def log_message(self, *_args):
        pass


class HeadlessCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        cls.server.seen = []
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/api/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.seen.clear()
        tmp = tempfile.TemporaryDirectory(prefix="callva-headless-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()

    def env(self, **extra):
        return {
            "CALLVA_HEADLESS": "read-only",
            "CALLVA_API_KEY": TOKEN,
            "CALLVA_API_URL": self.base,
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            **extra,
        }

    def run_cli(self, *argv, env=None, cwd=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *argv],
            cwd=cwd or self.home, env=env if env is not None else self.env(),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def assert_refused(self, proc, code):
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertEqual(json.loads(proc.stderr.strip().splitlines()[-1])["error"]["code"], code)
        self.assertEqual(self.server.seen, [])


class HeadlessTest(HeadlessCase):
    def test_a_read_reaches_http_with_the_token(self):
        proc = self.run_cli("agents", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["data"][0]["id"], "A1")
        self.assertEqual(self.server.seen, [("GET", "/api/v1/external/agents", f"Bearer {TOKEN}")])

    def test_routes_runs_locally(self):
        proc = self.run_cli("routes", env=self.env(CALLVA_API_KEY=""))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["routes"])
        self.assertEqual(self.server.seen, [])

    def test_every_declared_write_and_secret_exits_4_before_http(self):
        routes = json.loads(self.run_cli("routes").stdout)["routes"]
        closed = [r for r in routes if r["writes"] or r["secret"]]
        self.assertTrue(any(r["secret"] for r in closed))
        self.assertTrue(any(r["writes"] for r in closed))
        for route in closed:
            with self.subTest(verb=route["verb"]):
                proc = self.run_cli(*route["verb"].split())
                self.assert_refused(proc, "secret_refused" if route["secret"] else "read_only")

    def test_a_write_with_full_arguments_exits_4(self):
        self.assert_refused(self.run_cli("agents", "delete", "A1"), "read_only")
        self.assert_refused(self.run_cli("settings", "reveal", "D1"), "secret_refused")

    def test_no_env_value_lifts_the_refusal(self):
        env = self.env(CAPABILITIES_READ_ONLY="0", CALLVA_ALLOW_WRITE="1")
        self.assert_refused(self.run_cli("agents", "delete", "A1", env=env), "read_only")

    def test_contract_secret_and_record_verbs_are_refused(self):
        for argv in (("connections", "show", "headless", "--reveal", "secret_env"),
                     ("connection", "add", "x"), ("ids", "list"), ("setup",)):
            with self.subTest(argv=argv):
                self.assert_refused(self.run_cli(*argv), "headless_refused")

    def test_connection_flags_are_refused(self):
        for argv in (("agents", "list", "--api-url", "http://elsewhere.invalid"),
                     ("agents", "list", "--key-env", "OTHER"),
                     ("agents", "list", "--connection", "x")):
            with self.subTest(argv=argv):
                self.assert_refused(self.run_cli(*argv), "headless_refused")

    def test_any_other_value_is_refused(self):
        env = self.env(CALLVA_HEADLESS="admin")
        self.assert_refused(self.run_cli("agents", "list", env=env), "headless_refused")

    def test_a_project_closes_the_mode(self):
        project = self.home / "work" / "project"
        (project / "capabilities").mkdir(parents=True)
        (project / "capabilities" / "settings.json").write_text("{}")
        self.assert_refused(self.run_cli("agents", "list", cwd=project), "headless_refused")

    def test_machine_configuration_closes_the_mode(self):
        for name in ("capabilities", "callva"):
            with self.subTest(name=name):
                path = self.home / ".config" / name
                path.mkdir(parents=True)
                self.assert_refused(self.run_cli("agents", "list"), "headless_refused")
                path.rmdir()
        (self.home / ".capabilities").mkdir()
        self.assert_refused(self.run_cli("agents", "list"), "headless_refused")

    def test_a_store_setting_closes_the_mode(self):
        env = self.env(AGENTKIT_DB_URL="postgresql://nobody@127.0.0.1:1/none")
        self.assert_refused(self.run_cli("agents", "list", env=env), "headless_refused")

    def test_without_the_variable_the_gate_still_denies(self):
        env = self.env()
        del env["CALLVA_HEADLESS"]
        self.assert_refused(self.run_cli("agents", "list", env=env), "not_enabled")

    def test_a_missing_credential_exits_2(self):
        proc = self.run_cli("agents", "list", env=self.env(CALLVA_API_KEY=""))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.server.seen, [])

    def test_schedules_preview_stays_a_read(self):
        proc = self.run_cli("schedules", "preview", "S1", "2026-01-01T00:00:00Z")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.seen[0][:2], ("POST", "/api/v1/external/webhook-schedule/S1/preview"))

    # Each spelling argparse accepts for a flag naming a local path, on a verb the mode
    # would otherwise reach; `-` is stdin, and the file named here exists to be read.
    def local_path_argvs(self):
        source = self.home / "payload.json"
        source.write_text('{"a": 1}')
        target = self.home / "out.bin"
        recording = ("recordings", "stream", "C1", "--url", "u")
        yield "read-only", (*recording, "--output", str(target))
        yield "read-only", (*recording, f"--output={target}")
        yield "read-only", (*recording, "--out", str(target))
        for argv in (("agents", "update", "A1", "--prompt-file", str(source)),
                     ("presets", "set", "A1", "P", "--config-file", str(source)),
                     ("tools", "set", "A1", "K", "--config-file", str(source)),
                     ("assets", "create", "--name", "N", "--type", "prompt", "--content-file", str(source)),
                     ("assets", "update", "A1", "--content-f", str(source)),
                     ("calls", "run-action", "C1", "A1", "--inputs-file", str(source)),
                     ("actions", "create", f"--data-file={source}"),
                     ("ui-views", "put", "V1", "--data-file", "-"),
                     ("transcripts", "store", "C1", "--data-file", str(source)),
                     ("automations", "deploy", "A1", "--file", str(source)),
                     ("automations", "run", "A1", "--args-file", str(source)),
                     (*recording, "--output", str(target))):
            yield "read-write", argv

    def test_a_flag_naming_a_local_path_exits_4(self):
        for mode, argv in self.local_path_argvs():
            with self.subTest(mode=mode, argv=argv):
                self.assert_refused(self.run_cli(*argv, env=self.env(CALLVA_HEADLESS=mode)), "headless_refused")
                self.assertFalse((self.home / "out.bin").exists())

    def test_every_flag_naming_a_local_path_is_covered(self):
        code = SCRIPT.read_text()
        declared = set(re.findall(r'add_argument\("(--[a-z-]*file|--output)"', code))
        spelled = {token.split("=")[0] for _mode, argv in self.local_path_argvs() for token in argv
                   if token.startswith("--")}
        self.assertEqual(declared - spelled, set())

    def test_inline_content_still_works_in_a_write(self):
        env = self.env(CALLVA_HEADLESS="read-write")
        proc = self.run_cli("assets", "create", "--name", "N", "--type", "prompt", "--content", "text", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.seen, [("POST", "/api/v1/external/assets", f"Bearer {TOKEN}")])

    def test_a_recording_still_reaches_stdout(self):
        proc = self.run_cli("recordings", "stream", "C1", "--url", "u")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.seen[0][0], "GET")


class HeadlessReadWriteTest(HeadlessCase):
    def env(self, **extra):
        return super().env(**{"CALLVA_HEADLESS": "read-write", **extra})

    def test_a_write_reaches_http_with_the_token(self):
        proc = self.run_cli("agents", "delete", "A1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.seen, [("DELETE", "/api/v1/external/agents/A1", f"Bearer {TOKEN}")])

    def test_a_read_still_reaches_http(self):
        proc = self.run_cli("agents", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.seen, [("GET", "/api/v1/external/agents", f"Bearer {TOKEN}")])

    def test_a_secret_verb_exits_4(self):
        self.assert_refused(self.run_cli("settings", "reveal", "D1"), "secret_refused")

    def test_the_read_only_switch_closes_writes(self):
        proc = self.run_cli("agents", "delete", "A1", env=self.env(CAPABILITIES_READ_ONLY="1"))
        self.assert_refused(proc, "read_only_switch")

    def test_contract_secret_and_record_verbs_are_refused(self):
        for argv in (("connections", "show", "headless", "--reveal", "secret_env"),
                     ("connection", "add", "x"), ("ids", "list"), ("setup",)):
            with self.subTest(argv=argv):
                self.assert_refused(self.run_cli(*argv), "headless_refused")

    def test_connection_flags_are_refused(self):
        for argv in (("agents", "delete", "A1", "--api-url", "http://elsewhere.invalid"),
                     ("agents", "delete", "A1", "--key-env", "OTHER"),
                     ("agents", "delete", "A1", "--connection", "x")):
            with self.subTest(argv=argv):
                self.assert_refused(self.run_cli(*argv), "headless_refused")

    def test_a_project_closes_the_mode(self):
        project = self.home / "work" / "project"
        (project / "capabilities").mkdir(parents=True)
        (project / "capabilities" / "settings.json").write_text("{}")
        self.assert_refused(self.run_cli("agents", "delete", "A1", cwd=project), "headless_refused")

    def test_machine_configuration_closes_the_mode(self):
        path = self.home / ".config" / "capabilities"
        path.mkdir(parents=True)
        self.assert_refused(self.run_cli("agents", "delete", "A1"), "headless_refused")
        path.rmdir()
        (self.home / ".capabilities").mkdir()
        self.assert_refused(self.run_cli("agents", "delete", "A1"), "headless_refused")

    def test_a_store_setting_closes_the_mode(self):
        env = self.env(AGENTKIT_DB_URL="postgresql://nobody@127.0.0.1:1/none")
        self.assert_refused(self.run_cli("agents", "delete", "A1", env=env), "headless_refused")


if __name__ == "__main__":
    unittest.main()
