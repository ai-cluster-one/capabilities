from __future__ import annotations

import html
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

import httpx

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "instagram"


def _load_module():
    name = "instagram_capability_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


instagram = _load_module()


def _capture(message: str = "secret probe") -> str:
    variables = {
        "ig_thread_igid": "12345",
        "offline_threading_id": "999",
        "text": {"sensitive_string_value": message},
        "mentions": [],
    }
    form = urlencode(
        {
            "fb_dtsg": "token",
            "variables": json.dumps(variables),
            "doc_id": "123456789",
        }
    )
    return f"""curl 'https://www.instagram.com/api/graphql' \\
-X 'POST' \\
-H 'Content-Type: application/x-www-form-urlencoded' \\
-H 'Content-Length: 1000' \\
-H 'Cookie: csrftoken=csrf; sessionid=session; rur="CLN\\0541\\0542:hash"' \\
-H 'X-CSRFToken: csrf' \\
-H 'X-FB-Friendly-Name: IGDirectTextSendMutation' \\
-H 'X-IG-App-ID: 936619743392459' \\
--data '{form}'
"""


def _template():
    return instagram._parse_curl(_capture())


def _connection(path: Path, *, allow_write: bool = False):
    return instagram.Connection(
        id="main",
        expected_username="sender.account",
        allow_write=allow_write,
        timeout_seconds=30,
        session_path=path,
    )


class CaptureTests(unittest.TestCase):
    def test_parse_redacts_message_and_sensitive_headers(self) -> None:
        template = _template()
        self.assertEqual(template.cookies["rur"], "CLN,1,2:hash")
        self.assertNotIn("Cookie", template.headers)
        self.assertNotIn("X-Csrftoken", template.headers)
        self.assertNotIn("Content-Length", template.headers)
        variables = json.loads(template.form["variables"])
        self.assertEqual(variables["offline_threading_id"], "")
        self.assertEqual(variables["text"]["sensitive_string_value"], "")

    def test_session_is_written_atomically_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "session.json"
            instagram._write_template(_template(), path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            loaded = instagram._load_template(path)
            self.assertEqual(loaded.cookies["sessionid"], "session")

    def test_wrong_mutation_is_rejected(self) -> None:
        capture = _capture().replace("IGDirectTextSendMutation", "OtherMutation")
        with self.assertRaises(instagram.InstagramFailure) as caught:
            instagram._parse_curl(capture)
        self.assertEqual(caught.exception.code, "capture_invalid")


class ProfileTests(unittest.TestCase):
    def test_extracts_profile_and_fresh_tokens(self) -> None:
        payload = {
            "require": [
                ["DTSGInitialData", [], {"token": "dtsg-token"}],
                ["LSD", [], {"token": "lsd-token"}],
                [
                    "SiteData",
                    [],
                    {
                        "server_revision": 123,
                        "hsi": "456",
                        "__spin_r": 123,
                        "__spin_b": "trunk",
                        "__spin_t": 789,
                    },
                ],
            ],
            "result": {
                "data": {
                    "xig_user_by_username": {
                        "username": "target.user",
                        "pk": "12345",
                        "full_name": "Target User",
                        "is_private": False,
                    }
                }
            },
        }
        page = (
            '<script type="application/json">'
            + html.escape(json.dumps(payload), quote=False)
            + "</script>"
        )
        dtsg, lsd, site, profile = instagram._bootstrap_profile(
            page, "target.user"
        )
        self.assertEqual(dtsg, "dtsg-token")
        self.assertEqual(lsd, "lsd-token")
        self.assertEqual(site["server_revision"], 123)
        self.assertEqual(profile["pk"], "12345")

    def test_normalizes_handle_and_rejects_non_instagram_url(self) -> None:
        self.assertEqual(
            instagram._normalize_profile("@target.user"),
            ("https://www.instagram.com/target.user/", "target.user"),
        )
        with self.assertRaises(instagram.InstagramFailure):
            instagram._normalize_profile("https://example.com/target.user")


class ResponseTests(unittest.TestCase):
    def test_success_requires_server_message_id(self) -> None:
        result = instagram._parse_send_response(
            200,
            json.dumps(
                {
                    "data": {
                        "xig_direct_text_send_with_slide_messaging_response": {
                            "message_id": "mid.$abc",
                            "timestamp_ms": "123",
                        }
                    }
                }
            ),
        )
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.server_message_id, "mid.$abc")

    def test_invitation_acceptance_is_terminal(self) -> None:
        result = instagram._parse_send_response(
            200,
            json.dumps(
                {
                    "data": None,
                    "errors": [
                        {
                            "code": 1545120,
                            "description_raw": (
                                "You can send more messages after your invitation "
                                "to chat is accepted."
                            ),
                        }
                    ],
                }
            ),
        )
        self.assertEqual(result.status, "awaiting_acceptance")


