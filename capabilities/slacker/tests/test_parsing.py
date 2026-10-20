#!/usr/bin/env python3
"""What the capability decides with no network and no store: how a Slack
conversation and message are read, how the session pair is resolved, and what a
connection must carry. Every case here runs with no account and no engine."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

su = _cli.load()


class ConversationKind(unittest.TestCase):
    def test_each_flag_resolves_to_its_closed_kind(self):
        self.assertEqual(su._conv_kind({"is_im": True}), "im")
        self.assertEqual(su._conv_kind({"is_mpim": True}), "mpim")
        self.assertEqual(su._conv_kind({"is_private": True}), "private_channel")
        self.assertEqual(su._conv_kind({"is_channel": True}), "public_channel")

    def test_a_dm_without_a_name_is_labelled_by_its_partner(self):
        self.assertEqual(su._conv_name({"is_im": True, "user": "U9"}), "@U9")

    def test_a_channel_keeps_its_name(self):
        self.assertEqual(su._conv_name({"name": "general"}), "general")


class Timestamps(unittest.TestCase):
    def test_a_slack_ts_is_read_as_float_seconds(self):
        self.assertEqual(su._ts_float("1728999999.000100"), 1728999999.0001)

    def test_an_absent_or_unparsable_ts_is_none(self):
        self.assertIsNone(su._ts_float(None))
        self.assertIsNone(su._ts_float("later"))

    def test_an_empty_channel_id_is_refused(self):
        with self.assertRaises(su._Refusal) as caught:
            su._normalize_channel_id("")
        self.assertEqual(caught.exception.exit_code, 6)


class SessionCookie(unittest.TestCase):
    """The person may paste the `d` cookie as a bare value, a d= pair, or a
    whole Cookie header; the value alone is what the client re-wraps."""

    def test_a_bare_value_is_kept(self):
        self.assertEqual(su._resolve_cookie("xoxd-abc+def"), "xoxd-abc+def")

    def test_a_pair_is_unwrapped(self):
        self.assertEqual(su._resolve_cookie("d=xoxd-abc"), "xoxd-abc")

    def test_a_header_is_unwrapped(self):
        self.assertEqual(su._resolve_cookie("d=xoxd-abc; d-s=1700"), "xoxd-abc")

    def test_absent_is_none(self):
        self.assertIsNone(su._resolve_cookie(None))
        self.assertIsNone(su._resolve_cookie(""))


class ConnectionConfig(unittest.TestCase):
    """A connection materializes only with both session secrets, and only when
    the token is a web-session token."""

    def setUp(self):
        self._token = "xoxc-123"
        self._cookie = "xoxd-abc"
        su._resolve_env_key = self._fake_resolve  # type: ignore

    def _fake_resolve(self, key):
        store = {"SLACKER_TOKEN": self._token,
                 "SLACKER_COOKIE": self._cookie}
        return store.get(key), ("env" if store.get(key) else None), None

    def test_a_full_connection_resolves(self):
        cfg, problem = su._build_cfg("mine", {"team_id": "T1", "expect_user": "U1"})
        self.assertIsNone(problem)
        self.assertEqual(cfg["token"], "xoxc-123")
        self.assertEqual(cfg["cookie"], "xoxd-abc")
        self.assertEqual(cfg["account_binding"], "T1")
        self.assertFalse(cfg["allow_write"])

    def test_a_missing_token_is_a_credential_fault(self):
        self._token = None
        _cfg, problem = su._build_cfg("mine", {})
        self.assertEqual(problem["exit"], 2)
        self.assertEqual(problem["code"], "missing_secret")

    def test_a_missing_cookie_is_a_credential_fault(self):
        self._cookie = None
        _cfg, problem = su._build_cfg("mine", {})
        self.assertEqual(problem["exit"], 2)

    def test_quote_marks_around_a_pasted_secret_are_stripped(self):
        # A browser console prints a string quoted; the pair is pasted as shown.
        self._token = '"xoxc-123"'
        self._cookie = "'xoxd-abc+def'"
        cfg, problem = su._build_cfg("mine", {})
        self.assertIsNone(problem)
        self.assertEqual(cfg["token"], "xoxc-123")
        self.assertEqual(cfg["cookie"], "xoxd-abc+def")

    def test_a_bot_token_is_refused_as_the_wrong_route(self):
        self._token = "xoxb-999"
        _cfg, problem = su._build_cfg("mine", {})
        self.assertEqual(problem["exit"], 6)
        self.assertEqual(problem["code"], "not_a_session_token")


class _Listing(su.SlackClient):
    """users.conversations as Slack answers it: the caller's own conversations,
    with no is_member on a channel."""

    def __init__(self):
        pass

    def _call(self, method: str, **kwargs) -> dict:
        assert method == "users.conversations", method
        return {"ok": True, "channels": [
            {"id": "C1", "is_channel": True, "name": "general"},
            {"id": "G1", "is_group": True, "is_private": True, "name": "team"}]}


class MemberConversations(unittest.TestCase):
    def test_a_channel_users_conversations_returns_is_a_member_channel(self):
        convs = list(_Listing().conversations("public_channel,private_channel"))
        self.assertEqual([c["is_member"] for c in convs], [True, True])


class _Searching(su.SlackClient):
    """search.messages as Slack answers it, over pages of `per` matches; a
    reply carries its thread only in its permalink."""

    def __init__(self, matches, per=2):
        self.matches, self.per, self.pages_read = matches, per, 0

    def _call(self, method: str, **kwargs) -> dict:
        assert method == "search.messages", method
        self.pages_read += 1
        page = kwargs["page"]
        pages = max(1, -(-len(self.matches) // self.per))
        return {"ok": True, "messages": {
            "total": len(self.matches),
            "matches": self.matches[(page - 1) * self.per:page * self.per],
            "paging": {"page": page, "pages": pages}}}


class MessageSearch(unittest.TestCase):
    def test_a_reply_match_names_its_conversation_and_its_thread(self):
        client = _Searching([{
            "ts": "1700000002.000200", "user": "U1", "text": "the rollout plan",
            "channel": {"id": "C1", "name": "general", "is_channel": True},
            "permalink": "https://x.slack.com/archives/C1/p1700000002000200"
                         "?thread_ts=1700000001.000100&cid=C1"}])
        hits = [su._search_hit(m) for m in client.search("rollout")]
        self.assertEqual(
            {k: hits[0][k] for k in ("channel", "kind", "ts", "thread_ts")},
            {"channel": "C1", "kind": "public_channel",
             "ts": "1700000002.000200", "thread_ts": "1700000001.000100"})
        self.assertEqual(hits[0]["ts_at"], "2023-11-14T22:13:22.000200+00:00")
        self.assertEqual(client.search_total, 1)

    def test_matches_are_read_across_pages_up_to_the_limit(self):
        matches = [{"ts": f"170000000{i}.0", "channel": {"id": "C1"}}
                   for i in range(5)]
        client = _Searching(matches)
        self.assertEqual(len(list(client.search("x", limit=3))), 3)
        self.assertEqual(client.pages_read, 2)
        self.assertEqual(len(list(_Searching(matches).search("x", limit=50))), 5)

    def test_no_match_is_an_empty_answer(self):
        client = _Searching([])
        self.assertEqual(list(client.search("nothing")), [])
        self.assertEqual(client.search_total, 0)


class MessageValues(unittest.TestCase):
    def test_a_message_becomes_a_row_dict(self):
        v = su._message_values("C1", {
            "type": "message", "ts": "100.000100", "user": "U1",
            "text": "hi", "thread_ts": "90.0",
            "reactions": [{"name": "eyes", "users": ["U2", "U3"]}],
            "edited": {"ts": "101.0", "user": "U1"}, "reply_count": 2}, "live")
        self.assertEqual(v["ts"], "100.000100")
        self.assertEqual(v["user_id"], "U1")
        self.assertEqual(v["thread_ts"], "90.0")
        self.assertEqual(v["reactions"], {"eyes": ["U2", "U3"]})
        self.assertEqual(v["reply_count"], 2)
        self.assertIsNotNone(v["edited_f"])

    def test_a_message_without_a_ts_is_not_a_row(self):
        self.assertIsNone(su._message_values("C1", {"type": "message"}, "live"))

    def test_a_bot_message_is_attributed_to_its_bot_id(self):
        v = su._message_values("C1", {"ts": "1.0", "bot_id": "B9",
                                      "subtype": "bot_message"}, "live")
        self.assertEqual(v["user_id"], "B9")


if __name__ == "__main__":
    unittest.main()
