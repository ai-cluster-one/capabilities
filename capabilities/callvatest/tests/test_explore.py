#!/usr/bin/env python3
"""Tests for `callvatest explore`: the driver loop, the budget, the stage progression, the refusals and the report shape, with no provider and no tenant.

Run with: uv run --with httpx python3 capabilities/callvatest/tests/test_explore.py
(the callvatest bin declares httpx in its PEP-723 header, so bare python3 may not import it)
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next(
    (path for path in (CAPABILITY / "bin" / "callvatest",
                       CAPABILITY / "callvatest")
     if path.is_file()),
    CAPABILITY / "bin" / "callvatest")
AGENT = "11111111-2222-4333-8444-555555555555"


def _load_module():
    name = "callvatest_explore_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


callvatest = _load_module()


def _setting(**overrides):
    """A complete explore setting; a test removes or bends what it is about."""
    setting = {
        "driver": "gpt-oss",
        "budget": {"turns": 6, "calls": 1},
        "personas": {"mumbler": "You trail off mid-sentence."},
        "explorations": {
            "books-a-slot": {
                "agent": AGENT, "language": "en",
                "intent": "Book a slot next week.",
                "stages": ["state the reason", "give a day", "say goodbye"],
            },
        },
    }
    setting.update(overrides)
    return setting


def _args(**overrides):
    args = dict(exploration="books-a-slot", agent=None, persona=None, driver=None,
                report=None, json=True, text=True, timeout=1.0, budget=300.0,
                with_results=False)
    args.update(overrides)
    return types.SimpleNamespace(**args)


class _FakeRoom:
    """A joined room whose agent is scripted per (call, turn): it replies, hangs up, or never finishes its turn."""

    behaviour = staticmethod(lambda call_no, turn: ("reply", f"agent line {turn}"))
    calls_placed = 0

    def __init__(self, conn_details, obs, turn_timeout):
        _FakeRoom.calls_placed += 1
        self.call_no = _FakeRoom.calls_placed
        self.obs = obs
        self.closed = {}
        self.turn_in_call = 0

    def raise_fault(self):
        pass

    async def join(self):
        self.obs.agent_identity = "agent-x"
        self.obs.add("agent_joined", identity="agent-x")
        self.obs.add("agent_text", text="Hello, how can I help?", identity="agent-x")

    async def send(self, said, turn=None):
        self.obs.add("user_text", text=said, identity="me", turn=turn)
        action = self.behaviour(self.call_no, self.turn_in_call)
        self.turn_in_call += 1
        if action[0] == "hangup_on_send":
            self.closed["reason"] = "agent hung up"
            return None
        self.pending = action
        return time.time()

    async def await_turn(self, since, turn=None, timeout=None):
        # A real reply lands well after the line that provoked it; the fake
        # must not answer inside the same millisecond the line was sent.
        await asyncio.sleep(0.002)
        kind = self.pending[0]
        if kind == "reply":
            self.obs.add("agent_text", text=self.pending[1], identity="agent-x")
            self.obs.add("turn_complete", signal="state", turn=turn)
        elif kind == "hangup":
            self.closed["reason"] = "agent hung up"
            self.obs.add("turn_cut_short", turn=turn, reason="agent hung up")
        elif kind == "silent":
            self.obs.add("turn_timeout", turn=turn, waited=1.0, took_up=False)
            callvatest._die(7, "turn_timeout", "the agent did not finish its turn within 1s")

    async def close(self):
        self.obs.add("disconnected")


class _FakeDriver:
    """A driver that answers from a script of moves, one per call it receives; it records every message list it was shown."""

    def __init__(self, moves):
        self.moves = list(moves)
        self.seen = []

    def __call__(self, cfg, system, messages, max_tokens, role="judge"):
        self.seen.append((system, [dict(m) for m in messages], max_tokens, role))
        move = self.moves.pop(0) if self.moves else {"say": "hello?", "stages_done": 0}
        return move if isinstance(move, str) else json.dumps(move)


def _evidence(room_name, agent_id, obs, settle, wait_for_fields=None):
    number = room_name.rsplit("-", 1)[-1]
    return {"call_id": f"call-{number}", "call": {"status": "complete", "duration": 9,
                                                  "custom": "kept out"},
            "transcript": None, "settled": True,
            "marked": {"field": "test_call", "ok": True},
            "runs": [{"id": "r1", "automation_id": "a1", "name": "Lookup",
                      "status": "ok", "duration_ms": 5, "started_at": None,
                      "started_rel": None, "args": {"q": 1}, "result": {"slot": "x"}}]}


class ExploreCase(unittest.TestCase):
    def setUp(self):
        _FakeRoom.calls_placed = 0
        _FakeRoom.behaviour = staticmethod(lambda call_no, turn: ("reply", f"agent line {turn}"))
        self.setting = _setting()
        self.driver = _FakeDriver([])
        self.connects = []

        def _connect(agent_id, text_only=False):
            self.connects.append((agent_id, text_only))
            return {"room_name": f"room-{len(self.connects)}", "server_url": "",
                    "participant_token": ""}

        def _select_judge(wanted):
            if wanted != "gpt-oss":
                raise callvatest.JudgeError(f"no judge named {wanted!r}; known: gpt-oss")
            return {"id": "gpt-oss", "provider": "cerebras", "model": "gpt-oss-120b",
                    "key_env": "CEREBRAS_API_KEY", "api_key": "k", "reasoning_effort": None,
                    "url": "https://api.cerebras.ai/v1/chat/completions"}

        self.patches = [
            mock.patch.object(callvatest, "_setting_declared",
                              lambda key: (self.setting, "project", "<envelope>/callvatest/service/settings.json")),
            mock.patch.object(callvatest, "_select_judge", _select_judge),
            mock.patch.object(callvatest, "_resolve_agent", lambda ref: ref if ref else AGENT),
            mock.patch.object(callvatest, "platform_connect", _connect),
            mock.patch.object(callvatest, "gather_evidence", _evidence),
            mock.patch.object(callvatest, "_CallRoom", _FakeRoom),
            mock.patch.object(callvatest, "_model_call", lambda *a, **k: self.driver(*a, **k)),
            mock.patch.dict(callvatest._STATE, {"conn": {"id": "lab", "allow_write": True,
                                                         "test_call_field": None}}),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in self.patches])

    def explore(self, **overrides):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as stop:
                callvatest.cmd_explore(_args(**overrides))
        code = stop.exception.code if isinstance(stop.exception.code, int) else 1
        report = json.loads(out.getvalue()) if out.getvalue().strip() else None
        error = None
        for line in reversed(err.getvalue().splitlines()):
            try:
                error = json.loads(line)["error"]
                break
            except (ValueError, KeyError, TypeError):
                continue
        return code, report, error

    def assertRefused(self, code_wanted, fragment, **overrides):
        code, report, error = self.explore(**overrides)
        self.assertEqual(code, 6)
        self.assertIsNone(report)
        self.assertEqual(error["code"], code_wanted)
        self.assertIn(fragment, error["message"] + " " + error.get("hint", ""))
        self.assertEqual(self.connects, [], "a refusal must come before any call is placed")


class Refusals(ExploreCase):
    def test_no_setting_is_a_named_refusal_naming_the_home(self):
        self.setting = None
        self.assertRefused("no_explore_setting", "<envelope>/callvatest/service/settings.json")

    def test_setting_that_is_not_an_object(self):
        self.setting = "gpt-oss"
        self.assertRefused("bad_explore_setting", "not an object")

    def test_no_driver_never_falls_back_to_the_judges_default(self):
        del self.setting["driver"]
        self.assertRefused("no_driver", 'names no "driver"')

    def test_driver_flag_overrides_the_setting(self):
        del self.setting["driver"]
        self.driver.moves = [{"say": "bye", "stages_done": 3, "done": True}]
        code, report, _ = self.explore(driver="gpt-oss")
        self.assertEqual(code, 0)
        self.assertEqual(report["driver"]["id"], "gpt-oss")

    def test_unknown_driver(self):
        self.setting["driver"] = "nobody"
        self.assertRefused("bad_driver", "no judge named 'nobody'")

    def test_budget_turns_required(self):
        self.setting["budget"] = {"calls": 1}
        self.assertRefused("no_budget", '"turns"')

    def test_budget_calls_required(self):
        self.setting["budget"] = {"turns": 4}
        self.assertRefused("no_budget", '"calls"')

    def test_budget_must_be_a_positive_integer(self):
        self.setting["budget"] = {"turns": 0, "calls": 1}
        self.assertRefused("no_budget", '"turns"')
        self.setting["budget"] = {"turns": True, "calls": 1}
        self.assertRefused("no_budget", '"turns"')

    def test_no_explorations(self):
        self.setting["explorations"] = {}
        self.assertRefused("no_explorations", '"explorations"')

    def test_unknown_exploration_names_the_known_ones(self):
        self.assertRefused("unknown_exploration", "known: books-a-slot", exploration="other")

    def test_exploration_needs_intent_and_stages(self):
        self.setting["explorations"]["books-a-slot"]["intent"] = "  "
        self.assertRefused("bad_exploration", "`intent`")
        self.setting = _setting()
        self.setting["explorations"]["books-a-slot"]["stages"] = []
        self.assertRefused("bad_exploration", "`stages`")
        self.setting = _setting()
        self.setting["explorations"]["books-a-slot"]["stages"] = ["ok", 3]
        self.assertRefused("bad_exploration", "stage 1")

    def test_unknown_persona_from_flag(self):
        self.assertRefused("unknown_persona", "known: mumbler", persona="shouter")

    def test_unknown_persona_declared_on_the_exploration(self):
        self.setting["explorations"]["books-a-slot"]["persona"] = "shouter"
        self.assertRefused("unknown_persona", "'shouter'")

    def test_explore_is_a_write_verb(self):
        self.assertIn("explore", callvatest.WRITE_VERBS)


class DriverLoop(ExploreCase):
    def test_every_stage_reached_is_explored(self):
        self.driver.moves = [
            {"say": "I want to book.", "stages_done": 1, "done": False, "note": None},
            {"say": "Tuesday.", "stages_done": 2, "done": False, "note": "asked twice"},
            {"say": "Bye.", "stages_done": 3, "done": True, "note": None},
        ]
        code, report, _ = self.explore()
        self.assertEqual(code, 0)
        self.assertEqual(report["kind"], "exploration")
        self.assertEqual(report["verdict"], "explored")
        self.assertEqual(report["stopped_by"], "stages")
        self.assertEqual([s["reached"] for s in report["stages"]],
                         [{"call": 1, "turn": 0}, {"call": 1, "turn": 1}, {"call": 1, "turn": 2}])
        self.assertEqual(report["spent"], {"turns": 3, "calls": 1})
        self.assertEqual(report["budget"], {"turns": 6, "calls": 1, "seconds_per_call": 300.0})
        self.assertEqual(report["observations"], [{"call": 1, "turn": 1, "note": "asked twice"}])
        self.assertEqual(report["exploration"], "books-a-slot")
        self.assertEqual(report["intent"], "Book a slot next week.")
        self.assertIsNone(report["persona"])
        self.assertEqual(report["driver"], {"id": "gpt-oss", "provider": "cerebras",
                                            "model": "gpt-oss-120b"})
        # The driver saw the greeting, then each agent reply, as alternating turns.
        system, messages, max_tokens, role = self.driver.seen[-1]
        self.assertEqual(role, "driver")
        self.assertEqual(max_tokens, callvatest.DRIVER_MAX_TOKENS)
        self.assertIn("Book a slot next week.", system)
        self.assertIn("1. state the reason", system)
        self.assertEqual([m["role"] for m in messages],
                         ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(messages[0]["content"], "Hello, how can I help?")
        self.assertEqual(messages[2]["content"], "agent line 0")

    def test_report_is_not_a_gate(self):
        self.driver.moves = [{"say": "Bye.", "stages_done": 3, "done": True}]
        _, report, _ = self.explore()
        self.assertNotIn("checks", report)
        self.assertNotIn("scenario", report)
        self.assertNotIn(report["verdict"], ("pass", "fail", "observed"))
        self.assertEqual(set(callvatest._EXPLORE_VERDICTS.values()),
                         {"explored", "stalled", "dropped", "aborted"})

    def test_call_record_carries_the_run_evidence_without_checks(self):
        self.driver.moves = [{"say": "Bye.", "stages_done": 3, "done": True}]
        _, report, _ = self.explore()
        call = report["calls"][0]
        self.assertEqual(call["call"], 1)
        self.assertEqual(call["call_id"], "call-1")
        self.assertEqual(call["ended_by"], "caller")
        self.assertEqual(call["turns"], 1)
        self.assertEqual(call["post_call"], {"status": "complete", "duration": 9})
        self.assertEqual(call["tool_runs"][0]["name"], "Lookup")
        self.assertEqual(call["tool_runs"][0]["result_shape"], ["slot"])
        self.assertNotIn("result", call["tool_runs"][0])
        self.assertEqual([t["role"] for t in call["transcript"]], ["agent", "user", "agent"])
        self.assertTrue(any(e["kind"] == "driver_move" for e in call["events"]))

    def test_with_results_includes_them(self):
        self.driver.moves = [{"say": "Bye.", "stages_done": 3, "done": True}]
        _, report, _ = self.explore(with_results=True)
        self.assertEqual(report["calls"][0]["tool_runs"][0]["result"], {"slot": "x"})

    def test_persona_is_layered_into_the_brief(self):
        self.driver.moves = [{"say": "Bye.", "stages_done": 3, "done": True}]
        _, report, _ = self.explore(persona="mumbler")
        self.assertEqual(report["persona"], "mumbler")
        self.assertIn("You trail off mid-sentence.", self.driver.seen[0][0])

    def test_exploration_persona_is_the_default_and_the_flag_overrides(self):
        self.setting["personas"]["insister"] = "You push back."
        self.setting["explorations"]["books-a-slot"]["persona"] = "mumbler"
        self.driver.moves = [{"say": "Bye.", "stages_done": 3, "done": True}]
        _, report, _ = self.explore()
        self.assertEqual(report["persona"], "mumbler")
        self.driver.moves = [{"say": "Bye.", "stages_done": 3, "done": True}]
        _, report, _ = self.explore(persona="insister")
        self.assertEqual(report["persona"], "insister")
        self.assertIn("You push back.", self.driver.seen[-1][0])

    def test_turn_budget_stops_the_run_as_stalled(self):
        self.setting["budget"] = {"turns": 3, "calls": 1}
        self.driver.moves = [{"say": f"line {i}", "stages_done": 1, "done": False}
                             for i in range(10)]
        code, report, _ = self.explore()
        self.assertEqual(code, 0)
        self.assertEqual(report["verdict"], "stalled")
        self.assertEqual(report["stopped_by"], "turns")
        self.assertEqual(report["spent"], {"turns": 3, "calls": 1})
        self.assertEqual(report["calls"][0]["ended_by"], "turns")
        self.assertEqual(len(self.driver.seen), 3)
        self.assertEqual(report["stages"][0]["reached"], {"call": 1, "turn": 0})
        self.assertIsNone(report["stages"][1]["reached"])

    def test_stages_finished_on_the_last_allowed_turn_is_explored(self):
        self.setting["budget"] = {"turns": 2, "calls": 1}
        self.driver.moves = [{"say": "a", "stages_done": 2, "done": False},
                             {"say": "b", "stages_done": 3, "done": False}]
        _, report, _ = self.explore()
        self.assertEqual(report["verdict"], "explored")
        self.assertEqual(report["calls"][0]["ended_by"], "stages")

    def test_caller_giving_up_is_dropped(self):
        self.driver.moves = [{"say": "Never mind.", "stages_done": 1, "done": True}]
        code, report, _ = self.explore()
        self.assertEqual(code, 0)
        self.assertEqual(report["verdict"], "dropped")
        self.assertEqual(report["stopped_by"], "caller")

    def test_hanging_up_without_a_line_is_dropped_and_sends_nothing(self):
        self.driver.moves = [{"say": "", "stages_done": 0, "done": False}]
        _, report, _ = self.explore()
        self.assertEqual(report["verdict"], "dropped")
        self.assertEqual(report["spent"]["turns"], 0)
        self.assertEqual([t["role"] for t in report["calls"][0]["transcript"]], ["agent"])

    def test_driver_that_cannot_answer_aborts_with_exit_5(self):
        self.driver.moves = ["I refuse to play along"]
        code, report, _ = self.explore()
        self.assertEqual(code, 5)
        self.assertEqual(report["verdict"], "aborted")
        self.assertEqual(report["stopped_by"], "driver")
        self.assertIn("without a caller line", report["driver_error"])
        self.assertEqual(report["spent"], {"turns": 0, "calls": 1})

    def test_agent_hanging_up_redials_within_the_calls_budget(self):
        self.setting["budget"] = {"turns": 6, "calls": 2}
        _FakeRoom.behaviour = staticmethod(
            lambda call_no, turn: ("hangup",) if call_no == 1 else ("reply", "back again"))
        self.driver.moves = [
            {"say": "Hello, I want to book.", "stages_done": 1, "done": False},
            {"say": "It cut off; I want to book.", "stages_done": 1, "done": False},
            {"say": "Tuesday.", "stages_done": 2, "done": False},
            {"say": "Bye.", "stages_done": 3, "done": True},
        ]
        _, report, _ = self.explore()
        self.assertEqual(report["verdict"], "explored")
        self.assertEqual(report["spent"], {"turns": 4, "calls": 2})
        self.assertEqual([c["ended_by"] for c in report["calls"]], ["agent", "caller"])
        self.assertEqual([c["turns"] for c in report["calls"]], [1, 3])
        self.assertEqual(report["stages"][1]["reached"], {"call": 2, "turn": 2})
        self.assertEqual(len(self.connects), 2)
        # The second call's first message tells the driver it is dialling again,
        # and the conversation from the first call is still in front of it.
        messages = self.driver.seen[1][1]
        self.assertEqual(len(messages), 3)
        self.assertIn("calling again; this is call 2", messages[2]["content"])
        self.assertIn("the agent hung up", messages[2]["content"])
        self.assertIn("Hello, how can I help?", messages[2]["content"])

    def test_calls_budget_spent_is_stalled(self):
        self.setting["budget"] = {"turns": 6, "calls": 1}
        _FakeRoom.behaviour = staticmethod(lambda call_no, turn: ("hangup",))
        self.driver.moves = [{"say": "Hello.", "stages_done": 1, "done": False}]
        _, report, _ = self.explore()
        self.assertEqual(report["verdict"], "stalled")
        self.assertEqual(report["stopped_by"], "calls")
        self.assertEqual(len(self.connects), 1)

    def test_agent_that_never_finishes_a_turn_ends_the_call_not_the_run(self):
        self.setting["budget"] = {"turns": 6, "calls": 2}
        _FakeRoom.behaviour = staticmethod(
            lambda call_no, turn: ("silent",) if call_no == 1 else ("reply", "hi"))
        self.driver.moves = [
            {"say": "Hello.", "stages_done": 1, "done": False},
            {"say": "Bye.", "stages_done": 3, "done": True},
        ]
        code, report, _ = self.explore()
        self.assertEqual(code, 0)
        self.assertEqual(report["calls"][0]["ended_by"], "agent_silent")
        self.assertTrue(any(e["kind"] == "turn_timeout" for e in report["calls"][0]["events"]))
        self.assertEqual(report["verdict"], "explored")

    def test_hangup_on_send_is_the_agent_ending_the_call(self):
        _FakeRoom.behaviour = staticmethod(lambda call_no, turn: ("hangup_on_send",))
        self.driver.moves = [{"say": "Hello.", "stages_done": 1, "done": False}]
        _, report, _ = self.explore()
        self.assertEqual(report["calls"][0]["ended_by"], "agent")
        self.assertEqual(report["verdict"], "stalled")

    def test_prose_output_names_the_verdict_and_never_pass_or_fail(self):
        self.driver.moves = [{"say": "Bye.", "stages_done": 3, "done": True,
                              "note": "read the date back wrong"}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            callvatest.cmd_explore(_args(json=False))
        text = out.getvalue()
        self.assertTrue(text.startswith("EXPLORED books-a-slot  agent=11111111  mode=text  calls=1/1  turns=1/6\n"), text)
        self.assertIn("stages: 3/3 reached", text)
        self.assertIn("stopped by: stages", text)
        self.assertIn("call 1: ended by caller, 1 turns, call=call-1", text)
        self.assertIn("note  call 1 turn 0: read the date back wrong", text)
        self.assertNotIn("PASS", text)
        self.assertNotIn("FAIL", text)

    def test_report_file_is_written(self):
        import tempfile
        self.driver.moves = [{"say": "Bye.", "stages_done": 3, "done": True}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out" / "report.json"
            _, report, _ = self.explore(report=str(path))
            self.assertEqual(json.loads(path.read_text())["verdict"], report["verdict"])


class _Response:
    status_code = 200
    headers: dict = {}
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Transport(unittest.TestCase):
    """The driver rides the judge transport table: same providers, same request shaping, and the judge's own request is unchanged."""

    def _capture(self, payload):
        sent = {}

        def _post(url, json=None, headers=None, timeout=None):
            sent.update({"url": url, "body": json, "headers": headers, "timeout": timeout})
            return _Response(payload)
        return sent, _post

    def test_driver_carries_alternating_turns_on_an_openai_shaped_provider(self):
        cfg = {"id": "gpt-oss", "provider": "cerebras", "model": "gpt-oss-120b",
               "api_key": "k", "reasoning_effort": "low",
               "url": "https://api.cerebras.ai/v1/chat/completions"}
        sent, post = self._capture({"choices": [{"message": {"content": '{"say": "hi"}'}}]})
        turns = [{"role": "user", "content": "greeting"}, {"role": "assistant", "content": "{}"},
                 {"role": "user", "content": "reply"}]
        with mock.patch.object(callvatest.httpx, "post", post):
            text = callvatest._model_call(cfg, "BRIEF", turns, callvatest.DRIVER_MAX_TOKENS,
                                          role="driver")
        self.assertEqual(text, '{"say": "hi"}')
        self.assertEqual(sent["body"]["messages"],
                         [{"role": "system", "content": "BRIEF"}, *turns])
        self.assertEqual(sent["body"]["max_tokens"], callvatest.DRIVER_MAX_TOKENS)
        self.assertEqual(sent["body"]["model"], "gpt-oss-120b")
        self.assertEqual(sent["body"]["reasoning_effort"], "low")
        self.assertEqual(sent["body"]["temperature"],
                         callvatest.JUDGE_PROVIDERS["cerebras"]["temperature"])
        self.assertEqual(sent["headers"]["Authorization"], "Bearer k")

    def test_driver_on_anthropic_uses_the_system_field(self):
        cfg = {"id": "haiku", "provider": "anthropic", "model": "m", "api_key": "k",
               "reasoning_effort": None, "url": "https://api.anthropic.com/v1/messages"}
        sent, post = self._capture({"content": [{"type": "text", "text": "{}"}]})
        turns = [{"role": "user", "content": "greeting"}]
        with mock.patch.object(callvatest.httpx, "post", post):
            callvatest._model_call(cfg, "BRIEF", turns, 99, role="driver")
        self.assertEqual(sent["body"]["system"], "BRIEF")
        self.assertEqual(sent["body"]["messages"], turns)
        self.assertEqual(sent["body"]["max_tokens"], 99)
        self.assertEqual(sent["headers"]["x-api-key"], "k")

    def test_driver_failure_is_named_as_the_driver(self):
        cfg = {"id": "gpt-oss", "provider": "cerebras", "model": "m", "api_key": "k",
               "reasoning_effort": None, "url": "https://example.invalid"}
        response = _Response({})
        response.status_code = 401
        with mock.patch.object(callvatest.httpx, "post", lambda *a, **k: response):
            with self.assertRaises(callvatest.JudgeError) as failed:
                callvatest._model_call(cfg, "BRIEF", [], 10, role="driver")
        self.assertTrue(str(failed.exception).startswith("driver gpt-oss returned HTTP 401"))

    def test_judge_request_is_unchanged(self):
        cfg = {"id": "gpt-oss", "provider": "cerebras", "model": "gpt-oss-120b",
               "api_key": "k", "reasoning_effort": None,
               "url": "https://api.cerebras.ai/v1/chat/completions"}
        sent, post = self._capture({"choices": [{"message": {"content": "x"}}]})
        with mock.patch.object(callvatest.httpx, "post", post):
            callvatest._judge_call(cfg, "CLAIM")
        self.assertEqual(sent["body"]["messages"],
                         [{"role": "system", "content": callvatest._JUDGE_SYSTEM},
                          {"role": "user", "content": "CLAIM"}])
        self.assertEqual(sent["body"]["max_tokens"], callvatest.JUDGE_MAX_TOKENS)
        self.assertEqual(sent["timeout"], callvatest.JUDGE_TIMEOUT_S)


class ScenarioReportUnchanged(unittest.TestCase):
    def test_build_report_still_shapes_tool_runs_and_verdicts_as_before(self):
        obs = callvatest.Observed(text_only=True)
        evidence = _evidence("room-1", AGENT, obs, 0)
        report = callvatest.build_report({"id": "s", "_path": "p"}, {"id": "lab"}, AGENT,
                                         {"room_name": "room-1"}, obs, evidence, None, False)
        self.assertEqual(report["verdict"], "observed")
        self.assertEqual(report["checks"], [])
        self.assertEqual(report["tool_runs"][0]["result_shape"], ["slot"])
        self.assertNotIn("kind", report)


if __name__ == "__main__":
    unittest.main()
