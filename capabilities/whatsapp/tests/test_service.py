#!/usr/bin/env python3
"""The assistant service: its settings, who owns the account, what start
refuses, and how the listener holds, loses and regains the connection.

The listener is driven through its session seam with sessions that stand in for
the engine, so a dropped wire, a removed device and an engine that will not let
go are each produced on demand. Nothing here reaches WhatsApp or a store.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

wa = _cli.load()


def _cfg(home: str | None = None) -> dict:
    return {"id": "test", "home": home or tempfile.mkdtemp(),
            "account_key": "15550000000", "engine": wa.ENGINE_INHOUSE,
            "expected_account_id": "15550000000"}


class _Flag:
    def __init__(self, value=False):
        self.value = value

    def is_set(self):
        return self.value

    def set(self):
        self.value = True


class FakeClient:
    def __init__(self):
        self.disconnects = 0
        self.is_connected = True

    def disconnect(self):
        self.disconnects += 1


class FakeSession:
    """One session as the listener sees it. `drop_after` seconds after it
    connects the wire drops; `delivered` messages arrive on connecting, the way
    the offline queue drains; `logged_out` makes the device removed."""

    made: list = []

    def __init__(self, *, connects=True, drop_after=None, delivered=0,
                 logged_out=False, stalls=False, refuse=None):
        self.connects = connects
        self.drop_after = drop_after
        self.delivered = delivered
        self._logged_out = _Flag(logged_out)
        self._disconnected = threading.Event()
        self.disconnect_reason = None
        self.stalls = stalls
        self.refuse = refuse
        self.live_messages = 0
        self.offline_count = None
        self.errors: list = []
        self.last_event_at = None
        self.last_message_at = None
        self._client = FakeClient()
        self.stalled = False
        self.closed = False
        FakeSession.made.append(self)

    def open(self):
        if self.refuse:
            raise wa._Refusal(*self.refuse)
        return self

    def wait_connected(self, timeout=60):
        if not self.connects:
            time.sleep(min(timeout, 0.01))
            return False
        self.live_messages += self.delivered
        self.offline_count = self.delivered
        if self.delivered:
            self.last_message_at = time.time()
        if self.drop_after is not None:
            def drop():
                self.disconnect_reason = "Disconnected"
                self._disconnected.set()
            threading.Timer(self.drop_after, drop).start()
        return True

    def require_account(self):
        return {"id": "15550000000"}

    def refresh_identities(self):
        return 0

    def close(self):
        self.closed = True
        self.stalled = self.stalls


def _daemon(sessions, cfg=None, settings=None):
    queue = list(sessions)
    daemon = wa._ServiceDaemon(cfg or _cfg(), Path(tempfile.mkdtemp()),
                               settings or {"environment": "test"},
                               launch_nonce="nonce-1",
                               session_factory=lambda: queue.pop(0))
    daemon.install_signals = lambda: None
    daemon.log = lambda message: None
    waits = []
    real_wait = daemon.wait

    def wait(seconds):
        waits.append(seconds)
        real_wait(0)
    daemon.wait = wait
    return daemon, waits


FAST = {"SERVICE_TICK": 0.01, "SERVICE_HEARTBEAT": 0.05,
        "SERVICE_BACKOFF_START": 2.0, "SERVICE_BACKOFF_CEILING": 60.0}


def _fast():
    return mock.patch.multiple(wa, **FAST)


class Settings(unittest.TestCase):
    """The settings surface is closed: what it does not name is refused."""

    def test_the_seeded_template_is_valid(self):
        template = json.loads(wa._service_template().read_text())
        self.assertEqual(wa._validate_service_settings(template), template)
        self.assertEqual(set(template), set(wa.SERVICE_SETTINGS_KEYS))

    def test_an_unknown_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unsupported key"):
            wa._validate_service_settings({"connection": None, "allowed_users": {}})

    def test_a_bad_environment_is_refused(self):
        with self.assertRaisesRegex(ValueError, "environment"):
            wa._validate_service_settings({"environment": "two words"})

    def test_a_bad_connection_is_refused(self):
        with self.assertRaisesRegex(ValueError, "connection"):
            wa._validate_service_settings({"connection": ""})
        with self.assertRaisesRegex(ValueError, "connection"):
            wa._validate_service_settings({"connection": 7})

    def test_absent_settings_are_not_initialized(self):
        with mock.patch.object(wa, "_service_settings", return_value={}):
            with self.assertRaises(wa._Refusal) as caught:
                wa._checked_service_settings()
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (6, "service_not_initialized"))

    def test_invalid_settings_are_refused_where_they_are_read(self):
        with mock.patch.object(wa, "_service_settings",
                               return_value={"voice": True}):
            with self.assertRaises(wa._Refusal) as caught:
                wa._checked_service_settings()
        self.assertEqual(caught.exception.code, "service_settings_invalid")

    def test_reload_refuses_invalid_settings_before_signalling(self):
        with mock.patch.object(wa, "_service_settings",
                               return_value={"voice": True}), \
                mock.patch.object(wa, "_running_owner") as owner:
            with self.assertRaises(wa._Refusal) as caught:
                wa._cmd_service_reload(None, 1.0)
        self.assertEqual(caught.exception.code, "service_settings_invalid")
        owner.assert_not_called()


class Ownership(unittest.TestCase):
    """One listener per account, proven by the account lock and its owner
    record, and named to whoever is turned away."""

    def setUp(self):
        self.cfg = _cfg()
        self.paths = wa._service_paths(self.cfg)
        self.paths["state"].mkdir(parents=True)

    def _hold_lock(self):
        handle = self.paths["lock"].open("a+")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(handle.close)
        return handle

    def test_an_owner_whose_process_is_gone_owns_nothing(self):
        wa._atomic_json(self.paths["owner"], {"pid": 999999, "launch_nonce": "x"})
        self.assertIsNone(wa._service_owner(self.paths))

    def test_a_live_owner_is_reported(self):
        wa._atomic_json(self.paths["owner"], {"pid": os.getpid(),
                                              "launch_nonce": "x"})
        self.assertEqual(wa._service_owner(self.paths)["pid"], os.getpid())

    def test_the_lock_probe_sees_a_holder(self):
        self.assertTrue(wa._account_lock_free(self.paths))
        self._hold_lock()
        self.assertFalse(wa._account_lock_free(self.paths))

    def test_a_connected_verb_beside_the_service_is_told_whose_it_is(self):
        self._hold_lock()
        wa._atomic_json(self.paths["owner"], {
            "pid": os.getpid(), "project_root": "/projects/one"})
        with self.assertRaises(wa._Refusal) as caught:
            with wa._session_lock(self.cfg):
                pass
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (8, "session_busy"))
        self.assertIn("assistant service", caught.exception.message)
        self.assertIn("/projects/one", caught.exception.message)

    def test_a_verb_beside_another_verb_is_refused_as_before(self):
        self._hold_lock()
        with self.assertRaises(wa._Refusal) as caught:
            with wa._session_lock(self.cfg):
                pass
        self.assertEqual(caught.exception.code, "session_busy")
        self.assertIn("another invocation", caught.exception.message)

    def test_start_refuses_when_a_listener_owns_the_account(self):
        self._hold_lock()
        wa._atomic_json(self.paths["owner"], {"pid": os.getpid()})
        with mock.patch.object(wa, "_service_prepare",
                               return_value=(Path("/p"), {}, self.cfg)):
            with self.assertRaises(wa._Refusal) as caught:
                wa._cmd_service_start(None)
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (8, "service_running"))

    def test_start_refuses_while_a_verb_holds_the_session(self):
        self._hold_lock()
        with mock.patch.object(wa, "_service_prepare",
                               return_value=(Path("/p"), {}, self.cfg)):
            with self.assertRaises(wa._Refusal) as caught:
                wa._cmd_service_start(None)
        self.assertEqual(caught.exception.code, "session_busy")

    def test_run_refuses_a_second_listener(self):
        self._hold_lock()
        wa._atomic_json(self.paths["owner"], {"pid": os.getpid()})
        daemon, _ = _daemon([], cfg=self.cfg)
        with self.assertRaises(wa._Refusal) as caught:
            daemon.run()
        self.assertEqual(caught.exception.code, "service_running")


class StartRefusals(unittest.TestCase):
    """Each reason start will not go ahead is its own code."""

    def test_no_store_setting(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tempfile.mkdtemp()}), \
                mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("CAPABILITIES_STORE_URL", None)
            with self.assertRaises(wa._Refusal) as caught:
                wa._require_store_setting()
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (6, "store_not_configured"))

    def test_not_initialized_comes_before_the_account(self):
        with mock.patch.object(wa, "_service_project_root", return_value=Path("/p")), \
                mock.patch.object(wa, "_require_service_envelope"), \
                mock.patch.object(wa, "_service_settings", return_value={}), \
                mock.patch.object(wa, "_service_cfg") as cfg:
            with self.assertRaises(wa._Refusal) as caught:
                wa._service_prepare(None)
        self.assertEqual(caught.exception.code, "service_not_initialized")
        cfg.assert_not_called()

    def test_inherited_enablement_does_not_start_a_service(self):
        row = {"scope": "global", "value": {"enabled": True}}
        with mock.patch.object(sys, "argv", ["whatsapp", "service", "start"]), \
                mock.patch.object(wa, "_auth_gate"), \
                mock.patch.object(wa, "_machine_gate"), \
                mock.patch.object(wa, "read_only_switch", return_value=False), \
                mock.patch.object(wa, "_project_root", return_value=Path("/p")), \
                mock.patch.object(wa, "_policy_row", return_value=row), \
                mock.patch.object(wa, "_die", side_effect=wa._refuse):
            with self.assertRaises(wa._Refusal) as caught:
                wa._gate()
        self.assertEqual((caught.exception.exit_code, caught.exception.code),
                         (4, "project_enable_required"))

    def test_status_does_not_need_the_project_enable(self):
        row = {"scope": "global", "value": {"enabled": True}}
        with mock.patch.object(sys, "argv", ["whatsapp", "service", "status"]), \
                mock.patch.object(wa, "_auth_gate"), \
                mock.patch.object(wa, "_machine_gate"), \
                mock.patch.object(wa, "_policy_row", return_value=row):
            wa._gate()

    def test_a_bridge_connection_cannot_be_held(self):
        reg = {"connections": {"bridge": {"engine": "waha"}}}
        with mock.patch.object(wa, "_connections_registry", return_value=(reg, None)), \
                mock.patch.object(wa, "_build_cfg", return_value=(
                    {"id": "bridge", "engine": wa.ENGINE_WAHA}, None)):
            with self.assertRaises(wa._Refusal) as caught:
                wa._service_cfg({}, "bridge")
        self.assertEqual(caught.exception.code, "service_engine_unsupported")


class Listener(unittest.TestCase):
    """How the listener holds the wire, loses it, and comes back."""

    def setUp(self):
        FakeSession.made = []

    def _run_until(self, daemon, condition, limit=5.0):
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault(
            "code", daemon.loop()))
        thread.start()
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline and not condition():
            time.sleep(0.01)
        daemon.stop_requested.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        return result.get("code")

    def test_a_dropped_wire_is_reconnected_and_the_queue_drained(self):
        first = FakeSession(drop_after=0.05, delivered=1)
        second = FakeSession(delivered=2)
        daemon, waits = _daemon([first, second])
        with _fast():
            code = self._run_until(daemon, lambda: second.offline_count is not None
                                   and daemon.messages == 3)
        self.assertEqual(code, 0)
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(daemon.reconnects, 1)
        self.assertEqual(waits, [2.0])
        self.assertEqual(daemon.messages, 3)
        self.assertEqual(daemon.health["offline_drained"], 2)

    def test_the_wait_doubles_to_its_ceiling(self):
        sessions = [FakeSession(connects=False) for _ in range(8)]
        daemon, waits = _daemon(sessions + [FakeSession()])
        with _fast(), mock.patch.object(wa, "CONNECT_TIMEOUT", 0.02):
            self._run_until(daemon, lambda: len(waits) >= 7)
        self.assertEqual(waits[:7], [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])

    def test_a_forced_drop_disconnects_the_socket_and_reconnects(self):
        first = FakeSession()
        second = FakeSession()
        daemon, waits = _daemon([first, second])
        with _fast():
            def force():
                if first.offline_count is not None and not daemon.drop_requested.is_set() \
                        and not first.closed:
                    daemon.drop_requested.set()
                return second.offline_count is not None
            code = self._run_until(daemon, force)
        self.assertEqual(code, 0)
        self.assertEqual(first._client.disconnects, 1)
        self.assertEqual(daemon.reconnects, 1)

    def test_a_removed_device_stops_the_reconnecting(self):
        gone = FakeSession(connects=False, logged_out=True)
        daemon, waits = _daemon([gone, FakeSession()])
        with _fast():
            code = self._run_until(
                daemon, lambda: daemon.health.get("state") == "logged_out")
        self.assertEqual(code, 0)
        self.assertEqual(len(FakeSession.made), 2)   # the second never opened
        self.assertEqual(waits, [])
        self.assertEqual(daemon.health["state"], "logged_out")

    def test_an_unlinked_session_is_the_same_end(self):
        unlinked = FakeSession(refuse=(7, "not_linked", "no device"))
        daemon, waits = _daemon([unlinked])
        with _fast():
            self._run_until(daemon, lambda: daemon.health.get("state") == "logged_out")
        self.assertEqual(waits, [])

    def test_an_engine_that_will_not_let_go_ends_the_process(self):
        stuck = FakeSession(drop_after=0.01, stalls=True)
        daemon, waits = _daemon([stuck, FakeSession()])
        with _fast():
            code = daemon.loop()
        self.assertEqual(code, 5)
        self.assertEqual(waits, [])
        self.assertEqual(daemon.health["state"], "failed")

    def test_reload_swaps_valid_settings_and_keeps_the_old_on_refusal(self):
        daemon, _ = _daemon([], settings={"environment": "a"})
        with mock.patch.object(wa, "_service_settings",
                               return_value={"environment": "b"}):
            daemon.apply_reload()
        self.assertEqual(daemon.settings, {"environment": "b"})
        self.assertEqual(daemon.health["settings_generation"], 1)
        self.assertIsNone(daemon.health["settings_reload_error"])
        with mock.patch.object(wa, "_service_settings",
                               return_value={"environment": "c", "x": 1}):
            daemon.apply_reload()
        self.assertEqual(daemon.settings, {"environment": "b"})
        self.assertEqual(daemon.health["settings_reload_attempts"], 2)
        self.assertIn("unsupported", daemon.health["settings_reload_error"])

    def test_reload_will_not_switch_the_connection_underneath(self):
        daemon, _ = _daemon([])
        with mock.patch.object(wa, "_service_settings",
                               return_value={"connection": "other"}):
            daemon.apply_reload()
        self.assertIn("restart", daemon.health["settings_reload_error"])

    def test_run_owns_the_account_for_its_life_and_releases_it(self):
        cfg = _cfg()
        daemon, _ = _daemon([FakeSession()], cfg=cfg)
        paths = wa._service_paths(cfg)
        result = {}
        with _fast():
            thread = threading.Thread(target=lambda: result.setdefault(
                "code", daemon.run()))
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and \
                    daemon.health.get("state") != "healthy":
                time.sleep(0.01)
            owner = json.loads(paths["owner"].read_text())
            self.assertEqual(owner["launch_nonce"], "nonce-1")
            self.assertEqual(owner["pid"], os.getpid())
            self.assertFalse(wa._account_lock_free(paths))
            health = json.loads(paths["health"].read_text())
            self.assertTrue(health["connected"])
            daemon.stop_requested.set()
            thread.join(5)
        self.assertEqual(result["code"], 0)
        self.assertFalse(paths["owner"].exists())
        self.assertTrue(wa._account_lock_free(paths))
        self.assertEqual(json.loads(paths["health"].read_text())["state"],
                         "stopped")

    def test_status_reads_what_the_listener_published(self):
        cfg = _cfg()
        paths = wa._service_paths(cfg)
        daemon, _ = _daemon([], cfg=cfg)
        paths["state"].mkdir(parents=True)
        wa._atomic_json(paths["owner"], daemon.owner_record())
        daemon.publish("healthy", connected=True,
                       last_event_at="2026-01-01T00:00:00+00:00")
        with mock.patch.object(wa, "_service_project_root", return_value=Path("/p")), \
                mock.patch.object(wa, "_service_settings", return_value={"environment": "x"}), \
                mock.patch.object(wa, "_service_cfg", return_value=cfg):
            status = wa._cmd_service_status(None)
        self.assertTrue(status["running"])
        self.assertTrue(status["connected"])
        self.assertEqual(status["state"], "healthy")
        self.assertEqual(status["pid"], os.getpid())
        self.assertIsInstance(status["uptime_seconds"], int)
        self.assertEqual(status["last_event_at"], "2026-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
