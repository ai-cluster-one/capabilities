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
from urllib.parse import parse_qs, urlencode

import httpx

CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "instagram", CAPABILITY / "instagram")
    if path.is_file()), CAPABILITY / "bin" / "instagram")


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
            "av": "actor-123",
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


def _bootstrap_page() -> str:
    bootstrap = {
        "require": [
            ["DTSGInitialData", [], {"token": "dtsg-token"}],
            ["LSD", [], {"token": "lsd-token"}],
            ["SiteData", [], {"server_revision": 123}],
        ]
    }
    return (
        '<script type="application/json">'
        + html.escape(json.dumps(bootstrap), quote=False)
        + "</script>"
    )


def _requests_payload(
    *,
    edges=None,
    has_next_page: bool = False,
    spam_edges=None,
    spam_has_next_page: bool = False,
    include_spam: bool = True,
):
    data = {
        "get_slide_mailbox_for_iris_subscription": {
            "threads_by_folder": {
                "edges": edges or [],
                "page_info": {
                    "end_cursor": "next-requests" if has_next_page else None,
                    "has_next_page": has_next_page,
                },
            }
        }
    }
    if include_spam:
        data["spamMailbox"] = {
            "threads_by_folder": {
                "edges": spam_edges or [],
                "page_info": {
                    "end_cursor": "next-spam" if spam_has_next_page else None,
                    "has_next_page": spam_has_next_page,
                },
            }
        }
    return {"data": data}


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

    def test_relationship_follow_uses_validated_profile_and_server_confirmation(self) -> None:
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
        posts = []
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path, allow_write=True))
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                if request.method == "POST":
                    posts.append(request)
                    form = parse_qs(request.content.decode())
                    self.assertEqual(
                        form["fb_api_req_friendly_name"][0],
                        instagram.FOLLOW_FRIENDLY_NAME,
                    )
                    self.assertEqual(form["doc_id"][0], instagram.FOLLOW_DOC_ID)
                    variables = json.loads(form["variables"][0])
                    self.assertEqual(variables["target_user_id"], "12345")
                    self.assertEqual(variables["container_module"], "profile")
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "xdt_create_friendship": {
                                    "username": "target.user",
                                    "id": "12345",
                                    "friendship_status": {
                                        "following": True,
                                        "outgoing_request": False,
                                        "followed_by": False,
                                    },
                                }
                            }
                        },
                        request=request,
                    )
                if request.url.path == "/target.user/":
                    return httpx.Response(200, text=page, request=request)
                self.assertEqual(request.url.path, "/api/v1/users/web_profile_info/")
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "data": {
                            "user": {
                                "id": "12345",
                                "username": "target.user",
                                "is_private": False,
                                "followed_by_viewer": False,
                                "requested_by_viewer": False,
                                "follows_viewer": False,
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
                result = client.follow("target.user")
            finally:
                client.close()
            self.assertEqual(result["status"], "followed")
            self.assertTrue(result["following"])
            self.assertFalse(result["outgoing_request"])
            self.assertEqual(len(posts), 1)

    def test_relationship_follow_is_idempotent_when_already_following(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path, allow_write=True))
            client.relationship_show = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
                "is_private": False,
                "status": "following",
                "following": True,
                "outgoing_request": False,
                "followed_by": False,
                "blocked": False,
                "restricted": False,
            }
            try:
                result = client.follow("target.user")
            finally:
                client.close()
            self.assertEqual(result["status"], "already_following")
            self.assertTrue(result["following"])

    def test_messages_inspect_finds_thread_and_prior_outgoing_without_text(self) -> None:
        page = _bootstrap_page()
        inbox_calls = 0
        post_calls = 0
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal inbox_calls, post_calls
                if request.method == "POST":
                    post_calls += 1
                    return httpx.Response(500, request=request)
                if request.url.path == "/target.user/":
                    return httpx.Response(200, text=page, request=request)
                if request.url.path == "/api/v1/users/web_profile_info/":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ok",
                            "data": {"user": {"id": "12345", "username": "target.user"}},
                        },
                        request=request,
                    )
                if request.url.path == "/api/v1/direct_v2/inbox/":
                    inbox_calls += 1
                    if inbox_calls == 1:
                        return httpx.Response(
                            200,
                            json={
                                "status": "ok",
                                "inbox": {
                                    "threads": [],
                                    "has_older": True,
                                    "oldest_cursor": "next-page",
                                },
                            },
                            request=request,
                        )
                    self.assertEqual(request.url.params.get("cursor"), "next-page")
                    return httpx.Response(
                        200,
                        json={
                            "status": "ok",
                            "inbox": {
                                "threads": [
                                    {
                                        "thread_id": "thread-1",
                                        "thread_v2_id": "igid-1",
                                        "is_group": False,
                                        "pending": False,
                                        "users": [
                                            {"pk": "12345", "username": "target.user"}
                                        ],
                                    }
                                ],
                                "has_older": False,
                            },
                        },
                        request=request,
                    )
                self.assertEqual(
                    request.url.path, "/api/v1/direct_v2/threads/thread-1/"
                )
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "thread": {
                            "thread_id": "thread-1",
                            "viewer_id": "sender-1",
                            "items": [
                                {
                                    "item_id": "incoming-1",
                                    "user_id": "12345",
                                    "timestamp": "200",
                                    "item_type": "text",
                                    "text": "recipient secret",
                                },
                                {
                                    "item_id": "outgoing-1",
                                    "message_id": "mid.outgoing",
                                    "user_id": "sender-1",
                                    "is_sent_by_viewer": True,
                                    "timestamp": "100",
                                    "item_type": "text",
                                    "text": "viewer secret",
                                },
                            ],
                            "has_older": True,
                            "oldest_cursor": "older",
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
                result = client.messages_inspect("target.user")
            finally:
                client.close()
            self.assertEqual(result["status"], "already_contacted")
            self.assertTrue(result["viewer_sent_message"])
            self.assertEqual(result["last_outgoing_item"]["item_id"], "outgoing-1")
            rendered = json.dumps(result)
            self.assertNotIn("recipient secret", rendered)
            self.assertNotIn("viewer secret", rendered)
            self.assertEqual(inbox_calls, 2)
            self.assertEqual(post_calls, 0)

    def test_messages_inspect_proves_no_conversation_after_complete_scan(self) -> None:
        page = _bootstrap_page()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/target.user/":
                    return httpx.Response(200, text=page, request=request)
                if request.url.path == "/api/v1/users/web_profile_info/":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ok",
                            "data": {"user": {"id": "12345", "username": "target.user"}},
                        },
                        request=request,
                    )
                if request.url.path == "/api/graphql":
                    form = parse_qs(request.content.decode())
                    self.assertEqual(
                        form["fb_api_req_friendly_name"][0],
                        instagram.DIRECT_REQUESTS_FRIENDLY_NAME,
                    )
                    return httpx.Response(
                        200, json=_requests_payload(), request=request
                    )
                self.assertEqual(request.url.path, "/api/v1/direct_v2/inbox/")
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "inbox": {"threads": [], "has_older": False},
                    },
                    request=request,
                )

            client.http = httpx.Client(
                transport=httpx.MockTransport(handler),
                cookies=client.template.cookies,
                follow_redirects=True,
            )
            try:
                result = client.messages_inspect("target.user")
            finally:
                client.close()
            self.assertEqual(result["status"], "no_conversation")
            self.assertFalse(result["viewer_sent_message"])
            self.assertTrue(result["scan_complete"])

    def test_messages_inspect_finds_prior_outgoing_in_requests(self) -> None:
        page = _bootstrap_page()
        edge = {
            "node": {
                "as_ig_direct_thread": {
                    "id": "igid-request-1",
                    "thread_fbid": "igid-request-1",
                    "thread_id": "thread-request-1",
                    "is_group": False,
                    "users": [{"id": "12345", "username": "target.user"}],
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/target.user/":
                    return httpx.Response(200, text=page, request=request)
                if request.url.path == "/api/v1/users/web_profile_info/":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ok",
                            "data": {
                                "user": {"id": "12345", "username": "target.user"}
                            },
                        },
                        request=request,
                    )
                if request.url.path == "/api/v1/direct_v2/inbox/":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ok",
                            "inbox": {"threads": [], "has_older": False},
                        },
                        request=request,
                    )
                if request.url.path == "/api/graphql":
                    return httpx.Response(
                        200, json=_requests_payload(edges=[edge]), request=request
                    )
                self.assertEqual(
                    request.url.path,
                    "/api/v1/direct_v2/threads/thread-request-1/",
                )
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "thread": {
                            "thread_id": "thread-request-1",
                            "viewer_id": "sender-1",
                            "items": [
                                {
                                    "item_id": "outgoing-request-1",
                                    "message_id": "mid.request",
                                    "client_context": "offline-request",
                                    "user_id": "sender-1",
                                    "is_sent_by_viewer": True,
                                    "timestamp": "100",
                                    "item_type": "text",
                                    "text": "private text",
                                }
                            ],
                            "has_older": False,
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
                result = client.messages_inspect("target.user")
            finally:
                client.close()
            self.assertEqual(result["status"], "already_contacted")
            self.assertEqual(result["folder"], "requests")
            self.assertTrue(result["pending"])
            self.assertNotIn("private text", json.dumps(result))

    def test_conversation_find_paginates_requests_by_id_not_stale_handle(self) -> None:
        calls = 0
        edge = {
            "node": {
                "as_ig_direct_thread": {
                    "id": "igid-request-2",
                    "thread_id": "thread-request-2",
                    "is_group": False,
                    "users": [{"id": "12345", "username": "old.handle"}],
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.profile = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
            }
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                variables = json.loads(
                    parse_qs(request.content.decode())["variables"][0]
                )
                if calls == 1:
                    self.assertNotIn("after", variables)
                    return httpx.Response(
                        200,
                        json=_requests_payload(has_next_page=True),
                        request=request,
                    )
                self.assertEqual(variables["after"], "next-requests")
                return httpx.Response(
                    200, json=_requests_payload(edges=[edge]), request=request
                )

            client.http = httpx.Client(
                transport=httpx.MockTransport(handler),
                cookies=client.template.cookies,
                follow_redirects=True,
            )
            try:
                result = client.conversation_find("target.user", folders="requests")
            finally:
                client.close()
            self.assertEqual(result["status"], "found")
            self.assertEqual(result["folder"], "requests")
            self.assertEqual(result["pages_scanned"], 2)
            self.assertEqual(calls, 2)

    def test_conversation_find_labels_spam_and_fails_closed_if_missing(self) -> None:
        edge = {
            "node": {
                "as_ig_direct_thread": {
                    "id": "igid-spam-1",
                    "thread_id": "thread-spam-1",
                    "is_group": False,
                    "users": [{"id": "12345", "username": "target.user"}],
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.profile = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
            }
            client.http.close()
            responses = iter(
                [
                    _requests_payload(spam_edges=[edge]),
                    _requests_payload(include_spam=False),
                ]
            )

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=next(responses), request=request)

            client.http = httpx.Client(
                transport=httpx.MockTransport(handler),
                cookies=client.template.cookies,
                follow_redirects=True,
            )
            try:
                found = client.conversation_find("target.user", folders="spam")
                unknown = client.conversation_find("target.user", folders="spam")
            finally:
                client.close()
            self.assertEqual(found["status"], "found")
            self.assertEqual(found["folder"], "spam")
            self.assertEqual(found["folders_scanned"], ["spam"])
            self.assertEqual(unknown["status"], "unknown")
            self.assertFalse(unknown["scan_complete"])
            self.assertEqual(unknown["reason"], "direct_folder_unvalidated")

    def test_thread_identity_never_falls_back_to_handle_only(self) -> None:
        self.assertEqual(
            instagram.InstagramWebClient._thread_match_status(
                {"users": [{"id": "999", "username": "target.user"}]},
                username="target.user",
                recipient_pk="12345",
            ),
            "different",
        )
        self.assertEqual(
            instagram.InstagramWebClient._thread_match_status(
                {"users": [{"username": "target.user"}]},
                username="target.user",
                recipient_pk="12345",
            ),
            "unknown",
        )

    def test_messages_reconcile_matches_offline_id_without_exposing_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.conversation_find = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
                "found": True,
                "scan_complete": True,
                "folders_scanned": ["main"],
                "thread_id": "thread-1",
                "thread_igid": "igid-1",
                "pending": False,
                "folder": "main",
            }
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(
                    request.url.path, "/api/v1/direct_v2/threads/thread-1/"
                )
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "thread": {
                            "thread_id": "thread-1",
                            "viewer_id": "sender-1",
                            "items": [
                                {
                                    "item_id": "item-1",
                                    "message_id": "mid.delivered",
                                    "client_context": "offline-123",
                                    "user_id": "sender-1",
                                    "is_sent_by_viewer": True,
                                    "timestamp": "100",
                                    "item_type": "text",
                                    "text": "private text",
                                }
                            ],
                            "has_older": False,
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
                result = client.messages_reconcile(
                    "target.user", offline_id="offline-123"
                )
            finally:
                client.close()
            self.assertEqual(result["status"], "delivered")
            self.assertTrue(result["delivery_confirmed"])
            self.assertFalse(result["retry_safe"])
            self.assertEqual(
                result["matched_item"]["message_id"], "mid.delivered"
            )
            self.assertNotIn("private text", json.dumps(result))

    def test_messages_reconcile_matches_server_id_from_item_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.conversation_find = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
                "found": True,
                "scan_complete": True,
                "folders_scanned": ["spam"],
                "thread_id": "thread-1",
                "thread_igid": "igid-1",
                "pending": True,
                "folder": "spam",
            }
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "thread": {
                            "thread_id": "thread-1",
                            "viewer_id": "sender-1",
                            "items": [
                                {
                                    "item_id": "mid.server-returned",
                                    "user_id": "sender-1",
                                    "is_sent_by_viewer": True,
                                    "item_type": "text",
                                    "text": "never expose this",
                                }
                            ],
                            "has_older": False,
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
                result = client.messages_reconcile(
                    "target.user", server_message_id="mid.server-returned"
                )
            finally:
                client.close()
            self.assertTrue(result["delivery_confirmed"])
            self.assertFalse(result["retry_safe"])
            self.assertNotIn("never expose this", json.dumps(result))

    def test_messages_reconcile_not_found_never_authorizes_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.conversation_find = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
                "found": False,
                "scan_complete": True,
                "folders_scanned": ["main", "requests", "spam"],
            }
            try:
                result = client.messages_reconcile(
                    "target.user", offline_id="offline-missing"
                )
            finally:
                client.close()
            self.assertEqual(result["status"], "not_found")
            self.assertFalse(result["delivery_confirmed"])
            self.assertFalse(result["retry_safe"])

    def test_cli_contract_includes_spam_as_read_only(self) -> None:
        self.assertEqual(
            instagram._conversation_options(["--folders", "spam"]),
            (25, "spam"),
        )
        self.assertNotIn("messages.reconcile", instagram.WRITE_VERBS)
        self.assertIn("main|requests|spam|all", instagram.HELP)

    def test_messages_eligibility_marks_new_invitation_as_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.relationship_show = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
                "following": True,
                "followed_by": False,
                "blocked": False,
                "restricted": False,
            }
            client.messages_inspect = lambda *_args, **_kwargs: {
                "conversation_found": False,
                "scan_complete": True,
                "inbox_pages_scanned": 2,
            }
            try:
                result = client.messages_eligibility("target.user")
            finally:
                client.close()
            self.assertEqual(result["status"], "new_invitation_unverified")
            self.assertIsNone(result["eligible"])
            self.assertTrue(result["requires_new_invitation"])

    def test_messages_eligibility_accepts_existing_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.relationship_show = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
                "following": True,
                "followed_by": True,
                "blocked": False,
                "restricted": False,
            }
            client.messages_inspect = lambda *_args, **_kwargs: {
                "conversation_found": True,
                "scan_complete": True,
                "inbox_pages_scanned": 1,
                "thread_id": "thread-1",
                "thread_igid": "igid-1",
                "pending": False,
                "viewer_sent_message": True,
            }
            try:
                result = client.messages_eligibility("target.user")
            finally:
                client.close()
            self.assertEqual(result["status"], "existing_conversation")
            self.assertTrue(result["eligible"])
            self.assertFalse(result["requires_new_invitation"])

    def test_messages_eligibility_rejects_pending_outgoing_invitation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.relationship_show = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
                "following": False,
                "followed_by": False,
                "blocked": False,
                "restricted": False,
            }
            client.messages_inspect = lambda *_args, **_kwargs: {
                "conversation_found": True,
                "scan_complete": True,
                "inbox_pages_scanned": 1,
                "thread_id": "thread-1",
                "thread_igid": "igid-1",
                "pending": True,
                "viewer_sent_message": True,
            }
            try:
                result = client.messages_eligibility("target.user")
            finally:
                client.close()
            self.assertEqual(result["status"], "awaiting_acceptance")
            self.assertFalse(result["eligible"])
            self.assertEqual(result["reason"], "existing_chat_invitation_is_pending")

    def test_conversation_find_returns_unknown_when_page_limit_is_reached(self) -> None:
        page = _bootstrap_page()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path))
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/target.user/":
                    return httpx.Response(200, text=page, request=request)
                if request.url.path == "/api/v1/users/web_profile_info/":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ok",
                            "data": {"user": {"id": "12345", "username": "target.user"}},
                        },
                        request=request,
                    )
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "inbox": {
                            "threads": [],
                            "has_older": True,
                            "oldest_cursor": "older",
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
                result = client.conversation_find(
                    "target.user", max_pages=1, folders="main"
                )
            finally:
                client.close()
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["scan_complete"])
            self.assertEqual(result["reason"], "max_pages_reached")

    def test_media_list_and_like_use_validated_feed_item(self) -> None:
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
        posts = []
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path, allow_write=True))
            client.http.close()

            def handler(request: httpx.Request) -> httpx.Response:
                if request.method == "POST":
                    posts.append(request)
                    form = parse_qs(request.content.decode())
                    self.assertEqual(
                        form["fb_api_req_friendly_name"][0],
                        instagram.LIKE_FRIENDLY_NAME,
                    )
                    self.assertEqual(form["doc_id"][0], instagram.LIKE_DOC_ID)
                    variables = json.loads(form["variables"][0])
                    self.assertEqual(variables["input"]["actor_id"], "actor-123")
                    self.assertEqual(variables["input"]["media_id"], "98765")
                    self.assertEqual(
                        variables["input"]["tracking_token"], "tracking-token"
                    )
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "xig_media_like": {
                                    "media": {
                                        "id": "98765_12345",
                                        "has_liked": True,
                                    }
                                }
                            }
                        },
                        request=request,
                    )
                if request.url.path == "/target.user/":
                    return httpx.Response(200, text=page, request=request)
                if request.url.path == "/api/v1/users/web_profile_info/":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ok",
                            "data": {"user": {"id": "12345", "username": "target.user"}},
                        },
                        request=request,
                    )
                self.assertEqual(request.url.path, "/api/v1/feed/user/12345/")
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "items": [
                            {
                                "pk": "98765",
                                "id": "98765_12345",
                                "code": "POSTCODE",
                                "media_type": 1,
                                "product_type": "feed",
                                "has_liked": False,
                                "like_count": 10,
                                "taken_at": 123,
                                "organic_tracking_token": "tracking-token",
                                "user": {"username": "target.user"},
                            }
                        ],
                        "more_available": False,
                    },
                    request=request,
                )

            client.http = httpx.Client(
                transport=httpx.MockTransport(handler),
                cookies=client.template.cookies,
                follow_redirects=True,
            )
            try:
                result = client.like_media("target.user", "POSTCODE")
            finally:
                client.close()
            self.assertEqual(result["status"], "liked")
            self.assertEqual(result["media_pk"], "98765")
            self.assertNotIn("_tracking_token", result)
            self.assertEqual(len(posts), 1)

    def test_media_like_is_idempotent_when_feed_is_already_liked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            instagram._write_template(_template(), path)
            client = instagram.InstagramWebClient(_connection(path, allow_write=True))
            client.media_list = lambda *_args, **_kwargs: {
                "username": "target.user",
                "recipient_pk": "12345",
                "items": [
                    {
                        "media_pk": "98765",
                        "media_id": "98765_12345",
                        "code": "POSTCODE",
                        "permalink": "https://www.instagram.com/p/POSTCODE/",
                        "owner_username": "target.user",
                        "media_type": 1,
                        "product_type": "feed",
                        "has_liked": True,
                        "like_count": 11,
                        "taken_at": 123,
                        "is_pinned": False,
                        "_tracking_token": "tracking-token",
                    }
                ],
                "more_available": False,
                "next_max_id": None,
            }
            try:
                result = client.like_media("target.user", "POSTCODE")
            finally:
                client.close()
            self.assertEqual(result["status"], "already_liked")
            self.assertNotIn("_tracking_token", result)

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
