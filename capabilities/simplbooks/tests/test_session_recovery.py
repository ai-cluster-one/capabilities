"""Session recovery — the one choke point every GET flows through.

SimpleBooks keeps a single live session per account, so a login on another host
expires this one's cookie mid-run. Recovery therefore belongs to the client, not
to each read path: `SessionClient.get` re-authenticates, re-seats the refreshed
cookies and replays the request exactly once. These tests pin the boundary —
which responses count as an expiry, which are business outcomes, and that POST
is never retried (a 302 after a CakePHP POST is a SUCCESS redirect).
"""
from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "simplbooks"


def _load_module():
    name = "simplbooks_session_recovery_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


simplbooks = _load_module()

ACCOUNT = "a" * 32
APP = f"{simplbooks.BASE}/{ACCOUNT}"
FRESH_COOKIE = "SB_APP=fresh; SB_APP_CSRF=fresh-csrf"


def _response(status: int, *, location: str | None = None, text: str = "ok") -> httpx.Response:
    headers = {"location": location} if location else {}
    return httpx.Response(status, headers=headers, text=text)


class _Recorder:
    """Scripted transport: returns the queued response per call, recording each request."""

    def __init__(self, responses: list[httpx.Response]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.responses.pop(0)
        return httpx.MockTransport(handler)


class SessionStaleTests(unittest.TestCase):
    """Which responses mean "the session is gone" rather than "here is your answer"."""

    def _resp(self, status, location=None, url=f"{APP}/financial_reports/turnoverstatement"):
        r = _response(status, location=location)
        r.request = httpx.Request("GET", url)
        return r

    def test_login_host_redirect_is_stale(self):
        # No usable cookie at all: SB bounces to the SSO login page.
        loc = (f"{simplbooks.LOGIN_URL}?redirect="
               f"https%3A%2F%2Fapp.simplbooks.com%2F{ACCOUNT}%2Finvoices%2Fadd")
        self.assertTrue(simplbooks._session_stale(self._resp(302, loc)))

    def test_self_redirect_is_stale(self):
        # App cookie no longer matches the SSO session: SB re-handshakes by
        # redirecting the url back at itself.
        url = f"{APP}/financial_reports/turnoverstatement"
        self.assertTrue(simplbooks._session_stale(self._resp(302, url, url=url)))

    def test_401_is_stale(self):
        self.assertTrue(simplbooks._session_stale(self._resp(401)))

    def test_business_redirect_is_not_stale(self):
        # A payment-locked purchase bounces edit → view. That is an answer, not
        # an expiry — recovering here would fire a pointless login and then fail.
        r = self._resp(302, f"{APP}/purchases/view/771", url=f"{APP}/purchases/edit/771")
        self.assertFalse(simplbooks._session_stale(r))

    def test_redirect_to_list_after_delete_is_not_stale(self):
        r = self._resp(302, f"{APP}/purchases", url=f"{APP}/purchases/view/771")
        self.assertFalse(simplbooks._session_stale(r))

    def test_200_is_not_stale(self):
        self.assertFalse(simplbooks._session_stale(self._resp(200)))


class SessionClientTests(unittest.TestCase):
    """The choke point itself."""

    def _client(self, responses: list[httpx.Response]) -> tuple[simplbooks.SessionClient, _Recorder]:
        rec = _Recorder(responses)
        client = simplbooks.SessionClient(
            cookies={"SB_APP": "stale"}, follow_redirects=False,
            transport=rec.transport())
        return client, rec

    def test_recovers_once_and_replays(self):
        client, rec = self._client([
            _response(302, location=simplbooks.LOGIN_URL),
            _response(200, text="<html>report</html>"),
        ])
        with mock.patch.object(simplbooks, "auto_login_attempt", return_value=True) as login, \
             mock.patch.object(simplbooks, "load_credentials",
                               return_value={"SIMPLBOOKS_COOKIE": FRESH_COOKIE}):
            r = client.get(f"{APP}/financial_reports/turnoverstatement")
        self.assertEqual(r.status_code, 200)
        login.assert_called_once_with(interactive=False)
        self.assertEqual(len(rec.requests), 2)
        # The retry carries the refreshed cookie, on this same live client.
        self.assertIn("SB_APP=fresh", rec.requests[1].headers["cookie"])
        self.assertEqual(client.cookies.get("SB_APP"), "fresh")

    def test_query_params_survive_the_replay(self):
        client, rec = self._client([
            _response(302, location=simplbooks.LOGIN_URL),
            _response(200),
        ])
        with mock.patch.object(simplbooks, "auto_login_attempt", return_value=True), \
             mock.patch.object(simplbooks, "load_credentials",
                               return_value={"SIMPLBOOKS_COOKIE": FRESH_COOKIE}):
            client.get(f"{APP}/bank_transactions/process", params={"limit": "200"})
        self.assertEqual(str(rec.requests[1].url), f"{APP}/bank_transactions/process?limit=200")

    def test_still_stale_after_login_raises_autherror(self):
        client, rec = self._client([
            _response(302, location=simplbooks.LOGIN_URL),
            _response(302, location=simplbooks.LOGIN_URL),
        ])
        with mock.patch.object(simplbooks, "auto_login_attempt", return_value=True) as login, \
             mock.patch.object(simplbooks, "load_credentials",
                               return_value={"SIMPLBOOKS_COOKIE": FRESH_COOKIE}):
            with self.assertRaises(simplbooks.AuthError) as ctx:
                client.get(f"{APP}/invoices/add")
        self.assertIn("refresh", str(ctx.exception))
        # Exactly once — never a loop.
        login.assert_called_once()
        self.assertEqual(len(rec.requests), 2)

    def test_healthy_get_never_logs_in(self):
        client, rec = self._client([_response(200)])
        with mock.patch.object(simplbooks, "auto_login_attempt") as login:
            r = client.get(f"{APP}/invoices/add")
        self.assertEqual(r.status_code, 200)
        login.assert_not_called()
        self.assertEqual(len(rec.requests), 1)

    def test_business_redirect_passes_through_untouched(self):
        client, rec = self._client([_response(302, location=f"{APP}/purchases/view/771")])
        with mock.patch.object(simplbooks, "auto_login_attempt") as login:
            r = client.get(f"{APP}/purchases/edit/771")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], f"{APP}/purchases/view/771")
        login.assert_not_called()
        self.assertEqual(len(rec.requests), 1)

    def test_post_is_never_retried(self):
        # A 302 after a CakePHP POST is the SUCCESS redirect. Retrying it would
        # misread a completed write — and could post it twice.
        client, rec = self._client([_response(302, location=f"{APP}/invoices/view/9001")])
        with mock.patch.object(simplbooks, "auto_login_attempt") as login:
            r = client.post(f"{APP}/invoices/update", data={"x": "1"})
        self.assertEqual(r.status_code, 302)
        login.assert_not_called()
        self.assertEqual(len(rec.requests), 1)

    def test_post_to_login_host_redirect_is_not_recovered_either(self):
        # Even the expiry shape stays untouched on a write: fail loudly, never
        # replay a mutation.
        client, rec = self._client([_response(302, location=simplbooks.LOGIN_URL)])
        with mock.patch.object(simplbooks, "auto_login_attempt") as login:
            r = client.post(f"{APP}/invoices/update", data={"x": "1"})
        self.assertEqual(r.status_code, 302)
        login.assert_not_called()
        self.assertEqual(len(rec.requests), 1)

    def test_build_http_hands_out_the_recovering_client(self):
        with simplbooks.build_http({"SB_APP": "x"}, ACCOUNT) as http:
            self.assertIsInstance(http, simplbooks.SessionClient)
        with simplbooks.build_http({"SB_APP": "x"}, ACCOUNT, ajax=True) as http:
            self.assertIsInstance(http, simplbooks.SessionClient)