class ClientTests(unittest.TestCase):
    def test_preflight_checks_exact_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            conn = _connection(path)
            client = instagram.InstagramWebClient(conn)
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(str(request.url), instagram.PREFLIGHT_URL)
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "form_data": {"username": "sender.account"},
                    },
                    request=request,
                )

            client.http = httpx.Client(
                transport=httpx.MockTransport(handler),
                cookies=client.template.cookies,
                follow_redirects=True,
            )
            try:
                self.assertEqual(client.preflight()["username"], "sender.account")
            finally:
                client.close()

    def test_profile_falls_back_to_web_profile_info(self) -> None:
        bootstrap = {
            "require": [
                ["DTSGInitialData", [], {"token": "dtsg-token"}],
                ["LSD", [], {"token": "lsd-token"}],
                ["SiteData", [], {"server_revision": 123}],
            ]
        }
        page = (
            '<script type="application/json">'
            + html.escape(json.dumps(bootstrap), quote=False)
            + "</script>"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/target.user/":
                    return httpx.Response(200, text=page, request=request)
                self.assertEqual(request.url.path, "/api/v1/users/web_profile_info/")
                self.assertEqual(request.url.params["username"], "target.user")
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "data": {
                            "user": {
                                "id": "12345",
                                "username": "target.user",
                                "full_name": "Target User",
                                "is_private": False,
                                "edge_followed_by": {"count": 42},
                            }
                        },
                    },
                    request=request,
                )

            client.http = httpx.Client(
                transport=httpx.MockTransport(handler),
                cookies=client.template.cookies,
                follow_redirects=True,
            )
            try:
                profile = client.profile("target.user")
            finally:
                client.close()
            self.assertEqual(profile["recipient_pk"], "12345")
            self.assertEqual(profile["follower_count"], 42)

    def test_resolve_thread_validates_recipient(self) -> None:
        payload = {
            "require": [
                ["DTSGInitialData", [], {"token": "dtsg-token"}],
                ["LSD", [], {"token": "lsd-token"}],
                ["SiteData", [], {"server_revision": 123}],
            ],
            "profile": {"username": "target.user", "pk": "12345"},
        }
        page = (
            '<script type="application/json">'
            + html.escape(json.dumps(payload), quote=False)
            + "</script>"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            conn = _connection(path, allow_write=True)
            client = instagram.InstagramWebClient(conn)
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                if request.method == "GET":
                    return httpx.Response(200, text=page, request=request)
                return httpx.Response(
                    200,
                    json={
                        "thread_v2_id": "9988",
                        "thread_type": "private",
                        "users": [
                            {"pk": "12345", "username": "target.user"}
                        ],
                    },
                    request=request,
                )

            client.http = httpx.Client(
                transport=httpx.MockTransport(handler),
                cookies=client.template.cookies,
                follow_redirects=True,
            )
            try:
                result = client.resolve_thread("target.user")
            finally:
                client.close()
            self.assertEqual(result.thread_igid, "9988")
            self.assertEqual(result.recipient_pk, "12345")


class PolicyTests(unittest.TestCase):
    def test_send_is_refused_before_session_access_on_read_only_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            (config / "capabilities").mkdir(parents=True)
            (config / "capabilities" / "settings.json").write_text(
                json.dumps({"capabilities": {"instagram": {"enabled": True}}})
            )
            (config / "instagram").mkdir()
            (config / "instagram" / "connections.json").write_text(
                json.dumps(
                    {
                        "default": "main",
                        "connections": {
                            "main": {
                                "expected_username": "sender.account",
                                "allow_write": False,
                            }
                        },
                    }
                )
            )
            env = dict(os.environ)
            env.update(
                {
                    "HOME": str(root / "home"),
                    "XDG_CONFIG_HOME": str(config),
                    "XDG_STATE_HOME": str(root / "state"),
                }
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "messages",
                    "send",
                    "target.user",
                    "--text",
                    "hi",
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(proc.returncode, 4, proc.stderr)
            self.assertEqual(json.loads(proc.stderr)["error"]["code"], "read_only")


if __name__ == "__main__":
    unittest.main()
