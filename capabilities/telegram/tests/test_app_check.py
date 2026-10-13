#!/usr/bin/env python3
"""The machine's Telegram app: what a connection naming none inherits, and
the verdict `telegram app-check` reads from Telegram's answer."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import CLI_PATH  # noqa: E402


class _RPCError(Exception):
    code = 400
    message = "BAD_REQUEST"


class _ApiIdInvalidError(_RPCError):
    pass


class _ApiIdPublishedFloodError(_RPCError):
    pass


class _FloodWaitError(_RPCError):
    code = 420
    message = "FLOOD"

    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


class _AuthRestartError(_RPCError):
    code = 500
    message = "AUTH_RESTART"


class _Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def import_cli():
    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = object

    errors = types.ModuleType("telethon.errors")
    errors.FloodWaitError = _FloodWaitError
    errors.RPCError = _RPCError
    errors.SessionPasswordNeededError = type("SessionPasswordNeededError", (_RPCError,), {})

    rpc_errors = types.ModuleType("telethon.errors.rpcerrorlist")
    rpc_errors.ApiIdInvalidError = _ApiIdInvalidError
    rpc_errors.ApiIdPublishedFloodError = _ApiIdPublishedFloodError
    rpc_errors.AuthKeyUnregisteredError = type("AuthKeyUnregisteredError", (_RPCError,), {})
    rpc_errors.UsernameInvalidError = type("UsernameInvalidError", (_RPCError,), {})
    rpc_errors.UsernameNotOccupiedError = type("UsernameNotOccupiedError", (_RPCError,), {})
    rpc_errors.rpc_errors_dict = {"API_ID_INVALID": _ApiIdInvalidError,
                                  "API_ID_PUBLISHED_FLOOD": _ApiIdPublishedFloodError}
    errors.rpcerrorlist = rpc_errors

    sessions = types.ModuleType("telethon.sessions")
    sessions.MemorySession = lambda: "memory"

    tl = types.ModuleType("telethon.tl")
    functions = types.ModuleType("telethon.tl.functions")
    functions.auth = types.SimpleNamespace(ExportLoginTokenRequest=_Request)
    tl_types = types.ModuleType("telethon.tl.types")
    for name in ("Channel", "Chat", "MessageEmpty", "User"):
        setattr(tl_types, name, type(name, (), {}))
    tl.functions = functions
    tl.types = tl_types

    modules = {
        "telethon": telethon,
        "telethon.errors": errors,
        "telethon.errors.rpcerrorlist": rpc_errors,
        "telethon.sessions": sessions,
        "telethon.tl": tl,
        "telethon.tl.functions": functions,
        "telethon.tl.types": tl_types,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    name = f"telegram_app_check_test_{time.time_ns()}"
    spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, str(CLI_PATH)))
    if spec is None or spec.loader is None:
        raise AssertionError("cannot import telegram CLI")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    # The probe imports from telethon when it runs, so the stubs stay in place
    # for the test and are taken out again afterwards.
    module._restore_modules = previous
    return module


def restore_modules(module) -> None:
    for name, value in module._restore_modules.items():
        if value is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = value


class _Client:
    """A client whose one request answers with what the test names."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.disconnected = False

    async def connect(self):
        return None

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def disconnect(self):
        self.disconnected = True


def probe(cli, answer) -> tuple[dict, _Client]:
    client = _Client(answer)
    with mock.patch.object(cli, "TelegramClient",
                           lambda session, api_id, api_hash: client):
        verdict = asyncio.run(cli._probe_app(1234567, "f" * 32))
    return verdict, client


