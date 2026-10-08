#!/usr/bin/env python3
"""Speed and liveness of the assistant service: a queued message is picked up
at once, a listener that stops making progress leaves to be restarted, a stop
request is honoured even when the loop is blocked, a stale holder is taken
over while a healthy one is respected, and every report treats stale health
as a failure.

The listener runs against sessions that stand in for the engine. The stall,
stop and takeover cases run real processes, so their exit codes and signals
are the operating system's own. The store-backed cases need a throwaway
Postgres named by WHATSAPP_TEST_DSN and skip without one.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _cli  # noqa: E402

wa = _cli.load()
ENGINE = _cli.engine_available(wa)
needs_engine = unittest.skipUnless(ENGINE, "the engine does not load on this host")
CHAT = "15550001111@s.whatsapp.net"


class Client:
    """The engine's client as the sender sees it, noting when each send left."""

    def __init__(self):
        self.sent_at: list[float] = []
        self.is_connected = True

    def send_chat_presence(self, to, state, media):
        pass

    def send_message(self, to, message):
        self.sent_at.append(time.time())
        return types.SimpleNamespace(ID=f"WAL{time.monotonic_ns()}",
                                     Timestamp=int(time.time() * 1000))

    def disconnect(self):
        pass


class Held:
    """A session that connects at once and holds."""

    def __init__(self, db=None):
        self.db = db
        self._client = Client()
        self._logged_out = threading.Event()
        self._disconnected = threading.Event()
        self.disconnect_reason = None
        self.errors: list = []
        self.live_messages = 0
        self.offline_count = 0
        self.last_event_at = self.last_message_at = None
        self.stalled = False
        self.inflight: dict = {}

    def open(self):
        return self

    def wait_connected(self, timeout=60):
        return True

    def require_account(self):
        return {"id": "15550000000"}

    def account(self):
        return {"jid": "15550000000@s.whatsapp.net"}

    def refresh_identities(self):
        return 0

    def close(self):
        pass


def _request(text="hello") -> dict:
    return {"chat_id": CHAT, "text": text, "reply_to": None, "mentions": []}


# ── A queued message wakes the sender ───────────────────────────────────────


