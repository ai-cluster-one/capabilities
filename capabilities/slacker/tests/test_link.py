#!/usr/bin/env python3
"""What `slacker link` decides with no browser and no network: how it reads a
workspace session out of Chrome over the DevTools protocol, how it picks a
workspace among several, how it names the connection, and that it refuses
cleanly — leaving nothing written — when there is no Chrome, the window is
closed, or sign-in times out. The websocket and Slack libraries are stubbed by
`_cli.load()`, so the protocol exchange is exercised against a fake CDP peer
and a real local DevTools HTTP endpoint, never a GUI browser; the live sign-in
is proven by hand after release.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

su = _cli.load()


class _FakeCdp:
    """Stands in for the DevTools websocket: answers the exact commands `link`
    sends with canned CDP results."""

    def __init__(self, local_config=None, cookies=None):
        self._local_config = local_config
        self._cookies = cookies or []
        self.sent = []
        self.closed = False

    def call(self, method, **params):
        self.sent.append((method, params))
        if method == "Runtime.evaluate":
            value = None if self._local_config is None \
                else json.dumps(self._local_config)
            return {"result": {"value": value}}
        if method == "Network.enable":
            return {}
        if method == "Network.getAllCookies":
            return {"cookies": self._cookies}
        raise AssertionError(f"unexpected CDP method {method!r}")

    def close(self):
        self.closed = True


def _config(*teams):
    return {"teams": {t["id"]: t for t in teams}}


TEAM_A = {"id": "T001", "name": "Acme", "domain": "acme",
          "token": "xoxc-acme-123"}
TEAM_B = {"id": "T002", "name": "Beta Co", "domain": "beta",
          "token": "xoxc-beta-456"}
D_COOKIE = {"name": "d", "domain": ".slack.com", "value": "xoxd-the-cookie"}


class ReadsTheSessionOverTheProtocol(unittest.TestCase):
    def test_teams_are_read_from_local_config(self):
        cdp = _FakeCdp(local_config=_config(TEAM_A, TEAM_B))
        teams = su._read_teams(cdp)
        self.assertEqual({t["id"] for t in teams}, {"T001", "T002"})
        self.assertEqual(teams[0]["token"][:5], "xoxc-")
        # it asks localStorage for localConfig_v2, nothing else
        self.assertEqual(cdp.sent[0][0], "Runtime.evaluate")
        self.assertIn("localConfig_v2", cdp.sent[0][1]["expression"])

    def test_a_team_without_an_xoxc_token_is_skipped(self):
        bad = {"id": "T009", "name": "x", "domain": "x", "token": "xoxb-bot"}
        teams = su._read_teams(_FakeCdp(local_config=_config(TEAM_A, bad)))
        self.assertEqual([t["id"] for t in teams], ["T001"])

    def test_no_local_config_yet_reads_as_no_teams(self):
        self.assertEqual(su._read_teams(_FakeCdp(local_config=None)), [])

    def test_the_d_cookie_is_read_over_the_protocol(self):
        cdp = _FakeCdp(cookies=[{"name": "x", "domain": ".slack.com", "value": "no"},
                                D_COOKIE])
        self.assertEqual(su._read_d_cookie(cdp), "xoxd-the-cookie")
        self.assertIn(("Network.enable", {}), cdp.sent)

    def test_a_d_cookie_for_another_site_is_ignored(self):
        cdp = _FakeCdp(cookies=[{"name": "d", "domain": ".example.com", "value": "no"}])
        self.assertIsNone(su._read_d_cookie(cdp))


class PicksTheWorkspace(unittest.TestCase):
    def test_match_by_id_domain_or_name_case_insensitively(self):
        teams = [TEAM_A, TEAM_B]
        self.assertEqual(su._match_team(teams, "T002")["id"], "T002")
        self.assertEqual(su._match_team(teams, "acme")["id"], "T001")
        self.assertEqual(su._match_team(teams, "beta co")["id"], "T002")
        self.assertIsNone(su._match_team(teams, "gamma"))

    def test_connection_id_defaults_to_the_domain_then_name(self):
        self.assertEqual(su._link_connection_id(None, TEAM_A), "acme")
        self.assertEqual(su._link_connection_id("mine", TEAM_A), "mine")
        self.assertEqual(
            su._link_connection_id(None, {"id": "T1", "name": "My Space", "domain": None}),
            "my-space")


class FindsChrome(unittest.TestCase):
    def test_an_explicit_override_that_exists_is_used(self):
        import os
        exe = Path(self._tmp()) / "chrome"
        exe.write_text("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        os.environ["SLACKER_CHROME"] = str(exe)
        try:
            self.assertEqual(su._find_chrome(), str(exe))
        finally:
            os.environ.pop("SLACKER_CHROME", None)

    def test_a_bogus_override_finds_nothing(self):
        import os
        os.environ["SLACKER_CHROME"] = "/no/such/chrome-binary-xyz"
        try:
            self.assertIsNone(su._find_chrome())
        finally:
            os.environ.pop("SLACKER_CHROME", None)

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


class _JsonList(BaseHTTPRequestHandler):
    """A real local DevTools /json/list endpoint, so the discovery half of the
    exchange runs over a true socket."""

    targets: list = []

    def do_GET(self):
        if self.path == "/json/list":
            body = json.dumps(self.targets).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


class AwaitsSignInAgainstARealEndpoint(unittest.TestCase):
    def _serve(self, targets):
        _JsonList.targets = targets
        server = HTTPServer(("127.0.0.1", 0), _JsonList)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_it_reads_the_session_once_a_slack_page_appears(self):
        port = self._serve([{"type": "page", "url": "https://app.slack.com/client",
                             "webSocketDebuggerUrl": "ws://127.0.0.1:1/x"}])
        cdp = _FakeCdp(local_config=_config(TEAM_A), cookies=[D_COOKIE])
        self._with_fake_cdp(cdp)
        teams, cookie = su._await_session(port, timeout=5)
        self.assertEqual([t["id"] for t in teams], ["T001"])
        self.assertEqual(cookie, "xoxd-the-cookie")
        self.assertTrue(cdp.closed)

    def test_no_slack_page_before_the_deadline_is_link_timeout(self):
        port = self._serve([{"type": "page", "url": "https://news.example.com",
                             "webSocketDebuggerUrl": "ws://127.0.0.1:1/x"}])
        with self.assertRaises(su._Refusal) as caught:
            su._await_session(port, timeout=1)
        self.assertEqual(caught.exception.code, "link_timeout")

    def test_a_refused_endpoint_reads_as_the_window_closed(self):
        # nothing listening on this port → connection refused → window_closed
        with self.assertRaises(su._Refusal) as caught:
            su._await_session(59731, timeout=2)
        self.assertEqual(caught.exception.code, "window_closed")

    def _with_fake_cdp(self, cdp):
        original = su._Cdp
        su._Cdp = lambda ws_url: cdp
        self.addCleanup(lambda: setattr(su, "_Cdp", original))


class LinkRefusesCleanly(unittest.TestCase):
    class _Args:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def _args(self, **kw):
        base = dict(id=None, workspace=None, events=False, scope="global",
                    default=False, replace=False, timeout=2, connection=None)
        base.update(kw)
        return self._Args(**base)

    def test_no_chrome_refuses_and_points_to_paste(self):
        self._patch(su, "_find_chrome", lambda: None)
        self._forbid_persist()
        with self.assertRaises(su._Refusal) as caught:
            su.cmd_link(self._args())
        self.assertEqual(caught.exception.code, "no_chrome")
        self.assertIn("paste", (caught.exception.hint or ""))

    def test_a_closed_window_writes_nothing(self):
        self._patch(su, "_find_chrome", lambda: "/bin/true")
        self._patch(su, "_launch_chrome", lambda *a: (object(), 1))
        self._patch(su, "_end_chrome", lambda *a: None)
        def gone(*a):
            su._refuse(5, "window_closed", "closed")
        self._patch(su, "_await_session", gone)
        self._forbid_persist()
        with self.assertRaises(su._Refusal) as caught:
            su.cmd_link(self._args())
        self.assertEqual(caught.exception.code, "window_closed")

    def test_several_workspaces_without_a_selector_is_refused(self):
        self._stub_browser(teams=[TEAM_A, TEAM_B], cookie="xoxd-x")
        self._forbid_persist()
        with self.assertRaises(su._Refusal) as caught:
            su.cmd_link(self._args())
        self.assertEqual(caught.exception.code, "several_workspaces")

    def test_an_unverified_session_writes_nothing(self):
        self._stub_browser(teams=[TEAM_A], cookie="xoxd-x")
        self._patch(su, "_check_connection",
                    lambda cfg: {"ok": False, "category": 2, "error": "invalid_auth"})
        self._forbid_persist()
        with self.assertRaises(su._Refusal) as caught:
            su.cmd_link(self._args())
        self.assertEqual(caught.exception.code, "link_unverified")
        self.assertEqual(caught.exception.exit_code, 2)

    def test_the_happy_path_carries_no_secret_value_in_its_result(self):
        self._stub_browser(teams=[TEAM_A], cookie="xoxd-secret")
        self._patch(su, "_check_connection",
                    lambda cfg: {"ok": True, "user": "u", "user_id": "U1",
                                 "team": "Acme", "team_id": "T001", "url": "https://x"})
        captured = {}
        def fake_persist(cid, scope, token, cookie, team, default, replace):
            captured.update(cid=cid, token=token, cookie=cookie, scope=scope)
            return {"registry": "reg", "credentials": "/creds",
                    "token_env": "SLACKER_ACME_TOKEN", "cookie_env": "SLACKER_ACME_COOKIE"}
        self._patch(su, "_persist_link", fake_persist)
        out = su.cmd_link(self._args())
        self.assertTrue(out["ok"] and out["verified"])
        self.assertEqual(out["connection"], "acme")
        self.assertEqual(out["secrets"],
                         {"token_env": "SLACKER_ACME_TOKEN",
                          "cookie_env": "SLACKER_ACME_COOKIE"})
        # the persisted values were the ones read from the browser
        self.assertEqual(captured["token"], "xoxc-acme-123")
        self.assertEqual(captured["cookie"], "xoxd-secret")
        # no secret value anywhere in the emitted result
        blob = json.dumps(out)
        self.assertNotIn("xoxc-acme-123", blob)
        self.assertNotIn("xoxd-secret", blob)

    # helpers -----------------------------------------------------------------
    def _patch(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, original))

    def _stub_browser(self, teams, cookie):
        self._patch(su, "_find_chrome", lambda: "/bin/true")
        self._patch(su, "_launch_chrome", lambda *a: (object(), 1))
        self._patch(su, "_end_chrome", lambda *a: None)
        self._patch(su, "_await_session", lambda port, timeout: (teams, cookie))

    def _forbid_persist(self):
        def nope(*a, **k):
            raise AssertionError("a refused link must write no connection")
        self._patch(su, "_persist_link", nope)


if __name__ == "__main__":
    unittest.main()