class AppCheckVerdictTests(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        self.addCleanup(restore_modules, self.cli)

    def test_a_login_token_means_the_app_is_accepted(self):
        verdict, client = probe(self.cli, object())
        self.assertEqual(verdict, {"status": "accepted"})
        self.assertEqual(client.requests[0].api_id, 1234567)
        self.assertEqual(client.requests[0].except_ids, [])
        self.assertTrue(client.disconnected)

    def test_an_unknown_id_and_hash_are_invalid(self):
        verdict, _ = probe(self.cli, _ApiIdInvalidError("The api_id/api_hash combination is invalid"))
        self.assertEqual(verdict["status"], "invalid")
        self.assertEqual(verdict["error"]["name"], "API_ID_INVALID")
        self.assertEqual(verdict["error"]["code"], 400)

    def test_a_published_app_is_refused_as_blocked(self):
        verdict, _ = probe(self.cli, _ApiIdPublishedFloodError("This API id was published"))
        self.assertEqual((verdict["status"], verdict["reason"]), ("refused", "blocked"))
        self.assertEqual(verdict["error"]["name"], "API_ID_PUBLISHED_FLOOD")

    def test_a_flood_wait_is_refused_with_the_wait(self):
        verdict, _ = probe(self.cli, _FloodWaitError(42))
        self.assertEqual((verdict["status"], verdict["reason"]), ("refused", "flood_limited"))
        self.assertEqual(verdict["retry_after"], 42)
        self.assertEqual(verdict["error"]["name"], "FLOOD_WAIT_42")

    def test_any_other_refusal_carries_what_telegram_said(self):
        verdict, _ = probe(self.cli, _AuthRestartError("Restart the authorization process"))
        self.assertEqual((verdict["status"], verdict["reason"]), ("refused", "other"))
        self.assertEqual(verdict["error"]["name"], "AUTH_RESTART")


class MachineAppTests(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        self.addCleanup(restore_modules, self.cli)
        self.env = {"TELEGRAM_API_ID": "1234567", "TELEGRAM_API_HASH": "a" * 32,
                    "TELEGRAM_OWN_API_HASH": "b" * 32}
        patcher = mock.patch.object(
            self.cli, "_resolve_env_key",
            lambda key: (self.env.get(key), "user" if self.env.get(key) else None, None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_connection_naming_no_app_inherits_the_machines(self):
        api_id, api_hash, problem = self.cli._app_credentials("second", {"name": "Second"})
        self.assertIsNone(problem)
        self.assertEqual((api_id, api_hash), (1234567, "a" * 32))

    def test_a_connection_naming_its_own_app_keeps_it(self):
        entry = {"api_id": 7654321, "secret_env": "TELEGRAM_OWN_API_HASH"}
        api_id, api_hash, problem = self.cli._app_credentials("own", entry)
        self.assertIsNone(problem)
        self.assertEqual((api_id, api_hash), (7654321, "b" * 32))

    def test_no_machine_app_names_the_setup_write(self):
        self.env.clear()
        _, _, problem = self.cli._app_credentials("second", {})
        self.assertEqual((problem["exit"], problem["code"]), (2, "missing_credentials"))
        self.assertIn("TELEGRAM_API_ID", problem["message"])
        self.assertIn("telegram setup set", problem["hint"])

    def test_a_named_hash_that_does_not_resolve_is_still_refused(self):
        _, _, problem = self.cli._app_credentials("own", {"secret_env": "TELEGRAM_GONE_API_HASH"})
        self.assertEqual(problem["code"], "missing_secret")

    def test_the_machine_app_is_a_required_setup_prerequisite(self):
        setup = {field["key"]: field for field in self.cli.SETUP_FIELDS}
        self.assertTrue(setup["TELEGRAM_API_ID"]["required"])
        self.assertTrue(setup["TELEGRAM_API_HASH"]["required"])
        self.assertTrue(setup["TELEGRAM_API_HASH"]["secret"])
        conn = {field["key"]: field for field in self.cli.CONN_FIELDS}
        self.assertFalse(conn["api_id"]["required"])
        self.assertFalse(conn["secret_env"]["required"])


if __name__ == "__main__":
    unittest.main()