@_cli.needs_store
@needs_engine
class Pickup(unittest.TestCase):
    """The sender wakes the moment a row is queued, in this process through
    its event and from any other through the store's notification. The poll
    is set far out of reach here, so only the wake can explain a pickup."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = _cli.store_cfg(engine=wa.ENGINE_INHOUSE)
        self.db = wa._open_store(self.cfg)
        self.addCleanup(self.db.close)
        self.other = wa._open_store(self.cfg)
        self.addCleanup(self.other.close)

    def listener(self):
        held = Held(self.db)
        daemon = wa._ServiceDaemon(self.cfg, Path(tempfile.mkdtemp()),
                                   {"environment": "test"}, launch_nonce="n",
                                   session_factory=lambda: held)
        daemon.log = lambda message: None
        patcher = mock.patch.object(wa, "SERVICE_TICK", 30.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = threading.Thread(target=daemon.loop, daemon=True)
        thread.start()

        def stop():
            daemon.request_stop()
            thread.join(10)
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (
                daemon.health.get("state") == "healthy" and daemon.recovered):
            time.sleep(0.01)
        self.assertTrue(daemon.recovered)
        return daemon, held

    def sent(self, held, count, limit=5.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline and len(held._client.sent_at) < count:
            time.sleep(0.002)
        self.assertGreaterEqual(len(held._client.sent_at), count)
        return held._client.sent_at[count - 1]

    def test_a_row_queued_in_this_process_is_picked_up_at_once(self):
        daemon, held = self.listener()
        with mock.patch.object(wa, "_OUTBOX_WAKE", daemon.wake):
            times = []
            for n in range(3):
                time.sleep(0.3)
                queued = time.time()
                wa._queue_outgoing(self.other, _request(f"m{n}"))
                times.append(self.sent(held, n + 1) - queued)
        sys.stderr.write("\n  measured: in-process queue -> send "
                         + ", ".join(f"{1000 * t:.1f} ms" for t in times) + "\n")
        self.assertLess(max(times), 0.2)

    def test_a_row_queued_by_another_process_is_picked_up_through_the_store(self):
        daemon, held = self.listener()
        outbox = threading.Thread(target=daemon.listen_outbox, daemon=True)
        outbox.start()
        self.addCleanup(daemon.leaving.set)
        time.sleep(0.5)       # the listening connection is up
        child = (
            "import json, sys, time\n"
            f"sys.path.insert(0, {str(TESTS_DIR)!r})\n"
            "import _cli\n"
            "wa = _cli.load()\n"
            "db = wa._open_store(json.loads(sys.argv[1]))\n"
            "for n in range(3):\n"
            "    time.sleep(0.4)\n"
            "    wa._queue_outgoing(db, {'chat_id': %r, 'text': f'x{n}',"
            " 'reply_to': None, 'mentions': []})\n"
            "    print(time.time(), flush=True)\n" % CHAT)
        done = subprocess.run([sys.executable, "-c", child, json.dumps(self.cfg)],
                              capture_output=True, text=True, timeout=60,
                              env=dict(os.environ))
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        committed = [float(line) for line in done.stdout.split()]
        sends = [self.sent(held, n + 1) for n in range(3)]
        times = [s - c for s, c in zip(sends, committed)]
        sys.stderr.write("\n  measured: other process commit -> send "
                         + ", ".join(f"{1000 * t:.1f} ms" for t in times) + "\n")
        self.assertLess(max(times), 0.5)


# ── Stall and stop, in a real process ───────────────────────────────────────

LISTENER = r'''
import json, os, sys, threading
from pathlib import Path
sys.path.insert(0, os.environ["WA_TESTS_DIR"])
import _cli
wa = _cli.load()
spec = json.loads(sys.argv[3])
cfg = spec["cfg"]


class Client:
    is_connected = True

    def disconnect(self):
        pass


class Held:
    def __init__(self):
        self._client = Client()
        self._logged_out = threading.Event()
        self._disconnected = threading.Event()
        self.disconnect_reason = None
        self.errors, self.live_messages, self.offline_count = [], 0, 0
        self.last_event_at = self.last_message_at = None
        self.stalled = False
        self.inflight = {}

    def open(self):
        return self

    def wait_connected(self, timeout=60):
        return True

    def require_account(self):
        return {"id": cfg["account_key"]}

    def refresh_identities(self):
        return 0

    def close(self):
        pass


for name, value in spec["constants"].items():
    setattr(wa, name, value)
daemon = wa._ServiceDaemon(cfg, Path(spec["root"]), {"environment": "test"},
                           launch_nonce="child", session_factory=Held)
daemon.stall_seconds = spec["stall"]
daemon.term_grace = spec["grace"]
daemon.listen_outbox = lambda: None
sys.exit(daemon.run())
'''


class Process(unittest.TestCase):
    """A listener in a process of its own, driven by signals."""

    def start(self, *, stall=60.0, grace=30.0, seam=True):
        self.cfg = {"id": "test", "home": tempfile.mkdtemp(),
                    "account_key": "15550000000", "engine": wa.ENGINE_INHOUSE}
        self.paths = wa._service_paths(self.cfg)
        spec = {"cfg": self.cfg, "root": tempfile.mkdtemp(), "stall": stall,
                "grace": grace, "constants": {"SERVICE_WATCH_EVERY": 0.2,
                                              "SERVICE_HEARTBEAT": 0.5}}
        env = {k: v for k, v in os.environ.items()
               if k not in ("CAPABILITIES_STORE_URL", wa.SERVICE_STALL_SEAM_ENV)}
        env.update(WA_TESTS_DIR=str(TESTS_DIR), XDG_CONFIG_HOME=tempfile.mkdtemp())
        if seam:
            env[wa.SERVICE_STALL_SEAM_ENV] = "1"
        self.stderr_path = Path(tempfile.mkdtemp()) / "stderr"
        self.stderr = self.stderr_path.open("w")
        self.addCleanup(self.stderr.close)
        self.proc = subprocess.Popen([sys.executable, "-c", LISTENER, "service", "run",
                                      json.dumps(spec)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=self.stderr)
        self.addCleanup(self.reap)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            health = wa._json_file(self.paths["health"]) or {}
            if health.get("state") == "healthy":
                return health
            self.assertIsNone(self.proc.poll(), self.stderr_path.read_text())
            time.sleep(0.05)
        self.fail("the listener did not come up: " + self.stderr_path.read_text())

    def reap(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)

    def exit_within(self, limit):
        began = time.monotonic()
        try:
            code = self.proc.wait(limit)
        except subprocess.TimeoutExpired:
            self.fail(f"still running after {limit}s")
        return code, time.monotonic() - began

    def test_a_blocked_loop_dumps_its_stacks_and_leaves_with_exit_9(self):
        self.start(stall=3.0)
        os.kill(self.proc.pid, signal.SIGUSR2)
        time.sleep(1.0)
        first = (wa._json_file(self.paths["health"]) or {}).get("updated_epoch")
        time.sleep(1.2)
        later = (wa._json_file(self.paths["health"]) or {}).get("updated_epoch")
        # The heartbeat keeps its own time while the loop is blocked.
        self.assertGreater(later, first)
        code, took = self.exit_within(15)
        sys.stderr.write(f"\n  measured: stall limit 3s, exit {code} {took + 2.2:.1f}s "
                         "after the loop blocked\n")
        self.assertEqual(code, wa.SERVICE_STALL_EXIT)
        self.assertLess(took + 2.2, 3.0 + 2.0)
        stderr = self.stderr_path.read_text()
        log = self.paths["log"].read_text()
        for text in (stderr, log):
            self.assertIn("stalled: the listener's loop made no progress", text)
            self.assertIn("most recent call first", text)
            self.assertIn("stall_seam", text)
        self.assertIn('"code": "service_stalled"', stderr)
        health = wa._json_file(self.paths["health"])
        self.assertEqual(health["state"], "stalled")

    def test_sigterm_is_honoured_within_the_grace_with_the_loop_blocked(self):
        self.start(stall=60.0, grace=1.5)
        os.kill(self.proc.pid, signal.SIGUSR2)
        time.sleep(0.5)
        os.kill(self.proc.pid, signal.SIGTERM)
        code, took = self.exit_within(10)
        sys.stderr.write(f"\n  measured: SIGTERM on a blocked loop, grace 1.5s, "
                         f"exit {code} after {took:.1f}s\n")
        self.assertEqual(code, wa.SERVICE_STALL_EXIT)
        self.assertLess(took, 1.5 + 1.5)
        stderr = self.stderr_path.read_text()
        self.assertIn("the stop request was not honoured within 1.5s", stderr)
        self.assertIn("most recent call first", stderr)

    def test_sigterm_on_a_working_loop_stops_it_cleanly(self):
        self.start(stall=60.0, grace=10.0, seam=False)
        os.kill(self.proc.pid, signal.SIGTERM)
        code, took = self.exit_within(10)
        self.assertEqual(code, 0)
        self.assertLess(took, 5)
        self.assertEqual(wa._json_file(self.paths["health"])["state"], "stopped")
        self.assertTrue(wa._account_lock_free(self.paths))


class Progress(unittest.TestCase):
    """What counts as a stall."""

    def daemon(self):
        cfg = {"id": "test", "home": tempfile.mkdtemp(), "account_key": "15550000000"}
        daemon = wa._ServiceDaemon(cfg, Path(tempfile.mkdtemp()),
                                   {"environment": "test",
                                    "defaults": {"stall_seconds": 45}},
                                   launch_nonce="n")
        daemon.log = lambda message: None
        return daemon

    def test_the_limit_is_the_setting(self):
        self.assertEqual(self.daemon().stall_seconds, 45)
        self.assertEqual(wa._settings_stall_seconds({}), wa.SERVICE_STALL_DEFAULT)

    def test_a_loop_that_stopped_going_round_is_a_stall(self):
        daemon = self.daemon()
        daemon.beat("holding the connection")
        self.assertIsNone(daemon.stalled_on())
        daemon.progress_at -= 46
        self.assertIn("holding the connection", daemon.stalled_on())

    def test_an_engine_handler_that_never_returns_is_a_stall(self):
        daemon = self.daemon()
        daemon.session = types.SimpleNamespace(inflight={
            1: ("_on_message", time.monotonic() - 46),
            2: ("_on_history", time.monotonic())})
        self.assertIn("_on_message", daemon.stalled_on())

    def test_waiting_to_reconnect_is_progress(self):
        daemon = self.daemon()
        daemon.progress_at -= 100
        with mock.patch.object(daemon.stop_requested, "wait"):
            daemon.wait(0)
        self.assertIsNone(daemon.stalled_on())

    def test_the_watchdog_leaves_once_with_the_stacks(self):
        daemon = self.daemon()
        daemon.backstop = False
        daemon.stall_seconds = 0.3
        exits = []
        left = threading.Event()

        def leave(code):
            exits.append(code)
            left.set()
        daemon.hard_exit = leave
        with mock.patch.object(wa, "SERVICE_WATCH_EVERY", 0.05), \
                mock.patch.object(daemon, "dump_stacks") as dump:
            threading.Thread(target=daemon.watch_progress, daemon=True).start()
            self.assertTrue(left.wait(5))
            daemon.leave_stalled("again")
        self.assertEqual(exits, [wa.SERVICE_STALL_EXIT])
        dump.assert_called_once()
        self.assertEqual(wa._json_file(daemon.paths["health"])["state"], "stalled")

    def test_the_setting_is_validated(self):
        schema = wa._service_schema()
        schema.validate_settings({"defaults": {"stall_seconds": 30}})
        for bad in (29, 3601, "x"):
            with self.assertRaisesRegex(ValueError, "stall_seconds"):
                schema.validate_settings({"defaults": {"stall_seconds": bad}})


# ── Taking over a stale holder ──────────────────────────────────────────────

HOLDER = r'''
import fcntl, json, os, signal, sys, time
spec = json.loads(sys.argv[3])
signal.signal(signal.SIGTERM, signal.SIG_IGN if spec["deaf"] else signal.SIG_DFL)
handle = open(spec["lock"], "a+")
fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def health(epoch):
    temporary = spec["health"] + ".tmp"
    with open(temporary, "w") as out:
        out.write(json.dumps({"launch_nonce": "old", "updated_epoch": epoch,
                              "state": "healthy", "connected": True}))
    os.replace(temporary, spec["health"])


with open(spec["owner"], "w") as out:
    out.write(json.dumps({"pid": os.getpid(), "launch_nonce": "old"}))
health(time.time() - spec["age"])
print("ready", flush=True)
while True:
    time.sleep(0.3)
    if spec["fresh"]:
        health(time.time())
'''


class Takeover(unittest.TestCase):
    """A new listener ends a holder whose health went stale beyond the limit
    and takes the account; a healthy holder, or one that is no listener, is
    left alone and the new one exits 8."""

    def setUp(self):
        self.cfg = {"id": "test", "home": tempfile.mkdtemp(),
                    "account_key": "15550000000", "engine": wa.ENGINE_INHOUSE,
                    "expected_account_id": "15550000000"}
        self.paths = wa._service_paths(self.cfg)
        self.paths["state"].mkdir(parents=True)
        for name, value in (("SERVICE_TAKEOVER_GRACE", 1.0),
                            ("SERVICE_STALE_CONFIRM", 0.0),
                            ("SERVICE_TICK", 0.02), ("SERVICE_HEARTBEAT", 0.1)):
            patcher = mock.patch.object(wa, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def holder(self, *, age, fresh=False, deaf=True, verb="service"):
        spec = {"lock": str(self.paths["lock"]), "owner": str(self.paths["owner"]),
                "health": str(self.paths["health"]), "age": age, "fresh": fresh,
                "deaf": deaf}
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, verb, "run",
                                 json.dumps(spec)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(10),
                                 proc.stdout.close()))
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc

    def daemon(self):
        daemon = wa._ServiceDaemon(self.cfg, Path(tempfile.mkdtemp()),
                                   {"environment": "test",
                                    "defaults": {"stall_seconds": 30}},
                                   launch_nonce="new",
                                   session_factory=lambda: Held())
        daemon.install_signals = lambda: None
        daemon.listen_outbox = lambda: None
        daemon.backstop = False
        self.logs: list[str] = []
        daemon.log = self.logs.append
        return daemon

    def run_daemon(self, daemon):
        result = {}

        def body():
            try:
                result["code"] = daemon.run()
            except wa._Refusal as refusal:
                result["refusal"] = refusal
        thread = threading.Thread(target=body, daemon=True)
        thread.start()
        return thread, result

    def test_a_stale_holder_is_ended_and_the_account_taken(self):
        holder = self.holder(age=120)
        daemon = self.daemon()
        thread, result = self.run_daemon(daemon)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and daemon.health.get("state") != "healthy":
            time.sleep(0.02)
        self.assertEqual(daemon.health.get("state"), "healthy", result)
        # It ignored SIGTERM, so it was killed.
        self.assertEqual(holder.wait(5), -signal.SIGKILL)
        self.assertEqual(daemon.took_over, holder.pid)
        owner = json.loads(self.paths["owner"].read_text())
        self.assertEqual((owner["pid"], owner["launch_nonce"]), (os.getpid(), "new"))
        self.assertTrue(any("ending it and taking over" in line for line in self.logs))
        daemon.request_stop()
        thread.join(10)
        self.assertEqual(result.get("code"), 0)

    def test_a_stale_holder_that_listens_to_sigterm_is_not_killed(self):
        holder = self.holder(age=120, deaf=False)
        daemon = self.daemon()
        thread, result = self.run_daemon(daemon)
        self.assertEqual(holder.wait(10), -signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and daemon.health.get("state") != "healthy":
            time.sleep(0.02)
        self.assertEqual(daemon.took_over, holder.pid)
        daemon.request_stop()
        thread.join(10)

    def test_a_healthy_holder_is_respected(self):
        holder = self.holder(age=0, fresh=True)
        daemon = self.daemon()
        thread, result = self.run_daemon(daemon)
        thread.join(10)
        refusal = result.get("refusal")
        self.assertIsNotNone(refusal)
        self.assertEqual((refusal.exit_code, refusal.code), (8, "service_running"))
        self.assertIsNone(holder.poll())

    def test_a_stale_holder_that_is_no_listener_is_never_signalled(self):
        holder = self.holder(age=120, verb="other")
        daemon = self.daemon()
        thread, result = self.run_daemon(daemon)
        thread.join(10)
        self.assertEqual(result["refusal"].exit_code, 8)
        self.assertIsNone(holder.poll())

    def test_start_hands_a_stale_holder_to_the_listener_and_refuses_a_healthy_one(self):
        holder = self.holder(age=0, fresh=True)
        with mock.patch.object(wa, "_service_prepare",
                               return_value=(Path("/p"), {}, self.cfg)):
            with self.assertRaises(wa._Refusal) as caught:
                wa._cmd_service_start(None)
        self.assertEqual(caught.exception.code, "service_running")
        holder.kill()
        holder.wait(5)
        stale = self.holder(age=200)
        launched = []

        class Launched:
            pid = 0
            returncode = 3

            def __init__(self, command, **kwargs):
                launched.append(command)

            def poll(self):
                return 3
        with mock.patch.object(wa, "_service_prepare",
                               return_value=(Path("/p"), {}, self.cfg)), \
                mock.patch.object(wa.subprocess, "Popen", Launched):
            with self.assertRaises(wa._Refusal) as caught:
                wa._cmd_service_start(None)
        # Not refused as running: the listener was launched to take over.
        self.assertEqual(caught.exception.code, "service_start_failed")
        self.assertEqual(launched[0][1:3], ["service", "run"])
        self.assertIsNone(stale.poll())


# ── Reports ─────────────────────────────────────────────────────────────────


class Reports(unittest.TestCase):
    """`service status`, `service doctor` and the deploy doctor, which is
    `service doctor`, report a live holder with stale health as a failure."""

    def setUp(self):
        self.cfg = {"id": "test", "home": tempfile.mkdtemp(),
                    "account_key": "15550000000", "engine": wa.ENGINE_INHOUSE,
                    "expected_account_id": "15550000000"}
        self.paths = wa._service_paths(self.cfg)
        self.paths["state"].mkdir(parents=True)
        wa._atomic_json(self.paths["owner"], {"pid": os.getpid(),
                                              "launch_nonce": "n",
                                              "project_root": "/p"})
        for target, value in (("_service_project_root", Path("/p")),
                              ("_service_settings", {"environment": "test"}),
                              ("_service_cfg", self.cfg),
                              ("_session_account", ("15550000000", None, None)),
                              ("_store_location", "the test store")):
            patcher = mock.patch.object(wa, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(wa, "_open_store",
                                    return_value=types.SimpleNamespace(close=lambda: None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def health(self, age, nonce="n"):
        wa._atomic_json(self.paths["health"], {
            "launch_nonce": nonce, "state": "healthy", "connected": True,
            "updated_epoch": time.time() - age})

    def test_status_reports_stale_with_its_age(self):
        self.health(age=300)
        status = wa._cmd_service_status(None)
        self.assertEqual((status["running"], status["state"], status["healthy"]),
                         (True, "stale", False))
        self.assertGreaterEqual(status["health_age_seconds"], 300)
        self.health(age=1)
        self.assertEqual(wa._cmd_service_status(None)["state"], "healthy")

    def test_a_holder_that_never_published_goes_stale_from_its_start(self):
        self.health(age=1, nonce="someone-else")
        old = time.time() - 300
        os.utime(self.paths["owner"], (old, old))
        status = wa._cmd_service_status(None)
        self.assertEqual(status["state"], "stale")
        self.assertGreaterEqual(status["health_age_seconds"], 300)

    def test_doctor_fails_on_stale_health(self):
        self.health(age=300)
        report, fail = wa._cmd_service_doctor(None)
        self.assertEqual(fail, 5)
        ownership = [c for c in report["checks"] if c["item"] == "ownership"][0]
        self.assertEqual((ownership["ok"], ownership["code"]), (False, "service_stale"))
        self.assertIn("published no health for 300s", ownership["detail"])
        self.health(age=1)
        self.assertEqual(wa._cmd_service_doctor(None)[1], 0)

    def test_the_deploy_doctor_is_service_doctor_and_exits_non_zero(self):
        self.assertEqual(wa.SERVICE["deploy"]["doctor"], ["whatsapp", "service", "doctor"])
        self.health(age=300)
        args = types.SimpleNamespace(service_command="doctor", connection=None)
        with mock.patch.object(wa, "_emit"):
            with self.assertRaises(SystemExit) as caught:
                wa.cmd_service(args)
        self.assertEqual(caught.exception.code, 5)


if __name__ == "__main__":
    unittest.main()
