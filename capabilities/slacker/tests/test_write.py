#!/usr/bin/env python3
"""The write verbs: each is refused with exit 4 on a connection that does not
grant writes, before Slack is reached, and what each writes lands in the store
as the live capture records the same act.

The gate cases need no network and no store. The store cases need a throwaway
Postgres named by SLACKER_TEST_DSN and skip without one."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli  # noqa: E402

su = _cli.load()

_ARGV = {
    "send": ["send", "D1", "hello"],
    "edit": ["edit", "D1", "1.0", "hello"],
    "delete": ["delete", "D1", "1.0"],
    "react": ["react", "D1", "1.0", "thumbsup"],
    "unreact": ["unreact", "D1", "1.0", "thumbsup"],
    "upload": ["upload", "D1", "/nonexistent"],
    "mark": ["mark", "D1"],
    "open": ["open", "U1"],
}


class WriteGate(unittest.TestCase):
    def test_every_write_verb_is_declared_and_parsed(self):
        self.assertEqual(set(_ARGV), su.WRITE_VERBS)
        for verb, argv in _ARGV.items():
            self.assertEqual(su._build_parser().parse_args(argv).command, verb)

    def _run(self, argv, allow_write):
        cfg = {"id": "c1", "allow_write": allow_write}
        err = io.StringIO()
        with mock.patch.object(su, "_gate", lambda: None), \
                mock.patch.object(su, "_contract", lambda argv: None), \
                mock.patch.object(su, "get_config", lambda args=None: cfg), \
                mock.patch.object(su, "SlackClient",
                                  side_effect=AssertionError("Slack reached")), \
                mock.patch.object(sys, "argv", ["slacker", *argv]), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                su.main()
        return caught.exception.code, err.getvalue()

    def test_a_read_only_connection_refuses_each_write_with_exit_4(self):
        for verb, argv in _ARGV.items():
            with self.subTest(verb=verb):
                code, err = self._run(argv, allow_write=False)
                self.assertEqual(code, 4)
                self.assertIn("read_only", err)

    def test_a_granted_connection_passes_the_gate(self):
        # Past the gate the verb reaches Slack, which the stub refuses loudly.
        with self.assertRaises(AssertionError):
            self._run(_ARGV["send"], allow_write=True)


class _Writes(su.SlackClient):
    """A session that answers the write methods as Slack does, with no socket."""

    def __init__(self):
        self.calls = []

    def _post(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "chat.postMessage":
            msg = {"type": "message", "user": "UME", "text": kwargs["text"],
                   "ts": "200.000100"}
            if kwargs.get("thread_ts"):
                msg["thread_ts"] = kwargs["thread_ts"]
            return {"ok": True, "channel": kwargs["channel"], "ts": msg["ts"],
                    "message": msg}
        if method == "chat.update":
            return {"ok": True, "channel": kwargs["channel"], "ts": kwargs["ts"],
                    "text": kwargs["text"],
                    "message": {"text": kwargs["text"], "user": "UME",
                                "edited": {"user": "UME", "ts": "300.000000"}}}
        return {"ok": True}

    def _call(self, method, **kwargs):
        assert method == "auth.test", method
        return {"user_id": "UME"}


@_cli.needs_store
class WritesLandInTheStore(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _cli.store_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = _cli.store_cfg(allow_write=True)
        self.db = su._open_store(self.cfg)
        self.addCleanup(self.db.close)
        self.client = _Writes()
        for name, value in (("get_config", lambda args=None: self.cfg),
                            ("_open_store", lambda cfg: _Unclosed(self.db)),
                            ("SlackClient", lambda cfg: self.client)):
            p = mock.patch.object(su, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _row(self, ts, channel="D1"):
        rows = [m for m in su._store_messages(self.db, channel, 100, None, None)
                if m["ts"] == ts]
        return rows[0] if rows else None

    def test_a_sent_message_is_a_live_row(self):
        out = su.cmd_send(argparse.Namespace(channel="D1", text="hello",
                                             thread=None, broadcast=False))
        self.assertTrue(out["stored"])
        self.assertEqual(self._row("200.000100")["text"], "hello")

    def test_a_reply_raises_its_parents_reply_count(self):
        su._upsert_message(self.db, "D1", {"type": "message", "ts": "100.000100",
                                           "user": "U2", "text": "parent"},
                           "live", authoritative=True)
        su.cmd_send(argparse.Namespace(channel="D1", text="reply",
                                       thread="100.000100", broadcast=False))
        self.assertEqual(self._row("200.000100")["thread_ts"], "100.000100")
        self.assertEqual(self._row("100.000100")["reply_count"], 1)

    def test_an_edit_moves_the_text_and_keeps_the_reactions(self):
        su._upsert_message(self.db, "D1", {"type": "message", "ts": "100.000100",
                                           "user": "UME", "text": "old",
                                           "reactions": [{"name": "eyes",
                                                          "users": ["U2"]}]},
                           "live", authoritative=True)
        su.cmd_edit(argparse.Namespace(channel="D1", ts="100.000100", text="new"))
        row = self._row("100.000100")
        self.assertEqual(row["text"], "new")
        self.assertTrue(row["edited"])
        self.assertEqual(row["reactions"], {"eyes": ["U2"]})

    def test_a_deletion_is_the_soft_flag(self):
        su._upsert_message(self.db, "D1", {"type": "message", "ts": "100.000100",
                                           "user": "UME", "text": "x"},
                           "live", authoritative=True)
        su.cmd_delete(argparse.Namespace(channel="D1", ts="100.000100"))
        self.assertIsNone(self._row("100.000100"))   # reads leave it out
        self.assertTrue(self.db.execute(
            "SELECT deleted FROM slacker_messages WHERE account = %s "
            "AND channel_id = 'D1' AND ts = '100.000100'",
            (self.db.account,)).fetchone()["deleted"])

    def test_a_reaction_is_the_accounts_own_and_is_removed_again(self):
        su._upsert_message(self.db, "D1", {"type": "message", "ts": "100.000100",
                                           "user": "U2", "text": "x"},
                           "live", authoritative=True)
        su.cmd_react(argparse.Namespace(command="react", channel="D1",
                                        ts="100.000100", emoji=":thumbsup:"))
        self.assertEqual(self._row("100.000100")["reactions"], {"thumbsup": ["UME"]})
        su.cmd_react(argparse.Namespace(command="unreact", channel="D1",
                                        ts="100.000100", emoji="thumbsup"))
        self.assertEqual(self._row("100.000100")["reactions"], {})


class _Unclosed:
    """The case's store, which a verb's own close leaves open."""

    def __init__(self, db):
        self._db = db

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._db, name)


if __name__ == "__main__":
    unittest.main()
