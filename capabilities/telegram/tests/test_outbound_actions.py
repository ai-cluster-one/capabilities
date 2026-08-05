#!/usr/bin/env python3
"""Focused coverage for outgoing Telegram media and reactions."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


TELEGRAM_DIR = Path(__file__).resolve().parents[1]
CLI_PATH = TELEGRAM_DIR / "bin" / "telegram"
WORKER_SHIM_PATH = TELEGRAM_DIR / "service" / "worker-bin" / "telegram"


class _Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _ReactionEmoji:
    def __init__(self, *, emoticon):
        self.emoticon = emoticon


class _Error(Exception):
    pass


def import_cli():
    telethon = types.ModuleType("telethon")
    telethon.TelegramClient = object

    errors = types.ModuleType("telethon.errors")
    errors.FloodWaitError = _Error
    errors.RPCError = _Error
    errors.SessionPasswordNeededError = _Error

    rpc_errors = types.ModuleType("telethon.errors.rpcerrorlist")
    rpc_errors.ApiIdInvalidError = _Error
    rpc_errors.AuthKeyUnregisteredError = _Error
    rpc_errors.UsernameInvalidError = _Error
    rpc_errors.UsernameNotOccupiedError = _Error

    tl = types.ModuleType("telethon.tl")
    functions = types.ModuleType("telethon.tl.functions")
    functions.messages = types.SimpleNamespace(SendReactionRequest=_Request)
    tl_types = types.ModuleType("telethon.tl.types")
    for name in ("Channel", "Chat", "MessageEmpty", "User"):
        setattr(tl_types, name, type(name, (), {}))
    tl_types.ReactionEmoji = _ReactionEmoji
    tl.functions = functions
    tl.types = tl_types

    modules = {
        "telethon": telethon,
        "telethon.errors": errors,
        "telethon.errors.rpcerrorlist": rpc_errors,
        "telethon.tl": tl,
        "telethon.tl.functions": functions,
        "telethon.tl.types": tl_types,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        name = f"telegram_outbound_test_{time.time_ns()}"
        spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, str(CLI_PATH)))
        if spec is None or spec.loader is None:
            raise AssertionError("cannot import telegram CLI")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def import_worker_shim():
    name = f"telegram_worker_shim_test_{time.time_ns()}"
    spec = importlib.util.spec_from_loader(
        name, SourceFileLoader(name, str(WORKER_SHIM_PATH)))
    if spec is None or spec.loader is None:
        raise AssertionError("cannot import Telegram worker shim")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Client:
    def __init__(self):
        self.files = []
        self.requests = []
        self.disconnected = False

    async def send_file(self, entity, path, **kwargs):
        self.files.append((entity, path, kwargs))
        return types.SimpleNamespace(id=812)

    async def __call__(self, request):
        self.requests.append(request)

    async def disconnect(self):
        self.disconnected = True


class OutboundActionsTests(unittest.TestCase):
    def setUp(self):
        self.cli = import_cli()
        self.client = _Client()

        async def authorize(_client):
            return None

        async def resolve(_client, _chat):
            return "chat-entity"

        self.cli.make_client = lambda _cfg: self.client
        self.cli._require_auth = authorize
        self.cli.resolve_chat = resolve
        self.cli.entity_label = lambda entity: str(entity)

    def test_send_media_keeps_caption_reply_and_document_choice(self):
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "report.pdf"
            media.write_bytes(b"pdf")
            result = asyncio.run(self.cli.cmd_send_media(
                {"id": "test"}, "-1001", str(media), "Here", 71, True))

        self.assertEqual(result["sent_id"], 812)
        self.assertEqual(result["to"], "chat-entity")
        entity, path, kwargs = self.client.files[0]
        self.assertEqual(entity, "chat-entity")
        self.assertEqual(path, str(media))
        self.assertEqual(kwargs, {
            "caption": "Here", "reply_to": 71, "force_document": True,
        })
        self.assertTrue(self.client.disconnected)

    def test_react_builds_one_reaction_for_each_requested_emoji(self):
        result = asyncio.run(self.cli.cmd_react(
            {"id": "test"}, "-1001", 99, ["👍", "🔥"]))

        self.assertEqual(result, {
            "reacted_to": 99,
            "to": "chat-entity",
            "reactions": ["👍", "🔥"],
        })
        request = self.client.requests[0]
        self.assertEqual(request.peer, "chat-entity")
        self.assertEqual(request.msg_id, 99)
        self.assertEqual([item.emoticon for item in request.reaction], ["👍", "🔥"])
        self.assertTrue(self.client.disconnected)

    def test_send_media_rejects_missing_file_before_connecting(self):
        with self.assertRaisesRegex(ValueError, "media file not found"):
            asyncio.run(self.cli.cmd_send_media(
                {"id": "test"}, "-1001", "/does/not/exist", None, None, False))
        self.assertEqual(self.client.files, [])

    def test_worker_scope_recognizes_new_outbound_chat_commands(self):
        shim = import_worker_shim()
        self.assertEqual(
            shim.parse_command_and_chat(
                ["telegram", "--connection", "main", "send-media", "-1001", "a.jpg"]),
            ("send-media", "-1001"),
        )
        self.assertEqual(
            shim.parse_command_and_chat(
                ["telegram", "react", "-1001", "99", "👍"]),
            ("react", "-1001"),
        )


if __name__ == "__main__":
    unittest.main()
