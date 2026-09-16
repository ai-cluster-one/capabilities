#!/usr/bin/env python3
"""Tests for how callva builds a write body from the flags a command was given.

Run with: uv run --with httpx python3 capabilities/callva/tests/test_update_merge.py
(the callva bin declares httpx in its PEP-723 header, so bare python3 may not import it)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "callva", CAPABILITY / "callva")
    if path.is_file()), CAPABILITY / "bin" / "callva")


def _load_module():
    name = "callva_update_merge_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


callva = _load_module()


class _SentBody:
    """Captures the body the command hands to the API instead of sending it."""

    def __init__(self):
        self.method = None
        self.path = None
        self.body = None

    def __call__(self, method, path, body=None, query=None):
        self.method, self.path, self.body = method, path, body
        return {"data": {"ok": True}}


def _agent_args(**overrides):
    args = dict(
        id="AG", name=None, voice=None, prompt=None, prompt_file=None,
        greeting_type=None, greeting_exact=None, greeting_instruction=None,
        agent_speaks_first=None, config_json=None, payload=None)
    args.update(overrides)
    return types.SimpleNamespace(**args)


def _tool_args(**overrides):
    args = dict(
        agent_id="AG", key="fetchOpenSlots", config_json=None,
        config_file=None, type=None, is_enabled=None, position=None)
    args.update(overrides)
    return types.SimpleNamespace(**args)


def _automation_args(**overrides):
    args = dict(
        id="AU", name=None, description=None, is_active=None,
        settings_json=None, payload=None)
    args.update(overrides)
    return types.SimpleNamespace(**args)


class MergeUpdateTests(unittest.TestCase):
    def test_flag_wins_over_the_same_key_in_data(self):
        self.assertEqual(
            callva._merge_update({"name": "from-data"}, {"name": "from-flag"}),
            {"name": "from-flag"})

    def test_data_keys_no_flag_names_are_kept(self):
        self.assertEqual(
            callva._merge_update({"a": 1, "b": 2}, {"b": 3}),
            {"a": 1, "b": 3})

    def test_missing_data_leaves_the_flags_alone(self):
        self.assertEqual(callva._merge_update(None, {"a": 1}), {"a": 1})

    def test_the_callers_data_object_is_not_mutated(self):
        payload = {"name": "orig"}
        callva._merge_update(payload, {"name": "new"})
        self.assertEqual(payload, {"name": "orig"})


class AgentsUpdateBodyTests(unittest.TestCase):
    def _run(self, args):
        sent = _SentBody()
        with mock.patch.object(callva, "api", sent):
            callva.agents_update(args)
        return sent

    def test_typed_flags_survive_alongside_data(self):
        sent = self._run(_agent_args(
            payload={"config": {"x": 1}, "name": "from-data"},
            prompt="client prompt", greeting_exact="Hi there"))
        self.assertEqual(sent.method, "PATCH")
        self.assertEqual(sent.body, {
            "config": {"x": 1}, "name": "from-data",
            "prompt": "client prompt", "greeting_exact": "Hi there"})

    def test_config_flag_beats_a_config_key_in_data(self):
        sent = self._run(_agent_args(
            payload={"config": {"from": "data"}}, config_json={"from": "flag"}))
        self.assertEqual(sent.body, {"config": {"from": "flag"}})

    def test_empty_config_flag_is_still_written(self):
        sent = self._run(_agent_args(
            payload={"config": {"from": "data"}}, config_json={}))
        self.assertEqual(sent.body, {"config": {}})

    def test_flags_alone_still_build_the_body(self):
        sent = self._run(_agent_args(name="only-flag"))
        self.assertEqual(sent.body, {"name": "only-flag"})

    def test_data_alone_still_builds_the_body(self):
        sent = self._run(_agent_args(payload={"prompt": "only-data"}))
        self.assertEqual(sent.body, {"prompt": "only-data"})

    def test_no_fields_at_all_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self._run(_agent_args())
        self.assertEqual(caught.exception.code, 6)

    def test_empty_data_with_no_flags_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self._run(_agent_args(payload={}))
        self.assertEqual(caught.exception.code, 6)

    def test_voice_is_still_refused_before_the_request(self):
        with self.assertRaises(SystemExit) as caught:
            self._run(_agent_args(voice="alloy", payload={"name": "n"}))
        self.assertEqual(caught.exception.code, 6)

    def test_a_tools_key_in_config_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self._run(_agent_args(config_json={"tools": {"endCall": {}}}))
        self.assertEqual(caught.exception.code, 6)

    def test_a_tools_key_arriving_through_data_is_refused_too(self):
        with self.assertRaises(SystemExit) as caught:
            self._run(_agent_args(payload={"config": {"tools": {}}}))
        self.assertEqual(caught.exception.code, 6)

    def test_config_without_tools_still_goes_through(self):
        sent = self._run(_agent_args(config_json={"language": "et"}))
        self.assertEqual(sent.body, {"config": {"language": "et"}})


class ToolsBodyTests(unittest.TestCase):
    """A tool write carries the fields the command named, and no others."""

    def _run(self, fn, args):
        sent = _SentBody()
        with mock.patch.object(callva, "api", sent):
            fn(args)
        return sent

    def test_patch_sends_only_the_named_field(self):
        sent = self._run(callva.tools_patch, _tool_args(is_enabled=False))
        self.assertEqual(sent.method, "PATCH")
        self.assertEqual(sent.body, {"is_enabled": False})

    def test_patch_keeps_position_zero_because_it_was_named(self):
        sent = self._run(callva.tools_patch, _tool_args(position=0))
        self.assertEqual(sent.body, {"position": 0})

    def test_the_key_reaches_the_path_as_typed(self):
        sent = self._run(callva.tools_get, _tool_args())
        self.assertEqual(sent.path, "/external/agents/AG/tools/fetchOpenSlots")

    def test_set_states_the_whole_record(self):
        sent = self._run(callva.tools_set, _tool_args(
            config_json={"url": "https://example.test"}, type="http_request"))
        self.assertEqual(sent.method, "PUT")
        self.assertEqual(sent.body, {
            "type": "http_request", "config": {"url": "https://example.test"}})

    def test_set_sends_an_empty_body_when_that_is_what_was_given(self):
        sent = self._run(callva.tools_set, _tool_args(config_json={}))
        self.assertEqual(sent.body, {"config": {}})

    def test_set_without_a_body_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self._run(callva.tools_set, _tool_args(type="end_call"))
        self.assertEqual(caught.exception.code, 6)

    def test_patch_naming_nothing_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self._run(callva.tools_patch, _tool_args())
        self.assertEqual(caught.exception.code, 6)

    def test_delete_asks_for_the_one_tool(self):
        sent = self._run(callva.tools_delete, _tool_args())
        self.assertEqual(sent.method, "DELETE")
        self.assertEqual(sent.path, "/external/agents/AG/tools/fetchOpenSlots")


class AutomationsUpdateBodyTests(unittest.TestCase):
    def _run(self, args):
        sent = _SentBody()
        with mock.patch.object(callva, "api", sent):
            callva.automations_update(args)
        return sent

    def test_typed_flags_survive_alongside_data(self):
        sent = self._run(_automation_args(
            payload={"settings": {"a": 1}, "name": "from-data"}, is_active=False))
        self.assertEqual(sent.method, "PUT")
        self.assertEqual(sent.body, {
            "settings": {"a": 1}, "name": "from-data", "is_active": False})

    def test_settings_flag_beats_a_settings_key_in_data(self):
        sent = self._run(_automation_args(
            payload={"settings": {"from": "data"}}, settings_json={"from": "flag"}))
        self.assertEqual(sent.body, {"settings": {"from": "flag"}})

    def test_data_alone_still_builds_the_body(self):
        sent = self._run(_automation_args(payload={"description": "d"}))
        self.assertEqual(sent.body, {"description": "d"})

    def test_empty_data_with_no_flags_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self._run(_automation_args(payload={}))
        self.assertEqual(caught.exception.code, 6)


if __name__ == "__main__":
    unittest.main()
