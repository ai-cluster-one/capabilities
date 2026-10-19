#!/usr/bin/env python3
"""The capture service's reconnect loop with no network and no store: a
realtime handshake the gateway refuses is logged by its status, keeps doubling
the backoff, and never reads as connected."""

from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

su = _cli.load()


class _Refused(Exception):
    status_code = 401


class _RefusingSocket:
    """A WebSocketApp whose handshake is refused, as websocket-client reports
    it: on_error with the bad status, then run_forever returns."""

    def __init__(self, url, on_open=None, on_message=None, on_error=None,
                 **_kwargs):
        self.on_error = on_error

    def run_forever(self, **_kwargs):
        self.on_error(self, _Refused("Handshake status 401 Unauthorized"))


class _Stop:
    """Records each backoff the loop waits, and stops it after a few."""

    def __init__(self, rounds: int):
        self.waits: list[float] = []
        self.rounds = rounds

    def is_set(self) -> bool:
        return len(self.waits) >= self.rounds

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    def set(self) -> None:
        self.rounds = 0


class RefusedHandshake(unittest.TestCase):
    def setUp(self):
        state = tempfile.mkdtemp()
        for patcher in (
                mock.patch.object(su, "_STATE_HOME", Path(state)),
                mock.patch.dict(sys.modules, {"websocket": types.SimpleNamespace(
                    WebSocketApp=_RefusingSocket)}),
                mock.patch.object(su, "_open_store", return_value=mock.Mock()),
                mock.patch.object(su, "SlackClient"),
                mock.patch.object(su, "_identity"),
                mock.patch.object(su.signal, "signal"),
                mock.patch.object(su._Capture, "_backfill")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cap = su._Capture({"id": "test", "account_binding": "T1",
                                "token": "xoxc-123", "cookie": "c"})

    def test_the_backoff_keeps_growing_and_each_refusal_is_logged(self):
        self.cap.stop = _Stop(rounds=7)
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(self.cap.run(), 0)
        self.assertEqual(self.cap.stop.waits, [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])
        self.assertEqual(err.getvalue().count(
            "realtime handshake failed: _Refused (HTTP 401)"), 7)
        self.assertNotIn("xoxc-123", err.getvalue())

    def test_a_refused_handshake_never_reports_connected(self):
        self.cap.stop = _Stop(rounds=1)
        states = []
        real = self.cap.health
        self.cap.health = lambda state, extra=None: (states.append(state),
                                                     real(state, extra))
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.cap.run()
        self.assertNotIn("connected", states)


class _Gateway:
    """A WebSocketApp the gateway answers with the given frames after the
    socket opens, then closes. Records what the listener dialled."""

    frames: list[str] = []
    dialled: list[dict] = []

    def __init__(self, url, on_open=None, on_message=None, on_error=None,
                 header=None, **_kwargs):
        self.on_open, self.on_message = on_open, on_message
        _Gateway.dialled.append({"url": url, "header": list(header or [])})

    def run_forever(self, **kwargs):
        _Gateway.dialled[-1]["origin"] = kwargs.get("origin")
        if self.on_open:
            self.on_open(self)
        for frame in _Gateway.frames:
            self.on_message(self, frame)

    def close(self):
        pass


class GatewayFrames(unittest.TestCase):
    def setUp(self):
        state = tempfile.mkdtemp()
        _Gateway.dialled = []
        for patcher in (
                mock.patch.object(su, "_STATE_HOME", Path(state)),
                mock.patch.dict(sys.modules, {"websocket": types.SimpleNamespace(
                    WebSocketApp=_Gateway)}),
                mock.patch.object(su, "_open_store", return_value=mock.Mock()),
                mock.patch.object(su, "SlackClient"),
                mock.patch.object(su, "_identity"),
                mock.patch.object(su.signal, "signal"),
                mock.patch.object(su._Capture, "_backfill")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cap = su._Capture({"id": "test", "account_binding": "T1",
                                "token": "xoxc-123", "cookie": "xoxd-c"})
        self.states: list[str] = []
        real = self.cap.health
        self.cap.health = lambda state, extra=None: (self.states.append(state),
                                                     real(state, extra))

    def test_the_socket_carries_the_session_cookie_and_the_web_origin(self):
        _Gateway.frames = ['{"type": "hello"}']
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.cap._listen()
        dial = _Gateway.dialled[0]
        self.assertIn("Cookie: d=xoxd-c", dial["header"])
        self.assertEqual(dial["origin"], "https://app.slack.com")
        self.assertFalse(any(h.lower().startswith("origin:") for h in dial["header"]))
        self.assertNotIn("gateway_server", dial["url"])

    def test_an_error_frame_is_a_failed_connect_not_connected(self):
        _Gateway.frames = ['{"type": "error", "error": '
                           '{"code": 401, "msg": "invalid_auth"}}']
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(self.cap._listen(), 0.0)
        self.assertNotIn("connected", self.states)
        self.assertIn("realtime handshake failed: gateway error invalid_auth (401)",
                      err.getvalue())
        self.assertNotIn("xoxc-123", err.getvalue())

    def test_health_counts_each_captured_event(self):
        _Gateway.frames = ['{"type": "hello"}',
                           '{"type": "message", "channel": "C1", "ts": "1.0"}',
                           '{"type": "message", "channel": "C1", "ts": "2.0"}']
        with mock.patch.object(su, "_handle_event", side_effect=lambda _db, e: (
                {"change": "message"} if e["type"] == "message" else None)), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.cap._listen()
        health = su._service_read(self.cap.home, "health.json")
        self.assertEqual(health["events"], 2)
        self.assertIsNotNone(health["last_event_at"])
        self.assertEqual(health["state"], "connected")


if __name__ == "__main__":
    unittest.main()
