"""Regression tests for Telegram service-worker process boundaries."""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


TELEGRAM_CLI = Path(__file__).resolve().parents[1] / "bin" / "telegram"


def _placeholder(name):
    return type(name, (Exception,), {})


@contextlib.contextmanager
def load_cli():
    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = type("TelegramClient", (), {})
    errors = types.ModuleType("telethon.errors")
    errors.FloodWaitError = _placeholder("FloodWaitError")
    errors.RPCError = _placeholder("RPCError")
    errors.SessionPasswordNeededError = _placeholder("SessionPasswordNeededError")
    rpcerrors = types.ModuleType("telethon.errors.rpcerrorlist")
    for name in (
        "ApiIdInvalidError",
        "AuthKeyUnregisteredError",
        "UsernameInvalidError",
        "UsernameNotOccupiedError",
    ):
        setattr(rpcerrors, name, _placeholder(name))
    tl = types.ModuleType("telethon.tl")
    tl_types = types.ModuleType("telethon.tl.types")
    for name in ("Channel", "Chat", "MessageEmpty", "User"):
        setattr(tl_types, name, type(name, (), {}))

    names = {
        "telethon": telethon,
        "telethon.errors": errors,
        "telethon.errors.rpcerrorlist": rpcerrors,
        "telethon.tl": tl,
        "telethon.tl.types": tl_types,
    }
    previous = {name: sys.modules.get(name) for name in names}
    sys.modules.update(names)
    try:
        yield runpy.run_path(str(TELEGRAM_CLI))
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class WorkerCliGuardTests(unittest.TestCase):
    def test_send_uses_outbox_even_when_real_cli_wins_path_lookup(self):
        with tempfile.TemporaryDirectory() as raw_tmp, load_cli() as cli:
            tmp = Path(raw_tmp)
            outbox = tmp / "progress" / "job.jsonl"
            seen = {}

            def auth_gate(connection, session):
                seen["session"] = session
                return connection, session

            globals_ = cli["main"].__globals__
            globals_["_gate"] = lambda: None
            globals_["_auth_connection_gate"] = auth_gate
            globals_["_load_config"] = lambda *_: {
                "id": "main", "allow_write": True,
            }
            globals_["_write_gate"] = lambda *_: None
            globals_["cmd_send"] = mock.AsyncMock(
                side_effect=AssertionError("MTProto send must not run in a service worker"))

            env = {
                "TELEGRAM_PROGRESS_OUTBOX": str(outbox),
                "TELEGRAM_WORKER_SESSION": str(tmp / "worker-session"),
                "TELEGRAM_PROGRESS_REPLY_TO": "42",
                "TELEGRAM_PROGRESS_CHAT_TYPE": "group",
            }
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(sys, "argv", ["telegram", "send", "-1001", " hello "]), \
                    contextlib.redirect_stdout(stdout):
                cli["main"]()

            self.assertEqual(str(tmp / "worker-session"), seen["session"])
            self.assertFalse(globals_["cmd_send"].called)
            self.assertEqual(
                {"ok": True, "queued": "telegram-service-progress"},
                json.loads(stdout.getvalue()),
            )
            record = json.loads(outbox.read_text())
            self.assertEqual("-1001", record["chat"])
            self.assertEqual("hello", record["text"])
            self.assertEqual("42", record["reply_to"])
            self.assertEqual("group", record["chat_type"])

    def test_service_worker_cannot_stop_its_supervisor(self):
        with load_cli() as cli:
            stderr = io.StringIO()
            authority = json.dumps({"source": "telegram", "sender_role": "supervisor"})
            with mock.patch.dict(os.environ, {"CAPABILITIES_AUTH_CONTEXT": authority}, clear=False), \
                    mock.patch.object(os, "killpg") as killpg, \
                    contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                cli["_service_stop_result"](None, None, 0, False)

            self.assertEqual(4, raised.exception.code)
            self.assertFalse(killpg.called)
            self.assertEqual(
                "service_self_stop_refused",
                json.loads(stderr.getvalue())["error"]["code"],
            )

    def test_real_cli_keeps_current_chat_scope_when_wrapper_is_shadowed(self):
        with tempfile.TemporaryDirectory() as raw_tmp, load_cli() as cli:
            tmp = Path(raw_tmp)
            authority = json.dumps({
                "source": "telegram",
                "chat_id": "42",
                "allowed_capabilities": {
                    "telegram": {"scope": "current_chat"},
                },
            })
            env = {
                "CAPABILITIES_AUTH_CONTEXT": authority,
                "TELEGRAM_PROGRESS_OUTBOX": str(tmp / "progress.jsonl"),
            }
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), \
                    contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                cli["_queue_service_worker_send"]("99", "no")

            self.assertEqual(4, raised.exception.code)
            self.assertFalse((tmp / "progress.jsonl").exists())
            self.assertEqual(
                "chat_scope_denied",
                json.loads(stderr.getvalue())["error"]["code"],
            )

    def test_operator_can_still_stop_from_a_terminal(self):
        with load_cli() as cli, \
                mock.patch.dict(os.environ, {}, clear=True):
            globals_ = cli["_service_stop_result"].__globals__
            globals_["_service_project_root"] = lambda: Path("/tmp/project")
            globals_["_service_state_dir"] = lambda *_: (
                "main", {"pid": Path("/tmp/nonexistent-telegram-service.pid")})
            result = cli["_service_stop_result"](None, None, 0, False)
            self.assertFalse(result["stopped"])
            self.assertFalse(result["running"])


if __name__ == "__main__":
    unittest.main()