REPORT_PAGE = (
    '<html><form><input name="_csrfToken" value="tok-1"/>'
    '<input name="report[id]" value="42"/></form></html>'
)


def _report_result(payload) -> str:
    blob = base64.b64encode(json.dumps(payload).encode()).decode()
    return f'<html><input type="hidden" name="export_data" value="{blob}"/></html>'


class ReportFetchRecoveryTests(unittest.TestCase):
    """The live defect: `reports trial-balance` died on an expired session
    instead of recovering, because the financial_reports GET was one of the
    read paths that never got the hand-wired retry."""

    def test_expired_session_recovers_and_the_report_still_decodes(self):
        payload = {"header": [{"type": "name"}], "rows": []}
        rec = _Recorder([
            _response(302, location=simplbooks.LOGIN_URL),   # evicted by another host
            _response(200, text=REPORT_PAGE),                # after auto-login
            _response(200, text=_report_result(payload)),    # the POST render
        ])
        client = simplbooks.SessionClient(
            cookies={"SB_APP": "stale"}, follow_redirects=False, transport=rec.transport())
        with client, \
                mock.patch.object(simplbooks, "auto_login_attempt", return_value=True) as login, \
                mock.patch.object(simplbooks, "load_credentials",
                                  return_value={"SIMPLBOOKS_COOKIE": FRESH_COOKIE}):
            out = simplbooks._report_export_data(
                client, ACCOUNT, "turnoverstatement", "01.01.2026", "15.08.2026")
        self.assertEqual(out, payload)
        login.assert_called_once_with(interactive=False)
        # GET, replayed GET, then the report POST — which carries the token
        # scraped from the page fetched with the REFRESHED cookie.
        self.assertEqual([r.method for r in rec.requests], ["GET", "GET", "POST"])
        self.assertIn(b"tok-1", rec.requests[2].content)
        self.assertIn("SB_APP=fresh", rec.requests[2].headers["cookie"])


if __name__ == "__main__":
    unittest.main()
